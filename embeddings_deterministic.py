"""
DeterministicEmbeddingProvider
===============================
Same SHA-256-seeded fallback pattern as raven-memory's third-tier
degradation path. Given identical text, always produces the identical
384-dimensional vector. Zero dependencies, no network, no GPU, no
download — the point is to unblock development of store()/recall()/
reinforce()/migration/recruitment TODAY, against the real cluster,
without waiting on sentence-transformers and torch.

WARNING — read this before using it for anything but plumbing:
this provider has NO semantic property. "cat" and "kitten" are exactly
as (un)related as "cat" and "spreadsheet" here — there is no training,
no meaning, only a hash spread deterministically into 384 dimensions
and normalized to unit length so cosine distance is at least
well-defined, if meaningless. It exists to validate storage
transactions, the audit chain, migration cooldown, recruitment
consensus, and the vector index mechanics — never to evaluate recall
QUALITY. is_semantic = False exists specifically so calling code can
refuse to draw relevance conclusions when this provider is active. This
is ARCHITECTURE.md's Failure Philosophy applied to embeddings: the
system must not fabricate semantic certainty it does not have.
"""

from __future__ import annotations

import hashlib
import math
import random

from .base import EMBEDDING_DIM, validate_embedding, validate_text


class DeterministicEmbeddingProvider:
    is_semantic = False
    provider_id = "deterministic-sha256-v1"
    dim = EMBEDDING_DIM

    def embed(self, text: str) -> list[float]:
        validate_text(text)

        seed = int(hashlib.sha256(text.encode("utf-8")).hexdigest(), 16)
        rng = random.Random(seed)
        raw = [rng.uniform(-1.0, 1.0) for _ in range(self.dim)]

        norm = math.sqrt(sum(v * v for v in raw))
        if norm == 0.0:
            # Astronomically unlikely for a 384-dim vector, but a zero
            # vector would make cosine distance undefined downstream —
            # fail loudly rather than silently divide by it.
            raise ValueError(
                "Degenerate zero-norm embedding produced — this should be "
                "practically impossible; treat it as a bug, not noise."
            )

        vector = [x / norm for x in raw]
        validate_embedding(vector, EMBEDDING_DIM)
        return vector
