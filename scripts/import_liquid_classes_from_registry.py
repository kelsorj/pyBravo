"""Import liquid class definitions from a Windows registry (.reg) export.

Reads liquid class entries from the Velocity11 shared registry key
``HKLM\\SOFTWARE\\WOW6432Node\\Velocity11\\Shared\\Liquid Library``
and writes them into the OpenBravo liquid_classes.yaml store.

The registry file is typically UTF-16LE encoded (Windows .reg export default).
Each liquid class entry has aspirate/dispense velocity/acceleration parameters,
post-delays (dword hex), and a ``\\Coefficients`` sub-key with polynomial
correction coefficients.

Usage:
    python scripts/import_liquid_classes_from_registry.py path/to/liquid_classes.reg --machine-id 04-91-62-CF-7B-B0
    python scripts/import_liquid_classes_from_registry.py path/to/liquid_classes.reg --machine-id 04-91-62-CF-7B-B0 --dry-run
    python scripts/import_liquid_classes_from_registry.py path/to/liquid_classes.reg --machine-id 04-91-62-CF-7B-B0 --head-type HT_96_D_70
"""

from __future__ import annotations

import argparse
import re
import sys
import uuid
from pathlib import Path
from typing import Any

import yaml


_STORE_PATH = Path(__file__).resolve().parents[1] / "config" / "liquid_classes.yaml"

_BASE_KEY = r"HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Velocity11\Shared\Liquid Library"

_SECTION_RE = re.compile(
    r"\[" + re.escape(_BASE_KEY) + r"\\([^\]]+)\]"
)
_KV_RE = re.compile(r'^"((?:[^"\\]|\\.)*)"=(.*)$')
_DWORD_RE = re.compile(r"^dword:([0-9a-fA-F]+)$")
_VOLUME_RANGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*ul", re.IGNORECASE)

_HEAD_TYPE_INFERENCE = {
    ("384", "disposable"): "HT_384_D_70",
    ("96", "disposable"): "HT_96_D_70",
    ("96", "fixed"): "HT_96_F_50",
}


def _read_reg_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig", errors="replace")


def _unescape(s: str) -> str:
    return s.replace('\\"', '"').replace('\\\\', '\\')


def _parse_value(raw: str) -> Any:
    raw = raw.strip()
    m = _DWORD_RE.match(raw)
    if m:
        return int(m.group(1), 16)
    if raw.startswith('"') and raw.endswith('"'):
        return _unescape(raw[1:-1])
    return raw


def _make_id() -> str:
    return f"liq_{uuid.uuid4().hex[:10]}"


def _infer_head_type(name: str) -> str:
    name_lower = name.lower()
    if "384" in name_lower:
        channels = "384"
    elif "96" in name_lower:
        channels = "96"
    else:
        channels = "96"

    if "disposable" in name_lower:
        tip_type = "disposable"
    elif "fixed" in name_lower:
        tip_type = "fixed"
    else:
        tip_type = "disposable"

    head = _HEAD_TYPE_INFERENCE.get((channels, tip_type), "HT_96_D_70")

    m = _VOLUME_RANGE_RE.search(name_lower)
    if m:
        upper = float(m.group(2))
        if channels == "96" and tip_type == "disposable" and upper > 70:
            head = "HT_96_D_200"
        if channels == "96" and tip_type == "fixed" and upper > 50:
            head = "HT_96_F_200"

    return head


def _infer_tip_capacity(name: str) -> float:
    m = _VOLUME_RANGE_RE.search(name)
    if m:
        return float(m.group(2))
    name_lower = name.lower()
    if "384" in name_lower:
        return 10.0
    if "200" in name_lower:
        return 200.0
    return 50.0


