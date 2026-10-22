"""Provenance: imported configuration is marked, and marked again once edited here.

The UI colours profiles, labware, liquid classes and accessories by origin:
"registry_import" (from a registry export, unchanged), "registry_import_modified"
(imported, then edited in pyBravo) or local.
"""
from __future__ import annotations

import os
import shutil
import time

import pytest
import yaml

from pybravo import labware_editor, liquid_classes
from pybravo.deck.labware import LabwareDefinition
from pybravo.profile.profile import BravoProfile
from pybravo.profile.reg_import import reg_to_profile
from pybravo.web import server

REG = (
    "Windows Registry Editor Version 5.00\n\n"
    "[HKEY_LOCAL_MACHINE\\SOFTWARE\\WOW6432Node\\Velocity11\\Bravo2\\Profiles\\Lab Bravo]\n"
    '"Approach height"="10"\n'
)


def test_registry_import_records_its_origin_and_survives_a_save(tmp_path):
    profile, _ = reg_to_profile(REG)
    origin = profile.extra["origin"]
    assert origin["kind"] == "registry_import"
    assert origin["source_name"] == "Lab Bravo"
    assert origin["imported_at"]

    path = tmp_path / "lab.yaml"
    profile.save(path)
    assert BravoProfile.load(path).extra["origin"]["kind"] == "registry_import"


def _import_as_active(tmp_path, monkeypatch, name="imported"):
    profile, _ = reg_to_profile(REG)
    profile.name = name
    path = tmp_path / f"{name}.yaml"
    profile.save(path)
    monkeypatch.setattr(server, "_profile_dir", tmp_path)
    monkeypatch.setattr(server, "_profile_path", path)
    monkeypatch.setattr(server, "_bravo", server.Bravo(profile=path))
    return path


def test_profile_origin_tells_imported_edited_and_local_apart(tmp_path):
    profile, _ = reg_to_profile(REG)
    imported = tmp_path / "imported.yaml"
    profile.save(imported)
    assert server._profile_origin(imported) == "registry_import"

    edited = BravoProfile.load(imported)
    edited.safety.approach_height += 1.0
    edited.save(imported)
    assert server._profile_origin(imported) == "registry_import_modified"

    local = tmp_path / "local.yaml"
    BravoProfile.default().save(local)
    assert server._profile_origin(local) == "local"


@pytest.mark.asyncio
async def test_a_real_edit_right_after_import_is_marked(tmp_path, monkeypatch):
    path = _import_as_active(tmp_path, monkeypatch)
    assert server._profile_origin(path) == "registry_import"

    # Within the same second as the import: file times cannot tell this apart.
    res = await server.update_profile(server.ProfileUpdateRequest(approach_height=12.5))
    assert res["saved"]
    assert server._profile_origin(path) == "registry_import_modified"


@pytest.mark.asyncio
async def test_a_save_without_changes_keeps_the_import_mark(tmp_path, monkeypatch):
    path = _import_as_active(tmp_path, monkeypatch)

    res = await server.update_profile(server.ProfileUpdateRequest())
    assert res["saved"]
    later = time.time() + 3600
    os.utime(path, (later, later))
    assert server._profile_origin(path) == "registry_import"


def test_a_copy_of_an_unchanged_import_keeps_the_mark(tmp_path):
    profile, _ = reg_to_profile(REG)
    original = tmp_path / "original.yaml"
    profile.save(original)

    copied = tmp_path / "copy.yaml"
    shutil.copy(original, copied)
    later = time.time() + 3600
    os.utime(copied, (later, later))
    assert server._profile_origin(copied) == "registry_import"

    renamed = BravoProfile.load(copied)
    renamed.name = "copy"
    renamed.save(copied)
    assert server._profile_origin(copied) == "registry_import"


def test_labware_origin_round_trips_and_an_edit_marks_it(tmp_path, monkeypatch):
    monkeypatch.setenv("PYBRAVO_LABWARE_EDITOR_PATH", str(tmp_path / "editor.yaml"))
    monkeypatch.setenv("PYBRAVO_LABWARE_SNAPSHOT_PATH", str(tmp_path / "snapshot.yaml"))
    definition = LabwareDefinition(id="lw-1", name="Imported Plate", kind="sbs_plate", wells=96,
                                   origin="registry_import")

    item = labware_editor._definition_to_editor_type(definition)
    assert labware_editor._editor_type_to_definition(item).origin == "registry_import"

    labware_editor.save_store({"version": 1, "labware_types": [item], "labware_classes": []})
    edited = labware_editor.patch_type("lw-1", {"description": "changed here"})
    assert edited["origin"] == "registry_import_modified"

    snapshot = yaml.safe_load((tmp_path / "snapshot.yaml").read_text(encoding="utf-8"))
    assert snapshot["labware"][0]["origin"] == "registry_import_modified"


def test_liquid_class_edit_marks_an_imported_class(tmp_path, monkeypatch):
    monkeypatch.setenv("PYBRAVO_LIQUID_CLASS_STORE_PATH", str(tmp_path / "liquid.yaml"))
    created = liquid_classes.create_liquid_class({
        "name": "Water", "machine_id": "SIM_BRAVO", "head_type": "HT_96_D_70",
        "tip_capacity_ul": 70, "origin": "registry_import",
    })
    assert created["origin"] == "registry_import"

    edited = liquid_classes.patch_liquid_class(created["liquid_class_id"], {"description": "tuned"})
    assert edited["origin"] == "registry_import_modified"

    local = liquid_classes.create_liquid_class({
        "name": "Buffer", "machine_id": "SIM_BRAVO", "head_type": "HT_96_D_70", "tip_capacity_ul": 70,
    })
    assert local["origin"] == ""
