import pytest
import yaml

from fastapi import HTTPException
from pathlib import Path
from types import SimpleNamespace

from pybravo.bravo import Bravo
from pybravo.controllers.base import JogParams
from pybravo.darwin import DarwinController
from pybravo.controllers.simulation import SimulationController
from pybravo.deck import labware as labware_module
from pybravo import labware_editor
from pybravo.deck.labware import Labware, LabwareDefinition, _apply_mirrored_motion_fields, build_labware_catalog, synthesize_lid_labware
from pybravo.head_mode import TipSelection, normalize_head_mode, plate_selection
from pybravo.protocol.commands import CommandID
from pybravo.protocol.errors import BravoError, ErrorType
from pybravo.types import Axis, HeadType
from pybravo.web import server


class _FakeMongoCollection:
    def __init__(self, docs=None):
        self.docs = [dict(doc) for doc in (docs or [])]

    def find(self, query=None, projection=None):
        if not query:
            return [dict(doc) for doc in self.docs]
        matches = []
        for doc in self.docs:
            include = True
            for key, expected in query.items():
                if isinstance(expected, dict) and "$nin" in expected:
                    if doc.get(key) in set(expected["$nin"]):
                        include = False
                        break
                elif doc.get(key) != expected:
                    include = False
                    break
            if include:
                matches.append(dict(doc))
        return matches

    def insert_one(self, doc):
        self.docs.append(dict(doc))
        return None

    def replace_one(self, flt, doc, upsert=False):
        key, value = next(iter(flt.items()))
        for idx, existing in enumerate(self.docs):
            if existing.get(key) == value:
                self.docs[idx] = dict(doc)
                return
        if upsert:
            self.docs.append(dict(doc))

    def delete_many(self, flt):
        if not flt:
            self.docs = []
            return
        key, condition = next(iter(flt.items()))
        if isinstance(condition, dict) and "$nin" in condition:
            blocked = set(condition["$nin"])
            self.docs = [doc for doc in self.docs if doc.get(key) in blocked]
            return
        self.docs = [doc for doc in self.docs if doc.get(key) != condition]


class _FakeMongoDatabase:
    def __init__(self, collections):
        self.collections = collections

    def __getitem__(self, name):
        return self.collections[name]


class _FakeMongoClient:
    def __init__(self, databases):
        self.databases = databases

    def __getitem__(self, name):
        return self.databases[name]

    def close(self):
        return None


@pytest.mark.asyncio
async def test_change_head_keeps_enum_and_updates_simulation_controller():
    bravo = Bravo(mode="simulation")
    bravo.connect()
    previous_bravo = server._bravo

    try:
        server._bravo = bravo

        response = await server.change_head(server.ChangeHeadRequest(head_type="HT_384_D_70"))

        assert response["head_type"] == "HT_384_D_70"
        assert bravo.profile.head.head_type is HeadType.HT_384_D_70
        assert isinstance(bravo.controller, SimulationController)
        assert bravo.controller.read_smart_head_type() == int(HeadType.HT_384_D_70)
    finally:
        server._bravo = previous_bravo
        bravo.disconnect()


@pytest.mark.asyncio
async def test_change_head_rejects_unknown_head_type():
    bravo = Bravo(mode="simulation")
    bravo.connect()
    previous_bravo = server._bravo

    try:
        server._bravo = bravo

        with pytest.raises(HTTPException) as exc_info:
            await server.change_head(server.ChangeHeadRequest(head_type="NOT_A_HEAD"))

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "Unknown head type: NOT_A_HEAD"
    finally:
        server._bravo = previous_bravo
        bravo.disconnect()


@pytest.mark.asyncio
async def test_simulate_designer_workflow_passes_runtime_snapshot_into_executor(monkeypatch):
    bravo = Bravo(mode="simulation")
    bravo.set_head_mode("column", "back_left", column_count=1)
    bravo._tip_selection = TipSelection(location=2, row=0, col=23, row_count=16, column_count=1, mirror_corner="back_left")
    bravo._plate_selection = {5: plate_selection(5, 1, 5)}
    bravo._tips_on_head = True
    bravo._tips_on_head_mode = normalize_head_mode(HeadType.HT_384_D_70, "column", "back_left", column_count=1)
    bravo._tips_on_head_selection = bravo._tip_selection
    bravo._tip_labware_name = "384 V11 ST10 Tip Box 10734.102"
    bravo._tip_definition_id = "st_10ul"
    bravo._attached_tip_length_mm = 19.9
    previous_bravo = server._bravo

    class _FakeStorage:
        def get_workflow(self, workflow_id):
            assert workflow_id == "wf-1"
            return {"graph": {"nodes": []}, "deck": {}}

    captured: dict[str, object] = {}
    scheduled = []

    class _FakeExecutor:
        def __init__(self, bravo_obj, graph_data, deck_config=None, on_event=None, runtime_state=None, preview_animation=True, library_src=""):
            captured["runtime_state"] = runtime_state

        async def execute(self):
            return None

        def abort(self):
            return None

    def fake_ensure_future(coro):
        scheduled.append(coro)
        return None

    try:
        server._bravo = bravo
        monkeypatch.setattr(server, "_get_workflow_storage", lambda: _FakeStorage())
        monkeypatch.setattr("pybravo.workflow.executor.WorkflowExecutor", _FakeExecutor)
        monkeypatch.setattr(server.asyncio, "ensure_future", fake_ensure_future)

        response = await server.simulate_designer_workflow("wf-1")

        assert response == {"status": "started", "workflow_id": "wf-1", "mode": "simulate"}
        runtime_state = captured["runtime_state"]
        assert runtime_state["head_mode"]["subset_type"] == "column"
        assert runtime_state["tip_selection"]["location"] == 2
        assert runtime_state["tip_selection"]["col"] == 23
        assert runtime_state["plate_selection"]["5"] == {"location": 5, "row": 1, "col": 5}
        assert runtime_state["tips_on_head"] is True
        assert runtime_state["tips_on_head_selection"]["location"] == 2
        assert runtime_state["tip_labware"] == "384 V11 ST10 Tip Box 10734.102"
        assert runtime_state["tip_definition_id"] == "st_10ul"
        assert runtime_state["attached_tip_length_mm"] == pytest.approx(19.9)
        assert scheduled, "simulation coroutine was not scheduled"
    finally:
        server._bravo = previous_bravo
        for coro in scheduled:
            coro.close()


@pytest.mark.asyncio
async def test_bravo_error_handler_returns_json_response():
    exc = BravoError(
        ErrorType.COULD_NOT_HOME,
        custom_text="Homing timed out for: ['W']",
    )

    response = await server.bravo_error_handler(None, exc)

    assert response.status_code == 400
    assert response.body == (
        b'{"error":"Homing timed out for: [\'W\']","error_type":"COULD_NOT_HOME"}'
    )


@pytest.mark.asyncio
async def test_task_error_action_endpoints_report_not_accepted_when_no_prompt_is_waiting():
    bravo = Bravo(mode="simulation")
    previous_bravo = server._bravo

    try:
        server._bravo = bravo

        retry_response = await server.retry()
        ignore_response = await server.ignore_error()
        abort_response = await server.abort()

        assert retry_response == {"status": "retried", "accepted": False}
        assert ignore_response == {"status": "ignored", "accepted": False}
        assert abort_response == {"status": "aborted", "accepted": False}
    finally:
        server._bravo = previous_bravo


def test_darwin_controller_builds_positions_from_bridge(monkeypatch):
    profile = Bravo(mode="simulation").profile
    profile.connection.controller_type = "darwin"
    profile.head.head_type = HeadType.HT_384_D_70
    controller = DarwinController(profile)
    controller._address = "192.168.0.8"
    controller._connected = True

    axis_positions = {Axis.X: 1.0, Axis.Y: 2.0, Axis.Z: 3.0, Axis.W: 4.0, Axis.G: 5.0, Axis.Zg: 6.0}
    monkeypatch.setattr(controller, "get_position", lambda a: axis_positions[a])

    positions = controller.get_all_positions()

    assert positions["X"] == 1.0
    assert positions["Y"] == 2.0
    assert positions["Z"] == 3.0
    assert positions["W"] == pytest.approx(4.0)


