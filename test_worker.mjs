import assert from 'node:assert/strict';
import { test } from 'node:test';
import { gzipSync } from 'node:zlib';
import worker, { immutablePath } from './worker.js';

const prefix = '/catalogs/example/snapshots/snapshot-sha256-' + 'a'.repeat(64) + '/';

function fixture(row) {
  const queries = [];
  const waiting = [];
  const env = { HISTORY: { prepare(sql) {
    return { bind(value) { queries.push({ sql, value }); return { async first() { return row; } }; } };
  } } };
  globalThis.caches = { default: { async match() { return undefined; }, async put() {} } };
  return { env, queries, ctx: { waitUntil(promise) { waiting.push(promise); } }, waiting };
}

test('history path handling decodes exactly once and rejects traversal/mutable paths', () => {
  const name = 'source-files/a %20?#é.json';
  assert.equal(immutablePath(prefix + encodeURIComponent(name).replace('%2F', '/')), (prefix + name).slice(1));
  assert.equal(immutablePath('/_manja/search/directory-sha256-' + 'b'.repeat(64) + '.json'), '_manja/search/directory-sha256-' + 'b'.repeat(64) + '.json');
  for (const path of [prefix + '%2e%2e/private', prefix + '%5cprivate', prefix + '%00', prefix + '%ZZ', '/index.html', '/_manja/search/directory.json']) assert.equal(immutablePath(path), null);
});

test('historical immutable GET restores exact bytes, headers, and cache entry', async () => {
  const content = Buffer.from('{"old":"publicação"}\n');
  const f = fixture({ sha256: 'fixed', gzip_base64: gzipSync(content).toString('base64'), content_type: 'application/json', byte_length: content.length });
  const response = await worker.fetch(new Request('https://oapi.araihu.com' + prefix + 'file.json'), f.env, f.ctx);
  assert.equal(response.status, 200);
  assert.deepEqual(Buffer.from(await response.arrayBuffer()), content);
  assert.match(response.headers.get('Cache-Control'), /immutable/);
  assert.equal(response.headers.get('Content-Length'), String(content.length));
  assert.equal(f.queries[0].value, (prefix + 'file.json').slice(1));
  await Promise.all(f.waiting);
});

test('history HEAD preserves representation metadata; conditional requests omit a body', async () => {
  const data = Buffer.from('{"historical":true}');
  const f = fixture({ sha256: 'fixed', gzip_base64: gzipSync(data).toString('base64'), content_type: 'application/json', byte_length: data.length });
  const url = 'https://oapi.araihu.com' + prefix + 'file.json';
  const head = await worker.fetch(new Request(url, { method: 'HEAD' }), f.env, f.ctx);
  // The HTTP server strips this body for HEAD, as native_smoke.py verifies.
  assert.equal(head.headers.get('Content-Length'), String(data.length));
  const unmodified = await worker.fetch(new Request(url, { headers: { 'If-None-Match': '"fixed"' } }), f.env, f.ctx);
  assert.equal(unmodified.status, 304);
  assert.equal(await unmodified.text(), '');
});

test('warm historical cache preserves HEAD length and conditional 304 responses', async () => {
  const f = fixture(null);
  caches.default.match = async () => new Response('original bytes', { headers: { ETag: '"cached"', 'Content-Length': '14' } });
  const url = 'https://oapi.araihu.com' + prefix + 'cached.json';
  const head = await worker.fetch(new Request(url, { method: 'HEAD' }), f.env, f.ctx);
  assert.equal(head.headers.get('Content-Length'), '14');
  const unmodified = await worker.fetch(new Request(url, { headers: { 'If-None-Match': '"cached"' } }), f.env, f.ctx);
  assert.equal(unmodified.status, 304);
  assert.equal(await unmodified.text(), '');
  assert.equal(f.queries.length, 0);
});

test('unknown and mutable paths never expose storage; missing immutable assets return 404', async () => {
  const f = fixture(null);
  for (const path of ['/private.json', '/catalogs/example/documents/old/index.html']) {
    assert.equal((await worker.fetch(new Request('https://oapi.araihu.com' + path), f.env, f.ctx)).status, 404);
  }
  assert.equal(f.queries.length, 0);
  assert.equal((await worker.fetch(new Request('https://oapi.araihu.com' + prefix + 'missing.json'), f.env, f.ctx)).status, 404);
});

test('health is read-only, uncached, and supports HEAD', async () => {
  const f = fixture({ value: '{"status":"unchanged"}' });
  const response = await worker.fetch(new Request('https://oapi.araihu.com/status.json'), f.env, f.ctx);
  assert.deepEqual(await response.json(), { status: 'unchanged' });
  assert.equal(response.headers.get('Cache-Control'), 'no-store');
  const head = await worker.fetch(new Request('https://oapi.araihu.com/status.json', { method: 'HEAD' }), f.env, f.ctx);
  assert.equal(await head.text(), '');
  const denied = await worker.fetch(new Request('https://oapi.araihu.com/status.json', { method: 'POST' }), f.env, f.ctx);
  assert.equal(denied.status, 405);
});

test('reconciliation health is isolated from publication health and read-only', async () => {
  const f = fixture({ value: '{"status":"busy","source":"cloudflare-cron"}' });
  const url = 'https://oapi.araihu.com/reconciliation.json';
  const response = await worker.fetch(new Request(url), f.env, f.ctx);
  assert.equal((await response.json()).status, 'busy');
  assert.equal(response.headers.get('Cache-Control'), 'no-store');
  assert.equal(f.queries[0].value, 'reconciliation');
  assert.equal((await worker.fetch(new Request(url, { method: 'POST' }), f.env, f.ctx)).status, 405);
  const empty = fixture(null);
  assert.deepEqual(await (await worker.fetch(new Request(url), empty.env, empty.ctx)).json(), { status: 'not-run' });
});
