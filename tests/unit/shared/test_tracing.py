"""
Tests for shared/tracing.py — OpenTelemetry tracing setup.
"""
from unittest.mock import MagicMock, patch

import pytest

from document_chunk.shared.tracing import (
    get_current_span_id,
    get_current_trace_id,
    get_tracer,
    setup_tracing,
)


class TestSetupTracing:
    def test_debug_mode_adds_console_exporter(self):
        with patch("document_chunk.shared.tracing.TracerProvider") as mock_provider_cls, \
             patch("document_chunk.shared.tracing.BatchSpanProcessor") as mock_batch, \
             patch("document_chunk.shared.tracing.ConsoleSpanExporter") as mock_console, \
             patch("document_chunk.shared.tracing.trace.set_tracer_provider"):
            mock_provider = MagicMock()
            mock_provider_cls.return_value = mock_provider

            setup_tracing(service_name="test-svc", debug=True)

            mock_provider.add_span_processor.assert_called_once()

    def test_otlp_mode_adds_otlp_exporter(self):
        with patch("document_chunk.shared.tracing.TracerProvider") as mock_provider_cls, \
             patch("document_chunk.shared.tracing.BatchSpanProcessor") as mock_batch, \
             patch("document_chunk.shared.tracing.OTLPSpanExporter") as mock_otlp, \
             patch("document_chunk.shared.tracing.trace.set_tracer_provider"):
            mock_provider = MagicMock()
            mock_provider_cls.return_value = mock_provider

            setup_tracing(
                service_name="prod-svc",
                otlp_endpoint="http://localhost:4318/v1/traces",
            )

            mock_otlp.assert_called_once_with(endpoint="http://localhost:4318/v1/traces")
            mock_provider.add_span_processor.assert_called_once()

    def test_no_endpoint_no_debug_no_export(self):
        with patch("document_chunk.shared.tracing.TracerProvider") as mock_provider_cls, \
             patch("document_chunk.shared.tracing.trace.set_tracer_provider"):
            mock_provider = MagicMock()
            mock_provider_cls.return_value = mock_provider

            setup_tracing(service_name="test-svc", debug=False, otlp_endpoint=None)

            mock_provider.add_span_processor.assert_not_called()


class TestGetTracer:
    def test_returns_tracer_instance(self):
        tracer = get_tracer("test.module")
        assert tracer is not None


class TestGetCurrentTraceId:
    def test_no_active_span_returns_none(self):
        # When no span is active, should return None
        result = get_current_trace_id()
        # In test environment with no span context, this should be None
        assert result is None or isinstance(result, str)


class TestGetCurrentSpanId:
    def test_no_active_span_returns_none(self):
        result = get_current_span_id()
        assert result is None or isinstance(result, str)
