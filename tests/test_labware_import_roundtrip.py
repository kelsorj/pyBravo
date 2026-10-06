"""Labware imported from a registry export must survive the labware editor.

Two bugs dropped imported geometry without any error:

* the import script matched only ``Velocity11\\shared``, but Windows writes the
  key as ``Velocity11\\Shared``, so a real export imported nothing;
* seeding the editor store from the catalog snapshot left out well depth, the
  teachpoint-to-A1 offsets and the well pitch, and the next editor save wrote
  them back to the snapshot as 0. A zero pitch puts every well on top of A1.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from pybravo import labware_editor
from pybravo.deck.labware import LabwareDefinition

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "import_labware_from_registry.py"


def _load_import_script():
    spec = importlib.util.spec_from_file_location("import_labware_from_registry", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_registry_export_with_capitalised_shared_key_is_parsed():
    script = _load_import_script()
    text = (
        "Windows Registry Editor Version 5.00\n\n"
        "[HKEY_LOCAL_MACHINE\\SOFTWARE\\WOW6432Node\\Velocity11\\Shared\\Labware"
        "\\Labware_Entries\\Test 384 Plate]\n"
        '"NAME"="Test 384 Plate"\n'
        '"NUMBER_OF_WELLS"="384"\n'
        '"X_WELL_TO_WELL"="4.5"\n'
    )

    entries = script.parse_reg_labware(text)

    assert [e["NAME"] for e in entries] == ["Test 384 Plate"]
    assert entries[0]["X_WELL_TO_WELL"] == "4.5"


def test_editor_round_trip_keeps_well_geometry():
    definition = LabwareDefinition(
        id="test_384",
        name="Round Trip Test 384 Plate",
        kind="sbs_plate",
        wells=384,
        rows=16,
        cols=24,
        well_depth_mm=9.6,
        offset_x_mm=2.25,
        offset_y_mm=2.25,
        spacing_x_mm=4.5,
        spacing_y_mm=4.5,
    )

    item = labware_editor._definition_to_editor_type(definition)
    restored = labware_editor._editor_type_to_definition(item)

    assert restored.well_depth_mm == 9.6
    assert restored.offset_x_mm == 2.25
    assert restored.offset_y_mm == 2.25
    assert restored.spacing_x_mm == 4.5
    assert restored.spacing_y_mm == 4.5
