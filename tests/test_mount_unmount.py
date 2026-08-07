"""Mount / Unmount semantic tests.

Exercises the pure state-machine behavior — how ``is_mounted`` flags
propagate through :class:`LabwareStack` and :class:`DeckState`, and
the invariants the bravo-level ``mount_plates`` / ``unmount_plate``
tasks depend on. No hardware, no controller simulation: we're
testing that the DeckState vocabulary correctly expresses the
"plates locked together move as a unit" concept.

Intentionally separate from the end-to-end PickPlaceTask tests; those
need the full controller / profile / teachpoints stack. Here we just
verify the mount contract at its foundational layer so any future
refactor that breaks it fails loudly in CI.
"""

from __future__ import annotations

import pytest

from pybravo.deck.labware import (
    DeckState,
    Labware,
    LabwareDefinition,
    LabwareStack,
)


def _mk(name: str, **kw) -> Labware:
    """Cheap Labware factory for test stacks. Defaults to a generic
    15 mm plate — height doesn't matter for mount logic, it's purely
    a book-keeping concern here."""
    return Labware(
        id=kw.pop("id", name),
        name=name,
        height=kw.pop("height", 15.0),
        width=kw.pop("width", 127.76),
        length=kw.pop("length", 85.48),
        **kw,
    )


# ── LabwareStack.mounted_group_from_top ─────────────────────────────


def test_empty_stack_returns_empty_group():
    stack = LabwareStack()
    assert stack.mounted_group_from_top() == []


def test_unmounted_single_plate_group_is_just_itself():
    stack = LabwareStack()
    plate = _mk("A")
    stack.add(plate)
    assert stack.mounted_group_from_top() == [plate]


def test_unmounted_pair_returns_only_top():
    """Stack of two independent plates — picking the top moves only
    the top, the bottom stays behind (ordinary Stack semantics).
    """
    stack = LabwareStack()
    bottom, top = _mk("bottom"), _mk("top")
    stack.add(bottom)
    stack.add(top)
    assert stack.mounted_group_from_top() == [top]


def test_mounted_pair_returns_both_plates_top_first():
    """The essence of the mount concept: top.is_mounted=True means the
    bottom comes along when we pick the top.
    """
    stack = LabwareStack()
    bottom, top = _mk("bottom"), _mk("top", is_mounted=True)
    stack.add(bottom)
    stack.add(top)
    # Top first, so callers that iterate the result in order see the
    # visible plate before the hidden one beneath it.
    assert stack.mounted_group_from_top() == [top, bottom]


def test_mounted_triple_walks_all_the_way_down():
    """Three-plate mounted stack — rare but the model shouldn't stop
    walking at depth 2. A mid-plate with is_mounted=True chains
    another plate into the group.
    """
    stack = LabwareStack()
    a = _mk("a")                       # bottom
    b = _mk("b", is_mounted=True)      # mounted on a
    c = _mk("c", is_mounted=True)      # mounted on b
    stack.add(a)
    stack.add(b)
    stack.add(c)
    assert stack.mounted_group_from_top() == [c, b, a]


def test_stop_at_first_unmounted_layer():
    """Mounted top on an unmounted middle on a plain bottom — group
    includes only the top two, because middle.is_mounted=False tells
    us the bottom is NOT locked to the middle."""
    stack = LabwareStack()
    a = _mk("a")                       # bottom, unmounted
    b = _mk("b")                       # middle, unmounted
    c = _mk("c", is_mounted=True)      # top, mounted to b
    stack.add(a)
    stack.add(b)
    stack.add(c)
    # We stop at b even though c is mounted, because b itself is not
    # mounted to a. Walker exits after adding b.
    assert stack.mounted_group_from_top() == [c, b]


# ── DeckState.remove_mounted_group / add_mounted_group round-trip ──


def test_remove_and_readd_preserves_ordering_on_unmounted():
    """Round-trip an unmounted stack to a fresh location and verify
    the plate order survives (it's a single-item move; trivial but
    pins the invariant)."""
    deck = DeckState()
    deck.add(1, _mk("only"))
    group = deck.remove_mounted_group(1)
    assert [lw.name for lw in group] == ["only"]
    deck.add_mounted_group(5, group)
    assert deck.get_stack(1).top is None
    assert deck.get_stack(5).top.name == "only"


def test_round_trip_mounted_pair_preserves_orientation():
    """Move a mounted pair from loc 1 → loc 5. After the move, the
    pair should appear at loc 5 in the same physical orientation:
    former bottom sits on the pad, former top sits on it and still
    has is_mounted=True.
    """
    deck = DeckState()
    bottom = _mk("collection")
    top    = _mk("filter", is_mounted=True)
    deck.add(1, bottom)
    deck.add(1, top)

    group = deck.remove_mounted_group(1)
    assert deck.get_stack(1).top is None, "source location should be empty"

    deck.add_mounted_group(5, group)
    dest = deck.get_stack(5)
    assert len(dest) == 2
    # Bottom of destination stack = collection plate, still unmounted.
    assert dest.items[0].name == "collection"
    assert dest.items[0].is_mounted is False
    # Top = filter plate, still flagged as mounted (travels with the
    # instance because is_mounted is instance state, not a per-location
    # annotation).
    assert dest.items[1].name == "filter"
    assert dest.items[1].is_mounted is True