def test_darwin_controller_parses_controller_node_firmware_versions(monkeypatch):
    controller = DarwinController(address="192.168.0.8")

    def fake_get_value(addr, subcmd, timeout_ms=5000):
        return (1 << 24) | (2 << 16) | 3

    monkeypatch.setattr(controller._engine, "get_value", fake_get_value)

    firmware = controller.get_firmware_version()

    assert firmware.master == "1.2.3"
    assert "YX=" in firmware.sub1
    assert "GZg=" in firmware.sub2


def test_darwin_controller_jog_dispatches_to_native_sequence(monkeypatch):
    from pybravo.darwin import sequences

    profile = Bravo(mode="simulation").profile
    profile.connection.controller_type = "darwin"
    profile.head.head_type = HeadType.HT_384_D_70
    controller = DarwinController(profile)

    z_state = controller._axes[Axis.Z]
    z_state.params = SimpleNamespace()
    z_state.peak_current_max = 2.0
    z_state.limits = SimpleNamespace(velocity=100.0, acceleration=1000.0)

    captured: dict[str, object] = {}

    def fake_seq_jog(engine, addr, params, jog_params, *, read_position, **kw):
        captured["jog_params"] = jog_params
        return z_state.calibration.to_normalized(42.15)

    monkeypatch.setattr(sequences, "jog", fake_seq_jog)
    monkeypatch.setattr(controller, "_ensure_axis_enabled", lambda axis: None)

    final_position = controller.jog(
        JogParams(
            axis=Axis.Z,
            velocity=60.0,
            acceleration=500.0,
            max_position=42.15,
            tolerance=0.2,
            peak_current=0.8,
        )
    )

    assert final_position == pytest.approx(42.15, abs=0.5)
    jp = captured["jog_params"]
    assert jp.peak_current_amps == pytest.approx(0.8)
    assert jp.velocity_mm == pytest.approx(60.0)
    assert jp.acceleration_mm == pytest.approx(500.0)


def test_darwin_controller_scan_stack_with_gripper_accepts_expected_params():
    profile = Bravo(mode="simulation").profile
    profile.connection.controller_type = "darwin"
    profile.head.head_type = HeadType.HT_384_D_70
    controller = DarwinController(profile)

    import inspect
    sig = inspect.signature(controller.scan_stack_with_gripper)
    assert "start_zg" in sig.parameters
    assert "end_zg" in sig.parameters
    assert "speed" in sig.parameters
    assert "transient_ms" in sig.parameters


def test_darwin_controller_clear_motor_power_fault_is_noop():
    profile = Bravo(mode="simulation").profile
    profile.connection.controller_type = "darwin"
    profile.head.head_type = HeadType.HT_384_D_70
    controller = DarwinController(profile)

    assert controller.send_command(CommandID.CLEAR_MOTOR_POWER_FAULT) == b""


def test_darwin_controller_reset_faults_dispatches_per_axis(monkeypatch):
    from pybravo.darwin import axis as axis_module

    profile = Bravo(mode="simulation").profile
    profile.connection.controller_type = "darwin"
    profile.head.head_type = HeadType.HT_384_D_70
    controller = DarwinController(profile)

    calls: list[str] = []
    monkeypatch.setattr(axis_module, "reset_faults", lambda engine, addr: calls.append("reset"))

    controller.reset_faults([Axis.X, Axis.Y, Axis.Z])

    assert len(calls) == 3


def test_darwin_controller_home_axes_calls_initialize_per_axis(monkeypatch):
    from pybravo.darwin import axis as axis_module

    profile = Bravo(mode="simulation").profile
    profile.connection.controller_type = "darwin"
    profile.head.head_type = HeadType.HT_384_D_70
    controller = DarwinController(profile)

    axes_initialized: list[str] = []

    def fake_initialize(engine, addr, name, **kwargs):
        axes_initialized.append(name)

    monkeypatch.setattr(axis_module, "initialize", fake_initialize)

    controller.home_axes([Axis.Z])

    assert axes_initialized == ["Z"]


def test_darwin_controller_open_tcp_connects_engine(monkeypatch):
    controller = DarwinController(address="192.168.0.8")

    connected = []

    def fake_connect():
        connected.append(True)

    monkeypatch.setattr(controller._engine, "connect", fake_connect)

    controller.open_tcp("192.168.0.8")

    assert connected == [True]


def test_build_candidate_ips_always_includes_192_168_0_subnet():
    adapters = [{"name": "wifi", "ip": "10.0.0.5", "netmask": "255.255.255.0"}]

    candidates = server._build_candidate_ips(adapters, None)

    assert "192.168.0.8" in candidates
    assert "10.0.0.1" in candidates
    assert "10.0.0.5" not in candidates


def test_parse_bionet_reply_matches_vendor_capture():
    payload = bytes.fromhex("1103081dbd44415257494eec31")

    parsed = server._parse_bionet_reply(payload, "192.168.0.8")

    assert parsed == {
        "ip_address": "192.168.0.8",
        "device_type": "DARWIN",
        "raw_type": "DARWIN",
        "device_id": "EC31",
        "tcp_port": 7613,
        "controller_type": "darwin_native",
    }


def test_builtin_labware_mirrors_critical_mongo_geometry(monkeypatch, tmp_path):
    monkeypatch.setenv("PYBRAVO_LABWARE_MONGO_URI", "")
    monkeypatch.setenv("PYBRAVO_LABWARE_MONGO_DB", "")
    monkeypatch.setenv("PYBRAVO_LABWARE_MONGO_COLLECTION", "")
    monkeypatch.setenv("PYBRAVO_LABWARE_SNAPSHOT_PATH", str(tmp_path / "missing_labware_snapshot.yaml"))

    catalog = build_labware_catalog()
    definition = next(
        item for item in catalog.list_definitions()
        if item.name == "384 Greiner 781091 PS uclear"
    )

    assert definition.height_mm == 14.4
    assert definition.stack_height_mm == 8.6
    assert definition.gripper_offset_mm == 2.5
    assert definition.shim_thickness_mm == 4.0
    assert definition.lidded_height_mm == 16.5
    assert definition.lidded_stack_height_mm == 14.5
    assert definition.lid_resting_height_mm == 9.5
    assert definition.lid_departure_height_mm == 8.5


def test_mirrored_labware_overrides_conflicting_mongo_motion_fields():
    mongo_row = LabwareDefinition(
        id="mongo-1",
        name="384 Greiner 781091 PS uclear",
        kind="sbs_plate",
        height_mm=0.0,
        stack_height_mm=0.0,
        gripper_offset_mm=0.5,
    )

    merged = _apply_mirrored_motion_fields(mongo_row)

    assert merged.id == "mongo-1"
    assert merged.height_mm == 14.4
    assert merged.stack_height_mm == 8.6
    assert merged.gripper_offset_mm == 2.5


def test_build_labware_catalog_writes_local_snapshot_after_mongo_sync(monkeypatch, tmp_path):
    sample_definition = LabwareDefinition(
        id="mongo-plate",
        name="Snapshot Plate",
        kind="sbs_plate",
        wells=96,
        length_mm=127.76,
        width_mm=85.48,
        height_mm=14.2,
        stack_height_mm=12.1,
    )
    snapshot_path = tmp_path / "labware_snapshot.yaml"

    class FakeMongoCatalog:
        def __init__(self, uri: str, database: str, collection: str) -> None:
            assert uri == "mongodb://example"
            assert database == "labdb"
            assert collection == "types"

        def list_definitions(self) -> list[LabwareDefinition]:
            return [sample_definition]

    monkeypatch.setenv("PYBRAVO_LABWARE_MONGO_URI", "mongodb://example")
    monkeypatch.setenv("PYBRAVO_LABWARE_MONGO_DB", "labdb")
    monkeypatch.setenv("PYBRAVO_LABWARE_MONGO_COLLECTION", "types")
    monkeypatch.setenv("PYBRAVO_LABWARE_SNAPSHOT_PATH", str(snapshot_path))
    monkeypatch.setattr(labware_module, "MongoLabwareCatalog", FakeMongoCatalog)

    catalog = build_labware_catalog()

    definitions = catalog.list_definitions()
    assert [item.name for item in definitions] == ["Snapshot Plate"]
    saved = labware_module._read_labware_snapshot(snapshot_path)
    assert [item.name for item in saved] == ["Snapshot Plate"]


