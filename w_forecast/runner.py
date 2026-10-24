"""One pinned CH1 run, processed serially into a standalone static feed."""

import argparse
from contextlib import contextmanager
import datetime as dt
import fcntl
import hashlib
import json
from pathlib import Path
import resource
import subprocess
import time

import requests

from .config import Config
from .feed import Feed, utc
from .geometry import decode_w, prepare
from .grib import atomic_json, check_space, download


API = "https://data.geo.admin.ch/api/stac/v1"
COLLECTION = "ch.meteoschweiz.ogd-forecasting-icon-ch1"


def maximum_horizon(reference):
    return 45 if reference.hour == 3 else 33


class Source:
    def __init__(self):
        self.session = requests.Session()

    def json(self, method, endpoint, **kwargs):
        response = self.session.request(method, API + endpoint, timeout=(15, 45), **kwargs)
        if response.status_code != 200:
            raise RuntimeError(f"MeteoSwiss discovery returned HTTP {response.status_code}")
        return response.json()

    def static_urls(self):
        result = self.json("GET", f"/collections/{COLLECTION}/assets")
        assets = result["assets"]
        # The STAC collection-assets endpoint may return a list rather than a map.
        if isinstance(assets, list):
            assets = {a["id"]: a for a in assets}
        return {kind: assets[f"{kind}_constants_icon-ch1-eps.grib2"]["href"] for kind in ("horizontal", "vertical")}

    def w_url(self, reference, horizon):
        data = self.json("POST", "/search", json={
            "collections": [COLLECTION], "forecast:reference_datetime": utc(reference),
            "forecast:variable": "W", "forecast:perturbed": False,
            "forecast:horizon": f"P{horizon // 24}DT{horizon % 24:02d}H00M00S",
            "limit": 2,
        })
        features = data.get("features", [])
        if not features:
            return None
        if len(features) != 1:
            raise ValueError("ambiguous W asset discovery")
        feature = features[0]
        properties = feature["properties"]
        if (properties["forecast:variable"] != "W" or properties["forecast:perturbed"] is not False
                or properties["forecast:reference_datetime"] != utc(reference)):
            raise ValueError("STAC returned a different parameter, member or reference time")
        assets = list(feature["assets"].values())
        if len(assets) != 1:
            raise ValueError("ambiguous W source file")
        return assets[0]["href"]

    def latest(self, end=None):
        now = dt.datetime.now(dt.timezone.utc)
        nominal = now.replace(hour=now.hour // 3 * 3, minute=0, second=0, microsecond=0)
        for slot in range(8):
            reference = nominal - dt.timedelta(hours=slot * 3)
            last = end if end is not None else maximum_horizon(reference)
            if self.w_url(reference, last):
                return reference
        raise RuntimeError("no complete CH1 W run found inside the last 24 hours")


@contextmanager
def writer_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another W prototype writer already holds this lock") from None
        yield


def revision():
    root = Path(__file__).resolve().parents[1]
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
    status = subprocess.run(["git", "-C", str(root), "status", "--porcelain"], capture_output=True, text=True, timeout=5)
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.name.encode() + b"\0" + path.read_bytes())
    return {"git_commit": head.stdout.strip() if head.returncode == 0 else "unknown",
            "working_tree_dirty": bool(status.stdout.strip()), "implementation_sha256": digest.hexdigest()}


