"""Catalog editing and reimport must retain recorded tipbox metadata."""

import json
from pathlib import Path

import pytest
import yaml

from pybravo import labware_editor
from pybravo.deck.labware import LabwareDefinition, _read_labware_snapshot
from scripts.import_labware_from_registry import import_labware_reg

_ROOT = Path(__file__).resolve().parents[1]
_LIQUID_BASE_CLASSES = {"microplate", "filter_plate", "reservoir", "wash_station", "Tip Wash Station"}


def test_checked_in_liquid_catalog_rows_have_numeric_dead_volumes_with_a_valid_status():
    """Every shipped liquid plate is reviewable; accessories have no invented value.

    The snapshot and the editor YAML are written by the labware editor, where a
    scientist marks a dead volume ``reviewed`` on purpose (editing the value
    returns it to ``placeholder``), so those two stores may carry either status.
    The overlay and the Mongo seed are static files the editor never writes;
    they ship placeholders only.
    """
    sources = (
        ("config/labware_catalog.snapshot.yaml", "labware", False, True),
        ("config/labware_catalog.d/96am.yaml", "labware", False, False),
        ("config/labware_editor.yaml", "labware_types", True, True),
        ("config/labware_types.seed.json", None, True, False),
    )
    for filename, key, nested, editor_written in sources:
        allowed_statuses = {"placeholder", "reviewed"} if editor_written else {"placeholder"}
        text = (_ROOT / filename).read_text()
        document = json.loads(text) if filename.endswith(".json") else yaml.safe_load(text)
        rows = document if key is None else document[key]
        for row in rows:
            geometry = (row.get("well_dimensions_mm") or {}) if nested else row
            dead_volume = geometry.get("dead_volume_ul")
            if row.get("base_class") in _LIQUID_BASE_CLASSES:
                assert isinstance(dead_volume, (int, float)) and dead_volume > 0, (
                    filename, row["name"]
                )
                assert geometry.get("dead_volume_status") in allowed_statuses, (filename, row["name"])
                capacity = geometry.get("volume_ul" if nested else "well_volume_ul")
                if capacity and capacity > 0:
                    assert dead_volume < capacity, (filename, row["name"])
            else:
                assert dead_volume is None, (filename, row["name"])


def test_mongo_missing_dead_volume_is_placeholder_but_explicit_reviewed_value_survives():
    base = {"labware_type_id": "plate", "name": "Example plate", "kind": "sbs_plate",
            "base_class": "microplate", "wells": 384,
            "well_dimensions_mm": {"volume_ul": 130}}
    missing = LabwareDefinition.from_mongo(base)
    assert missing.dead_volume_ul == 6.5
    assert missing.dead_volume_status == "placeholder"
    assert missing.to_summary()["dead_volume_ul"] == 6.5

    base["well_dimensions_mm"]["dead_volume_status"] = "reviewed"
    assert LabwareDefinition.from_mongo(base).dead_volume_status == "placeholder"
    del base["well_dimensions_mm"]["dead_volume_status"]

    base["wells"] = 1536
    base["well_dimensions_mm"] = {"volume_ul": 5.5}
    assert LabwareDefinition.from_mongo(base).dead_volume_ul == 0.3
    base["wells"] = 384
    base["well_dimensions_mm"] = {"volume_ul": 130}

    base["well_dimensions_mm"].update(dead_volume_ul=8.0, dead_volume_status="reviewed")
    reviewed = LabwareDefinition.from_mongo(base)
    assert reviewed.dead_volume_ul == 8.0
    assert reviewed.dead_volume_status == "reviewed"

    base["well_dimensions_mm"] = {"volume_ul": 130, "dead_volume_ul": None}
    assert LabwareDefinition.from_mongo(base).dead_volume_ul is None
    base["base_class"] = "tip_box"
    base["well_dimensions_mm"] = {"volume_ul": 0}
    assert LabwareDefinition.from_mongo(base).dead_volume_ul is None


