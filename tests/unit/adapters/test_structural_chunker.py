import random
import re
from dataclasses import replace
from pathlib import Path

import pytest
from tokenizers import Tokenizer, models, trainers, pre_tokenizers

from document_chunk.adapters.chunkers.structural import StructuralChunker, build_chunker
from document_chunk.adapters.chunkers.structural.tokens import TokenCounter
from document_chunk.adapters.chunkers.structural.segment import sentences
from document_chunk.domain.entities.document import (
    Document,
    DocumentType,
    ElementType,
    ParsedDocument,
    Section,
)
from document_chunk.infrastructure.config import ChunkerConfig, EmbedderConfig


@pytest.fixture
def counter(tmp_path):
    tokenizer = Tokenizer(models.BPE(unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
    tokenizer.train_from_iterator(
        ["Tiếng Việt English 0123 | --- Code 🧑‍💻 中文"],
        trainers.BpeTrainer(
            vocab_size=300,
            special_tokens=["[UNK]"],
            initial_alphabet=pre_tokenizers.ByteLevel.alphabet(),
        ),
    )
    path = tmp_path / "tokenizer.json"
    tokenizer.save(str(path))
    return TokenCounter(ChunkerConfig(tokenizer_path=str(path)))


def document(sections, kind=DocumentType.MARKDOWN):
    return ParsedDocument(Document("doc-1", "test", None, kind, 0, "text/plain"), sections, 1)


def heading(title, level=1):
    return Section(title, ElementType.HEADING, title, level)


def body(text, kind=ElementType.PARAGRAPH, **kwargs):
    return Section(text, kind, **kwargs)


def run(counter, sections, **options):
    cfg = ChunkerConfig(strategy="structural", **options)
    chunker = StructuralChunker(cfg, counter)
    result = chunker.chunk(document(sections))
    assert result.is_ok(), str(result.error) if result.is_err() else ""
    chunks = result.unwrap()
    for i, chunk in enumerate(chunks):
        assert counter.count(chunk.content) <= cfg.max_tokens
        assert counter.count(chunk.embedding_input) <= cfg.embed_max_tokens - 2
        assert chunk.metadata.token_count == counter.count(chunk.content)
        assert chunk.metadata.chunk_index == i
    return chunks


def test_chapters_never_merge(counter):
    chunks = run(counter, [heading("Chương 1"), body("Một."), heading("Chương 2"), body("Hai.")])
    assert [c.metadata.heading_path for c in chunks] == [("Chương 1",), ("Chương 2",)]


def test_lcp_keeps_child_title(counter):
    chunks = run(counter, [heading("Parent"), body("Intro."), heading("Child", 3), body("Body.")])
    assert len(chunks) == 1
    assert chunks[0].metadata.heading_path == ("Parent",)
    assert "Child" in chunks[0].content


def test_table_200_rows_repeat_header(counter):
    header = "| id | value |\n| --- | --- |"
    rows = [f"| {i} | value number {i} |" for i in range(200)]
    chunks = run(
        counter, [heading("Table"), body(header + "\n" + "\n".join(rows), ElementType.TABLE)]
    )
    assert len(chunks) > 1
    assert all(c.content.startswith(header) for c in chunks)
    recovered = [line for c in chunks for line in c.content.splitlines()[2:]]
    assert recovered == rows


def test_huge_table_header_preserved(counter):
    text = "| " + "long " * 600 + " |\n| --- |\n| value |"
    chunks = run(counter, [body(text, ElementType.TABLE)])
    assert re.sub(r"\s+", "", "".join(c.content for c in chunks)) == re.sub(r"\s+", "", text)


def test_long_word_unicode_hard_cap(counter):
    text = "🧑‍💻中文" * 500
    chunks = run(counter, [body(text)])
    assert "".join(c.content for c in chunks) == text


def test_code_indent_preserved(counter):
    text = "".join(f"    value_{i} = {i}\n" for i in range(300))
    chunks = run(counter, [body(text, ElementType.CODE)])
    assert "".join(c.content for c in chunks) == text.rstrip("\n")


def test_toc_excluded_but_learning_objectives_remain(counter):
    chunks = run(
        counter,
        [
            heading("Mục lục"),
            body("Chương 1 .... 1\nChương 2 .... 20"),
            heading("Mục tiêu học tập"),
            body("- Một\n- Hai\n- Ba\n- Bốn\n- Năm", ElementType.LIST),
        ],
    )
    assert all("...." not in c.content for c in chunks)
    assert any("Một" in c.content for c in chunks)


def test_toc_retained_is_isolated(counter):
    chunks = run(
        counter,
        [heading("Mục lục"), body("Chapter .... 1"), heading("Actual"), body("Text")],
        index_toc=True,
    )
    assert any(c.metadata.content_type == "toc" for c in chunks)
    assert all(c.metadata.content_type != "mixed" for c in chunks)


def test_prose_conservation_and_overlap(counter):
    text = " ".join(f"Sentence {i} has useful information." for i in range(80))
    chunks = run(counter, [heading("Chapter"), body(text)])
    assert " ".join(c.content.strip() for c in chunks) == text
    assert len(chunks) > 1
    assert any(len(c.embedding_input) > len(c.content) + len("Chapter\n\n") for c in chunks[1:])


def test_deterministic_and_heading_change_changes_id(counter):
    sections = [heading("A"), body("Some text.")]
    first = run(counter, sections)
    assert first == run(counter, sections)
    changed = run(counter, [heading("B"), sections[1]])
    assert first[0].id != changed[0].id


def test_images_pages_and_skipped_heading_levels(counter):
    chunks = run(
        counter,
        [
            heading("Root"),
            heading("Child", 3),
            body("Body", page_number=2),
            body("[Image]", ElementType.IMAGE, images=("image.png",)),
            body("End", page_number=4),
        ],
    )
    assert chunks[0].metadata.heading_level == 3
    assert chunks[0].metadata.page_start == 2
    assert chunks[0].metadata.page_end == 4
    assert chunks[0].images == ["image.png"]


def test_empty(counter):
    assert run(counter, []) == []


def test_seeded_documents(counter):
    rng = random.Random(42)
    for _ in range(15):
        sections, originals = [], []
        for index in range(rng.randint(2, 12)):
            sections.append(heading(f"Chapter {index}"))
            text = " ".join(f"word{rng.randrange(1000)}" for _ in range(rng.randint(1, 400)))
            originals.append(text)
            sections.append(body(text))
        chunks = run(counter, sections)
        assert re.sub(r"\s+", "", "".join(c.content for c in chunks)) == re.sub(
            r"\s+", "", "".join(originals)
        )


def test_sentence_abbreviations():
    assert list(sentences("TS. Nam xem Hình 2.3. Sau đó tiếp tục. Kết thúc.")) == [
        "TS. Nam xem Hình 2.3. Sau đó tiếp tục.",
        "Kết thúc.",
    ]


def test_invalid_config_and_embedder():
    with pytest.raises(ValueError):
        ChunkerConfig(max_tokens=20)
    with pytest.raises(ValueError):
        build_chunker(ChunkerConfig(strategy="structural"), EmbedderConfig(max_length=512))


def test_missing_tokenizer_fails_closed(tmp_path):
    with pytest.raises(ValueError, match="exact tokenizer"):
        TokenCounter(ChunkerConfig(tokenizer_path=str(tmp_path / "missing.json")))


def test_counter_disables_saved_truncation(counter, tmp_path):
    counter.tokenizer.enable_truncation(2)
    p = tmp_path / "truncated.json"
    counter.tokenizer.save(str(p))
    exact = TokenCounter(ChunkerConfig(tokenizer_path=str(p)))
    assert exact.count("long text " * 100) > 2
    assert exact.count_many(["hi", "test"]) == [exact.count("hi"), exact.count("test")]


def test_pptx_deck_title_continuation_and_tables(counter):
    doc = document(
        [
            Section("Opening description for the deck.", ElementType.SLIDE, "Intro", 1, 1),
            Section(
                "Enough body content to be a normal slide with details to learn and examples to study.",
                ElementType.SLIDE,
                "SQL",
                1,
                2,
            ),
            Section("More examples to study.", ElementType.SLIDE, "SQL (tiếp)", 1, 3),
        ],
        DocumentType.PPTX,
    )
    doc.metadata["title"] = "Lesson 3"
    cfg = ChunkerConfig(strategy="structural", min_tokens=0)
    chunks = StructuralChunker(cfg, counter).chunk(doc).unwrap()
    assert all(c.metadata.heading_path[0] == "Lesson 3" for c in chunks)
    assert sum("SQL" in c.metadata.heading_path for c in chunks) == 1
    assert chunks[-1].metadata.page_end == 3


def test_real_bge_tokenizer_budget():
    # Optional local integration check, no network/download required by CI.
    paths = list(
        (Path.home() / ".cache/huggingface/hub/models--BAAI--bge-m3/snapshots").glob(
            "*/tokenizer.json"
        )
    )
    if not paths:
        pytest.skip("BGE-m3 tokenizer is not cached locally")
    exact = TokenCounter(ChunkerConfig(tokenizer_path=str(paths[0])))
    chunks = run(
        exact,
        [
            heading("Chương 2: Mô hình quan hệ"),
            body(
                "| Mã | Nội dung |\n| --- | --- |\n"
                + "\n".join(f"| {i} | Cơ sở dữ liệu quan hệ 🧑‍💻 中文 |" for i in range(200)),
                ElementType.TABLE,
            ),
        ],
    )
    assert len(chunks) > 1
