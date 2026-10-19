"""Strict, non-authoring merge and state checks for staged local ActionPlans.

Every stage and every initial-supply claim comes from model output with exact
source-line references. This module never supplies a benchmark protocol step.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .action_ir import (
    Action,
    ActionPlan,
    ActionPlanError,
    LabwareFacts,
    LabwareLoad,
    ModuleLoad,
    Pickup,
    PipetteFacts,
    PipetteLoad,
    RefillTips,
    SourceSpan,
    _expand_actions,
)
from .material_ledger import (
    InitialWell,
    LedgerResult,
    WellLimit,
    WellRef,
    audit_material_flow,
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class StageSpec(_Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")
    goal: str = Field(min_length=3, max_length=300)
    evidence_refs: list[str] = Field(min_length=1, max_length=20)


class InitialSupply(_Strict):
    """A source-grounded per-well starting amount, or an explicit unknown."""

    labware: str = Field(min_length=1)
    selection: Literal["all", "wells_in_columns", "wells"]
    columns: list[int] = Field(default_factory=list, max_length=48)
    wells: list[str] = Field(default_factory=list, max_length=384)
    volume_ul: float | None = Field(default=None, ge=0)
    material_id: str = Field(min_length=1)
    source_material_name: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1, max_length=20)


class PhasedSetup(_Strict):
    labware: list[LabwareLoad]
    modules: list[ModuleLoad] = Field(default_factory=list)
    pipettes: list[PipetteLoad]
    stages: list[StageSpec] = Field(min_length=1, max_length=16)
    initial_supplies: list[InitialSupply] = Field(default_factory=list, max_length=48)


class StageDraft(_Strict):
    stage_id: str
    evidence_refs: list[str] = Field(min_length=1, max_length=20)
    actions: list[Action] = Field(min_length=1, max_length=64)


_QUANTITY = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(µl|μl|ul|ml)\b", re.I)
_COLUMN = re.compile(r"([A-Z]+)([1-9][0-9]*)\Z")


def _source_binds_supply(
    quote: str, *, item: LabwareLoad, supply: InitialSupply, well: str,
) -> str | None:
    """Return the smallest cited clause that binds this material to this well.

    Deck instructions sometimes describe a reservoir by numbered columns
    rather than literal well names. Keep each semicolon-separated assignment
    separate so that an amount for another reagent cannot support this one.
    """
    named_labware = any(
        re.search(rf"(?<![\w]){re.escape(name)}(?![\w])", quote, re.I)
        for name in (item.label, item.id, item.load_name) if name is not None
    )
    if item.slot is not None:
        named_labware |= re.search(rf"\bslot\s*{item.slot}\b", quote, re.I) is not None
    if not named_labware:
        return None
    column_match = _COLUMN.fullmatch(well)
    column = int(column_match.group(2)) if column_match else None
    has_column_assignments = re.search(r"\bcolumns?\s*:", quote, re.I) is not None
    for clause in quote.split(";"):
        if supply.source_material_name.casefold() not in clause.casefold():
            continue
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(well)}(?![A-Za-z0-9])",
                     clause, re.I):
            return clause
        if supply.selection == "all" and re.search(
            r"\b(?:all|every|each|entire)\b.*\b(?:well|plate|rack)\b|"
            r"\bempty\s+at\s+the\s+start\b", clause, re.I,
        ):
            return clause
        if not has_column_assignments or column is None:
            continue
        # A column list is a series of assignments: ``2 beads; 4 buffer;
        # 6–7 alcohol``. Strip only Markdown decoration, then require the
        # number or range at the start of this reagent's own clause.
        assignment = re.split(r"\bcolumns?\s*:", clause, maxsplit=1,
                              flags=re.I)[-1]
        assignment = re.sub(r"[*_`]", "", assignment).strip()
        match = re.match(r"^(\d+)(?:\s*[-–—]\s*(\d+))?\s+", assignment)
        if match and int(match.group(1)) <= column <= int(match.group(2) or match.group(1)):
            return clause
    return None


def _quotes(
    refs: Sequence[str], *, spans: Mapping[str, SourceSpan],
    instruction: str, paper: str | None,
) -> list[str]:
    if not refs or len(refs) != len(set(refs)):
        raise ActionPlanError("Every claim needs unique, nonempty source references.")
    result: list[str] = []
    for ref in refs:
        span = spans.get(ref)
        if span is None:
            raise ActionPlanError(f"Unknown source reference {ref!r}.")
        corpus = instruction if span.source == "instruction" else paper
        if corpus is None or not 0 <= span.start < span.end <= len(corpus):
            raise ActionPlanError(f"Invalid source reference {ref!r}.")
        result.append(corpus[span.start:span.end])
    return result


def _selected_wells(supply: InitialSupply, facts: LabwareFacts) -> tuple[str, ...]:
    if supply.selection == "all":
        if supply.columns or supply.wells:
            raise ActionPlanError("An all-wells supply cannot also name columns or wells.")
        return facts.ordered_wells
    if supply.selection == "wells":
        if supply.columns or not supply.wells or len(supply.wells) != len(set(supply.wells)):
            raise ActionPlanError("An explicit-wells supply needs unique wells and no columns.")
        if set(supply.wells) - facts.wells:
            raise ActionPlanError("An initial supply names a well absent from trusted labware.")
        return tuple(supply.wells)
    if supply.wells or not supply.columns or len(supply.columns) != len(set(supply.columns)):
        raise ActionPlanError("A column supply needs unique columns and no explicit wells.")
    grouped: dict[int, list[str]] = defaultdict(list)
    for well in facts.ordered_wells:
        match = _COLUMN.fullmatch(well)
        if match is None:
            raise ActionPlanError("Trusted labware wells cannot be grouped by column.")
        grouped[int(match.group(2))].append(well)
    if set(supply.columns) - set(grouped):
        raise ActionPlanError("An initial supply names a column absent from trusted labware.")
    return tuple(well for column in supply.columns for well in grouped[column])


def parse_setup(
    raw: dict[str, Any], *, spans: Mapping[str, SourceSpan], instruction: str,
    paper: str | None, labware_catalog: Mapping[str, LabwareFacts],
) -> PhasedSetup:
    try:
        setup = PhasedSetup.model_validate(raw)
    except ValidationError as exc:
        raise ActionPlanError(str(exc)) from exc
    if len({stage.id for stage in setup.stages}) != len(setup.stages):
        raise ActionPlanError("Stage IDs must be unique and ordered once.")
    for stage in setup.stages:
        _quotes(stage.evidence_refs, spans=spans, instruction=instruction, paper=paper)
    loads = {item.id: item for item in setup.labware}
    if len(loads) != len(setup.labware):
        raise ActionPlanError("Setup repeats a labware ID.")
    assigned: set[tuple[str, str]] = set()
    for supply in setup.initial_supplies:
        quotes = _quotes(supply.evidence_refs, spans=spans,
                         instruction=instruction, paper=paper)
        item = loads.get(supply.labware)
        if item is None or item.load_name not in labware_catalog:
            raise ActionPlanError("An initial supply names unloaded or unknown labware.")
        facts = labware_catalog[item.load_name]
        if facts.is_tiprack:
            raise ActionPlanError("Tip racks cannot be liquid initial supplies.")
        wells = _selected_wells(supply, facts)
        if not wells:
            raise ActionPlanError("An initial supply selects no wells.")
        for well in wells:
            key = (supply.labware, well)
            if key in assigned:
                raise ActionPlanError("Initial supplies overlap a physical well.")
            assigned.add(key)
            capacity = facts.well_capacity_ul.get(well)
            if (supply.volume_ul is not None and capacity is not None and
                    supply.volume_ul > capacity):
                raise ActionPlanError("An initial supply exceeds trusted well capacity.")
            bound = [clause for ref, quote in zip(supply.evidence_refs, quotes, strict=True)
                     if spans[ref].source == "instruction"
                     if (clause := _source_binds_supply(
                         quote, item=item, supply=supply, well=well,
                     )) is not None]
            if not bound:
                raise ActionPlanError(
                    f"Initial supply {item.id}/{well} needs one cited line naming "
                    "that labware, well, and source material."
                )
            if supply.volume_ul is not None:
                quantities = [float(number) * (1000 if unit.lower() == "ml" else 1)
                              for quote in bound for number, unit in _QUANTITY.findall(quote)]
                cited_empty = supply.volume_ul == 0 and any(
                    re.search(r"\b(?:empty|blank|unfilled)\b", quote, re.I)
                    for quote in bound
                )
                if not cited_empty and not any(
                    math.isclose(supply.volume_ul, amount, abs_tol=1e-6)
                    for amount in quantities
                ):
                    raise ActionPlanError(
                        f"Initial volume for {item.id}/{well} must occur on the "
                        "same cited material/well/labware line with µL/mL units, "
                        "or be explicitly empty."
                    )
    return setup


def parse_stage(
    raw: dict[str, Any], *, expected: StageSpec,
    spans: Mapping[str, SourceSpan], instruction: str, paper: str | None,
    equipment_refs: Sequence[str] = (),
) -> StageDraft:
    try:
        stage = StageDraft.model_validate(raw)
    except ValidationError as exc:
        raise ActionPlanError(str(exc)) from exc
    if stage.stage_id != expected.id:
        raise ActionPlanError(f"Expected stage {expected.id!r}, received {stage.stage_id!r}.")
    allowed = set(expected.evidence_refs)
    if not set(stage.evidence_refs) <= allowed:
        raise ActionPlanError("Stage cites lines outside its setup-approved evidence scope.")
    _quotes(stage.evidence_refs, spans=spans, instruction=instruction, paper=paper)
    verified_equipment: set[str] = set()
    for ref in equipment_refs:
        quotes = _quotes([ref], spans=spans, instruction=instruction, paper=paper)
        if (spans[ref].source != "instruction" or
                re.search(r"\b(?:pipette|tiprack|tips?)\b", quotes[0], re.I) is None):
            raise ActionPlanError("Equipment references must be exact task instrument lines.")
        verified_equipment.add(ref)
    for index, action in enumerate(stage.actions):
        nested = [(f"actions[{index}]", action)]
        nested.extend((f"actions[{index}].actions[{body_index}]", item)
                      for body_index, item in enumerate(getattr(action, "actions", [])))
        for path, item in nested:
            item_allowed = allowed | verified_equipment if item.kind in {
                "pickup", "drop", "refill_tips",
            } else allowed
            if not set(item.evidence_refs) <= item_allowed:
                raise ActionPlanError(
                    f"{path} cites lines outside its stage evidence scope."
                )
            _quotes(item.evidence_refs, spans=spans,
                    instruction=instruction, paper=paper)
            if (any(ref.startswith("paper.") for ref in expected.evidence_refs)
                    and item.kind not in {"pickup", "drop", "refill_tips"}
                    and not any(ref.startswith("paper.") for ref in item.evidence_refs)):
                raise ActionPlanError(
                    f"{path} is a paper-grounded process action and must cite "
                    "its stage-specific paper evidence."
                )
    return stage


def merge_prefix(setup: PhasedSetup, stages: Sequence[StageDraft]) -> ActionPlan:
    """Concatenate exact, already parsed stage actions without any insertion."""
    if len(stages) > len(setup.stages):
        raise ActionPlanError("Too many stages for the setup outline.")
    for index, stage in enumerate(stages):
        if stage.stage_id != setup.stages[index].id:
            raise ActionPlanError("Stages must be merged in the model's setup order.")
    return ActionPlan(
        labware=setup.labware, modules=setup.modules, pipettes=setup.pipettes,
        actions=[action for stage in stages for action in stage.actions],
    )


def tip_handoff(
    plan: ActionPlan, *, labware_catalog: Mapping[str, LabwareFacts],
    pipette_catalog: Mapping[str, PipetteFacts],
) -> dict[str, dict[str, int | float]]:
    loads = {item.id: item for item in plan.labware}
    expanded, _ = _expand_actions(plan, loads, labware_catalog)
    pickups = Counter(action.pipette for action in expanded if isinstance(action, Pickup))
    refills = Counter(action.pipette for action in expanded if isinstance(action, RefillTips))
    output: dict[str, dict[str, int | float]] = {}
    for pipette in plan.pipettes:
        pipette_facts = pipette_catalog[pipette.model]
        rack_facts = [labware_catalog[loads[id_].load_name] for id_ in pipette.tip_rack_ids]
        rack_pickups = sum(len(facts.multichannel_anchor_wells)
                           if pipette_facts.channels > 1 else len(facts.wells)
                           for facts in rack_facts)
        effective_capacity = min(pipette_facts.max_volume_ul,
                                 *(facts.tip_capacity_ul for facts in rack_facts
                                   if facts.tip_capacity_ul is not None))
        output[pipette.id] = {
            "channels": pipette_facts.channels,
            "min_stroke_ul": pipette_facts.min_volume_ul,
            "effective_tip_capacity_ul": effective_capacity,
            "pickups_per_rack_load": rack_pickups,
            "pickups_used": pickups[pipette.id],
            "authorized_refills_used": refills[pipette.id],
            "pickups_remaining_current_load":
                rack_pickups * (1 + refills[pipette.id]) - pickups[pipette.id],
        }
    return output


def _resolve_event_wells(
    event: Mapping[str, Any], channels: int, *,
    event_labware: Mapping[str, str], catalog: Mapping[str, LabwareFacts],
) -> tuple[WellRef, ...]:
    name, anchor = event.get("labware"), event.get("well")
    if not isinstance(name, str) or not isinstance(anchor, str):
        raise ValueError("Event lacks a labware name or anchor well.")
    load_name = event_labware.get(name)
    facts = catalog.get(load_name) if load_name is not None else None
    if facts is None or anchor not in facts.wells:
        raise ValueError("Event labware or well is absent from the trusted catalog.")
    if channels == 1:
        return (WellRef(name, anchor),)
    if anchor not in facts.multichannel_anchor_wells:
        raise ValueError("Multichannel event has no trusted physical anchor.")
    match = _COLUMN.fullmatch(anchor)
    if match is not None:
        column = match.group(2)
        row_wells = tuple(f"{chr(ord('A') + channel)}{column}"
                          for channel in range(channels))
        if all(well in facts.wells for well in row_wells):
            return tuple(WellRef(name, well) for well in row_wells)
    # Installed long-trough definitions explicitly center all channels in one
    # rectangular well. Its trusted anchor is established by geometry loader.
    return tuple(WellRef(name, anchor) for _ in range(channels))


def audit_prefix_material(
    events: Sequence[Mapping[str, Any]], event_labware: Mapping[str, str],
    setup: PhasedSetup, *, labware_catalog: Mapping[str, LabwareFacts],
) -> LedgerResult:
    loads = {item.id: item for item in setup.labware}
    modules = {item.id: item for item in setup.modules}
    observed_names = {str(event["labware"]) for event in events
                      if isinstance(event.get("labware"), str)}
    effective_labware = dict(event_labware)
    for name in observed_names - set(effective_labware):
        candidates: set[str] = set()
        for item in setup.labware:
            module = modules.get(item.module_id) if item.module_id else None
            slot = item.slot if item.slot is not None else module.slot if module else None
            label_match = item.label is not None and name.startswith(item.label + " on ")
            slot_match = slot is not None and name.endswith(f" on {slot}")
            if label_match or slot_match:
                candidates.add(item.load_name)
        if len(candidates) != 1:
            raise ActionPlanError(
                f"Cannot identify pinned event labware {name!r} from fixed setup."
            )
        effective_labware[name] = next(iter(candidates))
    inventory: dict[WellRef, InitialWell] = {}
    for supply in setup.initial_supplies:
        item = loads[supply.labware]
        facts = labware_catalog[item.load_name]
        module = modules.get(item.module_id) if item.module_id else None
        slot = item.slot if item.slot is not None else module.slot if module else None
        matched = [name for name, load_name in effective_labware.items()
                   if load_name == item.load_name and item.label is not None
                   and name.startswith(item.label + " on ")]
        if not matched and slot is not None:
            matched = [name for name, load_name in effective_labware.items()
                       if load_name == item.load_name and name.endswith(f" on {slot}")]
        if not matched and not any(
            name.startswith(item.label + " on ") if item.label is not None
            else slot is not None and name.endswith(f" on {slot}")
            for name in observed_names
        ):
            # The pinned logger may report only labware touched in this prefix.
            # Keep the model-authored supply claim for a later stage instead of
            # manufacturing a run-log identity before it is observed.
            continue
        if len(matched) != 1:
            raise ActionPlanError(
                f"Cannot uniquely match initial supply {supply.labware!r} to the pinned run log."
            )
        for well in _selected_wells(supply, facts):
            inventory[WellRef(matched[0], well)] = InitialWell(
                supply.volume_ul, f"{supply.material_id}@{item.id}/{well}",
            )

    def resolve(event: Mapping[str, Any], channels: int) -> tuple[WellRef, ...]:
        return _resolve_event_wells(event, channels,
                                    event_labware=effective_labware,
                                    catalog=labware_catalog)

    limits: dict[WellRef, WellLimit] = {}
    active_channels: dict[str, int] = {}
    for event in events:
        instrument = event.get("instrument")
        if event.get("kind") == "pick" and isinstance(instrument, str):
            count = event.get("channels", 1)
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise ActionPlanError("Pinned pickup reported an invalid channel count.")
            active_channels[instrument] = count
        if event.get("kind") == "drop" and isinstance(instrument, str):
            active_channels.pop(instrument, None)
        if event.get("kind") not in {"aspirate", "dispense", "mix"}:
            continue
        channels = event.get("channels", active_channels.get(instrument, 1))
        if not isinstance(channels, int) or isinstance(channels, bool):
            raise ActionPlanError("Pinned event reported an invalid channel count.")
        for ref in resolve(event, channels):
            facts = labware_catalog[effective_labware[ref.labware]]
            capacity = facts.well_capacity_ul.get(ref.well)
            if capacity is not None:
                limits[ref] = WellLimit(max_volume_ul=capacity)
    return audit_material_flow(events, initial_wells=inventory, well_limits=limits,
                               well_resolver=resolve)


def material_handoff(ledger: LedgerResult) -> dict[str, Any]:
    """A compact, explicit state summary for the next independent model call."""
    by_labware: dict[str, dict[str, float]] = defaultdict(dict)
    unknown = 0
    for ref, snapshot in ledger.final_wells.items():
        if snapshot.volume_ul is None:
            unknown += 1
        if abs(snapshot.observed_net_change_ul) > 1e-9:
            by_labware[ref.labware][ref.well] = round(snapshot.observed_net_change_ul, 6)
    return {
        "observed_net_change_ul_by_labware_well": dict(by_labware),
        "unknown_absolute_well_count": unknown,
        "issues": [
            {"code": issue.code, "severity": issue.severity,
             "message": issue.message}
            for issue in ledger.issues[:24]
        ],
        "issue_count": len(ledger.issues),
    }
