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

_engine: AsyncEngine | None = None
_factory: async_sessionmaker[AsyncSession] | None = None


class PrivacyRuntimeUnavailable(DomainError):
    """The privileged connection is not configured.

    A closed code and no detail: the message travels into logs and task failure
    records, and a DSN carries a host, a database and a role name. Entry 11A's
    rule about exception text applies with extra force to a connection string.
    """

    status_code = 503
    error_type = "https://onyx.ledger/errors/privacy-runtime-unavailable"
    title = "Privacy Runtime Unavailable"

    def __init__(self) -> None:
        super().__init__("privacy worker database runtime is not configured")


def get_privacy_engine() -> AsyncEngine:
    """Build (once) the engine the privileged worker authenticates through."""
    global _engine, _factory
    settings = get_settings()
    if not settings.privacy_database_url:
        raise PrivacyRuntimeUnavailable()
    if _engine is None:
        _engine = create_async_engine(
            settings.privacy_database_url,
            # A small pool on purpose. This is one background worker draining a
            # queue, not a request tier; sizing it like the API would hold
            # privileged connections open for no reason.
            pool_size=2,
            max_overflow=2,
            pool_pre_ping=True,
            future=True,
        )
        _factory = async_sessionmaker(
            _engine, expire_on_commit=False, class_=AsyncSession)
    return _engine


@contextlib.asynccontextmanager
async def privacy_unit_of_work() -> AsyncIterator[AsyncSession]:
    """A unit of work authenticated as the dedicated privacy runtime.

    Sets no `app.user_id`. The purge does not reach user rows through ordinary
    RLS-scoped CRUD — it goes through the governed keyholes, which set the
    tenant context themselves for exactly one subject. A worker that set the
    GUC here would be asserting an authorization it does not have.
    """
    get_privacy_engine()
    assert _factory is not None
    session = _factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def dispose_privacy_engine() -> None:
    global _engine, _factory
    if _engine is not None:
        await _engine.dispose()
        _engine, _factory = None, None
