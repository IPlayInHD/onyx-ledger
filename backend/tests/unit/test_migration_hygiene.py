"""Revision-chain hygiene, checked without a database.

Migration smoke (scripts/check_migrations.sh) proves the chain BUILDS. These
assertions are about the chain's shape, and they are the ones that catch a bad
merge: two heads, a duplicated revision id, a down_revision pointing at nothing,
or a revision file that no longer imports. Each of those makes `alembic upgrade
head` fail or — worse — silently apply only one branch.

No database is required, so this runs in the fast lane alongside lint and types.
"""
from __future__ import annotations

import importlib.util
from collections import Counter
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
VERSIONS = BACKEND / "migrations" / "versions"
SQL_DIR = BACKEND / "db" / "sql"


def _revision_modules() -> list[tuple[Path, object]]:
    modules = []
    for path in sorted(VERSIONS.glob("[0-9]*.py")):
        spec = importlib.util.spec_from_file_location(f"_migration_{path.stem}", path)
        assert spec and spec.loader, path
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)          # an unimportable revision fails here
        modules.append((path, module))
    return modules


def test_every_revision_imports_and_declares_its_identity() -> None:
    modules = _revision_modules()
    assert modules, "no migration revisions found"
    for path, module in modules:
        assert getattr(module, "revision", None), f"{path.name} declares no revision id"
        assert hasattr(module, "down_revision"), f"{path.name} declares no down_revision"
        assert callable(getattr(module, "upgrade", None)), f"{path.name} has no upgrade()"
        assert callable(getattr(module, "downgrade", None)), f"{path.name} has no downgrade()"


def test_revision_ids_are_unique() -> None:
    ids = [module.revision for _, module in _revision_modules()]
    duplicates = [rev for rev, n in Counter(ids).items() if n > 1]
    assert not duplicates, f"duplicate revision ids: {duplicates}"


def test_the_chain_is_linear_with_exactly_one_head() -> None:
    """One head, one base, and every link resolves.

    A second head is the signature of two branches merged without rebasing:
    `alembic upgrade head` then applies one of them and reports success.
    """
    modules = [module for _, module in _revision_modules()]
    ids = {module.revision for module in modules}
    downs = {module.down_revision for module in modules}

    dangling = sorted(d for d in downs if d is not None and d not in ids)
    assert not dangling, f"down_revision values with no matching revision: {dangling}"

    bases = [m.revision for m in modules if m.down_revision is None]
    assert len(bases) == 1, f"expected exactly one base revision, found {bases}"

    heads = sorted(ids - {d for d in downs if d is not None})
    assert len(heads) == 1, f"expected exactly one head, found {heads}"

    # Every revision is reachable from the base: no orphaned sub-chain.
    by_down: dict[str | None, list[str]] = {}
    for module in modules:
        by_down.setdefault(module.down_revision, []).append(module.revision)
    reachable, frontier = set(), [None]
    while frontier:
        current = frontier.pop()
        for nxt in by_down.get(current, []):
            if nxt not in reachable:
                reachable.add(nxt)
                frontier.append(nxt)
    assert reachable == ids, f"unreachable revisions: {sorted(ids - reachable)}"


def test_every_applied_sql_file_is_referenced_by_a_revision() -> None:
    """The SQL baseline is the source of truth; the chain is how it is applied.

    A SQL file nobody applies is a change that exists in the repository and not
    in any database — which is precisely the drift the schema gate then reports
    as a mystery.
    """
    referenced = set()
    for path in sorted(VERSIONS.glob("[0-9]*.py")):
        text = path.read_text()
        for sql in SQL_DIR.glob("*.sql"):
            if sql.name in text:
                referenced.add(sql.name)

    # Seed files (90+) are data for development/test fixtures, applied by
    # scripts/apply_schema.sh rather than by the revision chain.
    schema_files = {p.name for p in SQL_DIR.glob("*.sql") if not p.name.startswith("9")}
    unreferenced = sorted(schema_files - referenced)
    assert not unreferenced, f"SQL files no revision applies: {unreferenced}"


def test_no_revision_id_exceeds_the_alembic_version_column() -> None:
    """Entry 11B5 shipped `0052_lifecycle_claim_purge_states` — 33 characters
    against a 32-character column — and it survived review, a WIP commit and a
    focused test run.

    It survived because the failure mode is late and quiet. Alembic can create
    the revision, import it and RUN its body; the error only appears when it
    writes the identifier back:

        StringDataRightTruncation: value too long for type character varying(32)
        UPDATE alembic_version SET version_num='0052_lifecycle_claim_purge_states'

    So a database that already recorded an earlier head never exercised it, and
    only walking the chain from base did. A convention in a comment would not
    have caught it; this does.

    The limit is read from alembic's own `MigrationContext`, not hard-coded to
    32, so a future alembic that widens the column does not make this test lie
    in either direction.
    """
    from alembic.runtime.migration import MigrationContext

    limit = getattr(MigrationContext, "_version_num_length", None) or 32

    too_long = sorted(
        f"{revision} ({len(revision)} > {limit})"
        for _, module in _revision_modules()
        for revision in [module.revision]
        if len(revision) > limit
    )
    assert not too_long, "\n  ".join([
        "these revision identifiers cannot be stored in "
        f"alembic_version.version_num (varchar({limit})):", *too_long,
    ])
