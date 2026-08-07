"""The protected typing scope is a policy, and this is the test that enforces it.

Reaching zero mypy errors is worth nothing if the scope can quietly shrink
afterwards. Three things are asserted here:

  1. The scope is expressed as PACKAGE ROOTS, and every importable top-level
     package in the repository sits under one of them. A new package therefore
     joins the gate by existing, not by someone remembering to add it.
  2. No configuration turns checking off — `ignore_errors` is banned outright,
     and the only permitted relaxations are the two named in pyproject.
  3. The strictness flags the gate depends on are actually set. Dropping
     `check_untyped_defs`, for instance, would leave every unannotated function
     body unchecked while still reporting "no issues found".
"""
from __future__ import annotations

import tomllib
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
PYPROJECT = BACKEND / "pyproject.toml"

# Directories that are legitimately outside the shipped application: test code,
# migration scripts run by Alembic, and operator shell/python utilities.
NON_APPLICATION_ROOTS = {"tests", "migrations", "scripts", "alembic"}

# Flags the zero-error claim depends on. Each one, if removed, would make the
# gate pass while checking materially less.
REQUIRED_STRICTNESS = (
    "check_untyped_defs",
    "disallow_untyped_defs",
    "disallow_incomplete_defs",
    "disallow_untyped_calls",
    "warn_return_any",
    "warn_unused_ignores",
    "warn_redundant_casts",
    "warn_unreachable",
    "disallow_subclassing_any",
    "strict_equality",
    "extra_checks",
)


def _config() -> dict:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def test_the_protected_scope_is_declared_as_package_roots() -> None:
    gate = _config()["tool"]["onyx"]["quality_gate"]
    roots = gate["protected_scope"]

    assert roots, "the protected scope must not be empty"
    for root in roots:
        assert (BACKEND / root).is_dir(), f"declared protected root {root!r} does not exist"
        # A root, not a file: everything beneath it is covered automatically.
        assert "/" not in root and not root.endswith(".py")


def test_every_application_package_is_inside_the_protected_scope() -> None:
    """The scope cannot be kept artificially small.

    Any top-level directory that is an importable package and is not explicitly
    non-application code must be under a protected root. Adding a new service
    package outside `app`/`workers` fails here rather than silently escaping the
    type gate.
    """
    roots = set(_config()["tool"]["onyx"]["quality_gate"]["protected_scope"])

    packages = {
        path.name
        for path in BACKEND.iterdir()
        if path.is_dir()
        and (path / "__init__.py").exists()
        and path.name not in NON_APPLICATION_ROOTS
        and not path.name.startswith(".")
    }

    outside = sorted(packages - roots)
    assert not outside, (
        f"application packages outside the protected typing scope: {outside}. "
        "Add them to [tool.onyx.quality_gate].protected_scope and fix their errors."
    )


def test_the_calculation_critical_packages_are_covered() -> None:
    """Named explicitly so a future scope change cannot drop them by accident.

    These are the packages that decide numbers, eligibility, sealed identity, and
    tenant boundaries. Losing type coverage here is not a style regression.
    """
    roots = set(_config()["tool"]["onyx"]["quality_gate"]["protected_scope"])
    must_be_covered = (
        "app/services/tax_engine",        # the only tax-calculation authority
        "app/services/ioe/domain",        # canonicalization, hashing, portfolio, scoring
        "app/services/ioe/frozen",        # frozen snapshot reconstruction
        "app/services/ioe/replay",        # replay verification + integrity scheduling
        "app/services/ioe/scenario",      # scenario execution
        "app/services/ioe/portfolio",     # portfolio evaluation + eligibility recheck
        "app/services/ioe/snapshot",      # rule snapshot capture
        "app/database",                   # session, RLS context, models
        "app/schemas",                    # API DTOs / domain contracts
        "workers",                        # IOE workers
    )
    for package in must_be_covered:
        assert (BACKEND / package).is_dir(), f"{package} moved or was removed"
        assert package.split("/")[0] in roots, f"{package} is no longer type-gated"


def test_no_configuration_disables_type_checking() -> None:
    mypy = _config()["tool"]["mypy"]
    permitted = set(_config()["tool"]["onyx"]["quality_gate"]["permitted_mypy_overrides"])

    assert "ignore_errors" not in mypy
    # A blanket `ignore_missing_imports` turns every future untyped dependency
    # into Any across the whole codebase, which is exactly the kind of silent
    # widening this gate exists to prevent.
    assert mypy.get("ignore_missing_imports") is False

    for override in mypy.get("overrides", []):
        modules = override["module"]
        modules = [modules] if isinstance(modules, str) else modules
        assert "ignore_errors" not in override, f"ignore_errors set for {modules}"
        undeclared = sorted(set(modules) - permitted)
        assert not undeclared, (
            f"undeclared mypy override for {undeclared}; "
            "add it to permitted_mypy_overrides with a documented reason"
        )


def test_the_required_strictness_flags_are_enabled() -> None:
    mypy = _config()["tool"]["mypy"]
    missing = [flag for flag in REQUIRED_STRICTNESS if mypy.get(flag) is not True]
    assert not missing, f"protected typing strictness weakened: {missing}"
