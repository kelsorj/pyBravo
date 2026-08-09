"""The jelly-skin shortcut must match one precise chord and nothing else.

Two things make this worth pinning. First, the chord is matched on
``event.code`` rather than ``event.key``: on macOS the Option key rewrites the
character, so Option+Shift+V arrives as ``◊`` and matching the letter would
silently depend on keyboard layout. Second, the control panel binds bare
letters to camera presets — ``v`` among them — so a regression that loosened
the modifier test would make the two shortcuts fight.

The matcher lives in browser JavaScript, so these assertions run the real
source text from ``frontend/src/main.js`` under Node. Tests skip when Node is
unavailable rather than failing.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

MAIN_JS = Path(__file__).resolve().parents[1] / "frontend" / "src" / "main.js"

_EXTRACTS: tuple[tuple[str, str], ...] = (
    ("isJellyToggleChord", r"function isJellyToggleChord\([\s\S]*?\n\}"),
    ("VIEW_KEYS", r"const VIEW_KEYS = \{[^}]*\};"),
)

_EPILOGUE = """
const ev = JSON.parse(process.argv[2]);
console.log(JSON.stringify({
    matches: isJellyToggleChord(ev),
    viewPreset: VIEW_KEYS[(ev.key || '').toLowerCase()] || null,
}));
"""


def _build_module(tmp_path: Path) -> Path:
    source = MAIN_JS.read_text(encoding="utf-8")
    parts = []
    for label, pattern in _EXTRACTS:
        match = re.search(pattern, source)
        if match is None:
            pytest.fail(
                f"could not find {label} in frontend/src/main.js — the jelly "
                "toggle logic was renamed or restructured; update this test"
            )
        parts.append(match.group(0))
    module = tmp_path / "jelly_toggle_harness.js"
    module.write_text("\n\n".join(parts) + "\n" + _EPILOGUE, encoding="utf-8")
    return module


def _chord(tmp_path: Path, **event) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; jelly toggle assertions need it")
    payload = {
        "code": "KeyV", "key": "v",
        "metaKey": False, "ctrlKey": False, "altKey": False, "shiftKey": False,
    }
    payload.update(event)
    proc = subprocess.run(
        [node, str(_build_module(tmp_path)), json.dumps(payload)],
        capture_output=True, text=True, timeout=180,
    )
    if proc.returncode != 0:
        pytest.fail(f"jelly toggle harness failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


def test_cmd_option_shift_v_matches(tmp_path):
    assert _chord(tmp_path, metaKey=True, altKey=True, shiftKey=True)["matches"]


def test_ctrl_option_shift_v_matches_for_windows_and_linux(tmp_path):
    """Labs running these instruments are overwhelmingly Windows."""
    assert _chord(tmp_path, ctrlKey=True, altKey=True, shiftKey=True)["matches"]


def test_matching_uses_code_not_key(tmp_path):
    """On macOS, Option+Shift+V reports key='◊'. The chord must still match."""
    result = _chord(tmp_path, key="◊", metaKey=True, altKey=True, shiftKey=True)
    assert result["matches"], "matcher regressed to event.key and is layout-dependent"


def test_a_different_physical_key_does_not_match(tmp_path):
    assert not _chord(
        tmp_path, code="KeyB", key="b", metaKey=True, altKey=True, shiftKey=True
    )["matches"]


@pytest.mark.parametrize(
    "missing", [{"altKey": False}, {"shiftKey": False}, {"metaKey": False, "ctrlKey": False}]
)
def test_every_modifier_is_required(tmp_path, missing):
    event = {"metaKey": True, "altKey": True, "shiftKey": True, **missing}
    assert not _chord(tmp_path, **event)["matches"]


def test_bare_v_does_not_toggle_the_skin(tmp_path):
    """Bare v is the camera's bottom-view preset and must stay that way."""
    result = _chord(tmp_path)
    assert not result["matches"]
    assert result["viewPreset"] == "bottom"


INDEX_HTML = Path(__file__).resolve().parents[1] / "frontend" / "index.html"

# `transform:` but not `transform-origin:` — the hyphen prevents a match.
_TRANSFORM_DECL = re.compile(r"transform\s*:\s*([^;]+);")
_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")


def _jelly_rules() -> list[tuple[str, str]]:
    """Every CSS rule in index.html scoped to the jelly skin.

    Comments are stripped first: the rules here carry long explanatory
    comments, and a phrase like ":not(.not-ready)" appearing in prose above a
    rule would otherwise satisfy the guard check without guarding anything.
    """
    css = re.sub(r"/\*.*?\*/", "", INDEX_HTML.read_text(encoding="utf-8"), flags=re.S)
    rules = [
        (sel.strip(), body)
        for sel, body in _RULE.findall(css)
        if 'data-jelly="on"' in sel
    ]
    if not rules:
        pytest.fail("no [data-jelly] rules found in frontend/index.html")
    return rules


def test_no_jelly_rule_can_move_a_gated_control():
    """The safety property, pinned.

    A gated motion control that squashes under a click tells the operator the
    press registered when it did not. This caught a real defect: the jog guard
    was originally only `:not(.motion-disabled)`, but markReadiness (main.js)
    also adds `.not-ready` to `.jog-btn[data-jog]`, and unlike motion-disabled
    that class does not set pointer-events:none.
    """
    offenders = []
    for selector, body in _jelly_rules():
        moves = [
            v.strip() for v in _TRANSFORM_DECL.findall(body)
            if v.strip().split()[0] != "none"
        ]
        if not moves:
            continue
        required = [":not([disabled])", ":not(:disabled)", ":not(.not-ready)"]
        if ".jog-btn" in selector:
            required = [":not(.not-ready)", ":not(.motion-disabled)"]
        missing = [g for g in required if g not in selector]
        if missing:
            offenders.append(f"{selector!r} sets transform {moves} but lacks {missing}")
    assert not offenders, "jelly rules can move a gated control:\n" + "\n".join(offenders)


def test_gated_controls_are_explicitly_reset():
    """Not merely 'no rule matches' — an explicit, !important inert state."""
    css = INDEX_HTML.read_text(encoding="utf-8")
    inert = [
        body for sel, body in _jelly_rules()
        if ".not-ready" in sel and "transform: none !important" in body
    ]
    assert inert, "no !important inert block for gated controls"
    for prop in ("animation", "box-shadow", "filter", "cursor"):
        assert any(f"{prop}:" in b for b in inert), f"inert block does not reset {prop}"
    for gated in (".btn[disabled]", ".btn:disabled", ".btn.not-ready",
                  ".jog-btn.not-ready", ".jog-btn.motion-disabled"):
        assert f'[data-jelly="on"] {gated}' in css, f"{gated} is not covered by the inert block"


def test_the_toggle_chord_does_not_also_hit_a_camera_preset(tmp_path):
    """v is in VIEW_KEYS, so the two shortcuts must not both fire.

    main.js returns early on the jelly chord, and the view-preset lookup now
    bails on any modified keypress; this pins the overlap that makes both
    guards necessary.
    """
    result = _chord(tmp_path, metaKey=True, altKey=True, shiftKey=True)
    assert result["matches"]
    assert result["viewPreset"] == "bottom", (
        "VIEW_KEYS no longer maps v — if that changed deliberately, this test "
        "and the modifier guard in main.js should be revisited together"
    )
