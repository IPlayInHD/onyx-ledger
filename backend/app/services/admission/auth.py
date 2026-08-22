"""Admission for the pre-authentication surface.

This is the one place in Entry 10 where admission runs BEFORE the caller is
known, and the ordering is the whole point:

    admission  →  user lookup  →  Argon2 verification

not the other way around. `verify_password` is an Argon2id verification, which
is deliberately expensive — that expense is what makes a stolen hash hard to
crack, and it is also what makes an unthrottled login endpoint the cheapest
denial-of-service in the product. A caller who can force one Argon2 verification
per request can spend a server's memory and CPU budget from a laptop. Admission
first means a refused attempt costs one indexed UPSERT.

TWO SCOPES, EITHER MAY REFUSE
-----------------------------
Every attempt is charged to BOTH the claimed identity and the source address,
and both counters advance even when one of them is the reason the attempt was
refused. An attempt was made, and the accounting says so regardless of which
limit caught it — otherwise an attacker who parks on a single account would fill
that account's counter and then run indefinitely without ever advancing the
address counter that exists to catch them.

WHAT THIS DOES NOT DO
---------------------
It never touches the user table. It cannot, and must not: a limiter that behaved
differently for a real address than for an invented one would be an account
enumeration oracle that answers faster than the login itself. The digest is
computed from what the caller typed, and an address with no account gets exactly
the same treatment as one with.

ACCEPTED COST
-------------
Charging the identity scope means someone who knows a victim's address can spend
that address's allowance and have the victim refused for the remainder of the
window. The window is one minute, the victim's next attempt lands in a fresh
one, and the alternative — not throttling per identity — leaves the account open
to sustained guessing from a rotating address pool. This is the standard trade
and it is made knowingly.
"""
from __future__ import annotations

from app.database.session import unit_of_work
from app.services.admission.identity import auth_subject_scope, source_ip_scope
from app.services.admission.policy import OperationClass
from app.services.admission.service import AdmissionService

#: Stand-in scope key for a request with no derivable peer address. A missing
#: address is pooled into ONE bucket rather than skipped, so a transport that
#: hides the peer cannot be used to opt out of address throttling.
_UNKNOWN_SOURCE = "unknown-source"


async def _charge(
    operation: OperationClass, *, source_ip: str | None, subject: str
) -> None:
    async with unit_of_work(actor_type="system") as session:
        decision = await AdmissionService(session).charge_preauth_attempt(
            operation,
            source_scope_id=source_ip_scope(source_ip or _UNKNOWN_SOURCE),
            subject_scope_id=auth_subject_scope(subject),
        )
    decision.raise_if_rejected()


async def admit_auth_attempt(*, source_ip: str | None, subject: str) -> None:
    """Charge one credential attempt, or raise `AdmissionRejected`.

    Returns nothing: `AUTH_ATTEMPT` declares no concurrency control, so there is
    no lease to hold and nothing for the caller to release. That is why this is a
    plain call rather than an `admission_guard` context manager — a guard whose
    ticket never holds anything would invite a `finally` that releases nothing
    and read as if a slot were being managed.

    Runs in its own short transaction, committed before the credential work
    starts, for the same reason `admission_guard` does: an uncommitted counter is
    invisible to the other API replicas, so the limit would hold only within one
    process. The refusal is raised AFTER that transaction closes, so the counters
    it just advanced are not rolled back by the exception that reports them.
    """
    await _charge(OperationClass.AUTH_ATTEMPT, source_ip=source_ip, subject=subject)


async def admit_recovery_request(*, source_ip: str | None, mailbox: str) -> None:
    """Charge one message-sending recovery request, or raise `AdmissionRejected`.

    SEPARATE FROM `admit_auth_attempt`, AND THAT IS THE POINT. Sharing the
    counter would have made either limit wrong for the other operation: at
    AUTH_ATTEMPT's allowance a stranger could put fifteen messages a minute in
    somebody's inbox, and at this one a customer who mistyped their password
    three times would be locked out of logging in.

    `mailbox` is whatever identifies the DESTINATION — the address a caller
    typed, or the account id of an authenticated resend. Never the account's
    stored address for an anonymous caller: looking it up to build the key would
    put a user-table read in front of the limiter, which is how a limiter
    becomes the enumeration oracle it was supposed to bound.
    """
    await _charge(
        OperationClass.ACCOUNT_RECOVERY, source_ip=source_ip, subject=mailbox
    )


__all__ = ["admit_auth_attempt", "admit_recovery_request"]
