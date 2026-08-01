"""Embedder adapters.

`LocalHashEmbedder` is a deterministic, dependency-free bag-of-words embedder
(feature-hashing into a fixed 1536-dim space) — good enough to demonstrate and
test pgvector retrieval offline. A real embedding model (Voyage / OpenAI /
sentence-transformers) drops in behind the same `Embedder` port for production.
"""
from __future__ import annotations

import hashlib
import math
import re

DIM = 1536
_TOKEN = re.compile(r"[a-z0-9]+")


class LocalHashEmbedder:
    dim = DIM

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * DIM
        for tok in _TOKEN.findall((text or "").lower()):
            # deterministic bucket (hashlib, not salted hash())
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            bucket = h % DIM
            sign = 1.0 if (h >> 17) & 1 else -1.0
            vec[bucket] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec


# Production adapter (behind the same port):
# class ApiEmbedder:
#     dim = 1536
#     def __init__(self, client, model): self.client, self.model = client, model
#     def embed(self, text): return self.client.embeddings.create(model=self.model, input=text)...


def get_embedder() -> LocalHashEmbedder:
    return LocalHashEmbedder()
