# OpenBravo documentation

Control software for Bravo liquid handling robots. Start wherever you are.

> [!WARNING]
> This software moves a physical robot. Read [Safety](safety.md) before
> connecting to real hardware.

## New here

1. **[Installation](installation.md)** — get it running on macOS, Linux, or Windows.
2. **[Quickstart](quickstart.md)** — first 15 minutes, in simulation, no hardware needed.
3. **[Safety](safety.md)** — read this before your first real move.
4. **[Hardware setup](hardware-setup.md)** — networking, profiles, teachpoints, calibration.

## Operating the instrument

| Page | What it covers |
|---|---|
| [User guide](user-guide.md) | Every screen in the web UI and the normal order of operations |
| [Workflows](workflows.md) | Building, simulating, and running protocols in the designer |
| [Configuration](configuration.md) | Profiles, environment variables, and config files |
| [Troubleshooting](troubleshooting.md) | Symptom → cause → fix |
| [FAQ](faq.md) | Common questions |

## Building on it

| Page | What it covers |
|---|---|
| [API reference](api-reference.md) | Every HTTP endpoint and the state WebSocket |
| [Architecture](architecture.md) | How the codebase is layered and where to extend it |
| [Protocol specification](protocol-spec.md) | The instrument wire protocols |

The live, interactive API documentation is at <http://localhost:8000/docs> when
the server is running.

## Contributing

See [CONTRIBUTING.md](../CONTRIBUTING.md) for development setup and how changes
get reviewed, [CODE_OF_CONDUCT.md](../CODE_OF_CONDUCT.md) for community
expectations, and [SECURITY.md](../SECURITY.md) for reporting a vulnerability or
a safety defect.

Design specifications for individual subsystems live in
[superpowers/specs/](superpowers/specs/).

## Quick reference

**Start the server**

```bash
./scripts/start_pybravo.sh
```

**Run the tests**

```bash
uv run --extra dev python -m pytest
```

**Turn on debug logging**

```bash
PYBRAVO_LOG_LEVEL=DEBUG ./scripts/start_pybravo.sh
```

The UI is at <http://localhost:8000>. Supported instruments and their
`controller_type` values are listed in [Hardware setup](hardware-setup.md).
