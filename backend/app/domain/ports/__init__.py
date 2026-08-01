"""Adapter ports (interfaces). The application layer depends on these; the
`integrations/` package provides concrete implementations. This is the seam that
keeps infrastructure swappable and the domain framework-free.
"""
from __future__ import annotations

from typing import Protocol


class ObjectStorage(Protocol):
    def presign_put(self, bucket: str, key: str, content_type: str) -> str: ...
    def presign_get(self, bucket: str, key: str) -> str: ...


class OcrProvider(Protocol):
    def extract_text(self, bucket: str, key: str) -> str: ...


class LlmClient(Protocol):
    async def complete(self, system: str, prompt: str, *, max_tokens: int = 1024) -> str: ...


class EmailSender(Protocol):
    async def send(self, to: str, subject: str, body: str) -> None: ...
