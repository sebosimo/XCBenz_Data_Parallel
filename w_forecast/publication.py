"""Publish verified packed W feeds using the shared Infomaniak v1 lease.

This module uses only the standard library and also runs on the remote host.
Uploads stay outside web_exports. The remote mutation guard covers the complete
short promotion, so a forecast swap or lease recovery cannot interrupt it.
"""

import argparse
from contextlib import contextmanager
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from urllib.error import HTTPError
from urllib.request import Request, urlopen


HASH = r"[0-9a-f]{64}"
MANIFEST = re.compile(r"runs/\d{8}_\d{4}/" + HASH + r"\.json")
OBJECT = re.compile(r"objects/" + HASH + r"\.wvp")
QUOTA = 250_000_000_000
RESERVE = 5 * 1024**3


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def verified(root, model):
    """Return the complete, allowlisted closure of a scheduled v2 pointer."""
    pointer = json.loads((root / "latest.json").read_bytes())
    relative = pointer["manifest"]
    if not MANIFEST.fullmatch(relative):
        raise ValueError("unsafe W manifest path")
    manifest_path = root / relative
    if (manifest_path.is_symlink() or manifest_path.stat().st_size != pointer["bytes"]
            or digest(manifest_path) != pointer["sha256"]
            or manifest_path.stem != pointer["sha256"]):
        raise ValueError("W manifest checksum mismatch")
    manifest = json.loads(manifest_path.read_bytes())
    reference = dt.datetime.fromisoformat(pointer["reference_time"].replace("Z", "+00:00"))
    maximum = 120 if model == "ch2" else 45 if reference.hour == 3 else 33
    hours = list(range(maximum + 1))
    if (pointer["schema"] != "xcbenz.w.v2" or manifest["schema"] != "xcbenz.w.v2"
            or manifest["model"] != "ICON-" + model.upper() + "-EPS"
            or manifest["reference_time"] != pointer["reference_time"]
            or manifest["operational_mode"] != "scheduled_forecast"
            or not manifest["coverage"]["full_model_run"]
            or manifest["coverage"]["horizons_hours"] != hours
            or manifest["coverage"]["model_maximum_horizon_hours"] != maximum
            or reference.strftime("%Y%m%d_%H%M") != relative.split("/")[1]):
        raise ValueError("W publication requires a complete scheduled model run")
    expected = {(hour, altitude) for hour in hours for altitude in manifest["altitudes_m"]}
    frames = manifest["frames"]
    if not expected or len(frames) != len(expected) or {
        (f["horizon_hours"], f["altitude_m"]) for f in frames
    } != expected:
        raise ValueError("incomplete or duplicate W frame coverage")
    files = {"latest.json", relative}
    for frame in frames:
        artifact = frame["artifact"]
        name = artifact["path"]
        valid = reference + dt.timedelta(hours=frame["horizon_hours"])
        if (not OBJECT.fullmatch(name) or Path(name).stem != artifact["sha256"]
                or frame["valid_time"] != valid.isoformat(timespec="seconds").replace("+00:00", "Z")):
            raise ValueError("unsafe or inconsistent W frame")
        path = root / name
        if name not in files and (path.is_symlink() or path.stat().st_size != artifact["bytes"]
                                  or digest(path) != artifact["sha256"]):
            raise ValueError("W object checksum mismatch")
        files.add(name)
    return pointer, manifest, files


