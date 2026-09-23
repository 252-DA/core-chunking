"""Pack inside a node and consolidate only neighbouring, compatible paths."""

from math import ceil
from .model import Draft, Run, render


def pack(node, units, counter, cfg):
    if not units:
        return []
    total = counter.count(render(Draft(node.path, [Run(node, units)])))
    per = (
        total / max(1, ceil(total / cfg.target_tokens))
        if total > cfg.max_tokens
        else cfg.max_tokens
    )
    drafts, current = [], []
    for unit in units:
        candidate = Draft(node.path, [Run(node, current + [unit])])
        text = render(candidate)
        size = counter.count(render(Draft(node.path, [Run(node, current)]))) if current else 0
        # TOC is always isolated, including when explicitly retained for inspection.
        isolate = current and ((current[-1].block.kind == "toc") != (unit.block.kind == "toc"))
        natural = (
            unit.block.source != current[-1].block.source or unit.joiner == "\n"
            if current
            else False
        )
        soft_cut = size >= per and (natural or size >= per * cfg.oversize_tolerance)
        if current and (
            isolate
            or counter.count(text) > cfg.max_tokens
            or (soft_cut and not current[-1].glue_next)
        ):
            drafts.append(Draft(node.path, [Run(node, current, not drafts)]))
            current = []
        current.append(unit)
        if counter.count(render(Draft(node.path, [Run(node, current)]))) > cfg.max_tokens:
            raise ValueError("Atomic unit exceeds content token budget")
    if current:
        drafts.append(Draft(node.path, [Run(node, current, not drafts)]))
    if len(drafts) > 1 and counter.count(render(drafts[-1])) < cfg.min_tokens:
        prev, last = drafts[-2:]
        combined = Draft(
            node.path,
            [Run(node, prev.runs[0].units + last.runs[0].units, prev.runs[0].starts_node)],
        )
        if (
            not any(u.block.kind == "toc" for d in (prev, last) for u in d.runs[0].units)
            and counter.count(render(combined)) <= cfg.max_tokens
        ):
            drafts[-2:] = [combined]
    for i, draft in enumerate(drafts):
        draft.part_index, draft.part_count = i, len(drafts)
    return drafts


def common_prefix(a, b):
    length = 0
    for x, y in zip(a, b):
        if x != y:
            break
        length += 1
    return a[:length]


def consolidate(drafts, counter, cfg):
    # Revisit only the changed neighbourhood; each merge reduces draft count.
    out = list(drafts)
    merges = 0
    i = 0
    while i < len(out):
        d = out[i]
        if counter.count(render(d)) >= cfg.min_tokens:
            i += 1
            continue
        choices = []
        if i + 1 < len(out) and out[i + 1].part_count == 1:
            choices.append(i + 1)
        if i and out[i - 1].part_count == 1:
            choices.append(i - 1)
        if (
            i + 1 < len(out)
            and out[i + 1].part_index == 0
            and len(d.path) < len(out[i + 1].path)
            and out[i + 1].path[: len(d.path)] == d.path
        ):
            choices.append(i + 1)
        merged = False
        for j in choices:
            a, b = sorted((i, j))
            x, y = out[a], out[b]
            if any(
                u.block.kind == "toc" for draft in (x, y) for run in draft.runs for u in run.units
            ):
                continue
            path = common_prefix(x.path, y.path)
            if not path and (x.path or y.path) and not cfg.merge_across_top_level:
                continue
            if len(path) < min(len(x.path), len(y.path)) - 1:
                continue
            candidate = Draft(path, x.runs + y.runs)
            if counter.count(render(candidate)) > cfg.target_tokens:
                continue
            out[a : b + 1] = [candidate]
            merges += 1
            i = max(0, a - 1)
            merged = True
            break
        if not merged:
            i += 1
    return out, merges
