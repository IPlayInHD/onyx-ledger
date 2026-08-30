"""The separately-authenticated worker database connections (Entry 11B5E5).

NOT ALL OF THEM ARE PRIVILEGED, and the file's original name predates that.
`privacy` and `freshness` hold capabilities the request path must not be able to
assume. `billshield` is the opposite case: it is a CONFINED identity that holds
less than the application does, because the component that parses untrusted bill
files should not be able to reach a tax record. Both directions need the same
mechanism — a separate `session_user` with its own no-fallback DSN — which is
why they share this registry.

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
import uuid
from collections.abc import AsyncIterator

from sqlalchemy import text
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


class BillShieldRuntimeUnavailable(WorkerRuntimeUnavailable):
    """The restricted BillShield connection is not configured.

    A SUBTYPE, not a rewrite of the base. The base class's `error_type` and
    `title` still say "privacy" because the privacy and freshness runtimes
    already emit them and a BillShield slice must not change what an existing
    client sees for two unrelated runtimes. What it must also not do is emit
    "Privacy Runtime Unavailable" for a bill-worker misconfiguration, which
    would point an incident at the account-deletion pipeline.

    The safety rule is inherited unchanged and is absolute: no DSN, host,
    database name, username, role name, port or driver text may appear in the
    type, the title, the detail or the log record. The runtime NAME is the whole
    of what may be disclosed, and it is what the operator needs.
    """

    error_type = "https://onyx.ledger/errors/billshield-runtime-unavailable"
    title = "BillShield Runtime Unavailable"

    #: THE RUNTIME NAME IS A CONSTANT OF THE CLASS, and `__init__` takes no
    #: argument at all. A parameter with a safe default would still be a
    #: parameter: any caller — most plausibly one catching this and re-raising
    #: "with a bit more context" — could put a DSN fragment, a host, or a
    #: provider's exception text into a message that travels into logs and task
    #: failure records. There is nothing to pass, so there is nothing to abuse.
    RUNTIME = "billshield"

    def __init__(self) -> None:
        super().__init__(self.RUNTIME)


#: Which closed error a runtime's missing configuration raises. A registry for
#: the same reason `_engines` is one: a runtime added without an entry here gets
#: the base class's behaviour, which is still fail-closed and still leaks
#: nothing — the mapping upgrades the operator signal, it does not gate safety.
#:
#: Entries are constructed with NO arguments, which is what keeps the closed
#: token closed at the one site that could reopen it.
_UNAVAILABLE: dict[str, type[WorkerRuntimeUnavailable]] = {
    "billshield": BillShieldRuntimeUnavailable,
}


def get_worker_engine(runtime: str) -> AsyncEngine:
    """Build (once) the engine a privileged runtime authenticates through."""
    settings = get_settings()
    # ADDING A RUNTIME MEANS ADDING A KEY HERE, not only a setting: an
    # unregistered name raises KeyError on this line, BEFORE the fail-closed
    # check below, which is an unhandled error instead of the governed code.
    dsn = {"privacy": settings.privacy_database_url,
           "freshness": settings.freshness_database_url,
           "billshield": settings.billshield_database_url}[runtime]
    if not dsn:
        # A runtime with its own closed error type constructs itself and takes
        # no argument, so the name cannot be re-supplied here. Everything else
        # falls back to the shared base, which still names its runtime and is
        # unchanged for the privacy and freshness callers that rely on it.
        closed = _UNAVAILABLE.get(runtime)
        raise closed() if closed is not None else WorkerRuntimeUnavailable(runtime)
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


@contextlib.asynccontextmanager
async def billshield_claim_unit_of_work() -> AsyncIterator[AsyncSession]:
    """ONLY for the privileged BillShield keyholes — claim, complete, fail.

    NO TENANT CONTEXT, by omission and by design. Claiming crosses tenants:
    the worker asks the queue for whatever is next, and it cannot know whose
    row that will be until it has one. A worker that asserted a tenant here
    would be claiming an authorization it does not have — the same argument
    `privacy_unit_of_work` makes for the purge.

    Shape copied from `privacy_unit_of_work` rather than invented: explicit
    commit on success, rollback on any exception, and `close()` in `finally` so
    the connection returns to the pool on every path.
    """
    get_worker_engine("billshield")
    # THE CALL ABOVE COMES FIRST, and it is not defensive style. The factory
    # registry is populated as a side effect of building the engine, so reading
    # it directly is a KeyError on the first use in a fresh process — precisely
    # the condition a worker boots in. It is also what keeps the engine inside
    # the registry that disposal iterates.
    session = _factories["billshield"]()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


@contextlib.asynccontextmanager
async def billshield_unit_of_work(user_id: uuid.UUID) -> AsyncIterator[AsyncSession]:
    """One tenant's BillShield work, as the restricted BillShield principal.

    `user_id` is REQUIRED, unlike `unit_of_work`, which accepts `None` because
    anonymous authentication genuinely needs it. BillShield has no such caller,
    and an optional parameter would make "no tenant context" reachable by
    forgetting rather than by deciding.

    Both GUCs go in ONE statement — `session.py` records that two statements
    meant two round trips on every transaction — and both are transaction-local,
    because connections are pooled and a context that outlived its transaction
    would be served to the next user of that connection.

    The actor is `'system'`. `app.actor_type` carries a closed database-enforced
    vocabulary of `user`/`admin`/`system`; a service-specific actor would be a
    governed migration widening that set, not a free choice made here.
    """
    get_worker_engine("billshield")            # registry first — see above
    async with _factories["billshield"]() as session:
        async with session.begin():            # commit on exit, rollback on raise
            await session.execute(
                text("SELECT set_config('app.actor_type', :atype, true),"
                     "       set_config('app.user_id', :uid, true)"),
                {"atype": "system", "uid": str(user_id)},
            )
            yield session


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
