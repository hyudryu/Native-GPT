"""Tests for tools/_lib/vectorize.py."""

from __future__ import annotations

import sys
from pathlib import Path

# Make _lib importable when running pytest from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import vectorize  # noqa: E402  (sys.path bootstrap above)


def test_tokens_lowercases_and_drops_single_characters() -> None:
    assert vectorize.tokens("Hello, World! A b c SQLite") == [
        "hello",
        "world",
        "sqlite",
    ]


def test_vectorize_is_deterministic_and_normalized() -> None:
    first = vectorize.vectorize("user prefers dark mode in the editor")
    second = vectorize.vectorize("user prefers dark mode in the editor")
    assert first == second
    assert len(first) == vectorize.EMBEDDING_DIMENSIONS
    norm = sum(value * value for value in first) ** 0.5
    assert abs(norm - 1.0) < 1e-6


def test_vectorize_empty_text_is_zero_vector() -> None:
    assert vectorize.vectorize("!!! ??") == [0.0] * vectorize.EMBEDDING_DIMENSIONS


def test_cosine_ranks_similar_above_unrelated() -> None:
    query = vectorize.vectorize("rust sqlite knowledge search")
    similar = vectorize.vectorize("sqlite search for rust knowledge")
    unrelated = vectorize.vectorize("watercolor landscape painting")
    assert vectorize.cosine(query, similar) > vectorize.cosine(query, unrelated)
    assert vectorize.cosine(query, query) > 0.99


def test_chunk_text_overlap_and_bounds() -> None:
    text = " ".join(f"w{i}" for i in range(400))
    chunks = vectorize.chunk_text(text)
    assert len(chunks) == 3
    assert all(len(chunk.split()) <= vectorize.CHUNK_WORDS for chunk in chunks)
    # Overlap: the tail of chunk N reappears at the head of chunk N+1.
    first_words = chunks[0].split()
    second_words = chunks[1].split()
    assert second_words[: vectorize.CHUNK_OVERLAP_WORDS] == (
        first_words[-vectorize.CHUNK_OVERLAP_WORDS :]
    )
    assert vectorize.chunk_text("") == []


def test_json_roundtrip() -> None:
    vector = vectorize.vectorize("roundtrip me")
    restored = vectorize.from_json(vectorize.to_json(vector))
    assert restored is not None
    assert vectorize.cosine(vector, restored) > 0.999
    assert vectorize.from_json(None) is None
    assert vectorize.from_json("not json") is None
    assert vectorize.from_json("[1, 2]") is None
