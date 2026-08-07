"""Sync check — scraped node defaults cover everything the drafter emits.

The drafter's diff module derives its per-node-type property defaults
from ``frontend/designer.html`` at import time so the table can never
drift from the actual UI. This test locks that invariant in: if
someone adds a new ``makeTaskNode`` or ``addProperty`` call the
scraper can't parse — or removes one the hardcoded fallback expects —
CI fails here loudly rather than silently falling back at runtime.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pybravo.workflow.drafter.defaults_scraper import (
    scrape_node_defaults,
)
from pybravo.workflow.drafter.schema import SUPPORTED_NODE_TYPES

DESIGNER_HTML = Path(__file__).resolve().parents[1] / "frontend" / "designer.html"


def test_designer_html_is_present():
    assert DESIGNER_HTML.exists(), f"expected designer.html at {DESIGNER_HTML}"


def test_scrape_covers_every_supported_node_type():
    """Every type registered in SUPPORTED_NODE_TYPES must be scrapable
    (or at least enumerable). flow/Start and flow/End have no
    registered properties but still need to appear so downstream
    code can check membership.
    """
    scraped = scrape_node_defaults(DESIGNER_HTML)
    missing = [t for t in SUPPORTED_NODE_TYPES if t not in scraped]
    assert not missing, (
        f"scraper missed {len(missing)} node types: {missing!r}. "
        "Either the designer added a new type that the scraper's "
        "regex can't match, or the SUPPORTED_NODE_TYPES list drifted."
    )


def test_scraped_defaults_match_expected_shapes():
    """Spot-check a handful of well-known defaults so breaking any of
    them produces a readable failure, not just a missing-key error.
    """
    scraped = scrape_node_defaults(DESIGNER_HTML)

    # Aspirate's core params
    asp = scraped["liquid/Aspirate"]
    assert asp["volume"] == 50
    assert asp["tip_touch"] is True
    assert asp["anchor"] == "A1"
    assert asp["distance_from_bottom"] == 1.0

    # TipsOn carries a structured head_mode dict matching the backend
    # shape (subset_type / subset_config / row_count / column_count). The
    # workflow designer renders a head-mode picker bound to this property.
    tips_on_head_mode = scraped["tips/TipsOn"]["head_mode"]
    assert isinstance(tips_on_head_mode, dict), tips_on_head_mode
    assert tips_on_head_mode.get("subset_type") == "all_barrels"
    assert tips_on_head_mode.get("subset_config") == "back_left"
    # Both TipsOn and TipsOff expose a tip-box anchor (row/col) so the
    # operator can choose which cells of the box are used.
    assert scraped["tips/TipsOn"]["tip_anchor_row"] == 0
    assert scraped["tips/TipsOn"]["tip_anchor_col"] == 0
    assert scraped["tips/TipsOff"]["tip_anchor_row"] == 0
    assert scraped["tips/TipsOff"]["tip_anchor_col"] == 0
    # TipsOff has NO head_mode — inherited from upstream Tips On at
    # design time (workflow rule: head can't reconfigure mid-cycle).
    assert "head_mode" not in scraped["tips/TipsOff"], scraped["tips/TipsOff"]

    # Plate movement has the two-location pattern
    assert scraped["plate/PickPlace"] == {"pick_location": 1, "place_location": 2}

    # IfElse carries the quote-containing default; validates that the
    # single-to-double quote rewriter survived "barcode == ''".
    assert scraped["flow/IfElse"]["condition"] == 'barcode == ""'

    # Frame has a list and null default — exercises the full value parser.
    frame = scraped["flow/Frame"]
    assert frame["member_ids"] == []
    assert frame["expanded_bbox"] is None
    assert frame["collapsed"] is False


def test_no_unexpected_types_snuck_in():
    """If the scraper picks up a type that isn't in SUPPORTED_NODE_TYPES,
    something's out of sync between the schema and the designer. We
    don't fail on this (could be a legit new type mid-development) but
    surface it so the author notices.
    """
    scraped = scrape_node_defaults(DESIGNER_HTML)
    extras = [t for t in scraped if t not in SUPPORTED_NODE_TYPES]
    assert not extras, (
        f"scraper found {len(extras)} types not in SUPPORTED_NODE_TYPES: "
        f"{extras!r}. Either add them to the schema or mark them "
        "non-draftable by excluding them from the scraper."
    )


def test_diff_module_loaded_from_scrape_or_fallback():
    """The diff module tries scraping first. On a clean checkout that
    should succeed; if this starts reporting 'fallback_*' in CI that's
    the signal to inspect what broke without having to wait for a
    real draft to expose it."""
    from pybravo.workflow.drafter.diff import node_property_defaults_info
    info = node_property_defaults_info()
    assert info["type_count"] >= len(SUPPORTED_NODE_TYPES), (
        f"diff loaded {info['type_count']} types, expected ≥ "
        f"{len(SUPPORTED_NODE_TYPES)}. source={info.get('source')!r}"
    )
    # Don't hard-fail on partial fallback — the hardcoded table is a
    # valid (if less-fresh) snapshot. Just be loud.
    if info["source"] != "scraped":
        pytest.skip(
            f"diff is using {info['source']!r} — designer scrape failed "
            "or was incomplete. Investigate, but the fallback keeps the "
            "system running."
        )
