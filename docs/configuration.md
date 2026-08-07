# Configuration

Everything OpenBravo reads at startup: environment variables, the files in
`config/`, and the instrument profiles in `profiles/`. Most installations only
need a profile — the rest have working defaults.

---

## Configuration precedence

Three layers, highest priority first:

1. **The shell environment.** Variables exported in the shell that starts the
   server always win.
2. **The `.env` file** in the project root. On startup the server loads it into
   the process environment without overriding anything already set, so `.env`
   supplies values the shell did not. Copy [`.env.example`](../.env.example) to
   `.env` and edit it. `.env` is gitignored; `.env.example` is the tracked
   template and must never contain real credentials.
3. **Built-in defaults**, listed in the tables below. Some of them read from a
   file in `config/` first — where that is the case, the environment variable
   overrides the file.

Instrument configuration is separate: it lives in profiles, not environment
variables. See [Profiles](#profiles) below and
[hardware-setup.md](hardware-setup.md).

If `python-dotenv` is not installed, or there is no `.env` file, loading is a
quiet no-op and the shell environment plus defaults apply.

---

## Environment variables

### Logging

Logging is configured once at process startup. See
[troubleshooting.md](troubleshooting.md) for how to use these when diagnosing a
problem.

| Variable | Purpose | Default |
|---|---|---|
| `PYBRAVO_LOG_LEVEL` | Root log level for all `pybravo.*` loggers. Accepts any standard level name. Set `DEBUG` to see controller and state-machine detail. | `INFO` |
| `PYBRAVO_LOG_FILE` | Also write console output to this file, with rotation at 10 MB keeping 3 backups. | unset (console only) |
| `PYBRAVO_LOG_DIR` | Write three rotated files in this directory: `pybravo.log` (everything), `protocol.log` (protocol and transport loggers), `api.log` (web layer). Takes precedence over `PYBRAVO_LOG_FILE`. | unset |
| `PYBRAVO_PROTOCOL_TRACE` | Set to `1`, `true`, or `yes` to enable TRACE-level hex dumps of protocol frames and raw transport bytes. Very verbose; zero cost when off. | off |

### Profiles and launchers

| Variable | Purpose | Default |
|---|---|---|
| `PYBRAVO_PROFILE_DIR` | Directory holding profile YAML files. Created if missing. | `profiles/` under the current working directory |
| `PYBRAVO_PYTHON` | Interpreter the `scripts/start_pybravo.*` and `scripts/start_vision_service.*` launchers should use. Bypasses their interpreter search. | unset (launcher searches for `uv`, then `.venv`, then a system Python 3.11+) |
| `PYBRAVO_EXTRAS` | Space-separated optional dependency groups for the launcher to install when running under `uv` — for example `llm`. | unset |

### Labware catalog

The labware catalog can be backed by MongoDB so several machines share one
catalog. When MongoDB is not configured or is unreachable, the server falls
back to a local snapshot file, so a single-machine install works with no
database at all. After every successful MongoDB load the snapshot is rewritten,
which means the fallback stays current.

Defaults marked "from `config/labware_catalog.yaml`" read that file first; the
environment variable overrides it.

| Variable | Purpose | Default |
|---|---|---|
| `PYBRAVO_LABWARE_MONGO_URI` | MongoDB connection string for the shared labware catalog. Leave empty to disable MongoDB entirely. | from `config/labware_catalog.yaml` (`mongo.uri`) |
| `PYBRAVO_LABWARE_MONGO_DB` | Database name. | from `config/labware_catalog.yaml` (`mongo.database`) |
| `PYBRAVO_LABWARE_MONGO_COLLECTION` | Collection holding labware type documents. | from `config/labware_catalog.yaml` (`mongo.collection`) |
| `PYBRAVO_LABWARE_MONGO_CLASS_COLLECTION` | Collection holding labware class documents. | from `config/labware_catalog.yaml` (`mongo.class_collection`), else `labware_classes` |
| `PYBRAVO_LABWARE_SNAPSHOT_PATH` | Local snapshot file used as the offline fallback. A relative path is resolved against the repository root. | from `config/labware_catalog.yaml` (`cache.snapshot_path`), else `config/labware_catalog.snapshot.yaml` |
| `PYBRAVO_LABWARE_EDITOR_PATH` | Editable store backing the labware editor UI. | `config/labware_editor.yaml` |
| `PYBRAVO_LABWARE_EDITOR_ASSET_DIR` | Directory for labware assets (3D models and images) served at `/labware-assets`. Created if missing. | `labware/editor_assets/` |

All three MongoDB values — URI, database, and collection — must be set for
MongoDB to be used, and `pymongo` must be installed. If any is missing, or the
connection fails, the snapshot path is used instead and a warning is logged.

### Liquid classes and pipette techniques

Liquid classes are stored the same way: MongoDB when configured, a local YAML
file otherwise. These variables are read from the environment only — they have
no `config/` file equivalent — but they fall back to the labware MongoDB
settings so one database can back both.

| Variable | Purpose | Default |
|---|---|---|
| `PYBRAVO_LIQUID_MONGO_URI` | MongoDB connection string for liquid classes. | value of `PYBRAVO_LABWARE_MONGO_URI`, else unset |
| `PYBRAVO_LIQUID_MONGO_DB` | Database name. | value of `PYBRAVO_LABWARE_MONGO_DB`, else unset |
| `PYBRAVO_LIQUID_MONGO_CLASS_COLLECTION` | Collection holding liquid classes. | `liquid_classes` |
| `PYBRAVO_LIQUID_MONGO_TECHNIQUE_COLLECTION` | Collection holding pipette techniques. | `pipette_techniques` |
| `PYBRAVO_LIQUID_CLASS_STORE_PATH` | Local YAML store, used when MongoDB is not configured or unreachable. | `config/liquid_classes.yaml` |

Liquid classes are keyed by the profile's `machine_id` together with head type
and tip, so sharing a database does not cause one instrument's calibration to
be applied to another.

### Vision

The depth-camera integration runs as a separate HTTP service. See
[hardware-setup.md](hardware-setup.md) for the full setup, including the
separately installed camera SDK.

| Variable | Purpose | Default |
|---|---|---|
| `PYBRAVO_VISION_SERVICE_URL` | Base URL of the vision service, used when no explicit URL is supplied. The main server passes the active profile's `vision.service_url`, which takes precedence; this variable is also exported to the vision service launcher. | `http://127.0.0.1:8101` |
| `PYBRAVO_VISION_SDK_ROOT` | Camera SDK location, exported to the vision service launcher from the profile's `vision.sdk_root`. | `external/pyorbbecsdk` |
| `PYBRAVO_VISION_STATIC_IMAGE` | Path to a still image the vision service should serve instead of opening a camera. Useful for development without hardware. | unset (live camera, falling back to the saved reference image) |

### LLM protocol drafter

Optional. The drafter turns a written protocol into a workflow draft via
`POST /api/workflow/draft`. It needs the `llm` optional dependency group
(`pip install -e '.[llm]'`, or `PYBRAVO_EXTRAS=llm` with the launcher) and at
least one provider API key. Without a key, the endpoint returns an error
explaining what to set; nothing else in the system is affected.

| Variable | Purpose | Default |
|---|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key. Preferred when both keys are present. | unset |
| `OPENAI_API_KEY` | OpenAI API key. | unset |
| `PYBRAVO_DRAFTER_PROVIDER` | Force a provider — `anthropic` or `openai` — when both keys are set. Ignored if the named provider has no key. | automatic |
| `PYBRAVO_DRAFTER_MODEL` | Override the model id. | `claude-sonnet-4-6` (Anthropic) or `gpt-4o` (OpenAI) |
| `PYBRAVO_DRAFTER_MAX_TOKENS` | Maximum response tokens. | `4096` |
| `PYBRAVO_DRAFTER_TEMPERATURE` | Sampling temperature. | `0.1` |
| `PYBRAVO_DRAFTER_REPAIR_ATTEMPTS` | How many times to retry when a draft fails validation. | `2` |
| `PYBRAVO_DRAFTER_MONGO_URI` | MongoDB for drafter persistence. Falls back to the labware URI so one database can serve both. | value of `PYBRAVO_LABWARE_MONGO_URI`, else unset |
| `PYBRAVO_DRAFTER_MONGO_DB` | Database name for drafter collections. | `pybravo_drafter` |
| `PYBRAVO_DRAFTER_PDF_DIR` | Directory for stored source PDFs. | `~/.pybravo/papers` |
| `PYBRAVO_DRAFTER_LOCAL_STORE` | Directory for the JSONL fallback store used when MongoDB is not configured. | `~/.pybravo/drafter_data` |
| `PYBRAVO_DOCLING_URL` | Base URL of a `docling-serve` instance used to parse PDFs for `POST /api/workflow/draft_from_pdf`. Without it that endpoint reports the feature as unconfigured. | unset |
| `PYBRAVO_DOCLING_TIMEOUT` | PDF parse timeout, in seconds. | `300` |

API keys are secrets. Keep them in `.env` (which is gitignored) or in the
shell environment — never in a profile or any tracked file.

---

## Files in `config/`

These are shared, machine-independent data files. Unlike profiles, they are not
per-instrument.

| File | What it is |
|---|---|
| `labware_catalog.yaml` | MongoDB connection settings for the shared labware catalog, plus the path of the local snapshot cache. Overridden by the `PYBRAVO_LABWARE_MONGO_*` and `PYBRAVO_LABWARE_SNAPSHOT_PATH` variables. |
| `labware_catalog.snapshot.yaml` | Cached copy of the catalog, rewritten after each successful MongoDB load and used as the offline fallback. Generated, and excluded from version control. |
| `labware_editor.yaml` | The editable labware store behind the labware editor UI: labware types and labware classes. Seeded from MongoDB or the snapshot on first use. |
| `labware_types.seed.json` | A JSON export of labware type documents, provided for seeding a MongoDB collection. Nothing reads it at runtime. |
| `liquid_classes.yaml` | Liquid classes and pipette techniques, keyed by machine id, head type, and tip. |
| `tips.yaml` | Tip definitions — identifier, capacity in microlitres, physical length, compatible head types, and 3D model path. Tip length feeds the Z tip-length compensation applied to teachpoints, so these values matter for motion. |
| `tip_offsets.yaml` | Per head-and-tip-box overrides for Tips On / Tips Off geometry: eject Z offset, plunger position, and press tolerances. Rows are matched top to bottom, first match wins; anything unset falls back to the profile's `safety.*` values. |
| `vision_calibration.yaml` | Saved camera calibration: the deck regions of interest, empty-deck depth baselines, and reference image path. Written by the vision service. |
| `vision_reference/` | Reference deck images captured during vision calibration. |

---

## Profiles

A profile is the per-instrument configuration: connection details, head type,
axis calibration, safety settings, teachpoints, and accessories. Everything in
this section is about *selecting* a profile — for what goes inside one, see
[hardware-setup.md](hardware-setup.md).

### Where they live

Profiles are YAML files in `profiles/` by default, or in the directory named by
`PYBRAVO_PROFILE_DIR`. The file stem is the profile name: `profiles/my-bravo.yaml`
is the profile `my-bravo`.

The repository ships several example profiles covering the supported instrument
generations. They are examples, not templates that will work unchanged on your
machine — copy one and calibrate it.

### How the active profile is chosen

At startup the server:

1. Creates the profile directory if it does not exist.
2. Reads the `.active_profile` marker file in that directory. If it names a
   profile whose YAML file exists, that profile is loaded.
3. Otherwise loads `default.yaml`.
4. If `default.yaml` does not exist either, starts in simulation mode and
   writes a fresh `default.yaml`.

The marker file records the last profile loaded through the API, so the choice
survives a restart. It is not tracked in version control.

### Switching profiles

| Endpoint | Purpose |
|---|---|
| `GET /api/profiles` | List available profile names and the active one. |
| `GET /api/profile` | Read the active profile's connection, head, safety, vision, and accessory settings. |
| `PATCH /api/profile` | Update fields on the active profile and save to disk. |
| `POST /api/profile/load` | Make a different profile active, by name. |
| `POST /api/profile/duplicate` | Copy a profile to a new name. |
| `POST /api/profile/rename` | Rename a profile. |
| `POST /api/profile/import_reg` | Import a Windows registry profile export. |
| `POST /api/profile/import_dat` | Import a legacy `.dat` directory tree. |

```bash
curl -s http://localhost:8000/api/profiles
curl -X POST http://localhost:8000/api/profile/load \
     -H 'Content-Type: application/json' -d '{"name": "my-bravo"}'
```

Loading a profile requires the robot to be **disconnected** — the request is
rejected with 409 otherwise. This is deliberate: swapping axis calibration or
teachpoints under a live connection would leave the driver and the instrument
disagreeing about where the head is.

Profile names are validated: no path separators, no `..`, no empty names.
Renaming the active profile also requires disconnecting first, and updates the
`.active_profile` marker.

Several operations save the profile automatically — connecting, selecting a
discovered device, patching the profile, and teaching a location all write the
active profile file. Hand-editing a profile YAML while the server is running
risks having those writes overwrite your changes; load the profile after
editing, or stop the server first.

> [!WARNING]
> Switching profiles switches teachpoints and axis calibration. Loading the
> wrong profile for the instrument in front of you will drive the head to
> coordinates taught on a different machine. Confirm the active profile before
> any motion, and see [safety.md](safety.md).

---

## Recommended setups

### Single machine, no database

The default, and the right choice for one instrument. Nothing to configure
beyond a profile.

- Leave the `PYBRAVO_LABWARE_MONGO_*` variables unset and the `mongo.uri` in
  `config/labware_catalog.yaml` pointing wherever it likes — an unreachable
  host falls back to the local snapshot with a warning. To silence the warning,
  clear `mongo.uri` in that file.
- Labware edits go to `config/labware_editor.yaml`, liquid classes to
  `config/liquid_classes.yaml`. Both are plain YAML you can read, diff, and
  back up.
- A minimal `.env`:

  ```bash
  PYBRAVO_LOG_LEVEL=INFO
  ```

### A lab sharing a labware catalog

Several instruments, one catalog. Run a MongoDB instance reachable from every
host and point them all at it. Each machine keeps its own profile — only the
catalog is shared.

```bash
# .env on every host
PYBRAVO_LABWARE_MONGO_URI=mongodb://labdb.example.internal:27017/
PYBRAVO_LABWARE_MONGO_DB=pybravo
PYBRAVO_LABWARE_MONGO_COLLECTION=labware_types
PYBRAVO_LABWARE_MONGO_CLASS_COLLECTION=labware_classes
```

Notes:

- Liquid classes will follow the same database automatically, because the
  `PYBRAVO_LIQUID_MONGO_*` settings fall back to the labware ones. Set them
  explicitly if you want liquid classes somewhere else.
- Give each instrument a distinct `machine_id` in its profile. Liquid classes
  are keyed by it, so distinct ids keep per-instrument calibration separate
  inside a shared database.
- `config/labware_types.seed.json` can be used to populate an empty collection.
- Every host still keeps a local snapshot, so an instrument keeps running if
  the database goes down.

### Enabling the LLM drafter

Optional, and entirely separable from instrument control.

1. Install the extra dependencies:

   ```bash
   pip install -e '.[llm]'
   # or, with the launcher scripts:
   PYBRAVO_EXTRAS=llm ./scripts/start_pybravo.sh
   ```

2. Add one provider key to `.env`:

   ```bash
   ANTHROPIC_API_KEY=sk-...
   # or
   OPENAI_API_KEY=sk-...
   ```

3. Optionally pin the provider and model, and point drafter persistence at a
   database:

   ```bash
   PYBRAVO_DRAFTER_PROVIDER=anthropic
   PYBRAVO_DRAFTER_MODEL=claude-sonnet-4-6
   PYBRAVO_DRAFTER_MONGO_URI=mongodb://labdb.example.internal:27017/
   PYBRAVO_DRAFTER_MONGO_DB=pybravo_drafter
   ```

   Without a MongoDB URI the drafter writes JSONL files under
   `~/.pybravo/drafter_data/`, which is fine for a single machine.

4. For PDF ingest, run a `docling-serve` instance and set
   `PYBRAVO_DOCLING_URL` to its base URL.

A drafted workflow is a starting point, not a validated protocol. Review every
step and dry-run it in simulation before running it on an instrument.

---

## See also

- [installation.md](installation.md) — installing OpenBravo and its dependencies
- [quickstart.md](quickstart.md) — first run
- [hardware-setup.md](hardware-setup.md) — what goes inside a profile
- [safety.md](safety.md) — required reading before connecting to hardware
- [user-guide.md](user-guide.md) — the web UI
- [api-reference.md](api-reference.md) — full endpoint documentation
- [architecture.md](architecture.md) — how the pieces fit together
- [troubleshooting.md](troubleshooting.md) — when something is not working
- [faq.md](faq.md) — common questions
