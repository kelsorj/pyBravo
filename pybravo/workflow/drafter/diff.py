"""Structural diff between a drafted workflow and the user-edited final.

The drafter emits a workflow JSON; the user edits it in the designer;
when the user saves / simulates / executes, we compare the two and
record what changed. The output of :func:`compute_workflow_diff` is
the training signal — "the LLM emitted volume=10 but the user changed
it to 20" is directly useful for future prompt tuning, fine-tuning, or
confidence calibration.

Design notes:

* Node identity is by ``id`` (LiteGraph's integer counter). LiteGraph
  keeps these stable across edits within a session; even across a
  save/reload cycle the ids are preserved because our serializer
  round-trips them.
* Links are compared as normalized 6-tuples
  ``(origin_id, origin_slot, target_id, target_slot, type)``. We drop
  the link_id from the tuple because LiteGraph can re-number links on
  reload.
* Property deltas are shallow (top-level keys only). Nested objects are
  diffed by ``str(old) != str(new)`` — good enough for designer-edited
  primitives, and avoids recursive-structure surprises.
* We compute a ``summary`` of aggregate metrics (unchanged %, citation
  retention, edit magnitude bucket) for easy dashboarding.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_CITATION_KEY = "_source_citation"   # matches DraftedWorkflow.to_designer_json


# Per-node-type property defaults — mirror what the designer's LiteGraph
# node constructors install via ``this.addProperty(key, val)``. When an
# LLM-drafted workflow omits (or nulls out) a property, loading it into
# the designer populates the field with this default, and the diff
# would otherwise flag that round-trip as a user edit.
#
# Source of truth: ``frontend/designer.html``. We scrape makeTaskNode()
# calls + addProperty() calls at module import so the Python table is
# always in sync with the designer. If scraping fails for any reason
# (file missing, unexpected JS syntax, etc.), we fall back to this
# hardcoded table — a frozen snapshot that's guaranteed to compile
# but may drift from the designer over time. The active source is
# reported by ``node_property_defaults_info()``.

_NODE_PROPERTY_DEFAULTS_HARDCODED: dict[str, dict[str, Any]] = {
    "liquid/Aspirate": {
        "location": 1, "volume": 50, "liquid_class": "", "pipette_technique": "",
        "pre_aspirate_volume": 0, "post_aspirate_volume": 0,
        "distance_from_bottom": 1.0, "dynamic_tip_extension": 0,
        "tip_touch": True, "anchor": "A1",
    },
    "liquid/Dispense": {
        "location": 1, "volume": 50, "liquid_class": "", "pipette_technique": "",
        "blowout_volume": 0, "empty_tips": False,
        "distance_from_bottom": 1.0, "dynamic_tip_retraction": 0,
        "tip_touch": True, "anchor": "A1",
    },
    "liquid/Mix": {
        "location": 1, "volume": 50, "cycles": 3, "liquid_class": "",
        "pipette_technique": "", "distance_from_bottom": 1.0, "anchor": "A1",
    },
    "tips/TipsOn":  {
        "location": 1,
        "head_mode": {"subset_type": "all_barrels", "subset_config": "back_left"},
        "tip_anchor_row": 0,
        "tip_anchor_col": 0,
    },
    # Tips Off has `location` + `tip_anchor_row`/`tip_anchor_col`. The
    # head_mode is INHERITED at design time from the upstream Tips On
    # (workflow domain rule: one Tips Off per Tips On, head can't
    # reconfigure mid-cycle), so it is NOT a property on Tips Off. The
    # return anchor defaults to (0, 0) and the picker (which uses the
    # inherited head_mode for legality) snaps it to a legal cell.
    "tips/TipsOff": {"location": 1, "tip_anchor_row": 0, "tip_anchor_col": 0},
    "plate/PickPlace": {"pick_location": 1, "place_location": 2},
    "plate/Stack":    {"source_location": 1, "base_location": 2},
    "plate/Destack":  {"source_location": 1, "destination_location": 2},
    "plate/Mount":    {"source_location": 1, "base_location": 2},
    "plate/Unmount":  {"source_location": 1, "destination_location": 2},
    "plate/Delid":    {"location": 1},
    "plate/Relid":    {"location": 1},
    "sensor/ReadBarcode":     {"location": 1, "store_as": ""},
    "sensor/ScanStackHeight": {"location": 1, "expected_count": 0, "store_as": ""},
    "system/Initialize":   {},
    "system/Home":         {"axes": "X,Y,Z,W,G,Zg"},
    "system/DockGripper":  {},
    "flow/IfElse":  {"condition": 'barcode == ""'},
    "flow/Loop":    {"count": 3},
    "flow/Frame":   {"title": "Group", "color": "#3a3f5a", "collapsed": False, "member_ids": [], "expanded_bbox": None},
    "logic/Script": {"script": "", "timeout": 30, "store_as": ""},
    "flow/Start": {},
    "flow/End":   {},
}


def _load_node_property_defaults() -> tuple[dict[str, dict[str, Any]], str]:
    """Try scraping designer.html; fall back to the hardcoded dict.

    Returns ``(defaults, source)`` where ``source`` is one of:

    * ``"scraped"``                      — parse succeeded and covers
      every SUPPORTED_NODE_TYPES entry
    * ``"scraped_partial_fallback"``     — parse succeeded but missed
      types; merged with hardcoded dict
    * ``"fallback_file_missing"``        — designer.html not at the
      expected path; pure hardcoded
    * ``"fallback_parse_error:<name>"``  — scraper raised; pure hardcoded
    """
    designer_html = Path(__file__).resolve().parents[3] / "frontend" / "designer.html"
    try:
        from pybravo.workflow.drafter.defaults_scraper import scrape_node_defaults
        scraped = scrape_node_defaults(designer_html)
    except FileNotFoundError:
        logger.warning("defaults_scrape_file_missing path=%s", designer_html)
        return dict(_NODE_PROPERTY_DEFAULTS_HARDCODED), "fallback_file_missing"
    except Exception as exc:
        logger.warning("defaults_scrape_failed err=%r", exc)
        return dict(_NODE_PROPERTY_DEFAULTS_HARDCODED), f"fallback_parse_error:{type(exc).__name__}"

    # Missing any expected types? Merge hardcoded in as fill-in.
    missing = set(_NODE_PROPERTY_DEFAULTS_HARDCODED) - set(scraped)
    if missing:
        logger.info("defaults_scrape_partial missing_types=%s", sorted(missing))
        merged = dict(_NODE_PROPERTY_DEFAULTS_HARDCODED)
        merged.update(scraped)      # scraped wins on overlap (source of truth)
        return merged, "scraped_partial_fallback"
    return scraped, "scraped"


_NODE_PROPERTY_DEFAULTS, _NODE_PROPERTY_DEFAULTS_SOURCE = _load_node_property_defaults()
logger.info(
    "defaults_loaded source=%s type_count=%d",
    _NODE_PROPERTY_DEFAULTS_SOURCE, len(_NODE_PROPERTY_DEFAULTS),
)


def node_property_defaults_info() -> dict[str, Any]:
    """Expose the loaded-defaults provenance for /api/drafter/status.

    Lets an operator see at a glance whether the diff is comparing
    against scraped-from-designer values (sync-by-construction) or a
    frozen hardcoded snapshot (may have drifted).
    """
    return {
        "source":     _NODE_PROPERTY_DEFAULTS_SOURCE,
        "type_count": len(_NODE_PROPERTY_DEFAULTS),
        "types":      sorted(_NODE_PROPERTY_DEFAULTS.keys()),
    }


def _normalize_link(link: Any) -> tuple[int, int, int, int, Any] | None:
    """LiteGraph links come either as dicts (from DraftedLink.model_dump)
    or 6-tuples (from LiteGraph's native serialization). Return a
    comparable tuple, or None if the shape isn't recognized.
    """
    if isinstance(link, (list, tuple)) and len(link) >= 6:
        # [link_id, origin_id, origin_slot, target_id, target_slot, type]
        return (int(link[1]), int(link[2]), int(link[3]), int(link[4]), link[5])
    if isinstance(link, dict):
        try:
            return (
                int(link["origin_id"]), int(link["origin_slot"]),
                int(link["target_id"]), int(link["target_slot"]),
                link.get("link_type", -1),
            )
        except (KeyError, TypeError, ValueError):
            return None
    return None


def _nodes_by_id(wf: dict[str, Any]) -> dict[int, dict[str, Any]]:
    graph = wf.get("graph") or {}
    return {int(n["id"]): n for n in (graph.get("nodes") or []) if "id" in n}


def _links_set(wf: dict[str, Any]) -> set[tuple[int, int, int, int, Any]]:
    graph = wf.get("graph") or {}
    out: set[tuple[int, int, int, int, Any]] = set()
    for raw in (graph.get("links") or []):
        t = _normalize_link(raw)
        if t is not None:
            out.add(t)
    return out


def _has_citation(node: dict[str, Any]) -> bool:
    props = node.get("properties") or {}
    return bool(props.get(_CITATION_KEY))


# Values that Python's equality considers distinct but which the
# designer / LLM / serializer treat as "the same non-value". The LLM
# often emits fields as null; the designer's node constructor then
# fills them in with the property's per-type zero (0 for numbers, ""
# for strings, false for bools, [] for lists, {} for dicts). That
# round-trip is noise, not a user edit — we fold it out here so the
# diff's edit_magnitude and cited_nodes_unchanged_pct metrics reflect
# real operator changes.
_EMPTY_VALUES = (None, "", 0, 0.0, False, [], {})


def _values_equivalent(old: Any, new: Any) -> bool:
    """Relaxed equality for property deltas.

    Equivalent if:
      * Python equality says so (``old == new``)
      * Both are "empty" values from a loose-type perspective
        (null ↔ 0 ↔ "" ↔ false ↔ [] ↔ {})
      * They're numerically equal across int/float (``150 == 150.0``)

    Otherwise the caller records a real delta.
    """
    if old == new:
        return True
    if old in _EMPTY_VALUES and new in _EMPTY_VALUES:
        return True
    try:
        if isinstance(old, (int, float)) and isinstance(new, (int, float)):
            return float(old) == float(new)
    except (TypeError, ValueError):
        pass
    return False


def _property_deltas(
    old_props: dict[str, Any],
    new_props: dict[str, Any],
    node_type: str = "",
) -> dict[str, dict[str, Any]]:
    """Top-level key-by-key comparison. Skips the citation sidecar (it's
    metadata, not a user-edited parameter) and folds out null↔default
    noise.

    Two equivalence rules stack:
      1. Loose scalar equivalence (null↔0/''/false, int↔float) via
         ``_values_equivalent``.
      2. If a node_type is provided and it has entries in
         ``_NODE_PROPERTY_DEFAULTS``, a pair
         ``(null, designer-default-for-that-key)`` is treated as
         equivalent. This covers the 'LLM emitted null, designer node
         constructor auto-filled its registered default' round-trip
         that was previously flagged as a real user edit.
    """
    deltas: dict[str, dict[str, Any]] = {}
    keys = set(old_props or {}) | set(new_props or {})
    type_defaults = _NODE_PROPERTY_DEFAULTS.get(node_type, {})
    for key in keys:
        if key == _CITATION_KEY:
            continue
        old_v = (old_props or {}).get(key)
        new_v = (new_props or {}).get(key)
        if _values_equivalent(old_v, new_v):
            continue
        # Designer-default fold: LLM didn't specify (null), designer
        # filled in the registered default on load.
        if old_v is None and key in type_defaults and _values_equivalent(new_v, type_defaults[key]):
            continue
        # And the reverse — rarer but matters when a saved workflow is
        # reloaded: designer emits default, diff-from-next-save sees
        # the same default, we also want that to be a no-op.
        if new_v is None and key in type_defaults and _values_equivalent(old_v, type_defaults[key]):
            continue
        deltas[key] = {"old": old_v, "new": new_v}
    return deltas


def _classify_magnitude(unchanged_pct: float, total_draft: int, total_final: int) -> str:
    """Bucket the diff for quick dashboard scans."""
    if total_draft == 0 and total_final == 0:
        return "empty"
    if unchanged_pct >= 99.0 and total_draft == total_final:
        return "none"
    if unchanged_pct >= 85.0:
        return "minor"
    if unchanged_pct >= 60.0:
        return "moderate"
    if unchanged_pct >= 30.0:
        return "heavy"
    return "rewrite"


def compute_workflow_diff(
    drafted: dict[str, Any],
    final: dict[str, Any],
) -> dict[str, Any]:
    """Return a structural diff of the two designer-JSON workflows.

    Both inputs are expected in the ``deserializeWorkflow`` shape
    (``{name, description, deck, graph: {nodes, links}, library, ...}``).
    Missing fields default to empty; the function never raises on
    partial data — it's defensive because drafts and saves come from
    different code paths.
    """
    draft_nodes = _nodes_by_id(drafted)
    final_nodes = _nodes_by_id(final)

    added_ids   = set(final_nodes) - set(draft_nodes)
    removed_ids = set(draft_nodes) - set(final_nodes)
    common_ids  = set(draft_nodes) & set(final_nodes)

    nodes_added: list[dict[str, Any]] = []
    for nid in sorted(added_ids):
        n = final_nodes[nid]
        nodes_added.append({
            "id": nid,
            "type": n.get("type", ""),
            "title": n.get("title", ""),
            "properties": {k: v for k, v in (n.get("properties") or {}).items() if k != _CITATION_KEY},
        })

    nodes_removed: list[dict[str, Any]] = []
    for nid in sorted(removed_ids):
        n = draft_nodes[nid]
        nodes_removed.append({
            "id": nid,
            "type": n.get("type", ""),
            "title": n.get("title", ""),
            "had_citation": _has_citation(n),
        })

    nodes_mutated: list[dict[str, Any]] = []
    cited_total  = 0
    cited_kept   = 0
    for nid in sorted(common_ids):
        d_node = draft_nodes[nid]
        f_node = final_nodes[nid]
        had_cit = _has_citation(d_node)
        if had_cit:
            cited_total += 1

        type_changed = d_node.get("type") != f_node.get("type")
        title_changed = d_node.get("title", "") != f_node.get("title", "")
        deltas = _property_deltas(
            d_node.get("properties") or {},
            f_node.get("properties") or {},
            node_type=str(d_node.get("type") or f_node.get("type") or ""),
        )
        if deltas or type_changed or title_changed:
            nodes_mutated.append({
                "id": nid,
                "type": d_node.get("type", ""),
                "type_changed_to": f_node.get("type") if type_changed else None,
                "title_changed_to": f_node.get("title") if title_changed else None,
                "property_deltas": deltas,
                "had_citation": had_cit,
            })
        else:
            if had_cit:
                cited_kept += 1

    # Links — compared as 5-tuples (dropping link_id) since LiteGraph
    # can renumber link ids on reload.
    draft_links = _links_set(drafted)
    final_links = _links_set(final)
    links_added_set   = final_links - draft_links
    links_removed_set = draft_links - final_links

    # Aggregate stats.
    total_draft  = len(draft_nodes)
    total_final  = len(final_nodes)
    changed_ids  = {m["id"] for m in nodes_mutated}
    touched_ids  = changed_ids | added_ids | removed_ids
    unchanged_ids = set(draft_nodes) - touched_ids
    unchanged_pct = (100.0 * len(unchanged_ids) / total_draft) if total_draft else 100.0
    cited_kept_pct = (100.0 * cited_kept / cited_total) if cited_total else 100.0

    # Top-level workflow metadata diffs (name, description, library).
    # Same equivalence rule as per-node properties so a drafted
    # description ''→LLM-value round-trip doesn't look like an edit.
    meta_deltas: dict[str, dict[str, Any]] = {}
    for key in ("name", "description", "library"):
        if not _values_equivalent(drafted.get(key, ""), final.get(key, "")):
            meta_deltas[key] = {"old": drafted.get(key, ""), "new": final.get(key, "")}

    # Deck-configuration diff — keyed by slot. We report what changed
    # rather than a full recursive object diff, because deck edits are
    # structurally meaningful (user moved a plate ⇒ protocol changed).
    deck_changes: dict[str, dict[str, Any]] = {}
    draft_deck = drafted.get("deck") or {}
    final_deck = final.get("deck") or {}
    for slot in set(draft_deck) | set(final_deck):
        if draft_deck.get(slot) != final_deck.get(slot):
            deck_changes[slot] = {
                "old": draft_deck.get(slot),
                "new": final_deck.get(slot),
            }

    return {
        "edits": {
            "nodes_added":   nodes_added,
            "nodes_removed": nodes_removed,
            "nodes_mutated": nodes_mutated,
            "links_added":   [list(t) for t in sorted(links_added_set)],
            "links_removed": [list(t) for t in sorted(links_removed_set)],
            "meta_deltas":   meta_deltas,
            "deck_changes":  deck_changes,
        },
        "summary": {
            "total_nodes_draft":    total_draft,
            "total_nodes_final":    total_final,
            "nodes_unchanged":      len(unchanged_ids),
            "nodes_unchanged_pct":  round(unchanged_pct, 1),
            "nodes_mutated":        len(nodes_mutated),
            "nodes_added":          len(nodes_added),
            "nodes_removed":        len(nodes_removed),
            "cited_nodes_total":    cited_total,
            "cited_nodes_unchanged": cited_kept,
            "cited_nodes_unchanged_pct": round(cited_kept_pct, 1),
            "links_added":          len(links_added_set),
            "links_removed":        len(links_removed_set),
            "edit_magnitude":       _classify_magnitude(unchanged_pct, total_draft, total_final),
        },
    }
