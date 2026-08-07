"""Persistence layer for the PDF-ingest drafter.

Two storage backends live here:

1. **Filesystem PDF cache** — content-addressed by SHA-256. PDFs land
   at ``~/.pybravo/papers/<sha256>.pdf``. Same file uploaded twice ⇒
   one blob on disk (dedup for free). No config needed; runs even when
   MongoDB isn't available.

2. **MongoDB drafter collections** — cached Docling output, drafted
   workflows, protocol selections, structural diffs. Uses the same
   ``pymongo`` driver the labware / liquid-classes stores already use;
   connection config is separate so you can point the drafter at a
   different DB if needed.

Collections (all in one database):

* ``parsed_papers``      — Docling output, keyed by pdf_hash
* ``protocol_candidates`` — Pass 0 segmenter output (when that lands)
* ``workflow_drafts``    — drafted workflow + diff against the final
                           saved version, keyed by drafter session_id
* ``protocol_selections`` — picker-modal interactions (when the picker
                           lands); for now we seed a single entry per
                           PDF draft with ``user_action: "all_methods"``
                           so analytics work uniformly before/after the
                           picker ships.

The store degrades gracefully when Mongo is unavailable: inserts are
no-ops and logged at INFO level. The file-cache path still works.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import uuid
from collections import OrderedDict
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from pymongo import MongoClient
    from pymongo.collection import Collection
except ImportError:  # pragma: no cover - optional
    MongoClient = None  # type: ignore[assignment]
    Collection = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


# ── Config ────────────────────────────────────────────────────────────

DEFAULT_DATA_DIR: Path = Path.home() / ".pybravo"
DEFAULT_PDF_DIR: Path  = DEFAULT_DATA_DIR / "papers"
DEFAULT_DB_NAME: str   = "pybravo_drafter"

# Fallback local-JSONL store used when Mongo isn't configured. Not a
# perfect substitute but means training data isn't dropped on the floor
# during development / evaluation runs.
DEFAULT_LOCAL_STORE_DIR: Path = DEFAULT_DATA_DIR / "drafter_data"


def _mongo_uri() -> str:
    """Drafter's own URI takes precedence; fall back to the labware
    URI if set (most deployments run a single MongoDB instance)."""
    uri = os.environ.get("PYBRAVO_DRAFTER_MONGO_URI", "").strip()
    if uri:
        return uri
    return os.environ.get("PYBRAVO_LABWARE_MONGO_URI", "").strip()


def _mongo_db_name() -> str:
    return os.environ.get("PYBRAVO_DRAFTER_MONGO_DB", DEFAULT_DB_NAME).strip() or DEFAULT_DB_NAME


def _pdf_dir() -> Path:
    configured = os.environ.get("PYBRAVO_DRAFTER_PDF_DIR", "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_PDF_DIR


def _local_store_dir() -> Path:
    configured = os.environ.get("PYBRAVO_DRAFTER_LOCAL_STORE", "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_LOCAL_STORE_DIR


# ── Mongo factory ─────────────────────────────────────────────────────

_client_lock = threading.Lock()
_client: "MongoClient | None" = None  # type: ignore[name-defined]
_indexes_ensured: bool = False


def _ensure_indexes(db) -> None:  # type: ignore[no-untyped-def]
    """Idempotent: create the drafter's required indexes on first
    client acquisition. Safe to call repeatedly — Mongo turns a
    duplicate-index request into a no-op.

    These indexes are what enforce our dedup invariants at the DB
    level (so concurrent uploads can't write two parsed_papers rows
    for the same pdf_hash) and what make the dashboard queries fast.
    """
    global _indexes_ensured
    if _indexes_ensured:
        return
    try:
        db["parsed_papers"].create_index("pdf_hash", unique=True)
        db["protocol_candidates"].create_index("pdf_hash", unique=True)
        db["workflow_drafts"].create_index("session_id", unique=True)
        db["workflow_drafts"].create_index("pdf_hash")           # many per paper
        db["workflow_drafts"].create_index("drafted_at")         # time-series
        db["protocol_selections"].create_index("pdf_hash")
        db["protocol_selections"].create_index("session_id")
        db["protocol_selections"].create_index("timestamp")
        _indexes_ensured = True
    except Exception as exc:
        # Non-fatal — the collections still work without indexes, just
        # slower. Log so an operator notices.
        logger.warning("drafter_index_create_failed", exc_info=exc)


def _get_client() -> "MongoClient | None":  # type: ignore[name-defined]
    """Lazy singleton MongoClient. None when Mongo isn't configured."""
    global _client
    if MongoClient is None:
        return None
    uri = _mongo_uri()
    if not uri:
        return None
    with _client_lock:
        if _client is None:
            try:
                _client = MongoClient(uri, serverSelectionTimeoutMS=3000)
                # Ping to fail fast when Mongo is unreachable; the
                # caller can then fall back to local JSONL.
                _client.admin.command("ping")
                _ensure_indexes(_client[_mongo_db_name()])
            except Exception as exc:
                logger.info("drafter_mongo_unreachable", exc_info=exc)
                _client = None
    return _client


