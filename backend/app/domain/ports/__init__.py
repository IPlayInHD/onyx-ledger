"""Adapter ports (interfaces). The application layer depends on these; the
`integrations/` package provides concrete implementations. This is the seam that
keeps infrastructure swappable and the domain framework-free.
"""
from __future__ import annotations

from typing import Protocol


class ObjectStorage(Protocol):
    # `max_bytes` is on the PORT, not just the adapter: bytes go straight to the
    # bucket without passing through the API, so the only place a size limit can
    # actually be enforced is the store. An implementation that ignored it would
    # leave the document bound declarative — a number the client is asked to
    # respect rather than one anything checks.
    def presign_put(
        self, bucket: str, key: str, content_type: str, *, max_bytes: int
    ) -> str: ...
    def presign_get(self, bucket: str, key: str) -> str: ...
    def put(self, bucket: str, key: str, data: bytes) -> None: ...
    def get(self, bucket: str, key: str) -> bytes: ...


class OcrProvider(Protocol):
    def extract_text(self, bucket: str, key: str) -> str: ...


class LlmClient(Protocol):
    async def complete(self, system: str, prompt: str, *, max_tokens: int = 1024) -> str: ...


class Embedder(Protocol):
    dim: int

    def embed(self, text: str) -> list[float]: ...


class EmailSender(Protocol):
    async def send(self, to: str, subject: str, body: str) -> None: ...
