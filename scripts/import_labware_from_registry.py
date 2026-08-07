"""Import labware definitions from a Windows registry (.reg) export.

Reads labware entries from the Velocity11 shared registry key
``HKLM\\SOFTWARE\\WOW6432Node\\Velocity11\\shared\\Labware\\Labware_Entries``
and writes them into the OpenBravo labware catalog snapshot YAML.

Usage:
    python scripts/import_labware_from_registry.py <path-to-labware.reg> [--output config/labware_catalog.snapshot.yaml]
    python scripts/import_labware_from_registry.py <path-to-labware.reg> --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

_SECTION_RE = re.compile(
    r"\[HKEY_LOCAL_MACHINE\\SOFTWARE\\WOW6432Node\\Velocity11\\shared\\Labware\\Labware_Entries\\([^\]]+)\]"
)
_KV_RE = re.compile(r'^"((?:[^"\\]|\\.)*)"=(.*)$')

_BASE_CLASS_MAP = {
    "1": "microplate",
    "2": "filter_plate",
    "3": "reservoir",
    "4": "wash_station",
    "5": "accessory",
    "6": "tip_box",
    "7": "lid",
    "8": "tip_waste",
}

_KIND_FROM_BASE_CLASS = {
    "microplate": "sbs_plate",
    "filter_plate": "sbs_plate",
    "reservoir": "reservoir",
    "wash_station": "wash_station",
    "tip_box": "tip_box",
    "tip_waste": "tip_waste",
    "lid": "lid",
    "accessory": "accessory",
}

_SPEED_MAP = {
    "0": "slow",
    "1": "medium",
    "2": "fast",
}

_WELL_COUNT_TO_GRID: dict[int, tuple[int, int]] = {
    6: (2, 3),
    12: (3, 4),
    24: (4, 6),
    48: (6, 8),
    96: (8, 12),
    384: (16, 24),
    1536: (32, 48),
}


def _read_reg_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig", errors="replace")


def _unescape(s: str) -> str:
    return s.replace('\\"', '"').replace('\\\\', '\\')


def _parse_value(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"'):
        return _unescape(raw[1:-1])
    return raw


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _to_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _to_bool(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes")
    return bool(v)


def _make_id(name: str) -> str:
    digest = hashlib.sha256(name.encode()).hexdigest()[:12]
    return f"legacy-{digest}"


def parse_reg_labware(text: str) -> list[dict[str, Any]]:
    """Parse labware sections from a .reg payload, return list of raw KV dicts."""
    entries: list[tuple[str, dict[str, str]]] = []
    current_name: str | None = None
    current_kv: dict[str, str] = {}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(";"):
            continue

        if line.startswith("["):
            if current_name is not None:
                entries.append((current_name, current_kv))
            m = _SECTION_RE.match(line)
            if m:
                current_name = m.group(1)
                current_kv = {}
            else:
                current_name = None
                current_kv = {}
            continue

        if current_name is None:
            continue

        m = _KV_RE.match(line)
        if m:
            key = _unescape(m.group(1))
            current_kv[key] = _parse_value(m.group(2))

    if current_name is not None:
        entries.append((current_name, current_kv))

    return [{"_section_name": name, **kv} for name, kv in entries]


def reg_entry_to_labware(entry: dict[str, str]) -> dict[str, Any]:
    """Convert one raw registry labware entry to OpenBravo snapshot format."""
    name = entry.get("NAME", entry.get("_section_name", "Unknown"))
    wells = _to_int(entry.get("NUMBER_OF_WELLS"), 0)
    rows, cols = _WELL_COUNT_TO_GRID.get(wells, (0, 0))
    base_class_raw = entry.get("BASE_CLASS", "1")
    base_class = _BASE_CLASS_MAP.get(base_class_raw, "microplate")
    kind = _KIND_FROM_BASE_CLASS.get(base_class, "sbs_plate")
    speed = _SPEED_MAP.get(entry.get("ROBOT_HANDLING_SPEED", ""), "")
    part_number = entry.get("MANUFACTURER_PART_NUMBER", "")

    tip_capacity = _to_float(entry.get("TIP_CAPACITY"), 0.0)

    return {
        "id": _make_id(name),
        "name": name,
        "kind": kind,
        "vendor": "",
        "catalog_number": part_number,
        "description": entry.get("DESCRIPTION", ""),
        "base_class": base_class,
        "wells": wells,
        "length_mm": 127.76,
        "width_mm": 85.48,
        "height_mm": _to_float(entry.get("THICKNESS")),
        "stack_height_mm": _to_float(entry.get("STACKING_THICKNESS")),
        "gripper_offset_mm": _to_float(entry.get("BRAVO_ROBOT_GRIPPER_OFFSET",
                                                   entry.get("ROBOT_GRIPPER_OFFSET"))),
        "lid_gripper_offset_mm": (
            _to_float(entry["BRAVO_ROBOT_LID_GRIPPER_OFFSET"])
            if entry.get("BRAVO_ROBOT_LID_GRIPPER_OFFSET") else None
        ),
        "empty_check_offset_mm": None,
        "shim_thickness_mm": _to_float(entry.get("SHIM_THICKNESS")),
        "can_be_sealed": _to_bool(entry.get("CAN_BE_SEALED", "0")),
        "sealed_height_mm": _to_float(entry.get("SEALED_THICKNESS")),
        "sealed_stacking_height_mm": _to_float(entry.get("SEALED_STACKING_THICKNESS")),
        "can_have_lid": _to_bool(entry.get("CAN_HAVE_LID", "0")),
        "lidded_height_mm": _to_float(entry.get("LIDDED_THICKNESS")),
        "lidded_stack_height_mm": _to_float(entry.get("LIDDED_STACKING_THICKNESS")),
        "lid_resting_height_mm": _to_float(entry.get("LID_RESTING_HEIGHT")),
        "lid_departure_height_mm": _to_float(entry.get("LID_DEPARTURE_HEIGHT")),
        "max_robot_handling_speed": speed,
        "rows": rows,
        "cols": cols,
        "well_depth_mm": _to_float(entry.get("WELL_DEPTH")),
        "offset_x_mm": _to_float(entry.get("X_TEACHPOINT_TO_WELL")),
        "offset_y_mm": _to_float(entry.get("Y_TEACHPOINT_TO_WELL")),
        "spacing_x_mm": _to_float(entry.get("X_WELL_TO_WELL")),
        "spacing_y_mm": _to_float(entry.get("Y_WELL_TO_WELL")),
        "well_volume_ul": _to_float(entry.get("WELL_TIP_VOLUME")),
        "well_diameter_mm": _to_float(entry.get("WELL_DIAMETER")),
        "disposable_tip_capacity_ul": tip_capacity,
        "tip_definition_id": "",
        "supported_tip_ids": [],
        "model_3d": None,
        "can_mount": _to_bool(entry.get("CAN_MOUNT", "0")),
        "can_be_mounted": _to_bool(entry.get("CAN_BE_MOUNTED", "0")),
    }


def import_labware_reg(
    reg_path: Path,
    *,
    output_path: Path | None = None,
    merge: bool = True,
) -> tuple[list[dict[str, Any]], int, int]:
    """Parse a labware .reg export and write to snapshot YAML.

    Returns (all_labware, new_count, updated_count).
    """
    text = _read_reg_text(reg_path)
    raw_entries = parse_reg_labware(text)
    imported = [reg_entry_to_labware(e) for e in raw_entries]

    existing: list[dict[str, Any]] = []
    if merge and output_path and output_path.exists():
        with open(output_path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        existing = raw.get("labware", [])
        if not isinstance(existing, list):
            existing = []

    existing_by_name = {e["name"]: i for i, e in enumerate(existing)}

    new_count = 0
    updated_count = 0
    for item in imported:
        idx = existing_by_name.get(item["name"])
        if idx is not None:
            existing[idx] = item
            updated_count += 1
        else:
            existing.append(item)
            new_count += 1

    existing.sort(key=lambda d: d.get("name", "").lower())

    if output_path:
        payload = {
            "version": 1,
            "source": "registry_import",
            "synced_at": datetime.now(timezone.utc).isoformat(),
            "labware": existing,
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(payload, fh, sort_keys=False, allow_unicode=True)

    return existing, new_count, updated_count


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import labware from a .reg export into OpenBravo's labware catalog."
    )
    parser.add_argument("reg_file", type=Path, help="Path to the labware .reg export")
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "config" / "labware_catalog.snapshot.yaml",
        help="Output snapshot YAML path (default: config/labware_catalog.snapshot.yaml)",
    )
    parser.add_argument(
        "--no-merge",
        action="store_true",
        help="Overwrite snapshot instead of merging with existing entries",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and print results without writing to disk",
    )
    args = parser.parse_args()

    if not args.reg_file.exists():
        print(f"Error: {args.reg_file} not found", file=sys.stderr)
        sys.exit(1)

    output = None if args.dry_run else args.output
    labware, new_count, updated_count = import_labware_reg(
        args.reg_file,
        output_path=output,
        merge=not args.no_merge,
    )

    print(f"Parsed {len(labware)} labware definitions from {args.reg_file.name}")
    print(f"  New: {new_count}  |  Updated: {updated_count}")

    if args.dry_run:
        print("\n--- Dry run output ---\n")
        for item in labware:
            print(f"  {item['name']}")
            print(f"    kind={item['kind']}  wells={item['wells']}  "
                  f"height={item['height_mm']}mm  stack={item['stack_height_mm']}mm  "
                  f"grip={item['gripper_offset_mm']}mm")
    else:
        print(f"\nWritten to {args.output}")


if __name__ == "__main__":
    main()
