"""Structural v2: exact token budgets, heading ancestry, deterministic IDs."""

from dataclasses import replace
import hashlib
import json
import uuid

from document_chunk.domain.entities.chunk import Chunk, ChunkMetadata
from document_chunk.domain.exceptions import ChunkError
from document_chunk.domain.ports.chunker import IChunker
from document_chunk.shared.result import Err, Ok
from document_chunk.shared.logger import get_logger
from .model import render
from .pack import pack, consolidate, common_prefix
from .segment import segment, sentences
from .tokens import TokenCounter
from .tree import build_nodes

NAMESPACE = uuid.UUID("a0f46668-b42c-50f0-909a-b03a6dc1818f")
VERSION = "structural-v2"
logger = get_logger(__name__)


class StructuralChunker(IChunker):
    def __init__(self, config, counter=None):
        self.config = config
        self.counter = counter if counter is not None else TokenCounter(config)
        self.stats = {}

    @property
    def supported_types(self):
        from document_chunk.domain.entities.document import DocumentType

        return tuple(DocumentType)

    def chunk(self, document):
        try:
            nodes, removed = build_nodes(document, self.counter, self.config)
            drafts = []
            for node in nodes:
                units = []
                for block in node.blocks:
                    block_units = segment(block, self.counter, self.config)
                    if block_units:
                        block_units[0].joiner = "\n\n"
                        for continuation in block_units[1:]:
                            continuation.block = replace(block, images=())
                    units.extend(block_units)
                drafts.extend(pack(node, units, self.counter, self.config))
            drafts, merges = consolidate(drafts, self.counter, self.config)
            chunks = []
            ancestors = {node.path: node for node in nodes}
            cfg = self.config
            for i, draft in enumerate(drafts):
                path = draft.runs[0].node.path
                for run in draft.runs[1:]:
                    path = common_prefix(path, run.node.path)
                if path != draft.path:
                    raise ValueError(
                        "Chunk heading path is not the common ancestor of its source runs"
                    )
                content = render(draft)
                count = self.counter.count(content)
                breadcrumb = self.counter.prefix(" > ".join(path), cfg.header_max_tokens)
                overlap = ""
                if i and draft.part_index and len(draft.runs) == 1:
                    previous = drafts[i - 1]
                    left, right = previous.runs[-1], draft.runs[0]
                    if (
                        left.node is right.node
                        and left.units[-1].block.kind == right.units[0].block.kind == "text"
                    ):
                        for sentence in reversed(list(sentences(render(previous)))):
                            # A trailing fragment is not a whole sentence.
                            if not sentence.rstrip().endswith((".", "!", "?", "…")):
                                break
                            candidate = " ".join(filter(None, (sentence, overlap)))
                            if self.counter.count(candidate) > cfg.overlap_tokens:
                                break
                            overlap = candidate
                embedding = "\n\n".join(filter(None, (breadcrumb, overlap, content)))
                # Exact final count also includes separator tokenization.
                if self.counter.count(embedding) > cfg.embed_max_tokens - 2:
                    embedding = "\n\n".join(filter(None, (breadcrumb, content)))
                if self.counter.count(embedding) > cfg.embed_max_tokens - 2:
                    embedding = content
                if (
                    count > cfg.max_tokens
                    or self.counter.count(embedding) > cfg.embed_max_tokens - 2
                ):
                    raise ValueError("Structural chunk exceeds exact token budget")
                units = [u for run in draft.runs for u in run.units]
                pages = [u.block.page for u in units if u.block.page is not None]
                kinds = {u.block.kind for u in units}
                digest = hashlib.md5(content.encode()).hexdigest()
                # Include structure as well as body: heading edits must invalidate IDs.
                identity = json.dumps(
                    [document.document.id, VERSION, i, path, digest], ensure_ascii=False
                )
                ancestor = ancestors.get(path, nodes[0])
                section_id = str(
                    uuid.uuid5(
                        NAMESPACE,
                        json.dumps(
                            [document.document.id, path, ancestor.ordinal], ensure_ascii=False
                        ),
                    )
                )
                chunks.append(
                    Chunk(
                        id=str(uuid.uuid5(NAMESPACE, identity)),
                        content=content,
                        embedding_input=embedding,
                        content_hash=digest,
                        images=list(
                            dict.fromkeys(image for u in units for image in u.block.images)
                        ),
                        metadata=ChunkMetadata(
                            document_id=document.document.id,
                            document_name=document.document.name,
                            document_type=document.document.doc_type,
                            chunk_index=i,
                            heading_path=path,
                            heading_level=ancestor.level,
                            section_id=section_id,
                            section_title=path[-1] if path else None,
                            parent_section=path[-2] if len(path) > 1 else None,
                            page_number=min(pages) if pages else None,
                            page_start=min(pages) if pages else None,
                            page_end=max(pages) if pages else None,
                            part_index=draft.part_index,
                            part_count=draft.part_count,
                            content_type=next(iter(kinds)) if len(kinds) == 1 else "mixed",
                            token_count=count,
                            char_count=len(content),
                            chunker_version=VERSION,
                            language=document.language,
                            course_id=document.metadata.get("course_id"),
                            owner_id=document.metadata.get("owner_id"),
                        ),
                    )
                )
            # Consolidation can absorb a child's first part into its parent.
            # Describe the final emitted section groups, not stale draft counts.
            groups = {}
            for chunk in chunks:
                groups.setdefault(chunk.metadata.section_id, []).append(chunk)
            for group in groups.values():
                for part_index, chunk in enumerate(group):
                    chunk.metadata = replace(chunk.metadata, part_index=part_index, part_count=len(group))
            self.stats = {"toc_blocks_removed": removed, "lcp_merges": merges}
            logger.info("structural.chunked", chunks=len(chunks), **self.stats)
            return Ok(chunks)
        except Exception as exc:
            logger.error("structural.failed", error=str(exc))
            return Err(ChunkError("Structural chunking failed", cause=exc))


def build_chunker(config, embedder_config=None):
    if config.strategy == "heading":
        from document_chunk.adapters.chunkers.heading_chunker import HeadingChunker

        return HeadingChunker(config)
    if embedder_config is not None:
        if (
            embedder_config.provider not in ("bge", "grpc")
            or config.tokenizer != embedder_config.bge_model
        ):
            raise ValueError(
                "Structural chunking requires the configured BGE tokenizer to match the embedder"
            )
        if config.embed_max_tokens != embedder_config.max_length:
            raise ValueError("Chunker embed_max_tokens must equal embedder max_length")
    return StructuralChunker(config)
