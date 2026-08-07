"""Delid and relid must be refused, visibly, before they are ever sent.

The Processes tab used to render a plate identically whether or not it carried
a lid — ``describeDeckLocationLabware`` returned only ``detail.name`` — so an
operator selecting a lidless plate for "Delid Plate" had nothing to go on. The
backend refused correctly, but the refusal arrived as a generic
``API error: ...`` line in the log panel, after the fact, easily missed among
other traffic. The deck payload has carried ``is_lidded`` the whole time; the
UI simply discarded it.

The gating lives in browser JavaScript, so these assertions run the real source
text from ``frontend/src/main.js`` under Node. Tests skip when Node is
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
    ("describeLidState", r"function describeLidState\([\s\S]*?\n\}"),
    ("delidPreflightBlocker", r"function delidPreflightBlocker\([\s\S]*?\n\}"),
    ("relidPreflightBlocker", r"function relidPreflightBlocker\([\s\S]*?\n\}"),
)

_EPILOGUE = """
const input = JSON.parse(process.argv[2]);
const out = {
    describeLidState: describeLidState(input.detail),
    delid: delidPreflightBlocker(
        input.plateLocation, input.lidDestination, input.plateDetail, input.destDetail),
    relid: relidPreflightBlocker(
        input.lidLocation, input.plateLocation, input.lidDetail, input.plateDetail),
};
console.log(JSON.stringify(out));
"""


def _build_module(tmp_path: Path) -> Path:
    source = MAIN_JS.read_text(encoding="utf-8")
    parts = []
    for label, pattern in _EXTRACTS:
        match = re.search(pattern, source)
        if match is None:
            pytest.fail(
                f"could not find {label} in frontend/src/main.js — the lid "
                "pre-flight logic was renamed or restructured; update this test"
            )
        parts.append(match.group(0))
    module = tmp_path / "lid_preflight_harness.js"
    module.write_text("\n\n".join(parts) + "\n" + _EPILOGUE, encoding="utf-8")
    return module


def _run(tmp_path: Path, **payload) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; lid pre-flight assertions need it")
    module = _build_module(tmp_path)
    payload.setdefault("detail", None)
    payload.setdefault("plateLocation", 5)
    payload.setdefault("lidDestination", 9)
    payload.setdefault("lidLocation", 9)
    payload.setdefault("plateDetail", None)
    payload.setdefault("destDetail", None)
    payload.setdefault("lidDetail", None)
    proc = subprocess.run(
        [node, str(module), json.dumps(payload)],
        capture_output=True, text=True, timeout=180,
    )
    if proc.returncode != 0:
        pytest.fail(f"lid pre-flight harness failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


PLATE = {"name": "384 Greiner 781091 PS uclear", "can_have_lid": True}
LIDDED = {**PLATE, "is_lidded": True}
BARE = {**PLATE, "is_lidded": False}
LID = {"name": "Lid", "base_class": "lid", "is_lidded": False}


def test_lid_state_is_visible_in_the_label(tmp_path):
    """The operator must be able to tell lidded from bare before pressing run."""
    lidded = _run(tmp_path, detail=LIDDED)["describeLidState"]
    bare = _run(tmp_path, detail=BARE)["describeLidState"]
    assert lidded != bare, "lidded and bare plates rendered identically"
    assert "no lid" in bare.lower()


def test_delid_is_blocked_when_the_plate_has_no_lid(tmp_path):
    """The reported bug: delid on a bare plate must refuse before dispatch."""
    blocker = _run(tmp_path, plateDetail=BARE, destDetail=None)["delid"]
    assert blocker, "delid on a lidless plate was allowed through"
    assert "lid" in blocker.lower()
    assert "5" in blocker, "the message should name the offending location"


def test_delid_is_allowed_when_the_plate_is_lidded(tmp_path):
    assert _run(tmp_path, plateDetail=LIDDED, destDetail=None)["delid"] is None


def test_delid_is_blocked_on_an_empty_location(tmp_path):
    blocker = _run(tmp_path, plateDetail=None, destDetail=None)["delid"]
    assert blocker, "delid on an empty location was allowed through"


def test_delid_is_blocked_when_the_lid_destination_is_occupied(tmp_path):
    """Dropping a lid onto occupied labware would collide."""
    blocker = _run(tmp_path, plateDetail=LIDDED, destDetail=BARE)["delid"]
    assert blocker, "delid onto an occupied destination was allowed through"
    assert "9" in blocker


def test_relid_is_blocked_when_the_plate_is_already_lidded(tmp_path):
    blocker = _run(tmp_path, lidDetail=LID, plateDetail=LIDDED)["relid"]
    assert blocker, "relid onto an already-lidded plate was allowed through"


def test_relid_is_blocked_when_the_source_holds_no_lid(tmp_path):
    blocker = _run(tmp_path, lidDetail=BARE, plateDetail=BARE)["relid"]
    assert blocker, "relid from a location with no lid was allowed through"


def test_relid_is_allowed_for_a_lid_and_a_bare_plate(tmp_path):
    assert _run(tmp_path, lidDetail=LID, plateDetail=BARE)["relid"] is None
