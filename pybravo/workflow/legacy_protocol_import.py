"""Import legacy .pro protocol files into pybravo workflow format.

Legacy .pro protocols are XML files that describe Bravo liquid handling workflows
in terms of mechanical tasks (Place Plate, Tips On, Aspirate, Dispense, etc.)
and process orchestration (Spawn Process, Loop, etc.).

This importer parses the XML, extracts the scientific intent, and produces
a structured workflow dict that can be serialized to YAML.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Legacy head-mode encoding tables
# ---------------------------------------------------------------------------

_SUBSET_TYPE_MAP: dict[int, str] = {
    0: "all_barrels",
    1: "row",
    2: "column",
    3: "single_barrel",
    4: "rectangle",
}

_SUBSET_CONFIG_MAP: dict[int, str] = {
    0: "back_left",
    1: "back_right",
    2: "front_left",
    3: "front_right",
}


# ---------------------------------------------------------------------------
# Data classes for parsed protocol elements
# ---------------------------------------------------------------------------

@dataclass
class HeadModeInfo:
    channels: int = 0
    row_count: int = 0
    column_count: int = 0
    subset_type: str = "all_barrels"
    subset_config: str = "back_left"
    tip_type: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "subset_type": self.subset_type,
            "subset_config": self.subset_config,
            "row_count": self.row_count,
            "column_count": self.column_count,
        }


@dataclass
class WellSelection:
    wells: list[tuple[int, int]] = field(default_factory=list)
    head_mode: HeadModeInfo | None = None
    is_quadrant_pattern: bool = False
    starting_quadrant: int = 1


@dataclass
class TipTouch:
    enabled: bool = False
    sides: str = "None"
    retract_distance: float = 0.0
    horizontal_offset: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False}
        return {
            "enabled": True,
            "sides": self.sides,
            "retract_distance": self.retract_distance,
            "horizontal_offset": self.horizontal_offset,
        }


@dataclass
class AspirateParams:
    plate: str = ""
    location: str = ""
    volume: str = ""
    pre_aspirate: float = 0.0
    post_aspirate: float = 0.0
    liquid_class: str = ""
    distance_from_bottom: float = 0.0
    dynamic_tip_extension: float = 0.0
    tip_touch: TipTouch = field(default_factory=TipTouch)
    well_selection: WellSelection | None = None
    script: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"from": self.plate}
        d["volume"] = self.volume
        if self.pre_aspirate:
            d["pre_aspirate_uL"] = self.pre_aspirate
        if self.post_aspirate:
            d["post_aspirate_uL"] = self.post_aspirate
        if self.liquid_class:
            d["liquid_class"] = self.liquid_class
        if self.distance_from_bottom:
            d["distance_from_bottom_mm"] = self.distance_from_bottom
        if self.dynamic_tip_extension:
            d["dynamic_tip_extension_mm"] = self.dynamic_tip_extension
        if self.tip_touch.enabled:
            d["tip_touch"] = self.tip_touch.to_dict()
        if self.well_selection and self.well_selection.wells:
            d["well"] = _format_well(self.well_selection.wells[0])
        if self.script:
            d["_script"] = self.script
        return d


@dataclass
class DispenseParams:
    plate: str = ""
    location: str = ""
    volume: str = ""
    blowout: float = 0.0
    empty_tips: bool = False
    liquid_class: str = ""
    distance_from_bottom: float = 0.0
    dynamic_tip_retraction: float = 0.0
    tip_touch: TipTouch = field(default_factory=TipTouch)
    well_selection: WellSelection | None = None
    script: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"to": self.plate}
        d["volume"] = self.volume
        if self.blowout:
            d["blowout_uL"] = self.blowout
        if self.empty_tips:
            d["empty_tips"] = True
        if self.liquid_class:
            d["liquid_class"] = self.liquid_class
        if self.distance_from_bottom:
            d["distance_from_bottom_mm"] = self.distance_from_bottom
        if self.dynamic_tip_retraction:
            d["dynamic_tip_retraction_mm"] = self.dynamic_tip_retraction
        if self.tip_touch.enabled:
            d["tip_touch"] = self.tip_touch.to_dict()
        if self.well_selection and self.well_selection.wells:
            d["well"] = _format_well(self.well_selection.wells[0])
        if self.script:
            d["_script"] = self.script
        return d


@dataclass
class PlateDefinition:
    name: str = ""
    plate_type: str = ""
    count: int = 1
    location: int | None = None
    is_tip_box: bool = False
    use_single_instance: bool = False
    has_lids: bool = False


@dataclass
class TaskInfo:
    task_type: str = ""
    task_name: str = ""
    description: str = ""
    disabled: bool = False
    skipped: bool = False
    script: str = ""
    parameters: dict[str, str] = field(default_factory=dict)
    head_mode: HeadModeInfo | None = None
    aspirate: AspirateParams | None = None
    dispense: DispenseParams | None = None
    well_selection: WellSelection | None = None
    spawn_target: str = ""
    loop_count: str = ""
    estimated_time: float = 0.0


@dataclass
class ProcessInfo:
    name: str = ""
    plate: PlateDefinition = field(default_factory=PlateDefinition)
    tasks: list[TaskInfo] = field(default_factory=list)
    is_pipette_process: bool = False
    spawns: list[str] = field(default_factory=list)


@dataclass
class ProtocolInfo:
    """Complete parsed representation of a legacy .pro file."""
    name: str = ""
    description: str = ""
    device_file: str = ""
    start_script: str = ""
    startup: list[ProcessInfo] = field(default_factory=list)
    main: list[ProcessInfo] = field(default_factory=list)
    cleanup: list[ProcessInfo] = field(default_factory=list)
    variables: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_well(well: tuple[int, int]) -> str:
    """Convert (row, col) to plate notation like A1."""
    row, col = well
    return f"{chr(65 + row)}{col + 1}"


def _parse_float(value: str, default: float = 0.0) -> float:
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def _parse_int(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _get_param(params: dict[str, str], name: str, default: str = "") -> str:
    return params.get(name, default)


def _decode_embedded_xml(raw: str) -> ET.Element | None:
    """Decode HTML-entity-escaped XML embedded in parameter values."""
    decoded = unescape(raw)
    try:
        return ET.fromstring(decoded)
    except ET.ParseError:
        return None


# ---------------------------------------------------------------------------
# Parsing functions
# ---------------------------------------------------------------------------

def _parse_head_mode_element(el: ET.Element) -> HeadModeInfo:
    subset_type_int = _parse_int(el.get("SubsetType", "0"))
    subset_config_int = _parse_int(el.get("SubsetConfig", "0"))
    return HeadModeInfo(
        channels=_parse_int(el.get("Channels", "0")),
        row_count=_parse_int(el.get("RowCount", "0")),
        column_count=_parse_int(el.get("ColumnCount", "0")),
        subset_type=_SUBSET_TYPE_MAP.get(subset_type_int, "all_barrels"),
        subset_config=_SUBSET_CONFIG_MAP.get(subset_config_int, "back_left"),
        tip_type=_parse_int(el.get("TipType", "0")),
    )


def _parse_well_selection(raw_xml: str) -> WellSelection | None:
    root = _decode_embedded_xml(raw_xml)
    if root is None:
        return None
    ws_el = root.find(".//WellSelection")
    if ws_el is None:
        return None
    head_mode_el = ws_el.find("PipetteHeadMode")
    wells_el = ws_el.find("Wells")
    wells: list[tuple[int, int]] = []
    if wells_el is not None:
        for well in wells_el.findall("Well"):
            row = _parse_int(well.get("Row", "0"))
            col = _parse_int(well.get("Column", "0"))
            wells.append((row, col))
    return WellSelection(
        wells=wells,
        head_mode=_parse_head_mode_element(head_mode_el) if head_mode_el is not None else None,
        is_quadrant_pattern=ws_el.get("IsQuadrantPattern", "0") == "1",
        starting_quadrant=_parse_int(ws_el.get("StartingQuadrant", "1")),
    )


def _parse_task_params(task_el: ET.Element) -> dict[str, str]:
    """Extract all Parameter Name=Value pairs from a Task element."""
    params: dict[str, str] = {}
    for param in task_el.findall(".//Parameters/Parameter"):
        name = param.get("Name", "")
        value = param.get("Value", "")
        if name:
            params[name] = value
    return params


def _parse_tip_touch(params: dict[str, str]) -> TipTouch:
    return TipTouch(
        enabled=_get_param(params, "Perform tip touch") == "1",
        sides=_get_param(params, "Which sides to use for tip touch", "None"),
        retract_distance=_parse_float(_get_param(params, "Tip touch retract distance")),
        horizontal_offset=_parse_float(_get_param(params, "Tip touch horizontal offset")),
    )


def _parse_aspirate(task_el: ET.Element, params: dict[str, str], script: str) -> AspirateParams:
    ws_raw = _get_param(params, "Well selection")
    volume_raw = _get_param(params, "Volume", "0")
    return AspirateParams(
        plate=_get_param(params, "Location, plate"),
        location=_get_param(params, "Location, location"),
        volume=volume_raw,
        pre_aspirate=_parse_float(_get_param(params, "Pre-aspirate volume")),
        post_aspirate=_parse_float(_get_param(params, "Post-aspirate volume")),
        liquid_class=_get_param(params, "Liquid class"),
        distance_from_bottom=_parse_float(_get_param(params, "Distance from well bottom")),
        dynamic_tip_extension=_parse_float(_get_param(params, "Dynamic tip extension")),
        tip_touch=_parse_tip_touch(params),
        well_selection=_parse_well_selection(ws_raw) if ws_raw else None,
        script=script,
    )


def _parse_dispense(task_el: ET.Element, params: dict[str, str], script: str) -> DispenseParams:
    ws_raw = _get_param(params, "Well selection")
    volume_raw = _get_param(params, "Volume", "0")
    return DispenseParams(
        plate=_get_param(params, "Location, plate"),
        location=_get_param(params, "Location, location"),
        volume=volume_raw,
        blowout=_parse_float(_get_param(params, "Blowout volume")),
        empty_tips=_get_param(params, "Empty tips") == "1",
        liquid_class=_get_param(params, "Liquid class"),
        distance_from_bottom=_parse_float(_get_param(params, "Distance from well bottom")),
        dynamic_tip_retraction=_parse_float(_get_param(params, "Dynamic tip retraction")),
        tip_touch=_parse_tip_touch(params),
        well_selection=_parse_well_selection(ws_raw) if ws_raw else None,
        script=script,
    )


def _extract_js_variables(script: str) -> dict[str, Any]:
    """Extract simple variable assignments from JavaScript."""
    variables: dict[str, Any] = {}
    for match in re.finditer(r'var\s+(\w+)\s*=\s*(.+?)\s*;', script):
        name = match.group(1)
        raw_value = match.group(2).strip()
        if raw_value.startswith('"') and raw_value.endswith('"'):
            variables[name] = raw_value.strip('"')
        elif raw_value.startswith("'") and raw_value.endswith("'"):
            variables[name] = raw_value.strip("'")
        elif raw_value == "[]":
            variables[name] = []
        else:
            try:
                if "." in raw_value:
                    variables[name] = float(raw_value)
                else:
                    variables[name] = int(raw_value)
            except ValueError:
                variables[name] = raw_value
    return variables


def _extract_quadrant_mapping(script: str) -> list[tuple[int, int]] | None:
    """Extract quadrant mapping from JavaScript like:
    if(plateCounter == 1) task.Wellselection = [[1,1]];
    """
    matches = re.findall(
        r'plateCounter\s*==\s*(\d+)\)\s*task\.Wellselection\s*=\s*\[\[(\d+),(\d+)\]\]',
        script,
    )
    if not matches:
        return None
    mapping: list[tuple[int, int]] = [(-1, -1)] * (max(int(m[0]) for m in matches))
    for plate_num, col, row in matches:
        idx = int(plate_num) - 1
        if 0 <= idx < len(mapping):
            mapping[idx] = (int(row), int(col))
    return mapping


def _parse_task(task_el: ET.Element) -> TaskInfo:
    task_name = task_el.get("Name", "")
    params = _parse_task_params(task_el)
    script_el = task_el.find("TaskScript")
    script = script_el.get("Value", "") if script_el is not None else ""
    # Unescape HTML entities in script
    script = unescape(script)

    info = TaskInfo(
        task_type=task_name,
        task_name=task_name,
        description=_get_param(params, "Task description"),
        disabled=task_el.findtext("Task_Disabled", "0") == "1",
        skipped=task_el.findtext("Task_Skipped", "0") == "1",
        script=script,
        parameters=params,
    )

    # Parse estimated time
    for setting in task_el.findall(".//Advanced_Settings/Setting"):
        if setting.get("Name") == "Estimated time":
            info.estimated_time = _parse_float(setting.get("Value", "0"))

    # Parse head mode from PipetteHead child
    head_el = task_el.find("PipetteHead/PipetteHeadMode")
    if head_el is not None:
        info.head_mode = _parse_head_mode_element(head_el)

    # Task-type-specific parsing
    if "Aspirate" in task_name:
        info.aspirate = _parse_aspirate(task_el, params, script)
    elif "Dispense" in task_name:
        info.dispense = _parse_dispense(task_el, params, script)
    elif "Tips On" in task_name or "Tips Off" in task_name:
        ws_raw = _get_param(params, "Well selection")
        if ws_raw:
            info.well_selection = _parse_well_selection(ws_raw)
    elif "Spawn Process" in task_name:
        info.spawn_target = _get_param(params, "Process to spawn")
    elif "Loop" in task_name and "Loop End" not in task_name:
        info.loop_count = _get_param(params, "Number of times to loop", "1")
    elif "Set Head Mode" in task_name:
        hm_raw = _get_param(params, "Head mode")
        if hm_raw:
            hm_root = _decode_embedded_xml(hm_raw)
            if hm_root is not None:
                hm_el = hm_root.find(".//PipetteHeadMode")
                if hm_el is not None:
                    info.head_mode = _parse_head_mode_element(hm_el)

    return info


def _parse_plate_params(process_el: ET.Element) -> PlateDefinition:
    plate = PlateDefinition()
    pp = process_el.find("Plate_Parameters")
    if pp is None:
        return plate
    for param in pp.findall("Parameter"):
        name = param.get("Name", "")
        value = param.get("Value", "")
        if name == "Plate name":
            plate.name = value
        elif name == "Plate type":
            plate.plate_type = value
            if value and "tip" in value.lower():
                plate.is_tip_box = True
        elif name == "Use single instance of plate":
            plate.use_single_instance = value == "1"
        elif name == "Plates have lids":
            plate.has_lids = value == "1"
    return plate


def _parse_process(process_el: ET.Element) -> ProcessInfo:
    is_pipette = process_el.tag == "Pipette_Process"
    proc = ProcessInfo(
        name=process_el.get("Name", ""),
        is_pipette_process=is_pipette,
    )
    proc.plate = _parse_plate_params(process_el)
    if not proc.name and proc.plate.name:
        proc.name = proc.plate.name

    for task_el in process_el.findall("Task"):
        task = _parse_task(task_el)
        proc.tasks.append(task)
        if task.spawn_target:
            proc.spawns.append(task.spawn_target)

    return proc


def _parse_device_locations(processes: list[ProcessInfo]) -> dict[str, int]:
    """Extract plate-name → location mappings from Place Plate tasks."""
    mapping: dict[str, int] = {}
    for proc in processes:
        for task in proc.tasks:
            if "Place Plate" in task.task_type:
                location_str = _get_param(task.parameters, "Location to use", "")
                if location_str:
                    try:
                        loc = int(location_str)
                        mapping[proc.plate.name] = loc
                    except ValueError:
                        pass
    return mapping


# ---------------------------------------------------------------------------
# Main parse function
# ---------------------------------------------------------------------------

def parse_pro_file(path: str | Path) -> ProtocolInfo:
    """Parse a legacy .pro file and return a structured ProtocolInfo."""
    # The .pro format declares encoding='ASCII' but often contains Latin-1 characters
    # (e.g., µ in "70 µL"). Read as bytes and decode permissively.
    raw = Path(path).read_bytes()
    text = raw.decode("latin-1")
    # Fix the encoding declaration so ET doesn't reject it
    text = re.sub(r"encoding='ASCII'", "encoding='latin-1'", text)
    root = ET.fromstring(text)

    info = ProtocolInfo()

    # File_Info
    file_info = root.find("File_Info")
    if file_info is not None:
        info.description = file_info.get("Description", "")
        info.device_file = file_info.get("Device_File", "")
        info.start_script = file_info.get("StartScript", "")

    # Derive name from filename
    info.name = Path(path).stem

    # Parse all process sections
    for section_tag, target_list in [
        ("Startup_Processes", info.startup),
        ("Main_Processes", info.main),
        ("Cleanup_Processes", info.cleanup),
    ]:
        section = root.find(f"Processes/{section_tag}")
        if section is None:
            continue
        for proc_el in section:
            if proc_el.tag in ("Process", "Pipette_Process"):
                target_list.append(_parse_process(proc_el))

    # Extract variables from startup JavaScript
    for proc in info.startup:
        for task in proc.tasks:
            if "JavaScript" in task.task_type and task.script:
                info.variables.update(_extract_js_variables(task.script))

    return info


# ---------------------------------------------------------------------------
# Workflow generation
# ---------------------------------------------------------------------------

def _classify_plates(protocol: ProtocolInfo) -> dict[str, list[PlateDefinition]]:
    """Classify plates into categories: sources, destinations, tips, other."""
    categories: dict[str, list[PlateDefinition]] = {
        "sources": [],
        "destinations": [],
        "tips": [],
        "other": [],
    }

    # Collect all plate definitions from main processes
    plates_seen: dict[str, PlateDefinition] = {}
    for proc in protocol.main:
        if proc.plate.name and proc.plate.name not in plates_seen:
            plates_seen[proc.plate.name] = proc.plate

    # Find plates referenced in aspirate/dispense to determine source vs dest
    aspirate_plates: set[str] = set()
    dispense_plates: set[str] = set()
    for proc in protocol.main:
        for task in proc.tasks:
            if task.aspirate:
                aspirate_plates.add(task.aspirate.plate)
            if task.dispense:
                dispense_plates.add(task.dispense.plate)

    for name, plate in plates_seen.items():
        if plate.is_tip_box:
            categories["tips"].append(plate)
        elif name in aspirate_plates and name not in dispense_plates:
            categories["sources"].append(plate)
        elif name in dispense_plates and name not in aspirate_plates:
            categories["destinations"].append(plate)
        elif name in aspirate_plates and name in dispense_plates:
            categories["sources"].append(plate)  # Both = likely source
        else:
            categories["other"].append(plate)

    return categories


def _extract_pipette_steps(protocol: ProtocolInfo) -> list[dict[str, Any]]:
    """Extract the core pipetting steps from Pipette_Process tasks."""
    steps: list[dict[str, Any]] = []

    for proc in protocol.main:
        if not proc.is_pipette_process:
            continue

        for task in proc.tasks:
            if task.disabled or task.skipped:
                continue

            if "Set Head Mode" in task.task_type and task.head_mode:
                steps.append({
                    "action": "set_head_mode",
                    **task.head_mode.to_dict(),
                })
            elif "Tips On" in task.task_type:
                plate_name = _get_param(task.parameters, "Location, plate", "")
                step: dict[str, Any] = {"action": "tips_on", "from": plate_name}
                if task.script:
                    step["_script"] = task.script
                steps.append(step)
            elif "Tips Off" in task.task_type:
                plate_name = _get_param(task.parameters, "Location, plate", "")
                step = {"action": "tips_off", "to": plate_name}
                if task.script:
                    step["_script"] = task.script
                steps.append(step)
            elif task.aspirate:
                step = {"action": "aspirate", **task.aspirate.to_dict()}
                # Check for quadrant mapping in script
                if task.script:
                    qm = _extract_quadrant_mapping(task.script)
                    if qm:
                        step["_quadrant_mapping"] = [
                            _format_well(w) for w in qm if w != (-1, -1)
                        ]
                steps.append(step)
            elif task.dispense:
                step = {"action": "dispense", **task.dispense.to_dict()}
                if task.script:
                    qm = _extract_quadrant_mapping(task.script)
                    if qm:
                        step["_quadrant_mapping"] = [
                            _format_well(w) for w in qm if w != (-1, -1)
                        ]
                steps.append(step)
            elif "Group Begin" in task.task_type or "Group End" in task.task_type:
                continue  # Skip grouping markers

    return steps


def _extract_loop_info(protocol: ProtocolInfo) -> dict[str, Any] | None:
    """Find the main loop in the control process."""
    for proc in protocol.main:
        for task in proc.tasks:
            if "Loop" in task.task_type and "Loop End" not in task.task_type:
                count = task.loop_count
                # Check if loop count is set by script variable
                if task.script:
                    match = re.search(r'task\.Numberoftimestoloop\s*=\s*(\w+)', task.script)
                    if match:
                        var_name = match.group(1)
                        if var_name in protocol.variables:
                            count = str(protocol.variables[var_name])
                        else:
                            count = var_name
                return {"count": count, "variable": "plateCounter"}
    return None


def _simplify_steps(raw_steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse mechanical low-level steps into clean scientific steps.

    Patterns recognized:
    - Multiple conditional Tips On from different boxes → single "mount tips"
    - Multiple conditional Tips Off to different boxes → single "discard tips"
    - Aspirate + Dispense pair with quadrant mapping → "stamp" operation
    - Set Head Mode at start → absorbed into workflow metadata
    """
    simplified: list[dict[str, Any]] = []
    i = 0

    while i < len(raw_steps):
        step = raw_steps[i]
        action = step.get("action", "")

        # --- Collapse conditional Tips On into single "mount tips" ---
        if action == "tips_on" and "_script" in step:
            tip_boxes = [step["from"]]
            j = i + 1
            while j < len(raw_steps) and raw_steps[j].get("action") == "tips_on":
                tip_boxes.append(raw_steps[j]["from"])
                j += 1
            simplified.append({"action": "mount tips", "from": tip_boxes})
            i = j
            continue

        # --- Collapse conditional Tips Off into single "discard tips" ---
        if action == "tips_off" and "_script" in step:
            tip_boxes = [step["to"]]
            j = i + 1
            while j < len(raw_steps) and raw_steps[j].get("action") == "tips_off":
                tip_boxes.append(raw_steps[j]["to"])
                j += 1
            simplified.append({"action": "discard tips", "to": tip_boxes})
            i = j
            continue

        # --- Collapse Aspirate + Dispense into "stamp" ---
        if action == "aspirate" and i + 1 < len(raw_steps):
            next_step = raw_steps[i + 1]
            if next_step.get("action") == "dispense":
                stamp: dict[str, Any] = {"action": "stamp"}
                stamp["volume"] = step.get("volume", "0")
                stamp["from"] = step.get("from", "")
                stamp["to"] = next_step.get("to", "")

                # Aspirate parameters
                asp_params: dict[str, Any] = {}
                for key in ("pre_aspirate_uL", "post_aspirate_uL", "liquid_class",
                            "distance_from_bottom_mm", "dynamic_tip_extension_mm"):
                    if key in step:
                        asp_params[key] = step[key]
                if asp_params:
                    stamp["aspirate"] = asp_params

                # Dispense parameters
                disp_params: dict[str, Any] = {}
                for key in ("blowout_uL", "liquid_class", "distance_from_bottom_mm",
                            "dynamic_tip_retraction_mm", "tip_touch"):
                    if key in next_step:
                        disp_params[key] = next_step[key]
                if disp_params:
                    stamp["dispense"] = disp_params

                # Quadrant mapping
                qm = next_step.get("_quadrant_mapping")
                if qm:
                    stamp["quadrant_mapping"] = qm

                simplified.append(stamp)
                i += 2
                continue

        # --- Skip standalone set_head_mode (absorbed into context) ---
        if action == "set_head_mode":
            i += 1
            continue

        # --- Pass through anything else ---
        clean_step = {k: v for k, v in step.items() if not k.startswith("_")}
        simplified.append(clean_step)
        i += 1

    return simplified


