"""BillShield Slice 3A — the restricted runtime's configuration and error boundary.

WHAT THIS FILE IS FOR. Slice 3A adds a fourth entry to the privileged-engine
registry. Everything that can be checked without a database is checked here:
the no-fallback DSN contract, the enablement flag that gates it, the
BillShield-specific problem document, the registry wiring, and the structural
shape of both units of work. The behavioural half — real GUCs on a real pooled
connection, and real refusals as a real login — lives in
`tests/security/billshield/test_billshield_runtime_identity.py`, because a
success claim has to be made by the principal that will make it in production
(plan §5.4.8, caller matrix).

THE CONTRACT BEING PROTECTED. `privacy_database_url` and
`freshness_database_url` each default to `None` and never fall back to
`database_url`, because PD-16 is what happens when a privileged capability is
reachable from the application identity. `billshield_database_url` inherits
that contract exactly. What it adds is `billshield_enabled`: BillShield ships
disabled (plan §21.1(14)), so an unconditional production validator would
refuse to start production for a feature nobody can reach. The validator is
therefore conditional, and the condition is the same flag that gates customer
reachability.
"""
from __future__ import annotations

import ast
import inspect
import uuid
from pathlib import Path

import pytest

from app.core.config import Settings
from app.database import privacy_session as ps

BACKEND = Path(__file__).resolve().parents[3]

#: A production Settings needs every one of these to get past the three
#: unrelated `_production_*` guards, so each case below varies only the
#: BillShield fields and nothing else.
PROD_BASE = dict(
    environment="production",
    jwt_secret="x" * 40,
    admission_identity_secret="y" * 40,
    storage_provider="s3",
    s3_region="ca-central-1",
    s3_bucket_documents="onyx-prod-documents",
    s3_bucket_legislation="onyx-prod-legislation",
    email_provider="ses",
    email_sender_address="noreply@example.test",
    ses_region="ca-central-1",
    app_public_url="https://app.example.test",
)

#: A syntactically real DSN, used only where a test needs the setting present.
#:
#: The database is deliberately NOT called `onyx`. The leak parametrization
#: below asserts that no component of this string reaches an error surface, and
#: every problem type in this repository is namespaced `https://onyx.ledger/...`
#: — so a database literally named `onyx` would make the assertion fail on the
#: vendor namespace rather than on a leak, and the obvious "fix" would be to
#: delete the database-name case. The name is distinct so the case stays real.
BILLSHIELD_DSN = "postgresql+asyncpg://onyx_billshield:s3cr3t@db.internal:5432/ledgerdb"


