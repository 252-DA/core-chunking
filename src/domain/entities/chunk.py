from dataclasses import dataclass, field

from src.domain.entities.document import DocumentType


@dataclass(frozen=True)
class ChunkMetadata:
    """
    Metadata gắn với một chunk — dùng cho retrieval và filtering.
    Immutable.
    """
    document_id: str
    document_name: str
    doc_type: DocumentType
    chunk_index: int                      # thứ tự chunk trong document
    heading_path: tuple[str, ...] = ()    # breadcrumb: ("Chương 1", "1.1 Giới thiệu")
    heading_level: int = 0                # level của heading trực tiếp chứa chunk
    page_number: int | None = None        # trang bắt đầu của chunk
    language: str | None = None


@dataclass
class Chunk:
    """
    Một đoạn nội dung đã được chia từ ParsedDocument.
    Output của Chunker, input của Embedder.
    """
    id: str                                    # UUID
    content: str                               # nội dung text của chunk
    metadata: ChunkMetadata
    images: list[str] = field(default_factory=list)  # filenames của ảnh đính kèm
    enriched_content: str | None = None        # content + heading context → dùng để embed
    embedding: list[float] | None = None       # vector, được gán sau khi embed

    @property
    def document_id(self) -> str:
        return self.metadata.document_id

    @property
    def heading_path_str(self) -> str:
        """Human-readable heading path, e.g. 'Chương 1 > 1.1 Giới thiệu'"""
        return " > ".join(self.metadata.heading_path)