def run(args):
    started = time.monotonic()
    provenance = revision()
    config = Config(bbox=tuple(args.bbox), spacing=args.spacing, altitudes=tuple(args.altitudes))
    work, output = Path(args.work_dir).resolve(), Path(args.output).resolve()
    if work == output or work.is_relative_to(output) or output.is_relative_to(work):
        raise ValueError("work and output must be separate directories")
    for path in (work, output):
        path.mkdir(parents=True, exist_ok=True)
        check_space(path, 1024**3)
    with writer_lock(work / ".writer.lock"), writer_lock(output / ".writer.lock"):
        source = Source()
        try:
            reference = source.latest(args.end) if args.run == "latest" else dt.datetime.strptime(args.run, "%Y%m%d_%H%M").replace(tzinfo=dt.timezone.utc)
            if reference.minute != 0 or reference.hour % 3:
                raise ValueError("CH1 reference time must be a three-hourly UTC cycle")
            end = maximum_horizon(reference) if args.end is None else args.end
            if not 0 <= args.start <= end <= maximum_horizon(reference):
                raise ValueError("horizon range is outside the selected model run")
            horizons = list(range(args.start, end + 1))
            print(f"CH1 W {utc(reference)}, horizons {args.start} through {end}, {config.width}x{config.height}, {len(config.altitudes)} altitudes", flush=True)
            static_urls = source.static_urls()
            static_files, static_metadata = {}, {}
            for kind, url in static_urls.items():
                path = work / "cache" / f"{kind}_constants_icon-ch1-eps.grib2"
                static_metadata[kind] = download(url, path, session=source.session, reuse=True)
                static_files[kind] = path
            hashes = {k: v["sha256"] for k, v in static_metadata.items()}
            plan, preparation = prepare(config, static_files["horizontal"], static_files["vertical"], work / "cache" / f"plan-{config.key}.npz", hashes)
            print(f"Interpolation plan: {'cached' if preparation['reused'] else 'built'} in {preparation['seconds']:.2f}s", flush=True)
            feed = Feed(output, config)
            existing = feed.existing(reference, hashes)
            checkpoint_path = work / "checkpoints" / f"{reference.strftime('%Y%m%d_%H%M')}-{config.key}.json"
            checkpoint_identity = {"reference_time": utc(reference), "config_id": config.key,
                                   "static_sha256": hashes, "output_root": str(output)}
            if checkpoint_path.exists():
                checkpoint = json.loads(checkpoint_path.read_text())
                if checkpoint.get("identity") == checkpoint_identity:
                    existing.update(feed.verified_frames(checkpoint["frames"], reference))
            frames, timings, sources = [], [], {}
            for horizon in horizons:
                if all((horizon, a) in existing for a in config.altitudes):
                    frames.extend(existing[(horizon, a)] for a in config.altitudes)
                    sample = existing[(horizon, config.altitudes[0])]
                    sources[str(horizon)] = sample["source_file"]
                    timings.append({"horizon": horizon, "resumed": True})
                    print(f"H+{horizon:03d}: reused verified frames", flush=True)
                    continue
                url = source.w_url(reference, horizon)
                if not url:
                    raise RuntimeError(f"W H+{horizon:03d} is not available; discovery pointer was preserved")
                path = work / "W.grib2"
                step_start = time.monotonic()
                try:
                    fetched = download(url, path, session=source.session)
                    decoded_start = time.monotonic()
                    native, decoded = decode_w(path, plan, reference, horizon)
                    decode_seconds = time.monotonic() - decoded_start
                    source_file = {"sha256": fetched["sha256"], "bytes": fetched["bytes"]}
                    sources[str(horizon)] = source_file
                    export_start = time.monotonic()
                    for index, altitude in enumerate(config.altitudes):
                        values = plan.regrid(native[index])
                        frame = feed.frame(values, reference, horizon, altitude)
                        frame["source_file"] = source_file
                        frames.append(frame)
                    del native, values
                    checkpoint_path.parent.mkdir(exist_ok=True)
                    atomic_json(checkpoint_path, {"identity": checkpoint_identity, "frames": frames})
                    timings.append({"horizon": horizon, "resumed": False, **decoded,
                                    "download_seconds": fetched["download_seconds"], "download_bytes": fetched["bytes"],
                                    "decode_seconds": decode_seconds, "export_seconds": time.monotonic() - export_start,
                                    "total_seconds": time.monotonic() - step_start})
                    print(f"H+{horizon:03d}: {decoded['decoded_levels']}/81 levels decoded, {timings[-1]['total_seconds']:.2f}s", flush=True)
                finally:
                    path.unlink(missing_ok=True)
            pointer = feed.publish(frames, reference, horizons, maximum_horizon(reference),
                                   {"provider": "MeteoSwiss Open Government Data", "attribution": "Source: MeteoSwiss",
                                    "documentation": "https://opendatadocs.meteoswiss.ch/e-forecast-data/e2-e3-numerical-weather-forecasting-model",
                                    "static_sha256": hashes, "grid_identity": plan.metadata["grid_identity"],
                                    "forecast_files": sources}, provenance)
            feed.prune(keep_runs=args.keep_runs)
            checkpoint_path.unlink(missing_ok=True)
            report = {"reference_time": utc(reference), "config_id": config.key, "static": static_metadata,
                      "plan": preparation, "steps": timings, "wall_seconds": time.monotonic() - started,
                      "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
                      "compressed_frame_bytes": sum(f["artifact"]["bytes"] for f in frames),
                      "decoded_frame_bytes": sum(f["artifact"]["decoded_bytes"] for f in frames),
                      "frames": len(frames), "pointer": pointer}
            atomic_json(output / "benchmark.json", report)
            print(json.dumps(report, sort_keys=True), flush=True)
            return report
        finally:
            source.session.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", default="latest", help="latest, or YYYYMMDD_HHMM UTC")
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--end", type=int, default=None, help="default: all 33 hours, or 45 for 03Z")
    parser.add_argument("--work-dir", required=True, help="private static/geometry cache and temporary GRIB")
    parser.add_argument("--output", required=True, help="standalone public static feed directory")
    parser.add_argument("--bbox", nargs=4, type=float, default=Config().bbox, metavar=("WEST", "SOUTH", "EAST", "NORTH"))
    parser.add_argument("--spacing", type=float, default=Config().spacing)
    parser.add_argument("--altitudes", nargs="+", type=int, default=Config().altitudes)
    parser.add_argument("--keep-runs", type=int, default=2)
    args = parser.parse_args()
    if args.keep_runs < 2:
        parser.error("keep-runs must be at least two")
    try:
        run(args)
    except requests.RequestException as exc:
        parser.exit(1, f"W prototype network failure: {type(exc).__name__}. Discovery pointer preserved.\n")
    except (ValueError, RuntimeError, OSError) as exc:
        parser.exit(1, f"W prototype failed: {exc}\n")


if __name__ == "__main__":
    main()
