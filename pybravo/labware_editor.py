"""Local editable labware catalog store for the dashboard UI."""

from __future__ import annotations

import logging
import os
import re
import shutil
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

import yaml

from pybravo.deck import labware as labware_module
from pybravo.deck.labware import LabwareDefinition

try:
    from pymongo import MongoClient
except ImportError:  # pragma: no cover
    MongoClient = None

_EDITOR_STORE_PATH = Path(__file__).resolve().parents[1] / "config" / "labware_editor.yaml"
_EDITOR_ASSET_DIR = Path(__file__).resolve().parents[1] / "labware" / "editor_assets"

logger = logging.getLogger(__name__)


def _mongo_config() -> tuple[str, str, str, str]:
    uri = ""
    database = ""
    types_collection = ""
    classes_collection = ""
    config_path = labware_module._LABWARE_CONFIG_PATH

    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        mongo = raw.get("mongo", {}) or {}
        uri = str(mongo.get("uri") or "").strip()
        database = str(mongo.get("database") or "").strip()
        types_collection = str(mongo.get("collection") or "").strip()
        classes_collection = str(mongo.get("class_collection") or "").strip()

    uri = os.environ.get("PYBRAVO_LABWARE_MONGO_URI", uri).strip()
    database = os.environ.get("PYBRAVO_LABWARE_MONGO_DB", database).strip()
    types_collection = os.environ.get("PYBRAVO_LABWARE_MONGO_COLLECTION", types_collection).strip()
    classes_collection = os.environ.get(
        "PYBRAVO_LABWARE_MONGO_CLASS_COLLECTION",
        classes_collection or "labware_classes",
    ).strip()
    return uri, database, types_collection, classes_collection


def _mongo_enabled() -> bool:
    uri, database, types_collection, _ = _mongo_config()
    return bool(uri and database and types_collection and MongoClient is not None)


def _mongo_collections():
    uri, database, types_collection, classes_collection = _mongo_config()
    if not (uri and database and types_collection):
        raise RuntimeError("Labware MongoDB is not configured")
    if MongoClient is None:
        raise RuntimeError("pymongo is not installed")
    client = MongoClient(uri, serverSelectionTimeoutMS=3000)
    db = client[database]
    return client, db[types_collection], db[classes_collection]


