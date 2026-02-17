"""Document parsers for different file types.

Parsers extract text and metadata from various document formats.
Each parser implements the BaseParser ABC.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from src.models.ingestion import FileType

log = structlog.get_logger(__name__)


@dataclass
class ParseResult:
    """Result of parsing a document."""

    text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    sections: list[dict[str, Any]] = field(default_factory=list)


class BaseParser(ABC):
    """Abstract base class for document parsers."""

    @abstractmethod
    async def parse(self, file_path: str) -> ParseResult:
        """Parse a document and extract text and metadata.

        Args:
            file_path: Path to the file to parse

        Returns:
            ParseResult with extracted text, metadata, and sections

        Raises:
            IOError: If file cannot be read
            ValueError: If file format is invalid
        """
        raise NotImplementedError


class TextParser(BaseParser):
    """Parser for plain text files."""

    async def parse(self, file_path: str) -> ParseResult:
        """Parse plain text file."""
        path = Path(file_path)

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except UnicodeDecodeError:
            # Try with fallback encoding
            text = path.read_text(encoding="latin-1", errors="replace")

        # Detect basic sections (lines that look like headers)
        sections = self._detect_sections(text)

        return ParseResult(
            text=text,
            metadata={"filename": path.name, "encoding": "utf-8"},
            sections=sections,
        )

    def _detect_sections(self, text: str) -> list[dict[str, Any]]:
        """Detect basic section structure in plain text."""
        sections = []
        lines = text.split("\n")

        for i, line in enumerate(lines):
            line_stripped = line.strip()
            # Simple heuristic: all-caps lines or lines with specific patterns
            if line_stripped and (
                line_stripped.isupper()
                or re.match(r"^[A-Z][A-Z\s]{3,}$", line_stripped)
                or re.match(r"^\d+\.\s+[A-Z]", line_stripped)
            ):
                sections.append(
                    {
                        "heading": line_stripped,
                        "line_number": i,
                        "content": "",  # Content extraction would require more logic
                    }
                )

        return sections


class MarkdownParser(BaseParser):
    """Parser for Markdown files with frontmatter support."""

    async def parse(self, file_path: str) -> ParseResult:
        """Parse Markdown file, extracting frontmatter and content."""
        path = Path(file_path)
        content = path.read_text(encoding="utf-8", errors="replace")

        # Extract YAML frontmatter if present
        metadata, body = self._extract_frontmatter(content)
        metadata["filename"] = path.name

        # Extract sections from markdown headers
        sections = self._extract_sections(body)

        return ParseResult(
            text=body,
            metadata=metadata,
            sections=sections,
        )

    def _extract_frontmatter(self, content: str) -> tuple[dict[str, Any], str]:
        """Extract YAML frontmatter from markdown content."""
        if not content.startswith("---"):
            return {}, content

        # Find closing ---
        parts = content.split("---", 2)
        if len(parts) < 3:
            return {}, content

        frontmatter_raw = parts[1].strip()
        body = parts[2].strip()

        # Parse YAML frontmatter (simple key: value parsing)
        metadata = {}
        for line in frontmatter_raw.split("\n"):
            if ":" in line:
                key, value = line.split(":", 1)
                metadata[key.strip()] = value.strip()

        return metadata, body

    def _extract_sections(self, markdown: str) -> list[dict[str, Any]]:
        """Extract sections from markdown headers."""
        sections = []
        lines = markdown.split("\n")
        current_section = None
        current_content = []

        for line in lines:
            # Match markdown headers (# Header)
            header_match = re.match(r"^(#{1,6})\s+(.+)$", line)

            if header_match:
                # Save previous section
                if current_section:
                    current_section["content"] = "\n".join(current_content).strip()
                    sections.append(current_section)

                # Start new section
                level = len(header_match.group(1))
                heading = header_match.group(2).strip()
                current_section = {
                    "heading": heading,
                    "level": level,
                }
                current_content = []
            elif current_section:
                current_content.append(line)

        # Save last section
        if current_section:
            current_section["content"] = "\n".join(current_content).strip()
            sections.append(current_section)

        return sections


class HTMLParser(BaseParser):
    """Parser for HTML files - strips tags and extracts text."""

    async def parse(self, file_path: str) -> ParseResult:
        """Parse HTML file and extract plain text."""

        path = Path(file_path)
        html_content = path.read_text(encoding="utf-8", errors="replace")

        # Use simple HTML parser to extract text
        extractor = _HTMLTextExtractor()
        extractor.feed(html_content)

        text = extractor.get_text()
        metadata = {"filename": path.name, "title": extractor.title or ""}

        return ParseResult(
            text=text,
            metadata=metadata,
            sections=[],  # Could parse <h1>, <h2> as sections
        )


class _HTMLTextExtractor:
    """Helper class to extract text from HTML."""

    def __init__(self) -> None:
        from html.parser import HTMLParser

        self._parser = HTMLParser()
        self._text_parts: list[str] = []
        self._title: str | None = None
        self._in_script = False
        self._in_style = False
        self._in_title = False

    def feed(self, html: str) -> None:
        """Feed HTML content to parser."""
        # Simple regex-based extraction (good enough for basic HTML)
        # Remove script and style tags
        html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)

        # Extract title
        title_match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.DOTALL | re.IGNORECASE)
        if title_match:
            self._title = title_match.group(1).strip()

        # Remove all HTML tags
        text = re.sub(r"<[^>]+>", " ", html)

        # Clean up whitespace
        text = re.sub(r"\s+", " ", text)
        self._text_parts.append(text.strip())

    def get_text(self) -> str:
        """Get extracted text."""
        return " ".join(self._text_parts)

    @property
    def title(self) -> str | None:
        """Get extracted title."""
        return self._title


class PDFParser(BaseParser):
    """Parser for PDF files using pypdf."""

    async def parse(self, file_path: str) -> ParseResult:
        """Parse PDF and extract text with page numbers."""
        import pypdf

        path = Path(file_path)

        with open(file_path, "rb") as f:
            reader = pypdf.PdfReader(f)

            # Extract metadata
            metadata: dict[str, Any] = {
                "filename": path.name,
                "page_count": len(reader.pages),
            }

            # Try to extract PDF metadata
            if reader.metadata:
                if reader.metadata.title:
                    metadata["title"] = reader.metadata.title
                if reader.metadata.author:
                    metadata["author"] = reader.metadata.author
                if reader.metadata.creation_date:
                    metadata["creation_date"] = str(reader.metadata.creation_date)

            # Extract text from all pages
            all_text_parts = []
            sections = []

            for page_num, page in enumerate(reader.pages, start=1):
                page_text = page.extract_text() or ""
                if page_text.strip():
                    all_text_parts.append(page_text)
                    sections.append(
                        {
                            "heading": f"Page {page_num}",
                            "content": page_text,
                            "page_number": page_num,
                        }
                    )

            full_text = "\n\n".join(all_text_parts)

            return ParseResult(
                text=full_text,
                metadata=metadata,
                sections=sections,
            )


def get_parser(file_type: FileType) -> BaseParser:
    """Factory function to get the appropriate parser for a file type.

    Args:
        file_type: The type of file to parse

    Returns:
        A parser instance for that file type

    Raises:
        ValueError: If no parser is available for the file type
    """
    parsers = {
        FileType.TEXT: TextParser,
        FileType.MARKDOWN: MarkdownParser,
        FileType.HTML: HTMLParser,
        FileType.PDF: PDFParser,
    }

    parser_class = parsers.get(file_type)
    if parser_class is None:
        raise ValueError(
            f"No parser available for file type {file_type}. "
            f"Supported types: {', '.join(ft.value for ft in parsers)}"
        )

    return parser_class()
