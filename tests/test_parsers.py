"""Tests for document parsers.

Test-first development: Define parser contracts before implementation.
"""

from __future__ import annotations

import pytest

from src.ingestion.parsers import (
    BaseParser,
    HTMLParser,
    MarkdownParser,
    ParseResult,
    PDFParser,
    TextParser,
    get_parser,
)
from src.models.ingestion import FileType


def test_parse_result_structure() -> None:
    """Test ParseResult dataclass structure."""
    result = ParseResult(
        text="Sample text content",
        metadata={"title": "Test Document", "author": "Test Author"},
        sections=[{"heading": "Introduction", "content": "Sample intro"}],
    )

    assert result.text == "Sample text content"
    assert result.metadata["title"] == "Test Document"
    assert len(result.sections) == 1


def test_get_parser_returns_correct_parser() -> None:
    """Test parser factory returns the right parser for each file type."""
    assert isinstance(get_parser(FileType.PDF), PDFParser)
    assert isinstance(get_parser(FileType.MARKDOWN), MarkdownParser)
    assert isinstance(get_parser(FileType.TEXT), TextParser)
    assert isinstance(get_parser(FileType.HTML), HTMLParser)


def test_get_parser_raises_for_unsupported_type() -> None:
    """Test parser factory raises error for unsupported file types."""
    with pytest.raises(ValueError, match="No parser available"):
        get_parser(FileType.DOCX)  # Not implemented yet


@pytest.mark.asyncio
async def test_text_parser_basic(tmp_path) -> None:
    """Test TextParser with a simple text file."""
    # Create a test file
    test_file = tmp_path / "test.txt"
    test_file.write_text("This is a test document.\n\nIt has multiple paragraphs.\n\nEnd.")

    parser = TextParser()
    result = await parser.parse(str(test_file))

    assert "This is a test document" in result.text
    assert "It has multiple paragraphs" in result.text
    assert result.metadata is not None


@pytest.mark.asyncio
async def test_markdown_parser_with_frontmatter(tmp_path) -> None:
    """Test MarkdownParser extracts frontmatter metadata."""
    # Create test markdown file with frontmatter
    test_file = tmp_path / "test.md"
    content = """---
title: Test Document
author: Test Author
date: 2026-02-17
---

# Introduction

This is a test markdown document.

## Section 1

Content here.
"""
    test_file.write_text(content)

    parser = MarkdownParser()
    result = await parser.parse(str(test_file))

    # Should extract frontmatter as metadata
    assert result.metadata.get("title") == "Test Document"
    assert result.metadata.get("author") == "Test Author"
    assert result.metadata.get("date") == "2026-02-17"

    # Should include markdown content (without frontmatter)
    assert "# Introduction" in result.text or "Introduction" in result.text
    assert "Section 1" in result.text


@pytest.mark.asyncio
async def test_markdown_parser_without_frontmatter(tmp_path) -> None:
    """Test MarkdownParser with plain markdown (no frontmatter)."""
    test_file = tmp_path / "simple.md"
    content = """# Simple Document

Just plain markdown content without any frontmatter.

- Item 1
- Item 2
"""
    test_file.write_text(content)

    parser = MarkdownParser()
    result = await parser.parse(str(test_file))

    assert "Simple Document" in result.text
    assert "Item 1" in result.text
    assert result.metadata is not None  # Empty dict is fine


@pytest.mark.asyncio
async def test_html_parser_strips_tags(tmp_path) -> None:
    """Test HTMLParser extracts text and strips HTML tags."""
    test_file = tmp_path / "test.html"
    content = """
<!DOCTYPE html>
<html>
<head>
    <title>Test Page</title>
</head>
<body>
    <h1>Main Heading</h1>
    <p>This is a <strong>test</strong> paragraph with <a href="#">links</a>.</p>
    <script>console.log('should be removed');</script>
</body>
</html>
"""
    test_file.write_text(content)

    parser = HTMLParser()
    result = await parser.parse(str(test_file))

    # Should extract text content
    assert "Main Heading" in result.text
    assert "test paragraph" in result.text

    # Should strip HTML tags
    assert "<strong>" not in result.text
    assert "<a href=" not in result.text

    # Should remove script tags
    assert "console.log" not in result.text


@pytest.mark.asyncio
async def test_pdf_parser_basic(tmp_path) -> None:
    """Test PDFParser extracts text from PDF.

    Note: This test requires pypdf. For now, we test the interface.
    A real PDF would be needed for integration testing.
    """
    parser = PDFParser()
    assert isinstance(parser, BaseParser)
    assert hasattr(parser, "parse")


def test_parser_sections_detection() -> None:
    """Test that parsers can detect document sections.

    This is a contract test - parsers should identify sections
    when possible (headings in markdown, headings in HTML, etc.)
    """
    result = ParseResult(
        text="Full document text",
        metadata={},
        sections=[
            {"heading": "Introduction", "content": "Intro text"},
            {"heading": "Methods", "content": "Methods text"},
            {"heading": "Results", "content": "Results text"},
        ],
    )

    assert len(result.sections) == 3
    assert result.sections[0]["heading"] == "Introduction"
    assert result.sections[1]["heading"] == "Methods"


@pytest.mark.asyncio
async def test_parser_handles_empty_file(tmp_path) -> None:
    """Test parsers handle empty files gracefully."""
    test_file = tmp_path / "empty.txt"
    test_file.write_text("")

    parser = TextParser()
    result = await parser.parse(str(test_file))

    assert result.text == ""
    assert isinstance(result.metadata, dict)


@pytest.mark.asyncio
async def test_parser_handles_large_file(tmp_path) -> None:
    """Test parsers can handle large files."""
    test_file = tmp_path / "large.txt"
    # Create a 1MB text file
    large_content = "Lorem ipsum dolor sit amet. " * 50000
    test_file.write_text(large_content)

    parser = TextParser()
    result = await parser.parse(str(test_file))

    assert len(result.text) > 1000000
    assert "Lorem ipsum" in result.text
