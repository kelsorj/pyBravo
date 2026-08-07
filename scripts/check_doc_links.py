#!/usr/bin/env python3
"""Verify that every relative markdown link in the repo points at a real file.

Two of these were broken at the first public launch (`docs/README.md` and
`NOTICE`, both linked from the README), so this runs in CI. External links are
not fetched — only repo-relative paths are checked.

Exits non-zero and prints GitHub Actions error annotations on failure.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# [text](target) — skip pure-anchor links and autolinks.
LINK = re.compile(r"\[[^\]]*\]\(\s*([^)\s]+)")
EXTERNAL = re.compile(r"^(https?:|mailto:|#)")


SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__"}


def tracked_markdown(repo: Path) -> list[Path]:
    """Markdown files to check, preferring git's view of what is tracked.

    Falls back to a filesystem walk so this still works in an exported tree or
    a tarball, rather than dying with a CalledProcessError.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "*.md"],
            capture_output=True,
            text=True,
            check=True,
            cwd=repo,
        ).stdout
        paths = [Path(line) for line in out.splitlines() if line]
        if paths:
            return paths
    except (OSError, subprocess.CalledProcessError):
        pass

    return [
        p.relative_to(repo)
        for p in repo.rglob("*.md")
        if not SKIP_DIRS.intersection(p.parts)
    ]


def main() -> int:
    repo = Path(__file__).resolve().parent.parent
    broken = 0

    for rel in tracked_markdown(repo):
        path = repo / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        for lineno, line in enumerate(text.splitlines(), start=1):
            for target in LINK.findall(line):
                if EXTERNAL.match(target):
                    continue
                # Strip any #fragment; we check the file, not the anchor.
                target = target.split("#", 1)[0]
                if not target:
                    continue
                if not (path.parent / target).exists():
                    print(
                        f"::error file={rel},line={lineno}::"
                        f"broken relative link -> {target}"
                    )
                    broken += 1

    if broken:
        print(f"\n{broken} broken link(s).", file=sys.stderr)
        return 1

    print("All relative markdown links resolve.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
