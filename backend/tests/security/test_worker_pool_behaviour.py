"""Entry 11B5I — worker concurrency and connection-pool behaviour.

This project has produced the same pooled-connection failure four times: a code
path quietly starts using a second engine, the disposal site next to it names
only the first, and tests pass alone and fail together. Every time it was found
by an unrelated test failing, never by a metric. So this file reads the metric.

Three engines exist in the final topology and each authenticates as a different
PostgreSQL principal:

    application   ONYX_DATABASE_URL             onyx_app_rw
    privacy       ONYX_PRIVACY_DATABASE_URL     onyx_privacy_worker
    freshness     ONYX_FRESHNESS_DATABASE_URL   onyx_freshness_worker

The freshness runtime carries the integrity scheduler too, since H2D moved
`claim_integrity_targets` and `recover_stale_integrity_checks` off the
application engine.
"""
from __future__ import annotations

import threading
import uuid

import psycopg2
import pytest

from tests.conftest import owner_dsn

PHASE = "SOURCE_DATA"


@pytest.fixture(autouse=True)
async def _dispose_engines():
    """Every engine, after every test.

    Without this the app engine's pool survives into the next test's event loop
    and closes against a dead one — which is the very failure this file exists
    to measure, arriving as a test defect instead of a finding.
    """
    yield
    from app.database.privacy_session import dispose_all_engines

    await dispose_all_engines()


def _owner():
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    return conn


def _pending_subject(cur) -> uuid.UUID:
    user = uuid.uuid4()
    cur.execute("INSERT INTO identity.user_account (id, email, status) "
                "VALUES (%s, %s, 'active')",
                (str(user), f"pool_{uuid.uuid4().hex[:12]}@example.com"))
    cur.execute("INSERT INTO profile.tax_profile (user_id, province_code, "
                "marital_status) VALUES (%s, 'ON', 'single')", (str(user),))
    cur.execute("""
        INSERT INTO finance.income_source
            (user_id, tax_year, income_type_id, amount, province_code)
        SELECT %s, 2025, id, 4200, 'ON' FROM ref.income_type WHERE code='employment'
    """, (str(user),))
    cur.execute("INSERT INTO identity.account_lifecycle (user_id, state) "
                "VALUES (%s, 'DELETION_REQUESTED')", (str(user),))
    return user


# ---------------------------------------------------------------------- §19 --
def test_many_workers_claiming_many_subjects_each_land_once():
    """Realistic sanity, not another exhaustive concurrency entry.

    Six subjects, four workers, all starting together on independent
    connections. `FOR UPDATE SKIP LOCKED` should hand every subject to exactly
    one worker and never the same subject to two.
    """
    admin = _owner()
    try:
        cur = admin.cursor()
        subjects = {str(_pending_subject(cur)) for _ in range(6)}

        start = threading.Barrier(4)
        claims: dict[str, list[str]] = {}
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker(name: str) -> None:
            conn = psycopg2.connect(owner_dsn())
            conn.autocommit = True
            try:
                c = conn.cursor()
                start.wait(timeout=20)
                c.execute("SELECT out_user_id FROM "
                          "identity.claim_account_lifecycle(50, %s)", (name,))
                mine = [str(r[0]) for r in c.fetchall()]
                with lock:
                    for subject in mine:
                        claims.setdefault(subject, []).append(name)
            except BaseException as exc:            # noqa: BLE001
                errors.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=45)

        assert not any(t.is_alive() for t in threads), (
            "a worker never finished — claims are serialising or deadlocked")
        assert not errors, f"a worker failed: {errors}"

        doubled = {s: w for s, w in claims.items() if len(w) > 1}
        assert not doubled, f"subjects claimed by more than one worker: {doubled}"

        ours = subjects & claims.keys()
        assert ours == subjects, (
            f"{len(subjects - claims.keys())} of this test's subjects were never "
            "claimed by anyone")

        # And each is genuinely owned by exactly the worker that reported it.
        for subject, workers in claims.items():
            if subject in subjects:
                cur.execute("SELECT claimed_by FROM identity.account_lifecycle "
                            " WHERE user_id = %s", (subject,))
                assert cur.fetchone()[0] == workers[0], (
                    f"{subject} is recorded against a different worker")
    finally:
        admin.close()


