"""Labware definitions, catalog loading, and deck tracking."""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from pybravo.types import MAX_LOCATIONS, MIN_LOCATION

logger = logging.getLogger(__name__)
_LABWARE_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "labware_catalog.yaml"
_LABWARE_ASSET_DIR = Path(__file__).resolve().parents[2] / "labware"
_DEFAULT_LABWARE_SNAPSHOT_PATH = Path(__file__).resolve().parents[2] / "config" / "labware_catalog.snapshot.yaml"

try:
    from pymongo import MongoClient
except ImportError:  # pragma: no cover - exercised indirectly when pymongo missing
    MongoClient = None


@dataclass
class LabwareDefinition:
    """Normalized labware definition loaded from Mongo or a local fallback."""

    id: str
    name: str
    kind: str
    vendor: str = ""
    catalog_number: str = ""
    description: str = ""
    base_class: str = ""
    wells: int = 0
    length_mm: float = 0.0
    width_mm: float = 0.0
    height_mm: float = 0.0
    stack_height_mm: float = 0.0
    gripper_offset_mm: float = 0.0
    lid_gripper_offset_mm: float | None = None
    empty_check_offset_mm: float | None = None
    shim_thickness_mm: float = 0.0
    can_be_sealed: bool = False
    sealed_height_mm: float = 0.0
    sealed_stacking_height_mm: float = 0.0
    can_have_lid: bool = False
    lidded_height_mm: float = 0.0
    lidded_stack_height_mm: float = 0.0
    lid_resting_height_mm: float = 0.0
    lid_departure_height_mm: float = 0.0
    max_robot_handling_speed: str = ""
    rows: int = 0
    cols: int = 0
    well_depth_mm: float = 0.0
    offset_x_mm: float = 0.0
    offset_y_mm: float = 0.0
    spacing_x_mm: float = 0.0
    spacing_y_mm: float = 0.0
    well_volume_ul: float = 0.0
    well_diameter_mm: float = 0.0
    disposable_tip_capacity_ul: float = 0.0
    tip_definition_id: str = ""
    supported_tip_ids: list[str] = field(default_factory=list)
    model_3d: str | None = None
    # Mount-ability flags — consulted by mount_plates() / unmount_plate().
    # can_mount        = "this plate can sit on top of another and lock into
    #                    it" (e.g. a filter plate that nests into a
    #                    collection plate for vacuum filtration).
    # can_be_mounted   = "another plate can sit on top of me and lock in"
    #                    (e.g. a collection plate that accepts a filter).
    # Both flags are soft — the mount task validates them but a missing
    # flag only emits a warning, so users can experiment without first
    # editing the labware catalog. Authoritative rejection lives in the
    # labware editor UI (LabwareDashboard.jsx), not here.
    can_mount: bool = False
    can_be_mounted: bool = False

    @classmethod
    def from_mongo(cls, doc: dict[str, Any]) -> "LabwareDefinition":
        dims = doc.get("plate_dimensions_mm", {}) or {}
        props = doc.get("plate_properties", {}) or {}
        wells = doc.get("well_dimensions_mm", {}) or {}
        object_id = str(doc.get("labware_type_id") or doc.get("_id") or doc.get("name") or "")
        return cls(
            id=object_id,
            name=str(doc.get("name") or ""),
            kind=str(doc.get("kind") or ""),
            vendor=str(doc.get("vendor") or ""),
            catalog_number=str(doc.get("catalog_number") or ""),
            description=str(doc.get("description") or ""),
            base_class=str(doc.get("base_class") or ""),
            wells=int(doc.get("wells") or 0),
            length_mm=float(dims.get("length_mm") or 0.0),
            width_mm=float(dims.get("width_mm") or 0.0),
            height_mm=float(dims.get("height_mm") or props.get("thickness_mm") or 0.0),
            stack_height_mm=float(props.get("stacking_thickness_mm") or dims.get("height_mm") or 0.0),
            gripper_offset_mm=float(props.get("robot_gripper_offset_mm") or 0.0),
            lid_gripper_offset_mm=(
                float(props["robot_lid_gripper_offset_mm"])
                if props.get("robot_lid_gripper_offset_mm") is not None
                else None
            ),
            empty_check_offset_mm=(
                float(props["empty_check_offset_mm"])
                if props.get("empty_check_offset_mm") is not None
                else None
            ),
            shim_thickness_mm=float(props.get("shim_thickness_mm") or 0.0),
            can_be_sealed=bool(props.get("can_be_sealed", False)),
            sealed_height_mm=float(props.get("sealed_thickness_mm") or 0.0),
            sealed_stacking_height_mm=float(props.get("sealed_stacking_thickness_mm") or 0.0),
            can_have_lid=bool(props.get("can_have_lid", False)),
            lidded_height_mm=float(props.get("lidded_thickness_mm") or 0.0),
            lidded_stack_height_mm=float(props.get("lidded_stacking_thickness_mm") or 0.0),
            lid_resting_height_mm=float(props.get("lid_resting_height_mm") or 0.0),
            lid_departure_height_mm=float(props.get("lid_departure_height_mm") or 0.0),
            max_robot_handling_speed=str(props.get("max_robot_handling_speed") or ""),
            rows=int(wells.get("rows") or 0),
            cols=int(wells.get("cols") or 0),
            well_depth_mm=float(wells.get("depth_mm") or 0.0),
            offset_x_mm=float(wells.get("offset_x_mm") or 0.0),
            offset_y_mm=float(wells.get("offset_y_mm") or 0.0),
            spacing_x_mm=float(wells.get("spacing_x_mm") or 0.0),
            spacing_y_mm=float(wells.get("spacing_y_mm") or 0.0),
            well_volume_ul=float(wells.get("volume_ul") or 0.0),
            well_diameter_mm=float(wells.get("diameter_mm") or 0.0),
            disposable_tip_capacity_ul=float(wells.get("disposable_tip_capacity_ul") or 0.0),
            tip_definition_id=str(doc.get("tip_definition_id") or ""),
            supported_tip_ids=list(doc.get("supported_tip_ids") or []),
            model_3d=_resolve_model_3d_path(doc),
            can_mount=bool(props.get("can_mount", False)),
            can_be_mounted=bool(props.get("can_be_mounted", False)),
        )

    def to_summary(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Labware:
    """Physical description of a single labware item on the deck."""

    id: str
    name: str
    height: float
    width: float
    length: float
    labware_type: str = ""
    gripper_offset: float = 0.0
    stack_height: float = 0.0
    is_lidded: bool = False
    is_sealed: bool = False
    definition_id: str = ""
    wells: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    # Instance-level barcode. Populated by sensor/ReadBarcode tasks; travels
    # with the plate across pick/place because LabwareStack.remove_top() and
    # .add() preserve the live instance. Distinct from `metadata` (which is
    # definition-level).
    barcode: str = ""
    # Free-form per-instance annotations written by workflow scripts
    # (`plate.tags["lane"] = "A2"`). Like `barcode`, travels with the plate
    # across pick/place/stack because it's on the live instance, not the
    # definition. Distinct from `metadata` (which is definition-level and
    # shared across all instances from a single LabwareDefinition).
    tags: dict[str, Any] = field(default_factory=dict)
    # Mount state — instance-level, set True by the ``mount_plates`` task
    # when this labware is placed on top of another and they become a
    # locked unit (e.g. filter plate mounted on collection plate for
    # vacuum filtration). When True, pick/place transports this labware
    # together with the one immediately below it on the stack. Cleared
    # by ``unmount_plate``. Travels with the instance across moves.
    is_mounted: bool = False

    @classmethod
    def from_definition(
        cls,
        definition: LabwareDefinition,
        *,
        is_lidded: bool = False,
        is_sealed: bool = False,
    ) -> "Labware":
        height, stack_height = _active_labware_geometry(
            definition,
            is_lidded=is_lidded,
            is_sealed=is_sealed,
        )
        metadata = definition.to_summary()
        metadata["base_height_mm"] = float(definition.height_mm or height)
        metadata["total_height_mm"] = float(height)
        metadata["is_lidded"] = bool(is_lidded)
        metadata["is_sealed"] = bool(is_sealed)
        metadata["height_mm"] = float(height)
        metadata["stack_height_mm"] = float(stack_height)
        if is_lidded:
            generated_lid = generated_lid_metadata(metadata)
            if generated_lid is not None:
                metadata["generated_lid"] = generated_lid
        return cls(
            id=definition.id,
            definition_id=definition.id,
            name=definition.name,
            height=height,
            width=definition.width_mm,
            length=definition.length_mm,
            labware_type=definition.kind,
            gripper_offset=definition.gripper_offset_mm,
            stack_height=stack_height,
            is_lidded=is_lidded,
            is_sealed=is_sealed,
            wells=definition.wells,
            metadata=metadata,
        )


def _active_labware_geometry(
    definition: LabwareDefinition,
    *,
    is_lidded: bool,
    is_sealed: bool,
) -> tuple[float, float]:
    base_height = float(definition.height_mm or 0.0)
    base_stack_height = float(definition.stack_height_mm or base_height)
    if is_lidded:
        height = float(definition.lidded_height_mm or base_height)
        stack_height = float(definition.lidded_stack_height_mm or height)
        return height, stack_height
    if is_sealed:
        height = float(definition.sealed_height_mm or base_height)
        stack_height = float(definition.sealed_stacking_height_mm or height)
        return height, stack_height
    return base_height, base_stack_height


def lid_thickness_mm(metadata: dict[str, Any] | None) -> float:
    meta = metadata or {}
    resting_height = float(meta.get("lid_resting_height_mm") or 0.0)
    lidded_height = float(meta.get("lidded_height_mm") or 0.0)
    if lidded_height > 0.0 and resting_height > 0.0 and lidded_height > resting_height:
        return max(0.1, lidded_height - resting_height)
    base_height = float(meta.get("base_height_mm") or meta.get("height_mm") or 0.0)
    if lidded_height > 0.0 and base_height > 0.0 and lidded_height > base_height:
        return max(0.1, lidded_height - base_height)
    if resting_height > 0.0:
        return max(0.1, resting_height)
    return 0.1


def lid_gripper_offset_mm(
    metadata: dict[str, Any] | None,
    *,
    fallback_gripper_offset_mm: float = 0.0,
    label: str = "labware",
) -> float:
    meta = dict(metadata or {})
    explicit = meta.get("lid_gripper_offset_mm")
    if explicit is None:
        explicit = meta.get("robot_lid_gripper_offset_mm")
    if explicit is not None:
        return max(0.0, float(explicit))

    fallback = max(
        0.0,
        float(
            fallback_gripper_offset_mm
            or meta.get("gripper_offset_mm")
            or meta.get("robot_gripper_offset_mm")
            or 0.0
        ),
    )
    lid_height = lid_thickness_mm(meta)
    if fallback > lid_height:
        logger.warning(
            "Clamping fallback lid gripper offset for %s from %.3f to lid thickness %.3f; "
            "configure robot_lid_gripper_offset_mm for correct lid handling geometry",
            label,
            fallback,
            lid_height,
        )
        fallback = lid_height
    return fallback


def generated_lid_metadata(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    meta = dict(metadata or {})
    length_mm = float(meta.get("length_mm") or meta.get("length") or 0.0)
    width_mm = float(meta.get("width_mm") or meta.get("width") or 0.0)
    if length_mm <= 0.0 or width_mm <= 0.0:
        return None
    thickness_mm = lid_thickness_mm(meta)
    grip_offset_mm = lid_gripper_offset_mm(
        meta,
        fallback_gripper_offset_mm=float(meta.get("gripper_offset_mm") or meta.get("robot_gripper_offset_mm") or 0.0),
        label=str(meta.get("name") or "labware"),
    )
    return {
        "name": f"{meta.get('name', 'Labware')} Lid",
        "kind": "lid",
        "base_class": "lid",
        "length_mm": length_mm,
        "width_mm": width_mm,
        "height_mm": thickness_mm,
        "stack_height_mm": thickness_mm,
        "lid_thickness_mm": thickness_mm,
        "lid_gripper_offset_mm": grip_offset_mm,
        "lid_resting_height_mm": float(meta.get("lid_resting_height_mm") or 0.0),
        "lid_departure_height_mm": float(meta.get("lid_departure_height_mm") or 0.0),
        "source_plate_name": str(meta.get("name") or ""),
        "render_mode": "generated_lid",
    }


def synthesize_lid_labware(plate: Labware) -> Labware:
    plate_meta = dict(plate.metadata or {})
    lid_meta = generated_lid_metadata(plate_meta)
    if lid_meta is None:
        raise ValueError(f"Cannot synthesize lid for labware without valid footprint: {plate.name}")
    return Labware(
        id=f"{plate.id}::lid",
        definition_id=plate.definition_id,
        name=str(lid_meta["name"]),
        height=float(lid_meta["height_mm"]),
        width=float(lid_meta["width_mm"]),
        length=float(lid_meta["length_mm"]),
        labware_type="lid",
        gripper_offset=float(lid_meta.get("lid_gripper_offset_mm") or 0.0),
        stack_height=float(lid_meta["stack_height_mm"]),
        is_lidded=False,
        is_sealed=False,
        wells=0,
        metadata=lid_meta,
    )


class LabwareCatalog:
    """Simple lookup interface for normalized labware definitions."""

    def list_definitions(self) -> list[LabwareDefinition]:
        raise NotImplementedError

    def get_definition(self, labware_id: str) -> LabwareDefinition | None:
        raise NotImplementedError


class InMemoryLabwareCatalog(LabwareCatalog):
    def __init__(
        self,
        definitions: list[LabwareDefinition],
        *,
        aliases: dict[str, str] | None = None,
    ) -> None:
        self._definitions = list(definitions)
        self._by_id = {d.id: d for d in definitions}
        for alias_id, canonical_id in dict(aliases or {}).items():
            if alias_id in self._by_id:
                continue
            canonical = self._by_id.get(canonical_id)
            if canonical is not None:
                self._by_id[alias_id] = canonical

    def list_definitions(self) -> list[LabwareDefinition]:
        return list(self._definitions)

    def get_definition(self, labware_id: str) -> LabwareDefinition | None:
        return self._by_id.get(labware_id)


class MongoLabwareCatalog(LabwareCatalog):
    def __init__(self, uri: str, database: str, collection: str) -> None:
        if MongoClient is None:
            raise RuntimeError("pymongo is not installed")
        self._client = MongoClient(uri, serverSelectionTimeoutMS=3000)
        self._collection = self._client[database][collection]
        self._definitions: list[LabwareDefinition] | None = None

    def _load(self) -> list[LabwareDefinition]:
        docs = list(
            self._collection.find(
                {},
                {
                    "labware_type_id": 1,
                    "name": 1,
                    "kind": 1,
                    "vendor": 1,
                    "catalog_number": 1,
                    "description": 1,
                    "base_class": 1,
                    "wells": 1,
                    "plate_dimensions_mm": 1,
                    "plate_properties": 1,
                    "well_dimensions_mm": 1,
                    "model_3d": 1,
                    "legacy_raw": 1,
                },
            )
        )
        definitions = []
        for doc in docs:
            if not doc.get("name"):
                continue
            definition = LabwareDefinition.from_mongo(doc)
            definitions.append(_apply_mirrored_motion_fields(definition))
        definitions.sort(key=lambda item: item.name.lower())
        return definitions

    def list_definitions(self) -> list[LabwareDefinition]:
        if self._definitions is None:
            self._definitions = self._load()
        return list(self._definitions)

    def get_definition(self, labware_id: str) -> LabwareDefinition | None:
        if self._definitions is None:
            self._definitions = self._load()
        for definition in self._definitions:
            if definition.id == labware_id:
                return definition
        return None


def _load_labware_snapshot_path() -> Path:
    configured_path = ""

    if _LABWARE_CONFIG_PATH.exists():
        try:
            with open(_LABWARE_CONFIG_PATH, "r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}
            cache = raw.get("cache", {}) or {}
            configured_path = str(cache.get("snapshot_path") or "").strip()
        except Exception as exc:
            logger.warning("Could not read labware catalog cache config %s: %s", _LABWARE_CONFIG_PATH, exc)

    env_path = os.environ.get("PYBRAVO_LABWARE_SNAPSHOT_PATH", "").strip()
    selected = env_path or configured_path
    if selected:
        candidate = Path(selected).expanduser()
        if candidate.is_absolute():
            return candidate
        return Path(__file__).resolve().parents[2] / candidate
    return _DEFAULT_LABWARE_SNAPSHOT_PATH


def _load_labware_catalog_config() -> tuple[str, str, str]:
    config_uri = ""
    config_database = ""
    config_collection = ""

    if _LABWARE_CONFIG_PATH.exists():
        try:
            with open(_LABWARE_CONFIG_PATH, "r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}
            mongo = raw.get("mongo", {}) or {}
            config_uri = str(mongo.get("uri") or "").strip()
            config_database = str(mongo.get("database") or "").strip()
            config_collection = str(mongo.get("collection") or "").strip()
        except Exception as exc:
            logger.warning("Could not read labware catalog config %s: %s", _LABWARE_CONFIG_PATH, exc)

    uri = os.environ.get("PYBRAVO_LABWARE_MONGO_URI", config_uri).strip()
    database = os.environ.get("PYBRAVO_LABWARE_MONGO_DB", config_database).strip()
    collection = os.environ.get("PYBRAVO_LABWARE_MONGO_COLLECTION", config_collection).strip()
    return uri, database, collection


def _normalize_labware_key(value: str) -> str:
    return " ".join(str(value or "").split()).strip().lower()


def _labware_definition_quality(definition: LabwareDefinition) -> int:
    score = 0
    for field_name, value in asdict(definition).items():
        if field_name == "id":
            continue
        if isinstance(value, str):
            score += 1 if value.strip() else 0
        elif isinstance(value, bool):
            score += 1 if value else 0
        elif isinstance(value, (int, float)):
            score += 1 if float(value) != 0.0 else 0
        elif isinstance(value, list):
            score += len([item for item in value if str(item or "").strip()])
        elif value is not None:
            score += 1
    return score


def _prefer_labware_definition(current: LabwareDefinition, candidate: LabwareDefinition) -> bool:
    current_score = _labware_definition_quality(current)
    candidate_score = _labware_definition_quality(candidate)
    if candidate_score != current_score:
        return candidate_score > current_score
    if candidate.model_3d and not current.model_3d:
        return True
    return False


def _resolve_labware_aliases(aliases: dict[str, str]) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for alias_id, canonical_id in aliases.items():
        target = str(canonical_id or "").strip()
        seen = {alias_id}
        while target in aliases and target not in seen:
            seen.add(target)
            target = str(aliases[target] or "").strip()
        if target and target != alias_id:
            resolved[alias_id] = target
    return resolved


def normalize_labware_definitions(
    definitions: list[LabwareDefinition],
) -> tuple[list[LabwareDefinition], dict[str, str]]:
    """Collapse duplicate runtime labware rows while preserving stale IDs as aliases."""

    unique_by_id: list[LabwareDefinition] = []
    index_by_id: dict[str, int] = {}
    dropped_duplicate_ids: list[str] = []
    for definition in list(definitions or []):
        definition_id = str(definition.id or "").strip()
        if not definition_id:
            unique_by_id.append(definition)
            continue
        existing_index = index_by_id.get(definition_id)
        if existing_index is None:
            index_by_id[definition_id] = len(unique_by_id)
            unique_by_id.append(definition)
            continue
        dropped_duplicate_ids.append(definition_id)
        if _prefer_labware_definition(unique_by_id[existing_index], definition):
            unique_by_id[existing_index] = definition

    if dropped_duplicate_ids:
        logger.warning(
            "Dropping %d duplicate runtime labware rows by id: %s",
            len(dropped_duplicate_ids),
            ", ".join(sorted(set(dropped_duplicate_ids))),
        )

    deduped: list[LabwareDefinition] = []
    index_by_name: dict[str, int] = {}
    alias_ids: dict[str, str] = {}
    dropped_duplicate_names: list[str] = []

    for definition in unique_by_id:
        name_key = _normalize_labware_key(definition.name)
        if not name_key:
            deduped.append(definition)
            continue
        existing_index = index_by_name.get(name_key)
        if existing_index is None:
            index_by_name[name_key] = len(deduped)
            deduped.append(definition)
            continue

        existing = deduped[existing_index]
        dropped_duplicate_names.append(definition.name or definition.id)
        if _prefer_labware_definition(existing, definition):
            deduped[existing_index] = definition
            if existing.id and existing.id != definition.id:
                alias_ids[existing.id] = definition.id
        elif definition.id and definition.id != existing.id:
            alias_ids[definition.id] = existing.id

    if dropped_duplicate_names:
        logger.warning(
            "Collapsing %d duplicate runtime labware rows by name: %s",
            len(dropped_duplicate_names),
            ", ".join(sorted(set(dropped_duplicate_names))),
        )

    deduped.sort(key=lambda item: item.name.lower())
    return deduped, _resolve_labware_aliases(alias_ids)


def build_labware_catalog() -> LabwareCatalog:
    uri, database, collection = _load_labware_catalog_config()
    snapshot_path = _load_labware_snapshot_path()

    if uri and database and collection:
        try:
            catalog = MongoLabwareCatalog(uri, database, collection)
            definitions, alias_ids = normalize_labware_definitions(catalog.list_definitions())
            count = len(definitions)
            if count:
                _write_labware_snapshot(snapshot_path, definitions, source="mongo")
                logger.info("Loaded %d labware definitions from Mongo", count)
                return InMemoryLabwareCatalog(definitions, aliases=alias_ids)
            # Connecting to the wrong database succeeds and returns nothing.
            # Overwriting the snapshot here would destroy the only local copy
            # of the catalog, so fall through and keep what we have.
            logger.warning(
                "Mongo catalog %s.%s is empty; keeping the local snapshot. Check "
                "the labware catalog configuration if this is unexpected.",
                database,
                collection,
            )
        except Exception as exc:
            logger.warning("Falling back to local labware snapshot after Mongo failure: %s", exc)

    snapshot_definitions, snapshot_alias_ids = normalize_labware_definitions(
        _read_labware_snapshot(snapshot_path)
    )
    if snapshot_definitions:
        logger.info(
            "Loaded %d labware definitions from local snapshot %s",
            len(snapshot_definitions),
            snapshot_path,
        )
        return InMemoryLabwareCatalog(snapshot_definitions, aliases=snapshot_alias_ids)

    return InMemoryLabwareCatalog(_mirrored_builtin_definitions())


def _write_labware_snapshot(
    snapshot_path: Path,
    definitions: list[LabwareDefinition],
    *,
    source: str,
) -> None:
    payload = {
        "version": 1,
        "source": source,
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "labware": [definition.to_summary() for definition in definitions],
    }
    try:
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        with open(snapshot_path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(payload, fh, sort_keys=False)
    except Exception as exc:
        logger.warning("Could not write labware snapshot %s: %s", snapshot_path, exc)


def _read_labware_snapshot(snapshot_path: Path) -> list[LabwareDefinition]:
    if not snapshot_path.exists():
        return []

    try:
        with open(snapshot_path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    except Exception as exc:
        logger.warning("Could not read labware snapshot %s: %s", snapshot_path, exc)
        return []

    items = raw.get("labware", raw)
    if not isinstance(items, list):
        logger.warning("Ignoring malformed labware snapshot %s: expected list payload", snapshot_path)
        return []

    definitions: list[LabwareDefinition] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            definition = LabwareDefinition(**item)
        except TypeError as exc:
            logger.warning("Skipping malformed labware snapshot row in %s: %s", snapshot_path, exc)
            continue
        definitions.append(_apply_mirrored_motion_fields(definition))

    definitions.sort(key=lambda item: item.name.lower())
    return definitions


def _resolve_model_3d_path(doc: dict[str, Any]) -> str | None:
    raw = doc.get("model_3d")
    if isinstance(raw, dict):
        url = str(raw.get("url") or "").strip()
        if url:
            return url
        filename = str(raw.get("filename") or "").strip()
        if filename:
            return filename
    if isinstance(raw, str) and raw.strip():
        return raw.strip()

    name = str(doc.get("name") or "").strip()
    if name:
        candidate = _LABWARE_ASSET_DIR / f"{name}.gltf"
        if candidate.exists():
            return candidate.name
        candidate = _LABWARE_ASSET_DIR / f"{name}.glb"
        if candidate.exists():
            return candidate.name

    source = doc.get("source", {}) or {}
    folder = str(source.get("folder") or "").strip()
    if folder:
        candidate = _LABWARE_ASSET_DIR / f"{folder}.gltf"
        if candidate.exists():
            return candidate.name
        candidate = _LABWARE_ASSET_DIR / f"{folder}.glb"
        if candidate.exists():
            return candidate.name
    return None


def _mirrored_builtin_definitions() -> list[LabwareDefinition]:
    """Checked-in mirror of Mongo labware rows used by PyBravo motion math."""
    return [
        LabwareDefinition(
            id="builtin-384-greiner-781091",
            name="384 Greiner 781091 PS uclear",
            kind="sbs_plate",
            vendor="Greiner",
            catalog_number="781091",
            description="384 Geiner 781091 PS uclear",
            base_class="microplate",
            wells=384,
            length_mm=127.76,
            width_mm=85.48,
            height_mm=14.4,
            stack_height_mm=8.6,
            gripper_offset_mm=2.5,
            empty_check_offset_mm=None,
            shim_thickness_mm=4.0,
            can_be_sealed=False,
            sealed_height_mm=0.0,
            sealed_stacking_height_mm=0.0,
            can_have_lid=True,
            lidded_height_mm=16.5,
            lidded_stack_height_mm=14.5,
            lid_resting_height_mm=9.5,
            lid_departure_height_mm=8.5,
            max_robot_handling_speed="fast",
            rows=16,
            cols=24,
            well_depth_mm=11.5,
            offset_x_mm=2.25,
            offset_y_mm=2.25,
            spacing_x_mm=4.5,
            spacing_y_mm=4.5,
            well_volume_ul=130.0,
            well_diameter_mm=3.3,
            disposable_tip_capacity_ul=10.0,
            model_3d="384 Greiner 781091 PS uclear.gltf",
        ),
        LabwareDefinition(
            id="builtin-96-greiner-655101",
            name="96 Greiner 655101 PS Clr Rnd Well Flat Btm",
            kind="sbs_plate",
            vendor="Greiner",
            catalog_number="655101",
            description="96-well Greiner round well flat bottom",
            base_class="microplate",
            wells=96,
            length_mm=127.76,
            width_mm=85.48,
            height_mm=14.4,
            stack_height_mm=8.6,
            gripper_offset_mm=0.5,
            empty_check_offset_mm=None,
            shim_thickness_mm=4.0,
            can_be_sealed=False,
            sealed_height_mm=0.0,
            sealed_stacking_height_mm=0.0,
            can_have_lid=True,
            lidded_height_mm=16.5,
            lidded_stack_height_mm=14.5,
            lid_resting_height_mm=9.5,
            lid_departure_height_mm=8.5,
            max_robot_handling_speed="fast",
            rows=8,
            cols=12,
            well_depth_mm=0.0,
            offset_x_mm=0.0,
            offset_y_mm=0.0,
            spacing_x_mm=9.0,
            spacing_y_mm=9.0,
            well_volume_ul=300.0,
            well_diameter_mm=6.9,
            disposable_tip_capacity_ul=200.0,
            model_3d=None,
        ),
    ]


_MIRRORED_BY_NAME = {
    definition.name: definition for definition in _mirrored_builtin_definitions()
}


def _apply_mirrored_motion_fields(definition: LabwareDefinition) -> LabwareDefinition:
    """Use checked-in mirrored motion geometry when a known labware row conflicts.

    This keeps robot math stable even when Mongo contains duplicate/stale rows for a
    plate name we have already verified manually.
    """
    mirrored = _MIRRORED_BY_NAME.get(definition.name)
    if mirrored is None:
        return definition
    if (
        definition.height_mm != mirrored.height_mm
        or definition.stack_height_mm != mirrored.stack_height_mm
        or definition.gripper_offset_mm != mirrored.gripper_offset_mm
    ):
        logger.warning(
            "Overriding motion-critical labware geometry for %s: "
            "mongo(height=%.3f, stack=%.3f, grip=%.3f) -> mirror(height=%.3f, stack=%.3f, grip=%.3f)",
            definition.name,
            definition.height_mm,
            definition.stack_height_mm,
            definition.gripper_offset_mm,
            mirrored.height_mm,
            mirrored.stack_height_mm,
            mirrored.gripper_offset_mm,
        )
    return LabwareDefinition(
        id=definition.id,
        name=definition.name,
        kind=definition.kind,
        vendor=definition.vendor or mirrored.vendor,
        catalog_number=definition.catalog_number or mirrored.catalog_number,
        description=definition.description or mirrored.description,
        base_class=definition.base_class or mirrored.base_class,
        wells=definition.wells or mirrored.wells,
        length_mm=definition.length_mm or mirrored.length_mm,
        width_mm=definition.width_mm or mirrored.width_mm,
        height_mm=mirrored.height_mm,
        stack_height_mm=mirrored.stack_height_mm,
        gripper_offset_mm=mirrored.gripper_offset_mm,
        lid_gripper_offset_mm=definition.lid_gripper_offset_mm,
        empty_check_offset_mm=(
            mirrored.empty_check_offset_mm
            if mirrored.empty_check_offset_mm is not None
            else definition.empty_check_offset_mm
        ),
        shim_thickness_mm=mirrored.shim_thickness_mm or definition.shim_thickness_mm,
        can_be_sealed=definition.can_be_sealed or mirrored.can_be_sealed,
        sealed_height_mm=mirrored.sealed_height_mm or definition.sealed_height_mm,
        sealed_stacking_height_mm=(
            mirrored.sealed_stacking_height_mm or definition.sealed_stacking_height_mm
        ),
        can_have_lid=definition.can_have_lid or mirrored.can_have_lid,
        lidded_height_mm=mirrored.lidded_height_mm or definition.lidded_height_mm,
        lidded_stack_height_mm=(
            mirrored.lidded_stack_height_mm or definition.lidded_stack_height_mm
        ),
        lid_resting_height_mm=mirrored.lid_resting_height_mm or definition.lid_resting_height_mm,
        lid_departure_height_mm=mirrored.lid_departure_height_mm or definition.lid_departure_height_mm,
        max_robot_handling_speed=definition.max_robot_handling_speed or mirrored.max_robot_handling_speed,
        rows=definition.rows or mirrored.rows,
        cols=definition.cols or mirrored.cols,
        well_depth_mm=definition.well_depth_mm or mirrored.well_depth_mm,
        offset_x_mm=definition.offset_x_mm or mirrored.offset_x_mm,
        offset_y_mm=definition.offset_y_mm or mirrored.offset_y_mm,
        spacing_x_mm=definition.spacing_x_mm or mirrored.spacing_x_mm,
        spacing_y_mm=definition.spacing_y_mm or mirrored.spacing_y_mm,
        well_volume_ul=definition.well_volume_ul or mirrored.well_volume_ul,
        well_diameter_mm=definition.well_diameter_mm or mirrored.well_diameter_mm,
        disposable_tip_capacity_ul=(
            definition.disposable_tip_capacity_ul or mirrored.disposable_tip_capacity_ul
        ),
        model_3d=definition.model_3d or mirrored.model_3d,
    )


class LabwareStack:
    """An ordered stack of :class:`Labware` at a single deck position."""

    def __init__(self) -> None:
        self._items: list[Labware] = []

    def add(self, labware: Labware) -> None:
        self._items.append(labware)

    def replace(self, labware: Labware) -> None:
        self._items = [labware]

    def remove_top(self) -> Labware:
        if not self._items:
            raise IndexError("Cannot remove from an empty labware stack")
        return self._items.pop()

    def get_total_height(self) -> float:
        if not self._items:
            return 0.0
        if len(self._items) == 1:
            return self._items[0].height
        total = 0.0
        for item in self._items[:-1]:
            total += item.stack_height or item.height
        total += self._items[-1].height
        return total

    def get_location_height(self) -> float:
        """Return vendor-style pickup offset above the taught plate-pad plane.

        This matches the vendor GetLocationHeight semantics for a visible top plate:
        the offset corresponds to the support surface under the top plate rather
        than the full physical height to the top of that plate.
        """
        if not self._items:
            return 0.0
        return max(0.0, self.get_total_height() - self._items[-1].height)

    def get_stacking_height(self) -> float:
        """Return the placement support height using stacking thickness.

        For placing a new plate onto a destination stack, we need the effective
        support surface produced by the plates already present, not the full
        physical top height of the visible top plate.
        """
        total = 0.0
        for item in self._items:
            total += item.stack_height or item.height
        return max(0.0, total)

    @property
    def top(self) -> Labware | None:
        return self._items[-1] if self._items else None

    @property
    def items(self) -> list[Labware]:
        return list(self._items)

    def mounted_group_from_top(self) -> list[Labware]:
        """Return the slice of plates that move as a unit when the top
        is picked up, ordered top→bottom.

        A plate with ``is_mounted=True`` is physically locked to the
        plate immediately below it (filter plate on collection plate,
        etc.), so picking the top drags the bottom along.  This walks
        the stack downward from the top, including each subsequent
        plate while the plate above it is ``is_mounted``.

        Always returns at least one item for a non-empty stack — the
        top itself — so callers can treat the result as "the thing we
        actually pick up" regardless of mount state.
        """
        if not self._items:
            return []
        group: list[Labware] = [self._items[-1]]
        i = len(self._items) - 1
        # While the current top of the group is mounted, the plate
        # immediately below it moves with us.
        while i > 0 and group[-1].is_mounted:
            i -= 1
            group.append(self._items[i])
        return group

    def get_support_height_below_group(self) -> float:
        """Stacking-surface height *under* the mounted group at the top.

        This is what the Bravo's gripper needs to clear on the way down
        to engage the bottom plate of the mounted group by its flanges.

        * For an ordinary (unmounted) stack, the group is just the top
          plate, so this is identical to :meth:`get_location_height` —
          everything beneath the top is support.
        * For a mounted pair at the top (filter on collection), the
          group is both plates. The gripper must engage the collection
          plate's flanges, which sit at the height of ANY plates below
          the collection plate (possibly zero if the pair is directly
          on the pad).

        Used by :class:`PickPlaceTask` to compute the pick-Z for a
        mounted-group transport so the whole locked unit lifts together.
        """
        if not self._items:
            return 0.0
        group_size = len(self.mounted_group_from_top())
        below_count = len(self._items) - group_size
        if below_count <= 0:
            return 0.0
        # Stack heights of everything that stays put when the group
        # lifts off — matches the semantics of get_stacking_height()
        # applied to the sub-stack below the group.
        total = 0.0
        for item in self._items[:below_count]:
            total += item.stack_height or item.height
        return max(0.0, total)

    def __len__(self) -> int:
        return len(self._items)

    def __bool__(self) -> bool:
        return bool(self._items)


class DeckState:
    """Tracks :class:`LabwareStack` at each of the nine deck locations."""

    def __init__(self) -> None:
        self._stacks: dict[int, LabwareStack] = {
            loc: LabwareStack() for loc in range(MIN_LOCATION, MAX_LOCATIONS + 1)
        }

    def _validate_location(self, location: int) -> None:
        if not (MIN_LOCATION <= location <= MAX_LOCATIONS):
            raise ValueError(f"Location must be {MIN_LOCATION}-{MAX_LOCATIONS}, got {location}")

    def add(self, location: int, labware: Labware) -> None:
        self._validate_location(location)
        self._stacks[location].add(labware)

    def set_single(self, location: int, labware: Labware) -> None:
        self._validate_location(location)
        self._stacks[location].replace(labware)

    def remove(self, location: int) -> Labware:
        self._validate_location(location)
        return self._stacks[location].remove_top()

    def remove_mounted_group(self, location: int) -> list[Labware]:
        """Remove every plate that moves as a unit when the top of the
        stack at ``location`` is picked up. Returns them top-first.

        Semantically a superset of :meth:`remove` — on an unmounted
        stack this is identical to removing the top plate (returns a
        single-item list). On a mounted pair (or N-level mounted
        stack) every locked member pops together so the move-to-
        destination side can re-add them as a unit.
        """
        self._validate_location(location)
        stack = self._stacks[location]
        group = stack.mounted_group_from_top()
        # Pop them off the stack in the same order the caller will
        # re-add them (top-first). This keeps the bottom of the
        # mounted group as the new top of the source after the move.
        for _ in group:
            stack.remove_top()
        return group

    def add_mounted_group(self, location: int, group: list[Labware]) -> None:
        """Place a mounted group produced by :meth:`remove_mounted_group`
        onto ``location``. Expects the group top-first, same ordering
        that remove returns, so we push items in reverse so the deck
        stack preserves the original bottom→top layout."""
        self._validate_location(location)
        stack = self._stacks[location]
        for labware in reversed(group):
            stack.add(labware)

    def get_stack(self, location: int) -> LabwareStack:
        self._validate_location(location)
        return self._stacks[location]

    def get_height(self, location: int) -> float:
        self._validate_location(location)
        return self._stacks[location].get_total_height()

    def get_location_height(self, location: int) -> float:
        self._validate_location(location)
        return self._stacks[location].get_location_height()

    def get_stacking_height(self, location: int) -> float:
        self._validate_location(location)
        return self._stacks[location].get_stacking_height()

    def get_all_heights(self) -> dict[int, float]:
        return {loc: stack.get_total_height() for loc, stack in self._stacks.items()}

    def clear(self, location: int) -> None:
        self._validate_location(location)
        self._stacks[location] = LabwareStack()

    def clear_all(self) -> None:
        for loc in self._stacks:
            self._stacks[loc] = LabwareStack()
