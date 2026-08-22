from __future__ import annotations

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    client_ip,
    current_user_id,
    db_anon,
    db_authed,
    db_authed_unverified_ok,
)
from app.schemas import LoginRequest, RefreshRequest, RegisterRequest, TokenPair
from app.schemas.recovery import (
    EmailVerificationRequest,
    PasswordResetCompletion,
    PasswordResetRequest,
    RecoveryAccepted,
    VerificationResult,
)
from app.services.admission.auth import admit_auth_attempt, admit_recovery_request
from app.services.auth.recovery import AccountRecoveryService
from app.services.auth.service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest, request: Request, session: AsyncSession = Depends(db_anon)
) -> dict:
    """Create an account.

    Throttled on the same class as login. Registration is not a credential test,
    but it is unauthenticated, it writes two rows and computes an Argon2 hash,
    and — because it must say whether an address is already taken — an
    unthrottled version is a fast account-enumeration oracle. The throttle does
    not remove that disclosure; it bounds how quickly it can be harvested.
    """
    await admit_auth_attempt(source_ip=client_ip(request), subject=body.email)
    user = await AuthService(session).register(body.email, body.password)
    return {"id": str(user.id), "email": user.email, "status": user.status}


@router.post("/login", response_model=TokenPair)
async def login(
    body: LoginRequest, request: Request, session: AsyncSession = Depends(db_anon)
) -> TokenPair:
    """Exchange credentials for a token pair.

    Admission runs FIRST — before the user lookup and before Argon2 — so a
    refused attempt costs one indexed UPSERT instead of a deliberately expensive
    key derivation. See `app.services.admission.auth`.
    """
    await admit_auth_attempt(source_ip=client_ip(request), subject=body.email)
    return await AuthService(session).authenticate(body.email, body.password, client_ip(request))


@router.post("/refresh", response_model=TokenPair)
async def refresh(
    body: RefreshRequest, request: Request, session: AsyncSession = Depends(db_anon)
) -> TokenPair:
    """Rotate a refresh token.

    Also a credential surface: a stolen or guessed refresh token is presented
    here. The identity scope is keyed on the presented TOKEN rather than on an
    email, because that is the only identity claim the request carries — it
    bounds how fast one token can be hammered, while the address scope bounds
    hammering with a rotating supply of them.
    """
    await admit_auth_attempt(source_ip=client_ip(request), subject=body.refresh_token)
    return await AuthService(session).refresh(body.refresh_token, client_ip(request))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> None:
    await AuthService(session).logout(user_id)


# ---------------------------------------------------------------------------
# Email verification and password recovery
#
# EVERY SEND IS A BACKGROUND TASK, without exception. `AccountRecoveryService.
# deliver` explains the three reasons; the one that decides the shape of these
# routes is timing. `POST /password-reset` must take the same measurable time
# for an address with an account and one without, and a provider round trip is
# three orders of magnitude larger than the database miss it would be compared
# against. Scheduling the send after the response is what keeps the careful
# wording of `RecoveryAccepted` from being undone by a stopwatch.
#
# The ordering in each handler is the same and is deliberate:
#
#     admission  →  unit of work (mint, audit)  →  COMMIT  →  schedule send
#
# The commit happens when the `db_*` dependency's context manager exits, which
# is after the handler returns — so the background task, which runs after the
# response, is always looking at committed state.
# ---------------------------------------------------------------------------


@router.post(
    "/verification",
    response_model=RecoveryAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def send_verification(
    request: Request,
    background: BackgroundTasks,
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed_unverified_ok),
) -> RecoveryAccepted:
    """Send, or re-send, this account's verification link.

    AUTHENTICATED, and that is what makes it safe to offer at all. An anonymous
    "send verification to this address" endpoint is an enumeration oracle and an
    email-flood amplifier pointed at anybody whose address you can guess; this
    one can only ever mail the account whose token was presented.

    `db_authed_unverified_ok` rather than `db_authed`: this is the endpoint that
    clears the unverified state, so it is the one that cannot require it to be
    clear already. Suspension, closure and the deletion cutoff all still apply.

    Throttled on `ACCOUNT_RECOVERY` keyed on the ACCOUNT, not the address — the
    address would have to be read from the user table before the limiter ran,
    and they are one to one anyway.

    202, and the same body whatever happened. An account that is already
    verified mints nothing and sends nothing, and says so no differently.
    """
    await admit_recovery_request(source_ip=client_ip(request), mailbox=str(user_id))
    service = AccountRecoveryService(session)
    pending = await service.request_verification(user_id)
    background.add_task(service.deliver, pending)
    return RecoveryAccepted()


@router.post("/verification/confirm", response_model=VerificationResult)
async def confirm_verification(
    body: EmailVerificationRequest,
    request: Request,
    session: AsyncSession = Depends(db_anon),
) -> VerificationResult:
    """Complete verification by presenting a link's token.

    ANONYMOUS on purpose: the link is clicked in whatever browser opened the
    mail, which is routinely not the one holding a session. Possession of the
    token is the entire claim, and requiring a bearer token as well would break
    the common case to add nothing — an attacker holding the token does not
    also need to be signed in.

    Throttled on `AUTH_ATTEMPT` keyed on the presented token, exactly as
    `/refresh` is and for the same reason: this is a credential surface, and the
    credential is the only identity claim the request carries.
    """
    await admit_auth_attempt(source_ip=client_ip(request), subject=body.token)
    status_after = await AccountRecoveryService(session).verify_email(body.token)
    return VerificationResult(status=status_after)


@router.post(
    "/password-reset",
    response_model=RecoveryAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_password_reset(
    body: PasswordResetRequest,
    request: Request,
    background: BackgroundTasks,
    session: AsyncSession = Depends(db_anon),
) -> RecoveryAccepted:
    """Ask for a reset link.

    NON-ENUMERATING, and the handler is written so that it cannot accidentally
    stop being so. There is one `return` and it is unconditional; the service
    answers `None` for every address it will not mail — unknown, suspended,
    closed, deleting, or asked for too recently — and `deliver(None)` does
    nothing. No branch in this function can observe which of those happened, so
    no future edit can leak it by adding a field to one arm.

    202 rather than 200: the honest status for "your request is accepted and
    something may happen later", and identical either way.
    """
    await admit_recovery_request(source_ip=client_ip(request), mailbox=body.email)
    service = AccountRecoveryService(session)
    pending = await service.request_password_reset(body.email)
    background.add_task(service.deliver, pending)
    return RecoveryAccepted()


@router.post("/password-reset/confirm", status_code=status.HTTP_204_NO_CONTENT)
async def complete_password_reset(
    body: PasswordResetCompletion,
    request: Request,
    background: BackgroundTasks,
    session: AsyncSession = Depends(db_anon),
) -> None:
    """Set a new password from a reset link, and end every existing session.

    Anonymous for the same reason as verification: the link is clicked wherever
    the mail was opened, and the token is the claim.

    204 and no body. Returning a token pair here would be convenient and wrong
    — it would hand a session to whoever holds the link, in the one flow whose
    premise is that somebody else may have had access. The customer signs in
    with the password they just chose, which also proves they know it.
    """
    await admit_auth_attempt(source_ip=client_ip(request), subject=body.token)
    service = AccountRecoveryService(session)
    email = await service.complete_password_reset(body.token, body.new_password)
    background.add_task(service.deliver, service.password_changed_notice(email))
