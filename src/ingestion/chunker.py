"""Document chunking engine.

Chunks documents into overlapping token windows while respecting:
- Token size limits (hard limit)
- Sentence boundaries (soft preference)
- Section boundaries (soft preference)
- Context continuity via overlap
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import structlog
import tiktoken

log = structlog.get_logger(__name__)

_TOKENIZER_NAME = "cl100k_base"  # OpenAI tokenizer compatible with most models


@dataclass
class Chunk:
    """A chunk of text with metadata."""

    content: str
    index: int
    token_count: int
    metadata: dict[str, Any] = field(default_factory=dict)


def chunk_document(
    text: str,
    chunk_size: int = 512,
    overlap: int = 50,
) -> list[Chunk]:
    """Chunk a document into overlapping token windows.

    This function:
    1. Splits text into sentences
    2. Groups sentences into chunks of ~chunk_size tokens
    3. Respects sentence boundaries (doesn't split mid-sentence when possible)
    4. Falls back to hard token splitting when sentences are too long
    5. Adds overlap between chunks for context continuity
    6. Tracks token counts accurately using tiktoken

    Args:
        text: The text to chunk
        chunk_size: Target size in tokens for each chunk (hard limit)
        overlap: Number of tokens to overlap between chunks

    Returns:
        List of Chunk objects with content, index, and metadata
    """
    if not text or not text.strip():
        return []

    enc = tiktoken.get_encoding(_TOKENIZER_NAME)

    # Split text into sentences
    sentences = _split_into_sentences(text)

    if not sentences:
        return []

    # Build chunks respecting sentence boundaries
    chunks: list[Chunk] = []
    current_sentences: list[str] = []
    current_token_count = 0
    chunk_index = 0

    for sentence in sentences:
        sentence_tokens = len(enc.encode(sentence))

        # If single sentence exceeds chunk_size, split it by tokens
        if sentence_tokens > chunk_size:
            # First, flush any accumulated sentences
            if current_sentences:
                chunk_text = " ".join(current_sentences)
                token_count = len(enc.encode(chunk_text))
                chunks.append(
                    Chunk(
                        content=chunk_text,
                        index=chunk_index,
                        token_count=token_count,
                        metadata={
                            "sentence_count": len(current_sentences),
                            "index": chunk_index,
                            "token_count": token_count,
                        },
                    )
                )
                chunk_index += 1
                current_sentences = []
                current_token_count = 0

            # Hard-split the long sentence by tokens with overlap
            sub_chunks = _hard_split_by_tokens(
                text=sentence,
                enc=enc,
                chunk_size=chunk_size,
                overlap=overlap,
                start_index=chunk_index,
            )
            chunks.extend(sub_chunks)
            chunk_index += len(sub_chunks)
            continue

        # Check if adding this sentence would exceed chunk size
        if current_token_count + sentence_tokens > chunk_size and current_sentences:
            # Finalize current chunk
            chunk_text = " ".join(current_sentences)
            token_count = len(enc.encode(chunk_text))
            chunks.append(
                Chunk(
                    content=chunk_text,
                    index=chunk_index,
                    token_count=token_count,
                    metadata={
                        "sentence_count": len(current_sentences),
                        "index": chunk_index,
                        "token_count": token_count,
                    },
                )
            )
            chunk_index += 1

            # Start new chunk with overlap sentences from the end of the current chunk
            overlap_sentences = _get_overlap_sentences(current_sentences, overlap, enc)
            current_sentences = overlap_sentences
            current_token_count = sum(len(enc.encode(s)) for s in current_sentences)

        # Add sentence to current chunk
        current_sentences.append(sentence)
        current_token_count += sentence_tokens

    # Add final chunk
    if current_sentences:
        chunk_text = " ".join(current_sentences)
        token_count = len(enc.encode(chunk_text))
        chunks.append(
            Chunk(
                content=chunk_text,
                index=chunk_index,
                token_count=token_count,
                metadata={
                    "sentence_count": len(current_sentences),
                    "index": chunk_index,
                    "token_count": token_count,
                },
            )
        )

    return chunks


def _hard_split_by_tokens(
    text: str,
    enc: tiktoken.Encoding,
    chunk_size: int,
    overlap: int,
    start_index: int,
) -> list[Chunk]:
    """Hard split text by token count, ignoring sentence boundaries.

    Used when a single sentence exceeds the chunk_size.
    """
    tokens = enc.encode(text)
    total_tokens = len(tokens)
    chunks: list[Chunk] = []
    chunk_index = start_index
    pos = 0

    while pos < total_tokens:
        end = min(pos + chunk_size, total_tokens)
        chunk_tokens = tokens[pos:end]
        chunk_text = enc.decode(chunk_tokens)
        token_count = len(chunk_tokens)

        chunks.append(
            Chunk(
                content=chunk_text,
                index=chunk_index,
                token_count=token_count,
                metadata={
                    "sentence_count": 1,
                    "index": chunk_index,
                    "token_count": token_count,
                    "hard_split": True,
                },
            )
        )

        chunk_index += 1

        if end >= total_tokens:
            break

        # Advance with overlap
        pos = end - overlap

    return chunks


def _split_into_sentences(text: str) -> list[str]:
    """Split text into sentences.

    Uses regex-based sentence detection handling:
    - Period, question mark, exclamation point
    - Followed by space and capital letter (new sentence)
    - Protects common abbreviations
    """
    # Protect common abbreviations to avoid false sentence splits
    abbreviations = [
        ("Dr. ", "Dr<DOT> "),
        ("Mr. ", "Mr<DOT> "),
        ("Mrs. ", "Mrs<DOT> "),
        ("Ms. ", "Ms<DOT> "),
        ("Ph.D.", "Ph<DOT>D<DOT>"),
        ("U.S.", "U<DOT>S<DOT>"),
        ("e.g.", "e<DOT>g<DOT>"),
        ("i.e.", "i<DOT>e<DOT>"),
        ("etc.", "etc<DOT>"),
        ("vs.", "vs<DOT>"),
        ("Fig.", "Fig<DOT>"),
        ("No.", "No<DOT>"),
    ]

    protected = text
    for original, replacement in abbreviations:
        protected = protected.replace(original, replacement)

    # Split on sentence boundaries: punctuation followed by whitespace
    sentence_pattern = re.compile(r'(?<=[.!?])\s+(?=[A-Z0-9])|(?<=[.!?])\s*\n+')
    raw_sentences = sentence_pattern.split(protected)

    # Also split on paragraph breaks
    result = []
    for raw in raw_sentences:
        # Split on double newlines (paragraphs)
        paragraphs = re.split(r'\n{2,}', raw)
        for para in paragraphs:
            para = para.strip()
            # Restore abbreviations
            for original, replacement in abbreviations:
                para = para.replace(replacement, original)
            if para:
                result.append(para)

    # If no sentences were detected, return the whole text as single element
    if not result and text.strip():
        return [text.strip()]

    return result


def _get_overlap_sentences(
    sentences: list[str],
    target_overlap_tokens: int,
    encoder: tiktoken.Encoding,
) -> list[str]:
    """Get sentences from the end of a chunk to use as overlap for the next chunk.

    Goes backwards through sentences until we have approximately
    target_overlap_tokens worth of content.
    """
    if target_overlap_tokens <= 0:
        return []

    overlap_sentences = []
    overlap_tokens = 0

    # Go backwards through sentences to fill overlap
    for sentence in reversed(sentences):
        sentence_tokens = len(encoder.encode(sentence))

        if overlap_tokens + sentence_tokens > target_overlap_tokens and overlap_sentences:
            # Already have enough overlap
            break

        overlap_sentences.insert(0, sentence)
        overlap_tokens += sentence_tokens

    return overlap_sentences
