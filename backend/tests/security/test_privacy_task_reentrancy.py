"""Entry 11B5J — every 11B5 worker must survive being called twice.

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
    # OLDEST IN THE QUEUE, deliberately. `run_account_deletion_phases` claims
    # a bounded batch (`_BATCH = 10`) ordered by `requested_at`, oldest first,
    # and it takes no arguments — it chooses its own work, so a test cannot
    # hand it a subject. A freshly requested account is therefore the LAST one
    # a worker reaches, and on a database carrying a backlog it is never
    # reached at all: measured at 671 claimable lifecycles, six invocations
    # claim 60 and this subject still read ("DELETION_REQUESTED", 2, None).
    #
    # Backdating the request makes the subject reachable without touching what
    # this file actually tests, which is re-entrancy across invocations in one
    # process — not queue position. Same lesson as the freshness drains: a
    # bounded worker is correct, and a test that assumes it will reach the
    # newest row is not.
    cur.execute("INSERT INTO identity.account_lifecycle "
                "  (user_id, state, requested_at) "
                "VALUES (%s, 'DELETION_REQUESTED', now() - interval '10 years')",
                (str(user),))
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


# ---------------------------------------------------------------------------
# Every recurring task the Entry 11B5 architecture depends on
# ---------------------------------------------------------------------------
def test_every_entry_11b5_task_survives_repeated_invocation_in_one_process():
    """The generalised guard, covering the whole 11B5-required task set.

    Fixing only the deletion worker left the freshness relay — scheduled every
    minute — still failing on its second call in a process, and the integrity
    verifier failing on its first and third. All of them are load-bearing for
    Entry 11B5: the relay drives freshness convergence, the sweep is its
    fallback, the invalidators are the event path, and the verifier is the
    replay/integrity path H3 certifies.

    Measured before the shared `workers.runtime.run_task` helper, three calls
    each in one process:

        privacy.run_account_deletion_phases   call 2 failed
        ioe.relay_freshness_outbox            call 2 failed
        ioe.sweep_scenario_freshness          call 2 failed
        ioe.verify_sealed_integrity           calls 1 and 3 failed

    Not a clean alternation — it depends which pooled connection is handed
    out — which is why it reads as flakiness rather than as a defect.
    """
    import uuid as _uuid

    from workers.tasks.ioe import (
        invalidate_scenarios_for_analysis,
        invalidate_scenarios_for_tax_year,
        relay_freshness_outbox,
        sweep_scenario_freshness,
        verify_sealed_integrity,
    )
    from workers.tasks.maintenance import purge_admission_history
    from workers.tasks.privacy import run_account_deletion_phases

    cases = {
        "privacy.run_account_deletion_phases": lambda: run_account_deletion_phases.run(),
        "ioe.relay_freshness_outbox": lambda: relay_freshness_outbox.run(),
        "ioe.sweep_scenario_freshness": lambda: sweep_scenario_freshness.run(),
        "ioe.verify_sealed_integrity": lambda: verify_sealed_integrity.run(),
        "ioe.invalidate_scenarios_for_tax_year":
            lambda: invalidate_scenarios_for_tax_year.run(2025, "BASELINE_INPUTS_CHANGED"),
        "ioe.invalidate_scenarios_for_analysis":
            lambda: invalidate_scenarios_for_analysis.run(
                str(_uuid.uuid4()), "BASELINE_INPUTS_CHANGED"),
        # ADDED AFTER THE LEAN-LAUNCH ENTRY FOUND IT STILL BROKEN.
        #
        # This list was written as "every task Entry 11B5 depends on", and
        # `purge_admission_history` was not one of them — so it kept calling
        # `asyncio.run` directly for three more entries while this test passed.
        # Measured then: call 1 ok, call 2 RuntimeError, call 3 ok.
        #
        # An enumeration goes stale exactly this way, which is why
        # `test_worker_runtime.py` now also asserts the STRUCTURAL rule: the
        # worker tree contains one `asyncio.run`, and it is the one inside
        # `run_task`. This entry stays because a behavioural proof and a
        # structural proof fail for different reasons.
        "maintenance.purge_admission_history":
            lambda: purge_admission_history.run(),
    }

    # FIVE consecutive calls, not three. The failure was never a clean
    # alternation — `verify_sealed_integrity` failed on calls 1 and 3 and
    # passed on 2 — so a short run can land on a lucky pattern. Five covers
    # both parities twice over.
    failures: list[str] = []
    for name, call in cases.items():
        for attempt in (1, 2, 3, 4, 5):
            try:
                call()
            except Exception as exc:                       # noqa: BLE001
                failures.append(f"{name} call {attempt}: {type(exc).__name__}: {exc}")

    assert not failures, (
        "these Entry 11B5 tasks cannot be called repeatedly in one Celery "
        "worker process:\n  " + "\n  ".join(f[:160] for f in failures))
