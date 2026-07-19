"""Anchor resolution: locating the span of source a memory actually depends on.

Pure string operations. Reading files is the caller's job.

An anchor is either a line span (``L40-L88``) or a symbol name (``rotate_token``).
A ``path::`` prefix is tolerated and stripped, since callers naturally write
``services/auth.py::rotate_token`` even though the path is stored separately.

The symbol finder is regex-based and knowingly lossy — see ``find_symbol_span``.
"""

from __future__ import annotations

import hashlib
import re

LINE_SPAN_RE = re.compile(r"^L(\d+)-L(\d+)$")

# Tried in order; the first match wins. The generic C-family method pattern is
# last because it is the most eager and would otherwise shadow better matches.
_SYMBOL_PATTERNS: tuple[str, ...] = (
    r"^[ \t]*(?:async[ \t]+)?def[ \t]+{name}\b",
    r"^[ \t]*class[ \t]+{name}\b",
    r"^[ \t]*(?:export[ \t]+)?(?:default[ \t]+)?(?:async[ \t]+)?function[ \t]*\*?[ \t]*{name}\b",
    r"^[ \t]*func[ \t]+(?:\([^)]*\)[ \t]*)?{name}\b",
    r"^[ \t]*(?:pub[ \t]+)?(?:async[ \t]+)?fn[ \t]+{name}\b",
    r"^[ \t]*(?:export[ \t]+)?(?:type|interface|struct|enum|trait|impl)[ \t]+{name}\b",
    r"^[ \t]*(?:export[ \t]+)?(?:const|let|var)[ \t]+{name}[ \t]*=",
    r"^[ \t]*(?:[\w<>\[\],.]+[ \t]+)+{name}[ \t]*\(",
)


def strip_path_prefix(anchor: str) -> str:
    """Drop a leading ``path::`` from an anchor, if present."""
    _, sep, tail = anchor.rpartition("::")
    return tail if sep else anchor


def resolve_anchor(source: str, anchor: str) -> tuple[int, int] | None:
    """Resolve an anchor to a 1-indexed inclusive line span, or None if not found."""
    anchor = strip_path_prefix(anchor).strip()

    match = LINE_SPAN_RE.match(anchor)
    if match:
        start, end = int(match.group(1)), int(match.group(2))
        if start < 1 or end < start:
            return None
        total = len(source.splitlines())
        if start > total:
            return None
        return start, min(end, total)

    return find_symbol_span(source, anchor)


def find_symbol_span(source: str, name: str) -> tuple[int, int] | None:
    """Find a symbol definition and the extent of its body.

    Deliberately regex-based for a first pass. It will be wrong on braces inside
    string literals, and on languages whose declaration syntax is not covered
    above. When it is wrong it fails toward "not found", which surfaces as a
    suspect memory rather than a silently accepted stale one.
    """
    if not name:
        return None

    lines = source.splitlines()
    escaped = re.escape(name)

    for pattern in _SYMBOL_PATTERNS:
        regex = re.compile(pattern.format(name=escaped))
        for index, line in enumerate(lines):
            if regex.match(line):
                return index + 1, _block_end(lines, index) + 1

    return None


def extract_span(source: str, span: tuple[int, int]) -> str:
    """Return the text of a 1-indexed inclusive line span."""
    start, end = span
    return "\n".join(source.splitlines()[start - 1 : end])


def hash_text(text: str) -> str:
    """Content hash of a span. Not a git object hash — spans are not blobs."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _block_end(lines: list[str], start: int) -> int:
    """Index of the last line of the block opened at ``start``."""
    header = lines[start]

    if "{" in header:
        depth = 0
        opened = False
        for index in range(start, len(lines)):
            for char in lines[index]:
                if char == "{":
                    depth += 1
                    opened = True
                elif char == "}":
                    depth -= 1
            if opened and depth <= 0:
                return index
        return len(lines) - 1

    base = _indent_width(header)
    end = start
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if not line.strip():
            continue
        if _indent_width(line) <= base:
            break
        end = index
    return end


def _indent_width(line: str) -> int:
    return len(line) - len(line.lstrip())
