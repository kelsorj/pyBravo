"""Unit tests for the vendor .pro file importer.

Covers:
- parse_pro_file() against the synthetic tests/fixtures/example_protocol.pro fixture
- _parse_head_mode_element() subset-type and subset-config decoding
- _extract_js_variables() numeric, string, float, and empty-array literals
- _extract_quadrant_mapping() pattern extraction and None for plain scripts
- _simplify_steps() collapsing tips/stamp, set_head_mode removal
- import_pro() / protocol_to_workflow() end-to-end deck and step structure
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from pybravo.workflow.legacy_protocol_import import (
    HeadModeInfo,
    PlateDefinition,
    _extract_js_variables,
    _extract_quadrant_mapping,
    _parse_head_mode_element,
    _simplify_steps,
    import_pro,
    parse_pro_file,
    protocol_to_workflow,
)

# ---------------------------------------------------------------------------
# Fixture path
# ---------------------------------------------------------------------------

PRO_FILE = Path(__file__).parent / "fixtures" / "example_protocol.pro"


# ---------------------------------------------------------------------------
# _parse_head_mode_element
# ---------------------------------------------------------------------------

class TestParseHeadModeElement:
    def _make_el(self, **attrs: str) -> ET.Element:
        el = ET.Element("PipetteHeadMode")
        for k, v in attrs.items():
            el.set(k, v)
        return el

    def test_subset_type_0_is_all_barrels(self):
        el = self._make_el(SubsetType="0", SubsetConfig="0")
        result = _parse_head_mode_element(el)
        assert result.subset_type == "all_barrels"

    def test_subset_type_1_is_row(self):
        el = self._make_el(SubsetType="1", SubsetConfig="0")
        result = _parse_head_mode_element(el)
        assert result.subset_type == "row"

    def test_subset_type_2_is_column(self):
        el = self._make_el(SubsetType="2", SubsetConfig="0")
        result = _parse_head_mode_element(el)
        assert result.subset_type == "column"

    def test_subset_type_3_is_single_barrel(self):
        el = self._make_el(SubsetType="3", SubsetConfig="0")
        result = _parse_head_mode_element(el)
        assert result.subset_type == "single_barrel"

    def test_subset_type_4_is_rectangle(self):
        el = self._make_el(SubsetType="4", SubsetConfig="0")
        result = _parse_head_mode_element(el)
        assert result.subset_type == "rectangle"

    def test_unknown_subset_type_falls_back_to_all_barrels(self):
        el = self._make_el(SubsetType="99", SubsetConfig="0")
        result = _parse_head_mode_element(el)
        assert result.subset_type == "all_barrels"

    def test_subset_config_0_is_back_left(self):
        el = self._make_el(SubsetType="0", SubsetConfig="0")
        result = _parse_head_mode_element(el)
        assert result.subset_config == "back_left"

    def test_subset_config_1_is_back_right(self):
        el = self._make_el(SubsetType="0", SubsetConfig="1")
        result = _parse_head_mode_element(el)
        assert result.subset_config == "back_right"

    def test_subset_config_2_is_front_left(self):
        el = self._make_el(SubsetType="0", SubsetConfig="2")
        result = _parse_head_mode_element(el)
        assert result.subset_config == "front_left"

    def test_subset_config_3_is_front_right(self):
        el = self._make_el(SubsetType="0", SubsetConfig="3")
        result = _parse_head_mode_element(el)
        assert result.subset_config == "front_right"

    def test_unknown_subset_config_falls_back_to_back_left(self):
        el = self._make_el(SubsetType="0", SubsetConfig="99")
        result = _parse_head_mode_element(el)
        assert result.subset_config == "back_left"

    def test_channels_and_row_column_count_parsed(self):
        el = self._make_el(
            SubsetType="1", SubsetConfig="2",
            Channels="8", RowCount="1", ColumnCount="12", TipType="3",
        )
        result = _parse_head_mode_element(el)
        assert result.channels == 8
        assert result.row_count == 1
        assert result.column_count == 12
        assert result.tip_type == 3

    def test_missing_attributes_default_to_zero(self):
        el = ET.Element("PipetteHeadMode")
        result = _parse_head_mode_element(el)
        assert result.channels == 0
        assert result.row_count == 0
        assert result.column_count == 0
        assert result.subset_type == "all_barrels"
        assert result.subset_config == "back_left"

    def test_to_dict_returns_expected_keys(self):
        el = self._make_el(
            SubsetType="1", SubsetConfig="2",
            Channels="8", RowCount="1", ColumnCount="12",
        )
        d = _parse_head_mode_element(el).to_dict()
        assert d["subset_type"] == "row"
        assert d["subset_config"] == "front_left"
        assert d["row_count"] == 1
        assert d["column_count"] == 12


# ---------------------------------------------------------------------------
# _extract_js_variables
# ---------------------------------------------------------------------------

class TestExtractJsVariables:
    def test_integer_variable(self):
        result = _extract_js_variables("var numPlates = 4;")
        assert result == {"numPlates": 4}
        assert isinstance(result["numPlates"], int)

    def test_float_variable(self):
        result = _extract_js_variables("var dispVol = 3.5;")
        assert result == {"dispVol": 3.5}
        assert isinstance(result["dispVol"], float)

    def test_double_quoted_string_variable(self):
        result = _extract_js_variables('var methodID = "run-001";')
        assert result == {"methodID": "run-001"}
        assert isinstance(result["methodID"], str)

    def test_single_quoted_string_variable(self):
        result = _extract_js_variables("var label = 'hello';")
        assert result == {"label": "hello"}

    def test_empty_array_variable(self):
        result = _extract_js_variables("var srcBC = [];")
        assert result == {"srcBC": []}
        assert isinstance(result["srcBC"], list)

    def test_multiple_variables_in_one_script(self):
        script = "var plateCounter = 1; var srcBC = []; var dispVol = 3.5; var numPlates = 4;"
        result = _extract_js_variables(script)
        assert result["plateCounter"] == 1
        assert result["srcBC"] == []
        assert result["dispVol"] == 3.5
        assert result["numPlates"] == 4

    def test_empty_script_returns_empty_dict(self):
        result = _extract_js_variables("")
        assert result == {}

    def test_script_with_no_var_declarations(self):
        result = _extract_js_variables("task.Volume = dispVol; print('done');")
        assert result == {}

    def test_integer_zero_variable(self):
        result = _extract_js_variables("var rescan = 0;")
        assert result == {"rescan": 0}
        assert isinstance(result["rescan"], int)

    def test_multiline_script(self):
        script = (
            "var loopCounter1;\n"
            "var dispVol = 3.5;\n"
            "var numPlates = 4;\n"
        )
        result = _extract_js_variables(script)
        assert result["dispVol"] == 3.5
        assert result["numPlates"] == 4
        # loopCounter1 has no value, should not appear
        assert "loopCounter1" not in result


# ---------------------------------------------------------------------------
# _extract_quadrant_mapping
# ---------------------------------------------------------------------------

class TestExtractQuadrantMapping:
    def test_four_plate_mapping_returns_four_entries(self):
        script = (
            "if(plateCounter == 1) task.Wellselection = [[1,1]];"
            " if(plateCounter == 2) task.Wellselection = [[1,2]];"
            " if(plateCounter == 3) task.Wellselection = [[2,1]];"
            " if(plateCounter == 4) task.Wellselection = [[2,2]];"
        )
        result = _extract_quadrant_mapping(script)
        assert result is not None
        assert len(result) == 4

    def test_plate_counter_1_maps_to_first_quadrant(self):
        script = "if(plateCounter == 1) task.Wellselection = [[1,1]];"
        result = _extract_quadrant_mapping(script)
        assert result is not None
        # [[col, row]] -> stored as (row, col) = (1, 1)
        assert result[0] == (1, 1)

    def test_script_without_quadrant_mapping_returns_none(self):
        result = _extract_quadrant_mapping("task.Volume = dispVol; print('hello');")
        assert result is None

    def test_empty_script_returns_none(self):
        result = _extract_quadrant_mapping("")
        assert result is None

    def test_full_four_quadrant_values(self):
        # From real .pro file: [[1,1]], [[1,2]], [[2,1]], [[2,2]]
        script = (
            "if(plateCounter == 1) task.Wellselection = [[1,1]];"
            " if(plateCounter == 2) task.Wellselection = [[1,2]];"
            " if(plateCounter == 3) task.Wellselection = [[2,1]];"
            " if(plateCounter == 4) task.Wellselection = [[2,2]];"
        )
        result = _extract_quadrant_mapping(script)
        assert result is not None
        # Entries: col=1,row=1 -> (1,1); col=1,row=2 -> (2,1); col=2,row=1 -> (1,2); col=2,row=2 -> (2,2)
        assert result[0] == (1, 1)
        assert result[1] == (2, 1)
        assert result[2] == (1, 2)
        assert result[3] == (2, 2)


# ---------------------------------------------------------------------------
# _simplify_steps
# ---------------------------------------------------------------------------

class TestSimplifySteps:
    def _tips_on(self, box: str, with_script: bool = True) -> dict:
        step = {"action": "tips_on", "from": box}
        if with_script:
            step["_script"] = "if (plateCounter != 1) { task.skip(); }"
        return step

    def _tips_off(self, box: str, with_script: bool = True) -> dict:
        step = {"action": "tips_off", "to": box}
        if with_script:
            step["_script"] = "if (plateCounter != 1) { task.skip(); }"
        return step

    def _aspirate(self, plate: str = "Source", volume: str = "5") -> dict:
        return {"action": "aspirate", "from": plate, "volume": volume}

    def _dispense(self, plate: str = "Dest", volume: str = "5") -> dict:
        return {"action": "dispense", "to": plate, "volume": volume}

    def test_four_conditional_tips_on_collapse_to_mount_tips(self):
        raw = [
            self._tips_on("Tip1"),
            self._tips_on("Tip2"),
            self._tips_on("Tip3"),
            self._tips_on("Tip4"),
        ]
        result = _simplify_steps(raw)
        assert len(result) == 1
        assert result[0]["action"] == "mount tips"
        assert result[0]["from"] == ["Tip1", "Tip2", "Tip3", "Tip4"]

    def test_four_conditional_tips_off_collapse_to_discard_tips(self):
        raw = [
            self._tips_off("Tip1"),
            self._tips_off("Tip2"),
            self._tips_off("Tip3"),
            self._tips_off("Tip4"),
        ]
        result = _simplify_steps(raw)
        assert len(result) == 1
        assert result[0]["action"] == "discard tips"
        assert result[0]["to"] == ["Tip1", "Tip2", "Tip3", "Tip4"]

    def test_aspirate_dispense_pair_merges_into_stamp(self):
        raw = [self._aspirate(), self._dispense()]
        result = _simplify_steps(raw)
        assert len(result) == 1
        stamp = result[0]
        assert stamp["action"] == "stamp"
        assert stamp["from"] == "Source"
        assert stamp["to"] == "Dest"
        assert stamp["volume"] == "5"

    def test_stamp_carries_aspirate_params(self):
        aspirate = {
            "action": "aspirate",
            "from": "Source",
            "volume": "dispVol",
            "pre_aspirate_uL": 4.0,
            "liquid_class": "DMSO",
            "distance_from_bottom_mm": 0.1,
        }
        raw = [aspirate, self._dispense()]
        result = _simplify_steps(raw)
        stamp = result[0]
        assert stamp["aspirate"]["pre_aspirate_uL"] == 4.0
        assert stamp["aspirate"]["liquid_class"] == "DMSO"
        assert stamp["aspirate"]["distance_from_bottom_mm"] == 0.1

    def test_stamp_carries_dispense_params(self):
        dispense = {
            "action": "dispense",
            "to": "Dest",
            "volume": "5",
            "blowout_uL": 1.0,
            "dynamic_tip_retraction_mm": 0.05,
        }
        raw = [self._aspirate(), dispense]
        result = _simplify_steps(raw)
        stamp = result[0]
        assert stamp["dispense"]["blowout_uL"] == 1.0
        assert stamp["dispense"]["dynamic_tip_retraction_mm"] == 0.05

    def test_stamp_carries_quadrant_mapping_from_dispense(self):
        dispense = {
            "action": "dispense",
            "to": "Dest",
            "volume": "5",
            "_quadrant_mapping": ["B2", "C2", "B3", "C3"],
        }
        raw = [self._aspirate(), dispense]
        result = _simplify_steps(raw)
        stamp = result[0]
        assert stamp["quadrant_mapping"] == ["B2", "C2", "B3", "C3"]

    def test_private_underscore_keys_stripped_from_passthrough_steps(self):
        raw = [{"action": "tips_on", "from": "Tip1", "_script": "foo"}]
        # tips_on with script but no adjacent tips_on → collapses, no _script
        result = _simplify_steps(raw)
        assert "_script" not in result[0]

    def test_set_head_mode_steps_are_removed(self):
        raw = [
            {"action": "set_head_mode", "subset_type": "all_barrels"},
            self._aspirate(),
            self._dispense(),
        ]
        result = _simplify_steps(raw)
        assert all(s["action"] != "set_head_mode" for s in result)

    def test_tips_on_without_script_passes_through_unchanged(self):
        raw = [{"action": "tips_on", "from": "Tip1"}]
        result = _simplify_steps(raw)
        assert len(result) == 1
        assert result[0] == {"action": "tips_on", "from": "Tip1"}

    def test_tips_off_without_script_passes_through_unchanged(self):
        raw = [{"action": "tips_off", "to": "Tip1"}]
        result = _simplify_steps(raw)
        assert len(result) == 1
        assert result[0] == {"action": "tips_off", "to": "Tip1"}

    def test_orphan_aspirate_at_end_passes_through(self):
        # Aspirate with no following dispense → treated as passthrough
        raw = [self._aspirate()]
        result = _simplify_steps(raw)
        assert len(result) == 1
        assert result[0]["action"] == "aspirate"

    def test_empty_input_returns_empty_list(self):
        assert _simplify_steps([]) == []

    def test_full_sequence_from_real_file(self):
        """End-to-end: set_head_mode + 4 tips_on + 2 asp/disp pairs + 4 tips_off."""
        raw = [
            {"action": "set_head_mode", "subset_type": "all_barrels", "subset_config": "back_left", "row_count": 16, "column_count": 24},
            self._tips_on("Tip1"),
            self._tips_on("Tip2"),
            self._tips_on("Tip3"),
            self._tips_on("Tip4"),
            {"action": "aspirate", "from": "Source Plates", "volume": "dispVol", "pre_aspirate_uL": 4.0},
            {"action": "dispense", "to": "Dest Plate 1", "volume": "dispVol", "blowout_uL": 1.0, "_quadrant_mapping": ["B2", "C2", "B3", "C3"]},
            {"action": "aspirate", "from": "Source Plates", "volume": "dispVol"},
            {"action": "dispense", "to": "Dest Plate 2", "volume": "5", "_quadrant_mapping": ["B2", "C2", "B3", "C3"]},
            self._tips_off("Tip1"),
            self._tips_off("Tip2"),
            self._tips_off("Tip3"),
            self._tips_off("Tip4"),
        ]
        result = _simplify_steps(raw)
        actions = [s["action"] for s in result]
        assert actions == ["mount tips", "stamp", "stamp", "discard tips"]

    def test_two_stamps_have_correct_targets(self):
        raw = [
            self._tips_on("Tip1"),
            {"action": "aspirate", "from": "Source Plates", "volume": "dispVol"},
            {"action": "dispense", "to": "Dest Plate 1", "volume": "dispVol"},
            {"action": "aspirate", "from": "Source Plates", "volume": "dispVol"},
            {"action": "dispense", "to": "Dest Plate 2", "volume": "5"},
            self._tips_off("Tip1"),
        ]
        result = _simplify_steps(raw)
        stamps = [s for s in result if s["action"] == "stamp"]
        assert len(stamps) == 2
        assert stamps[0]["to"] == "Dest Plate 1"
        assert stamps[1]["to"] == "Dest Plate 2"


# ---------------------------------------------------------------------------
# parse_pro_file — integration tests against tests/fixtures/example_protocol.pro
# ---------------------------------------------------------------------------

class TestParseProFile:
    @pytest.fixture(scope="class")
    def protocol(self):
        return parse_pro_file(PRO_FILE)

    @pytest.fixture(scope="class")
    def pipette(self, protocol):
        return next(p for p in protocol.main if p.is_pipette_process)

    # --- file-level metadata ---

    def test_protocol_name_derived_from_filename(self, protocol):
        assert protocol.name == "example_protocol"

    def test_description_read_from_file_info(self, protocol):
        assert protocol.description == (
            "Stamp a 96-well source plate into two 384-well assay plates."
        )

    def test_device_file_read_from_file_info(self, protocol):
        assert protocol.device_file == "ExampleDeck.dev"

    def test_start_script_read_from_file_info(self, protocol):
        assert protocol.start_script == "var runMode = 'demo';"

    # --- process sections ---

    def test_main_processes_count_is_7(self, protocol):
        assert len(protocol.main) == 7

    def test_main_process_names_in_file_order(self, protocol):
        assert [p.name for p in protocol.main] == [
            "Source Plate",
            "Assay Plate 1",
            "Assay Plate 2",
            "Tip Box 1",
            "Tip Box 2",
            "Stamp Control",
            "Plate Stamp",
        ]

    def test_pipette_process_name_is_plate_stamp(self, protocol):
        pipette_procs = [p for p in protocol.main if p.is_pipette_process]
        assert len(pipette_procs) == 1
        assert pipette_procs[0].name == "Plate Stamp"

    def test_startup_processes_present(self, protocol):
        assert len(protocol.startup) == 1
        assert protocol.startup[0].name == "Initialize Run"

    def test_cleanup_processes_present(self, protocol):
        assert len(protocol.cleanup) == 1
        assert protocol.cleanup[0].name == "Shutdown"

    # --- variables from the startup JavaScript task ---

    def test_variables_extracted_from_startup_javascript(self, protocol):
        # Variables should have been populated from startup JS tasks
        assert set(protocol.variables) == {
            "plateCounter", "srcBC", "dispVol", "numPlates", "runMode",
        }

    def test_variable_dispVol_is_2_5(self, protocol):
        assert protocol.variables["dispVol"] == 2.5
        assert isinstance(protocol.variables["dispVol"], float)

    def test_variable_numPlates_is_3(self, protocol):
        assert protocol.variables["numPlates"] == 3
        assert isinstance(protocol.variables["numPlates"], int)

    def test_variable_srcBC_is_empty_list(self, protocol):
        assert protocol.variables["srcBC"] == []

    def test_variable_plateCounter_is_1(self, protocol):
        assert protocol.variables["plateCounter"] == 1

    def test_variable_runMode_is_a_string(self, protocol):
        assert protocol.variables["runMode"] == "stamp"

    def test_valueless_var_declaration_is_not_captured(self, protocol):
        # `var loopIndex;` has no initializer and must not become a variable
        assert "loopIndex" not in protocol.variables

    # --- plate / deck definitions ---

    def test_source_plate_definition_exists(self, protocol):
        names = [p.plate.name for p in protocol.main]
        assert "Source Plate" in names

    def test_source_plate_type(self, protocol):
        source = next(p for p in protocol.main if p.plate.name == "Source Plate")
        assert source.plate.plate_type == "96 Well Polypropylene Plate"

    def test_source_plate_definition_fully_parsed(self, protocol):
        source = next(p for p in protocol.main if p.plate.name == "Source Plate")
        assert source.plate == PlateDefinition(
            name="Source Plate",
            plate_type="96 Well Polypropylene Plate",
            count=1,
            location=None,
            is_tip_box=False,
            use_single_instance=True,
            has_lids=False,
        )

    def test_assay_plate_1_definition_exists(self, protocol):
        names = [p.plate.name for p in protocol.main]
        assert "Assay Plate 1" in names

    def test_assay_plate_2_definition_exists(self, protocol):
        names = [p.plate.name for p in protocol.main]
        assert "Assay Plate 2" in names

    def test_assay_plates_have_lids_and_are_not_single_instance(self, protocol):
        assay = [p.plate for p in protocol.main if p.plate.name.startswith("Assay Plate")]
        assert len(assay) == 2
        assert all(p.has_lids for p in assay)
        assert not any(p.use_single_instance for p in assay)

    def test_two_tip_box_processes(self, protocol):
        tip_procs = [p for p in protocol.main if p.plate.is_tip_box]
        assert len(tip_procs) == 2

    def test_tip_box_names(self, protocol):
        tip_names = {p.plate.name for p in protocol.main if p.plate.is_tip_box}
        assert tip_names == {"Tip Box 1", "Tip Box 2"}

    def test_tip_box_plate_type(self, protocol):
        tip_procs = [p for p in protocol.main if p.plate.is_tip_box]
        assert all(p.plate.plate_type == "384 Well Disposable Tip Box" for p in tip_procs)

    def test_processes_without_plate_parameters_have_empty_plate_name(self, protocol):
        control = next(p for p in protocol.main if p.name == "Stamp Control")
        assert control.plate.name == ""
        assert control.plate.plate_type == ""

    # --- control process: loop + spawn ---

    def test_control_process_spawns_the_pipette_process(self, protocol):
        control = next(p for p in protocol.main if p.name == "Stamp Control")
        assert control.spawns == ["Plate Stamp"]

    def test_loop_task_records_its_loop_count(self, protocol):
        control = next(p for p in protocol.main if p.name == "Stamp Control")
        loop = next(t for t in control.tasks if t.task_type == "Loop")
        assert loop.loop_count == "1"

    def test_loop_end_task_is_not_treated_as_a_loop(self, protocol):
        control = next(p for p in protocol.main if p.name == "Stamp Control")
        loop_end = next(t for t in control.tasks if t.task_type == "Loop End")
        assert loop_end.loop_count == ""

    # --- pipette process tasks ---

    def test_pipette_process_task_sequence(self, pipette):
        assert [t.task_type for t in pipette.tasks] == [
            "Set Head Mode",
            "Tips On", "Tips On",
            "Aspirate", "Dispense",
            "Aspirate", "Dispense",
            "Tips Off", "Tips Off",
            "Aspirate", "Dispense",
        ]

    def test_set_head_mode_decodes_embedded_head_mode_xml(self, pipette):
        task = next(t for t in pipette.tasks if t.task_type == "Set Head Mode")
        assert task.head_mode == HeadModeInfo(
            channels=96,
            row_count=8,
            column_count=12,
            subset_type="rectangle",
            subset_config="front_left",
            tip_type=2,
        )

    def test_head_mode_read_from_pipette_head_child_element(self, pipette):
        # The first Aspirate carries a <PipetteHead><PipetteHeadMode .../></PipetteHead>
        aspirate = next(t for t in pipette.tasks if t.task_type == "Aspirate")
        assert aspirate.head_mode is not None
        assert aspirate.head_mode.subset_type == "rectangle"
        assert aspirate.head_mode.subset_config == "front_left"
        assert aspirate.head_mode.row_count == 8
        assert aspirate.head_mode.column_count == 12
        assert aspirate.head_mode.channels == 96

    def test_estimated_time_parsed_from_advanced_settings(self, pipette):
        by_desc = {t.description: t.estimated_time for t in pipette.tasks}
        assert by_desc["Draw sample for the first assay plate"] == 20.0
        assert by_desc["Deliver sample into the first assay plate"] == 18.0
        assert by_desc["Mount tips from the first box"] == 12.5

    def test_pipette_process_has_aspirate_tasks(self, pipette):
        aspirates = [t for t in pipette.tasks if t.aspirate is not None]
        # two live aspirates plus one disabled optional-wash aspirate
        assert len(aspirates) == 3

    def test_aspirate_plate_is_source_plate(self, pipette):
        live = [t for t in pipette.tasks
                if t.aspirate is not None and not (t.disabled or t.skipped)]
        assert len(live) == 2
        assert all(t.aspirate.plate == "Source Plate" for t in live)

    def test_aspirate_parameters_parsed(self, pipette):
        aspirate = next(t for t in pipette.tasks if t.task_type == "Aspirate").aspirate
        assert aspirate.volume == "dispVol"
        assert aspirate.location == "4"
        assert aspirate.pre_aspirate == 4.0
        assert aspirate.post_aspirate == 1.5
        assert aspirate.liquid_class == "Aqueous Low Volume"
        assert aspirate.distance_from_bottom == 0.3
        assert aspirate.dynamic_tip_extension == 0.1
        assert aspirate.tip_touch.enabled is False

    def test_aspirate_well_selection_parsed(self, pipette):
        aspirate = next(t for t in pipette.tasks if t.task_type == "Aspirate").aspirate
        ws = aspirate.well_selection
        assert ws is not None
        assert ws.wells == [(0, 0)]
        assert ws.is_quadrant_pattern is False
        assert ws.starting_quadrant == 1
        assert ws.head_mode.subset_type == "rectangle"
        assert ws.head_mode.tip_type == 2

    def test_dispense_targets_are_assay_plates(self, pipette):
        live = [t for t in pipette.tasks
                if t.dispense is not None and not (t.disabled or t.skipped)]
        dest_plates = {t.dispense.plate for t in live}
        assert dest_plates == {"Assay Plate 1", "Assay Plate 2"}

    def test_dispense_parameters_parsed(self, pipette):
        dispense = next(t for t in pipette.tasks if t.task_type == "Dispense").dispense
        assert dispense.plate == "Assay Plate 1"
        assert dispense.volume == "dispVol"
        assert dispense.blowout == 2.0
        assert dispense.empty_tips is True
        assert dispense.distance_from_bottom == 0.2
        assert dispense.dynamic_tip_retraction == 0.05

    def test_dispense_tip_touch_parsed(self, pipette):
        dispense = next(t for t in pipette.tasks if t.task_type == "Dispense").dispense
        assert dispense.tip_touch.enabled is True
        assert dispense.tip_touch.sides == "North and South"
        assert dispense.tip_touch.retract_distance == 1.5
        assert dispense.tip_touch.horizontal_offset == 0.25

    def test_dispense_well_selection_is_a_quadrant_pattern(self, pipette):
        dispense = next(t for t in pipette.tasks if t.task_type == "Dispense").dispense
        ws = dispense.well_selection
        assert ws is not None
        assert ws.is_quadrant_pattern is True
        assert ws.wells == [(0, 0), (0, 1)]

    def test_tips_on_tasks_reference_both_tip_boxes(self, pipette):
        tips_on = [t for t in pipette.tasks if t.task_type == "Tips On"]
        assert [t.parameters["Location, plate"] for t in tips_on] == ["Tip Box 1", "Tip Box 2"]

    def test_tips_on_well_selection_parsed(self, pipette):
        tips_on = next(t for t in pipette.tasks if t.task_type == "Tips On")
        assert tips_on.well_selection is not None
        assert tips_on.well_selection.wells == [(0, 0)]
        assert tips_on.well_selection.head_mode.column_count == 12

    def test_tips_off_tasks_reference_both_tip_boxes(self, pipette):
        tips_off = [t for t in pipette.tasks if t.task_type == "Tips Off"]
        assert [t.parameters["Location, plate"] for t in tips_off] == ["Tip Box 1", "Tip Box 2"]

    def test_disabled_wash_aspirate_is_parsed_and_flagged(self, pipette):
        disabled = [t for t in pipette.tasks if t.disabled]
        assert len(disabled) == 1
        assert disabled[0].task_type == "Aspirate"
        assert disabled[0].aspirate.plate == "Wash Reservoir"

    def test_skipped_wash_dispense_is_parsed_and_flagged(self, pipette):
        skipped = [t for t in pipette.tasks if t.skipped]
        assert len(skipped) == 1
        assert skipped[0].task_type == "Dispense"
        assert skipped[0].dispense.plate == "Waste"


# ---------------------------------------------------------------------------
# import_pro / protocol_to_workflow — integration tests
# ---------------------------------------------------------------------------

class TestImportPro:
    @pytest.fixture(scope="class")
    def workflow(self):
        return import_pro(PRO_FILE)

    def test_workflow_key_present(self, workflow):
        assert "workflow" in workflow

    def test_workflow_name_derived_from_filename(self, workflow):
        assert workflow["workflow"] == "example protocol"

    def test_description_carried_through(self, workflow):
        assert workflow["description"] == (
            "Stamp a 96-well source plate into two 384-well assay plates."
        )

    def test_protocol_to_workflow_matches_import_pro(self, workflow):
        assert protocol_to_workflow(parse_pro_file(PRO_FILE)) == workflow

    # --- deck ---

    def test_deck_key_present(self, workflow):
        assert "deck" in workflow

    def test_deck_has_source_plate(self, workflow):
        assert "Source Plate" in workflow["deck"]

    def test_source_plate_type(self, workflow):
        assert workflow["deck"]["Source Plate"]["type"] == "96 Well Polypropylene Plate"

    def test_source_plate_count_is_3(self, workflow):
        # count comes from the numPlates startup variable
        assert workflow["deck"]["Source Plate"]["count"] == 3

    def test_deck_has_assay_plate_1(self, workflow):
        assert workflow["deck"]["Assay Plate 1"] == {"type": "384 Well Assay Plate"}

    def test_deck_has_assay_plate_2(self, workflow):
        assert workflow["deck"]["Assay Plate 2"] == {"type": "384 Well Assay Plate"}

    def test_deck_has_tips_entry(self, workflow):
        assert "Tips" in workflow["deck"]

    def test_tips_type(self, workflow):
        assert workflow["deck"]["Tips"]["type"] == "384 Well Disposable Tip Box"

    def test_tips_boxes_are_both_tip_boxes(self, workflow):
        boxes = workflow["deck"]["Tips"]["boxes"]
        assert boxes == ["Tip Box 1", "Tip Box 2"]

    def test_deck_has_barcode_reader_at_location_6(self, workflow):
        assert workflow["deck"]["Barcode Reader"]["location"] == 6

    def test_tip_boxes_are_not_listed_as_plates(self, workflow):
        assert "Tip Box 1" not in workflow["deck"]
        assert "Tip Box 2" not in workflow["deck"]

    # --- variables ---

    def test_variables_section_present(self, workflow):
        assert "variables" in workflow

    def test_variables_dispVol(self, workflow):
        assert workflow["variables"]["dispVol"] == 2.5

    def test_variables_numPlates(self, workflow):
        assert workflow["variables"]["numPlates"] == 3

    def test_only_user_facing_variables_are_exported(self, workflow):
        assert set(workflow["variables"]) == {"dispVol", "numPlates"}

    # --- steps ---

    @staticmethod
    def _get_loop_steps(workflow):
        """Extract the steps inside the loop wrapper from the flat steps list."""
        for item in workflow["steps"]:
            if isinstance(item, dict) and not item.get("action"):
                return list(item.values())[0]
        return []

    def test_steps_is_a_list(self, workflow):
        assert isinstance(workflow["steps"], list)

    def test_barcode_scans_appear_before_loop(self, workflow):
        actions = [s.get("action") for s in workflow["steps"] if isinstance(s, dict)]
        scan_indices = [i for i, a in enumerate(actions) if a == "scan barcode"]
        loop_indices = [i for i, a in enumerate(actions) if a is None]
        assert len(scan_indices) == 2  # one per assay plate
        assert len(loop_indices) == 1
        assert max(scan_indices) < loop_indices[0]

    def test_barcode_scans_name_their_plates_and_variables(self, workflow):
        scans = [s for s in workflow["steps"]
                 if isinstance(s, dict) and s.get("action") == "scan barcode"]
        assert [s["plate"] for s in scans] == ["Assay Plate 1", "Assay Plate 2"]
        assert [s["store_as"] for s in scans] == ["destBC1", "destBC2"]
        assert all(s["reader"] == "location 6" for s in scans)

    def test_loop_key_contains_x3(self, workflow):
        loop_items = [s for s in workflow["steps"] if isinstance(s, dict) and not s.get("action")]
        assert len(loop_items) == 1
        loop_key = list(loop_items[0].keys())[0]
        # the literal "1" from the Loop task is overridden by the numPlates variable
        assert "x3" in loop_key

    def test_steps_list_has_four_items(self, workflow):
        steps = self._get_loop_steps(workflow)
        assert len(steps) == 4

    def test_first_step_is_mount_tips(self, workflow):
        steps = self._get_loop_steps(workflow)
        assert steps[0]["action"] == "mount tips"

    def test_mount_tips_has_two_boxes(self, workflow):
        steps = self._get_loop_steps(workflow)
        assert steps[0]["from"] == ["Tip Box 1", "Tip Box 2"]

    def test_second_step_is_stamp(self, workflow):
        steps = self._get_loop_steps(workflow)
        assert steps[1]["action"] == "stamp"

    def test_third_step_is_stamp(self, workflow):
        steps = self._get_loop_steps(workflow)
        assert steps[2]["action"] == "stamp"

    def test_last_step_is_discard_tips(self, workflow):
        steps = self._get_loop_steps(workflow)
        assert steps[-1]["action"] == "discard tips"

    def test_discard_tips_has_two_boxes(self, workflow):
        steps = self._get_loop_steps(workflow)
        assert steps[-1]["to"] == ["Tip Box 1", "Tip Box 2"]

    def test_stamp_1_from_source_plate(self, workflow):
        steps = self._get_loop_steps(workflow)
        stamps = [s for s in steps if s["action"] == "stamp"]
        assert stamps[0]["from"] == "Source Plate"

    def test_stamp_1_to_assay_plate_1(self, workflow):
        steps = self._get_loop_steps(workflow)
        stamps = [s for s in steps if s["action"] == "stamp"]
        assert stamps[0]["to"] == "Assay Plate 1"

    def test_stamp_2_to_assay_plate_2(self, workflow):
        steps = self._get_loop_steps(workflow)
        stamps = [s for s in steps if s["action"] == "stamp"]
        assert stamps[1]["to"] == "Assay Plate 2"

    def test_stamp_1_has_quadrant_mapping(self, workflow):
        steps = self._get_loop_steps(workflow)
        stamps = [s for s in steps if s["action"] == "stamp"]
        assert "quadrant_mapping" in stamps[0]
        assert len(stamps[0]["quadrant_mapping"]) == 3

    def test_stamp_1_quadrant_mapping_values(self, workflow):
        steps = self._get_loop_steps(workflow)
        stamps = [s for s in steps if s["action"] == "stamp"]
        # script maps plate 1 -> [[1,1]], plate 2 -> [[1,2]], plate 3 -> [[2,1]]
        assert stamps[0]["quadrant_mapping"] == ["B2", "C2", "B3"]

    def test_stamp_2_has_no_quadrant_mapping(self, workflow):
        steps = self._get_loop_steps(workflow)
        stamps = [s for s in steps if s["action"] == "stamp"]
        assert "quadrant_mapping" not in stamps[1]

    def test_stamp_1_volume_is_dispVol(self, workflow):
        steps = self._get_loop_steps(workflow)
        stamps = [s for s in steps if s["action"] == "stamp"]
        assert stamps[0]["volume"] == "dispVol"

    def test_stamp_1_carries_aspirate_parameters(self, workflow):
        steps = self._get_loop_steps(workflow)
        stamps = [s for s in steps if s["action"] == "stamp"]
        assert stamps[0]["aspirate"] == {
            "pre_aspirate_uL": 4.0,
            "post_aspirate_uL": 1.5,
            "liquid_class": "Aqueous Low Volume",
            "distance_from_bottom_mm": 0.3,
            "dynamic_tip_extension_mm": 0.1,
        }

    def test_stamp_1_carries_dispense_parameters_including_tip_touch(self, workflow):
        steps = self._get_loop_steps(workflow)
        stamps = [s for s in steps if s["action"] == "stamp"]
        assert stamps[0]["dispense"] == {
            "blowout_uL": 2.0,
            "liquid_class": "Aqueous Low Volume",
            "distance_from_bottom_mm": 0.2,
            "dynamic_tip_retraction_mm": 0.05,
            "tip_touch": {
                "enabled": True,
                "sides": "North and South",
                "retract_distance": 1.5,
                "horizontal_offset": 0.25,
            },
        }

    def test_stamp_2_has_no_tip_touch(self, workflow):
        steps = self._get_loop_steps(workflow)
        stamps = [s for s in steps if s["action"] == "stamp"]
        assert "tip_touch" not in stamps[1]["dispense"]

    def test_set_head_mode_is_absorbed_and_not_a_step(self, workflow):
        steps = self._get_loop_steps(workflow)
        assert all(s["action"] != "set_head_mode" for s in steps)

    def test_disabled_and_skipped_wash_tasks_are_dropped(self, workflow):
        rendered = repr(workflow["steps"])
        assert "Wash Reservoir" not in rendered
        assert "Waste" not in rendered

    def test_private_keys_are_stripped_from_steps(self, workflow):
        steps = self._get_loop_steps(workflow)
        assert all(not k.startswith("_") for s in steps for k in s)
