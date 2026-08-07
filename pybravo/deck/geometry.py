from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pybravo.head_mode import TipSelection


@dataclass(frozen=True)
class WellGeometry:
    rows: int
    cols: int
    pitch_x_mm: float
    pitch_y_mm: float
    offset_x_mm: float
    offset_y_mm: float


def _rows_cols_from_metadata(metadata: dict[str, Any]) -> tuple[int, int]:
    rows = int(metadata.get("rows") or 0)
    cols = int(metadata.get("cols") or 0)
    if rows > 0 and cols > 0:
        return rows, cols
    wells = int(metadata.get("wells") or 0)
    if wells == 96:
        return 8, 12
    if wells == 384:
        return 16, 24
    if wells == 1536:
        return 32, 48
    return rows, cols


def _default_pitch_mm(count: int) -> float:
    if count >= 32:
        return 2.25
    if count >= 16:
        return 4.5
    return 9.0


def _default_offset_mm(rows: int, cols: int, pitch_x_mm: float, pitch_y_mm: float) -> tuple[float, float]:
    if rows >= 32 or cols >= 48:
        return 3.375, 3.375
    if rows >= 16 or cols >= 24:
        return 2.25, 2.25
    return 0.0, 0.0


def well_geometry_from_metadata(metadata: dict[str, Any] | None) -> WellGeometry:
    raw = dict(metadata or {})
    well_dims = dict(raw.get("well_dimensions_mm") or raw)
    rows, cols = _rows_cols_from_metadata(well_dims or raw)
    pitch_x_mm = float(well_dims.get("spacing_x_mm") or _default_pitch_mm(cols))
    pitch_y_mm = float(well_dims.get("spacing_y_mm") or _default_pitch_mm(rows))
    default_offset_x_mm, default_offset_y_mm = _default_offset_mm(rows, cols, pitch_x_mm, pitch_y_mm)
    raw_offset_x_mm = well_dims.get("offset_x_mm")
    raw_offset_y_mm = well_dims.get("offset_y_mm")
    offset_x_mm = float(raw_offset_x_mm) if raw_offset_x_mm is not None else default_offset_x_mm
    offset_y_mm = float(raw_offset_y_mm) if raw_offset_y_mm is not None else default_offset_y_mm
    if offset_x_mm == 0.0 and offset_y_mm == 0.0 and (rows >= 16 or cols >= 24):
        offset_x_mm = default_offset_x_mm
        offset_y_mm = default_offset_y_mm
    return WellGeometry(
        rows=rows,
        cols=cols,
        pitch_x_mm=pitch_x_mm,
        pitch_y_mm=pitch_y_mm,
        offset_x_mm=offset_x_mm,
        offset_y_mm=offset_y_mm,
    )


def a1_center_offset_from_teachpoint_mm(metadata: dict[str, Any] | None) -> tuple[float, float]:
    geometry = well_geometry_from_metadata(metadata)
    return -geometry.offset_x_mm, -geometry.offset_y_mm


def well_center_offset_from_teachpoint_mm(
    metadata: dict[str, Any] | None,
    *,
    row: int,
    col: int,
) -> tuple[float, float]:
    geometry = well_geometry_from_metadata(metadata)
    base_x_mm, base_y_mm = a1_center_offset_from_teachpoint_mm(metadata)
    return (
        base_x_mm + int(col) * geometry.pitch_x_mm,
        base_y_mm + int(row) * geometry.pitch_y_mm,
    )


def tipbox_anchor_offset_from_teachpoint_mm(
    metadata: dict[str, Any] | None,
    selection: TipSelection,
) -> tuple[float, float]:
    return well_center_offset_from_teachpoint_mm(
        metadata,
        row=int(selection.row),
        col=int(selection.col),
    )
