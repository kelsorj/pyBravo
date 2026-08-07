# Contributing to OpenBravo

Thanks for wanting to help. This project keeps working instruments out of
scrapyards, and every fix matters to somebody's lab.

Please read the [Code of Conduct](CODE_OF_CONDUCT.md) before participating.

## Before you start

**This project controls a machine that can hurt people and destroy equipment.**
That shapes how we review changes. Read [docs/safety.md](docs/safety.md) — not
as a formality, but because it explains which parts of the codebase carry real
risk.

If your change touches `pybravo/controllers/`, `pybravo/motion/`,
`pybravo/state_machine/`, or `pybravo/darwin/`, you are working on code that
can crash a head into a deck. Say so in your pull request, and describe how you
tested it.

## Setting up

```bash
git clone https://github.com/kelsorj/OpenBravo.git
```

```bash
cd OpenBravo && ./scripts/start_pybravo.sh
```

The launcher uses [uv](https://docs.astral.sh/uv/) when available, which
installs Python 3.11 and every dependency from `uv.lock` on first run. See
[docs/installation.md](docs/installation.md) for alternatives.

Run the test suite with:

```bash
uv run --extra dev python -m pytest
```

Most tests need no hardware — the transport layer is mocked, so the full
protocol encode/decode path runs in software. Some tests skip when optional
fixtures are absent; skips are normal.

## Making a change

1. **Open an issue first** for anything substantial. For a typo or an obvious
   bug, just send the pull request.
2. **Branch** from `main`.
3. **Write a test.** Bug fixes get a test that fails before the fix. New
   behavior gets a test that describes it.
4. **Run the suite** and make sure you have not broken anything. Note that
   `test_back_left_rectangle_uses_front_right_tipbox_anchor` in
   `tests/test_bravo_init.py` is a known pre-existing failure; it is not yours.
5. **Match the surrounding code.** Read the file you are editing and follow its
   conventions rather than importing your own.
6. **Open a pull request** describing what changed, why, and how you tested it.

## Testing hardware changes

Simulation is the floor, not the ceiling. For anything that alters motion:

- Verify in `simulation` first.
- Then verify on a real instrument with a **clear deck**.
- Then verify in normal use with labware.
- Say in the pull request which instrument generation you tested on. A change
  that is correct for Darwin-generation hardware can be wrong for Agile 7612.

If you cannot test on hardware, say so plainly. A well-described untested change
is reviewable; a change implied to be tested when it was not is dangerous.

`tests/test_agile_srt_homing.py` pins the SRT homing frame sequence byte for
byte. If it fails, you changed homing. That is occasionally correct and usually
a mistake — confirm on hardware before updating the fixture.

## Code conventions

- Python 3.11+, following the style already in the file you are editing.
- Logging uses the stdlib `logging` module and the setup in
  `pybravo/logging_config.py`. Use `logger = logging.getLogger(__name__)` and
  %-style formatting (`logger.info("msg %s", val)`), never f-strings in log
  calls, and never `print()` for operational logging.
- Comments should explain constraints the code cannot express. Don't narrate
  what the next line does.
- `ruff` is available in the `dev` extra.

## Documentation

Docs live in `docs/` as plain markdown and render on GitHub with no build step.
If you change behavior, update the page that describes it.

Two rules specific to this project:

- **Describe formats functionally.** A `.pro` file is "a legacy XML protocol
  file"; a `.reg` import reads "a Windows registry profile export". Referring to
  the Bravo hardware by name is fine; naming other companies' software products
  is not.
- **Never commit instrument data.** No packet captures, no vendor manuals, no
  registry exports from a real machine, and nothing containing a lab's
  calibration values or teachpoints. Those describe someone's specific
  instrument and are not ours to publish. Use synthetic fixtures instead —
  `tests/test_dat_import.py` shows the pattern.

## Reporting bugs

Include your instrument generation and `controller_type`, the OpenBravo version
or commit, what you expected, what happened, and the relevant log output. Debug
logging helps:

```bash
PYBRAVO_LOG_LEVEL=DEBUG ./scripts/start_pybravo.sh
```

For protocol-level problems, `PYBRAVO_PROTOCOL_TRACE=1` adds frame hex dumps.
Scrub anything lab-identifying before pasting logs into a public issue.

**Do not report security vulnerabilities or safety defects in public issues.**
See [SECURITY.md](SECURITY.md).

## Licensing your contribution

OpenBravo is licensed under the [Apache License 2.0](LICENSE). By submitting a
contribution you agree that it is licensed under those same terms, per section 5
of the license. Please only submit code you have the right to contribute — if
you wrote it for an employer, make sure you are allowed to.
