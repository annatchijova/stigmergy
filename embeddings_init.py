from .base import EmbeddingProvider, EMBEDDING_DIM, validate_embedding, validate_text, to_pgvector_literal
from .deterministic import DeterministicEmbeddingProvider

__all__ = [
    "EmbeddingProvider",
    "EMBEDDING_DIM",
    "validate_embedding",
    "validate_text",
    "to_pgvector_literal",
    "DeterministicEmbeddingProvider",
]

# MiniLMEmbeddingProvider is deliberately NOT imported here — importing
# this package must never require sentence-transformers/torch to be
# installed. Import it explicitly where needed:
#   from embeddings.minilm import MiniLMEmbeddingProvider
