import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createHash, webcrypto } from 'node:crypto';
import { loadForecast, loadSectionForecast, openFrame, openSectionFrame } from './w-packed.mjs';

if (!globalThis.crypto) globalThis.crypto = webcrypto;
const fixture = JSON.parse(readFileSync(new URL('../tests/fixtures/w_packing_v2.json', import.meta.url)));

function source({ ignoreRanges = false, corrupt = false, badRange = false, withSection = false, mixedSection = false } = {}) {
  const manifest = JSON.parse(fixture.manifest);
  const artifact = manifest.frames[0].artifact;
  const payload = Buffer.from(fixture.package, 'base64');
  if (corrupt) payload[payload.length - 1] ^= 1;
  const files = new Map([
    ['latest.json', Buffer.from(JSON.stringify(fixture.pointer))],
    [fixture.pointer.manifest, Buffer.from(fixture.manifest)],
    [artifact.path, payload],
  ]);
  if (withSection) {
    const detail = { ...manifest, altitudes_m: [100, 125, 150], frames: [100, 125, 150].map(altitude_m => ({ ...manifest.frames[0], altitude_m })) };
    if (mixedSection) detail.model = 'ICON-CH2-EPS';
    const bytes = Buffer.from(JSON.stringify(detail));
    const sha256 = createHash('sha256').update(bytes).digest('hex');
    const path = fixture.pointer.manifest.replace(/[a-f0-9]{64}\.json$/, `${sha256}.json`);
    files.set(path, bytes);
    manifest.cross_section = { path, sha256, bytes: bytes.length };
    const main = Buffer.from(JSON.stringify(manifest));
    const hash = createHash('sha256').update(main).digest('hex');
    const pointer = { ...fixture.pointer, sha256: hash, bytes: main.length, manifest: fixture.pointer.manifest.replace(/[a-f0-9]{64}\.json$/, `${hash}.json`) };
    files.set('latest.json', Buffer.from(JSON.stringify(pointer)));
    files.set(pointer.manifest, main);
  }
  async function fetcher(url, options = {}) {
    const body = files.get(new URL(url).pathname.slice(1));
    if (!body) return new Response('', { status: 404 });
    const match = /bytes=(\d+)-(\d+)/.exec(options.headers?.Range ?? '');
    if (!match || ignoreRanges) return new Response(body);
    const [start, end] = match.slice(1).map(Number);
    return new Response(body.subarray(start, end + 1), {
      status: 206, headers: { 'Content-Range': badRange ? 'bytes 0-1/2' : `bytes ${start}-${end}/${body.length}` },
    });
  }
  return { fetcher, artifact };
}

test('Python packages decode exactly in JavaScript, including halo and int16 extremes', async () => {
  const { fetcher } = source();
  const forecast = await loadForecast('http://example.test/', { fetcher });
  const field = await openFrame(forecast, 1, 100, { fetcher });
  assert.equal(field.checksumVerified, true);
  const { width, height, pattern } = fixture.expected;
  for (let y = 0; y < field.overview.height; y++) for (let x = 0; x < field.overview.width; x++) {
    assert.equal(field.overview.values[y * field.overview.width + x], pattern[((y * 4) * width + x * 4) % pattern.length]);
  }
  const tiles = await field.loadViewport([0, 0, 5.14, 2.58]);
  assert.equal(tiles.length, 6);
  for (const tile of tiles) for (let y = 0; y < tile.height; y++) for (let x = 0; x < tile.width; x++) {
    const gx = Math.max(0, Math.min(width - 1, tile.x + x - 1));
    const gy = Math.max(0, Math.min(height - 1, tile.y + y - 1));
    assert.equal(tile.values[y * tile.width + x], pattern[(gy * width + gx) % pattern.length]);
  }
});

test('viewport loading fetches only intersecting tiles and reuses decoded blocks', async () => {
  const { fetcher, artifact } = source();
  const field = await openFrame(await loadForecast('http://example.test/', { fetcher }), 1, 100, { fetcher });
  assert.equal(field.metrics.requests, 1);
  assert.ok(field.metrics.transferred_bytes < artifact.bytes);
  assert.equal((await field.loadViewport([1, 1, 1.01, 1.01])).length, 1);
  assert.equal(field.metrics.requests, 2);
  await field.loadViewport([1, 1, 1.01, 1.01]);
  assert.equal(field.metrics.requests, 2);
  assert.equal((await field.loadViewport([20, 20, 21, 21])).length, 0);
});

test('section discovery is lazy, pinned to the map run, and preserves map heights', async () => {
  const { fetcher } = source({ withSection: true });
  const requested = [];
  const tracked = async (url, options) => { requested.push(new URL(url).pathname); return fetcher(url, options); };
  const map = await loadForecast('http://example.test/', { fetcher: tracked });
  assert.equal(requested.length, 2);
  assert.deepEqual(map.manifest.altitudes_m, [100]);
  const detail = await loadSectionForecast(map, { fetcher: tracked });
  assert.equal(requested.length, 3);
  assert.deepEqual(detail.manifest.altitudes_m, [100, 125, 150]);
  assert.deepEqual(map.manifest.altitudes_m, [100]);
  assert.equal(detail.checksumVerified, true);
  const mixed = source({ withSection: true, mixedSection: true });
  await assert.rejects(loadSectionForecast(await loadForecast('http://example.test/', mixed), mixed), /run identity/);
  const legacy = await loadForecast('http://example.test/', { fetcher });
  delete legacy.manifest.cross_section;
  assert.equal(await loadSectionForecast(legacy, { fetcher: () => { throw new Error('unexpected request'); } }), legacy);
});

test('section frames skip overviews and fetch only sample-owning tiles, including edge halos', async () => {
  const { fetcher, artifact } = source();
  const field = await openSectionFrame(await loadForecast('http://example.test/', { fetcher }), 1, 100, { fetcher });
  assert.equal(field.overview, null);
  assert.equal(field.metrics.transferred_bytes, 16 + artifact.directory.bytes);
  const points = [{ lon: 0, lat: 0 }, { lon: 2.55, lat: 2.55 }, { lon: 2.56, lat: 2.56 }, { lon: 5.14, lat: 2.58 }, { lon: -1, lat: 0 }];
  const tiles = await field.loadPoints(points);
  assert.deepEqual(tiles.map(t => [t.x, t.y]), [[0, 0], [256, 256], [512, 256]]);
  assert.equal(field.metrics.requests, 4);
  await field.loadPoints(points);
  assert.equal(field.metrics.requests, 4);
  assert.equal((await field.loadPoints([{ lon: 20, lat: 20 }])).length, 0);
});

test('servers without ranges download the package once and then reuse it', async () => {
  const { fetcher, artifact } = source({ ignoreRanges: true });
  const field = await openFrame(await loadForecast('http://example.test/', { fetcher }), 1, 100, { fetcher });
  await field.loadViewport([0, 0, 5.14, 2.58]);
  assert.equal(field.metrics.requests, 1);
  assert.equal(field.metrics.transferred_bytes, artifact.bytes);
  assert.equal(field.metrics.range_supported, false);
});

test('range metadata and corrupt tile blocks fail validation', async () => {
  const wrong = source({ badRange: true });
  await assert.rejects(openFrame(await loadForecast('http://example.test/', wrong), 1, 100, wrong), /Content-Range/);
  const broken = source({ corrupt: true });
  const field = await openFrame(await loadForecast('http://example.test/', broken), 1, 100, broken);
  await assert.rejects(field.loadViewport([0, 0, 5.14, 2.58]), /checksum/);
});
