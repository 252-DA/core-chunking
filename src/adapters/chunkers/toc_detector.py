"""
TocDetector — rule-based heuristic detector for Table of Contents sections.

Scoring per section (threshold ≥ 2 to start a TOC block, ≥ 1 to continue):
  +2  keyword match in heading or first 100 chars ("Mục lục", "Table of Contents", …)
  +2  ≥ 40% of non-empty lines match dot-leader + page pattern  (.....12)
  +1  ≥ 20% of non-empty lines match dot-leader + page pattern
  +1  position within first 20% of document
  +1  high short-line density (≥ 70% lines < 80 chars, ≥ 5 lines)
"""
import re
from dataclasses import replace

from src.domain.entities.document import Section

_TOC_KEYWORDS = frozenset({
    "mục lục", "table of contents", "danh mục",
})
_DOT_PAGE_RE = re.compile(r"\.{3,}\s*\d+\s*$")

_ANCHOR_THRESHOLD = 2
_CONTINUE_THRESHOLD = 2
_LOOKAHEAD = 5  # extra sections to scan past the 25% cutoff, handles late-starting TOC


def detect_toc(sections: list[Section]) -> set[int]:
    """Return set of section indices that belong to the Table of Contents."""
    toc_indices: set[int] = set()
    total = len(sections)
    if total == 0:
        return toc_indices

    cutoff = max(3, int(total * 0.25))
    in_toc = False

    for i, sec in enumerate(sections):
        if i > cutoff + _LOOKAHEAD and not in_toc:
            break

        score = _score(sec, i, total)

        if in_toc:
            if score >= _CONTINUE_THRESHOLD:
                toc_indices.add(i)
            else:
                in_toc = False
        elif score >= _ANCHOR_THRESHOLD:
            toc_indices.add(i)
            in_toc = True

    return toc_indices


def annotate_toc(sections: list[Section]) -> list[Section]:
    """Return new list[Section] with is_toc=True on detected TOC sections.
    Returns the original list unchanged if no TOC is found."""
    indices = detect_toc(sections)
    if not indices:
        return sections
    return [
        replace(sec, is_toc=True) if i in indices else sec
        for i, sec in enumerate(sections)
    ]


def _score(sec: Section, idx: int, total: int) -> int:
    score = 0
    heading = (sec.heading or "").lower()
    text = sec.content.strip()

    # Heuristic 4: keyword — check heading first, then only short content (likely title paragraph)
    if any(kw in heading for kw in _TOC_KEYWORDS):
        score += 2
    elif len(text) <= 60 and any(kw in text.lower() for kw in _TOC_KEYWORDS):
        score += 2

    # Heuristic 3: position in first 20% of document
    if total > 0 and idx / total <= 0.20:
        score += 1

    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return score

    # Heuristic 1: dot-leader + page number
    dot_matches = sum(1 for ln in lines if _DOT_PAGE_RE.search(ln))
    ratio = dot_matches / len(lines)
    if ratio >= 0.40:
        score += 2
    elif ratio >= 0.20:
        score += 1

    # Heuristic 2: high heading density (mostly short lines, few real paragraphs)
    short_lines = sum(1 for ln in lines if len(ln.strip()) < 80)
    if len(lines) >= 5 and short_lines / len(lines) >= 0.70:
        score += 1

    return score
