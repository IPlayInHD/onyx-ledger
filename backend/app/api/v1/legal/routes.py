"""Legal state and acceptance.

BOTH ENDPOINTS ARE LEGAL-EXEMPT, and that is the point rather than an
oversight: these are the two the customer uses to CLEAR outstanding acceptance,
so gating them on having no outstanding acceptance would make the state
permanent. Everything else the gate checks — verification, suspension, closure,
the deletion cutoff — still applies here.

WHAT A BLOCKED CUSTOMER CAN STILL REACH, decided explicitly rather than by
accident (B4 §11, §15):

    the legal documents      public routes, no session needed at all
    this state endpoint      so the screen can say which document changed
    this accept endpoint     so they can do something about it
    logout                   never trapped in a state they cannot leave
    account deletion         already lifecycle-exempt; leaving must not
                             require agreeing to new terms first

Everything carrying tax data stays behind the gate. A customer who has not
accepted the current Terms sees the terms screen, not a position.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status

from app.api.deps import LegalExemptSession, current_user_id
from app.schemas.legal import (
    LegalAcceptanceRequest,
    LegalDocumentState,
    LegalState,
)
from app.services.legal.service import DocumentState, LegalAcceptanceService

router = APIRouter(prefix="/legal", tags=["legal"])


def _rendered(state: DocumentState) -> LegalDocumentState:
    """One document state, as the client sees it. No row identifiers."""
    return LegalDocumentState(
        document_type=state.document.type.value,
        current_version=state.document.version,
        effective_date=state.document.effective_date,
        requires_acceptance=state.document.requires_acceptance,
        review_status=state.document.review_status.value,
        accepted_version=state.accepted_version,
        accepted_at=state.accepted_at,
        acceptance_outstanding=state.acceptance_outstanding,
    )


@router.get("/state", response_model=LegalState)
async def legal_state(
    session: LegalExemptSession,
    user_id: uuid.UUID = Depends(current_user_id),
) -> LegalState:
    """Every document, and where this account stands with each.

    THE WHOLE SET, not just the outstanding ones. A settings screen showing
    "you accepted the Terms on 3 March" needs the accepted ones too, and a
    client that had to ask twice would render half a page while it waited.

    `application_access_blocked` is computed here rather than left to the
    client. A client deriving it would be a second implementation of the
    gate's rule, and the two would eventually disagree — with the client's
    answer being the one the customer sees.
    """
    states = await LegalAcceptanceService(session).state(user_id)
    return LegalState(
        documents=[_rendered(s) for s in states],
        application_access_blocked=any(s.acceptance_outstanding for s in states),
    )


@router.post(
    "/acceptances",
    response_model=LegalDocumentState,
    status_code=status.HTTP_200_OK,
)
async def accept_document(
    body: LegalAcceptanceRequest,
    session: LegalExemptSession,
    user_id: uuid.UUID = Depends(current_user_id),
) -> LegalDocumentState:
    """Record that this account accepts one document at one version.

    THE ACCOUNT IS THE BEARER TOKEN'S and the time is the database's. Neither
    appears in the request schema, so there is no field to spoof and no check
    to forget — accepting on somebody else's behalf is not refused, it is
    unrepresentable.

    200 rather than 201, because this is idempotent and the second call creates
    nothing. Returning 201 twice would claim two records exist where one does.

    The response is the resulting STATE, read back after the write. A client
    that receives it knows the row is durable, which is the one thing this
    subsystem must never lie about: nothing here reports an acceptance that was
    not written, because the answer is derived from the write rather than from
    the request that asked for it.
    """
    state = await LegalAcceptanceService(session).accept(
        user_id, body.document_type, body.document_version
    )
    return _rendered(state)
