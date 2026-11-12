"""Atomic local protocol records, revision history, reusable setups and releases."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def workflow_digest(workflow: dict) -> str:
    """Ignore canvas layout, but bind all executable properties and flow links."""
    graph = workflow.get("graph") or {}
    nodes = [{"id": n.get("id"), "type": n.get("type"), "properties": n.get("properties", {}),
              "inputs": [slot.get("link") for slot in n.get("inputs", [])],
              "outputs": [slot.get("links") for slot in n.get("outputs", [])]}
             for n in graph.get("nodes", [])]
    return digest({"deck": workflow.get("deck", {}), "nodes": sorted(nodes, key=lambda n: n["id"]),
                   "links": sorted(graph.get("links", []), key=lambda link: link[0]),
                   "library": workflow.get("library", "") or ""})


def record_digest(record: dict, context_hash: str) -> str:
    return digest({"source": record["source"], "selected_paragraph_ids": record["selected_paragraph_ids"],
                   "plan": record.get("plan"), "setup": record.get("setup", {}),
                   "context_hash": context_hash})


class RevisionConflict(ValueError):
    pass


class ProtocolStore:
    def __init__(self, directory: str | Path | None = None):
        self.directory = Path(directory or os.environ.get("PYBRAVO_PROTOCOL_STORE", "~/.pybravo/protocols")).expanduser()
        self._lock = threading.RLock()

    def _path(self, kind: str, identity: str) -> Path:
        if kind not in {"sessions", "library", "setups", "releases", "sources", "proposals"} or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", identity):
            raise ValueError("Invalid record identifier")
        return self.directory / kind / f"{identity}.json"

    def pdf_path(self, identity: str) -> Path:
        return self._path("sources", identity).with_suffix(".pdf")

    def save_pdf(self, identity: str, data: bytes) -> None:
        path = self.pdf_path(identity)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_bytes(data)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def get(self, kind: str, identity: str) -> dict | None:
        path = self._path(kind, identity)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def list(self, kind: str) -> list[dict]:
        directory = self._path(kind, "index").parent
        items = []
        for path in directory.glob("*.json"):
            try:
                items.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return sorted(items, key=lambda r: r.get("updated", r.get("created", "")), reverse=True)

    def put(self, kind: str, data: dict) -> dict:
        with self._lock:
            value = copy.deepcopy(data)
            value.setdefault("id", str(uuid.uuid4()))
            value.setdefault("created", now())
            value["updated"] = now()
            path = self._path(kind, value["id"])
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
            try:
                temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            return value

    def create_session(self, source: dict, *, setup: dict | None = None, plan: dict | None = None,
                       chat_messages: list[dict] | None = None, model: dict | None = None,
                       identity: str | None = None) -> dict:
        record = {"revision": 1, "source": source, "name": source.get("name", "Protocol"),
            "selected_paragraph_ids": [p["id"] for p in source["paragraphs"]], "plan": plan,
            "setup": setup or {}, "issues": [], "validation": None, "simulation": None,
            "approval": None, "history": [], "runs": []}
        if identity is not None:
            record["id"] = identity
        if chat_messages is not None:
            record["chat_messages"] = chat_messages
        if model is not None:
            record["model"] = model
        return self.put("sessions", record)

    def update_session(self, identity: str, changes: dict, *, revision: int, event: str = "scientist_edit") -> dict:
        with self._lock:
            record = self.get("sessions", identity)
            if record is None:
                raise KeyError(identity)
            if record["revision"] != revision:
                raise RevisionConflict("This protocol changed in another request. Reload it before saving.")
            changed = any(record.get(key) != value for key, value in changes.items())
            if not changed:
                return record
            record["history"].append({"revision": record["revision"], "time": now(), "event": event,
                "plan": record.get("plan"), "setup": record.get("setup"),
                "selected_paragraph_ids": record["selected_paragraph_ids"], "approval": record.get("approval"),
                "simulation": record.get("simulation"), "model": record.get("model"),
                "source": record.get("source"), "chat_messages": record.get("chat_messages", [])})
            record.update(copy.deepcopy(changes))
            record.update(revision=revision + 1, validation=None, simulation=None, approval=None, issues=[])
            return self.put("sessions", record)

    def annotate(self, identity: str, changes: dict, *, revision: int) -> dict:
        """Attach results only if the exact reviewed revision still exists."""
        with self._lock:
            record = self.get("sessions", identity)
            if record is None:
                raise KeyError(identity)
            if record["revision"] != revision:
                raise RevisionConflict("Protocol changed while the operation was running; rerun it.")
            record.update(copy.deepcopy(changes))
            return self.put("sessions", record)

    def resolve_proposal(self, identity: str, *, accept: bool) -> dict:
        """Apply a server-owned draft once, against its original session revision."""
        with self._lock:
            proposal = self.get("proposals", identity)
            if proposal is None:
                raise KeyError(identity)
            if proposal["status"] != "pending":
                raise RevisionConflict("This suggestion was already accepted or rejected.")
            if not accept:
                return self.put("proposals", {**proposal, "status": "rejected"})
            candidate = proposal["candidate"]
            base = proposal["base_revision"]
            if base is None:
                if self.get("sessions", candidate["id"]) is not None:
                    raise RevisionConflict("This protocol already exists. Reload it before continuing.")
                saved = self.create_session(candidate["source"], identity=candidate["id"],
                    plan=candidate["plan"], setup=candidate["setup"],
                    chat_messages=candidate["chat_messages"], model=candidate["model"])
            else:
                saved = self.update_session(candidate["id"], {**{key: candidate[key] for key in (
                    "name", "source", "selected_paragraph_ids", "plan", "setup", "chat_messages", "model",
                )}, "agent_pending_paragraphs": candidate.get("agent_pending_paragraphs", [])},
                    revision=base, event="accepted_chat_proposal")
            self.put("proposals", {**proposal, "status": "accepted"})
            return saved
