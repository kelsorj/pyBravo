"""The autofill tray glTF is generated, and the UI relies on its "liquid" node."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "frontend" / "accessories" / "AutofillStation.gltf"


def _builder():
    spec = importlib.util.spec_from_file_location(
        "build_autofill_tray_model", ROOT / "scripts" / "build_autofill_tray_model.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _node(gltf: dict, name: str) -> dict:
    return next(n for n in gltf["nodes"] if n["name"] == name)


def _bounds(gltf: dict, name: str) -> tuple[list[float], list[float]]:
    mesh = gltf["meshes"][_node(gltf, name)["mesh"]]
    accessor = gltf["accessors"][mesh["primitives"][0]["attributes"]["POSITION"]]
    return accessor["min"], accessor["max"]


def test_checked_in_model_matches_the_generator():
    assert json.loads(MODEL.read_text(encoding="utf-8")) == json.loads(json.dumps(_builder().build()))


def test_liquid_node_scales_from_its_floor():
    gltf = _builder().build()
    lo, hi = _bounds(gltf, "liquid")
    assert lo[1] == 0.0 and hi[1] > 0.0  # bottom at the node origin, so scale.y keeps it on the floor
    assert _node(gltf, "liquid")["translation"][1] > 0.0
    material = gltf["materials"][gltf["meshes"][_node(gltf, "liquid")["mesh"]]["primitives"][0]["material"]]
    assert material["alphaMode"] == "BLEND"


def test_fittings_leave_through_the_back_wall():
    gltf = _builder().build()
    tub_min, _ = _bounds(gltf, "tub")
    for name in ("fitting_1", "fitting_2"):
        lo, hi = _bounds(gltf, name)
        assert hi[2] <= tub_min[2] + 1e-9  # glTF -Z is the back of the deck
        assert lo[0] > 0  # right-hand half
