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

test('history HEAD and conditional requests preserve status without a body', async () => {
  const f = fixture({ sha256: 'fixed', content_type: 'application/json', byte_length: 123 });
  const url = 'https://oapi.araihu.com' + prefix + 'file.json';
  const head = await worker.fetch(new Request(url, { method: 'HEAD' }), f.env, f.ctx);
  assert.equal(await head.text(), '');
  assert.equal(head.headers.get('Content-Length'), '123');
  const unmodified = await worker.fetch(new Request(url, { headers: { 'If-None-Match': '"fixed"' } }), f.env, f.ctx);
  assert.equal(unmodified.status, 304);
  assert.equal(await unmodified.text(), '');
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
