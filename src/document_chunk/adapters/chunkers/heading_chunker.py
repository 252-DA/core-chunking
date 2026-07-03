"""
HeadingChunker — chia ParsedDocument thành Chunks theo heading hierarchy.

Algorithm:
  1. Walk qua sections theo thứ tự
  2. Maintain heading path stack (breadcrumb)
  3. Gặp heading mới → tạo chunk mới
  4. Content quá lớn → split tại sentence boundary
  5. Chunk quá nhỏ → merge với chunk kế tiếp
"""
import hashlib
import re
import uuid
from dataclasses import dataclass, field, replace as dc_replace

from document_chunk.adapters.chunkers.toc_detector import annotate_toc
from document_chunk.domain.entities.chunk import Chunk, ChunkMetadata
from document_chunk.domain.entities.document import DocumentType, ElementType, ParsedDocument, Section
from document_chunk.domain.exceptions import ChunkError
from document_chunk.domain.ports.chunker import IChunker
from document_chunk.infrastructure.config import ChunkerConfig
from document_chunk.shared.logger import get_logger
from document_chunk.shared.result import Err, Ok, Result
from document_chunk.shared.tracing import get_tracer

logger = get_logger(__name__)
tracer = get_tracer(__name__)


@dataclass
class _ChunkBuffer:
    """Buffer tích lũy content trước khi tạo Chunk."""
    sections: list[Section] = field(default_factory=list)
    heading_path: list[str] = field(default_factory=list)
    heading_level: int = 0
    is_toc: bool = False

    @property
    def content(self) -> str:
        return "\n\n".join(s.content for s in self.sections if s.content.strip())

    @property
    def images(self) -> list[str]:
        imgs = []
        for s in self.sections:
            imgs.extend(s.images)
        return imgs

    @property
    def page_number(self) -> int | None:
        for s in self.sections:
            if s.page_number is not None:
                return s.page_number
        return None

    @property
    def size(self) -> int:
        return len(self.content)

    def is_empty(self) -> bool:
        return not self.content.strip()