def _collection(name: str) -> "Collection | None":  # type: ignore[name-defined]
    client = _get_client()
    if client is None:
        return None
    return client[_mongo_db_name()][name]


# ── JSONL fallback ────────────────────────────────────────────────────


def _jsonl_append(filename: str, doc: dict[str, Any]) -> None:
    """Append one document to the local-store JSONL fallback.

    Used when MongoDB isn't available. We serialize ISO-format datetimes
    so the file round-trips through json.loads for later migration into
    Mongo via a one-shot script.
    """
    import json
    root = _local_store_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{filename}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(doc, default=_json_default, ensure_ascii=False) + "\n")


def _json_default(o: Any) -> Any:
    if isinstance(o, datetime):
        return o.isoformat()
    if is_dataclass(o):
        return asdict(o)
    if hasattr(o, "model_dump"):
        return o.model_dump()
    return str(o)


# ── PDF filesystem cache ──────────────────────────────────────────────


def hash_pdf_bytes(pdf_bytes: bytes) -> str:
    """SHA-256 of the PDF payload, hex-encoded. This is the key everything
    else in the drafter joins on."""
    return hashlib.sha256(pdf_bytes).hexdigest()


def pdf_path_for(pdf_hash: str) -> Path:
    """Return the filesystem path the PDF would live at (whether or
    not it's actually there)."""
    return _pdf_dir() / f"{pdf_hash}.pdf"


def store_pdf_bytes(pdf_bytes: bytes) -> str:
    """Write the PDF to the content-addressed cache if it's not already
    there. Returns the hash. Safe to call repeatedly.
    """
    h = hash_pdf_bytes(pdf_bytes)
    target = pdf_path_for(h)
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write via a temp file to avoid half-written PDFs on crash.
        tmp = target.with_suffix(".pdf.tmp")
        tmp.write_bytes(pdf_bytes)
        tmp.replace(target)
        logger.info("drafter_pdf_stored", hash=h, bytes=len(pdf_bytes))
    return h


def read_pdf_bytes(pdf_hash: str) -> bytes | None:
    """Fetch PDF bytes by hash, if cached. None otherwise."""
    p = pdf_path_for(pdf_hash)
    if not p.exists():
        return None
    return p.read_bytes()


# ── parsed_papers cache ───────────────────────────────────────────────


# In-process LRU cache for parsed papers. Survives across HTTP
# requests in the same server process, bounded to a handful of recent
# papers (Docling output can be multi-megabyte). This is the primary
# read path — Mongo is a write-through secondary for cross-process
# sharing / long-term persistence.
#
# Why not trust Mongo alone: (a) dev machines may not have Mongo
# configured, (b) the picker's segment_paper -> draft_from_analyzed
# handoff needs the parsed paper to still be available a few seconds
# after parsing, regardless of durability, (c) in-memory hits are
# free, Mongo isn't.
_PARSED_CACHE: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
_PARSED_CACHE_MAX: int = 16
_parsed_cache_lock = threading.Lock()


def _cache_put(pdf_hash: str, doc: dict[str, Any]) -> None:
    with _parsed_cache_lock:
        _PARSED_CACHE[pdf_hash] = doc
        _PARSED_CACHE.move_to_end(pdf_hash)
        while len(_PARSED_CACHE) > _PARSED_CACHE_MAX:
            _PARSED_CACHE.popitem(last=False)


def _cache_get(pdf_hash: str) -> dict[str, Any] | None:
    with _parsed_cache_lock:
        doc = _PARSED_CACHE.get(pdf_hash)
        if doc is not None:
            _PARSED_CACHE.move_to_end(pdf_hash)
        return doc


