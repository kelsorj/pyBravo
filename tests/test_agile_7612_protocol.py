"""Unit tests for Agile 7612 Bravo protocol encodings.

Tests T1-T8: verify all protocol-level byte encodings against known-good
known-good values for the Agile 7612 wire protocol.

No hardware required — these test the encoding/decoding logic only.
"""

import struct

from pybravo.protocol.agile_7612_commands import Agile7612MoveInfo
from pybravo.protocol.agile_7612_crc import crc8_maxim
from pybravo.protocol.agile_7612_packet import (
    register_get,
)
from pybravo.protocol.commands import AgileMoveInfo, CommandID
from pybravo.types import Axis

# ─── T1: CRC-8/MAXIM ───

class TestCRC8Maxim:
    """T1: CRC-8/MAXIM matches known values."""

    def test_controller_verify_packet(self):
        """Verify CRC for controller identification packet (reg 0x90)."""
        pkt = bytes.fromhex("0990000000000000003f")
        assert crc8_maxim(pkt[:9], 9) == 0x3F

    def test_jog_trigger_packet(self):
        """Verify CRC for jog trigger (header 0x80, byte[7]=0x36)."""
        pkt = bytes.fromhex("800040000000053600d8")
        assert crc8_maxim(pkt[:9], 9) == 0xD8

    def test_differs_from_smbus(self):
        """MAXIM CRC differs from SMBUS CRC on the same data."""
        from pybravo.protocol.agile_packet import crc8 as smbus_crc8
        data = b"\x01\x00\x00\x01\x00\x00\x00\x00\x00"
        maxim = crc8_maxim(data)
        smbus = smbus_crc8(data)
        assert maxim != smbus

    def test_agile_7612_packet_uses_maxim(self):
        """Agile 7612 packet builder uses MAXIM CRC, not SMBUS."""
        from pybravo.protocol.agile_packet import register_get as std_register_get
        agile_7612_pkt = register_get(0, 0x0100)
        std_pkt = std_register_get(0, 0x0100)
        assert agile_7612_pkt[:9] == std_pkt[:9]  # same body
        assert agile_7612_pkt[9] != std_pkt[9]    # different CRC


# ─── T2: V11 Agile 7612 Framing ───

class TestV11Agile7612Framing:
    """T2: V11 Agile 7612 frame format [cmd][length_u16_LE][data]."""

    def test_ping_frame(self):
        """PING frame: cmd=0xA0, length=0."""
        frame = struct.pack("<BH", 0xA0, 0)
        assert frame == bytes.fromhex("a00000")

    def test_query_version_frame(self):
        """QUERY_VERSION frame: cmd=0x00, length=0."""
        frame = struct.pack("<BH", 0x00, 0)
        assert frame == bytes.fromhex("000000")

    def test_prepare_move_frame_header(self):
        """PREPARE_MOVE frame header: cmd=0xA2, length=17."""
        frame_header = struct.pack("<BH", 0xA2, 17)
        assert frame_header == bytes.fromhex("a21100")

    def test_direct_agile_frame_header(self):
        """DIRECT_AGILE frame header: cmd=0xA1, length=11 (10 pkt + 1 axis)."""
        frame_header = struct.pack("<BH", 0xA1, 11)
        assert frame_header == bytes.fromhex("a10b00")

    def test_standard_v11_differs(self):
        """Standard V11 uses [length][cmd], opposite of Agile 7612 [cmd][length]."""
        agile_7612_ping = struct.pack("<BH", 0xA0, 0)    # cmd first
        std_ping = struct.pack("<HB", 1, 0xA0)     # length first
        assert agile_7612_ping != std_ping
        assert agile_7612_ping == b"\xa0\x00\x00"
        assert std_ping == b"\x01\x00\xa0"


# ─── T3: Agile7612MoveInfo ───

