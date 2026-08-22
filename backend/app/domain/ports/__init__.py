"""Adapter ports (interfaces). The application layer depends on these; the
`integrations/` package provides concrete implementations. This is the seam that
keeps infrastructure swappable and the domain framework-free.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


@dataclass(frozen=True, slots=True)
class UploadAuthorization:
    """One bounded, single-object upload permit: where to send the bytes, and
    whatever the provider requires alongside them.

    `fields` is not decoration. S3 can only enforce a maximum object size
    through a POST policy's `content-length-range` condition, and that policy
    travels in the form fields — so a permit reduced to a bare URL is a permit
    with no ceiling, whatever the docstring above `max_bytes` claims.
    """

    url: str
    fields: dict[str, str] = field(default_factory=dict)


class DeleteOutcome(StrEnum):
    """What an object-storage deletion actually achieved.

    A closed set, so a lifecycle phase can branch on it without parsing a
    provider message. `DELETED` and `ALREADY_ABSENT` are both terminal success
    — a retry that finds the object already gone must converge, not fail
    forever — and are kept distinct only so an operator can tell a first
    deletion from a replay.
    """

    DELETED = "DELETED"
    ALREADY_ABSENT = "ALREADY_ABSENT"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    PERMANENT_FAILURE = "PERMANENT_FAILURE"


class ObjectStorage(Protocol):
    """Four operations, because four is what the callers use.

    `presign_put` authorizes an upload, `put`/`get` move bytes for the
    knowledge-ingestion path, and `delete` is the erasure primitive the privacy
    lifecycle is built on. There is deliberately no `exists`, no `metadata` and
    no `list`: an operation nobody calls is surface that still has to be
    implemented correctly by every adapter, and gets it wrong unobserved.

    `presign_get` USED TO BE HERE and had no callers anywhere in `app/` or
    `workers/` — there is no download path. A download-URL minter with no call
    site is not free: it is a way to hand out object access that no
    authorization check sits in front of, waiting for someone to reach for it.
    Removed rather than implemented a second time in the S3 adapter.
    """

    # `max_bytes` is on the PORT, not just the adapter: bytes go straight to the
    # bucket without passing through the API, so the only place a size limit can
    # actually be enforced is the store. An implementation that ignored it would
    # leave the document bound declarative — a number the client is asked to
    # respect rather than one anything checks.
    def presign_put(
        self, bucket: str, key: str, content_type: str, *, max_bytes: int
    ) -> UploadAuthorization: ...
    def put(self, bucket: str, key: str, data: bytes) -> None: ...
    def get(self, bucket: str, key: str) -> bytes: ...

    def delete(self, bucket: str, key: str) -> DeleteOutcome:
        """Remove one object. IDEMPOTENT: a missing object is not an error.

        PD-8: the port had no delete at all, so an uploaded binary was
        unreachable by any cascade — a document could be removed from the
        database while its bytes stayed in the bucket forever.

        The return value is a CLOSED OUTCOME rather than a bool or an exception
        because the lifecycle has to distinguish three different futures:
        `DELETED` and `ALREADY_ABSENT` both mean the object is gone and the
        phase may advance, while `RETRYABLE_FAILURE` means try again later.
        Squashing the first two into "success" is right; squashing the third
        into it would let a lifecycle report completion over a binary that is
        still there.

        Provider exceptions must NOT escape. Entry 11A proved exception strings
        carry values that have no business in a privacy store, and a lifecycle
        keyed on `str(e)` cannot be reasoned about. An adapter translates.
        """
        ...



class OcrProvider(Protocol):
    def extract_text(self, bucket: str, key: str) -> str: ...


class LlmClient(Protocol):
    async def complete(self, system: str, prompt: str, *, max_tokens: int = 1024) -> str: ...


class Embedder(Protocol):
    dim: int

    def embed(self, text: str) -> list[float]: ...


class EmailSender(Protocol):
    async def send(self, to: str, subject: str, body: str) -> None: ...
