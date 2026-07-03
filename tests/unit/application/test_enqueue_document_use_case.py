"""
Tests for EnqueueDocumentUseCase.
"""
from unittest.mock import MagicMock

import pytest

from document_chunk.application.dto.document_dto import ProcessDocumentRequest
from document_chunk.application.use_cases.enqueue_document import (
    EnqueueDocumentRequest,
    EnqueueDocumentResponse,
    EnqueueDocumentUseCase,
)
from document_chunk.domain.exceptions import UnsupportedFileTypeError
from document_chunk.shared.result import Err, Ok


class TestEnqueueDocumentRequest:
    def test_defaults(self):
        req = EnqueueDocumentRequest(
            file_path="/tmp/test.pdf",
            file_name="test.pdf",
        )
        assert req.document_id is None
        assert req.language is None
        assert req.metadata is None

    def test_with_all_fields(self):
        req = EnqueueDocumentRequest(
            file_path="/tmp/test.pdf",
            file_name="test.pdf",
            document_id="custom-id",
            language="vi",
            metadata={"course_id": "course-001"},
        )
        assert req.document_id == "custom-id"
        assert req.language == "vi"
        assert req.metadata == {"course_id": "course-001"}


class TestEnqueueDocumentUseCase:
    def test_successful_enqueue_pdf(
        self,
        sample_document,
        mock_file_storage,
        mock_metadata_store,
        mock_job_queue,
    ):
        use_case = EnqueueDocumentUseCase(
            file_storage=mock_file_storage,
            metadata_store=mock_metadata_store,
            job_queue=mock_job_queue,
        )
        result = use_case.execute(
            EnqueueDocumentRequest(
                file_path=sample_document.path,
                file_name=sample_document.name,
                document_id=sample_document.id,
                language="en",
                metadata={"course_id": "course-001"},
            )
        )
        assert result.is_ok()
        resp = result.unwrap()
        assert resp.document_id == sample_document.id
        assert resp.job_id == "job-001"
        assert resp.status == "QUEUED"

        mock_file_storage.upload.assert_called_once()
        mock_metadata_store.upsert_document.assert_called_once()
        mock_job_queue.enqueue.assert_called_once()

    def test_generates_document_id_when_not_provided(
        self,
        sample_document,
        mock_file_storage,
        mock_metadata_store,
        mock_job_queue,
    ):
        use_case = EnqueueDocumentUseCase(
            file_storage=mock_file_storage,
            metadata_store=mock_metadata_store,
            job_queue=mock_job_queue,
        )
        result = use_case.execute(
            EnqueueDocumentRequest(
                file_path=sample_document.path,
                file_name=sample_document.name,
                language="en",
            )
        )
        assert result.is_ok()
        resp = result.unwrap()
        assert resp.document_id is not None
        assert len(resp.document_id) == 36  # UUID4 format
        assert resp.job_id == "job-001"

    def test_unsupported_file_type(
        self,
        mock_file_storage,
        mock_metadata_store,
        mock_job_queue,
    ):
        use_case = EnqueueDocumentUseCase(
            file_storage=mock_file_storage,
            metadata_store=mock_metadata_store,
            job_queue=mock_job_queue,
        )
        result = use_case.execute(
            EnqueueDocumentRequest(
                file_path="/tmp/test.xyz",
                file_name="test.xyz",
            )
        )
        assert result.is_err()
        error = result.error
        assert isinstance(error, UnsupportedFileTypeError)
        assert error.file_type == ".xyz"

        mock_file_storage.upload.assert_not_called()
        mock_metadata_store.upsert_document.assert_not_called()
        mock_job_queue.enqueue.assert_not_called()

    def test_docx_file_type(self, tmp_path):
        docx_file = tmp_path / "test.docx"
        docx_file.write_text("fake docx")
        fs = MagicMock()
        fs.upload.return_value = Ok("docx/d1/test.docx")
        ms = MagicMock()
        ms.upsert_document.return_value = Ok(None)
        jq = MagicMock()
        jq.enqueue.return_value = Ok("job-docx")

        use_case = EnqueueDocumentUseCase(
            file_storage=fs, metadata_store=ms, job_queue=jq,
        )
        result = use_case.execute(
            EnqueueDocumentRequest(
                file_path=docx_file, file_name="test.docx",
            )
        )
        assert result.is_ok()
        resp = result.unwrap()
        assert resp.status == "QUEUED"
        jq.enqueue.assert_called_once()

    def test_markdown_file_type(self, tmp_path):
        md_file = tmp_path / "readme.md"
        md_file.write_text("# Hello")
        fs = MagicMock()
        fs.upload.return_value = Ok("markdown/d1/readme.md")
        ms = MagicMock()
        ms.upsert_document.return_value = Ok(None)
        jq = MagicMock()
        jq.enqueue.return_value = Ok("job-md")

        use_case = EnqueueDocumentUseCase(
            file_storage=fs, metadata_store=ms, job_queue=jq,
        )
        result = use_case.execute(
            EnqueueDocumentRequest(
                file_path=md_file, file_name="readme.md",
            )
        )
        assert result.is_ok()
        jq.enqueue.assert_called_once()

    def test_upload_failure_propagates_error(
        self,
        sample_document,
        mock_metadata_store,
        mock_job_queue,
    ):
        failing_storage = MagicMock()
        failing_storage.upload.return_value = Err(RuntimeError("minio down"))

        use_case = EnqueueDocumentUseCase(
            file_storage=failing_storage,
            metadata_store=mock_metadata_store,
            job_queue=mock_job_queue,
        )
        result = use_case.execute(
            EnqueueDocumentRequest(
                file_path=sample_document.path,
                file_name=sample_document.name,
            )
        )
        assert result.is_err()
        assert "minio down" in str(result.error)
        mock_metadata_store.upsert_document.assert_not_called()
        mock_job_queue.enqueue.assert_not_called()

    def test_metadata_failure_propagates_error(
        self,
        sample_document,
        mock_file_storage,
        mock_job_queue,
    ):
        failing_metadata = MagicMock()
        failing_metadata.upsert_document.return_value = Err(RuntimeError("pg down"))

        use_case = EnqueueDocumentUseCase(
            file_storage=mock_file_storage,
            metadata_store=failing_metadata,
            job_queue=mock_job_queue,
        )
        result = use_case.execute(
            EnqueueDocumentRequest(
                file_path=sample_document.path,
                file_name=sample_document.name,
            )
        )
        assert result.is_err()
        assert "pg down" in str(result.error)
        mock_file_storage.upload.assert_called_once()
        mock_job_queue.enqueue.assert_not_called()

    def test_enqueue_failure_propagates_error(
        self,
        sample_document,
        mock_file_storage,
        mock_metadata_store,
    ):
        failing_queue = MagicMock()
        failing_queue.enqueue.return_value = Err(RuntimeError("redis down"))

        use_case = EnqueueDocumentUseCase(
            file_storage=mock_file_storage,
            metadata_store=mock_metadata_store,
            job_queue=failing_queue,
        )
        result = use_case.execute(
            EnqueueDocumentRequest(
                file_path=sample_document.path,
                file_name=sample_document.name,
            )
        )
        assert result.is_err()
        assert "redis down" in str(result.error)
        mock_file_storage.upload.assert_called_once()
        mock_metadata_store.upsert_document.assert_called_once()

    def test_ppt_file_type(self, tmp_path):
        ppt_file = tmp_path / "slides.ppt"
        ppt_file.write_text("fake ppt")
        fs = MagicMock()
        fs.upload.return_value = Ok("pptx/d1/slides.ppt")
        ms = MagicMock()
        ms.upsert_document.return_value = Ok(None)
        jq = MagicMock()
        jq.enqueue.return_value = Ok("job-ppt")

        use_case = EnqueueDocumentUseCase(
            file_storage=fs, metadata_store=ms, job_queue=jq,
        )
        result = use_case.execute(
            EnqueueDocumentRequest(
                file_path=ppt_file, file_name="slides.ppt",
            )
        )
        assert result.is_ok()
        jq.enqueue.assert_called_once()

    def test_language_metadata_injected(
        self,
        sample_document,
        mock_file_storage,
        mock_metadata_store,
        mock_job_queue,
    ):
        use_case = EnqueueDocumentUseCase(
            file_storage=mock_file_storage,
            metadata_store=mock_metadata_store,
            job_queue=mock_job_queue,
        )
        result = use_case.execute(
            EnqueueDocumentRequest(
                file_path=sample_document.path,
                file_name=sample_document.name,
                language="vi",
            )
        )
        assert result.is_ok()
        _, upsert_kwargs = mock_metadata_store.upsert_document.call_args
        assert upsert_kwargs["metadata"]["language"] == "vi"

    def test_whitespace_only_language_not_injected(
        self,
        sample_document,
        mock_file_storage,
        mock_metadata_store,
        mock_job_queue,
    ):
        use_case = EnqueueDocumentUseCase(
            file_storage=mock_file_storage,
            metadata_store=mock_metadata_store,
            job_queue=mock_job_queue,
        )
        result = use_case.execute(
            EnqueueDocumentRequest(
                file_path=sample_document.path,
                file_name=sample_document.name,
                language="   ",
            )
        )
        assert result.is_ok()
        _, upsert_kwargs = mock_metadata_store.upsert_document.call_args
        assert "language" not in upsert_kwargs["metadata"]
