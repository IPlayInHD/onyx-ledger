"""Schema-drift gate: the applied database must be the schema the code believes in.

WHY THIS IS NOT "alembic autogenerate is empty"
-----------------------------------------------
It never will be. `backend/db/sql` is the source of truth for indexes, foreign
keys, unique and CHECK constraints, partition children, RLS, triggers, and
domains — none of which the ORM models declare and several of which Alembic
cannot model at all. A raw comparison therefore reports hundreds of differences
that are correct by design, and a gate that tolerates them by COUNT ("298 is
fine") tolerates the 299th, which might be the one that matters.

So the comparison runs at its WIDEST setting — types, server defaults, indexes,
foreign keys, unique constraints, CHECK constraints, comments, nullability, all
schemas — and every difference it produces is matched against a policy keyed by
OBJECT IDENTITY:

    (kind, schema, table, object)

A difference with a matching policy entry is a governed divergence and is
reported as such. A difference without one is UNEXPECTED and fails the gate.
Adding a table, dropping a column, changing a type, or losing an index therefore
fails on the first run, because no policy entry names it.

The policy lives in `db/schema_drift_policy.json` and is regenerated with
`--write-policy` when a schema change legitimately introduces a new governed
divergence. Regenerating it is a reviewable diff, not a silent widening.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, os.getcwd())

from alembic.autogenerate import compare_metadata  # noqa: E402
from alembic.migration import MigrationContext  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402

import app.database.models  # noqa: E402,F401  (register every mapped table)
from app.core.config import get_settings  # noqa: E402
from app.database.base import Base  # noqa: E402

POLICY_PATH = Path(__file__).resolve().parents[1] / "db" / "schema_drift_policy.json"

# The widest comparison alembic offers. Deliberately wider than migrations/env.py,
# whose narrower lens exists so `alembic revision --autogenerate` produces usable
# forward migrations. This gate is not generating migrations; it is looking for
# anything that moved.
COMPARISON_OPTS: dict[str, Any] = {
    "include_schemas": True,
    "compare_type": True,
    "compare_server_default": True,
}

# Divergence classes, in the vocabulary of WHY the difference is legitimate.
# Every policy entry carries exactly one.
CLASSES = {
    "RAW_SQL_MANAGED_CHECK": (
        "CHECK constraint created by backend/db/sql. Enumerations and range "
        "guards live with the DDL that owns them; duplicating them in the ORM "
        "would let two sources try to create the same constraint."
    ),
    "RAW_SQL_MANAGED_INDEX": (
        "Index created by backend/db/sql, including partial, expression, and "
        "HNSW vector indexes the ORM cannot express."
    ),
    "RAW_SQL_MANAGED_FK": (
        "Foreign key created by backend/db/sql, typically with an ON DELETE "
        "action or a name the ORM does not reproduce."
    ),
    "RAW_SQL_MANAGED_UNIQUE": "Unique constraint created by backend/db/sql.",
    "DB_ONLY_PARTITION_CHILD": (
        "Declarative-partition child table. It has no model by design — the "
        "parent is mapped and the children are managed by the partition DDL."
    ),
    "TYPE_AFFINITY": (
        "PostgreSQL-specific type the ORM column declares generically: TEXT for "
        "an unbounded String, CITEXT for a case-insensitive one, or a money_amt "
        "domain for Numeric. The stored type is the specific one; the recorded "
        "type pair is part of this entry's identity, so a change to a DIFFERENT "
        "type is not covered by it."
    ),
    "SERVER_DEFAULT_OWNED_BY_SQL": (
        "Server default declared in backend/db/sql. The ORM sets its Python-side "
        "default instead, so the two are describing different layers."
    ),
    "NULLABILITY_METADATA": "Governed nullability divergence.",
    "COLUMN_COMMENT_METADATA": "Governed column-comment divergence.",
    "TABLE_COMMENT_METADATA": "Governed table-comment divergence.",
}

# Which class a raw alembic diff kind maps to, when it is governed at all.
KIND_TO_CLASS = {
    "remove_constraint": "RAW_SQL_MANAGED_CHECK",
    "add_constraint": "RAW_SQL_MANAGED_CHECK",
    "remove_index": "RAW_SQL_MANAGED_INDEX",
    "add_index": "RAW_SQL_MANAGED_INDEX",
    "remove_fk": "RAW_SQL_MANAGED_FK",
    "add_fk": "RAW_SQL_MANAGED_FK",
    "remove_table": "DB_ONLY_PARTITION_CHILD",
    "modify_type": "TYPE_AFFINITY",
    "modify_default": "SERVER_DEFAULT_OWNED_BY_SQL",
    "modify_nullable": "NULLABILITY_METADATA",
    "modify_comment": "COLUMN_COMMENT_METADATA",
    "add_table_comment": "TABLE_COMMENT_METADATA",
    "remove_table_comment": "TABLE_COMMENT_METADATA",
}

# Kinds that are NEVER governable. Structural loss or gain of a table or column
# is the thing this gate exists to catch, so these fail even if someone adds a
# policy entry for them.
NEVER_GOVERNED = frozenset({"add_table", "add_column", "remove_column"})


class Difference:
    """One alembic diff, reduced to a stable identity plus a description."""

    __slots__ = ("kind", "schema", "table", "obj", "detail")

    def __init__(self, kind: str, schema: str | None, table: str, obj: str, detail: str = ""):
        self.kind = kind
        self.schema = schema or "public"
        self.table = table
        self.obj = obj
        self.detail = detail

    @property
    def key(self) -> str:
        return f"{self.kind}|{self.schema}|{self.table}|{self.obj}"

    def __str__(self) -> str:
        suffix = f"  ({self.detail})" if self.detail else ""
        return f"{self.kind:22s} {self.schema}.{self.table}.{self.obj}{suffix}"


def _name(obj: Any, fallback: str = "?") -> str:
    return str(getattr(obj, "name", None) or fallback)


def _type_pair(diff: tuple) -> str:
    """`DB type -> model type`, recorded as part of a TYPE_AFFINITY identity.

    Without it a policy entry would bless any future type change on the column.
    With it, TEXT->String stays governed while TEXT->Integer becomes a new,
    unmatched difference.
    """
    return f"{diff[5].__class__.__name__}->{diff[6].__class__.__name__}"


def _describe(diff: tuple) -> Difference:
    kind = diff[0]

    if kind in ("add_table", "remove_table"):
        table = diff[1]
        return Difference(kind, table.schema, table.name, "<table>")

    if kind in ("add_table_comment", "remove_table_comment"):
        table = diff[1]
        return Difference(kind, table.schema, table.name, "<table_comment>")

    if kind in ("add_column", "remove_column"):
        schema, table, column = diff[1], diff[2], diff[3]
        return Difference(kind, schema, table, _name(column))

    if kind in ("add_constraint", "remove_constraint", "add_fk", "remove_fk",
                "add_index", "remove_index"):
        obj = diff[1]
        table = getattr(obj, "table", None)
        schema = getattr(table, "schema", None)
        table_name = _name(table, "?")
        return Difference(kind, schema, table_name, _name(obj, "<unnamed>"))

    if kind.startswith("modify_"):
        schema, table, column = diff[1], diff[2], diff[3]
        detail = ""
        if kind == "modify_type":
            detail = _type_pair(diff)
        elif kind == "modify_nullable":
            detail = f"db={diff[5]} model={diff[6]}"
        return Difference(kind, schema, table, column, detail)

    return Difference(kind, None, "?", repr(diff)[:80])


def _flatten(diffs: list) -> list[tuple]:
    out: list[tuple] = []
    for item in diffs:
        if isinstance(item, list):
            out.extend(_flatten(item))
        else:
            out.append(item)
    return out


def collect() -> list[Difference]:
    engine = create_engine(get_settings().database_url_sync)
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn, opts=COMPARISON_OPTS)
            raw = compare_metadata(ctx, Base.metadata)
    finally:
        engine.dispose()
    return [_describe(d) for d in _flatten(raw)]


def load_policy() -> dict[str, dict[str, str]]:
    if not POLICY_PATH.exists():
        return {}
    with POLICY_PATH.open() as fh:
        return {entry["key"]: entry for entry in json.load(fh)["governed"]}


def write_policy(differences: list[Difference]) -> None:
    governed = []
    ungovernable = []
    for d in sorted(differences, key=lambda x: x.key):
        if d.kind in NEVER_GOVERNED:
            ungovernable.append(d)
            continue
        klass = KIND_TO_CLASS.get(d.kind)
        if klass is None:
            ungovernable.append(d)
            continue
        governed.append({
            "key": d.key,
            "kind": d.kind,
            "schema": d.schema,
            "table": d.table,
            "object": d.obj,
            "detail": d.detail,
            "class": klass,
            "reason": CLASSES[klass],
        })

    if ungovernable:
        print("REFUSING to write a policy: these differences are never governable:")
        for d in ungovernable:
            print(f"  {d}")
        raise SystemExit(2)

    POLICY_PATH.write_text(json.dumps({
        "_comment": (
            "Identity-keyed schema-drift policy. Each entry names ONE object "
            "whose divergence between backend/db/sql and the SQLAlchemy metadata "
            "is intentional. Regenerate with "
            "`python scripts/check_schema_drift.py --write-policy` and review the "
            "diff: a new entry is a new intentional divergence, and removing one "
            "makes that object's drift fail the gate again."
        ),
        "comparison_options": COMPARISON_OPTS,
        "divergence_classes": CLASSES,
        "governed": governed,
    }, indent=2, sort_keys=False) + "\n")
    print(f"wrote {len(governed)} governed entries to {POLICY_PATH}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-policy", action="store_true",
                        help="regenerate the governed-divergence policy from the current schema")
    parser.add_argument("--verbose", action="store_true",
                        help="list every governed divergence, not only the summary")
    args = parser.parse_args()

    differences = collect()

    if args.write_policy:
        write_policy(differences)
        return 0

    policy = load_policy()
    if not policy:
        print(f"FAIL: no drift policy at {POLICY_PATH}. Generate it with --write-policy.")
        return 1

    governed: list[Difference] = []
    unexpected: list[Difference] = []
    for d in differences:
        if d.kind in NEVER_GOVERNED or d.key not in policy:
            unexpected.append(d)
        elif policy[d.key].get("detail", "") != d.detail:
            # THE DETAIL IS PART OF THE MATCH, not decoration.
            #
            # `TYPE_AFFINITY` has always claimed that "the recorded type pair is
            # part of this entry's identity, so a change to a DIFFERENT type is
            # not covered by it". It was not: `key` is kind|schema|table|object,
            # the type pair lived only in `detail`, and `detail` was never
            # compared. Proven by injection — a column governed as
            # `TIMESTAMP->DateTime` was silently re-typed to `String` and the
            # gate reported zero unexpected drift.
            #
            # Entry 11B5 found this because two finance `deleted_at` columns
            # were governed as `TIMESTAMP->Date`, which is not an affinity
            # difference at all: it silently truncated the time of day off every
            # deletion stamp this entry was about to start writing.
            unexpected.append(d)
        else:
            governed.append(d)

    by_class: collections.Counter = collections.Counter(
        policy[d.key]["class"] for d in governed
    )
    print("governed divergences (policy-matched, by class):")
    for klass, n in sorted(by_class.items()):
        print(f"  {n:5d}  {klass}")
    print(f"  {len(governed):5d}  TOTAL")

    if args.verbose:
        for d in sorted(governed, key=lambda x: x.key):
            print(f"    {d}")

    # A policy entry that no longer matches anything is not housekeeping — it is
    # the ONLY way this gate can see a raw-SQL object disappear. An index, CHECK,
    # or foreign key created by backend/db/sql exists in the database and not in
    # the ORM metadata, so its whole contribution to the comparison is the one
    # governed difference named by its policy entry. Drop the index and that
    # difference simply stops being produced; nothing becomes "unexpected".
    # Treating the vanished entry as a failure is what closes that hole.
    stale = sorted(set(policy) - {d.key for d in governed})
    vanished = [key for key in stale if policy[key]["class"] != "TYPE_AFFINITY"]

    print()
    if unexpected:
        print(f"unexpected schema drift: {len(unexpected)}")
        for d in sorted(unexpected, key=lambda x: x.key):
            print(f"  FAIL  {d}")
        print("\nEach line is a difference between the applied schema and the "
              "SQLAlchemy metadata that no policy entry accounts for. Either fix "
              "the schema/metadata, or — if the divergence is intentional — "
              "regenerate the policy and justify the new entry in review.")

    if vanished:
        print(f"\ngoverned objects missing from the database: {len(vanished)}")
        for key in vanished:
            entry = policy[key]
            print(f"  FAIL  vanished  {entry['kind']:22s} "
                  f"{entry['schema']}.{entry['table']}.{entry['object']}")
        print("\nEach line names an object backend/db/sql is supposed to have "
              "created that the applied database no longer has. If it was removed "
              "on purpose, remove its policy entry in the same change.")

    if unexpected or vanished:
        return 1

    print("unexpected schema drift: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
