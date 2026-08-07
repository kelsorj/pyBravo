"""Scrape per-node-type property defaults out of ``frontend/designer.html``.

The designer is the source of truth for what a freshly-instantiated
LiteGraph node looks like — every ``addProperty(key, default)`` call in
a node constructor (or the ``props`` object passed to the
``makeTaskNode`` factory) defines a field that gets auto-filled when
the designer loads a drafted workflow. The drafter's diff needs this
same table to tell "LLM emitted null and the designer filled in its
default" apart from a real user edit.

Rather than maintain a second hand-written copy in Python (inevitable
drift), we parse the HTML at startup and derive the table directly.
If parsing fails for any reason — file missing, formatting change the
regex can't handle, malformed JS — the caller falls back to a
hardcoded table; nothing crashes.

Supported syntax in the scraped object literals:

* Unquoted keys (``foo:``)
* Strings in single or double quotes (``'bar'`` / ``"bar"``)
* Numbers (int and float)
* Booleans (``true`` / ``false``) — mapped to Python ``True`` / ``False``
* ``null`` → Python ``None``
* Empty arrays ``[]`` and empty objects ``{}``
* Trailing commas

NOT supported (raises ParseError; caller falls back):

* Computed keys
* Spread operators
* Function values / methods
* Arithmetic expressions in values
* Nested objects / arrays with content
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ParseError(RuntimeError):
    """Raised when a scraped object literal doesn't match supported syntax."""


# ── Brace-matching + token helpers ──────────────────────────────────


def _find_matching_brace(s: str, start: int, open_ch: str = "{", close_ch: str = "}") -> int | None:
    """Return the index of the brace that closes the one at ``start``.

    Tracks nesting and ignores braces that appear inside string
    literals (single or double quoted, escape-aware). Returns None if
    the string has no matching close.
    """
    if start >= len(s) or s[start] != open_ch:
        return None
    depth = 1
    i = start + 1
    in_str: str | None = None   # quote char if we're inside a string
    while i < len(s):
        c = s[i]
        if in_str is not None:
            if c == "\\":
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if c in ("'", '"'):
            in_str = c
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _strip_js_comments(text: str) -> str:
    """Remove // line comments and /* block */ comments.
    Conservative — keeps string contents alone."""
    out: list[str] = []
    i = 0
    in_str: str | None = None
    while i < len(text):
        c = text[i]
        if in_str is not None:
            out.append(c)
            if c == "\\" and i + 1 < len(text):
                out.append(text[i + 1])
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if c in ("'", '"'):
            in_str = c
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt == "/":
                j = text.find("\n", i + 2)
                if j == -1:
                    return "".join(out)
                i = j  # keep the newline itself
                continue
            if nxt == "*":
                j = text.find("*/", i + 2)
                if j == -1:
                    return "".join(out)
                i = j + 2
                continue
        out.append(c)
        i += 1
    return "".join(out)


def _js_singlequotes_to_double(text: str) -> str:
    """Turn every JS string literal into a Python-parseable form.

    Single-quoted strings become double-quoted (escaping any embedded
    ``"``). Double-quoted strings are left alone. Conservatively
    walks the string char by char so literal content never gets
    touched mid-string.
    """
    out: list[str] = []
    i = 0
    while i < len(text):
        c = text[i]
        if c == '"':
            # Passthrough the whole double-quoted string.
            j = i + 1
            while j < len(text):
                if text[j] == "\\" and j + 1 < len(text):
                    j += 2
                    continue
                if text[j] == '"':
                    break
                j += 1
            out.append(text[i:j + 1])
            i = j + 1
            continue
        if c == "'":
            # Locate close then rewrite as "...".
            j = i + 1
            buf: list[str] = []
            while j < len(text):
                if text[j] == "\\" and j + 1 < len(text):
                    buf.append(text[j])
                    buf.append(text[j + 1])
                    j += 2
                    continue
                if text[j] == "'":
                    break
                buf.append(text[j])
                j += 1
            if j >= len(text):
                raise ParseError(f"unterminated string starting at {i}")
            inner = "".join(buf).replace('"', '\\"')
            out.append('"' + inner + '"')
            i = j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


# ── Object-literal parser ────────────────────────────────────────────

_UNQUOTED_KEY_RE = re.compile(r"([\{,]\s*)([A-Za-z_$][A-Za-z0-9_$]*)(\s*):")


def _parse_js_object_literal(text: str) -> dict[str, Any]:
    """Parse a simple JavaScript object literal into a Python dict.

    Delegates the hard work to ``ast.literal_eval`` after normalizing
    JS-isms to Python-isms. Any construct that can't be expressed via
    literal_eval raises ParseError.
    """
    src = _strip_js_comments(text).strip()
    if not src.startswith("{") or not src.endswith("}"):
        raise ParseError("not an object literal")

    src = _js_singlequotes_to_double(src)
    # Keyword swaps — word-bounded so we don't touch identifiers
    # that happen to contain 'true' / 'false' / 'null' as substrings.
    src = re.sub(r"\btrue\b",  "True",  src)
    src = re.sub(r"\bfalse\b", "False", src)
    src = re.sub(r"\bnull\b",  "None",  src)
    # Quote unquoted keys
    src = _UNQUOTED_KEY_RE.sub(r'\1"\2"\3:', src)
    # Trailing commas (both `}` and `]` cases)
    src = re.sub(r",\s*([\}\]])", r"\1", src)

    try:
        return ast.literal_eval(src)
    except (ValueError, SyntaxError) as exc:
        raise ParseError(f"literal_eval failed: {exc}; source={src[:200]!r}") from exc


# ── makeTaskNode(...) extractor ─────────────────────────────────────


