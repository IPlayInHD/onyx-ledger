"""Engine disposal between tests in this directory.

`asyncio_mode = "auto"` gives every test its own event loop, while the async
engines are module-level singletons whose pooled connections stay bound to the
loop that opened them. The second test in a module to touch one therefore dies
in teardown with "attached to a different loop" or "Event loop is closed" —
which reads like a database result and is not one.

`tests/conftest.py` already learned this the hard way and disposes every engine
after each `client` fixture. The tests here do not use an HTTP client (they
drive services and psycopg2 directly), so they need the same disposal on their
own.
"""

from __future__ import annotations

import pytest_asyncio


@pytest_asyncio.fixture(autouse=True)
async def _dispose_engines_between_tests():
    yield
    from app.database.privacy_session import dispose_all_engines

    await dispose_all_engines()
