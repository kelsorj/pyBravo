"""FastAPI web server for the Bravo digital twin.

Provides:
- REST API for commands and state queries
- WebSocket for real-time position/state streaming
- Static file serving for the Three.js frontend
"""

import asyncio
import concurrent.futures
import ipaddress
import json
import logging
import os
import re
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# Load .env (project root) into os.environ before any handler reads a
# key. Shell-exported variables win over .env — dotenv defaults to
# override=False, which is what we want. Quiet no-op if python-dotenv
# isn't installed or there's no .env file, so nothing here is fatal.
try:
    from dotenv import load_dotenv

    _env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    if _env_path.exists():
        load_dotenv(_env_path, override=False)
except ImportError:
    pass

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pybravo import labware_editor
from pybravo import liquid_classes as liquid_classes_store
from pybravo.bravo import Bravo
from pybravo.deck.labware import build_labware_catalog, normalize_labware_definitions
from pybravo.head_mode import suggested_head_mode
from pybravo.logging_config import configure_logging
from pybravo.protocol.errors import BravoError
from pybravo.tips import (
    create_tip_definition,
    delete_tip_definition,
    get_default_tip_id_for_head,
    get_tip_capacity_ul,
    get_tip_definition,
    get_tip_id_for_capacity,
    get_tip_length_mm,
    list_tip_items,
    patch_tip_definition,
    serialize_tip_options_for_head,
)
from pybravo.types import Axis, HeadType, SpeedLevel, safe_home_order
from pybravo.vision_client import VisionServiceClient, VisionServiceError
from pybravo.web.middleware import RequestLoggingMiddleware

logger = logging.getLogger(__name__)

# Build a case-insensitive uppercase → Axis lookup so that 'zg'.upper() == 'ZG'
# correctly resolves to Axis.Zg (whose .name is 'Zg', not 'ZG').
_AXIS_BY_UPPER: dict[str, Axis] = {a.name.upper(): a for a in Axis}


def _parse_axis(name: str) -> Axis:
    """Resolve an axis name case-insensitively (handles 'Zg' / 'zg' / 'ZG')."""
    try:
        return _AXIS_BY_UPPER[name.upper()]
    except KeyError:
        raise HTTPException(status_code=400, detail=f"Unknown axis: {name!r}")


def _parse_speed_level(name: str | None, default: SpeedLevel = SpeedLevel.MED) -> SpeedLevel:
    if not name:
        return default
    try:
        return SpeedLevel[name.strip().upper()]
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=f"Unknown speed: {name}") from exc


def _tip_payload(head_type: HeadType, tip_id_or_capacity: str | float | None) -> dict[str, Any]:
    tip = get_tip_definition(head_type, tip_id_or_capacity)
    return {
        "tip_id": None if tip is None else tip.tip_id,
        "capacity_ul": None if tip_id_or_capacity is None else float(get_tip_capacity_ul(head_type, tip_id_or_capacity)),
        "label": None if tip is None else tip.label,
        "length_mm": None if tip is None else tip.length_mm,
        "source": None if tip is None else tip.source,
        "model_3d": None if tip is None else tip.model_3d,
    }

_OPENAPI_TAGS = [
    {"name": "Connection", "description": "Robot connection, initialization, and task control."},
    {"name": "Motion", "description": "Axis motion, deck movement, pipetting, and gripper actions."},
    {"name": "Teachpoints", "description": "Teachpoint read/write operations for deck locations."},
    {"name": "State", "description": "Robot state, I/O, and runtime status queries."},
    {"name": "Deck", "description": "Deck labware assignment and location state."},
    {"name": "Head", "description": "Head mode, tip selection, and head changes."},
    {"name": "Profiles", "description": "Profile listing, loading, and updates."},
    {"name": "Labware", "description": "Labware catalog, classes, and labware editor assets."},
    {"name": "Vision", "description": "Distance camera, calibration, preview, and deck verification endpoints."},
    {"name": "Discovery", "description": "Network device discovery and selection."},
]


def _route_meta(tag: str, summary: str, description: str | None = None, **kwargs: Any) -> dict[str, Any]:
    meta: dict[str, Any] = {"tags": [tag], "summary": summary}
    if description:
        meta["description"] = description
    meta.update(kwargs)
    return meta


CONNECT_DOC = """
Opens a connection to the configured Bravo. This does NOT initialize the instrument.

What this operation does:
- Opens the configured controller transport for simulation, Agile, or Darwin.
- Stores the selected address or port back into the active profile when possible.
- Performs connection-level setup only: per-axis parameter access, clearing stale
  instruction tables, and caching each axis's peak-current reference.

What it does NOT do:
- No ping, firmware check, or controller-identity verification.
- No safety-interlock check, motor-power-fault clear, or fault reset.
- No head or gripper detection, and no check for a plate left in the gripper.
- No homing. Every axis position is unknown until the instrument is initialized.

Manual alignment:
- This is the API equivalent of selecting a device and opening communications
  with it. The separate **Initializing the device** workflow is
  POST /api/initialize.

When to use it:
- Call this first, then POST /api/initialize before commanding any motion.
- After a connection drops, reconnecting alone is not enough — an instrument that
  was power-cycled needs initializing again.
""".strip()

INITIALIZE_DOC = """
Runs the Bravo device initialization sequence using the active profile.

What this operation does:
- Connects first if no active controller session exists.
- Verifies the controller can communicate.
- Performs the standard initialization task sequence used by PyBravo for real hardware or simulation.

Manual alignment:
- This is the API equivalent of the manual's **Initializing the device** workflow.
- It prepares the robot for later motion, teachpoint work, tip handling, and diagnostics-style tasks.

Notes:
- Initialization behavior depends on the active profile, including connection type, head settings, and safety preferences.
- If no Bravo address is configured for an Ethernet controller, the request is rejected.
""".strip()

TEACHPOINT_DOC = """
Teachpoints define the reference X, Y, and Z coordinates for each deck location.

What this endpoint family is for:
- Reading the current taught coordinates for a location.
- Writing explicit coordinates into the teachpoint store.
- Capturing the robot's current position as the teachpoint for a location.

Manual alignment:
- This maps directly to the manual's **Setting teachpoints** workflow.
- In Bravo usage, later commands such as Move, Approach, Tips On, Pick and Place, Aspirate, and Dispense all depend on correctly taught deck positions.

Important behavior:
- The `teach_current` form uses the live robot position and stores it as the new location reference.
- The stored values become the base coordinates used by task-level APIs.
""".strip()

MOVE_LOCATION_DOC = """
Moves the head to a taught deck location using the teachpoint reference for that location.

What this operation does:
- Uses the saved X and Y coordinates for the location.
- Uses the saved Z coordinate as the full taught position.
- Can optionally stop above the deck by applying an approach height rather than going all the way to the taught Z.

Manual alignment:
- This matches the diagnostics and command concepts around **Move** and **Approach**.
- It is the API equivalent of sending the head to a known taught location during setup, troubleshooting, or single-step task execution.

Typical use cases:
- Verifying that teachpoints are correct.
- Moving safely over a plate or tip box before a more specific task.
- Driving UI navigation actions that need the head over a selected deck position.

Prerequisites:
- The location must already have a valid teachpoint.
- The robot should be initialized and homed.
- The active profile should contain the correct safe-Z and speed behavior for the installed hardware.

Sequence:
1. Retract Z to safe height.
2. Move X and Y to the taught location unless `only_move_z` is requested.
3. Lower Z either to the taught Z or to an approach height above the taught Z.

Common failure cases:
- Missing or incorrect teachpoints.
- Safety interlock or disabled robot state.
- Profile/controller mismatch that makes the move invalid for the installed machine.
""".strip()

SAFE_Z_DOC = """
Retracts the liquid-handling Z axis to the configured safe-Z position.

What this operation does:
- Moves only the Z axis.
- Uses the configured safe clearance from the active profile.
- Provides vertical clearance before XY travel over deck labware or accessories.

Manual alignment:
- This reflects the same safe-clearance behavior operators expect before jogging or moving across deck locations.
- In practical Bravo use, safe-Z is a protection step that reduces the chance of striking labware, tips, lids, or accessory hardware.
""".strip()

ASPIRATE_DOC = """
Runs an aspirate task at the specified taught location.

What this operation does:
- Moves to the selected location using teachpoint geometry.
- Lowers the head toward the working depth.
- Aspirates the requested volume using the current head and profile settings.

Manual alignment:
- This is the API equivalent of running an individual liquid-handling task rather than a full protocol.
- It aligns with the description that tasks can be run individually from diagnostics-style tooling.

Important assumptions:
- The location must already be taught.
- The selected head, tip state, and labware geometry must be compatible with the requested operation.

Prerequisites:
- The robot should be initialized and positioned with valid teachpoints.
- Compatible tips and head configuration must already be on the instrument.
- The location should represent the intended source well or container position.

Sequence:
1. Retract to safe height.
2. Move to the taught location.
3. Lower to the working depth derived from the request parameters.
4. Aspirate the requested volume.
5. Retract to a safe position after the liquid-handling step.

Common failure cases:
- Aspirating at a location with invalid teachpoint geometry.
- Missing tips or incompatible head configuration.
- Volumes or liquid-handling settings that do not match the selected setup.
""".strip()

DISPENSE_DOC = """
Runs a dispense task at the specified taught location.

What this operation does:
- Moves to the location using its teachpoint.
- Lowers toward the dispense depth.
- Dispenses the requested volume and completes the task under profile-controlled motion settings.

Manual alignment:
- This is the companion to aspirate for single-task execution.
- It corresponds to performing a fluid-transfer action directly, outside of a larger style protocol run.

Prerequisites:
- The robot should already hold the required liquid volume.
- The destination location must be taught correctly.
- The current head, tip, and deck configuration must be compatible with the destination labware.

Sequence:
1. Retract to safe height.
2. Move to the taught destination.
3. Lower to the requested dispense depth.
4. Dispense the requested volume.
5. Retract after the dispense completes.

Common failure cases:
- Incorrect teachpoint depth causing the dispense position to be wrong.
- Missing tips or incompatible head/labware configuration.
- Requested volume or optional settings not matching the current liquid-handling setup.
""".strip()

TIPS_ON_DOC = """
Runs the disposable-tip pickup task for the specified tip-box location.

What this operation does:
- Uses the active head mode to determine how many tips should be picked up.
- Uses the active tip-selection anchor when partial pickup is enabled.
- Moves to the taught tip-box location and performs the pickup motion.

Manual alignment:
- This maps to the manual's disposable-tip head behavior and to the Diagnostics concept of running **Tips On** as an individual task.
- It also reflects the model in which head mode and well accessibility determine which tips can be acquired.

Important behavior:
- Tip-box geometry and compatibility matter.
- Partial pickup depends on the selected subset configuration and legal anchor position.

Prerequisites:
- The selected location must contain compatible tip-box labware.
- The location teachpoint must be valid.
- The active head mode and tip selection must be legal for the tip box and installed head.

Sequence:
1. Retract to safe height.
2. Move to the tip-box location using the taught XY position and any head-mode offset.
3. Lower the head onto the selected tip pattern.
4. Complete the pickup motion and record the resulting tip state on the head.
5. Retract from the tip box.

Common failure cases:
- Tip box missing or wrong labware type at the location.
- Illegal anchor position for partial pickup.
- Head mode incompatible with the available rows, columns, or selected tips.
""".strip()

TIPS_OFF_DOC = """
Runs the disposable-tip unload or return task for the specified location.

What this operation does:
- Uses the stored on-head tip-selection state when tips are currently mounted.
- Moves to the selected return or discard location.
- Performs the unload sequence using the active head/tip context.

Manual alignment:
- This is the API equivalent of running **Tips Off** as an individual task from diagnostics-style controls.
- It follows the same Bravo model that tip return must respect accessible wells and current tip occupancy.

Prerequisites:
- Tips must currently be on the head.
- The destination location must be valid for the return or discard operation.
- Teachpoints and head-mode state must still match the tip pattern that is currently mounted.

Sequence:
1. Retract to safe height.
2. Move to the requested return or discard location.
3. Lower into the return/drop-off position.
4. Release or return the active tip pattern.
5. Update the stored on-head tip state and retract.

Common failure cases:
- No tips present on the head.
- Returning to a tip box location that cannot legally accept the active tip pattern.
- Invalid or stale teachpoint data for the target location.
""".strip()

PICK_PLACE_DOC = """
Runs a full gripper-based pick-and-place operation between two deck locations.

What this operation does:
- Retracts to a safe starting condition.
- Opens and positions the gripper.
- Moves to the source location, grips the labware, lifts to a carry height, travels to the destination, and releases the labware.
- Uses taught deck coordinates plus labware handling geometry such as stack height and gripper offset.

Manual alignment:
- This is the API equivalent of a manual gripper task sequence described in the diagnostics and gripper-control sections.
- It matches the Bravo expectation that plate handling depends on correct teachpoints, gripper setup, and deck geometry.

Why this is higher level than raw moves:
- The endpoint solves intermediate carry poses and safe transitions automatically.
- It returns diagnostic information about the solved pick, carry, and place positions.

Prerequisites:
- Source and destination locations must be taught correctly.
- Source location must contain labware that can be handled by the gripper.
- Gripper setup and offsets must be correct for the installed hardware and active profile.

Sequence:
1. Retract to a safe starting condition.
2. Open and nest the gripper as needed.
3. Move to the source location.
4. Lower to the computed pick pose and grip the labware.
5. Move to a carry height that clears nearby deck obstacles.
6. Travel to the destination location.
7. Lower to the computed place pose and release the labware.
8. Return the gripper to its nesting position.

Common failure cases:
- No labware present at the source location.
- Incorrect teachpoints or gripper offsets causing poor pickup alignment.
- Deck geometry, stack height, or obstacle clearance not matching the physical setup.
""".strip()

LABWARE_DOC = """
Labware definitions describe the type and geometry of the containers and consumables that Bravo devices handle during runs.

What this area of the API is for:
- Listing the runtime labware catalog used by PyBravo.
- Creating and editing labware entry definitions.
- Managing labware classes and associated 2D or 3D assets.

Manual alignment:
- This mirrors the manual's concept of **Define labware** before attempting automated tasks.
- Geometry, well layout, and handling properties drive deck rendering, accessibility checks, and motion planning.
""".strip()

PROFILE_DOC = """
Profiles store the robot configuration used by PyBravo for connection, head setup, tip options, and safety behavior.

What this area of the API is for:
- Listing saved profiles.
- Reading the active profile.
- Updating the active profile.
- Loading a different profile from disk.

Manual alignment:
- This corresponds to the setup workflow for **Creating and managing profiles**.
- Profiles are the persistent configuration layer that determines how later task and motion operations behave.
""".strip()

DISCOVERY_DOC = """
Discovers Bravo devices on the local network and lets the selected device be stored into the active profile.

What this area of the API is for:
- Scanning adapters and Bravo-relevant subnets.
- Using the UDP discovery handshake when available.
- Falling back to TCP probing when broadcast discovery does not return a device.
- Storing the chosen device address for later connect and initialize requests.

Manual alignment:
- This supports the same practical setup step as creating or adding a Bravo device before operation.
- It is especially useful in multi-device or newly provisioned environments where the Bravo address is not yet fixed in the profile.
""".strip()

HEAD_MODE_DOC = """
Head mode controls which subset of a disposable-tip head is considered active for later tip pickup, return, and liquid-handling operations.

What this endpoint family is for:
- Reading the currently active subset configuration.
- Setting row, column, rectangle, or full-head usage modes.
- Suggesting a starting mode based on the installed head and assigned labware.

Manual alignment:
- This extends the concept that disposable-tip heads can operate on all wells or on selected rows, columns, or smaller subsets depending on the task.
- It is especially relevant for partial tip pickup and serial-dilution style workflows where only part of the head should be active.

Important behavior:
- The chosen subset changes how XY offsets and accessible wells are calculated.
- The active mode is also used when validating tip selection and tip-box access.
""".strip()

TIP_SELECTION_DOC = """
Tip selection tracks the anchor well used for partial tip pickup and return.

What this endpoint family is for:
- Inspecting the currently selected tip anchor.
- Updating the anchor row and column for later tip tasks.

Manual alignment:
- This is the API expression of the operator decision about which subset of a tip box should line up with the active subset of the head.
- It matters whenever the full head is not being used.

Important behavior:
- The selection is validated against head mode, tip-box geometry, and accessibility rules.
- The same selection state is reused by later `Tips On` and `Tips Off` operations.
""".strip()

GRIPPER_DOC = """
These operations expose the gripper controls used for plate-handling diagnostics and gripper-based tasks.

What this endpoint family is for:
- Opening the gripper.
- Closing the gripper.
- Docking or recessing the gripper Z axis.

Manual alignment:
- This matches the diagnostics and gripper setup sections of the Bravo manual, where operators verify gripper behavior before relying on automated plate moves.

Important behavior:
- These are low-level plate-handling controls.
- Higher-level plate transfer behavior is implemented by the `pick_place` task endpoint.
""".strip()

EXECUTE_COMMAND_DOC = """
Executes a named high-level task through a single generic endpoint.

What this operation does:
- Dispatches to one of the supported task APIs such as aspirate, dispense, mix, tips on, tips off, stack plates, delid plate, or scan stack height.
- Lets clients issue one command shape while still using the underlying task engine.

Manual alignment:
- This is close to running an individual task from a diagnostics or command dialog rather than from a full protocol definition.

Important behavior:
- The `command` field determines which task is run.
- Optional liquid-class and pipette-technique parameters are forwarded when the selected task supports them.
""".strip()

LIQUID_CONTEXT_DOC = """
Returns the active liquid-handling context that PyBravo uses to resolve liquid classes.

What this operation does:
- Reports the active machine identifier, installed head type, active tip definition, and active tip capacity.
- Provides the context key used when filtering or selecting liquid classes.

Manual alignment:
- This reflects the Bravo reality that liquid-handling behavior depends on the machine, head, and tip combination in use.
- In terms of the model, liquid-class compatibility is not global; it depends on the physical configuration.

Typical use cases:
- Populating editor defaults.
- Explaining why a given liquid class appears or does not appear in the UI.
- Debugging context-sensitive aspirate and dispense behavior.
""".strip()

TIPS_CATALOG_DOC = """
Tip definitions describe the disposable tip consumables available to the system.

What this area of the API is for:
- Listing tips available for a head type.
- Creating editable tip definitions.
- Updating or deleting custom tip definitions.

Manual alignment:
- The Bravo manual ties head behavior and compatibility closely to the installed tip type and volume.
- This API stores the corresponding software definitions used by PyBravo for tip length, capacity, compatibility, and optional visualization.

Important behavior:
- Tip definitions affect active liquid context, teach-tip calculations, compatibility checks, and liquid-class resolution.
""".strip()

LIQUID_CLASSES_DOC = """
Liquid classes define how aspirate and dispense behavior should be tuned for a given machine, head, and tip context.

What this area of the API is for:
- Listing liquid classes that match the current or requested context.
- Creating, updating, and deleting liquid class definitions.
- Managing aspirate, dispense, and equation fields that shape liquid-handling behavior.

Manual alignment:
- This corresponds to the idea that liquid handling depends on the physical setup and on liquid-specific behavior settings rather than only on raw volume.
- It complements the manual's discussion of speed, accuracy, and task behavior by making those settings explicit and reusable in software.

Selection behavior:
- The list endpoint first tries the active machine/head/tip context.
- If no match is found, it falls back through broader scopes until a usable class list is found.
""".strip()

PIPETTE_TECHNIQUES_DOC = """
Pipette techniques are reusable motion patterns layered onto aspirate and dispense operations.

What this area of the API is for:
- Listing saved techniques.
- Creating new techniques.
- Updating or deleting existing techniques.

Manual alignment:
- These techniques capture the kind of repeatable motion choices operators often associate with specialized liquid-handling behaviors, such as swirling or segmented movement.
- They provide a reusable configuration layer above the core task APIs.

Typical use cases:
- Defining a reusable swirl or circular motion.
- Reusing a technique across many aspirate or dispense calls.
- Editing motion parameters without changing task code.
""".strip()

DECK_DOC = """
Deck assignment endpoints control which labware is currently present at each Bravo location.

What this endpoint family is for:
- Assigning a labware definition to a deck location.
- Clearing a previously assigned location.
- Recording tip-box-specific metadata such as tip definition and fill state when relevant.

Manual alignment:
- This is the software equivalent of physically placing labware on the deck and making sure the software knows what is present before a run or diagnostic task.

Important behavior:
- Motion planning, visualization, compatibility checks, and task logic depend on accurate deck assignment.
- Tip boxes can carry additional metadata because tip pickup depends on both geometry and tip definition.
""".strip()

VISION_DOC = """
These endpoints expose the local distance-camera integration used to inspect the physical deck and support future camera-guided setup checks.

What this area of the API is for:
- Checking whether the local vision service is reachable.
- Reporting camera SDK availability and calibration state.
- Starting or scaffolding camera calibration for the active machine.
- Comparing the expected PyBravo deck scene against the physical setup seen by the camera.
- Fetching preview information used for calibration and diagnostics.

Manual alignment:
- This is closest to the setup and diagnostics mindset in the Bravo manual, where the operator verifies that the physical deck, accessories, and handling geometry match the configured software state.
- The camera integration extends that idea with an external distance sensor so deck verification can be automated instead of relying only on visual inspection.

Important behavior:
- These endpoints depend on the vision feature being enabled in the active profile.
- They proxy to the local vision service rather than talking to the camera directly from the main API process.
- The current implementation supports calibration scaffolding and verification reporting even when live camera hardware is not yet fully available.
""".strip()

