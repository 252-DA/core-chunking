"""
LlamaIndex-based chunkers — so sánh với custom HeadingChunker.

Ba chiến lược:
  1. LlamaIndexSentenceChunker  — SentenceSplitter (token + sentence boundary)
  2. LlamaIndexTokenChunker     — TokenTextSplitter (token boundary thuần túy)
  3. LlamaIndexSemanticChunker  — SemanticSplitterNodeParser (embedding similarity)

Cách dùng:
    from document_chunk.adapters.chunkers.llamaindex_chunkers import (
        LlamaIndexSentenceChunker,
        LlamaIndexTokenChunker,
        LlamaIndexSemanticChunker,
    )
    from document_chunk.infrastructure.config import LlamaIndexChunkerConfig

    config = LlamaIndexChunkerConfig()
    chunker = LlamaIndexSentenceChunker(config)
    result = chunker.chunk(parsed_doc)

Lưu ý về token vs char:
  - HeadingChunker: max_chunk_size=1500 chars
  - LlamaIndex: chunk_size tính bằng tokens (~4 chars/token)
  - Default chunk_size=375 tokens ≈ 1500 chars (tương đương để so sánh công bằng)
"""
import hashlib
import uuid
from dataclasses import dataclass, field

from document_chunk.domain.entities.chunk import Chunk, ChunkMetadata
from document_chunk.domain.entities.document import DocumentType, ElementType, ParsedDocument
from document_chunk.domain.exceptions import ChunkError
from document_chunk.domain.ports.chunker import IChunker
from document_chunk.infrastructure.config import LlamaIndexChunkerConfig
from document_chunk.shared.logger import get_logger
from document_chunk.shared.result import Err, Ok, Result

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

@dataclass
class _HeadingGroup:
    """Nhóm các section dưới cùng một heading context."""
    heading_path: list[str] = field(default_factory=list)
    heading_level: int = 0
    page_number: int | None = None
    sections_text: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join(t for t in self.sections_text if t.strip())

    def is_empty(self) -> bool:
        return not self.text.strip()


def _sections_to_heading_groups(doc: ParsedDocument) -> list[_HeadingGroup]:
    """
    Group sections theo heading hierarchy — giống logic của HeadingChunker.
    Dùng chung cho SentenceChunker và TokenChunker để so sánh công bằng.
    TOC sections bị bỏ qua.
    """
    groups: list[_HeadingGroup] = []
    current = _HeadingGroup()
    heading_stack: list[tuple[int, str]] = []

    for section in doc.sections:
        if section.is_toc:
            continue

        if section.element_type == ElementType.HEADING and section.heading_level > 0:
            if not current.is_empty():
                groups.append(current)

            heading_stack = [
                (lvl, txt) for lvl, txt in heading_stack
                if lvl < section.heading_level
            ]
            heading_text = section.heading or section.content.strip()
            heading_stack.append((section.heading_level, heading_text))

            current = _HeadingGroup(
                heading_path=[txt for _, txt in heading_stack],
                heading_level=section.heading_level,
                page_number=section.page_number,
            )
            if section.content.strip() and section.content.strip() != heading_text:
                current.sections_text.append(section.content)
                current.images.extend(section.images)
        else:
            if section.page_number is not None and current.page_number is None:
                current.page_number = section.page_number
            current.sections_text.append(section.content)
            current.images.extend(section.images)

    if not current.is_empty():
        groups.append(current)

    return groups


def _slides_to_groups(doc: ParsedDocument) -> list[_HeadingGroup]:
    """PPTX: mỗi slide = 1 heading group."""
    groups = []
    for idx, section in enumerate(doc.sections):
        if not section.content.strip():
            continue
        groups.append(_HeadingGroup(
            heading_path=[section.heading or f"Slide {idx + 1}"],
            heading_level=1,
            page_number=section.page_number,
            sections_text=[section.content],
            images=list(section.images),
        ))
    return groups


def _enrich_content(content: str, heading_path: list[str]) -> str:
    if not heading_path:
        return content
    return " > ".join(heading_path) + "\n\n" + content


# ---------------------------------------------------------------------------
# Base class cho SentenceChunker và TokenChunker
# ---------------------------------------------------------------------------

