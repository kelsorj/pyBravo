"""Tips On / Tips Off inside a Loop must walk across the tip box.

Three iterations of a 16x2 head should consume six columns from the source box
and deposit six into the destination, stepping one block at a time. A pickup
always consumes inward from the mirror corner; a return starts wherever the
operator anchors it and then walks on in that direction.

Five defects broke this, and they are worth naming because they interlock:

1. `head_mode._is_legal_tipbox_anchor` ran the pickup packing rule for returns
   too, demanding the outboard side be empty. Once the first Tips Off landed,
   every remaining anchor had occupied cells outboard of it, so a box went from
   23 legal return anchors to zero and the second Tips Off raised "No legal tip
   anchors are available for return".
2. `WorkflowExecutor._setup_deck` dropped `tipbox_fill_state`, so a box the
   designer marked empty began the run full — no room to return into at all.
3. Only the bottom entry of a stack goes through `set_labware`, and a tip box
   normally sits on a riser, so stacked boxes never had occupancy initialised.
4. `_set_removed_tip_cells` assigned instead of accumulating, so each pickup
   erased the record of the previous one and earlier tips reappeared on screen.
5. A box that started empty had no removed-cell baseline, so returning tips
   into it had nothing to subtract from and they never rendered.
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


def test_the_operator_picks_the_first_return_anchor():
    """An empty box imposes no direction — every placement is offered."""
    anchors = legal_tipbox_anchors(ROWS, COLS, MODE, set(), purpose="return")
    assert len(anchors) == COLS - MODE.column_count + 1 == 23


@pytest.mark.parametrize(
    "occupied, expected",
    [
        (_cols(22, 23), [20]),              # started at the right edge -> walk left
        (_cols(20, 21, 22, 23), [18]),
        (_cols(0, 1), [2]),                # started at the left edge  -> walk right
        (_cols(0, 1, 2, 3), [4]),
        (_cols(10, 11), [8, 12]),          # started mid-box -> either way, then committed
    ],
)
def test_later_returns_sit_flush_against_what_is_there(occupied, expected):
    anchors = legal_tipbox_anchors(ROWS, COLS, MODE, occupied, purpose="return")
    assert sorted(a.col for a in anchors) == expected, (
        "a return must abut the filled region so the head walks across the box "
        "in one direction and the box never fragments"
    )


def test_row_mode_returns_walk_down_rows():
    """The same rule on the other axis: a full-row head steps by rows."""
    row_mode = normalize_head_mode(HeadType.HT_384_D_70, "row", "back_left", 2, None)
    assert row_mode.row_count == 2 and row_mode.column_count == COLS
    occupied = {(r, c) for r in (0, 1) for c in range(COLS)}
    anchors = legal_tipbox_anchors(ROWS, COLS, row_mode, occupied, purpose="return")
    assert sorted(a.row for a in anchors) == [2], "row mode should step by rows"


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


# ── Viewer state ──────────────────────────────────────────────────────────
# The 3D view models a tip box as "these cells are missing", so both
# directions have to be maintained carefully. Two defects showed up here:
# pickups overwrote rather than accumulated, so tips taken in an earlier
# iteration popped back into view; and a box that started empty had no
# baseline to subtract from, so tips returned into it never appeared.

def _selection(row: int, col: int):
    from pybravo.head_mode import tipbox_selection
    return tipbox_selection(0, row, col, MODE)


async def _executor_with_boxes():
    bravo, _ = _make_workflow_executor_bravo()
    riser = {"labware_id": "tipbox-riser", "height_mm": 20.0}
    deck = {
        "1": [riser, {"labware_id": "tipbox-384", "tipbox_fill_state": "full"}],
        "4": [riser, {"labware_id": "tipbox-384", "tipbox_fill_state": "empty"}],
    }
    executor = WorkflowExecutor(bravo, {"nodes": []}, deck_config=deck)
    await executor._setup_deck()
    return executor


@pytest.mark.asyncio
async def test_picked_tips_stay_gone_across_loop_iterations():
    executor = await _executor_with_boxes()
    for col in (22, 20, 18):
        executor._set_removed_tip_cells(1, _selection(0, col))

    removed = executor._tipbox_removed_cells["1"]
    for col in (22, 20, 18):
        assert f"0:{col}" in removed, (
            f"column {col} reappeared — each pickup used to overwrite the "
            "previous one instead of accumulating"
        )
    assert len(removed) == 6 * ROWS


@pytest.mark.asyncio
async def test_an_empty_destination_box_starts_fully_empty_on_screen():
    executor = await _executor_with_boxes()
    baseline = executor._ensure_removed_baseline("4")
    assert len(baseline) == ROWS * COLS, "an empty box should render with no tips"


@pytest.mark.asyncio
async def test_returned_tips_appear_in_the_destination_box():
    executor = await _executor_with_boxes()
    executor._restore_tip_cells(4, _selection(0, 22))

    removed = executor._tipbox_removed_cells["4"]
    present = {f"{r}:{c}" for r in range(ROWS) for c in range(COLS)} - removed
    assert present == {f"{r}:{c}" for r in range(ROWS) for c in (22, 23)}, (
        "tips ejected into an empty box did not show up: the box had no "
        "removed-cell baseline, so there was nothing to subtract from"
    )


@pytest.mark.asyncio
async def test_nothing_hidden_is_reported_rather_than_omitted():
    """An empty list is a message, not an absence.

    The viewer falls back to the configured fill state when it has no per-cell
    information, so a box that started empty and has been filled back up must
    say "nothing hidden" explicitly. Omitting the entry made it fall back and
    draw no tips at all.
    """
    executor = await _executor_with_boxes()
    assert executor._ensure_removed_baseline("1") == set()
    payload = executor._serialize_tipbox_removed_cells(executor._tipbox_removed_cells)
    assert payload["1"] == []


@pytest.mark.asyncio
async def test_a_chosen_return_anchor_sets_the_direction_of_travel():
    """Pick where the first ejection lands; the head walks on from there.

    Starting at column 0 must fill 0-1, 2-3, 4-5 — the mirror of what happens
    when the operator leaves it at the default corner.
    """
    bravo, _ = _make_workflow_executor_bravo()
    riser = {"labware_id": "tipbox-riser", "height_mm": 20.0}
    deck = {
        "1": [riser, {"labware_id": "tipbox-384", "tipbox_fill_state": "full"}],
        "4": [riser, {"labware_id": "tipbox-384", "tipbox_fill_state": "empty"}],
    }
    executor = WorkflowExecutor(bravo, {"nodes": []}, deck_config=deck)
    await executor._setup_deck()
    bravo.set_head_mode("column", "back_left", column_count=2)

    for _ in range(3):
        bravo._tip_selection = None
        await bravo.tips_on(1)
        # Exactly what the executor does for a Tips Off node carrying a chosen
        # anchor. The suppressed error is load-bearing, not sloppiness: once
        # the chosen block is full, set_tip_selection rejects it and the
        # selection falls through to the next legal anchor, which is how "start
        # here, then keep going" works.
        try:
            bravo.set_tip_selection(4, 0, 0)
        except RuntimeError:
            pass
        await bravo.tips_off(4)

    assert sorted({c for _, c in bravo._occupied_tip_wells(4)}) == [0, 1, 2, 3, 4, 5], (
        "returns should have started at the chosen column 0 and walked right"
    )


@pytest.mark.asyncio
async def test_deck_details_reports_tipbox_fill_state():
    """The 3D view falls back to fill state when it has no per-cell data.

    Without it every tip box drew as full, so a box configured empty still
    showed 384 tips until a workflow started reporting occupancy. It is derived
    from live inventory rather than the original configuration, so it stays
    true as tips are picked and returned.
    """
    executor = await _executor_with_boxes()
    bravo = executor.bravo

    def fill(loc: int):
        details = bravo.get_state()["deck_details"][str(loc)]
        return details[-1].get("tipbox_fill_state")

    assert fill(1) == "full"
    assert fill(4) == "empty"

    bravo.set_head_mode("column", "back_left", column_count=2)
    bravo._tip_selection = None
    await bravo.tips_on(1)
    bravo._tip_selection = None
    await bravo.tips_off(4)

    assert fill(1) == "partial", "a part-used source box is neither full nor empty"
    assert fill(4) == "partial", "a part-filled destination box likewise"


@pytest.mark.asyncio
async def test_fill_state_is_only_attached_to_the_top_of_a_stack():
    """A riser under a tip box is not itself a tip box."""
    executor = await _executor_with_boxes()
    entries = executor.bravo.get_state()["deck_details"]["1"]
    assert len(entries) == 2
    assert "tipbox_fill_state" not in entries[0]
    assert entries[-1]["tipbox_fill_state"] == "full"
