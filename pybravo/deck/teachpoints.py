"""Per-location XYZ teachpoints.

Stores and manipulates the calibrated head positions for each of the nine
deck locations, with head-type-specific defaults and tip-length compensation.
"""

from __future__ import annotations

from pybravo.types import (
    Axis,
    HeadType,
    MAX_ROWS,
    MAX_COLS,
    MIN_LOCATION,
    X_TO_X_DISTANCE,
    Y_TO_Y_DISTANCE,
)

_LOC1_DEFAULTS: dict[str, dict[Axis, float]] = {
    "96ch_disposable": {Axis.X: 5.79, Axis.Y: 5.98, Axis.Z: 60.0},
    "384ch_disposable": {Axis.X: 8.03, Axis.Y: 8.22, Axis.Z: 60.0},
    "8ch_lt": {Axis.X: -49.24, Axis.Y: 5.98, Axis.Z: 60.0},
}

_HEAD_TYPE_CATEGORY: dict[HeadType, str] = {
    HeadType.HT_96_D_70: "96ch_disposable",
    HeadType.HT_96_D_70_S2: "96ch_disposable",
    HeadType.HT_96_D_200: "96ch_disposable",
    HeadType.HT_96_D_200_S2: "96ch_disposable",
    HeadType.HT_384_D_70: "384ch_disposable",
    HeadType.HT_384_D_70_S2: "384ch_disposable",
    HeadType.HT_8_D_LT: "8ch_lt",
}


class Teachpoints:
    """Calibrated head positions for each deck location.

    Positions are stored as ``{location: {axis: value_mm}}``.
    """

    def __init__(self) -> None:
        self._data: dict[int, dict[Axis, float]] = {}

    def get_teachpoint(self, location: int, axis: Axis) -> float:
        """Return the teachpoint value for *location* and *axis*."""
        try:
            return self._data[location][axis]
        except KeyError:
            raise KeyError(
                f"No teachpoint for location {location}, {axis.label}"
            ) from None

    def set_teachpoint(self, location: int, axis: Axis, value: float) -> None:
        """Set (or overwrite) a single teachpoint value."""
        self._data.setdefault(location, {})[axis] = value

    def set_default_teachpoints(self, head_type: HeadType) -> None:
        """Populate all 9 locations with default teachpoints for *head_type*.

        Location 1 uses head-type-specific offsets; remaining locations are
        derived from the grid spacing constants ``X_TO_X_DISTANCE`` and
        ``Y_TO_Y_DISTANCE``.
        """
        category = _HEAD_TYPE_CATEGORY.get(head_type)
        if category is None:
            raise ValueError(
                f"No default teachpoints defined for head type {head_type.name}"
            )

        loc1 = _LOC1_DEFAULTS[category]
        self._data.clear()

        for row in range(MAX_ROWS):
            for col in range(MAX_COLS):
                location = row * MAX_COLS + col + MIN_LOCATION
                self._data[location] = {
                    Axis.X: loc1[Axis.X] + col * X_TO_X_DISTANCE,
                    Axis.Y: loc1[Axis.Y] + row * Y_TO_Y_DISTANCE,
                    Axis.Z: loc1[Axis.Z],
                }

    def compensate_for_tip(
        self,
        location: int,
        default_tip_length: float,
        current_tip_length: float,
    ) -> None:
        """Adjust the Z teachpoint at *location* for a non-default tip length.

        The Z position is shifted by ``(default_tip_length - current_tip_length)``
        so the pipette tips reach the same physical height regardless of tip
        length.
        """
        z = self.get_teachpoint(location, Axis.Z)
        self.set_teachpoint(
            location,
            Axis.Z,
            z + (default_tip_length - current_tip_length),
        )

    @property
    def locations(self) -> list[int]:
        """Return sorted list of locations that have teachpoints."""
        return sorted(self._data.keys())

    def as_dict(self) -> dict[int, dict[Axis, float]]:
        """Return a shallow copy of the internal data."""
        return {loc: dict(axes) for loc, axes in self._data.items()}
