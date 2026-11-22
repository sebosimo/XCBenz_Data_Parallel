"""Generate precompressed Brotli (.br) sidecars for browser-ready web exports.

Infomaniak Apache serves static .br sidecars via mod_rewrite when browsers send
Accept-Encoding: br and no Range header is present. Generating sidecars during
web export retention gives Brotli compression on forecast binaries and JSON
manifests without requiring Apache mod_brotli.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator


COMPRESSIBLE_EXTENSIONS = frozenset({".bin", ".xvt", ".json", ".geojson"})
TEXT_EXTENSIONS = frozenset({".json", ".geojson"})
LIVE_OWNED_SUBTREES = frozenset({
    "live_stations",
    "webcams",
    "radar_maps",
    "airspace",
    "fai_records",
    "satellite_cloud_maps",
    "w_forecast",
})
SKIPPED_FILENAMES = frozenset({
    # Emagram profiles.bin files are read via HTTP Range requests on raw float32 offsets.
    "profiles.bin",
})
RUN_TAG_RE = re.compile(r"^\d{8}_\d{4}$")
DEFAULT_MIN_BYTES = 1024
DEFAULT_BROTLI_QUALITY = 5
MIN_SAVINGS_RATIO = 0.95


@dataclass(frozen=True)
class PrecompressResult:
    encoder: str | None
    scanned_files: int
    compressed_files: int
    reused_files: int
    removed_orphans: int
    skipped_incompressible: int
    source_bytes: int
    compressed_bytes: int
    skipped_no_encoder: bool = False


def precompression_enabled() -> bool:
    raw = os.getenv("PRECOMPRESS_WEB_EXPORTS", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def is_run_scoped_path(relative: Path) -> bool:
    """Return True when a path sits under a YYYYMMDD_HHMM forecast run folder.

    Top-level mutable manifests (manifest.json, manifest.compact.json,
    locations.json, *_maps/manifest*.json, etc.) are never mtime-reused so a
    restored or hydrated file with an older mtime can never leave a stale .br
    sidecar behind.
    """
    return any(RUN_TAG_RE.fullmatch(part) for part in relative.parts[:-1])


def should_precompress_file(
    path: Path,
    root: Path,
    *,
    min_bytes: int = DEFAULT_MIN_BYTES,
    include_value_tiles: bool = False,
) -> bool:
    if not path.is_file() or path.name.endswith((".br", ".gz", ".tmp")):
        return False
    if path.name in SKIPPED_FILENAMES:
        return False
    if path.suffix.lower() not in COMPRESSIBLE_EXTENSIONS:
        return False
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    if relative.parts:
        top = relative.parts[0]
        if top in LIVE_OWNED_SUBTREES:
            return False
        if top == "value_tiles" and not include_value_tiles:
            return False
    return path.stat().st_size >= min_bytes


def _iter_managed_files(root: Path, *, include_value_tiles: bool) -> Iterator[Path]:
    """Walk web_exports while pruning live-owned subtrees and (by default) value_tiles."""
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        if current == root:
            dirnames[:] = sorted(
                name
                for name in dirnames
                if name not in LIVE_OWNED_SUBTREES
                and (include_value_tiles or name != "value_tiles")
            )
        else:
            dirnames.sort()
        for filename in sorted(filenames):
            yield current / filename


def _python_brotli_module():
    for module_name in ("brotli", "brotlicffi"):
        try:
            return __import__(module_name)
        except ImportError:
            continue
    return None


def detect_brotli_encoder() -> str | None:
    if _python_brotli_module() is not None:
        return "python-brotli"
    if shutil.which("brotli"):
        return "brotli-cli"
    if shutil.which("node"):
        return "node-zlib"
    return None


def decompress_brotli_bytes(payload: bytes) -> bytes:
    """Decode Brotli bytes using the first available backend."""
    mod = _python_brotli_module()
    if mod is not None:
        return mod.decompress(payload)
    if shutil.which("brotli"):
        completed = subprocess.run(
            ["brotli", "-d", "-c"],
            input=payload,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return completed.stdout
    if shutil.which("node"):
        completed = subprocess.run(
            [
                "node",
                "-e",
                "const zlib=require('node:zlib');const fs=require('node:fs');"
                "process.stdout.write(zlib.brotliDecompressSync(fs.readFileSync(0)));",
            ],
            input=payload,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return completed.stdout
    raise RuntimeError("No Brotli decoder available (install python brotli/brotlicffi, brotli CLI, or node)")


def _write_sidecar_atomic(sidecar: Path, payload: bytes) -> None:
    tmp = sidecar.with_name(sidecar.name + ".tmp")
    try:
        tmp.write_bytes(payload)
        os.replace(tmp, sidecar)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _compress_with_python_brotli(
    paths: list[Path],
    *,
    quality: int,
    verify: bool,
) -> tuple[int, int, int, int]:
    mod = _python_brotli_module()
    assert mod is not None
    compressed_files = 0
    skipped_incompressible = 0
    source_bytes = 0
    compressed_bytes = 0
    for path in paths:
        raw = path.read_bytes()
        mode = mod.MODE_TEXT if path.suffix.lower() in TEXT_EXTENSIONS else mod.MODE_GENERIC
        encoded = mod.compress(raw, mode=mode, quality=quality)
        if verify and mod.decompress(encoded) != raw:
            raise RuntimeError(f"Brotli round-trip verification failed for {path}")
        sidecar = Path(f"{path}.br")
        if len(encoded) < int(len(raw) * MIN_SAVINGS_RATIO):
            _write_sidecar_atomic(sidecar, encoded)
            compressed_files += 1
            source_bytes += len(raw)
            compressed_bytes += len(encoded)
        else:
            sidecar.unlink(missing_ok=True)
            skipped_incompressible += 1
    return compressed_files, skipped_incompressible, source_bytes, compressed_bytes


def _compress_with_brotli_cli(
    paths: list[Path],
    *,
    quality: int,
    verify: bool,
) -> tuple[int, int, int, int]:
    compressed_files = 0
    skipped_incompressible = 0
    source_bytes = 0
    compressed_bytes = 0
    for path in paths:
        raw_size = path.stat().st_size
        sidecar = Path(f"{path}.br")
        tmp = sidecar.with_name(sidecar.name + ".tmp")
        try:
            subprocess.run(
                ["brotli", "-f", f"-q{quality}", "-o", str(tmp), str(path)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            encoded_size = tmp.stat().st_size
            if verify:
                decoded = subprocess.run(
                    ["brotli", "-d", "-c", str(tmp)],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                ).stdout
                if decoded != path.read_bytes():
                    raise RuntimeError(f"Brotli round-trip verification failed for {path}")
            if encoded_size < int(raw_size * MIN_SAVINGS_RATIO):
                os.replace(tmp, sidecar)
                compressed_files += 1
                source_bytes += raw_size
                compressed_bytes += encoded_size
            else:
                tmp.unlink(missing_ok=True)
                sidecar.unlink(missing_ok=True)
                skipped_incompressible += 1
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
    return compressed_files, skipped_incompressible, source_bytes, compressed_bytes


_NODE_WORKER_SCRIPT = r"""
const fs = require("node:fs");
const zlib = require("node:zlib");
const readline = require("node:readline");

