from datetime import datetime, timezone

from document_chunk.adapters.metadata.postgres_metadata_store import PostgresMetadataStore
from document_chunk.domain.entities.document import DocumentType
from document_chunk.infrastructure.config import SqlConfig


class TestDocumentMapping:
    def test_row_to_document_reconstructs_without_fake_path(self):
        store = PostgresMetadataStore(SqlConfig(enabled=True))

        document = store._row_to_document(
            (
                "doc-001",
                "lesson.pdf",
                DocumentType.PDF.value,
                "application/pdf",
                123,
                datetime.now(timezone.utc),
            )
        )

        assert document.id == "doc-001"
        assert document.name == "lesson.pdf"
        assert document.path is None
