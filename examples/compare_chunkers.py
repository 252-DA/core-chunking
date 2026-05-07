"""
Script so sánh trực tiếp giữa HeadingChunker và 3 LlamaIndex chunkers.

Chạy:
    cd chunking_v2
    python examples/compare_chunkers.py
    python examples/compare_chunkers.py --file path/to/doc.pdf
    python examples/compare_chunkers.py --semantic   # thêm SemanticChunker (chậm)

Yêu cầu:
    pip install 'document-chunk[llamaindex]'
"""
import argparse
import statistics
import sys
from pathlib import Path

# Đảm bảo import từ src/
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.adapters.chunkers.heading_chunker import HeadingChunker
from src.domain.entities.document import Document, DocumentType, ElementType, ParsedDocument, Section
from src.infrastructure.config import ChunkerConfig, LlamaIndexChunkerConfig
from src.shared.result import Ok, Err


# ---------------------------------------------------------------------------
# Sample document (Vietnamese academic content)
# ---------------------------------------------------------------------------

def make_sample_doc(doc_id: str = "sample-001") -> ParsedDocument:
    """Tạo ParsedDocument mẫu với nội dung tiếng Việt học thuật."""
    doc = Document(
        id=doc_id,
        name="bai_giang_ml.pdf",
        path=None,
        doc_type=DocumentType.PDF,
        size_bytes=0,
        mime_type="application/pdf",
    )
    sections = [
        Section(
            content="Chương 1: Giới thiệu về Học Máy",
            element_type=ElementType.HEADING,
            heading="Chương 1: Giới thiệu về Học Máy",
            heading_level=1, page_number=1,
        ),
        Section(
            content=(
                "Học máy (Machine Learning) là một nhánh của trí tuệ nhân tạo, "
                "cho phép máy tính học hỏi từ dữ liệu mà không cần lập trình tường minh. "
                "Lĩnh vực này đã và đang tạo ra cuộc cách mạng trong nhiều ngành công nghiệp, "
                "từ nhận diện hình ảnh, xử lý ngôn ngữ tự nhiên cho đến hệ thống gợi ý. "
                "Thuật ngữ 'machine learning' được đặt ra bởi Arthur Samuel vào năm 1959."
            ),
            element_type=ElementType.PARAGRAPH, page_number=1,
        ),
        Section(
            content=(
                "Có ba loại học máy chính: học có giám sát (supervised learning), "
                "học không giám sát (unsupervised learning), và học tăng cường (reinforcement learning). "
                "Mỗi loại có đặc điểm riêng và phù hợp với các bài toán khác nhau. "
                "Việc lựa chọn đúng phương pháp là chìa khóa để đạt được kết quả tốt."
            ),
            element_type=ElementType.PARAGRAPH, page_number=2,
        ),
        Section(
            content="1.1 Học Có Giám Sát",
            element_type=ElementType.HEADING,
            heading="1.1 Học Có Giám Sát",
            heading_level=2, page_number=2,
        ),
        Section(
            content=(
                "Trong học có giám sát, mô hình được huấn luyện trên dữ liệu có nhãn. "
                "Thuật toán học cách ánh xạ từ đặc trưng đầu vào sang nhãn đầu ra. "
                "Các thuật toán phổ biến bao gồm hồi quy tuyến tính, cây quyết định, "
                "và máy véc-tơ hỗ trợ (SVM). "
                "Thách thức chính là tổng quát hóa — thực hiện tốt trên dữ liệu chưa thấy."
            ),
            element_type=ElementType.PARAGRAPH, page_number=2,
        ),
        Section(
            content="1.2 Học Không Giám Sát",
            element_type=ElementType.HEADING,
            heading="1.2 Học Không Giám Sát",
            heading_level=2, page_number=3,
        ),
        Section(
            content=(
                "Học không giám sát làm việc với dữ liệu không có nhãn. "
                "Thuật toán phải tự tìm ra các mẫu và cấu trúc trong dữ liệu. "
                "Các thuật toán phân cụm như K-means và phân cụm phân cấp là ví dụ phổ biến. "
                "Kỹ thuật giảm chiều như PCA cũng thuộc loại này."
            ),
            element_type=ElementType.PARAGRAPH, page_number=3,
        ),
        Section(
            content="Chương 2: Mạng Nơ-ron Nhân Tạo",
            element_type=ElementType.HEADING,
            heading="Chương 2: Mạng Nơ-ron Nhân Tạo",
            heading_level=1, page_number=4,
        ),
        Section(
            content=(
                "Mạng nơ-ron nhân tạo được lấy cảm hứng từ cấu trúc não người. "
                "Chúng bao gồm các lớp nút được kết nối với nhau gọi là nơ-ron. "
                "Deep learning sử dụng mạng nơ-ron với nhiều lớp ẩn để học các biểu diễn phức tạp. "
                "Bước đột phá của deep learning năm 2012 với AlexNet đã biến đổi thị giác máy tính."
            ),
            element_type=ElementType.PARAGRAPH, page_number=4,
        ),
        Section(
            content="2.1 Lan Truyền Ngược (Backpropagation)",
            element_type=ElementType.HEADING,
            heading="2.1 Lan Truyền Ngược (Backpropagation)",
            heading_level=2, page_number=5,
        ),
        Section(
            content=(
                "Lan truyền ngược là thuật toán được dùng để huấn luyện mạng nơ-ron. "
                "Nó tính gradient của hàm mất mát theo từng trọng số bằng quy tắc dây chuyền. "
                "Gradient descent sau đó cập nhật trọng số để tối thiểu hóa mất mát. "
                "Các biến thể như SGD, Adam, và RMSprop cải thiện tốc độ hội tụ."
            ),
            element_type=ElementType.PARAGRAPH, page_number=5,
        ),
        Section(
            content="2.2 Hàm Kích Hoạt",
            element_type=ElementType.HEADING,
            heading="2.2 Hàm Kích Hoạt",
            heading_level=2, page_number=6,
        ),
        Section(
            content=(
                "Hàm kích hoạt đưa vào tính phi tuyến tính trong mạng nơ-ron. "
                "Không có hàm kích hoạt, mạng nhiều lớp vẫn chỉ là biến đổi tuyến tính. "
                "ReLU (Rectified Linear Unit) là hàm kích hoạt phổ biến nhất hiện nay. "
                "Sigmoid và tanh được dùng khi cần đầu ra trong khoảng [0,1] hoặc [-1,1]."
            ),
            element_type=ElementType.PARAGRAPH, page_number=6,
        ),
    ]
    return ParsedDocument(
        document=doc, sections=sections, page_count=6, language="vi"
    )


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------

