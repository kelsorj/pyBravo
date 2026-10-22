"""Generate frontend/accessories/AutofillStation.gltf, the autofill tray visual.

A pale tub with an inset tray on a dark weigh pad, with two hose fittings in
the back wall. The node named ``liquid`` is drawn at full depth with its
bottom at the node origin; the UI scales it along Y to show the weigh-pad
level (0 = empty, 1 = full).

glTF convention: metres, +Y up. The UI turns +Y into deck +Z, and glTF -Z into
deck +Y (the back of the deck, towards row 1-3), so the fittings sit at -Z.

Usage:
    python scripts/build_autofill_tray_model.py [--output PATH]
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import struct
from pathlib import Path

PAD_H = 0.010
TUB_L, TUB_W, TUB_H, WALL = 0.132, 0.092, 0.034, 0.006
TRAY_L, TRAY_W, TRAY_H, TRAY_WALL = 0.114, 0.076, 0.024, 0.003
FITTING_R, FITTING_LEN = 0.004, 0.012

Mesh = tuple[list[float], list[float], list[int]]  # positions, normals, indices


def box(cx: float, cy: float, cz: float, sx: float, sy: float, sz: float) -> Mesh:
    hx, hy, hz = sx / 2, sy / 2, sz / 2
    faces = [
        ((1, 0, 0), [(hx, -hy, -hz), (hx, hy, -hz), (hx, hy, hz), (hx, -hy, hz)]),
        ((-1, 0, 0), [(-hx, -hy, hz), (-hx, hy, hz), (-hx, hy, -hz), (-hx, -hy, -hz)]),
        ((0, 1, 0), [(-hx, hy, -hz), (-hx, hy, hz), (hx, hy, hz), (hx, hy, -hz)]),
        ((0, -1, 0), [(-hx, -hy, hz), (-hx, -hy, -hz), (hx, -hy, -hz), (hx, -hy, hz)]),
        ((0, 0, 1), [(-hx, -hy, hz), (hx, -hy, hz), (hx, hy, hz), (-hx, hy, hz)]),
        ((0, 0, -1), [(hx, -hy, -hz), (-hx, -hy, -hz), (-hx, hy, -hz), (hx, hy, -hz)]),
    ]
    pos, nrm, idx = [], [], []
    for normal, corners in faces:
        base = len(pos) // 3
        for x, y, z in corners:
            pos += [cx + x, cy + y, cz + z]
            nrm += list(normal)
        idx += [base, base + 1, base + 2, base, base + 2, base + 3]
    return pos, nrm, idx


def cylinder_z(cx: float, cy: float, cz: float, r: float, length: float, segments: int = 20) -> Mesh:
    """Capped cylinder along Z, centred at (cx, cy, cz)."""
    pos, nrm, idx = [], [], []
    z0, z1 = cz - length / 2, cz + length / 2
    for i in range(segments + 1):
        a = 2 * math.pi * i / segments
        x, y = math.cos(a), math.sin(a)
        pos += [cx + r * x, cy + r * y, z0, cx + r * x, cy + r * y, z1]
        nrm += [x, y, 0, x, y, 0]
    for i in range(segments):
        a, b = 2 * i, 2 * i + 2
        idx += [a, b, a + 1, b, b + 1, a + 1]
    for z, nz in ((z0, -1), (z1, 1)):
        centre = len(pos) // 3
        pos += [cx, cy, z]
        nrm += [0, 0, nz]
        for i in range(segments + 1):
            a = 2 * math.pi * i / segments
            pos += [cx + r * math.cos(a), cy + r * math.sin(a), z]
            nrm += [0, 0, nz]
        for i in range(segments):
            p, q = centre + 1 + i, centre + 2 + i
            idx += [centre, q, p] if nz < 0 else [centre, p, q]
    return pos, nrm, idx


def merge(*meshes: Mesh) -> Mesh:
    pos, nrm, idx = [], [], []
    for p, n, i in meshes:
        base = len(pos) // 3
        pos += p
        nrm += n
        idx += [base + k for k in i]
    return pos, nrm, idx


def open_box(length: float, width: float, height: float, wall: float, base_y: float) -> Mesh:
    """Floor plus four walls; length along X, width along Z, height along Y."""
    mid = base_y + height / 2
    return merge(
        box(0, base_y + wall / 2, 0, length, wall, width),
        box(0, mid, (width - wall) / 2, length, height, wall),
        box(0, mid, -(width - wall) / 2, length, height, wall),
        box((length - wall) / 2, mid, 0, wall, height, width - 2 * wall),
        box(-(length - wall) / 2, mid, 0, wall, height, width - 2 * wall),
    )


def build() -> dict:
    tray_base = PAD_H + TUB_H - TRAY_H
    liquid_floor = tray_base + TRAY_WALL
    liquid_depth = TRAY_H - TRAY_WALL - 0.002
    parts = [
        # name, mesh, material index, node translation
        ("pad", box(0, PAD_H / 2, 0, TUB_L - 0.004, PAD_H, TUB_W - 0.004), 0, None),
        ("tub", open_box(TUB_L, TUB_W, TUB_H, WALL, PAD_H), 1, None),
        ("tray", open_box(TRAY_L, TRAY_W, TRAY_H, TRAY_WALL, tray_base), 2, None),
        # Fittings leave through the back wall (-Z here), near the right-hand corner.
        ("fitting_1", cylinder_z(0.030, PAD_H + TUB_H * 0.55, -(TUB_W / 2 + FITTING_LEN / 2), FITTING_R, FITTING_LEN), 3, None),
        ("fitting_2", cylinder_z(0.052, PAD_H + TUB_H * 0.55, -(TUB_W / 2 + FITTING_LEN / 2), FITTING_R, FITTING_LEN), 3, None),
        # Full-depth liquid, bottom at the node origin so scaling Y keeps it on the floor.
        ("liquid", box(0, liquid_depth / 2, 0, TRAY_L - 2 * TRAY_WALL, liquid_depth, TRAY_W - 2 * TRAY_WALL), 4,
         [0, liquid_floor, 0]),
    ]
    materials = [
        {"name": "weigh_pad", "pbrMetallicRoughness": {"baseColorFactor": [0.231, 0.251, 0.282, 1], "metallicFactor": 0.25, "roughnessFactor": 0.6}},
        {"name": "tub", "pbrMetallicRoughness": {"baseColorFactor": [0.890, 0.922, 0.937, 1], "metallicFactor": 0.02, "roughnessFactor": 0.55}},
        {"name": "tray", "pbrMetallicRoughness": {"baseColorFactor": [0.839, 0.886, 0.914, 1], "metallicFactor": 0.02, "roughnessFactor": 0.5}},
        {"name": "fitting", "pbrMetallicRoughness": {"baseColorFactor": [0.914, 0.875, 0.769, 1], "metallicFactor": 0.0, "roughnessFactor": 0.7}},
        {"name": "liquid", "alphaMode": "BLEND", "doubleSided": True,
         "pbrMetallicRoughness": {"baseColorFactor": [0.184, 0.561, 0.910, 0.6], "metallicFactor": 0.0, "roughnessFactor": 0.15}},
    ]

    blob = bytearray()
    buffer_views, accessors, meshes, nodes = [], [], [], []

    def add_view(data: bytes, target: int) -> int:
        while len(blob) % 4:
            blob.append(0)
        buffer_views.append({"buffer": 0, "byteOffset": len(blob), "byteLength": len(data), "target": target})
        blob.extend(data)
        return len(buffer_views) - 1

    for name, (pos, nrm, idx), material, translation in parts:
        xs, ys, zs = pos[0::3], pos[1::3], pos[2::3]
        p_view = add_view(struct.pack(f"<{len(pos)}f", *pos), 34962)
        n_view = add_view(struct.pack(f"<{len(nrm)}f", *nrm), 34962)
        i_view = add_view(struct.pack(f"<{len(idx)}H", *idx), 34963)
        accessors += [
            {"bufferView": p_view, "componentType": 5126, "count": len(pos) // 3, "type": "VEC3",
             "min": [min(xs), min(ys), min(zs)], "max": [max(xs), max(ys), max(zs)]},
            {"bufferView": n_view, "componentType": 5126, "count": len(nrm) // 3, "type": "VEC3"},
            {"bufferView": i_view, "componentType": 5123, "count": len(idx), "type": "SCALAR"},
        ]
        a = len(accessors) - 3
        meshes.append({"name": name, "primitives": [
            {"attributes": {"POSITION": a, "NORMAL": a + 1}, "indices": a + 2, "material": material}
        ]})
        node = {"name": name, "mesh": len(meshes) - 1}
        if translation:
            node["translation"] = translation
        nodes.append(node)

    return {
        "asset": {"version": "2.0", "generator": "pybravo scripts/build_autofill_tray_model.py"},
        "scene": 0,
        "scenes": [{"name": "AutofillStation", "nodes": list(range(len(nodes)))}],
        "nodes": nodes,
        "meshes": meshes,
        "materials": materials,
        "accessors": accessors,
        "bufferViews": buffer_views,
        "buffers": [{"byteLength": len(blob),
                     "uri": "data:application/octet-stream;base64," + base64.b64encode(bytes(blob)).decode("ascii")}],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--output", type=Path,
        default=Path(__file__).resolve().parents[1] / "frontend" / "accessories" / "AutofillStation.gltf",
    )
    args = parser.parse_args()
    args.output.write_text(json.dumps(build(), indent=1), encoding="utf-8")
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