def test_build_labware_catalog_uses_local_snapshot_when_mongo_unavailable(monkeypatch, tmp_path):
    snapshot_path = tmp_path / "labware_snapshot.yaml"
    cached_definition = LabwareDefinition(
        id="cached-plate",
        name="Cached Plate",
        kind="sbs_plate",
        wells=384,
        length_mm=127.76,
        width_mm=85.48,
        height_mm=10.5,
        stack_height_mm=8.2,
    )
    labware_module._write_labware_snapshot(snapshot_path, [cached_definition], source="test")

    class FailingMongoCatalog:
        def __init__(self, uri: str, database: str, collection: str) -> None:
            raise RuntimeError("mongo offline")

    monkeypatch.setenv("PYBRAVO_LABWARE_MONGO_URI", "mongodb://example")
    monkeypatch.setenv("PYBRAVO_LABWARE_MONGO_DB", "labdb")
    monkeypatch.setenv("PYBRAVO_LABWARE_MONGO_COLLECTION", "types")
    monkeypatch.setenv("PYBRAVO_LABWARE_SNAPSHOT_PATH", str(snapshot_path))
    monkeypatch.setattr(labware_module, "MongoLabwareCatalog", FailingMongoCatalog)

    catalog = build_labware_catalog()

    definitions = catalog.list_definitions()
    assert [item.name for item in definitions] == ["Cached Plate"]


def test_build_labware_catalog_collapses_duplicate_names_and_preserves_alias_lookup(monkeypatch, tmp_path):
    stale_definition = LabwareDefinition(
        id="dup-old",
        name="Duplicate Plate",
        kind="sbs_plate",
        height_mm=10.0,
        stack_height_mm=8.0,
    )
    canonical_definition = LabwareDefinition(
        id="dup-new",
        name="Duplicate Plate",
        kind="sbs_plate",
        vendor="Canonical",
        catalog_number="12345",
        description="Preferred runtime row",
        base_class="microplate",
        wells=384,
        length_mm=127.76,
        width_mm=85.48,
        height_mm=10.4,
        stack_height_mm=9.8,
        rows=16,
        cols=24,
    )
    snapshot_path = tmp_path / "labware_snapshot.yaml"

    class FakeMongoCatalog:
        def __init__(self, uri: str, database: str, collection: str) -> None:
            assert uri == "mongodb://example"
            assert database == "labdb"
            assert collection == "types"

        def list_definitions(self) -> list[LabwareDefinition]:
            return [stale_definition, canonical_definition]

    monkeypatch.setenv("PYBRAVO_LABWARE_MONGO_URI", "mongodb://example")
    monkeypatch.setenv("PYBRAVO_LABWARE_MONGO_DB", "labdb")
    monkeypatch.setenv("PYBRAVO_LABWARE_MONGO_COLLECTION", "types")
    monkeypatch.setenv("PYBRAVO_LABWARE_SNAPSHOT_PATH", str(snapshot_path))
    monkeypatch.setattr(labware_module, "MongoLabwareCatalog", FakeMongoCatalog)

    catalog = build_labware_catalog()

    definitions = catalog.list_definitions()
    assert len(definitions) == 1
    assert definitions[0].id == "dup-new"
    assert catalog.get_definition("dup-new").id == "dup-new"
    assert catalog.get_definition("dup-old").id == "dup-new"

    saved = labware_module._read_labware_snapshot(snapshot_path)
    assert len(saved) == 1
    assert saved[0].id == "dup-new"


def test_labware_editor_writes_through_to_mongo(monkeypatch, tmp_path):
    editor_path = tmp_path / "labware_editor.yaml"
    snapshot_path = tmp_path / "labware_snapshot.yaml"
    mongo_types = _FakeMongoCollection()
    mongo_classes = _FakeMongoCollection()
    fake_client = _FakeMongoClient({"labdb": _FakeMongoDatabase({"types": mongo_types, "classes": mongo_classes})})

    monkeypatch.setenv("PYBRAVO_LABWARE_EDITOR_PATH", str(editor_path))
    monkeypatch.setenv("PYBRAVO_LABWARE_SNAPSHOT_PATH", str(snapshot_path))
    monkeypatch.setenv("PYBRAVO_LABWARE_MONGO_URI", "mongodb://example")
    monkeypatch.setenv("PYBRAVO_LABWARE_MONGO_DB", "labdb")
    monkeypatch.setenv("PYBRAVO_LABWARE_MONGO_COLLECTION", "types")
    monkeypatch.setenv("PYBRAVO_LABWARE_MONGO_CLASS_COLLECTION", "classes")
    monkeypatch.setattr(labware_editor, "MongoClient", lambda *args, **kwargs: fake_client)

    created = labware_editor.create_type({
        "name": "Mongo Tipbox",
        "kind": "tip_box",
        "base_class": "tip_box",
        "wells": 96,
    })

    assert created["name"] == "Mongo Tipbox"
    assert any(doc["name"] == "Mongo Tipbox" for doc in mongo_types.docs)
    assert any(item.name == "Mongo Tipbox" for item in labware_module._read_labware_snapshot(snapshot_path))


def test_labware_editor_prefers_mongo_over_stale_local_store(monkeypatch, tmp_path):
    editor_path = tmp_path / "labware_editor.yaml"
    snapshot_path = tmp_path / "labware_snapshot.yaml"
    editor_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "labware_types": [{"labware_type_id": "local-1", "name": "Local Only", "kind": "sbs_plate"}],
                "labware_classes": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    mongo_types = _FakeMongoCollection([
        {
            "labware_type_id": "mongo-1",
            "name": "Mongo Canonical",
            "kind": "sbs_plate",
            "base_class": "microplate",
            "wells": 96,
            "plate_dimensions_mm": {"length_mm": 127.76, "width_mm": 85.48, "height_mm": 14.0},
            "plate_properties": {"thickness_mm": 14.0, "stacking_thickness_mm": 12.0},
            "well_dimensions_mm": {},
            "labware_class_ids": [],
        }
    ])
    mongo_classes = _FakeMongoCollection()
    fake_client = _FakeMongoClient({"labdb": _FakeMongoDatabase({"types": mongo_types, "classes": mongo_classes})})

    monkeypatch.setenv("PYBRAVO_LABWARE_EDITOR_PATH", str(editor_path))
    monkeypatch.setenv("PYBRAVO_LABWARE_SNAPSHOT_PATH", str(snapshot_path))
    monkeypatch.setenv("PYBRAVO_LABWARE_MONGO_URI", "mongodb://example")
    monkeypatch.setenv("PYBRAVO_LABWARE_MONGO_DB", "labdb")
    monkeypatch.setenv("PYBRAVO_LABWARE_MONGO_COLLECTION", "types")
    monkeypatch.setenv("PYBRAVO_LABWARE_MONGO_CLASS_COLLECTION", "classes")
    monkeypatch.setattr(labware_editor, "MongoClient", lambda *args, **kwargs: fake_client)

    store = labware_editor.load_store()

    assert [item["name"] for item in store["labware_types"]] == ["Mongo Canonical"]


