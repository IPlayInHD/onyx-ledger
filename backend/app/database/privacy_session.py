"""The privileged privacy-worker database connection (Entry 11B5E5).

WHY A SECOND ENGINE. `app.database.session.engine` authenticates as the normal
application runtime, and PD-16 is the proof of what happens when a privileged
capability is reachable from that identity: an HTTP request-path session could
assume it. The worker able to purge an account must therefore not share the
connection that serves requests — the boundary has to exist at PostgreSQL's
`session_user`, not at a Python class name.

FAIL CLOSED. `privacy_database_url` has no default and never falls back to
`database_url`. A fallback would mean that on any host where the operator
forgot the setting, the purge would quietly run as `onyx_app_rw` and everything
would appear to work — which is precisely the configuration mistake this
separation exists to make impossible. Absent configuration refuses to run.
"""
from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings
from app.core.exceptions import DomainError

#: One engine per privileged runtime, keyed by name. A REGISTRY rather than a
#: global per runtime, because Entry 11B5E5 proved what a forgotten engine
#: costs: pooled connections outlive their event loop and an unrelated test
#: fails later in the suite. `dispose_worker_engines()` cannot miss one.
_engines: dict[str, AsyncEngine] = {}
_factories: dict[str, async_sessionmaker[AsyncSession]] = {}


class WorkerRuntimeUnavailable(DomainError):
    """The privileged connection is not configured.

    A closed code and no detail: the message travels into logs and task failure
    records, and a DSN carries a host, a database and a role name. Entry 11A's
    rule about exception text applies with extra force to a connection string.
    """

    status_code = 503
    error_type = "https://onyx.ledger/errors/privacy-runtime-unavailable"
    title = "Privacy Runtime Unavailable"

    def __init__(self, runtime: str = "privacy") -> None:
        # The runtime NAME, never the DSN. A connection string carries a host,
        # a database and a role name, and this reaches logs and failure records.
        self.runtime = runtime
        super().__init__(f"{runtime} worker database runtime is not configured")


def get_worker_engine(runtime: str) -> AsyncEngine:
    """Build (once) the engine a privileged runtime authenticates through."""
    settings = get_settings()
    dsn = {"privacy": settings.privacy_database_url,
           "freshness": settings.freshness_database_url}[runtime]
    if not dsn:
        raise WorkerRuntimeUnavailable(runtime)
    if runtime not in _engines:
        _engines[runtime] = create_async_engine(
            dsn,
            # A small pool on purpose. This is one background worker draining a
            # queue, not a request tier; sizing it like the API would hold
            # privileged connections open for no reason.
            pool_size=2,
            max_overflow=2,
            pool_pre_ping=True,
            future=True,
        )
        _factories[runtime] = async_sessionmaker(
            _engines[runtime], expire_on_commit=False, class_=AsyncSession)
    return _engines[runtime]


@contextlib.asynccontextmanager
async def privacy_unit_of_work() -> AsyncIterator[AsyncSession]:
    """A unit of work authenticated as the dedicated privacy runtime.

    Sets no `app.user_id`. The purge does not reach user rows through ordinary
    RLS-scoped CRUD — it goes through the governed keyholes, which set the
    tenant context themselves for exactly one subject. A worker that set the
    GUC here would be asserting an authorization it does not have.
    """
    get_worker_engine("privacy")
    session = _factories["privacy"]()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


@contextlib.asynccontextmanager
async def freshness_unit_of_work() -> AsyncIterator[AsyncSession]:
    """A unit of work authenticated as the dedicated freshness runtime.

    ONLY for the privileged keyholes — claim, complete, fail, fan-out. APPLYING
    an event to a tenant's rows deliberately keeps the ordinary application
    engine under that tenant's own `app.user_id`: that step is not privileged
    and must not become so just because its neighbours are. Converting the
    whole relay would be the same mistake PD-16 is, one layer up.
    """
    get_worker_engine("freshness")
    session = _factories["freshness"]()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def dispose_worker_engines() -> None:
    """Dispose every privileged runtime engine. One call, none forgotten."""
    for engine in list(_engines.values()):
        await engine.dispose()
    _engines.clear()
    _factories.clear()


async def dispose_all_engines() -> None:
    """Every engine this process may have opened — application AND workers.

    The pooled-connection-bound-to-a-dead-event-loop trap has now been hit
    three times, and each time the shape was identical: a code path quietly
    started using a SECOND engine, while the disposal site next to it still
    named only the first. Tests passed alone and failed together.

    A caller that has to remember to dispose two things will eventually
    remember one. This names the set instead, so the next runtime added to the
    registry is covered by every existing call site without anyone editing it.
    """
    from app.database.session import engine  # local: avoids a circular import

    await engine.dispose()
    await dispose_worker_engines()


async def dispose_privacy_engine() -> None:
    """Retained for call sites written before the registry existed."""
    await dispose_worker_engines()


#: Retained alias for the same reason.
PrivacyRuntimeUnavailable = WorkerRuntimeUnavailable


def get_privacy_engine() -> AsyncEngine:
    return get_worker_engine("privacy")
