"""Parse legacy Bravo .reg profile exports into BravoProfile.

Legacy Bravo control software stores per-machine configuration in the
Windows registry under
``HKLM\\SOFTWARE\\WOW6432Node\\Velocity11\\Bravo2\\Profiles\\<NAME>``. Sites
share configuration across machines by exporting that subtree to a ``.reg``
file (UTF-16LE encoded). This module parses such a file and emits a
``BravoProfile`` with everything we know how to map; unmapped values land in
``profile.extra`` and the caller receives a list of human-readable warnings
for fields requiring manual review (notably the head type and default tip
enums, whose registry numeric values are not documented in this codebase).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pybravo.deck.teachpoints import Teachpoints
from pybravo.motion.axes import AxisConfig, get_default_axis_config
from pybravo.profile.profile import BravoProfile
from pybravo.tips import get_tip_capacity_ul, get_tip_id_for_capacity
from pybravo.types import Axis, AxisRange, HeadType, SpeedLevel, SpeedProfile

_PROFILE_SECTION_RE = re.compile(
    r"\[HKEY_LOCAL_MACHINE\\SOFTWARE\\WOW6432Node\\Velocity11\\Bravo2\\Profiles\\([^\\\]]+)(?:\\(.*?))?\]"
)
_KV_RE = re.compile(r'^"((?:[^"\\]|\\.)*)"=(.*)$')

_VALID_AXES = {"X", "Y", "Z", "W", "G", "Zg"}

# Legacy Bravo2 registry "Head type" integer → pybravo HeadType.
#
# The registry does NOT use the same integer values as :class:`HeadType`, so this
# is an explicit translation table. Values are confirmed only when we have a
# .reg/.dat sample from a machine running that head — unknown values still
# surface as a warning and land in ``profile.extra``.
_REGISTRY_HEAD_TYPE_MAP: dict[int, HeadType] = {
    1: HeadType.HT_384_D_70,   # confirmed against a 384ST profile export
    3: HeadType.HT_96_D_200,   # confirmed against a 96LT profile export
}

_AXIS_KEY_MAP: dict[str, tuple[str, type]] = {
    "Ticks per engineering unit": ("ticks_per_eng_unit", float),
    "Min range": ("min_range", float),
    "Max range": ("max_range", float),
    "Homing offset": ("homing_offset", float),
    "Home flag register": ("home_flag_register", int),
    "Home flag bitmask": ("home_flag_bitmask", int),
    "Home in positive direction": ("home_in_positive_direction", bool),
    "Home complete register": ("home_complete_register", int),
    "Homing soft stop deceleration": ("homing_soft_stop_decel", float),
    "Minimum move distance at full accel": ("min_move_full_accel", float),
    "Check for alignment": ("check_for_alignment", bool),
    "Fast velocity": ("fast_velocity", float),
    "Medium velocity": ("med_velocity", float),
    "Slow velocity": ("slow_velocity", float),
    "Homing velocity": ("homing_velocity", float),
    "Safe velocity": ("safe_velocity", float),
    "Fast acceleration": ("fast_acceleration", float),
    "Medium acceleration": ("med_acceleration", float),
    "Slow acceleration": ("slow_acceleration", float),
    "Homing acceleration": ("homing_acceleration", float),
    "Safe acceleration": ("safe_acceleration", float),
}


def _read_text(path: Path | str) -> str:
    """Read a .reg file. Registry exports are UTF-16LE with BOM; fall back to
    UTF-8 for hand-edited files."""
    raw = Path(path).read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig", errors="replace")


def _unescape(s: str) -> str:
    return s.replace('\\"', '"').replace('\\\\', '\\')


def _parse_value(raw: str) -> Any:
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"'):
        return _unescape(raw[1:-1])
    if raw.startswith("dword:"):
        try:
            return int(raw.split(":", 1)[1], 16)
        except ValueError:
            return None
    if raw.startswith(("hex:", "hex(")):
        return None
    return raw


def parse_reg(text: str) -> tuple[str | None, dict[str, dict[str, Any]]]:
    """Return (profile_name, {section_path: {key: value}}).

    Section paths are *relative* to ``Profiles\\<NAME>`` — the empty string is
    the profile root, ``"Axes\\X"`` is the X-axis subkey, etc. Sections that
    don't fall under a Bravo2 Profiles tree are ignored, as are sections that
    reference a *different* profile name in the same export."""
    sections: dict[str, dict[str, Any]] = {}
    profile_name: str | None = None
    cur_path: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("["):
            m = _PROFILE_SECTION_RE.match(line)
            if not m:
                cur_path = None
                continue
            name = m.group(1)
            sub = m.group(2) or ""
            if profile_name is None:
                profile_name = name
            elif name != profile_name:
                cur_path = None
                continue
            cur_path = sub
            sections.setdefault(cur_path, {})
            continue
        if cur_path is None:
            continue
        m = _KV_RE.match(line)
        if not m:
            continue
        key = _unescape(m.group(1))
        sections[cur_path][key] = _parse_value(m.group(2))
    return profile_name, sections


def _to_float(v: Any, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _to_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _to_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes")
    return False


def _apply_root(profile: BravoProfile, root: dict[str, Any], warnings: list[str]) -> None:
    if "Uses ethernet" in root:
        profile.connection.use_ethernet = _to_bool(root["Uses ethernet"])
    if "Communication ID" in root and root["Communication ID"]:
        profile.connection.machine_id = str(root["Communication ID"])

    s = profile.safety
    if "Approach height" in root:
        s.approach_height = _to_float(root["Approach height"], s.approach_height)
    if "Z-axis safe position" in root:
        s.z_safe_position = _to_float(root["Z-axis safe position"], s.z_safe_position)
    if "W-axis tips-off position" in root:
        s.tips_off_w_position = _to_float(root["W-axis tips-off position"], s.tips_off_w_position)
    if "Z-axis tips-off offset" in root:
        s.tips_off_z_offset = _to_float(root["Z-axis tips-off offset"], s.tips_off_z_offset)
    if "Tips Off Tip Touch Distance" in root:
        s.tips_off_tip_touch_distance = _to_float(root["Tips Off Tip Touch Distance"], s.tips_off_tip_touch_distance)
    if "Head type tolerance" in root:
        s.head_tolerance = _to_int(root["Head type tolerance"], s.head_tolerance)
    if "Safe location" in root:
        s.safe_location = _to_int(root["Safe location"], s.safe_location)
    if "Tip press dwell time" in root:
        s.tip_press_dwell_time = _to_int(root["Tip press dwell time"], s.tip_press_dwell_time)
    if "Plate sensor transient (ms)" in root:
        s.plate_sensor_transient_ms = _to_int(root["Plate sensor transient (ms)"], s.plate_sensor_transient_ms)
    if "Prompt user to home W-axis" in root:
        s.prompt_home_w = _to_bool(root["Prompt user to home W-axis"])
    if "Ignore W-axis" in root:
        s.ignore_w_axis = _to_bool(root["Ignore W-axis"])
    if "Run protocol at medium speed" in root:
        s.run_medium_speed = _to_bool(root["Run protocol at medium speed"])
    if "Always move to safe Z before all processes" in root:
        s.always_move_to_safe_z = _to_bool(root["Always move to safe Z before all processes"])
    if "Ignore plate sensor during pick and place" in root:
        s.ignore_plate_sensor = _to_bool(root["Ignore plate sensor during pick and place"])
    if 'Allow "top of stack" fluid handling' in root:
        s.allow_tos_fluid_handling = _to_bool(root['Allow "top of stack" fluid handling'])
    if "Enable Tips Off Tip Touch" in root:
        s.enable_tips_off_tip_touch = _to_bool(root["Enable Tips Off Tip Touch"])
    if "Enable Tips On Tip Touch" in root:
        s.enable_tips_on_tip_touch = _to_bool(root["Enable Tips On Tip Touch"])
    if "This is a Bravo SRT" in root:
        s.is_srt = _to_bool(root["This is a Bravo SRT"])
    if "Simulate Bravo hardware" in root:
        s.simulation_mode = _to_bool(root["Simulate Bravo hardware"])
    if "Prevent Bravo operation during robotic access" in root:
        s.prevent_bravo_during_robotic_access = _to_bool(root["Prevent Bravo operation during robotic access"])
    if "PinTool Tip Type" in root and root["PinTool Tip Type"]:
        s.pin_tool_tip_type = str(root["PinTool Tip Type"])

    if "Check head type on initialize" in root:
        profile.head.check_on_init = _to_bool(root["Check head type on initialize"])

    extra: dict[str, Any] = {}
    mapped_head: HeadType | None = None
    if "Head type" in root:
        extra["registry_head_type"] = root["Head type"]
        raw = root["Head type"]
        key = raw if isinstance(raw, int) else _to_int(raw, -1)
        mapped_head = _REGISTRY_HEAD_TYPE_MAP.get(key)
        if mapped_head is not None:
            profile.head.head_type = mapped_head
        else:
            warnings.append(
                f"Registry 'Head type' = {raw!r}; no pybravo HeadType mapping is "
                "known for this value — verify head type in the Profiles tab after import."
            )
    if "Default tip" in root:
        extra["registry_default_tip"] = root["Default tip"]
        raw_tip = root["Default tip"]
        capacity_ul = _to_float(raw_tip, 0.0)
        head_for_tip = mapped_head or profile.head.head_type
        tip_id = get_tip_id_for_capacity(head_for_tip, capacity_ul) if capacity_ul else None
        if tip_id:
            profile.head.default_tip_id = tip_id
            profile.head.teach_tip_id = tip_id
            resolved_capacity = get_tip_capacity_ul(head_for_tip, tip_id)
            profile.head.default_tip_capacity = resolved_capacity
            profile.head.teach_tip_capacity = resolved_capacity
        else:
            warnings.append(
                f"Registry 'Default tip' = {raw_tip!r} (µL) has no matching tip "
                f"definition for head {head_for_tip.name if isinstance(head_for_tip, HeadType) else head_for_tip} — "
                "verify the teach tip selection after import."
            )
    if "Default tip additional" in root:
        extra["registry_default_tip_additional"] = root["Default tip additional"]
    if "Head type A/D register" in root:
        extra["registry_head_type_ad_register"] = root["Head type A/D register"]
    if "Z offset (mm) for single tip pressing on ST head" in root:
        extra["registry_st_single_tip_z_offset"] = root["Z offset (mm) for single tip pressing on ST head"]
    if extra:
        profile.extra = (profile.extra or {}) | extra


def _apply_gripper(profile: BravoProfile, gs: dict[str, Any]) -> None:
    g = profile.gripper
    if "Gripper Y offset" in gs:
        g.y_offset = _to_float(gs["Gripper Y offset"], g.y_offset)
    if "Gripper position" in gs:
        g.gripper_position = _to_float(gs["Gripper position"], g.gripper_position)
    if "Grip current" in gs:
        g.grip_current = _to_float(gs["Grip current"], g.grip_current)
    if "Lid Grip current" in gs:
        g.lid_grip_current = _to_float(gs["Lid Grip current"], g.lid_grip_current)


def _apply_axis(profile: BravoProfile, axis_name: str, kv: dict[str, Any]) -> None:
    try:
        axis_enum = Axis[axis_name]
    except KeyError:
        return
    base = profile.axes.get(axis_name) or get_default_axis_config(axis_enum)

    min_p = base.range.min_pos
    max_p = base.range.max_pos
    if "Min range" in kv:
        min_p = _to_float(kv["Min range"], min_p)
    if "Max range" in kv:
        max_p = _to_float(kv["Max range"], max_p)
    rng = AxisRange(min_p, max_p)

    speeds = dict(base.speeds)
    for level in SpeedLevel:
        v_key = f"{level.name.title()} velocity"
        a_key = f"{level.name.title()} acceleration"
        v_raw = kv.get(v_key)
        a_raw = kv.get(a_key)
        if v_raw is not None and a_raw is not None:
            speeds[level] = SpeedProfile(
                _to_float(v_raw, base.speeds[level].velocity if level in base.speeds else 0.0),
                _to_float(a_raw, base.speeds[level].acceleration if level in base.speeds else 0.0),
            )
    # registry uses "Medium" → SpeedLevel.MED
    if "Medium velocity" in kv and "Medium acceleration" in kv:
        speeds[SpeedLevel.MED] = SpeedProfile(
            _to_float(kv["Medium velocity"], 0.0),
            _to_float(kv["Medium acceleration"], 0.0),
        )

    profile.axes[axis_name] = AxisConfig(
        axis=axis_enum,
        ticks_per_eng_unit=_to_float(kv.get("Ticks per engineering unit"), base.ticks_per_eng_unit),
        range=rng,
        homing_offset=_to_float(kv.get("Homing offset"), base.homing_offset),
        home_in_positive_direction=_to_bool(kv.get("Home in positive direction", base.home_in_positive_direction)),
        home_flag_bitmask=_to_int(kv.get("Home flag bitmask"), base.home_flag_bitmask),
        home_flag_register=_to_int(kv.get("Home flag register"), base.home_flag_register),
        home_complete_register=_to_int(kv.get("Home complete register"), base.home_complete_register),
        homing_soft_stop_decel=_to_float(kv.get("Homing soft stop deceleration"), getattr(base, "homing_soft_stop_decel", 300.0)),
        min_move_full_accel=_to_float(kv.get("Minimum move distance at full accel"), getattr(base, "min_move_full_accel", 0.0)),
        check_for_alignment=_to_bool(kv.get("Check for alignment", getattr(base, "check_for_alignment", True))),
        speeds=speeds,
    )


def _apply_teachpoints(profile: BravoProfile, sections: dict[str, dict[str, Any]]) -> int:
    if profile.teachpoints is None:
        profile.teachpoints = Teachpoints()
    count = 0
    for path, kv in sections.items():
        if not path.startswith("Teachpoints\\Location "):
            continue
        try:
            loc = int(path.rsplit(" ", 1)[1])
        except (ValueError, IndexError):
            continue
        if "X" in kv:
            profile.teachpoints.set_teachpoint(loc, Axis.X, _to_float(kv["X"], 0.0))
        if "Y" in kv:
            profile.teachpoints.set_teachpoint(loc, Axis.Y, _to_float(kv["Y"], 0.0))
        if "Z" in kv:
            profile.teachpoints.set_teachpoint(loc, Axis.Z, _to_float(kv["Z"], 0.0))
        count += 1
    return count


def _apply_locations(profile: BravoProfile, sections: dict[str, dict[str, Any]]) -> None:
    locs: list[dict[str, Any]] = []
    for path, kv in sections.items():
        if not path.startswith("Locations\\Location "):
            continue
        try:
            n = int(path.rsplit(" ", 1)[1])
        except (ValueError, IndexError):
            continue
        locs.append({"location": n, "location_type": _to_int(kv.get("Location type"), 0)})
    if locs:
        locs.sort(key=lambda d: d["location"])
        profile.locations = locs


def _apply_current_limits(profile: BravoProfile, sections: dict[str, dict[str, Any]]) -> None:
    out: dict[str, dict[str, float]] = {}
    for path, kv in sections.items():
        if not path.startswith("Current limits\\"):
            continue
        head_kind = path.split("\\", 1)[1]
        out[head_kind] = {k: _to_float(v, 0.0) for k, v in kv.items()}
    if out:
        profile.current_limits = out


def _apply_external_robot_access(profile: BravoProfile, sections: dict[str, dict[str, Any]]) -> None:
    block: dict[str, Any] = {}
    root = sections.get("External robot access")
    if root:
        block["__root__"] = dict(root)
    for path, kv in sections.items():
        if not path.startswith("External robot access\\"):
            continue
        block[path.split("\\", 1)[1]] = dict(kv)
    if block:
        profile.external_robot_access = block


def reg_to_profile(text: str) -> tuple[BravoProfile, list[str]]:
    """Parse a .reg payload and return ``(BravoProfile, warnings)``.

    Raises ``ValueError`` if the payload contains no Bravo2 profile section."""
    name, sections = parse_reg(text)
    if name is None:
        raise ValueError("No Velocity11/Bravo2 profile section found in .reg payload")

    profile = BravoProfile.default()
    profile.name = name
    warnings: list[str] = []

    _apply_root(profile, sections.get("", {}), warnings)

    if "Gripper settings" in sections:
        _apply_gripper(profile, sections["Gripper settings"])

    for axis_name in _VALID_AXES:
        kv = sections.get(f"Axes\\{axis_name}")
        if kv:
            _apply_axis(profile, axis_name, kv)

    if "Axes\\W\\Motor Control Parameters" in sections:
        profile.w_axis_motor_control = dict(sections["Axes\\W\\Motor Control Parameters"])

    _apply_current_limits(profile, sections)
    _apply_locations(profile, sections)
    _apply_teachpoints(profile, sections)
    _apply_external_robot_access(profile, sections)

    return profile, warnings


def reg_file_to_profile(path: Path | str) -> tuple[BravoProfile, list[str]]:
    """Load a .reg file from disk (handling UTF-16 BOM) and convert it."""
    return reg_to_profile(_read_text(path))


def _dat_path_to_section(relative_path: str, profile_name: str) -> str | None:
    """Map a forward-slash path like ``"96LT/Axes/X/X.dat"`` to the registry
    sub-key ``"Axes\\X"``. The root .dat (``"<profile>/<profile>.dat"``) maps
    to ``""``. Returns ``None`` if the path does not match the expected
    ``<folder>/<folder>.dat`` convention."""
    parts = [p for p in relative_path.replace("\\", "/").split("/") if p]
    if not parts or not parts[-1].lower().endswith(".dat"):
        return None
    if parts[0] == profile_name:
        parts = parts[1:]
    if not parts:
        return None
    filename = parts[-1][:-4]  # strip .dat
    parent_dirs = parts[:-1]
    if not parent_dirs:
        # Root-level <profile>.dat — section is the profile root ("")
        return ""
    if filename != parent_dirs[-1]:
        # Permissive: accept mismatches rather than dropping data silently.
        pass
    return "\\".join(parent_dirs)


def dat_tree_to_reg(profile_name: str, files: dict[str, str]) -> str:
    """Synthesize an equivalent .reg payload from a ``.dat`` directory tree.

    ``files`` maps forward-slash relative paths (``"96LT/Axes/X/X.dat"``) to
    their text contents. Bravo2 stored the same key/value lines in legacy .dat
    files as the modern registry export — only the section header is missing —
    so we rebuild the headers and hand the result to :func:`reg_to_profile`.
    """
    header = "HKEY_LOCAL_MACHINE\\SOFTWARE\\WOW6432Node\\Velocity11\\Bravo2\\Profiles"
    out: list[str] = ["Windows Registry Editor Version 5.00", ""]
    sections: list[tuple[str, str]] = []
    for rel_path, body in files.items():
        section = _dat_path_to_section(rel_path, profile_name)
        if section is None:
            continue
        sections.append((section, body))
    # Emit root first, then nested sections in stable order.
    sections.sort(key=lambda it: (it[0] != "", it[0]))
    for section, body in sections:
        suffix = f"\\{section}" if section else ""
        out.append(f"[{header}\\{profile_name}{suffix}]")
        out.append(body.rstrip("\r\n"))
        out.append("")
    return "\n".join(out)


def dat_tree_to_profile(profile_name: str, files: dict[str, str]) -> tuple[BravoProfile, list[str]]:
    """Parse a legacy Bravo2 ``.dat`` directory tree into a :class:`BravoProfile`.

    The tree follows the convention ``<profile>/<section>/<section>.dat`` (with
    arbitrary nesting). ``files`` maps relative paths to file text. The
    top-level folder name is the profile name."""
    if not profile_name:
        raise ValueError("profile_name is required for .dat import")
    if not files:
        raise ValueError("No .dat files provided")
    synthesized = dat_tree_to_reg(profile_name, files)
    return reg_to_profile(synthesized)
