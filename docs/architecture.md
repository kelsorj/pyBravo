# Architecture

How pyBravo is put together, for people who intend to change it. If you only
want to run the software, start with the [Quickstart](quickstart.md) instead.

## The shape of the system

pyBravo is a layered stack. Each layer talks only to the one below it, which
is what makes it possible to support four instrument generations plus a
simulator behind one API.

```
Browser UI  (frontend/)
     │  HTTP + WebSocket
     ▼
Web layer  (pybravo/web/server.py)
     │  method calls
     ▼
Bravo facade  (pybravo/bravo.py)
     │
     ├──▶ State machine  (pybravo/state_machine/)   high-level operations as abortable tasks
     ├──▶ Deck / labware  (pybravo/deck/)           what is on the deck, and where
     ├──▶ Motion          (pybravo/motion/)         kinematics, speed profiles, path planning
     └──▶ Controller      (pybravo/controllers/, pybravo/darwin/)
                │  one class per instrument generation
                ▼
          Protocol codecs  (pybravo/protocol/)      framing, packets, CRC
                │
                ▼
          Transport        (pybravo/transport/)     TCP or serial bytes
```

The important boundary is `pybravo/controllers/base.py`. It defines the
abstract interface every backend implements — connection, motion, homing, motor
enable, device state, gripper, head detection. Everything above that line is
hardware-independent. Adding support for a new instrument means writing a new
class that satisfies that interface, and nothing above it needs to change.

## Layer by layer

### Web layer — `pybravo/web/`

`server.py` is a single large FastAPI application holding roughly 129 HTTP
routes plus one WebSocket. Routes are declared with a `_route_meta(tag, summary,
description)` helper that feeds FastAPI's OpenAPI schema, so the live
interactive docs at `/docs` stay in sync with the code. `middleware.py` logs
requests.

The single WebSocket, `/ws/state`, pushes the same payload as `GET /api/state`
on a loop. It polls at roughly 30 Hz against the simulator, and throttles to
5 Hz for real hardware so that state polling does not saturate the wire
protocol.

The web layer holds one global `Bravo` instance. It does not talk to hardware
directly.

### Facade — `pybravo/bravo.py`

The `Bravo` class is the API that applications use. It owns the profile, picks
and constructs the right controller from `profile.connection.controller_type`,
and wires together deck state, head configuration, and the state machine. If
you are writing a Python script against this project rather than using the web
UI, this is your entry point.

### State machine — `pybravo/state_machine/`

Every high-level operation — initialize, home, move to location, pick and place,
delid, aspirate, dispense, mix, tips on, tips off, scan stack height — is a
task object in `tasks.py`. `engine.py` runs them with support for abort, retry,
and ignore, which is what lets the UI recover from a recoverable error instead
of dying.

`tasks.py` is large. It is the single place where "what the operator asked for"
becomes "the ordered sequence of axis moves that accomplishes it," so it
concentrates a lot of the domain knowledge about doing things safely.

### Deck and motion — `pybravo/deck/`, `pybravo/motion/`

`deck/` models a 3×3 deck: which labware sits at each location, teachpoints for
each location, and stack tracking. `motion/` converts engineering units to
encoder ticks, applies per-axis speed profiles, and plans paths that keep the
head above obstacles.

These are pure software models. Nothing here senses the physical world, which
is why a mismatch between the model and reality causes crashes — see
[Safety](safety.md).

### Controllers — `pybravo/controllers/`, `pybravo/darwin/`

| Backend | Class | Notes |
|---|---|---|
| `simulation` | `SimulationController` | Tracks position in software; no I/O |
| `agile` | `AgileController` | Legacy instruments; serial or TCP 10000 |
| `agile_7612` | `Agile7612Controller` | Subclasses `AgileController`; TCP 7612 |
| `agile_srt` | `AgileSrtController` | Subclasses `Agile7612Controller`; four axes, no gripper |
| `darwin_native` | `DarwinController` | Separate stack under `pybravo/darwin/`; TCP 7613 |

