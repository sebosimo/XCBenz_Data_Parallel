"""Generate and validate xcbenz spatial value tiles contract v1."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import struct
import uuid
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np


CONTRACT = "xcbenz-spatial-value-tiles"
CONTRACT_VERSION = "1.0.0"
PACKAGE = "immutable-chunks-cloud-dual-v1"
CAPABILITY_STATUS = "dual_publish"
FALLBACK = "whole_grid_split_binary_v1"
HALO = 1
BASE_HEADER = struct.Struct("<4sBBHHHHBBHHHHHHIIIII")
SECTION_HEADER = struct.Struct("<HBBIII")


@dataclass(frozen=True)
class GridSpec:
    id: str
    width: int
    height: int
    coordinate_scale: int
    lon_origin: int
    lat_origin: int
    lon_step: int
    lat_step: int
    core_width: int
    core_height: int

    @property
    def tiles_x(self) -> int:
        return math.ceil(self.width / self.core_width)

    @property
    def tiles_y(self) -> int:
        return math.ceil(self.height / self.core_height)

    @property
    def payload_width(self) -> int:
        return self.core_width + 2 * HALO

    @property
    def payload_height(self) -> int:
        return self.core_height + 2 * HALO

    def contract_payload(self) -> dict[str, Any]:
        return {
            "projection": "EPSG:4326",
            "coordinate_scale": self.coordinate_scale,
            "width": self.width,
            "height": self.height,
            "lon": {"origin": self.lon_origin, "step": self.lon_step, "direction": "east"},
            "lat": {"origin": self.lat_origin, "step": self.lat_step, "direction": "north"},
            "storage_order": "row_major_y_then_x",
            "cell_reference": "center",
        }

    def tile_matrix_payload(self) -> dict[str, Any]:
        return {
            "core_width": self.core_width,
            "core_height": self.core_height,
            "halo": HALO,
            "tiles_x": self.tiles_x,
            "tiles_y": self.tiles_y,
            "tile_order": "y_then_x",
            "url_template": "{step}/t{tile_y}_{tile_x}.xvt",
        }


FINE_GRID = GridSpec(
    id="icon_ch_common_safe_002deg_v1",
    width=781,
    height=381,
    coordinate_scale=100_000,
    lon_origin=80_000,
    lat_origin=4_240_000,
    lon_step=2_000,
    lat_step=2_000,
    core_width=160,
    core_height=112,
)
LEGACY_FINE_GRID = GridSpec(
    id="alps_002deg_v1",
    width=626,
    height=291,
    coordinate_scale=100_000,
    lon_origin=400_000,
    lat_origin=4_300_000,
    lon_step=2_000,
    lat_step=2_000,
    core_width=160,
    core_height=112,
)
WIND_GRID = GridSpec(
    id="icon_ch_common_safe_004deg_v1",
    width=391,
    height=191,
    coordinate_scale=100_000,
    lon_origin=80_000,
    lat_origin=4_240_000,
    lon_step=4_000,
    lat_step=4_000,
    core_width=80,
    core_height=56,
)
LEGACY_WIND_GRID = GridSpec(
    id="alps_004deg_v1",
    width=313,
    height=146,
    coordinate_scale=100_000,
    lon_origin=400_000,
    lat_origin=4_300_000,
    lon_step=4_000,
    lat_step=4_000,
    core_width=80,
    core_height=56,
)
FINE_GRIDS = (FINE_GRID, LEGACY_FINE_GRID)
WIND_GRIDS = (WIND_GRID, LEGACY_WIND_GRID)
SUPPORTED_GRIDS = (*FINE_GRIDS, *WIND_GRIDS)


@dataclass(frozen=True)
class ChannelSpec:
    id: int
    name: str
    encoding_id: int
    component_count: int
    format: str
    dtype: str
    missing: int
    scale: float | None = None
    offset: float | None = None
    units: tuple[str, ...] = ()

    @property
    def is_cloud(self) -> bool:
        return self.encoding_id == 4

    @property
    def cell_bytes(self) -> int:
        return self.component_count

    @property
    def missing_bytes(self) -> bytes:
        return bytes((self.missing & 0xFF,)) * self.component_count

    def contract_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "channel_id": self.id,
            "channel": self.name,
            "encoding_id": self.encoding_id,
            "format": self.format,
            "dtype": self.dtype,
            "component_count": self.component_count,
            "missing": self.missing,
        }
        if self.scale is not None:
            payload["scale"] = self.scale
        if self.offset is not None:
            payload["offset"] = self.offset
        if self.units:
            payload["units"] = list(self.units)
        if self.is_cloud:
            payload.update(
                {
                    "bits_per_value": 4,
                    "nibble_order": "even_cell_low_nibble_odd_cell_high_nibble",
                    "valid_codes": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
                }
            )
        return payload


CHANNELS = {
    "wind_uv": ChannelSpec(
        1,
        "wind_uv",
        1,
        2,
        "int8-interleaved-u-v",
        "int8",
        -128,
        0.25,
        0.0,
        ("m/s", "m/s"),
    ),
    "sunrain_code": ChannelSpec(
        2,
        "sunrain_code",
        2,
        1,
        "uint8-semantic-sunrain-code",
        "uint8",
        0,
        units=("code",),
    ),
    "rain": ChannelSpec(
        3,
        "rain",
        3,
        1,
        "uint8-interleaved-components",
        "uint8",
        255,
        0.2,
        0.0,
        ("mm",),
    ),
    "cloud_total": ChannelSpec(4, "cloud_total", 4, 1, "packed-uint4-cloud-cover", "uint4", 15),
    "cloud_low": ChannelSpec(5, "cloud_low", 4, 1, "packed-uint4-cloud-cover", "uint4", 15),
    "cloud_mid": ChannelSpec(6, "cloud_mid", 4, 1, "packed-uint4-cloud-cover", "uint4", 15),
    "cloud_high": ChannelSpec(7, "cloud_high", 4, 1, "packed-uint4-cloud-cover", "uint4", 15),
}
CHANNELS_BY_ID = {channel.id: channel for channel in CHANNELS.values()}
CLOUD_VARIANTS = ("total", "low", "mid", "high")


@dataclass(frozen=True)
class StepSource:
    label: str
    horizon: int
    valid_time: str
    paths: tuple[Path, ...]


@dataclass(frozen=True)
class VariantSource:
    model: str
    run: str
    product: str
    variant: str
    grid: GridSpec
    channels: tuple[ChannelSpec, ...]
    source_metadata: tuple[str, ...]
    steps: tuple[StepSource, ...]

    @property
    def key(self) -> str:
        return f"{self.product}/{self.variant}"


@dataclass(frozen=True)
class ParsedSection:
    channel: ChannelSpec
    offset: int
    byte_length: int
    decoded_cell_count: int
    payload: bytes


@dataclass(frozen=True)
class ParsedTile:
    flags: int
    tile_x: int
    tile_y: int
    core_width: int
    core_height: int
    valid_core_width: int
    valid_core_height: int
    payload_width: int
    payload_height: int
    grid_width: int
    grid_height: int
    sections: tuple[ParsedSection, ...]


def canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False).encode(
        "utf-8"
    )


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def write_json(path: Path, payload: Any, *, pretty: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if pretty:
        encoded = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    else:
        encoded = canonical_json_bytes(payload).decode("utf-8")
    path.write_text(encoded, encoding="utf-8")


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _require_within(path: Path, root: Path, label: str) -> Path:
    resolved = path.resolve()
    resolved_root = root.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ValueError(f"{label} escapes its allowed root: {resolved}")
    return resolved


def _remove_tree(path: Path, allowed_root: Path) -> None:
    target = _require_within(path, allowed_root, "recursive-delete target")
    root = allowed_root.resolve()
    if target == root:
        raise ValueError(f"refusing to delete the allowed root itself: {root}")
    if path.exists():
        shutil.rmtree(path)


def pack_cloud_codes(values: Sequence[int], pad_code: int = 15) -> bytes:
    if not 0 <= pad_code <= 15:
        raise ValueError("Cloud padding code must fit uint4")
    source = (
        np.frombuffer(values, dtype="u1")
        if isinstance(values, (bytes, bytearray, memoryview))
        else np.asarray(values, dtype="u1")
    )
    if np.any(source > 15):
        raise ValueError("cloud codes must fit uint4")
    if source.size % 2:
        padded = np.empty(source.size + 1, dtype="u1")
        padded[:-1] = source
        padded[-1] = pad_code
        source = padded
    packed = source[0::2] | (source[1::2] << 4)
    return packed.tobytes()


def unpack_cloud_codes(payload: bytes, cell_count: int) -> bytes:
    expected = (cell_count + 1) // 2
    if len(payload) != expected:
        raise ValueError(f"packed Cloud payload has {len(payload)} bytes, expected {expected}")
    if cell_count % 2 and payload and payload[-1] >> 4 != 15:
        raise ValueError("packed Cloud payload has a non-missing odd-cell padding nibble")
    packed = np.frombuffer(payload, dtype="u1")
    result = np.empty(cell_count, dtype="u1")
    result[0::2] = packed[: (cell_count + 1) // 2] & 0x0F
    result[1::2] = packed[: cell_count // 2] >> 4
    return result.tobytes()


def _valid_core(grid: GridSpec, tile_x: int, tile_y: int) -> tuple[int, int]:
    if not 0 <= tile_x < grid.tiles_x or not 0 <= tile_y < grid.tiles_y:
        raise ValueError(f"tile ({tile_x}, {tile_y}) is outside {grid.tiles_x}x{grid.tiles_y} matrix")
    return (
        min(grid.core_width, grid.width - tile_x * grid.core_width),
        min(grid.core_height, grid.height - tile_y * grid.core_height),
    )


def _extract_window(decoded: bytes, grid: GridSpec, channel: ChannelSpec, tile_x: int, tile_y: int) -> bytes:
    payload_width = grid.payload_width
    payload_height = grid.payload_height
    x_start = tile_x * grid.core_width - HALO
    y_start = tile_y * grid.core_height - HALO
    cell_bytes = channel.cell_bytes
    expected = grid.width * grid.height * cell_bytes
    if len(decoded) != expected:
        raise ValueError(f"{channel.name} decoded length is {len(decoded)}, expected {expected}")
    result = bytearray(channel.missing_bytes * (payload_width * payload_height))
    copy_x0 = max(0, x_start)
    copy_x1 = min(grid.width, x_start + payload_width)
    if copy_x1 <= copy_x0:
        return bytes(result)
    copy_cells = copy_x1 - copy_x0
    out_x = copy_x0 - x_start
    for out_y in range(payload_height):
        source_y = y_start + out_y
        if not 0 <= source_y < grid.height:
            continue
        source_offset = (source_y * grid.width + copy_x0) * cell_bytes
        output_offset = (out_y * payload_width + out_x) * cell_bytes
        length = copy_cells * cell_bytes
        result[output_offset : output_offset + length] = decoded[source_offset : source_offset + length]
    return bytes(result)


def encode_xvt(
    grid: GridSpec,
    tile_x: int,
    tile_y: int,
    channels: Sequence[tuple[ChannelSpec, bytes]],
) -> bytes:
    if not channels:
        raise ValueError("an XVT tile needs at least one channel")
    if len({channel.id for channel, _values in channels}) != len(channels):
        raise ValueError("XVT channel ids must be unique")
    valid_width, valid_height = _valid_core(grid, tile_x, tile_y)
    section_payloads: list[bytes] = []
    for channel, decoded in channels:
        window = _extract_window(decoded, grid, channel, tile_x, tile_y)
        section_payloads.append(pack_cloud_codes(window) if channel.is_cloud else window)
    payload = b"".join(section_payloads)
    header_bytes = BASE_HEADER.size + SECTION_HEADER.size * len(channels)
    x_start = tile_x * grid.core_width - HALO
    y_start = tile_y * grid.core_height - HALO
    has_padding = (
        x_start < 0
        or y_start < 0
        or x_start + grid.payload_width > grid.width
        or y_start + grid.payload_height > grid.height
    )
    flags = (1 if has_padding else 0) | (2 if len(channels) > 1 else 0)
    base = BASE_HEADER.pack(
        b"XVT1",
        1,
        0,
        header_bytes,
        flags,
        tile_x,
        tile_y,
        HALO,
        len(channels),
        grid.core_width,
        grid.core_height,
        valid_width,
        valid_height,
        grid.payload_width,
        grid.payload_height,
        grid.width,
        grid.height,
        len(payload),
        zlib.crc32(payload) & 0xFFFFFFFF,
        0,
    )
    directory = bytearray()
    offset = 0
    decoded_cell_count = grid.payload_width * grid.payload_height
    for (channel, _decoded), section in zip(channels, section_payloads):
        directory.extend(
            SECTION_HEADER.pack(
                channel.id,
                channel.encoding_id,
                channel.component_count,
                offset,
                len(section),
                decoded_cell_count,
            )
        )
        offset += len(section)
    return base + bytes(directory) + payload


def parse_xvt(payload: bytes) -> ParsedTile:
    if len(payload) < BASE_HEADER.size:
        raise ValueError("XVT payload is shorter than the base header")
    (
        magic,
        major,
        minor,
        header_bytes,
        flags,
        tile_x,
        tile_y,
        halo,
        section_count,
        core_width,
        core_height,
        valid_width,
        valid_height,
        payload_width,
        payload_height,
        grid_width,
        grid_height,
        declared_payload_bytes,
        declared_crc,
        reserved,
    ) = BASE_HEADER.unpack_from(payload)
    if magic != b"XVT1" or major != 1 or minor != 0:
        raise ValueError("unsupported XVT magic or version")
    if halo != HALO or not section_count:
        raise ValueError("invalid XVT halo or section count")
    expected_header = BASE_HEADER.size + section_count * SECTION_HEADER.size
    if header_bytes != expected_header or header_bytes > len(payload):
        raise ValueError("invalid XVT header length")
    body = payload[header_bytes:]
    if len(body) != declared_payload_bytes:
        raise ValueError("XVT payload byte length mismatch")
    if zlib.crc32(body) & 0xFFFFFFFF != declared_crc:
        raise ValueError("XVT payload CRC-32 mismatch")
    if reserved != 0 or flags & ~0x03:
        raise ValueError("XVT reserved header fields are non-zero")
    if valid_width > core_width or valid_height > core_height:
        raise ValueError("XVT valid core exceeds configured core")
    sections: list[ParsedSection] = []
    expected_offset = 0
    expected_cells = payload_width * payload_height
    for index in range(section_count):
        entry = SECTION_HEADER.unpack_from(payload, BASE_HEADER.size + index * SECTION_HEADER.size)
        channel_id, encoding_id, component_count, offset, byte_length, decoded_cell_count = entry
        channel = CHANNELS_BY_ID.get(channel_id)
        if channel is None:
            raise ValueError(f"XVT has unknown channel id {channel_id}")
        if encoding_id != channel.encoding_id or component_count != channel.component_count:
            raise ValueError(f"XVT channel declaration does not match {channel.name}")
        if offset != expected_offset or offset + byte_length > len(body):
            raise ValueError("XVT section ranges are not contiguous")
        if decoded_cell_count != expected_cells:
            raise ValueError("XVT section decoded cell count mismatch")
        expected_length = (expected_cells + 1) // 2 if channel.is_cloud else expected_cells * channel.cell_bytes
        if byte_length != expected_length:
            raise ValueError(f"XVT {channel.name} section length mismatch")
        section_payload = body[offset : offset + byte_length]
        if channel.is_cloud:
            unpack_cloud_codes(section_payload, expected_cells)
        sections.append(
            ParsedSection(channel, offset, byte_length, decoded_cell_count, section_payload)
        )
        expected_offset += byte_length
    if expected_offset != len(body):
        raise ValueError("XVT sections do not cover the payload")
    if bool(flags & 2) != (section_count > 1):
        raise ValueError("XVT grouped-section flag is inconsistent")
    return ParsedTile(
        flags,
        tile_x,
        tile_y,
        core_width,
        core_height,
        valid_width,
        valid_height,
        payload_width,
        payload_height,
        grid_width,
        grid_height,
        tuple(sections),
    )


def _decoded_section(section: ParsedSection) -> bytes:
    if section.channel.is_cloud:
        return unpack_cloud_codes(section.payload, section.decoded_cell_count)
    return section.payload


def capability_declaration() -> dict[str, Any]:
    return {
        "contract": CONTRACT,
        "contract_version": CONTRACT_VERSION,
        "package": PACKAGE,
        "status": CAPABILITY_STATUS,
        "manifest": "web_exports/value_tiles/v1/manifest.json",
        "fallback": FALLBACK,
        "requires_range": False,
    }


def value_tiles_enabled(env: dict[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return str(source.get("ENABLE_VALUE_TILES", "false")).strip().lower() in {"1", "true", "yes", "on"}


def parse_value_tile_run_selection(raw: str | None) -> set[tuple[str, str]] | None:
    if raw is None or not raw.strip():
        return None
    selected: set[tuple[str, str]] = set()
    for item in raw.split(","):
        model, separator, run = item.strip().partition("=")
        if separator != "=" or model not in {"icon-ch1", "icon-ch2"} or not re.fullmatch(r"\d{8}_\d{4}", run):
            raise ValueError(
                "value-tile run selection must contain comma-separated "
                f"icon-ch1=YYYYMMDD_HHMM or icon-ch2=YYYYMMDD_HHMM entries, got {item!r}"
            )
        key = (model, run)
        if key in selected:
            raise ValueError(f"duplicate value-tile run selection {model}={run}")
        selected.add(key)
    return selected


def remove_value_tile_publication(web_root: Path) -> None:
    web_root = Path(web_root)
    value_tiles_root = web_root / "value_tiles"
    if value_tiles_root.exists():
        _remove_tree(value_tiles_root, web_root)


def _relative_web_url(web_root: Path, path: Path) -> str:
    return (Path("web_exports") / path.relative_to(web_root)).as_posix()


def _resolve_web_url(web_root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    if path.parts and path.parts[0] in {"web_exports", web_root.name}:
        return web_root.joinpath(*path.parts[1:])
    return path


def _axis_integer(grid_payload: dict[str, Any], axis: str, field: str, scale: int) -> int:
    value = (grid_payload.get(axis) or {}).get(field)
    if value is None:
        raise ValueError(f"grid {axis}.{field} is missing")
    return round(float(value) * scale)


def _validate_source_metadata(path: Path, grid: GridSpec, channel: ChannelSpec) -> dict[str, Any]:
    metadata = load_json(path)
    source_grid = metadata.get("grid") or {}
    if int(source_grid.get("width") or 0) != grid.width or int(source_grid.get("height") or 0) != grid.height:
        raise ValueError(f"{path} does not use required {grid.id} dimensions")
    if _axis_integer(source_grid, "lon", "start", grid.coordinate_scale) != grid.lon_origin:
        raise ValueError(f"{path} has an unexpected longitude origin")
    if _axis_integer(source_grid, "lat", "start", grid.coordinate_scale) != grid.lat_origin:
        raise ValueError(f"{path} has an unexpected latitude origin")
    if abs(_axis_integer(source_grid, "lon", "step", grid.coordinate_scale) - grid.lon_step) > 1:
        raise ValueError(f"{path} has an unexpected longitude step")
    if abs(_axis_integer(source_grid, "lat", "step", grid.coordinate_scale) - grid.lat_step) > 1:
        raise ValueError(f"{path} has an unexpected latitude step")
    encoding = metadata.get("encoding") or {}
    if encoding.get("format") != channel.format:
        raise ValueError(f"{path} has unexpected encoding {encoding.get('format')!r}")
    missing_key = "missing_code" if channel.is_cloud else "missing_value"
    if int(encoding.get(missing_key, 10_000)) != channel.missing:
        raise ValueError(f"{path} has unexpected {missing_key}")
    if channel.name == "wind_uv":
        if (
            encoding.get("dtype") != "int8"
            or encoding.get("components") != ["u", "v"]
            or encoding.get("scale_factor") != 0.25
            or encoding.get("add_offset") != 0.0
        ):
            raise ValueError(f"{path} has an unexpected Wind encoding declaration")
    elif channel.name == "sunrain_code":
        if (
            encoding.get("dtype") != "uint8"
            or encoding.get("components") != ["sunrain_code"]
            or encoding.get("units") != ["code"]
            or encoding.get("reserved_values") != [251, 252, 253, 254, 255]
        ):
            raise ValueError(f"{path} has an unexpected Sun/Rain encoding declaration")
    elif channel.name == "rain":
        if (
            encoding.get("dtype") != "uint8"
            or encoding.get("components") != ["precipitation_mm"]
            or encoding.get("units") != ["mm"]
            or encoding.get("scale_factor") != 0.2
            or encoding.get("add_offset") != 0.0
        ):
            raise ValueError(f"{path} has an unexpected Rain encoding declaration")
    elif channel.is_cloud:
        if (
            encoding.get("dtype") != "uint8"
            or encoding.get("components") != ["cloud_cover_pct"]
            or encoding.get("units") != ["%"]
            or encoding.get("bits_per_value") != 4
            or encoding.get("quantization_step_pct") != 10
            or encoding.get("add_offset") != 0.0
            or encoding.get("reserved_codes") != [11, 12, 13, 14]
            or encoding.get("nibble_order") != "even_cell_low_nibble_odd_cell_high_nibble"
        ):
            raise ValueError(f"{path} has an unexpected Cloud encoding declaration")
    steps = metadata.get("steps") or []
    if not steps:
        raise ValueError(f"{path} has no steps")
    return metadata


def _select_source_grid(path: Path, candidates: tuple[GridSpec, ...]) -> GridSpec:
    metadata = load_json(path)
    source_grid = metadata.get("grid") or {}
    for grid in candidates:
        if (
            int(source_grid.get("width") or 0) == grid.width
            and int(source_grid.get("height") or 0) == grid.height
            and _axis_integer(source_grid, "lon", "start", grid.coordinate_scale) == grid.lon_origin
            and _axis_integer(source_grid, "lat", "start", grid.coordinate_scale) == grid.lat_origin
            and abs(_axis_integer(source_grid, "lon", "step", grid.coordinate_scale) - grid.lon_step) <= 1
            and abs(_axis_integer(source_grid, "lat", "step", grid.coordinate_scale) - grid.lat_step) <= 1
        ):
            return grid
    supported = ", ".join(grid.id for grid in candidates)
    raise ValueError(f"{path} does not use a supported grid ({supported})")


def _step_sources(
    web_root: Path,
    metadata: dict[str, Any],
    channel: ChannelSpec,
    grid: GridSpec,
) -> tuple[StepSource, ...]:
    result: list[StepSource] = []
    expected_length = (grid.width * grid.height + 1) // 2 if channel.is_cloud else (
        grid.width * grid.height * channel.cell_bytes
    )
    seen: set[str] = set()
    for step in metadata.get("steps") or []:
        label = str(step.get("step") or "")
        if not label or label in seen:
            raise ValueError(f"duplicate or missing step label {label!r}")
        seen.add(label)
        source_path = _resolve_web_url(web_root, str(step.get("url") or step.get("path") or ""))
        if not source_path.exists():
            raise FileNotFoundError(f"whole-grid step is missing: {source_path}")
        actual_length = source_path.stat().st_size
        if actual_length != expected_length or int(step.get("byte_length") or -1) != actual_length:
            raise ValueError(
                f"{source_path} byte length mismatch: expected={expected_length}, "
                f"declared={step.get('byte_length')}, actual={actual_length}"
            )
        horizon = int(step.get("horizon") if step.get("horizon") is not None else _horizon(label))
        valid_time = str(step.get("valid_time") or "")
        if not valid_time:
            raise ValueError(f"{source_path} has no valid_time")
        result.append(StepSource(label, horizon, valid_time, (source_path,)))
    return tuple(sorted(result, key=lambda item: (item.horizon, item.label)))


def _horizon(label: str) -> int:
    match = re.search(r"(\d+)", label)
    if not match:
        raise ValueError(f"cannot derive horizon from {label!r}")
    return int(match.group(1))


def _individual_variant(
    web_root: Path,
    metadata_path: Path,
    product: str,
    variant: str,
    channel: ChannelSpec,
    grid: GridSpec,
) -> VariantSource:
    metadata = _validate_source_metadata(metadata_path, grid, channel)
    parts = metadata_path.relative_to(web_root).parts
    model, run = parts[1], parts[2]
    return VariantSource(
        model=model,
        run=run,
        product=product,
        variant=variant,
        grid=grid,
        channels=(channel,),
        source_metadata=(_relative_web_url(web_root, metadata_path),),
        steps=_step_sources(web_root, metadata, channel, grid),
    )


def discover_variants(
    web_root: Path,
    *,
    selected_runs: set[tuple[str, str]] | None = None,
) -> dict[tuple[str, str], list[VariantSource]]:
    discovered: dict[tuple[str, str], list[VariantSource]] = {}

    def add(variant: VariantSource) -> None:
        discovered.setdefault((variant.model, variant.run), []).append(variant)

    def selected(path: Path) -> bool:
        parts = path.relative_to(web_root).parts
        return selected_runs is None or (parts[1], parts[2]) in selected_runs

    for path in sorted(web_root.glob("wind_maps/*/*/*/metadata.json")):
        if not selected(path):
            continue
        grid = _select_source_grid(path, WIND_GRIDS)
        add(_individual_variant(web_root, path, "wind", path.parent.name, CHANNELS["wind_uv"], grid))
    for path in sorted(web_root.glob("sunrain_maps/*/*/surface/metadata.json")):
        if not selected(path):
            continue
        grid = _select_source_grid(path, FINE_GRIDS)
        add(_individual_variant(web_root, path, "sunrain", "surface", CHANNELS["sunrain_code"], grid))
    for path in sorted(web_root.glob("rain_maps/*/*/surface/metadata.json")):
        if not selected(path):
            continue
        grid = _select_source_grid(path, FINE_GRIDS)
        add(_individual_variant(web_root, path, "rain", "surface", CHANNELS["rain"], grid))
    cloud_by_run: dict[tuple[str, str], dict[str, VariantSource]] = {}
    for layer in CLOUD_VARIANTS:
        for path in sorted(web_root.glob(f"cloud_maps/*/*/{layer}/metadata.json")):
            if not selected(path):
                continue
            channel = CHANNELS[f"cloud_{layer}"]
            grid = _select_source_grid(path, FINE_GRIDS)
            variant = _individual_variant(web_root, path, "cloud", layer, channel, grid)
            add(variant)
            cloud_by_run.setdefault((variant.model, variant.run), {})[layer] = variant

    for key, layers in cloud_by_run.items():
        if set(layers) != set(CLOUD_VARIANTS):
            raise ValueError(f"{key[0]}/{key[1]} does not have all four Cloud layers")
        ordered = tuple(layers[layer] for layer in CLOUD_VARIANTS)
        if any(variant.grid != ordered[0].grid for variant in ordered[1:]):
            raise ValueError(f"{key[0]}/{key[1]} Cloud layers use different grids")
        first_steps = {step.label: step for step in ordered[0].steps}
        for layer_variant in ordered[1:]:
            if {step.label for step in layer_variant.steps} != set(first_steps):
                raise ValueError(f"{key[0]}/{key[1]} Cloud layers have different step sets")
        grouped_steps: list[StepSource] = []
        for label, first in sorted(first_steps.items(), key=lambda item: (item[1].horizon, item[0])):
            peers = [{step.label: step for step in variant.steps}[label] for variant in ordered]
            if any(peer.horizon != first.horizon or peer.valid_time != first.valid_time for peer in peers[1:]):
                raise ValueError(f"{key[0]}/{key[1]} Cloud {label} timing differs between layers")
            grouped_steps.append(
                StepSource(label, first.horizon, first.valid_time, tuple(peer.paths[0] for peer in peers))
            )
        add(
            VariantSource(
                model=key[0],
                run=key[1],
                product="cloud",
                variant="cloud4",
                grid=ordered[0].grid,
                channels=tuple(variant.channels[0] for variant in ordered),
                source_metadata=tuple(variant.source_metadata[0] for variant in ordered),
                steps=tuple(grouped_steps),
            )
        )

    for key, variants in discovered.items():
        variant_keys = [variant.key for variant in variants]
        if len(set(variant_keys)) != len(variant_keys):
            raise ValueError(f"{key[0]}/{key[1]} has duplicate value-tile variants")
        required = {
            "sunrain/surface",
            "rain/surface",
            "cloud/total",
            "cloud/low",
            "cloud/mid",
            "cloud/high",
            "cloud/cloud4",
        }
        missing = sorted(required.difference(variant_keys))
        has_wind = any(value.startswith("wind/") for value in variant_keys)
        if missing or not has_wind:
            details = [*missing, *([] if has_wind else ["wind/<level>"])]
            raise ValueError(f"{key[0]}/{key[1]} is missing required value-tile variants: {details}")
        variants.sort(key=lambda item: item.key)
    return discovered


def _load_decoded(path: Path, grid: GridSpec, channel: ChannelSpec) -> tuple[bytes, bytes]:
    raw = path.read_bytes()
    if channel.is_cloud:
        decoded = unpack_cloud_codes(raw, grid.width * grid.height)
        decoded_values = np.frombuffer(decoded, dtype="u1")
        invalid_values = decoded_values[(decoded_values > 10) & (decoded_values != 15)]
        if invalid_values.size:
            raise ValueError(f"{path} uses reserved Cloud code {int(invalid_values[0])}")
    else:
        decoded = raw
        if channel.name == "sunrain_code":
            invalid_values = np.frombuffer(decoded, dtype="u1")
            invalid_values = invalid_values[invalid_values >= 251]
            if invalid_values.size:
                raise ValueError(f"{path} uses reserved Sun/Rain code {int(invalid_values[0])}")
    return raw, decoded


def _variant_metadata_content(source: VariantSource, steps: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "contract": CONTRACT,
        "contract_version": CONTRACT_VERSION,
        "package": PACKAGE,
        "model": source.model,
        "run": source.run,
        "product": source.product,
        "variant": source.variant,
        "grid_id": source.grid.id,
        "grid": source.grid.contract_payload(),
        "tile_matrix": source.grid.tile_matrix_payload(),
        "channels": [channel.name for channel in source.channels],
        "encodings": [channel.contract_payload() for channel in source.channels],
        "source_whole_grid_metadata": list(source.source_metadata),
        "steps": steps,
    }


def _generate_run(
    web_root: Path,
    value_root: Path,
    model: str,
    run: str,
    variants: Sequence[VariantSource],
) -> dict[str, Any]:
    run_root = value_root / model / run
    run_root.mkdir(parents=True, exist_ok=True)
    build_root = run_root / f".build-{uuid.uuid4().hex}"
    tile_records: list[dict[str, Any]] = []
    metadata_records: list[dict[str, Any]] = []
    try:
        for source in variants:
            metadata_steps: list[dict[str, Any]] = []
            for step in source.steps:
                loaded = [
                    _load_decoded(path, source.grid, channel)
                    for path, channel in zip(step.paths, source.channels)
                ]
                crc_values = {
                    channel.name: f"{zlib.crc32(raw) & 0xFFFFFFFF:08x}"
                    for channel, (raw, _decoded) in zip(source.channels, loaded)
                }
                for tile_y in range(source.grid.tiles_y):
                    for tile_x in range(source.grid.tiles_x):
                        tile = encode_xvt(
                            source.grid,
                            tile_x,
                            tile_y,
                            [
                                (channel, decoded)
                                for channel, (_raw, decoded) in zip(source.channels, loaded)
                            ],
                        )
                        logical_path = (
                            Path(source.product)
                            / source.variant
                            / step.label
                            / f"t{tile_y}_{tile_x}.xvt"
                        ).as_posix()
                        tile_path = build_root / logical_path
                        tile_path.parent.mkdir(parents=True, exist_ok=True)
                        tile_path.write_bytes(tile)
                        tile_records.append(
                            {
                                "logical_path": logical_path,
                                "byte_length": len(tile),
                                "sha256": sha256_bytes(tile),
                            }
                        )
                metadata_steps.append(
                    {
                        "step": step.label,
                        "horizon": step.horizon,
                        "valid_time": step.valid_time,
                        "tile_count": source.grid.tiles_x * source.grid.tiles_y,
                        "full_grid_crc32": crc_values,
                    }
                )
            content = _variant_metadata_content(source, metadata_steps)
            metadata_records.append(
                {
                    "logical_path": (Path(source.product) / source.variant / "metadata.json").as_posix(),
                    "content": content,
                }
            )

        tile_records.sort(key=lambda item: item["logical_path"])
        metadata_records.sort(key=lambda item: item["logical_path"])
        used_grids = {variant.grid.id: variant.grid for variant in variants}
        record = {
            "contract": CONTRACT,
            "contract_version": CONTRACT_VERSION,
            "package": PACKAGE,
            "model": model,
            "run": run,
            "grids": {grid_id: grid.contract_payload() for grid_id, grid in sorted(used_grids.items())},
            "encodings": [CHANNELS[name].contract_payload() for name in CHANNELS],
            "metadata": metadata_records,
            "tiles": tile_records,
        }
        digest = sha256_bytes(canonical_json_bytes(record))
        revision = digest[:12]
        for metadata_record in metadata_records:
            published = dict(metadata_record["content"])
            published["revision"] = revision
            published["revision_sha256"] = digest
            write_json(build_root / metadata_record["logical_path"], published)
        wrapper = {"revision": revision, "revision_sha256": digest, "record": record}
        write_json(build_root / "revision.json", wrapper)
        destination = run_root / revision
        if destination.exists():
            existing = load_json(destination / "revision.json")
            if canonical_json_bytes(existing) != canonical_json_bytes(wrapper):
                raise ValueError(f"immutable revision collision at {destination}")
            _remove_tree(build_root, run_root)
        else:
            _require_within(build_root, run_root, "revision build directory")
            _require_within(destination, run_root, "revision destination")
            os.replace(build_root, destination)
        return {
            "run": run,
            "revision": revision,
            "revision_sha256": digest,
            "tile_count": len(tile_records),
            "revision_record": _relative_web_url(web_root, destination / "revision.json"),
            "variants": {
                f"{metadata['content']['product']}/{metadata['content']['variant']}": {
                    "metadata": _relative_web_url(web_root, destination / metadata["logical_path"])
                }
                for metadata in metadata_records
            },
        }
    finally:
        if build_root.exists():
            _remove_tree(build_root, run_root)


def generate_value_tiles(
    web_root: Path,
    *,
    selected_runs: set[tuple[str, str]] | None = None,
    validate: bool = True,
) -> dict[str, Any]:
    web_root = Path(web_root)
    discovered = discover_variants(web_root, selected_runs=selected_runs)
    if selected_runs is not None:
        missing = sorted(selected_runs.difference(discovered))
        if missing:
            formatted = ", ".join(f"{model}={run}" for model, run in missing)
            raise ValueError(f"selected value-tile run(s) were not discovered: {formatted}")
    if not discovered:
        raise ValueError("no supported whole-grid map variants were found")
    value_root = web_root / "value_tiles" / "v1"
    if value_root.exists():
        _remove_tree(value_root, web_root)
    models: dict[str, Any] = {}
    tile_count = 0
    variant_count = 0
    for (model, run), variants in sorted(discovered.items()):
        run_entry = _generate_run(web_root, value_root, model, run, variants)
        models.setdefault(model, {"runs": {}})["runs"][run] = run_entry
        variant_count += len(run_entry["variants"])
        wrapper = load_json(_resolve_web_url(web_root, run_entry["revision_record"]))
        tile_count += len((wrapper.get("record") or {}).get("tiles") or [])
    manifest = {
        "contract": CONTRACT,
        "contract_version": CONTRACT_VERSION,
        "package": PACKAGE,
        "generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "models": models,
        "counts": {
            "models": len(models),
            "runs": sum(len(model["runs"]) for model in models.values()),
            "variants": variant_count,
            "tiles": tile_count,
        },
    }
    write_json(value_root / "manifest.json", manifest)
    if validate:
        validate_value_tile_publication(web_root, manifest=manifest)
    return manifest


def _grid_from_metadata(metadata: dict[str, Any]) -> GridSpec:
    grid_id = str(metadata.get("grid_id") or "")
    expected = next((grid for grid in SUPPORTED_GRIDS if grid.id == grid_id), None)
    if expected is None:
        raise ValueError(f"unknown grid id {grid_id!r}")
    if metadata.get("grid") != expected.contract_payload():
        raise ValueError(f"metadata grid declaration differs from {grid_id}")
    if metadata.get("tile_matrix") != expected.tile_matrix_payload():
        raise ValueError(f"metadata tile matrix differs from {grid_id}")
    return expected


def _metadata_without_revision(metadata: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in metadata.items() if key not in {"revision", "revision_sha256"}}


def _validate_tile_identity(tile: ParsedTile, grid: GridSpec, tile_x: int, tile_y: int) -> None:
    valid_width, valid_height = _valid_core(grid, tile_x, tile_y)
    expected = (
        tile_x,
        tile_y,
        grid.core_width,
        grid.core_height,
        valid_width,
        valid_height,
        grid.payload_width,
        grid.payload_height,
        grid.width,
        grid.height,
    )
    actual = (
        tile.tile_x,
        tile.tile_y,
        tile.core_width,
        tile.core_height,
        tile.valid_core_width,
        tile.valid_core_height,
        tile.payload_width,
        tile.payload_height,
        tile.grid_width,
        tile.grid_height,
    )
    if actual != expected:
        raise ValueError(f"tile ({tile_x}, {tile_y}) header does not match its metadata")
    x_start = tile_x * grid.core_width - HALO
    y_start = tile_y * grid.core_height - HALO
    expected_padding = (
        x_start < 0
        or y_start < 0
        or x_start + grid.payload_width > grid.width
        or y_start + grid.payload_height > grid.height
    )
    if bool(tile.flags & 1) != expected_padding:
        raise ValueError(f"tile ({tile_x}, {tile_y}) padding flag is incorrect")


def _tile_path(metadata_path: Path, step: str, tile_x: int, tile_y: int) -> Path:
    return metadata_path.parent / step / f"t{tile_y}_{tile_x}.xvt"


def _validate_variant_declaration(
    metadata_path: Path,
    metadata: dict[str, Any],
) -> tuple[GridSpec, tuple[ChannelSpec, ...], list[dict[str, Any]], int]:
    grid = _grid_from_metadata(metadata)
    channel_names = metadata.get("channels") or []
    channels = tuple(CHANNELS.get(str(name)) for name in channel_names)
    if not channels or any(channel is None for channel in channels):
        raise ValueError(f"{metadata_path} declares unknown channels")
    typed_channels = tuple(channel for channel in channels if channel is not None)
    if metadata.get("encodings") != [channel.contract_payload() for channel in typed_channels]:
        raise ValueError(f"{metadata_path} encoding declaration differs from the contract")
    steps = metadata.get("steps") or []
    if not steps:
        raise ValueError(f"{metadata_path} has no steps")
    expected_tiles = grid.tiles_x * grid.tiles_y
    for step in steps:
        label = str(step.get("step") or "")
        if int(step.get("tile_count") or 0) != expected_tiles:
            raise ValueError(f"{metadata_path} {label} has an incomplete tile matrix declaration")
    return grid, typed_channels, steps, len(steps) * expected_tiles


def _validate_variant(
    metadata_path: Path,
    metadata: dict[str, Any],
    tile_records: dict[str, dict[str, Any]],
) -> tuple[int, int, set[str]]:
    grid, typed_channels, steps, _declared_tiles = _validate_variant_declaration(metadata_path, metadata)
    tile_count = 0
    validated_paths: set[str] = set()
    revision_root = metadata_path.parents[2]
    for step in steps:
        label = str(step.get("step") or "")
        reconstructed = {
            channel.name: bytearray(channel.missing_bytes * (grid.width * grid.height))
            for channel in typed_channels
        }
        parsed_tiles: dict[tuple[int, int], ParsedTile] = {}
        for tile_y in range(grid.tiles_y):
            for tile_x in range(grid.tiles_x):
                path = _tile_path(metadata_path, label, tile_x, tile_y)
                data = path.read_bytes()
                logical_path = path.relative_to(revision_root).as_posix()
                tile_record = tile_records.get(logical_path)
                if tile_record is None:
                    raise ValueError(f"{logical_path} is absent from the revision record")
                if len(data) != int(tile_record.get("byte_length") or -1):
                    raise ValueError(f"{logical_path} byte length differs from revision record")
                if sha256_bytes(data) != tile_record.get("sha256"):
                    raise ValueError(f"{logical_path} SHA-256 differs from revision record")
                tile = parse_xvt(data)
                parsed_tiles[(tile_x, tile_y)] = tile
                validated_paths.add(logical_path)
                _validate_tile_identity(tile, grid, tile_x, tile_y)
                if tuple(section.channel for section in tile.sections) != typed_channels:
                    raise ValueError(f"{path} channel order differs from metadata")
                for section in tile.sections:
                    decoded = _decoded_section(section)
                    cell_bytes = section.channel.cell_bytes
                    for core_y in range(tile.valid_core_height):
                        source_offset = ((core_y + HALO) * grid.payload_width + HALO) * cell_bytes
                        target_y = tile_y * grid.core_height + core_y
                        target_offset = (target_y * grid.width + tile_x * grid.core_width) * cell_bytes
                        length = tile.valid_core_width * cell_bytes
                        reconstructed[section.channel.name][target_offset : target_offset + length] = decoded[
                            source_offset : source_offset + length
                        ]
                tile_count += 1
        declared_crc = step.get("full_grid_crc32") or {}
        for channel in typed_channels:
            decoded = bytes(reconstructed[channel.name])
            encoded = pack_cloud_codes(decoded) if channel.is_cloud else decoded
            actual_crc = f"{zlib.crc32(encoded) & 0xFFFFFFFF:08x}"
            if declared_crc.get(channel.name) != actual_crc:
                raise ValueError(f"{metadata_path} {label} {channel.name} full-grid CRC mismatch")
        for tile_y in range(grid.tiles_y):
            for tile_x in range(grid.tiles_x):
                path = _tile_path(metadata_path, label, tile_x, tile_y)
                tile = parsed_tiles[(tile_x, tile_y)]
                x_start = tile_x * grid.core_width - HALO
                y_start = tile_y * grid.core_height - HALO
                for section in tile.sections:
                    decoded = _decoded_section(section)
                    expected_grid = bytes(reconstructed[section.channel.name])
                    cell_bytes = section.channel.cell_bytes
                    for payload_y in range(grid.payload_height):
                        source_y = y_start + payload_y
                        row = decoded[
                            payload_y * grid.payload_width * cell_bytes :
                            (payload_y + 1) * grid.payload_width * cell_bytes
                        ]
                        expected_row = bytearray(section.channel.missing_bytes * grid.payload_width)
                        if 0 <= source_y < grid.height:
                            copy_x0 = max(0, x_start)
                            copy_x1 = min(grid.width, x_start + grid.payload_width)
                            if copy_x1 > copy_x0:
                                source_offset = (source_y * grid.width + copy_x0) * cell_bytes
                                output_offset = (copy_x0 - x_start) * cell_bytes
                                length = (copy_x1 - copy_x0) * cell_bytes
                                expected_row[output_offset : output_offset + length] = expected_grid[
                                    source_offset : source_offset + length
                                ]
                        if row != bytes(expected_row):
                            raise ValueError(f"{path} halo or outside-domain padding differs from its global grid")
    return len(steps), tile_count, validated_paths


def validate_value_tile_publication(
    web_root: Path,
    *,
    manifest: dict[str, Any] | None = None,
    require_capability: bool = False,
    full_runs: set[tuple[str, str]] | None = None,
) -> dict[str, int]:
    web_root = Path(web_root)
    manifest_path = web_root / "value_tiles" / "v1" / "manifest.json"
    if manifest is None:
        if not manifest_path.exists():
            if require_capability:
                raise FileNotFoundError(f"missing value-tile manifest: {manifest_path}")
            return {"runs": 0, "variants": 0, "steps": 0, "tiles": 0}
        manifest = load_json(manifest_path)
    if (
        manifest.get("contract") != CONTRACT
        or manifest.get("contract_version") != CONTRACT_VERSION
        or manifest.get("package") != PACKAGE
    ):
        raise ValueError("value-tile manifest contract declaration is invalid")
    counts = {"runs": 0, "variants": 0, "steps": 0, "tiles": 0}
    seen_runs: set[tuple[str, str]] = set()
    for model, model_entry in sorted((manifest.get("models") or {}).items()):
        for run, run_entry in sorted((model_entry.get("runs") or {}).items()):
            run_key = (model, run)
            seen_runs.add(run_key)
            validate_fully = full_runs is None or run_key in full_runs
            counts["runs"] += 1
            run_tile_start = counts["tiles"]
            revision = str(run_entry.get("revision") or "")
            digest = str(run_entry.get("revision_sha256") or "")
            revision_path = _resolve_web_url(web_root, str(run_entry.get("revision_record") or ""))
            _require_within(revision_path, web_root, "revision record")
            wrapper = load_json(revision_path)
            record = wrapper.get("record") or {}
            actual_digest = sha256_bytes(canonical_json_bytes(record))
            if revision != digest[:12] or digest != actual_digest:
                raise ValueError(f"{model}/{run} revision digest is invalid")
            if wrapper.get("revision") != revision or wrapper.get("revision_sha256") != digest:
                raise ValueError(f"{revision_path} wrapper digest is invalid")
            if record.get("model") != model or record.get("run") != run:
                raise ValueError(f"{revision_path} record identity is invalid")
            if (
                record.get("contract") != CONTRACT
                or record.get("contract_version") != CONTRACT_VERSION
                or record.get("package") != PACKAGE
            ):
                raise ValueError(f"{revision_path} record contract declaration is invalid")
            revision_root = revision_path.parent
            tile_records = record.get("tiles") or []
            metadata_records = record.get("metadata") or []
            recorded_tiles = {str(item.get("logical_path")): item for item in tile_records}
            if len(recorded_tiles) != len(tile_records):
                raise ValueError(f"{revision_path} has duplicate tile record paths")
            if int(run_entry.get("tile_count") or -1) != len(tile_records):
                raise ValueError(f"{model}/{run} tile count differs from its revision record")
            if validate_fully:
                actual_tiles = {
                    path.relative_to(revision_root).as_posix() for path in revision_root.rglob("*.xvt")
                }
                if set(recorded_tiles) != actual_tiles:
                    raise ValueError(f"{revision_path} tile file set differs from its record")
            variants = run_entry.get("variants") or {}
            if len(variants) != len(metadata_records):
                raise ValueError(f"{model}/{run} variant index differs from revision metadata")
            recorded_metadata = {str(item.get("logical_path")) for item in metadata_records}
            if len(recorded_metadata) != len(metadata_records):
                raise ValueError(f"{revision_path} has duplicate metadata record paths")
            if validate_fully:
                actual_metadata = {
                    path.relative_to(revision_root).as_posix()
                    for path in revision_root.rglob("metadata.json")
                }
                if recorded_metadata != actual_metadata:
                    raise ValueError(f"{revision_path} metadata file set differs from its record")
            validated_tile_paths: set[str] = set()
            for metadata_record in metadata_records:
                logical_path = str(metadata_record.get("logical_path") or "")
                metadata_path = revision_root / logical_path
                metadata = load_json(metadata_path)
                if metadata.get("revision") != revision or metadata.get("revision_sha256") != digest:
                    raise ValueError(f"{metadata_path} has invalid derived revision fields")
                if canonical_json_bytes(_metadata_without_revision(metadata)) != canonical_json_bytes(
                    metadata_record.get("content")
                ):
                    raise ValueError(f"{metadata_path} differs from canonical revision content")
                variant_key = f"{metadata.get('product')}/{metadata.get('variant')}"
                indexed_path = _resolve_web_url(web_root, str((variants.get(variant_key) or {}).get("metadata") or ""))
                if indexed_path != metadata_path:
                    raise ValueError(f"{model}/{run} variant {variant_key} metadata path is invalid")
                if validate_fully:
                    step_count, variant_tiles, variant_tile_paths = _validate_variant(
                        metadata_path,
                        metadata,
                        recorded_tiles,
                    )
                else:
                    _grid, _channels, steps, variant_tiles = _validate_variant_declaration(
                        metadata_path,
                        metadata,
                    )
                    step_count = len(steps)
                    variant_tile_paths = set()
                counts["variants"] += 1
                counts["steps"] += step_count
                counts["tiles"] += variant_tiles
                validated_tile_paths.update(variant_tile_paths)
            if counts["tiles"] - run_tile_start != len(tile_records):
                raise ValueError(f"{model}/{run} did not validate every recorded tile")
            if validate_fully and validated_tile_paths != set(recorded_tiles):
                raise ValueError(f"{model}/{run} did not validate every recorded tile")
    if full_runs is not None:
        missing_full_runs = sorted(full_runs.difference(seen_runs))
        if missing_full_runs:
            formatted = ", ".join(f"{model}={run}" for model, run in missing_full_runs)
            raise ValueError(f"fully validated value-tile run(s) are absent from the manifest: {formatted}")
    declared = manifest.get("counts") or {}
    for key in ("runs", "variants", "tiles"):
        if int(declared.get(key) or 0) != counts[key]:
            raise ValueError(f"value-tile manifest count {key} is incorrect")
    return counts


def merge_value_tile_manifests(existing: dict[str, Any], staged: dict[str, Any]) -> dict[str, Any]:
    if not existing:
        return staged
    if not staged:
        return existing
    for payload in (existing, staged):
        if payload.get("contract") != CONTRACT or payload.get("contract_version") != CONTRACT_VERSION:
            raise ValueError("cannot merge an incompatible value-tile manifest")
    models: dict[str, Any] = {}
    for payload in (existing, staged):
        for model, model_entry in (payload.get("models") or {}).items():
            target = models.setdefault(model, {"runs": {}})
            target["runs"].update((model_entry.get("runs") or {}))
    merged = {
        "contract": CONTRACT,
        "contract_version": CONTRACT_VERSION,
        "package": PACKAGE,
        "generated_at": staged.get("generated_at") or existing.get("generated_at"),
        "models": models,
        "counts": {},
    }
    _refresh_manifest_counts(merged)
    return merged


def _refresh_manifest_counts(manifest: dict[str, Any]) -> None:
    runs = 0
    variants = 0
    tiles = 0
    for model_entry in (manifest.get("models") or {}).values():
        for run_entry in (model_entry.get("runs") or {}).values():
            runs += 1
            variants += len(run_entry.get("variants") or {})
            revision_path = str(run_entry.get("revision_record") or "")
            if revision_path:
                tiles += int(run_entry.get("tile_count") or 0)
    manifest["counts"] = {
        "models": len(manifest.get("models") or {}),
        "runs": runs,
        "variants": variants,
        "tiles": tiles,
    }


def prune_value_tile_manifest(web_root: Path, keep_by_model: dict[str, set[str]]) -> dict[str, Any] | None:
    manifest_path = Path(web_root) / "value_tiles" / "v1" / "manifest.json"
    if not manifest_path.exists():
        return None
    manifest = load_json(manifest_path)
    for model in list((manifest.get("models") or {}).keys()):
        model_entry = manifest["models"][model]
        keep = keep_by_model.get(model, set())
        for run in list((model_entry.get("runs") or {}).keys()):
            run_entry = model_entry["runs"][run]
            run_root = manifest_path.parent / model / run
            if run not in keep:
                if run_root.exists():
                    _remove_tree(run_root, manifest_path.parent)
                del model_entry["runs"][run]
                continue
            selected_revision = str(run_entry.get("revision") or "")
            if run_root.exists():
                for revision_path in run_root.iterdir():
                    if revision_path.is_dir() and revision_path.name != selected_revision:
                        _remove_tree(revision_path, run_root)
        if not model_entry.get("runs"):
            del manifest["models"][model]
    if not manifest.get("models"):
        value_tiles_root = manifest_path.parents[1]
        _remove_tree(value_tiles_root, Path(web_root))
        return None
    tile_count = 0
    variant_count = 0
    run_count = 0
    for model_entry in (manifest.get("models") or {}).values():
        for run_entry in (model_entry.get("runs") or {}).values():
            run_count += 1
            variant_count += len(run_entry.get("variants") or {})
            wrapper_path = _resolve_web_url(Path(web_root), run_entry["revision_record"])
            _require_within(wrapper_path, Path(web_root), "retained revision record")
            wrapper = load_json(wrapper_path)
            tile_count += len((wrapper.get("record") or {}).get("tiles") or [])
    manifest["counts"] = {
        "models": len(manifest.get("models") or {}),
        "runs": run_count,
        "variants": variant_count,
        "tiles": tile_count,
    }
    manifest["generated_at"] = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    write_json(manifest_path, manifest)
    return manifest