const quality = Number(process.argv[1] || 5);
const verify = process.argv[2] === "1";
const minRatio = Number(process.argv[3] || 0.95);
const textExts = new Set([".json", ".geojson"]);

let compressedFiles = 0;
let skippedIncompressible = 0;
let sourceBytes = 0;
let compressedBytes = 0;

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
rl.on("line", (line) => {
  if (!line) return;
  const filePath = JSON.parse(line);
  const raw = fs.readFileSync(filePath);
  const dot = filePath.lastIndexOf(".");
  const ext = dot >= 0 ? filePath.slice(dot).toLowerCase() : "";
  const mode = textExts.has(ext)
    ? zlib.constants.BROTLI_MODE_TEXT
    : zlib.constants.BROTLI_MODE_GENERIC;
  const encoded = zlib.brotliCompressSync(raw, {
    params: {
      [zlib.constants.BROTLI_PARAM_MODE]: mode,
      [zlib.constants.BROTLI_PARAM_QUALITY]: quality,
      [zlib.constants.BROTLI_PARAM_SIZE_HINT]: raw.length,
    },
  });
  if (verify) {
    const decoded = zlib.brotliDecompressSync(encoded);
    if (!raw.equals(decoded)) {
      throw new Error(`Brotli round-trip verification failed for ${filePath}`);
    }
  }
  const sidecar = `${filePath}.br`;
  if (encoded.length < Math.floor(raw.length * minRatio)) {
    const tmp = `${sidecar}.tmp`;
    fs.writeFileSync(tmp, encoded);
    fs.renameSync(tmp, sidecar);
    compressedFiles += 1;
    sourceBytes += raw.length;
    compressedBytes += encoded.length;
  } else {
    try { fs.unlinkSync(sidecar); } catch {}
    skippedIncompressible += 1;
  }
});