def protocol_to_workflow(protocol: ProtocolInfo) -> dict[str, Any]:
    """Convert a parsed ProtocolInfo into a clean workflow dict."""
    workflow: dict[str, Any] = {}
    workflow["workflow"] = protocol.name.replace("_", " ")
    if protocol.description:
        workflow["description"] = protocol.description

    # Deck section
    categories = _classify_plates(protocol)
    deck: dict[str, Any] = {}

    for plate in categories["sources"]:
        label = plate.name
        entry: dict[str, Any] = {"type": plate.plate_type}
        count = protocol.variables.get("numPlates")
        if count and isinstance(count, int) and count > 1:
            entry["count"] = count
        deck[label] = entry

    for plate in categories["destinations"]:
        deck[plate.name] = {"type": plate.plate_type}

    tip_plates = categories["tips"]
    if tip_plates:
        tip_type = tip_plates[0].plate_type
        tip_names = [p.name for p in tip_plates]
        deck["Tips"] = {"type": tip_type, "boxes": tip_names}

    workflow["deck"] = deck

    # Variables section (user-facing ones only)
    user_vars: dict[str, Any] = {}
    for key in ("dispVol", "numPlates"):
        if key in protocol.variables:
            user_vars[key] = protocol.variables[key]
    if user_vars:
        workflow["variables"] = user_vars

    # Barcode scanning — detect scan processes and add to deck/steps
    scan_steps = _extract_barcode_scans(protocol)
    if scan_steps:
        deck["Barcode Reader"] = {"type": "Microscan MS3", "location": 6}

    # Steps section — extract raw then simplify
    raw_steps = _extract_pipette_steps(protocol)
    steps = _simplify_steps(raw_steps)
    loop_info = _extract_loop_info(protocol)

    # Build ordered step list: scans first, then main loop
    all_steps: list[Any] = []
    if scan_steps:
        all_steps.extend(scan_steps)
    if loop_info:
        all_steps.append({f"for each Source Plate (x{loop_info['count']})": steps})
    else:
        all_steps.extend(steps)

    workflow["steps"] = all_steps

    return workflow