def test_round_trip_mounted_group_to_stack_above_existing_plate():
    """Drop a mounted pair onto a deck position that already has a
    plate. The existing plate stays at the bottom; the mounted pair
    stacks on top of it, preserving the mount flag.
    """
    deck = DeckState()
    existing = _mk("existing_plate")
    deck.add(5, existing)
    # Build a mounted pair at loc 1.
    deck.add(1, _mk("collection"))
    deck.add(1, _mk("filter", is_mounted=True))

    group = deck.remove_mounted_group(1)
    deck.add_mounted_group(5, group)

    dest = deck.get_stack(5)
    assert [lw.name for lw in dest.items] == ["existing_plate", "collection", "filter"]
    assert dest.items[0].is_mounted is False
    assert dest.items[1].is_mounted is False
    assert dest.items[2].is_mounted is True


def test_remove_mounted_group_degrades_to_single_plate_when_top_unmounted():
    """DeckState.remove_mounted_group on a plain (unmounted) stack of
    two plates should only pop the top one — same semantics as the
    pre-mount ``remove()`` method. This guards the 'mount support
    doesn't break Stack' invariant.
    """
    deck = DeckState()
    deck.add(1, _mk("bottom"))
    deck.add(1, _mk("top"))

    group = deck.remove_mounted_group(1)
    assert [lw.name for lw in group] == ["top"]
    remaining = deck.get_stack(1)
    assert len(remaining) == 1
    assert remaining.top.name == "bottom"


# ── LabwareDefinition.can_mount / can_be_mounted round-trip ────────


def test_labware_definition_from_mongo_round_trips_mount_flags():
    """The Mongo parser reads plate_properties.can_mount and
    plate_properties.can_be_mounted. Guards against future refactors
    dropping these.
    """
    doc = {
        "labware_type_id": "lw-filter-001",
        "name": "96 filter plate",
        "kind": "filter_plate",
        "base_class": "plate",
        "plate_dimensions_mm": {"length_mm": 127.76, "width_mm": 85.48, "height_mm": 14.5},
        "plate_properties": {
            "can_mount":      True,
            "can_be_mounted": False,
        },
    }
    d = LabwareDefinition.from_mongo(doc)
    assert d.can_mount is True
    assert d.can_be_mounted is False


def test_labware_definition_mount_flags_default_false():
    """Legacy catalog entries with no mount-related fields get
    can_mount=False / can_be_mounted=False — backward-compatible
    (every existing plate remains unmountable until opted in)."""
    doc = {
        "labware_type_id": "lw-legacy",
        "name": "plain plate",
        "kind": "sbs_plate",
        "plate_properties": {},
    }
    d = LabwareDefinition.from_mongo(doc)
    assert d.can_mount is False
    assert d.can_be_mounted is False


# ── Labware.is_mounted travels with the instance across moves ──────


def test_is_mounted_flag_preserved_across_remove_add():
    """Moving a mounted plate using the DeckState API must not strip
    its is_mounted flag — otherwise subsequent pick/place ops would
    treat it as unmounted and split the pair."""
    deck = DeckState()
    bottom = _mk("b")
    top    = _mk("t", is_mounted=True)
    deck.add(1, bottom)
    deck.add(1, top)

    group = deck.remove_mounted_group(1)
    deck.add_mounted_group(3, group)

    moved_top = deck.get_stack(3).items[-1]
    assert moved_top is top, "instance identity preserved"
    assert moved_top.is_mounted is True


# ── LabwareStack.get_support_height_below_group ───────────────────


def test_support_below_group_empty_stack_is_zero():
    stack = LabwareStack()
    assert stack.get_support_height_below_group() == 0.0


def test_support_below_group_equals_location_height_for_unmounted():
    """For an ordinary (unmounted) stack of two plates, the engage
    plate is just the top; support below it is the stacking height of
    the bottom plate — same answer get_location_height would give for
    ordinary Stack/Destack physics."""
    stack = LabwareStack()
    stack.add(_mk("bottom", height=14.4, stack_height=13.6))
    stack.add(_mk("top",    height=14.4, stack_height=13.6))
    # Support under the engage plate (top) = bottom's stack_height.
    assert stack.get_support_height_below_group() == pytest.approx(13.6)


def test_support_below_group_returns_zero_for_mounted_pair_on_bare_pad():
    """Mounted pair sitting directly on the pad. The engage plate is
    the bottom — which has nothing beneath it, so the support height
    is zero (gripper engages at bottom.gripper_offset above the pad).
    """
    stack = LabwareStack()
    stack.add(_mk("collection", height=14.4, stack_height=13.6))
    stack.add(_mk("filter",     height=14.4, stack_height=13.6, is_mounted=True))
    assert stack.get_support_height_below_group() == 0.0


def test_support_below_group_counts_plates_below_mounted_pair():
    """Mounted pair sitting on top of an unmounted base plate. Support
    below the group = stack_height of the base only (the engage plate
    of the group is above that, not the bottom of the whole stack).
    """
    stack = LabwareStack()
    stack.add(_mk("base_unmounted", height=14.4, stack_height=13.6))
    stack.add(_mk("collection",     height=14.4, stack_height=13.6))
    stack.add(_mk("filter",         height=14.4, stack_height=13.6, is_mounted=True))
    assert stack.get_support_height_below_group() == pytest.approx(13.6)
