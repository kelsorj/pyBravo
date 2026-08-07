# Security and safety policy

## Reporting a vulnerability

Please report security vulnerabilities privately, not in a public issue.

Use GitHub's private vulnerability reporting on this repository
(**Security → Report a vulnerability**), which opens a channel visible only to
the maintainers.

Please include what the issue is, how to reproduce it, what an attacker could
achieve, and any suggested fix. You should get an acknowledgement within a few
days. This is a small volunteer project, so please be patient with fix
timelines; we will keep you informed and credit you when the fix ships, unless
you would rather stay anonymous.

## Safety defects are security issues here

OpenBravo drives a physical robot. A defect that causes unintended motion —
homing into an occupied deck, ignoring an axis limit, moving without
initialization, mishandling an abort — can injure someone or destroy equipment.

**Report those privately too**, through the same channel. Please do not open a
public issue containing a working reproduction that would crash somebody else's
instrument.

## Threat model

Be realistic about what this software is.

**OpenBravo has no authentication or authorization.** The HTTP API and
WebSocket are unauthenticated. Anyone who can reach the port can move your
robot. By default the server binds to `0.0.0.0:8000`, meaning every interface.

**Run it on a trusted, isolated network.** The intended deployment is an
instrument network segment that only lab staff can reach. Do not expose port
8000 to the internet, and do not put it on a general-purpose corporate network
without restricting access. If you need remote access, put it behind a VPN or an
authenticating reverse proxy — do not rely on the application for access
control.

**The instrument protocols are unauthenticated too.** The Bravo firmware
protocols have no authentication or encryption; anyone with network access to
the instrument can command it directly, with or without this software.

**Some endpoints do more than move the robot.** `POST /api/shutdown` stops the
server process. Endpoints that write profiles, labware definitions, or
workflows modify files on the server host. Workflow Script nodes execute code.
Treat API access as equivalent to shell access on the host.

## Configuration and secrets

- API keys for the optional LLM drafter belong in `.env` or the environment,
  never in committed files. `.env` is gitignored.
- Profiles contain your instrument's calibration and teachpoints. They are
  specific to your machine and generally should not be shared publicly.
- Scrub hostnames, IP addresses, and lab-identifying details from logs before
  attaching them to a public issue.

## Supported versions

This project has not yet cut a stable release. Security fixes land on `main`.
Please confirm a problem still reproduces on current `main` before reporting it.