The Agile family forms an inheritance chain, because each generation is a
variation on the one before. Darwin-generation hardware speaks a different
protocol entirely and gets its own package, with per-axis state machines for
commutation, homing, and initialization in `darwin/axis.py`.

### Protocol and transport — `pybravo/protocol/`, `pybravo/transport/`

`protocol/` holds the codecs: frame headers, packet structures, CRC, and
command encoding. The `gemini/` subpackage implements the protocol used by
Darwin-generation firmware; the `agile_7612_*` modules implement the other
family. See [Protocol specification](protocol-spec.md) for the wire formats.

`transport/` is deliberately thin — it moves bytes over TCP or a serial port
and knows nothing about their meaning. This split is what makes the protocol
layer testable with a mock transport and no hardware.

## Supporting subsystems

- **`pybravo/profile/`** — YAML-based per-machine configuration, replacing the
  Windows registry configuration older systems used. `reg_import.py` reads
  registry profile exports and legacy `.dat` directory trees so an existing
  instrument's configuration can be migrated.
- **`pybravo/workflow/`** — the node-graph protocol system: `models.py` for the
  data model, `storage.py` for JSON persistence, and `executor.py`, which walks
  the graph and dispatches state-machine tasks. The `drafter/` subpackage is the
  optional LLM assist.
- **`pybravo/vision/`** and **`pybravo/vision_service.py`** — optional
  camera-based deck verification. The camera runs in a *separate* process
  (default port 8101) because the SDK is a heavy, optional dependency; the main
  server talks to it over HTTP via `vision_client.py`, and works fine when it is
  absent.
- **`pybravo/accessories/`** — barcode reader and orbital shaker drivers, with a
  manager handling their lifecycle.
- **`pybravo/model/`** — the URDF robot model and meshes that the browser renders
  as a live digital twin.

## How a request becomes motion

Tracing `POST /api/move` end to end:

1. FastAPI parses the request and calls the handler in `server.py`.
2. The handler calls the corresponding method on the global `Bravo` instance.
3. `Bravo` creates the appropriate task and hands it to the state machine
   engine, which can abort or retry it.
4. The task consults motion and deck state to turn the requested engineering
   units into a safe target in encoder ticks.
5. The task calls the controller's `move` method — the first hardware-specific
   step.
6. The controller encodes the command through the protocol codec.
7. The transport writes bytes to the socket or serial port.
8. Position updates flow back up and are broadcast to every browser over
   `/ws/state`.

## Testing strategy

The transport boundary is what makes this testable. Most protocol and
controller tests substitute a mock transport that replies with well-formed
frames, so the full encode/decode path runs with no instrument attached.

- `tests/protocol/gemini/` — codec-level tests for framing, packets, instructions
- `tests/darwin/` — axis state machines, homing, safety limits, motion, topology
- `tests/test_agile_srt_homing.py` — a golden-file test pinning the SRT homing
  frame sequence, because homing is the operation most likely to damage hardware
  and cannot be exercised in CI
- `tests/test_web_server.py` — API endpoint behavior
- `tests/test_bravo_init.py` — the facade's behavior, the largest suite

Run everything with `uv run --extra dev python -m pytest`.

## Extending it

**A new instrument generation:** subclass `BravoController` (or the closest
existing Agile class), implement the interface, add your `controller_type` to
the selection logic in `pybravo/bravo.py`, and add a profile. If the wire format
differs, add a codec under `pybravo/protocol/` rather than putting encoding
logic in the controller.

**A new high-level operation:** add a task in `pybravo/state_machine/tasks.py`,
expose it on `Bravo`, then add a route. Keeping the logic in the task means it
works from the API, from the workflow executor, and from a Python script.

**A new workflow node:** add the node type to the designer, then map it to a
task in `NODE_TYPE_MAP` in `pybravo/workflow/executor.py`.

**A new accessory:** add a driver under `pybravo/accessories/` and register it
with the manager.