def _extract_barcode_scans(protocol: ProtocolInfo) -> list[dict[str, Any]]:
    """Detect barcode scanning processes.

    Common pattern: a process named "Scan ..." that Place Plates to location 6
    (scanner), reads plate.barcode[EAST], then Place Plates back.
    """
    scans: list[dict[str, Any]] = []

    for proc in protocol.main:
        if proc.is_pipette_process:
            continue

        # Look for processes with "scan" in the name that have Place Plate to location 6
        has_scan_location = False
        barcode_variable = ""
        plate_name = proc.plate.name

        for task in proc.tasks:
            # Detect Place Plate to scanner location (6)
            if "Place Plate" in task.task_type:
                loc = _get_param(task.parameters, "Location to use", "")
                desc = task.description.lower()
                if loc == "6" or "scan barcode" in desc or "barcode" in desc:
                    has_scan_location = True

            # Detect barcode variable extraction from JavaScript
            if task.script:
                # Look for patterns like: destBC1 = plate.barcode[EAST]
                match = re.search(r'(\w+)\s*=\s*plate\.barcode\[EAST\]', task.script)
                if match:
                    barcode_variable = match.group(1)

        if has_scan_location and plate_name:
            scan: dict[str, Any] = {
                "action": "scan barcode",
                "plate": plate_name,
                "reader": "location 6",
            }
            if barcode_variable:
                scan["store_as"] = barcode_variable
            scans.append(scan)

    return scans


