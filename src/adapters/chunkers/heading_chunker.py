"""
HeadingChunker — chia ParsedDocument thành Chunks theo heading hierarchy.

Algorithm:
  1. Walk qua sections theo thứ tự
  2. Maintain heading path stack (breadcrumb)
  3. Gặp heading mới → tạo chunk mới
  4. Content quá lớn → split tại sentence boundary
  5. Chunk quá nhỏ → merge với chunk kế tiếp
"""
import re
import uuid
from dataclasses import dataclass, field

from src.domain.entities.chunk import Chunk, ChunkMetadata
from src.domain.entities.document import DocumentType, ElementType, ParsedDocument, Section
from src.domain.ports.chunker import IChunker
from src.infrastructure.config import ChunkerConfig
from src.shared.logger import get_logger
from src.shared.result import Err, Ok, Result

logger = get_logger(__name__)


@dataclass
class _ChunkBuffer:
    """Buffer tích lũy content trước khi tạo Chunk."""
    sections: list[Section] = field(default_factory=list)
    heading_path: list[str] = field(default_factory=list)
    heading_level: int = 0

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
        try:
            if doc.document.doc_type == DocumentType.PPTX:
                chunks = self._chunk_by_slides(doc)
            else:
                chunks = self._chunk_by_headings(doc)

            chunks = self._merge_small_chunks(chunks, doc.document.doc_type)

            logger.debug(
                "chunker.done",
                document_id=doc.document.id,
                chunks=len(chunks),
                doc_type=doc.document.doc_type.value,
            )
            return Ok(chunks)

        except Exception as e:
            logger.error("chunker.failed", document_id=doc.document.id, error=str(e))
            return Err(e)

    # ------------------------------------------------------------------
    # Strategy 1: Heading-based (PDF, DOCX, Markdown)
    # ------------------------------------------------------------------

    def _chunk_by_headings(self, doc: ParsedDocument) -> list[Chunk]:
        chunks: list[Chunk] = []
        buffer = _ChunkBuffer()
        heading_stack: list[tuple[int, str]] = []  # (level, text)
        chunk_index = 0

        for section in doc.sections:
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
    ) -> Chunk:
        enriched = self._enrich_content(content, heading_path)
        return Chunk(
            id=str(uuid.uuid4()),
            content=content,
            metadata=ChunkMetadata(
                document_id=doc.document.id,
                document_name=doc.document.name,
                doc_type=doc.document.doc_type,
                chunk_index=chunk_index,
                heading_path=tuple(heading_path),
                heading_level=heading_level,
                page_number=page_number,
                language=doc.language,
            ),
            images=images,
            enriched_content=enriched,
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
                    # Merge — giữ heading_path của current (xuất hiện trước)
                    merged_content = current.content + "\n\n" + next_chunk.content
                    merged.append(Chunk(
                        id=current.id,
                        content=merged_content,
                        metadata=ChunkMetadata(
                            document_id=current.metadata.document_id,
                            document_name=current.metadata.document_name,
                            doc_type=current.metadata.doc_type,
                            chunk_index=len(merged),
                            heading_path=current.metadata.heading_path,
                            heading_level=current.metadata.heading_level,
                            page_number=current.metadata.page_number,
                            language=current.metadata.language,
                        ),
                        images=current.images + next_chunk.images,
                        enriched_content=self._enrich_content(
                            merged_content, list(current.metadata.heading_path)
                        ),
                    ))
                    i += 2
                    continue

            merged.append(current)
            i += 1

        # Reindex sau khi merge
        return [
            Chunk(
                id=c.id,
                content=c.content,
                metadata=ChunkMetadata(
                    document_id=c.metadata.document_id,
                    document_name=c.metadata.document_name,
                    doc_type=c.metadata.doc_type,
                    chunk_index=idx,
                    heading_path=c.metadata.heading_path,
                    heading_level=c.metadata.heading_level,
                    page_number=c.metadata.page_number,
                    language=c.metadata.language,
                ),
                images=c.images,
                enriched_content=c.enriched_content,
            )
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
