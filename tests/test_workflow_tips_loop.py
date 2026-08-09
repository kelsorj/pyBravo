"""Tips On / Tips Off inside a Loop must walk across the tip box.

Three iterations of a 16x2 head should consume six columns from the source box
and deposit six into the destination box, stepping inward from the mirror
corner each time.

Two defects broke this. The packing rule in `head_mode._is_legal_tipbox_anchor`
required the region outboard of a candidate block to be *empty* for returns as
well as pickups; once the first Tips Off had filled the corner, every remaining
anchor had occupied cells outboard of it, so a destination box went from 23
legal return anchors to zero and the second Tips Off raised "No legal tip
anchors are available for return". And `WorkflowExecutor._setup_deck` did not
pass `tipbox_fill_state` to `set_labware`, so a box the designer had marked
empty began the run full, leaving no room to return into even before that.
"""

from __future__ import annotations

import pytest

from pybravo.head_mode import legal_tipbox_anchors, normalize_head_mode
from pybravo.types import HeadType
from pybravo.workflow.executor import WorkflowExecutor
from tests.test_bravo_init import _make_workflow_executor_bravo

MODE = normalize_head_mode(HeadType.HT_384_D_70, "column", "back_left", None, 2)
ROWS, COLS = 16, 24


def _cols(*columns) -> set[tuple[int, int]]:
    return {(r, c) for r in range(ROWS) for c in columns}


def test_a_return_stays_legal_after_the_corner_is_filled():
    """The regression: filling the corner used to make every anchor illegal."""
    assert legal_tipbox_anchors(ROWS, COLS, MODE, _cols(22, 23), purpose="return")


@pytest.mark.parametrize(
    "occupied, expected_anchor",
    [
        (set(), 22),               # empty box: pack against the mirror corner
        (_cols(22, 23), 20),       # one block returned: sit flush inboard of it
        (_cols(20, 21, 22, 23), 18),
    ],
)
def test_returns_pack_inward_from_the_mirror_corner(occupied, expected_anchor):
    anchors = legal_tipbox_anchors(ROWS, COLS, MODE, occupied, purpose="return")
    assert [(a.row, a.col) for a in anchors] == [(0, expected_anchor)], (
        "a return should have exactly one legal placement — flush against what "
        "has already been returned — so a box never fragments"
    )


def test_pickup_rule_is_unchanged():
    """Pickups still consume from the corner inward; only returns changed."""
    full = {(r, c) for r in range(ROWS) for c in range(COLS)}
    assert [(a.row, a.col) for a in legal_tipbox_anchors(
        ROWS, COLS, MODE, full, purpose="pickup")] == [(0, 22)]
    # After the corner block is gone, the next block inward is the only pickup.
    assert [(a.row, a.col) for a in legal_tipbox_anchors(
        ROWS, COLS, MODE, full - _cols(22, 23), purpose="pickup")] == [(0, 20)]


@pytest.mark.asyncio
async def test_setup_deck_honours_the_designer_tipbox_fill_state():
    """A box the designer marked empty must not start the run full."""
    bravo, _ = _make_workflow_executor_bravo()
    # A tip box on a riser — the common layout, and the one where only the
    # riser goes through set_labware.
    deck = {"4": [{"labware_id": "tipbox-riser", "height_mm": 20.0},
                  {"labware_id": "tipbox-384", "tipbox_fill_state": "empty"}]}
    executor = WorkflowExecutor(bravo, {"nodes": []}, deck_config=deck)
    await executor._setup_deck()

    assert bravo._occupied_tip_wells(4) == set(), (
        "tipbox_fill_state='empty' was dropped on the way to set_labware, so the "
        "destination box started full and had nowhere to return tips"
    )


@pytest.mark.asyncio
async def test_three_loop_iterations_move_six_columns():
    """The reported scenario, end to end."""
    bravo, _ = _make_workflow_executor_bravo()
    source, dest = 1, 4
    riser = {"labware_id": "tipbox-riser", "height_mm": 20.0}
    deck = {
        str(source): [riser, {"labware_id": "tipbox-384", "tipbox_fill_state": "full"}],
        str(dest): [riser, {"labware_id": "tipbox-384", "tipbox_fill_state": "empty"}],
    }
    executor = WorkflowExecutor(bravo, {"nodes": []}, deck_config=deck)
    await executor._setup_deck()
    bravo.set_head_mode("column", "back_left", column_count=2)

    picked = []
    for _ in range(3):
        bravo._tip_selection = None
        await bravo.tips_on(source)
        picked.append(bravo._tips_on_head_selection.col)
        bravo._tip_selection = None
        await bravo.tips_off(dest)

    assert picked == [22, 20, 18], "pickup did not step inward across iterations"
    filled = sorted({c for _, c in bravo._occupied_tip_wells(dest)})
    assert filled == [18, 19, 20, 21, 22, 23]
    assert len(bravo._occupied_tip_wells(source)) == ROWS * COLS - 6 * ROWS
