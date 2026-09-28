import re
from .model import Unit
from .tree import LIST, SEPARATOR

ABBREVIATIONS = {
    "v.v",
    "ts",
    "ths",
    "pgs",
    "gs",
    "tp",
    "tt",
    "đh",
    "e.g",
    "i.e",
    "etc",
    "vs",
    "fig",
    "no",
    "pp",
    "tr",
}


def sentences(text):
    start = 0
    for match in re.finditer(r'[.!?…]["\')\]»]*\s+', text):
        following = text[match.end() :]
        first = next((c for c in following if c.isalnum()), "")
        word = text[: match.start()].split()[-1:] or [""]
        if not (first.isupper() or first.isdigit()) or word[0].casefold() in ABBREVIATIONS:
            continue
        if (
            re.search(r"\b\d+(?:\.\d+)+$", text[: match.start()])
            or text[max(0, match.start() - 1) : match.start()] == "."
        ):
            continue
        yield text[start : match.end()].strip()
        start = match.end()
    if text[start:].strip():
        yield text[start:].strip()


def split_bounded(text, counter, budget, prefix=""):
    """Preserve every character, splitting words only when one cannot fit."""
    remaining = text
    while remaining:
        if len(remaining) <= budget * 8 and counter.count(prefix + remaining) <= budget:
            yield remaining
            return
        # Character window is bounded before tokenization even for huge input.
        hi = min(len(remaining), max(1, budget * 8))
        lo = 0
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if counter.count(prefix + remaining[:mid]) <= budget:
                lo = mid
            else:
                hi = mid - 1
        if lo == 0:
            raise ValueError("Token budget cannot fit a source character and its table header")
        cut = lo
        boundary = list(re.finditer(r"\s+", remaining[:lo]))
        if boundary:
            cut = boundary[-1].end()
        yield remaining[:cut]
        remaining = remaining[cut:]


def segment(block, counter, cfg):
    text = block.text
    if counter.count(text) <= cfg.max_tokens:
        return [
            Unit(
                text,
                block,
                glue_next=text.rstrip().endswith(":")
                or bool(re.match(r"^(Bảng|Hình|Table|Figure|Biểu đồ)\s*\d+[.\-\d]*\s*[:.]", text)),
            )
        ]
    if block.kind == "table":
        lines = text.splitlines()
        n = 2 if len(lines) > 1 and SEPARATOR.fullmatch(lines[1]) else 1
        header, rows = "\n".join(lines[:n]), lines[n:]
        # An oversized header cannot be repeated intact under a hard cap.
        # Preserve it once and split the table as raw lines in this rare case.
        if counter.count(header + "\n") > cfg.max_tokens - 8 or not rows:
            return [Unit(part, block, "") for part in split_bounded(text, counter, cfg.max_tokens)]
        return [
            Unit(part, block, "\n", header)
            for row in rows
            for part in split_bounded(row, counter, cfg.max_tokens, header + "\n")
        ]
    if block.kind == "code":
        pieces = text.splitlines(keepends=True)
        joiner = ""
    elif block.kind == "list":
        pieces = []
        for line in text.splitlines(keepends=True):
            if pieces and not LIST.match(line):
                pieces[-1] += line
            else:
                pieces.append(line)
        joiner = ""
    else:
        pieces = list(sentences(text))
        joiner = " "
    return [
        Unit(part, block, joiner if i == 0 else "")
        for piece in pieces
        for i, part in enumerate(split_bounded(piece, counter, cfg.max_tokens))
    ]
