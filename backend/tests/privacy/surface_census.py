"""A census of the privacy deletion universe: what actually happens to each table.

Entry 11B6G. Sixty of the seventy certified surfaces were `UNCLASSIFIED_BLOCKING`
and classifying them one prompt at a time was never going to finish. This
measures all seventy at once, at every stage of the deletion lifecycle, so that
classification arguments are made against observed behaviour rather than against
table names.

IT MEASURES, IT DOES NOT DECIDE. A row disappearing does not mean deletion is
correct, and a row surviving does not mean retention is required — Entry 11B6G
§19 and §18 both say so, and both directions have already produced real defects
in this repository. The census produces an observation column; the semantic
classification is argued separately, from writers, readers and replay.

OWNERSHIP IS DERIVED FROM THE CATALOG, not from a hand-written list of column
names. A table with `user_id` is scoped directly; everything else is reached by
walking foreign keys back to something that has one. A hand-written list would
be exactly the kind of artifact that goes stale silently and then reports zero
rows for a table that is full.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: How far to walk foreign keys looking for an ownership column before giving
#: up. The certified universe is at most three edges deep; the extra room is
#: for a future table, not for hope.
MAX_OWNERSHIP_DEPTH = 5


@dataclass(frozen=True)
class Ownership:
    """How rows of one table are attributed to an account."""

    table: str
    #: "DIRECT_USER_ID", "PARENT_OWNED", or "UNREACHABLE".
    kind: str
    #: A SQL predicate with a single `%(uid)s` parameter, or None.
    predicate: str | None = None
    #: The foreign-key path walked to reach an owner, for the report.
    path: tuple[str, ...] = field(default_factory=tuple)


def _catalog(cur) -> tuple[set[str], dict[str, list[tuple[str, str, str]]]]:
    """(tables with a user_id column, {child: [(fk_column, parent, parent_pk)]})."""
    cur.execute("""
        SELECT c.relnamespace::regnamespace::text || '.' || c.relname
          FROM pg_class c
          JOIN pg_attribute a ON a.attrelid = c.oid
         WHERE a.attname = 'user_id' AND a.attnum > 0 AND NOT a.attisdropped
           AND c.relkind IN ('r', 'p')
    """)
    owned = {row[0] for row in cur.fetchall()}

    cur.execute("""
        SELECT cn.nspname || '.' || cc.relname,
               a.attname,
               pn.nspname || '.' || pc.relname,
               af.attname
          FROM pg_constraint con
          JOIN pg_class cc ON cc.oid = con.conrelid
          JOIN pg_namespace cn ON cn.oid = cc.relnamespace
          JOIN pg_class pc ON pc.oid = con.confrelid
          JOIN pg_namespace pn ON pn.oid = pc.relnamespace
          JOIN pg_attribute a ON a.attrelid = cc.oid AND a.attnum = con.conkey[1]
          JOIN pg_attribute af ON af.attrelid = pc.oid AND af.attnum = con.confkey[1]
         WHERE con.contype = 'f'
    """)
    parents: dict[str, list[tuple[str, str, str]]] = {}
    for child, column, parent, parent_key in cur.fetchall():
        if child != parent:            # self-references lead nowhere useful here
            parents.setdefault(child, []).append((column, parent, parent_key))
    return owned, parents


def ownership_map(cur, tables: list[str]) -> dict[str, Ownership]:
    """Work out, per table, how to count one account's rows."""
    owned, parents = _catalog(cur)

    def resolve(table: str, depth: int, seen: frozenset[str]) -> Ownership:
        # The alias is depth-numbered. A shared alias looked fine until a
        # two-level chain nested `p` inside `p` and PostgreSQL resolved the
        # inner reference against the wrong relation — quietly, on a query that
        # still parsed.
        alias = f"o{depth}"
        if table in owned:
            return Ownership(
                table, "DIRECT_USER_ID", f"{alias}.user_id = %(uid)s", (table,)
            )
        if depth >= MAX_OWNERSHIP_DEPTH:
            return Ownership(table, "UNREACHABLE")
        for column, parent, parent_key in parents.get(table, []):
            if parent in seen:
                continue
            upstream = resolve(parent, depth + 1, seen | {parent})
            if upstream.kind == "UNREACHABLE" or upstream.predicate is None:
                continue
            inner_alias = f"o{depth + 1}"
            return Ownership(
                table, "PARENT_OWNED",
                f"EXISTS (SELECT 1 FROM {parent} {inner_alias}"
                f" WHERE {inner_alias}.{parent_key} = {alias}.{column}"
                f" AND {upstream.predicate})",
                (table, *upstream.path),
            )
        return Ownership(table, "UNREACHABLE")

    return {t: resolve(t, 0, frozenset({t})) for t in tables}


def census(cur, ownership: dict[str, Ownership], user_id) -> dict[str, int]:
    """Row count per table for one account. `-1` means ownership is unreachable.

    One statement per table rather than one per row: the census runs at five
    lifecycle stages and is analysis infrastructure, not something to make
    expensive.
    """
    counts: dict[str, int] = {}
    for table, own in ownership.items():
        if own.predicate is None:
            counts[table] = -1
            continue
        cur.execute(
            f"SELECT count(*) FROM {table} o0 WHERE {own.predicate}",
            {"uid": str(user_id)},
        )
        counts[table] = cur.fetchone()[0]
    return counts


#: The observed fate of a table across the lifecycle. An observation, not a
#: verdict — see the module docstring.
FATES = (
    "NOT_PRESENT",              # never had a row for this account
    "SURVIVES_UNCHANGED",       # same count before and after everything
    "DELETED_BY_PHASE",         # a pre-terminal phase emptied it
    "DELETED_BY_ACCOUNT_CASCADE",   # only the account row's removal emptied it
    "PARTIALLY_DELETED",        # count fell but did not reach zero
)


def fate(before: int, after_phases: int, after_removal: int) -> str:
    if before <= 0:
        return "NOT_PRESENT"
    if after_phases == 0:
        return "DELETED_BY_PHASE"
    if after_removal == 0:
        return "DELETED_BY_ACCOUNT_CASCADE"
    if after_removal < before:
        return "PARTIALLY_DELETED"
    return "SURVIVES_UNCHANGED"
