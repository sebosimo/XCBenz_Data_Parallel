// Range-readable W v2 packages. Independent gzip blocks need no npm or WASM codec.
export { loadForecast, loadSectionForecast } from './w-feed.mjs';

function check(value, message) {
  if (!value) throw new Error(`Invalid packed W feed: ${message}`);
}

async function hash(bytes, expected) {
  if (!globalThis.crypto?.subtle) return false;
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', bytes));
  check(Array.from(digest, n => n.toString(16).padStart(2, '0')).join('') === expected, 'checksum');
  return true;
}

async function decode(compressed, block) {
  const reader = new Blob([compressed]).stream().pipeThrough(new DecompressionStream('gzip')).getReader();
  const bytes = new Uint8Array(block.decoded_bytes);
  let used = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      check(used + value.length <= bytes.length, 'decompression limit');
      bytes.set(value, used);
      used += value.length;
    }
  } finally {
    await reader.cancel();
  }
  check(used === bytes.length, 'truncated block');
  const count = block.width * block.height;
  const values = new Int16Array(count);
  for (let y = 0, i = 0; y < block.height; y++) {
    for (let x = 0; x < block.width; x++, i++) {
      const z = bytes[i] | (bytes[count + i] << 8);
      const delta = (z >>> 1) ^ -(z & 1);
      values[i] = delta + (x ? values[i - 1] : 0) + (y ? values[i - block.width] : 0)
        - (x && y ? values[i - block.width - 1] : 0);
    }
  }
  return { values, width: block.width, height: block.height };
}

export function openSectionFrame(forecast, horizon, altitude, options = {}) {
  return openFrame(forecast, horizon, altitude, { ...options, includeOverview: false });
}