# ---------------------------------------------------------------------- §20 --
async def test_repeated_worker_activity_returns_every_connection_to_its_pool():
    """The metric, rather than the absence of a failure.

    Each runtime is exercised repeatedly and its pool is read directly:
    `checkedout()` must return to zero, and the pool must not grow across
    rounds. A leak shows here as a rising checked-out count long before it
    shows up as an unrelated test failing three files later.
    """
    from sqlalchemy import text

    from app.database.privacy_session import (
        dispose_all_engines,
        freshness_unit_of_work,
        get_worker_engine,
        privacy_unit_of_work,
    )
    from app.database.session import engine as app_engine
    from app.database.session import unit_of_work

    admin = _owner()
    try:
        cur = admin.cursor()
        subject = _pending_subject(cur)
    finally:
        admin.close()

    checked_out: list[tuple[int, int, int]] = []
    for _ in range(3):
        # application runtime
        async with unit_of_work(user_id=subject, actor_type="user") as s:
            await s.execute(text("SELECT 1"))
        # privacy runtime — the account-purge capability
        async with privacy_unit_of_work() as s:
            await s.execute(text("SELECT count(*) FROM "
                                 "identity.claim_account_lifecycle(1, 'pool')"))
        # freshness runtime — relay keyhole AND integrity scheduler keyhole
        async with freshness_unit_of_work() as s:
            await s.execute(text("SELECT count(*) FROM "
                                 "ioe.claim_freshness_events(1, 'pool')"))
            await s.execute(text("SELECT ioe.recover_stale_integrity_checks(1)"))

        checked_out.append((
            app_engine.pool.checkedout(),
            get_worker_engine("privacy").pool.checkedout(),
            get_worker_engine("freshness").pool.checkedout(),
        ))

    for round_index, counts in enumerate(checked_out):
        assert counts == (0, 0, 0), (
            f"after round {round_index + 1} connections were still checked out "
            f"(app, privacy, freshness) = {counts}; a unit of work is leaking")

    # `pool.size()` is the CONFIGURED capacity, not the live count — app 10,
    # workers 2 — so the meaningful assertion is that it is stable across
    # rounds and that the privileged runtimes stay deliberately small. A first
    # version compared it against a made-up threshold and failed on the
    # configured value, which measured nothing.
    sizes = (app_engine.pool.size(),
             get_worker_engine("privacy").pool.size(),
             get_worker_engine("freshness").pool.size())
    _, privacy_size, freshness_size = sizes
    assert privacy_size <= 4 and freshness_size <= 4, (
        f"a privileged worker pool is sized like a request tier: {sizes}. "
        "These are background queue drainers; holding privileged connections "
        "open for no reason is the thing the small pool avoids.")
    # `overflow()` counts from `-pool_size` upward, so an idle pool reports a
    # NEGATIVE number and "== 0" would be a bug in the test, not a finding.
    # Positive means connections were opened beyond the configured pool.
    for name in ("privacy", "freshness"):
        overflow = get_worker_engine(name).pool.overflow()
        assert overflow <= 0, (
            f"the {name} runtime opened {overflow} overflow connection(s) "
            "during steady sequential use; it should never exceed its pool")

    # dispose_all_engines must actually clear the worker registry, or the next
    # event loop inherits connections bound to this one.
    await dispose_all_engines()
    from app.database.privacy_session import _engines

    assert _engines == {}, (
        f"dispose_all_engines left worker engines registered: {list(_engines)}")


async def test_each_runtime_authenticates_as_its_own_principal():
    """The pools are only meaningful if they are actually different identities.
    Read `session_user` from each, which is the thing PostgreSQL enforces on."""
    from sqlalchemy import text

    from app.database.privacy_session import (
        dispose_all_engines,
        freshness_unit_of_work,
        privacy_unit_of_work,
    )
    from app.database.session import unit_of_work

    try:
        async with unit_of_work(actor_type="system") as s:
            app_user = await s.scalar(text("SELECT session_user"))
        async with privacy_unit_of_work() as s:
            privacy_user = await s.scalar(text("SELECT session_user"))
        async with freshness_unit_of_work() as s:
            freshness_user = await s.scalar(text("SELECT session_user"))
    finally:
        await dispose_all_engines()

    assert len({app_user, privacy_user, freshness_user}) == 3, (
        f"runtimes share a database principal: app={app_user}, "
        f"privacy={privacy_user}, freshness={freshness_user}. The whole "
        "separation is that the process which can purge an account is not the "
        "process serving HTTP.")


@pytest.mark.parametrize("runtime", ["privacy", "freshness"])
async def test_a_missing_privileged_dsn_still_fails_closed(runtime):
    """§21. Deployment now requires both privileged DSNs — the freshness one
    also carries the integrity scheduler since H2D. A fallback to the
    application DSN would reintroduce PD-16 by configuration."""
    from app.core.config import get_settings
    from app.database.privacy_session import (
        WorkerRuntimeUnavailable,
        get_worker_engine,
    )

    settings = get_settings()
    attribute = f"{runtime}_database_url"
    saved = getattr(settings, attribute)
    try:
        setattr(settings, attribute, None)
        with pytest.raises(WorkerRuntimeUnavailable) as unavailable:
            get_worker_engine(runtime)
        message = str(unavailable.value)
        assert "postgresql" not in message.lower(), "the DSN reached the message"
        assert "@" not in message, "the DSN reached the message"
    finally:
        setattr(settings, attribute, saved)
