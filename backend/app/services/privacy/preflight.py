"""The worker-side deletion cutoff (Entry 11B1 §10).

THE RACE THIS EXISTS FOR
------------------------
A task is queued at T1. Deletion is requested at T2. A worker picks the task up
at T3. Nothing about the queue prevents T1 < T2 < T3 — the message was published
long before anyone asked to be deleted, and it is sitting in a broker that knows
nothing about privacy.

Celery `revoke` does not close this. A reserved task is already in a worker's
hands, `acks_late` means it will be redelivered after a restart, and revocation
is best-effort broadcast state that a worker which was offline at the time never
sees. The only reliable place to refuse is immediately before the work commits
anything, in the same database the deletion was recorded in.

WHY IT RETURNS RATHER THAN RAISES
---------------------------------
A refused task has not failed. Raising would put it through the retry ladder,
log an error, and — before Entry 11A closed that path — write an exception into
the result backend. Declining is the correct outcome and should look like one.
"""
from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger

log = get_logger("onyx.privacy.preflight")


async def account_is_deleting(session: AsyncSession, user_id: uuid.UUID) -> bool:
    """True when the account is past its deletion cutoff.

    Reads through `identity.account_deletion_state`, because a worker session
    sets no `app.user_id` and the RLS policy would correctly hide the lifecycle
    row — a direct read would see nothing and let every task through.

    One statement on a connection the caller already holds.
    """
    state = await session.scalar(
        text("SELECT identity.account_deletion_state(:uid)"),
        {"uid": user_id},
    )
    return state is not None


async def refuse_if_deleting(
    session: AsyncSession, user_id: uuid.UUID, *, task: str
) -> bool:
    """Preflight for a user-data-producing task. True means STOP.

    Logs a closed code and no identifier beyond the task name — a deleting
    account should not become more visible in the logs than an active one.
    """
    if await account_is_deleting(session, user_id):
        log.info("privacy.task_refused", task=task,
                 reason_code="ACCOUNT_DELETION_IN_PROGRESS")
        return True
    return False


__all__ = ["account_is_deleting", "refuse_if_deleting"]
