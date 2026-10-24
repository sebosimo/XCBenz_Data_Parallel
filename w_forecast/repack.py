"""Convert verified CH1 prototype snapshots to v2 without downloading model data."""

import argparse
import datetime as dt
import gzip
import json
from pathlib import Path
import time

import numpy as np

from .config import Config
from .feed import Feed, pack
from .grib import atomic_json, check_space, sha256_file
from .packing import read_block, read_directory
from .runner import revision, writer_lock


def repack(source_root, output):
    started = time.monotonic()
    source_root, output = Path(source_root).resolve(), Path(output).resolve()
    if source_root == output:
        raise ValueError("repack output must be separate from the source")
    output.mkdir(parents=True, exist_ok=True)
    check_space(output, 1024**3)
    pointer = json.loads((source_root / "latest.json").read_text())
    manifest_path = source_root / pointer["manifest"]
    if sha256_file(manifest_path) != pointer["sha256"]:
        raise ValueError("source manifest checksum mismatch")
    manifest = json.loads(manifest_path.read_text())
    if manifest["schema"] != "xcbenz.w.ch1.v1":
        raise ValueError("repack expects the original CH1 v1 contract")
    grid = manifest["grid"]
    config = Config(bbox=tuple(grid["bbox"]), spacing=grid["longitude_step"], altitudes=tuple(manifest["altitudes_m"]))
    reference = dt.datetime.fromisoformat(manifest["reference_time"].replace("Z", "+00:00"))
    total_old, frames = 0, []
    with writer_lock(output / ".writer.lock"):
        feed = Feed(output, config, packaging="packed-v2")
        verifier = Feed(source_root, config)
        verified = verifier.verified_frames(manifest["frames"], reference)
        if len(verified) != len(manifest["frames"]):
            raise ValueError("source frame validation failed")
        for old in manifest["frames"]:
            asset = old["artifact"]
            raw = gzip.decompress((source_root / asset["path"]).read_bytes())
            original = np.frombuffer(raw, dtype="<i2").reshape(config.height, config.width)
            values = original.astype(np.float32) * .01
            values[original == -32768] = np.nan
            if not np.array_equal(pack(values), original):
                raise ValueError("repacking would alter a quantized source value")
            frame = feed.frame(values, reference, old["horizon_hours"], old["altitude_m"])
            frame["source_file"] = old["source_file"]
            payload = (output / frame["artifact"]["path"]).read_bytes()
            directory, body = read_directory(payload)
            reconstructed = np.empty_like(original)
            for tile in directory["tiles"]:
                decoded = read_block(payload, tile["block"], body)
                x, y, w, h = (tile[k] for k in ("x", "y", "core_width", "core_height"))
                reconstructed[y:y+h, x:x+w] = decoded[1:h+1, 1:w+1]
            if not np.array_equal(original, reconstructed):
                raise ValueError("lossless tile round trip failed")
            frames.append(frame)
            total_old += asset["bytes"]
        feed.publish(frames, reference, manifest["coverage"]["horizons_hours"],
                     manifest["coverage"]["model_maximum_horizon_hours"], manifest["source"], revision())
        feed.prune()
    result = {"frames": len(frames), "old_bytes": total_old,
              "new_bytes": sum(f["artifact"]["bytes"] for f in frames),
              "all_detail_values_identical": True, "seconds": round(time.monotonic() - started, 3)}
    atomic_json(output / "packing-results.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    print(json.dumps(repack(args.input, args.output), sort_keys=True))


if __name__ == "__main__":
    main()