class HeadingChunker(IChunker):
    """
    Chunk theo heading hierarchy — phù hợp cho PDF, DOCX, Markdown.
    PPTX: mỗi slide (ElementType.SLIDE) là một chunk riêng.
    """

    def __init__(self, config: ChunkerConfig) -> None:
        self._max_size = config.max_chunk_size
        self._min_size = config.min_chunk_size
        self._overlap = config.overlap_size

    @property
    def supported_types(self) -> tuple[DocumentType, ...]:
        return (
            DocumentType.PDF,
            DocumentType.DOCX,
            DocumentType.PPTX,
            DocumentType.MARKDOWN,
        )

    def chunk(self, doc: ParsedDocument) -> Result[list[Chunk], Exception]:
        with tracer.start_as_current_span("heading_chunker.chunk") as span:
            span.set_attribute("document.id", doc.document.id)
            span.set_attribute("doc_type", doc.document.doc_type.value)
            span.set_attribute("sections", len(doc.sections))
            try:
                if doc.document.doc_type == DocumentType.PPTX:
                    chunks = self._chunk_by_slides(doc)
                else:
                    annotated = annotate_toc(doc.sections)
                    if annotated is not doc.sections:
                        logger.debug("toc.detected", document_id=doc.document.id)
                        doc = dc_replace(doc, sections=annotated)
                    chunks = self._chunk_by_headings(doc)

                chunks = self._merge_small_chunks(chunks, doc.document.doc_type)

                span.set_attribute("chunks.count", len(chunks))
                logger.debug(
                    "chunker.done",
                    document_id=doc.document.id,
                    chunks=len(chunks),
                    doc_type=doc.document.doc_type.value,
                )
                return Ok(chunks)

            except Exception as e:
                logger.error("chunker.failed", document_id=doc.document.id, error=str(e))
                return Err(ChunkError(f"Failed to chunk document '{doc.document.id}'", cause=e))

    # ------------------------------------------------------------------
    # Strategy 1: Heading-based (PDF, DOCX, Markdown)
    # ------------------------------------------------------------------

    def _chunk_by_headings(self, doc: ParsedDocument) -> list[Chunk]:
        chunks: list[Chunk] = []
        buffer = _ChunkBuffer()
        heading_stack: list[tuple[int, str]] = []  # (level, text)
        chunk_index = 0

        for section in doc.sections:
            # --- TOC section: accumulate into a dedicated TOC buffer ---
            if section.is_toc:
                if not buffer.is_toc and not buffer.is_empty():
                    new_chunks = self._flush_buffer(buffer, doc, chunk_index)
                    chunks.extend(new_chunks)
                    chunk_index += len(new_chunks)
                    buffer = _ChunkBuffer(is_toc=True)
                buffer.sections.append(section)
                if buffer.size > self._max_size:
                    new_chunks = self._flush_buffer(buffer, doc, chunk_index)
                    chunks.extend(new_chunks)
                    chunk_index += len(new_chunks)
                    buffer = _ChunkBuffer(is_toc=True)
                continue

            # --- First non-TOC section after a TOC block: flush TOC buffer ---
            if buffer.is_toc and not buffer.is_empty():
                new_chunks = self._flush_buffer(buffer, doc, chunk_index)
                chunks.extend(new_chunks)
                chunk_index += len(new_chunks)
                buffer = _ChunkBuffer()

            # --- Normal heading/content logic ---
            if section.element_type == ElementType.HEADING and section.heading_level > 0:
                # Flush buffer trước khi bắt đầu heading mới
                if not buffer.is_empty():
                    new_chunks = self._flush_buffer(buffer, doc, chunk_index)
                    chunks.extend(new_chunks)
                    chunk_index += len(new_chunks)

                # Update heading stack
                heading_stack = [
                    (lvl, txt) for lvl, txt in heading_stack
                    if lvl < section.heading_level
                ]
                heading_text = section.heading or section.content.strip()
                heading_stack.append((section.heading_level, heading_text))

                # Reset buffer với heading mới
                buffer = _ChunkBuffer(
                    heading_path=[txt for _, txt in heading_stack],
                    heading_level=section.heading_level,
                )

                # Heading text cũng là content nếu có body
                if section.content.strip() and section.content.strip() != heading_text:
                    buffer.sections.append(section)

            else:
                # Content section — thêm vào buffer
                buffer.sections.append(section)

                # Nếu buffer quá lớn → flush ngay
                if buffer.size > self._max_size:
                    new_chunks = self._flush_buffer(buffer, doc, chunk_index)
                    chunks.extend(new_chunks)
                    chunk_index += len(new_chunks)
                    buffer = _ChunkBuffer(
                        heading_path=buffer.heading_path[:],
                        heading_level=buffer.heading_level,
                    )

        # Flush phần còn lại
        if not buffer.is_empty():
            new_chunks = self._flush_buffer(buffer, doc, chunk_index)
            chunks.extend(new_chunks)

        return chunks

    # ------------------------------------------------------------------
    # Strategy 2: Slide-based (PPTX)
    # ------------------------------------------------------------------

    def _chunk_by_slides(self, doc: ParsedDocument) -> list[Chunk]:
        chunks: list[Chunk] = []

        for idx, section in enumerate(doc.sections):
            if not section.content.strip():
                continue

            heading = section.heading or f"Slide {idx + 1}"
            content = section.content

            # Slide quá lớn → split
            sub_contents = self._split_text(content)
            for sub_idx, sub_content in enumerate(sub_contents):
                chunks.append(self._make_chunk(
                    content=sub_content,
                    heading_path=[heading],
                    heading_level=1,
                    images=list(section.images) if sub_idx == 0 else [],
                    page_number=section.page_number,
                    doc=doc,
                    chunk_index=len(chunks),
                ))

        return chunks

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _flush_buffer(
        self, buffer: _ChunkBuffer, doc: ParsedDocument, start_index: int
    ) -> list[Chunk]:
        """Flush buffer → list[Chunk], split nếu quá lớn."""
        content = buffer.content
        if not content.strip():
            return []

        sub_contents = self._split_text(content)
        chunks = []
        for sub_idx, sub_content in enumerate(sub_contents):
            chunks.append(self._make_chunk(
                content=sub_content,
                heading_path=buffer.heading_path[:],
                heading_level=buffer.heading_level,
                images=buffer.images if sub_idx == 0 else [],
                page_number=buffer.page_number,
                doc=doc,
                chunk_index=start_index + sub_idx,
                is_toc=buffer.is_toc,
            ))
        return chunks

    def _make_chunk(
        self,
        content: str,
        heading_path: list[str],
        heading_level: int,
        images: list[str],
        page_number: int | None,
        doc: ParsedDocument,
        chunk_index: int,
        is_toc: bool = False,
    ) -> Chunk:
        embedding_input = self._enrich_content(content, heading_path)
        course_id = self._metadata_value(doc.metadata, "course_id")
        owner_id = self._metadata_value(doc.metadata, "owner_id")
        return Chunk(
            id=str(uuid.uuid4()),
            content=content,
            embedding_input=embedding_input,
            content_hash=hashlib.md5(content.encode()).hexdigest(),
            metadata=ChunkMetadata(
                document_id=doc.document.id,
                document_name=doc.document.name,
                document_type=doc.document.doc_type,
                chunk_index=chunk_index,
                heading_path=tuple(heading_path),
                heading_level=heading_level,
                page_number=page_number,
                language=doc.language,
                course_id=course_id,
                owner_id=owner_id,
                content_type="toc" if is_toc else None,
                importance_score=0.1 if is_toc else None,
            ),
            images=images,
        )

    def _split_text(self, text: str) -> list[str]:
        """
        Split text tại sentence boundary nếu vượt max_size.
        Giữ overlap giữa các chunks liên tiếp.
        """
        if len(text) <= self._max_size:
            return [text]

        # Split tại dấu câu (. ! ? \n\n)
        sentences = re.split(r'(?<=[.!?])\s+|(?<=\n)\n', text)
        chunks: list[str] = []
        current = ""

        for sentence in sentences:
            if len(current) + len(sentence) > self._max_size and current:
                chunks.append(current.strip())
                # Overlap: lấy phần cuối của chunk trước
                overlap_text = current[-self._overlap:] if self._overlap else ""
                current = overlap_text + sentence
            else:
                current += (" " if current else "") + sentence

        if current.strip():
            chunks.append(current.strip())

        return chunks if chunks else [text]

    def _merge_small_chunks(
        self, chunks: list[Chunk], doc_type: DocumentType
    ) -> list[Chunk]:
        """
        Merge các chunks nhỏ hơn min_size với chunk kế tiếp
        nếu cùng heading parent.
        PPTX: không merge — mỗi slide là độc lập.
        """
        if doc_type == DocumentType.PPTX or len(chunks) <= 1:
            return chunks

        merged: list[Chunk] = []
        i = 0

        while i < len(chunks):
            current = chunks[i]

            if len(current.content) < self._min_size and i + 1 < len(chunks):
                next_chunk = chunks[i + 1]
                same_parent = (
                    current.metadata.heading_path[:-1]
                    == next_chunk.metadata.heading_path[:-1]
                )
                combined_size = len(current.content) + len(next_chunk.content)

                if same_parent and combined_size <= self._max_size:
                    merged_content = current.content + "\n\n" + next_chunk.content
                    merged.append(dc_replace(
                        current,
                        content=merged_content,
                        embedding_input=self._enrich_content(
                            merged_content, list(current.metadata.heading_path)
                        ),
                        content_hash=hashlib.md5(merged_content.encode()).hexdigest(),
                        metadata=dc_replace(current.metadata, chunk_index=len(merged)),
                        images=current.images + next_chunk.images,
                    ))
                    i += 2
                    continue

            merged.append(current)
            i += 1

        return [
            dc_replace(c, metadata=dc_replace(c.metadata, chunk_index=idx))
            for idx, c in enumerate(merged)
        ]

    def _enrich_content(self, content: str, heading_path: list[str]) -> str:
        """
        Thêm heading context vào content trước khi embed.
        Giúp embedding hiểu được context của chunk.
        e.g: "Chương 1 > Giới thiệu\n\nNội dung..."
        """
        if not heading_path:
            return content
        heading_context = " > ".join(heading_path)
        return f"{heading_context}\n\n{content}"

    def _metadata_value(self, metadata: dict, key: str) -> str | None:
        value = metadata.get(key)
        if value is None:
            return None
        text = str(value).strip()
        return text or None