rl.on("close", () => {
  process.stdout.write(JSON.stringify({
    compressedFiles,
    skippedIncompressible,
    sourceBytes,
    compressedBytes,
  }));
});
"""


def _compress_with_node(
    paths: list[Path],
    *,
    quality: int,
    verify: bool,
) -> tuple[int, int, int, int]:
    payload = "\n".join(json.dumps(str(path)) for path in paths) + "\n"
    completed = subprocess.run(
        ["node", "-e", _NODE_WORKER_SCRIPT, str(quality), "1" if verify else "0", str(MIN_SAVINGS_RATIO)],
        input=payload,
        text=True,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    summary = json.loads(completed.stdout)
    return (
        int(summary["compressedFiles"]),
        int(summary["skippedIncompressible"]),
        int(summary["sourceBytes"]),
        int(summary["compressedBytes"]),
    )


def cleanup_precompressed_sidecars(root: Path, *, include_value_tiles: bool = False) -> int:
    """Remove all managed .br and leftover .br.tmp sidecars outside live-owned subtrees.

    Used when PRECOMPRESS_WEB_EXPORTS is disabled or no Brotli encoder is
    available so Apache never serves stale manifests pointing at deleted runs.
    """
    resolved_root = root.resolve()
    if not resolved_root.exists():
        return 0
    removed = 0
    for path in _iter_managed_files(resolved_root, include_value_tiles=include_value_tiles):
        if path.name.endswith((".br", ".br.tmp")) and path.is_file():
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def precompress_web_exports(
    root: Path,
    *,
    quality: int = DEFAULT_BROTLI_QUALITY,
    min_bytes: int = DEFAULT_MIN_BYTES,
    include_value_tiles: bool = False,
    verify: bool = False,
    encoder: str | None = None,
    require_encoder: bool = False,
) -> PrecompressResult:
    resolved_root = root.resolve()
    if not resolved_root.exists():
        return PrecompressResult(None, 0, 0, 0, 0, 0, 0, 0, skipped_no_encoder=False)

    chosen_encoder = encoder or detect_brotli_encoder()
    if not chosen_encoder:
        if require_encoder:
            raise RuntimeError(
                "No Brotli encoder available (install python brotli/brotlicffi, brotli CLI, or node)"
            )
        removed_all = cleanup_precompressed_sidecars(
            resolved_root, include_value_tiles=include_value_tiles
        )
        return PrecompressResult(
            encoder=None,
            scanned_files=0,
            compressed_files=0,
            reused_files=0,
            removed_orphans=removed_all,
            skipped_incompressible=0,
            source_bytes=0,
            compressed_bytes=0,
            skipped_no_encoder=True,
        )

    candidates: list[Path] = []
    reused_files = 0
    reused_source_bytes = 0
    reused_compressed_bytes = 0
    scanned_files = 0
    removed_orphans = 0

    for path in _iter_managed_files(resolved_root, include_value_tiles=include_value_tiles):
        if path.name.endswith(".br.tmp"):
            path.unlink(missing_ok=True)
            removed_orphans += 1
            continue
        if path.name.endswith(".br"):
            if not path.is_file():
                continue
            source = path.with_name(path.name[:-3])
            if not source.is_file() or not should_precompress_file(
                source, resolved_root, min_bytes=0, include_value_tiles=include_value_tiles
            ):
                path.unlink(missing_ok=True)
                removed_orphans += 1
            continue

        if not should_precompress_file(
            path,
            resolved_root,
            min_bytes=min_bytes,
            include_value_tiles=include_value_tiles,
        ):
            # If an eligible file shrunk below min_bytes, remove any stale sidecar.
            stale_sidecar = Path(f"{path}.br")
            if stale_sidecar.is_file():
                stale_sidecar.unlink(missing_ok=True)
                removed_orphans += 1
            continue
        scanned_files += 1
        sidecar = Path(f"{path}.br")
        relative = path.relative_to(resolved_root)
        if is_run_scoped_path(relative) and sidecar.is_file():
            src_stat = path.stat()
            br_stat = sidecar.stat()
            if br_stat.st_size > 0 and br_stat.st_mtime_ns >= src_stat.st_mtime_ns:
                reused_files += 1
                reused_source_bytes += src_stat.st_size
                reused_compressed_bytes += br_stat.st_size
                continue
        candidates.append(path)

    compressed_files = 0
    skipped_incompressible = 0
    new_source_bytes = 0
    new_compressed_bytes = 0
    if candidates:
        if chosen_encoder == "python-brotli":
            (
                compressed_files,
                skipped_incompressible,
                new_source_bytes,
                new_compressed_bytes,
            ) = _compress_with_python_brotli(candidates, quality=quality, verify=verify)
        elif chosen_encoder == "brotli-cli":
            (
                compressed_files,
                skipped_incompressible,
                new_source_bytes,
                new_compressed_bytes,
            ) = _compress_with_brotli_cli(candidates, quality=quality, verify=verify)
        elif chosen_encoder == "node-zlib":
            (
                compressed_files,
                skipped_incompressible,
                new_source_bytes,
                new_compressed_bytes,
            ) = _compress_with_node(candidates, quality=quality, verify=verify)
        else:
            raise ValueError(f"Unsupported Brotli encoder: {chosen_encoder}")

    return PrecompressResult(
        encoder=chosen_encoder,
        scanned_files=scanned_files,
        compressed_files=compressed_files,
        reused_files=reused_files,
        removed_orphans=removed_orphans,
        skipped_incompressible=skipped_incompressible,
        source_bytes=reused_source_bytes + new_source_bytes,
        compressed_bytes=reused_compressed_bytes + new_compressed_bytes,
        skipped_no_encoder=False,
    )


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "root",
        nargs="?",
        default=os.getenv("WEB_EXPORT_DIR", "web_exports"),
        help="Path to web_exports directory (default: WEB_EXPORT_DIR or web_exports)",
    )
    parser.add_argument("--quality", type=int, default=DEFAULT_BROTLI_QUALITY)
    parser.add_argument("--min-bytes", type=int, default=DEFAULT_MIN_BYTES)
    parser.add_argument(
        "--include-value-tiles",
        action="store_true",
        default=os.getenv("PRECOMPRESS_VALUE_TILES", "false").strip().lower() == "true",
    )
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--require-encoder", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    result = precompress_web_exports(
        Path(args.root),
        quality=args.quality,
        min_bytes=args.min_bytes,
        include_value_tiles=args.include_value_tiles,
        verify=args.verify,
        require_encoder=args.require_encoder,
    )
    if result.skipped_no_encoder:
        print(
            "[precompress] Skipped Brotli sidecars: no Brotli encoder found "
            f"(removed_sidecars={result.removed_orphans})",
            flush=True,
        )
        return 0
    saved_pct = (
        (1.0 - result.compressed_bytes / result.source_bytes) * 100.0
        if result.source_bytes
        else 0.0
    )
    print(
        f"[precompress] encoder={result.encoder} scanned={result.scanned_files} "
        f"compressed={result.compressed_files} reused={result.reused_files} "
        f"skipped={result.skipped_incompressible} orphans_removed={result.removed_orphans} "
        f"bytes={result.source_bytes}->{result.compressed_bytes} ({saved_pct:.1f}% saved)",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
