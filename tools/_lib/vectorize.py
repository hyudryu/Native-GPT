"""Deterministic local feature-hash embeddings (stdlib-only).

A Python port of the Rust host's `vectorize` in
`crates/server/src/knowledge.rs`: tokenize on non-alphanumerics, hash each
unigram (weight 1.0) and adjacent bigram (weight 0.6) into a fixed-width
signed vector, then L2-normalize. The representation keeps private content
on device and can be regenerated later if a richer embedding model is
configured.

Hash note: Rust uses `DefaultHasher`, which is not stable across Rust
versions, so bit-for-bit parity was never guaranteed across processes.
Python uses BLAKE2b-64 (stdlib `hashlib`, fully deterministic). Vectors
produced here are therefore NOT bit-compatible with Rust-produced vectors —
compare only vectors made by the same implementation, and record
`EMBEDDING_VERSION` alongside stored embeddings so they can be rebuilt.

Shared by the memory tools today and the knowledge tools next stage.
Loaded by file path from `tools/<id>/tool.py` (no package context).
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

EMBEDDING_DIMENSIONS = 256
EMBEDDING_VERSION = "feature-hash-v1-py"

CHUNK_WORDS = 180
CHUNK_OVERLAP_WORDS = 30

_UNIGRAM_WEIGHT = 1.0
_BIGRAM_WEIGHT = 0.6


def tokens(text: str) -> list[str]:
    """Split on non-alphanumerics, lowercase, drop single-character tokens.

    Mirrors the Rust `tokens()`: `char::is_alphanumeric` and Python's
    `str.isalnum()` are both Unicode-aware, so tokenization matches.
    """
    words: list[str] = []
    current: list[str] = []
    for char in text:
        if char.isalnum():
            current.append(char)
        elif current:
            word = "".join(current).lower()
            if len(word) > 1:
                words.append(word)
            current = []
    if current:
        word = "".join(current).lower()
        if len(word) > 1:
            words.append(word)
    return words


def _hash64(feature: str) -> int:
    """Deterministic 64-bit feature hash (BLAKE2b, little-endian)."""
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "little")


def vectorize(text: str) -> list[float]:
    """Hash `text` into an L2-normalized 256-dim feature vector.

    Unigrams contribute weight 1.0, adjacent bigrams ("w1:w2") weight 0.6.
    Each feature maps to `index = hash % 256` with a sign taken from the
    hash's top bit, so collisions partially cancel instead of stacking.
    An empty/tokenless text returns the zero vector.
    """
    words = tokens(text)
    vector = [0.0] * EMBEDDING_DIMENSIONS

    def add_feature(feature: str, weight: float) -> None:
        hashed = _hash64(feature)
        index = hashed % EMBEDDING_DIMENSIONS
        direction = 1.0 if hashed & (1 << 63) == 0 else -1.0
        vector[index] += direction * weight

    for word in words:
        add_feature(word, _UNIGRAM_WEIGHT)
    for first, second in zip(words, words[1:]):
        add_feature(f"{first}:{second}", _BIGRAM_WEIGHT)

    norm = math.sqrt(sum(value * value for value in vector))
    if norm > 0.0:
        vector = [value / norm for value in vector]
    return vector


def cosine(left: list[float], right: list[float]) -> float:
    """Cosine similarity; vectors from `vectorize` are pre-normalized."""
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def chunk_text(text: str) -> list[str]:
    """Split `text` into word chunks (180 words, 30-word overlap).

    Mirrors the Rust `chunk_text` for the knowledge tools' ingestion path.
    """
    words = text.split()
    if not words:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(words):
        end = min(start + CHUNK_WORDS, len(words))
        chunks.append(" ".join(words[start:end]))
        if end == len(words):
            break
        start = max(end - CHUNK_OVERLAP_WORDS, 0)
    return chunks


def to_json(vector: list[float]) -> str:
    """Serialize a vector for an `embedding_json` TEXT column."""
    return json.dumps([round(value, 6) for value in vector], separators=(",", ":"))


def from_json(raw: str | None) -> list[float] | None:
    """Parse an `embedding_json` value back into a vector (None if unusable)."""
    if not raw:
        return None
    try:
        value: Any = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, list) or len(value) != EMBEDDING_DIMENSIONS:
        return None
    try:
        return [float(component) for component in value]
    except (TypeError, ValueError):
        return None
