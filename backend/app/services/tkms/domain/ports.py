"""TKMS domain ports — the framework-free seams the pipeline depends on.

Following the established port pattern (OcrProvider, LlmClient, Embedder), the
pipeline never depends on a concrete parser, clock, or id source — only on these
Protocols. Deterministic implementations (FixedClock, SequentialIdGen) make the
whole pipeline reproducible in tests; real parsers plug in via the registry.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from app.services.tkms.domain.models import ExtractedRuleSet


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(tz=UTC)


class FixedClock:
    """A clock frozen at a moment — for deterministic tests."""

    def __init__(self, moment: datetime):
        self._moment = moment

    def now(self) -> datetime:
        return self._moment


@runtime_checkable
class IdGen(Protocol):
    def new_id(self) -> uuid.UUID: ...


class SystemIdGen:
    def new_id(self) -> uuid.UUID:
        return uuid.uuid4()


class SequentialIdGen:
    """Deterministic UUIDs (uuid.UUID(int=n)) — for reproducible tests."""

    def __init__(self, start: int = 1):
        self._n = start

    def new_id(self) -> uuid.UUID:
        val = uuid.UUID(int=self._n)
        self._n += 1
        return val


@runtime_checkable
class Parser(Protocol):
    """A pluggable legislation parser.

    Two phases so text extraction (OCR/HTML/PDF, potentially AI-assisted) is
    separable from structured rule extraction. Both are pure with respect to the
    pipeline: given the same bytes/text a parser must return the same result.
    """

    name: str
    version: str
    source: str      # e.g. 'cra', 'finance', 'generic'
    fmt: str         # 'csv' | 'json' | 'xml' | 'manual' | 'html' | 'pdf'

    def extract_text(self, raw: bytes) -> str: ...

    def extract_rules(self, text: str) -> ExtractedRuleSet: ...
