"""
Tests for adapters/chunkers/toc_detector.py — rule-based Table of Contents detection.
"""
import pytest

from src.adapters.chunkers.toc_detector import annotate_toc, detect_toc
from src.domain.entities.document import ElementType, Section


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _section(content: str, heading: str | None = None, heading_level: int = 0) -> Section:
    element_type = ElementType.HEADING if heading else ElementType.PARAGRAPH
    return Section(
        content=content,
        element_type=element_type,
        heading=heading,
        heading_level=heading_level,
    )


# ---------------------------------------------------------------------------
# detect_toc
# ---------------------------------------------------------------------------

class TestDetectToc:
    def test_empty_sections(self):
        assert detect_toc([]) == set()

    def test_no_toc_in_regular_text(self):
        sections = [
            _section("This is a paragraph about machine learning."),
            _section("Another paragraph with details."),
            _section("Conclusion and summary."),
        ]
        assert detect_toc(sections) == set()

    def test_detects_vietnamese_toc(self):
        sections = [
            _section("Mục lục", heading="Mục lục", heading_level=1),
            _section("1. Giới thiệu .............. 3"),
            _section("2. Phương pháp .............. 12"),
            _section("3. Kết luận .............. 25"),
        ]
        result = detect_toc(sections)
        assert result == {0, 1, 2, 3}

    def test_detects_english_toc(self):
        sections = [
            _section("Table of Contents", heading="Table of Contents", heading_level=1),
            _section("1. Introduction .............. 1"),
            _section("2. Background .............. 5"),
            _section("3. Methods .............. 15"),
            _section("4. Results .............. 30"),
        ]
        result = detect_toc(sections)
        assert result == {0, 1, 2, 3, 4}

    def test_toc_stops_at_non_toc_section(self):
        sections = [
            _section("Mục lục", heading="Mục lục", heading_level=1),
            _section("1. Giới thiệu .............. 3"),
            _section("2. Phương pháp .............. 12"),
            _section(
                "CHƯƠNG 1: GIỚI THIỆU\n\nHọc máy là một nhánh của trí tuệ nhân tạo...\n\n"
                "Trong chương này chúng ta sẽ tìm hiểu về các khái niệm cơ bản."
            ),
            _section("Tiếp tục nội dung chương 1 với nhiều chi tiết hơn về các thuật toán."),
        ]
        result = detect_toc(sections)
        assert 3 not in result  # not a TOC line

    def test_no_toc_keyword_no_dot_lines(self):
        sections = [
            _section("Chapter 1: Introduction"),
            _section("This is the first chapter."),
            _section("Chapter 2: Background"),
        ]
        # Only position score (1) for first sections, not enough to trigger
        result = detect_toc(sections)
        assert result == set()

    def test_short_sections_no_dot(self):
        sections = [
            _section("Heading", heading="Heading", heading_level=1),
            _section("Short"),
            _section("Short"),
            _section("Short"),
            _section("Short"),
            _section("Short"),
        ]
        # short lines + position may give score >= 2
        result = detect_toc(sections)
        assert 0 in result or result == set()

    def test_late_toc_beyond_cutoff_ignored(self):
        # Create 50 sections before a TOC-like section
        sections = [_section(f"Section {i} content text here.") for i in range(50)]
        sections.append(_section("Mục lục", heading="Mục lục", heading_level=1))
        sections.append(_section("1. Nội dung .............. 3"))
        result = detect_toc(sections)
        # TOC keyword is too late; should not be detected
        assert result == set()

    def test_keyword_in_content_not_heading(self):
        sections = [
            _section("This chapter has mục lục but isn't one"),
            _section("1. Content .............. 10"),
        ]
        result = detect_toc(sections)
        # "mục lục" in short content (<=60 chars) gives +2, position gives +1 => >=2
        # But dot ratio is 0/1 = 0, so no bonus from dots
        # score = 2 (keyword in short content) + 1 (position) = 3 >= 2 -> TOC anchor
        # Next line has dot ratio 1/1 -> 2 >= 2 -> continues
        assert result == {0, 1}


# ---------------------------------------------------------------------------
# annotate_toc
# ---------------------------------------------------------------------------

class TestAnnotateToc:
    def test_returns_original_if_no_toc(self):
        sections = [
            _section("Regular text"),
            _section("More text"),
        ]
        result = annotate_toc(sections)
        assert result is sections  # same object reference
        assert not any(s.is_toc for s in result)

    def test_marks_toc_sections(self):
        sections = [
            _section("Mục lục", heading="Mục lục", heading_level=1),
            _section("1. Giới thiệu .............. 3"),
            _section("Nội dung chính bắt đầu từ đây với nhiều thông tin chi tiết."),
        ]
        result = annotate_toc(sections)
        assert result is not sections  # new list
        assert result[0].is_toc is True
        assert result[1].is_toc is True
        assert result[2].is_toc is False

    def test_does_not_modify_original(self):
        sections = [
            _section("Mục lục", heading="Mục lục", heading_level=1),
            _section("1. Giới thiệu .............. 3"),
            _section("Nội dung chính..."),
        ]
        result = annotate_toc(sections)
        assert sections[0].is_toc is False  # original unchanged
        assert result[0].is_toc is True     # new has TOC flag
