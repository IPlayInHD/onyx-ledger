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

    def hard_erase(self, bucket: str, key: str) -> DeleteOutcome:
        """Remove one object AND EVERY VERSION OF IT. IDEMPOTENT.

        SUCCESS MEANS, for the target key and no other:

            no current version · no historical version · no delete marker

        WHY THERE IS ONLY ONE ERASURE OPERATION. This was `delete`, and on a
        versioned bucket `DeleteObject` deletes nothing — it writes a delete
        marker and every previous version stays readable by anyone who can name
        one. B2 made the adapter report that as a failure rather than let it
        pass as erasure, which was right and left both callers stuck: the
        privacy phase could never complete, and ordinary customer deletion
        raised a 503 on every attempt, permanently.

        Splitting this into a soft `delete` and a privacy `hard_erase` was the
        obvious alternative and is worse. Both callers mean the same thing —
        Entry 11A's document contract says the binary is PURGED when a customer
        deletes it, not hidden — so a soft variant would exist only to be
        chosen by mistake, and the mistake would be silent, durable, and
        discovered by whoever eventually read the version history.

        Retaining versions is a durability feature, and for these objects the
        product does not use it: there is no undelete path anywhere, so the only
        thing a retained version can do is outlive a promise that it was purged.

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


class TransactionalEmail(StrEnum):
    """The complete set of messages this product sends.

    Closed on purpose. Onyx sends three transactional messages and has no
    marketing surface; a port that accepted an arbitrary subject and body would
    be one, and the first thing to arrive in it would be something nobody
    reviewed for the rule below.

    NO CUSTOMER FINANCIAL DATA IN ANY OF THEM. Email is unencrypted at rest in
    somebody else's mailbox, forwarded, and indexed. None of these messages
    carries a figure, a document, or anything about a tax position.
    """

    EMAIL_VERIFICATION = "EMAIL_VERIFICATION"
    PASSWORD_RESET = "PASSWORD_RESET"
    PASSWORD_CHANGED = "PASSWORD_CHANGED"


@dataclass(frozen=True, slots=True)
class RenderedEmail:
    """A message ready to transmit: subject, plain text, and safe HTML."""

    subject: str
    text: str
    html: str


class EmailProvider(Protocol):
    """Transmit one already-rendered transactional message.

    ONE OPERATION, NOT THREE. Per-message methods read better at a call site,
    and `AccountRecoveryService` offers exactly those. Putting them on the PORT
    would mean every adapter renders every template, which is duplication whose
    failure mode is two adapters that disagree about what a customer receives —
    the same shape as B2's `get`, where the local and S3 stores had to be made
    to agree about a missing object on purpose.

    So rendering happens once, in `app/services/email/templates.py`, and an
    adapter only transmits. `kind` travels alongside because a capture provider
    and an operator both need to know WHICH message went out without parsing a
    subject line.

    Provider exceptions must NOT escape. Same rule as `ObjectStorage`: an
    adapter translates a vendor failure into `EmailDeliveryFailed`, because a
    caller that has to catch `ClientError` is coupled to botocore and a caller
    keyed on `str(e)` cannot be reasoned about.
    """

    def send(
        self, to: str, kind: TransactionalEmail, message: RenderedEmail
    ) -> None: ...
