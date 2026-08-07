"""High-level Bravo liquid handler interface.

This is the main entry point for controlling a Bravo. It wraps the controller,
state machine, deck model, and profile into a single async API.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any

from pybravo import liquid_classes as liquid_classes_store
from pybravo.accessories.barcode_reader import BarcodeReadError
from pybravo.accessories.manager import AccessoryManager
from pybravo.controllers.base import AxisMoveInfo, BravoController
from pybravo.controllers.simulation import SimulationController
from pybravo.deck.geometry import well_center_offset_from_teachpoint_mm, well_geometry_from_metadata
from pybravo.deck.labware import (
    DeckState,
    Labware,
    LabwareCatalog,
    build_labware_catalog,
    synthesize_lid_labware,
)
from pybravo.deck.teachpoints import Teachpoints
from pybravo.head_mode import (
    HeadMode,
    PlateSelection,
    TipSelection,
    head_mode_offsets_mm,
    is_legal_plate_anchor,
    is_legal_tipbox_anchor,
    legal_plate_anchors,
    legal_tipbox_anchors,
    normalize_head_mode,
    plate_footprint_wells,
    plate_selection,
    selected_tip_wells,
    tip_task_head_offsets_mm,
    tipbox_selection,
)
from pybravo.motion.axes import DEFAULT_SPEEDS
from pybravo.profile.profile import BravoProfile
from pybravo.state_machine.engine import ErrorAction, StateMachineEngine, TaskStatus
from pybravo.state_machine.tasks import (
    AspirateTask,
    DelidPlateTask,
    DispenseTask,
    DockGripperTask,
    GripperTeachMoveTask,
    HomeTask,
    InitializeTask,
    MixTask,
    MoveToLocationTask,
    PickPlaceTask,
    RelidPlateTask,
    ScanStackHeightTask,
    TipsOffTask,
    TipsOnTask,
    _assert_neighbor_clearance,
    _gripper_head_offsets,
    _stack_total_height_for_count,
    _stacking_support_height_for_count,
)
from pybravo.tip_offsets import ResolvedTipOffsets, get_tip_offset_table
from pybravo.tips import (
    get_default_tip_id_for_head,
    get_tip_capacity_ul,
    get_tip_id_for_capacity,
    get_tip_length_mm,
)
from pybravo.types import (
    AXIS_EPSILON,
    OPEN_GRIPPER_POSITION,
    Axis,
    DeviceStateFlag,
    HeadType,
    SpeedLevel,
    safe_home_order,
)

logger = logging.getLogger(__name__)
_PICKUP_FAILURE_G_THRESHOLD_MM = 10.0
_PLATE_SENSOR_UNTRUSTWORTHY_G_THRESHOLD_MM = 7.0
_DARWIN_HEAD_RESISTOR_OHMS: dict[HeadType, int] = {
    HeadType.HT_96_ASSAYMAP: 1000,
    HeadType.HT_96_F_200: 1500,
    HeadType.HT_96_D_200: 1500,
    HeadType.HT_384_D_70_S2: 2210,
    HeadType.HT_384_F_50: 3160,
    HeadType.HT_384_D_70: 4220,
    HeadType.HT_96_D_70_S2: 5110,
    HeadType.HT_96_F_50: 8870,
    HeadType.HT_8_F_50: 11000,
    HeadType.HT_96_PINTOOL: 11000,
    HeadType.HT_384_PINTOOL: 11000,
    HeadType.HT_1536_PINTOOL: 11000,
    HeadType.HT_96_D_70: 13300,
    HeadType.HT_8_D_LT: 20000,
    HeadType.HT_16_D_ST: 30100,
}


class Bravo:
    """High-level interface for the Agilent Bravo liquid handler.

    Supports async context manager usage:
        async with Bravo(profile="my_bravo.yaml") as bravo:
            await bravo.initialize()
            await bravo.aspirate(location=3, volume=50.0)
    """

    def __init__(
        self,
        profile: str | Path | BravoProfile | None = None,
        mode: str | None = None,  # "simulation", "agile", "darwin_native" - overrides profile
    ):
        if isinstance(profile, (str, Path)):
            self._profile = BravoProfile.load(profile)
        elif isinstance(profile, BravoProfile):
            self._profile = profile
        else:
            self._profile = BravoProfile.default()

        if mode:
            self._profile.connection.controller_type = mode

        self._controller: BravoController | None = None
        self._engine = StateMachineEngine()
        self._engine.set_error_handler(self._handle_task_error)
        self._deck = DeckState()
        self._labware_catalog: LabwareCatalog = build_labware_catalog()
        self._teachpoints = self._build_teachpoints(self._profile)
        self._initialized = False
        # Axes homed since connecting. Tracked in software rather than polled:
        # is_axis_homed() costs a wire read per axis, and the state feed runs
        # several times a second. Same staleness caveat as _initialized — a
        # power cycle behind our back invalidates it.
        self._homed_axes: set[Axis] = set()
        self._event_handlers: dict[str, list] = {}
        self._head_mode = normalize_head_mode(self._profile.head.head_type, "all_barrels", "back_left")
        self._tips_on_head = False
        self._tip_labware_name = ""
        self._tip_definition_id = ""
        self._attached_tip_length_mm: float | None = None
        self._tips_on_head_mode: HeadMode | None = None
        self._tip_selection: TipSelection | None = None
        self._tips_on_head_selection: TipSelection | None = None
        self._plate_selection: dict[int, PlateSelection] = {}
        self._tipbox_occupancy: dict[int, set[tuple[int, int]]] = {}
        self._tipbox_untracked: set[int] = set()
        self._labware_names: dict[str, int] = {}
        self._accessories = AccessoryManager(self._profile)

    # -- Context manager --

    async def __aenter__(self):
        self.connect()
        return self

    async def __aexit__(self, *args):
        self.disconnect()

    # -- Connection --

    def connect(self) -> None:
        if self._controller is not None:
            self.disconnect()

        cfg = self._profile.connection
        if cfg.controller_type == "simulation":
            # Build a per-Axis homing-offset map from the loaded profile so
            # the simulation starts in the correct deck coordinate system.
            homing_offsets: dict[Axis, float] = {}
            for ax_name, ax_cfg in self._profile.axes.items():
                try:
                    homing_offsets[Axis[ax_name]] = ax_cfg.homing_offset
                except KeyError:
                    pass
            self._controller = SimulationController(
                self._profile.head.head_type,
                homing_offsets=homing_offsets,
            )
            self._controller.open_tcp("simulation")
        elif cfg.controller_type == "agile":
            from pybravo.controllers.agile import AgileController
            self._controller = AgileController()
            if cfg.use_ethernet:
                self._controller.open_tcp(cfg.address)
            else:
                self._controller.open_serial(cfg.serial_port)
        elif cfg.controller_type == "agile_7612":
            from pybravo.controllers.agile_7612 import Agile7612Controller
            self._controller = Agile7612Controller(profile=self._profile)
            self._controller.open_tcp(cfg.address)
        elif cfg.controller_type == "agile_srt":
            from pybravo.controllers.agile_srt import AgileSrtController
            self._controller = AgileSrtController(profile=self._profile)
            self._controller.open_tcp(cfg.address)
        elif cfg.controller_type == "darwin_native":
            # Pure-Python Gemini TCP implementation; the whole stack runs
            # in-process. Replaces an earlier out-of-process `darwin` controller
            # type, which was removed once every workflow had been validated on
            # this native path.
            from pybravo.darwin.controller import DarwinController as NativeDarwinController
            self._controller = NativeDarwinController(profile=self._profile,
                                                       address=cfg.address)
            self._controller.open_tcp(cfg.address)
        else:
            raise ValueError(f"Unknown controller type: {cfg.controller_type}")
        logger.info("Connected via %s", cfg.controller_type)

    def disconnect(self) -> None:
        if self._controller:
            self._controller.close()
            self._controller = None
        self._initialized = False
        self._homed_axes.clear()
        self._clear_tip_state()
        self._accessories.close_all()

    @property
    def is_connected(self) -> bool:
        return self._controller is not None and self._controller.is_connected

    @property
    def controller(self) -> BravoController:
        if self._controller is None and self._profile.connection.controller_type == "simulation":
            self.connect()
        if not self._controller:
            raise RuntimeError("Not connected")
        return self._controller

    # -- Accessories --

    def reinit_accessories(self) -> None:
        """Re-create accessory drivers from current profile settings."""
        self._profile.accessories.sync_legacy_barcode_from_devices()
        self._accessories.reconfigure(self._profile)

    @property
    def barcode_reader_location(self) -> int:
        cfg = self._profile.accessories.barcode_reader_from_devices()
        return cfg.location

    def reinit_barcode_reader(self) -> None:
        """Re-create the barcode reader from current profile settings."""
        self.reinit_accessories()

    def _barcode_accessory_for_location(self, location: int | None = None):
        return self._accessories.find_enabled("barcode_reader", location=location)

    def accessory_status(self) -> dict[str, Any]:
        devices: list[dict[str, Any]] = []
        for device in self._profile.accessories.devices:
            driver = self._accessories._drivers.get(device.id)
            devices.append(
                {
                    **device.to_dict(),
                    "runtime": {
                        "loaded": driver is not None,
                        "is_open": bool(getattr(driver, "is_open", False)),
                        "is_running": bool(getattr(driver, "is_running", False)),
                    },
                }
            )
        return {"devices": devices}

    def start_teleshake(
        self,
        accessory_id: str,
        *,
        rpm: int | None = None,
        direction: str | None = None,
    ) -> dict[str, Any]:
        device = self._accessories.find_by_id(accessory_id)
        if device is None:
            raise ValueError(f"Accessory {accessory_id!r} is not configured")
        if device.type != "teleshake":
            raise ValueError(f"Accessory {accessory_id!r} is not a Teleshake")
        if not device.enabled:
            raise ValueError(f"Accessory {accessory_id!r} is disabled")
        driver = self._accessories.get_driver(device)
        driver.start(rpm=rpm, direction=direction)
        return {
            "status": "running",
            "accessory_id": accessory_id,
            "rpm": rpm if rpm is not None else device.settings.get("default_rpm"),
            "direction": direction or device.settings.get("default_direction"),
        }

    def stop_teleshake(self, accessory_id: str) -> dict[str, Any]:
        device = self._accessories.find_by_id(accessory_id)
        if device is None:
            raise ValueError(f"Accessory {accessory_id!r} is not configured")
        if device.type != "teleshake":
            raise ValueError(f"Accessory {accessory_id!r} is not a Teleshake")
        driver = self._accessories.get_driver(device)
        driver.stop()
        return {"status": "stopped", "accessory_id": accessory_id}

    async def read_barcode(self, location: int) -> dict[str, Any]:
        """Read the barcode of the plate at `location`.

        Auto-transport: the barcode reader is a fixed scanner mounted at
        ``profile.accessories.barcode_reader.location``. If the caller
        asks to read a plate at a different location, the task implicitly
        pick-and-places the plate to the reader, scans, then returns the
        plate to its original position. Wrapped in try/finally so a
        reader failure still returns the plate.

        If the caller's location already IS the reader location, the
        plate doesn't move — just trigger the scanner.

        Every error path emits a WARNING-level log.
        """
        # Simulation short-circuit: no physical scanner + no hardware to
        # move, so return whatever barcode the virtual plate already
        # carries (from deck config or a previous operator entry), or a
        # deterministic synthetic barcode if the plate has none. This
        # keeps dry-run workflows from hitting the operator-prompt modal
        # on every ReadBarcode; we don't re-enact the auto-move either
        # since the sim controller's pick/place is pure animation cost
        # with no new information to gather.
        if self.controller.__class__.__name__ == "SimulationController":
            try:
                stack = self._deck.get_stack(int(location))
                top = stack.top if stack is not None else None
            except Exception:
                top = None
            if top is None:
                return {
                    "status": "error",
                    "message": f"[simulation] No plate at location {location}",
                    "location": location,
                    "barcode": "",
                    "simulated": True,
                }
            existing = str(getattr(top, "barcode", "") or "").strip()
            if existing:
                barcode = existing
            else:
                # Monotonic synthetic barcode per sim run so repeated scans of
                # the "same" location (post pick/place swaps) stay distinct.
                self._sim_barcode_counter = getattr(self, "_sim_barcode_counter", 0) + 1
                barcode = f"SIM-{getattr(top, 'name', 'plate')}-L{location}-{self._sim_barcode_counter:03d}"
                try:
                    top.barcode = barcode  # stick it on the virtual plate
                except Exception:
                    pass
            return {
                "status": "completed",
                "command": "Read Barcode",
                "barcode": barcode,
                "location": location,
                "simulated": True,
            }

        device = self._barcode_accessory_for_location(location)
        if device is None:
            msg = "Barcode reader is not enabled. Add or enable it in the Accessories section of the Config tab."
            logger.warning("read_barcode at loc %s: %s", location, msg)
            return {"status": "error", "message": msg, "location": location}
        if device.location < 1:
            msg = "Barcode reader deck location is not set. Configure it in the Accessories section of the Config tab."
            logger.warning("read_barcode at loc %s: %s", location, msg)
            return {"status": "error", "message": msg, "location": location}

        reader_loc = int(device.location)
        requested = int(location)
        # Fast path: plate is already at the reader (or the caller
        # explicitly asked us to read the reader's slot).
        if requested == reader_loc:
            return await self._trigger_barcode_read(requested, device)

        # Slow path: auto-transport. Check invariants before moving.
        try:
            src_stack = self._deck.get_stack(requested)
        except Exception:
            src_stack = None
        if src_stack is None or src_stack.top is None:
            msg = (
                f"No plate at location {requested}; cannot auto-transport "
                f"to barcode reader at location {reader_loc}."
            )
            logger.warning("read_barcode: %s", msg)
            return {"status": "error", "message": msg, "location": requested}
        try:
            reader_stack = self._deck.get_stack(reader_loc)
        except Exception:
            reader_stack = None
        if reader_stack is not None and reader_stack.top is not None:
            msg = (
                f"Barcode reader location {reader_loc} is occupied. Clear "
                f"it before running a ReadBarcode that targets a different "
                f"location (or move your plate to location {reader_loc} "
                "yourself first)."
            )
            logger.warning("read_barcode: %s", msg)
            return {"status": "error", "message": msg, "location": requested}

        logger.info(
            "read_barcode auto-transport: %d -> reader@%d -> %d",
            requested, reader_loc, requested,
        )
        # Pick + place the plate to the reader. If the outbound trip aborts
        # or returns a non-ok status, we bail without attempting to read or
        # return the plate (wherever it ended up is at least a known state
        # the operator can inspect).
        out = await self.pick_place(requested, reader_loc)
        if isinstance(out, dict) and out.get("status") == "aborted":
            return {"status": "aborted", "location": requested,
                    "message": f"Pick/place {requested}->{reader_loc} aborted."}

        # Wrap the read + return-trip so the plate always goes home,
        # even if the scan itself fails or the reader hangs.
        try:
            result = await self._trigger_barcode_read(reader_loc, device)
            # Overwrite `location` in the result with the CALLER's
            # requested slot so downstream vars_store / executor code
            # attaches the barcode to the right deck position.
            result["location"] = requested
            result["auto_transported"] = True
            result["reader_location"] = reader_loc
        finally:
            back = await self.pick_place(reader_loc, requested)
            if isinstance(back, dict) and back.get("status") == "aborted":
                logger.warning(
                    "read_barcode: plate left at reader (loc %d). Return "
                    "trip aborted by operator.", reader_loc,
                )
        return result

    async def _trigger_barcode_read(self, location: int, device=None) -> dict[str, Any]:
        """Lowest-level scanner trigger. No motion. Assumes the plate is
        already at the reader's deck position. Returns the same result
        shape as read_barcode() for the success / error paths."""
        device = device or self._barcode_accessory_for_location(location)
        if device is None:
            msg = "Could not initialize barcode reader: no enabled barcode reader accessory is configured."
            logger.warning("read_barcode at loc %s: %s", location, msg)
            return {"status": "error", "message": msg, "location": location}
        cfg = device.to_barcode_reader()
        try:
            reader = self._accessories.get_barcode_reader(device)
        except Exception as exc:
            msg = f"Could not initialize barcode reader (device_type={cfg.device_type!r}, port={cfg.port!r}): {exc}"
            logger.warning("read_barcode at loc %s: %s", location, msg)
            return {"status": "error", "message": msg, "location": location}

        # Open the reader if not already open
        if not reader.is_open:
            try:
                reader.open()
            except Exception as exc:
                msg = f"Could not open barcode reader on {cfg.port}: {exc}"
                logger.warning("read_barcode at loc %s: %s", location, msg)
                return {"status": "error", "message": msg, "location": location}

        try:
            barcode = reader.trigger_and_read()
        except BarcodeReadError as exc:
            msg = str(exc)
            logger.warning(
                "read_barcode at loc %s on %s: %s",
                location, cfg.port, msg,
            )
            return {"status": "error", "message": msg, "location": location}

        logger.info("Barcode at location %d: %s", location, barcode)
        return {
            "status": "completed",
            "command": "Read Barcode",
            "barcode": barcode,
            "location": location,
        }

    # -- High-level operations --

    async def initialize(self, *, auto_confirm: bool = False) -> None:
        task = InitializeTask(self.controller, self._profile)
        auto_resolve_task: asyncio.Task[None] | None = None
        if auto_confirm:
            async def _auto_resolve() -> None:
                while True:
                    await asyncio.sleep(0.05)
                    if self._engine._awaiting_error_action:
                        logger.info("Auto-confirming operator prompt (auto_confirm=True)")
                        self._engine.resolve_error(ErrorAction.RETRY)
            auto_resolve_task = asyncio.create_task(_auto_resolve())
        try:
            await self._engine.execute(task)
        finally:
            if auto_resolve_task is not None:
                auto_resolve_task.cancel()
                try:
                    await auto_resolve_task
                except asyncio.CancelledError:
                    pass
        if task.status == TaskStatus.ABORTED:
            logger.warning("Initialize aborted by operator; not marking robot as initialized.")
            return
        self._initialized = True
        self._homed_axes.update(self._axes_expected_home())
        self._emit("initialized")

    async def home(self, axes: list[Axis] | None = None, *, force: bool = False) -> list[Axis]:
        """Home the machine.

        ``force`` re-runs the routine on axes that already report themselves
        homed. Operator-initiated homes pass True: pressing Home and having
        nothing move because the axes "look" homed is never the right answer.
        """
        if axes is None:
            axes = [Axis.X, Axis.Y, Axis.Z]
            if not self._profile.safety.ignore_w_axis:
                axes.append(Axis.W)
            has_gripper = getattr(self.controller, "HAS_GRIPPER", True)
            if has_gripper and "G" in self._profile.axes and "Zg" in self._profile.axes:
                axes.extend([Axis.G, Axis.Zg])
        # Head and gripper up before the gantry moves; see SAFE_HOME_ORDER.
        axes = safe_home_order(axes)
        task = HomeTask(
            self.controller,
            self._profile,
            axes,
            safe_z_position=self._profile.safety.z_safe_position,
            force=force,
        )
        await self._engine.execute(task)
        if task.status == TaskStatus.ABORTED:
            logger.warning("Home aborted by operator.")
            return []
        self._homed_axes.update(axes)
        self._emit("homed", axes=axes)
        return list(axes)

    async def move_to_location(
        self,
        location: int,
        approach_height: float = 0.0,
        only_move_z: bool = False,
        speed: SpeedLevel = SpeedLevel.MED,
    ) -> None:
        task = MoveToLocationTask(
            self.controller,
            self._teachpoints,
            location,
            safe_z_position=self._profile.safety.z_safe_position,
            approach_height=approach_height,
            only_move_z=only_move_z,
            speed_profiles={
                axis: self._speed_profile(axis, speed)
                for axis in (Axis.X, Axis.Y, Axis.Z)
            },
        )
        await self._engine.execute(task)
        if task.status == TaskStatus.ABORTED:
            logger.warning("Move to location %d aborted by operator.", location)
            return
        self._emit("moved", location=location)

    def _speed_profile(self, axis: Axis, level: SpeedLevel) -> tuple[float, float]:
        cfg = self._profile.axes.get(axis.name)
        if cfg and level in cfg.speeds:
            sp = cfg.speeds[level]
            return sp.velocity, sp.acceleration
        fallback = DEFAULT_SPEEDS.get(axis, {}).get(level)
        if fallback is None:
            return 0.0, 0.0
        return fallback.velocity, fallback.acceleration

    async def move_to_safe_z(self, speed: SpeedLevel = SpeedLevel.MED) -> None:
        velocity, acceleration = self._speed_profile(Axis.Z, speed)
        self.controller.move(
            [AxisMoveInfo(axis=Axis.Z, position=self._profile.safety.z_safe_position, velocity=velocity, acceleration=acceleration)],
            wait=True,
        )
        self._emit("moved_safe_z", position=self._profile.safety.z_safe_position)

    async def aspirate(
        self,
        location: int | str,
        volume: float,
        pre_aspirate: float = 0.0,
        post_aspirate: float = 0.0,
        distance_from_bottom: float = 1.0,
        dynamic_tip_extension: float = 0.0,
        tip_touch: bool = False,
        liquid_class: str | None = None,
        pipette_technique: str | None = None,
    ) -> None:
        location = self._resolve_location(location)
        self._assert_well_access(location)
        liquid_class_def = self._resolve_liquid_class(liquid_class)
        pipette_technique_def = self._resolve_pipette_technique(pipette_technique)
        task = AspirateTask(
            self.controller, self._teachpoints, location,
            volume, pre_aspirate, post_aspirate, distance_from_bottom,
            safe_z_position=self._profile.safety.z_safe_position,
            labware=self._deck.get_stack(location).top,
            head_type=self._profile.head.head_type,
            head_mode=self._head_mode,
            plate_selection=self._effective_plate_selection(
                location,
                self._deck.get_stack(location).top,
                self._head_mode,
                command_name="Aspirate",
            ),
            dynamic_tip_extension=dynamic_tip_extension,
            tip_touch=tip_touch,
            liquid_class=liquid_class_def,
            pipette_technique=pipette_technique_def,
            deck=self._deck,
            teach_tip_length_mm=self._profile.head.teach_tip_length_mm,
            attached_tip_length_mm=self._attached_tip_length_mm,
            tips_on_head=self._tips_on_head,
        )
        await self._engine.execute(task)
        if task.status == TaskStatus.ABORTED:
            logger.warning("Aspirate at location %d aborted by operator.", location)
            return
        self._emit(
            "aspirated",
            location=location,
            volume=volume,
            liquid_class=None if liquid_class_def is None else liquid_class_def.get("name"),
            pipette_technique=None if pipette_technique_def is None else pipette_technique_def.get("name"),
        )

    async def dispense(
        self,
        location: int | str,
        volume: float,
        blowout: float = 0.0,
        distance_from_bottom: float = 1.0,
        empty_tips: bool = False,
        dynamic_tip_retraction: float = 0.0,
        tip_touch: bool = False,
        liquid_class: str | None = None,
        pipette_technique: str | None = None,
    ) -> None:
        location = self._resolve_location(location)
        self._assert_well_access(location)
        liquid_class_def = self._resolve_liquid_class(liquid_class)
        pipette_technique_def = self._resolve_pipette_technique(pipette_technique)
        task = DispenseTask(
            self.controller,
            self._teachpoints,
            location,
            volume=volume,
            blowout_volume=blowout,
            distance_from_bottom=distance_from_bottom,
            safe_z_position=self._profile.safety.z_safe_position,
            labware=self._deck.get_stack(location).top,
            head_type=self._profile.head.head_type,
            head_mode=self._head_mode,
            plate_selection=self._effective_plate_selection(
                location,
                self._deck.get_stack(location).top,
                self._head_mode,
                command_name="Dispense",
            ),
            empty_tips=empty_tips,
            dynamic_tip_retraction=dynamic_tip_retraction,
            tip_touch=tip_touch,
            liquid_class=liquid_class_def,
            pipette_technique=pipette_technique_def,
            deck=self._deck,
            teach_tip_length_mm=self._profile.head.teach_tip_length_mm,
            attached_tip_length_mm=self._attached_tip_length_mm,
            tips_on_head=self._tips_on_head,
        )
        await self._engine.execute(task)
        if task.status == TaskStatus.ABORTED:
            logger.warning("Dispense at location %d aborted by operator.", location)
            return
        self._emit(
            "dispensed",
            location=location,
            volume=volume,
            liquid_class=None if liquid_class_def is None else liquid_class_def.get("name"),
            pipette_technique=None if pipette_technique_def is None else pipette_technique_def.get("name"),
        )

    async def mix(
        self,
        location: int | str,
        volume: float,
        pre_aspirate: float = 0.0,
        blowout: float = 0.0,
        mix_cycles: int = 3,
        aspirate_distance: float = 1.0,
        dispense_at_different_distance: bool = False,
        dispense_distance: float = 1.0,
        dynamic_tip_extension: float = 0.0,
        tip_touch: bool = False,
        liquid_class: str | None = None,
        pipette_technique: str | None = None,
    ) -> None:
        location = self._resolve_location(location)
        self._assert_well_access(location)
        liquid_class_def = self._resolve_liquid_class(liquid_class)
        pipette_technique_def = self._resolve_pipette_technique(pipette_technique)
        if liquid_class_def:
            asp_cfg = liquid_class_def.get("aspirate", {})
            dsp_cfg = liquid_class_def.get("dispense", {})
            logger.info(
                "Mix liquid_class=%r  aspirate vel=%.3f µL/s  dispense vel=%.3f µL/s  tip=%s",
                liquid_class_def.get("name"),
                float(asp_cfg.get("w_velocity_ul_s") or 0),
                float(dsp_cfg.get("w_velocity_ul_s") or 0),
                liquid_class_def.get("tip_id"),
            )
        task = MixTask(
            self.controller,
            self._teachpoints,
            location,
            volume=volume,
            pre_aspirate_volume=pre_aspirate,
            blowout_volume=blowout,
            mix_cycles=mix_cycles,
            aspirate_distance=aspirate_distance,
            dispense_distance=dispense_distance,
            dispense_at_different_distance=dispense_at_different_distance,
            safe_z_position=self._profile.safety.z_safe_position,
            labware=self._deck.get_stack(location).top,
            head_type=self._profile.head.head_type,
            head_mode=self._head_mode,
            plate_selection=self._effective_plate_selection(
                location,
                self._deck.get_stack(location).top,
                self._head_mode,
                command_name="Mix",
            ),
            dynamic_tip_extension=dynamic_tip_extension,
            tip_touch=tip_touch,
            liquid_class=liquid_class_def,
            pipette_technique=pipette_technique_def,
            deck=self._deck,
            teach_tip_length_mm=self._profile.head.teach_tip_length_mm,
            attached_tip_length_mm=self._attached_tip_length_mm,
            tips_on_head=self._tips_on_head,
        )
        await self._engine.execute(task)
        if task.status == TaskStatus.ABORTED:
            logger.warning("Mix at location %d aborted by operator.", location)
            return
        self._emit(
            "mixed",
            location=location,
            volume=volume,
            mix_cycles=mix_cycles,
            liquid_class=None if liquid_class_def is None else liquid_class_def.get("name"),
            pipette_technique=None if pipette_technique_def is None else pipette_technique_def.get("name"),
        )

    async def stack_plates(self, *, base_location: int, source_location: int) -> dict[str, Any]:
        if base_location <= 0 or source_location <= 0:
            raise RuntimeError("Stack Plates requires both a base location and a source location")
        if base_location == source_location:
            raise RuntimeError("Stack Plates requires different source and base locations")
        base_labware = self._deck.get_stack(base_location).top  # may be None (empty pad)
        source_labware = self._labware_at_location(source_location)
        if base_labware is not None and (
            self._labware_base_class(base_labware) == "tip_box" or self._labware_kind(base_labware) == "tip_box"
        ):
            raise RuntimeError(f"Base location {base_location} must contain plate-style labware")
        if self._labware_base_class(source_labware) == "tip_box" or self._labware_kind(source_labware) == "tip_box":
            raise RuntimeError(f"Source location {source_location} must contain plate-style labware")
        diagnostics = await self.pick_place(source_location, base_location, speed=SpeedLevel.MED)
        base_plate_name = base_labware.name if base_labware is not None else None
        self._emit(
            "plates_stacked",
            base_location=base_location,
            base_plate=base_plate_name,
            source_location=source_location,
            plate_to_place=source_labware.name,
        )
        return {
            "status": "completed",
            "message": (
                f"Stacked plate from location {source_location} onto "
                f"{'base plate at' if base_plate_name else 'empty pad at'} location {base_location}."
            ),
            "base_location": base_location,
            "base_plate": base_plate_name,
            "source_location": source_location,
            "plate_to_place": source_labware.name,
            "diagnostics": diagnostics,
        }

    async def destack_plate(self, *, source_location: int, destination_location: int) -> dict[str, Any]:
        if source_location <= 0 or destination_location <= 0:
            raise RuntimeError("Destack Plate requires both a source location and a destination location")
        if source_location == destination_location:
            raise RuntimeError("Destack Plate requires different source and destination locations")
        source_stack = self._deck.get_stack(source_location)
        if len(source_stack) < 1:
            raise RuntimeError(f"Source location {source_location} must contain at least one plate")
        source_labware = self._labware_at_location(source_location)
        if self._labware_base_class(source_labware) in {"tip_box", "lid"} or self._labware_kind(source_labware) in {"tip_box", "lid"}:
            raise RuntimeError(f"Source location {source_location} must contain plate-style labware")
        if source_labware.is_mounted:
            raise RuntimeError(
                f"Top plate at location {source_location} is mounted to the plate below it; "
                "use Unmount to separate the pair, not Destack."
            )
        destination_top = self._deck.get_stack(destination_location).top
        if destination_top is not None:
            raise RuntimeError(f"Destination location {destination_location} must be an empty plate pad")
        diagnostics = await self.pick_place(source_location, destination_location, speed=SpeedLevel.MED)
        remaining_top = self._deck.get_stack(source_location).top
        placed_top = self._deck.get_stack(destination_location).top
        self._emit(
            "plate_destacked",
            source_location=source_location,
            destination_location=destination_location,
            plate_to_move=source_labware.name,
        )
        return {
            "status": "completed",
            "message": (
                f"Moved top plate from stack at location {source_location} to empty pad at location {destination_location}."
            ),
            "source_location": source_location,
            "destination_location": destination_location,
            "plate_to_move": source_labware.name,
            "remaining_source_plate": None if remaining_top is None else remaining_top.name,
            "placed_plate": None if placed_top is None else placed_top.name,
            "remaining_stack_count": len(self._deck.get_stack(source_location)),
            "diagnostics": diagnostics,
        }

    async def mount_plates(self, *, base_location: int, source_location: int) -> dict[str, Any]:
        """Stack the source plate onto the base plate AND lock them into a
        mounted pair.

        Physically identical to :meth:`stack_plates` — same pick/place
        motion — but the source plate's ``is_mounted`` flag is set on
        arrival so subsequent pick/place ops transport the pair as a
        single unit (filter-plate-on-collection-plate semantics used
        for vacuum filtration). Liquid-handling ops at the location
        continue to target the top plate of the stack, so aspirate /
        dispense 'into the mounted filter plate' needs no change on
        the caller side.
        """
        if base_location <= 0 or source_location <= 0:
            raise RuntimeError("Mount Plates requires both a base location and a source location")
        if base_location == source_location:
            raise RuntimeError("Mount Plates requires different source and base locations")

        base_labware = self._deck.get_stack(base_location).top
        if base_labware is None:
            raise RuntimeError(
                f"Base location {base_location} must contain a plate for the source to mount onto."
            )
        source_labware = self._labware_at_location(source_location)
        # Same plate-style guards as stack_plates.
        for lw, label in ((base_labware, "Base"), (source_labware, "Source")):
            if self._labware_base_class(lw) == "tip_box" or self._labware_kind(lw) == "tip_box":
                raise RuntimeError(f"{label} location must contain plate-style labware")
        # Soft check on the catalog flags — we warn but don't refuse,
        # so operators can experiment before editing the catalog to
        # mark new plates as mountable. Definitive validation lives
        # in the labware editor UI.
        if not bool(source_labware.metadata.get("can_mount", False)):
            logger.warning(
                "Source labware %r does not have can_mount=True in the catalog; proceeding anyway.",
                source_labware.name,
            )
        if not bool(base_labware.metadata.get("can_be_mounted", False)):
            logger.warning(
                "Base labware %r does not have can_be_mounted=True in the catalog; proceeding anyway.",
                base_labware.name,
            )
        if source_labware.is_mounted:
            raise RuntimeError(
                f"Source plate at location {source_location} is already mounted; "
                "unmount it first before re-mounting elsewhere."
            )

        diagnostics = await self.pick_place(source_location, base_location, speed=SpeedLevel.MED)

        # The plate that was at the top of the source is now at the top of
        # the base stack. Flag it as mounted so subsequent pick/place
        # moves the pair together. We fetch by identity rather than by
        # assumption so if pick_place somehow leaves state unexpected
        # we don't flag the wrong plate.
        dest_top = self._deck.get_stack(base_location).top
        if dest_top is not None and dest_top.id == source_labware.id:
            dest_top.is_mounted = True

        self._emit(
            "plates_mounted",
            base_location=base_location,
            base_plate=base_labware.name,
            source_location=source_location,
            mounted_plate=source_labware.name,
        )
        return {
            "status": "completed",
            "message": (
                f"Mounted plate {source_labware.name!r} (was at location {source_location}) "
                f"onto {base_labware.name!r} at location {base_location}. Pair now moves as a unit."
            ),
            "base_location": base_location,
            "base_plate": base_labware.name,
            "source_location": source_location,
            "mounted_plate": source_labware.name,
            "diagnostics": diagnostics,
        }

    async def unmount_plate(self, *, source_location: int, destination_location: int) -> dict[str, Any]:
        """Separate a mounted pair — move the mounted top plate to
        ``destination_location`` and leave its former base plate at
        ``source_location``.

        The inverse of :meth:`mount_plates`. Refuses to run if the
        top plate at ``source_location`` isn't flagged ``is_mounted``
        (caller probably wanted Destack instead).
        """
        if source_location <= 0 or destination_location <= 0:
            raise RuntimeError("Unmount Plate requires both a source location and a destination location")
        if source_location == destination_location:
            raise RuntimeError("Unmount Plate requires different source and destination locations")

        source_stack = self._deck.get_stack(source_location)
        if len(source_stack) < 2:
            raise RuntimeError(
                f"Location {source_location} must contain a mounted pair (≥2 plates) to unmount."
            )
        source_top = self._labware_at_location(source_location)
        if not source_top.is_mounted:
            raise RuntimeError(
                f"Top plate at location {source_location} is not flagged as mounted; "
                "use Destack for an ordinary unstack operation."
            )
        destination_top = self._deck.get_stack(destination_location).top
        if destination_top is not None:
            raise RuntimeError(
                f"Destination location {destination_location} must be an empty plate pad for an unmounted plate."
            )

        # Clear the mount flag BEFORE pick_place so the task's
        # DeckState update moves only the top plate, not the whole
        # group (which would be pointless — there's nothing to carry
        # along once the pair is being separated).
        source_top.is_mounted = False
        diagnostics = await self.pick_place(source_location, destination_location, speed=SpeedLevel.MED)

        remaining_top = self._deck.get_stack(source_location).top
        self._emit(
            "plate_unmounted",
            source_location=source_location,
            destination_location=destination_location,
            unmounted_plate=source_top.name,
        )
        return {
            "status": "completed",
            "message": (
                f"Unmounted {source_top.name!r} from location {source_location} "
                f"and placed it at location {destination_location}."
            ),
            "source_location": source_location,
            "destination_location": destination_location,
            "unmounted_plate": source_top.name,
            "remaining_source_plate": None if remaining_top is None else remaining_top.name,
            "diagnostics": diagnostics,
        }

    async def delid_plate(self, *, plate_location: int, lid_destination: int) -> dict[str, Any]:
        if plate_location <= 0 or lid_destination <= 0:
            raise RuntimeError("Delid Plate requires both a plate location and a lid destination")
        if plate_location == lid_destination:
            raise RuntimeError("Delid Plate requires different plate and lid-destination locations")
        plate_labware = self._labware_at_location(plate_location)
        if self._labware_base_class(plate_labware) == "tip_box" or self._labware_kind(plate_labware) == "tip_box":
            raise RuntimeError(f"Plate location {plate_location} must contain plate-style labware")
        if not plate_labware.is_lidded:
            raise RuntimeError(f"Plate at location {plate_location} does not currently have a lid")
        can_have_lid = bool((plate_labware.metadata or {}).get("can_have_lid", False))
        if not can_have_lid:
            raise RuntimeError(
                f"Labware at location {plate_location} does not support lids: {plate_labware.name}"
            )
        lid_target = self._deck.get_stack(lid_destination).top
        if lid_target is not None and (
            self._labware_base_class(lid_target) == "tip_box" or self._labware_kind(lid_target) == "tip_box"
        ):
            raise RuntimeError(f"Lid destination {lid_destination} cannot be a tip box location")
        task = DelidPlateTask(
            self.controller,
            self._teachpoints,
            self._profile,
            self._deck,
            plate_location,
            lid_destination,
            speed=SpeedLevel.MED,
        )
        await self._engine.execute(task)
        if task.status == TaskStatus.ABORTED:
            logger.warning("Delid plate aborted by operator.")
            return {"status": "aborted", "message": "Delid plate aborted by operator.", "plate_location": plate_location, "lid_destination": lid_destination}
        self._emit(
            "plate_delidded",
            plate_location=plate_location,
            plate_to_delid=plate_labware.name,
            lid_destination=lid_destination,
        )
        destination_top = self._deck.get_stack(lid_destination).top
        return {
            "status": "completed",
            "message": (
                f"Removed lid from plate at location {plate_location} and placed it at location {lid_destination}."
            ),
            "plate_location": plate_location,
            "plate_to_delid": plate_labware.name,
            "lid_destination": lid_destination,
            "lid_name": None if destination_top is None else destination_top.name,
            "current_destination_labware": None if lid_target is None else lid_target.name,
            "diagnostics": task.debug_plan(),
        }

    async def relid_plate(self, *, lid_location: int, plate_location: int) -> dict[str, Any]:
        if lid_location <= 0 or plate_location <= 0:
            raise RuntimeError("Relid Plate requires both a lid location and a plate location")
        if lid_location == plate_location:
            raise RuntimeError("Relid Plate requires different lid and plate locations")
        lid_labware = self._labware_at_location(lid_location)
        if self._labware_base_class(lid_labware) != "lid" and self._labware_kind(lid_labware) != "lid":
            raise RuntimeError(f"Lid location {lid_location} must contain standalone lid labware")
        plate_labware = self._labware_at_location(plate_location)
        if self._labware_base_class(plate_labware) == "tip_box" or self._labware_kind(plate_labware) == "tip_box":
            raise RuntimeError(f"Plate location {plate_location} must contain plate-style labware")
        if self._labware_base_class(plate_labware) == "lid" or self._labware_kind(plate_labware) == "lid":
            raise RuntimeError(f"Plate location {plate_location} must contain a plate, not a lid")
        if plate_labware.is_lidded:
            raise RuntimeError(f"Plate at location {plate_location} already has a lid")
        if plate_labware.is_sealed:
            raise RuntimeError(f"Plate at location {plate_location} is sealed and cannot be relidded")
        can_have_lid = bool((plate_labware.metadata or {}).get("can_have_lid", False))
        if not can_have_lid:
            raise RuntimeError(
                f"Labware at location {plate_location} does not support lids: {plate_labware.name}"
            )
        task = RelidPlateTask(
            self.controller,
            self._teachpoints,
            self._profile,
            self._deck,
            lid_location,
            plate_location,
            speed=SpeedLevel.MED,
        )
        await self._engine.execute(task)
        if task.status == TaskStatus.ABORTED:
            logger.warning("Relid plate aborted by operator.")
            return {"status": "aborted", "message": "Relid plate aborted by operator.", "lid_location": lid_location, "plate_location": plate_location}
        self._emit(
            "plate_relidded",
            lid_location=lid_location,
            lid_name=lid_labware.name,
            plate_location=plate_location,
            plate_to_relid=plate_labware.name,
        )
        source_top = self._deck.get_stack(lid_location).top
        destination_top = self._deck.get_stack(plate_location).top
        return {
            "status": "completed",
            "message": (
                f"Placed lid from location {lid_location} onto plate at location {plate_location}."
            ),
            "lid_location": lid_location,
            "lid_name": lid_labware.name,
            "plate_location": plate_location,
            "plate_to_relid": plate_labware.name,
            "current_source_labware": None if source_top is None else source_top.name,
            "current_plate_labware": None if destination_top is None else destination_top.name,
            "diagnostics": task.debug_plan(),
        }

    async def scan_stack_height(self, *, location: int, manual_count: int | None = None, expected_count: int | None = None) -> dict[str, Any]:
        if location <= 0:
            raise RuntimeError("Scan Stack Height requires a valid location")
        template_labware = self._labware_at_location(location)
        if self._labware_base_class(template_labware) == "tip_box" or self._labware_kind(template_labware) == "tip_box":
            raise RuntimeError(f"Scan Stack Height requires plate-style labware at location {location}")

        if manual_count is not None:
            count = max(0, int(manual_count))
            self._rebuild_stack_from_template(location, template_labware, count)
            stack_height = float(template_labware.stack_height or template_labware.height or 0.0)
            plate_height = float(template_labware.height or 0.0)
            theoretical_height = _stacking_support_height_for_count(count, stack_height)
            estimated_total_height = _stack_total_height_for_count(count, plate_height, stack_height)
            result = {
                "status": "completed",
                "location": location,
                "configured_labware": template_labware.name,
                "manual_count": count,
                "used_manual_override": True,
                "inferred_count": count,
                "stack_height_mm": stack_height,
                "plate_height_mm": plate_height,
                "theoretical_height_mm": theoretical_height,
                "estimated_total_height_mm": estimated_total_height,
                "rounded_stack_height_mm": round(theoretical_height),
                "message": f"Applied manual stack count {count} at location {location}.",
            }
            self._emit("stack_scanned", **result)
            return result

        task = ScanStackHeightTask(
            self.controller,
            self._teachpoints,
            self._profile,
            self._deck,
            location=location,
            template_labware=template_labware,
            expected_count=expected_count,
        )
        await self._engine.execute(task)
        if task.status == TaskStatus.ABORTED:
            logger.warning("Scan stack height at location %d aborted by operator.", location)
            return {"status": "aborted", "location": location, "message": "Scan stack height aborted by operator."}
        result = task.result_payload()
        if result.get("status") == "completed":
            self._rebuild_stack_from_template(location, template_labware, int(result.get("inferred_count") or 0))
            self._emit("stack_scanned", **result)
        return result

    async def tips_on(self, location: int | str) -> None:
        location = self._resolve_location(location)
        if self._tips_on_head:
            raise RuntimeError("Tips are already on the head")
        labware = self._require_tip_box(location, operation="Tips on")
        tip_selection = self._effective_tip_selection(location, labware, self._head_mode)
        logger.warning(
            (
                "Tips On at location %d: head_mode=%s %dx%d config=%s (%d channels) | "
                "selected tip region rows %d-%d cols %d-%d mirror=%s anchor=(r%d,c%d)"
            ),
            location,
            self._head_mode.subset_type,
            self._head_mode.row_count,
            self._head_mode.column_count,
            self._head_mode.subset_config,
            self._head_mode.num_channels,
            tip_selection.row + 1,
            tip_selection.row + tip_selection.row_count,
            tip_selection.col + 1,
            tip_selection.col + tip_selection.column_count,
            tip_selection.mirror_corner,
            tip_selection.to_dict()["anchor_row"] + 1,
            tip_selection.to_dict()["anchor_col"] + 1,
        )
        tip_length_mm = self._tip_length_for_labware(labware)
        tip_offsets = self._resolve_tip_offsets(labware)
        logger.info(
            "Tips On offsets for '%s': press tolerance=%.2f mm, z_offset=%.2f mm (%s)",
            labware.name,
            tip_offsets.tips_on_jog_tolerance,
            tip_offsets.tips_on_z_offset,
            tip_offsets.source,
        )
        task = TipsOnTask(
            self.controller,
            self._teachpoints,
            self._profile,
            labware,
            self._head_mode,
            tip_selection,
            location,
            tip_length_mm=tip_length_mm,
            safe_z_position=self._profile.safety.z_safe_position,
            deck=self._deck,
            tip_offsets=tip_offsets,
        )
        await self._engine.execute(task)
        if task.status == TaskStatus.ABORTED:
            logger.warning("Tips On at location %d was aborted; not marking tips as on head.", location)
            return
        self._apply_tipbox_selection(location, self._head_mode, tip_selection, purpose="pickup")
        self._set_tip_state(
            labware_name=labware.name,
            tip_definition_id=self._tip_id_for_labware(labware),
            tip_length_mm=tip_length_mm,
            head_mode=self._head_mode,
            tip_selection=tip_selection,
        )
        self._emit(
            "tips_on",
            location=location,
            labware=labware.name,
            tip_length_mm=self._attached_tip_length_mm,
            head_mode=self._head_mode.to_dict(),
        )

    async def tips_off(self, location: int | str) -> None:
        location = self._resolve_location(location)
        tips_are_tracked = bool(self._tips_on_head)
        tip_length = self._attached_tip_length_mm or 9.0
        labware = self._require_tip_receptacle(location, operation="Tips off")
        target_selection: TipSelection | None = None
        if self._labware_base_class(labware) == "tip_box" or self._labware_kind(labware) == "tip_box":
            target_selection = self._effective_tip_selection(
                location,
                labware,
                self._tips_on_head_mode or self._head_mode,
                purpose="return",
            )
        effective_mode = self._tips_on_head_mode or self._head_mode
        if target_selection is not None:
            logger.warning(
                "Tips Off at location %d: head_mode=%s %dx%d config=%s | "
                "return region rows %d-%d cols %d-%d mirror=%s anchor=(r%d,c%d)",
                location,
                effective_mode.subset_type,
                effective_mode.row_count,
                effective_mode.column_count,
                effective_mode.subset_config,
                target_selection.row + 1,
                target_selection.row + target_selection.row_count,
                target_selection.col + 1,
                target_selection.col + target_selection.column_count,
                target_selection.mirror_corner,
                target_selection.to_dict()["anchor_row"] + 1,
                target_selection.to_dict()["anchor_col"] + 1,
            )
        tip_offsets = self._resolve_tip_offsets(labware)
        logger.info(
            "Tips Off offsets for '%s': z_offset=%.2f mm, w_position=%.2f (%s)",
            labware.name,
            tip_offsets.tips_off_z_offset,
            tip_offsets.tips_off_w_position,
            tip_offsets.source,
        )
        task = TipsOffTask(
            self.controller,
            self._teachpoints,
            self._profile,
            labware,
            effective_mode,
            target_selection,
            location,
            attached_tip_length_mm=tip_length,
            safe_z_position=self._profile.safety.z_safe_position,
            deck=self._deck,
            tips_are_tracked=tips_are_tracked,
            tip_offsets=tip_offsets,
        )
        await self._engine.execute(task)
        if task.status == TaskStatus.ABORTED:
            logger.warning("Tips Off at location %d aborted by operator; tips remain marked on head.", location)
            return
        if target_selection is not None:
            self._apply_tipbox_selection(location, effective_mode, target_selection, purpose="return")
        self._clear_tip_state()
        self._emit("tips_off", location=location, labware=labware.name)

    async def move_gripper_to_location(
        self,
        location: int,
        approach_height: float = 0.0,
        speed: SpeedLevel = SpeedLevel.MED,
    ) -> dict[str, Any]:
        """Position the gripper over a location for Y-offset teaching.

        Runs the approach half of a real pick and stops — the gripper is never
        closed, so nothing is lifted. Raises if the location holds no labware:
        there is nothing to align against.
        """
        task = GripperTeachMoveTask(
            self.controller,
            self._teachpoints,
            self._profile,
            self._deck,
            location,
            approach_height=approach_height,
            speed=speed,
        )
        await self._engine.execute(task)
        if task.status == TaskStatus.ABORTED:
            logger.warning("Gripper teach move to %d aborted by operator.", location)
            return {"status": "aborted", "location": location}
        return {
            "status": "moved",
            "location": location,
            "approach_height": approach_height,
            "gripper_y_offset": self._profile.gripper.y_offset,
        }

    def teach_gripper_y_offset(self, location: int) -> dict[str, Any]:
        """Capture the gripper's Y alignment at ``location`` into the profile.

        The operator jogs until the gripper is centred on the plate; the offset
        is how far Y sits from that location's teachpoint.

        The stored value excludes the per-head constant. Pick/place computes
        ``profile.gripper.y_offset + head_y_offset`` (see
        ``PickPlaceTask._gripper_y_offset``), so storing the raw delta would add
        the head term twice — a 2.25 mm error on 384-class heads.
        """
        try:
            teach_y = self._teachpoints.get_teachpoint(location, Axis.Y)
        except KeyError as exc:
            # get_teachpoint raises rather than returning None; without this the
            # operator gets an opaque 500 instead of being told what is missing.
            raise RuntimeError(
                f"Location {location} has no taught Y position yet — teach the "
                "location on the Jog/Teach tab before teaching the gripper offset."
            ) from exc

        current_y = float(self.get_position(Axis.Y))
        _, head_y_offset = _gripper_head_offsets(self._profile.head.head_type)
        measured = current_y - float(teach_y)
        previous = float(self._profile.gripper.y_offset)
        self._profile.gripper.y_offset = measured - head_y_offset

        logger.info(
            "Taught gripper Y offset at location %d: Y=%.3f, teachpoint Y=%.3f, "
            "measured %.3f mm, head constant %.3f mm -> stored %.3f mm (was %.3f)",
            location, current_y, float(teach_y), measured, head_y_offset,
            self._profile.gripper.y_offset, previous,
        )
        self._emit("gripper_y_offset_taught", location=location,
                   y_offset=self._profile.gripper.y_offset)
        return {
            "status": "taught",
            "location": location,
            "current_y": current_y,
            "teachpoint_y": float(teach_y),
            "measured_offset": measured,
            "head_y_offset": head_y_offset,
            "y_offset": self._profile.gripper.y_offset,
            "previous_y_offset": previous,
        }

    async def pick_place(
        self,
        from_location: int | str,
        to_location: int,
        speed: SpeedLevel = SpeedLevel.MED,
    ) -> dict[str, Any]:
        from_loc = self._resolve_location(from_location)
        task = PickPlaceTask(
            self.controller,
            self._teachpoints,
            self._profile,
            self._deck,
            from_loc,
            to_location,
            speed=speed,
        )
        await self._engine.execute(task)
        if task.status == TaskStatus.ABORTED:
            logger.warning("Pick/place %d->%d aborted by operator.", from_loc, to_location)
            return {"status": "aborted", "from_location": from_loc, "to_location": to_location, "message": "Pick/place aborted by operator."}
        for name in list(self._labware_names):
            if self._labware_names[name] == from_loc:
                self._labware_names[name] = to_location
        self._emit("pick_placed", from_location=from_loc, to_location=to_location)
        return task.debug_plan()

    async def move_axis(self, axis: Axis, position: float,
                        velocity: float = 0.0, acceleration: float = 0.0) -> None:
        move = AxisMoveInfo(axis=axis, position=position,
                          velocity=velocity, acceleration=acceleration)
        self.controller.move([move])
        self._emit("axis_moved", axis=axis.name, position=position)

    async def jog_axis(self, axis: Axis, step: float, speed: SpeedLevel = SpeedLevel.MED, peak_current: float | None = None) -> float:
        """Jog an axis by a relative step. Returns new position.

        If peak_current is set, uses a force-limited jog (controller.jog) that
        stops when the motor current exceeds the limit.  Used for diagnostics
        and verifying current feedback before tip operations.
        """
        velocity, acceleration = self._speed_profile(axis, speed)
        if peak_current is not None:
            from pybravo.controllers.base import JogParams
            current_pos = self.controller.get_position(axis)
            target = current_pos + step
            logger.warning(
                "Force-limited jog %s: current=%.3f target=%.3f peak_current=%.3f",
                axis.name, current_pos, target, peak_current,
            )
            try:
                new_pos = self.controller.jog(JogParams(
                    axis=axis,
                    velocity=velocity,
                    acceleration=acceleration,
                    max_position=target,
                    tolerance=2.0,
                    peak_current=peak_current,
                ))
            except Exception as exc:
                # Force-limited jog often "exceeds destination" — the motor
                # hits the current limit and overshoots.  Return actual position.
                new_pos = self.controller.get_position(axis)
                logger.warning(
                    "Force-limited jog stopped: %s (actual position %.3f)",
                    exc, new_pos,
                )
        else:
            move = AxisMoveInfo(axis=axis, position=step, velocity=velocity, acceleration=acceleration, absolute=False)
            self.controller.move([move])
            new_pos = self.controller.get_position(axis)
        self._emit("axis_jogged", axis=axis.name, step=step, position=new_pos)
        return new_pos

    async def home_single_axis(self, axis: Axis) -> None:
        """Home one axis on explicit operator request.

        This forces the full routine even when the axis already reports itself
        homed. Without that, pressing Home on a healthy axis silently does
        nothing, because the controller's start-up path deliberately skips
        axes that are already initialized.

        W is then returned to 0, matching what a cold initialize leaves behind
        (see InitializeTask._home_w) — the plunger is expected to sit at zero
        after homing, and the operator asked for the same end state.
        """
        logger.info("Homing %s axis (operator request; forced)...", axis.name)
        self.controller.home_axes([axis], force=True)

        if axis is Axis.W:
            current = float(self.controller.get_position(Axis.W))
            if abs(current) > AXIS_EPSILON:
                logger.info("Parking W at 0.0 uL after homing (current %.3f uL)...", current)
                self.controller.move([AxisMoveInfo(axis=Axis.W, position=0.0)], wait=True)

        self._homed_axes.add(axis)
        self._emit("homed", axes=[axis.name])

    def enable_motor(self, axis: Axis) -> None:
        self.controller.enable_motor(axis)
        self._emit("motor_enabled", axis=axis.name)

    def disable_motor(self, axis: Axis) -> None:
        self.controller.disable_motor(axis)
        self._emit("motor_disabled", axis=axis.name)

    def open_gripper(self) -> None:
        self.controller.open_gripper()
        self._emit("gripper_opened")

    def close_gripper(self, position: float = 5.0) -> None:
        self.controller.grip(SpeedLevel.MED, position)
        self._emit("gripper_closed")

    async def dock_gripper(self) -> dict[str, float | bool]:
        task = DockGripperTask(self.controller, self._profile, force_if_plate_detected=True)
        await self._engine.execute(task)
        if task.status == TaskStatus.ABORTED:
            logger.warning("Dock gripper aborted by operator.")
            return {"status": "aborted", "message": "Dock gripper aborted by operator."}
        self._emit("gripper_docked", g_target=task._g_target, zg_target=task._zg_target)
        return {
            "g_target": task._g_target,
            "zg_target": task._zg_target,
            "forced_plate_sensor": bool(getattr(task, "_plate_detected", False)),
        }

    # -- State queries --

    def get_position(self, axis: Axis) -> float:
        return self.controller.get_position(axis)

    def get_all_positions(self) -> dict[str, float]:
        if self._controller is not None and hasattr(self._controller, "get_all_positions"):
            try:
                return self._controller.get_all_positions()
            except Exception as exc:
                logger.debug("Bulk position read failed, falling back to per-axis reads: %s", exc)
        positions: dict[str, float] = {}
        for axis in Axis:
            try:
                positions[axis.name] = self.controller.get_position(axis)
            except Exception as exc:
                logger.debug("Skipping position read for %s: %s", axis.name, exc)
        return positions

    @property
    def deck(self) -> DeckState:
        return self._deck

    @property
    def labware_catalog(self) -> LabwareCatalog:
        return self._labware_catalog

    @property
    def profile(self) -> BravoProfile:
        return self._profile

    @property
    def teachpoints(self) -> Teachpoints:
        return self._teachpoints

    @property
    def engine(self) -> StateMachineEngine:
        return self._engine

    def _is_real_hardware_motion_active(self) -> bool:
        return (
            self._engine.is_busy
            and self._profile.connection.controller_type in {"darwin", "darwin_native", "agile", "agile_7612"}
        )

    def _cached_controller_positions(self) -> dict[str, float]:
        ctrl = self._controller
        if ctrl is None:
            return {}

        snapshot = getattr(ctrl, "_last_snapshot", None)
        if isinstance(snapshot, dict):
            raw_positions = snapshot.get("positions", {}) or {}
            if isinstance(raw_positions, dict):
                return {
                    str(axis): float(value)
                    for axis, value in raw_positions.items()
                    if isinstance(value, (int, float))
                }

        raw_positions = getattr(ctrl, "_positions", None)
        if isinstance(raw_positions, list):
            positions: dict[str, float] = {}
            for axis in Axis:
                if axis.value < len(raw_positions):
                    value = raw_positions[axis.value]
                    if isinstance(value, (int, float)):
                        positions[axis.name] = float(value)
            return positions

        return {}

    def _cached_controller_state(self) -> dict[str, Any]:
        ctrl = self._controller
        positions = self._cached_controller_positions()
        telemetry: dict[str, Any] = {}
        motors_enabled: dict[str, bool] = {}
        head_attached = False
        go_button = False
        robot_disabled = False

        if ctrl is not None:
            snapshot = getattr(ctrl, "_last_snapshot", None)
            if isinstance(snapshot, dict):
                telemetry = dict(snapshot.get("telemetry", {}) or {})
                motors_enabled = {
                    str(axis): bool(enabled)
                    for axis, enabled in (snapshot.get("motors_enabled", {}) or {}).items()
                }
                head_attached = bool(snapshot.get("head_attached", False))
                go_button = bool(snapshot.get("go_button_pressed", False))
                robot_disabled = bool(snapshot.get("robot_disabled", False))

            if not motors_enabled:
                raw_motors = getattr(ctrl, "_motor_enabled", None)
                if isinstance(raw_motors, list):
                    for axis in Axis:
                        if axis.value < len(raw_motors):
                            motors_enabled[axis.name] = bool(raw_motors[axis.value])

        plate_in_gripper = self._resolve_plate_presence(positions.get("G"))

        return {
            "positions": positions,
            "motors_enabled": motors_enabled,
            "head_attached": head_attached,
            "go_button_pressed": go_button,
            "plate_in_gripper": plate_in_gripper,
            "robot_disabled": robot_disabled,
            "telemetry": telemetry,
        }

    def _darwin_nominal_head_adc(self, head_type: HeadType) -> int | None:
        resistance = _DARWIN_HEAD_RESISTOR_OHMS.get(head_type)
        if resistance is None:
            return None
        thevenin = (float(resistance) * 249000.0) / (float(resistance) + 249000.0)
        j2_8 = 5.0 * thevenin / (thevenin + 5110.0)
        u21_1 = j2_8 * 147000.0 / 249000.0
        return int(4096.0 * (u21_1 / 3.0))

    def _darwin_detect_analog_head(self, ctrl: BravoController) -> bool:
        tolerance = int(getattr(getattr(self._profile, "safety", None), "head_tolerance", 25) or 25)
        cached_adc_value = getattr(ctrl, "_last_head_adc", None)
        if cached_adc_value is None:
            try:
                adc_value = int(ctrl.read_head_adc())
            except Exception as exc:
                logger.debug("Skipping analog head detection during state read: %s", exc)
                return False
        else:
            adc_value = int(cached_adc_value)

        expected_nominal = self._darwin_nominal_head_adc(self._profile.head.head_type)
        if expected_nominal is not None and abs(adc_value - expected_nominal) <= tolerance:
            return True

        for head_type in _DARWIN_HEAD_RESISTOR_OHMS:
            nominal = self._darwin_nominal_head_adc(head_type)
            if nominal is not None and abs(adc_value - nominal) <= tolerance:
                return True
        return False

    def _detect_head_attached(self, ctrl: BravoController) -> bool:
        try:
            if ctrl.detect_smart_head():
                return True
        except Exception as exc:
            logger.debug("Skipping smart-head detection during state read: %s", exc)

        if self._profile.connection.controller_type in ("darwin", "darwin_native"):
            return self._darwin_detect_analog_head(ctrl)
        return False

    def _axes_expected_home(self) -> list[Axis]:
        """Axes a full home covers on this machine — the readiness benchmark."""
        axes = [Axis.X, Axis.Y, Axis.Z]
        if not self._profile.safety.ignore_w_axis:
            axes.append(Axis.W)
        has_gripper = getattr(self.controller, "HAS_GRIPPER", True) if self._controller else True
        if has_gripper and "G" in self._profile.axes and "Zg" in self._profile.axes:
            axes.extend([Axis.G, Axis.Zg])
        return axes

    def get_state(self) -> dict[str, Any]:
        """Return complete robot state as a JSON-serializable dict."""
        positions = self.get_all_positions() if self.is_connected else {}
        ctrl = self._controller

        motors_enabled: dict[str, bool] = {}
        head_attached = False
        go_button = False
        plate_in_gripper = False
        robot_disabled = False
        telemetry: dict[str, Any] = {}
        live_reads_allowed = not self._is_real_hardware_motion_active()

        if ctrl is not None:
            if not live_reads_allowed:
                cached = self._cached_controller_state()
                positions = cached["positions"] or positions
                motors_enabled = dict(cached["motors_enabled"])
                head_attached = bool(cached["head_attached"])
                go_button = bool(cached["go_button_pressed"])
                plate_in_gripper = bool(cached["plate_in_gripper"])
                robot_disabled = bool(cached["robot_disabled"])
                telemetry = dict(cached["telemetry"] or {})
            elif hasattr(ctrl, "get_state_snapshot"):
                try:
                    snapshot = ctrl.get_state_snapshot()
                    positions = snapshot.get("positions", positions)
                    motors_enabled = dict(snapshot.get("motors_enabled", {}))
                    head_attached = bool(snapshot.get("head_attached", False))
                    go_button = bool(snapshot.get("go_button_pressed", False))
                    robot_disabled = bool(snapshot.get("robot_disabled", False))
                    telemetry = dict(snapshot.get("telemetry", {}) or {})
                except Exception as exc:
                    logger.debug("Bundled state snapshot failed, falling back to per-field reads: %s", exc)

            if live_reads_allowed:
                raw_motors = getattr(ctrl, "_motor_enabled", None)
                for ax in Axis:
                    cached_enabled = False
                    if isinstance(raw_motors, list) and ax.value < len(raw_motors):
                        cached_enabled = bool(raw_motors[ax.value])
                    needs_verify = not motors_enabled or not motors_enabled.get(ax.name, False)
                    if not needs_verify:
                        continue
                    try:
                        if hasattr(ctrl, "is_motor_enabled"):
                            live_enabled = bool(ctrl.is_motor_enabled(ax))
                            motors_enabled[ax.name] = live_enabled or cached_enabled
                        else:
                            motors_enabled[ax.name] = cached_enabled
                    except Exception as exc:
                        logger.debug("Skipping motor-enabled read for %s: %s", ax.name, exc)
                        motors_enabled[ax.name] = cached_enabled
            if live_reads_allowed and not head_attached:
                head_attached = self._detect_head_attached(ctrl)
            if live_reads_allowed and not go_button:
                try:
                    go_button = ctrl.is_go_button_pressed()
                except Exception as exc:
                    logger.debug("Skipping go-button read during state read: %s", exc)
            if live_reads_allowed:
                try:
                    plate_in_gripper = self._resolve_plate_presence(
                        positions.get("G"),
                        sensor_detected=bool(ctrl.is_plate_in_gripper()),
                    )
                except Exception as exc:
                    logger.debug("Skipping gripper sensor read during state read: %s", exc)
                    plate_in_gripper = self._resolve_plate_presence(positions.get("G"))
            if live_reads_allowed and not robot_disabled:
                try:
                    state_flags = ctrl.query_state()
                    robot_disabled = bool(state_flags & DeviceStateFlag.ROBOT_DISABLE)
                except Exception as exc:
                    logger.debug("Skipping device-state read during state read: %s", exc)

        tp_data: dict[str, dict[str, float]] = {}
        for loc in range(1, 10):
            try:
                tp_data[str(loc)] = {
                    "x": self._teachpoints.get_teachpoint(loc, Axis.X),
                    "y": self._teachpoints.get_teachpoint(loc, Axis.Y),
                    "z": self._teachpoints.get_teachpoint(loc, Axis.Z),
                }
            except KeyError:
                pass

        task_status: dict[str, Any] = {}
        current_task = self._engine.current_task
        if current_task is not None:
            try:
                task_status = dict(current_task.status_payload() or {})
                task_status["step"] = getattr(current_task, "_current_step_name", None)
                task_status["step_index"] = int(getattr(current_task, "_current_step_index", 0))
                task_status["step_count"] = len(current_task.get_steps())
                task_status["status"] = current_task.status.name.lower()
                if current_task.error is not None:
                    task_status["error"] = {
                        "message": current_task.error.message,
                        "step_name": current_task.error.step_name,
                    }
            except Exception as exc:
                logger.debug("Skipping task-status read: %s", exc)

        tipbox_inventory = self._tipbox_inventory_state()

        return {
            "connected": self.is_connected,
            "initialized": self._initialized,
            "homed": bool(self._controller) and not (
                set(self._axes_expected_home()) - self._homed_axes
            ),
            "homed_axes": sorted(a.name for a in self._homed_axes),
            "positions": positions,
            "head_type": self._profile.head.head_type.name,
            "machine_id": self.machine_id,
            "active_tip_id": self.active_tip_id(),
            "active_tip_capacity_ul": self.active_tip_capacity_ul(),
            "head_mode": self._head_mode.to_dict(),
            "tips_on_head_mode": None if self._tips_on_head_mode is None else self._tips_on_head_mode.to_dict(),
            "tip_selection": None if self._tip_selection is None else self._tip_selection.to_dict(),
            "tips_on_head_selection": None if self._tips_on_head_selection is None else self._tips_on_head_selection.to_dict(),
            "plate_selection": {
                str(location): selection.to_dict()
                for location, selection in sorted(self._plate_selection.items())
            },
            "controller_type": self._profile.connection.controller_type,
            "deck": {str(loc): [lw.name for lw in stack.items]
                     for loc, stack in self._deck._stacks.items() if stack.items},
            "deck_details": {
                # Attach live is_mounted state to each entry so the
                # designer's 3D view can eventually draw a mount
                # indicator on plates that are locked to whatever is
                # beneath them (mirrors Labware.is_mounted; travels
                # with the instance across moves).
                str(loc): [
                    {
                        **(lw.metadata or {"name": lw.name, "height_mm": lw.height}),
                        "is_mounted": bool(lw.is_mounted),
                    }
                    for lw in stack.items
                ]
                for loc, stack in self._deck._stacks.items() if stack.items
            },
            "engine_busy": self._engine.is_busy,
            "motors_enabled": motors_enabled,
            "telemetry": telemetry,
            "head_attached": head_attached,
            "go_button_pressed": go_button,
            "plate_in_gripper": plate_in_gripper,
            "tips_on_head": self._tips_on_head,
            "tip_labware": self._tip_labware_name,
            "tip_definition_id": self._tip_definition_id,
            "attached_tip_length_mm": self._attached_tip_length_mm,
            "tipbox_inventory": tipbox_inventory,
            "robot_disabled": robot_disabled,
            "teachpoints": tp_data,
            "task_status": task_status,
        }

    @property
    def machine_id(self) -> str:
        value = str(getattr(self._profile.connection, "machine_id", "") or "").strip()
        if value:
            return value
        if self._profile.connection.controller_type == "simulation":
            return "SIM_BRAVO"
        return "BRAVO"

    def active_tip_capacity_ul(self) -> float:
        return get_tip_capacity_ul(
            self._profile.head.head_type,
            self.active_tip_id()
            or getattr(self._profile.head, "teach_tip_id", None)
            or getattr(self._profile.head, "default_tip_id", None)
            or float(getattr(self._profile.head, "teach_tip_capacity", 0.0) or 0.0)
            or float(getattr(self._profile.head, "default_tip_capacity", 0.0) or 0.0),
        )

    def active_tip_id(self) -> str:
        if self._tips_on_head and self._tip_definition_id:
            return str(self._tip_definition_id)
        if self._tips_on_head and self._tip_labware_name:
            detail = self._find_labware_definition_by_name(self._tip_labware_name)
            if detail:
                tip_id = str(getattr(detail, "tip_definition_id", "") or "").strip()
                if tip_id:
                    return tip_id
        taught = str(getattr(self._profile.head, "teach_tip_id", "") or "").strip()
        if taught:
            return taught
        default_tip = str(getattr(self._profile.head, "default_tip_id", "") or "").strip()
        if default_tip:
            return default_tip
        legacy_capacity = float(getattr(self._profile.head, "teach_tip_capacity", 0.0) or 0.0) or float(
            getattr(self._profile.head, "default_tip_capacity", 0.0) or 0.0
        )
        return get_tip_id_for_capacity(self._profile.head.head_type, legacy_capacity) or (
            get_default_tip_id_for_head(self._profile.head.head_type) or ""
        )

    def _find_labware_definition_by_name(self, name: str):
        for definition in self._labware_catalog.list_definitions():
            if definition.name == name or definition.id == name:
                return definition
        return None

    def _resolve_liquid_class(self, name: str | None) -> dict[str, Any] | None:
        if not name:
            return None
        item = liquid_classes_store.get_liquid_class(
            str(name),
            machine_id=self.machine_id,
            head_type=self._profile.head.head_type.name,
            tip_id=self.active_tip_id(),
            tip_capacity_ul=self.active_tip_capacity_ul(),
        )
        if item is None:
            raise RuntimeError(
                f"Unknown liquid class '{name}' for {self.machine_id} / {self._profile.head.head_type.name} / {self.active_tip_id() or f'{self.active_tip_capacity_ul():.0f} uL'}"
            )
        return item

    @staticmethod
    def _resolve_pipette_technique(name: str | None) -> dict[str, Any] | None:
        if not name:
            return None
        item = liquid_classes_store.get_pipette_technique(str(name))
        if item is None:
            raise RuntimeError(f"Unknown pipette technique '{name}'")
        return item

    def set_labware(
        self,
        location: int,
        labware_id: str,
        *,
        name: str | None = None,
        is_lidded: bool = False,
        is_sealed: bool = False,
        tip_definition_id: str | None = None,
        tipbox_fill_state: str | None = None,
        track_tips: bool = True,
    ) -> Labware:
        definition = self._labware_catalog.get_definition(labware_id)
        if definition is None:
            raise ValueError(f"Unknown labware definition: {labware_id}")
        labware = Labware.from_definition(definition, is_lidded=is_lidded, is_sealed=is_sealed)
        if tip_definition_id is not None:
            labware.metadata["tip_definition_id"] = str(tip_definition_id)
        elif not labware.metadata.get("tip_definition_id"):
            supported_tip_ids = list(labware.metadata.get("supported_tip_ids") or [])
            if supported_tip_ids:
                labware.metadata["tip_definition_id"] = str(supported_tip_ids[0])
        if not labware.metadata.get("tip_definition_id"):
            inferred_tip_id = get_tip_id_for_capacity(
                self._profile.head.head_type,
                labware.metadata.get("disposable_tip_capacity_ul"),
            )
            if inferred_tip_id:
                labware.metadata["tip_definition_id"] = inferred_tip_id
        if not labware.metadata.get("supported_tip_ids"):
            tip_id = str(labware.metadata.get("tip_definition_id") or "").strip()
            if tip_id:
                labware.metadata["supported_tip_ids"] = [tip_id]
        self._deck.set_single(location, labware)
        self._plate_selection.pop(location, None)
        self._initialize_tipbox_occupancy(location, labware, fill_state=tipbox_fill_state or "full")
        if not track_tips:
            self._tipbox_untracked.add(location)
        else:
            self._tipbox_untracked.discard(location)
        if name is not None:
            self._labware_names = {k: v for k, v in self._labware_names.items() if k != name}
            self._labware_names[name] = location
        self._emit("deck_updated", location=location, labware=labware.name)
        return labware

    def clear_labware(self, location: int | str) -> None:
        location = self._resolve_location(location)
        self._deck.clear(location)
        self._plate_selection.pop(location, None)
        self._tipbox_occupancy.pop(location, None)
        self._labware_names = {k: v for k, v in self._labware_names.items() if v != location}
        self._emit("deck_updated", location=location, labware=None)

    def _resolve_location(self, location: int | str) -> int:
        if isinstance(location, int):
            return location
        if location not in self._labware_names:
            raise ValueError(f"No labware named '{location}' on deck")
        return self._labware_names[location]

    def location_of(self, name: str) -> int:
        if name not in self._labware_names:
            raise ValueError(f"No labware named '{name}' on deck")
        return self._labware_names[name]

    @staticmethod
    def _preserve_live_labware_metadata(refreshed: Labware, existing: Labware) -> Labware:
        existing_metadata = dict(existing.metadata or {})
        if "tip_definition_id" in existing_metadata:
            refreshed.metadata["tip_definition_id"] = str(existing_metadata.get("tip_definition_id") or "")
        if "supported_tip_ids" in existing_metadata:
            refreshed.metadata["supported_tip_ids"] = list(existing_metadata.get("supported_tip_ids") or [])
        return refreshed

    def _rebuild_live_labware(self, current: Labware, definition_id: str) -> Labware | None:
        definition = self._labware_catalog.get_definition(definition_id)
        if definition is None:
            return None
        if self._labware_base_class(current) == "lid" or self._labware_kind(current) == "lid":
            base_plate = Labware.from_definition(definition, is_lidded=False, is_sealed=False)
            return synthesize_lid_labware(base_plate)
        refreshed = Labware.from_definition(
            definition,
            is_lidded=bool(current.is_lidded),
            is_sealed=bool(current.is_sealed),
        )
        return self._preserve_live_labware_metadata(refreshed, current)

    def refresh_live_labware(self, *definition_ids: str) -> list[int]:
        target_ids = {str(value or "").strip() for value in definition_ids if str(value or "").strip()}
        updated_locations: list[int] = []

        for location in range(1, 10):
            stack = self._deck.get_stack(location)
            if not stack:
                continue

            rebuilt_items: list[Labware] = []
            changed = False
            for item in stack.items:
                definition_id = str(item.definition_id or item.id or "").strip()
                if not definition_id or (target_ids and definition_id not in target_ids):
                    rebuilt_items.append(item)
                    continue
                refreshed = self._rebuild_live_labware(item, definition_id)
                if refreshed is None:
                    rebuilt_items.append(item)
                    continue
                rebuilt_items.append(refreshed)
                changed = True

            if not changed:
                continue

            self._deck.clear(location)
            for item in rebuilt_items:
                self._deck.add(location, item)
            top = self._deck.get_stack(location).top
            if top is None:
                self._tipbox_occupancy.pop(location, None)
            else:
                self._initialize_tipbox_occupancy(location, top, fill_state="preserve")
            self._emit("deck_updated", location=location, labware=None if top is None else top.name)
            updated_locations.append(location)

        return updated_locations

    # -- Event system --

    def on(self, event: str, handler) -> None:
        self._event_handlers.setdefault(event, []).append(handler)

    def _emit(self, event: str, **data) -> None:
        for handler in self._event_handlers.get(event, []):
            try:
                handler(event, data)
            except Exception:
                logger.exception("Event handler error for '%s'", event)

    def _handle_task_error(self, error) -> None:
        logger.warning("Task error at step '%s': %s", error.step_name, error.message)

    def _resolve_plate_presence(
        self,
        grip_pos: object,
        *,
        sensor_detected: bool | None = None,
    ) -> bool:
        try:
            grip_value = float(grip_pos) if isinstance(grip_pos, (int, float)) else None
        except Exception:
            grip_value = None
        if grip_value is not None and grip_value >= _PICKUP_FAILURE_G_THRESHOLD_MM:
            return False
        if sensor_detected is not None:
            if grip_value is not None and grip_value >= _PLATE_SENSOR_UNTRUSTWORTHY_G_THRESHOLD_MM:
                return False
            return bool(sensor_detected)
        if grip_value is None:
            return False
        return abs(grip_value - OPEN_GRIPPER_POSITION) > 0.05

    @property
    def head_mode(self) -> HeadMode:
        return self._head_mode

    def set_head_mode(
        self,
        subset_type: str | None,
        subset_config: str | None,
        row_count: int | None = None,
        column_count: int | None = None,
    ) -> HeadMode:
        self._head_mode = normalize_head_mode(
            self._profile.head.head_type,
            subset_type,
            subset_config,
            row_count,
            column_count,
        )
        self._emit("head_mode_changed", head_mode=self._head_mode.to_dict())
        return self._head_mode

    def set_tip_selection(self, location: int | str, row: int, col: int) -> TipSelection:
        location = self._resolve_location(location)
        labware = self._require_tip_box(location, operation="Tip selection")
        rows, cols = self._tipbox_dimensions(labware)
        if rows <= 0 or cols <= 0:
            raise RuntimeError(f"Tip box at location {location} has no row/column metadata")
        row = int(row)
        col = int(col)
        if row < 0 or row >= rows or col < 0 or col >= cols:
            raise RuntimeError(f"Tip selection ({row}, {col}) is outside the tip box at location {location}")
        purpose = "return" if self._tips_on_head else "pickup"
        active_mode = (self._tips_on_head_mode or self._head_mode) if self._tips_on_head else self._head_mode
        selection = self._selection_from_clicked_tip(location, labware, active_mode, row, col, purpose=purpose)
        self._tip_selection = selection
        self._emit("tip_selection_changed", tip_selection=selection.to_dict())
        return selection

    def set_plate_selection(self, location: int | str, row: int, col: int) -> PlateSelection:
        location = self._resolve_location(location)
        labware = self._require_well_labware(location, operation="Plate selection")
        geometry = well_geometry_from_metadata(labware.metadata)
        row = int(row)
        col = int(col)
        if row < 0 or row >= geometry.rows or col < 0 or col >= geometry.cols:
            raise RuntimeError(f"Plate selection ({row}, {col}) is outside the labware at location {location}")
        if not self._is_plate_anchor_reachable(location, labware, self._head_mode, row, col):
            raise RuntimeError(f"Plate selection ({row}, {col}) is not reachable at location {location}")
        if not self._is_legal_plate_anchor(labware, self._head_mode, row, col):
            raise RuntimeError(f"Plate selection ({row}, {col}) is not legal for the current head mode")
        self._assert_plate_anchor_clearance(location, labware, self._head_mode, row, col)
        selection = plate_selection(location, row, col)
        self._plate_selection[location] = selection
        self._emit("plate_selection_changed", plate_selection=selection.to_dict())
        return selection

    def get_plate_selection_state(self, location: int) -> dict[str, Any]:
        labware = self._require_well_labware(location, operation="Plate selection")
        geometry = well_geometry_from_metadata(labware.metadata)
        legal = self._legal_plate_anchors(location, labware, self._head_mode)
        legal_keys = {(int(anchor["row"]), int(anchor["col"])) for anchor in legal}
        current = self._plate_selection.get(location)
        if current is not None and (int(current.row), int(current.col)) not in legal_keys:
            current = None
        if current is None:
            if legal:
                current = plate_selection(location, legal[0]["row"], legal[0]["col"])
                self._plate_selection[location] = current
            else:
                self._plate_selection.pop(location, None)
        footprint: list[dict[str, int]] = []
        if current is not None:
            fp = plate_footprint_wells(
                self._profile.head.head_type, self._head_mode,
                geometry.rows, geometry.cols,
                geometry.pitch_x_mm, geometry.pitch_y_mm,
                current.row, current.col,
            )
            footprint = [{"row": r, "col": c} for r, c in fp]
        return {
            "location": location,
            "selection": None if current is None else current.to_dict(),
            "legal_anchors": legal,
            "footprint": footprint,
            "rows": geometry.rows,
            "cols": geometry.cols,
        }

    def _labware_at_location(self, location: int) -> Labware:
        labware = self._deck.get_stack(location).top
        if labware is None:
            raise RuntimeError(f"No labware assigned to location {location}")
        return labware

    def _assert_well_access(self, location: int) -> None:
        labware = self._deck.get_stack(location).top
        if labware is None:
            return
        if labware.is_lidded:
            raise RuntimeError(
                f"Cannot access wells at location {location}: plate '{labware.name}' has a lid on it. "
                f"Remove the lid first (Delid Plate)."
            )
        if labware.is_sealed:
            raise RuntimeError(
                f"Cannot access wells at location {location}: plate '{labware.name}' is sealed."
            )

    @staticmethod
    def _labware_base_class(labware: Labware) -> str:
        return str((labware.metadata or {}).get("base_class") or "").strip().lower()

    @staticmethod
    def _labware_kind(labware: Labware) -> str:
        return str((labware.metadata or {}).get("kind") or "").strip().lower()

    def _clone_labware_from_template(self, template: Labware) -> Labware:
        return Labware(
            id=template.id,
            definition_id=template.definition_id,
            name=template.name,
            height=float(template.height),
            width=float(template.width),
            length=float(template.length),
            labware_type=template.labware_type,
            gripper_offset=float(template.gripper_offset),
            stack_height=float(template.stack_height or template.height),
            is_lidded=bool(template.is_lidded),
            is_sealed=bool(template.is_sealed),
            wells=int(template.wells),
            metadata=dict(template.metadata or {}),
        )

    def _rebuild_stack_from_template(self, location: int, template: Labware, count: int) -> None:
        self._deck.clear(location)
        for _ in range(max(0, int(count))):
            self._deck.add(location, self._clone_labware_from_template(template))
        self._plate_selection.pop(location, None)
        self._emit("deck_updated", location=location, labware=template.name if count > 0 else None)

    def _require_tip_box(self, location: int, *, operation: str) -> Labware:
        labware = self._labware_at_location(location)
        base_class = self._labware_base_class(labware)
        kind = self._labware_kind(labware)
        if base_class != "tip_box" and kind != "tip_box":
            raise RuntimeError(f"{operation} requires a tip box at location {location}")
        return labware

    def _require_well_labware(self, location: int, *, operation: str) -> Labware:
        labware = self._labware_at_location(location)
        base_class = self._labware_base_class(labware)
        kind = self._labware_kind(labware)
        if base_class in {"tip_box", "tip_trash"} or kind in {"tip_box", "tip_trash"}:
            raise RuntimeError(f"{operation} requires plate-style labware at location {location}")
        geometry = well_geometry_from_metadata(labware.metadata)
        if geometry.rows <= 0 or geometry.cols <= 0:
            raise RuntimeError(f"{operation} requires well-based labware at location {location}")
        return labware

    def _require_tip_receptacle(self, location: int, *, operation: str) -> Labware:
        labware = self._labware_at_location(location)
        base_class = self._labware_base_class(labware)
        kind = self._labware_kind(labware)
        if base_class not in {"tip_box", "tip_trash"} and kind not in {"tip_box", "tip_trash"}:
            raise RuntimeError(f"{operation} requires a tip box or tip trash at location {location}")
        return labware

    def _tip_length_for_labware(self, labware: Labware) -> float:
        tip_id = self._tip_id_for_labware(labware)
        length = get_tip_length_mm(self._profile.head.head_type, tip_id)
        if length is None:
            length = get_tip_length_mm(
                self._profile.head.head_type,
                getattr(self._profile.head, "default_tip_id", None) or self._profile.head.default_tip_capacity,
            )
        if length is None:
            raise RuntimeError(
                f"Tip length is not configured for {self._profile.head.head_type.name}"
            )
        return float(length)

    def _resolve_tip_offsets(self, labware: Labware) -> ResolvedTipOffsets:
        """Resolve per-(head, tip box) Tips On/Off offsets for this labware.

        Matches the active head type and the tip box against
        ``config/tip_offsets.yaml``; any unset field — or no matching row —
        falls back to the profile's ``safety.*`` defaults.
        """
        safety = self._profile.safety
        return get_tip_offset_table().resolve(
            self._profile.head.head_type,
            tipbox_name=getattr(labware, "name", "") or "",
            tipbox_id=getattr(labware, "definition_id", "") or getattr(labware, "id", "") or "",
            default_z_offset=float(safety.tips_off_z_offset),
            default_w_position=float(safety.tips_off_w_position),
        )

    def _tip_id_for_labware(self, labware: Labware) -> str:
        metadata = labware.metadata or {}
        tip_id = str(metadata.get("tip_definition_id") or "").strip()
        if tip_id:
            return tip_id
        capacity = metadata.get("disposable_tip_capacity_ul")
        inferred = get_tip_id_for_capacity(self._profile.head.head_type, capacity)
        return inferred or self.active_tip_id()

    def _effective_plate_selection(
        self,
        location: int,
        labware: Labware | None,
        head_mode: HeadMode,
        *,
        command_name: str = "Plate selection",
    ) -> PlateSelection | None:
        if labware is None:
            return None
        if self._labware_base_class(labware) in {"tip_box", "tip_trash"} or self._labware_kind(labware) in {"tip_box", "tip_trash"}:
            return None
        geometry = well_geometry_from_metadata(labware.metadata)
        if geometry.rows <= 0 or geometry.cols <= 0:
            return None
        current = self._plate_selection.get(location)
        if current is not None:
            try:
                if self._is_plate_anchor_selectable(location, labware, head_mode, current.row, current.col):
                    return current
            except Exception:
                pass
        candidate_anchors = self._candidate_plate_anchors(location, labware, head_mode)
        legal = [
            anchor
            for anchor in candidate_anchors
            if self._is_plate_anchor_selectable(location, labware, head_mode, anchor["row"], anchor["col"])
        ]
        if not legal:
            if candidate_anchors:
                first_anchor = candidate_anchors[0]
                self._assert_plate_anchor_clearance(
                    location,
                    labware,
                    head_mode,
                    int(first_anchor["row"]),
                    int(first_anchor["col"]),
                    command_name=command_name,
                )
            raise RuntimeError(f"No legal plate anchors are available at location {location} for the current head mode")
        selection = plate_selection(location, legal[0]["row"], legal[0]["col"])
        self._plate_selection[location] = selection
        return selection

    def _legal_plate_anchors(self, location: int, labware: Labware, head_mode: HeadMode) -> list[dict[str, int]]:
        return [
            anchor
            for anchor in self._candidate_plate_anchors(location, labware, head_mode)
            if self._is_plate_anchor_selectable(location, labware, head_mode, anchor["row"], anchor["col"])
        ]

    def _candidate_plate_anchors(self, location: int, labware: Labware, head_mode: HeadMode) -> list[dict[str, int]]:
        geometry = well_geometry_from_metadata(labware.metadata)
        anchors = legal_plate_anchors(
            self._profile.head.head_type,
            head_mode,
            geometry.rows,
            geometry.cols,
            geometry.pitch_x_mm,
            geometry.pitch_y_mm,
        )
        return [
            {"row": anchor.row, "col": anchor.col}
            for anchor in anchors
            if self._is_plate_anchor_reachable(location, labware, head_mode, anchor.row, anchor.col)
        ]

    def _is_legal_plate_anchor(self, labware: Labware, head_mode: HeadMode, row: int, col: int) -> bool:
        geometry = well_geometry_from_metadata(labware.metadata)
        if geometry.rows <= 0 or geometry.cols <= 0:
            return False
        return is_legal_plate_anchor(
            self._profile.head.head_type,
            head_mode,
            geometry.rows,
            geometry.cols,
            geometry.pitch_x_mm,
            geometry.pitch_y_mm,
            row,
            col,
        )

    def _plate_xy_target(self, location: int, labware: Labware, head_mode: HeadMode, selection: PlateSelection) -> tuple[float, float]:
        teach_x = self._teachpoints.get_teachpoint(location, Axis.X)
        teach_y = self._teachpoints.get_teachpoint(location, Axis.Y)
        well_offset_x, well_offset_y = well_center_offset_from_teachpoint_mm(
            labware.metadata,
            row=int(selection.row),
            col=int(selection.col),
        )
        head_offset_x, head_offset_y = head_mode_offsets_mm(self._profile.head.head_type, head_mode)
        return teach_x + well_offset_x - head_offset_x, teach_y + well_offset_y - head_offset_y

    def _is_plate_anchor_reachable(self, location: int, labware: Labware, head_mode: HeadMode, row: int, col: int) -> bool:
        try:
            selection = plate_selection(location, row, col)
            target_x, target_y = self._plate_xy_target(location, labware, head_mode, selection)
            (x_min, x_max), (y_min, y_max) = self._axis_xy_range()
        except Exception:
            return False
        epsilon = 1e-6
        return (x_min - epsilon) <= target_x <= (x_max + epsilon) and (y_min - epsilon) <= target_y <= (y_max + epsilon)

    def _is_plate_anchor_selectable(self, location: int, labware: Labware, head_mode: HeadMode, row: int, col: int) -> bool:
        if not self._is_plate_anchor_reachable(location, labware, head_mode, row, col):
            return False
        if not self._is_legal_plate_anchor(labware, head_mode, row, col):
            return False
        try:
            self._assert_plate_anchor_clearance(location, labware, head_mode, row, col)
        except RuntimeError:
            return False
        return True

    _NEIGHBOR_CLEARANCE_SAFETY_MM = 2.0

    def _assert_plate_anchor_clearance(
        self,
        location: int,
        labware: Labware,
        head_mode: HeadMode,
        row: int,
        col: int,
        *,
        command_name: str = "Plate selection",
    ) -> None:
        selection = plate_selection(location, row, col)
        target_x, target_y = self._plate_xy_target(location, labware, head_mode, selection)
        target_height = float(self._deck.get_height(location))
        tip_length = self._attached_tip_length_mm or 0.0
        housing_bottom = target_height + tip_length - self._NEIGHBOR_CLEARANCE_SAFETY_MM
        _assert_neighbor_clearance(
            command_name=command_name,
            teachpoints=self._teachpoints,
            deck=self._deck,
            head_type=self._profile.head.head_type,
            head_mode=head_mode,
            target_location=location,
            target_x=target_x,
            target_y=target_y,
            allowed_top_plane_mm=housing_bottom,
        )

    def _validated_tip_wells(
        self,
        labware: Labware,
        head_mode: HeadMode,
        selection: TipSelection,
        *,
        purpose: str = "pickup",
    ) -> list[tuple[int, int]]:
        rows, cols = self._tipbox_dimensions(labware)
        if rows <= 0 or cols <= 0:
            raise RuntimeError("Tip box metadata is missing rows/cols")
        wells = selected_tip_wells(rows, cols, selection)
        if not wells:
            raise RuntimeError("No tips are selected for the current head mode")
        if self._labware_base_class(labware) == "tip_box" or self._labware_kind(labware) == "tip_box":
            if selection.location not in self._tipbox_untracked:
                self._ensure_tipbox_occupancy(selection.location, labware)
                occupied = self._occupied_tip_wells(selection.location)
                if not is_legal_tipbox_anchor(
                    rows,
                    cols,
                    head_mode,
                    occupied,
                    selection.row,
                    selection.col,
                    purpose=purpose,
                ):
                    raise RuntimeError(f"Tip selection ({selection.row}, {selection.col}) is not accessible for {purpose}")
        return wells

    def _effective_tip_selection(
        self,
        location: int,
        labware: Labware,
        head_mode: HeadMode,
        *,
        purpose: str = "pickup",
    ) -> TipSelection:
        if self._tip_selection is not None and self._tip_selection.location == location:
            try:
                self._validated_tip_wells(labware, head_mode, self._tip_selection, purpose=purpose)
                return self._tip_selection
            except RuntimeError:
                pass
        rows, cols = self._tipbox_dimensions(labware)
        if rows <= 0 or cols <= 0:
            raise RuntimeError("Tip box metadata is missing rows/cols")
        anchors = self._legal_tip_anchors(location, labware, head_mode, purpose=purpose)
        if not anchors:
            raise RuntimeError(f"No legal tip anchors are available for {purpose} at location {location}")
        selection = tipbox_selection(location, anchors[0]["row"], anchors[0]["col"], head_mode)
        self._validated_tip_wells(labware, head_mode, selection, purpose=purpose)
        self._tip_selection = selection
        return selection

    def _selection_from_clicked_tip(
        self,
        location: int,
        labware: Labware,
        head_mode: HeadMode,
        clicked_row: int,
        clicked_col: int,
        *,
        purpose: str,
    ) -> TipSelection:
        anchors = self._legal_tip_anchors(location, labware, head_mode, purpose=purpose)
        for anchor in anchors:
            row_start = int(anchor["row"])
            col_start = int(anchor["col"])
            row_count = int(anchor["row_count"])
            column_count = int(anchor["column_count"])
            if row_start <= clicked_row < row_start + row_count and col_start <= clicked_col < col_start + column_count:
                selection = tipbox_selection(location, row_start, col_start, head_mode)
                self._validated_tip_wells(labware, head_mode, selection, purpose=purpose)
                return selection
        selection = tipbox_selection(location, clicked_row, clicked_col, head_mode)
        self._validated_tip_wells(labware, head_mode, selection, purpose=purpose)
        return selection

    @staticmethod
    def _tipbox_dimensions(labware: Labware) -> tuple[int, int]:
        metadata = labware.metadata or {}
        rows = int(metadata.get("rows") or 0)
        cols = int(metadata.get("cols") or 0)
        if rows > 0 and cols > 0:
            return rows, cols
        wells = int(metadata.get("wells") or 0)
        if wells == 96:
            return 8, 12
        if wells == 384:
            return 16, 24
        if wells == 1536:
            return 32, 48
        return rows, cols

    def _initialize_tipbox_occupancy(self, location: int, labware: Labware, *, fill_state: str = "full") -> None:
        if self._labware_base_class(labware) != "tip_box" and self._labware_kind(labware) != "tip_box":
            self._tipbox_occupancy.pop(location, None)
            return
        rows, cols = self._tipbox_dimensions(labware)
        if rows <= 0 or cols <= 0:
            self._tipbox_occupancy.pop(location, None)
            return
        normalized_fill = str(fill_state or "full").strip().lower()
        if normalized_fill == "preserve" and location in self._tipbox_occupancy:
            return
        if normalized_fill == "empty":
            self._tipbox_occupancy[location] = set()
            return
        self._tipbox_occupancy[location] = {
            (row, col)
            for row in range(rows)
            for col in range(cols)
        }

    def _ensure_tipbox_occupancy(self, location: int, labware: Labware) -> None:
        if location not in self._tipbox_occupancy:
            self._initialize_tipbox_occupancy(location, labware)

    def _occupied_tip_wells(self, location: int) -> set[tuple[int, int]]:
        return set(self._tipbox_occupancy.get(location, set()))

    def _apply_tipbox_selection(
        self,
        location: int,
        head_mode: HeadMode,
        selection: TipSelection,
        *,
        purpose: str,
    ) -> None:
        labware = self._require_tip_box(location, operation="Tip inventory update")
        wells = self._validated_tip_wells(labware, head_mode, selection, purpose=purpose)
        if location in self._tipbox_untracked:
            return
        occupied = self._occupied_tip_wells(location)
        if purpose == "pickup":
            occupied.difference_update(wells)
        elif purpose == "return":
            box_tip_id = self._tip_id_for_labware(labware)
            if self._tip_definition_id and box_tip_id and box_tip_id != self._tip_definition_id:
                raise RuntimeError(
                    f"Tip return requires matching tip definitions ({self._tip_definition_id} on head, {box_tip_id} in box)"
                )
            occupied.update(wells)
        else:
            raise ValueError(f"Unknown tip inventory purpose: {purpose}")
        self._tipbox_occupancy[location] = occupied

    def _legal_tip_anchors(
        self,
        location: int,
        labware: Labware,
        head_mode: HeadMode,
        *,
        purpose: str,
    ) -> list[dict[str, int]]:
        rows, cols = self._tipbox_dimensions(labware)
        if rows <= 0 or cols <= 0:
            return []
        if location in self._tipbox_untracked:
            occupied: set[tuple[int, int]] = (
                {(r, c) for r in range(rows) for c in range(cols)}
                if purpose == "pickup"
                else set()
            )
        else:
            self._ensure_tipbox_occupancy(location, labware)
            occupied = self._occupied_tip_wells(location)
        anchors = legal_tipbox_anchors(
            rows,
            cols,
            head_mode,
            occupied,
            purpose=purpose,
        )
        return [
            anchor.to_dict()
            for anchor in anchors
            if self._is_tip_anchor_reachable(location, labware, head_mode, anchor.row, anchor.col)
        ]

    def _axis_xy_range(self) -> tuple[tuple[float, float], tuple[float, float]]:
        x_cfg = self._profile.axes.get("X")
        y_cfg = self._profile.axes.get("Y")
        if x_cfg is None or y_cfg is None:
            raise RuntimeError("Missing X/Y axis config")
        return (
            (float(x_cfg.range.min_pos), float(x_cfg.range.max_pos)),
            (float(y_cfg.range.min_pos), float(y_cfg.range.max_pos)),
        )

    def _tip_xy_target(self, location: int, labware: Labware, head_mode: HeadMode, anchor_row: int, anchor_col: int) -> tuple[float, float]:
        teach_x = self._teachpoints.get_teachpoint(location, Axis.X)
        teach_y = self._teachpoints.get_teachpoint(location, Axis.Y)
        head_offset_x, head_offset_y = tip_task_head_offsets_mm(self._profile.head.head_type, head_mode)
        selection = tipbox_selection(location, anchor_row, anchor_col, head_mode)
        tipbox_offset_x, tipbox_offset_y = well_center_offset_from_teachpoint_mm(
            labware.metadata,
            row=int(selection.row),
            col=int(selection.col),
        )
        return teach_x + tipbox_offset_x - head_offset_x, teach_y + tipbox_offset_y - head_offset_y

    def _is_tip_anchor_reachable(self, location: int, labware: Labware, head_mode: HeadMode, anchor_row: int, anchor_col: int) -> bool:
        try:
            target_x, target_y = self._tip_xy_target(location, labware, head_mode, anchor_row, anchor_col)
            (x_min, x_max), (y_min, y_max) = self._axis_xy_range()
        except Exception:
            return False
        epsilon = 1e-6
        return (x_min - epsilon) <= target_x <= (x_max + epsilon) and (y_min - epsilon) <= target_y <= (y_max + epsilon)

    def _tipbox_inventory_state(self) -> dict[str, dict[str, Any]]:
        state: dict[str, dict[str, Any]] = {}
        for location in range(1, 10):
            stack = self._deck.get_stack(location)
            labware = stack.top
            if labware is None:
                continue
            if self._labware_base_class(labware) != "tip_box" and self._labware_kind(labware) != "tip_box":
                continue
            self._ensure_tipbox_occupancy(location, labware)
            rows, cols = self._tipbox_dimensions(labware)
            occupied = self._occupied_tip_wells(location)
            pickup_mode = self._head_mode
            return_mode = self._tips_on_head_mode or self._head_mode
            state[str(location)] = {
                "labware_name": labware.name,
                "rows": rows,
                "cols": cols,
                "tip_id": self._tip_id_for_labware(labware),
                "occupied": [f"{row}:{col}" for row, col in sorted(occupied)],
                "legal_pickup_anchors": self._legal_tip_anchors(location, labware, pickup_mode, purpose="pickup"),
                "legal_return_anchors": self._legal_tip_anchors(location, labware, return_mode, purpose="return"),
            }
        return state

    def _set_tip_state(
        self,
        *,
        labware_name: str,
        tip_definition_id: str = "",
        tip_length_mm: float,
        head_mode: HeadMode,
        tip_selection: TipSelection,
    ) -> None:
        self._tips_on_head = True
        self._tip_labware_name = labware_name
        self._tip_definition_id = tip_definition_id
        self._attached_tip_length_mm = float(tip_length_mm)
        self._tips_on_head_mode = head_mode
        self._tips_on_head_selection = tip_selection

    def _clear_tip_state(self) -> None:
        self._tips_on_head = False
        self._tip_labware_name = ""
        self._tip_definition_id = ""
        self._attached_tip_length_mm = None
        self._tips_on_head_mode = None
        self._tips_on_head_selection = None

    # -- Error handling --

    def abort(self) -> bool:
        return self._engine.abort()

    def retry(self) -> bool:
        return self._engine.retry()

    def ignore(self) -> bool:
        return self._engine.ignore()

    @staticmethod
    def _build_teachpoints(profile: BravoProfile) -> Teachpoints:
        if profile.teachpoints is not None:
            return profile.teachpoints

        teachpoints = Teachpoints()
        teachpoints.set_default_teachpoints(profile.head.head_type)
        return teachpoints