VISION_STATUS_DOC = """
Returns the overall status of the local vision stack.

What this operation does:
- Checks connectivity to the local vision service.
- Reports whether the camera SDK can be imported.
- Returns the currently saved calibration artifact, if one exists.

Typical use cases:
- Confirming that distance-camera support is configured correctly on the host.
- Determining whether camera-related UI features should be enabled.
- Troubleshooting SDK or local service installation problems.
""".strip()

VISION_CALIBRATION_DOC = """
Returns the saved camera-to-deck calibration artifact used by the local vision service.

What this operation does:
- Loads the currently saved calibration payload for the active machine context.
- Reports whether calibration has been completed or whether only a scaffold exists.

Manual alignment:
- This serves the same operational purpose as checking whether a setup/calibration procedure has been completed before relying on an accessory or sensor-driven workflow.
""".strip()

VISION_CALIBRATION_RUN_DOC = """
Starts the guided calibration workflow, or when hardware is not yet ready, creates a calibration scaffold that can be completed later.

What this operation does:
- Sends the active machine identifier to the local vision service.
- Creates or updates a calibration record describing the intended camera mount and solve mode.
- Persists a scaffold so later capture/solve steps have a machine-specific starting point.

Manual alignment:
- This is analogous to the kind of guided setup and verification procedures described in the Bravo setup chapters, but for the external distance camera rather than for teachpoints or gripper setup.

Prerequisites:
- Vision must be enabled in the active profile.
- The local vision service must be reachable.

Typical use cases:
- First-time setup of the distance camera on a machine.
- Resetting or re-running calibration after the camera mount changes.
- Creating a placeholder calibration record before full hardware capture is available.
""".strip()

VISION_VERIFY_DOC = """
Verifies the physical deck setup against PyBravo's expected software model using the local distance-camera service.

What this operation does:
- Builds the expected scene from current deck assignments, teachpoints, head type, and tip-box occupancy.
- Sends that scene to the vision service.
- Returns a slot-by-slot report describing expected labware, observed status, and confidence.

Manual alignment:
- This directly supports the operator goal of confirming that the physical deck matches the configured software state before running tasks.
- It is the camera-assisted equivalent of manually verifying plates, tip boxes, and setup details during diagnostics or pre-run checks.

Prerequisites:
- Vision must be enabled in the active profile.
- Deck assignments and teachpoints should already reflect the intended physical setup.
- Calibration should exist if geometric verification is expected to be meaningful.

Current limitations:
- The reporting pipeline is designed for the Femto Bolt integration, but some verification states may still return review-oriented placeholders until the live camera path is fully complete.
""".strip()

VISION_PREVIEW_DOC = """
Returns the latest preview response from the local vision service for calibration and diagnostics.

What this operation does:
- Requests the latest preview payload from the vision service.
- Exposes the most recent camera-side preview state to the UI or debugging tools.

Typical use cases:
- Checking that the distance camera is producing a usable view.
- Supporting calibration workflows.
- Debugging camera placement and service connectivity.

Notes:
- If live preview is not yet available, the vision service may return a not-implemented or placeholder response.
""".strip()

VISION_ROI_CALIBRATION_START_DOC = """
Launches the interactive ROI calibration tool against the latest saved reference image.

What this operation does:
- Reads the current reference image path from the saved vision calibration scaffold.
- Launches `python -m pybravo.vision.calibrate_rois <image_path>` in a separate window.
- Used after snapshot capture so the operator can click the 4 corners for all 9 deck positions.
""".strip()

VISION_SERVICE_START_DOC = """
Starts the local vision service in a separate process using the repository's Windows launcher script.

What this operation does:
- Launches `python -m pybravo.vision_service` in a separate console window.
- Passes the active profile's vision settings as environment variables.
- Returns immediately so the UI can poll for readiness.

Prerequisites:
- Vision must be enabled in the active profile.
- The host should be running Windows when using this helper.
""".strip()


app = FastAPI(
    title="PyBravo API",
    version="0.1.0",
    description=(
        "Backend API for PyBravo robot control, labware editing, deck configuration, "
        "profile management, and device discovery."
    ),
    openapi_tags=_OPENAPI_TAGS,
)

app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RuntimeError)
async def runtime_error_handler(request: Request, exc: RuntimeError) -> JSONResponse:
    """Return a 400 JSON response for RuntimeErrors (e.g. 'Not connected')."""
    return JSONResponse(status_code=400, content={"error": str(exc)})


