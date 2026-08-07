"""PDF page rendering via pypdfium2.

Produces PNG images of individual pages for the picker UI's preview
pane. Cached on disk at ``~/.pybravo/papers/<sha256>.pages/<n>.png`` —
same content-addressed scheme the PDF cache uses, just with a
per-paper subdirectory. Rendering is idempotent and cache-hit fast.

Uses pypdfium2 (Apache 2.0) rather than PyMuPDF (AGPL) so nothing in
the drafter pulls in copyleft obligations.  Dependencies:
``pypdfium2 >= 4.0`` and ``Pillow`` (for PNG encoding).
"""

from __future__ import annotations

import logging
import threading
from io import BytesIO
from pathlib import Path

from pybravo.workflow.drafter import store as _store

logger = logging.getLogger(__name__)


# pypdfium2 is thread-safe within a process but serializing rendering
# avoids transient lock contention on many-page papers.
_render_lock = threading.Lock()


def _pages_dir(pdf_hash: str) -> Path:
    """Per-paper subdirectory where rendered PNGs live."""
    base = _store.pdf_path_for(pdf_hash).parent / f"{pdf_hash}.pages"
    return base


def _page_png_path(pdf_hash: str, page_no: int, scale: float) -> Path:
    """Cache path for one rendered page. Scale goes into the filename
    so different zoom levels coexist without clobbering each other."""
    tag = f"{page_no:04d}@{int(scale * 100):04d}.png"
    return _pages_dir(pdf_hash) / tag


def render_page(pdf_hash: str, page_no: int, *, scale: float = 1.5) -> bytes | None:
    """Return PNG bytes for the requested 1-indexed page, or None if
    the PDF isn't cached.

    Cache hit: reads the PNG straight off disk.
    Cache miss: opens the PDF via pypdfium2, renders, encodes, writes,
    returns. Subsequent calls at the same scale skip all of that.
    """
    pdf_path = _store.pdf_path_for(pdf_hash)
    if not pdf_path.exists():
        return None

    out_path = _page_png_path(pdf_hash, page_no, scale)
    if out_path.exists():
        try:
            return out_path.read_bytes()
        except OSError as exc:
            logger.warning("pdf_render_cache_read_failed", exc_info=exc)

    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        logger.warning("pdf_render_pypdfium2_missing", exc_info=exc)
        return None

    with _render_lock:
        try:
            doc = pdfium.PdfDocument(str(pdf_path))
        except Exception as exc:
            logger.warning("pdf_render_open_failed", exc_info=exc)
            return None
        try:
            n_pages = len(doc)
            if not (1 <= page_no <= n_pages):
                return None
            page = doc[page_no - 1]
            try:
                pil = page.render(scale=scale).to_pil()
            finally:
                page.close()
        finally:
            doc.close()

    buf = BytesIO()
    pil.save(buf, format="PNG", optimize=True)
    png = buf.getvalue()

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(".png.tmp")
        tmp.write_bytes(png)
        tmp.replace(out_path)
    except OSError as exc:
        logger.warning("pdf_render_cache_write_failed", exc_info=exc)

    return png


def page_count(pdf_hash: str) -> int | None:
    """Open the cached PDF just long enough to read its page count.
    None when the PDF isn't cached or pypdfium2 isn't installed.
    """
    pdf_path = _store.pdf_path_for(pdf_hash)
    if not pdf_path.exists():
        return None
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return None
    try:
        doc = pdfium.PdfDocument(str(pdf_path))
        try:
            return len(doc)
        finally:
            doc.close()
    except Exception as exc:
        logger.warning("pdf_render_open_failed", exc_info=exc)
        return None