@contextmanager
def lease(root):
    """Participate in v1, failing closed on any existing lease, including expired."""
    lock = root / ".xcbenz_web_exports_publish.lock"
    with (root / ".xcbenz_web_exports_publish.lock.guard").open("a") as guard:
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BlockingIOError("shared publication guard is busy") from None
        if lock.exists():
            raise BlockingIOError("shared publication lease is busy; retry next poll")
        owner = "w-forecast-" + uuid.uuid4().hex
        lock.mkdir()
        try:
            now = int(time.time())
            fields = dict(protocol_version=1, owner=owner, publisher="w-forecast",
                          host=socket.gethostname(), pid=os.getpid(), lease_seconds=300,
                          acquired_at_epoch=now, heartbeat_at=now,
                          acquired_at=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
            for name, value in fields.items():
                (lock / name).write_text(str(value) + "\n")
            yield
        finally:
            if (lock / "owner").exists() and (lock / "owner").read_text().strip() == owner:
                shutil.rmtree(lock)


def capacity(root, incoming, quota=QUOTA):
    # Account quota, not the hosting server's shared filesystem capacity.
    # Infomaniak mounts provider-managed backup snapshots below the home dir.
    # Stay on the active account filesystem instead of recounting remote NFS.
    result = subprocess.run(["nice", "-n", "19", "du", "-sxk", str(Path.home())], check=True,
                            capture_output=True, text=True, timeout=600)
    used = int(result.stdout.split()[0]) * 1024
    if used + incoming + RESERVE > quota:
        raise ValueError(f"Infomaniak quota preflight failed: used={used} incoming={incoming} quota={quota}")
    if shutil.disk_usage(root).free < incoming + RESERVE:
        raise ValueError("Infomaniak filesystem reserve would be crossed")
    print(json.dumps(dict(account_used_bytes=used, incoming_bytes=incoming, quota_bytes=quota)), flush=True)


def prune(root, now=None):
    """W-only retention: current, two newest runs, and six hours for readers."""
    cutoff = (time.time() if now is None else now) - 21600
    pointer = json.loads((root / "latest.json").read_bytes())
    directories = sorted((p for p in (root / "runs").iterdir()
                          if p.is_dir() and re.fullmatch(r"\d{8}_\d{4}", p.name)), reverse=True)
    reachable, expired = set(), []
    # Parse all retained manifests before deleting anything. Unknown files stay.
    for directory in directories:
        for path in directory.glob("*.json"):
            if not re.fullmatch(HASH + r"\.json", path.name):
                continue
            relative = path.relative_to(root).as_posix()
            if directory in directories[:2] or relative == pointer["manifest"] or path.stat().st_mtime >= cutoff:
                if digest(path) != path.stem:
                    raise ValueError("corrupt retained W manifest")
                for frame in json.loads(path.read_bytes())["frames"]:
                    name = frame["artifact"]["path"]
                    if not OBJECT.fullmatch(name):
                        raise ValueError("unsafe retained W object")
                    reachable.add(name)
            else:
                expired.append(path)
    expired.extend(p for p in (root / "objects").iterdir()
                   if OBJECT.fullmatch("objects/" + p.name)
                   and "objects/" + p.name not in reachable and p.stat().st_mtime < cutoff)
    removed = 0
    for path in expired[:5000]:
        size = path.stat().st_size
        if removed + size > 2 * 1024**3:
            break
        path.unlink()
        removed += size
    for directory in directories:
        if not any(directory.iterdir()):
            directory.rmdir()


def promote(root, stage, model):
    source = stage / model
    pointer, manifest, files = verified(source, model)
    target = root / "web_exports" / "w_forecast" / model
    with lease(root):
        if (target / "latest.json").exists():
            previous, old, _ = verified(target, model)
            if previous["reference_time"] > pointer["reference_time"]:
                raise ValueError("refusing W forecast downgrade")
            if previous["reference_time"] == pointer["reference_time"] and old["config_id"] != manifest["config_id"]:
                raise ValueError("refusing W configuration change within a run")
        target.mkdir(parents=True, exist_ok=True)
        # Immutable files arrive before the one mutable commit point. Rename on
        # this filesystem is atomic and leaves in-flight old readers intact.
        for name in sorted(files - {"latest.json"}):
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if digest(destination) != digest(source / name):
                    raise ValueError("existing immutable W artifact is corrupt")
            else:
                (source / name).chmod(0o644)
                os.replace(source / name, destination)
        policy = target.parent / ".htaccess"
        shutil.copyfile(stage / "w.htaccess", target.parent / ".htaccess.next")
        (target.parent / ".htaccess.next").chmod(0o644)
        os.replace(target.parent / ".htaccess.next", policy)
        (source / "latest.json").chmod(0o644)
        os.replace(source / "latest.json", target / "latest.json")
        verified(target, model)
        prune(target)
        # An interrupted SSH upload may leave a private staging directory. No
        # invocation can run six hours; remove only this publisher's old names.
        for abandoned in sorted(root.glob(".w-upload-*"))[:20]:
            if (abandoned != stage and not abandoned.is_symlink() and abandoned.is_dir()
                    and re.fullmatch(r"\.w-upload-[0-9a-f]{32}", abandoned.name)
                    and abandoned.stat().st_mtime < time.time() - 21600):
                entries, size = 0, 0
                for entry in abandoned.rglob("*"):
                    entries += 1
                    size += entry.lstat().st_size
                    if entries > 5000 or size > 2 * 1024**3:
                        break
                else:
                    shutil.rmtree(abandoned)
    print(json.dumps(dict(model=model, reference_time=pointer["reference_time"], published=True)), flush=True)


def public_pointer(base, model):
    try:
        with urlopen(Request(base + model + "/latest.json", headers={"Cache-Control": "no-cache"}), timeout=30) as response:
            if "no-cache" not in response.headers.get("Cache-Control", ""):
                raise ValueError("public W pointer must revalidate")
            return json.load(response)
    except HTTPError as error:
        if error.code == 404:
            return None
        raise


def verify_https(base, model, expected):
    pointer = public_pointer(base, model)
    fields = ("schema", "sha256", "manifest", "bytes", "reference_time", "operational_mode")
    if not pointer or any(pointer.get(name) != expected[name] for name in fields):
        raise ValueError("public HTTPS W pointer differs from candidate")
    with urlopen(base + model + "/" + pointer["manifest"], timeout=30) as response:
        payload = response.read()
    if len(payload) != pointer["bytes"] or hashlib.sha256(payload).hexdigest() != pointer["sha256"]:
        raise ValueError("public HTTPS W manifest checksum mismatch")
    manifest = json.loads(payload)
    for frame in (manifest["frames"][0], manifest["frames"][-1]):
        artifact = frame["artifact"]
        request = Request(base + model + "/" + artifact["path"], headers={
            "Range": "bytes=0-15", "Accept-Encoding": "gzip, br", "Origin": "https://xcbenz.com"})
        with urlopen(request, timeout=30) as response:
            if (response.status != 206 or response.headers.get("Content-Encoding")
                    or response.headers.get("Content-Range") != f"bytes 0-15/{artifact['bytes']}"
                    or response.headers.get("Access-Control-Allow-Origin") != "*"
                    or "content-range" not in response.headers.get("Access-Control-Expose-Headers", "").lower()
                    or (prefix := response.read(17))[:8] != b"XCBWVP2\0" or len(prefix) != 16):
                raise ValueError("public W byte ranges, encoding or CORS failed")
    print(json.dumps(dict(model=model, https_verified=True)), flush=True)


def publish(feed, model):
    """Snapshot verified source files, stage remotely, then promote under v1."""
    env = os.environ
    host, user, root = (env[n] for n in ("INFOMANIAK_HOST", "INFOMANIAK_USER", "INFOMANIAK_DATA_ROOT"))
    key, known = (env[n] for n in ("INFOMANIAK_SSH_KEY_PATH", "INFOMANIAK_KNOWN_HOSTS_PATH"))
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+", host) or not re.fullmatch(r"[a-zA-Z0-9_.-]+", user):
        raise ValueError("unsafe SSH endpoint")
    if not re.fullmatch(r"/?[a-zA-Z0-9_./-]+", root) or ".." in Path(root).parts:
        raise ValueError("unsafe remote data root")
    ssh = ["ssh", "-i", key, "-p", env.get("INFOMANIAK_PORT", "22"),
           "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
           "-o", "UserKnownHostsFile=" + known, "-o", "ConnectTimeout=10",
           "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"]
    target = user + "@" + host
    stage = root.rstrip("/") + "/.w-upload-" + uuid.uuid4().hex
    base = env.get("DATA_HOST_BASE_URL", "https://data.xcbenz.com").rstrip("/") + "/web_exports/w_forecast/"
    if not base.startswith("https://"):
        raise ValueError("public W feed requires HTTPS")

    def remote(*args, timeout=240):
        subprocess.run([*ssh, target, shlex.join(args)], check=True, timeout=timeout)

    # Keep hardlink snapshots on the source filesystem. They pin bytes without
    # copying hundreds of MB or keeping the producer lock during network I/O.
    with tempfile.TemporaryDirectory(prefix=".publish-", dir=feed) as temporary:
        snapshot = Path(temporary)
        with (feed / model / ".writer.lock").open("a") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            pointer, _, files = verified(feed / model, model)
            for name in files:
                destination = snapshot / model / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.link(feed / model / name, destination)
        current = public_pointer(base, model)
        if current and current["sha256"] == pointer["sha256"]:
            verify_https(base, model, pointer)
            print(json.dumps(dict(model=model, unchanged=True)), flush=True)
            return
        shutil.copyfile(__file__, snapshot / "publish.py")
        shutil.copyfile(Path(__file__).parent.parent / "deploy/w-prototype/infomaniak-w.htaccess", snapshot / "w.htaccess")
        incoming = sum(p.stat().st_size for p in snapshot.rglob("*") if p.is_file())
        try:
            remote("mkdir", "-p", stage)
            transport = shlex.join(ssh)
            subprocess.run(["rsync", "-rt", "-e", transport, str(snapshot / "publish.py"),
                            target + ":" + stage + "/publish.py"], check=True, timeout=60)
            remote("timeout", "630", "python3", stage + "/publish.py", "preflight",
                   "--root", root, "--incoming", str(incoming),
                   "--quota", env.get("DEPLOY_ACCOUNT_QUOTA_BYTES", str(QUOTA)), timeout=660)
            subprocess.run(["rsync", "-rt", "--chmod=D755,F644", "-e", transport,
                            str(snapshot) + "/", target + ":" + stage + "/"], check=True, timeout=600)
            remote("timeout", "210", "python3", stage + "/publish.py", "promote",
                   "--root", root, "--stage", stage, "--model", model)
            verify_https(base, model, pointer)
        finally:
            remote("rm", "-rf", "--", stage)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("publish", "preflight", "promote"))
    parser.add_argument("--root", type=Path)
    parser.add_argument("--stage", type=Path)
    parser.add_argument("--feed", type=Path)
    parser.add_argument("--model", choices=("ch1", "ch2"))
    parser.add_argument("--incoming", type=int)
    parser.add_argument("--quota", type=int, default=QUOTA)
    args = parser.parse_args()
    try:
        if args.action == "publish":
            publish(args.feed.resolve(), args.model)
        elif args.action == "preflight":
            capacity(args.root, args.incoming, args.quota)
        else:
            promote(args.root, args.stage, args.model)
    except BlockingIOError as error:
        print(str(error), flush=True)
        raise SystemExit(75)


if __name__ == "__main__":
    main()
