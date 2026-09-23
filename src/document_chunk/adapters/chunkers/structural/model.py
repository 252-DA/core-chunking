from dataclasses import dataclass, field


@dataclass
class Block:
    text: str
    kind: str
    page: int | None
    images: tuple[str, ...]
    source: int


@dataclass
class Node:
    path: tuple[str, ...]
    level: int
    ordinal: int
    blocks: list[Block] = field(default_factory=list)


@dataclass
class Unit:
    text: str
    block: Block
    joiner: str = "\n\n"
    header: str = ""
    glue_next: bool = False


@dataclass
class Run:
    node: Node
    units: list[Unit]
    starts_node: bool = True


@dataclass
class Draft:
    path: tuple[str, ...]
    runs: list[Run]
    part_index: int = 0
    part_count: int = 1


def render(draft: Draft) -> str:
    parts = []
    for run in draft.runs:
        extra = run.node.path[len(draft.path) :]
        if extra and run.starts_node:
            parts.append(" > ".join(extra))
        text = ""
        seen = set()
        for unit in run.units:
            # Tables with identical headers still have different identities.
            if unit.header and unit.block.source not in seen:
                text += ("\n\n" if text else "") + unit.header + "\n" + unit.text
                seen.add(unit.block.source)
            else:
                text += (unit.joiner if text else "") + unit.text
        parts.append(text)
    return "\n\n".join(parts)