@pytest.mark.parametrize("invalid", ["not-a-number", float("nan"), float("inf"), -1, True])
def test_invalid_dead_volume_downgrades_reviewed_catalog_row(invalid, caplog, tmp_path):
    mongo_row = {"name": "Example plate", "kind": "sbs_plate", "base_class": "microplate",
                 "wells": 96, "well_dimensions_mm": {
                     "volume_ul": 100, "dead_volume_ul": invalid,
                     "dead_volume_status": "reviewed"}}
    from_mongo = LabwareDefinition.from_mongo(mongo_row)
    assert from_mongo.dead_volume_ul is None
    assert from_mongo.dead_volume_status == "placeholder"

    snapshot_path = tmp_path / "snapshot.yaml"
    snapshot_path.write_text(yaml.safe_dump({"labware": [{
        "id": "example", "name": "Example plate", "kind": "sbs_plate",
        "base_class": "microplate", "wells": 96,
        "dead_volume_ul": invalid, "dead_volume_status": "reviewed",
    }]}))
    loaded = _read_labware_snapshot(snapshot_path)[0]
    assert loaded.dead_volume_ul is None
    assert loaded.dead_volume_status == "placeholder"
    editor_row = labware_editor._normalize_type_item({
        "labware_type_id": "example", "name": "Example plate", "kind": "sbs_plate",
        "base_class": "microplate", "wells": 96,
        "well_dimensions_mm": {"dead_volume_ul": invalid, "dead_volume_status": "reviewed"},
    })
    assert editor_row["well_dimensions_mm"]["dead_volume_ul"] is None
    assert editor_row["well_dimensions_mm"]["dead_volume_status"] == "placeholder"
    assert "Ignoring invalid dead_volume_ul" in caplog.text


def test_editor_exposes_missing_placeholder_and_preserves_reviewed_edit(monkeypatch, tmp_path):
    editor_path = tmp_path / "editor.yaml"
    snapshot_path = tmp_path / "snapshot.yaml"
    monkeypatch.setenv("PYBRAVO_LABWARE_EDITOR_PATH", str(editor_path))
    monkeypatch.setenv("PYBRAVO_LABWARE_SNAPSHOT_PATH", str(snapshot_path))
    monkeypatch.setattr(labware_editor, "_mongo_enabled", lambda: False)
    editor_path.write_text(yaml.safe_dump({"labware_types": [
        {"labware_type_id": "plate", "name": "Example plate", "kind": "sbs_plate",
         "base_class": "microplate", "wells": 384, "well_dimensions_mm": {"volume_ul": 130}},
        {"labware_type_id": "tips", "name": "Tips", "kind": "sbs_plate",
         "base_class": "tip_box", "wells": 384, "well_dimensions_mm": {}},
    ]}))

    listed = {row["labware_type_id"]: row for row in labware_editor.list_types()}
    assert listed["plate"]["well_dimensions_mm"]["dead_volume_ul"] == 6.5
    assert listed["plate"]["well_dimensions_mm"]["dead_volume_status"] == "placeholder"
    assert "dead_volume_ul" not in listed["tips"]["well_dimensions_mm"]

    labware_editor.patch_type("plate", {"well_dimensions_mm": {
        "dead_volume_ul": 7.0, "dead_volume_status": "reviewed"}})
    saved = yaml.safe_load(snapshot_path.read_text())["labware"]
    plate = next(row for row in saved if row["id"] == "plate")
    assert plate["dead_volume_ul"] == 7.0
    assert plate["dead_volume_status"] == "reviewed"