def test_labware_editor_patch_type_clears_membership_and_collapses_duplicates(monkeypatch, tmp_path):
    editor_path = tmp_path / "labware_editor.yaml"
    snapshot_path = tmp_path / "labware_snapshot.yaml"
    monkeypatch.setenv("PYBRAVO_LABWARE_EDITOR_PATH", str(editor_path))
    monkeypatch.setenv("PYBRAVO_LABWARE_SNAPSHOT_PATH", str(snapshot_path))
    monkeypatch.setenv("PYBRAVO_LABWARE_MONGO_URI", "")
    monkeypatch.setenv("PYBRAVO_LABWARE_MONGO_DB", "")
    monkeypatch.setenv("PYBRAVO_LABWARE_MONGO_COLLECTION", "")

    duplicate_row = {
        "labware_type_id": "dup-plate",
        "name": "Duplicate Plate",
        "kind": "sbs_plate",
        "base_class": "microplate",
        "wells": 96,
        "plate_dimensions_mm": {"length_mm": 127.76, "width_mm": 85.48, "height_mm": 14.0},
        "plate_properties": {"thickness_mm": 14.0, "stacking_thickness_mm": 12.0},
        "well_dimensions_mm": {},
        "pf400": {},
        "planar_motor": {},
        "labware_class_ids": ["class-tipbox"],
        "tip_definition_id": "",
        "supported_tip_ids": [],
    }
    editor_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "labware_types": [duplicate_row, dict(duplicate_row)],
                "labware_classes": [{"labware_class_id": "class-tipbox", "name": "Tipbox"}],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    updated = labware_editor.patch_type("dup-plate", {"labware_class_ids": []})

    assert updated["labware_class_ids"] == []

    reloaded = yaml.safe_load(editor_path.read_text(encoding="utf-8"))
    rows = [item for item in reloaded["labware_types"] if item["labware_type_id"] == "dup-plate"]
    assert len(rows) == 1
    assert rows[0]["labware_class_ids"] == []

    snapshot = yaml.safe_load(snapshot_path.read_text(encoding="utf-8"))
    snapshot_rows = [item for item in snapshot["labware"] if item["id"] == "dup-plate"]
    assert len(snapshot_rows) == 1


def test_labware_editor_write_store_to_mongo_removes_duplicate_docs_for_same_type_id(monkeypatch, tmp_path):
    editor_path = tmp_path / "labware_editor.yaml"
    snapshot_path = tmp_path / "labware_snapshot.yaml"
    mongo_types = _FakeMongoCollection([
        {"labware_type_id": "dup-plate", "name": "Old Plate", "kind": "sbs_plate", "labware_class_ids": ["class-tipbox"]},
        {"labware_type_id": "dup-plate", "name": "Older Plate", "kind": "sbs_plate", "labware_class_ids": ["class-tipbox"]},
    ])
    mongo_classes = _FakeMongoCollection([
        {"labware_class_id": "class-tipbox", "name": "Tipbox"},
    ])
    fake_client = _FakeMongoClient({"labdb": _FakeMongoDatabase({"types": mongo_types, "classes": mongo_classes})})

    monkeypatch.setenv("PYBRAVO_LABWARE_EDITOR_PATH", str(editor_path))
    monkeypatch.setenv("PYBRAVO_LABWARE_SNAPSHOT_PATH", str(snapshot_path))
    monkeypatch.setenv("PYBRAVO_LABWARE_MONGO_URI", "mongodb://example")
    monkeypatch.setenv("PYBRAVO_LABWARE_MONGO_DB", "labdb")
    monkeypatch.setenv("PYBRAVO_LABWARE_MONGO_COLLECTION", "types")
    monkeypatch.setenv("PYBRAVO_LABWARE_MONGO_CLASS_COLLECTION", "classes")
    monkeypatch.setattr(labware_editor, "MongoClient", lambda *args, **kwargs: fake_client)

    labware_editor.save_store(
        {
            "version": 1,
            "labware_types": [
                {
                    "labware_type_id": "dup-plate",
                    "name": "Canonical Plate",
                    "kind": "sbs_plate",
                    "base_class": "microplate",
                    "wells": 96,
                    "plate_dimensions_mm": {"length_mm": 127.76, "width_mm": 85.48, "height_mm": 14.0},
                    "plate_properties": {"thickness_mm": 14.0, "stacking_thickness_mm": 12.0},
                    "well_dimensions_mm": {},
                    "pf400": {},
                    "planar_motor": {},
                    "labware_class_ids": [],
                    "tip_definition_id": "",
                    "supported_tip_ids": [],
                }
            ],
            "labware_classes": [{"labware_class_id": "class-tipbox", "name": "Tipbox"}],
        }
    )

    rows = [item for item in mongo_types.docs if item["labware_type_id"] == "dup-plate"]
    assert len(rows) == 1
    assert rows[0]["name"] == "Canonical Plate"
    assert rows[0]["labware_class_ids"] == []


def test_editor_model_asset_url_survives_into_runtime_snapshot(monkeypatch, tmp_path):
    editor_path = tmp_path / "labware_editor.yaml"
    snapshot_path = tmp_path / "labware_snapshot.yaml"
    monkeypatch.setenv("PYBRAVO_LABWARE_EDITOR_PATH", str(editor_path))
    monkeypatch.setenv("PYBRAVO_LABWARE_SNAPSHOT_PATH", str(snapshot_path))
    monkeypatch.setenv("PYBRAVO_LABWARE_MONGO_URI", "")
    monkeypatch.setenv("PYBRAVO_LABWARE_MONGO_DB", "")
    monkeypatch.setenv("PYBRAVO_LABWARE_MONGO_COLLECTION", "")

    labware_editor.save_store(
        {
            "version": 1,
            "labware_types": [
                {
                    "labware_type_id": "tipbox-1",
                    "name": "Editor Tipbox",
                    "kind": "tip_box",
                    "base_class": "tip_box",
                    "wells": 96,
                    "plate_dimensions_mm": {"length_mm": 127.76, "width_mm": 85.48, "height_mm": 10.0},
                    "plate_properties": {"thickness_mm": 10.0, "stacking_thickness_mm": 10.0},
                    "well_dimensions_mm": {},
                    "pf400": {},
                    "planar_motor": {},
                    "labware_class_ids": [],
                    "model_3d": {
                        "url": "/labware-assets/tipbox-1/ST_Tip_Box.gltf",
                        "filename": "ST_Tip_Box.gltf",
                        "format": "gltf",
                    },
                }
            ],
            "labware_classes": [],
        }
    )

    snapshot = labware_module._read_labware_snapshot(snapshot_path)

    assert len(snapshot) == 1
    assert snapshot[0].model_3d == "/labware-assets/tipbox-1/ST_Tip_Box.gltf"


def test_mongo_catalog_preserves_model_3d_from_documents(monkeypatch):
    class FakeCollection:
        def find(self, query, projection):
            assert projection["model_3d"] == 1
            return [{
                "labware_type_id": "mongo-tipbox",
                "name": "Mongo Tipbox",
                "kind": "tip_box",
                "base_class": "tip_box",
                "wells": 96,
                "plate_dimensions_mm": {"length_mm": 127.76, "width_mm": 85.48, "height_mm": 45.0},
                "plate_properties": {"thickness_mm": 45.0, "stacking_thickness_mm": 40.0},
                "well_dimensions_mm": {},
                "model_3d": {"url": "/labware-assets/mongo-tipbox/ST_Tip_Box.gltf", "filename": "ST_Tip_Box.gltf"},
            }]

    class FakeClient:
        def __init__(self, uri, serverSelectionTimeoutMS):
            self.uri = uri

        def __getitem__(self, database):
            return {"labware_types": FakeCollection()}

    monkeypatch.setattr(labware_module, "MongoClient", FakeClient)

    catalog = labware_module.MongoLabwareCatalog("mongodb://example", "labdb", "labware_types")
    definitions = catalog.list_definitions()

    assert len(definitions) == 1
    assert definitions[0].model_3d == "/labware-assets/mongo-tipbox/ST_Tip_Box.gltf"


