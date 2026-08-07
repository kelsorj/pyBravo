## In memory of Ben Shamah

This project exists because of Ben Shamah (1973–2018).

Ben studied mechanical engineering at UC San Diego and robotics at Carnegie
Mellon, where he worked on NASA-funded robots built to explore other planets.
He brought that work back down to the bench, joining Velocity11 to build
machines that would speed up medical research — and it was there, with
colleagues including Brad Nelson, Dave Assmusen, and Chris Shaw, that the Bravo
was born. He later co-founded BioNex Solutions and led its technical side.
People who worked with him describe someone quiet and generous, who liked
puzzling out hard engineering problems and liked helping other people solve
theirs even more. He died of cancer in 2018, at 45.

The Bravo is a genuinely good machine, and a lot of them are still running. The 
goal here is to let the hardware Ben helped design join the open source 
community.

---

# pyBravo

An open-source Python driver and web control system for Bravo liquid handling
robots. It connects to the instrument over Ethernet or serial, exposes every
motion and liquid-handling operation through a REST API and a browser UI, and
runs complete pipetting workflows. It has a full simulation mode so you can
build and test protocols without touching hardware.

```bash
git clone https://github.com/kelsorj/pyBravo.git
cd pyBravo
./scripts/start_pybravo.sh      # Windows: scripts\start_pybravo.bat
```

Then open <http://localhost:8000>. Out of the box the server runs against a
simulated robot, so this works on any machine.

> [!WARNING]
> This software drives a physical robot with enough force to break labware,
> crush a pipette head, or injure a hand. Read
> [docs/safety.md](docs/safety.md) before you connect to real hardware, and
> dry-run every new protocol in simulation first.

https://github.com/user-attachments/assets/48de0bf6-f5a1-409d-ab84-00fcac42d9b6

---

## What it does

- **Direct hardware control** — connect, initialize, home, move, and jog every
  axis, with live position and state streaming over WebSocket.
- **Liquid handling** — aspirate, dispense, and mix, with configurable liquid
  classes and pipette techniques.
- **Tips and labware** — tip pickup and ejection, an editable labware catalog,
  gripper pick-and-place, and lid handling.
- **Visual workflow designer** — build protocols as a node graph in the
  browser, simulate them, then run them on the instrument.
- **Deck management** — a 3×3 deck model with per-location teachpoints, and
  obstacle-aware Z clearance when the gripper carries plates between locations.
- **3D digital twin** — watch a URDF model of the robot mirror the real
  machine's position in real time.
- **Simulation mode** — the full motion, liquid-handling, and workflow stack
  runs with no hardware attached. The optional accessories below are the
  exception: they talk to real serial ports.
- **Optional extras** — camera-based deck verification, barcode reading,
  orbital shaker control, and an LLM-assisted protocol drafter.

## Supported hardware

| Instrument generation | `controller_type` | Connection |
|---|---|---|
| Darwin-generation Bravo | `darwin_native` | TCP, port 7613 |
| Agile 7612 Bravo | `agile_7612` | TCP, port 7612 |
| Bravo SRT | `agile_srt` | TCP, port 7612 |
| Legacy Agile Bravo | `agile` | Serial, or TCP port 10000 |
| No hardware | `simulation` | — |

Details and wiring notes are in [docs/hardware-setup.md](docs/hardware-setup.md).

## Documentation

Start with the [documentation index](docs/README.md). The most-used pages:

| Page | What it covers |
|---|---|
| [Installation](docs/installation.md) | Requirements and install paths for macOS, Linux, and Windows |
| [Quickstart](docs/quickstart.md) | First run in simulation, then your first real move |
| [Hardware setup](docs/hardware-setup.md) | Networking, finding your robot, profiles, teachpoints |
| [Safety](docs/safety.md) | What can go wrong and how to avoid it |
| [User guide](docs/user-guide.md) | The web UI, end to end |
| [Workflows](docs/workflows.md) | Building and running protocols in the designer |
| [Configuration](docs/configuration.md) | Profiles, environment variables, config files |
| [API reference](docs/api-reference.md) | Every HTTP and WebSocket endpoint |
| [Architecture](docs/architecture.md) | How the codebase fits together |
| [Protocol specification](docs/protocol-spec.md) | The instrument wire protocols |
| [Troubleshooting](docs/troubleshooting.md) | Symptoms, causes, fixes |
| [FAQ](docs/faq.md) | Common questions |

## Requirements

Python 3.11 or newer. The launcher scripts use [uv](https://docs.astral.sh/uv/)
when it is available, which installs a suitable Python and all dependencies
automatically; see [Installation](docs/installation.md) for the alternatives.

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for how to
set up a development environment, run the tests, and submit a change. Please
also read the [Code of Conduct](CODE_OF_CONDUCT.md). To report a security
issue, follow [SECURITY.md](SECURITY.md).

## License

pyBravo is an independent project. It is not affiliated with, endorsed by, or
supported by Agilent Technologies, Inc. Product names are trademarks of their
respective owners and are used only to identify the hardware this software
runs. See [NOTICE](NOTICE) for the full trademark statement.
