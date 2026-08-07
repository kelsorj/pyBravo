"""Tests for InstructionEvent decoding + RESERVED safety event detection.

The reference behaviour is a home after power cycle, where
frame 1505 contains a STOP_DISABLE event from the master node when the
light curtain was tripped: ``rx src=1.0 dest=63.0 cmd=1 sub=0 val=0x000008ff``.
"""

from __future__ import annotations

import pytest

from pybravo.protocol.gemini.enums import (
    EVENT_RESERVED,
    ReservedEvent,
    decode_instruction_event,
    is_reserved_event,
)

# --- Decoder ----------------------------------------------------------------


@pytest.mark.parametrize(
    "evt, expected",
    [
        # plain start_event=1 (NOT composite)
        (0x00000001, (False, 1, 0)),
        # SEND_EVT for single-axis move: composite, mask=1, event_no=2
        (0x00000182, (True, 2, 1)),
        # SEND_EVT seen for second axis: composite, mask=2, event_no=2
        (0x00000282, (True, 2, 2)),
        # STOP_DISABLE during recovery: composite, event_no=127, mask=8
        (0x000008FF, (True, 127, 8)),
    ],
)
def test_decode_instruction_event(evt, expected):
    assert decode_instruction_event(evt) == expected


# --- is_reserved_event -----------------------------------------------------


def test_stop_disable_decoded_from_observed_value():
    """The exact bytes observed during a recovery sequence."""
    assert is_reserved_event(0x000008FF) == ReservedEvent.STOP_DISABLE


@pytest.mark.parametrize(
    "name, mask",
    [
        ("STOP", 1),
        ("CONTINUE", 2),
        ("ERROR", 3),
        ("FAULT", 4),
        ("ETEACH_PRESSED", 5),
        ("ETEACH_RELEASED", 6),
        ("SAFETY_NOTICE", 7),
        ("STOP_DISABLE", 8),
    ],
)
def test_all_reserved_events_decode(name, mask):
    """Each defined reserved event maps to its expected enum member."""
    evt = (mask << 8) | 0x80 | EVENT_RESERVED  # composite encoding
    decoded = is_reserved_event(evt)
    assert decoded is not None
    assert decoded.name == name
    assert int(decoded) == mask


def test_normal_send_event_is_not_reserved():
    # 0x182 is the standard single-axis SEND_EVT — event_no=2, not 127
    assert is_reserved_event(0x182) is None


def test_plain_start_event_is_not_reserved():
    # 0x1 is a plain start event — composite bit not set
    assert is_reserved_event(0x1) is None


def test_unknown_reserved_subcode_returns_none():
    """A composite event_no=127 with an unknown subcode → None (not crash)."""
    evt = (99 << 8) | 0x80 | 127  # mask=99 isn't in our enum
    assert is_reserved_event(evt) is None