@pytest.mark.asyncio
async def test_labware_editor_type_routes_round_trip(monkeypatch, tmp_path):
    editor_path = tmp_path / "labware_editor.yaml"
    snapshot_path = tmp_path / "labware_snapshot.yaml"
    monkeypatch.setenv("PYBRAVO_LABWARE_EDITOR_PATH", str(editor_path))
    monkeypatch.setenv("PYBRAVO_LABWARE_SNAPSHOT_PATH", str(snapshot_path))
    monkeypatch.setenv("PYBRAVO_LABWARE_MONGO_URI", "")
    monkeypatch.setenv("PYBRAVO_LABWARE_MONGO_DB", "")
    monkeypatch.setenv("PYBRAVO_LABWARE_MONGO_COLLECTION", "")

    created = await server.create_labware_type(
        server.LabwareTypeRequest(
            name="Editor Plate",
            kind="sbs_plate",
            wells=96,
            plate_dimensions_mm={"length_mm": 127.76, "width_mm": 85.48, "height_mm": 14.0},
        )
    )
    assert created["labware_type"]["name"] == "Editor Plate"

    types = await server.list_labware_types()
    assert any(item["name"] == "Editor Plate" for item in types["labware_types"])

    snapshot = labware_module._read_labware_snapshot(snapshot_path)
    assert any(item.name == "Editor Plate" for item in snapshot)


@pytest.mark.asyncio
async def test_update_labware_type_refreshes_live_deck_labware(monkeypatch, tmp_path):
    editor_path = tmp_path / "labware_editor.yaml"
    snapshot_path = tmp_path / "labware_snapshot.yaml"
    monkeypatch.setenv("PYBRAVO_LABWARE_EDITOR_PATH", str(editor_path))
    monkeypatch.setenv("PYBRAVO_LABWARE_SNAPSHOT_PATH", str(snapshot_path))
    monkeypatch.setenv("PYBRAVO_LABWARE_MONGO_URI", "")
    monkeypatch.setenv("PYBRAVO_LABWARE_MONGO_DB", "")
    monkeypatch.setenv("PYBRAVO_LABWARE_MONGO_COLLECTION", "")

    bravo = Bravo(mode="simulation")
    previous_bravo = server._bravo

    try:
        server._bravo = bravo
        created = await server.create_labware_type(
            server.LabwareTypeRequest(
                name="Editor Plate",
                kind="sbs_plate",
                wells=384,
                plate_dimensions_mm={"length_mm": 127.76, "width_mm": 85.48, "height_mm": 14.4},
                plate_properties={
                    "robot_gripper_offset_mm": 1.5,
                    "thickness_mm": 14.4,
                    "stacking_thickness_mm": 13.6,
                },
            )
        )
        type_id = created["labware_type"]["labware_type_id"]

        bravo.set_labware(1, type_id)
        assert bravo._deck.get_stack(1).top.gripper_offset == pytest.approx(1.5)

        await server.update_labware_type(
            type_id,
            server.LabwareTypeRequest(
                plate_properties={
                    "robot_gripper_offset_mm": 4.0,
                    "thickness_mm": 14.4,
                    "stacking_thickness_mm": 13.6,
                }
            ),
        )

        top = bravo._deck.get_stack(1).top
        assert top is not None
        assert top.gripper_offset == pytest.approx(4.0)
        assert float(top.metadata["gripper_offset_mm"]) == pytest.approx(4.0)
    finally:
        server._bravo = previous_bravo


@pytest.mark.asyncio
async def test_labware_editor_class_delete_removes_membership(monkeypatch, tmp_path):
    editor_path = tmp_path / "labware_editor.yaml"
    snapshot_path = tmp_path / "labware_snapshot.yaml"
    monkeypatch.setenv("PYBRAVO_LABWARE_EDITOR_PATH", str(editor_path))
    monkeypatch.setenv("PYBRAVO_LABWARE_SNAPSHOT_PATH", str(snapshot_path))
    monkeypatch.setenv("PYBRAVO_LABWARE_MONGO_URI", "")
    monkeypatch.setenv("PYBRAVO_LABWARE_MONGO_DB", "")
    monkeypatch.setenv("PYBRAVO_LABWARE_MONGO_COLLECTION", "")

    created_type = await server.create_labware_type(server.LabwareTypeRequest(name="Classed Plate"))
    created_class = await server.create_labware_class(server.LabwareClassRequest(name="FitsDevice"))
    type_id = created_type["labware_type"]["labware_type_id"]
    class_id = created_class["labware_class"]["labware_class_id"]

    await server.update_labware_type(
        type_id,
        server.LabwareTypeRequest(labware_class_ids=[class_id]),
    )
    await server.remove_labware_class(class_id)

    updated = labware_editor.get_type(type_id)
    assert updated is not None
    assert updated["labware_class_ids"] == []


@pytest.mark.asyncio
async def test_discover_devices_uses_request_controller_type_override(monkeypatch):
    bravo = Bravo(mode="simulation")
    previous_bravo = server._bravo

    monkeypatch.setattr(
        server,
        "_enumerate_adapters",
        lambda: [{"name": "eth0", "ip": "10.0.0.5", "netmask": "255.255.255.0"}],
    )

    async def fake_to_thread(func, *args):
        if func is server._discover_bionet_devices:
            return [{"ip_address": "192.168.0.8", "device_type": "Bravo", "serial": "8", "mac_address": "AA-BB"}]
        if func is server._scan_subnet:
            return []
        return None

    monkeypatch.setattr(server.asyncio, "to_thread", fake_to_thread)

    try:
        server._bravo = bravo
        response = await server.discover_devices(
            server.DiscoverDevicesRequest(
                adapter="All interfaces",
                controller_type="agile",
            )
        )
    finally:
        server._bravo = previous_bravo

    assert len(response["devices"]) == 1
    device = response["devices"][0]
    assert device["device_id"] == "8"
    assert device["device_type"] == "Bravo"
    assert device["ip_address"] == "192.168.0.8"
    assert device["mac_address"] == "AA-BB"
    assert device["status"] == "Found"


@pytest.mark.asyncio
async def test_discover_devices_prefers_bionet_udp_results(monkeypatch):
    bravo = Bravo(mode="simulation")
    previous_bravo = server._bravo

    monkeypatch.setattr(
        server,
        "_enumerate_adapters",
        lambda: [{"name": "eth0", "ip": "192.168.0.100", "netmask": "255.255.255.0"}],
    )

    async def fake_to_thread(func, *args):
        if func is server._discover_bionet_devices:
            return [{
                "ip_address": "192.168.0.8",
                "device_type": "Bravo",
                "raw_type": "DARWIN",
                "device_id": "EC31",
                "tcp_port": 7613,
                "mac_address": "04-91-62-CF-7B-B0",
                "controller_type": "darwin_native",
            }]
        if func is server._scan_subnet:
            return []
        return None

    monkeypatch.setattr(server.asyncio, "to_thread", fake_to_thread)

    try:
        server._bravo = bravo
        response = await server.discover_devices(
            server.DiscoverDevicesRequest(
                adapter="All interfaces",
                controller_type="agile",
            )
        )
    finally:
        server._bravo = previous_bravo

    assert len(response["devices"]) == 1
    device = response["devices"][0]
    assert device["device_id"] == "EC31"
    assert device["device_type"] == "DARWIN"
    assert device["ip_address"] == "192.168.0.8"
    assert device["mac_address"] == "04-91-62-CF-7B-B0"
    assert device["status"] == "Found"


@pytest.mark.asyncio
async def test_select_device_persists_ip_to_profile(tmp_path: Path):
    bravo = Bravo(mode="simulation")
    previous_bravo = server._bravo
    previous_profile_path = server._profile_path
    profile_path = tmp_path / "active.yaml"
    bravo.profile.save(profile_path)

    try:
        server._bravo = bravo
        server._profile_path = profile_path

        response = await server.select_device(
            server.SelectDeviceRequest(
                device_id="EC31",
                ip_address="192.168.0.8",
                controller_type="agile",
            )
        )

        reloaded = bravo.profile.load(profile_path)
        assert response["ip_address"] == "192.168.0.8"
        assert reloaded.connection.address == "192.168.0.8"
    finally:
        server._bravo = previous_bravo
        server._profile_path = previous_profile_path


