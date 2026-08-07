# CLAUDE.md — working notes for contributors and AI assistants

Conventions and hard-won details for working in this codebase. If you are new
here, read [docs/architecture.md](docs/architecture.md) first for the map, then
come back to this for the rules.

## What this project is

OpenBravo is a Python/FastAPI control system for Bravo liquid handling robots.
It supports several instrument generations behind one interface, plus a
simulation backend that needs no hardware.

| `controller_type` | Hardware | Transport |
|---|---|---|
| `darwin_native` | Darwin-generation Bravo | TCP port 7613 (Gemini protocol) |
| `agile_7612` | Agile 7612 Bravo | TCP port 7612 |
| `agile_srt` | Bravo SRT | TCP port 7612 |
| `agile` | Legacy Agile Bravo | Serial, or TCP port 10000 |
| `simulation` | None | — |

Everything that touches hardware goes through the abstract interface in
`pybravo/controllers/base.py`. A new backend only has to satisfy that
interface.

## Safety comes first

This code moves a real machine. When changing anything in `controllers/`,
`state_machine/`, `motion/`, or `darwin/`:

- Never widen an axis range, raise a current limit, or remove a sensor check
  without saying so explicitly in the change description.
- Homing and tip-pickup sequences are the highest-risk paths. `Z` and `W`
  moves can drive the head into the deck.
- Prefer failing closed. If state is unknown, stop rather than guess.
- Test in `simulation` first, always.

## Key files

### Core
- `pybravo/bravo.py` — the `Bravo` facade; the main entry point applications use
- `pybravo/controllers/base.py` — abstract controller interface
- `pybravo/state_machine/tasks.py` — every high-level operation as a task
- `pybravo/web/server.py` — the FastAPI app and all HTTP/WebSocket routes
- `pybravo/profile/profile.py` — YAML profile configuration

### Protocol
- `pybravo/protocol/gemini/` — Gemini protocol (Darwin-generation instruments)
- `pybravo/protocol/agile_7612_*.py`, `v11_agile_7612_comm.py` — Agile 7612 framing, packets, CRC
- `docs/protocol-spec.md` — the wire protocol reference

### Tests
- `tests/` — 33 files. Run with `uv run --extra dev python -m pytest`
- `tests/test_agile_srt_homing.py` — golden-file test pinning the SRT homing
  frame sequence. If it fails, the homing sequence changed; confirm the change
  is intended and hardware-verified before touching the fixture.

## Running

```bash
# Server — macOS/Linux
./scripts/start_pybravo.sh

# Server — Windows
scripts\start_pybravo.bat

# Tests
uv run --extra dev python -m pytest
```

The UI is at <http://localhost:8000>. Use `-B` (the launchers already do) so
stale bytecode can't survive a code change.

## Critical protocol details

These are non-obvious and have each caused real bugs:

- **V11 Agile 7612 frame order** is `[cmd][length_u16_LE][data]`, NOT the
  standard `[length][cmd][data]`.
- **CRC is CRC-8/MAXIM** (polynomial 0x8C, reflected), NOT CRC-8/SMBUS (0x07).
- **Status register (0x90) goes in byte[7]** of the Agile packet, not byte[1].
  Byte[1] is the axis bitmask for status and trigger commands.
- **Home-complete register writes use header 0x01**, not the servo-write header
  (`local_axis * 0x10`).
- **The homing-complete marker (byte7=0x52) uses header 0x00**, not
  `local_axis * 0x10`.
- **Register 0x4A must be read before homing**, and register 0x10 (via header
  0x09) must be read after servo config. Without both reads the firmware does
  not enter homing mode.
- **PREPARE_JOG (0xAA) is only for force-controlled tip pickup.** UI jogs use
  PREPARE_MOVE (0xA2) plus a trigger (byte7=0x38).
- **W-axis `ticks_per_unit` must come from the profile** (448.0), not the
  parent class default (48.0).
- **Controller 2 fault reset** (mask=0x01, axis_idx=4, byte7=0x31) must be sent
  after every move, not only during G homing.

## Logging

Use the stdlib `logging` module with the central config in
`pybravo/logging_config.py`. Call `configure_logging()` once at startup.

- `logger = logging.getLogger(__name__)` at module level, one per file
- Use %-style formatting: `logger.info("msg %s", val)` — not f-strings
- Levels: `TRACE` (5) for raw protocol hex dumps, `DEBUG` for controller state
  and internal decisions, `INFO` for connection events and operation starts,
  `WARNING` for recoverable problems, `ERROR`/`EXCEPTION` for aborted operations
- Gate expensive formatting: `if logger.isEnabledFor(TRACE): ...`
- Never use `print()` for operational logging — only for CLI output in scripts
- Never call `logging.basicConfig()`

Environment variables: `PYBRAVO_LOG_LEVEL`, `PYBRAVO_LOG_FILE`,
`PYBRAVO_LOG_DIR`, `PYBRAVO_PROTOCOL_TRACE`. See
[docs/configuration.md](docs/configuration.md).

## Documentation rules

Documentation is public and describes this software on its own terms.

- Describe file formats functionally: a `.pro` file is "a legacy XML protocol
  file"; a `.reg` import reads "a Windows registry profile export".
- Do not name other companies' software products, and do not frame the protocol
  work as reverse engineering. Naming the Bravo hardware itself is fine.
- Never commit packet captures, vendor manuals, registry exports from a real
  instrument, or anything containing a lab's calibration or teachpoint data.

## Known issues

- **W position bounces** — the W position register is unstable, with a spread of
  roughly 5–10 µL between reads. Static reads are directionally correct but noisy.
- **G/Zg positions during motion** — controller 2 position reads jump around
  during active motion. At-rest positions are correct.
- **Head detection is unavailable on Agile 7612** — the firmware supports
  neither ADC read (0xB3, which causes clicking) nor smart head detect (0xB5).
  Set the head type manually in the profile.
- **`test_back_left_rectangle_uses_front_right_tipbox_anchor`** in
  `tests/test_bravo_init.py` currently fails. It is a pre-existing failure in
  head-mode anchor selection, unrelated to protocol work.
