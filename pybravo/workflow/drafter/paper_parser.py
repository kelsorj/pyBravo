"""Docling-serve client — parse a scientific-paper PDF into structured Markdown.

This is the first pass of the Phase 3 PDF-ingest pipeline. It does ONE
job: take a PDF, post it to a remote docling-serve instance (runs on a
GPU box; see PYBRAVO_DOCLING_URL in .env), return a :class:`ParsedPaper`
holding the Markdown body + structured document JSON + stable
paragraph IDs for citation tracking.

A separate module will consume a ParsedPaper and extract just the
Materials/Methods section; another will turn that into a drafted
workflow JSON.

Environment:
    PYBRAVO_DOCLING_URL   — base URL of the docling-serve instance.
                            No default — missing env var raises
                            MissingDoclingConfigError so the endpoint
                            returns a clear 501.

    PYBRAVO_DOCLING_TIMEOUT — per-request timeout in seconds (default 300).
                              Big scanned papers take a while on first
                              model load.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


# ── Exceptions ────────────────────────────────────────────────────────


class PaperParserError(RuntimeError):
    """Base class for Docling-side failures surfaced to the API layer."""


class MissingDoclingConfigError(PaperParserError):
    """PYBRAVO_DOCLING_URL is not set."""


class DoclingServiceError(PaperParserError):
    """docling-serve returned a non-2xx response or was unreachable."""


# ── Public result type ────────────────────────────────────────────────


@dataclass
class ParsedParagraph:
    """One paragraph / text block / table in the parsed document.

    ``paragraph_id`` is a stable opaque string (e.g. ``p-7``) that
    follow-on passes use to cite back to a specific location in the
    paper. It's assigned by this module, not Docling — Docling's
    internal ref_ids are stable enough but vary by format.
    """

    paragraph_id: str
    text: str
    kind: str = "paragraph"      # paragraph | heading | list_item | table | caption
    section: str = ""             # best-effort section name (e.g. "Methods")
    page_no: int | None = None
    heading_level: int | None = None


@dataclass
class ParsedPaper:
    """Structured view of a parsed scientific paper."""

    markdown: str                             # full Markdown body (Docling's rendering)
    paragraphs: list[ParsedParagraph] = field(default_factory=list)
    raw_document: dict[str, Any] = field(default_factory=dict)  # Docling DocumentJson
    page_count: int = 0
    source_name: str = ""                     # original filename, for logs / citations

    def section_text(self, section_name: str, *, fuzzy: bool = True) -> str:
        """Concatenated text of paragraphs whose ``section`` matches.

        If ``fuzzy`` is True (default), case-insensitive substring match
        so ``section_text("methods")`` picks up "Materials and Methods",
        "Methods and Materials", etc.
        """
        needle = section_name.strip().lower()
        hits: list[str] = []
        for p in self.paragraphs:
            sec = (p.section or "").lower()
            if fuzzy:
                if needle and needle in sec:
                    hits.append(p.text)
            else:
                if sec == needle:
                    hits.append(p.text)
        return "\n\n".join(hits)

    def paragraph(self, paragraph_id: str) -> ParsedParagraph | None:
        """O(n) lookup by id — fine for single-paper scale."""
        for p in self.paragraphs:
            if p.paragraph_id == paragraph_id:
                return p
        return None


# ── docling-serve HTTP client ─────────────────────────────────────────


def _resolved_base_url() -> str:
    url = (os.environ.get("PYBRAVO_DOCLING_URL") or "").strip().rstrip("/")
    if not url:
        raise MissingDoclingConfigError(
            "PYBRAVO_DOCLING_URL is not set. Point it at your docling-serve "
            "instance, e.g. `PYBRAVO_DOCLING_URL=http://<dgx-host>:5001` "
            "in your .env. See docs for how to stand up docling-serve on the "
            "DGX."
        )
    return url


def _resolved_timeout() -> float:
    raw = os.environ.get("PYBRAVO_DOCLING_TIMEOUT", "300")
    try:
        return float(raw)
    except ValueError:
        return 300.0


async def parse_pdf_bytes(
    pdf_bytes: bytes,
    *,
    filename: str = "paper.pdf",
    base_url: str | None = None,
) -> ParsedPaper:
    """Post a PDF's bytes to docling-serve and return a ParsedPaper.

    Keyword args:
        filename   — used by docling-serve in its log lines; also stored
                     on ParsedPaper.source_name for citation display.
        base_url   — override PYBRAVO_DOCLING_URL for tests.

    Raises:
        MissingDoclingConfigError: PYBRAVO_DOCLING_URL not set.
        DoclingServiceError: unreachable / non-2xx / bad response.
    """
    # Lazy import so this module loads cleanly on machines without httpx.
    try:
        import httpx  # type: ignore[import-not-found]
    except ImportError as exc:
        raise PaperParserError(
            "The `httpx` package is required for PDF ingest. "
            "It should already be present via the LLM-drafter deps. "
            "pip install httpx"
        ) from exc

    url = (base_url or _resolved_base_url()).rstrip("/") + "/v1/convert/file"
    timeout = _resolved_timeout()

    # docling-serve's multipart upload uses the `files` part name (plural).
    logger.info(
        "parse_pdf_bytes: posting to docling-serve",
        url=url, size_bytes=len(pdf_bytes), filename=filename, timeout_s=timeout,
    )

    files = {"files": (filename, pdf_bytes, "application/pdf")}
    # docling-serve accepts options as form fields with the same names
    # (not a nested 'options' JSON). Build a matching form dict.
    form_data: dict[str, Any] = {
        "to_formats": ["md", "json"],
        "do_table_structure": "true",
        "do_ocr": "true",
        "do_code_enrichment": "false",
        "do_formula_enrichment": "false",
        "do_picture_classification": "false",
        "do_picture_description": "false",
    }

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
        try:
            resp = await client.post(url, files=files, data=form_data)
        except httpx.HTTPError as exc:
            raise DoclingServiceError(
                f"docling-serve at {url} is unreachable: {exc}"
            ) from exc
    if resp.status_code >= 400:
        snippet = resp.text[:600] if resp.text else ""
        raise DoclingServiceError(
            f"docling-serve returned {resp.status_code} for {url}: {snippet}"
        )
    try:
        payload = resp.json()
    except Exception as exc:
        raise DoclingServiceError(
            f"docling-serve response wasn't JSON ({resp.headers.get('content-type')}): {exc}"
        ) from exc

    return _payload_to_parsed_paper(payload, source_name=filename)


# ── Payload → ParsedPaper ──────────────────────────────────────────────


def _payload_to_parsed_paper(payload: dict[str, Any], *, source_name: str) -> ParsedPaper:
    """Convert docling-serve's response into our ParsedPaper shape.

    docling-serve responses typically look like::

        {
            "status": "success",
            "document": {
                "md_content": "...",
                "json_content": {...},   # Docling DocumentJson
                "html_content": "...",
                ...
            },
            "errors": [...],
        }

    The exact shape has drifted across docling-serve versions. This
    function is tolerant: it looks for ``md_content`` / ``json_content``
    at both the top level and nested under ``document`` / ``result``.
    """
    # Surface any docling-side errors into our logs so the operator
    # sees them even when the markdown is partial.
    errs = payload.get("errors") or []
    if errs:
        for e in errs:
            logger.warning("docling-serve reported error: %s", e)

    # Find the document-ish dict.
    doc = payload.get("document") or payload.get("result") or payload

    markdown = ""
    raw_document: dict[str, Any] = {}
    for candidate in (doc, payload):
        if not isinstance(candidate, dict):
            continue
        if not markdown:
            markdown = (
                candidate.get("md_content")
                or candidate.get("markdown")
                or ""
            )
        if not raw_document:
            jc = candidate.get("json_content") or candidate.get("doc") or candidate.get("document")
            if isinstance(jc, dict):
                raw_document = jc

    paragraphs = _extract_paragraphs(raw_document)

    page_count = 0
    try:
        pages = raw_document.get("pages") or {}
        if isinstance(pages, dict):
            page_count = len(pages)
        elif isinstance(pages, list):
            page_count = len(pages)
    except Exception:
        page_count = 0

    return ParsedPaper(
        markdown=markdown or "",
        paragraphs=paragraphs,
        raw_document=raw_document,
        page_count=page_count,
        source_name=source_name,
    )


# Section-name tracking: walk the document's text elements in reading
# order. Whenever we see a heading, update the "current section" to the
# nearest ancestor that looks like a top-level Methods/Results/etc.
# heading; subsequent paragraphs inherit it. Heuristic — good enough for
# citation tracking, not a replacement for proper outline parsing.

_SECTION_HEADINGS_OF_INTEREST = (
    "abstract", "introduction", "background", "results", "discussion",
    "conclusion", "conclusions", "methods", "materials and methods",
    "experimental", "experimental procedures", "experimental section",
    "methods and materials", "procedures", "protocol", "protocols",
    "supplementary", "supplementary methods", "supplementary information",
    "references", "acknowledgements", "acknowledgments",
)

# Regex that strips numeric section prefixes like "1.", "1.2.", "4.1.3.",
# "1)", etc. so "4. Methods" → "Methods" and "4.1. Analysis" → "Analysis"
# for heading-text matching. Leaves unrelated content alone.
_SECTION_PREFIX_RE = re.compile(r"^\s*(?:\d+[\.\)]\s*)+")


def _extract_paragraphs(document: dict[str, Any]) -> list[ParsedParagraph]:
    """Flatten a Docling DocumentJson into our ParsedParagraph list.

    Docling's DocumentJson has ``texts`` (inline flat list of text items)
    and ``main_text`` (reading-order references). We iterate in reading
    order so section tracking reflects how the paper reads, not the
    arbitrary JSON order.
    """
    if not document:
        return []

    # The 'texts' array is the flat inventory. Each entry has:
    #   { "label": "section_header" | "text" | "list_item" | "caption" | ..,
    #     "text": "...", "level": 1, "prov": [{"page_no": 1, ...}] }
    texts: list[dict[str, Any]] = []
    raw_texts = document.get("texts")
    if isinstance(raw_texts, list):
        texts = [t for t in raw_texts if isinstance(t, dict)]

    # If we have main_text, follow its reading-order references. Each
    # entry is either inline {"type": "text", ...} or a "$ref" pointer
    # like "#/texts/7".
    reading_order: list[dict[str, Any]] = []
    main_text = document.get("main_text") or document.get("body")
    if isinstance(main_text, list):
        for entry in main_text:
            if not isinstance(entry, dict):
                continue
            ref = entry.get("$ref") or entry.get("ref")
            if isinstance(ref, str) and ref.startswith("#/texts/"):
                try:
                    idx = int(ref.rsplit("/", 1)[-1])
                    if 0 <= idx < len(texts):
                        reading_order.append(texts[idx])
                except (ValueError, IndexError):
                    continue
            elif "text" in entry:
                reading_order.append(entry)
    if not reading_order:
        reading_order = texts

    out: list[ParsedParagraph] = []
    current_section = ""
    # Track the numeric prefix of the current top-level section so
    # subsections like "4.1." inherit from "4. Methods". A heading with a
    # new top-level number (e.g. "5.") either matches a new section-of-
    # interest or clears current_section back to "".
    current_section_prefix = ""  # e.g. "4." when we're inside "4. Methods"
    for i, item in enumerate(reading_order):
        label = str(item.get("label") or item.get("type") or "text").lower()
        text = (item.get("text") or "").strip()
        if not text:
            continue
        # Page info — depends on Docling version; prov[0].page_no is the
        # most common path.
        page_no: int | None = None
        prov = item.get("prov")
        if isinstance(prov, list) and prov:
            first = prov[0]
            if isinstance(first, dict):
                try:
                    page_no = int(first.get("page_no") or first.get("page") or 0) or None
                except (TypeError, ValueError):
                    page_no = None

        heading_level: int | None = None
        if label in ("section_header", "heading", "title"):
            heading_level = item.get("level") if isinstance(item.get("level"), int) else None
            # Strip numeric prefixes ("4. Methods" → "Methods", "4.1." → "")
            # and capture the prefix for subsection inheritance.
            raw = text.strip(" :.")
            prefix_match = _SECTION_PREFIX_RE.match(raw)
            prefix = prefix_match.group(0).strip() if prefix_match else ""
            stripped = _SECTION_PREFIX_RE.sub("", raw).strip().lower()

            # Normalize the top-level prefix ("4.1." → "4.", "4.1.2." → "4.")
            # so every subsection of a top-level numbered section shares a
            # common family prefix.
            top_level_prefix = ""
            if prefix:
                first_num = prefix.split(".", 1)[0]
                if first_num.rstrip(")").isdigit():
                    top_level_prefix = first_num + "."

            # Does this heading's own stripped text match a section-of-
            # interest? Matches "Methods", "Materials and Methods",
            # "Experimental", etc. regardless of numeric prefix.
            matched = next(
                (h for h in _SECTION_HEADINGS_OF_INTEREST
                 if stripped == h or stripped.startswith(h + " ")
                 or stripped.startswith(h + ":")),
                None,
            )

            if matched:
                current_section = matched
                current_section_prefix = top_level_prefix
            elif current_section_prefix and top_level_prefix == current_section_prefix:
                # Subsection of the currently-active top-level section
                # (e.g. "4.7. Time calculations" inside "4. Methods").
                # Keep current_section unchanged.
                pass
            elif top_level_prefix and top_level_prefix != current_section_prefix:
                # A DIFFERENT top-level numbered section started. Clear
                # current_section — we'll pick up a new one if this or a
                # later heading matches a section-of-interest.
                current_section = ""
                current_section_prefix = top_level_prefix
            # else: unnumbered heading that doesn't match any keyword —
            # leave state untouched (e.g. the paper's title, journal
            # banner, etc. that Docling labels as section_header).
            kind = "heading"
        elif label in ("list_item", "list-item"):
            kind = "list_item"
        elif label in ("table", "tbl"):
            kind = "table"
        elif label in ("caption",):
            kind = "caption"
        else:
            kind = "paragraph"

        out.append(ParsedParagraph(
            paragraph_id=f"p-{i + 1}",
            text=text,
            kind=kind,
            section=current_section,
            page_no=page_no,
            heading_level=heading_level,
        ))
    return out