class _LlamaIndexBaseChunker(IChunker):
    """
    Base chunker: heading-grouping → LlamaIndex node parser → Chunk.
    Subclasses chỉ cần implement _build_parser().
    """

    def __init__(self, config: LlamaIndexChunkerConfig) -> None:
        self._chunk_size = config.chunk_size
        self._chunk_overlap = config.chunk_overlap

    @property
    def supported_types(self) -> tuple[DocumentType, ...]:
        return (
            DocumentType.PDF,
            DocumentType.DOCX,
            DocumentType.PPTX,
            DocumentType.MARKDOWN,
        )

    def _build_parser(self):
        raise NotImplementedError

    def chunk(self, doc: ParsedDocument) -> Result[list[Chunk], Exception]:
        try:
            from llama_index.core import Document as LlamaDocument

            if doc.document.doc_type == DocumentType.PPTX:
                groups = _slides_to_groups(doc)
            else:
                groups = _sections_to_heading_groups(doc)

            parser = self._build_parser()
            chunks: list[Chunk] = []

            for group in groups:
                if group.is_empty():
                    continue

                llama_doc = LlamaDocument(
                    text=group.text,
                    metadata={
                        "heading_path": group.heading_path,
                        "heading_level": group.heading_level,
                        "page_number": group.page_number,
                        "language": doc.language,
                        "course_id": doc.metadata.get("course_id"),
                        "owner_id": doc.metadata.get("owner_id"),
                    },
                    # Không để LlamaIndex inject metadata vào text — ta tự enrich
                    excluded_embed_metadata_keys=list(
                        LlamaDocument.__fields__.keys()
                        if hasattr(LlamaDocument, "__fields__") else []
                    ),
                )

                nodes = parser.get_nodes_from_documents([llama_doc])

                for node_idx, node in enumerate(nodes):
                    images = group.images if node_idx == 0 else []
                    chunks.append(self._node_to_chunk(node, doc, len(chunks), images))

            logger.debug(
                "llamaindex_chunker.done",
                document_id=doc.document.id,
                chunks=len(chunks),
                chunker=self.__class__.__name__,
            )
            return Ok(chunks)

        except ImportError as e:
            return Err(ChunkError(
                "llama-index-core chưa được cài. Chạy: pip install 'document-chunk[llamaindex]'",
                cause=e,
            ))
        except Exception as e:
            logger.error(
                "llamaindex_chunker.failed",
                document_id=doc.document.id,
                error=str(e),
                chunker=self.__class__.__name__,
            )
            return Err(ChunkError(f"LlamaIndex chunking failed for '{doc.document.id}'", cause=e))

    def _node_to_chunk(self, node, doc: ParsedDocument, chunk_index: int, images: list[str]) -> Chunk:
        content = node.text
        meta = node.metadata

        heading_path: list[str] = meta.get("heading_path") or []
        heading_level: int = meta.get("heading_level") or 0
        page_number: int | None = meta.get("page_number")
        language: str | None = meta.get("language") or doc.language
        course_id: str | None = meta.get("course_id")
        owner_id: str | None = meta.get("owner_id")

        return Chunk(
            id=str(uuid.uuid4()),
            content=content,
            embedding_input=_enrich_content(content, heading_path),
            content_hash=hashlib.md5(content.encode()).hexdigest(),
            metadata=ChunkMetadata(
                document_id=doc.document.id,
                document_name=doc.document.name,
                document_type=doc.document.doc_type,
                chunk_index=chunk_index,
                heading_path=tuple(heading_path),
                heading_level=heading_level,
                page_number=page_number,
                language=language,
                course_id=course_id,
                owner_id=owner_id,
                char_count=len(content),
            ),
            images=images,
        )


# ---------------------------------------------------------------------------
# Chunker 1: SentenceSplitter
# ---------------------------------------------------------------------------

class LlamaIndexSentenceChunker(_LlamaIndexBaseChunker):
    """
    Chunk bằng SentenceSplitter của LlamaIndex.

    - Tách tại sentence boundary (dấu câu)
    - Size tính bằng token (dùng tiktoken/simple tokenizer)
    - Có overlap để giữ context
    - So sánh gần nhất với HeadingChunker (đều sentence-aware + heading-grouped)

    Điểm khác biệt chính với HeadingChunker:
      • Đếm token thay vì char → chunk count có thể khác
      • Dùng NLTK / spaCy sentence tokenizer (nếu cài) hoặc regex đơn giản
      • Không có min_size merging
    """

    def _build_parser(self):
        from llama_index.core.node_parser import SentenceSplitter
        return SentenceSplitter(
            chunk_size=self._chunk_size,
            chunk_overlap=self._chunk_overlap,
            include_metadata=True,
            include_prev_next_rel=False,
        )


# ---------------------------------------------------------------------------
# Chunker 2: TokenTextSplitter
# ---------------------------------------------------------------------------

class LlamaIndexTokenChunker(_LlamaIndexBaseChunker):
    """
    Chunk bằng TokenTextSplitter của LlamaIndex.

    - Tách tại token boundary (không quan tâm sentence)
    - Size tính bằng token
    - Có overlap

    Dùng để kiểm tra: liệu việc tách cứng tại token boundary (không theo câu)
    có ảnh hưởng đến chất lượng retrieval so với HeadingChunker và SentenceChunker?

    Điểm khác biệt:
      • Có thể cắt giữa câu, thậm chí giữa từ ghép
      • Chunk count thường thấp hơn SentenceSplitter do ít "padding" sentence
    """

    def _build_parser(self):
        from llama_index.core.node_parser import TokenTextSplitter
        return TokenTextSplitter(
            chunk_size=self._chunk_size,
            chunk_overlap=self._chunk_overlap,
            include_metadata=True,
        )


