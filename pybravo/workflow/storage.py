"""
Workflow storage — JSON file persistence for workflow definitions.

Workflows are stored as individual JSON files in ~/.pybravo/workflows/.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

DEFAULT_WORKFLOWS_DIR = Path.home() / ".pybravo" / "workflows"


class WorkflowStorage:
    """CRUD operations for workflow JSON files on disk."""

    def __init__(self, directory: Path | str | None = None) -> None:
        self._dir = Path(directory) if directory else DEFAULT_WORKFLOWS_DIR
        self._dir.mkdir(parents=True, exist_ok=True)

    # ── List ──────────────────────────────────────────────────────────

    def list_workflows(self) -> list[dict[str, Any]]:
        """Return summary metadata for every saved workflow."""
        results = []
        for path in sorted(self._dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                results.append({
                    "id": data.get("id", path.stem),
                    "name": data.get("name", path.stem),
                    "description": data.get("description", ""),
                    "modified": data.get("modified", ""),
                    "created": data.get("created", ""),
                })
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Skipping malformed workflow file", path=str(path), error=str(exc))
        return results

    # ── Get ───────────────────────────────────────────────────────────

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        """Load a single workflow by ID."""
        path = self._resolve_path(workflow_id)
        if not path or not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    # ── Create ────────────────────────────────────────────────────────

    def create_workflow(self, data: dict[str, Any]) -> dict[str, Any]:
        """Create a new workflow.  Assigns an ID if not present."""
        if not data.get("id"):
            data["id"] = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        data.setdefault("created", now)
        data["modified"] = now
        self._write(data)
        logger.info("Workflow created", id=data["id"], name=data.get("name"))
        return data

    # ── Update ────────────────────────────────────────────────────────

    def update_workflow(self, workflow_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
        """Update an existing workflow by ID."""
        existing = self.get_workflow(workflow_id)
        if existing is None:
            return None
        existing.update(data)
        existing["id"] = workflow_id
        existing["modified"] = datetime.now(timezone.utc).isoformat()
        self._write(existing)
        logger.info("Workflow updated", id=workflow_id)
        return existing

    # ── Delete ────────────────────────────────────────────────────────

    def delete_workflow(self, workflow_id: str) -> bool:
        """Delete a workflow by ID.  Returns True if deleted."""
        path = self._resolve_path(workflow_id)
        if not path or not path.exists():
            return False
        path.unlink()
        logger.info("Workflow deleted", id=workflow_id)
        return True

    # ── Import / Export ───────────────────────────────────────────────

    def import_workflow(self, raw_json: str | bytes) -> dict[str, Any]:
        """Import a workflow from raw JSON content."""
        data = json.loads(raw_json)
        if not isinstance(data, dict):
            raise ValueError("Workflow JSON must be an object")
        # Assign a new ID on import to avoid collisions
        data["id"] = str(uuid.uuid4())
        data["modified"] = datetime.now(timezone.utc).isoformat()
        self._write(data)
        logger.info("Workflow imported", id=data["id"], name=data.get("name"))
        return data

    def export_workflow(self, workflow_id: str) -> str | None:
        """Export a workflow as a formatted JSON string."""
        data = self.get_workflow(workflow_id)
        if data is None:
            return None
        return json.dumps(data, indent=2, ensure_ascii=False)

    # ── Internal ──────────────────────────────────────────────────────

    def _resolve_path(self, workflow_id: str) -> Path | None:
        """Find the file for a given workflow ID."""
        # First try direct filename match
        direct = self._dir / f"{workflow_id}.json"
        if direct.exists():
            return direct
        # Search by ID inside files
        for path in self._dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("id") == workflow_id:
                    return path
            except (json.JSONDecodeError, OSError):
                continue
        return None

    def _write(self, data: dict[str, Any]) -> None:
        """Write workflow data to disk."""
        workflow_id = data["id"]
        # Use a safe filename derived from the ID
        safe_name = workflow_id.replace("/", "_").replace("\\", "_")
        path = self._dir / f"{safe_name}.json"
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
