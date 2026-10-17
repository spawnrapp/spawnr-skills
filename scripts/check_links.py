#!/usr/bin/env python3
"""Dead-link check for this repo's Markdown (stdlib only, Python 3.9+).

Checks README.md, CHANGELOG.md and every skills/**/*.md. A relative link must
point at a file or folder that exists; a `#anchor` must match a heading in the
file it points at. http(s) and mailto links are not fetched. Fenced code blocks
are ignored, so an example link inside one is not checked.

    python3 scripts/check_links.py     # prints `file:line: target` per dead link; exit 1 if any
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FENCE = re.compile(r"```(?:\w+)?\n(.*?)```", re.DOTALL)
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)


def prose(text: str) -> str:
    """The text with fenced blocks blanked out; newlines stay so line numbers hold."""
    return FENCE.sub(lambda m: "\n" * m.group(0).count("\n"), text)


def slug(heading: str) -> str:
    # ponytail: GitHub numbers duplicate headings (-1, -2); this does not model that.
    return re.sub(r"[^\w\s-]", "", heading.lower()).replace(" ", "-")


def anchors(path: Path) -> set:
    return {slug(h) for h in HEADING.findall(prose(path.read_text(encoding="utf-8")))}


def dead_links(path: Path) -> list:
    dead = []
    for number, line in enumerate(prose(path.read_text(encoding="utf-8")).split("\n"), 1):
        for target in LINK.findall(line):
            if target.startswith(("http:", "https:", "mailto:")):
                continue
            file_part, _, anchor = target.partition("#")
            resolved = (path.parent / file_part).resolve() if file_part else path
            if not resolved.exists():
                dead.append((number, target))
            elif anchor and resolved.suffix == ".md" and anchor not in anchors(resolved):
                dead.append((number, target))
    return dead


def main() -> int:
    files = [ROOT / "README.md", ROOT / "CHANGELOG.md", *sorted((ROOT / "skills").rglob("*.md"))]
    found = 0
    for path in (f for f in files if f.exists()):
        for number, target in dead_links(path):
            print(f"{path.relative_to(ROOT)}:{number}: {target}")
            found += 1
    if not found:
        print("no dead links")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
