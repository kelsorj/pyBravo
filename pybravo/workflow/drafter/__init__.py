"""LLM-powered workflow drafter.

Public entrypoints:

* :class:`DraftedWorkflow` — Pydantic schema the LLM is constrained to emit.
* :func:`validate_drafted_workflow` — post-generation sanity check.
* :func:`build_system_prompt` — assembles the long context (node catalog +
  deck + labware + snippets + exemplars).
* :func:`draft_workflow` — async, end-to-end: prompt → LLM → validated JSON.

The LLM invocation path (``invoke_llm_structured``) imports ``instructor``
lazily — the rest of the module works without LLM packages installed, so
tests can exercise the schema + validator deterministically on CI.
"""

from __future__ import annotations

from pybravo.workflow.drafter.facts import (
    ExtractedFact,
    FactKind,
    PaperFacts,
)
from pybravo.workflow.drafter.llm import (
    LLMDrafterError,
    MissingLLMDependencyError,
    draft_workflow,
    draft_workflow_from_facts,
    draft_workflow_from_paper,
    extract_facts,
)
from pybravo.workflow.drafter.paper_parser import (
    DoclingServiceError,
    MissingDoclingConfigError,
    PaperParserError,
    ParsedPaper,
    ParsedParagraph,
    parse_pdf_bytes,
)
from pybravo.workflow.drafter.prompt import build_system_prompt
from pybravo.workflow.drafter.schema import (
    SUPPORTED_NODE_TYPES,
    DraftedDeckItem,
    DraftedGraph,
    DraftedLink,
    DraftedNode,
    DraftedWorkflow,
    SourceCitation,
)
from pybravo.workflow.drafter.segmenter import (
    PaperProtocols,
    ProtocolCandidate,
    autoselect_top,
    segment_paper,
)
from pybravo.workflow.drafter.validator import (
    ValidationIssue,
    validate_drafted_workflow,
)

__all__ = [
    "DraftedDeckItem",
    "DraftedGraph",
    "DraftedLink",
    "DraftedNode",
    "DraftedWorkflow",
    "SourceCitation",
    "SUPPORTED_NODE_TYPES",
    "ValidationIssue",
    "validate_drafted_workflow",
    "build_system_prompt",
    "LLMDrafterError",
    "MissingLLMDependencyError",
    "draft_workflow",
    # Phase 3: PDF ingest
    "DoclingServiceError",
    "MissingDoclingConfigError",
    "PaperParserError",
    "ParsedPaper",
    "ParsedParagraph",
    "parse_pdf_bytes",
    # Phase 3: Two-pass PDF → workflow
    "ExtractedFact",
    "FactKind",
    "PaperFacts",
    "draft_workflow_from_facts",
    "draft_workflow_from_paper",
    "extract_facts",
    # Phase 3 Pass 0: protocol segmentation
    "PaperProtocols",
    "ProtocolCandidate",
    "autoselect_top",
    "segment_paper",
]