def chunk_stats(chunks) -> dict:
    if not chunks:
        return {"count": 0}
    sizes = [len(c.content) for c in chunks]
    return {
        "count": len(chunks),
        "total_chars": sum(sizes),
        "avg_chars": statistics.mean(sizes),
        "median_chars": statistics.median(sizes),
        "min_chars": min(sizes),
        "max_chars": max(sizes),
        "stdev": statistics.stdev(sizes) if len(sizes) > 1 else 0,
        "with_heading": sum(1 for c in chunks if c.metadata.heading_path),
        "heading_coverage_pct": (
            sum(1 for c in chunks if c.metadata.heading_path) / len(chunks) * 100
        ),
    }


def print_stats_table(results: dict[str, list]) -> None:
    metrics = [
        ("Số chunks", "count", "{:.0f}"),
        ("Tổng chars", "total_chars", "{:.0f}"),
        ("Avg chars/chunk", "avg_chars", "{:.0f}"),
        ("Median chars", "median_chars", "{:.0f}"),
        ("Min chars", "min_chars", "{:.0f}"),
        ("Max chars", "max_chars", "{:.0f}"),
        ("Std deviation", "stdev", "{:.0f}"),
        ("Chunks có heading", "with_heading", "{:.0f}"),
        ("Heading coverage %", "heading_coverage_pct", "{:.1f}%"),
    ]

    chunker_names = list(results.keys())
    col_w = 18
    print(f"\n{'Metric':<22}", end="")
    for name in chunker_names:
        print(f"{name:>{col_w}}", end="")
    print()
    print("-" * (22 + col_w * len(chunker_names)))

    for label, key, fmt in metrics:
        print(f"{label:<22}", end="")
        for name in chunker_names:
            stats = results[name]
            val = stats.get(key, 0)
            print(f"{fmt.format(val):>{col_w}}", end="")
        print()


