"""Citation formatting and source tracking.

Citations connect agent responses back to the source documents. Each citation
includes:
- Sequence number (used in the response as [1], [2], etc.)
- Document name and version
- Page/chunk location
- The verbatim chunk content for verification

Citations are stored as JSON in the message record so they are retrievable
without re-running retrieval.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class Citation:
    """A single source citation with location metadata."""
    index: int          # 1-based citation number
    document_id: str
    document_name: str
    document_version: str
    chunk_index: int
    content_snippet: str  # First 200 chars of the chunk
    page_number: int | None = None
    section: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def format_inline(self) -> str:
        """Format as inline citation marker: [1]"""
        return f"[{self.index}]"

    def format_reference(self) -> str:
        """Format as numbered reference entry."""
        parts = [
            f"[{self.index}] {self.document_name}",
            f"v{self.document_version}",
        ]
        if self.page_number is not None:
            parts.append(f"page {self.page_number}")
        if self.section:
            parts.append(f"§ {self.section}")
        parts.append(f"(chunk {self.chunk_index})")
        return ", ".join(parts)


def build_citations(
    chunks: list[dict[str, Any]],
) -> list[Citation]:
    """Convert retrieved chunks into Citation objects.

    Args:
        chunks: List of chunk dicts from RetrievalService.retrieve()
                Each has: document_id, document_name, version,
                          chunk_index, content, metadata
    """
    citations = []
    for i, chunk in enumerate(chunks, start=1):
        meta = chunk.get("metadata", {})
        snippet = chunk.get("content", "")[:200]
        if len(chunk.get("content", "")) > 200:
            snippet += "..."

        citation = Citation(
            index=i,
            document_id=str(chunk.get("document_id", "")),
            document_name=chunk.get("document_name", "Unknown Document"),
            document_version=chunk.get("document_version", "1.0"),
            chunk_index=chunk.get("chunk_index", 0),
            content_snippet=snippet,
            page_number=meta.get("page_number"),
            section=meta.get("section"),
        )
        citations.append(citation)
    return citations


def format_citations_for_prompt(citations: list[Citation]) -> str:
    """Format citations as a readable block for inclusion in the system prompt.

    The LLM is instructed to reference these using [1], [2], etc.
    """
    if not citations:
        return ""

    lines = []
    for citation in citations:
        lines.append(f"{citation.format_reference()}:")
        lines.append(f"  {citation.content_snippet}")
        lines.append("")

    return "\n".join(lines)


def format_citations_for_response(citations: list[Citation]) -> str:
    """Format citations as a numbered list for end-of-response references."""
    if not citations:
        return ""

    lines = ["\n\n**Sources:**"]
    for c in citations:
        lines.append(f"- {c.format_reference()}")
    return "\n".join(lines)
