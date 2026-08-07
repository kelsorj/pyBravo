<!--
Thanks for contributing. See CONTRIBUTING.md for the full guide.
Security and safety defects go through SECURITY.md, never a public PR.
-->

## What changed and why

<!-- One or two sentences. Link the issue if there is one. -->

## How you tested it

<!-- Be specific. "Ran the suite" is fine for non-motion changes. -->

- [ ] `uv run --extra dev python -m pytest` passes
- [ ] Verified in `simulation`

## Motion-code checklist

Does this touch `pybravo/controllers/`, `pybravo/motion/`,
`pybravo/state_machine/`, or `pybravo/darwin/`?

- [ ] **No** — skip the rest of this section.
- [ ] **Yes** — this change can move a real instrument. Complete the following:

If yes:

- [ ] Which instrument generation did you test on? <!-- e.g. Agile 7612 -->
- [ ] Verified on hardware with a **clear deck**
- [ ] Verified in normal use with labware
- [ ] This change does **not** widen an axis range, raise a current limit, or
      remove a sensor check. *(If it does, say so explicitly here and explain
      why — this is the single most important thing a reviewer needs to know.)*
- [ ] `tests/test_agile_srt_homing.py` still passes, or the fixture change is
      intentional and hardware-verified

If you could not test on hardware, say so plainly. A well-described untested
change is reviewable; a change implied to be tested when it was not is
dangerous.