def test_editor_mongo_list_shows_placeholder_for_legacy_plate(monkeypatch, tmp_path):
    mongo_doc = {
        "labware_type_id": "legacy-plate", "name": "Legacy plate", "kind": "sbs_plate",
        "base_class": "microplate", "wells": 96,
        "well_dimensions_mm": {"volume_ul": 200},
    }

    class Collection:
        def __init__(self, docs):
            self.docs = docs

        def find(self, query):
            return self.docs

    class Client:
        def close(self):
            pass

    monkeypatch.setattr(labware_editor, "_mongo_enabled", lambda: True)
    monkeypatch.setattr(labware_editor, "_mongo_collections", lambda: (
        Client(), Collection([mongo_doc]), Collection([]),
    ))
    monkeypatch.setenv("PYBRAVO_LABWARE_EDITOR_PATH", str(tmp_path / "editor.yaml"))
    monkeypatch.setenv("PYBRAVO_LABWARE_SNAPSHOT_PATH", str(tmp_path / "snapshot.yaml"))

    row = labware_editor.list_types()[0]
    assert row["well_dimensions_mm"]["dead_volume_ul"] == 10.0
    assert row["well_dimensions_mm"]["dead_volume_status"] == "placeholder"


@pytest.mark.parametrize(
    "geometry",
    [
        {"rows": 16, "cols": 24, "spacing_x_mm": 4.5, "spacing_y_mm": 4.5,
         "offset_x_mm": 11.25, "offset_y_mm": 12.75, "well_depth_mm": 20.5},
        {"rows": 0, "cols": 0, "spacing_x_mm": 0, "spacing_y_mm": 0,
         "offset_x_mm": 0, "offset_y_mm": 0, "well_depth_mm": 0},
    ],
    ids=["recorded_geometry", "unknown_geometry"],
)
def test_editor_seed_patch_and_reload_preserve_tipbox_metadata(monkeypatch, tmp_path, geometry):
    editor_path = tmp_path / "editor.yaml"
    snapshot_path = tmp_path / "snapshot.yaml"
    monkeypatch.setenv("PYBRAVO_LABWARE_EDITOR_PATH", str(editor_path))
    monkeypatch.setenv("PYBRAVO_LABWARE_SNAPSHOT_PATH", str(snapshot_path))
    monkeypatch.setattr(labware_editor, "_mongo_enabled", lambda: False)
    definition = LabwareDefinition(
        id="synthetic-tipbox", name="Synthetic rack", kind="tip_box", wells=384,
        tip_definition_id="synthetic-tip", supported_tip_ids=["synthetic-tip", "second-tip"],
        **geometry,
    )
    snapshot_path.write_text(yaml.safe_dump({"labware": [definition.to_summary()]}))

    # The initial editor seed and subsequent unrelated edit both rewrite the snapshot.
    labware_editor.load_store()
    labware_editor.patch_type(definition.id, {"description": "Reviewed label"})
    reloaded = labware_editor.get_type(definition.id)
    saved = yaml.safe_load(snapshot_path.read_text())["labware"][0]

    assert saved["description"] == "Reviewed label"
    assert {key: saved[key] for key in geometry} == geometry
    assert saved["tip_definition_id"] == definition.tip_definition_id
    assert saved["supported_tip_ids"] == definition.supported_tip_ids
    assert reloaded["tip_definition_id"] == definition.tip_definition_id
    assert reloaded["supported_tip_ids"] == definition.supported_tip_ids
    assert reloaded["well_dimensions_mm"]["rows"] == geometry["rows"]
    assert reloaded["well_dimensions_mm"]["cols"] == geometry["cols"]


@pytest.mark.parametrize(
    "links",
    [
        {"tip_definition_id": "curated-tip", "supported_tip_ids": ["curated-tip", "other-tip"]},
        {"tip_definition_id": "curated-tip", "supported_tip_ids": []},
        {"tip_definition_id": "", "supported_tip_ids": ["curated-tip"]},
    ],
    ids=["both_links", "primary_only", "supported_only"],
)
def test_registry_reimport_keeps_curated_tip_links_while_updating_source_fields(tmp_path, links):
    registry_path = _write_registry(tmp_path)
    output_path = tmp_path / "snapshot.yaml"
    old = {"name": "Synthetic rack", "height_mm": 1, **links}
    output_path.write_text(yaml.safe_dump({"labware": [old]}))

    items, new_count, updated_count = import_labware_reg(registry_path, output_path=output_path)

    assert (new_count, updated_count) == (0, 1)
    assert {key: items[0][key] for key in links} == links
    assert items[0]["height_mm"] == 25.0
    assert items[0]["spacing_x_mm"] == 4.5
    assert yaml.safe_load(output_path.read_text())["labware"] == items


