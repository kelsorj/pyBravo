"""One-shot importer: drafter JSONL fallback → MongoDB.

Run this once after configuring Mongo for the first time to pull any
events that accumulated in ``~/.pybravo/drafter_data/*.jsonl`` while
the drafter was running without Mongo. Idempotent — re-running skips
rows that are already in Mongo.

Usage::

    python -m pybravo.workflow.drafter.migrate_jsonl
    python -m pybravo.workflow.drafter.migrate_jsonl --dry-run

The files processed:

    workflow_drafts.jsonl           — frozen as-drafted snapshots
    workflow_drafts_updates.jsonl   — diff patches from Save / Save-As / Execute
    protocol_selections.jsonl       — picker interactions / all_methods events

Each run reports counts and renames processed files to ``*.jsonl.imported``
so subsequent runs don't double-import. Rows that fail insertion are
logged at WARN level and left in a ``.failed.jsonl`` file for inspection.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from pybravo.workflow.drafter import store as _dstore

logger = logging.getLogger("drafter.migrate")


def _parse_datetime(value: Any) -> Any:
    """ISO-string → datetime, leave everything else alone."""
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    return value


def _rehydrate_dates(doc: dict[str, Any]) -> dict[str, Any]:
    """Walk the dict once and parse any ISO date-strings back into
    datetimes so Mongo stores them as real Date values (the rest of
    the app queries ranges on these fields)."""
    for key, value in list(doc.items()):
        if isinstance(value, str) and ("_at" in key or key in ("timestamp", "drafted_at", "final_saved_at")):
            doc[key] = _parse_datetime(value)
        elif isinstance(value, dict):
            _rehydrate_dates(value)
    return doc


def _process_file(
    path: Path,
    insert_fn: Callable[[dict[str, Any]], bool],
    *,
    dry_run: bool,
) -> tuple[int, int, int]:
    """Stream one JSONL file through ``insert_fn``. Returns
    (total_rows, inserted, skipped_or_failed)."""
    total   = 0
    ok      = 0
    failed  = 0
    failed_lines: list[str] = []

    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        total += 1
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("jsonl_parse_failed line=%d err=%s", total, exc)
            failed += 1
            failed_lines.append(raw)
            continue
        doc = _rehydrate_dates(doc)

        if dry_run:
            ok += 1
            continue
        try:
            accepted = insert_fn(doc)
        except Exception as exc:
            logger.warning("insert_failed err=%s doc_preview=%s", exc, raw[:120])
            failed += 1
            failed_lines.append(raw)
            continue
        if accepted:
            ok += 1
        else:
            # Treat "already present" as success — idempotency is the goal.
            ok += 1

    # Quarantine failed rows for manual review.
    if failed_lines:
        fail_path = path.with_suffix(".failed.jsonl")
        with fail_path.open("a", encoding="utf-8") as fh:
            for line in failed_lines:
                fh.write(line + "\n")
        logger.warning("jsonl_rows_quarantined path=%s count=%d", fail_path, len(failed_lines))

    return total, ok, failed


# ── Insert handlers keyed to specific collections ────────────────────


def _insert_draft(doc: dict[str, Any]) -> bool:
    coll = _dstore._collection("workflow_drafts")   # noqa: SLF001
    if coll is None:
        raise RuntimeError("workflow_drafts collection unavailable")
    sid = doc.get("session_id")
    if not sid:
        return False
    # setOnInsert so repeat rows (same session_id) don't overwrite
    # a later Save patch that has final_workflow/diff populated.
    coll.update_one({"session_id": sid}, {"$setOnInsert": doc}, upsert=True)
    return True


def _insert_draft_update(doc: dict[str, Any]) -> bool:
    coll = _dstore._collection("workflow_drafts")   # noqa: SLF001
    if coll is None:
        raise RuntimeError("workflow_drafts collection unavailable")
    sid = doc.get("session_id")
    if not sid:
        return False
    patch = {
        "final_workflow": doc.get("final_workflow"),
        "final_saved_at": doc.get("updated_at") or datetime.utcnow(),
        "diff": doc.get("diff"),
        "final_outcome.last_trigger": doc.get("trigger"),
    }
    if doc.get("workflow_id_saved_as"):
        patch["final_outcome.workflow_id_saved_as"] = doc["workflow_id_saved_as"]
    coll.update_one({"session_id": sid}, {"$set": patch})
    return True


def _insert_selection(doc: dict[str, Any]) -> bool:
    coll = _dstore._collection("protocol_selections")  # noqa: SLF001
    if coll is None:
        raise RuntimeError("protocol_selections collection unavailable")
    # Dedup key — selections aren't unique so we accept duplicates only
    # when the same (session_id, timestamp) combo is already present.
    sid = doc.get("session_id")
    ts  = doc.get("timestamp")
    if sid and ts:
        existing = coll.count_documents({"session_id": sid, "timestamp": ts}, limit=1)
        if existing:
            return True
    coll.insert_one(doc)
    return True


_HANDLERS: dict[str, Callable[[dict[str, Any]], bool]] = {
    "workflow_drafts.jsonl":          _insert_draft,
    "workflow_drafts_updates.jsonl":  _insert_draft_update,
    "protocol_selections.jsonl":      _insert_selection,
}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Parse and report counts but don't write to Mongo.")
    ap.add_argument("--dir", default=None,
                    help="Override the local-store directory (default: "
                         "$PYBRAVO_DRAFTER_LOCAL_STORE or ~/.pybravo/drafter_data).")
    args = ap.parse_args()

    root = Path(args.dir).expanduser() if args.dir else _dstore._local_store_dir()  # noqa: SLF001
    if not root.exists():
        print(f"Nothing to migrate — {root} does not exist.")
        return 0

    if not args.dry_run:
        status = _dstore.status()
        if not status.get("mongo_reachable"):
            print("Mongo is not reachable. Set PYBRAVO_DRAFTER_MONGO_URI + "
                  "PYBRAVO_DRAFTER_MONGO_DB in .env and retry.")
            print(f"  mongo_configured: {status.get('mongo_configured')}")
            print(f"  mongo_db:         {status.get('mongo_db')}")
            return 2

    print(f"Source dir: {root}")
    grand_total = grand_ok = grand_failed = 0
    for filename, handler in _HANDLERS.items():
        path = root / filename
        if not path.exists():
            continue
        print(f"\n→ {filename}")
        total, ok, failed = _process_file(path, handler, dry_run=args.dry_run)
        grand_total += total
        grand_ok    += ok
        grand_failed += failed
        print(f"  rows: {total}  imported: {ok}  failed: {failed}")
        if not args.dry_run and ok > 0 and failed == 0:
            # Rename so repeated runs don't re-import. Keep the file
            # under a timestamped suffix for audit trail.
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            archived = path.with_suffix(f".jsonl.imported-{stamp}")
            path.rename(archived)
            print(f"  archived → {archived.name}")

    print(f"\nTOTAL: rows={grand_total} imported={grand_ok} failed={grand_failed}"
          + ("  (dry-run)" if args.dry_run else ""))
    return 0 if grand_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
