import assert from 'node:assert/strict';
import { generateKeyPairSync, verify, createPrivateKey } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, statSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { appJWT, reconcile } from './reconciliation.js';

const { privateKey, publicKey } = generateKeyPairSync('rsa', { modulusLength: 2048 });
const pem = privateKey.export({ type: 'pkcs8', format: 'pem' });
const event = { cron: '*/5 * * * *', scheduledTime: Date.UTC(2026, 10, 1, 15) };
const workflow = '/repos/araihu/oapi/actions/workflows/publish.yml';

function fixture(previous = {}) {
  let stored = previous;
  const history = [];
  const env = {
    GITHUB_APP_ID: 'fixture-app', GITHUB_APP_PRIVATE_KEY: pem, GITHUB_APP_INSTALLATION_ID: '123',
    HISTORY: { prepare(sql) { return { bind(key, value) {
      assert.equal(key, 'reconciliation');
      return {
        async first() { return { value: JSON.stringify(stored) }; },
        async run() { stored = JSON.parse(value); history.push(structuredClone(stored)); },
      };
    } }; } },
  };
  const calls = [];
  async function request(url, options) {
    const path = new URL(url).pathname;
    calls.push({ path, url, ...options });
    if (path.includes('/access_tokens')) return Response.json({ token: 'sensitive-test-token' }, { status: 201 });
    assert.equal(options.headers.Authorization, 'Bearer sensitive-test-token');
    if (path.endsWith('/runs')) return Response.json({ total_count: 0, workflow_runs: [] });
    return new Response(null, { status: 204 });
  }
  return { env, calls, history, request, state: () => stored };
}

test('App JWT has a valid RS256 signature, bounded lifetime, issuer and clock allowance', async () => {
  const jwt = await appJWT('fixture-app', pem, event.scheduledTime);
  const [header, payload, signature] = jwt.split('.');
  assert.deepEqual(JSON.parse(Buffer.from(header, 'base64url')), { alg: 'RS256', typ: 'JWT' });
  const claims = JSON.parse(Buffer.from(payload, 'base64url'));
  assert.equal(claims.iss, 'fixture-app');
  assert.equal(claims.iat, event.scheduledTime / 1000 - 60);
  assert.equal(claims.exp - claims.iat, 600);
  assert.ok(verify('RSA-SHA256', Buffer.from(`${header}.${payload}`), publicKey, Buffer.from(signature, 'base64url')));
});

test('cron narrows token scope, dispatches canonical main without forcing a build, records health and revokes', async () => {
  const f = fixture();
  const result = await reconcile(event, f.env, f.request);
  assert.equal(result.status, 'dispatched');
  assert.equal(f.history[0].status, 'running');
  assert.deepEqual(JSON.parse(f.calls[0].body), { repositories: ['oapi'], permissions: { actions: 'write' } });
  const dispatch = f.calls.find(c => c.path.endsWith('/dispatches'));
  assert.equal(dispatch.path, workflow + '/dispatches');
  assert.deepEqual(JSON.parse(dispatch.body), {
    ref: 'main', inputs: { force: 'false', reconciliation_id: `cloudflare-cron-${event.scheduledTime}` },
  });
  assert.equal(f.calls.at(-1).method, 'DELETE');
  assert.equal(f.calls.at(-1).path, '/installation/token');
  assert.equal(f.state().scheduledAt, '2026-11-01T15:00:00.000Z');
  assert.ok(f.state().lastDispatchedAt);
  assert.ok(!JSON.stringify(f.state()).includes('sensitive-test-token'));
  assert.ok(!JSON.stringify(f.state()).includes('PRIVATE KEY'));
});

for (const busy of ['in_progress', 'queued', 'waiting', 'pending', 'requested']) {
  test(`an existing ${busy} publication is preserved without dispatching another run`, async () => {
    const f = fixture({ lastDispatchedAt: '2026-10-31T00:00:00Z' });
    const request = async (url, options) => {
      if (new URL(url).searchParams.get('status') === busy) {
        f.calls.push({ url, ...options });
        return Response.json({ workflow_runs: [{ html_url: 'https://github.com/araihu/oapi/actions/runs/123' }] });
      }
      return f.request(url, options);
    };
    await reconcile(event, f.env, request);
    assert.equal(f.state().status, 'busy');
    assert.equal(f.state().lastDispatchedAt, '2026-10-31T00:00:00Z');
    assert.ok(f.state().activeRun.endsWith('/123'));
    assert.ok(!f.calls.some(c => new URL(c.url).pathname.endsWith('/dispatches')));
    assert.equal(f.calls.at(-1).method, 'DELETE');
  });
}

for (const failedStage of ['mint', 'list', 'dispatch']) {
  test(`GitHub ${failedStage} failure records safe diagnostics, fails the invocation and revokes issued tokens`, async () => {
    const f = fixture({ lastDispatchedAt: '2026-10-31T00:00:00Z' });
    const request = async (url, options) => {
      const path = new URL(url).pathname;
      if ((failedStage === 'mint' && path.endsWith('/access_tokens')) ||
          (failedStage === 'list' && path.endsWith('/runs')) ||
          (failedStage === 'dispatch' && path.endsWith('/dispatches'))) {
        f.calls.push({ url, ...options });
        return new Response('sensitive-test-token PRIVATE KEY provider error', { status: 403 });
      }
      return f.request(url, options);
    };
    await assert.rejects(reconcile(event, f.env, request), /GitHub .* HTTP 403/);
    assert.equal(f.state().status, 'failed');
    assert.equal(f.state().lastDispatchedAt, '2026-10-31T00:00:00Z');
    assert.ok(!JSON.stringify(f.state()).includes('sensitive-test-token'));
    if (failedStage !== 'mint') assert.equal(f.calls.at(-1).method, 'DELETE');
    else assert.equal(f.calls.length, 1);
  });
}

test('missing or malformed credentials fail before any GitHub request and are never persisted', async () => {
  const f = fixture();
  f.env.GITHUB_APP_PRIVATE_KEY = '-----BEGIN PRIVATE KEY-----broken-secret';
  await assert.rejects(reconcile(event, f.env, f.request), /credentials are missing or invalid/);
  assert.equal(f.calls.length, 0);
  assert.equal(f.state().status, 'failed');
  assert.ok(!JSON.stringify(f.state()).includes('broken-secret'));
});

test('deployment accepts GitHub PKCS#1 input, writes private PKCS#8 JSON and refuses overwrites', () => {
  const directory = mkdtempSync(join(tmpdir(), 'oapi-secrets-fixture-'));
  const path = join(directory, 'secrets.json');
  const env = { ...process.env, ARAIHU_ASSETS_APP_ID: 'fixture-app', ARAIHU_ASSETS_APP_PRIVATE_KEY: privateKey.export({ type: 'pkcs1', format: 'pem' }) };
  try {
    execFileSync(process.execPath, ['.github/scripts/worker-secrets.mjs', path], { env });
    const data = JSON.parse(readFileSync(path));
    assert.equal(data.GITHUB_APP_ID, 'fixture-app');
    assert.ok(data.GITHUB_APP_PRIVATE_KEY.startsWith('-----BEGIN PRIVATE KEY-----'));
    assert.equal(createPrivateKey(data.GITHUB_APP_PRIVATE_KEY).asymmetricKeyType, 'rsa');
    assert.equal(statSync(path).mode & 0o777, 0o600);
    assert.throws(() => execFileSync(process.execPath, ['.github/scripts/worker-secrets.mjs', path], { env, stdio: 'pipe' }));
  } finally { rmSync(directory, { recursive: true, force: true }); }
});