# ---------------------------------------------------------------------------
# Chunker 3: SemanticSplitter
# ---------------------------------------------------------------------------

class LlamaIndexSemanticChunker(IChunker):
    """
    Chunk bằng SemanticSplitterNodeParser của LlamaIndex.

    - Dùng cosine similarity giữa embedding của từng câu để tìm ranh giới
    - KHÔNG dùng heading-grouping — để embedding tự quyết định boundaries
    - Cần embedding model (mặc định: BAAI/bge-small-en-v1.5 cho tốc độ)

    Điểm đặc biệt:
      • Không cần biết heading structure
      • Boundaries dựa trên sự thay đổi topic thực sự trong content
      • Chunk size không cố định — phụ thuộc vào nội dung
      • Chậm hơn do cần embed từng câu

    Điểm khác biệt so với HeadingChunker:
      • Không đảm bảo mỗi chunk nằm trong một section/heading
      • Chunk có thể span qua nhiều heading
      • Không có max_size đảm bảo cứng (chỉ điều chỉnh qua breakpoint_percentile)
    """

    def __init__(self, config: LlamaIndexChunkerConfig) -> None:
        self._buffer_size = config.semantic_buffer_size
        self._breakpoint_percentile = config.semantic_breakpoint_percentile
        self._embed_model_name = config.semantic_embed_model

    @property
    def supported_types(self) -> tuple[DocumentType, ...]:
        return (
            DocumentType.PDF,
            DocumentType.DOCX,
            DocumentType.PPTX,
            DocumentType.MARKDOWN,
        )

    def _build_parser(self):
        from llama_index.core.node_parser import SemanticSplitterNodeParser
        from llama_index.embeddings.huggingface import HuggingFaceEmbedding

        embed_model = HuggingFaceEmbedding(model_name=self._embed_model_name)
        return SemanticSplitterNodeParser(
            buffer_size=self._buffer_size,
            breakpoint_percentile_threshold=self._breakpoint_percentile,
            embed_model=embed_model,
            include_metadata=True,
        )

    def chunk(self, doc: ParsedDocument) -> Result[list[Chunk], Exception]:
        try:
            from llama_index.core import Document as LlamaDocument

            # Flatten toàn bộ content — để semantic boundary tự nổi lên
            full_text = "\n\n".join(
                s.content for s in doc.sections
                if s.content.strip() and not s.is_toc
            )

            if not full_text.strip():
                return Ok([])

            llama_doc = LlamaDocument(
                text=full_text,
                metadata={
                    "document_id": doc.document.id,
                    "document_name": doc.document.name,
                    "language": doc.language,
                    "course_id": doc.metadata.get("course_id"),
                    "owner_id": doc.metadata.get("owner_id"),
                },
            )

            parser = self._build_parser()
            nodes = parser.get_nodes_from_documents([llama_doc])

            chunks = [
                self._node_to_chunk(node, doc, idx)
                for idx, node in enumerate(nodes)
            ]

            logger.debug(
                "llamaindex_semantic_chunker.done",
                document_id=doc.document.id,
                chunks=len(chunks),
            )
            return Ok(chunks)

        except ImportError as e:
            return Err(ChunkError(
                "Thiếu dependency. Chạy: pip install 'document-chunk[llamaindex]'",
                cause=e,
            ))
        except Exception as e:
            logger.error(
                "llamaindex_semantic_chunker.failed",
                document_id=doc.document.id,
                error=str(e),
            )
            return Err(ChunkError(f"Semantic chunking failed for '{doc.document.id}'", cause=e))

    def _node_to_chunk(self, node, doc: ParsedDocument, chunk_index: int) -> Chunk:
        content = node.text
        meta = node.metadata
        language: str | None = meta.get("language") or doc.language
        course_id: str | None = meta.get("course_id")
        owner_id: str | None = meta.get("owner_id")

        return Chunk(
            id=str(uuid.uuid4()),
            content=content,
            # Semantic chunker không có heading context — dùng content trực tiếp
            embedding_input=content,
            content_hash=hashlib.md5(content.encode()).hexdigest(),
            metadata=ChunkMetadata(
                document_id=doc.document.id,
                document_name=doc.document.name,
                document_type=doc.document.doc_type,
                chunk_index=chunk_index,
                language=language,
                course_id=course_id,
                owner_id=owner_id,
                char_count=len(content),
            ),
        )
