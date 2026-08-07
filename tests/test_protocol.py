"""Tests for protocol layer -- commands, Agile packets, and errors."""

from pybravo.protocol.agile_packet import (
    crc8, verify_packet, register_get, move_go, servo_enable,
    AgileReply, AGILE_PACKET_SIZE,
)
from pybravo.protocol.commands import (
    AgileMoveInfo, LightCommandData, SmartHeadEEPROMData,
)
from pybravo.protocol.errors import (
    BravoError, ErrorType, RabbitErrorCode, rabbit_error_to_bravo_error,
)
from pybravo.types import Axis, LightColor


class TestCRC8:
    def test_empty_data(self):
        assert crc8(b"", 0) == 0

    def test_known_bytes(self):
        result = crc8(b"\x01\x00\x00\x00\x00\x00\x00\x00\x00", 9)
        assert isinstance(result, int)
        assert 0 <= result <= 255

    def test_deterministic(self):
        data = b"\xA1\x00\x01\x02\x03\x04\x05\x06\x07"
        assert crc8(data) == crc8(data)


class TestAgilePacket:
    def test_packet_size(self):
        pkt = register_get(0, 0x0100)
        assert len(pkt) == AGILE_PACKET_SIZE

    def test_packet_crc_valid(self):
        pkt = register_get(0, 0x0100)
        assert verify_packet(pkt)

    def test_move_go_packet(self):
        pkt = move_go(0, 0x0F)
        assert len(pkt) == AGILE_PACKET_SIZE
        assert verify_packet(pkt)

    def test_servo_enable_packet(self):
        pkt = servo_enable(0, 0)
        assert len(pkt) == AGILE_PACKET_SIZE
        assert verify_packet(pkt)

    def test_invalid_crc(self):
        pkt = bytearray(register_get(0, 0x0100))
        pkt[9] ^= 0xFF  # corrupt CRC
        assert not verify_packet(bytes(pkt))

    def test_reply_parsing(self):
        pkt = register_get(0, 0x0100)
        reply = AgileReply.from_packet(pkt)
        assert reply.crc_valid
        assert reply.controller_id == 0


class TestAgileMoveInfo:
    def test_pack_unpack(self):
        info = AgileMoveInfo(
            axis=Axis.Z,
            position=1000.0,
            velocity=50.0,
            acceleration=100.0,
            absolute_move=True,
            check_for_homed=True,
        )
        data = info.pack()
        restored = AgileMoveInfo.unpack(data)
        assert restored.axis == Axis.Z
        assert abs(restored.position - 1000.0) < 0.01
        assert restored.absolute_move is True


class TestLightCommand:
    def test_pack_unpack(self):
        cmd = LightCommandData(
            light=LightColor.RED | LightColor.GREEN,
            period_ms=500,
            duty_cycle=0.5,
        )
        data = cmd.pack()
        restored = LightCommandData.unpack(data)
        assert LightColor.RED in restored.light
        assert LightColor.GREEN in restored.light
        assert restored.period_ms == 500
        assert abs(restored.duty_cycle - 0.5) < 0.01


class TestEEPROMData:
    def test_pack_unpack(self):
        eeprom = SmartHeadEEPROMData(address=0x01, length=1, data=b"\x03")
        data = eeprom.pack()
        restored = SmartHeadEEPROMData.unpack(data)
        assert restored.address == 0x01
        assert restored.length == 1
        assert restored.data == b"\x03"


class TestErrors:
    def test_bravo_error_str(self):
        err = BravoError(ErrorType.ROBOT_DISABLE)
        assert "safety interlock" in str(err).lower() or "E-stop" in str(err)

    def test_bravo_error_with_axis(self):
        err = BravoError(ErrorType.MOVE_TIMEOUT, axis=Axis.Z)
        assert "Z-axis" in str(err)

    def test_bravo_error_custom(self):
        err = BravoError(ErrorType.NO_ERROR, custom_text="All good")
        assert str(err) == "All good"

    def test_rabbit_error_mapping(self):
        err = rabbit_error_to_bravo_error(RabbitErrorCode.ROBOT_DISABLE)
        assert err.error_type == ErrorType.ROBOT_DISABLE

    def test_rabbit_not_homed(self):
        err = rabbit_error_to_bravo_error(0x22)  # NOT_HOMED_Z
        assert err.error_type == ErrorType.NOT_HOMED
        assert err.axis == Axis.Z