def parse_reg_liquid_classes(text: str) -> list[dict[str, Any]]:
    """Parse liquid class sections from a .reg payload.

    Returns list of dicts, each with the entry name, aspirate/dispense
    parameters, and polynomial coefficients.
    """
    sections: list[tuple[str, dict[str, Any]]] = []
    current_path: str | None = None
    current_kv: dict[str, Any] = {}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(";"):
            continue

        if line.startswith("["):
            if current_path is not None:
                sections.append((current_path, current_kv))
            m = _SECTION_RE.match(line)
            if m:
                current_path = m.group(1)
                current_kv = {}
            else:
                current_path = None
                current_kv = {}
            continue

        if current_path is None:
            continue

        m = _KV_RE.match(line)
        if m:
            key = _unescape(m.group(1))
            current_kv[key] = _parse_value(m.group(2))

    if current_path is not None:
        sections.append((current_path, current_kv))

    entries: dict[str, dict[str, Any]] = {}
    coeff_entries: dict[str, dict[str, Any]] = {}

    for path, kv in sections:
        if path.endswith("\\Coefficients"):
            parent_name = path[: -len("\\Coefficients")]
            coeff_entries[parent_name] = kv
        else:
            entries[path] = kv

    result = []
    for name, kv in entries.items():
        coefficients = []
        coeff_kv = coeff_entries.get(name, {})
        num_coeffs = coeff_kv.get("Number of Coefficients", 0)
        if isinstance(num_coeffs, str):
            num_coeffs = int(num_coeffs)
        for i in range(num_coeffs):
            val = coeff_kv.get(str(i), "0.0")
            coefficients.append(float(val))

        def _float(key: str) -> float:
            v = kv.get(key, "0.0")
            return float(v) if isinstance(v, str) else float(v)

        def _int(key: str) -> int:
            v = kv.get(key, 0)
            return int(v)

        result.append({
            "name": name,
            "note": kv.get("Note", ""),
            "aspirate": {
                "w_velocity_ul_s": _float("Aspirate Velocity"),
                "w_acceleration_ul_s2": _float("Aspirate Acceleration"),
                "post_delay_ms": _int("Post Aspirate Delay"),
                "z_in_velocity_mm_s": _float("Aspirate Velocity Into Wells"),
                "z_in_acceleration_mm_s2": _float("Aspirate Acceleration Into Wells"),
                "z_out_velocity_mm_s": _float("Aspirate Velocity Out Of Wells"),
                "z_out_acceleration_mm_s2": _float("Aspirate Acceleration Out Of Wells"),
            },
            "dispense": {
                "w_velocity_ul_s": _float("Dispense Velocity"),
                "w_acceleration_ul_s2": _float("Dispense Acceleration"),
                "post_delay_ms": _int("Post Dispense Delay"),
                "z_in_velocity_mm_s": _float("Dispense Velocity Into Wells"),
                "z_in_acceleration_mm_s2": _float("Dispense Acceleration Into Wells"),
                "z_out_velocity_mm_s": _float("Dispense Velocity Out Of Wells"),
                "z_out_acceleration_mm_s2": _float("Dispense Acceleration Out Of Wells"),
            },
            "coefficients": coefficients,
        })

    return result


def _coefficients_to_control_points(
    coefficients: list[float],
    tip_capacity_ul: float,
) -> list[dict[str, float]]:
    """Convert polynomial coefficients to piecewise control_points.

    The registry stores correction as a polynomial: commanded = c0 + c1*desired + c2*desired^2 + ...
    OpenBravo uses sampled control_points with (desired_ul, commanded_ul) pairs.
    """
    if not coefficients:
        return [
            {"desired_ul": 0.0, "commanded_ul": 0.0},
            {"desired_ul": tip_capacity_ul, "commanded_ul": tip_capacity_ul},
        ]

    max_vol = max(1.0, tip_capacity_ul)
    if len(coefficients) <= 2:
        samples = [0.0, max_vol]
    else:
        n_samples = min(len(coefficients) + 2, 8)
        samples = [max_vol * i / (n_samples - 1) for i in range(n_samples)]

    points = []
    for desired in samples:
        commanded = sum(c * (desired ** exp) for exp, c in enumerate(coefficients))
        points.append({
            "desired_ul": round(desired, 6),
            "commanded_ul": round(max(0.0, commanded), 6),
        })
    return points


def reg_entry_to_liquid_class(
    entry: dict[str, Any],
    *,
    machine_id: str,
    head_type_override: str | None = None,
) -> dict[str, Any]:
    """Convert one parsed registry entry to the OpenBravo liquid class YAML format."""
    name = entry["name"]
    head_type = head_type_override or _infer_head_type(name)
    tip_capacity = _infer_tip_capacity(name)
    coefficients = entry.get("coefficients", [])

    return {
        "liquid_class_id": _make_id(),
        "name": name,
        "description": entry.get("note", ""),
        "machine_id": machine_id,
        "head_type": head_type,
        "tip_id": "",
        "tip_capacity_ul": tip_capacity,
        "aspirate": entry["aspirate"],
        "dispense": entry["dispense"],
        "equation": {
            "control_points": _coefficients_to_control_points(coefficients, tip_capacity),
        },
    }