# ---------------------------------------------------------------------------
# YAML output
# ---------------------------------------------------------------------------

def _yaml_repr(obj: Any, indent: int = 0) -> str:
    """Simple YAML-like serialization without requiring PyYAML."""
    prefix = "  " * indent
    if isinstance(obj, dict):
        if not obj:
            return "{}"
        lines = []
        for key, value in obj.items():
            key_str = str(key)
            if isinstance(value, (dict, list)):
                lines.append(f"{prefix}{key_str}:")
                lines.append(_yaml_repr(value, indent + 1))
            else:
                lines.append(f"{prefix}{key_str}: {_yaml_value(value)}")
        return "\n".join(lines)
    elif isinstance(obj, list):
        if not obj:
            return f"{prefix}[]"
        lines = []
        for item in obj:
            if isinstance(item, dict):
                # First key on same line as dash
                items = list(item.items())
                if items:
                    first_key, first_val = items[0]
                    if isinstance(first_val, (dict, list)):
                        lines.append(f"{prefix}- {first_key}:")
                        lines.append(_yaml_repr(first_val, indent + 2))
                    else:
                        lines.append(f"{prefix}- {first_key}: {_yaml_value(first_val)}")
                    for key, value in items[1:]:
                        if isinstance(value, (dict, list)):
                            lines.append(f"{prefix}  {key}:")
                            lines.append(_yaml_repr(value, indent + 2))
                        else:
                            lines.append(f"{prefix}  {key}: {_yaml_value(value)}")
            else:
                lines.append(f"{prefix}- {_yaml_value(item)}")
        return "\n".join(lines)
    else:
        return f"{prefix}{_yaml_value(obj)}"


def _yaml_value(val: Any) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, str):
        if "\n" in val or ":" in val or val.startswith("["):
            return f'"{val}"'
        return val
    if val is None:
        return "null"
    return str(val)


def workflow_to_yaml(workflow: dict[str, Any]) -> str:
    """Serialize a workflow dict to YAML-like text."""
    return _yaml_repr(workflow)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def import_pro(path: str | Path) -> dict[str, Any]:
    """Import a legacy .pro file and return a workflow dict.

    This is the main entry point for the importer.

    >>> workflow = import_pro("plate_stamp.pro")
    >>> print(workflow_to_yaml(workflow))
    """
    protocol = parse_pro_file(path)
    return protocol_to_workflow(protocol)