def _mongo_doc_from_lru(pdf_hash: str, full_doc: dict[str, Any]) -> dict[str, Any]:
    """Shape the in-memory doc for durable Mongo storage.

    We intentionally DROP ``raw_document`` from the Mongo copy: it's
    Docling's full layout JSON, multi-megabyte, not read anywhere
    downstream (see grep for uses — only ``raw_document.get('pages')``
    for page-count inference, and we store page_count as its own
    field). The LRU keeps the full-fidelity doc for the in-process
    segment_paper → draft_from_analyzed handoff.

    Returns a shallow copy — safe to further truncate if the BSON
    encoder still complains.
    """
    out = {k: v for k, v in full_doc.items() if k != "raw_document"}
    out["raw_document_dropped"] = True
    return out


def _upsert_parsed_paper_to_mongo(
    coll: Any,
    pdf_hash: str,
    lru_doc: dict[str, Any],
) -> None:
    """Idempotent upsert with progressive fallback.

    Tries increasingly-small variants of the doc so a PDF too big for
    the full markdown still gets *something* (paragraphs + metadata)
    into Mongo. Catches a broad exception set rather than just
    DocumentTooLarge because pymongo versions differ on which class
    they raise for oversize / invalid docs (DocumentTooLarge,
    InvalidDocument, OperationFailure, WriteError).
    """
    # Always drop raw_document — that's the single biggest win.
    doc_no_raw = _mongo_doc_from_lru(pdf_hash, lru_doc)
    doc_no_md  = {k: v for k, v in doc_no_raw.items() if k != "markdown"}
    doc_no_md["markdown_dropped"] = True

    attempts = [
        ("paragraphs+markdown", doc_no_raw),
        ("paragraphs_only",     doc_no_md),
    ]
    for i, (label, doc) in enumerate(attempts):
        try:
            coll.update_one({"pdf_hash": pdf_hash}, {"$set": doc}, upsert=True)
            if label != "paragraphs+markdown":
                logger.warning(
                    "drafter_parsed_paper_truncated_to_fit hash=%s mode=%s",
                    pdf_hash, label,
                )
            return
        except Exception as exc:  # noqa: BLE001 — see docstring
            is_last = (i == len(attempts) - 1)
            if is_last:
                logger.error(
                    "drafter_parsed_paper_write_failed hash=%s err=%r",
                    pdf_hash, exc,
                )
            else:
                logger.warning(
                    "drafter_parsed_paper_write_retrying hash=%s mode=%s err=%r",
                    pdf_hash, label, exc,
                )


def _ensure_parsed_paper_in_mongo(pdf_hash: str, lru_doc: dict[str, Any]) -> None:
    """Write-through helper — run on LRU hit when Mongo may not yet
    have the paper.

    Cheap existence check first so we don't upsert on every single
    picker-open for the same paper. Silently no-ops when Mongo isn't
    available (the LRU path works fine on its own)."""
    coll = _collection("parsed_papers")
    if coll is None:
        return
    try:
        if coll.count_documents({"pdf_hash": pdf_hash}, limit=1) > 0:
            return
    except Exception as exc:  # noqa: BLE001
        logger.warning("drafter_parsed_paper_existence_check_failed exc=%r", exc)
        return
    _upsert_parsed_paper_to_mongo(coll, pdf_hash, lru_doc)


def get_parsed_paper(pdf_hash: str) -> dict[str, Any] | None:
    """Return the cached ParsedPaper as a dict, or None on miss.

    Checks the in-process LRU first (works with or without Mongo),
    then falls through to the parsed_papers collection. When Mongo
    has it but memory doesn't, the result is promoted into the LRU
    for faster subsequent lookups.

    Crucially: when the LRU has it but Mongo DOESN'T (e.g. the paper
    was parsed before Mongo was configured, or an earlier write
    failed), we write it through now. This is what makes the
    'configure Mongo after the fact' path actually populate the
    collection instead of staying silently empty.
    """
    hit = _cache_get(pdf_hash)
    if hit is not None:
        # Opportunistic write-through: repair any Mongo cache miss.
        _ensure_parsed_paper_in_mongo(pdf_hash, hit)
        return hit
    coll = _collection("parsed_papers")
    if coll is None:
        return None
    doc = coll.find_one({"pdf_hash": pdf_hash})
    if not doc:
        return None
    doc.pop("_id", None)
    _cache_put(pdf_hash, doc)
    return doc