def import_liquid_classes_reg(
    reg_path: Path,
    *,
    machine_id: str,
    head_type_override: str | None = None,
    output_path: Path | None = None,
    merge: bool = True,
) -> tuple[list[dict[str, Any]], int, int]:
    """Parse a liquid class .reg export and write to liquid_classes.yaml.

    Returns (all_classes, new_count, updated_count).
    """
    text = _read_reg_text(reg_path)
    raw_entries = parse_reg_liquid_classes(text)
    imported = [
        reg_entry_to_liquid_class(e, machine_id=machine_id, head_type_override=head_type_override)
        for e in raw_entries
    ]

    existing_store: dict[str, Any] = {"version": 1, "liquid_classes": [], "pipette_techniques": []}
    if merge and output_path and output_path.exists():
        with open(output_path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        existing_store.update(raw)

    existing_classes = list(existing_store.get("liquid_classes") or [])
    existing_by_key: dict[str, int] = {}
    for i, cls in enumerate(existing_classes):
        key = (
            str(cls.get("name") or "").lower(),
            str(cls.get("machine_id") or ""),
            str(cls.get("head_type") or ""),
        )
        existing_by_key[key] = i

    new_count = 0
    updated_count = 0
    for item in imported:
        key = (
            str(item["name"]).lower(),
            str(item["machine_id"]),
            str(item["head_type"]),
        )
        idx = existing_by_key.get(key)
        if idx is not None:
            item["liquid_class_id"] = existing_classes[idx].get("liquid_class_id", item["liquid_class_id"])
            existing_classes[idx] = item
            updated_count += 1
        else:
            existing_classes.append(item)
            new_count += 1

    existing_classes.sort(key=lambda d: (
        str(d.get("machine_id") or "").lower(),
        str(d.get("head_type") or "").lower(),
        str(d.get("tip_id") or "").lower(),
        float(d.get("tip_capacity_ul") or 0.0),
        str(d.get("name") or "").lower(),
    ))

    if output_path:
        existing_store["liquid_classes"] = existing_classes
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(existing_store, fh, sort_keys=False, allow_unicode=True)

    return existing_classes, new_count, updated_count


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import liquid classes from a .reg export into OpenBravo's liquid_classes.yaml."
    )
    parser.add_argument("reg_file", type=Path, help="Path to the liquid class .reg export")
    parser.add_argument(
        "--machine-id", "-m",
        required=True,
        help="Machine ID to bind imported classes to (e.g. 04-91-62-CF-7B-B0)",
    )
    parser.add_argument(
        "--head-type",
        default=None,
        help="Override head type for ALL entries (e.g. HT_96_D_70). "
             "If not set, head type is inferred from the entry name.",
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=_STORE_PATH,
        help="Output YAML path (default: config/liquid_classes.yaml)",
    )
    parser.add_argument(
        "--no-merge",
        action="store_true",
        help="Replace all existing classes instead of merging",
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
    classes, new_count, updated_count = import_liquid_classes_reg(
        args.reg_file,
        machine_id=args.machine_id,
        head_type_override=args.head_type,
        output_path=output,
        merge=not args.no_merge,
    )

    print(f"Parsed {new_count + updated_count} liquid class entries from {args.reg_file.name}")
    print(f"  New: {new_count}  |  Updated: {updated_count}  |  Total in store: {len(classes)}")

    if args.dry_run:
        print("\n--- Dry run output ---\n")

    for item in classes:
        is_imported = item.get("machine_id") == args.machine_id
        marker = " *" if is_imported else ""
        asp = item.get("aspirate", {})
        dsp = item.get("dispense", {})
        pts = len((item.get("equation") or {}).get("control_points", []))
        print(
            f"  {item['name']}{marker}\n"
            f"    head={item.get('head_type', '?')}  tip_cap={item.get('tip_capacity_ul', '?')} uL  "
            f"machine={item.get('machine_id', '?')}\n"
            f"    asp: vel={asp.get('w_velocity_ul_s', '?')} uL/s  "
            f"accel={asp.get('w_acceleration_ul_s2', '?')} uL/s^2  "
            f"delay={asp.get('post_delay_ms', 0)}ms\n"
            f"    dsp: vel={dsp.get('w_velocity_ul_s', '?')} uL/s  "
            f"accel={dsp.get('w_acceleration_ul_s2', '?')} uL/s^2  "
            f"delay={dsp.get('post_delay_ms', 0)}ms\n"
            f"    z_in: asp={asp.get('z_in_velocity_mm_s', '?')} mm/s  "
            f"dsp={dsp.get('z_in_velocity_mm_s', '?')} mm/s\n"
            f"    z_out: asp={asp.get('z_out_velocity_mm_s', '?')} mm/s  "
            f"dsp={dsp.get('z_out_velocity_mm_s', '?')} mm/s\n"
            f"    correction: {pts} control points"
        )

    if not args.dry_run:
        print(f"\nWritten to {args.output}")
    else:
        print(f"\n(* = imported from {args.reg_file.name})")
        print("\nHead type inference from entry names:")
        print("  384 disposable -> HT_384_D_70")
        print("  96 disposable (<=70uL) -> HT_96_D_70")
        print("  96 disposable (>70uL) -> HT_96_D_200")
        print("  fixed (<=50uL) -> HT_96_F_50")
        print("  fixed (>50uL) -> HT_96_F_200")
        print("  Use --head-type to override for all entries")


if __name__ == "__main__":
    main()
