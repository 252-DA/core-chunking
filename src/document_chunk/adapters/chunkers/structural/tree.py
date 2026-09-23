"""Normalize and build ordered section nodes with a heading stack."""

import re
import unicodedata

from document_chunk.domain.entities.document import DocumentType, ElementType, Section
from .model import Block, Node

SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
LIST = re.compile(r"^\s*(?:[-*•+▪–]|\d+[.)]|[a-zđ][.)])\s+")
TOC = re.compile(
    r"^(?:mục lục|table of contents|contents|danh mục hình|danh mục bảng|list of figures|list of tables)\s*[:.]?$",
    re.I,
)


def toc_flags(sections):
    active = False
    for index, sec in enumerate(sections):
        lines = [line for line in sec.content.splitlines() if line.strip()]
        title_text = (
            (sec.heading or sec.content) if sec.element_type == ElementType.HEADING else sec.content
        )
        title = len(title_text) <= 60 and bool(TOC.fullmatch(title_text.strip()))
        structure = bool(re.fullmatch(r"toc\s+[1-9]", sec.metadata.get("style", ""), re.I))
        if lines:
            structure |= (
                sum(bool(re.search(r"\.{3,}\s*\d+\s*$", x)) for x in lines) / len(lines) >= 0.4
            )
            structure |= (
                sum(bool(re.search(r"(?:\t| {2,})\d+\s*$", x)) for x in lines) / len(lines) >= 0.5
            )
        active = bool(
            sec.is_toc
            or (structure and active)
            or ((title or structure) and (title or index < max(1, len(sections) * 0.2)))
        )
        yield active


def prepare_sections(parsed, counter):
    if parsed.document.doc_type != DocumentType.PPTX:
        return parsed.sections
    sections = []
    title = parsed.metadata.get("title")
    if not title and parsed.sections and counter.count(parsed.sections[0].content) <= 12:
        title = parsed.sections[0].heading
    offset = 1 if title else 0
    if title:
        sections.append(Section(title, ElementType.HEADING, title, 1))
    has_divider = False
    last_title = None
    for sec in parsed.sections:
        heading = re.sub(
            r"\s*(?:\((?:tiếp|cont\.|2)\)|continued)$", "", sec.heading or "", flags=re.I
        ).strip()
        divider = bool(heading) and counter.count(sec.content) <= 12
        if heading and heading != last_title and heading != title:
            level = 1 + offset if divider or not has_divider else 2 + offset
            sections.append(Section(heading, ElementType.HEADING, heading, level, sec.page_number))
        if divider:
            has_divider = True
        last_title = heading or last_title
        blocks = sec.metadata.get("blocks")
        if blocks is None:
            blocks = [{"kind": "text", "text": sec.content}] if sec.content != sec.heading else []
        for i, block in enumerate(blocks):
            kind = {
                "table": ElementType.TABLE,
                "list": ElementType.LIST,
                "code": ElementType.CODE,
            }.get(block["kind"], ElementType.PARAGRAPH)
            sections.append(
                Section(
                    block["text"],
                    kind,
                    page_number=sec.page_number,
                    images=sec.images if i == 0 else (),
                    metadata={"kind": block["kind"]},
                )
            )
        if not blocks and sec.images:
            sections.append(
                Section("", ElementType.IMAGE, page_number=sec.page_number, images=sec.images)
            )
    return sections


def build_nodes(parsed, counter, config):
    root = Node((), 0, 0)
    nodes, stack = [root], [root]
    removed = 0
    sections = prepare_sections(parsed, counter)
    for index, (sec, is_toc) in enumerate(zip(sections, toc_flags(sections))):
        if is_toc and not config.index_toc:
            removed += 1
            continue
        text = unicodedata.normalize("NFC", sec.content.replace("\r\n", "\n"))
        raw_heading = sec.heading or text
        title = " ".join(unicodedata.normalize("NFC", sec.heading or text).split()).rstrip(".:")
        heading = sec.element_type in (ElementType.HEADING, ElementType.SLIDE) and bool(title)
        if parsed.document.doc_type == DocumentType.PDF and (
            len(title) > 200
            or len(title.split()) > 25
            or (raw_heading.rstrip().endswith((".", ";", ",")) and len(title.split()) > 8)
        ):
            heading = False
        if heading and not is_toc:
            level = max(1, sec.heading_level)
            if sec.element_type == ElementType.SLIDE:
                title = re.sub(
                    r"\s*(?:\((?:tiếp|cont\.|2)\)|continued)$", "", title, flags=re.I
                ).strip()
            repeated = next(
                (n for n in stack[1:] if n.level == level and n.path[-1] == title), None
            )
            if repeated is None:
                while stack[-1].level >= level:
                    stack.pop()
                node = Node(stack[-1].path + (title,), level, len(nodes))
                nodes.append(node)
                stack.append(node)
            if sec.element_type == ElementType.HEADING:
                raw_title = unicodedata.normalize("NFC", sec.heading or sec.content)
                text = text[len(raw_title) :].strip() if text.startswith(raw_title) else text
            elif text.strip() == (sec.heading or "").strip():
                text = ""
        node = stack[-1]
        if not text.strip() or (
            sec.element_type == ElementType.IMAGE and text.strip() == "[Image]"
        ):
            if sec.images:
                node.blocks.append(Block("", "image", sec.page_number, sec.images, index))
            continue
        kind = {ElementType.TABLE: "table", ElementType.LIST: "list", ElementType.CODE: "code"}.get(
            sec.element_type, "text"
        )
        lines = text.splitlines()
        if kind == "text":
            if len(lines) > 1 and SEPARATOR.fullmatch(lines[1]):
                kind = "table"
            elif len(lines) > 1 and sum(bool(LIST.match(x)) for x in lines) >= len(lines) * 0.6:
                kind = "list"
        if kind == "text":
            if parsed.document.doc_type == DocumentType.PDF:
                text = re.sub(r"([a-zà-ỹ])-\n([a-zà-ỹ])", r"\1\2", text)
            text = "\n\n".join(" ".join(p.split()) for p in text.split("\n\n")).strip()
        else:
            text = text.strip("\n")
        node.blocks.append(
            Block(text, "toc" if is_toc else kind, sec.page_number, sec.images, index)
        )
    # Image-only blocks attach to the nearest text block inside their own node.
    for node in nodes:
        for i, block in enumerate(node.blocks):
            if block.kind != "image":
                continue
            candidates = [b for b in node.blocks[:i] if b.text]
            following = [b for b in node.blocks[i + 1 :] if b.text]
            target = candidates[-1] if candidates else following[0] if following else None
            if target:
                target.images = tuple(dict.fromkeys(target.images + block.images))
        node.blocks = [b for b in node.blocks if b.text]
    return nodes, removed