def put_parsed_paper(
    pdf_hash: str,
    *,
    source_file: str,
    page_count: int,
    markdown: str,
    paragraphs: list[dict[str, Any]],
    raw_document: dict[str, Any],
) -> None:
    """Write-through: always populates the in-process LRU cache;
    additionally upserts to Mongo when configured. This keeps the
    segment_paper -> draft_from_analyzed handoff working on dev
    machines without Mongo and lets the same call durably persist
    when Mongo is available.

    The Mongo copy never carries ``raw_document`` (Docling's layout
    JSON, not used downstream, dominant contributor to BSON size).
    If even the slim copy can't fit, we further drop ``markdown`` and
    retry. The LRU copy always has the full fidelity.
    """
    full_doc = {
        "pdf_hash": pdf_hash,
        "source_file": source_file,
        "page_count": page_count,
        "parsed_at": datetime.now(timezone.utc),
        "markdown": markdown,
        "paragraphs": paragraphs,
        "raw_document": raw_document,
        "schema_version": 1,
    }
    _cache_put(pdf_hash, full_doc)

    coll = _collection("parsed_papers")
    if coll is None:
        logger.info("drafter_parsed_paper_in_memory_only hash=%s", pdf_hash)
        return

    _upsert_parsed_paper_to_mongo(coll, pdf_hash, full_doc)


# ── workflow_drafts — the draft + diff collection ─────────────────────


def new_session_id() -> str:
    """Fresh drafter session id. Uses uuid4 hex for URL-safety."""
    return uuid.uuid4().hex


def record_draft(
    *,
    session_id: str,
    pdf_hash: str | None,
    source_file: str,
    drafted_workflow: dict[str, Any],
    provider: str,
    model: str,
    attempts: int,
    prompt: str | None = None,
    selected_paragraph_ids: list[str] | None = None,
    warnings: list[str] | None = None,
    errors: list[str] | None = None,
) -> None:
    """Persist the frozen as-drafted workflow snapshot for this session.

    Called at draft time, before the user has a chance to edit. The
    ``drafted_workflow`` field is immutable for the lifetime of the
    session — later saves patch ``final_workflow`` + ``diff``.
    """
    doc = {
        "session_id": session_id,
        "pdf_hash": pdf_hash or "",
        "source_file": source_file or "",
        "drafted_at": datetime.now(timezone.utc),
        "drafted_workflow": drafted_workflow,
        "provider": provider,
        "model": model,
        "attempts": attempts,
        "prompt": prompt or "",
        "selected_paragraph_ids": selected_paragraph_ids or [],
        "draft_warnings": warnings or [],
        "draft_errors": errors or [],
        "final_workflow": None,
        "final_saved_at": None,
        "diff": None,
        "final_outcome": {},
        "schema_version": 1,
    }
    coll = _collection("workflow_drafts")
    if coll is None:
        _jsonl_append("workflow_drafts", doc)
        return
    try:
        coll.update_one({"session_id": session_id}, {"$setOnInsert": doc}, upsert=True)
    except Exception as exc:
        logger.warning("drafter_record_draft_failed", exc_info=exc)
        _jsonl_append("workflow_drafts", doc)


def update_draft_final(
    *,
    session_id: str,
    final_workflow: dict[str, Any],
    diff: dict[str, Any],
    trigger: str,
    workflow_id_saved_as: str | None = None,
) -> None:
    """Update the draft's final snapshot + diff on Save / Save-As /
    Execute / Simulate. No-op if Mongo is unavailable (we still append
    to local JSONL so the data isn't dropped)."""
    patch = {
        "final_workflow": final_workflow,
        "final_saved_at": datetime.now(timezone.utc),
        "diff": diff,
        "final_outcome.last_trigger": trigger,
    }
    if workflow_id_saved_as:
        patch["final_outcome.workflow_id_saved_as"] = workflow_id_saved_as
    coll = _collection("workflow_drafts")
    if coll is None:
        _jsonl_append("workflow_drafts_updates", {
            "session_id": session_id, "trigger": trigger, "workflow_id_saved_as": workflow_id_saved_as,
            "final_workflow": final_workflow, "diff": diff,
            "updated_at": datetime.now(timezone.utc),
        })
        return
    try:
        coll.update_one({"session_id": session_id}, {"$set": patch})
    except Exception as exc:
        logger.warning("drafter_update_draft_failed", exc_info=exc)


