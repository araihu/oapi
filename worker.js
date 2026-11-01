import aliases from './aliases.json' with { type: 'json' };
import { reconcile } from './reconciliation.js';

export function immutablePath(encoded) {
  let path;
  try { path = decodeURIComponent(encoded); } catch { return null; }
  if (!path.startsWith('/') || /[\\\x00-\x1f\x7f]/.test(path)) return null;
  if (path.slice(1).split('/').some(part => ['', '.', '..'].includes(part))) return null;
  const immutable = /^\/catalogs\/[a-z][a-z0-9-]{0,24}\/snapshots\/snapshot-sha256-[0-9a-f]{64}\/.+$/;
  const directory = /^\/_manja\/search\/directory-sha256-[0-9a-f]{64}\.json$/;
  const brand = /^\/brand-assets\/[a-zA-Z0-9-]+-[0-9a-f]{12}\.[a-zA-Z0-9]+$/;
  return immutable.test(path) || directory.test(path) || brand.test(path) ? path.slice(1) : null;
}

export default {
  async scheduled(controller, env) {
    await reconcile(controller, env);
  },
  async fetch(request, env, ctx) {
    if (!['GET', 'HEAD'].includes(request.method)) {
      return new Response('Method not allowed', { status: 405, headers: { Allow: 'GET, HEAD' } });
    }
    const url = new URL(request.url);
    if (['/status.json', '/reconciliation.json'].includes(url.pathname)) {
      const key = url.pathname === '/status.json' ? 'health' : 'reconciliation';
      const row = await env.HISTORY.prepare('SELECT value FROM metadata WHERE key=?').bind(key).first();
      return new Response(request.method === 'HEAD' ? null : row?.value ?? (key === 'health' ? '{}' : '{"status":"not-run"}'), {
        headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' },
      });
    }
    if (Object.hasOwn(aliases, url.pathname)) {
      const mutable = /\/(next|latest)\/$/.test(url.pathname) || url.pathname.includes('/branches/');
      return new Response(null, { status: mutable ? 307 : 308, headers: {
        Location: aliases[url.pathname], 'Cache-Control': mutable ? 'no-store' : 'public, max-age=31536000, immutable',
      } });
    }
    // Cloudflare serves current static assets before invoking this Worker.
    // Only missing, explicitly immutable paths may fall back to durable history.
    const path = immutablePath(url.pathname);
    if (!path) return new Response(request.method === 'HEAD' ? null : 'Not found', { status: 404 });
    const cacheKey = new Request(url.origin + url.pathname);
    const cached = await caches.default.match(cacheKey);
    if (cached) {
      if (request.headers.get('If-None-Match') === cached.headers.get('ETag')) {
        return new Response(null, { status: 304, headers: cached.headers });
      }
      // The HTTP runtime omits HEAD bodies. Keep the representation here so
      // workerd retains its real Content-Length instead of calculating zero.
      return cached;
    }
    const row = await env.HISTORY.prepare(
      'SELECT b.sha256,b.gzip_base64,b.content_type,b.byte_length FROM paths p JOIN blobs b ON b.sha256=p.sha256 WHERE p.path=?',
    ).bind(path).first();
    if (!row) return new Response(request.method === 'HEAD' ? null : 'Not found', { status: 404 });
    const headers = {
      'Content-Type': row.content_type, 'Content-Length': String(row.byte_length),
      'Cache-Control': 'public, max-age=31536000, immutable, no-transform',
      'X-Content-Type-Options': 'nosniff', ETag: `"${row.sha256}"`,
    };
    if (request.headers.get('If-None-Match') === headers.ETag) return new Response(null, { status: 304, headers });
    const compressed = Uint8Array.from(atob(row.gzip_base64), c => c.charCodeAt(0));
    const body = new Blob([compressed]).stream().pipeThrough(new DecompressionStream('gzip'));
    // A known-length body is required: workerd ignores Content-Length for
    // generic streams and recalculates it as zero for a null HEAD body.
    const bytes = await new Response(body).arrayBuffer();
    const response = new Response(bytes, { headers });
    ctx.waitUntil(caches.default.put(cacheKey, response.clone()));
    return response;
  },
};
