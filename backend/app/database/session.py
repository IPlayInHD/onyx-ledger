"""Async engine, session factory, and the Unit of Work.

The UoW opens a transaction and — critically — sets the per-request RLS GUCs
(`app.user_id`, `app.actor_type`) so PostgreSQL Row-Level Security and the audit
triggers see the acting principal. Runtime connects as `onyx_app_rw`.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

_settings = get_settings()

engine = create_async_engine(
    _settings.database_url,
    pool_size=_settings.db_pool_size,
    max_overflow=_settings.db_max_overflow,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def unit_of_work(
    user_id: uuid.UUID | None = None,
    actor_type: str = "system",
) -> AsyncIterator[AsyncSession]:
    """Transactional scope with RLS context set for the acting principal."""
    async with SessionLocal() as session:
        async with session.begin():
            # set_config(..., is_local=true) scopes the GUC to this transaction.
            #
            # Both GUCs go in ONE statement when there is a user: two statements
            # meant two round trips on every transaction, and a request that
            # opens three transactions paid six. `app.user_id` is still left
            # UNSET when there is no user — deny-by-default depends on its
            # absence, so it must never be set to an empty string instead.
            if user_id is not None:
                await session.execute(
                    text(
                        "SELECT set_config('app.actor_type', :atype, true),"
                        "       set_config('app.user_id', :uid, true)"
                    ),
                    {"atype": actor_type, "uid": str(user_id)},
                )
            else:
                await session.execute(
                    text("SELECT set_config('app.actor_type', :atype, true)"),
                    {"atype": actor_type},
                )
            yield session
