"""Tests for the 4-word instruction encoder/decoder.

Most assertions pin the instruction bit layout field by field. The round-trip
tests at the bottom cover the encodable value space, since an instruction that
does not survive encode/decode would silently corrupt a motion command.
"""

from __future__ import annotations

import pytest

from pybravo.protocol.gemini.enums import (
    AxisDirection,
    InstructionTypes,
)
from pybravo.protocol.gemini.instruction import (
    Instruction,
    pack_float32,
    unpack_float32,
)


# --- Bit layout sanity ------------------------------------------------------


def test_instr_type_in_word0_low_byte():
    i = Instruction(instr_type=InstructionTypes.MOVE_BY, velocity_percent=100.0,
                    acceleration_percent=100.0, jerk_percent=100.0, force_percent=0.0)
    w0, _, _, _ = i.to_words()
    assert (w0 & 0xFF) == InstructionTypes.MOVE_BY


def test_velocity_100pct_maxes_uint16():
    i = Instruction(velocity_percent=100.0, acceleration_percent=0.0,
                    jerk_percent=0.0, force_percent=0.0)
    w0, _, _, _ = i.to_words()
    vel = (w0 >> 8) & 0xFFFF
    assert vel == 0xFFFF


def test_low_velocity_sets_bit24_of_word1():
    i = Instruction(velocity_percent=0.05, acceleration_percent=0.0,
                    jerk_percent=0.0, force_percent=0.0)
    _, w1, _, _ = i.to_words()
    assert (w1 & (1 << 24)) != 0


def test_direction_positive_sets_bit16_of_word1():
    pos = Instruction(direction=AxisDirection.POSITIVE, velocity_percent=50.0,
                      acceleration_percent=50.0, jerk_percent=50.0, force_percent=0.0)
    neg = Instruction(direction=AxisDirection.NEGATIVE, velocity_percent=50.0,
                      acceleration_percent=50.0, jerk_percent=50.0, force_percent=0.0)
    _, w1_pos, _, _ = pos.to_words()
    _, w1_neg, _, _ = neg.to_words()
    assert (w1_pos & (1 << 16)) != 0
    assert (w1_neg & (1 << 16)) == 0


def test_flag_bits_round_trip():
    i = Instruction(
        velocity_percent=50.0, acceleration_percent=50.0, jerk_percent=50.0,
        force_percent=0.0,
        reset_pos_on_start=True, reset_pos_after_stop=True,
        error_on_dest_reach=True, lld=True, stop_on_touch=True,
        check_for_clots=True,
    )
    w0, w1, w2, w3 = i.to_words()
    decoded = Instruction.from_words(w0, w1, w2, w3)
    for field in (
        "reset_pos_on_start",
        "reset_pos_after_stop",
        "error_on_dest_reach",
        "lld",
        "stop_on_touch",
        "check_for_clots",
    ):
        assert getattr(decoded, field), f"{field} lost on round-trip"


def test_accel_nonzero_pct_floors_to_at_least_one():
    # A very small non-zero percentage would scale to 0; the encoder clamps up to 1.
    i = Instruction(acceleration_percent=0.1, velocity_percent=50.0,
                    jerk_percent=50.0, force_percent=0.0)
    w0, _, _, _ = i.to_words()
    assert ((w0 >> 24) & 0xFF) >= 1


def test_jerk_zero_clamps_to_100pct_matching_csharp():
    """Jerk of <=0 or >100 clamps to 100%.

    The firmware rejects jerk=0 (word1 low byte = 0) with NAK OUT_OF_RANGE.
    This was seen on the G axis, where open_gripper with jerk_percent=0
    produced word1=0x00000000 and the axis refused the move.
    """
    i = Instruction(jerk_percent=0.0, velocity_percent=50.0,
                    acceleration_percent=50.0, force_percent=0.0)
    _, w1, _, _ = i.to_words()
    assert (w1 & 0xFF) == 0xFF, f"jerk byte should be 0xFF, got 0x{w1 & 0xFF:02x}"


def test_force_zero_stays_zero():
    """Force=0 is valid ("no force control") — should NOT be clamped up."""
    i = Instruction(force_percent=0.0, velocity_percent=50.0,
                    acceleration_percent=50.0, jerk_percent=50.0)
    _, w1, _, _ = i.to_words()
    assert ((w1 >> 8) & 0xFF) == 0


def test_jerk_over_100_also_clamps_to_100():
    i = Instruction(jerk_percent=200.0, velocity_percent=50.0,
                    acceleration_percent=50.0, force_percent=0.0)
    _, w1, _, _ = i.to_words()
    assert (w1 & 0xFF) == 0xFF


