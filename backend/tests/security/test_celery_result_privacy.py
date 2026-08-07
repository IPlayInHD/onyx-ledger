"""Nothing sensitive may survive in persistent Celery result metadata (§9–§12).

Redis is both the broker and the result backend. Payloads were audited in Entry
11A and are identifier-only — but the FAILURE path is a different surface, and
it was missed on the first pass: Celery serializes the exception itself into the
backend, and `exc_message` is `str(exception)` verbatim.

SQLAlchemy renders a `DBAPIError` as the statement plus its bound parameters, so
a database error during a financial write would put the amount, the source name
and the SQL into Redis with no tenant boundary. That is what these tests exist to
prevent.

No broker is required. The exception payload is produced through Celery's own
`prepare_exception`, which is the exact function the backend uses, so the test
measures the real serializer rather than a guess about it.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from celery.backends.base import BaseBackend

from workers.celery_app import celery_app

#: Synthetic values. Structurally realistic, deliberately not real: a
#: reserved-for-testing domain, an amount no fixture uses, a SIN-shaped number
#: that is not a valid assignment.
FAKE_EMAIL = "probe@example.invalid"
FAKE_AMOUNT = "874321.19"
FAKE_SIN = "046454286"
FAKE_EMPLOYER = "Acme Payroll Services"
FAKE_DOCUMENT = "T4 slip box 14 employment income"

#: Shaped exactly like SQLAlchemy's `DBAPIError.__str__`, which is how sensitive
#: values realistically reach an exception message in this system.
_REALISTIC_DB_ERROR = (
    "(asyncpg.exceptions.DataError) invalid input for query argument\n"
    "[SQL: INSERT INTO finance.income_source "
    "(user_id, amount, source_name, notes) VALUES ($1, $2, $3, $4)]\n"
    f"[parameters: ('019f-fake-uuid', '{FAKE_AMOUNT}', '{FAKE_EMPLOYER}', "
    f"'{FAKE_EMAIL} {FAKE_SIN} {FAKE_DOCUMENT}')]"
)

SENSITIVE = (FAKE_EMAIL, FAKE_AMOUNT, FAKE_SIN, FAKE_EMPLOYER, FAKE_DOCUMENT)


class _RealisticDbError(Exception):
    """Stands in for a SQLAlchemy DBAPIError without needing a live failure."""


def test_an_exception_message_would_be_serialized_verbatim():
    """The premise. If this ever stops being true the fix below is unnecessary —
    but it should stop being true deliberately, not silently.

    This asserts the RISK exists, which is what justifies disabling result
    storage. Without it, the configuration assertions below would look like
    arbitrary settings nobody could explain.
    """
    payload = BaseBackend(app=celery_app).prepare_exception(
        _RealisticDbError(_REALISTIC_DB_ERROR)
    )
    blob = repr(payload)

    assert "exc_message" in payload, (
        "Celery no longer serializes an exception message; re-check whether "
        "result storage still needs to be disabled"
    )
    leaked = [value for value in SENSITIVE if value in blob]
    assert leaked, (
        "the serializer no longer carries the message — the premise for "
        "disabling result storage has changed and should be re-examined"
    )


def test_results_are_not_stored_at_all():
    """THE fix. Nothing in this repository reads a task result, so the entire
    stored-result surface is cost without benefit — and disabling it removes the
    exception payload rather than trying to sanitize every exception that could
    reach the boundary."""
    assert celery_app.conf.task_ignore_result is True, (
        "task results are being stored in Redis; a failing task will write its "
        "exception message — statement and bound parameters included — into the "
        "result backend"
    )


def test_errors_are_not_stored_even_though_results_are_ignored():
    """The single setting that would put exception payloads back into Redis.

    `task_store_errors_even_if_ignored` overrides `task_ignore_result` for the
    failure path specifically, which is exactly the path that carries the
    sensitive message. Asserted explicitly rather than trusted to the default.
    """
    assert celery_app.conf.task_store_errors_even_if_ignored is False


def test_the_result_ttl_is_an_explicit_decision():
    """It was previously the framework default — a reasonable bound arrived at
    by accident. Any task that opts back into results is capped by this."""
    expires = celery_app.conf.result_expires
    assert expires is not None and expires is not False, (
        "result_expires disabled: a stored result would live until Redis evicts "
        "it, which is a deployment property rather than a decision"
    )
    if isinstance(expires, timedelta):
        assert expires <= timedelta(days=7), f"result retention is {expires}"
    else:
        assert int(expires) <= 7 * 24 * 3600


def test_task_arguments_are_not_stored_with_results():
    """`result_extended` would persist each task's args and kwargs alongside its
    result. Two tasks take a `user_id`, so switching it on would write tenant
    identifiers into Redis for every call, not only for failures."""
    assert not celery_app.conf.get("result_extended", False)


@pytest.mark.parametrize("value", SENSITIVE)
def test_no_task_signature_could_carry_this_kind_of_value(value):
    """Belt to the braces of the payload matrix: every task argument in the
    repository is an identifier, an integer or a closed code, so no sensitive
    value has a path into the broker in the first place.

    Asserted against the source rather than a written list, so a task added with
    a `notes=` or `amount=` parameter fails here.
    """
    import re
    from pathlib import Path

    workers = Path(__file__).resolve().parents[2] / "workers" / "tasks"
    forbidden = re.compile(
        r"def \w+\([^)]*\b("
        r"amount|income|expense|salary|notes?|description|text|content|"
        r"payload|snapshot|email|filename|employer"
        r")\b\s*[:=]",
        re.S,
    )
    offenders = [
        path.name for path in workers.glob("*.py") if forbidden.search(path.read_text())
    ]
    assert not offenders, (
        f"a task signature accepts a sensitive-looking argument: {offenders}; "
        "task payloads must be identifiers, counts and closed codes"
    )
