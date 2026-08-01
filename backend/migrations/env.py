"""Alembic environment — runs migrations with a sync (psycopg2) engine.

Schema DDL is applied by the revision chain via app.database.sql_migrations.
target_metadata is wired to the SQLAlchemy models so future, incremental changes
can use `alembic revision --autogenerate` (keep partitioning/RLS/triggers/HNSW
as hand-written ops — autogenerate does not model them).
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# make the `app` package importable when alembic runs from backend/
sys.path.insert(0, os.getcwd())

import app.database.models  # noqa: E402,F401  (register all mapped tables)
from app.core.config import get_settings  # noqa: E402
from app.database.base import Base  # noqa: E402

config = context.config
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url_sync)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to):
    """Scope autogenerate to table/column changes on MAPPED tables only.

    The raw-SQL baseline owns partitioning, indexes, foreign keys, unique/CHECK
    constraints, RLS, and triggers (autogenerate can't model them). So:
      - skip DB-only tables (partition children have no model) → no bogus drops
      - skip index/FK/unique-constraint diffs → managed in backend/db/sql
    Future forward migrations then cleanly autogenerate add_column / add_table,
    and anything special is hand-written as a new SQL file + revision.
    """
    if type_ == "table" and reflected and compare_to is None:
        return False
    if type_ in ("foreign_key_constraint", "index", "unique_constraint"):
        return False
    return True


_AUTOGEN_OPTS = dict(
    include_schemas=True,
    include_object=include_object,
    compare_type=False,
    compare_server_default=False,
)


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url_sync,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **_AUTOGEN_OPTS,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            **_AUTOGEN_OPTS,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
