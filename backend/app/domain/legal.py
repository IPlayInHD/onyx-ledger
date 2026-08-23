"""The legal-document registry: ONE backend-owned authority for policy versions.

WHY THIS IS IN THE BACKEND AND NOT THE FRONTEND. Before B4 the only statement
of what version the Terms were was `DOC_VERSION` in
`frontend/src/pages/legal/content.ts` — a constant in a bundle the customer
downloads. A version the client owns is a version the client can be wrong
about, and the failure it produces is the worst kind available here: a durable
record saying somebody accepted Terms 2.0 while their browser rendered 1.9.

So the registry below decides four things, and the frontend is told them:

    which documents exist · what version each is at · when it took effect
    whether accepting it is required

WHAT THIS FILE DOES NOT DECIDE. Whether a change is material enough to demand
re-acceptance. That is a legal judgement, it is made by a person, and it
arrives here as `requires_acceptance` and a version number — never inferred
from a text diff, because "the software decided your new Terms were minor" is
not a defensible sentence.

THE DOCUMENT TEXT IS NOT HERE EITHER. It stays in the frontend, where it is
rendered; storing a second copy would create two things to keep in step and no
way to tell which one a customer actually read. What keeps them aligned is a
contract test asserting this registry and `content.ts` agree on type, version
and effective date — see `tests/security/test_legal_document_integrity.py`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class LegalDocumentType(StrEnum):
    """The complete set of documents Onyx publishes.

    CLOSED, and the values are the frontend's own route slugs — `/legal/terms`
    renders `TERMS`. Using the slug rather than inventing a parallel code means
    a stored acceptance names a document a person can navigate to, and the
    integrity test can match the two sets without a translation table that
    could itself be wrong.
    """

    TERMS = "terms"
    PRIVACY = "privacy"
    AI_TRANSPARENCY = "ai-transparency"
    TAX_DISCLAIMER = "tax-disclaimer"
    ACCESSIBILITY = "accessibility"
    SECURITY = "security"


class ReviewStatus(StrEnum):
    """Whether a version has been through Canadian legal review.

    `DRAFT` is the honest state of every document today and it is not a
    formality: `production_blockers()` refuses to let a draft be a document
    production REQUIRES people to accept. Development and staging exercise
    drafts freely — that is how the flow gets tested — and production is where
    an unreviewed draft stops being acceptable.

    Nothing in this repository may move a version to `COUNSEL_APPROVED`. That
    is a person's decision recorded after a person made it, and a commit that
    flipped this value without one would be the software inventing the review.
    """

    DRAFT = "DRAFT"
    COUNSEL_APPROVED = "COUNSEL_APPROVED"


@dataclass(frozen=True, slots=True)
class LegalDocument:
    """One document at one version.

    `requires_acceptance` is the distinction B4 §2 asks the schema to carry:
    some documents a customer must affirmatively accept, others they must
    simply be able to read. Both are represented, and moving a document
    between them is a one-line change here rather than a redesign.
    """

    type: LegalDocumentType
    version: str
    effective_date: date
    requires_acceptance: bool
    review_status: ReviewStatus

    @property
    def is_approved(self) -> bool:
        return self.review_status is ReviewStatus.COUNSEL_APPROVED


#: Every document, at the version currently in force.
#:
#: ONE VERSION ACROSS THE SET, matching `content.ts`: the six were drafted
#: together and are reviewed together, and letting them drift apart would imply
#: a review history that did not happen.
#:
#: WHICH TWO REQUIRE ACCEPTANCE, and why those two. The sign-up screen already
#: tells every customer that creating an account means agreeing to the Terms of
#: Service and the Privacy Policy. That sentence has been shown to people; what
#: was missing was any record of it. Marking exactly those two matches the
#: durable record to what the product actually says, which is what B4 §14 asks
#: for — it is not a legal judgement invented here.
#:
#: The other four are notice-only: the product makes them available and does
#: not ask anyone to sign them. Counsel may move any document to either side.
_CURRENT: tuple[LegalDocument, ...] = (
    LegalDocument(
        type=LegalDocumentType.TERMS,
        version="0.1.0-draft",
        effective_date=date(2026, 8, 21),
        requires_acceptance=True,
        review_status=ReviewStatus.DRAFT,
    ),
    LegalDocument(
        type=LegalDocumentType.PRIVACY,
        version="0.1.0-draft",
        effective_date=date(2026, 8, 21),
        requires_acceptance=True,
        review_status=ReviewStatus.DRAFT,
    ),
    LegalDocument(
        type=LegalDocumentType.AI_TRANSPARENCY,
        version="0.1.0-draft",
        effective_date=date(2026, 8, 21),
        requires_acceptance=False,
        review_status=ReviewStatus.DRAFT,
    ),
    LegalDocument(
        type=LegalDocumentType.TAX_DISCLAIMER,
        version="0.1.0-draft",
        effective_date=date(2026, 8, 21),
        requires_acceptance=False,
        review_status=ReviewStatus.DRAFT,
    ),
    LegalDocument(
        type=LegalDocumentType.ACCESSIBILITY,
        version="0.1.0-draft",
        effective_date=date(2026, 8, 21),
        requires_acceptance=False,
        review_status=ReviewStatus.DRAFT,
    ),
    LegalDocument(
        type=LegalDocumentType.SECURITY,
        version="0.1.0-draft",
        effective_date=date(2026, 8, 21),
        requires_acceptance=False,
        review_status=ReviewStatus.DRAFT,
    ),
)

CURRENT_DOCUMENTS: dict[LegalDocumentType, LegalDocument] = {
    document.type: document for document in _CURRENT
}


def current_document(document_type: LegalDocumentType) -> LegalDocument:
    return CURRENT_DOCUMENTS[document_type]


def documents_requiring_acceptance() -> tuple[LegalDocument, ...]:
    """The documents a customer must accept before using the application."""
    return tuple(d for d in _CURRENT if d.requires_acceptance)


def production_blockers() -> tuple[str, ...]:
    """Why this registry may not serve production. Empty means it may.

    ONE RULE, and it is the one B4 §17 asks for: production must not require
    anybody to accept a document Canadian counsel has not read. A draft that
    nobody has to accept is fine in production — it is published information,
    clearly marked as a draft, and the product is better for showing it than
    for hiding it.

    Returned as strings rather than raised, because the caller differs. A
    startup check wants to refuse; a status endpoint wants to report; a test
    wants to assert the list is exactly what it expects.
    """
    return tuple(
        f"{d.type.value} {d.version} requires acceptance but is "
        f"{d.review_status.value}; production must not ask a customer to "
        "accept a document that has not had legal review"
        for d in _CURRENT
        if d.requires_acceptance and not d.is_approved
    )


__all__ = [
    "CURRENT_DOCUMENTS",
    "LegalDocument",
    "LegalDocumentType",
    "ReviewStatus",
    "current_document",
    "documents_requiring_acceptance",
    "production_blockers",
]
