"""Request and response contracts for legal acceptance.

`extra="forbid"` ON THE REQUEST, and here it matters more than usual. This
endpoint writes a durable record of what a person agreed to; every field that
decides WHOSE agreement it is, WHEN it happened, and WHETHER the document
needed accepting is derived on the server. A client that submits `user_id`,
`accepted_at` or `requires_acceptance` is trying to author part of a legal
record, and the answer is 422 rather than a silent ignore — silent ignoring is
indistinguishable from acceptance right up until a field is added with a
matching name.

NO DATABASE IDENTIFIERS LEAVE THIS MODULE. The responses name documents by the
slug a person can navigate to, never by row id. A legal state screen has no use
for a primary key, and exposing one invites a client to start addressing rows.
"""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

#: A version string as the registry publishes it. Bounded to the column's own
#: CHECK so an over-long value is refused by the contract rather than by the
#: database — a 422 naming the field beats a 500 naming a constraint.
_VERSION = Field(min_length=1, max_length=64)


class LegalAcceptanceRequest(BaseModel):
    """Accept one document at one version.

    Two fields, and both are things the client legitimately knows: which
    document it is showing, and which version it showed. The server decides
    everything that follows from them.
    """

    model_config = ConfigDict(extra="forbid")

    document_type: str = Field(min_length=1, max_length=64)
    document_version: str = _VERSION


class LegalDocumentState(BaseModel):
    """One document, and where the caller stands with it."""

    document_type: str
    current_version: str
    effective_date: date
    requires_acceptance: bool
    review_status: str

    #: What this caller accepted, if anything. `accepted_version` may lag
    #: `current_version` — that is precisely the re-acceptance case, and the
    #: screen needs both numbers to say which document changed.
    accepted_version: str | None = None
    accepted_at: datetime | None = None
    acceptance_outstanding: bool


class LegalState(BaseModel):
    """Every document, and whether the application is currently reachable.

    `application_access_blocked` is stated rather than left for the client to
    derive by scanning the list. A client that computed it itself would be a
    second implementation of the gate's rule, and the two would eventually
    disagree — with the client's version being the one the customer sees.
    """

    documents: list[LegalDocumentState]
    application_access_blocked: bool


__all__ = ["LegalAcceptanceRequest", "LegalDocumentState", "LegalState"]