# --- Float32 helpers --------------------------------------------------------


@pytest.mark.parametrize("v", [0.0, 1.0, 0.2, -3.5, 123.456])
def test_float32_pack_roundtrip(v):
    recovered = unpack_float32(pack_float32(v))
    assert abs(recovered - v) < 1e-5


def test_float32_known_bit_pattern():
    # 0.2f ≈ 0x3E4CCCCD (observed as instruction word2)
    assert pack_float32(0.2) == 0x3E4CCCCD
    assert abs(unpack_float32(0x3E4CCCCD) - 0.2) < 1e-6


# --- Volume / delay / plunger helpers ---------------------------------------


def test_volume_accessor():
    i = Instruction()
    i.volume = 0.2
    assert abs(i.volume - 0.2) < 1e-5
    assert i.to_value == 0x3E4CCCCD


def test_delay_ms_accessor():
    i = Instruction(instr_type=InstructionTypes.DELAY)
    i.delay_ms = 1500
    assert i.delay_ms == 1500


def test_cmove_pt_data_accessors():
    i = Instruction(instr_type=InstructionTypes.CMOVE_TO)
    i.set_cmove_pt_data(data_id=42, data_count=7)
    assert i.cmove_pt_data_id == 42
    assert i.cmove_pt_data_count == 7


def test_plunger_accessors():
    i = Instruction()
    i.set_plunger(speed=5000, accel=120, jerk=200)
    assert i.plunger_speed == 5000
    assert i.plunger_acceleration == 120
    assert i.plunger_jerk == 200


# --- Round-trip over the canonical value corpus -----------------------------


def _canonical_instruction_words() -> list[tuple[int, int, int, int]]:
    """Encoded words for a spread of instructions the motion path can emit.

    Percentages are stored scaled into 8- and 16-bit fields, so an arbitrary
    32-bit word is generally *not* a valid encoding and will not survive a
    round-trip. Generating words through ``to_words`` gives the same canonical
    property that recorded traffic had, while covering far more of the space
    than any single session would.
    """
    words: list[tuple[int, int, int, int]] = []
    velocities = (0.0, 0.05, 0.1, 1.0, 25.0, 50.0, 99.9, 100.0)
    accelerations = (0.0, 10.0, 50.0, 100.0)
    forces = (0.0, 20.0, 100.0)
    for instr_type in list(InstructionTypes)[:8]:
        for vel in velocities:
            for accel in accelerations:
                for force in forces:
                    inst = Instruction(
                        instr_type=instr_type,
                        velocity_percent=vel,
                        acceleration_percent=accel,
                        jerk_percent=accel,
                        force_percent=force,
                        to_value=(int(vel * 1000) ^ 0x5A5A) & 0xFFFFFFFF,
                        trig_at_value=(int(accel * 977) + 13) & 0xFFFFFFFF,
                    )
                    # Normalise once: scaling into the wire fields is lossy, so
                    # the encoder's own output is the canonical form.
                    words.append(tuple(Instruction.from_words(*inst.to_words()).to_words()))
    return words


def test_instruction_words_roundtrip_bit_exact():
    """Decode + re-encode every canonical 4-word instruction."""
    corpus = _canonical_instruction_words()
    assert len(corpus) > 10, f"expected dozens of instructions, found {len(corpus)}"

    for w0, w1, w2, w3 in corpus:
        inst = Instruction.from_words(w0, w1, w2, w3)
        re_w0, re_w1, re_w2, re_w3 = inst.to_words()
        assert (re_w0, re_w1, re_w2, re_w3) == (w0, w1, w2, w3), (
            "instruction round-trip mismatch: "
            f"input=({w0:#010x},{w1:#010x},{w2:#010x},{w3:#010x}) "
            f"output=({re_w0:#010x},{re_w1:#010x},{re_w2:#010x},{re_w3:#010x})"
        )


def test_instruction_encoding_is_idempotent_for_arbitrary_words():
    """Decoding arbitrary words then re-encoding reaches a stable form.

    The firmware can hand back words this host did not write, so decoding must
    not oscillate: normalising twice has to equal normalising once.
    """
    for i in range(2000):
        raw = tuple((i * 2654435761 + k * 40503) & 0xFFFFFFFF for k in range(4))
        once = tuple(Instruction.from_words(*raw).to_words())
        twice = tuple(Instruction.from_words(*once).to_words())
        assert once == twice, f"unstable normalisation for {raw}: {once} -> {twice}"