def get_draft(session_id: str) -> dict[str, Any] | None:
    coll = _collection("workflow_drafts")
    if coll is None:
        return None
    doc = coll.find_one({"session_id": session_id})
    if doc:
        doc.pop("_id", None)
    return doc


def debug_snapshot() -> dict[str, Any]:
    """Show what's actually in each drafter collection.

    Returned by /api/drafter/debug and meant for manual diagnosis —
    when the dashboard looks empty, this tells you whether writes
    landed at all and, if so, what the last row looks like.

    Keys per collection:
      * count           — total documents
      * last            — the most-recent doc (trimmed to avoid dumping
                          megabytes of markdown / raw_document)
      * indexes         — index names reported by list_indexes()
    """
    snap: dict[str, Any] = {
        "status":   status(),
        "collections": {},
    }

    def _dump_last(doc: dict[str, Any] | None) -> dict[str, Any] | None:
        if not doc:
            return None
        out = {}
        TRUNCABLE = {"markdown", "raw_document", "drafted_workflow", "final_workflow"}
        for k, v in doc.items():
            if k == "_id":
                out[k] = str(v)
            elif k in TRUNCABLE and isinstance(v, (str, dict, list)):
                if isinstance(v, str):
                    out[k] = f"<str len={len(v)}>"
                else:
                    out[k] = f"<{type(v).__name__} len={len(v)}>"
            elif isinstance(v, datetime):
                out[k] = v.isoformat()
            else:
                out[k] = v
        return out

    for name, sort_field in (
        ("parsed_papers",        "parsed_at"),
        ("protocol_candidates",  "generated_at"),
        ("workflow_drafts",      "drafted_at"),
        ("protocol_selections",  "timestamp"),
    ):
        coll = _collection(name)
        if coll is None:
            snap["collections"][name] = {"error": "collection unavailable (mongo not configured/reachable)"}
            continue
        try:
            count = coll.count_documents({})
            last = None
            try:
                last = coll.find_one({}, sort=[(sort_field, -1)])
            except Exception:
                last = coll.find_one({})
            snap["collections"][name] = {
                "count":  count,
                "last":   _dump_last(last),
                "indexes": [ix["name"] for ix in coll.list_indexes()],
            }
        except Exception as exc:  # noqa: BLE001
            snap["collections"][name] = {"error": repr(exc)}
    return snap