@pytest.mark.asyncio
async def test_teach_current_position_persists_teach_tip_capacity(tmp_path: Path):
    bravo = Bravo(mode="simulation")
    bravo.connect()
    previous_bravo = server._bravo
    previous_profile_path = server._profile_path
    profile_path = tmp_path / "active.yaml"
    bravo.profile.save(profile_path)

    try:
        server._bravo = bravo
        server._profile_path = profile_path

        response = await server.teach_current_position(
            1,
            server.TeachCurrentRequest(tip_capacity=30.0),
        )

        reloaded = bravo.profile.load(profile_path)
        assert response["teach_tip_capacity"] == 30.0
        assert response["teach_tip_height_mm"] == 26.1
        assert response["teach_tip"]["label"] == "30 uL"
        assert reloaded.head.teach_tip_capacity == 30.0
        assert reloaded.head.teach_tip_length_mm == 26.1
    finally:
        server._bravo = previous_bravo
        server._profile_path = previous_profile_path
        bravo.disconnect()


@pytest.mark.asyncio
async def test_get_profile_exposes_vendor_teach_tip_options():
    bravo = Bravo(mode="simulation")
    bravo.profile.head.head_type = HeadType.HT_384_D_70
    previous_bravo = server._bravo

    try:
        server._bravo = bravo
        response = await server.get_profile()
    finally:
        server._bravo = previous_bravo

    options = response["head"]["teach_tip_options"]
    assert [opt["capacity_ul"] for opt in options] == [10.0, 15.0, 30.0, 50.0, 51.0, 70.0]
    assert next(opt for opt in options if opt["capacity_ul"] == 10.0)["length_mm"] == 19.9
    assert next(opt for opt in options if opt["capacity_ul"] == 30.0)["length_mm"] == 26.1


@pytest.mark.asyncio
async def test_discover_devices_marks_matching_ip_as_matched(monkeypatch):
    bravo = Bravo(mode="simulation")
    bravo.profile.connection.address = "192.168.0.8"
    previous_bravo = server._bravo

    monkeypatch.setattr(
        server,
        "_enumerate_adapters",
        lambda: [{"name": "eth0", "ip": "192.168.0.100", "netmask": "255.255.255.0"}],
    )

    async def fake_to_thread(func, *args):
        if func is server._discover_bionet_devices:
            return [{
                "ip_address": "192.168.0.8",
                "device_type": "Bravo",
                "raw_type": "DARWIN",
                "device_id": "EC31",
                "tcp_port": 7613,
                "mac_address": "04-91-62-CF-7B-B0",
                "controller_type": "darwin_native",
            }]
        if func is server._scan_subnet:
            return []
        return None

    monkeypatch.setattr(server.asyncio, "to_thread", fake_to_thread)

    try:
        server._bravo = bravo
        response = await server.discover_devices(
            server.DiscoverDevicesRequest(
                adapter="All interfaces",
                controller_type="agile",
            )
        )
    finally:
        server._bravo = previous_bravo

    assert len(response["devices"]) == 1
    device = response["devices"][0]
    assert device["device_id"] == "EC31"
    assert device["ip_address"] == "192.168.0.8"
    assert device["status"] == "Matched"


@pytest.mark.asyncio
async def test_select_device_sets_controller_type():
    bravo = Bravo(mode="simulation")
    previous_bravo = server._bravo

    try:
        server._bravo = bravo

        response = await server.select_device(
            server.SelectDeviceRequest(
                device_id="EC31",
                ip_address="192.168.0.8",
                controller_type="darwin",
            )
        )

        assert response["controller_type"] == "darwin"
        assert bravo.profile.connection.controller_type == "darwin"
    finally:
        server._bravo = previous_bravo


@pytest.mark.asyncio
async def test_connect_initializes_after_successful_connect(tmp_path: Path):
    bravo = Bravo(mode="simulation")
    bravo.profile.safety.prompt_home_w = False
    previous_bravo = server._bravo
    previous_profile_path = server._profile_path
    profile_path = tmp_path / "active.yaml"
    bravo.profile.save(profile_path)

    try:
        server._bravo = bravo
        server._profile_path = profile_path

        response = await server.connect(server.ConnectRequest(controller_type="simulation"))

        assert response == {"status": "connected", "controller": "simulation"}
        assert bravo.is_connected is True
    finally:
        server._bravo = previous_bravo
        server._profile_path = previous_profile_path
        bravo.disconnect()


@pytest.mark.asyncio
async def test_initialize_connects_first_when_disconnected(monkeypatch):
    bravo = Bravo(mode="simulation")
    previous_bravo = server._bravo

    try:
        server._bravo = bravo
        calls: list[str] = []

        def fake_connect():
            bravo._controller = SimpleNamespace(is_connected=True, close=lambda: None)
            calls.append("connect")

        async def fake_initialize():
            calls.append("initialize")
            bravo._initialized = True

        monkeypatch.setattr(bravo, "connect", fake_connect)
        monkeypatch.setattr(bravo, "initialize", fake_initialize)

        response = await server.initialize()

        assert response == {"status": "initialized", "controller": "simulation"}
        assert calls == ["connect", "initialize"]
    finally:
        server._bravo = previous_bravo


@pytest.mark.asyncio
async def test_jog_forwards_selected_speed(monkeypatch):
    bravo = Bravo(mode="simulation")
    previous_bravo = server._bravo

    try:
        server._bravo = bravo
        captured: dict[str, object] = {}

        async def fake_jog_axis(axis, step, speed, peak_current=None):
            captured["axis"] = axis
            captured["step"] = step
            captured["speed"] = speed
            captured["peak_current"] = peak_current
            return 12.34

        monkeypatch.setattr(bravo, "jog_axis", fake_jog_axis)

        response = await server.jog(server.JogRequest(axis="x", step=5, direction=-1, speed="slow"))

        assert response["position"] == 12.34
        assert captured["axis"] == server.Axis.X
        assert captured["step"] == -5
        assert captured["speed"] == server.SpeedLevel.SLOW
        assert captured["peak_current"] is None
    finally:
        server._bravo = previous_bravo


@pytest.mark.asyncio
async def test_move_to_location_forwards_selected_speed(monkeypatch):
    bravo = Bravo(mode="simulation")
    previous_bravo = server._bravo

    try:
        server._bravo = bravo
        captured: dict[str, object] = {}

        async def fake_move_to_location(location, approach_height=0.0, only_move_z=False, speed=None):
            captured["location"] = location
            captured["approach_height"] = approach_height
            captured["only_move_z"] = only_move_z
            captured["speed"] = speed

        monkeypatch.setattr(bravo, "move_to_location", fake_move_to_location)

        response = await server.move_to_location(
            server.MoveToLocationRequest(location=3, approach_height=20, only_move_z=False, speed="fast")
        )

        assert response["speed"] == "FAST"
        assert captured == {
            "location": 3,
            "approach_height": 20,
            "only_move_z": False,
            "speed": server.SpeedLevel.FAST,
        }
    finally:
        server._bravo = previous_bravo


@pytest.mark.asyncio
async def test_list_labware_returns_catalog():
    bravo = Bravo(mode="simulation")
    previous_bravo = server._bravo

    try:
        server._bravo = bravo
        response = await server.list_labware()
        assert response["labware"]
        ids = [item["id"] for item in response["labware"]]
        names = [item["name"] for item in response["labware"]]
        assert all(name for name in names)
        assert len(ids) == len(set(ids))
    finally:
        server._bravo = previous_bravo