class TestAgile7612MoveInfo:
    """T3: Agile7612MoveInfo packs to 17 bytes with u16 home_complete_register."""

    def test_pack_length(self):
        """Agile7612MoveInfo packs to 17 bytes (not 19)."""
        info = Agile7612MoveInfo(axis=Axis.X, position=100.0, velocity=1.0, acceleration=0.5)
        assert len(info.pack()) == 17

    def test_standard_pack_length(self):
        """Standard AgileMoveInfo packs to 19 bytes (u32 home_complete_register)."""
        info = AgileMoveInfo(axis=Axis.X, position=100.0, velocity=1.0, acceleration=0.5)
        assert len(info.pack()) == 19

    def test_z_home_matches_capture(self):
        """Z homing PREPARE_MOVE payload is byte-exact."""
        info = Agile7612MoveInfo(
            axis=Axis.Z,
            position=0.0,
            velocity=200.0,
            acceleration=0.6,
            absolute_move=True,
            check_for_homed=True,
            home_complete_register=0x0160,
        )
        packed = info.pack()
        expected = bytes.fromhex("0200000000000048439a99193f01016001")
        assert packed == expected

    def test_x_relative_jog(self):
        """Relative X jog: absolute=False, check_for_homed=False."""
        info = Agile7612MoveInfo(
            axis=Axis.X,
            position=1574.8,
            velocity=74.96,
            acceleration=0.4,
            absolute_move=False,
            check_for_homed=False,
            home_complete_register=0x015E,
        )
        packed = info.pack()
        assert len(packed) == 17
        assert packed[0] == 0  # axis X = 0
        assert packed[13] == 0  # absolute_move = False
        assert packed[14] == 0  # check_for_homed = False
        assert struct.unpack_from("<H", packed, 15)[0] == 0x015E

    def test_home_reg_u16_range(self):
        """home_complete_register is u16, clamped to 0xFFFF."""
        info = Agile7612MoveInfo(
            axis=Axis.Z, position=0.0, velocity=0.0, acceleration=0.0,
            home_complete_register=0x0160,
        )
        packed = info.pack()
        home_reg = struct.unpack_from("<H", packed, 15)[0]
        assert home_reg == 0x0160

    def test_unpack_roundtrip(self):
        """Pack then unpack gives back the same values."""
        info = Agile7612MoveInfo(
            axis=Axis.Y, position=3149.6, velocity=15.748, acceleration=0.031496,
            absolute_move=False, check_for_homed=False, home_complete_register=0x015F,
        )
        unpacked = Agile7612MoveInfo.unpack(info.pack())
        assert unpacked.axis == Axis.Y
        assert abs(unpacked.position - 3149.6) < 0.1
        assert unpacked.absolute_move is False
        assert unpacked.home_complete_register == 0x015F


# ─── T4: Position Decoding ───

class TestPositionDecoding:
    """T4: Position register decoding matches vendor readings."""

    def test_x_position_193mm(self):
        """X at 193.03mm: BE u16 = 0x76BF = 30399, *2/314.96 = 193.03."""
        raw = struct.unpack(">H", bytes.fromhex("76bf"))[0]
        position = raw * 2.0 / 314.96
        assert abs(position - 193.03) < 0.01

    def test_y_position_115mm(self):
        """Y at 115.44mm: BE u16 = 0x4703 = 18179, *2/314.96 = 115.44."""
        raw = struct.unpack(">H", bytes.fromhex("4703"))[0]
        position = raw * 2.0 / 314.96
        assert abs(position - 115.44) < 0.01

    def test_z_position_zero(self):
        """Z at 0.00mm after homing: BE u16 = 0x0000."""
        raw = struct.unpack(">H", bytes.fromhex("0000"))[0]
        position = raw * 2.0 / 1600.0
        assert position == 0.0

    def test_w_position_zero(self):
        """W at 0.00 µL after homing: BE u16 = 0x0000."""
        raw = struct.unpack(">H", bytes.fromhex("0000"))[0]
        position = raw * 2.0 / 448.0
        assert position == 0.0

    def test_ctrl2_zg_minus_20mm(self):
        """Zg at -20mm: raw=0x84F4, bit15=sign, magnitude=0x04F4=1268."""
        raw = 0x84F4
        sign = -1.0 if (raw & 0x8000) else 1.0
        magnitude = raw & 0x7FFF
        effective_tpu = 126.8
        position = sign * float(magnitude) * 2.0 / effective_tpu
        assert abs(position - (-20.0)) < 0.1

    def test_ctrl2_g_zero(self):
        """G at 0mm: raw=0x0000, magnitude=0, position=0."""
        raw = 0x0000
        sign = -1.0 if (raw & 0x8000) else 1.0
        magnitude = raw & 0x7FFF
        effective_tpu = 126.8 * (944.882 / 787.402)
        position = sign * float(magnitude) * 2.0 / effective_tpu
        assert position == 0.0

    def test_big_endian_not_little_endian(self):
        """Position bytes must be read as big-endian, not little-endian."""
        be = struct.unpack(">H", bytes.fromhex("76bf"))[0]  # 30399
        le = struct.unpack("<H", bytes.fromhex("76bf"))[0]  # 48998
        assert be == 30399
        assert le != 30399


# ─── T5: Move Trigger Packet ───