def _store_path() -> Path:
    configured = os.environ.get("PYBRAVO_LABWARE_EDITOR_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    return _EDITOR_STORE_PATH


def _asset_dir() -> Path:
    configured = os.environ.get("PYBRAVO_LABWARE_EDITOR_ASSET_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return _EDITOR_ASSET_DIR


def _empty_store() -> dict[str, Any]:
    return {
        "version": 1,
        "labware_types": [],
        "labware_classes": [],
    }


def _dedupe_string_list(values: list[Any] | None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in list(values or []):
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _normalize_type_item(item: dict[str, Any]) -> dict[str, Any]:
    normalized = deepcopy(item)
    normalized["labware_class_ids"] = _dedupe_string_list(normalized.get("labware_class_ids") or [])
    normalized["supported_tip_ids"] = _dedupe_string_list(normalized.get("supported_tip_ids") or [])
    normalized["tip_definition_id"] = str(normalized.get("tip_definition_id") or "")
    return normalized


def _dedupe_records(
    records: list[Any] | None,
    *,
    id_key: str,
    label: str,
    normalize: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    dropped_ids: list[str] = []

    for raw in list(records or []):
        if not isinstance(raw, dict):
            logger.warning("Skipping malformed %s row in labware store: %r", label, raw)
            continue
        item = normalize(raw) if normalize is not None else deepcopy(raw)
        record_id = str(item.get(id_key) or "").strip()
        if record_id and record_id in seen_ids:
            dropped_ids.append(record_id)
            continue
        if record_id:
            seen_ids.add(record_id)
        deduped.append(item)

    if dropped_ids:
        logger.warning(
            "Dropping %d duplicate labware %s rows by %s: %s",
            len(dropped_ids),
            label,
            id_key,
            ", ".join(sorted(set(dropped_ids))),
        )

    return deduped


def _normalize_store(store: dict[str, Any]) -> dict[str, Any]:
    normalized = _empty_store()
    normalized.update(deepcopy(store or {}))
    normalized["labware_types"] = _dedupe_records(
        normalized.get("labware_types", []),
        id_key="labware_type_id",
        label="type",
        normalize=_normalize_type_item,
    )
    normalized["labware_classes"] = _dedupe_records(
        normalized.get("labware_classes", []),
        id_key="labware_class_id",
        label="class",
    )
    return normalized


def _sorted_store(store: dict[str, Any]) -> dict[str, Any]:
    store = _normalize_store(store)
    store["labware_types"] = sorted(
        store.get("labware_types", []),
        key=lambda item: str(item.get("name") or "").lower(),
    )
    store["labware_classes"] = sorted(
        store.get("labware_classes", []),
        key=lambda item: str(item.get("name") or "").lower(),
    )
    return store


def _write_store(store: dict[str, Any]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(_sorted_store(store), fh, sort_keys=False)


def _seed_store() -> dict[str, Any]:
    if _mongo_enabled():
        try:
            return _load_store_from_mongo()
        except Exception:
            pass
    snapshot_path = labware_module._load_labware_snapshot_path()
    definitions = labware_module._read_labware_snapshot(snapshot_path)
    if not definitions:
        definitions = labware_module._mirrored_builtin_definitions()
    return _sorted_store(
        {
            "version": 1,
            "labware_types": [_definition_to_editor_type(item) for item in definitions],
            "labware_classes": [],
        }
    )


def load_store() -> dict[str, Any]:
    if _mongo_enabled():
        try:
            store = _load_store_from_mongo()
            _write_store(store)
            sync_snapshot(store)
            return store
        except Exception:
            pass

    path = _store_path()
    if path.exists():
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        store = _empty_store()
        store.update(raw)
        store["labware_types"] = list(store.get("labware_types", []) or [])
        store["labware_classes"] = list(store.get("labware_classes", []) or [])
        return _sorted_store(store)

    store = _seed_store()
    _write_store(store)
    sync_snapshot(store)
    return store


def save_store(store: dict[str, Any]) -> None:
    store = _sorted_store(store)
    if _mongo_enabled():
        _write_store_to_mongo(store)
    _write_store(store)
    sync_snapshot(store)


def sync_snapshot(store: dict[str, Any] | None = None) -> None:
    payload = load_store() if store is None else _sorted_store(store)
    definitions = [_editor_type_to_definition(item) for item in payload.get("labware_types", [])]
    labware_module._write_labware_snapshot(
        labware_module._load_labware_snapshot_path(),
        definitions,
        source="local_editor",
    )


def list_types() -> list[dict[str, Any]]:
    return deepcopy(load_store()["labware_types"])


def list_classes() -> list[dict[str, Any]]:
    return deepcopy(load_store()["labware_classes"])


def get_type(labware_type_id: str) -> dict[str, Any] | None:
    for item in load_store()["labware_types"]:
        if item.get("labware_type_id") == labware_type_id:
            return deepcopy(item)
    return None


def create_type(payload: dict[str, Any]) -> dict[str, Any]:
    store = load_store()
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("Labware entry name is required")
    item = {
        "labware_type_id": _make_id("lw"),
        "name": name,
        "kind": str(payload.get("kind") or "sbs_plate"),
        "vendor": str(payload.get("vendor") or ""),
        "catalog_number": str(payload.get("catalog_number") or ""),
        "description": str(payload.get("description") or ""),
        "base_class": str(payload.get("base_class") or "microplate"),
        "wells": int(payload.get("wells") or 0),
        "plate_dimensions_mm": deepcopy(payload.get("plate_dimensions_mm") or {}),
        "plate_properties": deepcopy(payload.get("plate_properties") or {}),
        "well_dimensions_mm": deepcopy(payload.get("well_dimensions_mm") or {}),
        "pf400": deepcopy(payload.get("pf400") or {}),
        "planar_motor": deepcopy(payload.get("planar_motor") or {}),
        "labware_class_ids": list(payload.get("labware_class_ids") or []),
        "tip_definition_id": str(payload.get("tip_definition_id") or ""),
        "supported_tip_ids": list(payload.get("supported_tip_ids") or []),
    }
    store["labware_types"].append(item)
    save_store(store)
    return deepcopy(item)


def patch_type(labware_type_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    store = load_store()
    for item in store["labware_types"]:
        if item.get("labware_type_id") != labware_type_id:
            continue
        _merge_type(item, payload)
        save_store(store)
        return deepcopy(item)
    raise KeyError(labware_type_id)


def delete_type(labware_type_id: str) -> None:
    store = load_store()
    before = len(store["labware_types"])
    store["labware_types"] = [
        item for item in store["labware_types"] if item.get("labware_type_id") != labware_type_id
    ]
    if len(store["labware_types"]) == before:
        raise KeyError(labware_type_id)
    save_store(store)
    shutil.rmtree(_asset_dir() / labware_type_id, ignore_errors=True)


def create_class(payload: dict[str, Any]) -> dict[str, Any]:
    store = load_store()
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("Labware class name is required")
    item = {
        "labware_class_id": _make_id("class"),
        "name": name,
        "description": str(payload.get("description") or ""),
    }
    store["labware_classes"].append(item)
    save_store(store)
    return deepcopy(item)


def patch_class(labware_class_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    store = load_store()
    for item in store["labware_classes"]:
        if item.get("labware_class_id") != labware_class_id:
            continue
        if "name" in payload:
            name = str(payload.get("name") or "").strip()
            if not name:
                raise ValueError("Labware class name is required")
            item["name"] = name
        if "description" in payload:
            item["description"] = str(payload.get("description") or "")
        save_store(store)
        return deepcopy(item)
    raise KeyError(labware_class_id)


def delete_class(labware_class_id: str) -> None:
    store = load_store()
    before = len(store["labware_classes"])
    store["labware_classes"] = [
        item for item in store["labware_classes"] if item.get("labware_class_id") != labware_class_id
    ]
    if len(store["labware_classes"]) == before:
        raise KeyError(labware_class_id)
    for item in store["labware_types"]:
        memberships = list(item.get("labware_class_ids") or [])
        item["labware_class_ids"] = [value for value in memberships if value != labware_class_id]
    save_store(store)


def save_asset(labware_type_id: str, kind: str, filename: str, content: bytes) -> dict[str, Any]:
    store = load_store()
    target = None
    for item in store["labware_types"]:
        if item.get("labware_type_id") == labware_type_id:
            target = item
            break
    if target is None:
        raise KeyError(labware_type_id)

    safe_name = _sanitize_filename(filename)
    if not safe_name:
        raise ValueError("Uploaded filename is invalid")

    asset_root = _asset_dir() / labware_type_id
    asset_root.mkdir(parents=True, exist_ok=True)
    path = asset_root / safe_name
    path.write_bytes(content)

    rel_url = f"/labware-assets/{labware_type_id}/{safe_name}"
    if kind == "image":
        target["image_2d"] = {"url": rel_url, "filename": safe_name}
    elif kind == "model":
        target["model_3d"] = {
            "url": rel_url,
            "filename": safe_name,
            "format": path.suffix.lstrip(".").lower(),
        }
    else:
        raise ValueError(f"Unknown asset kind: {kind}")

    save_store(store)
    return deepcopy(target)


def _load_store_from_mongo() -> dict[str, Any]:
    client, types_collection, classes_collection = _mongo_collections()
    try:
        type_docs = list(types_collection.find({}))
        class_docs = list(classes_collection.find({}))
    finally:
        client.close()

    return _sorted_store(
        {
            "version": 1,
            "labware_types": [_mongo_type_to_editor_type(doc) for doc in type_docs if doc.get("name")],
            "labware_classes": [_mongo_class_to_editor_class(doc) for doc in class_docs if doc.get("name")],
        }
    )


def _write_store_to_mongo(store: dict[str, Any]) -> None:
    client, types_collection, classes_collection = _mongo_collections()
    try:
        desired_type_ids = set()
        for item in store.get("labware_types", []):
            desired_type_ids.add(str(item.get("labware_type_id") or ""))
            doc = _editor_type_to_mongo_doc(item)
            types_collection.delete_many({"labware_type_id": doc["labware_type_id"]})
            types_collection.insert_one(doc)
        if desired_type_ids:
            types_collection.delete_many({"labware_type_id": {"$nin": list(desired_type_ids)}})
        else:
            types_collection.delete_many({})

        desired_class_ids = set()
        for item in store.get("labware_classes", []):
            desired_class_ids.add(str(item.get("labware_class_id") or ""))
            doc = _editor_class_to_mongo_doc(item)
            classes_collection.delete_many({"labware_class_id": doc["labware_class_id"]})
            classes_collection.insert_one(doc)
        if desired_class_ids:
            classes_collection.delete_many({"labware_class_id": {"$nin": list(desired_class_ids)}})
        else:
            classes_collection.delete_many({})
    finally:
        client.close()


def _merge_type(item: dict[str, Any], payload: dict[str, Any]) -> None:
    nested_keys = {
        "plate_dimensions_mm",
        "plate_properties",
        "well_dimensions_mm",
        "pf400",
        "planar_motor",
        "image_2d",
        "model_3d",
    }
    for key, value in payload.items():
        if key in nested_keys and isinstance(value, dict):
            current = deepcopy(item.get(key) or {})
            current.update(value)
            item[key] = current
            continue
        if key == "labware_class_ids":
            item[key] = list(value or [])
            continue
        item[key] = value


def _editor_type_to_definition(item: dict[str, Any]) -> LabwareDefinition:
    dims = item.get("plate_dimensions_mm", {}) or {}
    props = item.get("plate_properties", {}) or {}
    wells = item.get("well_dimensions_mm", {}) or {}
    model = item.get("model_3d", {}) or {}
    definition = LabwareDefinition(
        id=str(item.get("labware_type_id") or ""),
        name=str(item.get("name") or ""),
        kind=str(item.get("kind") or ""),
        vendor=str(item.get("vendor") or ""),
        catalog_number=str(item.get("catalog_number") or ""),
        description=str(item.get("description") or ""),
        base_class=str(item.get("base_class") or ""),
        wells=int(item.get("wells") or 0),
        length_mm=float(dims.get("length_mm") or 0.0),
        width_mm=float(dims.get("width_mm") or 0.0),
        height_mm=float(props.get("thickness_mm") or dims.get("height_mm") or 0.0),
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
        tip_definition_id=str(item.get("tip_definition_id") or ""),
        supported_tip_ids=list(item.get("supported_tip_ids") or []),
        model_3d=str(model.get("url") or model.get("filename") or "") or None,
    )
    return labware_module._apply_mirrored_motion_fields(definition)


def _mongo_type_to_editor_type(doc: dict[str, Any]) -> dict[str, Any]:
    item = deepcopy(doc)
    item.pop("_id", None)
    item.setdefault("labware_type_id", str(doc.get("labware_type_id") or doc.get("_id") or _make_id("lw")))
    item.setdefault("kind", "sbs_plate")
    item.setdefault("vendor", "")
    item.setdefault("catalog_number", "")
    item.setdefault("description", "")
    item.setdefault("base_class", "")
    item.setdefault("wells", 0)
    item.setdefault("plate_dimensions_mm", {})
    item.setdefault("plate_properties", {})
    item.setdefault("well_dimensions_mm", {})
    item.setdefault("pf400", {})
    item.setdefault("planar_motor", {})
    item["labware_class_ids"] = list(item.get("labware_class_ids", []) or [])
    item["tip_definition_id"] = str(item.get("tip_definition_id") or "")
    item["supported_tip_ids"] = list(item.get("supported_tip_ids", []) or [])

    image_2d = item.get("image_2d")
    if image_2d is not None:
        item["image_2d"] = deepcopy(image_2d)

    model_3d = item.get("model_3d")
    if isinstance(model_3d, str):
        item["model_3d"] = {
            "url": f"/labware/{model_3d}",
            "filename": model_3d,
            "format": Path(model_3d).suffix.lstrip(".").lower(),
        }
    elif model_3d is None:
        item["model_3d"] = None
    else:
        item["model_3d"] = deepcopy(model_3d)

    return item


def _mongo_class_to_editor_class(doc: dict[str, Any]) -> dict[str, Any]:
    item = deepcopy(doc)
    item.pop("_id", None)
    item.setdefault(
        "labware_class_id",
        str(doc.get("labware_class_id") or doc.get("_id") or _make_id("class")),
    )
    item.setdefault("description", "")
    return item


def _editor_type_to_mongo_doc(item: dict[str, Any]) -> dict[str, Any]:
    doc = deepcopy(item)
    doc.pop("_id", None)
    if doc.get("image_2d") is None:
        doc.pop("image_2d", None)
    if doc.get("model_3d") is None:
        doc["model_3d"] = None
    return doc


def _editor_class_to_mongo_doc(item: dict[str, Any]) -> dict[str, Any]:
    doc = deepcopy(item)
    doc.pop("_id", None)
    return doc


def _definition_to_editor_type(definition: LabwareDefinition) -> dict[str, Any]:
    model_url = None
    model_name = definition.model_3d
    if model_name:
        model_url = f"/labware/{model_name}"
    return {
        "labware_type_id": definition.id,
        "name": definition.name,
        "kind": definition.kind,
        "vendor": definition.vendor,
        "catalog_number": definition.catalog_number,
        "description": definition.description,
        "base_class": definition.base_class,
        "wells": definition.wells,
        "plate_dimensions_mm": {
            "length_mm": definition.length_mm,
            "width_mm": definition.width_mm,
            "height_mm": definition.height_mm,
        },
        "plate_properties": {
            "robot_gripper_offset_mm": definition.gripper_offset_mm,
            "robot_lid_gripper_offset_mm": definition.lid_gripper_offset_mm,
            "empty_check_offset_mm": definition.empty_check_offset_mm,
            "thickness_mm": definition.height_mm,
            "stacking_thickness_mm": definition.stack_height_mm,
            "shim_thickness_mm": definition.shim_thickness_mm,
            "can_be_sealed": definition.can_be_sealed,
            "sealed_thickness_mm": definition.sealed_height_mm,
            "sealed_stacking_thickness_mm": definition.sealed_stacking_height_mm,
            "can_have_lid": definition.can_have_lid,
            "lidded_thickness_mm": definition.lidded_height_mm,
            "lidded_stacking_thickness_mm": definition.lidded_stack_height_mm,
            "lid_resting_height_mm": definition.lid_resting_height_mm,
            "lid_departure_height_mm": definition.lid_departure_height_mm,
            "max_robot_handling_speed": definition.max_robot_handling_speed,
        },
        "well_dimensions_mm": {
            "rows": definition.rows,
            "cols": definition.cols,
            "volume_ul": definition.well_volume_ul,
            "diameter_mm": definition.well_diameter_mm,
            "disposable_tip_capacity_ul": definition.disposable_tip_capacity_ul,
        },
        "pf400": {},
        "planar_motor": {},
        "labware_class_ids": [],
        "tip_definition_id": definition.tip_definition_id,
        "supported_tip_ids": list(definition.supported_tip_ids or []),
        "image_2d": None,
        "model_3d": (
            {
                "url": model_url,
                "filename": definition.model_3d,
                "format": Path(definition.model_3d).suffix.lstrip(".").lower(),
            }
            if model_url
            else None
        ),
    }


def _sanitize_filename(filename: str) -> str:
    name = Path(filename).name
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def _make_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"