def dashboard_aggregates(days: int = 30) -> dict[str, Any]:
    """Aggregations used by /drafter-dashboard.

    Cheap Mongo queries that produce the five charts + two tables the
    dashboard HTML renders. Returns ``{}`` when Mongo is unavailable
    so the dashboard can render a "not configured" banner instead of
    erroring.

    * drafts_per_day     [{date, count}]          time-series
    * edit_magnitude     [{bucket, count}]        categorical
    * top_mutated_props  [{key, count}]           top 15 property names
                                                  the user changed
    * citation_retention [{bucket, count}]        histogram of
                                                  cited_nodes_unchanged_pct
    * picker_rank_picked [{rank, count}]          which rank did the
                                                  user pick, per
                                                  protocol_selections.rank_shown
    * top_papers         [{pdf_hash, source_file, draft_count}]
    * provider_attempts  [{provider, model, avg_attempts, drafts}]
    """
    drafts = _collection("workflow_drafts")
    sels   = _collection("protocol_selections")
    if drafts is None or sels is None:
        return {}

    from datetime import timedelta
    since = datetime.now(timezone.utc) - timedelta(days=days)
    out: dict[str, Any] = {"days": days, "since": since.isoformat()}

    try:
        # Drafts per day
        out["drafts_per_day"] = list(drafts.aggregate([
            {"$match": {"drafted_at": {"$gte": since}}},
            {"$group": {
                "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$drafted_at"}},
                "count": {"$sum": 1},
            }},
            {"$sort": {"_id": 1}},
            {"$project": {"date": "$_id", "count": 1, "_id": 0}},
        ]))

        # Edit magnitude distribution — only count drafts that have
        # been saved at least once (final_workflow is populated).
        out["edit_magnitude"] = list(drafts.aggregate([
            {"$match": {"final_workflow": {"$ne": None}}},
            {"$group": {"_id": "$diff.summary.edit_magnitude", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
            {"$project": {"bucket": "$_id", "count": 1, "_id": 0}},
        ]))

        # Top mutated property names.
        out["top_mutated_props"] = list(drafts.aggregate([
            {"$match": {"diff.edits.nodes_mutated": {"$exists": True, "$ne": []}}},
            {"$unwind": "$diff.edits.nodes_mutated"},
            {"$project": {"keys": {"$objectToArray": "$diff.edits.nodes_mutated.property_deltas"}}},
            {"$unwind": "$keys"},
            {"$group": {"_id": "$keys.k", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
            {"$limit": 15},
            {"$project": {"key": "$_id", "count": 1, "_id": 0}},
        ]))

        # Citation retention histogram — bucket by 10%.
        out["citation_retention"] = list(drafts.aggregate([
            {"$match": {"diff.summary.cited_nodes_total": {"$gt": 0}}},
            {"$bucket": {
                "groupBy": "$diff.summary.cited_nodes_unchanged_pct",
                "boundaries": [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100.1],
                "default": "other",
                "output": {"count": {"$sum": 1}},
            }},
        ]))

        # Picker: which rank_shown did the user pick?
        out["picker_rank_picked"] = list(sels.aggregate([
            {"$match": {"user_action": "picker_pick", "selected_candidate_idx": {"$ne": None}}},
            {"$group": {"_id": "$selected_candidate_idx", "count": {"$sum": 1}}},
            {"$sort": {"_id": 1}},
            {"$project": {"rank": "$_id", "count": 1, "_id": 0}},
        ]))

        # Papers with the most drafts — useful for spotting ones the
        # operator is iterating on.
        out["top_papers"] = list(drafts.aggregate([
            {"$match": {"pdf_hash": {"$ne": ""}}},
            {"$group": {
                "_id": "$pdf_hash",
                "source_file": {"$first": "$source_file"},
                "draft_count": {"$sum": 1},
                "last_drafted": {"$max": "$drafted_at"},
            }},
            {"$sort": {"draft_count": -1, "last_drafted": -1}},
            {"$limit": 20},
            {"$project": {
                "pdf_hash":   "$_id",
                "source_file": 1,
                "draft_count": 1,
                "last_drafted": 1,
                "_id": 0,
            }},
        ]))

        # Provider x Model breakdown
        out["provider_attempts"] = list(drafts.aggregate([
            {"$group": {
                "_id": {"provider": "$provider", "model": "$model"},
                "drafts": {"$sum": 1},
                "avg_attempts": {"$avg": "$attempts"},
            }},
            {"$sort": {"drafts": -1}},
            {"$project": {
                "provider": "$_id.provider",
                "model":    "$_id.model",
                "drafts":   1,
                "avg_attempts": {"$round": ["$avg_attempts", 2]},
                "_id": 0,
            }},
        ]))

        # Simple summary stats.
        total_drafts = drafts.count_documents({})
        total_saved  = drafts.count_documents({"final_workflow": {"$ne": None}})
        unique_papers = len(drafts.distinct("pdf_hash", {"pdf_hash": {"$ne": ""}}))
        out["summary"] = {
            "total_drafts":  total_drafts,
            "total_saved":   total_saved,
            "saved_pct":     round(100.0 * total_saved / total_drafts, 1) if total_drafts else 0.0,
            "unique_papers": unique_papers,
        }
    except Exception as exc:
        logger.warning("drafter_dashboard_aggregate_failed", exc_info=exc)

    # Mongo returns dates as datetime; the dashboard JSON-serializes,
    # so convert to ISO strings.
    for row in out.get("top_papers", []):
        if isinstance(row.get("last_drafted"), datetime):
            row["last_drafted"] = row["last_drafted"].isoformat()

    return out


def paper_upload_history(pdf_hash: str) -> dict[str, Any]:
    """Summary of previous activity on the given PDF hash.

    Returned by the draft endpoint so the UI can surface
    "you've drafted this paper N times before". Cheap query — reads
    at most the last 10 drafts and counts the rest.
    """
    info: dict[str, Any] = {
        "pdf_hash": pdf_hash,
        "previously_parsed": False,
        "previous_drafts_count": 0,
        "previous_drafts": [],
    }
    if not pdf_hash:
        return info

    papers = _collection("parsed_papers")
    if papers is not None:
        try:
            info["previously_parsed"] = papers.count_documents({"pdf_hash": pdf_hash}, limit=1) > 0
        except Exception as exc:
            logger.warning("drafter_paper_history_lookup_failed", exc_info=exc)

    drafts = _collection("workflow_drafts")
    if drafts is not None:
        try:
            info["previous_drafts_count"] = drafts.count_documents({"pdf_hash": pdf_hash})
            cursor = drafts.find(
                {"pdf_hash": pdf_hash},
                {"session_id": 1, "drafted_at": 1, "provider": 1, "model": 1,
                 "diff.summary.edit_magnitude": 1, "final_outcome": 1},
            ).sort("drafted_at", -1).limit(10)
            for d in cursor:
                d.pop("_id", None)
                info["previous_drafts"].append({
                    "session_id":     d.get("session_id"),
                    "drafted_at":     d.get("drafted_at").isoformat() if d.get("drafted_at") else None,
                    "provider":       d.get("provider"),
                    "model":          d.get("model"),
                    "edit_magnitude": (d.get("diff", {}).get("summary", {}) or {}).get("edit_magnitude"),
                    "outcome":        d.get("final_outcome") or {},
                })
        except Exception as exc:
            logger.warning("drafter_paper_history_lookup_failed", exc_info=exc)
    return info


# ── protocol_selections — picker-modal training data ─────────────────


def record_protocol_selection(
    *,
    session_id: str,
    pdf_hash: str,
    source_file: str,
    candidates_presented: list[dict[str, Any]],
    selected_candidate_idx: int | None,
    selected_paragraph_ids_final: list[str],
    user_action: str,
    manual_paragraph_adds: list[str] | None = None,
    manual_paragraph_removes: list[str] | None = None,
    time_on_picker_s: float | None = None,
) -> None:
    """Record one picker interaction.

    For drafts that didn't go through a picker yet (current PDF-draft
    flow, or NL drafts), call this with ``user_action="all_methods"``
    and an empty ``candidates_presented`` list so downstream analytics
    get a uniform event stream.
    """
    doc = {
        "session_id": session_id,
        "pdf_hash": pdf_hash or "",
        "source_file": source_file or "",
        "timestamp": datetime.now(timezone.utc),
        "candidates_presented": candidates_presented or [],
        "selected_candidate_idx": selected_candidate_idx,
        "selected_paragraph_ids_final": selected_paragraph_ids_final or [],
        "user_action": user_action,
        "manual_paragraph_adds": manual_paragraph_adds or [],
        "manual_paragraph_removes": manual_paragraph_removes or [],
        "time_on_picker_s": time_on_picker_s,
        "schema_version": 1,
    }
    coll = _collection("protocol_selections")
    if coll is None:
        _jsonl_append("protocol_selections", doc)
        return
    try:
        coll.insert_one(doc)
    except Exception as exc:
        logger.warning("drafter_protocol_selection_failed", exc_info=exc)
        _jsonl_append("protocol_selections", doc)


# ── status for health/diagnostic endpoints ───────────────────────────


def status() -> dict[str, Any]:
    """Snapshot of what's configured and reachable. Surfaced by a
    future ``/api/drafter/status`` endpoint; also useful for logs."""
    # Lazy import of diff so the status endpoint reports which
    # node-defaults source is active (scraped vs hardcoded fallback).
    try:
        from pybravo.workflow.drafter.diff import node_property_defaults_info
        defaults_info = node_property_defaults_info()
    except Exception as exc:  # noqa: BLE001
        defaults_info = {"error": repr(exc)}

    return {
        "mongo_configured": bool(_mongo_uri()),
        "mongo_reachable":  _get_client() is not None,
        "mongo_db":         _mongo_db_name(),
        "pdf_cache_dir":    str(_pdf_dir()),
        "pdf_count":        sum(1 for _ in _pdf_dir().glob("*.pdf")) if _pdf_dir().exists() else 0,
        "local_store_dir":  str(_local_store_dir()),
        "parsed_papers_in_memory": len(_PARSED_CACHE),
        "node_property_defaults": defaults_info,
    }
