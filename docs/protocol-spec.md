# Protocol specification

The wire protocols pyBravo speaks to Bravo instruments. This page is for
people maintaining the driver or adding support for another instrument
generation; you do not need it to operate the software.

Two protocol families are implemented. Which one applies depends on your
instrument's controller generation.

| Family | Instruments | `controller_type` | Port |
|---|---|---|---|
| Gemini | Darwin-generation | `darwin_native` | TCP 7613 |
| V11 / Agile | Agile, Agile 7612, SRT | `agile`, `agile_7612`, `agile_srt` | TCP 7612, TCP 10000, or serial |

Neither protocol has authentication or encryption. Anyone with network access to
the instrument can command it — see [SECURITY.md](../SECURITY.md).

## Gemini protocol (Darwin-generation)

Implemented in `pybravo/protocol/gemini/`.

### Outer TCP frame

Every message is an 8-byte header followed by a payload:

| Offset | Size | Field | Value |
|---|---|---|---|
| 0–1 | 2 | `msg_sync` | `0xAAAA`, little-endian |
| 2–3 | 2 | `protocol_version` | `0x0001`, little-endian |
| 4–5 | 2 | `payload_type` | little-endian uint16 |
| 6–7 | 2 | `payload_size` | little-endian uint16, bytes following |
| 8… | n | `payload` | interpreted per `payload_type` |

### Payload types

| Value | Name | Payload |
|---|---|---|
| 1 | `PACKET` | Exactly 8 bytes — one packet |
| 4 | `MULTIPACKET` | Up to 512 bytes. Outgoing: N concatenated 8-byte packets. Incoming: an 8-byte response |
| 5 | `SERIAL_DATA` | Exactly 9 bytes — a serial-peripheral payload |

Packet encoding, instruction encoding, and the enum tables live in
`packet.py`, `instruction.py`, and `enums.py`. An instruction is the 4-word
encoding used for motion, delay, and tip operations.

### Axis control

Darwin-generation axes are not initialized through a single firmware call.
Commutation, homing, and initialization are each driven step by step through
`SUBCMD_MOTOR_STATE` writes and polled state reads, implemented as per-axis
state machines in `pybravo/darwin/axis.py`. Driving them explicitly is what
makes retry-on-regression and the timing-sensitive behavior possible.

## V11 / Agile protocol

Implemented in `pybravo/protocol/agile_packet.py`,
`agile_7612_packet.py`, `agile_7612_commands.py`, `agile_7612_crc.py`,
`v11_comm.py`, and `v11_agile_7612_comm.py`.

### Frame order differs by generation

This is the single most important difference, and getting it backwards produces
frames the instrument silently ignores:

- **Legacy Agile:** `[length][cmd][data]`
- **Agile 7612 and SRT:** `[cmd][length_u16_LE][data]`

### Checksum

Agile 7612 and SRT use **CRC-8/MAXIM** (Dallas 1-Wire): polynomial `0x8C`, the
reflected representation of x⁸+x⁵+x⁴+1, with `init=0x00`. This is *not*
CRC-8/SMBUS (`0x07`), and the two agree often enough on short inputs to make a
mistake here hard to spot.

### Agile packets

Command `0xA1` frames carry a 10-byte Agile packet followed by a one-byte axis
index. Byte semantics that matter:

- **Byte[1]** is the axis bitmask for status and trigger commands.
- **Byte[7]** carries the register or operation selector. Notably, the status
  register `0x90` goes in **byte[7]**, not byte[1] — a common error.

Selected byte[7] values:

| Value | Meaning |
|---|---|
| `0x10` | Servo register write |
| `0x31` | Fault reset |
| `0x38` | Move trigger |
| `0x52` | Homing-complete marker |
| `0x90` | Status read |

### Motion commands

| Command | Purpose |
|---|---|
| `0xA2` | `PREPARE_MOVE` — 17-byte struct on Agile 7612 and SRT |
| `0xAA` | `PREPARE_JOG` — force-controlled moves only |
| `0xA7` | Position read |

`PREPARE_JOG` (`0xAA`) is used **only** for force-controlled tip pickup.
Ordinary jogs from the UI use `PREPARE_MOVE` (`0xA2`) followed by a trigger
(byte[7] = `0x38`). Using `0xAA` for a normal jog applies force where none is
wanted.

### Header selection

Servo register writes use a header of `local_axis * 0x10`. Two operations do
not follow that rule:

- **Home-complete register writes** use header `0x01`.
- **The homing-complete marker** (byte[7] = `0x52`) uses header `0x00`.

### Homing

Homing is host-driven and stateful. The ordering constraints are real
requirements, not conventions:

1. Register `0x4A` must be **read** before homing begins.
2. After servo configuration, register `0x10` must be **read** via header
   `0x09`. Without both reads the firmware never enters homing mode, and the
   failure is silent.
3. The phase pattern is chosen at runtime from the home-sensor state in register
   `0x10`: on-sensor gives a 2-phase home (depart, then slow approach back);
   off-sensor gives a 3-phase home (approach, depart overshoot, slow approach).
4. A homing-complete marker (byte[7] = `0x52`) is sent per axis, with an empty
   data field. The X and Y markers can carry situational data bytes; those must
   not be replayed from one homing scenario into another.

`tests/test_agile_srt_homing.py` pins the complete SRT homing frame sequence,
because homing is the operation most likely to damage hardware and cannot be
exercised in CI.

### Generation-specific notes

**Agile 7612.** Two servo controllers, indexed 0 and 4. Controller 2 requires a
fault reset (mask `0x01`, axis index 4, byte[7] = `0x31`) after **every** move,
not only during G-axis homing. The firmware supports neither ADC head read
(`0xB3`, which causes audible clicking) nor smart head detect (`0xB5`), so head
type must be configured manually.

**SRT.** Four axes — X, Y, Z, W — with no gripper, and four servo controllers
indexed 0–3, so homing servo headers are `0x00`, `0x10`, `0x20`, `0x30`. Homing
order is Z, W, X, Y. The home-complete register field in `PREPARE_MOVE` is
encoded `0x01nn`. The W (pipettor) axis needs a pump-parameter pre-config block
written before its homing servo configuration, and a distinct register-`0xA0`
value.

## Units and scaling

Positions cross the wire as encoder ticks. Conversion to engineering units uses
each axis's `ticks_per_eng_unit` from the profile.

The W axis is the trap: its `ticks_per_unit` must come from the profile
(448.0), not the inherited default (48.0). A wrong value here means every
volume is wrong by roughly a factor of nine.

## Known measurement limitations

- The W position register is unstable, with a spread of roughly 5–10 µL between
  consecutive reads. Static readings are directionally correct but noisy.
- Controller 2 position reads (G and Zg) are unreliable while motion is active.
  At-rest values are correct.

## Adding a new generation

Add a codec under `pybravo/protocol/` and a controller under
`pybravo/controllers/` implementing the interface in `base.py`. Keep encoding in
the codec, not the controller. The transport layer is deliberately thin, so a
mock transport lets you test the full encode/decode path with no instrument —
see [Architecture](architecture.md).