def test_registry_replace_does_not_copy_links_from_discarded_catalog(tmp_path):
    registry_path = _write_registry(tmp_path)
    output_path = tmp_path / "snapshot.yaml"
    output_path.write_text(yaml.safe_dump({"labware": [{
        "name": "Synthetic rack", "tip_definition_id": "old-tip", "supported_tip_ids": ["old-tip"],
    }]}))

    items, new_count, updated_count = import_labware_reg(registry_path, output_path=output_path, merge=False)

    assert (new_count, updated_count) == (1, 0)
    assert items[0]["tip_definition_id"] == ""
    assert items[0]["supported_tip_ids"] == []


def test_registry_reimport_keeps_reviewed_dead_volume(tmp_path):
    registry_path = _write_registry(tmp_path, base_class="1", well_volume_ul=130)
    output_path = tmp_path / "snapshot.yaml"
    prior = import_labware_reg(registry_path, merge=False)[0][0]
    prior.update(dead_volume_ul=7.0, dead_volume_status="reviewed")
    output_path.write_text(yaml.safe_dump({"labware": [prior]}))

    items, new_count, updated_count = import_labware_reg(registry_path, output_path=output_path)

    assert (new_count, updated_count) == (0, 1)
    assert items[0]["dead_volume_ul"] == 7.0
    assert items[0]["dead_volume_status"] == "reviewed"
    assert yaml.safe_load(output_path.read_text())["labware"][0]["dead_volume_ul"] == 7.0

    imported, _, _ = import_labware_reg(registry_path, merge=False)
    assert imported[0]["dead_volume_ul"] == 6.5
    assert imported[0]["dead_volume_status"] == "placeholder"


def test_registry_geometry_change_demotes_reviewed_dead_volume(tmp_path):
    registry_path = _write_registry(tmp_path, base_class="1", well_volume_ul=130)
    output_path = tmp_path / "snapshot.yaml"
    prior = import_labware_reg(registry_path, merge=False)[0][0]
    prior.update(dead_volume_ul=7.0, dead_volume_status="reviewed")
    output_path.write_text(yaml.safe_dump({"labware": [prior]}))
    _write_registry(tmp_path, base_class="1", well_volume_ul=200)

    items, _, _ = import_labware_reg(registry_path, output_path=output_path)

    assert items[0]["dead_volume_ul"] == 7.0
    assert items[0]["dead_volume_status"] == "placeholder"
    assert items[0]["well_volume_ul"] == 200.0


def test_registry_reclassification_to_tipbox_clears_dead_volume(tmp_path):
    registry_path = _write_registry(tmp_path, base_class="1", well_volume_ul=130)
    output_path = tmp_path / "snapshot.yaml"
    prior = import_labware_reg(registry_path, merge=False)[0][0]
    prior.update(dead_volume_ul=7.0, dead_volume_status="reviewed")
    output_path.write_text(yaml.safe_dump({"labware": [prior]}))
    _write_registry(tmp_path, base_class="6", well_volume_ul=0)

    items, _, _ = import_labware_reg(registry_path, output_path=output_path)

    assert items[0]["base_class"] == "tip_box"
    assert "dead_volume_ul" not in items[0]
    assert "dead_volume_status" not in items[0]


def _write_registry(tmp_path, *, base_class="6", well_volume_ul=None):
    path = tmp_path / "synthetic.reg"
    path.write_text(
        r"[HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Velocity11\shared\Labware\Labware_Entries\Synthetic rack]"
        f'\n"NAME"="Synthetic rack"\n"BASE_CLASS"="{base_class}"\n"NUMBER_OF_WELLS"="384"'
        '\n"THICKNESS"="25"\n"X_WELL_TO_WELL"="4.5"\n"Y_WELL_TO_WELL"="4.5"\n'
        + (f'"WELL_TIP_VOLUME"="{well_volume_ul}"\n' if well_volume_ul is not None else '')
    )
    return path
