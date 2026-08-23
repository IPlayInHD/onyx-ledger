"""Legal acceptance: what a customer has agreed to, and recording a new one.

THE ONE THING THIS SUBSYSTEM MUST NEVER DO is report an acceptance that was not
durably written. Everything below is arranged around that: the row is the legal
event, the response is derived from the row, and there is no path where the
customer is told "recorded" by something that only intended to record.

WHAT THE CLIENT MAY DECIDE: which document it is accepting, and which version
it believes is current. Nothing else. The account comes from the bearer token,
the timestamp comes from the database, and whether that document requires
acceptance at all comes from the registry — a client that submits a version the
registry does not currently require is refused, not obeyed.

STALE FRONTENDS ARE EXPECTED, not exceptional. A customer who left a tab open
across a policy change will submit the version they were shown, and the honest
answer is a typed conflict telling them to re-read — never a quiet success that
records agreement to superseded text, and never a 500.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import DomainError
from app.database.models import ConsentLog, LegalAcceptance
from app.domain.legal import (
    CURRENT_DOCUMENTS,
    LegalDocument,
    LegalDocumentType,
    documents_requiring_acceptance,
)


class LegalDocumentUnknown(DomainError):
    """A document type or version the registry does not publish.

    404 rather than 422: the caller named something that does not exist. The
    body says which document type was not recognised — that is public
    information, the registry is served to every visitor, and withholding it
    would only make an integration harder to debug.
    """

    status_code = 404
    error_type = "https://onyx.ledger/errors/legal-document-unknown"
    title = "Unknown Legal Document"


class LegalVersionStale(DomainError):
    """The client accepted a version that is no longer the current one.

    409, and it carries the version the registry now requires. A customer who
    left a tab open across a policy change is the ordinary case, not an attack:
    they are told the document moved, given the new version, and asked to read
    it. Recording their agreement to superseded text would be worse than
    refusing — it would be a durable record of consent to something they were
    never shown.
    """

    status_code = 409
    error_type = "https://onyx.ledger/errors/legal-version-stale"
    title = "Legal Document Has Changed"

    def __init__(self, document_type: str, current_version: str) -> None:
        self.document_type = document_type
        self.current_version = current_version
        super().__init__(
            "This document has been updated since your last visit. Review the "
            "current version and accept that one."
        )


@dataclass(frozen=True, slots=True)
class DocumentState:
    """One document, and where this customer stands with it."""

    document: LegalDocument
    accepted_version: str | None
    accepted_at: datetime | None

    @property
    def acceptance_outstanding(self) -> bool:
        """Required, and not accepted at the version currently in force.

        Covers both cases with one expression on purpose: never accepted, and
        accepted at a version that has since been replaced. The gate does not
        care which — both mean the customer has not agreed to what is in force.
        """
        if not self.document.requires_acceptance:
            return False
        return self.accepted_version != self.document.version


class LegalAcceptanceService:
    """Legal state and acceptance for one request."""

    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    # ------------------------------------------------------------- reading --
    async def _accepted(self, user_id: uuid.UUID) -> dict[str, tuple[str, datetime]]:
        """The newest acceptance per document type, for this account.

        Ordered by `accepted_at` ascending so the dict keeps the LAST one
        written per type. Rows are never updated, so "newest" is unambiguous
        and a document accepted at three successive versions yields the third.
        """
        rows = await self.s.execute(
            select(
                LegalAcceptance.document_type,
                LegalAcceptance.document_version,
                LegalAcceptance.accepted_at,
            )
            .where(LegalAcceptance.user_id == user_id)
            .order_by(LegalAcceptance.accepted_at.asc())
        )
        return {r.document_type: (r.document_version, r.accepted_at) for r in rows}

    async def state(self, user_id: uuid.UUID) -> list[DocumentState]:
        """Every document, with this customer's standing against each.

        THE REGISTRY DRIVES THE LIST, not the acceptance table. A document
        nobody has accepted still appears — that is the whole point of the
        screen this feeds — and an acceptance of a document the registry no
        longer publishes simply does not appear, because it is no longer
        something the customer is being asked about.
        """
        accepted = await self._accepted(user_id)
        states: list[DocumentState] = []
        for document_type in LegalDocumentType:
            document = CURRENT_DOCUMENTS[document_type]
            found = accepted.get(document_type.value)
            states.append(
                DocumentState(
                    document=document,
                    accepted_version=found[0] if found else None,
                    accepted_at=found[1] if found else None,
                )
            )
        return states

    async def outstanding(self, user_id: uuid.UUID) -> list[LegalDocumentType]:
        """Required documents this account has not accepted at the current version.

        Empty means the application may be used. This is the gate's question and
        it is deliberately the ONLY thing the gate asks — see
        `AccountLifecycleService.assert_may_act`.
        """
        required = {d.type for d in documents_requiring_acceptance()}
        if not required:
            return []
        accepted = await self._accepted(user_id)
        return [
            document_type
            for document_type in LegalDocumentType
            if document_type in required
            and accepted.get(document_type.value, (None, None))[0]
            != CURRENT_DOCUMENTS[document_type].version
        ]

    # ------------------------------------------------------------ writing --
    async def accept(
        self, user_id: uuid.UUID, document_type: str, document_version: str
    ) -> DocumentState:
        """Record an acceptance. Idempotent. Returns the resulting state.

        THE ORDER OF THE CHECKS IS THE CONTRACT:

        1. Is this a document the registry publishes?  No → 404.
        2. Is the submitted version the one in force?  No → 409 with the
           current version, because a stale tab is the ordinary case.
        3. Write, ON CONFLICT DO NOTHING.

        Step 3 is what makes a retry safe. Two tabs, a double-click and a
        network retry all issue the same INSERT; the unique key
        (user, document, version) turns every one after the first into a no-op
        in PostgreSQL rather than a race in Python, and every caller gets the
        same answer. No integrity error ever reaches the customer.

        A DOCUMENT THAT DOES NOT REQUIRE ACCEPTANCE IS STILL ACCEPTABLE. It
        costs one row, it is true, and refusing would mean a customer who
        chose to acknowledge the AI notice gets an error for being thorough.
        What such a row does NOT do is affect the gate, which reads the
        registry rather than the table to decide what is required.
        """
        document = self._resolve(document_type)
        if document_version != document.version:
            raise LegalVersionStale(document.type.value, document.version)

        await self.s.execute(
            pg_insert(LegalAcceptance)
            .values(
                user_id=user_id,
                document_type=document.type.value,
                document_version=document.version,
            )
            .on_conflict_do_nothing(
                constraint="uq_legal_acceptance_user_document_version"
            )
        )

        # The EVIDENCE THAT OUTLIVES THE ACCOUNT. `identity.legal_acceptance`
        # is live tenant state and cascades away with the user; the deletion
        # pipeline severs `audit.consent_log` instead of destroying it, so this
        # row is what survives, de-identified, to show an agreement was given.
        #
        # Nothing ever reads it back — it is write-only, like the security log
        # in B3 — so there is no second source of truth to diverge. `granted`
        # is always true because this table records acceptances and there is no
        # un-accept; `ip_address` is left NULL for the reason the acceptance
        # table has no such column at all.
        await self.s.execute(
            insert(ConsentLog)
            .values(
                user_id=user_id,
                consent_type=document.type.value,
                granted=True,
                version=document.version,
            )
            .inline()
        )
        await self.s.flush()

        accepted = await self._accepted(user_id)
        found = accepted.get(document.type.value)
        return DocumentState(
            document=document,
            accepted_version=found[0] if found else None,
            accepted_at=found[1] if found else None,
        )

    def _resolve(self, document_type: str) -> LegalDocument:
        try:
            return CURRENT_DOCUMENTS[LegalDocumentType(document_type)]
        except ValueError:
            raise LegalDocumentUnknown(
                f"There is no legal document called {document_type!r}."
            ) from None


__all__ = [
    "DocumentState",
    "LegalAcceptanceService",
    "LegalDocumentUnknown",
    "LegalVersionStale",
]