@pytest.mark.asyncio
async def test_list_labware_collapses_duplicate_runtime_names():
    bravo = Bravo(mode="simulation")
    previous_bravo = server._bravo
    bravo._labware_catalog = SimpleNamespace(
        list_definitions=lambda: [
            LabwareDefinition(id="dup-old", name="Duplicate Plate", kind="sbs_plate", height_mm=10.0),
            LabwareDefinition(
                id="dup-new",
                name="Duplicate Plate",
                kind="sbs_plate",
                vendor="Canonical",
                base_class="microplate",
                wells=384,
                height_mm=10.4,
                stack_height_mm=9.8,
            ),
        ]
    )

    try:
        server._bravo = bravo
        response = await server.list_labware()
        labware = response["labware"]
        assert len(labware) == 1
        item = labware[0]
        assert item["id"] == "dup-new"
        assert item["name"] == "Duplicate Plate"
        assert item["kind"] == "sbs_plate"
        assert item["vendor"] == "Canonical"
        assert item["base_class"] == "microplate"
        assert item["wells"] == 384
        assert item["height_mm"] == pytest.approx(10.4)
        assert item["stack_height_mm"] == pytest.approx(9.8)
    finally:
        server._bravo = previous_bravo


def test_openapi_schema_includes_route_summaries_and_tags():
    schema = server.app.openapi()

    assert schema["info"]["title"] == "PyBravo API"
    assert any(tag["name"] == "Motion" for tag in schema["tags"])
    assert any(tag["name"] == "Vision" for tag in schema["tags"])
    assert schema["paths"]["/api/pick_place"]["post"]["summary"] == (
        "Pick labware from one deck location and place it in another"
    )
    assert schema["paths"]["/api/pick_place"]["post"]["tags"] == ["Motion"]
    assert "What this operation does" in schema["paths"]["/api/pick_place"]["post"]["description"]
    assert "Retracts to a safe starting condition" in schema["paths"]["/api/pick_place"]["post"]["description"]
    assert "Prerequisites" in schema["paths"]["/api/pick_place"]["post"]["description"]
    assert "Sequence" in schema["paths"]["/api/tips_on"]["post"]["description"]
    assert "Common failure cases" in schema["paths"]["/api/aspirate"]["post"]["description"]
    assert "manual alignment" in schema["paths"]["/api/connect"]["post"]["description"].lower()
    assert "teachpoints define the reference x, y, and z coordinates" in (
        schema["paths"]["/api/teachpoint/{location}"]["get"]["description"].lower()
    )
    assert "define labware" in schema["paths"]["/labware/types"]["get"]["description"].lower()
    assert "machine identifier, installed head type, active tip definition" in (
        schema["paths"]["/api/liquid_context"]["get"]["description"].lower()
    )
    assert "tip definitions describe the disposable tip consumables" in (
        schema["paths"]["/api/tips"]["get"]["description"].lower()
    )
    assert "selection behavior" in schema["paths"]["/api/liquid_classes"]["get"]["description"].lower()
    assert "reusable motion patterns" in schema["paths"]["/api/pipette_techniques"]["get"]["description"].lower()
    assert "deck assignment endpoints control which labware is currently present" in (
        schema["paths"]["/api/deck/{location}/labware"]["put"]["description"].lower()
    )
    assert "overall status of the local vision stack" in (
        schema["paths"]["/api/vision/status"]["get"]["description"].lower()
    )
    assert "camera-to-deck calibration artifact" in (
        schema["paths"]["/api/vision/calibration"]["get"]["description"].lower()
    )
    assert "slot-by-slot report" in schema["paths"]["/api/vision/verify"]["post"]["description"].lower()
    assert schema["paths"]["/api/profile"]["get"]["tags"] == ["Profiles"]
    assert "/labware-editor" not in schema["paths"]


@pytest.mark.asyncio
async def test_set_and_clear_deck_labware():
    bravo = Bravo(mode="simulation")
    previous_bravo = server._bravo

    try:
        server._bravo = bravo
        definitions = bravo.labware_catalog.list_definitions()
        target = definitions[0]

        assigned = await server.set_deck_labware(
            2,
            server.DeckLabwareRequest(labware_id=target.id, is_lidded=True, is_sealed=False),
        )
        assert assigned["status"] == "assigned"
        assert bravo.deck.get_stack(2).top is not None
        assert bravo.deck.get_stack(2).top.name == target.name

        cleared = await server.clear_deck_labware(2)
        assert cleared == {"status": "cleared", "location": 2}
        assert bravo.deck.get_stack(2).top is None
    finally:
        server._bravo = previous_bravo


def test_labware_catalog_config_file_is_checked_in():
    assert labware_module._LABWARE_CONFIG_PATH.exists()
    uri, database, collection = labware_module._load_labware_catalog_config()
    # The shipped config has no Mongo URI, so a fresh install reads the local
    # snapshot instead of connecting to whatever happens to be on localhost.
    assert uri == ""
    assert database == ""
    assert collection == "labware_types"


@pytest.mark.asyncio
async def test_connect_requires_ip_for_ethernet_controller():
    bravo = Bravo(mode="simulation")
    previous_bravo = server._bravo

    try:
        server._bravo = bravo
        bravo.profile.connection.controller_type = "agile"
        bravo.profile.connection.address = ""

        with pytest.raises(RuntimeError) as exc_info:
            await server.connect(server.ConnectRequest(controller_type="agile"))

        assert "No Bravo IP address is configured" in str(exc_info.value)
    finally:
        server._bravo = previous_bravo


@pytest.mark.asyncio
async def test_connect_allows_darwin_when_controller_succeeds(monkeypatch):
    bravo = Bravo(mode="simulation")
    previous_bravo = server._bravo

    try:
        server._bravo = bravo
        calls: list[str] = []

        def fake_connect():
            bravo._profile.connection.controller_type = "darwin"
            bravo._profile.connection.address = "192.168.0.8"
            bravo._controller = SimpleNamespace(is_connected=True, close=lambda: None)
            calls.append("connect")

        monkeypatch.setattr(bravo, "connect", fake_connect)

        response = await server.connect(
            server.ConnectRequest(
                controller_type="darwin",
                address="192.168.0.8",
            )
        )

        assert response == {"status": "connected", "controller": "darwin"}
        assert calls == ["connect"]
    finally:
        server._bravo = previous_bravo


@pytest.mark.asyncio
async def test_execute_command_stack_plates_returns_not_implemented_with_named_base_and_source():
    bravo = Bravo(mode="simulation")
    previous_bravo = server._bravo

    try:
        server._bravo = bravo
        base_plate = LabwareDefinition(
            id="base-plate",
            name="384 Greiner 781091 PS uclear",
            kind="sbs_plate",
            base_class="microplate",
            height_mm=14.4,
        )
        source_plate = LabwareDefinition(
            id="source-plate",
            name="384 Greiner 781091 PS uclear Lid",
            kind="sbs_plate",
            base_class="microplate",
            height_mm=10.0,
        )
        bravo._labware_catalog = type("Catalog", (), {
            "get_definition": lambda self, labware_id: {
                "base-plate": base_plate,
                "source-plate": source_plate,
            }.get(labware_id),
            "list_definitions": lambda self: [base_plate, source_plate],
        })()
        bravo.set_labware(1, "base-plate")
        bravo.set_labware(2, "source-plate")

        captured: dict[str, object] = {}

        async def fake_pick_place(from_location, to_location, speed=None):
            captured["from_location"] = from_location
            captured["to_location"] = to_location
            captured["speed"] = speed
            return {"ok": True}

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(bravo, "pick_place", fake_pick_place)
        try:
            response = await server.execute_command(
                server.ExecuteCommandRequest(command="Stack Plates", base_location=1, source_location=2)
            )
        finally:
            monkeypatch.undo()

        assert response["status"] == "completed"
        assert response["base_plate"] == "384 Greiner 781091 PS uclear"
        assert response["plate_to_place"] == "384 Greiner 781091 PS uclear Lid"
        assert captured["from_location"] == 2
        assert captured["to_location"] == 1
    finally:
        server._bravo = previous_bravo


