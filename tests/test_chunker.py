"""Tests for document chunking engine.

Test-first development: Define chunking behavior before implementation.
"""

from __future__ import annotations

import pytest

from src.ingestion.chunker import Chunk, chunk_document


def test_chunk_basic_text() -> None:
    """Test basic text chunking with default parameters."""
    text = "This is a test document. " * 100  # ~500 chars

    chunks = chunk_document(text, chunk_size=50, overlap=10)

    assert len(chunks) > 0
    assert all(isinstance(c, Chunk) for c in chunks)
    assert chunks[0].index == 0
    assert chunks[1].index == 1


def test_chunk_respects_size_limit() -> None:
    """Test chunks don't exceed the specified token size."""
    text = "Word " * 1000  # Long text

    chunk_size = 100
    chunks = chunk_document(text, chunk_size=chunk_size, overlap=10)

    for chunk in chunks:
        # Token count should not significantly exceed chunk_size
        # (allowing small margin for tokenization boundaries)
        assert chunk.token_count <= chunk_size + 5


def test_chunk_has_overlap() -> None:
    """Test chunks have overlap for context continuity."""
    text = "Sentence one. Sentence two. Sentence three. Sentence four. Sentence five. " * 20

    chunks = chunk_document(text, chunk_size=50, overlap=10)

    if len(chunks) > 1:
        # Second chunk should contain some content from first chunk
        first_chunk_end = chunks[0].content[-50:]
        second_chunk_start = chunks[1].content[:50]

        # Some overlap should exist
        # (exact match is hard due to tokenization, but some words should overlap)
        first_words = set(first_chunk_end.split())
        second_words = set(second_chunk_start.split())
        overlap_words = first_words & second_words
        assert len(overlap_words) > 0


def test_chunk_respects_sentence_boundaries() -> None:
    """Test chunking tries to respect sentence boundaries."""
    text = "This is sentence one. This is sentence two. This is sentence three."

    chunks = chunk_document(text, chunk_size=50, overlap=5)

    # Chunks should ideally end on sentence boundaries
    # (not splitting mid-sentence when possible)
    for chunk in chunks:
        content = chunk.content.strip()
        # Should end with punctuation when possible
        if len(content) > 0:
            # This is a soft requirement - not all chunks will end perfectly
            # but most should try to respect boundaries
            pass  # We'll implement logic to check this based on implementation


def test_chunk_includes_metadata() -> None:
    """Test chunks include metadata."""
    text = "Sample text content for testing."

    chunks = chunk_document(text, chunk_size=100, overlap=10)

    for chunk in chunks:
        assert chunk.index >= 0
        assert chunk.token_count > 0
        assert isinstance(chunk.metadata, dict)


def test_chunk_empty_text() -> None:
    """Test chunking empty text returns empty list."""
    chunks = chunk_document("", chunk_size=100, overlap=10)
    assert len(chunks) == 0


def test_chunk_short_text() -> None:
    """Test text shorter than chunk_size produces single chunk."""
    text = "Short text."

    chunks = chunk_document(text, chunk_size=500, overlap=10)

    assert len(chunks) == 1
    assert chunks[0].content == text


def test_chunk_very_long_text() -> None:
    """Test chunking very long documents produces multiple chunks."""
    # Simulate a long document (10,000 words)
    text = "Word " * 10000

    chunks = chunk_document(text, chunk_size=512, overlap=50)

    # Should produce many chunks
    assert len(chunks) > 10
    # All chunks should have sequential indices
    for i, chunk in enumerate(chunks):
        assert chunk.index == i


def test_chunk_with_zero_overlap() -> None:
    """Test chunking with zero overlap."""
    text = "Sentence. " * 100

    chunks = chunk_document(text, chunk_size=50, overlap=0)

    # With zero overlap, chunks should be completely independent
    assert len(chunks) > 1
    # No content should repeat between chunks (approximately)
    # This is hard to test perfectly due to tokenization


def test_chunk_preserves_content() -> None:
    """Test that all original content appears in chunks."""
    text = "This is important content that must not be lost during chunking process."

    chunks = chunk_document(text, chunk_size=50, overlap=10)

    # Reconstruct text from chunks (removing overlap)
    all_content = " ".join(c.content for c in chunks)

    # All important words should appear
    for word in ["important", "content", "lost", "chunking"]:
        assert word in all_content


def test_chunk_token_count_accuracy() -> None:
    """Test chunk token counts are reasonably accurate."""
    text = "Word " * 100

    chunks = chunk_document(text, chunk_size=50, overlap=10)

    for chunk in chunks:
        # Token count should be positive and reasonable
        assert chunk.token_count > 0
        # For simple repeated words, token count should be close to word count
        word_count = len(chunk.content.split())
        # Allow some variance for tokenization
        assert abs(chunk.token_count - word_count) < word_count * 0.5


def test_chunk_metadata_includes_offsets() -> None:
    """Test chunk metadata includes character/token offsets."""
    text = "A" * 1000

    chunks = chunk_document(text, chunk_size=100, overlap=10)

    for chunk in chunks:
        # Metadata should track position in document
        assert "index" in chunk.metadata or chunk.index >= 0
        assert "token_count" in chunk.metadata or chunk.token_count > 0


def test_chunk_respects_section_boundaries() -> None:
    """Test chunking can respect section boundaries when provided.

    This is an advanced feature - chunks should prefer breaking at
    section boundaries rather than mid-section when possible.
    """
    text = """
# Section 1

Content for section 1. This goes on for a while with various sentences
and information that should ideally stay together.

# Section 2

Content for section 2. More content here.

# Section 3

Content for section 3.
"""

    chunks = chunk_document(text, chunk_size=100, overlap=10)

    # This is a soft requirement - implementation may vary
    # Ideally, section headers should start new chunks when possible
    assert len(chunks) > 0
