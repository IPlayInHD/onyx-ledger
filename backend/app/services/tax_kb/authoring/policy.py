"""Which registered sources may carry production tax knowledge.

Registration and authority are different questions. The Source Registry answers
"is this a source we have recorded, and exactly which edition?"; it deliberately
declines to rank sources, because legal precedence is a tax opinion and nothing
in this repository is entitled to hold one.

This module answers a narrower, product question that CAN be answered by policy:
*may this kind of source be the sole authority for something Onyx tells a user
to do?* It is a closed allow list, not a ranking. Nothing here says a statute
outranks a regulation; it says commentary about the law is not the law.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.services.tax_kb.sources.domain import SourceStatus, SourceType

#: Bumped when the qualifying set or the superseded/withdrawn stance changes.
#: Stored on every report and every publication, so a later reader can tell
#: which policy a decision was made under rather than assuming today's.
PUBLICATION_POLICY_VERSION = "1.0.0"

#: Source kinds that may stand alone as production authority.
#:
#: SECONDARY_COMMENTARY is absent by design — §12. A practitioner's article may
#: be registered, cited and read; it may not be the only thing standing behind a
#: number a user acts on. Note this is an allow list of KINDS, not a precedence
#: order among them: a CRA guide does not "outrank" a regulation here, and
#: neither is preferred to the other.
QUALIFYING_PRODUCTION_AUTHORITY: frozenset[SourceType] = frozenset({
    SourceType.STATUTE,
    SourceType.REGULATION,
    SourceType.GOVERNMENT_GUIDANCE,
    SourceType.CRA_FOLIO,
    SourceType.CRA_GUIDE,
    SourceType.FORM,
    SourceType.FORM_INSTRUCTIONS,
    SourceType.SCHEDULE,
    SourceType.RATE_TABLE,
    SourceType.INDEXED_PARAMETER_PUBLICATION,
})


@dataclass(frozen=True)
class SourceVerdict:
    """Why one citation does or does not carry production authority."""

    qualifies: bool
    withdrawn: bool
    superseded: bool


def assess_source(source_type: str, version_status: str,
                  superseded: bool) -> SourceVerdict:
    """Judge one resolved citation against the policy.

    `superseded` is reported but never disqualifying on its own. A rule authored
    in 2024 against the 2024 edition rests on the words that were in force then;
    treating a later edition's existence as an integrity failure would make
    every historical rule rot on a schedule — §22 keeps *superseded* and
    *broken* apart, and so does this.

    A WITHDRAWN edition is different in kind: the publisher retracted it, so it
    never carried the authority it appeared to.
    """
    try:
        kind = SourceType(source_type)
    except ValueError:
        return SourceVerdict(qualifies=False, withdrawn=False,
                             superseded=superseded)
    return SourceVerdict(
        qualifies=kind in QUALIFYING_PRODUCTION_AUTHORITY,
        withdrawn=version_status == SourceStatus.WITHDRAWN,
        superseded=superseded,
    )