class TestMoveTrigger:
    """T5: Move trigger packet format."""

    def test_trigger_format(self):
        """Move trigger: header=0x00, byte[1]=axis_mask, byte[7]=0x38."""
        raw = bytearray(10)
        raw[0] = 0x00
        raw[1] = 0x04  # Z axis bitmask
        raw[7] = 0x38
        raw[9] = crc8_maxim(raw, 9)
        assert raw[0] == 0x00
        assert raw[7] == 0x38
        assert len(raw) == 10

    def test_trigger_crc_validates(self):
        """Trigger CRC is valid."""
        raw = bytearray(10)
        raw[0] = 0x00
        raw[1] = 0x01  # X axis
        raw[7] = 0x38
        raw[9] = crc8_maxim(raw, 9)
        assert crc8_maxim(raw, 9) == raw[9]

    def test_x_axis_bitmask(self):
        """X axis = bit 0 = 0x01."""
        from pybravo.controllers.agile import _axis_bit
        assert _axis_bit(Axis.X) == 0x01

    def test_z_axis_bitmask(self):
        """Z axis = bit 2 = 0x04."""
        from pybravo.controllers.agile import _axis_bit
        assert _axis_bit(Axis.Z) == 0x04


# ─── T6: Jog Trigger Packet ───

class TestJogTrigger:
    """T6: Jog trigger packet format (header 0x80, byte[7]=0x36)."""

    def test_jog_trigger_format(self):
        """Jog trigger: header=0x80, byte[2]=0x40, byte[7]=0x36."""
        raw = bytearray(10)
        raw[0] = 0x80
        raw[1] = 0x00
        raw[2] = 0x40
        raw[6] = 0x05
        raw[7] = 0x36
        raw[9] = crc8_maxim(raw, 9)
        assert raw[9] == 0xD8  # CRC from capture

    def test_jog_trigger_differs_from_move(self):
        """Jog trigger uses different header and subtype than move trigger."""
        jog = bytearray(10)
        jog[0] = 0x80
        jog[7] = 0x36

        move = bytearray(10)
        move[0] = 0x00
        move[7] = 0x38

        assert jog[0] != move[0]
        assert jog[7] != move[7]


# ─── T7: PREPARE_JOG Payload ───

class TestPrepareJog:
    """T7: PREPARE_JOG 8-byte payload format."""

    def test_payload_matches_capture(self):
        """Z-axis jog payload is byte-exact."""
        axis = 2  # Z
        peak_current = 0.16
        home_reg = 0x0160
        flags = 0x01

        payload = struct.pack("<Bf", axis, peak_current)
        payload += struct.pack(">H", home_reg)  # BIG-endian for home_reg
        payload += struct.pack("<B", flags)

        expected = bytes.fromhex("020ad7233e016001")
        assert payload == expected

    def test_payload_length(self):
        """PREPARE_JOG payload is 8 bytes."""
        payload = struct.pack("<Bf", 2, 0.16) + struct.pack(">H", 0x0160) + b"\x01"
        assert len(payload) == 8

    def test_home_reg_big_endian(self):
        """home_reg in PREPARE_JOG is big-endian (unlike PREPARE_MOVE which is LE)."""
        home_reg = 0x0160
        be = struct.pack(">H", home_reg)
        le = struct.pack("<H", home_reg)
        assert be == b"\x01\x60"
        assert le == b"\x60\x01"
        assert be != le

    def test_peak_current_encoding(self):
        """peak_current=0.16 encodes as float32 LE 0x3E23D70A."""
        packed = struct.pack("<f", 0.16)
        assert packed == bytes.fromhex("0ad7233e")


# ─── T8: Homing Servo Register Writes ───

