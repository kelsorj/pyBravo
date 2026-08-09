"""Cached module URLs must carry the file's mtime, not a hand-written token.

Browsers cache ES modules by full URL. The pages import them with a `?v=`
token, so a token that never changes pins every client to whatever it fetched
first — a frontend fix can be committed, served correctly by the server, and
still not be running in a single browser. That happened: a tip-box rendering
fix looked completely inert until the token moved.

No-cache headers on the HTML do not help, because the module URL is cached
independently of the page that names it.
"""

from __future__ import annotations

import os

from pybravo.web.server import _version_static_assets


def _frontend(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.js").write_text("// main", encoding="utf-8")
    (src / "robot-scene.js").write_text("// scene", encoding="utf-8")
    return tmp_path


def test_token_becomes_the_files_mtime(tmp_path):
    root = _frontend(tmp_path)
    os.utime(root / "src" / "main.js", (1_700_000_000, 1_700_000_000))
    html = '<script type="module" src="/static/src/main.js?v=dev"></script>'

    assert "main.js?v=1700000000" in _version_static_assets(html, root)


def test_the_token_moves_when_the_file_changes(tmp_path):
    root = _frontend(tmp_path)
    html = "import { RobotScene } from '/static/src/robot-scene.js?v=dev';"

    os.utime(root / "src" / "robot-scene.js", (1_700_000_000, 1_700_000_000))
    before = _version_static_assets(html, root)
    os.utime(root / "src" / "robot-scene.js", (1_700_000_999, 1_700_000_999))
    after = _version_static_assets(html, root)

    assert before != after, (
        "editing a module must change its URL, or browsers keep the old copy"
    )


def test_every_asset_in_a_page_is_rewritten(tmp_path):
    root = _frontend(tmp_path)
    html = (
        '<script type="module" src="/static/src/main.js?v=init3"></script>\n'
        "import { RobotScene } from '/static/src/robot-scene.js?v=stickytargets1';"
    )
    out = _version_static_assets(html, root)

    assert "init3" not in out and "stickytargets1" not in out


def test_an_unknown_file_keeps_its_original_token(tmp_path):
    """Never invent a version for a path we cannot stat — that breaks the URL."""
    root = _frontend(tmp_path)
    html = '<script src="/static/src/does-not-exist.js?v=keepme"></script>'

    assert "does-not-exist.js?v=keepme" in _version_static_assets(html, root)


def test_urls_without_a_token_are_left_alone(tmp_path):
    """The rewrite is opt-in: a URL with no ?v= is not given one."""
    root = _frontend(tmp_path)
    html = '<script src="/static/src/main.js"></script>'

    assert _version_static_assets(html, root) == html


def test_the_shipped_pages_still_expose_a_token_to_rewrite():
    """Guard the coupling: the rewrite only works if the token is present."""
    from pathlib import Path

    frontend = Path(__file__).resolve().parents[1] / "frontend"
    assert "main.js?v=" in (frontend / "index.html").read_text(encoding="utf-8")
    assert "robot-scene.js?v=" in (frontend / "designer.html").read_text(encoding="utf-8")