@app.exception_handler(BravoError)
async def bravo_error_handler(request: Request, exc: BravoError) -> JSONResponse:
    """Return structured JSON for hardware/protocol failures."""
    return JSONResponse(
        status_code=400,
        content={
            "error": str(exc),
            "error_type": exc.error_type.name,
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Keep API failures JSON-shaped so the frontend can render them cleanly."""
    logger.exception("Unhandled API error on %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse(status_code=500, content={"error": "Internal server error"})


_bravo: Bravo | None = None
_profile_path: Path | None = None
_profile_dir: Path | None = None
_labware_assets_mounted = False


def get_bravo() -> Bravo:
    if _bravo is None:
        raise RuntimeError("Bravo not initialized")
    return _bravo


def _refresh_runtime_labware_catalog() -> None:
    if _bravo is not None:
        _bravo._labware_catalog = build_labware_catalog()


def _labware_dashboard_source() -> str:
    source_path = Path(__file__).resolve().parents[2] / "frontend" / "src" / "LabwareDashboard.jsx"
    source = source_path.read_text(encoding="utf-8")
    rewritten_imports = """import React, { useEffect, useMemo, useState, Suspense } from 'react';\nimport { createRoot } from 'react-dom/client';\nimport { Canvas, useLoader } from '@react-three/fiber';\nimport { OrbitControls, Html, Bounds, useGLTF } from '@react-three/drei';\nimport * as THREE from 'three';\nimport { STLLoader } from 'three/examples/jsm/loaders/STLLoader';\nimport { OBJLoader } from 'three/examples/jsm/loaders/OBJLoader';\n"""
    original_imports = """import { useEffect, useMemo, useState, Suspense } from 'react'\nimport { Canvas, useLoader } from '@react-three/fiber'\nimport { OrbitControls, Html, Bounds, useGLTF } from '@react-three/drei'\nimport * as THREE from 'three'\nimport { STLLoader } from 'three/examples/jsm/loaders/STLLoader'\nimport { OBJLoader } from 'three/examples/jsm/loaders/OBJLoader'\n"""
    source = source.replace(original_imports, rewritten_imports)
    source = source.replace(
        "const DEFAULT_API_URL = `${window.location.protocol}//${window.location.hostname}:8091`\nconst API_URL = import.meta.env.VITE_API_URL || DEFAULT_API_URL",
        "const API_URL = window.location.origin",
    )
    source = source.replace("export default LabwareDashboard", "")
    source += "\nconst root = createRoot(document.getElementById('root'))\nroot.render(<LabwareDashboard />)\n"
    return source


# -- Pydantic models --

class ConnectRequest(BaseModel):
    controller_type: str | None = None  # None = use stored profile setting
    address: str | None = None
    serial_port: str | None = None


class LabwareTypeRequest(BaseModel):
    kind: str | None = None
    name: str | None = None
    vendor: str | None = None
    catalog_number: str | None = None
    description: str | None = None
    base_class: str | None = None
    wells: int | None = None
    plate_dimensions_mm: dict[str, Any] | None = None
    plate_properties: dict[str, Any] | None = None
    well_dimensions_mm: dict[str, Any] | None = None
    pf400: dict[str, Any] | None = None
    planar_motor: dict[str, Any] | None = None
    labware_class_ids: list[str] | None = None
    tip_definition_id: str | None = None
    supported_tip_ids: list[str] | None = None


class LabwareClassRequest(BaseModel):
    name: str | None = None
    description: str | None = None

class CommandRequest(BaseModel):
    name: str
    params: dict[str, Any] = {}

class MoveRequest(BaseModel):
    axis: str
    position: float
    velocity: float = 0.0
    acceleration: float = 0.0

class JogRequest(BaseModel):
    axis: str
    step: float
    direction: int = 1
    speed: str | None = None
    peak_current: float | None = None  # if set, use force-limited jog

class AxisRequest(BaseModel):
    axis: str | None = None
    # Optional multi-axis form. Prefer this over issuing one request per axis:
    # the server orders them so the head and gripper lift before the gantry
    # moves. A client that sequences axes itself bypasses that guarantee.
    axes: list[str] | None = None

class GripperTeachMoveRequest(BaseModel):
    location: int
    approach_height: float = 0.0
    speed: str | None = None


class GripperTeachRequest(BaseModel):
    location: int


class PickPlaceRequest(BaseModel):
    from_location: int
    to_location: int
    speed: str | None = None

class MoveToLocationRequest(BaseModel):
    location: int
    approach_height: float = 0.0
    only_move_z: bool = False
    speed: str | None = None

class SpeedRequest(BaseModel):
    speed: str | None = None

class DeckLabwareRequest(BaseModel):
    labware_id: str
    is_lidded: bool = False
    is_sealed: bool = False
    tip_definition_id: str | None = None
    tipbox_fill_state: str | None = None

class TeachpointSetRequest(BaseModel):
    x: float
    y: float
    z: float

class TeachCurrentRequest(BaseModel):
    tip_capacity: float | None = None
    tip_id: str | None = None

class ProfileUpdateRequest(BaseModel):
    approach_height: float | None = None
    z_safe_position: float | None = None
    prompt_home_w: bool | None = None
    run_medium_speed: bool | None = None
    always_safe_z: bool | None = None
    ignore_plate_sensor: bool | None = None
    enable_tips_off_tip_touch: bool | None = None
    is_srt: bool | None = None
    controller_type: str | None = None
    use_ethernet: bool | None = None
    serial_port: str | None = None
    address: str | None = None
    machine_id: str | None = None
    head_type: str | None = None
    check_on_init: bool | None = None
    teach_tip_capacity: float | None = None
    teach_tip_id: str | None = None
    vision_enabled: bool | None = None
    vision_service_url: str | None = None
    vision_sdk_root: str | None = None
    accessories: dict[str, Any] | None = None
    barcode_reader_enabled: bool | None = None
    barcode_reader_device_type: str | None = None
    barcode_reader_port: str | None = None
    barcode_reader_side: str | None = None
    barcode_reader_location: int | None = None

class ExecuteCommandRequest(BaseModel):
    command: str
    location: int = 1
    source_location: int | None = None
    base_location: int | None = None
    destination_location: int | None = None
    lid_location: int | None = None
    plate_location: int | None = None
    lid_destination: int | None = None
    manual_count: int | None = None
    volume: float = 0.0
    pre_aspirate: float = 0.0
    post_aspirate: float = 0.0
    distance_from_bottom: float = 2.0
    aspirate_distance: float | None = None
    dispense_distance: float | None = None
    dispense_at_different_distance: bool = False
    blowout: float = 0.0
    empty_tips: bool = False
    mix_cycles: int = 3
    dynamic_tip_extension: float = 0.0
    dynamic_tip_retraction: float = 0.0
    tip_touch: bool = False
    liquid_class: str | None = None
    pipette_technique: str | None = None


class TeleshakeActionRequest(BaseModel):
    rpm: int | None = None
    direction: str | None = None


class LiquidClassRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    machine_id: str | None = None
    head_type: str | None = None
    tip_id: str | None = None
    tip_capacity_ul: float | None = None
    aspirate: dict[str, Any] | None = None
    dispense: dict[str, Any] | None = None
    equation: dict[str, Any] | None = None


class TipDefinitionRequest(BaseModel):
    tip_id: str | None = None
    label: str | None = None
    capacity_ul: float | None = None
    length_mm: float | None = None
    source: str | None = None
    model_3d: str | None = None
    compatible_heads: list[str] | None = None


class PipetteTechniqueRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    motion_type: str | None = None
    radius_mm: float | None = None
    segments: int | None = None
    clockwise: bool | None = None
    apply_on_aspirate: bool | None = None
    apply_on_dispense: bool | None = None
    z_phase: str | None = None

class ChangeHeadRequest(BaseModel):
    head_type: str


class HeadModeRequest(BaseModel):
    subset_type: str | None = None
    subset_config: str | None = None
    row_count: int | None = None
    column_count: int | None = None


class TipSelectionRequest(BaseModel):
    location: int
    row: int
    col: int


class PlateSelectionRequest(BaseModel):
    location: int
    row: int
    col: int


class VisionCalibrationRunRequest(BaseModel):
    notes: str | None = None

class DiscoverDevicesRequest(BaseModel):
    adapter: str = "All interfaces"
    controller_type: str | None = None

class SelectDeviceRequest(BaseModel):
    device_id: str = ""
    ip_address: str = ""
    controller_type: str | None = None


class ProfileLoadRequest(BaseModel):
    name: str


class ProfileDuplicateRequest(BaseModel):
    """Copy ``source`` profile to ``new_name``. If ``source`` is omitted,
    duplicates the currently-active profile."""
    new_name: str
    source: str | None = None


class ProfileRenameRequest(BaseModel):
    """Rename ``old_name`` profile to ``new_name``. If the renamed profile
    was the active one, the .active_profile marker is updated."""
    old_name: str
    new_name: str


class ProfileImportRegRequest(BaseModel):
    """Import a legacy Bravo ``.reg`` profile export. ``content`` is the
    raw .reg payload (already decoded to a string by the client). If
    ``save_as`` is provided, the parsed profile is written to
    ``<profile_dir>/<save_as>.yaml`` (rejects conflicts with 409). If
    ``save_as`` is omitted, the parsed profile is returned for preview."""
    content: str
    save_as: str | None = None
    overwrite: bool = False


class ProfileImportDatFile(BaseModel):
    """One ``.dat`` file from a legacy Bravo2 profile directory tree. The
    ``relative_path`` is forward-slash (``"96LT/Axes/X/X.dat"``) and
    ``content`` is the file's decoded text."""
    relative_path: str
    content: str


class ProfileImportDatRequest(BaseModel):
    """Import a legacy Bravo2 ``.dat`` directory tree. ``profile_name`` is the
    top-level folder name (also the parsed profile name). ``files`` is the
    full set of ``.dat`` files discovered under it. Preview/save semantics
    match :class:`ProfileImportRegRequest`."""
    profile_name: str
    files: list[ProfileImportDatFile]
    save_as: str | None = None
    overwrite: bool = False


# -- REST endpoints: Connection & Core --

@app.post("/api/connect", **_route_meta("Connection", "Connect to the configured robot", CONNECT_DOC))
async def connect(req: ConnectRequest):
    global _profile_path
    bravo = get_bravo()
    # Use stored profile values as defaults when fields are omitted
    ctrl = req.controller_type if req.controller_type is not None else bravo._profile.connection.controller_type
    addr = req.address if req.address is not None else bravo._profile.connection.address
    port = req.serial_port if req.serial_port is not None else bravo._profile.connection.serial_port

    if ctrl in {"agile", "agile_7612", "agile_srt", "darwin_native"} and not (addr or "").strip():
        raise RuntimeError(
            "No Bravo IP address is configured. Use 'Find Available Device' and select the device first."
        )
    if ctrl == "darwin_serial" and not (port or "").strip():
        raise RuntimeError("No serial port is configured.")

    if bravo.is_connected:
        bravo.disconnect()
    bravo._profile.connection.controller_type = ctrl
    bravo._profile.connection.address = addr
    bravo._profile.connection.serial_port = port
    try:
        bravo.connect()
    except OSError as exc:
        raise RuntimeError(f"Could not connect to {addr or port}: {exc}") from exc

    if _profile_path is not None:
        try:
            bravo.profile.save(_profile_path)
        except OSError as exc:
            logger.warning("Failed to save profile to %s after connect: %s", _profile_path, exc)

    return {"status": "connected", "controller": ctrl}

@app.post("/api/disconnect", **_route_meta("Connection", "Disconnect from the current robot", "Closes the active controller connection and leaves the server in a disconnected state."))
async def disconnect():
    get_bravo().disconnect()
    return {"status": "disconnected"}


@app.post("/api/shutdown", **_route_meta("Connection", "Shut down the server process", "Hard-kills the Python process. Intended for the UI 'Quit' button — bypasses the graceful-shutdown path which can hang for 30+ seconds on stale Darwin bridge pollers."))
async def shutdown():
    # Best-effort disconnect so the controller gets a clean teardown before
    # we hard-exit. Any exceptions are swallowed — we are leaving anyway.
    try:
        get_bravo().disconnect()
    except Exception:
        pass

    # Delay os._exit until after the response flushes to the client so the
    # browser sees a 200 and the confirmation toast, then the socket dies.
    async def _exit_soon() -> None:
        await asyncio.sleep(0.15)
        os._exit(0)

    asyncio.create_task(_exit_soon())
    return {"status": "shutting_down"}

@app.post("/api/initialize", **_route_meta("Connection", "Initialize the robot using the current profile", INITIALIZE_DOC))
async def initialize():
    bravo = get_bravo()
    if not bravo.is_connected:
        cfg = bravo._profile.connection
        ctrl = cfg.controller_type
        addr = cfg.address
        port = cfg.serial_port
        if ctrl in {"agile", "agile_7612", "agile_srt", "darwin_native"} and not (addr or "").strip():
            raise RuntimeError(
                "No Bravo IP address is configured. Use 'Find Available Device' and select the device first."
            )
        if ctrl == "darwin_serial" and not (port or "").strip():
            raise RuntimeError("No serial port is configured.")
        try:
            bravo.connect()
        except OSError as exc:
            raise RuntimeError(f"Could not connect to {addr or port}: {exc}") from exc
    await bravo.initialize()
    return {"status": "initialized", "controller": bravo._profile.connection.controller_type}

@app.post("/api/home", **_route_meta("Connection", "Home the robot into a safe parked state", "Retracts to safe Z, places the gripper into a safe docked state when present, homes the machine axes, and moves the homed axes to their park positions."))
async def home():
    bravo = get_bravo()
    axes = await bravo.home(force=True)
    return {"status": "homed", "axes": [axis.name for axis in axes]}

@app.post("/api/abort", **_route_meta("Connection", "Abort the active task", "Signals the task engine to abort the currently running operation after a fault or user stop request."))
async def abort():
    accepted = get_bravo().abort()
    return {"status": "aborted", "accepted": bool(accepted)}

@app.post("/api/retry", **_route_meta("Connection", "Retry the last failed task step", "Instructs the task engine to retry the current failed state-machine step."))
async def retry():
    accepted = get_bravo().retry()
    return {"status": "retried", "accepted": bool(accepted)}

@app.post("/api/ignore", **_route_meta("Connection", "Ignore the current task error and continue", "Tells the task engine to ignore the current error and continue to the next step, similar to continuing past a diagnostics fault."))
async def ignore_error():
    accepted = get_bravo().ignore()
    return {"status": "ignored", "accepted": bool(accepted)}


# -- REST endpoints: Motion --

@app.post("/api/move", **_route_meta("Motion", "Move a single axis to a target position", "Runs an individual axis move, similar to manual axis control in Bravo Diagnostics. Positions are expressed in engineering units for the selected axis."))
async def move(req: MoveRequest):
    axis = _parse_axis(req.axis)
    await get_bravo().move_axis(axis, req.position, req.velocity, req.acceleration)
    return {"status": "moved", "axis": req.axis, "position": req.position}

@app.post("/api/jog", **_route_meta("Motion", "Jog an axis by a relative step", "Performs a relative jog on a single axis for manual positioning or teaching."))
async def jog(req: JogRequest):
    axis = _parse_axis(req.axis)
    step = abs(req.step) * (1 if req.direction >= 0 else -1)
    new_pos = await get_bravo().jog_axis(axis, step, speed=_parse_speed_level(req.speed), peak_current=req.peak_current)
    return {"status": "jogged", "axis": req.axis, "step": step, "position": new_pos}

@app.post("/api/tip_force_jog", **_route_meta("Motion", "Force-controlled Z jog for tip pickup testing", "Experimental endpoint for diagnosing force-jog behavior. Uses tip_force_jog() with position-based settle detection."))
async def tip_force_jog_endpoint(req: JogRequest):
    axis = _parse_axis(req.axis)
    bravo = get_bravo()
    ctrl = bravo.controller
    if not hasattr(ctrl, "tip_force_jog"):
        raise RuntimeError("tip_force_jog not available on this controller")
    current_pos = ctrl.get_position(axis)
    step = abs(req.step) * (1 if req.direction >= 0 else -1)
    max_pos = current_pos + step
    peak_current = req.peak_current or 0.10
    final_pos = ctrl.tip_force_jog(axis, peak_current, max_pos)
    return {"status": "jogged", "axis": req.axis, "step": step, "position": final_pos,
            "start": current_pos, "max_position": max_pos, "peak_current": peak_current}

@app.post("/api/home_axis", **_route_meta("Motion", "Home a single axis", "Homes one axis independently, useful for diagnostics and recovery workflows."))
async def home_axis(req: AxisRequest):
    if req.axes:
        requested = [_parse_axis(name) for name in req.axes]
        ordered = safe_home_order(requested)
        bravo = get_bravo()
        logger.info(
            "Homing %s (requested %s, reordered for clearance)",
            ", ".join(a.name for a in ordered),
            ", ".join(a.name for a in requested),
        )
        for axis in ordered:
            await bravo.home_single_axis(axis)
        return {"status": "homed", "axes": [a.name for a in ordered]}

    if not req.axis:
        raise HTTPException(status_code=400, detail="Provide 'axis' or 'axes'")
    axis = _parse_axis(req.axis)
    await get_bravo().home_single_axis(axis)
    return {"status": "homed", "axis": req.axis}


# -- REST endpoints: Motor control --

@app.post("/api/motor/enable", **_route_meta("Motion", "Enable a motor for one axis", "Enables servo power for a single axis so it can be driven under software control."))
async def motor_enable(req: AxisRequest):
    axis = _parse_axis(req.axis)
    get_bravo().enable_motor(axis)
    return {"status": "enabled", "axis": req.axis}

@app.post("/api/motor/disable", **_route_meta("Motion", "Disable a motor for one axis", "Disables servo power for a single axis to allow manual repositioning or diagnostics."))
async def motor_disable(req: AxisRequest):
    axis = _parse_axis(req.axis)
    get_bravo().disable_motor(axis)
    return {"status": "disabled", "axis": req.axis}

@app.post("/api/motor/enable_all", **_route_meta("Motion", "Enable motors for all axes", "Enables servo power on every robot axis."))
async def motor_enable_all():
    bravo = get_bravo()
    for ax in Axis:
        bravo.enable_motor(ax)
    return {"status": "all_enabled"}

@app.post("/api/motor/disable_all", **_route_meta("Motion", "Disable motors for all axes", "Disables servo power on every robot axis."))
async def motor_disable_all():
    bravo = get_bravo()
    for ax in Axis:
        bravo.disable_motor(ax)
    return {"status": "all_disabled"}


# -- REST endpoints: Teachpoints --

@app.get("/api/teachpoint/{location}", **_route_meta("Teachpoints", "Get the teachpoint for a deck location", TEACHPOINT_DOC))
async def get_teachpoint(location: int):
    bravo = get_bravo()
    try:
        tp = {
            "x": bravo.teachpoints.get_teachpoint(location, Axis.X),
            "y": bravo.teachpoints.get_teachpoint(location, Axis.Y),
            "z": bravo.teachpoints.get_teachpoint(location, Axis.Z),
        }
        return {"location": location, "teachpoint": tp}
    except KeyError:
        return {"location": location, "teachpoint": None}

@app.post("/api/teachpoint/{location}", **_route_meta("Teachpoints", "Set the teachpoint for a deck location", TEACHPOINT_DOC))
async def set_teachpoint(location: int, req: TeachpointSetRequest):
    bravo = get_bravo()
    bravo.teachpoints.set_teachpoint(location, Axis.X, req.x)
    bravo.teachpoints.set_teachpoint(location, Axis.Y, req.y)
    bravo.teachpoints.set_teachpoint(location, Axis.Z, req.z)
    profile_name = _profile_path.stem if _profile_path is not None else None
    if _profile_path is not None:
        bravo.profile.save(_profile_path)
    logger.info(
        "Set location %d in profile '%s' to X=%.2f Y=%.2f Z=%.2f",
        location, profile_name or "<unsaved>", req.x, req.y, req.z,
    )
    return {"status": "taught", "location": location, "profile": profile_name}

@app.post("/api/teachpoint/{location}/teach_current", **_route_meta("Teachpoints", "Teach the current robot position into a deck location", TEACHPOINT_DOC))
async def teach_current_position(location: int, req: TeachCurrentRequest | None = None):
    """Teach the current head position as the teachpoint for this location."""
    bravo = get_bravo()
    head_type = bravo.profile.head.head_type
    requested_tip_id = str((req.tip_id if req is not None and req.tip_id is not None else "") or "").strip()
    tip_ref = requested_tip_id or float(
        (req.tip_capacity if req is not None and req.tip_capacity is not None else None)
        or bravo.profile.head.teach_tip_capacity
        or bravo.profile.head.default_tip_capacity
    )
    tip_id = requested_tip_id or get_tip_id_for_capacity(head_type, tip_ref) or bravo.active_tip_id() or get_default_tip_id_for_head(head_type) or ""
    tip_capacity = get_tip_capacity_ul(head_type, tip_id or tip_ref)
    tip_length_mm = get_tip_length_mm(head_type, tip_id or tip_ref)
    bravo.profile.head.teach_tip_id = tip_id
    bravo.profile.head.teach_tip_capacity = tip_capacity
    bravo.profile.head.teach_tip_length_mm = tip_length_mm
    for axis in (Axis.X, Axis.Y, Axis.Z):
        pos = bravo.get_position(axis)
        bravo.teachpoints.set_teachpoint(location, axis, pos)
    profile_name = _profile_path.stem if _profile_path is not None else None
    if _profile_path is not None:
        bravo.profile.save(_profile_path)
    # Teaching writes into whichever profile is active. Say so on every single
    # teach: silently teaching a whole deck into the wrong profile costs an
    # operator the entire session, and there is no other feedback that names it.
    logger.info(
        "Taught location %d into profile '%s' at X=%.2f Y=%.2f Z=%.2f (teach tip %s)",
        location,
        profile_name or "<unsaved>",
        bravo.teachpoints.get_teachpoint(location, Axis.X),
        bravo.teachpoints.get_teachpoint(location, Axis.Y),
        bravo.teachpoints.get_teachpoint(location, Axis.Z),
        tip_id or "?",
    )
    return {
        "status": "taught",
        "location": location,
        "profile": profile_name,
        "teach_tip_id": tip_id,
        "teach_tip_capacity": tip_capacity,
        "teach_tip_height_mm": tip_length_mm,
        "teach_tip": _tip_payload(head_type, tip_id or tip_capacity),
        "teachpoint": {
            "x": bravo.get_position(Axis.X),
            "y": bravo.get_position(Axis.Y),
            "z": bravo.get_position(Axis.Z),
        },
    }

@app.post("/api/move_to_location", **_route_meta("Motion", "Move the robot to a deck location", MOVE_LOCATION_DOC))
async def move_to_location(req: MoveToLocationRequest):
    await get_bravo().move_to_location(
        req.location,
        approach_height=req.approach_height,
        only_move_z=req.only_move_z,
        speed=_parse_speed_level(req.speed),
    )
    return {
        "status": "moved",
        "location": req.location,
        "approach_height": req.approach_height,
        "only_move_z": req.only_move_z,
        "speed": _parse_speed_level(req.speed).name,
    }

@app.post("/api/move_safe_z", **_route_meta("Motion", "Move the Z axis to the configured safe position", SAFE_Z_DOC))
async def move_safe_z(req: SpeedRequest):
    speed = _parse_speed_level(req.speed)
    await get_bravo().move_to_safe_z(speed=speed)
    return {"status": "moved_safe_z", "speed": speed.name}


# -- REST endpoints: Aspirate / Dispense / Tips --

@app.post("/api/aspirate", **_route_meta("Motion", "Aspirate from a deck location", ASPIRATE_DOC))
async def aspirate(location: int, volume: float):
    await get_bravo().aspirate(location=location, volume=volume)
    return {"status": "completed"}

@app.post("/api/dispense", **_route_meta("Motion", "Dispense to a deck location", DISPENSE_DOC))
async def dispense(location: int, volume: float):
    await get_bravo().dispense(location=location, volume=volume)
    return {"status": "completed"}

@app.post("/api/tips_on", **_route_meta("Motion", "Pick up disposable tips from a tip location", TIPS_ON_DOC))
async def tips_on(location: int):
    await get_bravo().tips_on(location)
    return {"status": "completed"}

@app.post("/api/tips_off", **_route_meta("Motion", "Discard disposable tips at a tip location", TIPS_OFF_DOC))
async def tips_off(location: int):
    await get_bravo().tips_off(location)
    return {"status": "completed"}


@app.get("/api/head_mode", **_route_meta("Head", "Get the active head mode configuration", HEAD_MODE_DOC))
async def get_head_mode():
    bravo = get_bravo()
    return {
        "head_type": bravo.profile.head.head_type.name,
        "head_mode": bravo.head_mode.to_dict(),
    }


@app.put("/api/head_mode", **_route_meta("Head", "Update the active head mode configuration", HEAD_MODE_DOC))
async def set_head_mode(req: HeadModeRequest):
    bravo = get_bravo()
    mode = bravo.set_head_mode(req.subset_type, req.subset_config, req.row_count, req.column_count)
    return {
        "status": "updated",
        "head_type": bravo.profile.head.head_type.name,
        "head_mode": mode.to_dict(),
    }


@app.get("/api/head_mode/suggest", **_route_meta("Head", "Suggest a head mode for the labware at a location", HEAD_MODE_DOC))
async def suggest_mode(location: int):
    bravo = get_bravo()
    labware = bravo.deck.get_stack(location).top
    wells = None if labware is None else int((labware.metadata or {}).get("wells") or 0)
    mode = suggested_head_mode(bravo.profile.head.head_type, wells)
    return {
        "location": location,
        "wells": wells,
        "head_type": bravo.profile.head.head_type.name,
        "head_mode": mode.to_dict(),
    }


@app.get("/api/tipbox/legal_anchors", **_route_meta("Head", "Compute legal tipbox anchors for a head mode", HEAD_MODE_DOC))
async def get_tipbox_legal_anchors(
    subset_type: str = "all_barrels",
    subset_config: str = "back_left",
    row_count: int | None = None,
    column_count: int | None = None,
    tipbox_rows: int = 8,
    tipbox_cols: int = 12,
    purpose: str = "pickup",
    occupied_cells: str | None = None,
):
    """Return the design-time legal anchor positions for the given head mode
    over a tipbox of `tipbox_rows × tipbox_cols`. Used by the workflow
    designer's Tips On / Tips Off pickers so the operator can only choose
    placements that won't have the head's inactive barrels collide with
    other tips.

    Occupancy:
    - If `occupied_cells` is provided (comma-separated "row:col" pairs, e.g.
      "0:0,0:1,1:2"), it's used as the exact set of occupied cells. The
      designer's forward-simulation passes its computed occupancy here so
      the picker reflects which cells are full/empty AT THIS POINT in the
      workflow.
    - Otherwise, defaults are derived from `purpose`: 'pickup' assumes the
      tipbox is FULL (occupied=all); 'return' assumes it is EMPTY (occupied=∅).
      Used as fallbacks when the caller can't simulate occupancy.
    """
    from pybravo.head_mode import legal_tipbox_anchors, normalize_head_mode
    bravo = get_bravo()
    try:
        mode = normalize_head_mode(
            bravo.profile.head.head_type,
            subset_type,
            subset_config,
            row_count,
            column_count,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid head_mode: {exc}")
    if tipbox_rows <= 0 or tipbox_cols <= 0:
        raise HTTPException(status_code=400, detail="tipbox_rows and tipbox_cols must be positive")
    if purpose not in ("pickup", "return"):
        raise HTTPException(status_code=400, detail="purpose must be 'pickup' or 'return'")

    occupied: set[tuple[int, int]]
    if occupied_cells is not None:
        # Explicit occupancy from the caller (typically the designer's
        # simulator). Parse "r:c,r:c,..." into a set of tuples. Silently
        # drops malformed tokens — the picker degrades to a less-accurate
        # but still-functional view rather than 500-ing.
        occupied = set()
        for token in occupied_cells.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                r_str, c_str = token.split(":", 1)
                r, c = int(r_str), int(c_str)
                if 0 <= r < tipbox_rows and 0 <= c < tipbox_cols:
                    occupied.add((r, c))
            except (ValueError, TypeError):
                continue
    elif purpose == "pickup":
        occupied = {(r, c) for r in range(tipbox_rows) for c in range(tipbox_cols)}
    else:
        occupied = set()
    anchors = legal_tipbox_anchors(tipbox_rows, tipbox_cols, mode, occupied, purpose=purpose)
    return {
        "head_mode": mode.to_dict(),
        "tipbox_rows": tipbox_rows,
        "tipbox_cols": tipbox_cols,
        "purpose": purpose,
        "occupied_cells_count": len(occupied),
        "legal_anchors": [anchor.to_dict() for anchor in anchors],
    }


@app.get("/api/tip_selection", **_route_meta("Head", "Get the current tip selection state", TIP_SELECTION_DOC))
async def get_tip_selection():
    bravo = get_bravo()
    state = bravo.get_state()
    return {
        "tip_selection": state.get("tip_selection"),
        "tips_on_head_selection": state.get("tips_on_head_selection"),
    }


@app.put("/api/tip_selection", **_route_meta("Head", "Update the selected tip position", TIP_SELECTION_DOC))
async def set_tip_selection(req: TipSelectionRequest):
    bravo = get_bravo()
    selection = bravo.set_tip_selection(req.location, req.row, req.col)
    return {
        "status": "updated",
        "tip_selection": selection.to_dict(),
    }


@app.get("/api/plate_selection", **_route_meta("Head", "Get the current selected plate anchor and legal anchors", HEAD_MODE_DOC))
async def get_plate_selection(location: int):
    bravo = get_bravo()
    return bravo.get_plate_selection_state(location)


@app.put("/api/plate_selection", **_route_meta("Head", "Update the selected plate anchor well", HEAD_MODE_DOC))
async def set_plate_selection(req: PlateSelectionRequest):
    bravo = get_bravo()
    selection = bravo.set_plate_selection(req.location, req.row, req.col)
    state = bravo.get_plate_selection_state(req.location)
    return {
        "status": "updated",
        "plate_selection": selection.to_dict(),
        "legal_anchors": state.get("legal_anchors", []),
        "footprint": state.get("footprint", []),
    }


# -- REST endpoints: Gripper --

@app.post("/api/gripper/open", **_route_meta("Motion", "Open the gripper", GRIPPER_DOC))
async def gripper_open():
    get_bravo().open_gripper()
    return {"status": "opened"}

@app.post("/api/gripper/close", **_route_meta("Motion", "Close the gripper", GRIPPER_DOC))
async def gripper_close():
    get_bravo().close_gripper()
    return {"status": "closed"}

@app.post("/api/gripper/dock", **_route_meta("Motion", "Open and dock the gripper", GRIPPER_DOC))
async def gripper_dock():
    bravo = get_bravo()
    result = await bravo.dock_gripper()
    return {"status": "docked", **result}

@app.post("/api/gripper/move_to_location", **_route_meta("Motion", "Position the gripper over a deck location", "Runs the approach half of a pick so the gripper's Y alignment can be judged, then stops. The gripper is never closed, so no plate is lifted. Fails if the location holds no labware. MOVES HARDWARE."))
async def gripper_move_to_location(req: GripperTeachMoveRequest):
    return await get_bravo().move_gripper_to_location(
        req.location,
        approach_height=req.approach_height,
        speed=_parse_speed_level(req.speed),
    )


@app.post("/api/gripper/teach_y_offset", **_route_meta("Teachpoints", "Teach the gripper Y offset from the current position", "Captures how far Y sits from the location's teachpoint into profile.gripper.y_offset, and saves the profile. Jog the gripper until it is centred on the plate first."))
async def gripper_teach_y_offset(req: GripperTeachRequest):
    bravo = get_bravo()
    result = bravo.teach_gripper_y_offset(req.location)
    profile_name = _profile_path.stem if _profile_path is not None else None
    if _profile_path is not None:
        bravo.profile.save(_profile_path)
    logger.info(
        "Saved gripper Y offset %.3f mm into profile '%s'",
        result["y_offset"], profile_name or "<unsaved>",
    )
    result["profile"] = profile_name
    return result


@app.post("/api/pick_place", **_route_meta("Motion", "Pick labware from one deck location and place it in another", PICK_PLACE_DOC))
async def pick_place(req: PickPlaceRequest):
    bravo = get_bravo()
    diagnostics = await bravo.pick_place(
        req.from_location,
        req.to_location,
        speed=_parse_speed_level(req.speed, SpeedLevel.MED),
    )
    return {
        "status": "completed",
        "from": req.from_location,
        "to": req.to_location,
        "diagnostics": diagnostics,
    }


# -- REST endpoints: Command execution --

@app.post("/api/execute_command", **_route_meta("Motion", "Execute a high-level command by name", EXECUTE_COMMAND_DOC))
async def execute_command(req: ExecuteCommandRequest):
    bravo = get_bravo()
    cmd = req.command.lower().replace(" ", "_")
    if cmd == "aspirate":
        await bravo.aspirate(
            location=req.location, volume=req.volume,
            pre_aspirate=req.pre_aspirate, post_aspirate=req.post_aspirate,
            distance_from_bottom=req.distance_from_bottom,
            dynamic_tip_extension=req.dynamic_tip_extension,
            tip_touch=req.tip_touch,
            liquid_class=req.liquid_class,
            pipette_technique=req.pipette_technique,
        )
    elif cmd == "dispense":
        await bravo.dispense(
            location=req.location, volume=req.volume,
            blowout=req.blowout,
            distance_from_bottom=req.distance_from_bottom,
            empty_tips=req.empty_tips,
            dynamic_tip_retraction=req.dynamic_tip_retraction,
            tip_touch=req.tip_touch,
            liquid_class=req.liquid_class,
            pipette_technique=req.pipette_technique,
        )
    elif cmd == "tips_on":
        await bravo.tips_on(req.location)
    elif cmd == "tips_off":
        await bravo.tips_off(req.location)
    elif cmd == "mix":
        await bravo.mix(
            location=req.location,
            volume=req.volume,
            pre_aspirate=req.pre_aspirate,
            blowout=req.blowout,
            mix_cycles=req.mix_cycles,
            aspirate_distance=req.aspirate_distance if req.aspirate_distance is not None else req.distance_from_bottom,
            dispense_at_different_distance=req.dispense_at_different_distance,
            dispense_distance=req.dispense_distance if req.dispense_distance is not None else req.distance_from_bottom,
            dynamic_tip_extension=req.dynamic_tip_extension,
            tip_touch=req.tip_touch,
            liquid_class=req.liquid_class,
            pipette_technique=req.pipette_technique,
        )
    elif cmd == "stack_plates":
        result = await bravo.stack_plates(
            base_location=int(req.base_location or 0),
            source_location=int(req.source_location or 0),
        )
        return result
    elif cmd == "destack_plate":
        result = await bravo.destack_plate(
            source_location=int(req.source_location or 0),
            destination_location=int(req.destination_location or 0),
        )
        return result
    elif cmd == "mount_plates":
        # Same parameter shape as stack_plates; the task flags the
        # resulting pair so later pick/place moves transport both
        # plates together (vacuum-filtration use case).
        result = await bravo.mount_plates(
            base_location=int(req.base_location or 0),
            source_location=int(req.source_location or 0),
        )
        return result
    elif cmd == "unmount_plate":
        # Inverse of mount_plates — separates a mounted pair by
        # moving the mounted top plate off to an empty pad.
        result = await bravo.unmount_plate(
            source_location=int(req.source_location or 0),
            destination_location=int(req.destination_location or 0),
        )
        return result
    elif cmd == "delid_plate":
        result = await bravo.delid_plate(
            plate_location=int(req.plate_location or 0),
            lid_destination=int(req.lid_destination or 0),
        )
        return result
    elif cmd == "relid_plate":
        result = await bravo.relid_plate(
            lid_location=int(req.lid_location or 0),
            plate_location=int(req.plate_location or 0),
        )
        return result
    elif cmd == "scan_stack_height":
        result = await bravo.scan_stack_height(
            location=req.location,
            manual_count=req.manual_count,
        )
        return result
    elif cmd == "read_barcode":
        result = await bravo.read_barcode(location=req.location)
        return result
    else:
        return {"status": "error", "message": f"Unknown command: {req.command}"}
    return {"status": "completed", "command": req.command}


# -- REST endpoints: State & I/O --

@app.get("/api/state", **_route_meta("State", "Get the full robot runtime state", "Returns the consolidated runtime state used by the UI, including positions, teachpoints, deck contents, telemetry, head/tip state, and active task status."))
async def get_state():
    return get_bravo().get_state()

@app.get("/api/positions", **_route_meta("State", "Get the current axis positions", "Returns the current engineering-unit positions for available robot axes."))
async def get_positions():
    return get_bravo().get_all_positions()

@app.get("/api/io_status", **_route_meta("State", "Get summarized robot I/O and safety status", "Returns a compact diagnostics view of safety interlocks, head presence, go-button state, plate-in-gripper state, and motor enable status."))
async def get_io_status():
    bravo = get_bravo()
    state = bravo.get_state()
    return {
        "robot_disabled": state.get("robot_disabled", False),
        "head_attached": state.get("head_attached", False),
        "head_type": state.get("head_type", "unknown"),
        "go_button_pressed": state.get("go_button_pressed", False),
        "plate_in_gripper": state.get("plate_in_gripper", False),
        "motors_enabled": state.get("motors_enabled", {}),
    }


@app.get("/api/diagnostics", **_route_meta("State", "Get wire-level diagnostics", "Returns command counts and error log from the V11 comm layer. Only available for agile_7612 controller type."))
async def get_diagnostics():
    bravo = get_bravo()
    ctrl = bravo.controller
    if hasattr(ctrl, 'get_diagnostics'):
        return ctrl.get_diagnostics()
    return {"error": "Diagnostics not available for this controller type"}


@app.get("/api/accessories", **_route_meta("State", "Get accessory runtime status", "Returns configured accessories plus lazy driver runtime state."))
async def get_accessories():
    return get_bravo().accessory_status()


@app.post("/api/accessories/{accessory_id}/teleshake/start", **_route_meta("State", "Start a Teleshake accessory", "Starts the configured Teleshake orbital shaker."))
async def start_teleshake(accessory_id: str, req: TeleshakeActionRequest):
    try:
        return get_bravo().start_teleshake(
            accessory_id,
            rpm=req.rpm,
            direction=req.direction,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("Teleshake start failed for %s: %s", accessory_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/accessories/{accessory_id}/teleshake/stop", **_route_meta("State", "Stop a Teleshake accessory", "Stops the configured Teleshake orbital shaker."))
async def stop_teleshake(accessory_id: str):
    try:
        return get_bravo().stop_teleshake(accessory_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("Teleshake stop failed for %s: %s", accessory_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/vision/status", **_route_meta("Vision", "Get vision service and camera status", VISION_STATUS_DOC))
async def get_vision_status():
    _require_vision_enabled()
    try:
        return _vision_client().request_json("/status", method="GET")
    except VisionServiceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/vision/calibration", **_route_meta("Vision", "Get saved vision calibration", VISION_CALIBRATION_DOC))
async def get_vision_calibration():
    _require_vision_enabled()
    try:
        return _vision_client().request_json("/calibration", method="GET")
    except VisionServiceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/vision/calibration/run", **_route_meta("Vision", "Run or scaffold camera calibration", VISION_CALIBRATION_RUN_DOC))
async def run_vision_calibration(req: VisionCalibrationRunRequest):
    bravo = _require_vision_enabled()
    try:
        return _vision_client().request_json(
            "/calibration/run",
            method="POST",
            payload={
                "machine_id": bravo.machine_id,
                "notes": req.notes or "",
            },
        )
    except VisionServiceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/vision/calibration/capture_baselines", **_route_meta("Vision", "Capture empty-deck depth baselines", "Captures a live depth reference for all 9 calibrated deck ROIs. Run this with an empty deck after ROI calibration and before verification."))
async def capture_vision_baselines():
    _require_vision_enabled()
    try:
        return _vision_client().request_json(
            "/calibration/capture_baselines",
            method="POST",
            payload={},
        )
    except VisionServiceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/vision/verify", **_route_meta("Vision", "Verify physical deck setup with the camera", VISION_VERIFY_DOC))
async def verify_vision_deck():
    bravo = _require_vision_enabled()
    try:
        return _vision_client().request_json(
            "/verify",
            method="POST",
            payload={"expected_scene": _expected_vision_scene(bravo)},
        )
    except VisionServiceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/vision/preview", **_route_meta("Vision", "Get the latest camera preview state", VISION_PREVIEW_DOC))
async def get_vision_preview():
    _require_vision_enabled()
    try:
        content, content_type = _vision_client().request_bytes("/preview", method="GET")
        return Response(content=content, media_type=content_type)
    except VisionServiceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/vision/preview/depth", **_route_meta("Vision", "Get the latest colorized depth preview", VISION_PREVIEW_DOC))
async def get_vision_preview_depth():
    _require_vision_enabled()
    try:
        content, content_type = _vision_client().request_bytes("/preview/depth", method="GET")
        return Response(content=content, media_type=content_type)
    except VisionServiceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/vision/detect", include_in_schema=False)
async def get_vision_detect():
    bravo = _require_vision_enabled()
    try:
        data = _vision_client().request_json("/detect", method="GET")
        report = data.get("report") or {}
        slots = report.get("slots") or []
        expected_by_location = _expected_vision_labware_by_location(bravo)
        for slot in slots:
            expected = expected_by_location.get(slot.get("location"))
            slot["expected_labware"] = expected
            if expected and slot.get("occupied"):
                slot["display_label"] = expected.get("name") or expected.get("kind") or "configured labware"
            elif slot.get("observed_class"):
                slot["display_label"] = slot.get("observed_class")
            else:
                slot["display_label"] = "empty"
        return data
    except VisionServiceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/vision/stream", include_in_schema=False)
async def get_vision_stream():
    _require_vision_enabled()
    return RedirectResponse(url=_vision_client().url_for("/stream"), status_code=307)


@app.get("/api/vision/stream/depth", include_in_schema=False)
async def get_vision_stream_depth():
    _require_vision_enabled()
    return RedirectResponse(url=_vision_client().url_for("/stream/depth"), status_code=307)


@app.post("/api/vision/calibration/roi/start", **_route_meta("Vision", "Launch the ROI calibration tool", VISION_ROI_CALIBRATION_START_DOC))
async def start_vision_roi_calibration(location: int | None = None):
    _require_vision_enabled()
    repo_root = Path(__file__).resolve().parents[2]
    try:
        status = _vision_client().request_json("/status", method="GET")
    except VisionServiceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    calibration_file = (status.get("calibration") or {}).get("file") or {}
    reference_image_path = calibration_file.get("reference_image_path")
    if not reference_image_path:
        raise HTTPException(
            status_code=400,
            detail="No reference image is available yet. Run Start Guided Calibration first.",
        )

    reference_path = Path(str(reference_image_path))
    if not reference_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Reference image not found: {reference_path}",
        )

    if location is not None and location not in range(1, 10):
        raise HTTPException(status_code=400, detail="ROI location must be between 1 and 9")

    command = [sys.executable, "-m", "pybravo.vision.calibrate_rois", str(reference_path)]
    if location is not None:
        command.append(str(location))

    subprocess.Popen(
        ["cmd", "/c", "start", "", *command],
        cwd=str(repo_root),
    )
    return {
        "status": "starting",
        "reference_image_path": str(reference_path),
        "location": location,
        "command": command,
    }


@app.post("/api/vision/service/start", **_route_meta("Vision", "Start the local vision service", VISION_SERVICE_START_DOC))
async def start_vision_service():
    bravo = _require_vision_enabled()
    try:
        status = _vision_client().request_json("/status", method="GET", timeout=1.0)
        return {"status": "already_running", "service": status}
    except VisionServiceError:
        pass

    repo_root = Path(__file__).resolve().parents[2]
    batch_path = repo_root / "scripts" / "start_vision_service.bat"
    if os.name != "nt":
        raise HTTPException(status_code=400, detail="Vision service launcher is only implemented for Windows")
    if not batch_path.exists():
        raise HTTPException(status_code=404, detail="Vision service launcher script not found")

    env = os.environ.copy()
    env["PYBRAVO_VISION_SERVICE_URL"] = str(getattr(bravo.profile.vision, "service_url", "http://127.0.0.1:8101"))
    env["PYBRAVO_VISION_SDK_ROOT"] = str(getattr(bravo.profile.vision, "sdk_root", "external/pyorbbecsdk"))
    env["PYBRAVO_PYTHON"] = sys.executable
    subprocess.Popen(
        ["cmd", "/c", "start", "", "/min", str(batch_path)],
        cwd=str(repo_root),
        env=env,
    )
    return {
        "status": "starting",
        "command": str(batch_path),
        "python": env["PYBRAVO_PYTHON"],
        "service_url": env["PYBRAVO_VISION_SERVICE_URL"],
        "sdk_root": env["PYBRAVO_VISION_SDK_ROOT"],
    }


@app.get("/api/labware", **_route_meta("Labware", "List the runtime labware catalog used by PyBravo", LABWARE_DOC))
async def list_labware():
    bravo = get_bravo()
    definitions, _alias_ids = normalize_labware_definitions(bravo.labware_catalog.list_definitions())
    return {"labware": [definition.to_summary() for definition in definitions]}


def _active_liquid_context(bravo: Bravo) -> dict[str, Any]:
    return {
        "machine_id": bravo.machine_id,
        "head_type": bravo.profile.head.head_type.name,
        "tip_id": bravo.active_tip_id(),
        "tip_capacity_ul": bravo.active_tip_capacity_ul(),
    }


def _vision_client() -> VisionServiceClient:
    bravo = get_bravo()
    return VisionServiceClient(getattr(bravo.profile.vision, "service_url", None))


def _vision_enabled(bravo: Bravo | None = None) -> bool:
    bravo = bravo or get_bravo()
    return bool(getattr(bravo.profile.vision, "enabled", False))


def _require_vision_enabled() -> Bravo:
    bravo = get_bravo()
    if not _vision_enabled(bravo):
        raise HTTPException(status_code=404, detail="Vision feature is disabled in the active profile")
    return bravo


def _expected_vision_scene(bravo: Bravo) -> dict[str, Any]:
    state = bravo.get_state()
    slots: list[dict[str, Any]] = []
    deck_details = state.get("deck_details") or {}
    teachpoints = state.get("teachpoints") or {}
    tipbox_inventory = state.get("tipbox_inventory") or {}
    for location in range(1, 10):
        items = deck_details.get(str(location)) or []
        detail = items[-1] if items else None
        inventory = tipbox_inventory.get(str(location))
        slots.append(
            {
                "location": location,
                "teachpoint": teachpoints.get(str(location)),
                "expected_labware": detail,
                "expected_tipbox_occupancy": None if inventory is None else {
                    "rows": inventory.get("rows"),
                    "cols": inventory.get("cols"),
                    "occupied": inventory.get("occupied"),
                    "tip_id": inventory.get("tip_id"),
                },
            }
        )
    return {
        "machine_id": state.get("machine_id"),
        "head_type": state.get("head_type"),
        "camera_mount": "fixed_side_view",
        "slots": slots,
    }


def _expected_vision_labware_by_location(bravo: Bravo) -> dict[int, dict[str, Any] | None]:
    expected: dict[int, dict[str, Any] | None] = {}
    for location in range(1, 10):
        stack = bravo._deck.get_stack(location)  # lightweight deck read for live occupancy decoration
        labware = stack.top
        expected[location] = None if labware is None else (labware.metadata or {"name": labware.name})
    return expected


@app.get("/api/liquid_context", **_route_meta("Labware", "Get the active liquid handling context", LIQUID_CONTEXT_DOC))
async def get_liquid_context():
    bravo = get_bravo()
    return _active_liquid_context(bravo)


@app.get("/api/tips", **_route_meta("Head", "List tip definitions", TIPS_CATALOG_DOC))
async def list_tip_definitions(head_type: str | None = None):
    if head_type:
        return {"tips": serialize_tip_options_for_head(head_type)}
    return {"tips": list_tip_items()}


@app.post("/tips", **_route_meta("Head", "Create a tip definition", TIPS_CATALOG_DOC))
async def create_tip(req: TipDefinitionRequest):
    try:
        return {"tip": create_tip_definition(req.model_dump(exclude_none=True))}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/tips/{tip_id}", **_route_meta("Head", "Update a tip definition", TIPS_CATALOG_DOC))
async def update_tip(tip_id: str, req: TipDefinitionRequest):
    try:
        return {"tip": patch_tip_definition(tip_id, req.model_dump(exclude_none=True))}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown tip definition: {tip_id}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/tips/{tip_id}", **_route_meta("Head", "Delete a tip definition", TIPS_CATALOG_DOC))
async def remove_tip(tip_id: str):
    try:
        delete_tip_definition(tip_id)
        return {"status": "deleted", "tip_id": tip_id}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown tip definition: {tip_id}") from exc


@app.get("/api/liquid_classes", **_route_meta("Labware", "List liquid classes", LIQUID_CLASSES_DOC))
async def list_liquid_classes(machine_id: str | None = None, head_type: str | None = None, tip_id: str | None = None, tip_capacity_ul: float | None = None, all: bool = False):
    """List liquid classes.

    By default, returns ONLY classes that strictly match the current (or
    explicitly requested) (machine_id, head_type, tip_id) context. This
    prevents stale / cross-machine classes from showing up in the Designer
    dropdown and then failing at execution inside `Bravo._resolve_liquid_class`.

    Pass ?all=true to return the entire catalogue (used by the liquid-class
    manager page where the operator is authoring across contexts).
    """
    bravo = get_bravo()
    context = _active_liquid_context(bravo)
    if all:
        return {
            "context": context,
            "liquid_classes": liquid_classes_store.list_liquid_classes(
                machine_id=None,
                head_type=None,
                tip_id=None,
                tip_capacity_ul=None,
            ),
        }
    machine_id = machine_id or context["machine_id"]
    head_type = head_type or context["head_type"]
    tips_on = bool(getattr(bravo, "_tips_on_head", False))
    # Tip narrowing tracks the physical head state:
    #   - tips ON  -> restrict to the loaded tip (the class that will actually run)
    #   - tips OFF -> list EVERY class for this (machine_id, head_type) so the
    #                 operator can pick one while authoring with no tips loaded.
    # machine_id + head_type stay strict in BOTH cases, so classes for other
    # devices/heads never leak in (we never drop those filters). An explicit
    # ?tip_id / ?tip_capacity_ul query narrows on demand regardless of head state.
    explicit_tip = tip_id is not None or tip_capacity_ul is not None
    if explicit_tip:
        pass  # honor the caller-supplied tip narrowing as-is
    elif tips_on:
        tip_id = context["tip_id"]
        tip_capacity_ul = context["tip_capacity_ul"]
    else:
        tip_id = None
        tip_capacity_ul = None
    # Strict (machine, head[, tip]) match. A single safety fallback drops the
    # tip_capacity_ul check while keeping tip_id fixed — this mirrors the
    # strict-match semantics in `liquid_classes.get_liquid_class`.
    items = liquid_classes_store.list_liquid_classes(
        machine_id=machine_id,
        head_type=head_type,
        tip_id=tip_id,
        tip_capacity_ul=tip_capacity_ul,
    )
    if not items and tip_capacity_ul is not None:
        items = liquid_classes_store.list_liquid_classes(
            machine_id=machine_id,
            head_type=head_type,
            tip_id=tip_id,
            tip_capacity_ul=None,
        )
    return {
        "context": {
            "machine_id": machine_id,
            "head_type": head_type,
            "tip_id": tip_id,
            "tip_capacity_ul": tip_capacity_ul,
            "tips_on": tips_on,
        },
        "liquid_classes": items,
    }


@app.post("/liquid-classes", **_route_meta("Labware", "Create a liquid class", LIQUID_CLASSES_DOC))
async def create_liquid_class(req: LiquidClassRequest):
    try:
        item = liquid_classes_store.create_liquid_class(req.model_dump(exclude_none=True))
        return {"liquid_class": item}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/liquid-classes/{liquid_class_id}", **_route_meta("Labware", "Update a liquid class", LIQUID_CLASSES_DOC))
async def update_liquid_class(liquid_class_id: str, req: LiquidClassRequest):
    try:
        item = liquid_classes_store.patch_liquid_class(liquid_class_id, req.model_dump(exclude_none=True))
        return {"liquid_class": item}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown liquid class: {liquid_class_id}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/liquid-classes/{liquid_class_id}", **_route_meta("Labware", "Delete a liquid class", LIQUID_CLASSES_DOC))
async def delete_liquid_class(liquid_class_id: str):
    try:
        liquid_classes_store.delete_liquid_class(liquid_class_id)
        return {"status": "deleted", "liquid_class_id": liquid_class_id}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown liquid class: {liquid_class_id}") from exc


@app.get("/api/pipette_techniques", **_route_meta("Labware", "List pipette techniques", PIPETTE_TECHNIQUES_DOC))
async def list_pipette_techniques():
    return {"pipette_techniques": liquid_classes_store.list_pipette_techniques()}


@app.post("/pipette-techniques", **_route_meta("Labware", "Create a pipette technique", PIPETTE_TECHNIQUES_DOC))
async def create_pipette_technique(req: PipetteTechniqueRequest):
    try:
        item = liquid_classes_store.create_pipette_technique(req.model_dump(exclude_none=True))
        return {"pipette_technique": item}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/pipette-techniques/{technique_id}", **_route_meta("Labware", "Update a pipette technique", PIPETTE_TECHNIQUES_DOC))
async def update_pipette_technique(technique_id: str, req: PipetteTechniqueRequest):
    try:
        item = liquid_classes_store.patch_pipette_technique(technique_id, req.model_dump(exclude_none=True))
        return {"pipette_technique": item}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown pipette technique: {technique_id}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/pipette-techniques/{technique_id}", **_route_meta("Labware", "Delete a pipette technique", PIPETTE_TECHNIQUES_DOC))
async def delete_pipette_technique(technique_id: str):
    try:
        liquid_classes_store.delete_pipette_technique(technique_id)
        return {"status": "deleted", "technique_id": technique_id}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown pipette technique: {technique_id}") from exc


@app.get("/labware/types", **_route_meta("Labware", "List editable labware types", LABWARE_DOC))
async def list_labware_types():
    return {"labware_types": labware_editor.list_types()}


@app.post("/labware/types", **_route_meta("Labware", "Create a labware type", LABWARE_DOC))
async def create_labware_type(req: LabwareTypeRequest):
    try:
        item = labware_editor.create_type(req.model_dump(exclude_none=True))
        _refresh_runtime_labware_catalog()
        return {"labware_type": item}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/labware/types/{labware_type_id}", **_route_meta("Labware", "Update a labware type", LABWARE_DOC))
async def update_labware_type(labware_type_id: str, req: LabwareTypeRequest):
    try:
        item = labware_editor.patch_type(labware_type_id, req.model_dump(exclude_none=True))
        _refresh_runtime_labware_catalog()
        if _bravo is not None:
            _bravo.refresh_live_labware(labware_type_id)
        return {"labware_type": item}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown labware type: {labware_type_id}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/labware/types/{labware_type_id}", **_route_meta("Labware", "Delete a labware type", LABWARE_DOC))
async def remove_labware_type(labware_type_id: str):
    try:
        labware_editor.delete_type(labware_type_id)
        _refresh_runtime_labware_catalog()
        return {"status": "deleted", "labware_type_id": labware_type_id}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown labware type: {labware_type_id}") from exc


@app.post("/labware/types/{labware_type_id}/assets/image", **_route_meta("Labware", "Upload a 2D image asset for a labware type", "Uploads a 2D image used by the labware editor or UI for a labware definition."))
async def upload_labware_image(labware_type_id: str, request: Request):
    try:
        form = await request.form()
        file = form.get("file")
        if file is None:
            raise ValueError("Missing uploaded file")
        item = labware_editor.save_asset(
            labware_type_id,
            "image",
            getattr(file, "filename", "image.bin"),
            await file.read(),
        )
        return {"labware_type": item}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown labware type: {labware_type_id}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/labware/types/{labware_type_id}/assets/model", **_route_meta("Labware", "Upload a 3D model asset for a labware type", "Uploads a 3D asset used to preview or render a labware definition in the UI."))
async def upload_labware_model(labware_type_id: str, request: Request):
    try:
        form = await request.form()
        file = form.get("file")
        if file is None:
            raise ValueError("Missing uploaded file")
        item = labware_editor.save_asset(
            labware_type_id,
            "model",
            getattr(file, "filename", "model.bin"),
            await file.read(),
        )
        _refresh_runtime_labware_catalog()
        return {"labware_type": item}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown labware type: {labware_type_id}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/labware/classes", **_route_meta("Labware", "List editable labware classes", "Lists editable labware classes used to group compatible labware entries."))
async def list_labware_classes():
    return {"labware_classes": labware_editor.list_classes()}


@app.post("/labware/classes", **_route_meta("Labware", "Create a labware class", "Creates a new labware class for grouping related or compatible entries."))
async def create_labware_class(req: LabwareClassRequest):
    try:
        item = labware_editor.create_class(req.model_dump(exclude_none=True))
        return {"labware_class": item}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/labware/classes/{labware_class_id}", **_route_meta("Labware", "Update a labware class", "Updates a labware class name or description."))
async def update_labware_class(labware_class_id: str, req: LabwareClassRequest):
    try:
        item = labware_editor.patch_class(labware_class_id, req.model_dump(exclude_none=True))
        return {"labware_class": item}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown labware class: {labware_class_id}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/labware/classes/{labware_class_id}", **_route_meta("Labware", "Delete a labware class", "Deletes a labware class and removes its membership references from labware entries."))
async def remove_labware_class(labware_class_id: str):
    try:
        labware_editor.delete_class(labware_class_id)
        _refresh_runtime_labware_catalog()
        return {"status": "deleted", "labware_class_id": labware_class_id}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown labware class: {labware_class_id}") from exc


@app.get("/labware-editor", response_class=HTMLResponse, include_in_schema=False)
async def labware_editor_page():
    source = _labware_dashboard_source().replace("</script>", "<\\/script>")
    html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>PyBravo Labware Editor</title>
  <style>
    body {{ margin: 0; font-family: Arial, sans-serif; background: #0b0b12; color: #fff; }}
    #root {{ min-height: 100vh; }}
  </style>
  <script type="importmap">
  {
    "imports": {
      "react": "https://esm.sh/react@18",
      "react/jsx-runtime": "https://esm.sh/react@18/jsx-runtime",
      "react/jsx-dev-runtime": "https://esm.sh/react@18/jsx-dev-runtime",
      "react-dom/client": "https://esm.sh/react-dom@18/client?external=react",
      "three": "https://esm.sh/three@0.160.0",
      "@react-three/fiber": "https://esm.sh/@react-three/fiber@8?external=react,react-dom,three",
      "@react-three/drei": "https://esm.sh/@react-three/drei@9?external=react,react-dom,three,@react-three/fiber",
      "three/examples/jsm/loaders/STLLoader": "https://esm.sh/three@0.160.0/examples/jsm/loaders/STLLoader.js?external=three",
      "three/examples/jsm/loaders/OBJLoader": "https://esm.sh/three@0.160.0/examples/jsm/loaders/OBJLoader.js?external=three"
    }
  }
  </script>
</head>
<body>
  <div id="root"></div>
  <script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>
  <script type="text/babel" data-presets="react" data-type="module">
__LABWARE_DASHBOARD_SOURCE__
  </script>
</body>
</html>"""
    return html.replace("__LABWARE_DASHBOARD_SOURCE__", source)


@app.get("/liquid-class-editor", response_class=HTMLResponse, include_in_schema=False)
async def liquid_class_editor_page():
    source_path = Path(__file__).resolve().parents[2] / "frontend" / "liquid_class_editor.html"
    if not source_path.exists():
        raise HTTPException(status_code=404, detail="Liquid class editor page not found")
    return HTMLResponse(source_path.read_text(encoding="utf-8"))


@app.get("/tip-editor", response_class=HTMLResponse, include_in_schema=False)
async def tip_editor_page():
    source_path = Path(__file__).resolve().parents[2] / "frontend" / "tip_editor.html"
    if not source_path.exists():
        raise HTTPException(status_code=404, detail="Tip editor page not found")
    return HTMLResponse(source_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Workflow editor
# ---------------------------------------------------------------------------

@app.get("/workflow", response_class=HTMLResponse, include_in_schema=False)
async def workflow_editor_page():
    source_path = Path(__file__).resolve().parents[2] / "frontend" / "workflow_editor.html"
    if not source_path.exists():
        raise HTTPException(status_code=404, detail="Workflow editor page not found")
    return HTMLResponse(source_path.read_text(encoding="utf-8"))


_STATIC_ASSET_VERSION_RE = re.compile(
    r'(/static/[A-Za-z0-9_./-]+\.(?:js|css|gltf))\?v=[^"\'\s)]*'
)

NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


def _version_static_assets(html: str, static_root: Path) -> str:
    """Stamp cached asset URLs with the referenced file's mtime.

    These pages import ES modules with a hand-written ``?v=`` token. Browsers
    cache modules by full URL, so a token that never changes pins every client
    to whatever it fetched first: a frontend fix can be committed, served
    correctly, and still not be running in a single browser. That is not
    hypothetical — a tip-box rendering fix looked completely inert for exactly
    this reason.

    Deriving the token from the file's mtime removes the need for anyone to
    remember. No-cache headers on the HTML are not enough on their own, because
    the module URL is cached independently of the page that names it.
    """

    def replace(match: re.Match[str]) -> str:
        url = match.group(1)
        candidate = static_root / url[len("/static/"):]
        try:
            stamp = int(candidate.stat().st_mtime)
        except OSError:
            # Unknown file — leave the author's token alone rather than
            # inventing one that would break the URL.
            return match.group(0)
        return f"{url}?v={stamp}"

    return _STATIC_ASSET_VERSION_RE.sub(replace, html)


@app.get("/designer", response_class=HTMLResponse, include_in_schema=False)
async def workflow_designer_page():
    source_path = Path(__file__).resolve().parents[2] / "frontend" / "designer.html"
    if not source_path.exists():
        raise HTTPException(status_code=404, detail="Workflow designer page not found")
    # Designer HTML is edited in-place during active development; without
    # explicit no-cache headers browsers serve a stale copy across server
    # restarts and changes go invisible until the operator hard-refreshes.
    return HTMLResponse(
        _version_static_assets(
            source_path.read_text(encoding="utf-8"), source_path.parent
        ),
        headers=NO_CACHE_HEADERS,
    )


# ---------------------------------------------------------------------------
# Workflow Designer API  (JSON file persistence)
# ---------------------------------------------------------------------------

_workflow_storage = None


def _get_workflow_storage():
    global _workflow_storage
    if _workflow_storage is None:
        from pybravo.workflow.storage import WorkflowStorage
        _workflow_storage = WorkflowStorage()
    return _workflow_storage


@app.get("/api/workflows", tags=["Designer"])
async def list_designer_workflows():
    """List all saved designer workflows."""
    return {"workflows": _get_workflow_storage().list_workflows()}


@app.get("/api/workflows/{workflow_id}", tags=["Designer"])
async def get_designer_workflow(workflow_id: str):
    """Load a designer workflow by ID."""
    data = _get_workflow_storage().get_workflow(workflow_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return data


@app.post("/api/workflows", tags=["Designer"])
async def create_designer_workflow(request: Request):
    """Create a new designer workflow."""
    body = await request.json()
    return _get_workflow_storage().create_workflow(body)


@app.put("/api/workflows/{workflow_id}", tags=["Designer"])
async def update_designer_workflow(workflow_id: str, request: Request):
    """Update an existing designer workflow."""
    body = await request.json()
    result = _get_workflow_storage().update_workflow(workflow_id, body)
    if result is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return result


@app.delete("/api/workflows/{workflow_id}", tags=["Designer"])
async def delete_designer_workflow(workflow_id: str):
    """Delete a designer workflow."""
    if not _get_workflow_storage().delete_workflow(workflow_id):
        raise HTTPException(status_code=404, detail="Workflow not found")
    return {"status": "deleted"}


@app.post("/api/workflows/import-json", tags=["Designer"])
async def import_designer_workflow(file: UploadFile = File(...)):
    """Import a designer workflow from a JSON file."""
    content = await file.read()
    try:
        return _get_workflow_storage().import_workflow(content)
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/workflows/{workflow_id}/export", tags=["Designer"])
async def export_designer_workflow(workflow_id: str):
    """Export a designer workflow as a downloadable JSON file."""
    raw = _get_workflow_storage().export_workflow(workflow_id)
    if raw is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    from starlette.responses import Response
    return Response(
        content=raw,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{workflow_id}.json"'},
    )


_active_workflow_executor = None


def _designer_runtime_snapshot(bravo: Bravo | None) -> dict[str, Any]:
    if bravo is None:
        return {}
    state = bravo.get_state()
    return {
        "head_type": state.get("head_type"),
        "head_mode": state.get("head_mode"),
        "tip_selection": state.get("tip_selection"),
        "plate_selection": state.get("plate_selection"),
        "tips_on_head": state.get("tips_on_head"),
        "tips_on_head_mode": state.get("tips_on_head_mode"),
        "tips_on_head_selection": state.get("tips_on_head_selection"),
        "tip_labware": state.get("tip_labware"),
        "tip_definition_id": state.get("tip_definition_id"),
        "attached_tip_length_mm": state.get("attached_tip_length_mm"),
        "active_tip_capacity_ul": state.get("active_tip_capacity_ul"),
    }


_LIQUID_NODE_TYPES = {"liquid/Aspirate", "liquid/Dispense", "liquid/Mix"}


def _validate_workflow_liquid_classes(graph_data: dict, bravo: Bravo) -> list[dict[str, Any]]:
    """Walk the workflow graph and pre-resolve every referenced liquid class
    and pipette technique against the current Bravo context.

    Returns a list of error descriptors — one per node whose `liquid_class`
    or `pipette_technique` property does not resolve. Empty list means the
    workflow is safe to launch.
    """
    errors: list[dict[str, Any]] = []
    context = _active_liquid_context(bravo)
    machine_id = context["machine_id"]
    head_type = context["head_type"]

    # NOTE: we intentionally do NOT filter by tip_id / tip_capacity_ul here.
    # A workflow may use multiple tip types across its nodes (e.g. pick up
    # 10 uL tips, aspirate with D10, later pick up 30 uL tips, aspirate
    # with D30).  Validation only checks that the liquid class EXISTS for
    # this machine + head — the tip context is verified at run-time when the
    # node actually executes and the correct tips are equipped.
    all_classes = liquid_classes_store.list_liquid_classes(
        machine_id=machine_id,
        head_type=head_type,
        tip_id=None,
        tip_capacity_ul=None,
    )
    all_class_names = {str(lc.get("name") or "") for lc in all_classes}

    nodes = graph_data.get("nodes") or []
    for node in nodes:
        node_type = node.get("type") or ""
        if node_type not in _LIQUID_NODE_TYPES:
            continue
        props = node.get("properties") or {}
        node_id = node.get("id")
        node_title = node.get("title") or node_type

        lc_name = props.get("liquid_class")
        if lc_name:
            if str(lc_name) not in all_class_names:
                errors.append({
                    "node_id": node_id,
                    "node_type": node_type,
                    "node_title": node_title,
                    "field": "liquid_class",
                    "value": lc_name,
                    "reason": (
                        f"No liquid class named '{lc_name}' for "
                        f"{machine_id} / {head_type}."
                    ),
                })

        pt_name = props.get("pipette_technique")
        if pt_name:
            hit = liquid_classes_store.get_pipette_technique(str(pt_name))
            if hit is None:
                errors.append({
                    "node_id": node_id,
                    "node_type": node_type,
                    "node_title": node_title,
                    "field": "pipette_technique",
                    "value": pt_name,
                    "reason": f"No pipette technique named '{pt_name}'.",
                })
    return errors


async def _run_designer_workflow(workflow_id: str, *, mode: str) -> dict:
    """Shared implementation for both simulate and execute endpoints.

    mode="simulate": builds a fresh simulation-mode Bravo that reuses the
    active profile (so calibrated teachpoints drive the 3D viewport), and
    applies a snapshot of the live runtime selection state so dense-plate /
    subset geometry matches the main UI.

    mode="execute": dispatches against the live `_bravo` so every task call
    hits real hardware. The runtime snapshot is skipped (live bravo already
    carries its own state).
    """
    global _active_workflow_executor
    from pybravo.workflow.executor import WorkflowExecutor

    data = _get_workflow_storage().get_workflow(workflow_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Workflow not found")

    if mode == "execute":
        if _bravo is None:
            raise HTTPException(status_code=409, detail="No active Bravo — connect before executing")
        if not _bravo.is_connected:
            raise HTTPException(
                status_code=409,
                detail="Bravo is not connected — click Connect then Initialize before executing.",
            )
        controller_type = getattr(_bravo._profile.connection, "controller_type", "")
        if controller_type == "simulation":
            raise HTTPException(
                status_code=409,
                detail=(
                    "Bravo is configured for the simulation controller — change the "
                    "profile's controller_type to 'agile' or 'darwin_native' and reconnect "
                    "before executing on hardware."
                ),
            )
        # Warm up the controller before the first task so the initial
        # state-machine bootstrap is not charged to the first node.  This
        # is a no-op if the user already clicked Initialize.
        if not getattr(_bravo, "_initialized", False):
            try:
                await _bravo.initialize()
            except Exception as exc:
                raise HTTPException(
                    status_code=409,
                    detail=f"Bravo initialize() failed: {exc}",
                ) from exc
        target_bravo = _bravo
        runtime_snapshot = None
    else:
        runtime_snapshot = _designer_runtime_snapshot(_bravo)
        if _bravo is not None:
            target_bravo = Bravo(profile=_bravo.profile, mode="simulation")
        else:
            target_bravo = Bravo(mode="simulation")

    graph_data = data.get("graph", {})
    deck_config = data.get("deck", {})

    # Pre-flight validation: catch stale liquid-class / pipette-technique
    # references before any motion. Validating in simulate mode too means
    # users get caught early in designer iteration.
    validation_bravo = target_bravo if mode == "execute" else _bravo
    logger.info(
        "Running workflow pre-flight validation (mode=%s, bravo=%s)",
        mode, "present" if validation_bravo is not None else "absent",
    )
    if validation_bravo is not None:
        invalid_nodes = _validate_workflow_liquid_classes(graph_data, validation_bravo)
        logger.info("Pre-flight validation found %d invalid liquid references", len(invalid_nodes))
        if invalid_nodes:
            summary = ", ".join(
                f"{item['node_title']} ({item['field']}='{item['value']}')"
                for item in invalid_nodes
            )
            raise HTTPException(
                status_code=400,
                detail={
                    "message": (
                        f"Workflow has {len(invalid_nodes)} invalid reference(s) "
                        f"for the current tip/head context: {summary}. "
                        "Open each node and pick a valid class, or switch tips."
                    ),
                    "invalid_nodes": invalid_nodes,
                },
            )

    async def broadcast_event(event):
        await ws_manager.broadcast(event)

    executor = WorkflowExecutor(
        target_bravo,
        graph_data,
        deck_config=deck_config,
        on_event=broadcast_event,
        runtime_state=runtime_snapshot,
        preview_animation=(mode != "execute"),
        library_src=data.get("library", "") or "",
    )
    _active_workflow_executor = executor

    async def run():
        global _active_workflow_executor
        try:
            await executor.execute()
        finally:
            _active_workflow_executor = None

    asyncio.ensure_future(run())
    return {"status": "started", "workflow_id": workflow_id, "mode": mode}


@app.post("/api/workflows/{workflow_id}/simulate", tags=["Designer"])
async def simulate_designer_workflow(workflow_id: str):
    """Run a workflow in simulation mode (no hardware)."""
    return await _run_designer_workflow(workflow_id, mode="simulate")


@app.post("/api/workflows/{workflow_id}/execute", tags=["Designer"])
async def execute_designer_workflow(workflow_id: str):
    """Run a workflow against the live Bravo hardware."""
    return await _run_designer_workflow(workflow_id, mode="execute")


@app.post("/api/workflows/stop", tags=["Designer"])
async def stop_designer_workflow():
    """Abort a running workflow simulation or execution."""
    global _active_workflow_executor
    if _active_workflow_executor:
        _active_workflow_executor.abort()
        _active_workflow_executor = None
        return {"status": "stopped"}
    return {"status": "no_workflow_running"}


class ScriptActionRequest(BaseModel):
    """Operator response to a script-error modal."""
    action: str  # "retry" | "edit_retry" | "abort"
    new_source: str = ""  # updated script text for "edit_retry"


@app.post("/api/script_action", tags=["Designer"])
async def script_action(req: ScriptActionRequest):
    """Resolve a paused script-error prompt (retry / edit & retry / abort)."""
    global _active_workflow_executor
    if not _active_workflow_executor:
        return {"accepted": False, "reason": "no_workflow_running"}
    accepted = _active_workflow_executor.resolve_script_error(
        req.action, req.new_source,
    )
    return {"accepted": accepted, "action": req.action}


class UserPromptResponse(BaseModel):
    """Operator's response to a Script-driven prompt_user() modal."""
    request_id: str
    value: str = ""
    cancelled: bool = False


@app.get("/api/script_snippets", tags=["Designer"])
async def get_script_snippets():
    """Return the script-snippet registry used by the designer's Script
    editor "Insert snippet..." menu and the "Ask Operator" task chip.

    The UI is purely a view of whatever the registry contains, so adding a
    new snippet to pybravo/workflow/script_snippets.py automatically makes
    it show up in the editor dropdown on next page load.
    """
    from pybravo.workflow.script_snippets import get_snippets
    return {"snippets": get_snippets()}


@app.post("/api/user_prompt_response", tags=["Designer"])
async def user_prompt_response(req: UserPromptResponse):
    """Resolve a paused prompt_user() call in a Script node.

    The Script's `prompt_user(...)` blocks the sandbox thread on a Future
    keyed by `request_id`. Setting that Future (success or cancel) unblocks
    the script and returns the typed value — or raises OperatorCancelled
    which falls through to the standard script-error pause UI.
    """
    global _active_workflow_executor
    if not _active_workflow_executor:
        return {"accepted": False, "reason": "no_workflow_running"}
    accepted = _active_workflow_executor.resolve_user_prompt(
        req.request_id, req.value, req.cancelled,
    )
    return {"accepted": accepted, "request_id": req.request_id}


class WorkflowDraftRequest(BaseModel):
    """Natural-language description + optional deck context for the drafter."""

    prompt: str
    deck: dict[str, Any] | None = None  # live deckConfig from the active designer tab


@app.post("/api/workflow/draft", tags=["Designer"])
async def workflow_draft(req: WorkflowDraftRequest):
    """Generate a draft workflow JSON from a natural-language prompt.

    The designer POSTs the current tab's deck (when "Use current deck"
    is checked) so the LLM can reuse exact labware_ids. Returns a
    payload shaped for direct deserialization into a new designer tab:

        {"workflow": <DraftedWorkflow.to_designer_json()>,
         "warnings": ["…"], "errors": ["…"],
         "meta": {"provider": "...", "model": "...", "attempts": N}}
    """
    from pybravo.workflow.drafter import (
        LLMDrafterError,
        MissingLLMDependencyError,
        draft_workflow,
    )
    from pybravo.workflow.drafter.llm import NoLLMCredentialsError

    if not req.prompt or not req.prompt.strip():
        raise HTTPException(status_code=400, detail="`prompt` must not be empty.")

    try:
        result = await draft_workflow(req.prompt.strip(), current_deck=req.deck)
    except MissingLLMDependencyError as exc:
        # 501 — feature not installed on this server.
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except NoLLMCredentialsError as exc:
        # 503 — server is up but can't reach any LLM provider.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except LLMDrafterError as exc:
        # 502 — upstream LLM call failed (network, auth, context length, etc.).
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # Record the as-drafted snapshot. Every draft gets a session_id
    # regardless of source (NL / PDF) so the training-data schema is
    # uniform; "how did the user edit this" works the same whether the
    # draft came from text or a paper.
    from pybravo.workflow.drafter import store as _dstore
    payload = result.designer_payload()
    session_id = _dstore.new_session_id()
    payload["session_id"] = session_id
    try:
        _dstore.record_draft(
            session_id=session_id,
            pdf_hash=None,
            source_file="",
            drafted_workflow=payload["workflow"],
            provider=result.provider,
            model=result.model,
            attempts=result.attempts,
            prompt=req.prompt.strip(),
            selected_paragraph_ids=[],
            warnings=payload.get("warnings") or [],
            errors=payload.get("errors") or [],
        )
        # No picker for NL drafts — synthesize a "selection" event
        # so downstream analytics get a uniform stream.
        _dstore.record_protocol_selection(
            session_id=session_id,
            pdf_hash="",
            source_file="",
            candidates_presented=[],
            selected_candidate_idx=None,
            selected_paragraph_ids_final=[],
            user_action="nl_prompt",
        )
    except Exception:  # noqa: BLE001
        logger.exception("drafter_record_draft_failed")
    return payload


@app.post("/api/workflow/parse_pdf", tags=["Designer"])
async def workflow_parse_pdf(file: UploadFile = File(...)):
    """Parse a scientific-paper PDF via the remote docling-serve instance.

    Phase 3 week 1 deliverable: returns the structured Markdown + a
    paragraph-id-keyed breakdown of the document (+ best-effort section
    labels). DOES NOT yet draft a workflow from it — that's the next
    sprint. Use this endpoint to sanity-check the docling-serve path
    end-to-end and inspect what the LLM will eventually see.

    Configuration: set `PYBRAVO_DOCLING_URL` in .env.

    Response shape::

        {
          "source_name": "paper.pdf",
          "page_count": 8,
          "markdown_preview": "...first 2000 chars...",
          "markdown_length": 45_123,
          "paragraph_count": 312,
          "sections_detected": {"abstract": 1, "methods": 47, "results": 89, ...},
          "methods_paragraphs": [
              {"id": "p-134", "text": "...", "page": 4},
              ...
          ]
        }
    """
    from pybravo.workflow.drafter import (
        DoclingServiceError,
        MissingDoclingConfigError,
        PaperParserError,
        parse_pdf_bytes,
    )

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=400,
            detail="Upload a .pdf file.",
        )
    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="Uploaded PDF is empty.")

    try:
        parsed = await parse_pdf_bytes(pdf_bytes, filename=file.filename)
    except MissingDoclingConfigError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except DoclingServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except PaperParserError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    # Section histogram for quick sanity check.
    section_counts: dict[str, int] = {}
    for p in parsed.paragraphs:
        section_counts[p.section or "(unlabeled)"] = section_counts.get(p.section or "(unlabeled)", 0) + 1

    methods_paragraphs = [
        {"id": p.paragraph_id, "text": p.text, "page": p.page_no, "kind": p.kind}
        for p in parsed.paragraphs
        if "methods" in (p.section or "").lower() or "experimental" in (p.section or "").lower()
    ]

    return {
        "source_name": parsed.source_name,
        "page_count": parsed.page_count,
        "markdown_preview": parsed.markdown[:2000],
        "markdown_length": len(parsed.markdown),
        "paragraph_count": len(parsed.paragraphs),
        "sections_detected": section_counts,
        "methods_paragraphs": methods_paragraphs,
    }


@app.post("/api/workflow/draft_from_pdf", tags=["Designer"])
async def workflow_draft_from_pdf(
    file: UploadFile = File(...),
    include_deck: bool = True,
):
    """Draft an pyBravo workflow from a scientific paper PDF.

    Phase 3 week 2 deliverable: end-to-end pipeline
    1. Docling on the DGX extracts structured paragraphs + section
       labels from the PDF.
    2. Pass 1 (LLM) reads the Methods paragraphs and emits a
       ``PaperFacts`` object — a flat list of grounded facts, each
       tagged with its source paragraph_id.
    3. Pass 2 (LLM) converts the facts into a DraftedWorkflow where
       every non-structural node carries a ``source_citation`` back
       to the fact + paragraph that spawned it.
    4. Validator checks citation coverage + all the usual graph /
       physical sanity rules.

    Response shape extends the /draft payload with a ``facts`` block
    and per-paragraph source excerpts, so the designer can render
    citation badges + side-panel excerpts on each drafted node.
    """
    from pybravo.workflow.drafter import (
        DoclingServiceError,
        LLMDrafterError,
        MissingDoclingConfigError,
        MissingLLMDependencyError,
        PaperParserError,
        draft_workflow_from_paper,
        parse_pdf_bytes,
    )
    from pybravo.workflow.drafter.llm import NoLLMCredentialsError

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Upload a .pdf file.")
    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="Uploaded PDF is empty.")

    # Content-address the PDF bytes so cache lookups work across
    # duplicate uploads. Persist to the filesystem cache now — this
    # is the ground-truth record of what the user analyzed, and we
    # need it later for the picker's PDF preview and for training-data
    # re-runs against updated prompts.
    from pybravo.workflow.drafter import store as _dstore
    pdf_hash = _dstore.store_pdf_bytes(pdf_bytes)

    # "Have we seen this paper before?" — lookup runs before we
    # decide whether to re-parse, so the response can tell the UI
    # how many times the user has drafted this exact PDF previously.
    history = _dstore.paper_upload_history(pdf_hash)

    # Cache hit on parsed Docling output? Skip the remote parse.
    cached = _dstore.get_parsed_paper(pdf_hash)
    if cached:
        from pybravo.workflow.drafter.paper_parser import ParsedPaper, ParsedParagraph
        parsed = ParsedPaper(
            markdown=cached.get("markdown", ""),
            paragraphs=[
                ParsedParagraph(
                    paragraph_id=p.get("paragraph_id", ""),
                    text=p.get("text", ""),
                    kind=p.get("kind", "paragraph"),
                    section=p.get("section", ""),
                    page_no=p.get("page_no"),
                    heading_level=p.get("heading_level"),
                ) for p in (cached.get("paragraphs") or [])
            ],
            raw_document=cached.get("raw_document") or {},
            page_count=int(cached.get("page_count") or 0),
            source_name=cached.get("source_file") or file.filename,
        )
        logger.info("drafter_parsed_paper_cache_hit", hash=pdf_hash)
    else:
        try:
            parsed = await parse_pdf_bytes(pdf_bytes, filename=file.filename)
        except MissingDoclingConfigError as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc
        except DoclingServiceError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except PaperParserError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        # Upsert into the parsed_papers cache so repeat uploads skip
        # Docling entirely. Fire-and-forget — a store failure doesn't
        # affect the draft flow.
        try:
            _dstore.put_parsed_paper(
                pdf_hash=pdf_hash,
                source_file=parsed.source_name,
                page_count=parsed.page_count,
                markdown=parsed.markdown,
                paragraphs=[
                    {
                        "paragraph_id": p.paragraph_id,
                        "text": p.text,
                        "kind": p.kind,
                        "section": p.section,
                        "page_no": p.page_no,
                        "heading_level": p.heading_level,
                    } for p in parsed.paragraphs
                ],
                raw_document=parsed.raw_document,
            )
        except Exception:  # noqa: BLE001
            logger.exception("drafter_parsed_paper_cache_write_failed")

    # Current deck context — optional, same treatment as /api/workflow/draft.
    deck_ctx = None
    # The /draft endpoint receives deck in the JSON body; here it's a
    # form field, so we don't have live designer state. A follow-up
    # sprint will add a second form field for current deck JSON — for
    # now Pass 2 runs without deck context.
    _ = include_deck

    try:
        facts, result = await draft_workflow_from_paper(parsed, current_deck=deck_ctx)
    except MissingLLMDependencyError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except NoLLMCredentialsError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except LLMDrafterError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # Enrich the designer payload with the facts list + paragraph
    # excerpts, keyed so the UI can light up citation badges without
    # a second round-trip.
    payload = result.designer_payload()
    payload["facts"] = [f.model_dump() for f in facts.facts]
    payload["summary"] = facts.summary

    # Excerpts for every cited paragraph (trimmed to 500 chars for
    # tooltip display; longer reads happen via a separate paragraph
    # lookup endpoint later if needed).
    cited_ids: set[str] = set()
    for n in result.workflow.graph.nodes:
        if n.source_citation and n.source_citation.paragraph_id:
            cited_ids.add(n.source_citation.paragraph_id)
    # Also cite paragraphs referenced by any fact, so the UI can render
    # "all facts" view with full context.
    for f in facts.facts:
        cited_ids.add(f.paragraph_id)

    payload["paragraph_excerpts"] = {
        pid: {
            "text": (parsed.paragraph(pid).text if parsed.paragraph(pid) else "")[:500],
            "page": parsed.paragraph(pid).page_no if parsed.paragraph(pid) else None,
            "section": parsed.paragraph(pid).section if parsed.paragraph(pid) else "",
        }
        for pid in cited_ids
    }
    payload["source_file"] = parsed.source_name
    payload["page_count"] = parsed.page_count

    # ── Drafter session bookkeeping ─────────────────────────────────
    # Every PDF draft gets a session_id attached to the workflow
    # forever (the designer writes it into the workflow JSON before
    # saving). Every subsequent Save / Save-As / Execute / Simulate
    # posts the current workflow back so we can diff it against this
    # frozen as-drafted snapshot and record the training signal.
    session_id = _dstore.new_session_id()
    payload["session_id"] = session_id
    payload["pdf_hash"] = pdf_hash
    # Surface "you've seen this paper before" so the UI can offer
    # "Open previous draft" instead of silently creating a dupe.
    payload["paper_history"] = history
    # The paragraphs that actually drove Pass 1 — useful for picker
    # retraining later. With the current "all methods" flow this is
    # the complete methods-ish paragraph list.
    methods_para_ids = [
        p.paragraph_id for p in parsed.paragraphs
        if p.section and any(
            k in p.section.lower() for k in ("method", "experimental", "procedure", "protocol")
        )
    ]
    try:
        _dstore.record_draft(
            session_id=session_id,
            pdf_hash=pdf_hash,
            source_file=parsed.source_name,
            drafted_workflow=payload["workflow"],
            provider=result.provider,
            model=result.model,
            attempts=result.attempts,
            prompt="",
            selected_paragraph_ids=methods_para_ids,
            warnings=payload.get("warnings") or [],
            errors=payload.get("errors") or [],
        )
        # Until the picker ships, synthesize an "all_methods" selection
        # event so the protocol_selections stream is uniform.
        _dstore.record_protocol_selection(
            session_id=session_id,
            pdf_hash=pdf_hash,
            source_file=parsed.source_name,
            candidates_presented=[],
            selected_candidate_idx=None,
            selected_paragraph_ids_final=methods_para_ids,
            user_action="all_methods",
        )
    except Exception:  # noqa: BLE001
        logger.exception("drafter_record_pdf_draft_failed")

    return payload


class DraftPatchRequest(BaseModel):
    """Payload the designer posts on Save / Save-As / Execute / Simulate
    for a drafted workflow tab. The server diffs it against the frozen
    as-drafted snapshot and records the delta as training data."""

    workflow: dict[str, Any]
    trigger:  str = "save"      # "save" | "save_as" | "execute" | "simulate"
    workflow_id_saved_as: str | None = None


@app.post("/api/workflow/draft/{session_id}/patch", tags=["Designer"])
async def workflow_draft_patch(session_id: str, req: DraftPatchRequest):
    """Record the user-edited workflow against the as-drafted snapshot.

    Called by the designer whenever a drafted tab is saved, duplicated,
    simulated, or executed. The server looks up the draft by
    ``session_id`` (attached to the workflow forever), computes a
    structural diff against ``drafted_workflow``, and upserts it onto
    ``workflow_drafts.final_workflow`` + ``.diff``.

    Returns the summary stats so the UI can optionally surface a
    "your edits" panel, but the primary purpose is side-effect only —
    a 200 with ``{"recorded": false}`` just means the session_id wasn't
    recognized (e.g. on a freshly imported workflow that predates the
    drafter).
    """
    from pybravo.workflow.drafter import store as _dstore
    from pybravo.workflow.drafter.diff import compute_workflow_diff

    draft = _dstore.get_draft(session_id)
    if not draft:
        # Not an error — could be an old workflow, or Mongo is down.
        return {"recorded": False, "reason": "unknown_session"}

    diff = compute_workflow_diff(draft.get("drafted_workflow") or {}, req.workflow or {})
    try:
        _dstore.update_draft_final(
            session_id=session_id,
            final_workflow=req.workflow,
            diff=diff,
            trigger=req.trigger or "save",
            workflow_id_saved_as=req.workflow_id_saved_as,
        )
    except Exception:  # noqa: BLE001
        logger.exception("drafter_patch_write_failed")
    return {"recorded": True, "diff_summary": diff.get("summary", {})}


@app.get("/api/drafter/status", tags=["Designer"])
async def drafter_status():
    """Diagnostic endpoint — shows whether the drafter's persistence
    layer is configured and reachable (Mongo + PDF cache + fallback
    JSONL). Handy when debugging "training data isn't showing up"."""
    from pybravo.workflow.drafter import store as _dstore
    return _dstore.status()


@app.get("/api/drafter/debug", tags=["Designer"])
async def drafter_debug():
    """Per-collection counts + sampled last rows + index list.

    Answers the question "did my last draft actually get written, and
    to which collections?" at a glance, without opening a mongo shell.
    Large fields (markdown, raw_document, drafted_workflow,
    final_workflow) are replaced with ``<type len=N>`` placeholders
    so the response stays under a few KB even for heavy papers.
    """
    from pybravo.workflow.drafter import store as _dstore
    return _dstore.debug_snapshot()


@app.get("/api/drafter/dashboard", tags=["Designer"])
async def drafter_dashboard_data(days: int = 30):
    """JSON backing the /drafter-dashboard HTML page.

    ``days`` controls the time-series window for the drafts_per_day
    chart only; the other aggregations look at the whole history so
    a 30-day default gives a useful activity snapshot while long-tail
    retention metrics remain cumulative.
    """
    from pybravo.workflow.drafter import store as _dstore
    return _dstore.dashboard_aggregates(days=max(1, min(days, 365)))


@app.get("/drafter-dashboard", response_class=HTMLResponse, include_in_schema=False)
async def drafter_dashboard_page():
    """Render the drafter telemetry dashboard."""
    source_path = Path(__file__).resolve().parents[2] / "frontend" / "drafter_dashboard.html"
    if not source_path.exists():
        raise HTTPException(status_code=404, detail="Dashboard page not found")
    return HTMLResponse(source_path.read_text(encoding="utf-8"))


@app.post("/api/workflow/segment_paper", tags=["Designer"])
async def workflow_segment_paper(
    file: UploadFile = File(...),
    refine_with_llm: bool = Form(True),
):
    """Pass 0: parse the uploaded PDF and return candidate protocols.

    The picker modal calls this first.  The response shape is::

        {
          "pdf_hash": "sha256...",
          "source_file": "PMC10716174.pdf",
          "page_count": 16,
          "paper_history": { ... see paper_upload_history() ... },
          "candidates": [ ProtocolCandidate, ... ],   # sorted by confidence
          "autoselect_idx": null | int,
          "notes": "",                                # empty on happy path
        }

    Caller then shows a picker to the user (or auto-selects when
    ``autoselect_idx`` is non-null) and calls ``/api/workflow/draft_from_analyzed``
    with the chosen candidate's paragraph ids.
    """
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Upload a .pdf file.")
    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="Uploaded PDF is empty.")

    from pybravo.workflow.drafter import (
        DoclingServiceError,
        MissingDoclingConfigError,
        PaperParserError,
        parse_pdf_bytes,
    )
    from pybravo.workflow.drafter import store as _dstore
    from pybravo.workflow.drafter.segmenter import autoselect_top, segment_paper

    pdf_hash = _dstore.store_pdf_bytes(pdf_bytes)
    history = _dstore.paper_upload_history(pdf_hash)

    # Parse (cache-aware)
    cached = _dstore.get_parsed_paper(pdf_hash)
    if cached:
        from pybravo.workflow.drafter.paper_parser import ParsedPaper, ParsedParagraph
        parsed = ParsedPaper(
            markdown=cached.get("markdown", ""),
            paragraphs=[
                ParsedParagraph(
                    paragraph_id=p.get("paragraph_id", ""),
                    text=p.get("text", ""),
                    kind=p.get("kind", "paragraph"),
                    section=p.get("section", ""),
                    page_no=p.get("page_no"),
                    heading_level=p.get("heading_level"),
                ) for p in (cached.get("paragraphs") or [])
            ],
            raw_document=cached.get("raw_document") or {},
            page_count=int(cached.get("page_count") or 0),
            source_name=cached.get("source_file") or file.filename,
        )
    else:
        try:
            parsed = await parse_pdf_bytes(pdf_bytes, filename=file.filename)
        except MissingDoclingConfigError as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc
        except DoclingServiceError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except PaperParserError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        try:
            _dstore.put_parsed_paper(
                pdf_hash=pdf_hash,
                source_file=parsed.source_name,
                page_count=parsed.page_count,
                markdown=parsed.markdown,
                paragraphs=[
                    {
                        "paragraph_id": p.paragraph_id, "text": p.text,
                        "kind": p.kind, "section": p.section,
                        "page_no": p.page_no, "heading_level": p.heading_level,
                    } for p in parsed.paragraphs
                ],
                raw_document=parsed.raw_document,
            )
        except Exception:  # noqa: BLE001
            logger.exception("drafter_parsed_paper_cache_write_failed")

    try:
        protocols = await segment_paper(parsed, refine_with_llm=refine_with_llm)
    except Exception as exc:
        logger.exception("drafter_segment_paper_failed")
        raise HTTPException(status_code=500, detail=f"Segmenter failed: {exc}") from exc

    auto_idx = autoselect_top(protocols.candidates)
    return {
        "pdf_hash":       pdf_hash,
        "source_file":    parsed.source_name,
        "page_count":     parsed.page_count,
        "paper_history":  history,
        "candidates":     [c.model_dump() for c in protocols.candidates],
        "autoselect_idx": auto_idx,
        "notes":          protocols.notes,
    }


class DraftFromAnalyzedRequest(BaseModel):
    """Pass 1 + 2 on a previously-analyzed paper, restricted to the
    paragraphs the user picked in the modal."""

    pdf_hash: str
    selected_paragraph_ids: list[str]
    selected_candidate_title: str = ""   # for bookkeeping only
    candidates_presented: list[dict[str, Any]] | None = None  # picker telemetry
    deck: dict[str, Any] | None = None
    time_on_picker_s: float | None = None


@app.post("/api/workflow/draft_from_analyzed", tags=["Designer"])
async def workflow_draft_from_analyzed(req: DraftFromAnalyzedRequest):
    """Run Pass 1 + Pass 2 on a user-picked subset of a paper's paragraphs.

    Requires a prior ``/api/workflow/segment_paper`` call so the paper
    is already parsed and cached.  Returns the same designer payload
    shape as ``/api/workflow/draft_from_pdf`` but only uses the
    selected paragraphs as Pass 1 input — the rest of the paper is
    ignored.
    """
    from pybravo.workflow.drafter import (
        LLMDrafterError,
        MissingLLMDependencyError,
        ParsedPaper,
        ParsedParagraph,
        draft_workflow_from_paper,
    )
    from pybravo.workflow.drafter import store as _dstore
    from pybravo.workflow.drafter.llm import NoLLMCredentialsError

    cached = _dstore.get_parsed_paper(req.pdf_hash)
    if not cached:
        raise HTTPException(
            status_code=404,
            detail="PDF not analyzed yet. Call /api/workflow/segment_paper first.",
        )

    # Rebuild the ParsedPaper, but KEEP only the selected paragraphs
    # in the paragraphs list. draft_workflow_from_paper filters to
    # methods-like sections internally; by pre-filtering we force it
    # to use exactly what the picker chose.
    selected_ids = set(req.selected_paragraph_ids or [])
    all_paras = [
        ParsedParagraph(
            paragraph_id=p.get("paragraph_id", ""),
            text=p.get("text", ""),
            kind=p.get("kind", "paragraph"),
            # Tag the selected paragraphs with a synthetic section so
            # extract_facts' substring filter ('method' etc.) picks
            # them up regardless of the paper's original labels. This
            # keeps the picker authoritative over the segmenter's
            # automatic section detection.
            section=("methods (user-selected)" if p.get("paragraph_id") in selected_ids else (p.get("section") or "")),
            page_no=p.get("page_no"),
            heading_level=p.get("heading_level"),
        ) for p in (cached.get("paragraphs") or [])
    ]
    parsed = ParsedPaper(
        markdown=cached.get("markdown", ""),
        paragraphs=all_paras,
        raw_document=cached.get("raw_document") or {},
        page_count=int(cached.get("page_count") or 0),
        source_name=cached.get("source_file") or "paper.pdf",
    )

    try:
        facts, result = await draft_workflow_from_paper(parsed, current_deck=req.deck)
    except MissingLLMDependencyError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except NoLLMCredentialsError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except LLMDrafterError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    payload = result.designer_payload()
    payload["facts"] = [f.model_dump() for f in facts.facts]
    payload["summary"] = facts.summary

    cited_ids: set[str] = set()
    for n in result.workflow.graph.nodes:
        if n.source_citation and n.source_citation.paragraph_id:
            cited_ids.add(n.source_citation.paragraph_id)
    for f in facts.facts:
        cited_ids.add(f.paragraph_id)

    payload["paragraph_excerpts"] = {
        pid: {
            "text": (parsed.paragraph(pid).text if parsed.paragraph(pid) else "")[:500],
            "page": parsed.paragraph(pid).page_no if parsed.paragraph(pid) else None,
            "section": parsed.paragraph(pid).section if parsed.paragraph(pid) else "",
        }
        for pid in cited_ids
    }
    payload["source_file"] = parsed.source_name
    payload["page_count"] = parsed.page_count
    payload["pdf_hash"] = req.pdf_hash

    # ── Drafter session bookkeeping ─────────────────────────────────
    session_id = _dstore.new_session_id()
    payload["session_id"] = session_id

    try:
        _dstore.record_draft(
            session_id=session_id,
            pdf_hash=req.pdf_hash,
            source_file=parsed.source_name,
            drafted_workflow=payload["workflow"],
            provider=result.provider,
            model=result.model,
            attempts=result.attempts,
            prompt="",
            selected_paragraph_ids=list(req.selected_paragraph_ids or []),
            warnings=payload.get("warnings") or [],
            errors=payload.get("errors") or [],
        )
        # Picker-driven selection event — different user_action from the
        # legacy "all_methods" fall-through so the dashboards can
        # distinguish the two regimes.
        #
        # Resolve the picked rank from the payload: match either by
        # title (exact) or by paragraph-ids set equality, because the
        # frontend may send one, the other, or both. We need the
        # numeric rank in the selections doc so the dashboard's
        # picker_rank_picked chart has something to group on.
        resolved_idx: int | None = None
        wanted_pids = set(req.selected_paragraph_ids or [])
        for idx, c in enumerate(req.candidates_presented or []):
            cand_title = (c.get("title") if isinstance(c, dict) else "") or ""
            cand_pids  = set((c.get("paragraph_ids") if isinstance(c, dict) else []) or [])
            if req.selected_candidate_title and cand_title == req.selected_candidate_title:
                resolved_idx = idx
                break
            if wanted_pids and cand_pids and wanted_pids == cand_pids:
                resolved_idx = idx
                break
        # user_action marks legacy escape-hatch submissions differently
        # so the dashboard's "how many users fell back?" query is trivial.
        picker_action = "picker_pick" if resolved_idx is not None else "picker_draft_all"
        _dstore.record_protocol_selection(
            session_id=session_id,
            pdf_hash=req.pdf_hash,
            source_file=parsed.source_name,
            candidates_presented=list(req.candidates_presented or []),
            selected_candidate_idx=resolved_idx,
            selected_paragraph_ids_final=list(req.selected_paragraph_ids or []),
            user_action=picker_action,
            time_on_picker_s=req.time_on_picker_s,
        )
    except Exception:  # noqa: BLE001
        logger.exception("drafter_record_picker_draft_failed")

    return payload


@app.get("/api/drafter/paper/{pdf_hash}/page/{page_no}.png", tags=["Designer"])
async def drafter_paper_page_png(pdf_hash: str, page_no: int, scale: float = 1.5):
    """Serve one PDF page as PNG.

    Path params:
      * ``pdf_hash`` — SHA-256 of the PDF bytes (from segment_paper).
      * ``page_no``  — 1-indexed page number.

    Query:
      * ``scale`` — render scale factor (default 1.5 = 144 DPI-ish).

    Caches PNGs next to the PDF (``<hash>.pages/<n>@<scale*100>.png``),
    so the first request is ~80ms and every subsequent request is a
    file read.
    """
    from pybravo.workflow.drafter.pdf_render import render_page
    png = render_page(pdf_hash, page_no, scale=scale)
    if png is None:
        raise HTTPException(
            status_code=404,
            detail="PDF not cached or page out of range (was the PDF uploaded via /segment_paper?).",
        )
    return Response(content=png, media_type="image/png")


@app.get("/api/drafter/paper/{pdf_hash}/paragraphs", tags=["Designer"])
async def drafter_paper_paragraphs(pdf_hash: str):
    """Return the full paragraph list for a previously-analyzed PDF.

    Used by the picker modal to show body text + page numbers for
    each candidate's paragraphs without a re-parse.
    """
    from pybravo.workflow.drafter import store as _dstore
    cached = _dstore.get_parsed_paper(pdf_hash)
    if not cached:
        raise HTTPException(status_code=404, detail="PDF not cached.")
    return {
        "pdf_hash":   pdf_hash,
        "source_file": cached.get("source_file", ""),
        "page_count": cached.get("page_count", 0),
        "paragraphs": cached.get("paragraphs") or [],
    }


@app.post("/api/workflow/import", tags=["Workflow"])
async def workflow_import(file: UploadFile = File(...)):
    """Import a legacy .pro protocol file and return the parsed workflow."""
    import tempfile

    from pybravo.workflow.legacy_protocol_import import import_pro

    content = await file.read()
    with tempfile.NamedTemporaryFile(suffix=".pro", delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        workflow = import_pro(tmp_path)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to parse .pro file: {exc}")
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    return {"workflow": workflow}


@app.get("/vision-calibration", response_class=HTMLResponse, include_in_schema=False)
async def vision_calibration_page():
    source_path = Path(__file__).resolve().parents[2] / "frontend" / "vision_calibration.html"
    if not source_path.exists():
        raise HTTPException(status_code=404, detail="Vision calibration page not found")
    return HTMLResponse(source_path.read_text(encoding="utf-8"))


@app.put("/api/deck/{location}/labware", **_route_meta("Deck", "Assign labware to a deck location", DECK_DOC))
async def set_deck_labware(location: int, req: DeckLabwareRequest):
    bravo = get_bravo()
    try:
        labware = bravo.set_labware(
            location,
            req.labware_id,
            is_lidded=req.is_lidded,
            is_sealed=req.is_sealed,
            tip_definition_id=req.tip_definition_id,
            tipbox_fill_state=req.tipbox_fill_state,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "status": "assigned",
        "location": location,
        "labware": labware.metadata or {"name": labware.name},
    }


@app.delete("/api/deck/{location}/labware", **_route_meta("Deck", "Clear labware from a deck location", DECK_DOC))
async def clear_deck_labware(location: int):
    bravo = get_bravo()
    try:
        bravo.clear_labware(location)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "cleared", "location": location}


# -- REST endpoints: Profile --

@app.get("/api/profiles", **_route_meta("Profiles", "List available profiles and the active profile", PROFILE_DOC))
async def list_profiles():
    """List available profile names (YAML files in profile directory) and current profile."""
    if _profile_dir is None or not _profile_dir.is_dir():
        return {"profiles": [], "current": None}
    profiles = sorted(
        p.stem for p in _profile_dir.glob("*.yaml") if p.is_file()
    )
    current = _profile_path.stem if _profile_path is not None else None
    return {"profiles": profiles, "current": current}


@app.get("/api/profile", **_route_meta("Profiles", "Get the active profile", PROFILE_DOC))
async def get_profile():
    bravo = get_bravo()
    prof = bravo.profile
    return {
        "name": prof.name,
        "connection": {
            "controller_type": prof.connection.controller_type,
            "use_ethernet": prof.connection.use_ethernet,
            "address": prof.connection.address,
            "serial_port": prof.connection.serial_port,
            "machine_id": prof.connection.machine_id,
        },
        "head": {
            "head_type": prof.head.head_type.name,
            "check_on_init": prof.head.check_on_init,
            "default_tip_id": prof.head.default_tip_id,
            "teach_tip_id": prof.head.teach_tip_id,
            "default_tip_capacity": prof.head.default_tip_capacity,
            "teach_tip_capacity": prof.head.teach_tip_capacity,
            "teach_tip_length_mm": prof.head.teach_tip_length_mm,
            "teach_tip_options": serialize_tip_options_for_head(prof.head.head_type),
        },
        "gripper": {
            "y_offset": prof.gripper.y_offset,
            "pad_zg_reference_mm": prof.gripper.pad_zg_reference_mm,
            "pad_reference_tip_length_mm": prof.gripper.pad_reference_tip_length_mm,
        },
        "safety": {
            "approach_height": prof.safety.approach_height,
            "z_safe_position": prof.safety.z_safe_position,
            "always_move_to_safe_z": prof.safety.always_move_to_safe_z,
            "run_medium_speed": prof.safety.run_medium_speed,
            "prompt_home_w": prof.safety.prompt_home_w,
            "ignore_plate_sensor": prof.safety.ignore_plate_sensor,
            "enable_tips_off_tip_touch": prof.safety.enable_tips_off_tip_touch,
            "is_srt": prof.safety.is_srt,
        },
        "vision": {
            "enabled": prof.vision.enabled,
            "service_url": prof.vision.service_url,
            "sdk_root": prof.vision.sdk_root,
        },
        "accessories": prof.accessories.to_dict(),
    }

@app.patch("/api/profile", **_route_meta("Profiles", "Update the active profile", PROFILE_DOC))
async def update_profile(req: ProfileUpdateRequest):
    global _profile_path
    bravo = get_bravo()
    prof = bravo.profile
    if req.approach_height is not None:
        prof.safety.approach_height = req.approach_height
    if req.z_safe_position is not None:
        prof.safety.z_safe_position = req.z_safe_position
    if req.always_safe_z is not None:
        prof.safety.always_move_to_safe_z = req.always_safe_z
    if req.run_medium_speed is not None:
        prof.safety.run_medium_speed = req.run_medium_speed
    if req.prompt_home_w is not None:
        prof.safety.prompt_home_w = req.prompt_home_w
    if req.ignore_plate_sensor is not None:
        prof.safety.ignore_plate_sensor = req.ignore_plate_sensor
    if req.enable_tips_off_tip_touch is not None:
        prof.safety.enable_tips_off_tip_touch = req.enable_tips_off_tip_touch
    if req.is_srt is not None:
        prof.safety.is_srt = req.is_srt
    if req.controller_type is not None:
        prof.connection.controller_type = req.controller_type
    if req.use_ethernet is not None:
        prof.connection.use_ethernet = req.use_ethernet
    if req.serial_port is not None:
        prof.connection.serial_port = req.serial_port
    if req.address is not None:
        prof.connection.address = req.address
    if hasattr(req, "machine_id") and req.machine_id is not None:
        prof.connection.machine_id = req.machine_id
    if req.vision_enabled is not None:
        prof.vision.enabled = req.vision_enabled
    if req.vision_service_url is not None:
        prof.vision.service_url = req.vision_service_url
    if req.vision_sdk_root is not None:
        prof.vision.sdk_root = req.vision_sdk_root
    accessories_changed = False
    if req.accessories is not None:
        from pybravo.profile.profile import AccessoriesConfig

        prof.accessories = AccessoriesConfig.from_dict(req.accessories)
        accessories_changed = True
    if req.barcode_reader_enabled is not None:
        prof.accessories.barcode_reader.enabled = req.barcode_reader_enabled
    if req.barcode_reader_device_type is not None:
        prof.accessories.barcode_reader.device_type = req.barcode_reader_device_type
    if req.barcode_reader_port is not None:
        prof.accessories.barcode_reader.port = req.barcode_reader_port
    if req.barcode_reader_side is not None:
        prof.accessories.barcode_reader.side = req.barcode_reader_side
    if req.barcode_reader_location is not None:
        prof.accessories.barcode_reader.location = req.barcode_reader_location
    legacy_barcode_changed = any(
        v is not None
        for v in [
            req.barcode_reader_enabled,
            req.barcode_reader_device_type,
            req.barcode_reader_port,
            req.barcode_reader_side,
            req.barcode_reader_location,
        ]
    )
    if legacy_barcode_changed:
        prof.accessories.upsert_barcode_reader_device()
        accessories_changed = True
    if accessories_changed:
        bravo.reinit_accessories()
    if req.head_type is not None:
        try:
            prof.head.head_type = HeadType[req.head_type]
            prof.head.default_tip_id = get_default_tip_id_for_head(prof.head.head_type)
            prof.head.teach_tip_id = prof.head.default_tip_id
        except KeyError:
            logger.warning("Unknown head type: %s", req.head_type)
    if req.check_on_init is not None:
        prof.head.check_on_init = req.check_on_init
    if req.teach_tip_id is not None:
        prof.head.teach_tip_id = req.teach_tip_id
        prof.head.teach_tip_capacity = get_tip_capacity_ul(prof.head.head_type, req.teach_tip_id)
    if req.teach_tip_capacity is not None:
        prof.head.teach_tip_capacity = req.teach_tip_capacity
        prof.head.teach_tip_id = get_tip_id_for_capacity(prof.head.head_type, req.teach_tip_capacity) or prof.head.teach_tip_id
    if not getattr(prof.head, "default_tip_id", None):
        prof.head.default_tip_id = get_default_tip_id_for_head(prof.head.head_type)
    if not getattr(prof.head, "teach_tip_id", None):
        prof.head.teach_tip_id = prof.head.default_tip_id
    prof.head.default_tip_capacity = get_tip_capacity_ul(prof.head.head_type, prof.head.default_tip_id or prof.head.default_tip_capacity)
    prof.head.teach_tip_capacity = get_tip_capacity_ul(prof.head.head_type, prof.head.teach_tip_id or prof.head.teach_tip_capacity)
    prof.head.teach_tip_length_mm = get_tip_length_mm(
        prof.head.head_type,
        prof.head.teach_tip_id or prof.head.teach_tip_capacity,
    )
    saved = False
    if _profile_path is not None:
        try:
            prof.save(_profile_path)
            saved = True
        except OSError as e:
            logger.warning("Failed to save profile to %s: %s", _profile_path, e)
    return {"status": "updated", "saved": saved}


@app.post("/api/profile/load", **_route_meta("Profiles", "Load a profile from disk", PROFILE_DOC))
async def load_profile(req: ProfileLoadRequest):
    """Load a profile from disk by name. Requires disconnect first if connected."""
    global _profile_path
    bravo = get_bravo()
    if bravo.is_connected:
        raise HTTPException(
            status_code=409,
            detail="Disconnect before switching profile",
        )
    if _profile_dir is None or not _profile_dir.is_dir():
        raise HTTPException(
            status_code=400,
            detail="Profile directory not available",
        )
    path = _profile_dir / f"{req.name}.yaml"
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Profile '{req.name}' not found",
        )
    from pybravo.deck.teachpoints import Teachpoints
    from pybravo.profile.profile import BravoProfile

    loaded = BravoProfile.load(path)
    bravo._profile = loaded
    bravo._teachpoints = (
        loaded.teachpoints
        if loaded.teachpoints
        else Teachpoints()
    )
    if not loaded.teachpoints:
        bravo._teachpoints.set_default_teachpoints(loaded.head.head_type)
    bravo.reinit_accessories()
    bravo.set_head_mode("all_barrels", "back_left")
    _profile_path = path
    if _profile_dir is not None:
        _write_active_profile(_profile_dir, req.name)
    logger.info("Loaded profile %s from %s", req.name, path)
    return {"status": "loaded", "name": req.name}


def _validate_profile_name(name: str) -> str:
    """Reject names containing path separators, '..', or empty/whitespace.
    Returns the cleaned name (stripped). Raises HTTPException on rejection."""
    cleaned = (name or "").strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Profile name cannot be empty")
    if cleaned != Path(cleaned).name or cleaned in (".", "..") or "/" in cleaned or "\\" in cleaned:
        raise HTTPException(status_code=400, detail=f"Invalid profile name: {name!r}")
    return cleaned


@app.post("/api/profile/duplicate", **_route_meta("Profiles", "Duplicate a profile to a new name", PROFILE_DOC))
async def duplicate_profile(req: ProfileDuplicateRequest):
    """Copy ``source`` profile YAML to ``new_name``. If ``source`` omitted, the
    currently-active profile is duplicated. Does not change the active profile."""
    if _profile_dir is None or not _profile_dir.is_dir():
        raise HTTPException(status_code=400, detail="Profile directory not available")
    new_name = _validate_profile_name(req.new_name)
    if req.source:
        source_name = _validate_profile_name(req.source)
        source_path = _profile_dir / f"{source_name}.yaml"
    elif _profile_path is not None:
        source_path = _profile_path
    else:
        raise HTTPException(status_code=400, detail="No source profile specified and no active profile")
    if not source_path.exists():
        raise HTTPException(status_code=404, detail=f"Source profile not found: {source_path.stem}")
    dest_path = _profile_dir / f"{new_name}.yaml"
    if dest_path.exists():
        raise HTTPException(status_code=409, detail=f"Profile '{new_name}' already exists")
    import shutil
    shutil.copy2(source_path, dest_path)
    logger.info("Duplicated profile %s -> %s", source_path.stem, new_name)
    return {"status": "duplicated", "source": source_path.stem, "name": new_name}


@app.post("/api/profile/rename", **_route_meta("Profiles", "Rename a profile", PROFILE_DOC))
async def rename_profile(req: ProfileRenameRequest):
    """Rename ``old_name`` profile YAML to ``new_name``. If the renamed profile
    is the active one, disconnect first; the .active_profile marker and the
    in-memory profile path are updated."""
    global _profile_path
    if _profile_dir is None or not _profile_dir.is_dir():
        raise HTTPException(status_code=400, detail="Profile directory not available")
    old_name = _validate_profile_name(req.old_name)
    new_name = _validate_profile_name(req.new_name)
    if old_name == new_name:
        raise HTTPException(status_code=400, detail="New name is the same as old name")
    old_path = _profile_dir / f"{old_name}.yaml"
    new_path = _profile_dir / f"{new_name}.yaml"
    if not old_path.exists():
        raise HTTPException(status_code=404, detail=f"Profile '{old_name}' not found")
    if new_path.exists():
        raise HTTPException(status_code=409, detail=f"Profile '{new_name}' already exists")
    is_active = _profile_path is not None and _profile_path.resolve() == old_path.resolve()
    if is_active and get_bravo().is_connected:
        raise HTTPException(status_code=409, detail="Disconnect before renaming the active profile")
    old_path.rename(new_path)
    if is_active:
        _profile_path = new_path
        _write_active_profile(_profile_dir, new_name)
    logger.info("Renamed profile %s -> %s%s", old_name, new_name, " (active)" if is_active else "")
    return {"status": "renamed", "old_name": old_name, "new_name": new_name, "was_active": is_active}


@app.post("/api/profile/import_reg", **_route_meta("Profiles", "Import a legacy .reg profile export", PROFILE_DOC))
async def import_reg_profile(req: ProfileImportRegRequest):
    """Parse a Bravo2 ``.reg`` export and either preview it or save as a new
    profile. Returns ``warnings`` highlighting fields that need manual review
    (notably the registry head-type and tip-id enums, which we do not map)."""
    from pybravo.profile.reg_import import reg_to_profile

    try:
        profile, warnings = reg_to_profile(req.content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Failed to parse .reg payload")
        raise HTTPException(status_code=400, detail=f"Could not parse .reg: {exc}")

    payload: dict[str, object] = {
        "parsed_name": profile.name,
        "warnings": warnings,
        "axes": sorted(profile.axes.keys()),
        "teachpoint_locations": sorted(profile.teachpoints.locations) if profile.teachpoints else [],
    }

    if req.save_as is None:
        payload["status"] = "previewed"
        return payload

    if _profile_dir is None or not _profile_dir.is_dir():
        raise HTTPException(status_code=400, detail="Profile directory not available")
    save_name = _validate_profile_name(req.save_as)
    dest_path = _profile_dir / f"{save_name}.yaml"
    if dest_path.exists() and not req.overwrite:
        raise HTTPException(status_code=409, detail=f"Profile '{save_name}' already exists (set overwrite=true to replace)")
    profile.name = save_name
    try:
        profile.save(dest_path)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not save profile: {exc}")
    logger.info("Imported .reg -> %s (warnings: %d)", dest_path, len(warnings))
    payload["status"] = "saved"
    payload["name"] = save_name
    return payload


@app.post("/api/profile/import_dat", **_route_meta("Profiles", "Import a legacy Bravo2 .dat directory tree", PROFILE_DOC))
async def import_dat_profile(req: ProfileImportDatRequest):
    """Parse a legacy Bravo2 ``.dat`` directory export (pre-registry format).
    The top-level folder name is the profile name, and each subdirectory's
    ``<folder>/<folder>.dat`` file holds the registry-style key/value lines for
    that sub-key. The payload is converted to an equivalent .reg document and
    run through the standard importer, so mapping/warnings behave identically."""
    from pybravo.profile.reg_import import dat_tree_to_profile

    files_map: dict[str, str] = {f.relative_path: f.content for f in req.files}
    if not files_map:
        raise HTTPException(status_code=400, detail="No .dat files provided")

    try:
        profile, warnings = dat_tree_to_profile(req.profile_name, files_map)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Failed to parse .dat payload")
        raise HTTPException(status_code=400, detail=f"Could not parse .dat tree: {exc}")

    payload: dict[str, object] = {
        "parsed_name": profile.name,
        "warnings": warnings,
        "axes": sorted(profile.axes.keys()),
        "teachpoint_locations": sorted(profile.teachpoints.locations) if profile.teachpoints else [],
    }

    if req.save_as is None:
        payload["status"] = "previewed"
        return payload

    if _profile_dir is None or not _profile_dir.is_dir():
        raise HTTPException(status_code=400, detail="Profile directory not available")
    save_name = _validate_profile_name(req.save_as)
    dest_path = _profile_dir / f"{save_name}.yaml"
    if dest_path.exists() and not req.overwrite:
        raise HTTPException(status_code=409, detail=f"Profile '{save_name}' already exists (set overwrite=true to replace)")
    profile.name = save_name
    try:
        profile.save(dest_path)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not save profile: {exc}")
    logger.info("Imported .dat -> %s (warnings: %d)", dest_path, len(warnings))
    payload["status"] = "saved"
    payload["name"] = save_name
    return payload


# -- REST endpoints: Change Head & Device Discovery --

HEAD_TYPE_DISPLAY = {
    "HT_1536_PINTOOL": "1536 Pin tool",
    "HT_384_PINTOOL": "384 Pin tool",
    "HT_384_D_70": "384ST, 70 uL Series III",
    "HT_96_PINTOOL": "96 Pin tool",
    "HT_96_ASSAYMAP": "96AM",
    "HT_96_F_50": "96F, 50 uL",
    "HT_96_D_200": "96LT, 200 uL Series III",
    "HT_96_D_70": "96ST, 70 uL Series III",
}

@app.post("/api/change_head", **_route_meta("Head", "Change the configured head type", "Changes the configured liquid-handling or pin-tool head type and resets the active head mode to the default full-head configuration."))
async def change_head(req: ChangeHeadRequest):
    bravo = get_bravo()
    try:
        head_type = HeadType[req.head_type]
    except KeyError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown head type: {req.head_type}",
        ) from exc

    display = HEAD_TYPE_DISPLAY.get(head_type.name, head_type.name)
    logger.info(f"Head change requested: {display}")
    bravo._profile.head.head_type = head_type
    bravo.set_head_mode("all_barrels", "back_left")
    if hasattr(bravo.controller, "set_head_type"):
        bravo.controller.set_head_type(head_type)
    return {
        "status": "head_changed",
        "head_type": head_type.name,
        "head_type_display": display,
    }


# ═══════════════════════════════════════════════════════════════════════
# Device discovery helpers
# ═══════════════════════════════════════════════════════════════════════

_BRAVO_TCP_PORT = 10000         # Agile V11-framed protocol
_DARWIN_TCP_PORT = 7613         # Darwin/Gemini protocol
_V11_PING_CMD = 0xA0
_V11_GET_SERIAL_NUMBER = 0xB9
_BIONET_DISCOVERY_PORT = 7611
_BIONET_DISCOVERY_REQUEST = bytes.fromhex("1102001231")
_SCAN_CONNECT_TIMEOUT = 0.3   # seconds per TCP connect probe
_SCAN_RESPONSE_TIMEOUT = 1.0  # seconds to wait for V11 response
_PROBE_BUDGET_S = 3.0         # hard wall-clock cap for one IP probe
_SCAN_BUDGET_S = 15.0         # hard wall-clock cap for the whole sweep
_MAX_SCAN_WORKERS = 64
_MAX_SUBNET_PREFIX = 22        # don't scan subnets larger than /22
_PREFERRED_SCAN_NETWORK = ipaddress.IPv4Network("192.168.0.0/24")


def _enumerate_adapters() -> list[dict]:
    """Enumerate network interfaces with IPv4 addresses and netmasks.

    Uses ``ipconfig`` on Windows, ``ifconfig`` on macOS/Linux.
    Falls back to ``socket.getaddrinfo`` if both fail.
    """
    adapters: list[dict] = []

    if os.name == "nt":
        # Windows: parse ipconfig output
        try:
            result = subprocess.run(
                ["ipconfig"], capture_output=True, text=True, timeout=5,
            )
            current_iface: str | None = None
            current_ip: str | None = None
            current_mask: str | None = None
            for line in result.stdout.split("\n"):
                stripped = line.strip()
                # New adapter section (not indented, ends with colon)
                if line and not line[0].isspace() and ":" in line:
                    if current_iface and current_ip and current_mask:
                        adapters.append({
                            "name": current_iface,
                            "ip": current_ip,
                            "netmask": current_mask,
                        })
                    current_iface = line.split(":")[0].strip()
                    current_ip = None
                    current_mask = None
                m = re.search(r"IPv4 Address[^:]*:\s*(\d+\.\d+\.\d+\.\d+)", stripped)
                if m:
                    ip = m.group(1)
                    if not ip.startswith("127."):
                        current_ip = ip
                m = re.search(r"Subnet Mask[^:]*:\s*(\d+\.\d+\.\d+\.\d+)", stripped)
                if m:
                    current_mask = m.group(1)
            if current_iface and current_ip and current_mask:
                adapters.append({
                    "name": current_iface,
                    "ip": current_ip,
                    "netmask": current_mask,
                })
        except Exception:
            pass
    else:
        # macOS/Linux: parse ifconfig output
        try:
            result = subprocess.run(
                ["ifconfig"], capture_output=True, text=True, timeout=5,
            )
            current_iface = None
            for line in result.stdout.split("\n"):
                m = re.match(r"^(\w+):", line)
                if m:
                    current_iface = m.group(1)
                m = re.match(
                    r"\s+inet\s+(\d+\.\d+\.\d+\.\d+)"
                    r"\s+netmask\s+(0x[0-9a-fA-F]+|\d+\.\d+\.\d+\.\d+)",
                    line,
                )
                if m and current_iface:
                    ip = m.group(1)
                    if ip.startswith("127."):
                        continue
                    netmask_raw = m.group(2)
                    if netmask_raw.startswith("0x"):
                        nm = int(netmask_raw, 16)
                        netmask = (
                            f"{(nm >> 24) & 0xFF}."
                            f"{(nm >> 16) & 0xFF}."
                            f"{(nm >> 8) & 0xFF}."
                            f"{nm & 0xFF}"
                        )
                    else:
                        netmask = netmask_raw
                    adapters.append({"name": current_iface, "ip": ip, "netmask": netmask})
        except Exception:
            pass

    # Fallback: socket.getaddrinfo
    if not adapters:
        try:
            hostname = socket.gethostname()
            all_ips = socket.getaddrinfo(hostname, None, socket.AF_INET)
            seen: set[str] = set()
            for info in all_ips:
                ip = info[4][0]
                if ip not in seen and not ip.startswith("127."):
                    seen.add(ip)
                    adapters.append({"name": hostname, "ip": ip, "netmask": "255.255.255.0"})
        except Exception:
            pass

    if not adapters:
        adapters.append({"name": "localhost", "ip": "127.0.0.1", "netmask": "255.255.255.0"})

    return adapters


def _recv_exact_raw(
    sock: socket.socket, n: int, deadline: float | None = None
) -> bytes | None:
    """Read exactly *n* bytes from *sock*, or ``None`` on failure.

    ``deadline`` is an absolute ``time.monotonic()`` value bounding the whole
    read. Without it the socket timeout applies per ``recv``, so a peer that
    dribbles one byte at a time keeps resetting it and can hold the caller for
    ``n`` times the timeout. During a subnet scan that is any device on the LAN
    that happens to accept the port without speaking the protocol.
    """
    buf = bytearray()
    while len(buf) < n:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                sock.settimeout(min(remaining, _SCAN_RESPONSE_TIMEOUT))
            except OSError:
                return None
        try:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                return None
            buf.extend(chunk)
        except socket.timeout:
            return None
    return bytes(buf)


def _broadcast_address(ip: str, netmask: str) -> str | None:
    try:
        network = ipaddress.IPv4Network(f"{ip}/{netmask}", strict=False)
        return str(network.broadcast_address)
    except ValueError:
        return None


def _parse_bionet_reply(data: bytes, responder_ip: str) -> dict | None:
    """Parse a BioNet discovery reply.

    Observed format:
      0: 0x11
      1: 0x03
      2: device IP last octet (often — used to be enforced, now informational)
      3-4: TCP service port (big-endian), observed 7613 for Darwin
      5-10: ASCII device type, observed 'DARWIN'
      11-12: opaque device id bytes
    """
    if len(data) < 11 or data[0] != 0x11 or data[1] != 0x03:
        return None

    # Log if the host-octet byte disagrees with what we expected — but don't
    # drop the reply. The exact field semantics are only partially known and
    # may differ across firmware revisions; the 0x11/0x03 header above is
    # already enough to reject unrelated UDP traffic.
    host_octet = data[2]
    try:
        expected_octet = int(responder_ip.split(".")[-1])
    except (ValueError, IndexError):
        expected_octet = None
    if expected_octet is not None and host_octet != expected_octet:
        logger.info(
            "BioNet reply from %s has data[2]=%s (expected %s) — accepting anyway",
            responder_ip, host_octet, expected_octet,
        )

    tcp_port = int.from_bytes(data[3:5], "big")
    raw_type = data[5:11].decode("ascii", errors="ignore").rstrip("\x00").strip()
    device_id = data[11:13].hex().upper() if len(data) >= 13 else responder_ip

    controller_type = (
        "darwin_native" if raw_type.upper() == "DARWIN"
        else "agile" if raw_type.upper() in {"AGILE", "BRAVO", ""}
        else "agile"
    )

    return {
        "ip_address": responder_ip,
        "device_type": raw_type or "Bravo",
        "raw_type": raw_type,
        "device_id": device_id,
        "tcp_port": tcp_port,
        "controller_type": controller_type,
    }


def _discover_bionet_devices(adapters: list[dict], adapter_ip: str | None) -> list[dict]:
    """Discover Bravo devices using the UDP broadcast handshake."""
    devices_by_ip: dict[str, dict] = {}

    for adapter in adapters:
        if adapter_ip and adapter["ip"] != adapter_ip:
            continue

        broadcast_ip = _broadcast_address(adapter["ip"], adapter["netmask"])
        if not broadcast_ip:
            continue

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((adapter["ip"], 0))
            sock.settimeout(0.35)
            sock.sendto(_BIONET_DISCOVERY_REQUEST, (broadcast_ip, _BIONET_DISCOVERY_PORT))

            while True:
                try:
                    payload, (src_ip, _src_port) = sock.recvfrom(1024)
                except socket.timeout:
                    break
                parsed = _parse_bionet_reply(payload, src_ip)
                if parsed:
                    parsed["mac_address"] = _get_mac_from_arp(src_ip)
                    devices_by_ip[src_ip] = parsed
        except OSError as exc:
            logger.debug("BioNet discovery failed on adapter %s: %s", adapter["ip"], exc)
        finally:
            sock.close()

    return list(devices_by_ip.values())


def _probe_darwin(ip: str) -> dict | None:
    """Cheap liveness probe for a Darwin/Gemini Bravo — just verify TCP port
    7613 accepts a connection. The port is distinctive enough that a
    successful open is strong evidence; the real Gemini handshake happens
    when the user actually selects the device and Connect runs."""
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(_SCAN_CONNECT_TIMEOUT)
        sock.connect((ip, _DARWIN_TCP_PORT))
        return {
            "ip_address": ip,
            "device_type": "DARWIN",
            "raw_type": "DARWIN",
            "controller_type": "darwin_native",
            "tcp_port": _DARWIN_TCP_PORT,
        }
    except (socket.timeout, ConnectionRefusedError, OSError):
        return None
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _probe_agile(ip: str) -> dict | None:
    """Probe a single IP for an Agile Bravo — TCP 10000 + V11
    PING + optional GET_SERIAL_NUMBER."""
    sock = None
    # One IP gets a fixed slice of wall clock, no matter how the peer behaves.
    deadline = time.monotonic() + _PROBE_BUDGET_S
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(_SCAN_CONNECT_TIMEOUT)
        sock.connect((ip, _BRAVO_TCP_PORT))
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(_SCAN_RESPONSE_TIMEOUT)

        # Send V11-framed PING: [length=1 LE16] [cmd=0xA0]
        sock.sendall(struct.pack("<HB", 1, _V11_PING_CMD))

        # Read response header (2-byte LE length)
        hdr = _recv_exact_raw(sock, 2, deadline)
        if not hdr:
            return None
        resp_len = struct.unpack("<H", hdr)[0]
        if resp_len == 0 or resp_len > 1024:
            return None

        payload = _recv_exact_raw(sock, resp_len, deadline)
        if not payload or payload[0] != 0:
            return None

        info: dict = {
            "ip_address": ip,
            "device_type": "AGILE",
            "raw_type": "AGILE",
            "controller_type": "agile",
            "tcp_port": _BRAVO_TCP_PORT,
        }

        # Try to read the serial number
        try:
            sock.sendall(struct.pack("<HB", 1, _V11_GET_SERIAL_NUMBER))
            hdr2 = _recv_exact_raw(sock, 2, deadline)
            if hdr2:
                sn_len = struct.unpack("<H", hdr2)[0]
                if 0 < sn_len <= 256:
                    sn_payload = _recv_exact_raw(sock, sn_len, deadline)
                    if sn_payload and sn_payload[0] == 0 and len(sn_payload) > 1:
                        info["serial"] = (
                            sn_payload[1:].decode("ascii", errors="replace").rstrip("\x00")
                        )
        except Exception:
            pass

        return info
    except (socket.timeout, ConnectionRefusedError, OSError):
        return None
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _probe_bravo(ip: str) -> dict | None:
    """Probe a single IP for any Bravo — Darwin (port 7613) first, Agile
    (port 10000 + V11 PING) second. Returns the first hit or ``None`` if
    neither port responds."""
    darwin = _probe_darwin(ip)
    if darwin is not None:
        return darwin
    return _probe_agile(ip)


def _get_mac_from_arp(ip: str) -> str:
    """Look up MAC address from ARP cache (best-effort)."""
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["arp", "-a", ip], capture_output=True, text=True, timeout=2,
            )
            m = re.search(
                r"([0-9a-fA-F]{2}[:-][0-9a-fA-F]{2}[:-][0-9a-fA-F]{2}"
                r"[:-][0-9a-fA-F]{2}[:-][0-9a-fA-F]{2}[:-][0-9a-fA-F]{2})",
                result.stdout,
            )
            if m:
                return m.group(1).upper().replace(":", "-")
        else:
            result = subprocess.run(
                ["arp", "-n", ip], capture_output=True, text=True, timeout=2,
            )
            m = re.search(r"at\s+([0-9a-fA-F:]+)", result.stdout)
            if m:
                return m.group(1).upper().replace(":", "-")
    except Exception:
        pass
    return "—"


def _build_candidate_ips(adapters: list[dict], adapter_ip: str | None) -> set[str]:
    """Build the IP list for discovery, prioritizing the common Bravo subnet."""
    candidate_ips: set[str] = set()
    local_ips: set[str] = {a["ip"] for a in adapters}

    # The model flow is adapter-driven, but in this environment the user
    # explicitly expects Find Available Device to sweep the Bravo LAN.
    for host in _PREFERRED_SCAN_NETWORK.hosts():
        hip = str(host)
        if hip not in local_ips:
            candidate_ips.add(hip)

    for adapter in adapters:
        if adapter_ip and adapter["ip"] != adapter_ip:
            continue
        try:
            network = ipaddress.IPv4Network(
                f"{adapter['ip']}/{adapter['netmask']}", strict=False,
            )
            if network.prefixlen < _MAX_SUBNET_PREFIX:
                logger.info("Skipping large subnet %s (prefix=%d)", network, network.prefixlen)
                continue
            for host in network.hosts():
                hip = str(host)
                if hip not in local_ips:
                    candidate_ips.add(hip)
        except ValueError:
            continue

    return candidate_ips


def _scan_subnet(adapters: list[dict], adapter_ip: str | None) -> list[dict]:
    """Scan subnets for Bravo devices (TCP port 10000 + V11 PING)."""
    candidate_ips = _build_candidate_ips(adapters, adapter_ip)

    if not candidate_ips:
        return []

    logger.info("Scanning %d IPs for Bravo devices…", len(candidate_ips))
    devices: list[dict] = []
    started = time.monotonic()

    # NOT a `with` block. ThreadPoolExecutor.__exit__ calls shutdown(wait=True),
    # which joins every worker — so a single probe stuck on an unresponsive peer
    # keeps the whole request hanging long after the budget below has expired.
    # The timeout on as_completed only stops us collecting; it does not stop
    # them running. Shut down without waiting instead, and report what we got.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=_MAX_SCAN_WORKERS)
    try:
        futures = {pool.submit(_probe_bravo, ip): ip for ip in candidate_ips}
        try:
            for future in concurrent.futures.as_completed(futures, timeout=_SCAN_BUDGET_S):
                result = future.result()
                if result:
                    result["mac_address"] = _get_mac_from_arp(result["ip_address"])
                    devices.append(result)
        except concurrent.futures.TimeoutError:
            pending = sum(1 for f in futures if not f.done())
            logger.warning(
                "Subnet scan hit its %.0fs budget with %d of %d probes still "
                "outstanding; returning %d device(s) found so far.",
                _SCAN_BUDGET_S, pending, len(futures), len(devices),
            )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    logger.info(
        "Subnet scan finished in %.2fs -> %d device(s)",
        time.monotonic() - started, len(devices),
    )
    return devices


# ═══════════════════════════════════════════════════════════════════════


def _merge_device_records(records: list[dict]) -> list[dict]:
    """Deduplicate discovery results by IP, merging fields so BioNet metadata
    (``raw_type``, ``device_id``) and TCP-scan metadata (``serial``) combine
    into one row. Darwin beats Agile when both ports happen to be open on the
    same IP — modern firmware wins."""
    merged: dict[str, dict] = {}
    for rec in records:
        ip = rec.get("ip_address")
        if not ip:
            continue
        existing = merged.get(ip)
        if existing is None:
            merged[ip] = dict(rec)
            continue
        for k, v in rec.items():
            if v in (None, "", "—"):
                continue
            if existing.get(k) in (None, "", "—"):
                existing[k] = v
        if rec.get("controller_type") == "darwin_native":
            existing["controller_type"] = "darwin_native"
            existing["device_type"] = rec.get("device_type", existing.get("device_type"))
            existing["raw_type"] = rec.get("raw_type", existing.get("raw_type"))
            if rec.get("tcp_port"):
                existing["tcp_port"] = rec["tcp_port"]
    return list(merged.values())


def _probe_ip_sync(ip: str) -> dict | None:
    """Thread-pool entry point used by the directed probe path."""
    hit = _probe_bravo(ip)
    if hit is not None:
        hit["mac_address"] = _get_mac_from_arp(ip)
    return hit


@app.post("/api/discover_devices", **_route_meta("Discovery", "Discover available Bravo devices on the network", DISCOVERY_DOC))
async def discover_devices(req: DiscoverDevicesRequest):
    """Discover Bravo devices on the local network.

    Runs three probes concurrently and merges their results by IP:
      1. UDP BioNet broadcast (port 7611) — instrument discovery protocol.
      2. TCP subnet sweep — port 7613 (Darwin) first, port 10000 + V11 PING
         (Agile) second.
      3. Directed probe of the currently-configured profile address — cheap
         insurance that a known-good machine always surfaces, even if the
         broadcast is silent and the scan is scoped away.

    In simulation mode, returns a clearly-labelled virtual device.
    """
    adapters = _enumerate_adapters()
    adapter_list = [{"name": a["name"], "ip": a["ip"]} for a in adapters]

    bravo = get_bravo()
    controller_type = req.controller_type or bravo._profile.connection.controller_type

    # Simulation mode — return a clearly-labelled virtual device
    if controller_type == "simulation":
        return {
            "devices": [
                {
                    "device_id": "Simulation",
                    "device_type": "Bravo (Simulation)",
                    "ip_address": "127.0.0.1",
                    "mac_address": "—",
                    "status": "Simulation",
                    "controller_type": "simulation",
                }
            ],
            "adapters": adapter_list,
        }

    adapter_ip = req.adapter if req.adapter != "All interfaces" else None
    target_address = (bravo._profile.connection.address or "").strip()

    logger.info(
        "Discover: adapters=%d adapter_ip=%s target=%s",
        len(adapters), adapter_ip or "all", target_address or "—",
    )

    # Run BioNet, subnet-scan, and directed-probe concurrently.
    start = time.monotonic()
    tasks = [
        asyncio.to_thread(_discover_bionet_devices, adapters, adapter_ip),
        asyncio.to_thread(_scan_subnet, adapters, adapter_ip),
    ]
    if target_address:
        tasks.append(asyncio.to_thread(_probe_ip_sync, target_address))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    elapsed_ms = int((time.monotonic() - start) * 1000)

    bionet_result = results[0] if not isinstance(results[0], Exception) else []
    scan_result = results[1] if not isinstance(results[1], Exception) else []
    directed_result = None
    if target_address:
        dr = results[2]
        if not isinstance(dr, Exception):
            directed_result = dr

    for idx, label in enumerate(("bionet", "scan", "directed")):
        if idx < len(results) and isinstance(results[idx], Exception):
            logger.warning("Discover: %s probe raised %s", label, results[idx])

    logger.info(
        "Discover: bionet=%d scan=%d directed=%s (%d ms)",
        len(bionet_result), len(scan_result),
        "hit" if directed_result else "miss",
        elapsed_ms,
    )

    raw: list[dict] = []
    raw.extend(bionet_result)
    raw.extend(scan_result)
    if directed_result is not None:
        raw.append(directed_result)

    merged = _merge_device_records(raw)

    devices: list[dict] = []
    for d in merged:
        serial = d.get("serial", "") or d.get("device_id", "")
        status = "Matched" if target_address and d["ip_address"] == target_address else "Found"
        devices.append({
            "device_id": serial or d["ip_address"],
            "device_type": d.get("raw_type") or d.get("device_type", "Bravo"),
            "ip_address": d["ip_address"],
            "mac_address": d.get("mac_address", "—"),
            "status": status,
            "controller_type": d.get("controller_type", ""),
        })

    # Surface the matched target first so the UI selects the user's machine by default.
    devices.sort(key=lambda row: (row["status"] != "Matched", row["ip_address"]))

    if not devices:
        logger.info("No Bravo devices found on the network")

    return {"devices": devices, "adapters": adapter_list}


@app.post("/api/select_device", **_route_meta("Discovery", "Select a discovered device and persist it to the profile", DISCOVERY_DOC))
async def select_device(req: SelectDeviceRequest):
    global _profile_path
    bravo = get_bravo()
    if req.controller_type:
        bravo._profile.connection.controller_type = req.controller_type
    if req.ip_address:
        bravo._profile.connection.address = req.ip_address
        bravo._profile.connection.use_ethernet = True
    if _profile_path is not None:
        try:
            bravo.profile.save(_profile_path)
        except OSError as exc:
            logger.warning("Failed to save profile to %s after device select: %s", _profile_path, exc)
    logger.info(f"Device selected: id={req.device_id!r} ip={req.ip_address!r}")
    return {
        "status": "selected",
        "device_id": req.device_id,
        "ip_address": req.ip_address,
        "controller_type": bravo._profile.connection.controller_type,
    }


# -- WebSocket for real-time state streaming --

class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        data = json.dumps(message)
        for connection in self.active_connections:
            try:
                await connection.send_text(data)
            except Exception:
                pass

ws_manager = ConnectionManager()

@app.websocket("/ws/state")
async def websocket_state(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        while True:
            bravo = get_bravo()
            if bravo.is_connected:
                state = bravo.get_state()
                await websocket.send_json(state)
            sleep_s = 1 / 30
            if bravo.profile.connection.controller_type in {"darwin", "darwin_native", "agile", "agile_7612"}:
                sleep_s = 0.2
            await asyncio.sleep(sleep_s)
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)


# -- Server startup --

def _write_active_profile(profile_dir: Path, name: str) -> None:
    """Persist the active profile name so it survives server restarts."""
    try:
        (profile_dir / ".active_profile").write_text(name, encoding="utf-8")
    except OSError as e:
        logger.warning("Could not write .active_profile: %s", e)


def _read_active_profile(profile_dir: Path) -> str | None:
    """Return the previously saved active profile name, or None."""
    marker = profile_dir / ".active_profile"
    if marker.exists():
        name = marker.read_text(encoding="utf-8").strip()
        if name and (profile_dir / f"{name}.yaml").exists():
            return name
    return None


def run_server(
    bravo: Bravo | None = None,
    host: str = "0.0.0.0",
    port: int = 8000,
    static_dir: str | None = None,
):
    """Start the FastAPI server."""
    configure_logging()
    import uvicorn

    global _labware_assets_mounted
    global _bravo, _profile_path, _profile_dir
    if bravo is None:
        profile_dir = Path(
            os.environ.get("PYBRAVO_PROFILE_DIR", str(Path.cwd() / "profiles"))
        )
        _profile_dir = profile_dir
        profile_dir.mkdir(parents=True, exist_ok=True)

        # Prefer the last-used profile. The fallback is the simulation profile
        # rather than a hardware one on purpose: a fresh clone should never
        # reach for an instrument address nobody chose.
        active_name = _read_active_profile(profile_dir)
        if active_name:
            profile_path = profile_dir / f"{active_name}.yaml"
            logger.info("Resuming last active profile: %s", active_name)
        else:
            profile_path = profile_dir / "simulation.yaml"
            logger.warning(
                "No active profile recorded (%s); falling back to 'simulation'. "
                "Load the profile you intend to work in BEFORE teaching — "
                "teachpoints are saved into whichever profile is active.",
                profile_dir / ".active_profile",
            )

        if profile_path.exists():
            _bravo = Bravo(profile=profile_path)
            _profile_path = profile_path
            logger.info("Loaded profile from %s", profile_path)
        else:
            _bravo = Bravo(mode="simulation")
            _profile_path = profile_dir / "simulation.yaml"
            try:
                _bravo.profile.save(_profile_path)
                logger.info("Created default profile at %s", _profile_path)
            except OSError as e:
                logger.warning("Could not create default profile file: %s", e)
    else:
        _bravo = bravo
        _profile_path = None
        _profile_dir = None

    editor_asset_dir = labware_editor._asset_dir()
    editor_asset_dir.mkdir(parents=True, exist_ok=True)
    if not _labware_assets_mounted:
        app.mount("/labware-assets", StaticFiles(directory=str(editor_asset_dir)), name="labware_assets")
        _labware_assets_mounted = True

    if static_dir:
        model_dir = Path(__file__).resolve().parent.parent / "model"
        if model_dir.is_dir():
            app.mount("/model", StaticFiles(directory=str(model_dir)), name="model")
        labware_dir = Path(__file__).resolve().parent.parent.parent / "labware"
        if labware_dir.is_dir():
            app.mount("/labware", StaticFiles(directory=str(labware_dir)), name="labware")

        app.mount("/static", StaticFiles(directory=static_dir, html=True), name="static_files")

        @app.get("/")
        async def serve_index():
            # Served as rewritten HTML rather than a FileResponse so the
            # `?v=` tokens on its module imports carry the file's mtime; see
            # _version_static_assets.
            index_path = Path(static_dir) / "index.html"
            return HTMLResponse(
                _version_static_assets(
                    index_path.read_text(encoding="utf-8"), Path(static_dir)
                ),
                headers=NO_CACHE_HEADERS,
            )

    uvicorn.run(app, host=host, port=port, log_config=None)


if __name__ == "__main__":
    frontend_dir = Path(__file__).resolve().parent.parent.parent / "frontend"
    static = str(frontend_dir) if frontend_dir.is_dir() else None
    run_server(static_dir=static)