export async function openFrame(forecast, horizon, altitude, { fetcher = fetch, signal, includeOverview = true } = {}) {
  check(forecast.manifest.schema === 'xcbenz.w.v2', 'schema');
  const frame = forecast.manifest.frames.find(f => f.horizon_hours === horizon && f.altitude_m === altitude);
  check(frame, 'unavailable time or altitude');
  const artifact = frame.artifact;
  const url = new URL(artifact.path, forecast.root);
  const metrics = { transferred_bytes: 0, requests: 0, range_supported: null };
  let whole = null;
  async function range(start, end) {
    check(Number.isSafeInteger(start) && Number.isSafeInteger(end) && start >= 0 && end >= start && end < artifact.bytes, 'range bounds');
    if (whole) return whole.slice(start, end + 1);
    const response = await fetcher(url, { signal, headers: { Range: `bytes=${start}-${end}` } });
    check(response.status === 200 || response.status === 206, `HTTP ${response.status}`);
    const data = new Uint8Array(await response.arrayBuffer());
    metrics.requests++;
    metrics.transferred_bytes += data.length;
    if (response.status === 200) {
      check(data.length === artifact.bytes, 'full package length');
      await hash(data, artifact.sha256);
      whole = data;
      metrics.range_supported = false;
      return whole.slice(start, end + 1);
    }
    check(data.length === end - start + 1 && response.headers.get('Content-Range') === `bytes ${start}-${end}/${artifact.bytes}`, 'Content-Range');
    metrics.range_supported = true;
    return data;
  }
  const prefix = await range(0, (includeOverview ? artifact.directory.initial_bytes : 16 + artifact.directory.bytes) - 1);
  const view = new DataView(prefix.buffer, prefix.byteOffset, prefix.byteLength);
  check(new TextDecoder().decode(prefix.slice(0, 8)) === 'XCBWVP2\0'
    && view.getUint32(8, true) === 2 && view.getUint32(12, true) === artifact.directory.bytes, 'header');
  const body = 16 + artifact.directory.bytes;
  const index = prefix.slice(16, body);
  const checksumVerified = await hash(index, artifact.directory.sha256);
  const directory = JSON.parse(new TextDecoder().decode(index));
  const grid = forecast.manifest.grid;
  check(directory.schema === 'xcbenz.w.tiles.v2' && directory.width === grid.width && directory.height === grid.height
    && directory.tile_size === 256 && directory.halo === 1 && directory.compression === 'gzip'
    && directory.transform === 'delta2d-zigzag-byteplanes-v1' && directory.overview.stride === 4, 'directory contract');
  const blocks = [directory.overview.block, ...directory.tiles.map(t => t.block)];
  let offset = 0;
  for (const block of blocks) {
    check(Number.isSafeInteger(block.width) && Number.isSafeInteger(block.height) && block.width > 0 && block.height > 0
      && block.width * block.height * 2 === block.decoded_bytes && block.decoded_bytes <= 2 * 1024 * 1024
      && block.offset === offset && Number.isSafeInteger(block.bytes) && block.bytes > 0
      && /^[a-f0-9]{64}$/.test(block.sha256), 'block contract');
    offset += block.bytes;
  }
  check(body + offset === artifact.bytes && body + directory.overview.block.bytes === artifact.directory.initial_bytes, 'package length');
  check(directory.overview.block.width === Math.ceil(grid.width / 4)
    && directory.overview.block.height === Math.ceil(grid.height / 4), 'overview grid');
  let tileIndex = 0;
  for (let y = 0; y < grid.height; y += 256) for (let x = 0; x < grid.width; x += 256) {
    const t = directory.tiles[tileIndex++];
    check(t && t.x === x && t.y === y && t.core_width === Math.min(256, grid.width - x)
      && t.core_height === Math.min(256, grid.height - y)
      && t.block.width === t.core_width + 2 && t.block.height === t.core_height + 2, 'tile grid or halo');
  }
  check(tileIndex === directory.tiles.length, 'extra tiles');
  const cache = new Map();
  async function load(block) {
    if (!cache.has(block.offset)) {
      const promise = (async () => {
        const start = body + block.offset;
        const data = start + block.bytes <= prefix.length ? prefix.slice(start, start + block.bytes) : await range(start, start + block.bytes - 1);
        await hash(data, block.sha256);
        return decode(data, block);
      })();
      cache.set(block.offset, promise);
      if (cache.size > 32) cache.delete(cache.keys().next().value);
      promise.catch(() => cache.delete(block.offset));
    }
    return cache.get(block.offset);
  }
  const overview = includeOverview ? { ...await load(directory.overview.block), stride: 4 } : null;
  async function loadTile(tile) {
    check(directory.tiles.includes(tile), 'unknown tile');
    return { ...tile, ...await load(tile.block) };
  }
  async function loadViewport(bbox, concurrency = 4) {
    check(Number.isInteger(concurrency) && concurrency >= 1, 'viewport concurrency');
    const [west, south, east, north] = bbox;
    check(bbox.length === 4 && bbox.every(Number.isFinite) && west <= east && south <= north, 'viewport bbox');
    const x0 = Math.floor((west - grid.longitude_start) / grid.longitude_step);
    const x1 = Math.ceil((east - grid.longitude_start) / grid.longitude_step);
    const y0 = Math.floor((south - grid.latitude_start) / grid.latitude_step);
    const y1 = Math.ceil((north - grid.latitude_start) / grid.latitude_step);
    const pending = directory.tiles.filter(t => t.x <= x1 && t.x + t.core_width - 1 >= x0 && t.y <= y1 && t.y + t.core_height - 1 >= y0);
    return loadTiles(pending, concurrency);
  }
  async function loadPoints(points, concurrency = 2) {
    check(Number.isInteger(concurrency) && concurrency >= 1, 'point concurrency');
    const columns = Math.ceil(grid.width / 256);
    const keys = new Set();
    for (const { lon, lat } of points) {
      const x = (lon - grid.longitude_start) / grid.longitude_step;
      const y = (lat - grid.latitude_start) / grid.latitude_step;
      if (Number.isFinite(x) && Number.isFinite(y) && x >= 0 && y >= 0 && x <= grid.width - 1 && y <= grid.height - 1) {
        keys.add(Math.floor(Math.floor(y) / 256) * columns + Math.floor(Math.floor(x) / 256));
      }
    }
    // The owning tile has a one-cell halo for all four bilinear neighbours.
    return loadTiles(directory.tiles.filter(t => keys.has(Math.floor(t.y / 256) * columns + Math.floor(t.x / 256))), concurrency);
  }
  async function loadTiles(pending, concurrency) {
    const output = new Array(pending.length);
    let next = 0;
    const workers = Array.from({ length: Math.min(4, Math.max(1, concurrency), pending.length) }, async () => {
      while (next < pending.length) {
        const i = next++;
        output[i] = await loadTile(pending[i]);
      }
    });
    await Promise.all(workers);
    return output;
  }
  return { frame, directory, overview, loadTile, loadViewport, loadPoints, metrics, checksumVerified };
}
