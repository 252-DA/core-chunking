"""
Đo truy xuất theo TRANG trên tài liệu thật, không ghi gì vào Postgres hay Qdrant.

Mỗi biến thể = cách parse PDF × chunker:
  flow    đoán heading theo cỡ chữ trên cả file (cách cũ)
  slides  PDF slide thành một Section mỗi trang (pdf_slide_mode=auto)
  v1      HeadingChunker; v2 StructuralChunker; v2p là v2 không ghép hai trang

Câu hỏi gán nhãn bằng (tên file, trang), nên dùng lại được sau mỗi lần đổi
parser/chunker; chunk_id thì không (uuid4, đổi mỗi lần index). Định dạng giống
ai-tutor/eval/questions.seed.jsonl: {"id", "question", "evidence": [{"document", "page"}]}.
Bộ câu dùng cho đo này: packages-ai/eval/retrieval_questions.jsonl (xem README cùng thư mục).

Chạy trong container document-worker (có PyMuPDF, Docling, tokenizer, embedding-service):

    python eval_retrieval_pages.py --docs /tmp/eval_docs --questions q.jsonl \\
        --cache /tmp/eval_cache --tokenizer-path /app/bge-m3-tokenizer.json \\
        --grpc-target embedding-service:50051

Parse (Docling OCR ~1–2 phút mỗi bộ slide) và embedding được cache trong --cache.

Chỉ số, tìm trên toàn bộ chunk của mọi tài liệu như search theo khóa học:
  hit@k     có ít nhất một trang đúng trong k chunk đầu (chunk phủ trang đó)
  recall@6  tỉ lệ trang đúng có mặt trong 6 chunk đầu (câu hỏi nhiều trang)
  mrr@10    nghịch đảo hạng của chunk đúng đầu tiên
  strict@6  như hit@6 nhưng chỉ tính page_number, trang mà Postgres lưu và giao
            diện hiển thị; chunk phủ nhiều trang chỉ được tính ở trang đầu
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import multiprocessing
import pickle
import random
import re
import statistics
import sys
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from pathlib import Path

from document_chunk.adapters.chunkers.heading_chunker import HeadingChunker
from document_chunk.adapters.chunkers.structural import StructuralChunker
from document_chunk.adapters.chunkers.structural.tokens import TokenCounter
from document_chunk.adapters.parsers.adaptive_pdf_parser import AdaptivePdfParser
from document_chunk.adapters.parsers.docling_pdf_parser import DoclingPdfParser
from document_chunk.adapters.parsers.pptx_parser import PptxParser
from document_chunk.infrastructure.config import ChunkerConfig, EmbedderConfig, ParserConfig
from document_chunk.shared.result import Ok

PARSE_MODES = {"flow": "never", "slides": "auto"}
VARIANTS = {
    "v1-flow": ("flow", "heading", False),
    "v2-flow": ("flow", "structural", False),
    "v1-slides": ("slides", "heading", False),
    "v2-slides": ("slides", "structural", False),
    "v2p-slides": ("slides", "structural", True),
}
TOP_K = (1, 3, 6)


@dataclass
class Row:
    document: str
    page: int | None
    page_start: int | None
    page_end: int | None
    heading_path: tuple[str, ...]
    content: str
    embedding_input: str
    content_type: str | None

    def covers(self, document: str, page: int, *, strict: bool) -> bool:
        if document != self.document or self.page is None:
            return False
        if strict:
            return page == self.page
        first = self.page_start if self.page_start is not None else self.page
        last = self.page_end if self.page_end is not None else self.page
        return first <= page <= last


# ---------------------------------------------------------------- parse


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:10]


class _CachedDocling(DoclingPdfParser):
    """
    Hai chế độ parse gửi đúng cùng các trang sang Docling (đánh giá trang không
    phụ thuộc chế độ), nên kết quả OCR theo khoảng trang được dùng lại.
    """

    cache: Path

    def parse_pages(self, path, first_page, last_page):
        def run():
            return super(_CachedDocling, self).parse_pages(path, first_page, last_page)

        return self._cached(f"{first_page}-{last_page}", run, path)

    def parse(self, path):
        return self._cached("all", lambda: super(_CachedDocling, self).parse(path), path)

    def _cached(self, span, run, path):
        target = self.cache / f"docling-{_digest(path.name)}-{span}.pkl"
        if target.exists():
            return Ok(pickle.loads(target.read_bytes()))
        result = run()
        if result.is_ok():
            target.write_bytes(pickle.dumps(result.unwrap()))
        return result


def _parse_one(path: Path, mode: str, target: Path, cache: Path, ocr: bool) -> str | None:
    """Chạy trong tiến trình con: Docling giữ vài GB RAM, trả lại khi tiến trình kết thúc."""
    import document_chunk.adapters.parsers.adaptive_pdf_parser as adaptive

    _CachedDocling.cache = cache
    adaptive.DoclingPdfParser = _CachedDocling
    # Không OCR: trang ảnh không có chữ ở mọi biến thể như nhau; nhanh, không nạp Docling.
    options = {} if ocr else {"pdf_page_fallback_enabled": False, "pdf_strict_missing_text": False}
    parser = (
        PptxParser(ParserConfig())
        if mode == "pptx"
        else AdaptivePdfParser(ParserConfig(pdf_slide_mode=PARSE_MODES[mode], **options))
    )
    result = parser.parse(path)
    if result.is_err():
        return f"{path.name}: {result.error}"
    doc = result.unwrap()
    doc.images = {}  # bytes ảnh không cần cho đo, chỉ làm cache nặng
    target.write_bytes(pickle.dumps(doc))
    return None


def parse_all(docs: Path, cache: Path, mode: str, ocr: bool) -> dict:
    parsed = {}
    spawn = multiprocessing.get_context("spawn")
    for path in sorted(docs.iterdir()):
        if path.suffix.lower() not in (".pdf", ".pptx"):
            continue
        key = "pptx" if path.suffix.lower() == ".pptx" else mode
        target = cache / f"parsed-{key}{'' if ocr else '-noocr'}-{_digest(path.name)}.pkl"
        if not target.exists():
            print(f"  parse [{key}] {path.name}", file=sys.stderr, flush=True)
            # Không dùng multiprocessing.Pool: khi kernel OOM kill tiến trình con,
            # Pool lặng lẽ tạo tiến trình mới và apply() chờ mãi (Ch2 trang 52,
            # 2026-10-27). Executor báo BrokenProcessPool ngay.
            with ProcessPoolExecutor(1, mp_context=spawn) as pool:
                try:
                    error = pool.submit(_parse_one, path, key, target, cache, ocr).result()
                except BrokenProcessPool:
                    raise SystemExit(
                        f"parse failed: {path.name}: tiến trình con chết đột ngột "
                        "(thường là kernel OOM kill, xem dmesg)"
                    ) from None
            if error:
                raise SystemExit(f"parse failed: {error}")
        parsed[path.name] = pickle.loads(target.read_bytes())
    return parsed


def chunk_all(parsed: dict, strategy: str, paged: bool, tokenizer_path: str) -> list[Row]:
    config = ChunkerConfig(
        strategy=strategy, tokenizer_path=tokenizer_path, split_slides_by_page=paged
    )
    chunker = (
        HeadingChunker(config)
        if strategy == "heading"
        else StructuralChunker(config, TokenCounter(config))
    )
    rows = []
    for name, doc in parsed.items():
        result = chunker.chunk(doc)
        if result.is_err():
            raise SystemExit(f"chunk failed: {name}: {result.error}")
        for chunk in result.unwrap():
            meta = chunk.metadata
            rows.append(
                Row(
                    document=name,
                    page=meta.page_number,
                    page_start=meta.page_start,
                    page_end=meta.page_end,
                    heading_path=meta.heading_path,
                    content=chunk.content,
                    embedding_input=chunk.embedding_input,
                    content_type=meta.content_type,
                )
            )
    return rows


# ---------------------------------------------------------------- embed


class EmbeddingCache:
    def __init__(self, path: Path, grpc_target: str, dimension: int) -> None:
        self._path = path
        self._vectors: dict[str, list[float]] = {}
        if path.exists():
            for line in path.read_text().splitlines():
                row = json.loads(line)
                self._vectors[row["k"]] = row["v"]
        self._target = grpc_target
        self._dimension = dimension
        self._embedder = None

    def vectors(self, texts: list[str]) -> list[list[float]]:
        keys = [hashlib.sha1(t.encode()).hexdigest() for t in texts]
        missing = list(dict.fromkeys(k for k in keys if k not in self._vectors))
        if missing:
            by_key = dict(zip(keys, texts))
            self._fetch([(k, by_key[k]) for k in missing])
        return [self._vectors[k] for k in keys]

    def _fetch(self, items: list[tuple[str, str]]) -> None:
        if self._embedder is None:
            from document_chunk.adapters.embedders.grpc_embedder import GrpcEmbedder

            config = EmbedderConfig(provider="grpc", grpc_target=self._target)
            self._embedder = GrpcEmbedder(config, expected_dimension=self._dimension)
        print(f"  embed {len(items)} texts", file=sys.stderr, flush=True)
        with self._path.open("a") as out:
            for start in range(0, len(items), 32):
                batch = items[start : start + 32]
                result = self._embedder.embed([text for _, text in batch])
                if result.is_err():
                    raise SystemExit(f"embedding failed: {result.error}")
                for (key, _), vector in zip(batch, result.unwrap()):
                    self._vectors[key] = vector
                    out.write(json.dumps({"k": key, "v": vector}) + "\n")


def _unit(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]


# ---------------------------------------------------------------- score


def rank(query: list[float], matrix: list[list[float]]) -> list[int]:
    scores = [sum(a * b for a, b in zip(query, row)) for row in matrix]
    return sorted(range(len(scores)), key=lambda i: -scores[i])


def score_variant(rows: list[Row], questions: list[dict], cache: EmbeddingCache) -> tuple[dict, list]:
    # Search thật loại chunk mục lục (SearchFilter.exclude_content_types).
    rows = [r for r in rows if r.content_type != "toc"]
    matrix = [_unit(v) for v in cache.vectors([r.embedding_input for r in rows])]
    queries = [_unit(v) for v in cache.vectors([q["question"] for q in questions])]
    totals = {f"hit@{k}": 0.0 for k in TOP_K} | {"recall@6": 0.0, "mrr@10": 0.0, "strict@6": 0.0}
    detail = []
    for question, query in zip(questions, queries):
        order = rank(query, matrix)
        gold = [(e["document"], e["page"]) for e in question["evidence"]]
        first = next(
            (i for i, idx in enumerate(order[:10], 1) if any(rows[idx].covers(d, p, strict=False) for d, p in gold)),
            None,
        )
        for k in TOP_K:
            totals[f"hit@{k}"] += bool(first and first <= k)
        top6 = [rows[i] for i in order[:6]]
        found = sum(any(r.covers(d, p, strict=False) for r in top6) for d, p in gold)
        totals["recall@6"] += found / len(gold)
        totals["mrr@10"] += 1 / first if first else 0
        totals["strict@6"] += any(r.covers(d, p, strict=True) for r in top6 for d, p in gold)
        detail.append(
            {
                "id": question["id"],
                "first_rank": first,
                "top3": [f"{rows[i].document[:24]} p{rows[i].page}" for i in order[:3]],
            }
        )
    n = len(questions)
    return {k: round(v / n, 3) for k, v in totals.items()}, detail


def paired(base: list[dict], other: list[dict], name: str) -> str:
    """
    Chênh lệch trên cùng câu hỏi, không phải hai trung bình độc lập: vài chục câu
    thì 1–2 câu đổi hạng đã làm trung bình lệch vài phần trăm.
    """
    def rr(row):
        return 1 / row["first_rank"] if row["first_rank"] else 0.0

    def hit(row):
        return bool(row["first_rank"] and row["first_rank"] <= 6)

    other_by_id = {row["id"]: row for row in other}
    pairs = [(row, other_by_id[row["id"]]) for row in base]
    diffs = [rr(b) - rr(o) for b, o in pairs]
    rng = random.Random(0)
    boots = sorted(
        statistics.fmean(rng.choice(diffs) for _ in diffs) for _ in range(10_000)
    )
    low, high = -boots[int(0.975 * len(boots))], -boots[int(0.025 * len(boots))]
    better = sum(hit(o) and not hit(b) for b, o in pairs)
    worse = sum(hit(b) and not hit(o) for b, o in pairs)
    n = better + worse
    tail = sum(math.comb(n, k) for k in range(min(better, worse) + 1)) / 2**n if n else 1.0
    return (
        f"| {name} | {-statistics.fmean(diffs):+.3f} | [{low:+.3f}, {high:+.3f}] "
        f"| +{better} / −{worse} | {min(1.0, 2 * tail):.2f} |"
    )


def _title(text: str) -> str:
    # v2 gộp "Bloom filtering (2)" vào "Bloom filtering": vẫn là cùng tiêu đề.
    return re.sub(r"\s*\((?:\d+|tiếp|cont\.)\)$", "", " ".join(text.split()), flags=re.I).casefold()


def structure(rows: list[Row], titles: dict[str, dict[int, str]]) -> dict:
    """
    ``titles``: tài liệu → trang → tiêu đề slide (trang không tiêu đề mang tiêu đề
    slide trước). ``label_ok``: heading cuối của chunk, thứ chatbot hiện làm nhãn
    nguồn, có đúng là tiêu đề slide ở page_number của chunk không.
    """
    sizes = [len(r.content) for r in rows]
    spans = [r for r in rows if r.page_start is not None and r.page_end not in (None, r.page_start)]
    kept = total = labelled = correct = 0
    for document, by_page in titles.items():
        mine = [r for r in rows if r.document == document]
        haystack = [(r.content + " " + " ".join(r.heading_path)).casefold() for r in mine]
        for name in dict.fromkeys(_title(t) for t in by_page.values()):
            total += 1
            kept += any(name in text for text in haystack)
        for r in mine:
            if r.page in by_page:
                labelled += 1
                correct += bool(r.heading_path) and _title(r.heading_path[-1]) == _title(by_page[r.page])
    return {
        "chunks": len(rows),
        "median_chars": int(statistics.median(sizes)) if sizes else 0,
        "under_200_chars": sum(s < 200 for s in sizes),
        "multi_page": len(spans),
        "slide_titles_kept": f"{kept}/{total}",
        "label_ok": round(correct / labelled, 3) if labelled else None,
    }


# ---------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--docs", type=Path, required=True)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--tokenizer-path", required=True)
    parser.add_argument("--grpc-target", required=True)
    parser.add_argument("--dimension", type=int, default=1024)
    parser.add_argument("--variants", default=",".join(VARIANTS))
    parser.add_argument("--detail", type=Path, help="ghi hạng từng câu hỏi theo biến thể (JSONL)")
    parser.add_argument("--no-ocr", action="store_true", help="không gửi trang ảnh sang Docling")
    args = parser.parse_args()

    args.cache.mkdir(parents=True, exist_ok=True)
    questions = [
        q
        for q in (json.loads(line) for line in args.questions.read_text().splitlines() if line.strip())
        if q.get("evidence")
    ]
    cache = EmbeddingCache(args.cache / "embeddings.jsonl", args.grpc_target, args.dimension)
    parsed = {mode: None for mode in PARSE_MODES}

    def docs_for(mode: str) -> dict:
        if parsed[mode] is None:
            parsed[mode] = parse_all(args.docs, args.cache, mode, not args.no_ocr)
        return parsed[mode]

    # Tiêu đề tham chiếu: tiêu đề slide mà chế độ slides đọc được từ PDF slide;
    # slide không tiêu đề là phần tiếp của slide trước.
    titles = {}
    for name, doc in docs_for("slides").items():
        if doc.metadata.get("layout") != "slides" or doc.document.doc_type.value != "pdf":
            continue
        current, by_page = None, {}
        for section in doc.sections:
            current = section.heading or current
            if current:
                by_page[section.page_number] = current
        titles[name] = by_page

    results = {}
    for name in args.variants.split(","):
        mode, strategy, paged = VARIANTS[name]
        rows = chunk_all(docs_for(mode), strategy, paged, args.tokenizer_path)
        metrics, detail = score_variant(rows, questions, cache)
        results[name] = {"metrics": metrics, "structure": structure(rows, titles), "detail": detail}

    print(f"{len(questions)} câu hỏi có trang đúng\n")
    columns = [f"hit@{k}" for k in TOP_K] + ["recall@6", "mrr@10", "strict@6"]
    shape = ["chunks", "median_chars", "under_200_chars", "multi_page", "slide_titles_kept", "label_ok"]
    print("| variant | " + " | ".join(columns + shape) + " |")
    print("|---" * (1 + len(columns) + len(shape)) + "|")
    for name, result in results.items():
        cells = [str(result["metrics"][c]) for c in columns] + [str(result["structure"][c]) for c in shape]
        print(f"| {name} | " + " | ".join(cells) + " |")
    names = list(results)
    if len(names) > 1:
        print(f"\nSo với {names[0]} trên cùng từng câu (bootstrap 10.000 lần, McNemar chính xác):\n")
        print("| variant | ΔMRR@10 | 95% CI | hit@6 hơn/kém | p (McNemar) |")
        print("|---|---|---|---|---|")
        for name in names[1:]:
            print(paired(results[names[0]]["detail"], results[name]["detail"], name))
    if args.detail:
        with args.detail.open("w") as out:
            for name, result in results.items():
                for row in result["detail"]:
                    out.write(json.dumps({"variant": name, **row}, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
