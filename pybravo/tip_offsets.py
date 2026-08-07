"""Per-(head, tip box) Tips On / Tips Off geometry overrides.

The Tips On press depth/tolerance and the Tips Off eject depth (Z) and ejector
throw (W) depend on the physical combination of head type and tip box: a long
LT200 tip needs to eject much further above the box than a short ST10 tip, and a
deeper-engaging tip trips the Tips On force-press accept window if it is sized
for a shorter tip.

These overrides live in ``config/tip_offsets.yaml`` and are resolved at Tips On /
Tips Off time by matching the active head type and the tip box labware (by name,
case/whitespace-insensitive, or by ``labware_type_id``). Any field left unset on
a matching entry — or the absence of any matching entry — falls back to the
profile's ``safety.*`` defaults (``tips_off_z_offset`` / ``tips_off_w_position``)
and the global :data:`pybravo.types.TIPBOX_JOG_TOLERANCE` for the press window.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from pybravo.types import TIPBOX_JOG_TOLERANCE, HeadType

logger = logging.getLogger(__name__)

_STORE_PATH = Path(__file__).resolve().parents[1] / "config" / "tip_offsets.yaml"


@dataclass(frozen=True)
class TipOffsetEntry:
    """One (head, tip box) override row from ``tip_offsets.yaml``.

    Every numeric field is optional; ``None`` means "fall back to the profile
    default for this field". Matching requires ``head_type`` plus at least one of
    ``tipbox`` (labware name) or ``tipbox_id`` (labware_type_id).
    """

    head_type: str
    tipbox: str = ""
    tipbox_id: str = ""
    tips_off_z_offset: float | None = None
    tips_off_w_position: float | None = None
    tips_on_jog_tolerance: float | None = None
    tips_on_z_offset: float | None = None


@dataclass(frozen=True)
class ResolvedTipOffsets:
    """Fully resolved offsets, with every field filled (override or default)."""

    tips_off_z_offset: float
    tips_off_w_position: float
    tips_on_jog_tolerance: float
    tips_on_z_offset: float
    matched: bool
    source: str


def _normalize_key(value: Any) -> str:
    return " ".join(str(value or "").split()).strip().lower()


def _normalize_head(value: Any) -> str:
    """Normalize a head type (enum, name, or int value) to its enum name."""
    if isinstance(value, HeadType):
        return value.name
    text = str(value if value is not None else "").strip()
    if not text:
        return ""
    try:
        return HeadType(int(text)).name
    except (ValueError, KeyError):
        return text.upper()


def _coerce_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class TipOffsetTable:
    """In-memory collection of :class:`TipOffsetEntry` rows with lookup."""

    def __init__(self, entries: list[TipOffsetEntry]) -> None:
        self._entries = list(entries)

    @property
    def entries(self) -> list[TipOffsetEntry]:
        return list(self._entries)

    def find(
        self,
        head_type: HeadType | str,
        *,
        tipbox_name: str = "",
        tipbox_id: str = "",
    ) -> TipOffsetEntry | None:
        """Return the first entry matching ``head_type`` and the tip box.

        A row matches when its head type matches AND either its tip box id or its
        tip box name matches the supplied values. Id match is preferred but a
        name match is equally accepted (entries are scanned in file order).
        """
        head = _normalize_head(head_type)
        if not head:
            return None
        name_key = _normalize_key(tipbox_name)
        id_key = _normalize_key(tipbox_id)
        for entry in self._entries:
            if _normalize_head(entry.head_type) != head:
                continue
            entry_id = _normalize_key(entry.tipbox_id)
            entry_name = _normalize_key(entry.tipbox)
            if entry_id and id_key and entry_id == id_key:
                return entry
            if entry_name and name_key and entry_name == name_key:
                return entry
        return None

    def resolve(
        self,
        head_type: HeadType | str,
        *,
        tipbox_name: str = "",
        tipbox_id: str = "",
        default_z_offset: float,
        default_w_position: float,
        default_jog_tolerance: float = TIPBOX_JOG_TOLERANCE,
        default_z_on_offset: float = 0.0,
    ) -> ResolvedTipOffsets:
        """Resolve offsets for a (head, tip box), filling gaps with defaults."""
        entry = self.find(head_type, tipbox_name=tipbox_name, tipbox_id=tipbox_id)
        if entry is None:
            return ResolvedTipOffsets(
                tips_off_z_offset=float(default_z_offset),
                tips_off_w_position=float(default_w_position),
                tips_on_jog_tolerance=float(default_jog_tolerance),
                tips_on_z_offset=float(default_z_on_offset),
                matched=False,
                source="profile defaults",
            )
        label = entry.tipbox or entry.tipbox_id or "?"
        return ResolvedTipOffsets(
            tips_off_z_offset=float(
                entry.tips_off_z_offset
                if entry.tips_off_z_offset is not None
                else default_z_offset
            ),
            tips_off_w_position=float(
                entry.tips_off_w_position
                if entry.tips_off_w_position is not None
                else default_w_position
            ),
            tips_on_jog_tolerance=float(
                entry.tips_on_jog_tolerance
                if entry.tips_on_jog_tolerance is not None
                else default_jog_tolerance
            ),
            tips_on_z_offset=float(
                entry.tips_on_z_offset
                if entry.tips_on_z_offset is not None
                else default_z_on_offset
            ),
            matched=True,
            source=f"tip_offsets[{_normalize_head(entry.head_type)} / {label}]",
        )


def _entries_from_raw(raw: dict[str, Any]) -> list[TipOffsetEntry]:
    items = raw.get("offsets")
    if items is None:
        items = raw.get("tip_offsets")
    entries: list[TipOffsetEntry] = []
    for item in list(items or []):
        if not isinstance(item, dict):
            continue
        head = _normalize_head(item.get("head_type"))
        if not head:
            continue
        entries.append(
            TipOffsetEntry(
                head_type=head,
                tipbox=str(item.get("tipbox") or item.get("tipbox_name") or "").strip(),
                tipbox_id=str(item.get("tipbox_id") or item.get("labware_type_id") or "").strip(),
                tips_off_z_offset=_coerce_float(item.get("tips_off_z_offset")),
                tips_off_w_position=_coerce_float(item.get("tips_off_w_position")),
                tips_on_jog_tolerance=_coerce_float(item.get("tips_on_jog_tolerance")),
                tips_on_z_offset=_coerce_float(item.get("tips_on_z_offset")),
            )
        )
    return entries


def load_tip_offset_table(path: Path | str | None = None) -> TipOffsetTable:
    """Load the tip-offset table from disk. Missing/malformed files yield an
    empty table (so callers fall back to profile defaults)."""
    store_path = Path(path) if path is not None else _STORE_PATH
    if not store_path.exists():
        return TipOffsetTable([])
    try:
        with open(store_path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    except Exception as exc:  # noqa: BLE001 - config errors must never break motion
        logger.warning("Could not read tip offsets config %s: %s", store_path, exc)
        return TipOffsetTable([])
    if not isinstance(raw, dict):
        logger.warning(
            "Ignoring malformed tip offsets config %s: expected a mapping", store_path
        )
        return TipOffsetTable([])
    entries = _entries_from_raw(raw)
    logger.info("Loaded %d tip-offset override(s) from %s", len(entries), store_path)
    return TipOffsetTable(entries)


_CACHED_TABLE: TipOffsetTable | None = None


def get_tip_offset_table(*, reload: bool = False) -> TipOffsetTable:
    """Return the process-wide tip-offset table, loading/caching on first use."""
    global _CACHED_TABLE
    if _CACHED_TABLE is None or reload:
        _CACHED_TABLE = load_tip_offset_table()
    return _CACHED_TABLE
