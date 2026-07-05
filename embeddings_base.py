"""
STIGMERGY — Embedding provider interface.

Two implementations exist (see deterministic.py and minilm.py)
behind the same Protocol, so store()/recall()/reinforce() and the whole
schema (VECTOR(384)) never need to know which one is active. Only the
provider changes; if the dimension ever changes (e.g. moving to a 1024-dim
Bedrock Titan embedding), that is a schema migration, not a code change
here — see EMBEDDING_DIM as the single source of truth for that number.
"""

from __future__ import annotations

import math
from typing import Protocol, runtime_checkable

EMBEDDING_DIM = 384


@runtime_checkable
class EmbeddingProvider(Protocol):
    """
    is_semantic tells the caller whether the vectors produced actually
    encode meaning, or are structurally valid but semantically empty
    (see DeterministicEmbeddingProvider). Any code path that reasons
    about recall QUALITY — not just plumbing — must check this before
    drawing conclusions. Failing to check it is exactly the kind of
    fabricated certainty ARCHITECTURE.md's Failure Philosophy forbids.
    """

    is_semantic: bool
    provider_id: str

    def embed(self, text: str) -> list[float]:
        ...


def validate_text(text: str) -> None:
    """
    Shared input validation for all embedding providers.
    A memory without content is not embeddable.
    """
    if not isinstance(text, str):
        raise TypeError(f"text must be a string — got {type(text).__name__}.")
    if not text.strip():
        raise ValueError("text must be a non-empty string.")


def validate_embedding(vector: list[float], dim: int = EMBEDDING_DIM) -> None:
    """
    Fail loudly, at the boundary, before a malformed vector ever reaches
    CockroachDB. A silent shape or NaN error here would otherwise surface
    much later as an opaque database error or, worse, a vector index that
    quietly returns nonsense nearest neighbors.
    """
    if len(vector) != dim:
        raise ValueError(
            f"Embedding has {len(vector)} dimensions, expected {dim}. "
            "This is a provider/schema mismatch — do not truncate or pad "
            "silently to make it fit."
        )
    for value in vector:
        # Reject non-float strictly. int is rejected because bool is a
        # subclass of int, and accepting int would silently accept True/False
        # as 1.0/0.0. If you need int coercion, do it explicitly before
        # calling validate_embedding.
        if not isinstance(value, float):
            raise ValueError(f"Embedding contains a non-float value: {value!r}")
        if math.isnan(value) or math.isinf(value):
            raise ValueError("Embedding contains NaN or Inf — refusing to store it.")

    # Guard against zero-norm vectors (undefined cosine distance)
    norm_sq = sum(v * v for v in vector)
    if norm_sq == 0.0:
        raise ValueError("Zero-norm embedding — cosine distance undefined.")


def to_pgvector_literal(vector: list[float]) -> str:
    """
    Render a Python float list as the text form CockroachDB's VECTOR
    type accepts, for use with an explicit ::VECTOR cast in SQL:

        cur.execute(
            "INSERT INTO memories (embedding, ...) VALUES (%s::VECTOR, ...)",
            (to_pgvector_literal(vector), ...),
        )

    psycopg has no built-in adapter for pgvector's wire format, so this
    is done explicitly rather than relying on implicit type coercion.
    """
    validate_embedding(vector)
    return "[" + ",".join(repr(v) for v in vector) + "]"
