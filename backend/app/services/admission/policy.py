"""Governed admission policy: what may be attempted, how often, and how many at once.

The registry below is the ONLY place a limit is written down. Endpoints name an
`OperationClass`; they do not carry numbers. That is deliberate — a limit
expressed as a magic number at a call site is invisible to review, impossible to
audit as a set, and drifts silently from the operation it was meant to describe.

Three controls, deliberately separate, because they fail in different ways:

  RATE          "how often may this be asked for"      — retry storms, scripted abuse
  CONCURRENCY   "how much may be in flight at once"    — queue flooding, cost monopoly
  GLOBAL        "how much may the platform be doing"   — aggregate overload

A caller under its own rate limit can still be refused because the platform is
saturated; a caller with capacity to spare can still be refused for asking too
fast. Collapsing these into one counter loses both distinctions.

SCOPE, AND WHAT "TENANT" MEANS HERE
-----------------------------------
Onyx Ledger has no organization or household entity: `identity.user_account` is
the tenant, and row-level security is keyed on `app.user_id`. So the per-user and
per-tenant dimensions the threat model asks about are the SAME dimension in this
product, and claiming otherwise would be inventing a boundary that does not
exist. `ScopeType` is nevertheless an enum rather than a boolean so that adding
an organization tier later is an added member and a policy field — not a
redesign of the limiter.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum


class OperationClass(StrEnum):
    """Closed set. Every admission-controlled operation maps to exactly one.

    Endpoint paths are NOT policy keys: they change with routing, they multiply
    with versioning, and two endpoints of wildly different cost can share a
    prefix. A class names what the work COSTS.
    """

    CHEAP_READ = "CHEAP_READ"
    NORMAL_WRITE = "NORMAL_WRITE"
    AUTH_ATTEMPT = "AUTH_ATTEMPT"
    ANALYSIS_RUN = "ANALYSIS_RUN"
    OPTIMIZATION_RUN = "OPTIMIZATION_RUN"
    SCENARIO_RUN = "SCENARIO_RUN"
    DOCUMENT_UPLOAD = "DOCUMENT_UPLOAD"
    DOCUMENT_PROCESS = "DOCUMENT_PROCESS"
    IMPORT_RUN = "IMPORT_RUN"
    INTEGRITY_VERIFY = "INTEGRITY_VERIFY"
    ADMIN_RULE_PUBLISH = "ADMIN_RULE_PUBLISH"
    AI_EXPLAIN = "AI_EXPLAIN"


class ScopeType(StrEnum):
    """Whose budget is being spent.

    The first three are PRINCIPAL scopes: the caller has already been
    authenticated, so the identity is one the server assigned. The last two are
    PRE-AUTHENTICATION scopes and exist only for `AUTH_ATTEMPT`, where there is
    by definition no principal yet — the whole point of the operation is to find
    out whether the caller is one. They are derived, never accepted: see
    `app.services.admission.identity`.
    """

    USER = "USER"      # the tenant boundary in this product; see module docstring
    ADMIN = "ADMIN"    # an operator principal from `admin.admin_user`
    GLOBAL = "GLOBAL"  # the platform as a whole
    IP = "IP"          # the transport peer address, keyed-digested. Pre-auth only.
    AUTH_SUBJECT = "AUTH_SUBJECT"  # keyed digest of the claimed identity. Pre-auth only.


class RejectionReason(StrEnum):
    """Closed enumeration. What a caller is told, and what a metric counts.

    An exception message is unbounded text that has been near financial values;
    these codes are the only thing that crosses the API boundary or reaches a
    metric label.
    """

    USER_RATE_LIMIT = "USER_RATE_LIMIT"
    TENANT_RATE_LIMIT = "TENANT_RATE_LIMIT"
    #: Credential-testing throttle. A DISTINCT code from USER_RATE_LIMIT on
    #: purpose: it is returned before the caller is known to be anybody, so it
    #: must not be read as "your account is being limited" — which would itself
    #: disclose that the account exists.
    AUTH_RATE_LIMIT = "AUTH_RATE_LIMIT"
    USER_CONCURRENCY_LIMIT = "USER_CONCURRENCY_LIMIT"
    TENANT_CONCURRENCY_LIMIT = "TENANT_CONCURRENCY_LIMIT"
    GLOBAL_CAPACITY = "GLOBAL_CAPACITY"
    QUEUE_CAPACITY = "QUEUE_CAPACITY"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    TOO_MANY_ITEMS = "TOO_MANY_ITEMS"
    DUPLICATE_ACTIVE_OPERATION = "DUPLICATE_ACTIVE_OPERATION"
    OPERATION_COMPLEXITY_LIMIT = "OPERATION_COMPLEXITY_LIMIT"
    ADMISSION_STORE_UNAVAILABLE = "ADMISSION_STORE_UNAVAILABLE"


class StoreFailurePolicy(StrEnum):
    """What to do when the admission store itself cannot answer.

    Stated per class rather than globally, because the two wrong answers cost
    different amounts. Failing open on an optimization run means an outage
    removes exactly the protection that an outage makes most necessary. Failing
    closed on a profile read means a limiter problem becomes a site outage.
    """

    FAIL_OPEN = "FAIL_OPEN"      # admit; the operation is cheap enough to risk
    FAIL_CLOSED = "FAIL_CLOSED"  # refuse; the operation is too expensive to guess


@dataclass(frozen=True)
class AdmissionPolicy:
    """One operation class's complete admission contract.

    `None` means "this control does not apply to this class" — distinct from 0,
    which would mean "never admit". The constructor rejects 0 for exactly that
    reason.
    """

    operation: OperationClass

    # --- request rate: bounded attempts inside a fixed window ---
    per_user_per_minute: int | None = None
    burst: int = 0                       # extra attempts tolerated inside one window

    #: Allowance for a PRE-AUTHENTICATION source scope (`ScopeType.IP`), which
    #: only `AUTH_ATTEMPT` uses. It is a separate number rather than a reuse of
    #: `per_user_per_minute` because the two scopes bound different attacks and
    #: are wrong at each other's value: one address legitimately carries many
    #: people (an office behind NAT), while one identity legitimately carries
    #: one. Setting the address limit as tight as the identity limit would lock
    #: out a whole office; setting the identity limit as loose as the address
    #: limit would leave a single account open to sustained guessing.
    per_source_ip_per_minute: int | None = None

    # --- concurrency: bounded work IN FLIGHT ---
    max_active_per_user: int | None = None
    max_active_global: int | None = None

    # --- how long an admitted job may hold its slot before the lease expires ---
    lease_seconds: int = 900

    # --- behaviour when the store cannot answer ---
    on_store_failure: StoreFailurePolicy = StoreFailurePolicy.FAIL_OPEN

    # --- what a rejected caller is told to do ---
    retry_after_seconds: int = 60

    def __post_init__(self) -> None:
        # Validated at import, so a malformed policy is a startup failure rather
        # than a limit that silently never triggers.
        for field_name in ("per_user_per_minute", "per_source_ip_per_minute",
                           "max_active_per_user", "max_active_global"):
            value = getattr(self, field_name)
            if value is None:
                continue
            if value <= 0:
                raise ValueError(
                    f"{self.operation}.{field_name} must be positive "
                    f"(got {value}); use None to disable the control"
                )
        if self.burst < 0:
            raise ValueError(f"{self.operation}.burst must not be negative")
        if self.lease_seconds <= 0:
            raise ValueError(f"{self.operation}.lease_seconds must be positive")
        if self.retry_after_seconds <= 0:
            raise ValueError(f"{self.operation}.retry_after_seconds must be positive")
        if (
            self.max_active_per_user is not None
            and self.max_active_global is not None
            and self.max_active_per_user > self.max_active_global
        ):
            raise ValueError(
                f"{self.operation}: max_active_per_user ({self.max_active_per_user}) "
                f"exceeds max_active_global ({self.max_active_global}), so the "
                "per-user limit could never be reached"
            )

    @property
    def rate_allowance(self) -> int | None:
        """Attempts permitted in one window, burst included."""
        if self.per_user_per_minute is None:
            return None
        return self.per_user_per_minute + self.burst

    @property
    def source_rate_allowance(self) -> int | None:
        """Attempts permitted in one window from one source address."""
        if self.per_source_ip_per_minute is None:
            return None
        return self.per_source_ip_per_minute + self.burst

    def allowance_for(self, scope_type: ScopeType) -> int | None:
        """The window allowance that applies to one scope.

        Routed through a method so `_check_rate` cannot pick the wrong number by
        reading the wrong attribute: the mapping from scope to allowance is
        written once, here.
        """
        if scope_type is ScopeType.IP:
            return self.source_rate_allowance
        return self.rate_allowance

    @property
    def tracks_concurrency(self) -> bool:
        return self.max_active_per_user is not None or self.max_active_global is not None


# ---------------------------------------------------------------------------
# The registry. Every number here is justified in
# docs/architecture/rate-limiting-and-admission-control.md §4–§8.
#
# Defaults are deliberately conservative-but-usable: they are set where a human
# working normally will never meet them and a script will meet them immediately.
# All of them are overridable through validated settings.
# ---------------------------------------------------------------------------
_POLICIES: tuple[AdmissionPolicy, ...] = (
    # Reads are cheap and constant-cost. A generous ceiling stops a runaway
    # polling loop without ever inconveniencing a real session, and a limiter
    # outage must not take reads down with it.
    AdmissionPolicy(
        OperationClass.CHEAP_READ,
        per_user_per_minute=300,
        burst=60,
        on_store_failure=StoreFailurePolicy.FAIL_OPEN,
        retry_after_seconds=10,
    ),
    # Ordinary CRUD: one or two statements. Still cheap, but it writes.
    AdmissionPolicy(
        OperationClass.NORMAL_WRITE,
        per_user_per_minute=60,
        burst=20,
        on_store_failure=StoreFailurePolicy.FAIL_OPEN,
        retry_after_seconds=15,
    ),
    # Credential-testing surface, and the only class with no principal to bill:
    # an attempt is throttled per CLAIMED IDENTITY and per SOURCE ADDRESS, and
    # either one may refuse it. Two scopes because one attack evades each:
    #   * sustained guessing at one account arrives from many addresses, so the
    #     address counter never fills — the identity counter is what stops it;
    #   * credential stuffing sprays thousands of distinct accounts, so no
    #     identity counter fills — the address counter is what stops it.
    # Fail-closed: if the limiter cannot answer, slowing logins is the safer
    # failure, and login is not the surface to guess about.
    AdmissionPolicy(
        OperationClass.AUTH_ATTEMPT,
        per_user_per_minute=10,
        per_source_ip_per_minute=30,
        burst=5,
        on_store_failure=StoreFailurePolicy.FAIL_CLOSED,
        retry_after_seconds=60,
    ),
    # A full engine run + rules evaluation + snapshot, SYNCHRONOUS in the
    # request. One at a time per user is not a hardship: the second click has
    # nothing to add while the first is still running.
    AdmissionPolicy(
        OperationClass.ANALYSIS_RUN,
        per_user_per_minute=10,
        burst=2,
        max_active_per_user=1,
        max_active_global=200,
        lease_seconds=300,
        on_store_failure=StoreFailurePolicy.FAIL_CLOSED,
        retry_after_seconds=30,
    ),
    # The most expensive path in the system: pinned rule snapshot, candidate
    # normalization, scored ranking, portfolio assembly with a bounded engine
    # budget of up to 200 runs. Two concurrent per user is already generous.
    AdmissionPolicy(
        OperationClass.OPTIMIZATION_RUN,
        per_user_per_minute=6,
        burst=2,
        max_active_per_user=2,
        max_active_global=50,
        lease_seconds=900,
        on_store_failure=StoreFailurePolicy.FAIL_CLOSED,
        retry_after_seconds=60,
    ),
    # Cheaper than an optimization but still real engine work, and the surface
    # most likely to be driven from a UI slider.
    AdmissionPolicy(
        OperationClass.SCENARIO_RUN,
        per_user_per_minute=20,
        burst=10,
        max_active_per_user=3,
        max_active_global=100,
        lease_seconds=300,
        on_store_failure=StoreFailurePolicy.FAIL_CLOSED,
        retry_after_seconds=30,
    ),
    # Creating an upload is metadata-only; the bytes go straight to object
    # storage. Bounded to stop storage-key farming.
    AdmissionPolicy(
        OperationClass.DOCUMENT_UPLOAD,
        per_user_per_minute=30,
        burst=10,
        on_store_failure=StoreFailurePolicy.FAIL_CLOSED,
        retry_after_seconds=30,
    ),
    # OCR/extraction. Bounded concurrency because it is the classic path for
    # turning one cheap request into minutes of CPU.
    AdmissionPolicy(
        OperationClass.DOCUMENT_PROCESS,
        per_user_per_minute=20,
        burst=5,
        max_active_per_user=2,
        max_active_global=40,
        lease_seconds=600,
        on_store_failure=StoreFailurePolicy.FAIL_CLOSED,
        retry_after_seconds=60,
    ),
    # Legislation ingestion: operator-triggered, parses whole documents.
    AdmissionPolicy(
        OperationClass.IMPORT_RUN,
        per_user_per_minute=10,
        burst=2,
        max_active_per_user=2,
        max_active_global=10,
        lease_seconds=1800,
        on_store_failure=StoreFailurePolicy.FAIL_CLOSED,
        retry_after_seconds=120,
    ),
    # A manual replay re-executes a sealed calculation end to end. The service
    # already refuses a second verification of the SAME entity; this bounds how
    # many DIFFERENT entities one caller can replay at once.
    AdmissionPolicy(
        OperationClass.INTEGRITY_VERIFY,
        per_user_per_minute=10,
        burst=5,
        max_active_per_user=2,
        max_active_global=20,
        lease_seconds=300,
        on_store_failure=StoreFailurePolicy.FAIL_CLOSED,
        retry_after_seconds=60,
    ),
    # Publication is four-eyes governed and rare; the limit is an anti-runaway
    # guard, not a workflow constraint.
    AdmissionPolicy(
        OperationClass.ADMIN_RULE_PUBLISH,
        per_user_per_minute=30,
        burst=10,
        on_store_failure=StoreFailurePolicy.FAIL_CLOSED,
        retry_after_seconds=30,
    ),
    # Retrieval + a provider call. Cost is dominated by something outside this
    # process, which is exactly why it needs a ceiling here.
    AdmissionPolicy(
        OperationClass.AI_EXPLAIN,
        per_user_per_minute=20,
        burst=5,
        max_active_per_user=2,
        max_active_global=50,
        lease_seconds=180,
        on_store_failure=StoreFailurePolicy.FAIL_CLOSED,
        retry_after_seconds=30,
    ),
)

def _configured(base: tuple[AdmissionPolicy, ...]) -> dict[OperationClass, AdmissionPolicy]:
    """Overlay validated settings onto the compiled defaults.

    The defaults above are the documented starting point; an operator can move
    any of them without a deploy. Settings are validated in `app.core.config`
    (positive, and per-user never above the platform cap), and `AdmissionPolicy`
    validates again on construction — so an override that would produce an
    unreachable or never-firing limit fails at import rather than at 3am.

    Each override is written out with real keywords rather than routed through a
    `**kwargs` helper. A helper reads more compactly and defeats the type
    checker: `**dict[str, int]` against a dataclass whose fields are not all
    ints checks nothing, so a misspelled field name would surface at runtime in
    production rather than here.
    """
    from app.core.config import get_settings

    settings = get_settings()
    resolved = {policy.operation: policy for policy in base}

    resolved[OperationClass.AUTH_ATTEMPT] = replace(
        resolved[OperationClass.AUTH_ATTEMPT],
        per_user_per_minute=settings.rate_limit_auth_per_minute,
        per_source_ip_per_minute=settings.rate_limit_auth_per_source_ip_per_minute,
    )
    resolved[OperationClass.ANALYSIS_RUN] = replace(
        resolved[OperationClass.ANALYSIS_RUN],
        per_user_per_minute=settings.rate_limit_analysis_per_minute,
        max_active_per_user=settings.max_active_analyses_per_user,
        max_active_global=settings.global_max_active_analyses,
        lease_seconds=settings.admission_lease_seconds,
    )
    resolved[OperationClass.OPTIMIZATION_RUN] = replace(
        resolved[OperationClass.OPTIMIZATION_RUN],
        per_user_per_minute=settings.rate_limit_optimization_per_minute,
        max_active_per_user=settings.max_active_optimizations_per_user,
        max_active_global=settings.global_max_active_optimizations,
        lease_seconds=settings.admission_lease_seconds,
    )
    resolved[OperationClass.SCENARIO_RUN] = replace(
        resolved[OperationClass.SCENARIO_RUN],
        per_user_per_minute=settings.rate_limit_scenario_per_minute,
        max_active_per_user=settings.max_active_scenarios_per_user,
        max_active_global=settings.global_max_active_scenarios,
    )
    resolved[OperationClass.DOCUMENT_PROCESS] = replace(
        resolved[OperationClass.DOCUMENT_PROCESS],
        max_active_per_user=settings.max_active_document_processing_per_user,
        max_active_global=settings.global_max_active_document_processing,
    )
    return resolved


POLICIES: dict[OperationClass, AdmissionPolicy] = _configured(_POLICIES)

# Every class must have exactly one policy. A class added to the enum without a
# policy is a startup failure, not an operation that quietly has no limits.
_missing = sorted(set(OperationClass) - set(POLICIES))
if _missing:
    raise RuntimeError(f"OperationClass without an admission policy: {_missing}")


def policy_for(operation: OperationClass) -> AdmissionPolicy:
    return POLICIES[operation]


# ---------------------------------------------------------------------------
# WIRING LEDGER
#
# Every member of `OperationClass` is either guarded at a real call site or
# listed here with the reason it is not. A class may not be silently absent
# from both, and `tests/unit/test_admission_wiring.py` fails if one is.
#
# This exists because the opposite failure is the easy one to miss. A policy
# with no call site LOOKS like protection in review, reads as a limit in the
# registry, and bounds nothing — Entry 10 Phase 1 shipped six of them. Requiring
# an explicit entry here turns "nobody wired it" into a visible, reviewable
# claim rather than an omission.
#
# It is NOT a licence to close by enum count in the other direction either: an
# entry is a statement that the class has no reachable production surface today,
# and adding one for a class that does have a surface is the same defect wearing
# a comment.
# ---------------------------------------------------------------------------
UNWIRED_BY_DESIGN: dict[OperationClass, str] = {
    OperationClass.CHEAP_READ: (
        "REACHABLE_ADMISSION_NOT_NEEDED. Every GET in the API is a bounded, "
        "indexed, RLS-scoped read with no amplification: one request costs one "
        "or two index lookups and returns a bounded page. Guarding each one "
        "would add a round trip to the admission store to the cheapest "
        "operations in the system — the limiter would become a meaningful "
        "share of the load it exists to protect against, and its own store "
        "would be the first thing to saturate. Read-abuse belongs at the "
        "ingress/CDN layer, which sees the request before a worker is woken at "
        "all. The policy stays as the DECLARED template for that layer and for "
        "any future read that stops being cheap; it is not pretending to be "
        "enforced here."
    ),
}


UNUSED_REJECTION_REASONS: dict[RejectionReason, str] = {
    RejectionReason.TENANT_RATE_LIMIT: (
        "The user IS the tenant in this product (see the module docstring), so "
        "USER_RATE_LIMIT already is the tenant limit. Kept as a distinct code "
        "so that adding an organization tier does not have to reinterpret an "
        "existing one in flight."
    ),
    RejectionReason.TENANT_CONCURRENCY_LIMIT: (
        "Same reason as TENANT_RATE_LIMIT."
    ),
    RejectionReason.QUEUE_CAPACITY: (
        "RESERVED. Nothing in `app/` publishes a Celery task: the only "
        "`.delay()` calls in the repository are worker-to-worker stage "
        "chaining inside `workers/tasks/tkms.py`, reached only after an "
        "operator-triggered import has already passed IMPORT_RUN admission at "
        "the API. There is therefore no user-reachable enqueue path whose "
        "broker depth could be refused, and a code that no caller can receive "
        "would be a claim of protection that does not exist. "
        "`tests/unit/test_admission_wiring.py` asserts the premise, so this "
        "reservation cannot quietly become false."
    ),
    RejectionReason.PAYLOAD_TOO_LARGE: (
        "Size and complexity bounds are refused as ValidationError (422) by "
        "the schema or the handler, before admission is consulted at all — a "
        "payload that is too large is malformed, not throttled, and telling "
        "the caller to retry after 60 seconds would be wrong. Kept for a future "
        "surface that streams rather than declares."
    ),
    RejectionReason.TOO_MANY_ITEMS: ("Same reason as PAYLOAD_TOO_LARGE."),
    RejectionReason.OPERATION_COMPLEXITY_LIMIT: ("Same reason as PAYLOAD_TOO_LARGE."),
}


__all__ = [
    "POLICIES",
    "UNUSED_REJECTION_REASONS",
    "UNWIRED_BY_DESIGN",
    "AdmissionPolicy",
    "OperationClass",
    "RejectionReason",
    "ScopeType",
    "StoreFailurePolicy",
    "policy_for",
]
