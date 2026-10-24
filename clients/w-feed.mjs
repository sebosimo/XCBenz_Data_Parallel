// Browser/Node ESM reference consumer. No framework or npm dependencies.
const SCHEMA = 'xcbenz.w.ch1.v1';

function requireValue(condition, message) {
  if (!condition) throw new Error(`Invalid W feed: ${message}`);
}

async function verifyHash(bytes, expected) {
  // HTTPS browsers and Node provide WebCrypto. Plain HTTP tailnet previews may not.
  if (!globalThis.crypto?.subtle) return false;
  const hash = new Uint8Array(await crypto.subtle.digest('SHA-256', bytes));
  const actual = Array.from(hash, n => n.toString(16).padStart(2, '0')).join('');
  requireValue(actual === expected, 'SHA-256 mismatch');
  return true;
}

async function get(url, fetcher, signal, cache = 'default') {
  const response = await fetcher(url, { signal, cache });
  if (!response.ok) throw new Error(`W feed HTTP ${response.status}`);
  return response;
}

export async function loadForecast(baseUrl, { fetcher = fetch, signal } = {}) {
  const root = new URL(baseUrl);
  requireValue(root.pathname.endsWith('/'), 'base URL needs a trailing slash');
  const pointer = await (await get(new URL('latest.json', root), fetcher, signal, 'no-store')).json();
  requireValue([SCHEMA, 'xcbenz.w.v2'].includes(pointer.schema), 'unsupported discovery schema');
  requireValue(/^runs\/\d{8}_\d{4}\/[a-f0-9]{64}\.json$/.test(pointer.manifest), 'unsafe manifest path');
  requireValue(pointer.manifest.endsWith(`/${pointer.sha256}.json`), 'manifest identity mismatch');
  const raw = await (await get(new URL(pointer.manifest, root), fetcher, signal)).arrayBuffer();
  requireValue(raw.byteLength === pointer.bytes, 'manifest byte count');
  const checksumVerified = await verifyHash(raw, pointer.sha256);
  const manifest = JSON.parse(new TextDecoder().decode(raw));
  requireValue(manifest.schema === pointer.schema, 'unsupported manifest schema');
  requireValue(manifest.reference_time === pointer.reference_time, 'mixed forecast runs');
  requireValue(manifest.quantity === 'geometric_vertical_velocity' && manifest.units === 'm/s'
    && manifest.positive_direction === 'up', 'unexpected quantity or units');
  requireValue(manifest.encoding.dtype === 'int16' && manifest.encoding.byte_order === 'little'
    && manifest.encoding.scale_factor === 0.01 && manifest.encoding.fill_value === -32768
    && manifest.encoding.add_offset === 0, 'unsupported encoding');
  const grid = manifest.grid;
  requireValue(Number.isSafeInteger(grid.width) && Number.isSafeInteger(grid.height)
    && grid.width > 0 && grid.height > 0 && grid.width * grid.height <= 5_000_000, 'grid size');
  requireValue(grid.crs === 'EPSG:4326' && grid.layout === 'row-major'
    && grid.row_order === 'south_to_north' && grid.column_order === 'west_to_east', 'grid orientation');
  const keys = new Set();
  for (const frame of manifest.frames) {
    const key = `${frame.horizon_hours}:${frame.altitude_m}`;
    requireValue(!keys.has(key) && manifest.coverage.horizons_hours.includes(frame.horizon_hours)
      && manifest.altitudes_m.includes(frame.altitude_m) && frame.altitude_reference === 'AMSL', 'frame coverage');
    keys.add(key);
    requireValue(Date.parse(frame.valid_time) === Date.parse(manifest.reference_time) + frame.horizon_hours * 3600000, 'frame valid time');
    const artifact = frame.artifact;
    const packed = manifest.schema === 'xcbenz.w.v2';
    requireValue(/^[a-f0-9]{64}$/.test(artifact.sha256)
      && artifact.path === `objects/${artifact.sha256}${packed ? '.wvp' : '.bin.gz'}`, 'unsafe frame path');
    requireValue(artifact.compression === (packed ? 'independent-gzip-blocks' : 'gzip')
      && artifact.decoded_bytes === grid.width * grid.height * 2, 'frame encoding');
    if (packed) requireValue(Number.isSafeInteger(artifact.directory.bytes) && artifact.directory.bytes > 0
      && artifact.directory.bytes <= 1024 * 1024 && artifact.directory.header_bytes === 16
      && Number.isSafeInteger(artifact.directory.initial_bytes)
      && artifact.directory.initial_bytes > 16 + artifact.directory.bytes
      && artifact.directory.initial_bytes <= artifact.bytes
      && /^[a-f0-9]{64}$/.test(artifact.directory.sha256), 'package directory');
  }
  requireValue(keys.size === manifest.coverage.horizons_hours.length * manifest.altitudes_m.length, 'incomplete frames');
  return { root, pointer, manifest, checksumVerified };
}

export async function loadFrame(forecast, horizon, altitude, { fetcher = fetch, signal } = {}) {
  requireValue(forecast.manifest.schema === SCHEMA, 'use w-packed.mjs for the packed-v2 feed');
  const frame = forecast.manifest.frames.find(f => f.horizon_hours === horizon && f.altitude_m === altitude);
  requireValue(frame, 'requested time/altitude is unavailable');
  const artifact = frame.artifact;
  const compressed = await (await get(new URL(artifact.path, forecast.root), fetcher, signal)).arrayBuffer();
  requireValue(compressed.byteLength === artifact.bytes, 'compressed byte count');
  const checksumVerified = await verifyHash(compressed, artifact.sha256);
  const reader = new Blob([compressed]).stream().pipeThrough(new DecompressionStream('gzip')).getReader();
  const output = new Uint8Array(artifact.decoded_bytes);
  let offset = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      requireValue(offset + value.length <= output.length, 'decompressed frame exceeds grid');
      output.set(value, offset);
      offset += value.length;
    }
  } finally {
    await reader.cancel();
  }
  requireValue(offset === output.length, 'truncated decoded frame');
  const values = new Int16Array(output.buffer);
  // Typed arrays use host endianness. Normalize on the uncommon big-endian host.
  if (new Uint8Array(new Uint16Array([1]).buffer)[0] !== 1) {
    const view = new DataView(output.buffer);
    for (let i = 0; i < values.length; i++) values[i] = view.getInt16(i * 2, true);
  }
  const { width, height } = forecast.manifest.grid;
  return {
    frame, values, width, height, checksumVerified,
    valueAt(x, y) {
      if (!Number.isInteger(x) || !Number.isInteger(y) || x < 0 || y < 0 || x >= width || y >= height) return null;
      const raw = values[y * width + x];
      return raw === -32768 ? null : raw * 0.01;
    },
  };
}
