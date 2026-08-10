"""Entry 11B5J — the deletion worker must survive being called twice.

A Celery worker calls a task many times in one process. `asyncio.run` creates a
fresh event loop per invocation and closes it on the way out, but the engines
are module-level and their pools are not: a connection opened under one loop
goes back into the pool, the loop dies, and the next invocation is handed a
connection bound to a dead loop.

Measured before the fix, calling the task repeatedly in one process:

    call 1: OK
    call 2: RuntimeError ... got Future ... attached to a different loop
    call 3: OK          (the poisoned connection had been evicted)

So roughly every other run failed, and for this task the run it abandons is an
account purge. The task now disposes its engines inside the loop that owns
them, before `asyncio.run` tears that loop down.

THIS TEST IS ABOUT RE-ENTRANCY, not about the lifecycle: it drives the real
zero-argument production task end to end, several times, in ONE process — which
is the only shape in which the defect appears. A test that called it once would
have passed throughout.
"""
from __future__ import annotations

import uuid

import psycopg2
import pytest

from tests.conftest import owner_dsn

PHASE = "SOURCE_DATA"


@pytest.fixture(autouse=True)
async def _dispose_engines():
    yield
    from app.database.privacy_session import dispose_all_engines

    await dispose_all_engines()


def _owner():
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    return conn


def _subject(cur) -> uuid.UUID:
    user = uuid.uuid4()
    cur.execute("INSERT INTO identity.user_account (id, email, status) "
                "VALUES (%s, %s, 'active')",
                (str(user), f"reent_{uuid.uuid4().hex[:10]}@example.com"))
    cur.execute("INSERT INTO profile.tax_profile (user_id, province_code, "
                "marital_status) VALUES (%s, 'ON', 'single')", (str(user),))
    cur.execute("""
        INSERT INTO finance.income_source
            (user_id, tax_year, income_type_id, amount, province_code)
        SELECT %s, 2025, id, 5150, 'ON' FROM ref.income_type
         WHERE code = 'employment'
    """, (str(user),))
    cur.execute("INSERT INTO identity.account_lifecycle (user_id, state) "
                "VALUES (%s, 'DELETION_REQUESTED')", (str(user),))
    return user


def _state(cur, user: uuid.UUID) -> tuple[str, int, str | None]:
    cur.execute("SELECT state FROM identity.account_lifecycle WHERE user_id = %s",
                (str(user),))
    state = cur.fetchone()[0]
    cur.execute("SELECT identity.count_remaining_source_data(%s)", (str(user),))
    remaining = cur.fetchone()[0]
    cur.execute("SELECT status FROM identity.account_lifecycle_phase "
                " WHERE user_id = %s AND phase = %s", (str(user), PHASE))
    row = cur.fetchone()
    return state, remaining, (row[0] if row else None)


def test_the_deletion_worker_can_be_called_repeatedly_in_one_process():
    """Six invocations, as a Celery worker would make them.

    The task is synchronous by design — Celery owns the process, the task owns
    its loop — so this test is synchronous too and calls `.run()` directly
    rather than going through a fixture that would create a loop of its own.
    """
    from workers.tasks.privacy import run_account_deletion_phases

    admin = _owner()
    try:
        cur = admin.cursor()
        user = _subject(cur)
        assert _state(cur, user) == ("DELETION_REQUESTED", 2, None), (
            f"unexpected starting state: {_state(cur, user)}")

        failures: list[str] = []
        for attempt in range(1, 7):
            try:
                run_account_deletion_phases.run()
            except Exception as exc:                       # noqa: BLE001
                failures.append(f"call {attempt}: {type(exc).__name__}: {exc}")

        assert not failures, (
            "the deletion worker is not re-entrant — a Celery process calls it "
            "many times:\n  " + "\n  ".join(failures))

        state, remaining, phase = _state(cur, user)
        assert remaining == 0, (
            f"the worker did not finish the purge across six runs: {remaining} "
            "qualifying rows remain")
        assert phase == "COMPLETE", f"SOURCE_DATA phase is {phase}"
        # SOURCE_DATA completing is not the account completing — later phases
        # have not run, and the worker must not pretend otherwise.
        assert state != "COMPLETE", (
            "the account reached COMPLETE though only SOURCE_DATA has run")
    finally:
        admin.close()


def test_repeated_runs_after_completion_stay_idempotent():
    """The worker keeps being scheduled after an account is done. Extra runs
    must not re-open a phase, duplicate a phase row, or resurrect data."""
    from workers.tasks.privacy import run_account_deletion_phases

    admin = _owner()
    try:
        cur = admin.cursor()
        user = _subject(cur)
        for _ in range(4):
            run_account_deletion_phases.run()
        settled = _state(cur, user)
        assert settled[1] == 0 and settled[2] == "COMPLETE", (
            f"the fixture never reached a settled state: {settled}")

        for _ in range(3):
            run_account_deletion_phases.run()

        assert _state(cur, user) == settled, (
            f"further runs changed a settled account: {_state(cur, user)} "
            f"was {settled}")
        cur.execute("SELECT count(*) FROM identity.account_lifecycle_phase "
                    " WHERE user_id = %s AND phase = %s", (str(user), PHASE))
        assert cur.fetchone()[0] == 1, "repeated runs duplicated the phase record"
    finally:
        admin.close()
