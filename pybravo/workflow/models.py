"""Pydantic models for workflow designer data."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class WorkflowNodeConfig(BaseModel):
    """Configuration for a single workflow node (from Litegraph serialization)."""

    id: int
    type: str
    pos: list[float] = Field(default_factory=lambda: [0, 0])
    properties: dict[str, Any] = Field(default_factory=dict)
    outputs: list[dict[str, Any]] = Field(default_factory=list)
    inputs: list[dict[str, Any]] = Field(default_factory=list)


class WorkflowDeckEntry(BaseModel):
    """Labware assignment for a single deck position."""

    labware_id: str
    name: str = ""
    kind: str = ""
    base_class: str = ""
    wells: int = 0
    is_lidded: bool = False
    is_sealed: bool = False
    tip_definition_id: str = ""


class WorkflowDefinition(BaseModel):
    """Full workflow definition as stored in JSON."""

    id: str = ""
    name: str = "Untitled Workflow"
    description: str = ""
    created: str = ""
    modified: str = ""
    deck: dict[str, WorkflowDeckEntry] = Field(default_factory=dict)
    graph: dict[str, Any] = Field(default_factory=dict)


class ExecutionMode(str, Enum):
    SIMULATE = "simulate"
    EXECUTE = "execute"


class WorkflowExecutionRequest(BaseModel):
    """Request to execute or simulate a workflow."""

    mode: ExecutionMode = ExecutionMode.SIMULATE
    speed: float = 1.0  # Playback speed multiplier (simulation only)