@pytest.mark.asyncio
async def test_execute_command_destack_plate_moves_top_plate_to_empty_pad():
    bravo = Bravo(mode="simulation")
    previous_bravo = server._bravo

    try:
        server._bravo = bravo
        plate = LabwareDefinition(
            id="stack-plate",
            name="384 Greiner 781091 PS uclear",
            kind="sbs_plate",
            base_class="microplate",
            height_mm=14.4,
            stack_height_mm=8.6,
            gripper_offset_mm=2.5,
        )
        bravo._labware_catalog = type("Catalog", (), {
            "get_definition": lambda self, labware_id: plate if labware_id == "stack-plate" else None,
            "list_definitions": lambda self: [plate],
        })()
        bravo._deck.add(5, Labware.from_definition(plate))
        bravo._deck.add(5, Labware.from_definition(plate))

        captured: dict[str, object] = {}

        async def fake_pick_place(from_location, to_location, speed=None):
            captured["from_location"] = from_location
            captured["to_location"] = to_location
            captured["speed"] = speed
            moved = bravo._deck.remove(from_location)
            bravo._deck.add(to_location, moved)
            return {"ok": True}

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(bravo, "pick_place", fake_pick_place)
        try:
            response = await server.execute_command(
                server.ExecuteCommandRequest(command="Destack Plate", source_location=5, destination_location=4)
            )
        finally:
            monkeypatch.undo()

        assert response["status"] == "completed"
        assert response["source_location"] == 5
        assert response["destination_location"] == 4
        assert response["remaining_stack_count"] == 1
        assert captured["from_location"] == 5
        assert captured["to_location"] == 4
    finally:
        server._bravo = previous_bravo


@pytest.mark.asyncio
async def test_execute_command_delid_plate_moves_lid_to_destination():
    bravo = Bravo(mode="simulation")
    previous_bravo = server._bravo

    try:
        server._bravo = bravo
        plate = LabwareDefinition(
            id="liddable-plate",
            name="384 Greiner 781091 PS uclear",
            kind="sbs_plate",
            base_class="microplate",
            length_mm=127.76,
            width_mm=85.48,
            height_mm=14.4,
            can_have_lid=True,
            lidded_height_mm=16.5,
            lid_resting_height_mm=9.5,
            lid_departure_height_mm=8.5,
        )
        bravo._labware_catalog = type("Catalog", (), {
            "get_definition": lambda self, labware_id: plate if labware_id == "liddable-plate" else None,
            "list_definitions": lambda self: [plate],
        })()
        bravo.set_labware(5, "liddable-plate", is_lidded=True)

        response = await server.execute_command(
            server.ExecuteCommandRequest(command="Delid Plate", plate_location=5, lid_destination=9)
        )

        assert response["status"] == "completed"
        assert response["plate_to_delid"] == "384 Greiner 781091 PS uclear"
        assert response["lid_destination"] == 9
        assert response["lid_name"] == "384 Greiner 781091 PS uclear Lid"
    finally:
        server._bravo = previous_bravo


@pytest.mark.asyncio
async def test_execute_command_relid_plate_moves_lid_back_to_plate():
    bravo = Bravo(mode="simulation")
    previous_bravo = server._bravo

    try:
        server._bravo = bravo
        plate = LabwareDefinition(
            id="liddable-plate",
            name="384 Greiner 781091 PS uclear",
            kind="sbs_plate",
            base_class="microplate",
            length_mm=127.76,
            width_mm=85.48,
            height_mm=14.4,
            can_have_lid=True,
            lidded_height_mm=16.5,
            lid_resting_height_mm=9.5,
            lid_departure_height_mm=8.5,
            lid_gripper_offset_mm=2.8,
        )
        bravo._labware_catalog = type("Catalog", (), {
            "get_definition": lambda self, labware_id: plate if labware_id == "liddable-plate" else None,
            "list_definitions": lambda self: [plate],
        })()
        bravo.set_labware(5, "liddable-plate", is_lidded=False)
        bravo._deck.set_single(9, synthesize_lid_labware(Labware.from_definition(plate, is_lidded=True)))

        response = await server.execute_command(
            server.ExecuteCommandRequest(command="Relid Plate", lid_location=9, plate_location=5)
        )

        assert response["status"] == "completed"
        assert response["lid_location"] == 9
        assert response["plate_location"] == 5
        assert response["plate_to_relid"] == "384 Greiner 781091 PS uclear"
        assert response["current_source_labware"] is None
        assert response["current_plate_labware"] == "384 Greiner 781091 PS uclear"
    finally:
        server._bravo = previous_bravo


@pytest.mark.asyncio
async def test_execute_command_stack_plates_rejects_same_location():
    bravo = Bravo(mode="simulation")
    previous_bravo = server._bravo

    try:
        server._bravo = bravo
        plate = LabwareDefinition(
            id="plate",
            name="384 Greiner 781091 PS uclear",
            kind="sbs_plate",
            base_class="microplate",
            height_mm=14.4,
        )
        bravo._labware_catalog = type("Catalog", (), {
            "get_definition": lambda self, labware_id: plate if labware_id == "plate" else None,
            "list_definitions": lambda self: [plate],
        })()
        bravo.set_labware(3, "plate")

        with pytest.raises(RuntimeError, match="different source and base locations"):
            await server.execute_command(
                server.ExecuteCommandRequest(command="Stack Plates", base_location=3, source_location=3)
            )
    finally:
        server._bravo = previous_bravo


@pytest.mark.asyncio
async def test_liquid_classes_tip_filter_follows_head_state(monkeypatch):
    """Tips OFF -> list every class for (machine, head); tips ON -> narrow to
    the loaded tip; machine_id + head_type always stay strict so other
    devices/heads never leak in. An explicit ?tip_id narrows regardless."""
    bravo = Bravo(mode="simulation")
    bravo.connect()
    bravo.profile.head.head_type = HeadType.HT_96_D_200
    bravo.profile.connection.machine_id = "MACH-1"
    bravo.profile.head.default_tip_id = "lt_200ul"
    bravo.profile.head.teach_tip_id = "lt_250ul"  # teach tip must NOT drive the idle context

    calls: list[dict] = []

    def fake_list(*, machine_id=None, head_type=None, tip_id=None, tip_capacity_ul=None):
        calls.append({"machine_id": machine_id, "head_type": head_type, "tip_id": tip_id, "tip_capacity_ul": tip_capacity_ul})
        return [{"name": "stub"}]

    monkeypatch.setattr(server.liquid_classes_store, "list_liquid_classes", fake_list)
    previous_bravo = server._bravo
    try:
        server._bravo = bravo

        # Tips OFF: no tip narrowing, machine + head strict.
        bravo._tips_on_head = False
        calls.clear()
        res = await server.list_liquid_classes()
        assert res["context"]["tips_on"] is False
        assert res["context"]["machine_id"] == "MACH-1"
        assert res["context"]["head_type"] == "HT_96_D_200"
        assert res["context"]["tip_id"] is None
        assert calls[-1] == {"machine_id": "MACH-1", "head_type": "HT_96_D_200", "tip_id": None, "tip_capacity_ul": None}

        # Tips ON: narrow to the loaded tip (not the teach tip).
        bravo._tips_on_head = True
        bravo._tip_definition_id = "lt_200ul"
        calls.clear()
        res = await server.list_liquid_classes()
        assert res["context"]["tips_on"] is True
        assert res["context"]["tip_id"] == "lt_200ul"
        assert calls[-1]["machine_id"] == "MACH-1"
        assert calls[-1]["head_type"] == "HT_96_D_200"
        assert calls[-1]["tip_id"] == "lt_200ul"

        # Explicit ?tip_id narrows even with tips off.
        bravo._tips_on_head = False
        calls.clear()
        res = await server.list_liquid_classes(tip_id="st_30ul")
        assert calls[-1]["tip_id"] == "st_30ul"
        assert calls[-1]["machine_id"] == "MACH-1"
        assert calls[-1]["head_type"] == "HT_96_D_200"
    finally:
        server._bravo = previous_bravo
        bravo.disconnect()


