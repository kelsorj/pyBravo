"""Readiness gating in the web UI must never disable a button whose own job is
to clear the gate it is being blocked by.

This regression exists because the blanket loop over ``MOTION_BUTTON_IDS`` used
to overwrite the per-button gating applied just above it, leaving Initialize
disabled with the tooltip "Not initialized — press Initialize first." — advice
the user could not act on, because the control giving it was the one they
needed to press.

The gating lives in browser JavaScript, so the assertions run the real source
text from ``frontend/src/main.js`` under Node against a stub DOM. Tests skip
when Node is unavailable rather than failing.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

MAIN_JS = Path(__file__).resolve().parents[1] / "frontend" / "src" / "main.js"

# The declarations that make up the readiness logic, in dependency order.
_EXTRACTS: tuple[tuple[str, str], ...] = (
    ("MOTION_BUTTON_IDS", r"const MOTION_BUTTON_IDS = \[[\s\S]*?\];"),
    ("SELF_REMEDY_BUTTON_IDS", r"const SELF_REMEDY_BUTTON_IDS = new Set\(\[[^\]]*\]\);"),
    ("READINESS_GATES", r"const READINESS_GATES = \[[\s\S]*?\];"),
    ("readinessBlocker", r"function readinessBlocker\(\) \{[\s\S]*?\n\}"),
    ("setStatusButton", r"function setStatusButton\([\s\S]*?\n\}"),
    ("applyReadiness", r"function applyReadiness\(snapshot\) \{[\s\S]*?\n\}"),
    ("markReadiness", r"function markReadiness\(el, blocker\) \{[\s\S]*?\n\}"),
)

# Prelude and epilogue wrapped around the extracted source to make it a plain
# CommonJS module: a stub DOM in, gate state out. No dynamic evaluation.
_PRELUDE = """
class El {
    constructor() {
        this.dataset = {}; this.textContent = ''; this.title = '';
        const c = new Set();
        this.classList = {
            add: x => c.add(x), remove: x => c.delete(x), contains: x => c.has(x),
            toggle: (x, on) => on ? c.add(x) : c.delete(x),
        };
    }
    removeAttribute() { this.title = ''; }
}
const els = {};
for (const id of BUTTON_IDS) els[id] = new El();
const document = { getElementById: id => els[id] || null, querySelectorAll: () => [] };
const state = {};
"""

_EPILOGUE = """
applyReadiness(JSON.parse(process.argv[2]));
const out = {};
for (const [id, el] of Object.entries(els)) {
    out[id] = { blocked: el.classList.contains('not-ready'),
                why: el.dataset.notReady || null,
                text: el.textContent };
}
console.log(JSON.stringify(out));
"""


def _build_module(tmp_path: Path) -> Path:
    source = MAIN_JS.read_text()
    parts = []
    for label, pattern in _EXTRACTS:
        match = re.search(pattern, source)
        if match is None:
            pytest.fail(
                f"could not find {label} in frontend/src/main.js — the readiness "
                "logic was renamed or restructured; update this test to match"
            )
        parts.append(match.group(0))
    body = "\n\n".join(parts)

    button_ids = sorted(set(re.findall(r"'(btn-[a-z-]+)'", body)))
    prelude = _PRELUDE.replace("BUTTON_IDS", json.dumps(button_ids))

    module = tmp_path / "readiness_harness.js"
    module.write_text(prelude + "\n" + body + "\n" + _EPILOGUE)
    return module


def _gate_state(tmp_path: Path, **snapshot: bool) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; frontend gating assertions need it")
    module = _build_module(tmp_path)
    proc = subprocess.run(
        [node, str(module), json.dumps(snapshot)],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        pytest.fail(f"readiness harness failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


def test_initialize_is_clickable_once_connected(tmp_path):
    """The reported bug: connected but not initialized left Initialize disabled,
    with a tooltip telling the user to press Initialize."""
    state = _gate_state(tmp_path, connected=True, initialized=False, homed=False)

    init = state["btn-init"]
    assert not init["blocked"], (
        f"Initialize was disabled with tooltip {init['why']!r} — but pressing "
        "Initialize is exactly how the user clears that gate"
    )
    assert init["text"] == "Initialize"


def test_home_all_is_clickable_once_initialized(tmp_path):
    """Same trap one gate along: Home All must not be blocked by 'not homed'."""
    state = _gate_state(tmp_path, connected=True, initialized=True, homed=False)

    home = state["btn-home"]
    assert not home["blocked"], (
        f"Home All was disabled with tooltip {home['why']!r} — pressing Home All "
        "is how the user clears that gate"
    )


def test_no_button_is_blocked_by_its_own_remedy(tmp_path):
    """General form of the defect, across every readiness combination."""
    remedies = {
        "btn-connect": "press Connect",
        "btn-init": "press Initialize",
        "btn-home": "press Home All",
    }
    for connected in (False, True):
        for initialized in (False, True):
            for homed in (False, True):
                state = _gate_state(
                    tmp_path, connected=connected,
                    initialized=initialized, homed=homed,
                )
                for button, remedy in remedies.items():
                    entry = state.get(button)
                    if entry is None or not entry["blocked"]:
                        continue
                    assert remedy not in (entry["why"] or ""), (
                        f"{button} is disabled but its tooltip {entry['why']!r} "
                        f"tells the user to {remedy} — unactionable advice "
                        f"(connected={connected} initialized={initialized} homed={homed})"
                    )


@pytest.mark.parametrize(
    "button_id, endpoint",
    [
        ("btn-init", "/api/initialize"),
        ("btn-home", "/api/home"),
        ("btn-abort", "/api/abort"),
    ],
)
def test_header_action_buttons_are_wired(button_id, endpoint):
    """A gated button that nothing listens to is worse than a disabled one: it
    looks live, and clicking it does nothing at all.

    Initialize shipped in this state — present in the markup, gated by the
    readiness system, styled as ready once connected, and bound to no handler,
    so ``/api/initialize`` was only reachable from Reinitialize in Profiles.
    """
    js = MAIN_JS.read_text()
    html = (MAIN_JS.parents[2] / "frontend" / "index.html").read_text()

    assert f'id="{button_id}"' in html, f"{button_id} is missing from index.html"

    binding = re.search(
        rf"getElementById\('{re.escape(button_id)}'\)\??\.addEventListener\(\s*'click'",
        js,
    )
    assert binding is not None, (
        f"{button_id} exists in the markup but no click handler is bound to it "
        "in main.js — pressing it does nothing"
    )

    # The handler body should reach the endpoint the button is named for.
    tail = js[binding.start():binding.start() + 1200]
    assert endpoint in tail, (
        f"{button_id}'s click handler does not call {endpoint}"
    )


def test_motion_controls_stay_gated_until_fully_ready(tmp_path):
    """The gating still has to do its actual job."""
    partial = _gate_state(tmp_path, connected=True, initialized=True, homed=False)
    assert partial["btn-tp-move"]["blocked"]
    assert "Home All" in (partial["btn-tp-move"]["why"] or "")

    ready = _gate_state(tmp_path, connected=True, initialized=True, homed=True)
    assert not ready["btn-tp-move"]["blocked"]
    assert not ready["btn-open-gripper"]["blocked"]
