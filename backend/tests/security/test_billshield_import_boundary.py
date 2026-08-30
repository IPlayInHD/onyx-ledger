"""BillShield Slice 1 — the cross-service firewall's static direction (§13.4).

PostgreSQL enforces that the BillShield WORKER cannot touch tax tables; the
reverse direction — tax code staying out of BillShield, and BillShield code
staying off the tax engines — has no grant behind it and is enforced HERE,
statically, by AST. That asymmetry is deliberate and documented in the plan;
this file is the enforcement, so it must outlive Slice 1 unchanged.

PATH-SCOPED TIERS, so later slices extend membership instead of weakening a
test:

  * Tier 1 (pure): billshield extraction/evaluation/domain modules and the
    thin CLI. Allowed imports: the standard library, other BillShield
    modules, and EXACTLY the canonicalization authority
    `app.services.ioe.domain.canonical` (§5.2's one deliberate exception).
    No database, no API, no workers, no tax services, no third-party module.
  * Tier 2 (platform integration — future service.py, API routes, the
    billshield worker task, the provider integration): additionally the §5.2
    platform seams. The tier exists NOW with zero members so Slice 2/3 add
    files, not rules.
  * Tax-owned packages may not import BillShield at all. Platform packages
    (core, api, database, domain, admission, auth, …) are deliberately NOT
    in the tax-owned roster — the API layer must be able to reach BillShield
    routes — and the roster is an explicit reviewed constant, not a guess.

Every direction is proven non-vacuous with planted violations on isolated
tmp_path trees, the same discipline as `test_celery_registration.py`.
"""
from __future__ import annotations

import ast
import sys
from collections.abc import Iterable
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
BILLSHIELD = BACKEND / "app" / "services" / "billshield"

#: The one blessed non-billshield application import for Tier 1 (§5.2).
CANONICAL_EXCEPTION = "app.services.ioe.domain.canonical"

#: Prefixes forbidden to EVERY billshield tier: the tax calculation and
#: knowledge surfaces, tax document processing, tax models, and legacy.
TAX_FORBIDDEN_PREFIXES = (
    "app.services.tax_engine",
    "app.services.ioe",            # except the canonical exception, tested exactly
    "app.services.document_processing",
    "app.services.analysis",
    "app.services.optimization",
    "app.services.state_graph",
    "app.services.tkms",
    "app.services.tax_kb",
    "app.services.ai",
    "app.services.data_ingestion",
    "app.database.models",
    "legacy",
)

#: §5.2's platform seams, available to Tier 2 only. Listed now so adding the
#: Slice 2/3 files is a membership change, not a rule change.
TIER2_ALLOWED_PREFIXES = (
    "app.core",
    "app.api.deps",
    "app.database",
    "app.domain.ports",
    "app.schemas",
    "app.services.admission",
    "app.services.billing",
    "app.services.billshield",
    "app.integrations",
    "workers.runtime",
    CANONICAL_EXCEPTION,
)

#: Future Tier 2 members, relative to the backend root. Empty membership
#: today — Slice 1 ships no integration module.
TIER2_PATHS = (
    "app/services/billshield/service.py",
    "app/api/v1/billshield",
    "workers/tasks/billshield.py",
    "app/integrations/bill_extraction.py",
)

#: The generic request-path unit of work. BillShield BACKGROUND code may not
#: reach it (§5.4.4); BillShield API routes may, because the API is
#: `onyx_app_rw` by design and a BillShield route is an ordinary request.
GENERIC_UNIT_OF_WORK = "app.database.session"

#: The Tier 2 members that are BACKGROUND processing — the split of §5.4.4 and
#: §5.4.11 row 15. Everything in TIER2_PATHS that is not listed here is request
#: handling and keeps the shared path.
#:
#: THE PROHIBITION IS NOT "IT WOULD RUN AS onyx_app_rw". Under the required
#: `db_secret = "billshield"` the container's generic engine authenticates as
#: the restricted credential too. The three accurate grounds are: it bypasses
#: the named, no-fallback `billshield_database_url` contract; it makes the
#: engine registry non-load-bearing, and with it fail-closed configuration and
#: single-path disposal; and it can activate a second connection pool, which is
#: exactly what the capacity ceiling of §5.4.10 turns on.
TIER2_BACKGROUND_PATHS = (
    "app/services/billshield/service.py",
    "workers/tasks/billshield.py",
    "app/integrations/bill_extraction.py",
)

#: Slice 3A's restricted units of work, which background code uses INSTEAD.
BILLSHIELD_UNITS_OF_WORK = (
    "app.database.privacy_session.billshield_claim_unit_of_work",
    "app.database.privacy_session.billshield_unit_of_work",
)

