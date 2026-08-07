import asyncio

import pytest

from pybravo import liquid_classes as liquid_classes_store
from pybravo.bravo import Bravo
from pybravo.controllers.base import AxisMoveInfo, FirmwareVersion
from pybravo.controllers.simulation import SimulationController
from pybravo.deck.geometry import tipbox_anchor_offset_from_teachpoint_mm, well_center_offset_from_teachpoint_mm
from pybravo.deck.labware import DeckState, Labware, LabwareDefinition, synthesize_lid_labware
from pybravo.deck.teachpoints import Teachpoints
from pybravo.head_mode import (
    TipSelection,
    head_anchor_cell,
    legal_plate_anchors,
    normalize_head_mode,
    plate_footprint_wells,
    plate_selection,
    tip_task_head_offsets_mm,
    tipbox_selection,
)
from pybravo.profile.profile import BravoProfile
from pybravo.protocol.errors import BravoError, ErrorType
from pybravo.state_machine.engine import StateMachineEngine, StateMachineTask
from pybravo.state_machine.tasks import (
    DelidPlateTask,
    DockGripperTask,
    InitializeTask,
    PickPlaceTask,
    _infer_stack_count_from_scan_height,
)
from pybravo.types import Axis, AxisRange, DeviceStateFlag, GripperDetectionState, HeadType, SpeedLevel
from pybravo.workflow.executor import WorkflowExecutor


def _make_plate_definition(
    *,
    labware_id: str,
    name: str,
    wells: int,
    rows: int,
    cols: int,
    spacing_x_mm: float,
    spacing_y_mm: float,
    offset_x_mm: float,
    offset_y_mm: float,
    height_mm: float = 14.5,
) -> LabwareDefinition:
    return LabwareDefinition(
        id=labware_id,
        name=name,
        kind="plate",
        base_class="plate",
        height_mm=height_mm,
        wells=wells,
        rows=rows,
        cols=cols,
        spacing_x_mm=spacing_x_mm,
        spacing_y_mm=spacing_y_mm,
        offset_x_mm=offset_x_mm,
        offset_y_mm=offset_y_mm,
    )


def _attach_test_tips(bravo: Bravo, *, tip_id: str = "st_30ul", tip_length_mm: float | None = None) -> None:
    bravo._tips_on_head = True
    bravo._tip_labware_name = "Test Tips"
    bravo._tip_definition_id = tip_id
    bravo._attached_tip_length_mm = float(
        tip_length_mm
        if tip_length_mm is not None
        else (getattr(bravo.profile.head, "teach_tip_length_mm", None) or 0.0)
    )
    bravo._tips_on_head_mode = bravo._head_mode
    bravo._tips_on_head_selection = None


def _make_plate_selection_collision_bravo() -> Bravo:
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    tp = Teachpoints()
    tp.set_default_teachpoints(profile.head.head_type)
    profile.teachpoints = tp

    bravo = Bravo(profile=profile)
    bravo.set_head_mode("column", "back_left", column_count=1)

    plate = LabwareDefinition(
        id="plate-1536",
        name="1536 Labcyte LP-0400 LDV",
        kind="sbs_plate",
        base_class="plate",
        height_mm=10.5,
        wells=1536,
        rows=32,
        cols=48,
        offset_x_mm=1.125,
        offset_y_mm=1.125,
        spacing_x_mm=2.25,
        spacing_y_mm=2.25,
    )
    tipbox = LabwareDefinition(
        id="tipbox-10",
        name="384 V11 ST10 Tip Box 10734.102",
        kind="tip_box",
        base_class="tip_box",
        height_mm=50.0,
        length_mm=127.76,
        width_mm=85.48,
        wells=384,
        disposable_tip_capacity_ul=10.0,
        tip_definition_id="st_10ul",
        offset_x_mm=2.25,
        offset_y_mm=2.25,
        spacing_x_mm=4.5,
        spacing_y_mm=4.5,
    )
    bravo.deck.set_single(5, Labware.from_definition(plate))
    bravo.deck.set_single(6, Labware.from_definition(tipbox))
    return bravo


def _catalog_from_definitions(*definitions: LabwareDefinition):
    by_id = {definition.id: definition for definition in definitions}

    class _Catalog:
        def get_definition(self, labware_id):
            return by_id.get(labware_id)

        def list_definitions(self):
            return list(by_id.values())

    return _Catalog()


def _make_workflow_executor_bravo() -> tuple[Bravo, dict[str, list[dict[str, object]]]]:
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    tp = Teachpoints()
    tp.set_default_teachpoints(profile.head.head_type)
    profile.teachpoints = tp
    bravo = Bravo(profile=profile, mode="simulation")
    bravo.connect()

    tipbox = LabwareDefinition(
        id="tipbox-384",
        name="384 V11 ST10 Tip Box 10734.102",
        kind="tip_box",
        base_class="tip_box",
        height_mm=50.0,
        length_mm=127.76,
        width_mm=85.48,
        wells=384,
        rows=16,
        cols=24,
        spacing_x_mm=4.5,
        spacing_y_mm=4.5,
        offset_x_mm=2.25,
        offset_y_mm=2.25,
        disposable_tip_capacity_ul=10.0,
        tip_definition_id="st_10ul",
    )
    tipbox_riser = LabwareDefinition(
        id="tipbox-riser",
        name="Tip Box Riser",
        kind="sbs_plate",
        base_class="plate",
        height_mm=20.0,
        stack_height_mm=20.0,
    )
    plate_1536 = LabwareDefinition(
        id="plate-1536",
        name="1536 Labcyte LP-0400 LDV",
        kind="sbs_plate",
        base_class="plate",
        height_mm=10.5,
        wells=1536,
        rows=32,
        cols=48,
        offset_x_mm=1.125,
        offset_y_mm=1.125,
        spacing_x_mm=2.25,
        spacing_y_mm=2.25,
        well_depth_mm=5.0,
    )
    bravo._labware_catalog = _catalog_from_definitions(tipbox, tipbox_riser, plate_1536)
    deck_config = {
        "2": [
            {"labware_id": "tipbox-riser", "height_mm": 20.0, "stack_height_mm": 20.0},
            {"labware_id": "tipbox-384", "height_mm": 50.0},
        ],
        "5": [{"labware_id": "plate-1536", "height_mm": 10.5, "well_depth_mm": 5.0}],
    }
    return bravo, deck_config


@pytest.mark.asyncio
async def test_bravo_preserves_loaded_teachpoints():
    profile = BravoProfile.default()
    profile.teachpoints = Teachpoints()
    profile.teachpoints.set_teachpoint(1, Axis.X, 123.4)
    profile.teachpoints.set_teachpoint(1, Axis.Y, 56.7)
    profile.teachpoints.set_teachpoint(1, Axis.Z, 89.0)

    bravo = Bravo(profile=profile)

    assert bravo.teachpoints.get_teachpoint(1, Axis.X) == 123.4
    assert bravo.teachpoints.get_teachpoint(1, Axis.Y) == 56.7
    assert bravo.teachpoints.get_teachpoint(1, Axis.Z) == 89.0


@pytest.mark.asyncio
async def test_home_uses_profile_safe_z_position():
    profile = BravoProfile.default()
    profile.safety.z_safe_position = 42.5
    bravo = Bravo(profile=profile, mode="simulation")
    bravo.connect()

    try:
        await bravo.home([Axis.X])

        assert bravo.get_position(Axis.X) == 0.0
        assert bravo.get_position(Axis.Z) == 42.5
    finally:
        bravo.disconnect()


@pytest.mark.asyncio
async def test_home_all_homes_gripper_axes_and_finishes_docked():
    profile = BravoProfile.default()
    bravo = Bravo(profile=profile, mode="simulation")
    bravo.connect()

    try:
        axes = await bravo.home()

        # Head and gripper lift before the gantry moves — see SAFE_HOME_ORDER.
        # This used to assert [X, Y, Z, W, G, Zg], which homed X and Y while the
        # head could still be down in labware.
        assert axes == [Axis.Z, Axis.Zg, Axis.G, Axis.X, Axis.Y, Axis.W]
        for vertical in (Axis.Z, Axis.Zg):
            for lateral in (Axis.X, Axis.Y):
                assert axes.index(vertical) < axes.index(lateral)
        assert bravo.get_position(Axis.G) == 0.0
        assert bravo.get_position(Axis.Zg) == -20.0
    finally:
        bravo.disconnect()


def test_full_384_head_on_384_plate_only_allows_a1_a2_b1_b2_anchors():
    mode = normalize_head_mode(HeadType.HT_384_D_70, "all_barrels", "back_left")

    anchors = legal_plate_anchors(
        HeadType.HT_384_D_70,
        mode,
        16,
        24,
        4.5,
        4.5,
    )

    assert [(anchor.row, anchor.col) for anchor in anchors] == [(0, 0)]


def test_full_384_head_on_1536_plate_only_allows_a1_a2_b1_b2_anchors():
    mode = normalize_head_mode(HeadType.HT_384_D_70, "all_barrels", "back_left")

    anchors = legal_plate_anchors(
        HeadType.HT_384_D_70,
        mode,
        32,
        48,
        2.25,
        2.25,
    )

    assert [(anchor.row, anchor.col) for anchor in anchors] == [(0, 0), (0, 1), (1, 0), (1, 1)]


def test_back_left_full_row_on_384_plate_allows_both_dense_phases_for_all_rows():
    mode = normalize_head_mode(HeadType.HT_384_D_70, "row", "back_left", row_count=1)

    anchors = legal_plate_anchors(
        HeadType.HT_384_D_70,
        mode,
        16,
        24,
        4.5,
        4.5,
    )

    # 384 head on 384 plate is 1:1 pitch, so phase=1: one unique anchor per row,
    # column is always 0 (all 24 columns selected).
    assert len(anchors) == 16
    assert [(anchor.row, anchor.col) for anchor in anchors[:2]] == [(0, 0), (1, 0)]


def test_dense_plate_footprint_stays_anchored_to_selected_well():
    mode = normalize_head_mode(HeadType.HT_384_D_70, "column", "back_left", column_count=1)

    wells = plate_footprint_wells(
        HeadType.HT_384_D_70,
        mode,
        32,
        48,
        2.25,
        2.25,
        1,
        5,
    )

    assert wells[:4] == [(1, 5), (3, 5), (5, 5), (7, 5)]
    assert wells[-1] == (31, 5)


def test_set_plate_selection_persists_selected_anchor_well():
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    bravo = Bravo(profile=profile)
    plate = _make_plate_definition(
        labware_id="plate-384",
        name="384 Test Plate",
        wells=384,
        rows=16,
        cols=24,
        spacing_x_mm=4.5,
        spacing_y_mm=4.5,
        offset_x_mm=2.25,
        offset_y_mm=2.25,
    )
    bravo._labware_catalog = type("Catalog", (), {
        "get_definition": lambda self, labware_id: plate if labware_id == "plate-384" else None,
        "list_definitions": lambda self: [plate],
    })()
    bravo.set_labware(1, "plate-384")

    selection = bravo.set_plate_selection(1, 0, 0)

    assert selection.to_dict() == {"location": 1, "row": 0, "col": 0}
    assert bravo.get_state()["plate_selection"]["1"] == {"location": 1, "row": 0, "col": 0}


def test_plate_selection_state_filters_anchors_blocked_by_neighboring_footprint_overlap():
    bravo = _make_plate_selection_collision_bravo()

    state = bravo.get_plate_selection_state(5)
    anchors = {(int(anchor["row"]), int(anchor["col"])) for anchor in state["legal_anchors"]}

    assert len(anchors) == 10
    assert (0, 0) in anchors
    assert (1, 4) in anchors
    assert (0, 5) not in anchors
    assert (1, 47) not in anchors
    assert state["selection"] == {"location": 5, "row": 0, "col": 0}


def test_set_plate_selection_rejects_anchor_blocked_by_neighboring_occupied_location():
    bravo = _make_plate_selection_collision_bravo()

    with pytest.raises(RuntimeError, match="Plate selection at location 5 is blocked:.*location 6 top 50.0 mm"):
        bravo.set_plate_selection(5, 0, 36)


def test_get_plate_selection_state_resets_stale_blocked_anchor_selection():
    bravo = _make_plate_selection_collision_bravo()
    bravo._plate_selection[5] = plate_selection(5, 0, 36)

    state = bravo.get_plate_selection_state(5)

    assert state["selection"] == {"location": 5, "row": 0, "col": 0}
    assert bravo.get_state()["plate_selection"]["5"] == {"location": 5, "row": 0, "col": 0}


@pytest.mark.asyncio
async def test_workflow_executor_tips_on_uses_tipbox_top_height_without_support_stack(monkeypatch):
    bravo, deck_config = _make_workflow_executor_bravo()
    bravo.set_head_mode("column", "back_left", column_count=1)
    events: list[dict[str, object]] = []

    async def on_event(event):
        events.append(event)

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr("pybravo.workflow.executor.asyncio.sleep", _no_sleep)

    try:
        executor = WorkflowExecutor(bravo, {"nodes": []}, deck_config=deck_config, on_event=on_event)
        await executor._setup_deck()
        bravo.set_tip_selection(2, 0, 23)

        await executor._animate_task_motion("tips/TipsOn", {"location": 2})

        positions = [event["positions"] for event in events if event.get("type") == "workflow:positions"]
        expected_z = (
            float(bravo.teachpoints.get_teachpoint(2, Axis.Z))
            + float(bravo.profile.head.teach_tip_length_mm or 0.0)
            - 50.0
        )
        assert positions[1]["Z"] == pytest.approx(expected_z)
        tips_events = [event for event in events if event.get("type") == "workflow:tips_change"]
        tips_event = next(e for e in tips_events if e.get("tips_on") is True)
        assert tips_event["tips_on"] is True
        assert tips_event["head_mode"]["subset_type"] == "column"
        assert tips_event["tip_selection"]["col"] == 23
        assert tips_event["tip_definition_id"] == "st_10ul"
        assert tips_event["attached_tip_length_mm"] == pytest.approx(19.9)
        assert "0:23" in tips_event["tipbox_removed_cells"]["2"]
        assert "15:23" in tips_event["tipbox_removed_cells"]["2"]
    finally:
        bravo.disconnect()


