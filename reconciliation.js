const API = 'https://api.github.com';
const ACTIONS = '/repos/araihu/oapi/actions';
const WORKFLOW = ACTIONS + '/workflows/publish.yml';
const QUEUED_WITHOUT_JOBS_MS = 60 * 60 * 1000;
const HEALTH_KEY = 'reconciliation';

class ReconciliationError extends Error {}

function base64url(bytes) {
  return btoa(String.fromCharCode(...new Uint8Array(bytes)))
    .replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '');
}

export async function appJWT(appID, privateKey, time = Date.now()) {
  if (!appID || !privateKey?.startsWith('-----BEGIN PRIVATE KEY-----')) {
    throw new ReconciliationError('GitHub App credentials are missing or invalid');
  }
  let key;
  try {
    const der = Uint8Array.from(atob(privateKey.replace(/-----[A-Z ]+-----|\s/g, '')), c => c.charCodeAt(0));
    key = await crypto.subtle.importKey('pkcs8', der, { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' }, false, ['sign']);
  } catch {
    throw new ReconciliationError('GitHub App credentials are missing or invalid');
  }
  const seconds = Math.floor(time / 1000);
  const encode = value => base64url(new TextEncoder().encode(JSON.stringify(value)));
  const unsigned = `${encode({ alg: 'RS256', typ: 'JWT' })}.${encode({ iss: appID, iat: seconds - 60, exp: seconds + 540 })}`;
  const signature = await crypto.subtle.sign('RSASSA-PKCS1-v1_5', key, new TextEncoder().encode(unsigned));
  return `${unsigned}.${base64url(signature)}`;
}

async function github(request, path, token, method = 'GET', body) {
  let response;
  try {
    response = await request(API + path, {
      method,
      headers: {
        Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'araihu-oapi-reconciler',
        ...(body ? { 'Content-Type': 'application/json' } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(15000),
      // Workers supports manual/follow, but rejects redirect: 'error'.
      // Treat every 3xx as failure below, without forwarding Authorization.
      redirect: 'manual',
    });
  } catch {
    throw new ReconciliationError(`GitHub ${method} request failed`);
  }
  if (!response.ok) throw new ReconciliationError(`GitHub ${method} returned HTTP ${response.status}`);
  return response;
}

async function isStaleQueuedRun(request, token, run, staleBefore) {
  if (!Number.isSafeInteger(run.id) || !(Date.parse(run.updated_at) <= staleBefore)) return false;
  const response = await github(request, `${ACTIONS}/runs/${run.id}/jobs?per_page=1`, token);
  const { total_count: count } = await response.json();
  if (!Number.isSafeInteger(count) || count < 0) {
    throw new ReconciliationError('GitHub returned an invalid workflow job count');
  }
  return count === 0;
}

export async function reconcile(controller, env, request = fetch) {
  const prior = await env.HISTORY.prepare('SELECT value FROM metadata WHERE key=?').bind(HEALTH_KEY).first();
  const previous = prior ? JSON.parse(prior.value) : {};
  const state = {
    source: 'cloudflare-cron', cron: controller.cron,
    scheduledAt: new Date(controller.scheduledTime).toISOString(),
    attemptedAt: new Date().toISOString(),
    attemptID: `cloudflare-cron-${controller.scheduledTime}`,
    lastDispatchedAt: previous.lastDispatchedAt ?? null,
  };
  async function persist(status, extra = {}) {
    Object.assign(state, extra, { status, checkedAt: new Date().toISOString() });
    await env.HISTORY.prepare('INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value')
      .bind(HEALTH_KEY, JSON.stringify(state)).run();
  }
  let token;
  try {
    await persist('running');
    if (!/^\d+$/.test(env.GITHUB_APP_INSTALLATION_ID ?? '')) {
      throw new ReconciliationError('GitHub App installation is missing or invalid');
    }
    const jwt = await appJWT(env.GITHUB_APP_ID, env.GITHUB_APP_PRIVATE_KEY);
    const response = await github(request, `/app/installations/${env.GITHUB_APP_INSTALLATION_ID}/access_tokens`, jwt, 'POST', {
      repositories: ['oapi'], permissions: { actions: 'write' },
    });
    ({ token } = await response.json());
    if (typeof token !== 'string' || !token) throw new ReconciliationError('GitHub did not issue an installation token');

    // Keep status filters so completed runs cannot hide an active publication.
    const staleBefore = Date.parse(state.attemptedAt) - QUEUED_WITHOUT_JOBS_MS;
    for (const status of ['in_progress', 'queued', 'waiting', 'pending', 'requested']) {
      const perPage = status === 'queued' ? 100 : 1;
      for (let page = 1; ; page++) {
        const response = await github(request, `${WORKFLOW}/runs?branch=main&status=${status}&per_page=${perPage}&page=${page}`, token);
        const runs = await response.json();
        if (!Array.isArray(runs.workflow_runs)) throw new ReconciliationError('GitHub returned an invalid workflow list');
        for (const run of runs.workflow_runs) {
          if (status === 'queued' && await isStaleQueuedRun(request, token, run, staleBefore)) {
            (state.ignoredQueuedRuns ??= []).push(run.id);
            continue;
          }
          await persist('busy', { activeRun: run.html_url });
          console.log(JSON.stringify({ event: 'spec-reconciliation', ...state }));
          return state;
        }
        if (!response.headers.get('link')?.includes('rel="next"')) break;
      }
    }
    await github(request, `${WORKFLOW}/dispatches`, token, 'POST', {
      ref: 'main', inputs: { force: 'false', reconciliation_id: state.attemptID },
    });
    await persist('dispatched', { lastDispatchedAt: new Date().toISOString() });
    console.log(JSON.stringify({ event: 'spec-reconciliation', ...state }));
    return state;
  } catch (error) {
    const message = error instanceof ReconciliationError ? error.message : 'Reconciliation failed';
    await persist('failed', { error: message });
    console.error(JSON.stringify({ event: 'spec-reconciliation-failed', ...state }));
    throw new Error(message);
  } finally {
    if (token) {
      try { await github(request, '/installation/token', token, 'DELETE'); }
      catch { console.warn('GitHub installation token revocation failed; the token expires automatically'); }
    }
  }
}
