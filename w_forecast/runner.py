"""One pinned CH1/CH2 run, with bounded disk prefetch and serial decoding."""

import argparse
from contextlib import contextmanager
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import resource
import subprocess
import time

import requests

from .config import Config, MODELS, SECTION_ALTITUDES
from .feed import Feed, utc
from .geometry import decode_w, prepare
from .grib import atomic_json, check_space, download
from .prefetch import downloads


API = "https://data.geo.admin.ch/api/stac/v1"
COLLECTION = "ch.meteoschweiz.ogd-forecasting-icon-ch1"


def maximum_horizon(reference):
    return 45 if reference.hour == 3 else 33


class Source:
    def __init__(self, model="ch1"):
        self.session = requests.Session()
        self.model = MODELS[model]

    def json(self, method, endpoint, **kwargs):
        response = self.session.request(method, API + endpoint, timeout=(15, 45), **kwargs)
        if response.status_code != 200:
            raise RuntimeError(f"MeteoSwiss discovery returned HTTP {response.status_code}")
        return response.json()

    def static_urls(self):
        result = self.json("GET", f"/collections/{self.model.collection}/assets")
        assets = result["assets"]
        # The STAC collection-assets endpoint may return a list rather than a map.
        if isinstance(assets, list):
            assets = {a["id"]: a for a in assets}
        return {kind: assets[f"{kind}_constants_icon-{self.model.id}-eps.grib2"]["href"] for kind in ("horizontal", "vertical")}

    def w_url(self, reference, horizon):
        data = self.json("POST", "/search", json={
            "collections": [self.model.collection], "forecast:reference_datetime": utc(reference),
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
        cadence = self.model.cycle_hours
        nominal = now.replace(hour=now.hour // cadence * cadence, minute=0, second=0, microsecond=0)
        for slot in range(24 // cadence):
            reference = nominal - dt.timedelta(hours=slot * cadence)
            last = end if end is not None else self.model.maximum_horizon(reference)
            if self.w_url(reference, last):
                return reference
        raise RuntimeError(f"no complete {self.model.id.upper()} W run found inside the last 24 hours")


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
    try:
        head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
        status = subprocess.run(["git", "-C", str(root), "status", "--porcelain"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        head = status = None
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.name.encode() + b"\0" + path.read_bytes())
    archived_revision = root / "REVISION"
    fallback = archived_revision.read_text().strip() if archived_revision.is_file() else os.environ.get("W_SOURCE_REVISION", "unknown")
    commit = head.stdout.strip() if head is not None and head.returncode == 0 else fallback
    return {"git_commit": commit,
            "working_tree_dirty": bool(status.stdout.strip()) if status is not None else False, "implementation_sha256": digest.hexdigest()}


def run(args):
    started = time.monotonic()
    provenance = revision()
    model = MODELS[getattr(args, "model", "ch1")]
    packaging = getattr(args, "packaging", "whole-v1")
    config = Config(bbox=tuple(args.bbox), spacing=args.spacing or model.spacing,
                    altitudes=tuple(args.altitudes), section_altitudes=tuple(getattr(args, "section_altitudes", ())))
    work, output = Path(args.work_dir).resolve(), Path(args.output).resolve()
    if work == output or work.is_relative_to(output) or output.is_relative_to(work):
        raise ValueError("work and output must be separate directories")
    for path in (work, output):
        path.mkdir(parents=True, exist_ok=True)
        check_space(path, 1024**3)
    with writer_lock(work / ".writer.lock"), writer_lock(output / ".writer.lock"):
        source = Source(model.id)
        try:
            reference = source.latest(args.end) if args.run == "latest" else dt.datetime.strptime(args.run, "%Y%m%d_%H%M").replace(tzinfo=dt.timezone.utc)
            if reference.minute != 0 or reference.hour % model.cycle_hours:
                raise ValueError("reference time does not match the model's UTC cycle")
            end = model.maximum_horizon(reference) if args.end is None else args.end
            if not 0 <= args.start <= end <= model.maximum_horizon(reference):
                raise ValueError("horizon range is outside the selected model run")
            horizons = list(range(args.start, end + 1))
            print(f"{model.id.upper()} W {utc(reference)}, horizons {args.start} through {end}, {config.width}x{config.height}, {len(config.all_altitudes)} altitudes", flush=True)
            static_urls = source.static_urls()
            static_files, static_metadata = {}, {}
            for kind, url in static_urls.items():
                path = work / "cache" / f"{kind}_constants_icon-{model.id}-eps.grib2"
                static_metadata[kind] = download(url, path, session=source.session, reuse=True)
                static_files[kind] = path
            hashes = {k: v["sha256"] for k, v in static_metadata.items()}
            plan_path = work / "cache" / f"plan-{model.id}-{config.key}.npz"
            legacy_plan = work / "cache" / f"plan-{config.key}.npz"
            if model.id == "ch1" and not plan_path.exists() and legacy_plan.exists():
                # Its signature still verifies configuration and both static hashes.
                plan_path = legacy_plan
            plan, preparation = prepare(config, static_files["horizontal"], static_files["vertical"], plan_path, hashes)
            print(f"Interpolation plan: {'cached' if preparation['reused'] else 'built'} in {preparation['seconds']:.2f}s", flush=True)
            feed = Feed(output, config, model=model.id, packaging=packaging)
            existing = feed.existing(reference, hashes)
            checkpoint_path = work / "checkpoints" / f"{model.id}-{packaging}-{reference.strftime('%Y%m%d_%H%M')}-{config.key}.json"
            checkpoint_identity = {"reference_time": utc(reference), "config_id": config.key,
                                   "static_sha256": hashes, "output_root": str(output), "model": model.id, "packaging": packaging}
            if checkpoint_path.exists():
                checkpoint = json.loads(checkpoint_path.read_text())
                if checkpoint.get("identity") == checkpoint_identity:
                    existing.update(feed.verified_frames(checkpoint["frames"], reference))
            frames, timings, sources = [], [], {}
            missing = [h for h in horizons if not all((h, a) in existing for a in config.all_altitudes)]

            def fetch(horizon, path, cancelled):
                url = source.w_url(reference, horizon)
                if not url:
                    raise RuntimeError(f"W H+{horizon:03d} is not available; discovery pointer was preserved")
                return download(url, path, session=source.session, cancelled=cancelled)

            with downloads(missing, fetch, work / "downloads", prefetch=getattr(args, "prefetch", True)) as pending_downloads:
                for horizon in horizons:
                    if all((horizon, a) in existing for a in config.all_altitudes):
                        frames.extend(existing[(horizon, a)] for a in config.all_altitudes)
                        sample = existing[(horizon, config.altitudes[0])]
                        sources[str(horizon)] = sample["source_file"]
                        timings.append({"horizon": horizon, "resumed": True})
                        print(f"H+{horizon:03d}: reused verified frames", flush=True)
                        continue
                    step_start = time.monotonic()
                    path = None
                    try:
                        downloaded_horizon, path, fetched = next(pending_downloads)
                        if downloaded_horizon != horizon:
                            raise RuntimeError("W prefetch horizon mismatch")
                        fetch_wait_seconds = time.monotonic() - step_start
                        decoded_start = time.monotonic()
                        native, decoded = decode_w(path, plan, reference, horizon)
                        decode_seconds = time.monotonic() - decoded_start
                        source_file = {"sha256": fetched["sha256"], "bytes": fetched["bytes"]}
                        sources[str(horizon)] = source_file
                        export_start = time.monotonic()
                        for index, altitude in enumerate(config.all_altitudes):
                            values = plan.regrid(native[index])
                            frame = feed.frame(values, reference, horizon, altitude)
                            frame["source_file"] = source_file
                            frames.append(frame)
                        del native, values
                        checkpoint_path.parent.mkdir(exist_ok=True)
                        atomic_json(checkpoint_path, {"identity": checkpoint_identity, "frames": frames})
                        timings.append({"horizon": horizon, "resumed": False, **decoded,
                                        "download_seconds": fetched["download_seconds"], "download_bytes": fetched["bytes"],
                                        "fetch_wait_seconds": fetch_wait_seconds,
                                        "decode_seconds": decode_seconds, "export_seconds": time.monotonic() - export_start,
                                        "total_seconds": time.monotonic() - step_start})
                        print(f"H+{horizon:03d}: {decoded['decoded_levels']}/81 levels decoded, {timings[-1]['total_seconds']:.2f}s", flush=True)
                    finally:
                        if path is not None:
                            path.unlink(missing_ok=True)
            pointer = feed.publish(frames, reference, horizons, model.maximum_horizon(reference),
                                   {"provider": "MeteoSwiss Open Government Data", "attribution": "Source: MeteoSwiss",
                                    "documentation": "https://opendatadocs.meteoswiss.ch/e-forecast-data/e2-e3-numerical-weather-forecasting-model",
                                    "static_sha256": hashes, "grid_identity": plan.metadata["grid_identity"],
                                    "forecast_files": sources}, provenance, mode=getattr(args, "mode", "prototype_snapshot"))
            feed.prune(keep_runs=args.keep_runs)
            checkpoint_path.unlink(missing_ok=True)
            report = {"model": model.id, "packaging": packaging, "prefetch": getattr(args, "prefetch", True), "reference_time": utc(reference), "config_id": config.key, "static": static_metadata,
                      "plan": preparation, "steps": timings, "wall_seconds": time.monotonic() - started,
                      "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
                      "compressed_frame_bytes": sum(f["artifact"]["bytes"] for f in frames),
                      "decoded_frame_bytes": sum(f["artifact"]["decoded_bytes"] for f in frames),
                      "frames": len(frames), "pointer": pointer}
            atomic_json(output / "run-stats.json", report)
            print(json.dumps(report, sort_keys=True), flush=True)
            return report
        finally:
            source.session.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=tuple(MODELS), default="ch1")
    parser.add_argument("--mode", choices=("prototype_snapshot", "scheduled_forecast"), default="prototype_snapshot")
    parser.add_argument("--packaging", choices=("packed-v2", "whole-v1"), default="packed-v2")
    parser.add_argument("--prefetch", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--run", default="latest", help="latest, or YYYYMMDD_HHMM UTC")
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--end", type=int, default=None, help="default: complete selected model run")
    parser.add_argument("--work-dir", required=True, help="private static/geometry cache and temporary GRIB")
    parser.add_argument("--output", required=True, help="standalone public static feed directory")
    parser.add_argument("--bbox", nargs=4, type=float, default=Config().bbox, metavar=("WEST", "SOUTH", "EAST", "NORTH"))
    parser.add_argument("--spacing", type=float, default=None, help="default: CH1 0.01 degrees, CH2 0.02")
    parser.add_argument("--altitudes", nargs="+", type=int, default=Config().altitudes)
    parser.add_argument("--section-altitudes", nargs="*", type=int, default=SECTION_ALTITUDES,
                        help="Additional cross-section heights; pass without values to disable")
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
