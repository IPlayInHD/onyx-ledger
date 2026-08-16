"""Before-You-Act comparison — the product-facing read contract (Entry 12B).

WHAT THIS CONTRACT PROMISES. One sealed scenario, described as the difference
between the frozen baseline and the sealed counterfactual: what would change if
you did this. Every value was determined by a governed authority at seal time
and is reported here unchanged.

WHAT IT DELIBERATELY DOES NOT PROMISE. No savings figure, no ranking, no "best"
strategy, no recommendation, no probability that a filing is accepted. This
surface reports WHAT CHANGED. Deciding what someone should do is a different
question owned elsewhere, and a comparator that answered it would be making a
tax decision while claiming only to describe one.

ITS OWN VERSION. `schema_version` belongs to this contract and moves when the
product shape moves. It is deliberately NOT the scenario-result schema version:
a client rendering this payload cares what the payload looks like, and coupling
it to the sealed protocol would re-version every client for a change no client
can see. Both appear in the response, so a caller can tell them apart.

NUMBERS ARE STRINGS. Money and rates arrive as the canonicalizer rendered them
at seal time and are passed through verbatim. Parsing them into a float here to
"clean them up" would change the value the seal recorded, so nothing in this
module converts a numeric field.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

#: This product contract's version. Independent of
#: `CURRENT_SCENARIO_RESULT_SCHEMA_VERSION` — see the module docstring.
BEFORE_YOU_ACT_SCHEMA_VERSION = "1.0.0"


class FieldChangeOut(BaseModel):
    """One field that moved, with the arithmetic only where it is defined.

    `delta` is present only for a governed numeric field whose two sides share a
    declared unit. Its absence is meaningful: it says the domain does not define
    a difference here, not that the difference is zero.
    """

    model_config = ConfigDict(from_attributes=True)

    field: str
    before: str | None
    after: str | None
    delta: str | None


class ChangeOut(BaseModel):
    """One node that was added, removed, changed, or left alone.

    `key` is the certified semantic identity. It is exposed because a client
    needs a stable handle to correlate a record across two requests; it is not
    an internal row id and resolves to nothing on its own.
    """

    model_config = ConfigDict(from_attributes=True)

    key: str
    change: str
    fields: list[FieldChangeOut] = Field(default_factory=list)


class FamilyApplicabilityOut(BaseModel):
    """Why a family is comparable, or why it is not.

    Carried so a reader can tell a family that is comparable and empty from one
    that does not apply at all. Dropping this would leave "no resources shown"
    and "resources are not a single-scenario concept" looking identical, which
    is the distinction the whole comparison rests on.
    """

    model_config = ConfigDict(from_attributes=True)

    family: str
    status: str
    reason_code: str


class ComparisonSummaryOut(BaseModel):
    """Counts over the FULL comparison, before any presentation filtering.

    They do not move when `include_unchanged` changes, because the summary
    describes the comparison and the lists describe what was rendered.
    """

    model_config = ConfigDict(from_attributes=True)

    node_counts_by_change: dict[str, int]
    edge_counts_by_change: dict[str, int]
    changed_families: list[str]


class ScenarioContextOut(BaseModel):
    """Scenario metadata appropriate for customer display.

    Enough to title and date the comparison. No internal storage identifiers, no
    document ids, no object keys.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    label: str | None
    tax_year: int | None
    jurisdiction: str | None
    result_schema_version: str | None
    completed_at: datetime | None


class BeforeYouActComparisonOut(BaseModel):
    """The whole product contract.

    Change records are grouped by family rather than returned as one list, so a
    client renders "what happens to your tax position" separately from "what you
    would need to prove". A single flat list would push that classification onto
    every client, and they would each do it differently.
    """

    model_config = ConfigDict(from_attributes=True)

    schema_version: str
    direction: str
    scenario: ScenarioContextOut

    summary: ComparisonSummaryOut

    tax_state_changes: list[ChangeOut]
    opportunity_changes: list[ChangeOut]
    evidence_changes: list[ChangeOut]
    deadline_changes: list[ChangeOut]
    fact_changes: list[ChangeOut]
    assumption_changes: list[ChangeOut]
    scenario_changes: list[ChangeOut]

    family_applicability: list[FamilyApplicabilityOut]

    #: The governed comparison identity, in the same family as
    #: `scenario_result_hash`. Two responses carrying the same hash describe the
    #: same difference between the same two sealed artifacts.
    comparison_hash: str

    #: Whether UNCHANGED records were rendered. Presentation only: the hash and
    #: the summary are identical either way.
    includes_unchanged: bool
