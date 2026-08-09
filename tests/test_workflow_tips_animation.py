"""The simulate animation must move tips at the moment of the press.

What the operator saw before this was pinned: on Tips On, the tips vanished
from the box before the head had started to descend, and then appeared on the
head only after it had retracted — the exchange happened everywhere except
where it physically does.

Two mechanisms drive the fix, and both are asserted here:

- ``_tip_change_event`` serializes ``_tipbox_removed_cells`` at build time, so
  the inventory mutation has to sit *between* building the pre-press event
  (box still full during the approach) and the main event (box emptied).
- The main event is emitted between the press step and the retract step, not
  after the retract.

These are display events only. The real task path (``bravo.tips_on`` /
``tips_off``) is untouched and runs after the animation in simulate mode.
"""

from __future__ import annotations

import pytest

from pybravo.workflow.executor import WorkflowExecutor
from tests.test_bravo_init import _make_workflow_executor_bravo

RISER = {"labware_id": "tipbox-riser", "height_mm": 20.0}


async def _executor_with_events(monkeypatch):
    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr("pybravo.workflow.executor.asyncio.sleep", _no_sleep)

    bravo, _ = _make_workflow_executor_bravo()
    deck = {
        "1": [RISER, {"labware_id": "tipbox-384", "tipbox_fill_state": "full"}],
        "4": [RISER, {"labware_id": "tipbox-384", "tipbox_fill_state": "empty"}],
    }
    events: list[dict] = []

    async def on_event(event):
        events.append(event)

    executor = WorkflowExecutor(bravo, {"nodes": []}, deck_config=deck, on_event=on_event)
    await executor._setup_deck()
    bravo.set_head_mode("column", "back_left", column_count=2)
    return bravo, executor, events


def _positions(events):
    return [e for e in events if e.get("type") == "workflow:positions"]


def _index_of(events, event):
    """Index by identity: the approach and retract positions payloads are
    equal dicts, so ``list.index`` would find the approach both times."""
    for i, e in enumerate(events):
        if e is event:
            return i
    raise AssertionError("event not found in stream")


def _tips_changes(events):
    return [e for e in events if e.get("type") == "workflow:tips_change"]


@pytest.mark.asyncio
async def test_tips_on_exchange_happens_at_the_bottom_of_the_press(monkeypatch):
    bravo, executor, events = await _executor_with_events(monkeypatch)

    await executor._animate_task_motion("tips/TipsOn", {"location": 1})

    changes = _tips_changes(events)
    assert len(changes) == 2, "expected exactly a pre-clear and a main tips_change"
    pre, main = changes

    # During the approach the head renders empty and the box still shows its
    # pre-pickup contents — nothing has been taken yet.
    assert pre["tips_on_head"] is False
    picked = set(main["tipbox_removed_cells"]["1"]) - set(
        pre.get("tipbox_removed_cells", {}).get("1", [])
    )
    assert len(picked) == 32, "one 16x2 block should change hands, at the main event only"
    assert not (set(pre.get("tipbox_removed_cells", {}).get("1", [])) & picked), (
        "the box emptied before the head descended — the pre-press event "
        "already carried the picked cells"
    )

    # The exchange happens between the press step and the retract step.
    assert main["tips_on_head"] is True
    assert main["tips_on_head_selection"] is not None
    pos = _positions(events)
    assert len(pos) == 4, "approach, descend, press, retract"
    z_values = [p["positions"]["Z"] for p in pos]
    press_idx = _index_of(events, pos[2])
    retract_idx = _index_of(events, pos[3])
    main_idx = _index_of(events, main)
    assert z_values[2] > z_values[1] > z_values[0] or z_values[2] > z_values[3], (
        f"unexpected Z profile {z_values}; the press should be the deepest point"
    )
    assert press_idx < main_idx < retract_idx, (
        "tips must attach at the bottom of the press: after the press step, "
        "before the retract — they used to attach after the head was back up"
    )


@pytest.mark.asyncio
async def test_tips_off_exchange_happens_at_the_eject_before_the_retract(monkeypatch):
    bravo, executor, events = await _executor_with_events(monkeypatch)

    # Get tips onto the head for real (simulation controller), then animate the
    # return. The animation is what the operator watches; the real task runs
    # after it in simulate mode.
    bravo._tip_selection = None
    await bravo.tips_on(1)
    events.clear()

    await executor._animate_task_motion("tips/TipsOff", {"location": 4})

    changes = _tips_changes(events)
    assert len(changes) == 1, "tips_off should emit exactly one tips_change"
    main = changes[0]
    assert main["tips_on_head"] is False

    # The destination box started empty (all cells removed); the return puts
    # one 16x2 block back.
    removed_after = set(main["tipbox_removed_cells"]["4"])
    assert len(removed_after) == 384 - 32, "one block should reappear in the box"

    pos = _positions(events)
    assert len(pos) == 4, "approach, descend, eject, retract"
    eject_idx = _index_of(events, pos[2])
    retract_idx = _index_of(events, pos[3])
    main_idx = _index_of(events, main)
    assert eject_idx < main_idx < retract_idx, (
        "tips must leave the head at the eject, before the retract"
    )


@pytest.mark.asyncio
async def test_animation_leaves_the_same_final_inventory_state(monkeypatch):
    """The reorder moves *when* the mutation happens, never *what* it does."""
    bravo, executor, events = await _executor_with_events(monkeypatch)

    await executor._animate_task_motion("tips/TipsOn", {"location": 1})

    removed = executor._tipbox_removed_cells.get("1", set())
    assert len(removed) == 32, "animation bookkeeping must end with the block removed"
