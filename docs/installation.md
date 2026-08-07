# Installation

How to install OpenBravo on macOS, Linux, or Windows, including the optional
LLM extras and the optional camera-based deck verification service. Nothing on
this page requires a robot — the software runs fully in simulation.

---

## Requirements

| Requirement | Notes |
|---|---|
| Python 3.11 or newer | Declared as `requires-python = ">=3.11"` in `pyproject.toml`. 3.11, 3.12, and 3.13 are all supported by the launcher scripts. |
| Git | To clone the repository. |
| An operating system | macOS, Linux, and Windows are all supported. |
| Network access to the instrument | Only needed for real hardware. See [hardware setup](hardware-setup.md). |

The runtime dependencies (FastAPI, uvicorn, pydantic, PyYAML, pyserial,
pymongo, structlog, websockets, python-dotenv, python-multipart) are declared in
`pyproject.toml` and pinned in `uv.lock`. You do not install them by hand.

No database is required. OpenBravo can use MongoDB to share a labware catalog
across machines, but a single-machine install works with no database at all —
see the note in [Verifying the install](#verifying-the-install).

---

## Option 1 — install with uv (recommended)

[uv](https://docs.astral.sh/uv/) is a Python package and project manager. It
does three jobs here that otherwise fall to you:

- It downloads and manages a suitable Python interpreter, so you do not need a
  system Python 3.11+ at all.
- It creates and maintains the project virtualenv in `.venv/`.
- It installs the exact dependency versions recorded in `uv.lock`, so everyone
  gets the same environment.

The launcher scripts detect uv automatically. If it is on your `PATH`, they use
it; if it is not, they fall back to other interpreters (see
[How the launcher picks an interpreter](#how-the-launcher-picks-an-interpreter)).

### 1. Install uv

On macOS or Linux:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

On Windows, follow the installation instructions at
<https://docs.astral.sh/uv/getting-started/installation/>. uv is also available
from Homebrew, `pipx`, and most package managers.

### 2. Clone the repository

```bash
git clone https://github.com/kelsorj/OpenBravo.git
```

### 3. Start the server

```bash
cd OpenBravo && ./scripts/start_pybravo.sh
```

On Windows, run `scripts\start_pybravo.bat` instead — but read
[Windows specifics](#windows-specifics) first, because the Windows launcher
does not use uv.

The first run takes a minute or two while uv fetches an interpreter and builds
the environment. Subsequent runs are fast. The launcher prints the interpreter
it chose, for example:

```
Starting pybravo.web.server with: uv run --frozen python -B -m pybravo.web.server
```

Then open <http://localhost:8000>.

### How the launcher picks an interpreter

`scripts/start_pybravo.sh` and `scripts/start_vision_service.sh` both delegate
to `scripts/_pybravo_launch.sh`, which tries these in order and uses the first
one that works:

1. `$PYBRAVO_PYTHON` — an explicit override. Point it at any interpreter you
   already have, including a conda environment.
2. `uv` — runs `uv run --frozen`, which re-syncs `.venv/` against `uv.lock` on
   every launch. A `git pull` that adds a dependency therefore cannot leave you
   with a stale environment.
3. `./.venv/bin/python` — a virtualenv already present in the repository.
4. `python3.13`, `python3.12`, or `python3.11` on `PATH`.

If none of those is available, the launcher exits with instructions rather than
failing obscurely.

Both launchers pass `-B` to Python so no `.pyc` files are written. This matters
if you are editing controller code — stale bytecode cannot survive a change.

Because the uv path uses `--frozen`, optional extras are pruned unless you ask
for them. Use `PYBRAVO_EXTRAS` to add them:

```bash
PYBRAVO_EXTRAS=llm ./scripts/start_pybravo.sh
```

`PYBRAVO_EXTRAS` accepts a space-separated list, so `PYBRAVO_EXTRAS="llm dev"`
works too.

---

## Option 2 — install with pip and a virtualenv

Use this if you cannot install uv, or you want the environment somewhere you
control. You need a system Python 3.11 or newer.

### macOS and Linux

```bash
git clone https://github.com/kelsorj/OpenBravo.git
```

```bash
cd OpenBravo && python3.12 -m venv .venv
```

```bash
.venv/bin/pip install -e .
```

```bash
./scripts/start_pybravo.sh
```

The launcher finds `./.venv/bin/python` on its own, so you do not need to
activate the virtualenv first.

### Windows

```bat
git clone https://github.com/kelsorj/OpenBravo.git
```

```bat
cd OpenBravo && py -3.12 -m venv .venv
```

```bat
.venv\Scripts\activate
```

```bat
pip install -e .
```

```bat
scripts\start_pybravo.bat
```

> **Note on pinning.** `pip install -e .` resolves dependencies fresh against
> PyPI using the version floors in `pyproject.toml`. It does not read
> `uv.lock`. If you need the exact locked versions, use the uv path.

### Running the server directly

Both launchers are thin wrappers. Once an environment exists you can also run:

```bash
python -B -m pybravo.web.server
```

Run this from the repository root. The server resolves the profile directory
relative to the current working directory (`./profiles`), so starting it
elsewhere will create an empty profile directory in the wrong place. Override
it with `PYBRAVO_PROFILE_DIR` if you need to.

---

## Windows specifics

A few things differ on Windows and are worth knowing before you start.

**The Windows launcher does not use uv.** `scripts\start_pybravo.bat` runs
`%PYBRAVO_PYTHON%` if that variable is set, and otherwise plain `python` from
`PATH`. It does not perform the multi-step interpreter search that the shell
launcher does. So on Windows you must either:

- activate a virtualenv that has the package installed before running the
  `.bat`, or
- set `PYBRAVO_PYTHON` to a full interpreter path, or
- run `uv run --frozen python -B -m pybravo.web.server` yourself.

Pointing the launcher at a specific interpreter:

```bat
set PYBRAVO_PYTHON=C:\Users\you\OpenBravo\.venv\Scripts\python.exe
```

**Corporate TLS interception.** The `llm` extra includes `pip-system-certs` on
Windows only. On machines behind a re-signing proxy, Python's bundled
certificate store does not trust the corporate CA; `pip-system-certs` makes
`urllib3`/`httpx` use the Windows certificate store instead. It is harmless on
machines without such a proxy.

**Serial ports** are named `COM1`, `COM5`, and so on rather than
`/dev/ttyUSB0`. This applies to the legacy serial controller type and to the
barcode reader configuration in a profile.

**Launching the vision service from the UI** (`POST /api/vision/service/start`)
is implemented for Windows only. On macOS and Linux, start it from the shell —
see [The optional vision service](#the-optional-vision-service).

---

## Optional extras

The project defines two optional dependency groups in `pyproject.toml`.

### `dev` — testing and linting

Adds `pytest`, `pytest-asyncio`, `pytest-cov`, and `ruff`.

With uv (nothing to install ahead of time — uv resolves it per command):

```bash
uv run --extra dev python -m pytest
```

With pip:

```bash
pip install -e ".[dev]"
```

### `llm` — the workflow drafter

Adds `instructor`, `anthropic`, `openai`, `pypdfium2`, and `Pillow` (plus
`pip-system-certs` on Windows). These back the LLM-assisted protocol drafter at
`POST /api/workflow/draft` and the PDF page rendering used by its picker modal.

With uv:

```bash
PYBRAVO_EXTRAS=llm ./scripts/start_pybravo.sh
```

With pip:

```bash
pip install -e ".[llm]"
```

The drafter needs an API key. Copy `.env.example` to `.env` in the repository
root and fill in `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`. The server loads
`.env` at startup; a shell-exported variable takes precedence over the file.
`.env` is gitignored. The same file documents the optional drafter overrides
(`PYBRAVO_DRAFTER_PROVIDER`, `PYBRAVO_DRAFTER_MODEL`, and others) — see
[configuration](configuration.md).

Everything except the drafter works without this extra. If it is not installed,
the drafter endpoints return an error explaining what to install; nothing else
is affected.

---

## The optional vision service

OpenBravo can use a depth camera to verify that the physical deck matches what
the software expects before a run. This is optional and disabled by default.

The vision service is a **separate process** listening on port **8101**. The
main server talks to it over HTTP at the URL in the active profile.

### Dependencies you must install yourself

The camera stack is deliberately not part of the base install:

- **The Orbbec SDK Python bindings (`pyorbbecsdk`) are not vendored** with
  OpenBravo and are not in `uv.lock`. You install them separately, following
  Orbbec's own build and install instructions for your platform. The default
  location OpenBravo expects is `external/pyorbbecsdk` — the `external/`
  directory is gitignored precisely because the SDK lives there and is not part
  of this repository.
- **NumPy** is imported unconditionally by the camera module and is *not* a
  declared project dependency. Install it into the same environment.
- **OpenCV** (`cv2`) is imported optionally and is used for image encoding and
  the region-of-interest overlays. Install it if you want the preview and
  calibration overlays.

### Profile settings

The camera is configured per profile under the `vision:` block. The shipped
defaults are:

```yaml
vision:
  enabled: false
  service_url: http://127.0.0.1:8101
  sdk_root: external/pyorbbecsdk
```

Set `enabled: true` to turn the feature on. Point `sdk_root` at wherever you
actually installed the Orbbec bindings if it is not the default path. These
three fields are editable from the **Profiles** tab in the UI ("Enable deck
verification camera", "Vision service", "Vision SDK root") and via
`PATCH /api/profile`.

While `enabled` is `false`, every `/api/vision/*` endpoint returns
`404 Vision feature is disabled in the active profile`, which is the expected
behavior for a normal install.

### Starting the service

```bash
./scripts/start_vision_service.sh
```

On Windows:

```bat
scripts\start_vision_service.bat
```

Both are thin wrappers around `python -m pybravo.vision_service`, and the shell
version uses the same interpreter-resolution order as the main launcher.

Check it is alive with `GET /api/vision/status` from the main server, or by
requesting the service's own `/status` endpoint on port 8101. If no camera is
attached the service reports `camera_available: false` with the underlying
import or device error in `message`; if `PYBRAVO_VISION_STATIC_IMAGE` points at
an image file, it runs in `static_image` mode for development.

Calibration and day-to-day use are covered in the [user guide](user-guide.md).

---

## Verifying the install

### 1. The server starts and serves the UI

```bash
./scripts/start_pybravo.sh
```

Open <http://localhost:8000>. You should see the PyBravo UI: a header with a
connection indicator, a 3D viewport, and a tabbed side panel. The API's
interactive documentation is at <http://localhost:8000/docs>.

**A MongoDB warning on first run is expected.** The server logs something like:

```
WARNING  pybravo.deck.labware: Falling back to local labware snapshot after Mongo failure: ...
```

This is not an error. The labware catalog can optionally be backed by MongoDB
(configured in `config/labware_catalog.yaml`); when no database is reachable,
OpenBravo falls back to a local snapshot and keeps working normally. See
[troubleshooting](troubleshooting.md#the-server-logs-a-mongodb-connection-warning-on-startup)
for the full explanation.

### 2. The tests pass

```bash
uv run --extra dev python -m pytest
```

Or, in an activated virtualenv with the `dev` extra installed:

```bash
python -m pytest
```

Expect roughly **616 passing tests**, plus:

- **One known pre-existing failure**:
  `test_back_left_rectangle_uses_front_right_tipbox_anchor` in
  `tests/test_bravo_init.py`. This is a known issue and does not indicate a
  broken install.
- **Some skipped tests.** Skips are normal. Several tests skip when an optional
  fixture or dependency is absent — for example the legacy protocol import
  tests skip without a local protocol fixture, and the vision tests skip when
  NumPy is not installed.

A run that reports only that one failure plus some skips is a healthy install.

### 3. The simulated robot moves

Follow the [quickstart](quickstart.md). It walks through connecting to the
built-in simulation, homing, jogging, and running an aspirate and a dispense —
no hardware required.

---

## Updating

```bash
git pull
```

**If you use uv**, that is the whole update. The launcher runs
`uv run --frozen` on every start, which re-syncs `.venv/` against the
(now updated) `uv.lock` before launching. New dependencies are installed
automatically; removed ones are pruned. If you use optional extras, keep
passing `PYBRAVO_EXTRAS` — `--frozen` prunes extras you do not ask for.

**If you installed with pip**, re-run the install after pulling so any new or
changed dependencies are picked up:

```bash
.venv/bin/pip install -e .
```

Add the extras you use, for example `.venv/bin/pip install -e ".[dev,llm]"`.

After updating, re-run the test suite as described above before you drive real
hardware.

### Your configuration survives an update

Profiles in `profiles/*.yaml` and the catalogs in `config/` are ordinary files
in the working tree. `git pull` will not silently discard local edits, but it
can conflict with them. If you have customised a shipped profile, consider
duplicating it under your own name (Profiles tab → **Duplicate…**, or
`POST /api/profile/duplicate`) so upstream changes to the shipped profiles
never collide with your machine's settings.

---

## Before you connect to real hardware

> **This software drives a physical robot.** It can break labware, damage a
> pipette head, and injure hands. Read [safety](safety.md) before connecting to
> an instrument, and dry-run every new protocol in simulation first.

Installation alone does not touch hardware — nothing moves until you select a
non-simulation controller type and connect. When you are ready, continue with
[hardware setup](hardware-setup.md).

---

## Next steps

- [Quickstart](quickstart.md) — your first fifteen minutes, in simulation.
- [Hardware setup](hardware-setup.md) — networking, finding the instrument,
  profiles, and teachpoints.
- [Safety](safety.md) — read this before the robot moves.
- [Configuration](configuration.md) — profiles, environment variables, catalogs.
- [Troubleshooting](troubleshooting.md) — if any step above did not go as
  described.
- [Architecture](architecture.md) — how the codebase fits together.
