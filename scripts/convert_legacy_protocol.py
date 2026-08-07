"""Convert a legacy .pro protocol file to a Python script for OpenBravo.

Parses the XML-based .pro protocol format, extracts variables, deck layout,
subprocess definitions (tips on/off, aspirate, dispense, mix, loops), and
generates a Python script that uses pybravo.bravo.Bravo as a library.

Limitations:
  - Head mode subsets (half-plate columns) are noted in comments but the
    generated code uses full-head operations.
  - JavaScript TaskScript expressions (dynamic well selection) are emitted
    as comments for manual translation.
  - Place Plate tasks become user prompts (input()) since they require
    physical plate movement.
  - Mix [Dual Height] is implemented as repeated aspirate/dispense cycles.

Usage:
    python scripts/convert_legacy_protocol.py "path/to/protocol.pro"
    python scripts/convert_legacy_protocol.py "path/to/protocol.pro" -o output.py
    python scripts/convert_legacy_protocol.py "path/to/protocol.pro" --dry-run
"""

from __future__ import annotations

import argparse
import html
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_CATALOG_PATH = Path(__file__).resolve().parents[1] / "config" / "labware_catalog.snapshot.yaml"
_LIQUID_CLASSES_PATH = Path(__file__).resolve().parents[1] / "config" / "liquid_classes.yaml"


