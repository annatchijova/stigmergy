"""
MiniLMEmbeddingProvider
========================
Wraps sentence-transformers' all-MiniLM-L6-v2 — 384 dimensions, matching
EMBEDDING_DIM exactly, so switching from DeterministicEmbeddingProvider
requires no schema change. This is the provider used for the actual
demo, once real semantic recall needs to be shown.

Lazy dependency: importing this module does NOT require
sentence-transformers or torch to be installed. The import only happens
inside __init__, when MiniLMEmbeddingProvider is actually instantiated —
so the deterministic development path never needs to pull in torch just
to run store()/recall()/reinforce() tests.
"""

from __future__ import annotations

from .base import EMBEDDING_DIM, validate_embedding, validate_text


class MiniLMEmbeddingProvider:
    is_semantic = True
    provider_id = "all-MiniLM-L6-v2"

    def __init__(self) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise ImportError(
                "sentence-transformers is required for MiniLMEmbeddingProvider. "
                "Install with:\n"
                "    pip install sentence-transformers --break-system-packages"
            ) from exc

        self._model = SentenceTransformer("all-MiniLM-L6-v2")

        actual_dim = self._model.get_sentence_embedding_dimension()
        if actual_dim != EMBEDDING_DIM:
            # Fail at construction, not on the first insert. A silent
            # dimension mismatch here would surface later as an opaque
            # CockroachDB error far from its actual cause.
            raise RuntimeError(
                f"all-MiniLM-L6-v2 reports {actual_dim} dimensions, "
                f"but the schema expects {EMBEDDING_DIM}. This indicates "
                f"the model was swapped or its config changed upstream — "
                f"do not proceed without resolving the mismatch explicitly."
            )

    def embed(self, text: str) -> list[float]:
        validate_text(text)

        vector = self._model.encode(text, normalize_embeddings=True)
        vector = vector.tolist()
        validate_embedding(vector, EMBEDDING_DIM)
        return vector