# Matches the open of `makeTaskNode('Title', 'category', 'color', {`.
# The trailing `{` is deliberately captured so _find_matching_brace
# can walk from there.
_MAKE_TASK_NODE_RE = re.compile(
    r"""
    makeTaskNode \s* \(
        \s* ['"](?P<title>[^'"]+)['"] \s* ,
        \s* ['"](?P<category>[^'"]+)['"] \s* ,
        \s* ['"][^'"]+['"] \s* ,
        \s* (?P<brace>\{)
    """,
    re.VERBOSE,
)


def _title_to_type_suffix(title: str) -> str:
    """Mirror the designer's ``title.replace(/[\\s\\/]/g, '')``
    transformation. 'Tips On' → 'TipsOn'; 'Pick/Place' → 'PickPlace'.
    """
    return re.sub(r"[\s/]", "", title)


def _scrape_make_task_nodes(html: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for match in _MAKE_TASK_NODE_RE.finditer(html):
        brace_start = match.start("brace")
        brace_end = _find_matching_brace(html, brace_start)
        if brace_end is None:
            logger.warning("defaults_scraper_unclosed_brace title=%s", match.group("title"))
            continue
        obj_src = html[brace_start:brace_end + 1]
        try:
            props = _parse_js_object_literal(obj_src)
        except ParseError as exc:
            logger.warning(
                "defaults_scraper_parse_error title=%s err=%s",
                match.group("title"), exc,
            )
            continue
        type_key = f"{match.group('category')}/{_title_to_type_suffix(match.group('title'))}"
        out[type_key] = props
    return out


# ── addProperty(...) extractor for registerNodeType-only nodes ─────


_REGISTER_RE = re.compile(
    r"LiteGraph\.registerNodeType\(\s*['\"](?P<type>[^'\"]+)['\"]\s*,\s*(?P<ctor>[A-Za-z_$][A-Za-z0-9_$]*)\s*\)"
)

# Accepts values that are simple primitives: strings, numbers, true/false/null,
# or empty literal []/{}. Good enough for every addProperty we have today.
_ADDPROP_RE = re.compile(
    r"""
    this\.addProperty \s* \(
        \s* ['"](?P<key>[^'"]+)['"] \s* ,
        \s* (?P<val>
            (?:'(?:\\.|[^'])*') |
            (?:"(?:\\.|[^"])*") |
            (?:-?\d+\.\d+) |
            (?:-?\d+) |
            true | false | null |
            \[\s*\] |
            \{\s*\}
        )
        \s* \)
    """,
    re.VERBOSE,
)


def _parse_js_scalar(text: str) -> Any:
    """Single-value parser for the addProperty case. Reuses the object
    pipeline by wrapping the value in a throwaway object."""
    wrapped = "{ _v: " + text + " }"
    return _parse_js_object_literal(wrapped)["_v"]


def _scrape_register_node_types(html: str, already_scraped: set[str]) -> dict[str, dict[str, Any]]:
    """For every registerNodeType(type, Ctor) whose type isn't already
    covered by makeTaskNode, locate the constructor function body and
    extract its addProperty calls. Flow / logic nodes are reached this
    way.
    """
    out: dict[str, dict[str, Any]] = {}
    for match in _REGISTER_RE.finditer(html):
        type_key = match.group("type")
        if type_key in already_scraped:
            continue
        ctor = match.group("ctor")
        func_re = re.compile(rf"function\s+{re.escape(ctor)}\s*\(\s*\)\s*\{{")
        func_match = func_re.search(html)
        if not func_match:
            logger.debug(
                "defaults_scraper_ctor_not_found type=%s ctor=%s",
                type_key, ctor,
            )
            out[type_key] = {}
            continue
        brace_start = func_match.end() - 1
        brace_end = _find_matching_brace(html, brace_start)
        if brace_end is None:
            logger.warning("defaults_scraper_ctor_unclosed type=%s", type_key)
            out[type_key] = {}
            continue
        body = html[brace_start + 1:brace_end]
        props: dict[str, Any] = {}
        for p in _ADDPROP_RE.finditer(body):
            try:
                props[p.group("key")] = _parse_js_scalar(p.group("val"))
            except ParseError as exc:
                logger.warning(
                    "defaults_scraper_addprop_unparseable type=%s key=%s err=%s",
                    type_key, p.group("key"), exc,
                )
        out[type_key] = props
    return out


# ── Public entry point ───────────────────────────────────────────────


def scrape_node_defaults(html_path: str | Path) -> dict[str, dict[str, Any]]:
    """Parse designer.html and return the per-node-type defaults table.

    Args:
        html_path: absolute path to ``frontend/designer.html``.

    Returns:
        Dict keyed by node type (e.g. ``"liquid/Aspirate"``) mapping to
        a ``{property_name: default_value}`` dict.  Entries with an
        empty value dict are still included when the node type exists
        but has no registered defaults (e.g. ``flow/Start``).

    Raises:
        FileNotFoundError: the HTML file doesn't exist at ``html_path``.
        ParseError: only if the shape of the file is so broken that
            even an empty result set can't be produced.  Caller is
            expected to catch and fall back to a hardcoded table.
    """
    path = Path(html_path)
    if not path.exists():
        raise FileNotFoundError(f"designer.html not found at {path}")
    html = path.read_text(encoding="utf-8")

    # 1. makeTaskNode(...) calls (most node types)
    scraped = _scrape_make_task_nodes(html)
    already = set(scraped.keys())

    # 2. registerNodeType(...) for everything else (flow/logic)
    reg = _scrape_register_node_types(html, already)
    scraped.update(reg)

    # flow/Start and flow/End have no properties but are valid types;
    # ensure they appear so consumers can `in` them.
    for zero_default in ("flow/Start", "flow/End"):
        scraped.setdefault(zero_default, {})

    return scraped
