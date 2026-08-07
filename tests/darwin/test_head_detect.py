"""Tests for head-type auto-detection via smart-head EEPROM."""

from __future__ import annotations

import pytest

from pybravo.darwin.controller import DarwinController
from pybravo.protocol.gemini.engine import GeminiEngine
from pybravo.protocol.gemini.enums import (
    CommandNAKTypes,
    CommandTypes,
    DarwinMasterNodeSubCommands,
)
from pybravo.protocol.gemini.packet import InstructionAddress
from pybravo.types import HeadType
from tests.fakes.gemini_fake import FakeGeminiServer


@pytest.fixture
def fake():
    s = FakeGeminiServer()
    s.start()
    try:
        yield s
    finally:
        s.stop()


@pytest.fixture
def controller(fake):
    engine = GeminiEngine("127.0.0.1", port=fake.port)
    ctrl = DarwinController(engine=engine)
    ctrl.open_tcp("127.0.0.1")
    try:
        yield ctrl, fake
    finally:
        ctrl.close()


# --- detect_smart_head --------------------------------------------------------


def test_detect_smart_head_true_when_init_succeeds(controller):
    ctrl, _ = controller
    # Fake's default set_handler returns SETCMD_RESP (success) → smart head present
    assert ctrl.detect_smart_head() is True


def test_detect_smart_head_false_when_nak_unsuccessful(controller):
    ctrl, fake = controller
    master = InstructionAddress(1, 0)
    fake.seed_nak(
        master,
        DarwinMasterNodeSubCommands.SMART_INIT,
        CommandNAKTypes.UNSUCCESSFUL_OPERATION,
    )
    assert ctrl.detect_smart_head() is False


def test_detect_smart_head_reraises_other_naks(controller):
    ctrl, fake = controller
    master = InstructionAddress(1, 0)
    fake.seed_nak(
        master,
        DarwinMasterNodeSubCommands.SMART_INIT,
        CommandNAKTypes.INVALID_SUBCMD,
    )
    from pybravo.protocol.gemini.errors import NAKError
    with pytest.raises(NAKError):
        ctrl.detect_smart_head()


# --- read_smart_head_type -----------------------------------------------------


def test_read_smart_head_type_sends_eeprom_read_and_returns_byte(controller):
    ctrl, fake = controller
    master = InstructionAddress(1, 0)

    # Seed the GET response for SMART_RD_EEPROM_VAL
    fake.storage[(1, 0, DarwinMasterNodeSubCommands.SMART_RD_EEPROM_VAL)] = 7

    head_byte = ctrl.read_smart_head_type()
    assert head_byte == 7

    # Verify SMART_RD_EEPROM was issued with (offset=1 << 8) | length=1 = 0x0101
    rd_eeprom = [
        p for p in fake.received_packets
        if p.dest == master
        and p.sub_command == DarwinMasterNodeSubCommands.SMART_RD_EEPROM
        and p.cmd_type == CommandTypes.SETCMD
    ]
    assert rd_eeprom and rd_eeprom[-1].cmd_val == 0x0101


# --- detect_head_type (deprecated — always HT_UNKNOWN until mapping known) ---


def test_detect_head_type_is_always_unknown_until_mapping_verified(controller):
    """detect_head_type() always returns HT_UNKNOWN for safety until we have
    ground-truth byte→HeadType mappings from real hardware.

    Observed on bench: a 384ST 70µL Series III head returned eeprom_byte=1,
    which is NOT HT_8_F_50 despite the enum value coincidentally matching.
    """
    ctrl, fake = controller
    fake.storage[(1, 0, DarwinMasterNodeSubCommands.SMART_RD_EEPROM_VAL)] = 3
    # Even with a seemingly-sensible byte value, the deprecated method
    # returns HT_UNKNOWN to force callers to use read_head_identification()
    assert ctrl.detect_head_type() == HeadType.HT_UNKNOWN


# --- read_head_identification (raw data) --------------------------------------


def test_read_head_identification_smart_head_present(controller):
    ctrl, fake = controller
    fake.storage[(1, 0, DarwinMasterNodeSubCommands.SMART_RD_EEPROM_VAL)] = 1
    fake.storage[(1, 0, DarwinMasterNodeSubCommands.STUPID_HEAD_COUNTS)] = 1803

    ident = ctrl.read_head_identification()
    assert ident["has_smart_head"] is True
    assert ident["eeprom_byte"] == 1
    assert ident["adc_counts"] == 1803


def test_read_head_identification_no_smart_head(controller):
    ctrl, fake = controller
    master = InstructionAddress(1, 0)
    fake.seed_nak(
        master,
        DarwinMasterNodeSubCommands.SMART_INIT,
        CommandNAKTypes.UNSUCCESSFUL_OPERATION,
    )
    fake.storage[(1, 0, DarwinMasterNodeSubCommands.STUPID_HEAD_COUNTS)] = 4095

    ident = ctrl.read_head_identification()
    assert ident["has_smart_head"] is False
    assert ident["eeprom_byte"] is None  # skipped — no smart head to read from
    assert ident["adc_counts"] == 4095
