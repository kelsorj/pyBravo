"""The two JOINT_AXIS_MAP copies must agree, or the two viewers disagree.

The designer (robot-scene.js) and the control panel (main.js) each render the
same URDF from their own copy of the joint→axis table. The duplication is
deliberate — a cross-module import would dodge the mtime cache-busting that
only page-level URLs receive — but it means the two 3D views silently drift
apart when someone recalibrates one copy and not the other. That happened: the
X/Y/Z visual datums were corrected in robot-scene.js while main.js kept the
old values, so the designer drew the head over the wells and the control panel
drew it 10.4 mm away.

This test parses both source files and compares the maps numerically.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[1] / "frontend" / "src"

_MAP_RE = re.compile(r"const JOINT_AXIS_MAP = \{(.*?)\n\};", re.S)
_ENTRY_RE = re.compile(r"'([a-z-]+)':\s*\{([^{}]*)\}", re.S)
_FIELD_RE = re.compile(
    r"(bravoAxis|homeOffset|scale|coupledScale|useTeachTipLength|coupledAxis)"
    r"\s*:\s*('[^']*'|true|false|-?[\d.]+)"
)


def _parse_map(path: Path) -> dict[str, dict]:
    src = path.read_text(encoding="utf-8")
    # Strip comments so prose cannot satisfy (or hide) a field.
    src = re.sub(r"//[^\n]*", "", src)
    match = _MAP_RE.search(src)
    if match is None:
        pytest.fail(f"could not find JOINT_AXIS_MAP in {path.name}")
    entries: dict[str, dict] = {}
    for joint, body in _ENTRY_RE.findall(match.group(1)):
        fields: dict = {}
        for key, raw in _FIELD_RE.findall(body):
            if raw in ("true", "false"):
                fields[key] = raw == "true"
            elif raw.startswith("'"):
                fields[key] = raw.strip("'")
            else:
                fields[key] = float(raw)
        entries[joint] = fields
    return entries


def test_designer_and_control_panel_joint_datums_match():
    scene = _parse_map(FRONTEND / "robot-scene.js")
    panel = _parse_map(FRONTEND / "main.js")

    assert set(scene) == set(panel), (
        f"joint sets differ: robot-scene {sorted(scene)} vs main {sorted(panel)}"
    )
    mismatches = {
        joint: {"robot-scene.js": scene[joint], "main.js": panel[joint]}
        for joint in scene
        if scene[joint] != panel[joint]
    }
    assert not mismatches, (
        "JOINT_AXIS_MAP copies drifted apart — the designer and the control "
        "panel now draw the robot in different places for the same commanded "
        "position:\n" + json.dumps(mismatches, indent=2)
    )


def test_the_measured_datums_are_the_ones_in_force():
    """Pin the calibrated values so a revert is loud (docs/urdf-alignment.md)."""
    for name in ("robot-scene.js", "main.js"):
        m = _parse_map(FRONTEND / name)
        assert m["xaxis"]["homeOffset"] == pytest.approx(182.64), name
        assert m["yaxis"]["homeOffset"] == pytest.approx(2.3), name
        assert m["zaxis"].get("useTeachTipLength") is True, (
            f"{name}: Z must be referenced to the teach tip, not the barrel face"
        )
