"""Small, task-selected instructions for the local protocol planning model.

The application owns tool calls, validation, and hardware access. These skills
only shape the scientific intent returned by the one-shot extractor; they do
not grant the model a direct robot command surface.
"""

from __future__ import annotations

import re
from pathlib import Path

from .ingest import IngestedProtocol

_ROOT = Path(__file__).resolve().parent / "skills"
_TRIGGERS = {
    "deck-and-tips": re.compile(r"\b(?:deck|slots?|stacks?|tips?|tipboxes?|racks?|boxes?|waste|plates?|heads?|labware)\b", re.I),
    "liquid-methods": re.compile(r"\b(transfer|aspirat|dispens|mix|aliquot|reagent|liquid|buffer|solution|solvent|DMSO)\w*\b", re.I),
}
_QUADRANT_TRANSFER = re.compile(r"\bquadrants?\b", re.I)
_SOURCE_GRID = re.compile(r"\b384\b")
_DESTINATION_GRID = re.compile(r"\b1536\b")


def selected_skills(source: IngestedProtocol) -> list[tuple[str, str]]:
    """Return only the concise skill bodies relevant to the selected source."""
    source_text = "\n".join(paragraph.text for paragraph in source.paragraphs)
    names = ["protocol-interpretation", *(
        name for name, trigger in _TRIGGERS.items() if trigger.search(source_text)
    )]
    if (_QUADRANT_TRANSFER.search(source_text) and _SOURCE_GRID.search(source_text)
            and _DESTINATION_GRID.search(source_text)):
        names.append("384-to-1536-quadrants")
    if re.search(r"\bstack\w*\b", source_text, re.I) and _SOURCE_GRID.search(source_text):
        names.append("stacked-plate-processing")
    result = []
    for name in names:
        content = (_ROOT / name / "SKILL.md").read_text(encoding="utf-8")
        # Frontmatter is discovery metadata for other Agent Skills clients;
        # the local model only needs the concise operative body.
        body = content.split("---", 2)[-1].strip() if content.startswith("---") else content.strip()
        result.append((name, body))
    return result