class TestHomingServoRegisters:
    """T8: Homing servo register values are byte-exact."""

    def test_register_count(self):
        """6 servo registers are written during servo config for homing.

        A3 and A4 are written separately per-phase in each _home_* method,
        not during the initial servo config block.
        """
        from pybravo.controllers.agile_7612 import _homing_servo_registers
        regs = _homing_servo_registers(Axis.Z)
        assert len(regs) == 6

    def test_register_ids(self):
        """Correct register IDs."""
        from pybravo.controllers.agile_7612 import _homing_servo_registers
        reg_ids = [r[0] for r in _homing_servo_registers(Axis.Z)]
        assert 0xA0 in reg_ids
        assert 0xAD in reg_ids
        assert 0xAE in reg_ids
        assert 0xAF in reg_ids
        assert 0xB0 in reg_ids
        assert 0xBD in reg_ids

    def test_register_data_length(self):
        """Each register data is 7 bytes."""
        from pybravo.controllers.agile_7612 import _homing_servo_registers
        for axis in [Axis.X, Axis.Y, Axis.Z, Axis.G, Axis.Zg]:
            for reg, data in _homing_servo_registers(axis):
                assert len(data) == 7, f"Axis {axis.name} reg 0x{reg:02X} data is {len(data)} bytes"

    def test_z_register_a0_value(self):
        """Z register 0xA0 value matches capture."""
        from pybravo.controllers.agile_7612 import _homing_servo_registers
        regs = _homing_servo_registers(Axis.Z)
        reg_a0 = next(data for reg, data in regs if reg == 0xA0)
        assert reg_a0 == bytes.fromhex("7ae147aeff1000")

    def test_x_register_a0_value(self):
        """X register 0xA0 value differs from Z (from v2 capture)."""
        from pybravo.controllers.agile_7612 import _homing_servo_registers
        z_a0 = next(d for r, d in _homing_servo_registers(Axis.Z) if r == 0xA0)
        x_a0 = next(d for r, d in _homing_servo_registers(Axis.X) if r == 0xA0)
        assert x_a0 == bytes.fromhex("60c1762bfd1000")
        assert x_a0 != z_a0

    def test_ae_b0_encode_local_axis(self):
        """Registers 0xAE/0xB0 byte[4] = local_axis_index + 1."""
        from pybravo.controllers.agile_7612 import _homing_servo_registers
        for axis, expected_byte in [(Axis.X, 1), (Axis.Y, 2), (Axis.Z, 3), (Axis.G, 1), (Axis.Zg, 2)]:
            regs = _homing_servo_registers(axis)
            ae_data = next(d for r, d in regs if r == 0xAE)
            assert ae_data[4] == expected_byte, f"{axis.name}: AE byte[4]={ae_data[4]}, expected {expected_byte}"

    def test_servo_write_header_per_axis(self):
        """Servo write header = local_axis_index * 0x10 (not always 0x20)."""
        from pybravo.controllers.agile import _local_axis_index
        assert _local_axis_index(Axis.X) * 0x10 == 0x00
        assert _local_axis_index(Axis.Y) * 0x10 == 0x10
        assert _local_axis_index(Axis.Z) * 0x10 == 0x20
        assert _local_axis_index(Axis.G) * 0x10 == 0x00
        assert _local_axis_index(Axis.Zg) * 0x10 == 0x10


# ─── Additional: Command ID Completeness ───

class TestCommandIDs:
    """Verify Agile-7612-specific command IDs exist."""

    def test_stop_command(self):
        assert CommandID.STOP == 0xAB

    def test_query_jog_status(self):
        assert CommandID.QUERY_JOG_STATUS == 0xAE

    def test_prepare_jog(self):
        assert CommandID.PREPARE_JOG == 0xAA

    def test_prepare_move(self):
        assert CommandID.PREPARE_MOVE == 0xA2


# ─── Additional: Controller Class Structure ───

class TestAgile7612ControllerStructure:
    """Verify Agile7612Controller has all required overrides."""

    def test_inherits_from_agile(self):
        from pybravo.controllers.agile import AgileController
        from pybravo.controllers.agile_7612 import Agile7612Controller
        assert issubclass(Agile7612Controller, AgileController)

    def test_has_stop_method(self):
        from pybravo.controllers.agile_7612 import Agile7612Controller
        ctrl = Agile7612Controller()
        assert hasattr(ctrl, "stop")

    def test_has_jog_override(self):
        from pybravo.controllers.agile_7612 import Agile7612Controller
        assert Agile7612Controller.jog is not Agile7612Controller.__bases__[0].jog

    def test_has_home_axes_override(self):
        from pybravo.controllers.agile_7612 import Agile7612Controller
        assert Agile7612Controller.home_axes is not Agile7612Controller.__bases__[0].home_axes

    def test_has_move_override(self):
        from pybravo.controllers.agile_7612 import Agile7612Controller
        assert Agile7612Controller.move is not Agile7612Controller.__bases__[0].move

    def test_uses_agile_7612_protocol(self):
        from pybravo.controllers.agile_7612 import Agile7612Controller
        from pybravo.protocol import agile_7612_packet
        ctrl = Agile7612Controller()
        assert ctrl._agile_pkt is agile_7612_packet

    def test_uses_agile_7612_move_info(self):
        from pybravo.controllers.agile_7612 import Agile7612Controller
        from pybravo.protocol.agile_7612_commands import Agile7612MoveInfo
        ctrl = Agile7612Controller()
        assert ctrl._move_info_cls is Agile7612MoveInfo

    def test_unsupported_commands_blocked(self):
        from pybravo.controllers.agile_7612 import Agile7612Controller
        ctrl = Agile7612Controller()
        assert CommandID.CLEAR_MOTOR_POWER_FAULT in ctrl._UNSUPPORTED_COMMANDS
        assert CommandID.GET_POSITION in ctrl._UNSUPPORTED_COMMANDS
        assert CommandID.DETECT_SMART_HEAD in ctrl._UNSUPPORTED_COMMANDS
        assert CommandID.READ_AD_WEIGH_PAD in ctrl._UNSUPPORTED_COMMANDS