@pytest.mark.asyncio
async def test_workflow_executor_applies_runtime_snapshot_to_plate_and_tip_targets():
    live_bravo, deck_config = _make_workflow_executor_bravo()
    sim_bravo, _ = _make_workflow_executor_bravo()

    try:
        live_bravo.set_labware(2, "tipbox-384")
        live_bravo.set_labware(5, "plate-1536")
        live_bravo.set_head_mode("column", "back_left", column_count=1)
        tip_selection = live_bravo.set_tip_selection(2, 0, 23)
        plate_anchor = live_bravo.set_plate_selection(5, 1, 5)
        live_bravo._tips_on_head = True
        live_bravo._tip_labware_name = "384 V11 ST10 Tip Box 10734.102"
        live_bravo._tip_definition_id = "st_10ul"
        live_bravo._attached_tip_length_mm = 19.9
        live_bravo._tips_on_head_mode = live_bravo._head_mode
        live_bravo._tips_on_head_selection = tip_selection
        state = live_bravo.get_state()
        runtime_snapshot = {
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

        executor = WorkflowExecutor(sim_bravo, {"nodes": []}, deck_config=deck_config, runtime_state=runtime_snapshot)
        await executor._setup_deck()
        executor._apply_runtime_snapshot()

        assert sim_bravo._tip_selection is not None
        assert sim_bravo._tip_selection.location == 2
        assert sim_bravo._tip_selection.row == 0
        assert sim_bravo._tip_selection.col == 23
        assert sim_bravo._plate_selection[5].to_dict() == plate_anchor.to_dict()
        assert sim_bravo._tips_on_head is True
        assert sim_bravo._tips_on_head_mode is not None
        assert sim_bravo._tips_on_head_selection is not None

        tip_teach_x = float(sim_bravo.teachpoints.get_teachpoint(2, Axis.X))
        tip_teach_y = float(sim_bravo.teachpoints.get_teachpoint(2, Axis.Y))
        tip_offset_x, tip_offset_y = tipbox_anchor_offset_from_teachpoint_mm(
            sim_bravo.deck.get_stack(2).top.metadata,
            sim_bravo._tip_selection,
        )
        head_offset_x, head_offset_y = tip_task_head_offsets_mm(
            sim_bravo.profile.head.head_type,
            sim_bravo._head_mode,
        )
        expected_tip_xy = (
            tip_teach_x + tip_offset_x - head_offset_x,
            tip_teach_y + tip_offset_y - head_offset_y,
        )
        actual_tip_xy = executor._get_tip_xy(2, purpose="pickup")
        assert actual_tip_xy == pytest.approx(expected_tip_xy)

        expected_plate_xy = sim_bravo._plate_xy_target(
            5,
            sim_bravo.deck.get_stack(5).top,
            sim_bravo._head_mode,
            sim_bravo._plate_selection[5],
        )
        actual_plate_xy = executor._get_well_xy(5, row=0, col=0, command="Mix")
        assert actual_plate_xy == pytest.approx(expected_plate_xy)
    finally:
        live_bravo.disconnect()
        sim_bravo.disconnect()


@pytest.mark.asyncio
async def test_aspirate_uses_selected_plate_anchor_well_for_xy_target():
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    profile.safety.z_safe_position = 40.0
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    bravo = Bravo(profile=profile)
    bravo._controller = controller
    plate = _make_plate_definition(
        labware_id="plate-384",
        name="384 Test Plate",
        wells=384,
        rows=16,
        cols=24,
        spacing_x_mm=4.5,
        spacing_y_mm=4.5,
        offset_x_mm=2.25,
        offset_y_mm=2.25,
    )
    bravo._labware_catalog = type("Catalog", (), {
        "get_definition": lambda self, labware_id: plate if labware_id == "plate-384" else None,
        "list_definitions": lambda self: [plate],
    })()
    bravo.set_labware(1, "plate-384")
    bravo.teachpoints.set_teachpoint(1, Axis.X, 100.0)
    bravo.teachpoints.set_teachpoint(1, Axis.Y, 200.0)
    bravo.teachpoints.set_teachpoint(1, Axis.Z, 300.0)
    bravo.set_plate_selection(1, 0, 0)
    _attach_test_tips(bravo)

    await bravo.aspirate(location=1, volume=10.0)

    xy_move = next(
        moves for moves in controller.move_calls
        if any(move.axis == Axis.X for move in moves) and any(move.axis == Axis.Y for move in moves)
    )
    x_target = next(move.position for move in xy_move if move.axis == Axis.X)
    y_target = next(move.position for move in xy_move if move.axis == Axis.Y)
    assert x_target == pytest.approx(97.75)
    assert y_target == pytest.approx(197.75)


@pytest.mark.asyncio
async def test_dock_gripper_forces_open_and_recess_even_when_plate_sensor_is_active():
    profile = BravoProfile.default()
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    controller._plate_in_gripper = True
    controller._axes[Axis.G].position = 5.0
    controller._axes[Axis.Zg].position = 12.0
    engine = StateMachineEngine()

    await engine.execute(DockGripperTask(controller, profile, force_if_plate_detected=True))

    assert controller.get_position(Axis.G) == 0.0
    assert controller.get_position(Axis.Zg) == -20.0


@pytest.mark.asyncio
async def test_dock_gripper_uses_absolute_negative_recess_even_if_profile_zg_min_is_zero():
    profile = BravoProfile.default()
    profile.axes["Zg"].range = AxisRange(0.0, profile.axes["Zg"].range.max_pos)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    _mark_initialize_cold_start(controller)
    engine = StateMachineEngine()

    await engine.execute(DockGripperTask(controller, profile, force_if_plate_detected=True))

    assert controller.get_position(Axis.Zg) == -20.0


class FailingTaskWithoutPrompt(StateMachineTask):
    def __init__(self):
        super().__init__("FailingNoPrompt")

    def get_steps(self):
        async def fail():
            raise RuntimeError("boom")
        return [("fail", fail)]


@pytest.mark.asyncio
async def test_engine_raises_immediately_for_non_interactive_task_errors():
    engine = StateMachineEngine()

    with pytest.raises(RuntimeError, match="boom"):
        await engine.execute(FailingTaskWithoutPrompt())


class RecordingSimulationController(SimulationController):
    def __init__(self):
        super().__init__()
        self.detect_smart_head_calls = 0
        self.read_smart_head_type_calls = 0
        self.read_head_adc_calls = 0
        self.reset_faults_calls: list[list[Axis]] = []
        self.home_axes_calls: list[list[Axis]] = []
        self.move_calls: list[list[AxisMoveInfo]] = []
        self.jog_calls = []
        self.grip_calls: list[tuple[SpeedLevel, float, bool]] = []
        self._simulated_scan_raw_height_mm: float | None = None
        self._simulated_scan_approach_height_mm = 10.0

    def detect_smart_head(self) -> bool:
        self.detect_smart_head_calls += 1
        return super().detect_smart_head()

    def read_smart_head_type(self) -> int:
        self.read_smart_head_type_calls += 1
        return super().read_smart_head_type()

    def read_head_adc(self) -> int:
        self.read_head_adc_calls += 1
        return super().read_head_adc()

    def reset_faults(self, axes: list[Axis]) -> None:
        self.reset_faults_calls.append(list(axes))
        super().reset_faults(axes)

    def home_axes(self, axes: list[Axis]) -> None:
        self.home_axes_calls.append(list(axes))
        super().home_axes(axes)

    def move(self, moves: list[AxisMoveInfo], wait: bool = True, timeout_ms: int = 30000) -> None:
        self.move_calls.append(list(moves))
        super().move(moves, wait=wait, timeout_ms=timeout_ms)

    def jog(self, params) -> float:
        self.jog_calls.append(params)
        return super().jog(params)

    def open_gripper(self, position: float | None = None) -> None:
        target = 0.0 if position is None else float(position)
        self.move_calls.append([AxisMoveInfo(axis=Axis.G, position=target)])
        super().open_gripper(position)

    def grip(self, speed: SpeedLevel, position: float, grip_lid: bool = False) -> None:
        self.grip_calls.append((speed, position, grip_lid))
        super().grip(speed, position, grip_lid=grip_lid)

    def scan_stack_with_gripper(
        self,
        *,
        start_zg: float,
        end_zg: float,
        speed: SpeedLevel,
        transient_ms: int = 0,
    ) -> dict[str, float | bool | None]:
        if self._simulated_scan_raw_height_mm is not None:
            baseline_zg = float(start_zg) + float(self._simulated_scan_approach_height_mm)
            trigger_zg = baseline_zg - float(self._simulated_scan_raw_height_mm)
            self._plate_sensor_present = True
            self._axes[Axis.Zg].position = float(trigger_zg)
            return {
                "detected": True,
                "final_zg": float(trigger_zg),
            }
        return super().scan_stack_with_gripper(
            start_zg=start_zg,
            end_zg=end_zg,
            speed=speed,
            transient_ms=transient_ms,
        )

    def set_simulated_scan_raw_height_mm(self, raw_height_mm: float | None, *, approach_height_mm: float = 10.0) -> None:
        self._simulated_scan_raw_height_mm = None if raw_height_mm is None else float(raw_height_mm)
        self._simulated_scan_approach_height_mm = float(approach_height_mm)


class ScanStackDebugController(RecordingSimulationController):
    def __init__(self):
        super().__init__()
        self.scan_calls: list[dict[str, float | str]] = []

    def scan_stack_with_gripper(
        self,
        *,
        start_zg: float,
        end_zg: float,
        speed: SpeedLevel,
        transient_ms: int = 0,
    ) -> dict[str, float | bool | str | None]:
        self.scan_calls.append({
            "start_zg": float(start_zg),
            "end_zg": float(end_zg),
            "speed": speed.name,
            "transient_ms": float(transient_ms),
        })
        result = dict(super().scan_stack_with_gripper(
            start_zg=start_zg,
            end_zg=end_zg,
            speed=speed,
            transient_ms=transient_ms,
        ))
        result.update({
            "scan_mode": "simulated_fast_path",
            "stop_strategy": "not_applicable",
            "elapsed_ms": 42.0,
            "poll_count": 7.0,
            "sensor_reads": 8.0,
            "sensor_read_failures": 0.0,
        })
        return result


class TipsOnFailingController(RecordingSimulationController):
    def jog(self, params) -> float:
        self.jog_calls.append(params)
        raise RuntimeError(
            'Exceeded destination position on the Z axis. Target was 113.908 and actual position was 118.901234865189.'
        )


class TipsOnDarwinWFaultRecoveryController(RecordingSimulationController):
    def __init__(self):
        super().__init__()
        self._w_disable_seen = False
        self.w_recovered = False

    def disable_motor(self, axis: Axis) -> None:
        super().disable_motor(axis)
        if axis == Axis.W:
            self._w_disable_seen = True

    def enable_motor(self, axis: Axis) -> None:
        super().enable_motor(axis)
        if axis == Axis.W and self._w_disable_seen:
            self.w_recovered = True

    def jog(self, params) -> float:
        self.jog_calls.append(params)
        if params.axis == Axis.Z and not self.w_recovered:
            raise RuntimeError(
                'Exception calling "ForceMove" with "8" argument(s): '
                '"An axis not involved in this move (addr 5.1) reported an error, '
                'error code was 0x50004 (Motor control error: Motor has gone over I2T current limit either by a spike or for a length of time)"'
            )
        return super().jog(params)


class PickupVerificationController(RecordingSimulationController):
    def __init__(self, failures_before_success: int):
        super().__init__()
        self.failures_before_success = failures_before_success
        self._last_snapshot = {
            "positions": {"X": 0.0, "Y": 0.0, "Z": 0.0, "G": 0.0, "Zg": 0.0},
            "telemetry": {"G": {"measured_current": 0.0, "last_peak_current_percent": 0.0, "last_force_percent": 0.0}},
        }
        self.forced_sensor_state: bool | None = None

    def move(self, moves: list[AxisMoveInfo], wait: bool = True, timeout_ms: int = 30000) -> None:
        super().move(moves, wait=wait, timeout_ms=timeout_ms)
        positions = dict((self._last_snapshot or {}).get("positions", {}) or {})
        for move in moves:
            positions[move.axis.name] = move.position
        self._last_snapshot = {
            "positions": positions,
            "telemetry": dict((self._last_snapshot or {}).get("telemetry", {}) or {}),
        }

    def open_gripper(self, position: float | None = None) -> None:
        super().open_gripper(position)
        positions = dict((self._last_snapshot or {}).get("positions", {}) or {})
        positions["G"] = 0.0 if position is None else float(position)
        self._last_snapshot = {
            "positions": positions,
            "telemetry": {"G": {"measured_current": 0.0, "last_peak_current_percent": 0.0, "last_force_percent": 0.0}},
        }

    def grip(self, speed: SpeedLevel, position: float, grip_lid: bool = False) -> None:
        self.grip_calls.append((speed, position, grip_lid))
        success = self.failures_before_success <= 0
        if self.failures_before_success > 0:
            self.failures_before_success -= 1
        self._axes[Axis.G].position = min(position, 6.0) if success else 11.2
        self._plate_in_gripper = success
        self._last_snapshot = {
            "positions": {
                "X": self._axes[Axis.X].position,
                "Y": self._axes[Axis.Y].position,
                "Z": self._axes[Axis.Z].position,
                "G": self._axes[Axis.G].position,
                "Zg": self._axes[Axis.Zg].position,
            },
            "telemetry": {
                "G": {
                    "measured_current": 0.12 if success else 0.0,
                    "last_peak_current_percent": 0.18 if success else 0.0,
                    "last_force_percent": 42.0 if success else 0.0,
                },
            },
        }

    def is_plate_in_gripper(self) -> bool:
        if self.forced_sensor_state is not None:
            return self.forced_sensor_state
        return super().is_plate_in_gripper()


class InitializeGPrehomeMoveFailingController(RecordingSimulationController):
    def move(self, moves: list[AxisMoveInfo], wait: bool = True, timeout_ms: int = 30000) -> None:
        if any(move.axis == Axis.G for move in moves):
            raise RuntimeError("simulated G-axis widen failure before home")
        super().move(moves, wait=wait, timeout_ms=timeout_ms)


class InitializeWNeedsParkingController(RecordingSimulationController):
    def home_axes(self, axes: list[Axis]) -> None:
        super().home_axes(axes)
        if axes == [Axis.W]:
            self._axes[Axis.W].position = 2.699


def _mark_initialize_cold_start(controller: RecordingSimulationController) -> None:
    controller._axes[Axis.Z].homed = False
    controller._axes[Axis.W].homed = False
    controller._axes[Axis.Zg].homed = False


async def _wait_for_pick_place_failure(bravo: Bravo) -> dict:
    for _ in range(200):
        state = bravo.get_state()
        if state.get("task_status", {}).get("status") == "failed":
            return state
        await asyncio.sleep(0.01)
    raise AssertionError("Pick/place task did not enter failed state")


async def _wait_for_engine_failure(bravo: Bravo):
    for _ in range(200):
        if bravo.engine.awaiting_error_action:
            return bravo.engine.current_task
        await asyncio.sleep(0.01)
    raise AssertionError("Engine did not enter error state")


@pytest.mark.asyncio
async def test_initialize_skips_head_check_and_homes_w_without_prompt_when_prompt_is_disabled():
    profile = BravoProfile.default()
    profile.head.check_on_init = False
    profile.safety.prompt_home_w = False

    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    _mark_initialize_cold_start(controller)
    engine = StateMachineEngine()

    await engine.execute(InitializeTask(controller, profile))

    assert controller.detect_smart_head_calls == 0
    assert controller.read_smart_head_type_calls == 0
    assert controller.read_head_adc_calls == 0
    assert controller.reset_faults_calls == [[Axis.X, Axis.Y, Axis.Z, Axis.W, Axis.G, Axis.Zg]]
    assert controller.home_axes_calls == [[Axis.Z], [Axis.Zg], [Axis.W]]


@pytest.mark.asyncio
async def test_initialize_skips_w_axis_when_profile_marks_it_ignored():
    profile = BravoProfile.default()
    profile.safety.ignore_w_axis = True
    profile.safety.prompt_home_w = False

    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    _mark_initialize_cold_start(controller)
    engine = StateMachineEngine()

    await engine.execute(InitializeTask(controller, profile))

    assert controller.reset_faults_calls == [[Axis.X, Axis.Y, Axis.Z, Axis.G, Axis.Zg]]
    assert controller.home_axes_calls == [[Axis.Z], [Axis.Zg]]


@pytest.mark.asyncio
async def test_initialize_plate_in_gripper_prompt_can_be_ignored_to_continue():
    profile = BravoProfile.default()
    profile.safety.prompt_home_w = False
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    _mark_initialize_cold_start(controller)
    controller._plate_in_gripper = True
    engine = StateMachineEngine()
    engine.set_error_handler(lambda error: None)
    task_obj = InitializeTask(controller, profile)

    task = asyncio.create_task(engine.execute(task_obj))
    for _ in range(50):
        if task_obj.status.name.lower() == "failed":
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("Initialize task did not pause for the plate-in-gripper prompt")

    payload = task_obj.status_payload()
    assert payload["operator_prompt"]["kind"] == "initialize_plate_in_gripper"
    assert task_obj.error is not None
    assert task_obj.error.step_name == "handle_plate_in_gripper"

    engine.ignore()
    await asyncio.wait_for(task, timeout=1.0)

    assert controller.home_axes_calls == [[Axis.Z], [Axis.Zg], [Axis.W]]


@pytest.mark.asyncio
async def test_initialize_darwin_ignore_plate_sensor_still_homes_g_when_preopen_fails():
    profile = BravoProfile.default()
    profile.connection.controller_type = "darwin_native"
    profile.safety.prompt_home_w = False
    controller = InitializeGPrehomeMoveFailingController()
    controller.open_tcp("simulation")
    _mark_initialize_cold_start(controller)
    controller._axes[Axis.G].homed = False
    controller._plate_in_gripper = True
    engine = StateMachineEngine()
    engine.set_error_handler(lambda error: None)
    task_obj = InitializeTask(controller, profile)

    task = asyncio.create_task(engine.execute(task_obj))
    for _ in range(50):
        if task_obj.status.name.lower() == "failed":
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("Initialize task did not pause for the plate-in-gripper prompt")

    assert task_obj.error is not None
    assert task_obj.error.step_name == "handle_plate_in_gripper"

    engine.ignore()
    await asyncio.wait_for(task, timeout=1.0)

    assert controller.home_axes_calls == [[Axis.Z], [Axis.G], [Axis.Zg], [Axis.W]]


@pytest.mark.asyncio
async def test_initialize_gripper_detect_prompt_can_force_gripper_home():
    profile = BravoProfile.default()
    profile.safety.prompt_home_w = False
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    _mark_initialize_cold_start(controller)
    controller._gripper_detected = GripperDetectionState.NOT_DETECTED
    engine = StateMachineEngine()
    engine.set_error_handler(lambda error: None)
    task_obj = InitializeTask(controller, profile)

    task = asyncio.create_task(engine.execute(task_obj))
    for _ in range(120):
        if task_obj.status.name.lower() == "failed":
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("Initialize task did not pause for the gripper-detect prompt")

    payload = task_obj.status_payload()
    assert payload["operator_prompt"]["kind"] == "initialize_detect_gripper"
    assert task_obj.error is not None
    assert task_obj.error.step_name == "detect_gripper"

    engine.ignore()
    await asyncio.wait_for(task, timeout=1.0)

    assert controller.home_axes_calls == [[Axis.Z], [Axis.Zg], [Axis.W]]


@pytest.mark.asyncio
async def test_initialize_suppresses_gripper_detect_prompt_when_g_and_zg_are_already_homed():
    profile = BravoProfile.default()
    profile.safety.prompt_home_w = False
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    controller._gripper_detected = GripperDetectionState.NOT_DETECTED
    engine = StateMachineEngine()
    engine.set_error_handler(lambda error: None)
    task_obj = InitializeTask(controller, profile)

    await engine.execute(task_obj)

    assert task_obj.status.name.lower() == "completed"
    assert task_obj.error is None
    assert controller.home_axes_calls == []


@pytest.mark.asyncio
async def test_initialize_w_axis_prompt_can_skip_w_home():
    profile = BravoProfile.default()
    profile.safety.prompt_home_w = True
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    _mark_initialize_cold_start(controller)
    engine = StateMachineEngine()
    engine.set_error_handler(lambda error: None)
    task_obj = InitializeTask(controller, profile)

    task = asyncio.create_task(engine.execute(task_obj))
    for _ in range(50):
        if task_obj.status.name.lower() == "failed":
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("Initialize task did not pause for the W-axis prompt")

    payload = task_obj.status_payload()
    assert payload["operator_prompt"]["kind"] == "initialize_home_w_axis"
    assert task_obj.error is not None
    assert task_obj.error.step_name == "prompt_home_w"

    engine.ignore()
    await asyncio.wait_for(task, timeout=1.0)

    assert controller.home_axes_calls == [[Axis.Z], [Axis.Zg]]


@pytest.mark.asyncio
async def test_initialize_w_axis_prompt_first_response_wins():
    profile = BravoProfile.default()
    profile.safety.prompt_home_w = True
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    _mark_initialize_cold_start(controller)
    engine = StateMachineEngine()
    engine.set_error_handler(lambda error: None)
    task_obj = InitializeTask(controller, profile)

    task = asyncio.create_task(engine.execute(task_obj))
    for _ in range(50):
        if task_obj.status.name.lower() == "failed":
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("Initialize task did not pause for the W-axis prompt")

    assert engine.retry() is True
    assert engine.ignore() is False
    assert engine.abort() is False
    await asyncio.wait_for(task, timeout=1.0)

    assert controller.home_axes_calls == [[Axis.Z], [Axis.Zg], [Axis.W]]


@pytest.mark.asyncio
async def test_get_state_exposes_initialize_prompt_step_name():
    profile = BravoProfile.default()
    profile.safety.prompt_home_w = True
    bravo = Bravo(profile=profile, mode="simulation")
    bravo.connect()
    _mark_initialize_cold_start(bravo.controller)

    try:
        task = asyncio.create_task(bravo.initialize())
        for _ in range(50):
            state = bravo.get_state()
            task_status = state.get("task_status", {})
            if task_status.get("status") == "failed":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("Initialize task did not pause for the W-axis prompt")

        assert task_status["task"] == "initialize"
        assert task_status["step"] == "prompt_home_w"
        assert task_status["step_index"] >= 0
        assert task_status["step_count"] >= 1

        bravo.retry()
        await asyncio.wait_for(task, timeout=1.0)
    finally:
        bravo.disconnect()


@pytest.mark.asyncio
async def test_initialize_skips_z_zg_and_w_when_already_homed_on_entry():
    profile = BravoProfile.default()
    profile.safety.prompt_home_w = False

    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    engine = StateMachineEngine()

    await engine.execute(InitializeTask(controller, profile))

    assert controller.home_axes_calls == []


@pytest.mark.asyncio
async def test_initialize_skips_plate_in_gripper_prompt_when_gripper_axes_are_already_homed():
    profile = BravoProfile.default()
    profile.safety.prompt_home_w = False

    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    controller._plate_in_gripper = True
    engine = StateMachineEngine()
    engine.set_error_handler(lambda error: None)
    task_obj = InitializeTask(controller, profile)

    await engine.execute(task_obj)

    assert task_obj.status.name.lower() == "completed"
    assert task_obj.error is None
    assert controller.home_axes_calls == []


@pytest.mark.asyncio
async def test_initialize_only_homes_unhomed_xy_axes():
    profile = BravoProfile.default()
    profile.safety.prompt_home_w = False

    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    controller._axes[Axis.Y].homed = False
    engine = StateMachineEngine()

    await engine.execute(InitializeTask(controller, profile))

    assert controller.home_axes_calls == [[Axis.Y]]


@pytest.mark.asyncio
async def test_initialize_does_not_prompt_for_w_when_w_is_already_homed():
    profile = BravoProfile.default()
    profile.safety.prompt_home_w = True

    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    engine = StateMachineEngine()
    engine.set_error_handler(lambda error: None)
    task_obj = InitializeTask(controller, profile)

    await engine.execute(task_obj)

    assert task_obj.status.name.lower() == "completed"
    assert task_obj.error is None
    assert controller.home_axes_calls == []


@pytest.mark.asyncio
async def test_initialize_parks_w_to_zero_after_home_if_controller_reports_nonzero_position():
    profile = BravoProfile.default()
    profile.safety.prompt_home_w = False

    controller = InitializeWNeedsParkingController()
    controller.open_tcp("simulation")
    _mark_initialize_cold_start(controller)
    engine = StateMachineEngine()

    await engine.execute(InitializeTask(controller, profile))

    assert controller.home_axes_calls == [[Axis.Z], [Axis.Zg], [Axis.W]]
    assert controller.get_position(Axis.W) == pytest.approx(0.0)
    assert any(
        len(moves) == 1
        and moves[0].axis == Axis.W
        and moves[0].position == pytest.approx(0.0)
        for moves in controller.move_calls
    )


class PartiallyFailingController:
    def __init__(self):
        self.is_connected = True

    def close(self) -> None:
        pass

    def get_position(self, axis: Axis) -> float:
        if axis in (Axis.G, Axis.Zg):
            raise BravoError(ErrorType.COULD_NOT_READ_POSITION, axis=axis)
        return {
            Axis.X: 1.0,
            Axis.Y: 2.0,
            Axis.Z: 3.0,
            Axis.W: 4.0,
        }[axis]

    def detect_smart_head(self) -> bool:
        raise BravoError(ErrorType.COULD_NOT_DETECT_SMART_HEAD)

    def is_go_button_pressed(self) -> bool:
        raise BravoError(ErrorType.COULD_NOT_QUERY_GO_BUTTON)

    def is_plate_in_gripper(self) -> bool:
        raise BravoError(ErrorType.GRIP_POSITION)

    def query_state(self) -> DeviceStateFlag:
        raise BravoError(ErrorType.COULD_NOT_QUERY_STATE)

    def open_serial(self, port: str) -> None:
        raise NotImplementedError

    def open_tcp(self, address: str) -> None:
        raise NotImplementedError

    def ping(self) -> bool:
        return True

    def get_firmware_version(self) -> FirmwareVersion:
        return FirmwareVersion()

    def move(self, moves, wait: bool = True, timeout_ms: int = 30000) -> None:
        raise NotImplementedError

    def home_axes(self, axes: list[Axis]) -> None:
        raise NotImplementedError

    def jog(self, params) -> float:
        raise NotImplementedError

    def is_axis_homed(self, axis: Axis) -> bool:
        return True

    def enable_motor(self, axis: Axis) -> None:
        raise NotImplementedError

    def disable_motor(self, axis: Axis) -> None:
        raise NotImplementedError

    def reset_faults(self, axes: list[Axis]) -> None:
        raise NotImplementedError

    def clear_go_button(self) -> None:
        raise NotImplementedError

    def set_light(self, command) -> None:
        raise NotImplementedError

    def clear_lights(self) -> None:
        raise NotImplementedError

    def read_head_adc(self) -> int:
        raise NotImplementedError

    def read_smart_head_type(self) -> int:
        raise NotImplementedError

    def detect_gripper(self) -> GripperDetectionState:
        return GripperDetectionState.NOT_DETECTED

    def grip(self, speed: SpeedLevel, position: float, grip_lid: bool = False) -> None:
        raise NotImplementedError

    def open_gripper(self, position: float | None = None) -> None:
        raise NotImplementedError

    def send_command(self, command_id: int, data: bytes = b"", timeout_ms: int = 2000) -> bytes:
        raise NotImplementedError

    @property
    def last_error(self):
        return None


def test_get_state_survives_partial_hardware_read_failures():
    bravo = Bravo(profile=BravoProfile.default())
    bravo._controller = PartiallyFailingController()

    state = bravo.get_state()

    assert state["connected"] is True
    assert state["positions"] == {
        "X": 1.0,
        "Y": 2.0,
        "Z": 3.0,
        "W": 4.0,
    }
    assert state["head_attached"] is False
    assert state["go_button_pressed"] is False
    assert state["plate_in_gripper"] is False
    assert state["robot_disabled"] is False


class DarwinAnalogHeadController(PartiallyFailingController):
    def read_head_adc(self) -> int:
        return 1806


def test_get_state_detects_darwin_analog_head_when_smart_head_is_absent():
    profile = BravoProfile.default()
    profile.connection.controller_type = "darwin"
    profile.head.head_type = HeadType.HT_384_D_70
    bravo = Bravo(profile=profile)
    bravo._controller = DarwinAnalogHeadController()

    state = bravo.get_state()

    assert state["head_attached"] is True
    assert state["positions"]["X"] == 1.0
    assert state["positions"]["Z"] == 3.0


class DarwinSnapshotNeedsWMotorRefreshController:
    def __init__(self):
        self.is_connected = True
        self._motor_enabled = [False] * len(Axis)
        self._motor_enabled[Axis.W] = True

    def close(self) -> None:
        pass

    def get_state_snapshot(self, max_age_s: float = 0.15):
        return {
            "positions": {"X": 1.0, "Y": 2.0, "Z": 3.0, "W": 4.0},
            "motors_enabled": {"X": True, "Y": True, "Z": True, "W": False},
            "head_attached": False,
            "go_button_pressed": False,
            "robot_disabled": False,
            "telemetry": {},
        }

    def is_motor_enabled(self, axis: Axis) -> bool:
        return axis != Axis.G and axis != Axis.Zg

    def detect_smart_head(self) -> bool:
        return False

    def read_head_adc(self) -> int:
        return 1806

    def is_go_button_pressed(self) -> bool:
        return False

    def is_plate_in_gripper(self) -> bool:
        return False

    def query_state(self) -> DeviceStateFlag:
        return DeviceStateFlag(0)

    def get_all_positions(self):
        return {"X": 1.0, "Y": 2.0, "Z": 3.0, "W": 4.0}

    def get_position(self, axis: Axis) -> float:
        return self.get_all_positions()[axis.name]


def test_get_state_rechecks_false_darwin_w_motor_and_cached_head_status():
    profile = BravoProfile.default()
    profile.connection.controller_type = "darwin"
    profile.head.head_type = HeadType.HT_384_D_70
    bravo = Bravo(profile=profile)
    bravo._controller = DarwinSnapshotNeedsWMotorRefreshController()

    state = bravo.get_state()

    assert state["head_attached"] is True
    assert state["motors_enabled"]["W"] is True
    assert state["motors_enabled"]["X"] is True


def test_set_tipbox_labware_persists_child_tip_definition_and_full_inventory():
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    bravo = Bravo(profile=profile)

    tipbox = LabwareDefinition(
        id="tipbox-d10",
        name="Agilent d10 Tipbox",
        kind="tip_box",
        base_class="tip_box",
        height_mm=50.0,
        wells=384,
        rows=16,
        cols=24,
        tip_definition_id="st_10ul",
        supported_tip_ids=["st_10ul", "st_30ul"],
        disposable_tip_capacity_ul=10.0,
    )
    bravo._labware_catalog = type("Catalog", (), {
        "get_definition": lambda self, labware_id: tipbox if labware_id == "tipbox-d10" else None,
        "list_definitions": lambda self: [tipbox],
    })()

    labware = bravo.set_labware(5, "tipbox-d10", tip_definition_id="st_10ul", tipbox_fill_state="full")

    assert labware.metadata["tip_definition_id"] == "st_10ul"
    inventory = bravo.get_state()["tipbox_inventory"]["5"]
    assert inventory["tip_id"] == "st_10ul"
    assert len(inventory["occupied"]) == 16 * 24


def test_set_tipbox_labware_can_start_empty_for_discard_use():
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    bravo = Bravo(profile=profile)

    tipbox = LabwareDefinition(
        id="tipbox-d10",
        name="Agilent d10 Tipbox",
        kind="tip_box",
        base_class="tip_box",
        height_mm=50.0,
        wells=384,
        rows=16,
        cols=24,
        tip_definition_id="st_10ul",
        supported_tip_ids=["st_10ul"],
        disposable_tip_capacity_ul=10.0,
    )
    bravo._labware_catalog = type("Catalog", (), {
        "get_definition": lambda self, labware_id: tipbox if labware_id == "tipbox-d10" else None,
        "list_definitions": lambda self: [tipbox],
    })()

    bravo.set_labware(5, "tipbox-d10", tip_definition_id="st_10ul", tipbox_fill_state="empty")

    inventory = bravo.get_state()["tipbox_inventory"]["5"]
    assert inventory["tip_id"] == "st_10ul"
    assert inventory["occupied"] == []


class SnapshotTelemetryController:
    def __init__(self):
        self.is_connected = True

    def close(self) -> None:
        pass

    def get_state_snapshot(self, max_age_s: float = 0.15):
        return {
            "positions": {"X": 10.0, "Y": 20.0, "Z": 30.0, "G": 9.0, "Zg": 95.0},
            "motors_enabled": {"X": True, "Y": True, "Z": True, "G": True, "Zg": True},
            "head_attached": True,
            "go_button_pressed": False,
            "robot_disabled": False,
            "telemetry": {
                "G": {
                    "last_peak_current_percent": 0.15,
                    "last_force_percent": 80.0,
                    "current_position_error": 0.25,
                    "last_command": {"mode": "grip", "target_position": 9.0},
                }
            },
        }

    def detect_smart_head(self) -> bool:
        return True

    def is_go_button_pressed(self) -> bool:
        return False

    def is_plate_in_gripper(self) -> bool:
        return False

    def query_state(self) -> DeviceStateFlag:
        return DeviceStateFlag(0)

    def get_all_positions(self):
        return {"X": 10.0, "Y": 20.0, "Z": 30.0, "G": 9.0, "Zg": 95.0}

    def get_position(self, axis: Axis) -> float:
        return self.get_all_positions()[axis.name]

    def open_serial(self, port: str) -> None:
        raise NotImplementedError

    def open_tcp(self, address: str) -> None:
        raise NotImplementedError

    def ping(self) -> bool:
        return True

    def get_firmware_version(self) -> FirmwareVersion:
        return FirmwareVersion()

    def move(self, moves, wait: bool = True, timeout_ms: int = 30000) -> None:
        raise NotImplementedError

    def home_axes(self, axes: list[Axis]) -> None:
        raise NotImplementedError

    def jog(self, params) -> float:
        raise NotImplementedError

    def is_axis_homed(self, axis: Axis) -> bool:
        return True

    def enable_motor(self, axis: Axis) -> None:
        raise NotImplementedError

    def disable_motor(self, axis: Axis) -> None:
        raise NotImplementedError

    def reset_faults(self, axes: list[Axis]) -> None:
        raise NotImplementedError

    def clear_go_button(self) -> None:
        raise NotImplementedError

    def set_light(self, command) -> None:
        raise NotImplementedError

    def clear_lights(self) -> None:
        raise NotImplementedError

    def read_head_adc(self) -> int:
        raise NotImplementedError

    def read_smart_head_type(self) -> int:
        return 0

    def detect_gripper(self) -> GripperDetectionState:
        return GripperDetectionState.DETECTED

    def grip(self, speed: SpeedLevel, position: float, grip_lid: bool = False) -> None:
        raise NotImplementedError

    def open_gripper(self, position: float | None = None) -> None:
        raise NotImplementedError

    def send_command(self, command_id: int, data: bytes = b"", timeout_ms: int = 2000) -> bytes:
        raise NotImplementedError

    @property
    def last_error(self):
        return None


def test_get_state_includes_snapshot_telemetry():
    bravo = Bravo(profile=BravoProfile.default())
    bravo._controller = SnapshotTelemetryController()

    state = bravo.get_state()

    assert state["positions"]["G"] == 9.0
    assert state["telemetry"]["G"]["last_peak_current_percent"] == 0.15
    assert state["telemetry"]["G"]["last_force_percent"] == 80.0
    assert state["telemetry"]["G"]["last_command"]["mode"] == "grip"


class BusyDarwinLikeController:
    def __init__(self):
        self.is_connected = True
        self._positions = [0.0] * len(Axis)
        self._positions[Axis.X.value] = 10.0
        self._positions[Axis.Y.value] = 20.0
        self._positions[Axis.Z.value] = 30.0
        self._positions[Axis.G.value] = 9.0
        self._positions[Axis.Zg.value] = 95.0
        self._motor_enabled = [True] * len(Axis)
        self.get_state_snapshot_calls = 0
        self.detect_smart_head_calls = 0
        self.is_go_button_pressed_calls = 0
        self.is_plate_in_gripper_calls = 0
        self.query_state_calls = 0

    def close(self) -> None:
        pass

    def get_state_snapshot(self, max_age_s: float = 0.15):
        self.get_state_snapshot_calls += 1
        raise AssertionError("Busy hardware path should not request a live snapshot")

    def detect_smart_head(self) -> bool:
        self.detect_smart_head_calls += 1
        raise AssertionError("Busy hardware path should not read head state")

    def is_go_button_pressed(self) -> bool:
        self.is_go_button_pressed_calls += 1
        raise AssertionError("Busy hardware path should not read go button")

    def is_plate_in_gripper(self) -> bool:
        self.is_plate_in_gripper_calls += 1
        raise AssertionError("Busy hardware path should not read gripper sensor")

    def query_state(self) -> DeviceStateFlag:
        self.query_state_calls += 1
        raise AssertionError("Busy hardware path should not query device state")

    def get_all_positions(self):
        raise AssertionError("Busy hardware path should not bulk-read positions")

    def get_position(self, axis: Axis) -> float:
        raise AssertionError("Busy hardware path should not read per-axis positions")

    def open_serial(self, port: str) -> None:
        raise NotImplementedError

    def open_tcp(self, address: str) -> None:
        raise NotImplementedError

    def ping(self) -> bool:
        return True

    def get_firmware_version(self) -> FirmwareVersion:
        return FirmwareVersion()

    def move(self, moves, wait: bool = True, timeout_ms: int = 30000) -> None:
        raise NotImplementedError

    def home_axes(self, axes: list[Axis]) -> None:
        raise NotImplementedError

    def jog(self, params) -> float:
        raise NotImplementedError

    def is_axis_homed(self, axis: Axis) -> bool:
        return True

    def enable_motor(self, axis: Axis) -> None:
        raise NotImplementedError

    def disable_motor(self, axis: Axis) -> None:
        raise NotImplementedError

    def reset_faults(self, axes: list[Axis]) -> None:
        raise NotImplementedError

    def clear_go_button(self) -> None:
        raise NotImplementedError

    def set_light(self, command) -> None:
        raise NotImplementedError

    def clear_lights(self) -> None:
        raise NotImplementedError

    def read_head_adc(self) -> int:
        raise NotImplementedError

    def read_smart_head_type(self) -> int:
        raise NotImplementedError

    def detect_gripper(self) -> GripperDetectionState:
        return GripperDetectionState.DETECTED

    def grip(self, speed: SpeedLevel, position: float, grip_lid: bool = False) -> None:
        raise NotImplementedError

    def open_gripper(self, position: float | None = None) -> None:
        raise NotImplementedError

    def send_command(self, command_id: int, data: bytes = b"", timeout_ms: int = 2000) -> bytes:
        raise NotImplementedError

    @property
    def last_error(self):
        return None


def test_get_state_uses_cached_values_while_real_hardware_task_is_busy():
    profile = BravoProfile.default()
    profile.connection.controller_type = "darwin"
    bravo = Bravo(profile=profile)
    bravo._controller = BusyDarwinLikeController()
    bravo.engine._current_task = object()

    try:
        state = bravo.get_state()
    finally:
        bravo.engine._current_task = None

    assert state["positions"]["X"] == 10.0
    assert state["positions"]["Y"] == 20.0
    assert state["positions"]["G"] == 9.0
    assert state["motors_enabled"]["X"] is True
    assert bravo.controller.get_state_snapshot_calls == 0
    assert bravo.controller.detect_smart_head_calls == 0
    assert bravo.controller.is_go_button_pressed_calls == 0
    assert bravo.controller.is_plate_in_gripper_calls == 0
    assert bravo.controller.query_state_calls == 0


@pytest.mark.asyncio
async def test_move_to_location_approach_stops_above_teachpoint():
    profile = BravoProfile.default()
    profile.safety.z_safe_position = 42.5
    tp = Teachpoints()
    tp.set_teachpoint(1, Axis.X, 123.0)
    tp.set_teachpoint(1, Axis.Y, 45.0)
    tp.set_teachpoint(1, Axis.Z, 12.0)
    profile.teachpoints = tp

    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    bravo._controller = controller

    await bravo.move_to_location(1, approach_height=5.0)

    assert len(controller.move_calls) == 3
    assert controller.move_calls[0][0].axis == Axis.Z
    assert controller.move_calls[0][0].position == 42.5
    assert [m.axis for m in controller.move_calls[1]] == [Axis.X, Axis.Y]
    assert controller.move_calls[2][0].axis == Axis.Z
    assert controller.move_calls[2][0].position == 7.0


@pytest.mark.asyncio
async def test_move_to_location_only_move_z_goes_to_safe_height():
    profile = BravoProfile.default()
    profile.safety.z_safe_position = 33.0
    tp = Teachpoints()
    tp.set_teachpoint(1, Axis.X, 123.0)
    tp.set_teachpoint(1, Axis.Y, 45.0)
    tp.set_teachpoint(1, Axis.Z, 12.0)
    profile.teachpoints = tp

    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    bravo._controller = controller

    await bravo.move_to_location(1, only_move_z=True)

    assert len(controller.move_calls) == 1
    assert controller.move_calls[0][0].axis == Axis.Z
    assert controller.move_calls[0][0].position == 33.0


@pytest.mark.asyncio
async def test_tips_on_uses_tip_box_geometry_and_sets_tip_state():
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    profile.head.default_tip_capacity = 30.0
    profile.head.teach_tip_capacity = 30.0
    profile.safety.z_safe_position = 42.5
    profile.safety.tip_press_dwell_time = 0
    tp = Teachpoints()
    tp.set_teachpoint(1, Axis.X, 123.0)
    tp.set_teachpoint(1, Axis.Y, 45.0)
    tp.set_teachpoint(1, Axis.Z, 60.0)
    profile.teachpoints = tp

    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    bravo._controller = controller

    tipbox = LabwareDefinition(
        id="tipbox-30",
        name="30 uL Tip Box",
        kind="tip_box",
        base_class="tip_box",
        height_mm=50.0,
        wells=384,
        disposable_tip_capacity_ul=30.0,
    )
    bravo.deck.set_single(1, Labware.from_definition(tipbox))

    await bravo.tips_on(1)

    assert len(controller.move_calls) == 3
    assert [(m.axis, m.position) for m in controller.move_calls[0]] == [
        (Axis.Z, 42.5),
    ]
    assert [(m.axis, m.position) for m in controller.move_calls[1]] == [
        (Axis.X, pytest.approx(120.75, abs=1e-6)),
        (Axis.Y, pytest.approx(42.75, abs=1e-6)),
    ]
    assert len(controller.jog_calls) == 1
    assert controller.jog_calls[0].axis == Axis.Z
    assert controller.jog_calls[0].max_position == pytest.approx(36.1, abs=1e-6)
    assert [(m.axis, m.position) for m in controller.move_calls[2]] == [
        (Axis.Z, 42.5),
    ]

    state = bravo.get_state()
    assert state["tips_on_head"] is True
    assert state["tip_labware"] == "30 uL Tip Box"
    assert state["attached_tip_length_mm"] == pytest.approx(26.1, abs=1e-6)
    assert state["tips_on_head_mode"]["subset_type"] == "all_barrels"


@pytest.mark.asyncio
async def test_tips_on_uses_teach_tip_reference_for_tipbox_top():
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    profile.head.default_tip_id = "st_30ul"
    profile.head.default_tip_capacity = 30.0
    profile.safety.z_safe_position = 42.5
    profile.safety.tip_press_dwell_time = 0
    tp = Teachpoints()
    tp.set_teachpoint(2, Axis.X, 194.918)
    tp.set_teachpoint(2, Axis.Y, 9.01924)
    tp.set_teachpoint(2, Axis.Z, 137.7)
    profile.teachpoints = tp

    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    bravo._controller = controller

    tipbox = LabwareDefinition(
        id="tipbox-10",
        name="384 V11 ST10 Tip Box 10734.102",
        kind="tip_box",
        base_class="tip_box",
        height_mm=50.0,
        wells=384,
        disposable_tip_capacity_ul=10.0,
        tip_definition_id="st_10ul",
        offset_x_mm=2.25,
        offset_y_mm=2.25,
        spacing_x_mm=4.5,
        spacing_y_mm=4.5,
    )
    bravo.deck.set_single(2, Labware.from_definition(tipbox))

    await bravo.tips_on(2)

    assert len(controller.jog_calls) == 1
    assert controller.jog_calls[0].max_position == pytest.approx(113.8, abs=1e-6)

    state = bravo.get_state()
    assert state["attached_tip_length_mm"] == pytest.approx(19.9, abs=1e-6)


@pytest.mark.asyncio
async def test_tips_on_resets_w_to_zero_before_press():
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    profile.safety.z_safe_position = 42.5
    tp = Teachpoints()
    tp.set_teachpoint(2, Axis.X, 194.918)
    tp.set_teachpoint(2, Axis.Y, 9.01924)
    tp.set_teachpoint(2, Axis.Z, 137.7)
    profile.teachpoints = tp

    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    controller._axes[Axis.W].position = 12.0
    bravo._controller = controller

    tipbox = LabwareDefinition(
        id="tipbox-10",
        name="384 V11 ST10 Tip Box 10734.102",
        kind="tip_box",
        base_class="tip_box",
        height_mm=50.0,
        wells=384,
        disposable_tip_capacity_ul=10.0,
        tip_definition_id="st_10ul",
        offset_x_mm=2.25,
        offset_y_mm=2.25,
        spacing_x_mm=4.5,
        spacing_y_mm=4.5,
    )
    bravo.deck.set_single(2, Labware.from_definition(tipbox))

    await bravo.tips_on(2)

    assert len(controller.move_calls) >= 3
    assert [(m.axis, m.position) for m in controller.move_calls[1]] == [
        (Axis.W, 0.0),
    ]
    assert controller.reset_faults_calls
    assert controller.reset_faults_calls[-1] == [Axis.X, Axis.Y, Axis.Z, Axis.W]


@pytest.mark.asyncio
async def test_tips_on_recycles_w_enable_on_darwin_before_z_press():
    profile = BravoProfile.default()
    profile.connection.controller_type = "darwin_native"
    profile.head.head_type = HeadType.HT_384_D_70
    profile.safety.z_safe_position = 42.5
    tp = Teachpoints()
    tp.set_teachpoint(2, Axis.X, 194.918)
    tp.set_teachpoint(2, Axis.Y, 9.01924)
    tp.set_teachpoint(2, Axis.Z, 137.7)
    profile.teachpoints = tp

    bravo = Bravo(profile=profile)
    controller = TipsOnDarwinWFaultRecoveryController()
    controller.open_tcp("simulation")
    controller._axes[Axis.W].position = 0.0
    bravo._controller = controller

    tipbox = LabwareDefinition(
        id="tipbox-10",
        name="384 V11 ST10 Tip Box 10734.102",
        kind="tip_box",
        base_class="tip_box",
        height_mm=50.0,
        wells=384,
        disposable_tip_capacity_ul=10.0,
        tip_definition_id="st_10ul",
        offset_x_mm=2.25,
        offset_y_mm=2.25,
        spacing_x_mm=4.5,
        spacing_y_mm=4.5,
    )
    bravo.deck.set_single(2, Labware.from_definition(tipbox))

    await bravo.tips_on(2)

    assert controller.w_recovered is True
    assert controller.jog_calls
    assert all(params.axis == Axis.Z for params in controller.jog_calls)


@pytest.mark.asyncio
async def test_tips_on_failure_retracts_to_safe_z_and_raises_clear_error():
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    profile.safety.z_safe_position = 42.5
    tp = Teachpoints()
    tp.set_teachpoint(1, Axis.X, 123.0)
    tp.set_teachpoint(1, Axis.Y, 45.0)
    tp.set_teachpoint(1, Axis.Z, 137.7)
    profile.teachpoints = tp

    bravo = Bravo(profile=profile)
    controller = TipsOnFailingController()
    controller.open_tcp("simulation")
    bravo._controller = controller

    tipbox = LabwareDefinition(
        id="tipbox-10",
        name="384 V11 ST10 Tip Box 10734.102",
        kind="tip_box",
        base_class="tip_box",
        height_mm=50.0,
        wells=384,
        disposable_tip_capacity_ul=10.0,
        tip_definition_id="st_10ul",
        offset_x_mm=2.25,
        offset_y_mm=2.25,
        spacing_x_mm=4.5,
        spacing_y_mm=4.5,
    )
    bravo.deck.set_single(1, Labware.from_definition(tipbox))

    task = asyncio.create_task(bravo.tips_on(1))
    failed_task = await _wait_for_engine_failure(bravo)
    assert "tipbox may be missing" in failed_task.error.message or "selected tips may be absent" in failed_task.error.message
    assert controller.move_calls[-1][0].axis == Axis.Z
    assert controller.move_calls[-1][0].position == pytest.approx(42.5, abs=1e-6)
    bravo.engine.abort()
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_tips_off_uses_tip_receptacle_and_clears_tip_state():
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    profile.head.default_tip_capacity = 30.0
    profile.head.teach_tip_capacity = 30.0
    profile.safety.z_safe_position = 42.5
    profile.safety.tips_off_w_position = -11.0
    profile.safety.tips_off_z_offset = 10.0
    tp = Teachpoints()
    tp.set_teachpoint(2, Axis.X, 222.0)
    tp.set_teachpoint(2, Axis.Y, 33.0)
    tp.set_teachpoint(2, Axis.Z, 60.0)
    profile.teachpoints = tp

    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    bravo._controller = controller

    tip_trash = LabwareDefinition(
        id="tip-trash",
        name="Tip Trash",
        kind="tip_trash",
        base_class="tip_trash",
        height_mm=5.0,
    )
    bravo.deck.set_single(2, Labware.from_definition(tip_trash))
    bravo._set_tip_state(
        labware_name="30 uL Tip Box",
        tip_length_mm=26.1,
        head_mode=normalize_head_mode(profile.head.head_type, "all_barrels", "front_left"),
        tip_selection=TipSelection(location=1, row=0, col=0),
    )

    await bravo.tips_off(2)

    # Steps: safe_z, move_xy, tip_touch (bump left + return), eject_z, eject_w, reset_w, retract_z
    assert len(controller.move_calls) == 8
    assert [(m.axis, m.position) for m in controller.move_calls[0]] == [
        (Axis.Z, 42.5),
    ]
    assert [(m.axis, m.position) for m in controller.move_calls[1]] == [
        (Axis.X, 222.0),
        (Axis.Y, 33.0),
    ]
    # Tip touch: X bumps left ~1mm then returns
    assert controller.move_calls[2][0].axis == Axis.X
    assert controller.move_calls[2][0].position < 222.0
    assert [(m.axis, m.position) for m in controller.move_calls[3]] == [
        (Axis.X, pytest.approx(222.0, abs=0.1)),
    ]
    assert [(m.axis, m.position) for m in controller.move_calls[4]] == [
        (Axis.Z, pytest.approx(71.1, abs=1e-6)),
    ]
    assert [(m.axis, m.position) for m in controller.move_calls[5]] == [
        (Axis.W, -11.0),
    ]
    assert [(m.axis, m.position) for m in controller.move_calls[6]] == [
        (Axis.W, 0.0),
    ]
    assert [(m.axis, m.position) for m in controller.move_calls[7]] == [
        (Axis.Z, 42.5),
    ]

    state = bravo.get_state()
    assert state["tips_on_head"] is False
    assert state["tip_labware"] == ""
    assert state["attached_tip_length_mm"] is None
    assert state["tips_on_head_mode"] is None


@pytest.mark.asyncio
async def test_tips_on_applies_head_mode_xy_offset():
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    profile.head.default_tip_capacity = 30.0
    profile.head.teach_tip_capacity = 30.0
    profile.safety.z_safe_position = 42.5
    tp = Teachpoints()
    tp.set_teachpoint(8, Axis.X, 192.48)
    tp.set_teachpoint(8, Axis.Y, 224.166)
    tp.set_teachpoint(8, Axis.Z, 60.0)
    profile.teachpoints = tp

    bravo = Bravo(profile=profile)
    bravo.set_head_mode("rectangle", "front_left", row_count=8, column_count=12)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    bravo._controller = controller

    tipbox = LabwareDefinition(
        id="tipbox-30",
        name="30 uL Tip Box",
        kind="tip_box",
        base_class="tip_box",
        height_mm=50.0,
        wells=384,
        disposable_tip_capacity_ul=30.0,
    )
    bravo.deck.set_single(8, Labware.from_definition(tipbox))

    await bravo.tips_on(8)

    assert [(m.axis, m.position) for m in controller.move_calls[1]] == [
        (Axis.X, pytest.approx(244.23, abs=1e-6)),
        (Axis.Y, pytest.approx(185.916, abs=1e-6)),
    ]


def test_rectangle_head_mode_preserves_arbitrary_row_and_column_counts():
    mode = normalize_head_mode(HeadType.HT_384_D_70, "rectangle", "front_left", row_count=2, column_count=7)
    assert mode.row_count == 2
    assert mode.column_count == 7
    assert mode.num_channels == 14


def test_front_left_rectangle_head_anchor_uses_front_row_and_left_column():
    mode = normalize_head_mode(HeadType.HT_384_D_70, "rectangle", "front_left", row_count=4, column_count=4)
    assert head_anchor_cell(HeadType.HT_384_D_70, mode) == (15, 0)


@pytest.mark.asyncio
async def test_back_left_rectangle_uses_front_right_tipbox_anchor():
    """A back-left head rectangle draws from the front-right of the tipbox.

    Two different corners are reported and they are easy to confuse:

    ``mirror_corner`` is where the selected block sits in the tipbox — a
    back-left head mode mirrors to the tipbox's front-right corner, so the 5x9
    block starts at (11, 15) and runs to (15, 23).

    ``anchor_row``/``anchor_col`` is the tipbox cell under the head's own
    reference barrel, which for a back-left mode is the block's back-left cell,
    (11, 15). It follows ``head_anchor``, not ``mirror_corner``.
    """
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    bravo._controller = controller
    mode = bravo.set_head_mode("rectangle", "back_left", row_count=5, column_count=9)

    tipbox = LabwareDefinition(
        id="tipbox-10",
        name="10 uL Tip Box",
        kind="tip_box",
        base_class="tip_box",
        height_mm=50.0,
        wells=384,
        disposable_tip_capacity_ul=10.0,
    )
    labware = Labware.from_definition(tipbox)
    bravo.deck.set_single(2, labware)

    anchors = bravo._legal_tip_anchors(2, labware, mode, purpose="pickup")
    assert anchors == [{
        "row": 11,
        "col": 15,
        "row_count": 5,
        "column_count": 9,
        "mirror_corner": "front_right",
        "head_anchor": "back_left",
        "anchor_row": 11,
        "anchor_col": 15,
    }]


def test_back_left_rectangle_tip_xy_uses_mirrored_anchor_cell_not_region_origin():
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    tp = Teachpoints()
    tp.set_teachpoint(2, Axis.X, 100.0)
    tp.set_teachpoint(2, Axis.Y, 50.0)
    tp.set_teachpoint(2, Axis.Z, 60.0)
    profile.teachpoints = tp

    bravo = Bravo(profile=profile)
    mode = bravo.set_head_mode("rectangle", "back_left", row_count=5, column_count=9)

    tipbox = LabwareDefinition(
        id="tipbox-10",
        name="10 uL Tip Box",
        kind="tip_box",
        base_class="tip_box",
        height_mm=50.0,
        wells=384,
        disposable_tip_capacity_ul=10.0,
    )
    labware = Labware.from_definition(tipbox)
    selection = tipbox_selection(2, 11, 15, mode)

    target_x, target_y = bravo._tip_xy_target(2, labware, mode, selection.row, selection.col)

    assert target_x == pytest.approx(165.25, abs=1e-6)
    assert target_y == pytest.approx(97.25, abs=1e-6)


def test_set_tip_selection_maps_clicked_rectangle_tip_to_legal_region_origin():
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    bravo = Bravo(profile=profile)
    bravo.set_head_mode("rectangle", "back_left", row_count=4, column_count=4)

    tipbox = LabwareDefinition(
        id="tipbox-10",
        name="10 uL Tip Box",
        kind="tip_box",
        base_class="tip_box",
        height_mm=50.0,
        wells=384,
        disposable_tip_capacity_ul=10.0,
    )
    bravo.deck.set_single(2, Labware.from_definition(tipbox))

    selection = bravo.set_tip_selection(2, 15, 23)

    assert selection.row == 12
    assert selection.col == 20
    assert selection.row_count == 4
    assert selection.column_count == 4


@pytest.mark.asyncio
async def test_unreachable_front_rectangle_has_no_legal_pickup_anchor():
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    tp = Teachpoints()
    tp.set_teachpoint(2, Axis.X, 7.70114)
    tp.set_teachpoint(2, Axis.Y, 9.01924)
    tp.set_teachpoint(2, Axis.Z, 60.0)
    profile.teachpoints = tp

    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    bravo._controller = controller
    mode = bravo.set_head_mode("rectangle", "front_left", row_count=5, column_count=9)

    tipbox = LabwareDefinition(
        id="tipbox-10",
        name="10 uL Tip Box",
        kind="tip_box",
        base_class="tip_box",
        height_mm=50.0,
        wells=384,
        disposable_tip_capacity_ul=10.0,
    )
    labware = Labware.from_definition(tipbox)
    bravo.deck.set_single(2, labware)

    anchors = bravo._legal_tip_anchors(2, labware, mode, purpose="pickup")
    assert anchors == []


@pytest.mark.asyncio
async def test_tips_on_rejects_non_tip_box_labware():
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    profile.head.default_tip_capacity = 30.0
    profile.head.teach_tip_capacity = 30.0
    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    bravo._controller = controller

    plate = LabwareDefinition(
        id="plate-1",
        name="Microplate",
        kind="sbs_plate",
        base_class="microplate",
        height_mm=14.0,
    )
    bravo.deck.set_single(1, Labware.from_definition(plate))

    with pytest.raises(RuntimeError, match="tip box"):
        await bravo.tips_on(1)


@pytest.mark.asyncio
async def test_tips_on_blocks_when_shifted_full_head_overlaps_equal_height_neighbor():
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    profile.safety.z_safe_position = 42.5
    profile.safety.tip_press_dwell_time = 0
    tp = Teachpoints()
    tp.set_default_teachpoints(profile.head.head_type)
    profile.teachpoints = tp

    bravo = Bravo(profile=profile)
    bravo.set_head_mode("column", "front_right", column_count=1)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    bravo._controller = controller

    tipbox = LabwareDefinition(
        id="tipbox-10",
        name="384 V11 ST10 Tip Box 10734.102",
        kind="tip_box",
        base_class="tip_box",
        height_mm=50.0,
        wells=384,
        disposable_tip_capacity_ul=10.0,
        tip_definition_id="st_10ul",
        offset_x_mm=2.25,
        offset_y_mm=2.25,
        spacing_x_mm=4.5,
        spacing_y_mm=4.5,
    )
    bravo.deck.set_single(4, Labware.from_definition(tipbox))
    bravo.deck.set_single(5, Labware.from_definition(tipbox))

    task = asyncio.create_task(bravo.tips_on(5))
    failed_task = await _wait_for_engine_failure(bravo)
    assert "Tips On at location 5 is blocked:" in failed_task.error.message
    assert "location 4 top 50.0 mm" in failed_task.error.message
    bravo.engine.abort()
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_tip_selection_requires_accessible_edge_for_single_column():
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    bravo._controller = controller
    bravo.set_head_mode("column", "front_left", column_count=1)

    tipbox = LabwareDefinition(
        id="tipbox-30",
        name="30 uL Tip Box",
        kind="tip_box",
        base_class="tip_box",
        height_mm=50.0,
        wells=384,
        disposable_tip_capacity_ul=30.0,
    )
    bravo.deck.set_single(1, Labware.from_definition(tipbox))

    with pytest.raises(RuntimeError, match="not accessible"):
        bravo.set_tip_selection(1, 0, 0)

    selection = bravo.set_tip_selection(1, 0, 23)
    assert selection.col == 23


@pytest.mark.asyncio
async def test_partial_column_pickup_advances_to_next_accessible_column():
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    bravo._controller = controller
    mode = bravo.set_head_mode("column", "front_left", column_count=1)

    tipbox = LabwareDefinition(
        id="tipbox-30",
        name="30 uL Tip Box",
        kind="tip_box",
        base_class="tip_box",
        height_mm=50.0,
        wells=384,
        disposable_tip_capacity_ul=30.0,
    )
    labware = Labware.from_definition(tipbox)
    bravo.deck.set_single(1, labware)

    selection = bravo._effective_tip_selection(1, labware, mode, purpose="pickup")
    assert selection.col == 23
    assert selection.row == 0

    bravo._apply_tipbox_selection(1, mode, selection, purpose="pickup")

    next_selection = bravo._effective_tip_selection(1, labware, mode, purpose="pickup")
    assert next_selection.col == 22
    assert next_selection.row == 0

    inventory = bravo.get_state()["tipbox_inventory"]["1"]
    assert not any(item["row"] == 0 and item["col"] == 23 for item in inventory["legal_pickup_anchors"])
    assert any(item["row"] == 0 and item["col"] == 22 for item in inventory["legal_pickup_anchors"])


@pytest.mark.asyncio
async def test_tips_off_into_tip_box_requires_empty_legal_anchor():
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    bravo._controller = controller
    mode = normalize_head_mode(profile.head.head_type, "column", "front_left", column_count=1)

    tipbox = LabwareDefinition(
        id="tipbox-30",
        name="30 uL Tip Box",
        kind="tip_box",
        base_class="tip_box",
        height_mm=50.0,
        wells=384,
        disposable_tip_capacity_ul=30.0,
    )
    labware = Labware.from_definition(tipbox)
    bravo.deck.set_single(1, labware)
    bravo._set_tip_state(
        labware_name="30 uL Tip Box",
        tip_length_mm=26.1,
        head_mode=mode,
        tip_selection=tipbox_selection(1, 0, 23, mode),
    )

    with pytest.raises(RuntimeError, match="No legal tip anchors|not accessible"):
        bravo.set_tip_selection(1, 0, 23)

    bravo._apply_tipbox_selection(1, mode, tipbox_selection(1, 0, 23, mode), purpose="pickup")
    selection = bravo.set_tip_selection(1, 0, 23)
    assert selection.col == 23


@pytest.mark.asyncio
async def test_back_left_row_mode_uses_front_row_tipbox_without_full_head_xy_pose():
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    profile.safety.z_safe_position = 42.5
    profile.safety.tip_press_dwell_time = 0
    tp = Teachpoints()
    tp.set_teachpoint(2, Axis.X, 100.0)
    tp.set_teachpoint(2, Axis.Y, 50.0)
    tp.set_teachpoint(2, Axis.Z, 60.0)
    profile.teachpoints = tp

    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    bravo._controller = controller
    mode = bravo.set_head_mode("row", "back_left", row_count=1)

    tipbox = LabwareDefinition(
        id="tipbox-10",
        name="10 uL Tip Box",
        kind="tip_box",
        base_class="tip_box",
        height_mm=50.0,
        wells=384,
        disposable_tip_capacity_ul=10.0,
    )
    labware = Labware.from_definition(tipbox)
    bravo.deck.set_single(2, labware)

    selection = bravo._effective_tip_selection(2, labware, mode, purpose="pickup")
    assert selection.row == 15
    assert selection.col == 0

    await bravo.tips_on(2)

    xy_move = [(m.axis, m.position) for m in controller.move_calls[1]]
    assert xy_move == [
        (Axis.X, pytest.approx(97.75, abs=1e-6)),
        (Axis.Y, pytest.approx(115.25, abs=1e-6)),
    ]


def test_well_center_offset_uses_vendor_a1_reference_for_dense_formats():
    assert well_center_offset_from_teachpoint_mm({"wells": 96}, row=0, col=0) == pytest.approx((0.0, 0.0))
    assert well_center_offset_from_teachpoint_mm({"wells": 384}, row=0, col=0) == pytest.approx((-2.25, -2.25))
    assert well_center_offset_from_teachpoint_mm({"wells": 1536}, row=0, col=0) == pytest.approx((-3.375, -3.375))
    assert well_center_offset_from_teachpoint_mm({"wells": 384}, row=1, col=1) == pytest.approx((2.25, 2.25))


@pytest.mark.asyncio
async def test_aspirate_moves_to_corrected_384_a1_center():
    profile = BravoProfile.default()
    profile.teachpoints = Teachpoints()
    profile.teachpoints.set_teachpoint(1, Axis.X, 10.0)
    profile.teachpoints.set_teachpoint(1, Axis.Y, 20.0)
    profile.teachpoints.set_teachpoint(1, Axis.Z, 30.0)
    profile.connection.machine_id = "BRAVO_A"
    profile.head.head_type = HeadType.HT_384_D_70
    profile.head.teach_tip_capacity = 30.0

    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    bravo._controller = controller
    bravo.deck.set_single(
        1,
        Labware.from_definition(
            LabwareDefinition(
                id="plate-384",
                name="384 Greiner 781091 PS uclear",
                kind="sbs_plate",
                height_mm=14.4,
                stack_height_mm=8.6,
                gripper_offset_mm=2.5,
                rows=16,
                cols=24,
                offset_x_mm=2.25,
                offset_y_mm=2.25,
                spacing_x_mm=4.5,
                spacing_y_mm=4.5,
            )
        ),
    )
    _attach_test_tips(bravo)

    await bravo.aspirate(1, 10.0, distance_from_bottom=2.0)

    assert [(m.axis, m.position) for m in controller.move_calls[1]] == [
        (Axis.X, pytest.approx(7.75)),
        (Axis.Y, pytest.approx(17.75)),
    ]


@pytest.mark.asyncio
async def test_aspirate_uses_safe_anchor_when_left_neighbor_blocks_front_right_column_positions():
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    tp = Teachpoints()
    tp.set_default_teachpoints(profile.head.head_type)
    profile.teachpoints = tp

    bravo = Bravo(profile=profile)
    bravo.set_head_mode("column", "front_right", column_count=1)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    bravo._controller = controller

    plate = LabwareDefinition(
        id="plate-384",
        name="384 Greiner 781091 PS uclear",
        kind="sbs_plate",
        base_class="plate",
        height_mm=14.4,
        stack_height_mm=8.6,
        gripper_offset_mm=2.5,
        rows=16,
        cols=24,
        offset_x_mm=2.25,
        offset_y_mm=2.25,
        spacing_x_mm=4.5,
        spacing_y_mm=4.5,
    )
    tipbox = LabwareDefinition(
        id="tipbox-10",
        name="384 V11 ST10 Tip Box 10734.102",
        kind="tip_box",
        base_class="tip_box",
        height_mm=50.0,
        wells=384,
        disposable_tip_capacity_ul=10.0,
        tip_definition_id="st_10ul",
        offset_x_mm=2.25,
        offset_y_mm=2.25,
        spacing_x_mm=4.5,
        spacing_y_mm=4.5,
    )
    bravo.deck.set_single(4, Labware.from_definition(tipbox))
    bravo.deck.set_single(5, Labware.from_definition(plate))
    state = bravo.get_plate_selection_state(5)
    _attach_test_tips(bravo)

    assert [anchor["col"] for anchor in state["legal_anchors"]] == list(range(12, 24))
    assert state["selection"] == {"location": 5, "row": 0, "col": 12}

    await bravo.aspirate(5, 10.0, distance_from_bottom=2.0)

    assert bravo.get_state()["plate_selection"]["5"] == {"location": 5, "row": 0, "col": 12}


@pytest.mark.asyncio
async def test_tips_off_requires_tips_on_head():
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    profile.head.default_tip_capacity = 30.0
    profile.head.teach_tip_capacity = 30.0
    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    bravo._controller = controller

    tip_trash = LabwareDefinition(
        id="tip-trash",
        name="Tip Trash",
        kind="tip_trash",
        base_class="tip_trash",
        height_mm=5.0,
    )
    bravo.deck.set_single(1, Labware.from_definition(tip_trash))

    task = asyncio.create_task(bravo.tips_off(1))
    failed_task = await _wait_for_engine_failure(bravo)
    assert "No tips are currently" in failed_task.error.message
    bravo.engine.abort()
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_tips_off_all_barrels_is_not_blocked_by_non_overlapping_tipbox():
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    profile.head.default_tip_capacity = 30.0
    profile.head.teach_tip_capacity = 30.0
    profile.safety.z_safe_position = 42.5
    profile.safety.tips_off_w_position = -11.0
    profile.safety.tips_off_z_offset = 10.0
    tp = Teachpoints()
    tp.set_default_teachpoints(profile.head.head_type)
    profile.teachpoints = tp

    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    bravo._controller = controller

    tipbox = LabwareDefinition(
        id="tipbox-10",
        name="384 V11 ST10 Tip Box 10734.102",
        kind="tip_box",
        base_class="tip_box",
        height_mm=50.0,
        length_mm=127.76,
        width_mm=85.48,
        wells=384,
        disposable_tip_capacity_ul=10.0,
        tip_definition_id="st_10ul",
        offset_x_mm=2.25,
        offset_y_mm=2.25,
        spacing_x_mm=4.5,
        spacing_y_mm=4.5,
    )
    tip_trash = LabwareDefinition(
        id="tip-trash",
        name="Tip Trash",
        kind="tip_trash",
        base_class="tip_trash",
        height_mm=5.0,
        length_mm=127.76,
        width_mm=85.48,
    )
    bravo.deck.set_single(2, Labware.from_definition(tip_trash))
    bravo.deck.set_single(6, Labware.from_definition(tipbox))
    bravo._set_tip_state(
        labware_name="384 V11 ST10 Tip Box 10734.102",
        tip_length_mm=19.9,
        head_mode=normalize_head_mode(profile.head.head_type, "all_barrels", "back_left"),
        tip_selection=TipSelection(location=2, row=0, col=0),
    )

    await bravo.tips_off(2)


@pytest.mark.asyncio
async def test_pick_place_uses_labware_geometry_and_updates_deck():
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    profile.head.default_tip_capacity = 30.0
    profile.head.teach_tip_capacity = 30.0
    profile.safety.z_safe_position = 0.0
    profile.gripper.y_offset = -0.42
    tp = Teachpoints()
    tp.set_teachpoint(1, Axis.X, 7.70114)
    tp.set_teachpoint(1, Axis.Y, 8.96845)
    tp.set_teachpoint(1, Axis.Z, 137.808)
    tp.set_teachpoint(2, Axis.X, 194.918)
    tp.set_teachpoint(2, Axis.Y, 9.01924)
    tp.set_teachpoint(2, Axis.Z, 137.700)
    profile.teachpoints = tp

    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    bravo._controller = controller

    definition = LabwareDefinition(
        id="plate-384",
        name="384 Greiner 781091 PS uclear",
        kind="sbs_plate",
        height_mm=14.4,
        stack_height_mm=8.6,
        gripper_offset_mm=2.5,
    )
    bravo.deck.set_single(1, Labware.from_definition(definition))

    await bravo.pick_place(1, 2)

    assert bravo.deck.get_stack(1).top is None
    assert bravo.deck.get_stack(2).top is not None

    assert len(controller.move_calls) == 9
    assert [(m.axis, m.position) for m in controller.move_calls[0]] == [
        (Axis.Z, 0.0),
    ]
    assert [(m.axis, m.position) for m in controller.move_calls[1]] == [
        (Axis.Zg, -20.0),
    ]
    assert [(m.axis, m.position) for m in controller.move_calls[2]] == [
        (Axis.X, pytest.approx(7.70114)),
        (Axis.Y, pytest.approx(6.29845)),
    ]
    assert [(m.axis, m.position) for m in controller.move_calls[3]] == [
        (Axis.Z, pytest.approx(42.3080, abs=1e-3)),
        (Axis.Zg, pytest.approx(100.0, abs=1e-3)),
    ]
    assert controller.grip_calls[-1] == (SpeedLevel.MED, 9.0, False)
    assert [(m.axis, m.position) for m in controller.move_calls[4]] == [
        (Axis.Z, pytest.approx(22.3080, abs=1e-3)),
        (Axis.Zg, pytest.approx(100.0, abs=1e-3)),
    ]
    assert [(m.axis, m.position) for m in controller.move_calls[5]] == [
        (Axis.X, pytest.approx(194.918)),
        (Axis.Y, pytest.approx(6.34924)),
    ]
    assert [(m.axis, m.position) for m in controller.move_calls[6]] == [
        (Axis.Z, pytest.approx(42.2000, abs=1e-3)),
        (Axis.Zg, pytest.approx(100.0, abs=1e-3)),
    ]
    assert [(m.axis, m.position) for m in controller.move_calls[7]] == [
        (Axis.G, 0.0),
    ]
    assert [(m.axis, m.position) for m in controller.move_calls[8]] == [
        (Axis.Zg, -20.0),
    ]


@pytest.mark.asyncio
async def test_stack_plates_moves_source_plate_onto_destination_stack():
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    profile.head.default_tip_capacity = 30.0
    profile.head.teach_tip_capacity = 30.0
    profile.safety.z_safe_position = 0.0
    profile.gripper.y_offset = -0.42
    tp = Teachpoints()
    tp.set_teachpoint(4, Axis.X, 7.70114)
    tp.set_teachpoint(4, Axis.Y, 8.96845)
    tp.set_teachpoint(4, Axis.Z, 137.808)
    tp.set_teachpoint(5, Axis.X, 194.918)
    tp.set_teachpoint(5, Axis.Y, 9.01924)
    tp.set_teachpoint(5, Axis.Z, 137.700)
    profile.teachpoints = tp

    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    bravo._controller = controller

    base_definition = LabwareDefinition(
        id="base-plate",
        name="384 Greiner 781091 PS uclear",
        kind="sbs_plate",
        base_class="microplate",
        height_mm=14.4,
        stack_height_mm=8.6,
        gripper_offset_mm=2.5,
    )
    source_definition = LabwareDefinition(
        id="source-plate",
        name="384 Greiner 781091 PS uclear",
        kind="sbs_plate",
        base_class="microplate",
        height_mm=14.4,
        stack_height_mm=8.6,
        gripper_offset_mm=2.5,
    )
    bravo.deck.set_single(5, Labware.from_definition(base_definition))
    bravo.deck.set_single(4, Labware.from_definition(source_definition))

    result = await bravo.stack_plates(base_location=5, source_location=4)

    assert result["status"] == "completed"
    assert bravo.deck.get_stack(4).top is None
    stack = bravo.deck.get_stack(5)
    assert len(stack.items) == 2
    assert stack.items[0].name == "384 Greiner 781091 PS uclear"
    assert stack.items[1].name == "384 Greiner 781091 PS uclear"


@pytest.mark.asyncio
async def test_destack_plate_moves_top_plate_from_stack_to_empty_pad():
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    profile.head.default_tip_capacity = 30.0
    profile.head.teach_tip_capacity = 30.0
    profile.safety.z_safe_position = 0.0
    profile.gripper.y_offset = -0.42
    tp = Teachpoints()
    tp.set_teachpoint(4, Axis.X, 7.70114)
    tp.set_teachpoint(4, Axis.Y, 8.96845)
    tp.set_teachpoint(4, Axis.Z, 137.808)
    tp.set_teachpoint(5, Axis.X, 194.918)
    tp.set_teachpoint(5, Axis.Y, 9.01924)
    tp.set_teachpoint(5, Axis.Z, 137.700)
    profile.teachpoints = tp

    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    bravo._controller = controller

    definition = LabwareDefinition(
        id="plate-384",
        name="384 Greiner 781091 PS uclear",
        kind="sbs_plate",
        base_class="microplate",
        height_mm=14.4,
        stack_height_mm=8.6,
        gripper_offset_mm=2.5,
    )
    bravo.deck.add(5, Labware.from_definition(definition))
    bravo.deck.add(5, Labware.from_definition(definition))

    result = await bravo.destack_plate(source_location=5, destination_location=4)

    assert result["status"] == "completed"
    assert result["remaining_stack_count"] == 1
    source_stack = bravo.deck.get_stack(5)
    dest_top = bravo.deck.get_stack(4).top
    assert len(source_stack.items) == 1
    assert source_stack.top is not None
    assert source_stack.top.name == "384 Greiner 781091 PS uclear"
    assert dest_top is not None
    assert dest_top.name == "384 Greiner 781091 PS uclear"


def test_deck_stacking_height_uses_stack_thickness_for_existing_plate():
    deck = DeckState()
    definition = LabwareDefinition(
        id="plate-384",
        name="384 Greiner 781091 PS uclear",
        kind="sbs_plate",
        base_class="microplate",
        height_mm=14.4,
        stack_height_mm=8.6,
        gripper_offset_mm=2.5,
    )
    deck.set_single(5, Labware.from_definition(definition))

    assert deck.get_height(5) == pytest.approx(14.4)
    assert deck.get_location_height(5) == pytest.approx(0.0)
    assert deck.get_stacking_height(5) == pytest.approx(8.6)


@pytest.mark.asyncio
async def test_stack_plates_place_uses_destination_stacking_height_not_full_height():
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    profile.head.default_tip_capacity = 30.0
    profile.head.teach_tip_capacity = 30.0
    profile.head.teach_tip_length_mm = 26.1
    profile.safety.z_safe_position = 0.0
    profile.gripper.y_offset = -0.42
    tp = Teachpoints()
    tp.set_teachpoint(4, Axis.X, 7.70114)
    tp.set_teachpoint(4, Axis.Y, 8.96845)
    tp.set_teachpoint(4, Axis.Z, 137.808)
    tp.set_teachpoint(5, Axis.X, 194.918)
    tp.set_teachpoint(5, Axis.Y, 9.01924)
    tp.set_teachpoint(5, Axis.Z, 137.700)
    profile.teachpoints = tp

    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    bravo._controller = controller

    definition = LabwareDefinition(
        id="plate-384",
        name="384 Greiner 781091 PS uclear",
        kind="sbs_plate",
        base_class="microplate",
        height_mm=14.4,
        stack_height_mm=8.6,
        gripper_offset_mm=2.5,
    )
    bravo.deck.set_single(5, Labware.from_definition(definition))
    bravo.deck.set_single(4, Labware.from_definition(definition))
    assert bravo.deck.get_stacking_height(5) == pytest.approx(8.6)

    await bravo.stack_plates(base_location=5, source_location=4)

    assert [(m.axis, m.position) for m in controller.move_calls[6]] == [
        (Axis.Z, pytest.approx(33.6, abs=1e-3)),
        (Axis.Zg, pytest.approx(100.0, abs=1e-3)),
    ]


@pytest.mark.asyncio
async def test_delid_plate_moves_only_lid_and_uses_lid_grip():
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    profile.head.default_tip_capacity = 30.0
    profile.head.teach_tip_capacity = 30.0
    profile.safety.z_safe_position = 0.0
    profile.gripper.y_offset = -0.42
    tp = Teachpoints()
    tp.set_teachpoint(5, Axis.X, 194.918)
    tp.set_teachpoint(5, Axis.Y, 9.01924)
    tp.set_teachpoint(5, Axis.Z, 137.700)
    tp.set_teachpoint(9, Axis.X, 380.000)
    tp.set_teachpoint(9, Axis.Y, 200.000)
    tp.set_teachpoint(9, Axis.Z, 137.650)
    profile.teachpoints = tp

    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    bravo._controller = controller

    definition = LabwareDefinition(
        id="plate-384",
        name="384 Greiner 781091 PS uclear",
        kind="sbs_plate",
        base_class="microplate",
        length_mm=127.76,
        width_mm=85.48,
        height_mm=14.4,
        stack_height_mm=8.6,
        gripper_offset_mm=2.5,
        can_have_lid=True,
        lidded_height_mm=16.5,
        lid_resting_height_mm=9.5,
        lid_departure_height_mm=8.5,
    )
    bravo.deck.set_single(5, Labware.from_definition(definition, is_lidded=True))

    result = await bravo.delid_plate(plate_location=5, lid_destination=9)

    assert result["status"] == "completed"
    assert controller.grip_calls[-1] == (SpeedLevel.MED, 9.0, True)
    source_top = bravo.deck.get_stack(5).top
    dest_top = bravo.deck.get_stack(9).top
    assert source_top is not None
    assert source_top.name == "384 Greiner 781091 PS uclear"
    assert source_top.is_lidded is False
    assert source_top.height == pytest.approx(14.4)
    assert dest_top is not None
    assert dest_top.name == "384 Greiner 781091 PS uclear Lid"
    assert dest_top.labware_type == "lid"
    assert dest_top.height == pytest.approx(7.0)


@pytest.mark.asyncio
async def test_relid_plate_moves_lid_back_onto_plate():
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    profile.head.default_tip_capacity = 30.0
    profile.head.teach_tip_capacity = 30.0
    profile.safety.z_safe_position = 0.0
    profile.gripper.y_offset = -0.42
    tp = Teachpoints()
    tp.set_teachpoint(5, Axis.X, 194.918)
    tp.set_teachpoint(5, Axis.Y, 9.01924)
    tp.set_teachpoint(5, Axis.Z, 137.700)
    tp.set_teachpoint(9, Axis.X, 380.000)
    tp.set_teachpoint(9, Axis.Y, 200.000)
    tp.set_teachpoint(9, Axis.Z, 137.650)
    profile.teachpoints = tp

    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    bravo._controller = controller

    definition = LabwareDefinition(
        id="plate-384",
        name="384 Greiner 781091 PS uclear",
        kind="sbs_plate",
        base_class="microplate",
        length_mm=127.76,
        width_mm=85.48,
        height_mm=14.4,
        stack_height_mm=8.6,
        gripper_offset_mm=2.5,
        lid_gripper_offset_mm=2.8,
        can_have_lid=True,
        lidded_height_mm=16.5,
        lidded_stack_height_mm=14.5,
        lid_resting_height_mm=9.5,
        lid_departure_height_mm=8.5,
    )
    bravo.deck.set_single(5, Labware.from_definition(definition))
    bravo.deck.set_single(9, synthesize_lid_labware(Labware.from_definition(definition, is_lidded=True)))

    result = await bravo.relid_plate(lid_location=9, plate_location=5)

    assert result["status"] == "completed"
    assert controller.grip_calls[-1] == (SpeedLevel.MED, 9.0, True)
    place_move = controller.move_calls[6]
    place_axes = {m.axis: m.position for m in place_move}
    assert Axis.Z in place_axes
    assert Axis.Zg in place_axes
    assert place_axes[Axis.Zg] == pytest.approx(100.0, abs=1e-3)
    assert bravo.deck.get_stack(9).top is None
    dest_top = bravo.deck.get_stack(5).top
    assert dest_top is not None
    assert dest_top.name == "384 Greiner 781091 PS uclear"
    assert dest_top.is_lidded is True
    assert dest_top.height == pytest.approx(16.5)
    assert dest_top.stack_height == pytest.approx(14.5)
    assert dest_top.metadata["generated_lid"]["lid_gripper_offset_mm"] == pytest.approx(2.8)


# -- Well-access guards: lidded / sealed plates --


def _make_lidded_bravo(*, is_lidded: bool = True, is_sealed: bool = False) -> Bravo:
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_96_D_200
    profile.safety.z_safe_position = 0.0
    tp = Teachpoints()
    tp.set_default_teachpoints(profile.head.head_type)
    profile.teachpoints = tp
    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    bravo._controller = controller
    definition = LabwareDefinition(
        id="plate-96-lid-test",
        name="96 Greiner Test Plate",
        kind="sbs_plate",
        base_class="microplate",
        wells=96,
        rows=8,
        cols=12,
        spacing_x_mm=9.0,
        spacing_y_mm=9.0,
        height_mm=14.4,
        stack_height_mm=8.6,
        gripper_offset_mm=0.5,
        can_have_lid=True,
        lidded_height_mm=16.5,
        lidded_stack_height_mm=14.5,
        lid_resting_height_mm=9.5,
        lid_departure_height_mm=8.5,
    )
    bravo.deck.set_single(5, Labware.from_definition(definition, is_lidded=is_lidded, is_sealed=is_sealed))
    _attach_test_tips(bravo)
    return bravo


@pytest.mark.asyncio
async def test_aspirate_blocked_when_lidded():
    bravo = _make_lidded_bravo(is_lidded=True)
    with pytest.raises(RuntimeError, match="has a lid"):
        await bravo.aspirate(5, volume=50.0)


@pytest.mark.asyncio
async def test_dispense_blocked_when_lidded():
    bravo = _make_lidded_bravo(is_lidded=True)
    with pytest.raises(RuntimeError, match="has a lid"):
        await bravo.dispense(5, volume=50.0)


@pytest.mark.asyncio
async def test_mix_blocked_when_lidded():
    bravo = _make_lidded_bravo(is_lidded=True)
    with pytest.raises(RuntimeError, match="has a lid"):
        await bravo.mix(5, volume=50.0)


@pytest.mark.asyncio
async def test_aspirate_blocked_when_sealed():
    bravo = _make_lidded_bravo(is_lidded=False, is_sealed=True)
    with pytest.raises(RuntimeError, match="is sealed"):
        await bravo.aspirate(5, volume=50.0)


@pytest.mark.asyncio
async def test_aspirate_allowed_when_not_lidded():
    bravo = _make_lidded_bravo(is_lidded=False)
    await bravo.aspirate(5, volume=50.0)


@pytest.mark.asyncio
async def test_dispense_allowed_when_not_lidded():
    bravo = _make_lidded_bravo(is_lidded=False)
    await bravo.dispense(5, volume=50.0)


def test_delid_clamps_invalid_plate_gripper_offset_when_lid_specific_offset_is_missing():
    definition = LabwareDefinition(
        id="cellvis-384",
        name="384 CellVis 1.5H",
        kind="sbs_plate",
        base_class="microplate",
        length_mm=127.76,
        width_mm=85.48,
        height_mm=14.3,
        stack_height_mm=14.3,
        gripper_offset_mm=8.0,
        can_have_lid=True,
        lidded_height_mm=16.4,
        lid_resting_height_mm=9.7,
    )
    plate = Labware.from_definition(definition, is_lidded=True)

    lid_offset = DelidPlateTask._resolve_lid_gripper_offset(plate)

    assert lid_offset == pytest.approx(6.7)


def test_delid_uses_explicit_lid_gripper_offset_when_present():
    definition = LabwareDefinition(
        id="cellvis-384",
        name="384 CellVis 1.5H",
        kind="sbs_plate",
        base_class="microplate",
        length_mm=127.76,
        width_mm=85.48,
        height_mm=14.3,
        stack_height_mm=14.3,
        gripper_offset_mm=8.0,
        lid_gripper_offset_mm=2.8,
        can_have_lid=True,
        lidded_height_mm=16.4,
        lid_resting_height_mm=9.7,
    )
    plate = Labware.from_definition(definition, is_lidded=True)

    lid_offset = DelidPlateTask._resolve_lid_gripper_offset(plate)

    assert lid_offset == pytest.approx(2.8)


def test_lidded_labware_metadata_includes_generated_lid_geometry():
    definition = LabwareDefinition(
        id="plate-384",
        name="384 Greiner 781091 PS uclear",
        kind="sbs_plate",
        base_class="microplate",
        length_mm=127.76,
        width_mm=85.48,
        height_mm=14.4,
        stack_height_mm=8.6,
        can_have_lid=True,
        lidded_height_mm=16.5,
        lidded_stack_height_mm=14.5,
        lid_resting_height_mm=9.5,
        lid_departure_height_mm=8.5,
    )

    labware = Labware.from_definition(definition, is_lidded=True)

    assert labware.metadata["base_height_mm"] == pytest.approx(14.4)
    assert labware.metadata["total_height_mm"] == pytest.approx(16.5)
    generated_lid = labware.metadata.get("generated_lid")
    assert generated_lid is not None
    assert generated_lid["length_mm"] == pytest.approx(127.76)
    assert generated_lid["width_mm"] == pytest.approx(85.48)
    assert generated_lid["height_mm"] == pytest.approx(7.0)
    assert generated_lid["render_mode"] == "generated_lid"


def test_lidded_labware_uses_lidded_stack_geometry():
    definition = LabwareDefinition(
        id="plate-384",
        name="384 Greiner 781091 PS uclear",
        kind="sbs_plate",
        base_class="microplate",
        length_mm=127.76,
        width_mm=85.48,
        height_mm=14.4,
        stack_height_mm=8.6,
        can_have_lid=True,
        lidded_height_mm=16.5,
        lidded_stack_height_mm=14.5,
        lid_resting_height_mm=9.5,
        lid_departure_height_mm=8.5,
    )

    labware = Labware.from_definition(definition, is_lidded=True)
    deck = DeckState()
    deck.set_single(5, labware)

    assert labware.height == pytest.approx(16.5)
    assert labware.stack_height == pytest.approx(14.5)
    assert labware.metadata["stack_height_mm"] == pytest.approx(14.5)
    assert deck.get_stacking_height(5) == pytest.approx(14.5)


def test_sealed_labware_uses_sealed_stack_geometry():
    definition = LabwareDefinition(
        id="plate-384",
        name="384 Greiner 781091 PS uclear",
        kind="sbs_plate",
        base_class="microplate",
        height_mm=14.4,
        stack_height_mm=8.6,
        can_be_sealed=True,
        sealed_height_mm=15.2,
        sealed_stacking_height_mm=10.1,
    )

    labware = Labware.from_definition(definition, is_sealed=True)

    assert labware.height == pytest.approx(15.2)
    assert labware.stack_height == pytest.approx(10.1)
    assert labware.metadata["stack_height_mm"] == pytest.approx(10.1)


def test_synthesized_lid_uses_parent_plate_footprint():
    definition = LabwareDefinition(
        id="plate-384",
        name="384 Greiner 781091 PS uclear",
        kind="sbs_plate",
        base_class="microplate",
        length_mm=127.76,
        width_mm=85.48,
        height_mm=14.4,
        stack_height_mm=8.6,
        can_have_lid=True,
        lidded_height_mm=16.5,
        lid_resting_height_mm=9.5,
        lid_departure_height_mm=8.5,
    )
    plate = Labware.from_definition(definition, is_lidded=True)

    lid = synthesize_lid_labware(plate)

    assert lid.name == "384 Greiner 781091 PS uclear Lid"
    assert lid.labware_type == "lid"
    assert lid.length == pytest.approx(127.76)
    assert lid.width == pytest.approx(85.48)
    assert lid.height == pytest.approx(7.0)


def test_synthesized_lid_uses_lid_specific_gripper_offset():
    definition = LabwareDefinition(
        id="plate-384",
        name="384 Greiner 781091 PS uclear",
        kind="sbs_plate",
        base_class="microplate",
        length_mm=127.76,
        width_mm=85.48,
        height_mm=14.4,
        stack_height_mm=8.6,
        gripper_offset_mm=2.5,
        lid_gripper_offset_mm=2.8,
        can_have_lid=True,
        lidded_height_mm=16.5,
        lid_resting_height_mm=9.5,
    )
    plate = Labware.from_definition(definition, is_lidded=True)

    lid = synthesize_lid_labware(plate)

    assert lid.gripper_offset == pytest.approx(2.8)
    assert lid.metadata["lid_gripper_offset_mm"] == pytest.approx(2.8)


def test_pick_place_debug_plan_reports_source_plate_height():
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    profile.head.default_tip_capacity = 30.0
    profile.head.teach_tip_capacity = 30.0
    profile.head.teach_tip_length_mm = 26.1
    profile.safety.z_safe_position = 0.0
    tp = Teachpoints()
    tp.set_teachpoint(1, Axis.X, 7.70114)
    tp.set_teachpoint(1, Axis.Y, 8.96845)
    tp.set_teachpoint(1, Axis.Z, 137.808)
    tp.set_teachpoint(2, Axis.X, 194.918)
    tp.set_teachpoint(2, Axis.Y, 9.01924)
    tp.set_teachpoint(2, Axis.Z, 137.700)
    profile.teachpoints = tp

    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    deck = DeckState()
    definition = LabwareDefinition(
        id="plate-384",
        name="384 Greiner 781091 PS uclear",
        kind="sbs_plate",
        height_mm=14.4,
        stack_height_mm=8.6,
        gripper_offset_mm=2.5,
    )
    deck.set_single(1, Labware.from_definition(definition))

    task = PickPlaceTask(
        controller=controller,
        teachpoints=tp,
        profile=profile,
        deck=deck,
        from_location=1,
        to_location=2,
    )

    plan = task.debug_plan()
    assert plan["source_pick_height_mm"] == pytest.approx(14.4, abs=1e-6)
    assert plan["source_top_z"] == pytest.approx(123.408, abs=1e-3)
    assert plan["source_grip_plane_z"] == pytest.approx(135.308, abs=1e-3)


def test_pick_place_calibrates_disposable_teach_tip_reference_for_10ul():
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    profile.head.teach_tip_capacity = 10.0
    profile.head.teach_tip_length_mm = 19.9
    tp = Teachpoints()
    tp.set_teachpoint(1, Axis.X, 7.70114)
    tp.set_teachpoint(1, Axis.Y, 8.96845)
    tp.set_teachpoint(1, Axis.Z, 137.808)
    tp.set_teachpoint(2, Axis.X, 194.918)
    tp.set_teachpoint(2, Axis.Y, 9.01924)
    tp.set_teachpoint(2, Axis.Z, 137.700)
    profile.teachpoints = tp

    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    deck = DeckState()
    definition = LabwareDefinition(
        id="plate-384",
        name="384 Greiner 781091 PS uclear",
        kind="sbs_plate",
        height_mm=14.4,
        stack_height_mm=8.6,
        gripper_offset_mm=2.5,
    )
    deck.set_single(1, Labware.from_definition(definition))

    task = PickPlaceTask(
        controller=controller,
        teachpoints=tp,
        profile=profile,
        deck=deck,
        from_location=1,
        to_location=2,
    )

    assert task._gripper_pad_reference_zg(19.9) == pytest.approx(0.8, abs=1e-6)
    assert task._positions.pick_z == pytest.approx(36.1080, abs=1e-3)
    assert task._positions.pick_zg == pytest.approx(100.0, abs=1e-3)


def test_pick_place_uses_clearance_carry_pose_instead_of_zero_zero():
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    profile.head.teach_tip_capacity = 30.0
    profile.head.teach_tip_length_mm = 26.1
    profile.safety.z_safe_position = 0.0
    tp = Teachpoints()
    tp.set_teachpoint(1, Axis.X, 7.70114)
    tp.set_teachpoint(1, Axis.Y, 8.96845)
    tp.set_teachpoint(1, Axis.Z, 137.808)
    tp.set_teachpoint(2, Axis.X, 194.918)
    tp.set_teachpoint(2, Axis.Y, 9.01924)
    tp.set_teachpoint(2, Axis.Z, 137.700)
    profile.teachpoints = tp

    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    deck = DeckState()
    definition = LabwareDefinition(
        id="plate-384",
        name="384 Greiner 781091 PS uclear",
        kind="sbs_plate",
        height_mm=14.4,
        stack_height_mm=8.6,
        gripper_offset_mm=2.5,
    )
    deck.set_single(1, Labware.from_definition(definition))

    task = PickPlaceTask(
        controller=controller,
        teachpoints=tp,
        profile=profile,
        deck=deck,
        from_location=1,
        to_location=2,
    )

    assert task._positions.carry_z > 0.0
    assert task._positions.carry_zg == pytest.approx(100.0, abs=1e-3)


def test_get_state_forces_no_plate_when_g_axis_is_above_likely_threshold():
    bravo = Bravo(profile=BravoProfile.default())
    controller = SnapshotTelemetryController()
    controller.get_state_snapshot = lambda max_age_s=0.15: {
        "positions": {"X": 10.0, "Y": 20.0, "Z": 30.0, "G": 7.2, "Zg": 95.0},
        "motors_enabled": {"X": True, "Y": True, "Z": True, "G": True, "Zg": True},
        "head_attached": True,
        "go_button_pressed": False,
        "robot_disabled": False,
        "telemetry": {},
    }
    controller.is_plate_in_gripper = lambda: True
    bravo._controller = controller

    state = bravo.get_state()

    assert state["plate_in_gripper"] is False


@pytest.mark.asyncio
async def test_pick_place_missing_plate_can_be_ignored_without_corrupting_deck():
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    profile.head.teach_tip_capacity = 30.0
    profile.head.teach_tip_length_mm = 26.1
    profile.safety.z_safe_position = 0.0
    tp = Teachpoints()
    tp.set_teachpoint(1, Axis.X, 7.70114)
    tp.set_teachpoint(1, Axis.Y, 8.96845)
    tp.set_teachpoint(1, Axis.Z, 137.808)
    tp.set_teachpoint(2, Axis.X, 194.918)
    tp.set_teachpoint(2, Axis.Y, 9.01924)
    tp.set_teachpoint(2, Axis.Z, 137.700)
    profile.teachpoints = tp

    bravo = Bravo(profile=profile)
    controller = PickupVerificationController(failures_before_success=999)
    controller.open_tcp("simulation")
    bravo._controller = controller

    definition = LabwareDefinition(
        id="plate-384",
        name="384 Greiner 781091 PS uclear",
        kind="sbs_plate",
        height_mm=14.4,
        stack_height_mm=8.6,
        gripper_offset_mm=2.5,
    )
    bravo.deck.set_single(1, Labware.from_definition(definition))

    task = asyncio.create_task(bravo.pick_place(1, 2))
    failed_task = await _wait_for_engine_failure(bravo)
    assert failed_task.error.step_name == "grip_plate"

    bravo.engine.ignore()
    await asyncio.wait_for(task, timeout=5.0)

    assert bravo.deck.get_stack(1).top is None
    assert bravo.deck.get_stack(2).top is not None


@pytest.mark.asyncio
async def test_pick_place_missing_plate_can_be_retried_and_then_succeed():
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    profile.head.teach_tip_capacity = 30.0
    profile.head.teach_tip_length_mm = 26.1
    profile.safety.z_safe_position = 0.0
    tp = Teachpoints()
    tp.set_teachpoint(1, Axis.X, 7.70114)
    tp.set_teachpoint(1, Axis.Y, 8.96845)
    tp.set_teachpoint(1, Axis.Z, 137.808)
    tp.set_teachpoint(2, Axis.X, 194.918)
    tp.set_teachpoint(2, Axis.Y, 9.01924)
    tp.set_teachpoint(2, Axis.Z, 137.700)
    profile.teachpoints = tp

    bravo = Bravo(profile=profile)
    controller = PickupVerificationController(failures_before_success=1)
    controller.open_tcp("simulation")
    bravo._controller = controller

    definition = LabwareDefinition(
        id="plate-384",
        name="384 Greiner 781091 PS uclear",
        kind="sbs_plate",
        height_mm=14.4,
        stack_height_mm=8.6,
        gripper_offset_mm=2.5,
    )
    bravo.deck.set_single(1, Labware.from_definition(definition))

    task = asyncio.create_task(bravo.pick_place(1, 2))
    await _wait_for_engine_failure(bravo)
    bravo.engine.retry()
    await asyncio.wait_for(task, timeout=5.0)

    assert bravo.deck.get_stack(1).top is None
    assert bravo.deck.get_stack(2).top is not None
    assert len(controller.grip_calls) == 2


@pytest.mark.asyncio
async def test_pick_place_g_rule_rejects_pickup_when_g_is_10_or_more():
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    profile.head.teach_tip_capacity = 30.0
    profile.head.teach_tip_length_mm = 26.1
    profile.safety.z_safe_position = 0.0
    tp = Teachpoints()
    tp.set_teachpoint(1, Axis.X, 7.70114)
    tp.set_teachpoint(1, Axis.Y, 8.96845)
    tp.set_teachpoint(1, Axis.Z, 137.808)
    tp.set_teachpoint(2, Axis.X, 194.918)
    tp.set_teachpoint(2, Axis.Y, 9.01924)
    tp.set_teachpoint(2, Axis.Z, 137.700)
    profile.teachpoints = tp

    bravo = Bravo(profile=profile)
    controller = PickupVerificationController(failures_before_success=999)
    controller.forced_sensor_state = True
    controller.open_tcp("simulation")
    bravo._controller = controller

    definition = LabwareDefinition(
        id="plate-384",
        name="384 Greiner 781091 PS uclear",
        kind="sbs_plate",
        height_mm=14.4,
        stack_height_mm=8.6,
        gripper_offset_mm=2.5,
    )
    bravo.deck.set_single(1, Labware.from_definition(definition))

    task = asyncio.create_task(bravo.pick_place(1, 2))
    await _wait_for_engine_failure(bravo)
    state = bravo.get_state()
    bravo.engine.abort()
    await asyncio.wait_for(task, timeout=5.0)

    assert state["task_status"]["pickup_verification"]["g_rule_failed"] is True
    assert state["task_status"]["pickup_verification"]["sensor_detected"] is True


@pytest.mark.asyncio
async def test_pick_place_g_rule_accepts_pickup_when_g_is_below_10_even_if_sensor_disagrees():
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    profile.head.teach_tip_capacity = 30.0
    profile.head.teach_tip_length_mm = 26.1
    profile.safety.z_safe_position = 0.0
    tp = Teachpoints()
    tp.set_teachpoint(1, Axis.X, 7.70114)
    tp.set_teachpoint(1, Axis.Y, 8.96845)
    tp.set_teachpoint(1, Axis.Z, 137.808)
    tp.set_teachpoint(2, Axis.X, 194.918)
    tp.set_teachpoint(2, Axis.Y, 9.01924)
    tp.set_teachpoint(2, Axis.Z, 137.700)
    profile.teachpoints = tp

    bravo = Bravo(profile=profile)
    controller = PickupVerificationController(failures_before_success=0)
    controller.forced_sensor_state = False
    controller.open_tcp("simulation")
    bravo._controller = controller

    definition = LabwareDefinition(
        id="plate-384",
        name="384 Greiner 781091 PS uclear",
        kind="sbs_plate",
        height_mm=14.4,
        stack_height_mm=8.6,
        gripper_offset_mm=2.5,
    )
    bravo.deck.set_single(1, Labware.from_definition(definition))

    await bravo.pick_place(1, 2)

    assert bravo.deck.get_stack(1).top is None
    assert bravo.deck.get_stack(2).top is not None


@pytest.mark.asyncio
async def test_pick_place_does_not_treat_peak_current_limit_as_pickup_evidence():
    profile = BravoProfile.default()
    profile.head.head_type = HeadType.HT_384_D_70
    profile.head.teach_tip_capacity = 30.0
    profile.head.teach_tip_length_mm = 26.1
    profile.safety.z_safe_position = 0.0
    tp = Teachpoints()
    tp.set_teachpoint(1, Axis.X, 7.70114)
    tp.set_teachpoint(1, Axis.Y, 8.96845)
    tp.set_teachpoint(1, Axis.Z, 137.808)
    tp.set_teachpoint(2, Axis.X, 194.918)
    tp.set_teachpoint(2, Axis.Y, 9.01924)
    tp.set_teachpoint(2, Axis.Z, 137.700)
    profile.teachpoints = tp

    bravo = Bravo(profile=profile)
    controller = PickupVerificationController(failures_before_success=999)
    controller.open_tcp("simulation")
    controller._last_snapshot = {
        "positions": {"X": 0.0, "Y": 0.0, "Z": 0.0, "G": 11.2, "Zg": 0.0},
        "telemetry": {"G": {"peak_current": 0.5, "last_force_percent": 0.0}},
    }
    bravo._controller = controller

    definition = LabwareDefinition(
        id="plate-384",
        name="384 Greiner 781091 PS uclear",
        kind="sbs_plate",
        height_mm=14.4,
        stack_height_mm=8.6,
        gripper_offset_mm=2.5,
    )
    bravo.deck.set_single(1, Labware.from_definition(definition))

    task = asyncio.create_task(bravo.pick_place(1, 2))
    await _wait_for_engine_failure(bravo)
    state = bravo.get_state()
    bravo.engine.abort()
    await asyncio.wait_for(task, timeout=5.0)

    assert state["task_status"]["pickup_verification"]["current_detected"] is False
    assert state["task_status"]["pickup_verification"]["peak_current_after"] == pytest.approx(0.0, abs=1e-9)


def test_liquid_class_lookup_is_scoped_by_machine_head_and_tip(tmp_path, monkeypatch):
    monkeypatch.setenv("PYBRAVO_LIQUID_CLASS_STORE_PATH", str(tmp_path / "liquid_classes.yaml"))
    liquid_classes_store.create_liquid_class(
        {
            "name": "Water",
            "machine_id": "BRAVO_A",
            "head_type": "HT_384_D_70",
            "tip_capacity_ul": 30.0,
        }
    )
    liquid_classes_store.create_liquid_class(
        {
            "name": "Water",
            "machine_id": "BRAVO_B",
            "head_type": "HT_384_D_70",
            "tip_capacity_ul": 30.0,
        }
    )

    match = liquid_classes_store.get_liquid_class(
        "Water",
        machine_id="BRAVO_A",
        head_type="HT_384_D_70",
        tip_capacity_ul=30.0,
    )
    mismatch = liquid_classes_store.get_liquid_class(
        "Water",
        machine_id="BRAVO_A",
        head_type="HT_384_D_70",
        tip_capacity_ul=10.0,
    )

    assert match is not None
    assert match["machine_id"] == "BRAVO_A"
    assert mismatch is None


@pytest.mark.asyncio
async def test_aspirate_applies_liquid_class_kinematics_and_polynomial():
    profile = BravoProfile.default()
    profile.safety.z_safe_position = 0.0
    profile.teachpoints = Teachpoints()
    profile.teachpoints.set_teachpoint(1, Axis.X, 10.0)
    profile.teachpoints.set_teachpoint(1, Axis.Y, 20.0)
    profile.teachpoints.set_teachpoint(1, Axis.Z, 30.0)
    profile.connection.machine_id = "BRAVO_A"
    profile.head.head_type = HeadType.HT_384_D_70
    profile.head.teach_tip_capacity = 30.0

    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    bravo._controller = controller

    liquid_class = {
        "name": "Water",
        "machine_id": "BRAVO_A",
        "head_type": "HT_384_D_70",
        "tip_capacity_ul": 30.0,
        "aspirate": {
            "w_velocity_ul_s": 123.0,
            "w_acceleration_ul_s2": 456.0,
            "post_delay_ms": 0,
            "z_in_velocity_mm_s": 33.0,
            "z_in_acceleration_mm_s2": 44.0,
            "z_out_velocity_mm_s": 55.0,
            "z_out_acceleration_mm_s2": 66.0,
        },
        "dispense": {},
        "equation": {"control_points": [{"desired_ul": 0.0, "commanded_ul": 1.0}, {"desired_ul": 10.0, "commanded_ul": 21.0}]},
    }

    # target_z = teachpoint(30) - plate_height(14.5) + well_depth(10) - distance_from_bottom(2) = 23.5
    plate_def = LabwareDefinition(
        id="test-plate", name="Test Plate", kind="plate", base_class="plate",
        height_mm=14.5, wells=384, rows=16, cols=24,
        spacing_x_mm=4.5, spacing_y_mm=4.5, offset_x_mm=2.25, offset_y_mm=2.25,
        well_depth_mm=10.0,
    )
    bravo.deck.set_single(1, Labware.from_definition(plate_def))
    bravo._resolve_liquid_class = lambda name: liquid_class if name else None
    _attach_test_tips(bravo)
    await bravo.aspirate(1, 10.0, distance_from_bottom=2.0, liquid_class="Water")

    z_moves = [
        call[0]
        for call in controller.move_calls
        if len(call) == 1 and call[0].axis == Axis.Z
    ]
    # Enter fast leg: safe_z(0) -> corrected plate-top head position(15.5)
    assert any(
        move.position == pytest.approx(15.5)
        and move.velocity == pytest.approx(0.0)
        and move.acceleration == pytest.approx(0.0)
        for move in z_moves
    )
    # Enter slow leg: corrected top(15.5) -> target(23.5) with z_in kinematics
    assert any(
        move.position == pytest.approx(23.5)
        and move.velocity == pytest.approx(33.0)
        and move.acceleration == pytest.approx(44.0)
        for move in z_moves
    )
    # Exit slow leg: target(23.5) -> corrected top(15.5) with z_out kinematics
    assert any(
        move.position == pytest.approx(15.5)
        and move.velocity == pytest.approx(55.0)
        and move.acceleration == pytest.approx(66.0)
        for move in z_moves
    )
    # Exit fast leg: corrected top(15.5) -> safe_z(0)
    assert z_moves[-1].position == pytest.approx(0.0)
    assert z_moves[-1].velocity == pytest.approx(0.0)
    assert z_moves[-1].acceleration == pytest.approx(0.0)
    w_move = next(
        call[0] for call in controller.move_calls
        if len(call) == 1 and call[0].axis == Axis.W
    )
    assert w_move.axis == Axis.W and w_move.position == pytest.approx(21.0)
    assert w_move.velocity == pytest.approx(123.0)
    assert w_move.acceleration == pytest.approx(456.0)


@pytest.mark.asyncio
async def test_dispense_only_applies_z_out_kinematics_below_plate_top():
    profile = BravoProfile.default()
    profile.safety.z_safe_position = 0.0
    profile.teachpoints = Teachpoints()
    profile.teachpoints.set_teachpoint(1, Axis.X, 10.0)
    profile.teachpoints.set_teachpoint(1, Axis.Y, 20.0)
    profile.teachpoints.set_teachpoint(1, Axis.Z, 30.0)
    profile.connection.machine_id = "BRAVO_A"
    profile.head.head_type = HeadType.HT_384_D_70
    profile.head.teach_tip_capacity = 30.0

    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    controller._axes[Axis.W].position = 100.0
    bravo._controller = controller

    liquid_class = {
        "name": "Water",
        "machine_id": "BRAVO_A",
        "head_type": "HT_384_D_70",
        "tip_capacity_ul": 30.0,
        "aspirate": {},
        "dispense": {
            "w_velocity_ul_s": 111.0,
            "w_acceleration_ul_s2": 222.0,
            "post_delay_ms": 0,
            "z_in_velocity_mm_s": 11.0,
            "z_in_acceleration_mm_s2": 22.0,
            "z_out_velocity_mm_s": 55.0,
            "z_out_acceleration_mm_s2": 66.0,
        },
        "equation": {
            "control_points": [
                {"desired_ul": 0.0, "commanded_ul": 0.0},
                {"desired_ul": 10.0, "commanded_ul": 12.0},
            ]
        },
    }

    # target_z = teachpoint(30) - plate_height(14.5) + well_depth(10) - distance_from_bottom(2) = 23.5
    plate_def = LabwareDefinition(
        id="test-plate", name="Test Plate", kind="plate", base_class="plate",
        height_mm=14.5, wells=384, rows=16, cols=24,
        spacing_x_mm=4.5, spacing_y_mm=4.5, offset_x_mm=2.25, offset_y_mm=2.25,
        well_depth_mm=10.0,
    )
    bravo.deck.set_single(1, Labware.from_definition(plate_def))
    bravo._resolve_liquid_class = lambda name: liquid_class if name else None
    _attach_test_tips(bravo)

    await bravo.dispense(1, 10.0, distance_from_bottom=2.0, liquid_class="Water")

    z_moves = [
        call[0]
        for call in controller.move_calls
        if len(call) == 1 and call[0].axis == Axis.Z
    ]
    # Enter fast leg: safe_z(0) -> corrected plate-top head position(15.5)
    assert any(
        move.position == pytest.approx(15.5)
        and move.velocity == pytest.approx(0.0)
        and move.acceleration == pytest.approx(0.0)
        for move in z_moves
    )
    # Enter slow leg: corrected top(15.5) -> target(23.5) with z_in kinematics
    assert any(
        move.position == pytest.approx(23.5)
        and move.velocity == pytest.approx(11.0)
        and move.acceleration == pytest.approx(22.0)
        for move in z_moves
    )
    # Exit slow leg: target(23.5) -> corrected top(15.5) with z_out kinematics
    assert any(
        move.position == pytest.approx(15.5)
        and move.velocity == pytest.approx(55.0)
        and move.acceleration == pytest.approx(66.0)
        for move in z_moves
    )
    # Exit fast leg: corrected top(15.5) -> safe_z(0)
    assert z_moves[-1].position == pytest.approx(0.0)
    assert z_moves[-1].velocity == pytest.approx(0.0)
    assert z_moves[-1].acceleration == pytest.approx(0.0)

    w_move = next(
        call[0] for call in controller.move_calls
        if len(call) == 1 and call[0].axis == Axis.W
    )
    assert w_move.position == pytest.approx(88.0)
    assert w_move.velocity == pytest.approx(111.0)
    assert w_move.acceleration == pytest.approx(222.0)


@pytest.mark.asyncio
async def test_aspirate_applies_shorter_attached_tip_delta_to_liquid_z():
    profile = BravoProfile.default()
    profile.safety.z_safe_position = 0.0
    profile.teachpoints = Teachpoints()
    profile.teachpoints.set_teachpoint(1, Axis.X, 10.0)
    profile.teachpoints.set_teachpoint(1, Axis.Y, 20.0)
    profile.teachpoints.set_teachpoint(1, Axis.Z, 30.0)
    profile.head.head_type = HeadType.HT_384_D_70
    profile.head.teach_tip_capacity = 30.0
    profile.head.teach_tip_length_mm = 26.1

    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    bravo._controller = controller
    bravo.deck.set_single(
        1,
        Labware.from_definition(
            LabwareDefinition(
                id="test-plate",
                name="Test Plate",
                kind="plate",
                base_class="plate",
                height_mm=14.5,
                wells=384,
                rows=16,
                cols=24,
                spacing_x_mm=4.5,
                spacing_y_mm=4.5,
                offset_x_mm=2.25,
                offset_y_mm=2.25,
                well_depth_mm=10.0,
            )
        ),
    )
    _attach_test_tips(bravo, tip_id="st_10ul", tip_length_mm=19.9)

    await bravo.aspirate(1, 10.0, distance_from_bottom=2.0)

    z_moves = [
        call[0]
        for call in controller.move_calls
        if len(call) == 1 and call[0].axis == Axis.Z
    ]
    assert any(move.position == pytest.approx(21.7) for move in z_moves)
    assert any(move.position == pytest.approx(29.7) for move in z_moves)
    assert z_moves[-1].position == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_dispense_applies_longer_attached_tip_delta_to_liquid_z():
    profile = BravoProfile.default()
    profile.safety.z_safe_position = 0.0
    profile.teachpoints = Teachpoints()
    profile.teachpoints.set_teachpoint(1, Axis.X, 10.0)
    profile.teachpoints.set_teachpoint(1, Axis.Y, 20.0)
    profile.teachpoints.set_teachpoint(1, Axis.Z, 30.0)
    profile.head.head_type = HeadType.HT_384_D_70
    profile.head.teach_tip_capacity = 30.0
    profile.head.teach_tip_length_mm = 26.1

    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    controller._axes[Axis.W].position = 40.0
    bravo._controller = controller
    bravo.deck.set_single(
        1,
        Labware.from_definition(
            LabwareDefinition(
                id="test-plate",
                name="Test Plate",
                kind="plate",
                base_class="plate",
                height_mm=14.5,
                wells=384,
                rows=16,
                cols=24,
                spacing_x_mm=4.5,
                spacing_y_mm=4.5,
                offset_x_mm=2.25,
                offset_y_mm=2.25,
                well_depth_mm=10.0,
            )
        ),
    )
    _attach_test_tips(bravo, tip_id="st_30ul", tip_length_mm=31.0)

    await bravo.dispense(1, 10.0, distance_from_bottom=2.0)

    z_moves = [
        call[0]
        for call in controller.move_calls
        if len(call) == 1 and call[0].axis == Axis.Z
    ]
    assert any(move.position == pytest.approx(10.6) for move in z_moves)
    assert any(move.position == pytest.approx(18.6) for move in z_moves)
    assert z_moves[-1].position == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_disposable_head_liquid_tasks_require_tips_on_head():
    profile = BravoProfile.default()
    profile.teachpoints = Teachpoints()
    profile.teachpoints.set_teachpoint(1, Axis.X, 10.0)
    profile.teachpoints.set_teachpoint(1, Axis.Y, 20.0)
    profile.teachpoints.set_teachpoint(1, Axis.Z, 30.0)
    profile.head.head_type = HeadType.HT_384_D_70
    profile.head.teach_tip_capacity = 30.0
    profile.head.teach_tip_length_mm = 26.1

    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    bravo._controller = controller

    task = asyncio.create_task(bravo.aspirate(1, 10.0, distance_from_bottom=2.0))
    failed_task = await _wait_for_engine_failure(bravo)
    assert "requires tips on the head" in failed_task.error.message
    bravo.engine.abort()
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_disposable_head_liquid_tasks_require_known_attached_tip_length():
    profile = BravoProfile.default()
    profile.teachpoints = Teachpoints()
    profile.teachpoints.set_teachpoint(1, Axis.X, 10.0)
    profile.teachpoints.set_teachpoint(1, Axis.Y, 20.0)
    profile.teachpoints.set_teachpoint(1, Axis.Z, 30.0)
    profile.head.head_type = HeadType.HT_384_D_70
    profile.head.teach_tip_capacity = 30.0
    profile.head.teach_tip_length_mm = 26.1

    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    bravo._controller = controller
    bravo._tips_on_head = True
    bravo._tip_definition_id = "st_30ul"
    bravo._tips_on_head_mode = bravo._head_mode

    task = asyncio.create_task(bravo.aspirate(1, 10.0, distance_from_bottom=2.0))
    failed_task = await _wait_for_engine_failure(bravo)
    assert "requires a known attached tip length" in failed_task.error.message
    bravo.engine.abort()
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_fixed_head_liquid_tasks_use_zero_tip_delta_without_attached_tip_state():
    profile = BravoProfile.default()
    profile.safety.z_safe_position = 0.0
    profile.teachpoints = Teachpoints()
    profile.teachpoints.set_teachpoint(1, Axis.X, 10.0)
    profile.teachpoints.set_teachpoint(1, Axis.Y, 20.0)
    profile.teachpoints.set_teachpoint(1, Axis.Z, 30.0)
    profile.head.head_type = HeadType.HT_96_F_50

    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    controller._axes[Axis.W].position = 20.0
    bravo._controller = controller
    bravo.deck.set_single(
        1,
        Labware.from_definition(
            LabwareDefinition(
                id="test-plate",
                name="Test Plate",
                kind="plate",
                base_class="plate",
                height_mm=14.5,
                wells=96,
                rows=8,
                cols=12,
                spacing_x_mm=9.0,
                spacing_y_mm=9.0,
                well_depth_mm=10.0,
            )
        ),
    )

    await bravo.dispense(1, 10.0, distance_from_bottom=2.0)

    z_moves = [
        call[0]
        for call in controller.move_calls
        if len(call) == 1 and call[0].axis == Axis.Z
    ]
    assert any(move.position == pytest.approx(15.5) for move in z_moves)
    assert any(move.position == pytest.approx(23.5) for move in z_moves)
    assert z_moves[-1].position == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_dispense_uses_piecewise_interpolated_control_points():
    profile = BravoProfile.default()
    profile.teachpoints = Teachpoints()
    profile.teachpoints.set_teachpoint(1, Axis.X, 10.0)
    profile.teachpoints.set_teachpoint(1, Axis.Y, 20.0)
    profile.teachpoints.set_teachpoint(1, Axis.Z, 30.0)
    profile.connection.machine_id = "BRAVO_A"
    profile.head.head_type = HeadType.HT_384_D_70
    profile.head.teach_tip_capacity = 30.0

    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    bravo._controller = controller
    controller._axes[Axis.W].position = 100.0
    liquid_class = {
        "name": "Water",
        "machine_id": "BRAVO_A",
        "head_type": "HT_384_D_70",
        "tip_capacity_ul": 30.0,
        "aspirate": {},
        "dispense": {},
        "equation": {
            "control_points": [
                {"desired_ul": 0.0, "commanded_ul": 0.0},
                {"desired_ul": 10.0, "commanded_ul": 12.0},
                {"desired_ul": 20.0, "commanded_ul": 25.0},
            ]
        },
    }
    bravo._resolve_liquid_class = lambda name: liquid_class if name else None
    _attach_test_tips(bravo)

    await bravo.dispense(1, 15.0, liquid_class="Water")

    w_move = next(
        call[0] for call in controller.move_calls
        if len(call) == 1 and call[0].axis == Axis.W
    )
    assert w_move.axis == Axis.W
    assert w_move.position == pytest.approx(81.5)


@pytest.mark.asyncio
async def test_dispense_empty_tips_moves_w_to_zero():
    profile = BravoProfile.default()
    profile.teachpoints = Teachpoints()
    profile.teachpoints.set_teachpoint(1, Axis.X, 10.0)
    profile.teachpoints.set_teachpoint(1, Axis.Y, 20.0)
    profile.teachpoints.set_teachpoint(1, Axis.Z, 30.0)
    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    controller._axes[Axis.W].position = 42.0
    bravo._controller = controller
    _attach_test_tips(bravo)

    await bravo.dispense(1, 10.0, empty_tips=True)

    w_move = next(
        call[0] for call in controller.move_calls
        if len(call) == 1 and call[0].axis == Axis.W
    )
    assert w_move.axis == Axis.W
    assert w_move.position == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_mix_uses_separate_aspirate_and_dispense_depths():
    profile = BravoProfile.default()
    profile.teachpoints = Teachpoints()
    profile.teachpoints.set_teachpoint(1, Axis.X, 10.0)
    profile.teachpoints.set_teachpoint(1, Axis.Y, 20.0)
    profile.teachpoints.set_teachpoint(1, Axis.Z, 30.0)
    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    controller._axes[Axis.W].position = 50.0
    bravo._controller = controller
    _attach_test_tips(bravo)

    await bravo.mix(
        1,
        10.0,
        mix_cycles=2,
        aspirate_distance=3.0,
        dispense_at_different_distance=True,
        dispense_distance=1.0,
    )

    z_targets = [
        move.position
        for call in controller.move_calls
        for move in call
        if move.axis == Axis.Z
    ]
    assert 27.0 in z_targets
    assert 29.0 in z_targets


@pytest.mark.asyncio
async def test_dispense_swirl_generates_segmented_xy_moves():
    profile = BravoProfile.default()
    profile.teachpoints = Teachpoints()
    profile.teachpoints.set_teachpoint(1, Axis.X, 10.0)
    profile.teachpoints.set_teachpoint(1, Axis.Y, 20.0)
    profile.teachpoints.set_teachpoint(1, Axis.Z, 30.0)
    profile.connection.machine_id = "SIM_BRAVO"
    profile.head.head_type = HeadType.HT_384_D_70
    profile.head.teach_tip_capacity = 30.0

    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    bravo._controller = controller
    bravo.deck.set_single(
        1,
        Labware.from_definition(
            LabwareDefinition(
                id="plate-384",
                name="384 Greiner 781091 PS uclear",
                kind="sbs_plate",
                height_mm=14.4,
                stack_height_mm=8.6,
                gripper_offset_mm=2.5,
                rows=16,
                cols=24,
                well_diameter_mm=3.2,
            )
        ),
    )
    _attach_test_tips(bravo)

    technique = {
        "name": "Swirl",
        "radius_mm": 0.8,
        "segments": 6,
        "clockwise": True,
        "apply_on_aspirate": False,
        "apply_on_dispense": True,
        "z_phase": "enter",
    }
    bravo._resolve_pipette_technique = lambda name: technique if name else None

    await bravo.dispense(1, 10.0, distance_from_bottom=2.0, pipette_technique="Swirl")

    xy_multi_moves = [
        call for call in controller.move_calls
        if any(move.axis == Axis.X for move in call) and any(move.axis == Axis.Y for move in call)
    ]
    assert len(xy_multi_moves) >= 7


@pytest.mark.asyncio
async def test_aspirate_exit_swirl_completes_before_safe_z_retract():
    profile = BravoProfile.default()
    profile.teachpoints = Teachpoints()
    profile.teachpoints.set_teachpoint(1, Axis.X, 10.0)
    profile.teachpoints.set_teachpoint(1, Axis.Y, 20.0)
    profile.teachpoints.set_teachpoint(1, Axis.Z, 30.0)
    profile.connection.machine_id = "SIM_BRAVO"
    profile.head.head_type = HeadType.HT_384_D_70
    profile.head.teach_tip_capacity = 30.0

    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    bravo._controller = controller
    bravo.deck.set_single(
        1,
        Labware.from_definition(
            LabwareDefinition(
                id="plate-384",
                name="384 Greiner 781091 PS uclear",
                kind="sbs_plate",
                height_mm=14.4,
                stack_height_mm=8.6,
                gripper_offset_mm=2.5,
                rows=16,
                cols=24,
                well_diameter_mm=3.2,
            )
        ),
    )

    technique = {
        "name": "Clockwise Asp-Dsp",
        "radius_mm": 0.5,
        "segments": 12,
        "clockwise": True,
        "apply_on_aspirate": True,
        "apply_on_dispense": True,
        "z_phase": "both",
    }
    bravo._resolve_pipette_technique = lambda name: technique if name else None
    _attach_test_tips(bravo)

    await bravo.aspirate(1, 10.0, distance_from_bottom=2.0, pipette_technique="Clockwise Asp-Dsp")

    safe_z = profile.safety.z_safe_position
    safe_z_index = max(
        idx for idx, call in enumerate(controller.move_calls)
        if len(call) == 1 and call[0].axis == Axis.Z and call[0].position == pytest.approx(safe_z)
    )
    for later_call in controller.move_calls[safe_z_index + 1:]:
        assert not (
            any(move.axis == Axis.X for move in later_call)
            and any(move.axis == Axis.Y for move in later_call)
        )


@pytest.mark.asyncio
async def test_scan_stack_height_rounds_to_nearest_plate_count_and_rebuilds_runtime_stack():
    profile = BravoProfile.default()
    profile.teachpoints = Teachpoints()
    profile.teachpoints.set_teachpoint(5, Axis.X, 100.0)
    profile.teachpoints.set_teachpoint(5, Axis.Y, 200.0)
    profile.teachpoints.set_teachpoint(5, Axis.Z, 300.0)
    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    # Top-of-stack height for a 4-high stack: plate_height 14.4 + 3*8.6 support.
    controller.set_simulated_scan_height_mm(40.2)
    bravo._controller = controller

    definition = LabwareDefinition(
        id="plate-384",
        name="384 Greiner 781091 PS uclear",
        kind="sbs_plate",
        base_class="microplate",
        height_mm=14.4,
        stack_height_mm=8.6,
        gripper_offset_mm=2.5,
    )
    bravo.deck.set_single(5, Labware.from_definition(definition))

    result = await bravo.scan_stack_height(location=5)

    assert result["status"] == "completed"
    assert result["configured_labware"] == "384 Greiner 781091 PS uclear"
    assert result["raw_measured_height_mm"] == pytest.approx(40.2)
    assert result["height_offset_mm"] == pytest.approx(0.0)
    assert result["measured_height_mm"] == pytest.approx(40.2)
    assert result["stack_height_mm"] == pytest.approx(8.6)
    assert result["inferred_count"] == 4
    assert result["theoretical_height_mm"] == pytest.approx(25.8)
    assert result["estimated_total_height_mm"] == pytest.approx(40.2)
    assert result["rounded_stack_height_mm"] == 26
    stack = bravo.deck.get_stack(5)
    assert len(stack) == 4
    assert all(item.name == "384 Greiner 781091 PS uclear" for item in stack.items)


@pytest.mark.asyncio
async def test_scan_stack_height_returns_manual_override_flow_when_no_plate_is_detected():
    profile = BravoProfile.default()
    profile.teachpoints = Teachpoints()
    profile.teachpoints.set_teachpoint(5, Axis.X, 100.0)
    profile.teachpoints.set_teachpoint(5, Axis.Y, 200.0)
    profile.teachpoints.set_teachpoint(5, Axis.Z, 300.0)
    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    controller.set_simulated_scan_height_mm(None)
    bravo._controller = controller

    definition = LabwareDefinition(
        id="plate-384",
        name="384 Greiner 781091 PS uclear",
        kind="sbs_plate",
        base_class="microplate",
        height_mm=14.4,
        stack_height_mm=8.6,
        gripper_offset_mm=2.5,
    )
    bravo.deck.set_single(5, Labware.from_definition(definition))

    scan_result = await bravo.scan_stack_height(location=5)

    assert scan_result["status"] == "manual_count_required"
    assert scan_result["used_manual_override"] is False
    assert "Enter the number of stacked plates" in scan_result["message"]
    assert len(bravo.deck.get_stack(5)) == 1

    manual_result = await bravo.scan_stack_height(location=5, manual_count=3)

    assert manual_result["status"] == "completed"
    assert manual_result["used_manual_override"] is True
    assert manual_result["manual_count"] == 3
    assert manual_result["inferred_count"] == 3
    assert manual_result["theoretical_height_mm"] == pytest.approx(17.2)
    assert manual_result["estimated_total_height_mm"] == pytest.approx(31.6)
    assert manual_result["rounded_stack_height_mm"] == 17
    assert len(bravo.deck.get_stack(5)) == 3


@pytest.mark.asyncio
async def test_scan_stack_height_fallback_counts_visible_top_plate_from_stacking_height():
    profile = BravoProfile.default()
    profile.safety.approach_height = 10.0
    profile.teachpoints = Teachpoints()
    profile.teachpoints.set_teachpoint(7, Axis.X, 100.0)
    profile.teachpoints.set_teachpoint(7, Axis.Y, 200.0)
    # Reachable: Z travel tops out at 150, so a 300 teachpoint saturates both
    # axes and the geometry stops meaning anything.
    profile.teachpoints.set_teachpoint(7, Axis.Z, 60.0)
    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    # Four plates stand 14.4 + 3*13.6 = 55.2 mm above the pad. The harness sets
    # the trigger relative to the gripper's parking datum, which sits 7.0 mm
    # (the gripper offset) above the pad, so it is driven with 48.2 mm — but the
    # task measures against the PAD, so the reading it derives is the full
    # 55.2 mm. The gripper offset cancels out of the measurement entirely.
    controller.set_simulated_scan_raw_height_mm(48.2, approach_height_mm=profile.safety.approach_height)
    bravo._controller = controller

    definition = LabwareDefinition(
        id="plate-384-stack",
        name="384 Labcyte PP0200 PP sq flt",
        kind="sbs_plate",
        base_class="microplate",
        height_mm=14.4,
        stack_height_mm=13.6,
        gripper_offset_mm=7.0,
    )
    bravo.deck.set_single(7, Labware.from_definition(definition))

    result = await bravo.scan_stack_height(location=7)

    assert result["status"] == "completed"
    assert result["configured_labware"] == "384 Labcyte PP0200 PP sq flt"
    # Pad-relative, so the raw reading already equals the true stack height.
    assert result["raw_measured_height_mm"] == pytest.approx(55.2)
    # Only the fixed sensor standoff is subtracted; no per-labware term.
    assert result["height_offset_mm"] == pytest.approx(0.0)
    assert result["sensor_standoff_mm"] == pytest.approx(0.0)
    assert result["measured_height_mm"] == pytest.approx(55.2)
    assert result["stack_height_mm"] == pytest.approx(13.6)
    assert result["plate_height_mm"] == pytest.approx(14.4)
    assert result["inferred_count"] == 4
    assert result["theoretical_height_mm"] == pytest.approx(40.8)
    assert result["estimated_total_height_mm"] == pytest.approx(55.2)
    assert result["rounded_stack_height_mm"] == 41
    stack = bravo.deck.get_stack(7)
    assert len(stack) == 4
    assert all(item.name == "384 Labcyte PP0200 PP sq flt" for item in stack.items)


@pytest.mark.asyncio
async def test_scan_stack_height_records_scan_debug_fields_and_transient():
    profile = BravoProfile.default()
    profile.safety.plate_sensor_transient_ms = 175
    profile.teachpoints = Teachpoints()
    profile.teachpoints.set_teachpoint(5, Axis.X, 100.0)
    profile.teachpoints.set_teachpoint(5, Axis.Y, 200.0)
    profile.teachpoints.set_teachpoint(5, Axis.Z, 300.0)
    bravo = Bravo(profile=profile)
    controller = ScanStackDebugController()
    controller.open_tcp("simulation")
    # Top-of-stack for a 3-high stack: plate_height 14.4 + 2*8.6 support = 31.6.
    controller.set_simulated_scan_height_mm(31.6)
    bravo._controller = controller

    definition = LabwareDefinition(
        id="plate-384",
        name="384 Greiner 781091 PS uclear",
        kind="sbs_plate",
        base_class="microplate",
        height_mm=14.4,
        stack_height_mm=8.6,
        gripper_offset_mm=2.5,
    )
    bravo.deck.set_single(5, Labware.from_definition(definition))

    result = await bravo.scan_stack_height(location=5)

    assert controller.scan_calls[0]["speed"] == "SLOW"
    assert controller.scan_calls[0]["transient_ms"] == pytest.approx(175.0)
    assert result["status"] == "completed"
    assert result["inferred_count"] == 3
    assert result["scan_mode"] == "simulated_fast_path"
    assert result["scan_stop_strategy"] == "not_applicable"
    assert result["scan_elapsed_ms"] == pytest.approx(42.0)
    assert result["scan_poll_count"] == pytest.approx(7.0)
    assert result["scan_sensor_reads"] == pytest.approx(8.0)
    assert result["scan_sensor_read_failures"] == pytest.approx(0.0)
    assert result["scan_transient_ms"] == 175


def test_infer_stack_count_subtracts_top_plate_height():
    # measured is the top-of-stack height (includes the top plate's own height).
    # Tall plate: height == stacking thickness (e.g. 96 Deepwell, 32.4mm).
    assert _infer_stack_count_from_scan_height(28.3, 32.4, 32.4) == 1   # the reported bug: 1, not 2
    assert _infer_stack_count_from_scan_height(32.4, 32.4, 32.4) == 1
    assert _infer_stack_count_from_scan_height(64.8, 32.4, 32.4) == 2
    assert _infer_stack_count_from_scan_height(97.2, 32.4, 32.4) == 3
    # Short, nesting plate: full height 14.4, stacking pitch 8.6 (384 Greiner).
    assert _infer_stack_count_from_scan_height(14.4, 8.6, 14.4) == 1
    assert _infer_stack_count_from_scan_height(40.2, 8.6, 14.4) == 4
    # A single plate of any height clamps to 1 even if measured undershoots height.
    assert _infer_stack_count_from_scan_height(10.0, 8.6, 14.4) == 1
    # Backward-compatible default: with no plate height, behaves like the old
    # "measured == support height" contract.
    assert _infer_stack_count_from_scan_height(25.8, 8.6) == 4
    # Degenerate stacking thickness never divides by zero.
    assert _infer_stack_count_from_scan_height(50.0, 0.0, 14.4) == 1


@pytest.mark.asyncio
async def test_scan_single_tall_plate_infers_one_plate():
    # Regression: a single 96 Deepwell (32.4mm tall, stack_height_mm 0 -> falls
    # back to its full height as the stacking pitch) must infer 1, not 2.
    profile = BravoProfile.default()
    profile.teachpoints = Teachpoints()
    profile.teachpoints.set_teachpoint(5, Axis.X, 100.0)
    profile.teachpoints.set_teachpoint(5, Axis.Y, 200.0)
    profile.teachpoints.set_teachpoint(5, Axis.Z, 300.0)
    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    # Real-world reading: a lone 32.4mm plate scans ~28.3mm of top-of-stack.
    controller.set_simulated_scan_height_mm(28.3)
    bravo._controller = controller

    definition = LabwareDefinition(
        id="deepwell-ubottom",
        name="96 Deepwell U-Bottom",
        kind="sbs_plate",
        base_class="microplate",
        height_mm=32.4,
        stack_height_mm=0.0,
        gripper_offset_mm=5.0,
    )
    bravo.deck.set_single(5, Labware.from_definition(definition))

    result = await bravo.scan_stack_height(location=5)

    assert result["status"] == "completed"
    assert result["measured_height_mm"] == pytest.approx(28.3)
    assert result["stack_height_mm"] == pytest.approx(32.4)
    assert result["plate_height_mm"] == pytest.approx(32.4)
    assert result["inferred_count"] == 1
    assert result["theoretical_height_mm"] == pytest.approx(0.0)
    assert len(bravo.deck.get_stack(5)) == 1


@pytest.mark.asyncio
async def test_scan_single_short_plate_infers_one_plate():
    # The same generality property for a short nesting plate.
    profile = BravoProfile.default()
    profile.teachpoints = Teachpoints()
    profile.teachpoints.set_teachpoint(5, Axis.X, 100.0)
    profile.teachpoints.set_teachpoint(5, Axis.Y, 200.0)
    profile.teachpoints.set_teachpoint(5, Axis.Z, 300.0)
    bravo = Bravo(profile=profile)
    controller = RecordingSimulationController()
    controller.open_tcp("simulation")
    controller.set_simulated_scan_height_mm(14.4)
    bravo._controller = controller

    definition = LabwareDefinition(
        id="plate-384",
        name="384 Greiner 781091 PS uclear",
        kind="sbs_plate",
        base_class="microplate",
        height_mm=14.4,
        stack_height_mm=8.6,
        gripper_offset_mm=2.5,
    )
    bravo.deck.set_single(5, Labware.from_definition(definition))

    result = await bravo.scan_stack_height(location=5)

    assert result["status"] == "completed"
    assert result["inferred_count"] == 1
    assert len(bravo.deck.get_stack(5)) == 1