@pytest.fixture(autouse=True)
def _no_ambient_worker_settings(monkeypatch):
    """The harness exports a DSN for every worker runtime so the security suites
    can connect as the real logins. A settings test that read one would be
    asserting the shell it was launched from rather than the compiled default,
    and the fail-closed assertions here would silently stop testing anything.
    """
    for name in ("ONYX_BILLSHIELD_DATABASE_URL", "ONYX_BILLSHIELD_ENABLED",
                 "ONYX_PRIVACY_DATABASE_URL", "ONYX_FRESHNESS_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------- #
# A. Configuration defaults and the conditional production validator
# --------------------------------------------------------------------------- #
def test_billshield_ships_disabled_and_unconfigured():
    """Both defaults, together, are the disabled posture of §21.1(14)."""
    settings = Settings(environment="development")
    assert settings.billshield_enabled is False, (
        "BillShield must ship disabled; an enabled default would make every "
        "later gate optional"
    )
    assert settings.billshield_database_url is None, (
        "the restricted DSN must be absent by default, never inherited"
    )


def test_production_with_billshield_disabled_accepts_an_absent_dsn():
    """The condition is not politeness.

    No production validator covers `privacy_database_url` or
    `freshness_database_url` today, and BillShield is disabled by default. An
    unconditional validator would refuse to start production for a feature
    nobody can reach — a guard whose only effect is an outage.
    """
    settings = Settings(**PROD_BASE, billshield_enabled=False)
    assert settings.billshield_database_url is None


def test_production_with_billshield_enabled_refuses_an_absent_dsn():
    with pytest.raises(ValueError) as caught:
        Settings(**PROD_BASE, billshield_enabled=True)
    rendered = str(caught.value)
    assert "ONYX_BILLSHIELD_DATABASE_URL" in rendered, (
        "the failure must name the setting an operator has to set"
    )


def test_production_with_billshield_enabled_and_a_dsn_is_accepted():
    settings = Settings(
        **PROD_BASE, billshield_enabled=True,
        billshield_database_url=BILLSHIELD_DSN,
    )
    assert settings.billshield_database_url == BILLSHIELD_DSN


def test_the_generic_database_url_is_not_a_billshield_fallback(monkeypatch):
    """The whole point of the separation, asserted rather than assumed.

    A perfectly valid `database_url` must not satisfy the BillShield runtime.
    If it did, the boundary would collapse to the application identity on any
    host where the operator forgot the setting — invisibly, because the worker
    would run and the bills would process.
    """
    settings = Settings(
        environment="development",
        database_url="postgresql+asyncpg://onyx_app_rw@localhost:5432/onyx",
        billshield_database_url=None,
    )
    monkeypatch.setattr(ps, "get_settings", lambda: settings)
    with pytest.raises(ps.WorkerRuntimeUnavailable):
        ps.get_worker_engine("billshield")


# --------------------------------------------------------------------------- #
# A (cont.). The problem document, and what it may never carry
# --------------------------------------------------------------------------- #
def test_billshield_has_its_own_runtime_unavailable_subtype():
    """Decision 21.1(13): a subtype, not a rewrite of the shared base.

    A BillShield misconfiguration that surfaced as "Privacy Runtime
    Unavailable" would point an incident at the account-deletion pipeline when
    the fault is the bill worker.
    """
    error = ps.BillShieldRuntimeUnavailable()
    assert isinstance(error, ps.WorkerRuntimeUnavailable), (
        "the subtype must keep the shared fail-closed behaviour"
    )
    assert error.status_code == 503
    assert "privacy" not in error.error_type.lower()
    assert "privacy" not in error.title.lower()
    assert "billshield" in error.error_type.lower()
    assert error.runtime == "billshield"


def test_the_privacy_and_freshness_metadata_are_left_untouched():
    """The additive half of decision 21.1(13).

    Rewriting the shared base would change what an existing client sees for two
    runtimes that have nothing to do with BillShield.
    """
    assert ps.WorkerRuntimeUnavailable.error_type == (
        "https://onyx.ledger/errors/privacy-runtime-unavailable")
    assert ps.WorkerRuntimeUnavailable.title == "Privacy Runtime Unavailable"
    assert ps.PrivacyRuntimeUnavailable is ps.WorkerRuntimeUnavailable
    default_runtime = inspect.signature(
        ps.WorkerRuntimeUnavailable.__init__).parameters["runtime"].default
    assert default_runtime == "privacy", (
        "removing the default would change the base class's behaviour for its "
        "existing callers, which is exactly what the subtype avoids"
    )


@pytest.mark.parametrize("secret", [
    "onyx_billshield",          # the role name
    "s3cr3t",                   # the password
    "db.internal",              # the host
    "5432",                     # the port
    "ledgerdb",                 # the database
    "postgresql+asyncpg",       # the driver, and with it the DSN shape
])
def test_the_runtime_error_carries_no_credential_text(secret):
    """A connection string holds a host, a database, a role and a password, and
    this message travels into logs and task failure records."""
    error = ps.BillShieldRuntimeUnavailable()
    surfaces = " ".join([
        error.error_type, error.title, str(error), error.detail or "",
    ])
    assert secret not in surfaces, (
        f"{secret!r} reached an error surface; the runtime NAME is the whole "
        "of what may be disclosed"
    )


def test_the_closed_runtime_token_is_the_maximum_identification():
    """`billshield` is a short closed token an operator can act on. It is also
    the ceiling: nothing that helps locate the database may accompany it."""
    error = ps.BillShieldRuntimeUnavailable()
    rendered = str(error)
    assert "billshield" in rendered
    assert "@" not in rendered and "://" not in rendered, (
        "a DSN fragment reached the message"
    )


def test_the_billshield_subtype_accepts_no_caller_supplied_runtime_text():
    """THE TOKEN IS CLOSED, not merely defaulted.

    A subtype that still took a `runtime` argument would be a hole in the
    no-leak rule with a polite default in front of it: any caller — including
    one handling an exception and re-raising with context — could put a DSN
    fragment, a host, or a provider's error text into a message that travels
    into logs and task failure records. The identifier is a constant of the
    class, so there is no argument to abuse.
    """
    parameters = [
        name for name in inspect.signature(
            ps.BillShieldRuntimeUnavailable.__init__).parameters
        if name != "self"
    ]
    assert parameters == [], (
        f"the BillShield subtype still accepts {parameters}; a closed token "
        "must not be caller-supplied"
    )
    with pytest.raises(TypeError):
        ps.BillShieldRuntimeUnavailable("postgresql://onyx@db.internal/ledgerdb")


def test_the_engine_factory_raises_the_billshield_subtype_for_billshield(
        monkeypatch):
    """And the registry that chooses it must not pass the name through.

    The base class still takes a runtime name — privacy and freshness both rely
    on it — so the construction site is where the two contracts meet, and it is
    the site a leak would actually come from.
    """
    settings = Settings(environment="development", billshield_database_url=None)
    monkeypatch.setattr(ps, "get_settings", lambda: settings)
    with pytest.raises(ps.BillShieldRuntimeUnavailable) as caught:
        ps.get_worker_engine("billshield")
    assert caught.value.runtime == "billshield"
    assert "billshield" in str(caught.value)


@pytest.mark.parametrize("runtime", ["privacy", "freshness"])
def test_the_other_runtimes_still_raise_the_unchanged_base(runtime, monkeypatch):
    """Strictly additive, asserted rather than asserted-about: an operator
    watching the privacy pipeline must keep seeing exactly what they saw."""
    settings = Settings(environment="development")
    assert getattr(settings, f"{runtime}_database_url") is None, (
        "the ambient environment configured this runtime; the test would "
        "assert nothing"
    )
    monkeypatch.setattr(ps, "get_settings", lambda: settings)
    with pytest.raises(ps.WorkerRuntimeUnavailable) as caught:
        ps.get_worker_engine(runtime)
    assert not isinstance(caught.value, ps.BillShieldRuntimeUnavailable)
    assert caught.value.runtime == runtime
    assert caught.value.error_type == (
        "https://onyx.ledger/errors/privacy-runtime-unavailable")


# --------------------------------------------------------------------------- #
# B. The engine registry
# --------------------------------------------------------------------------- #
def _dsn_mapping_source() -> str:
    """The literal dict inside `get_worker_engine`, as source.

    Read structurally rather than executed: §4.5(4) is that an unregistered
    runtime name raises `KeyError` BEFORE the fail-closed check, so the mapping
    membership is the property, not the exception the call happens to raise.
    """
    return inspect.getsource(ps.get_worker_engine)


def test_billshield_is_registered_in_the_dsn_mapping():
    source = _dsn_mapping_source()
    assert '"billshield"' in source, (
        "§4.5(4): adding a runtime means adding to the mapping, not only "
        "adding a setting — otherwise the name raises KeyError before the "
        "governed closed code"
    )
    assert "billshield_database_url" in source, (
        "the BillShield entry must read its own setting"
    )


def test_an_unknown_runtime_name_still_fails_rather_than_falling_through():
    with pytest.raises((KeyError, ps.WorkerRuntimeUnavailable)):
        ps.get_worker_engine("not_a_runtime")


def test_the_restricted_pool_literals_are_unchanged():
    """Plan §5.4.3: `pool_size=2, max_overflow=2` are literals in the engine
    factory, not settings. The capacity ceiling of §5.4.10 counts the restricted
    pool at 4 in every profile precisely because they are not reachable from
    configuration or from Terraform.
    """
    source = inspect.getsource(ps.get_worker_engine)
    assert "pool_size=2" in source and "max_overflow=2" in source, (
        "converting these to settings would make the restricted pool's "
        "capacity profile-dependent and silently invalidate the ceiling"
    )


def test_the_billshield_engine_joins_the_shared_registry(monkeypatch):
    """Not a module-level engine of its own (§5.4.9).

    An engine outside `_engines` is disposed by nothing, and the pooled
    connection outlives its event loop — the defect this file's neighbour has
    recorded three occurrences of.
    """
    settings = Settings(environment="development",
                        billshield_database_url=BILLSHIELD_DSN)
    monkeypatch.setattr(ps, "get_settings", lambda: settings)
    built = []
    monkeypatch.setattr(ps, "create_async_engine",
                        lambda *a, **k: built.append((a, k)) or object())
    monkeypatch.setattr(ps, "async_sessionmaker", lambda *a, **k: object())
    monkeypatch.setattr(ps, "_engines", {})
    monkeypatch.setattr(ps, "_factories", {})

    ps.get_worker_engine("billshield")

    assert "billshield" in ps._engines
    assert "billshield" in ps._factories
    assert built and built[0][1]["pool_size"] == 2
    assert built[0][1]["max_overflow"] == 2
    assert built[0][0][0] == BILLSHIELD_DSN, (
        "the engine must be built from `billshield_database_url` and nothing else"
    )


def test_no_billshield_module_constructs_its_own_engine():
    """§5.4.9 / §16.1, as a source scan over the BillShield tree."""
    roots = [BACKEND / "app" / "services" / "billshield"]
    workers_tasks = BACKEND / "workers" / "tasks"
    if workers_tasks.exists():
        roots.extend(
            p for p in workers_tasks.glob("billshield*.py"))
    offenders = []
    for root in roots:
        files = [root] if root.is_file() else [
            p for p in root.rglob("*.py") if "__pycache__" not in p.parts]
        for path in files:
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                        and node.func.id == "create_async_engine":
                    offenders.append(f"{path}:{node.lineno}")
                if isinstance(node, ast.Attribute) and node.attr == "create_async_engine":
                    offenders.append(f"{path}:{node.lineno}")
    assert offenders == [], (
        f"BillShield modules must obtain their engine through "
        f"get_worker_engine; found {offenders}"
    )


def test_disposal_is_a_property_over_the_registry_not_a_runtime_list():
    """`runtime.py` records that the first guard for this defect WAS an
    enumeration, and that four task modules kept failing behind a green test.

    So the assertion is that disposal iterates whatever the registry holds —
    checked by planting an arbitrary runtime name and requiring it disposed.
    """
    source = inspect.getsource(ps.dispose_worker_engines)
    assert "_engines" in source
    assert "billshield" not in source, (
        "naming BillShield in the disposal path would reintroduce the "
        "enumeration this design replaced"
    )


@pytest.mark.asyncio
async def test_disposal_covers_a_planted_registry_entry(monkeypatch):
    disposed = []

    class _Engine:
        async def dispose(self):
            disposed.append(self)

    engines = {"privacy": _Engine(), "billshield": _Engine()}
    monkeypatch.setattr(ps, "_engines", engines)
    monkeypatch.setattr(ps, "_factories", dict.fromkeys(engines))
    await ps.dispose_worker_engines()
    assert len(disposed) == 2, "every registered engine must be disposed"
    assert ps._engines == {} and ps._factories == {}


# --------------------------------------------------------------------------- #
# C. The two units of work, structurally
# --------------------------------------------------------------------------- #
def test_both_units_of_work_exist_with_the_plan_s_names():
    """§5.4.4 names them. A rename would silently detach the plan from the code."""
    assert hasattr(ps, "billshield_claim_unit_of_work")
    assert hasattr(ps, "billshield_unit_of_work")


def test_the_tenant_unit_of_work_requires_a_user_id():
    """`unit_of_work` accepts `None` because anonymous authentication genuinely
    needs it. BillShield has no such caller, and an optional parameter would
    make the unprotected case reachable by forgetting rather than by deciding.
    """
    signature = inspect.signature(ps.billshield_unit_of_work)
    parameters = list(signature.parameters.values())
    assert [p.name for p in parameters] == ["user_id"]
    assert parameters[0].default is inspect.Parameter.empty, (
        "a default would make 'no tenant context' reachable by omission"
    )


def test_the_claim_unit_of_work_takes_no_tenant_argument():
    """Claiming crosses tenants by design; a worker that set the GUC there
    would be asserting an authorization it does not have."""
    assert list(inspect.signature(ps.billshield_claim_unit_of_work)
                .parameters) == []


def test_the_claim_unit_of_work_sets_no_tenant_context():
    source = inspect.getsource(ps.billshield_claim_unit_of_work)
    assert "app.user_id" not in source, (
        "the claim unit of work must set no tenant GUC, by omission and by "
        "fallback alike"
    )
    assert "set_config" not in source


def test_both_units_of_work_initialise_the_registry_before_reading_factories():
    """§5.4.4: `_factories` is populated as a side effect of building the
    engine, so reading it without the call is a `KeyError` on first use in a
    fresh process — precisely the condition a worker boots in.
    """
    for factory in (ps.billshield_claim_unit_of_work, ps.billshield_unit_of_work):
        source = inspect.getsource(factory)
        engine_at = source.index('get_worker_engine("billshield")')
        factories_at = source.index("_factories")
        assert engine_at < factories_at, (
            f"{factory.__name__} reads _factories before building the engine"
        )


def test_the_tenant_unit_of_work_sets_both_gucs_in_one_statement():
    """`session.py:39-45` records that two statements meant two round trips on
    every transaction. The tenant context is also `actor_type = 'system'`,
    because §4.5(1) makes the database's closed actor vocabulary the authority.
    """
    source = inspect.getsource(ps.billshield_unit_of_work)
    assert source.count("set_config") == 2, (
        "both GUCs go in ONE statement, so there are two set_config calls "
        "inside a single SELECT"
    )
    assert source.count("session.execute") == 1
    assert "'system'" in source or '"system"' in source, (
        "§4.5(1): the database enforces actor_type IN ('user','admin','system')"
    )
    assert "app.actor_type" in source and "app.user_id" in source


def test_the_tenant_unit_of_work_scopes_both_gucs_to_the_transaction():
    """`is_local => true`. Connections are pooled; a context that outlived its
    transaction would be served to the next user of that connection."""
    source = inspect.getsource(ps.billshield_unit_of_work)
    assert "true" in source.lower().split("set_config")[1], (
        "set_config must be transaction-local"
    )


def test_the_billshield_actor_type_does_not_widen_the_closed_vocabulary():
    """§4.5(1) and decision 21.1(4): `'system'`, not a BillShield-specific
    actor. Widening the closed set is a separate governed migration."""
    source = inspect.getsource(ps.billshield_unit_of_work)
    assert "billshield_worker" not in source
    assert "billshield" not in source.split("app.actor_type")[1][:80]


@pytest.mark.asyncio
async def test_the_claim_unit_of_work_commits_rolls_back_and_always_closes(
        monkeypatch):
    """The `privacy_unit_of_work` shape, not a new one: explicit commit on
    success, rollback on any exception, close in `finally` on every path."""
    events: list[str] = []

    class _Session:
        async def commit(self): events.append("commit")
        async def rollback(self): events.append("rollback")
        async def close(self): events.append("close")

    monkeypatch.setattr(ps, "get_worker_engine", lambda runtime: None)
    monkeypatch.setattr(ps, "_factories", {"billshield": _Session})

    async with ps.billshield_claim_unit_of_work():
        events.append("body")
    assert events == ["body", "commit", "close"]

    events.clear()
    with pytest.raises(RuntimeError):
        async with ps.billshield_claim_unit_of_work():
            raise RuntimeError("boom")
    assert events == ["rollback", "close"]


def test_the_tenant_unit_of_work_is_annotated_for_a_uuid():
    annotation = inspect.signature(
        ps.billshield_unit_of_work).parameters["user_id"].annotation
    assert annotation in (uuid.UUID, "uuid.UUID"), annotation
