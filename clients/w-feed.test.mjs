import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash, webcrypto } from 'node:crypto';
import { gzipSync } from 'node:zlib';
import { loadForecast, loadFrame } from './w-feed.mjs';

if (!globalThis.crypto) globalThis.crypto = webcrypto;
const sha = data => createHash('sha256').update(data).digest('hex');

function fixture(change = () => {}) {
  const raw = Buffer.alloc(8);
  [-123, -32768, 250, 0].forEach((v, i) => raw.writeInt16LE(v, i * 2));
  const compressed = gzipSync(raw);
  const objectHash = sha(compressed);
  const frame = {
    horizon_hours: 1, altitude_m: 3000, altitude_reference: 'AMSL', valid_time: '2026-10-24T01:00:00Z',
    artifact: { path: `objects/${objectHash}.bin.gz`, sha256: objectHash, bytes: compressed.length, decoded_bytes: 8, compression: 'gzip' },
  };
  const manifest = {
    schema: 'xcbenz.w.ch1.v1', reference_time: '2026-10-24T00:00:00Z',
    quantity: 'geometric_vertical_velocity', units: 'm/s', positive_direction: 'up',
    encoding: { dtype: 'int16', byte_order: 'little', scale_factor: .01, add_offset: 0, fill_value: -32768 },
    grid: { crs: 'EPSG:4326', width: 2, height: 2, layout: 'row-major', row_order: 'south_to_north', column_order: 'west_to_east' },
    coverage: { horizons_hours: [1] }, altitudes_m: [3000], frames: [frame],
  };
  change(manifest, compressed);
  const payload = Buffer.from(JSON.stringify(manifest));
  const hash = sha(payload);
  const pointer = { schema: manifest.schema, reference_time: manifest.reference_time, manifest: `runs/20261024_0000/${hash}.json`, sha256: hash, bytes: payload.length };
  const objects = new Map([
    ['latest.json', Buffer.from(JSON.stringify(pointer))], [pointer.manifest, payload], [frame.artifact.path, compressed],
  ]);
  const fetcher = async url => {
    const data = objects.get(new URL(url).pathname.slice(1));
    return new Response(data ?? '', { status: data ? 200 : 404 });
  };
  return { fetcher, objects, pointer, frame };
}

test('real gzip/int16 round trip preserves sign, zero, missing and row order', async () => {
  const { fetcher } = fixture();
  const forecast = await loadForecast('http://example.test/', { fetcher });
  const field = await loadFrame(forecast, 1, 3000, { fetcher });
  assert.equal(forecast.checksumVerified, true);
  assert.equal(field.checksumVerified, true);
  assert.equal(field.valueAt(0, 0), -1.23);
  assert.equal(field.valueAt(1, 0), null);
  assert.equal(field.valueAt(0, 1), 2.5);
  assert.equal(field.valueAt(1, 1), 0);
  assert.equal(field.valueAt(-1, 0), null);
  await assert.rejects(loadFrame(forecast, 3, 3000, { fetcher }), /unavailable/);
});

test('rejects unsafe object paths, invalid valid times and duplicate frames', async () => {
  for (const change of [
    m => { m.frames[0].artifact.path = '../private.bin.gz'; },
    m => { m.frames[0].valid_time = '2026-10-24T02:00:00Z'; },
    m => { m.frames.push(m.frames[0]); },
    m => { m.grid.row_order = 'north_to_south'; },
  ]) {
    const { fetcher } = fixture(change);
    await assert.rejects(loadForecast('http://example.test/', { fetcher }), /Invalid W feed/);
  }
});

test('rejects a corrupt frame before decoding', async () => {
  const { fetcher, objects, frame } = fixture();
  const forecast = await loadForecast('http://example.test/', { fetcher });
  const broken = Buffer.from(objects.get(frame.artifact.path));
  broken[broken.length - 1] ^= 1;
  objects.set(frame.artifact.path, broken);
  await assert.rejects(loadFrame(forecast, 1, 3000, { fetcher }), /SHA-256/);
});

test('rejects gzip that expands beyond its declared grid', async () => {
  const { fetcher } = fixture(m => {
    // Keep a valid declared 1x2 grid while supplying 2x2 compressed values.
    m.grid.height = 1;
    m.frames[0].artifact.decoded_bytes = 4;
  });
  const forecast = await loadForecast('http://example.test/', { fetcher });
  await assert.rejects(loadFrame(forecast, 1, 3000, { fetcher }), /exceeds grid/);
});