def _load_labware_catalog() -> dict[str, str]:
    """Load labware catalog and return {name_lower: id} mapping."""
    if not _CATALOG_PATH.exists():
        return {}
    with open(_CATALOG_PATH, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return {
        item["name"].lower(): item["id"]
        for item in data.get("labware", [])
        if item.get("name") and item.get("id")
    }


def _load_liquid_classes() -> dict[str, str]:
    """Load liquid classes and return {name_lower: liquid_class_id} mapping."""
    if not _LIQUID_CLASSES_PATH.exists():
        return {}
    with open(_LIQUID_CLASSES_PATH, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return {
        item["name"].lower(): item["liquid_class_id"]
        for item in data.get("liquid_classes", [])
        if item.get("name") and item.get("liquid_class_id")
    }


_WELLSELECTION_RE = re.compile(
    r"task\.Wellselection\s*=\s*\[\[\s*(.+?)\s*,\s*(.+?)\s*\]\]",
    re.IGNORECASE,
)


def _js_expr_to_python_0indexed(expr: str) -> str:
    """Convert a 1-indexed JavaScript expression to a 0-indexed Python expression."""
    expr = expr.strip().rstrip(";")

    try:
        val = int(expr)
        return str(val - 1)
    except ValueError:
        pass

    m = re.match(r"^(.+?)\s*\+\s*(\d+)\s*$", expr)
    if m:
        base, n = m.group(1).strip(), int(m.group(2))
        if n - 1 == 0:
            return base
        return f"{base} + {n - 1}"

    m = re.match(r"^(\d+)\s*-\s*(.+)$", expr)
    if m:
        n, rest = int(m.group(1)), m.group(2).strip()
        return f"{n - 1} - {rest}"

    return f"({expr}) - 1"


def _parse_wellselection_script(script: str) -> tuple[str, str] | None:
    """Parse ``task.Wellselection = [[row, col]]`` from a TaskScript.

    Returns (row_expr, col_expr) as 0-indexed Python expression strings,
    or None if no well selection override was found.
    """
    m = _WELLSELECTION_RE.search(script)
    if not m:
        return None
    row_raw = m.group(1)
    col_raw = m.group(2)
    return _js_expr_to_python_0indexed(row_raw), _js_expr_to_python_0indexed(col_raw)


@dataclass
class Variable:
    name: str
    value: str


@dataclass
class HeadMode:
    columns: int = 12
    rows: int = 8
    subset_config: int = 0
    subset_type: int = 0
    tip_type: int = 0


@dataclass
class WellSelection:
    column: int = 0
    row: int = 0


@dataclass
class PipetteStep:
    task_type: str  # aspirate, dispense, tips_on, tips_off, mix, set_head_mode
    plate: str = ""
    location: str = ""
    volume: str = ""
    volume_script: str = ""
    liquid_class: str = ""
    distance_from_bottom: str = "0"
    distance_from_bottom_script: str = ""
    empty_tips: bool = False
    blowout_volume: str = "0"
    pre_aspirate: str = "0"
    post_aspirate: str = "0"
    dynamic_tip_extension: str = "0"
    dynamic_tip_retraction: str = "0"
    tip_touch: bool = False
    tip_touch_sides: str = "None"
    tip_touch_retract: str = "0"
    tip_touch_offset: str = "0"
    mix_cycles: int = 0
    mix_aspirate_distance: str = "0"
    mix_dispense_distance: str = "0"
    head_mode: HeadMode | None = None
    well_selection: WellSelection | None = None
    well_selection_script: str = ""
    well_selection_row_expr: str = ""
    well_selection_col_expr: str = ""
    mark_tips_used: bool = False
    task_script: str = ""
    task_number: int = 0
    task_description: str = ""
    estimated_time: int = 0
    disabled: bool = False
    skipped: bool = False


@dataclass
class LoopStart:
    count: int = 1
    change_tips_every: int = 1
    variable_name: str = "LoopCounter"
    initial_value: str = "1"
    increment: str = "1"


@dataclass
class LoopEnd:
    pass


@dataclass
class GroupBegin:
    pass


@dataclass
class GroupEnd:
    pass


@dataclass
class IncubateStep:
    time_seconds: int = 0
    plate: str = ""
    location: str = ""


@dataclass
class PlacePlateStep:
    device: str = ""
    location: str = ""


@dataclass
class ReserveLocationStep:
    plate: str = ""
    location: str = ""
    time_seconds: int = 0


@dataclass
class UserMessageStep:
    title: str = ""
    body: str = ""
    script: str = ""
    pause: bool = False


@dataclass
class SubProcessCall:
    name: str = ""
    labware_config: dict[str, str] = field(default_factory=dict)


@dataclass
class SubProcess:
    name: str
    steps: list = field(default_factory=list)


@dataclass
class Process:
    plate_name: str = ""
    plate_type: str = ""
    steps: list = field(default_factory=list)


@dataclass
class Protocol:
    description: str = ""
    device_file: str = ""
    variables: list[Variable] = field(default_factory=list)
    processes: list[Process] = field(default_factory=list)
    subprocesses: dict[str, SubProcess] = field(default_factory=dict)
    labware_config: dict[str, str] = field(default_factory=dict)


def _unescape(s: str) -> str:
    return html.unescape(s)


def _get_param(params_elem: ET.Element, name: str, category: str = "") -> str | None:
    for p in params_elem.findall("Parameter"):
        if p.get("Name") == name:
            if category and p.get("Category", "") != category:
                continue
            return _unescape(p.get("Value", ""))
    return None


def _get_param_script(params_elem: ET.Element, name: str) -> str:
    for p in params_elem.findall("Parameter"):
        if p.get("Name") == name:
            script = p.get("TaskParameterScript", "")
            return _unescape(script) if script else ""
    return ""


def _parse_head_mode_xml(raw: str) -> HeadMode:
    raw = _unescape(raw)
    try:
        inner = ET.fromstring(raw)
        phm = inner.find(".//PipetteHeadMode")
        if phm is not None:
            return HeadMode(
                columns=int(phm.get("ColumnCount", "12")),
                rows=int(phm.get("RowCount", "8")),
                subset_config=int(phm.get("SubsetConfig", "0")),
                subset_type=int(phm.get("SubsetType", "0")),
                tip_type=int(phm.get("TipType", "0")),
            )
    except ET.ParseError:
        pass
    return HeadMode()


def _parse_well_selection_xml(raw: str) -> WellSelection:
    raw = _unescape(raw)
    try:
        inner = ET.fromstring(raw)
        well = inner.find(".//Well")
        if well is not None:
            return WellSelection(
                column=int(well.get("Column", "0")),
                row=int(well.get("Row", "0")),
            )
    except ET.ParseError:
        pass
    return WellSelection()


def _parse_pipette_task(task: ET.Element, task_type: str) -> PipetteStep:
    step = PipetteStep(task_type=task_type)
    step.disabled = task.findtext("Task_Disabled", "0") == "1"
    step.skipped = task.findtext("Task_Skipped", "0") == "1"

    ts = task.find("TaskScript")
    if ts is not None:
        step.task_script = _unescape(ts.get("Value", ""))

    adv = task.find("Advanced_Settings")
    if adv is not None:
        for s in adv.findall("Setting"):
            if s.get("Name") == "Estimated time":
                step.estimated_time = int(s.get("Value", "0"))

    for params in task.findall("Parameters"):
        plate = _get_param(params, "Location, plate")
        if plate:
            step.plate = plate
        loc = _get_param(params, "Location, location")
        if loc:
            step.location = loc

        vol = _get_param(params, "Volume", "Volume")
        if vol is not None:
            step.volume = vol
        vol_script = _get_param_script(params, "Volume")
        if vol_script:
            step.volume_script = vol_script

        pre = _get_param(params, "Pre-aspirate volume", "Volume")
        if pre is not None:
            step.pre_aspirate = pre
        post = _get_param(params, "Post-aspirate volume", "Volume")
        if post is not None:
            step.post_aspirate = post

        empty = _get_param(params, "Empty tips", "Volume")
        if empty is not None:
            step.empty_tips = empty == "1"
        blow = _get_param(params, "Blowout volume", "Volume")
        if blow is not None:
            step.blowout_volume = blow

        lc = _get_param(params, "Liquid class", "Properties")
        if lc is not None:
            step.liquid_class = lc

        dist = _get_param(params, "Distance from well bottom", "Properties")
        if dist is not None:
            step.distance_from_bottom = dist
        dist_script = _get_param_script(params, "Distance from well bottom")
        if dist_script:
            step.distance_from_bottom_script = dist_script

        dte = _get_param(params, "Dynamic tip extension", "Properties")
        if dte is not None:
            step.dynamic_tip_extension = dte
        dtr = _get_param(params, "Dynamic tip retraction", "Properties")
        if dtr is not None:
            step.dynamic_tip_retraction = dtr

        tt = _get_param(params, "Perform tip touch", "Tip Touch")
        if tt is not None:
            step.tip_touch = tt == "1"
        tt_sides = _get_param(params, "Which sides to use for tip touch", "Tip Touch")
        if tt_sides is not None:
            step.tip_touch_sides = tt_sides
        tt_retract = _get_param(params, "Tip touch retract distance", "Tip Touch")
        if tt_retract is not None:
            step.tip_touch_retract = tt_retract
        tt_offset = _get_param(params, "Tip touch horizontal offset", "Tip Touch")
        if tt_offset is not None:
            step.tip_touch_offset = tt_offset

        cycles = _get_param(params, "Mix cycles", "Properties")
        if cycles is not None:
            step.mix_cycles = int(cycles)
        asp_dist = _get_param(params, "Aspirate distance", "Distance From Well Bottom")
        if asp_dist is not None:
            step.mix_aspirate_distance = asp_dist
        dsp_dist = _get_param(params, "Dispense distance", "Distance From Well Bottom")
        if dsp_dist is not None:
            step.mix_dispense_distance = dsp_dist

        mark = _get_param(params, "Mark tips as used", "Properties")
        if mark is not None:
            step.mark_tips_used = mark == "1"

        ws_raw = _get_param(params, "Well selection", "Properties")
        if ws_raw:
            step.well_selection = _parse_well_selection_xml(ws_raw)

        tn = _get_param(params, "Task number", "Task Description")
        if tn is not None:
            step.task_number = int(tn)
        td = _get_param(params, "Task description", "Task Description")
        if td is not None:
            step.task_description = td

    phm = task.find("PipetteHead/PipetteHeadMode")
    if phm is not None:
        step.head_mode = HeadMode(
            columns=int(phm.get("ColumnCount", "12")),
            rows=int(phm.get("RowCount", "8")),
            subset_config=int(phm.get("SubsetConfig", "0")),
        )

    if step.task_script:
        ws = _parse_wellselection_script(step.task_script)
        if ws:
            step.well_selection_row_expr, step.well_selection_col_expr = ws

    return step


def parse_protocol(path: Path) -> Protocol:
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    text = re.sub(r"encoding='ASCII'", "encoding='utf-8'", text)
    root = ET.fromstring(text)
    proto = Protocol()

    fi = root.find("File_Info")
    if fi is not None:
        proto.description = fi.get("Description", "")
        proto.device_file = fi.get("Device_File", "")

    for proc_elem in root.findall(".//Main_Processes/Process"):
        process = Process()
        pp = proc_elem.find("Plate_Parameters")
        if pp is not None:
            for p in pp.findall("Parameter"):
                if p.get("Name") == "Plate name":
                    process.plate_name = p.get("Value", "")
                elif p.get("Name") == "Plate type":
                    process.plate_type = p.get("Value", "")

        for task in proc_elem:
            if task.tag != "Task":
                continue
            task_name = task.get("Name", "")
            _parse_process_task(task, task_name, process.steps)

        proto.processes.append(process)

    for sp_elem in root.findall(".//Main_Processes/Pipette_Process"):
        sp_name = sp_elem.get("Name", "")
        subprocess = SubProcess(name=sp_name)

        for task in sp_elem:
            if task.tag != "Task":
                continue
            task_name = task.get("Name", "")
            _parse_subprocess_task(task, task_name, subprocess.steps)

        proto.subprocesses[sp_name] = subprocess

    return proto


def _parse_process_task(task: ET.Element, task_name: str, steps: list):
    if "Place Plate" in task_name:
        pp = PlacePlateStep()
        devs = task.find("Devices/Device")
        if devs is not None:
            pp.device = devs.get("Device_Name", "")
            pp.location = devs.get("Location_Name", "")
        else:
            for params in task.findall("Parameters"):
                loc = _get_param(params, "Location to use")
                if loc:
                    pp.location = loc
        steps.append(pp)

    elif "JavaScript" in task_name:
        ts = task.find("TaskScript")
        script = ""
        if ts is not None:
            script = _unescape(ts.get("Value", ""))
        steps.append(UserMessageStep(title="JavaScript", script=script))

    elif "Define Variables" in task_name:
        arr = task.find("arrVariables")
        if arr is not None:
            for key, val in arr.attrib.items():
                steps.append(Variable(name=key, value=val))

    elif "User Message" in task_name:
        msg = UserMessageStep()
        ts = task.find("TaskScript")
        if ts is not None:
            msg.script = _unescape(ts.get("Value", ""))
        for params in task.findall("Parameters"):
            title = _get_param(params, "Title")
            if title:
                msg.title = title
            body = _get_param(params, "Body")
            if body:
                msg.body = body
            pause = _get_param(params, "Pause process")
            if pause:
                msg.pause = pause == "1"
        steps.append(msg)

    elif "Incubate" in task_name:
        inc = IncubateStep()
        devs = task.find("Devices/Device")
        if devs is not None:
            inc.location = devs.get("Location_Name", "")
        for params in task.findall("Parameters"):
            t = _get_param(params, "Incubation time")
            if t:
                inc.time_seconds = int(t)
        steps.append(inc)

    elif "SubProcess" in task_name:
        sp = SubProcessCall()
        for params in task.findall("Parameters"):
            name = _get_param(params, "Sub-process name")
            if name:
                sp.name = name
            for p in params.findall("Parameter"):
                cat = p.get("Category", "")
                if cat == "Static labware configuration":
                    loc_num = p.get("Name", "")
                    val = _unescape(p.get("Value", ""))
                    if loc_num.isdigit() and val != "<use default>":
                        sp.labware_config[loc_num] = val
        steps.append(sp)


def _parse_subprocess_task(task: ET.Element, task_name: str, steps: list):
    if "Set Head Mode" in task_name:
        hm_step = PipetteStep(task_type="set_head_mode")
        for params in task.findall("Parameters"):
            raw = _get_param(params, "Head mode")
            if raw:
                hm_step.head_mode = _parse_head_mode_xml(raw)
        steps.append(hm_step)

    elif "Tips On" in task_name:
        step = _parse_pipette_task(task, "tips_on")
        steps.append(step)

    elif "Tips Off" in task_name:
        step = _parse_pipette_task(task, "tips_off")
        steps.append(step)

    elif "Aspirate" in task_name and "Mix" not in task_name:
        step = _parse_pipette_task(task, "aspirate")
        steps.append(step)

    elif "Dispense" in task_name and "Mix" not in task_name:
        step = _parse_pipette_task(task, "dispense")
        steps.append(step)

    elif "Mix" in task_name:
        step = _parse_pipette_task(task, "mix")
        steps.append(step)

    elif "Loop End" in task_name:
        steps.append(LoopEnd())

    elif "Loop" in task_name:
        loop = LoopStart()
        for params in task.findall("Parameters"):
            n = _get_param(params, "Number of times to loop")
            if n:
                loop.count = int(n)
            ct = _get_param(params, "Change tips every N times, N = ")
            if ct:
                loop.change_tips_every = int(ct)
        for var in task.findall("Variables/Variable"):
            loop.variable_name = var.get("strVariableName", "LoopCounter")
            loop.initial_value = var.get("strInitialValue", "1")
            loop.increment = var.get("fIncrement", "1")
        steps.append(loop)

    elif "Group Begin" in task_name:
        steps.append(GroupBegin())

    elif "Group End" in task_name:
        steps.append(GroupEnd())

    elif "Reserve Location" in task_name:
        rl = ReserveLocationStep()
        for params in task.findall("Parameters"):
            plate = _get_param(params, "Location to use, plate")
            if plate:
                rl.plate = plate
            t = _get_param(params, "Reservation time")
            if t:
                rl.time_seconds = int(t)
        steps.append(rl)


def _plate_to_var(plate_name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "_", plate_name).upper().strip("_")


def _describe_head_mode(hm: HeadMode) -> str:
    total = hm.columns * hm.rows
    if hm.subset_config == 0 and hm.columns == 12:
        return f"full {total}-channel"
    return f"{hm.columns}x{hm.rows} (subset={hm.subset_config})"


_LEGACY_SUBSET_CONFIG_MAP = {
    0: "back_left",
    1: "back_right",
    2: "back_left",
    3: "back_right",
}


def _legacy_to_head_mode(hm: HeadMode) -> tuple[str, str, int]:
    """Map the legacy PipetteHeadMode to OpenBravo set_head_mode args.

    Returns (subset_type, subset_config, column_count).
    """
    if hm.columns == 12 and hm.rows == 8 and hm.subset_config == 0:
        return "all_barrels", "back_left", 12

    subset_config = _LEGACY_SUBSET_CONFIG_MAP.get(hm.subset_config, "back_left")

    if hm.subset_type == 1:
        return "column", subset_config, hm.columns
    elif hm.subset_type == 0:
        return "row", subset_config, hm.columns
    else:
        return "rectangle", subset_config, hm.columns


def generate_python(proto: Protocol, pro_filename: str) -> str:
    lines: list[str] = []

    plate_locations: dict[str, str] = {}
    labware_types: dict[str, str] = {}
    for proc in proto.processes:
        if proc.plate_name:
            labware_types[proc.plate_name] = proc.plate_type
            for s in proc.steps:
                if isinstance(s, PlacePlateStep) and s.location:
                    plate_locations.setdefault(proc.plate_name, s.location)
                    break

    catalog = _load_labware_catalog()
    liquid_classes = _load_liquid_classes()

    labware_ids: dict[str, str] = {}
    for plate_name, plate_type in labware_types.items():
        lid = catalog.get(plate_type.lower(), "")
        if lid:
            labware_ids[plate_name] = lid

    all_lc_names: set[str] = set()
    for sp in proto.subprocesses.values():
        for s in sp.steps:
            if isinstance(s, PipetteStep) and s.liquid_class:
                all_lc_names.add(s.liquid_class)

    lc_ids: dict[str, str] = {}
    for lc_name in all_lc_names:
        lcid = liquid_classes.get(lc_name.lower(), "")
        if lcid:
            lc_ids[lc_name] = lcid

    lines.append('"""Auto-generated from legacy protocol: %s' % pro_filename)
    lines.append("")
    lines.append("Deck layout:")
    for name, loc in sorted(plate_locations.items(), key=lambda x: int(x[1]) if x[1].isdigit() else 0):
        ltype = labware_types.get(name, "")
        lines.append(f"    Location {loc}: {name} ({ltype})")
    lines.append("")
    lines.append("Usage:")
    lines.append("    python -B scripts/%s" % pro_filename.replace(".pro", ".py"))
    lines.append('    python -B scripts/%s --profile profiles/Opportunity.yaml' % pro_filename.replace(".pro", ".py"))
    lines.append('    python -B scripts/%s --simulation' % pro_filename.replace(".pro", ".py"))
    lines.append('"""')
    lines.append("")
    lines.append("from __future__ import annotations")
    lines.append("")
    lines.append("import argparse")
    lines.append("import asyncio")
    lines.append("import logging")
    lines.append("import sys")
    lines.append("import time")
    lines.append("from pathlib import Path")
    lines.append("")
    lines.append("sys.path.insert(0, str(Path(__file__).resolve().parents[1]))")
    lines.append("")
    lines.append("from pybravo.bravo import Bravo")
    lines.append("from pybravo.types import Axis")
    lines.append("")
    lines.append("logging.basicConfig(")
    lines.append('    level=logging.INFO,')
    lines.append('    format="%(asctime)s  %(levelname)-7s  %(message)s",')
    lines.append('    datefmt="%H:%M:%S",')
    lines.append(")")
    lines.append("logger = logging.getLogger(__name__)")
    lines.append("")

    lines.append("# --- Deck layout ---")
    for name, loc in sorted(plate_locations.items(), key=lambda x: int(x[1]) if x[1].isdigit() else 0):
        var = _plate_to_var(name) + "_LOCATION"
        lines.append(f"{var} = {loc}")
    lines.append("")

    lines.append("# --- Labware IDs ---")
    for name in sorted(labware_types.keys()):
        var = _plate_to_var(name) + "_ID"
        lid = labware_ids.get(name, "")
        if lid:
            lines.append(f'{var} = "{lid}"  # {labware_types[name]}')
        else:
            lines.append(f'{var} = ""  # {labware_types[name]} (NOT FOUND in catalog)')
    lines.append("")

    if all_lc_names:
        lines.append("# --- Liquid classes ---")
        for lc_name in sorted(all_lc_names):
            var = "LC_" + re.sub(r"[^a-zA-Z0-9]", "_", lc_name).upper().strip("_")
            lcid = lc_ids.get(lc_name, "")
            if lcid:
                lines.append(f'{var} = "{lc_name}"  # id={lcid}')
            else:
                lines.append(f'{var} = "{lc_name}"  # NOT FOUND in liquid_classes.yaml')
        lines.append("")

    variables = []
    main_proc = proto.processes[0] if proto.processes else None
    if main_proc:
        for s in main_proc.steps:
            if isinstance(s, Variable):
                variables.append(s)
    if variables:
        lines.append("# --- Protocol variables ---")
        for v in variables:
            lines.append(f"{v.name} = {v.value}")
        lines.append("")

    lines.append("ALL_AXES = [Axis.X, Axis.Y, Axis.Z, Axis.W, Axis.G, Axis.Zg]")
    lines.append("")
    lines.append("")
    lines.append('def step(name: str):')
    lines.append('    """Print a step banner and return start time."""')
    lines.append("    print(f\"\\n{'='*60}\")")
    lines.append("    print(f\"  {name}\")")
    lines.append("    print(f\"{'='*60}\")")
    lines.append("    return time.monotonic()")
    lines.append("")
    lines.append("")
    lines.append("def done(t0: float):")
    lines.append("    elapsed = time.monotonic() - t0")
    lines.append('    print(f"  OK ({elapsed:.1f}s)")')
    lines.append("")
    lines.append("")
    lines.append("def pause(msg: str = 'Press Enter to continue...'):")
    lines.append("    input(f'  >>> {msg}')")
    lines.append("")
    lines.append("")

    lines.append("async def run(profile_path: str, simulation: bool) -> int:")
    lines.append("    bravo = Bravo(")
    lines.append('        profile=profile_path,')
    lines.append('        mode="simulation" if simulation else None,')
    lines.append("    )")
    lines.append("")

    step_num = [0]

    def next_step(label: str) -> str:
        step_num[0] += 1
        return f"{step_num[0]}. {label}"

    lines.append("    # --- Connect ---")
    lines.append(f"    t0 = step('{next_step('Connect')}')")
    lines.append("    bravo.connect()")
    lines.append("    done(t0)")
    lines.append("")

    lines.append("    try:")
    lines.append("        # --- Initialize ---")
    lines.append(f"        t0 = step('{next_step('Initialize')}')")
    lines.append("        all_homed = all(bravo.controller.is_axis_homed(ax) for ax in ALL_AXES)")
    lines.append("        if all_homed:")
    lines.append("            print('     All axes already homed')")
    lines.append("            bravo._initialized = True")
    lines.append("        else:")
    lines.append("            await bravo.initialize(auto_confirm=True)")
    lines.append("        done(t0)")
    lines.append("")

    lines.append("        # --- Deck setup ---")
    lines.append(f"        t0 = step('{next_step('Set up deck')}')")
    for name, loc in sorted(plate_locations.items(), key=lambda x: int(x[1]) if x[1].isdigit() else 0):
        loc_var = _plate_to_var(name) + "_LOCATION"
        id_var = _plate_to_var(name) + "_ID"
        lines.append(f"        if {id_var}:")
        lines.append(f"            bravo.set_labware({loc_var}, {id_var})")
        lines.append(f"            print(f'     Location {{{loc_var}}}: {name}')")
    lines.append("        done(t0)")
    lines.append("")

    if main_proc:
        _generate_process_steps(lines, main_proc, proto, plate_locations, step_num, indent=2)

    lines.append("        print(f\"\\n{'='*60}\")")
    lines.append("        print('  ALL STEPS COMPLETED SUCCESSFULLY')")
    lines.append("        print(f\"{'='*60}\\n\")")
    lines.append("        return 0")
    lines.append("")
    lines.append("    except Exception:")
    lines.append("        logger.exception('Protocol failed')")
    lines.append("        return 1")
    lines.append("")
    lines.append("    finally:")
    lines.append(f"        t0 = step('{next_step('Disconnect')}')")
    lines.append("        bravo.disconnect()")
    lines.append("        done(t0)")
    lines.append("")
    lines.append("")
    lines.append("def main() -> int:")
    lines.append("    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)")
    lines.append("    ap.add_argument(")
    lines.append("        '--profile', default='profiles/Opportunity.yaml',")
    lines.append("        help='Path to Bravo profile YAML',")
    lines.append("    )")
    lines.append("    ap.add_argument(")
    lines.append("        '--simulation', action='store_true',")
    lines.append("        help='Run in simulation mode',")
    lines.append("    )")
    lines.append("    args = ap.parse_args()")
    lines.append("    return asyncio.run(run(args.profile, args.simulation))")
    lines.append("")
    lines.append("")
    lines.append("if __name__ == '__main__':")
    lines.append("    sys.exit(main())")
    lines.append("")

    return "\n".join(lines)


def _generate_process_steps(
    lines: list[str],
    process: Process,
    proto: Protocol,
    plate_locations: dict[str, str],
    step_num: list[int],
    indent: int = 2,
):
    pad = "    " * indent
    current_plate_location: str | None = None
    first_place = True

    for s in process.steps:
        if isinstance(s, Variable):
            continue

        if isinstance(s, PlacePlateStep):
            step_num[0] += 1
            if first_place:
                lines.append(f"{pad}# --- Place {process.plate_name} at location {s.location} (operator) ---")
                lines.append(f"{pad}t0 = step('{step_num[0]}. Place {process.plate_name} at location {s.location}')")
                lines.append(f"{pad}pause('Place {process.plate_name} at location {s.location}, then press Enter')")
                lines.append(f"{pad}done(t0)")
                first_place = False
            else:
                from_loc = current_plate_location or "?"
                lines.append(f"{pad}# --- Pick/Place {process.plate_name}: {from_loc} -> {s.location} ---")
                lines.append(f"{pad}t0 = step('{step_num[0]}. Pick/place {process.plate_name} ({from_loc} -> {s.location})')")
                lines.append(f"{pad}await bravo.pick_place({from_loc}, {s.location})")
                lines.append(f"{pad}done(t0)")
            current_plate_location = s.location
            lines.append("")

        elif isinstance(s, UserMessageStep):
            if s.script:
                lines.append(f"{pad}# Original protocol JavaScript: {s.script[:80]}...")
            if s.title == "JavaScript":
                continue

        elif isinstance(s, IncubateStep):
            step_num[0] += 1
            lines.append(f"{pad}# --- Incubate {s.time_seconds}s ---")
            lines.append(f"{pad}t0 = step('{step_num[0]}. Incubate {s.time_seconds}s at location {s.location}')")
            lines.append(f"{pad}print(f'     Waiting {s.time_seconds} seconds...')")
            lines.append(f"{pad}await asyncio.sleep({s.time_seconds})")
            lines.append(f"{pad}done(t0)")
            lines.append("")

        elif isinstance(s, SubProcessCall):
            sp = proto.subprocesses.get(s.name)
            if sp:
                step_num[0] += 1
                lines.append(f"{pad}# {'='*50}")
                lines.append(f"{pad}# SubProcess: {s.name}")
                lines.append(f"{pad}# {'='*50}")
                lines.append(f"{pad}t0 = step('{step_num[0]}. {s.name}')")
                lines.append("")
                _generate_subprocess_steps(lines, sp, plate_locations, indent)
                lines.append(f"{pad}done(t0)")
                lines.append("")


def _generate_subprocess_steps(
    lines: list[str],
    sp: SubProcess,
    plate_locations: dict[str, str],
    indent: int = 2,
):
    pad = "    " * indent
    loop_depth = 0

    for s in sp.steps:
        extra_pad = "    " * loop_depth

        if isinstance(s, PipetteStep):
            if s.disabled or s.skipped:
                lines.append(f"{pad}{extra_pad}# DISABLED/SKIPPED: {s.task_type}")
                continue

            if s.task_type == "set_head_mode" and s.head_mode:
                subset_type, subset_config, col_count = _legacy_to_head_mode(s.head_mode)
                desc = _describe_head_mode(s.head_mode)
                lines.append(f"{pad}{extra_pad}# Head mode: {desc}")
                lines.append(f"{pad}{extra_pad}bravo.set_head_mode('{subset_type}', '{subset_config}', column_count={col_count})")
                continue

            if s.task_type == "tips_on":
                loc = plate_locations.get(s.plate, "?")
                if s.well_selection_row_expr and s.well_selection_col_expr:
                    lines.append(f"{pad}{extra_pad}bravo.set_tip_selection({loc}, row={s.well_selection_row_expr}, col={s.well_selection_col_expr})")
                elif s.well_selection:
                    lines.append(f"{pad}{extra_pad}bravo.set_tip_selection({loc}, row={s.well_selection.row}, col={s.well_selection.column})")
                lines.append(f"{pad}{extra_pad}await bravo.tips_on({loc})")
                continue

            if s.task_type == "tips_off":
                loc = plate_locations.get(s.plate, "?")
                if s.well_selection_row_expr and s.well_selection_col_expr:
                    lines.append(f"{pad}{extra_pad}bravo.set_tip_selection({loc}, row={s.well_selection_row_expr}, col={s.well_selection_col_expr})")
                elif s.well_selection:
                    lines.append(f"{pad}{extra_pad}bravo.set_tip_selection({loc}, row={s.well_selection.row}, col={s.well_selection.column})")
                lines.append(f"{pad}{extra_pad}await bravo.tips_off({loc})")
                continue

            if s.task_type == "aspirate":
                loc = plate_locations.get(s.plate, "?")
                vol_expr = s.volume_script.lstrip("=") if s.volume_script else s.volume
                dist = s.distance_from_bottom_script.lstrip("=") if s.distance_from_bottom_script else s.distance_from_bottom
                lc = f", liquid_class='{s.liquid_class}'" if s.liquid_class else ""
                if s.well_selection_row_expr and s.well_selection_col_expr:
                    lines.append(f"{pad}{extra_pad}bravo.set_plate_selection({loc}, row={s.well_selection_row_expr}, col={s.well_selection_col_expr})")
                elif s.well_selection:
                    lines.append(f"{pad}{extra_pad}bravo.set_plate_selection({loc}, row={s.well_selection.row}, col={s.well_selection.column})")
                lines.append(f"{pad}{extra_pad}await bravo.aspirate({loc}, volume={vol_expr}, distance_from_bottom={dist}{lc})")
                continue

            if s.task_type == "dispense":
                loc = plate_locations.get(s.plate, "?")
                vol_expr = s.volume_script.lstrip("=") if s.volume_script else s.volume
                dist = s.distance_from_bottom_script.lstrip("=") if s.distance_from_bottom_script else s.distance_from_bottom
                lc = f", liquid_class='{s.liquid_class}'" if s.liquid_class else ""
                if s.well_selection_row_expr and s.well_selection_col_expr:
                    lines.append(f"{pad}{extra_pad}bravo.set_plate_selection({loc}, row={s.well_selection_row_expr}, col={s.well_selection_col_expr})")
                elif s.well_selection:
                    lines.append(f"{pad}{extra_pad}bravo.set_plate_selection({loc}, row={s.well_selection.row}, col={s.well_selection.column})")
                if s.empty_tips:
                    lines.append(f"{pad}{extra_pad}await bravo.dispense({loc}, volume={vol_expr}, empty_tips=True, distance_from_bottom={dist}{lc})")
                else:
                    lines.append(f"{pad}{extra_pad}await bravo.dispense({loc}, volume={vol_expr}, distance_from_bottom={dist}{lc})")
                continue

            if s.task_type == "mix":
                loc = plate_locations.get(s.plate, "?")
                vol_expr = s.volume_script.lstrip("=") if s.volume_script else s.volume
                cycles = s.mix_cycles
                lc = f", liquid_class='{s.liquid_class}'" if s.liquid_class else ""
                if s.well_selection_row_expr and s.well_selection_col_expr:
                    lines.append(f"{pad}{extra_pad}bravo.set_plate_selection({loc}, row={s.well_selection_row_expr}, col={s.well_selection_col_expr})")
                elif s.well_selection:
                    lines.append(f"{pad}{extra_pad}bravo.set_plate_selection({loc}, row={s.well_selection.row}, col={s.well_selection.column})")
                lines.append(f"{pad}{extra_pad}# Mix {cycles} cycles, aspirate_dist={s.mix_aspirate_distance}mm, dispense_dist={s.mix_dispense_distance}mm")
                lines.append(f"{pad}{extra_pad}for _mix_i in range({cycles}):")
                lines.append(f"{pad}{extra_pad}    await bravo.aspirate({loc}, volume={vol_expr}, distance_from_bottom={s.mix_aspirate_distance}{lc})")
                lines.append(f"{pad}{extra_pad}    await bravo.dispense({loc}, volume={vol_expr}, distance_from_bottom={s.mix_dispense_distance}{lc})")
                continue

        elif isinstance(s, LoopStart):
            lines.append(f"{pad}{extra_pad}for {s.variable_name} in range({s.initial_value}, {int(s.initial_value) + s.count}):")
            lines.append(f"{pad}{extra_pad}    print(f'     Loop iteration {{{s.variable_name}}}/{s.count}')")
            loop_depth += 1
            continue

        elif isinstance(s, LoopEnd):
            loop_depth = max(0, loop_depth - 1)
            continue

        elif isinstance(s, GroupBegin):
            lines.append(f"{pad}{extra_pad}# --- Group ---")
            continue

        elif isinstance(s, GroupEnd):
            continue

        elif isinstance(s, ReserveLocationStep):
            lines.append(f"{pad}{extra_pad}# Reserve location for {s.plate} ({s.time_seconds}s)")
            lines.append(f"{pad}{extra_pad}await asyncio.sleep({s.time_seconds})")
            continue


def print_summary(proto: Protocol):
    print(f"\nProtocol: {proto.device_file}")
    print(f"Description: {proto.description or '(none)'}")

    variables = []
    if proto.processes:
        for s in proto.processes[0].steps:
            if isinstance(s, Variable):
                variables.append(s)
    if variables:
        print("\nVariables:")
        for v in variables:
            print(f"  {v.name} = {v.value}")

    print(f"\nProcesses ({len(proto.processes)} plates):")
    for i, proc in enumerate(proto.processes):
        print(f"  {i+1}. {proc.plate_name or '?'} ({proc.plate_type})")
        for s in proc.steps:
            if isinstance(s, PlacePlateStep):
                print(f"     - Place at location {s.location}")
            elif isinstance(s, SubProcessCall):
                print(f"     - SubProcess: {s.name}")
            elif isinstance(s, IncubateStep):
                print(f"     - Incubate {s.time_seconds}s")

    print(f"\nSubprocesses ({len(proto.subprocesses)}):")
    for name, sp in proto.subprocesses.items():
        step_types = []
        for s in sp.steps:
            if isinstance(s, PipetteStep):
                step_types.append(s.task_type)
            elif isinstance(s, LoopStart):
                step_types.append(f"loop({s.count}x)")
            elif isinstance(s, LoopEnd):
                step_types.append("end_loop")
        print(f"  {name}: {' -> '.join(step_types)}")


def main():
    parser = argparse.ArgumentParser(
        description="Convert a legacy .pro protocol to a Python script for OpenBravo."
    )
    parser.add_argument("pro_file", type=Path, help="Path to the legacy .pro file")
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Output Python file path (default: print to stdout)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Parse and print summary without generating code",
    )
    args = parser.parse_args()

    if not args.pro_file.exists():
        print(f"Error: {args.pro_file} not found", file=sys.stderr)
        sys.exit(1)

    proto = parse_protocol(args.pro_file)

    if args.dry_run:
        print_summary(proto)
        return

    code = generate_python(proto, args.pro_file.name)

    if args.output:
        args.output.write_text(code, encoding="utf-8")
        print(f"Written to {args.output}")
        print_summary(proto)
    else:
        print(code)


if __name__ == "__main__":
    main()
