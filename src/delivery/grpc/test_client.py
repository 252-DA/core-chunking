"""
Lightweight gRPC CLI for manual ChunkingService smoke tests.

Examples:
    python -m src.delivery.grpc.test_client health
    python -m src.delivery.grpc.test_client process /workspace/chunking_v2/examples/grpc/sample.md --language vi
    python -m src.delivery.grpc.test_client search "chunking pipeline"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import grpc
from google.protobuf.json_format import MessageToDict

from src.delivery.grpc.proto import chunking_pb2, chunking_pb2_grpc

_MAX_MESSAGE_LENGTH = 100 * 1024 * 1024  # 100 MB


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manual gRPC client for ChunkingService.",
    )
    parser.add_argument(
        "--target",
        default=os.environ.get("GRPC_TARGET", "localhost:50051"),
        help="gRPC target in host:port format.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="RPC timeout in seconds.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    health_parser = subparsers.add_parser("health", help="Call HealthCheck.")
    health_parser.set_defaults(func=_cmd_health)

    process_parser = subparsers.add_parser(
        "process",
        help="Read a local file and call ProcessDocument.",
    )
    process_parser.add_argument("file_path", help="Path to the local file to upload.")
    process_parser.add_argument(
        "--file-name",
        help="Override file_name sent to gRPC. Defaults to the local filename.",
    )
    process_parser.add_argument("--document-id", help="Optional document ID.")
    process_parser.add_argument("--language", help="Optional language, e.g. vi or en.")
    process_parser.add_argument(
        "--meta",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Attach metadata pairs. Repeatable.",
    )
    process_parser.set_defaults(func=_cmd_process)

    search_parser = subparsers.add_parser("search", help="Call Search.")
    search_parser.add_argument("query", help="Semantic search query.")
    search_parser.add_argument("--top-k", type=int, default=5, help="Top K results.")
    search_parser.add_argument(
        "--score-threshold",
        type=float,
        default=0.0,
        help="Optional minimum similarity score.",
    )
    search_parser.add_argument(
        "--doc-type",
        action="append",
        default=[],
        dest="doc_types",
        help="Optional doc_type filter. Repeatable.",
    )
    search_parser.add_argument(
        "--document-id",
        action="append",
        default=[],
        dest="document_ids",
        help="Optional document_id filter. Repeatable.",
    )
    search_parser.add_argument("--language", help="Optional language filter.")
    search_parser.add_argument("--course-id", help="Optional course_id filter.")
    search_parser.add_argument("--owner-id", help="Optional owner_id filter.")
    search_parser.set_defaults(func=_cmd_search)

    delete_parser = subparsers.add_parser("delete", help="Call DeleteDocument.")
    delete_parser.add_argument("document_id", help="Document ID to delete.")
    delete_parser.set_defaults(func=_cmd_delete)

    return parser


def _channel(target: str) -> grpc.Channel:
    return grpc.insecure_channel(
        target,
        options=[
            ("grpc.max_send_message_length", _MAX_MESSAGE_LENGTH),
            ("grpc.max_receive_message_length", _MAX_MESSAGE_LENGTH),
        ],
    )


def _stub(target: str) -> chunking_pb2_grpc.ChunkingServiceStub:
    return chunking_pb2_grpc.ChunkingServiceStub(_channel(target))


def _metadata_dict(values: list[str]) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for raw_value in values:
        if "=" not in raw_value:
            raise ValueError(f"Invalid metadata item '{raw_value}'. Use KEY=VALUE.")
        key, value = raw_value.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError(f"Invalid metadata item '{raw_value}'. Empty key.")
        metadata[key] = value
    return metadata


def _print_message(message) -> None:
    payload = MessageToDict(message, preserving_proto_field_name=True)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _cmd_health(args: argparse.Namespace) -> None:
    response = _stub(args.target).HealthCheck(
        chunking_pb2.HealthCheckRequest(),
        timeout=args.timeout,
    )
    _print_message(response)


def _cmd_process(args: argparse.Namespace) -> None:
    file_path = Path(args.file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    if not file_path.is_file():
        raise ValueError(f"Path is not a file: {file_path}")

    request = chunking_pb2.ProcessDocumentRequest(
        file_data=file_path.read_bytes(),
        file_name=args.file_name or file_path.name,
        document_id=args.document_id or "",
        language=args.language or "",
        metadata=_metadata_dict(args.meta),
    )
    response = _stub(args.target).ProcessDocument(request, timeout=args.timeout)
    _print_message(response)


def _cmd_search(args: argparse.Namespace) -> None:
    request = chunking_pb2.SearchRequest(
        query=args.query,
        top_k=args.top_k,
        score_threshold=args.score_threshold,
        doc_types=args.doc_types,
        document_ids=args.document_ids,
        language=args.language or "",
        course_id=args.course_id or "",
        owner_id=args.owner_id or "",
    )
    response = _stub(args.target).Search(request, timeout=args.timeout)
    _print_message(response)


def _cmd_delete(args: argparse.Namespace) -> None:
    request = chunking_pb2.DeleteDocumentRequest(document_id=args.document_id)
    response = _stub(args.target).DeleteDocument(request, timeout=args.timeout)
    _print_message(response)


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    try:
        args.func(args)
        return 0
    except grpc.RpcError as exc:
        error_payload = {
            "code": exc.code().name if exc.code() else "UNKNOWN",
            "details": exc.details(),
        }
        print(json.dumps(error_payload, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
