from dataclasses import dataclass, field

from document_chunk.domain.entities.document import DocumentType


@dataclass(frozen=True)
class ChunkMetadata:
    """
    Metadata gắn với một chunk — dùng cho retrieval và filtering.
    Immutable.
    """
    #- Identity
    document_id: str
    document_name: str
    document_type: DocumentType
    chunk_index: int

    #-- Structure
    heading_path: tuple[str, ...] = ()  # e.g. ("Chương 1", "1.1 Giới thiệu")
    heading_level: int = 0
    section_id: str | None = None # ID của section chứa chunk, nếu có
    section_title: str | None = None # title của section chứa chunk, nếu có
    parent_section: str | None = None # title của section cha, nếu có

    # --- Position ---
    page_number: int | None = None
    page_start: int | None = None
    page_end: int | None = None
    part_index: int | None = None
    part_count: int | None = None
    chunker_version: str | None = None
    start_char: int | None = None
    end_char: int | None = None

    content_type: str | None = None  # e.g. "text", "table", "code", "list"

    # --- Semantic signal ---
    keywords: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()         # NER (optional)

    # --- Quality signal ---
    token_count: int | None = None
    char_count: int | None = None
    density_score: float | None = None     # heuristic

    # --- Context ---
    language: str | None = None
    course_id: str | None = None
    owner_id: str | None = None

    # --- Retrieval tuning ---
    importance_score: float | None = None  # boost khi rerank

@dataclass
class Chunk:
    """
    Một đoạn nội dung đã được chia từ ParsedDocument.
    Output của Chunker, input của Embedder.
    """
    # --- Identity ---
    id: str

    # --- Content ---
    content: str
    embedding_input: str                  # heading context + content, dùng để embed
    content_hash: str                     # để detect change

    # --- Metadata ---
    metadata: ChunkMetadata

    # --- Assets ---
    images: list[str] = field(default_factory=list)

    # --- Embedding ---
    embedding: list[float] | None = None
    embedding_model: str | None = None
    embedding_version: int | None = None

    # --- Versioning ---
    chunk_version: int = 1

    # --- Retrieval (runtime) ---
    score: float | None = None
    rerank_score: float | None = None

    @property
    def document_id(self) -> str:
        return self.metadata.document_id

    @property
    def heading_path_str(self) -> str:
        """Human-readable heading path, e.g. 'Chương 1 > 1.1 Giới thiệu'"""
        return " > ".join(self.metadata.heading_path)
