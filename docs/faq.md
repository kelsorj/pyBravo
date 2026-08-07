# Frequently asked questions

## About the project

### What is OpenBravo?

An open-source control system for Bravo liquid handling robots: a Python driver,
an HTTP and WebSocket API, and a browser UI for running pipetting protocols. It
supports several instrument generations plus a simulator, so you can build and
test protocols without hardware.

### Why does it exist?

A lot of working Bravo instruments are still in labs that can no longer get
software for them. A functioning robot with no usable control software is scrap.
This project keeps those instruments running. See the dedication in the
[README](../README.md).

### Is it affiliated with Agilent?

No. OpenBravo is an independent project, not affiliated with, endorsed by, or
supported by Agilent Technologies. Product names are used only to identify the
hardware this software works with. See [NOTICE](../NOTICE).

### What license is it under?

Apache License 2.0. You can use it commercially, modify it, and distribute it,
provided you preserve the license and attribution notices. There is no warranty
of any kind — see [LICENSE](../LICENSE).

### Can I use it for regulated or clinical work?

Not as-is. OpenBravo is not a validated instrument control system and is not
certified for clinical, diagnostic, or GxP use. If you operate under a quality
system, validation is your responsibility. Read [Safety](safety.md).

## Getting started

### What do I need to run it?

Python 3.11 or newer. That is all for simulation. See
[Installation](installation.md).

### Can I try it without a robot?

Yes — this is the default. `controller_type: simulation` runs the entire system,
including the UI, the 3D view, and workflow execution, with no hardware
attached. It is also where you should test every new protocol.

### Which instruments are supported?

| Generation | `controller_type` | Connection |
|---|---|---|
| Darwin-generation Bravo | `darwin_native` | TCP 7613 |
| Agile 7612 Bravo | `agile_7612` | TCP 7612 |
| Bravo SRT | `agile_srt` | TCP 7612 |
| Legacy Agile Bravo | `agile` | Serial, or TCP 10000 |
| None | `simulation` | — |

See [Hardware setup](hardware-setup.md).

### How do I find my robot's IP address?

`POST /api/discover_devices` sweeps the network for instruments, or use the
"Find Available Device" control in the UI. Details in
[Hardware setup](hardware-setup.md).

### Why does it warn about MongoDB on startup?

Because MongoDB is optional and you probably do not have it. The labware catalog
can be backed by a shared MongoDB so several machines see the same catalog; when
that is unreachable, the server logs a warning and falls back to a local
snapshot file. Everything works normally. See
[Troubleshooting](troubleshooting.md).

## Using it

### Do I have to home the robot every time?

Yes, after a power cycle. Until an axis is homed, the software does not know
where it is, so positions are meaningless and motion is unsafe. Clear the deck
before homing — see [Safety](safety.md).

### Can I migrate my existing protocols and labware?

Partly. `POST /api/workflow/import` reads legacy XML protocol files, and there
are scripts to import labware definitions and liquid classes from Windows
registry exports (`scripts/import_labware_from_registry.py` and
`scripts/import_liquid_classes_from_registry.py`). Existing instrument profiles
can be imported from `.reg` exports or legacy `.dat` directory trees via
`POST /api/profile/import_reg` and `POST /api/profile/import_dat`.

Treat every import as a draft. Verify teachpoints and simulate before running.

### Can I drive it from my own Python script?

Yes. `pybravo.bravo.Bravo` is the programmatic entry point and is what the web
layer itself uses. Or drive the HTTP API from any language — see the
[API reference](api-reference.md).

### Is there authentication on the API?

No. Anyone who can reach the port can move your robot, and the server binds to
all interfaces by default. Run it on a trusted, isolated network and put it
behind a VPN or authenticating proxy if you need remote access. See
[SECURITY.md](../SECURITY.md).

### What is the LLM drafter, and should I trust it?

An optional feature that drafts a workflow from a text description or a paper
PDF. It requires the `llm` extra and an API key. It produces a **draft**, not a
validated protocol: it can misread volumes, invent steps, or get the deck layout
wrong. Read every generated workflow and simulate it before running it. See
[Workflows](workflows.md).

## Troubleshooting

### It connects but will not move.

Connecting is not initializing. `POST /api/connect` opens the transport;
`POST /api/initialize` prepares the instrument for motion. Then home it. Also
check for an engaged emergency stop or an open safety interlock. See
[Troubleshooting](troubleshooting.md).

### An axis moves the wrong distance.

Almost always `ticks_per_eng_unit` in the profile. If it is wrong by a factor of
ten, a 10 mm move travels 100 mm. Stop and fix the profile before moving again.

### The head crashed into the deck.

Stop and check three things: teachpoints for the location involved, the labware
definition's height, and whether the deck state in software matched reality. All
three are software's model of the world, and nothing senses the physical deck
unless you have the optional camera verification configured.

### Some tests skip when I run the suite.

Normal. Some tests need optional fixtures or hardware and skip when absent. One
test, `test_back_left_rectangle_uses_front_right_tipbox_anchor`, is a known
pre-existing failure in head-mode anchor selection.

## Contributing

### How do I help?

See [CONTRIBUTING.md](../CONTRIBUTING.md). Useful contributions that need no
hardware include documentation, tests, UI work, and labware definitions.

### I have an instrument generation you do not support.

That is a valuable contribution. The abstract interface in
`pybravo/controllers/base.py` is the extension point — see
[Architecture](architecture.md). Open an issue before starting so we can help.

### Can I share my profile or labware definitions?

Labware definitions, yes — those describe commercially available consumables and
help everyone. Profiles, generally no: they contain your specific machine's
calibration and teachpoints, which are meaningless on another instrument and
potentially dangerous if someone loads them. Never commit packet captures or
data exported from a real instrument.