def print_sample_chunks(chunker_name: str, chunks: list, n: int = 2) -> None:
    print(f"\n--- {chunker_name}: {n} chunks đầu tiên ---")
    for i, chunk in enumerate(chunks[:n]):
        heading = " > ".join(chunk.metadata.heading_path) if chunk.metadata.heading_path else "(no heading)"
        print(f"  [Chunk {i}] heading: {heading}")
        print(f"  chars: {len(chunk.content)}")
        preview = chunk.content[:150].replace("\n", " ")
        print(f"  preview: {preview}...")
        print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="So sánh chunking strategies")
    parser.add_argument("--file", type=str, default=None, help="Path đến file PDF/DOCX để test thực")
    parser.add_argument("--semantic", action="store_true", help="Thêm SemanticSplitter (cần embedding, chậm hơn)")
    parser.add_argument("--max-chunk", type=int, default=800, help="max_chunk_size cho HeadingChunker (chars)")
    parser.add_argument("--token-size", type=int, default=200, help="chunk_size cho LlamaIndex chunkers (tokens)")
    args = parser.parse_args()

    # Config
    hc_config = ChunkerConfig(
        max_chunk_size=args.max_chunk,
        min_chunk_size=50,
        overlap_size=100,
    )
    li_config = LlamaIndexChunkerConfig(
        chunk_size=args.token_size,
        chunk_overlap=25,
    )

    # Chunkers
    try:
        from src.adapters.chunkers.llamaindex_chunkers import (
            LlamaIndexSentenceChunker,
            LlamaIndexTokenChunker,
        )
    except ImportError:
        print("ERROR: llama-index-core chưa cài.")
        print("Chạy: pip install 'document-chunk[llamaindex]'")
        sys.exit(1)

    chunkers = {
        "HeadingChunker": HeadingChunker(hc_config),
        "LI-SentenceSplitter": LlamaIndexSentenceChunker(li_config),
        "LI-TokenSplitter": LlamaIndexTokenChunker(li_config),
    }

    if args.semantic:
        from src.adapters.chunkers.llamaindex_chunkers import LlamaIndexSemanticChunker
        chunkers["LI-SemanticSplitter"] = LlamaIndexSemanticChunker(li_config)

    # Document
    if args.file:
        print(f"Đang parse file: {args.file}")
        from src.infrastructure.container import Container
        container = Container()
        path = Path(args.file)
        from src.domain.entities.document import Document, DocumentType
        import mimetypes
        mime, _ = mimetypes.guess_type(str(path))
        ext = path.suffix.lower().lstrip(".")
        dtype_map = {"pdf": DocumentType.PDF, "docx": DocumentType.DOCX,
                     "pptx": DocumentType.PPTX, "md": DocumentType.MARKDOWN}
        doc_obj = Document(
            id="cli-test", name=path.name, path=path,
            doc_type=dtype_map.get(ext, DocumentType.PDF),
            size_bytes=path.stat().st_size,
            mime_type=mime or "application/pdf",
        )
        # Find appropriate parser
        parsed_doc = None
        for p in container.parsers:
            if p.supports(doc_obj.doc_type):
                r = p.parse(doc_obj)
                if r.is_ok():
                    parsed_doc = r.unwrap()
                break
        if parsed_doc is None:
            print("ERROR: Không parse được file")
            sys.exit(1)
    else:
        print("Dùng document mẫu (tiếng Việt, học thuật)...")
        parsed_doc = make_sample_doc()

    print(f"\nDocument: {parsed_doc.document.name}")
    print(f"Sections: {len(parsed_doc.sections)}")
    print(f"Total content: {parsed_doc.total_content_length} chars")
    print(f"\nConfig:")
    print(f"  HeadingChunker: max={args.max_chunk} chars, overlap=100 chars")
    print(f"  LlamaIndex:     chunk_size={args.token_size} tokens (~{args.token_size * 4} chars), overlap=25 tokens")

    # Run chunkers
    all_stats = {}
    all_chunks = {}
    print("\nRunning chunkers", end="", flush=True)

    for name, chunker in chunkers.items():
        print(f"  {name}...", end="", flush=True)
        result = chunker.chunk(parsed_doc)
        if result.is_ok():
            chunks = result.unwrap()
            all_chunks[name] = chunks
            all_stats[name] = chunk_stats(chunks)
            print(f" → {len(chunks)} chunks")
        else:
            print(f" → ERROR: {result.error}")
            all_stats[name] = {"count": 0, "error": str(result.error)}

    # Print comparison table
    print("\n" + "=" * 80)
    print("BẢNG SO SÁNH CHUNKING STRATEGIES")
    print("=" * 80)
    print_stats_table(all_stats)

    # Sample chunks
    print("\n" + "=" * 80)
    print("SAMPLE CHUNKS (2 đầu tiên của mỗi chunker)")
    print("=" * 80)
    for name, chunks in all_chunks.items():
        print_sample_chunks(name, chunks, n=2)

    # Analysis notes
    print("=" * 80)
    print("NHẬN XÉT")
    print("=" * 80)
    if "HeadingChunker" in all_stats and "LI-SentenceSplitter" in all_stats:
        hc_n = all_stats["HeadingChunker"].get("count", 0)
        sc_n = all_stats["LI-SentenceSplitter"].get("count", 0)
        tc_n = all_stats.get("LI-TokenSplitter", {}).get("count", 0)
        print(f"• HeadingChunker: {hc_n} chunks — heading-aware, char-based, min-size merging")
        print(f"• SentenceSplitter: {sc_n} chunks — token-based, sentence boundary, no merge")
        if tc_n:
            print(f"• TokenSplitter: {tc_n} chunks — token-based, cứng, có thể cắt giữa câu")
        if "LI-SemanticSplitter" in all_stats:
            sn = all_stats["LI-SemanticSplitter"].get("count", 0)
            print(f"• SemanticSplitter: {sn} chunks — embedding-based boundaries, không theo heading")

        hc_cov = all_stats["HeadingChunker"].get("heading_coverage_pct", 0)
        sc_cov = all_stats["LI-SentenceSplitter"].get("heading_coverage_pct", 0)
        print(f"\n• Heading coverage: HeadingChunker={hc_cov:.0f}% vs SentenceSplitter={sc_cov:.0f}%")
        if hc_cov > sc_cov:
            print("  → HeadingChunker bảo toàn heading context tốt hơn do merging strategy")

    print()


if __name__ == "__main__":
    main()