#: Packages that OWN tax semantics and must never import BillShield. An
#: explicit roster, reviewed like RESERVED_ROUTES: platform packages are
#: deliberately absent because the shared API process must reach BillShield.
TAX_OWNED_SERVICE_ROOTS = (
    "app/services/tax_engine",
    "app/services/ioe",
    "app/services/tax_kb",
    "app/services/tkms",
    "app/services/analysis",
    "app/services/optimization",
    "app/services/document_processing",
    "app/services/financial",
    "app/services/state_graph",
    "app/services/ai",
    "app/services/data_ingestion",
)


def _python_files(root: Path) -> list[Path]:
    return sorted(
        p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _import_targets(source_file: Path, package_parts: tuple[str, ...]) -> set[str]:
    """Absolute dotted targets a module declares.

    For `from m import a`, the target is `m.a` — that is what actually binds
    — and relative imports resolve against `package_parts` so an
    intra-package import is judged by what it reaches, not how it is spelt.
    """
    tree = ast.parse(source_file.read_text(), filename=str(source_file))
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                targets.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            else:
                anchor = package_parts[: len(package_parts) - (node.level - 1)]
                base = ".".join(
                    (*anchor, node.module) if node.module else anchor)
            for alias in node.names:
                targets.add(f"{base}.{alias.name}" if base else alias.name)
    return targets


def _package_parts(source_file: Path, root: Path) -> tuple[str, ...]:
    relative = source_file.relative_to(root).with_suffix("")
    parts = relative.parts
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    else:
        parts = parts[:-1]  # the package a module's relative imports anchor on
    return parts


def _matches(target: str, prefix: str) -> bool:
    return target == prefix or target.startswith(prefix + ".")


def _is_canonical_exception(target: str) -> bool:
    # `from app.services.ioe.domain import canonical` binds the canonical
    # module itself; anything else under ioe stays forbidden.
    return _matches(target, CANONICAL_EXCEPTION)


def tier1_violations(files: Iterable[Path], root: Path) -> list[str]:
    """Tier 1 allowlist: stdlib, billshield, the canonical exception."""
    violations: list[str] = []
    for source_file in files:
        parts = _package_parts(source_file, root)
        for target in sorted(_import_targets(source_file, parts)):
            top = target.split(".", 1)[0]
            if top in sys.stdlib_module_names:
                continue
            if _matches(target, "app.services.billshield"):
                continue
            if _is_canonical_exception(target):
                continue
            violations.append(f"{source_file.relative_to(root)}: {target}")
    return violations


def tier2_violations(files: Iterable[Path], root: Path) -> list[str]:
    """Tier 2: the §5.2 platform seams — and never the tax surfaces."""
    violations: list[str] = []
    for source_file in files:
        parts = _package_parts(source_file, root)
        for target in sorted(_import_targets(source_file, parts)):
            top = target.split(".", 1)[0]
            if top in sys.stdlib_module_names:
                continue
            if any(_matches(target, p) for p in TAX_FORBIDDEN_PREFIXES) and (
                    not _is_canonical_exception(target)):
                violations.append(f"{source_file.relative_to(root)}: {target}")
                continue
            if not any(_matches(target, p) for p in TIER2_ALLOWED_PREFIXES):
                violations.append(f"{source_file.relative_to(root)}: {target}")
    return violations


def background_session_violations(files: Iterable[Path], root: Path) -> list[str]:
    """BillShield BACKGROUND modules reaching the generic request session.

    Judged on the resolved dotted target, so `from app.database.session import
    unit_of_work`, `from app.database import session`, and
    `import app.database.session` are all caught — the binding is what matters,
    not how it is spelt.
    """
    violations: list[str] = []
    for source_file in files:
        parts = _package_parts(source_file, root)
        for target in sorted(_import_targets(source_file, parts)):
            if _matches(target, GENERIC_UNIT_OF_WORK):
                violations.append(f"{source_file.relative_to(root)}: {target}")
    return violations


def tax_owned_violations(files: Iterable[Path], root: Path) -> list[str]:
    """Tax-owned code may not import BillShield — the §16.1 direction that
    has no grant behind it."""
    violations: list[str] = []
    for source_file in files:
        parts = _package_parts(source_file, root)
        for target in sorted(_import_targets(source_file, parts)):
            if _matches(target, "app.services.billshield"):
                violations.append(f"{source_file.relative_to(root)}: {target}")
    return violations


def _tier1_files() -> list[Path]:
    files = [
        p for p in _python_files(BILLSHIELD)
        if str(p.relative_to(BACKEND)) not in TIER2_PATHS
    ]
    files.append(BACKEND / "scripts" / "billshield_eval.py")
    return files


def _tier2_files() -> list[Path]:
    files: list[Path] = []
    for entry in TIER2_PATHS:
        path = BACKEND / entry
        if path.is_dir():
            files.extend(_python_files(path))
        elif path.is_file():
            files.append(path)
    return files


def _tier2_background_files() -> list[Path]:
    files: list[Path] = []
    for entry in TIER2_BACKGROUND_PATHS:
        path = BACKEND / entry
        if path.is_dir():
            files.extend(_python_files(path))
        elif path.is_file():
            files.append(path)
    return files


def _tier2_request_files() -> list[Path]:
    background = set(TIER2_BACKGROUND_PATHS)
    files: list[Path] = []
    for entry in TIER2_PATHS:
        if entry in background:
            continue
        path = BACKEND / entry
        if path.is_dir():
            files.extend(_python_files(path))
        elif path.is_file():
            files.append(path)
    return files


def _tax_owned_files() -> list[Path]:
    files: list[Path] = []
    for entry in TAX_OWNED_SERVICE_ROOTS:
        root = BACKEND / entry
        assert root.is_dir(), (
            f"tax-owned roster names a package that no longer exists: {entry}"
        )
        files.extend(_python_files(root))
    # Worker task modules are tax/platform code too — all except the future
    # billshield task, which is Tier 2.
    files.extend(
        p for p in _python_files(BACKEND / "workers")
        if str(p.relative_to(BACKEND)) not in TIER2_PATHS)
    return files


# ---------------------------------------------------------------------------
# The real tree
# ---------------------------------------------------------------------------
def test_tier1_billshield_modules_import_only_their_allowlist():
    files = _tier1_files()
    assert files, "no Tier 1 files found; the guard is scanning nothing"
    violations = tier1_violations(files, BACKEND)
    assert not violations, (
        "pure BillShield modules may import only the stdlib, BillShield, and "
        "the canonicalization authority:\n  " + "\n  ".join(violations))


def test_tier2_membership_is_currently_empty_and_rules_are_ready():
    """Slice 1 ships no integration module. When Slice 2/3 add one, it joins
    TIER2_PATHS and is judged by tier2_violations — this test then checks it
    without any rule change."""
    files = _tier2_files()
    violations = tier2_violations(files, BACKEND)
    assert not violations, (
        "Tier 2 modules may use only the §5.2 platform seams:\n  "
        + "\n  ".join(violations))


def test_tax_owned_code_does_not_import_billshield():
    files = _tax_owned_files()
    assert files, "no tax-owned files found; the guard is scanning nothing"
    violations = tax_owned_violations(files, BACKEND)
    assert not violations, (
        "tax-owned code imported BillShield — §13.4's static direction:\n  "
        + "\n  ".join(violations))


# ---------------------------------------------------------------------------
# The background / request split (§5.4.4, §5.4.11 row 15)
# ---------------------------------------------------------------------------
def test_billshield_background_modules_do_not_reach_the_generic_session():
    """Membership is empty until Slice 3C adds the task — and that is the
    point: the RULE exists now, so the slice that adds a background module adds
    a file, not an exemption."""
    violations = background_session_violations(_tier2_background_files(), BACKEND)
    assert not violations, (
        "BillShield background processing must use "
        f"{' or '.join(BILLSHIELD_UNITS_OF_WORK)}, never the generic request "
        "session:\n  " + "\n  ".join(violations))


def test_the_background_roster_is_a_strict_subset_of_tier_two():
    """A background path that fell out of TIER2_PATHS would be scanned by
    neither guard."""
    assert set(TIER2_BACKGROUND_PATHS) < set(TIER2_PATHS)


def test_the_restricted_units_of_work_the_split_points_at_actually_exist():
    """Naming a replacement that does not exist would make the prohibition a
    dead end rather than a redirection."""
    import importlib
    for dotted in BILLSHIELD_UNITS_OF_WORK:
        module_name, _, attribute = dotted.rpartition(".")
        module = importlib.import_module(module_name)
        assert hasattr(module, attribute), dotted


# ---------------------------------------------------------------------------
# Non-vacuity: every direction has been SEEN to fail, on isolated trees
# ---------------------------------------------------------------------------
def _plant(tmp_path, relative: str, source: str) -> Path:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    return path


def test_tier1_guard_fails_on_database_and_tax_imports(tmp_path):
    planted = [
        _plant(tmp_path, "app/services/billshield/extraction/bad_db.py",
               "from app.database.session import unit_of_work\n"),
        _plant(tmp_path, "app/services/billshield/extraction/bad_ioe.py",
               "from app.services.ioe.scenario import service\n"),
        _plant(tmp_path, "app/services/billshield/extraction/bad_3p.py",
               "import celery\n"),
    ]
    violations = tier1_violations(planted, tmp_path)
    assert len(violations) == 3, violations
    assert any("app.database.session" in v for v in violations)
    assert any("app.services.ioe.scenario" in v for v in violations)
    assert any("celery" in v for v in violations)


def test_tier1_guard_accepts_exactly_the_canonical_import(tmp_path):
    planted = [
        _plant(tmp_path, "app/services/billshield/extraction/good.py",
               "from app.services.ioe.domain.canonical import domain_hash\n"
               "from app.services.ioe.domain import canonical\n"
               "import hashlib\n"
               "from app.services.billshield.extraction import codes\n"),
    ]
    assert tier1_violations(planted, tmp_path) == []


def test_tier2_guard_accepts_platform_seams_and_rejects_tax(tmp_path):
    good = _plant(tmp_path, "workers/tasks/billshield.py",
                  "from app.api.deps import authenticated_transaction\n"
                  "from workers.runtime import run_task\n"
                  "from app.services.billshield.extraction import contract\n")
    bad = _plant(tmp_path, "app/api/v1/billshield/routes.py",
                 "from app.services.tax_engine.service import TaxEngineService\n")
    assert tier2_violations([good], tmp_path) == []
    violations = tier2_violations([bad], tmp_path)
    assert len(violations) == 1 and "tax_engine" in violations[0]


def test_tax_owned_guard_fails_on_a_planted_billshield_import(tmp_path):
    planted = [
        _plant(tmp_path, "app/services/tax_engine/sneaky.py",
               "from app.services.billshield.extraction.contract import "
               "BillExtractionV1\n"),
        _plant(tmp_path, "app/services/ioe/clean.py",
               "from app.services.ioe.domain import canonical\n"),
    ]
    violations = tax_owned_violations(planted, tmp_path)
    assert len(violations) == 1, violations
    assert "billshield" in violations[0]


def test_relative_imports_resolve_before_judgement(tmp_path):
    """An intra-billshield relative import is judged by what it reaches."""
    planted = [
        _plant(tmp_path, "app/services/billshield/extraction/relative.py",
               "from . import codes\nfrom .contract import Present\n"),
    ]
    assert tier1_violations(planted, tmp_path) == []


def test_the_background_split_fails_on_a_planted_generic_session_import(tmp_path):
    """Non-vacuity for §5.4.11 row 15, in both directions on one tree.

    The forbidden background import must be caught however it is spelt, and the
    permitted API-route import must pass — otherwise the rule would be either
    decorative or a ban on the request path the API legitimately uses.
    """
    background = [
        _plant(tmp_path, "workers/tasks/billshield.py",
               "from app.database.session import unit_of_work\n"),
        _plant(tmp_path, "app/services/billshield/service.py",
               "from app.database import session\n"),
        _plant(tmp_path, "app/integrations/bill_extraction.py",
               "import app.database.session\n"),
    ]
    violations = background_session_violations(background, tmp_path)
    assert len(violations) == 3, violations
    assert all("app.database.session" in v for v in violations)


def test_the_background_split_permits_the_api_route_and_the_restricted_uow(
        tmp_path):
    # A BillShield API route is an ordinary request: the shared path is correct
    # for it, and banning it repository-wide would be wrong. The exemption is
    # structural — the route is a Tier 2 member that the BACKGROUND roster
    # deliberately omits — so that is what is asserted, rather than feeding the
    # route to a scanner whose whole job is to reject that import.
    assert "app/api/v1/billshield" in TIER2_PATHS
    assert "app/api/v1/billshield" not in TIER2_BACKGROUND_PATHS, (
        "the split must not reach request handling"
    )
    _plant(tmp_path, "app/api/v1/billshield/routes.py",
           "from app.database.session import unit_of_work\n")
    assert not any(
        "app/api/v1/billshield" in str(path)
        for path in _tier2_background_files()
    ), "an API route entered the background roster"

    background = [
        _plant(tmp_path, "workers/tasks/billshield_ok.py",
               "from app.database.privacy_session import "
               "billshield_unit_of_work, billshield_claim_unit_of_work\n"),
    ]
    assert background_session_violations(background, tmp_path) == [], (
        "the restricted units of work are what background code SHOULD reach"
    )
