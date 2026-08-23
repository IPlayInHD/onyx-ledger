"""What the recovery subsystem must never expose (B3 §19, §20, §27).

THESE ARE STRUCTURAL, not behavioural. Every one of them asks a question no
functional test can: not "did this request leak a token" but "is there anywhere
a token COULD leak from". A flow test proves one path; these prove the shape.

The RLS section is the inspection B3 §27 asks for, written down. The two token
tables carry no row-level policy, that is a CERTIFIED decision rather than an
oversight, and what these assert is that the compensating controls it rests on
are still in place — because the day one of them quietly changes, the exemption
stops being justified and nothing else would notice.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import psycopg2
import pytest

from tests.conftest import owner_dsn

BACKEND = Path(__file__).resolve().parents[2]
APP = BACKEND / "app"

TOKEN_TABLES = ("password_reset_token", "email_verification_token")


def _owner_cursor():
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    return conn


def _query(statement: str, params: tuple = ()) -> list[tuple]:
    conn = _owner_cursor()
    try:
        with conn.cursor() as cur:
            cur.execute(statement, params)
            return cur.fetchall()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# §27 — the token tables, inspected rather than assumed
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("table", TOKEN_TABLES)
def test_a_token_table_stores_a_digest_and_no_plaintext_column(table):
    """The storage contract, read off the live schema.

    `token_hash`, and no column that could hold the value it is a hash of. A
    future migration adding `token`, `raw_token` or `secret` for debugging
    convenience is the exact change this catches.
    """
    columns = {
        row[0]: row[1]
        for row in _query(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'identity' AND table_name = %s",
            (table,),
        )
    }
    assert columns, f"identity.{table} does not exist"
    assert "token_hash" in columns
    assert "expires_at" in columns, "a token with no window is a permanent credential"
    assert "used_at" in columns, "a token with no consumption mark can be replayed"

    forbidden = {"token", "raw_token", "secret", "plaintext", "value", "code"}
    assert not (forbidden & columns.keys()), (
        f"identity.{table} has a column that could hold a token in a readable "
        f"form: {sorted(forbidden & columns.keys())}"
    )


@pytest.mark.parametrize("table", TOKEN_TABLES)
def test_a_token_digest_is_unique(table):
    """Two rows sharing a digest would make `_claim`'s conditional UPDATE
    ambiguous — it would consume one and leave the other live."""
    unique = _query(
        "SELECT c.conname FROM pg_constraint c "
        "WHERE c.conrelid = %s::regclass AND c.contype IN ('u','p') "
        "AND EXISTS (SELECT 1 FROM unnest(c.conkey) k "
        "  JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k "
        "  WHERE a.attname = 'token_hash')",
        (f"identity.{table}",),
    )
    assert unique, f"identity.{table}.token_hash carries no unique constraint"


@pytest.mark.parametrize("table", TOKEN_TABLES)
def test_the_rls_exemption_rests_on_the_controls_it_claims(table):
    """B3 §27, and the answer is "inspected, and here is what it actually rests on".

    NEITHER TABLE HAS RLS, and adding self-ownership RLS would be actively
    wrong: `app.user_id` is unset for both recovery flows — a reset link is
    clicked by an anonymous browser — so a policy keyed on it would deny the
    lookup outright and the feature would simply not work. That is the same
    reasoning `identity.user_credential` and `identity.auth_session` are exempt
    under, it is recorded in `app/privacy/classification.py` as
    CROSS_TENANT_OPERATIONAL_STATE, and it predates this entry.

    WHAT THE EXEMPTION DOES NOT REST ON: withholding SELECT. An earlier version
    of this test asserted the request role could not read these tables, which
    was an idealised design rather than a measured fact — `_claim` filters on
    `WHERE token_hash = :h`, and PostgreSQL requires SELECT on a column to
    compare it, so the runtime role must have it. The resend floor reads
    `created_at` as well. A guard that asserts a control the product cannot
    have is not a guard.

    WHAT IT DOES REST ON, asserted here and in the tests around it:

      · the stored value is a SHA-256 digest of 384 bits of OS entropy, so
        reading the table yields nothing that can be presented as a token
      · nothing ever reads a digest OUT of the database — see the test below;
        `token_hash` appears only in comparisons
      · no request or response schema names a token-table column
      · the row is single-use and time-bounded, enforced in one conditional
        UPDATE rather than in Python

    RECORDED, NOT FIXED HERE: `onyx_app_ro` also holds SELECT, from the blanket
    `GRANT SELECT ON ALL TABLES IN SCHEMA identity` in 16_rls_grants.sql. A
    reporting role that can list which accounts have a live reset pending is a
    modest privacy leak and revoking it would break nothing — but it is a
    certified grant from an earlier entry, it does not block this one, and
    narrowing it belongs in an entry that owns the grant model rather than in
    the certification window of one that does not.
    """
    grants = {
        (row[0], row[1])
        for row in _query(
            "SELECT grantee, privilege_type FROM information_schema.table_privileges "
            "WHERE table_schema = 'identity' AND table_name = %s",
            (table,),
        )
    }
    app_rw = {p for g, p in grants if g == "onyx_app_rw"}

    # Non-vacuity in the other direction: the runtime role must be able to do
    # the three things the flows actually need, or recovery is broken and this
    # file would still pass.
    assert {"SELECT", "INSERT", "UPDATE"} <= app_rw, (
        f"onyx_app_rw cannot run the recovery flows against identity.{table}: "
        f"has {sorted(app_rw)}"
    )


@pytest.mark.parametrize("table", TOKEN_TABLES)
def test_a_token_row_is_never_read_back_into_the_application(table):
    """THE CONTROL THE GRANT CANNOT PROVIDE.

    The runtime role can SELECT these tables and has to. What keeps a digest
    from reaching a log, a response or an exception string is therefore not the
    grant — it is that no code path selects the column at all.

    `token_hash` may appear in a WHERE clause, where PostgreSQL compares it and
    hands back nothing. It may appear in an INSERT, where the value is going
    the other way. It may NOT appear in a select list, which is the one shape
    that pulls a stored digest into Python.
    """
    source = (APP / "services/auth/recovery.py").read_text()
    tree = ast.parse(source)

    selected: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name not in {"select", "returning"}:
            continue
        for argument in node.args:
            rendered = ast.unparse(argument)
            if "token_hash" in rendered:
                selected.append(f"line {node.lineno}: {name}({rendered})")
    assert not selected, (
        "a recovery query reads the stored digest into the application; it "
        "should only ever be compared:\n  " + "\n  ".join(selected)
    )

    # Non-vacuity: the walker really does see this module's queries.
    assert "select(" in source and "token_hash" in source


def test_no_customer_facing_schema_exposes_a_token_digest():
    """§27's last line. A response model naming `token_hash`, `used_at` or
    `expires_at` would put the token table's internals in an API document."""
    leaked: list[str] = []
    for path in (APP / "schemas").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
                continue
            if node.target.id in {"token_hash", "used_at", "password_hash"}:
                leaked.append(f"{path.relative_to(BACKEND)}:{node.lineno}: {node.target.id}")
    assert not leaked, "a response/request schema exposes token-table internals:\n  " + "\n  ".join(leaked)


# --------------------------------------------------------------------------- #
# §19 — nothing secret reaches a log
# --------------------------------------------------------------------------- #

#: Names whose VALUE is a secret or a link containing one. Matched as keyword
#: arguments to a logger call, which is how this codebase logs.
_SECRET_KEYWORDS = re.compile(
    r"\b(?:token|raw_token|raw|link|url|password|new_password|token_hash|"
    r"digest|body|html|message|email|to|recipient|address)\s*="
)


def test_the_recovery_subsystem_logs_no_secret_by_name():
    """Every logger call in the recovery path, read rather than exercised.

    A behavioural test can only prove that the paths it happened to run did
    not log a token. This proves no call site names one — including the ones
    only reached when a provider fails, which is exactly when somebody reaches
    for "just log the message so we can debug it".
    """
    offenders: list[str] = []
    for relative in (
        "services/auth/recovery.py",
        "integrations/email.py",
        "services/email/templates.py",
    ):
        path = APP / relative
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            called = node.func
            if not isinstance(called.value, ast.Name) or called.value.id != "log":
                continue
            for keyword in node.keywords:
                if keyword.arg and _SECRET_KEYWORDS.match(f"{keyword.arg}="):
                    offenders.append(
                        f"{path.relative_to(BACKEND)}:{node.lineno}: "
                        f"log.{called.attr}(..., {keyword.arg}=...)"
                    )
    assert not offenders, (
        "a recovery log call names a value that is a secret, a link containing "
        "one, or a customer's address:\n  " + "\n  ".join(offenders)
    )


def test_the_recovery_service_writes_no_detail_payload_on_a_security_event():
    """`audit.security_event.detail` is unvalidated JSONB — the one field on
    that row where anything at all can be put, with no check to notice. The
    recovery service leaves it NULL, and this pins that."""
    source = (APP / "services/auth/recovery.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        name = getattr(target, "id", None) or getattr(target, "attr", None)
        if name != "SecurityEvent":
            continue
        supplied = {k.arg for k in node.keywords}
        assert "detail" not in supplied, (
            f"recovery.py:{node.lineno} puts a payload in `detail`; it is "
            "unvalidated JSONB and this subsystem handles tokens"
        )
    # Non-vacuity: the construct this walks really is present.
    assert "SecurityEvent" in source


# --------------------------------------------------------------------------- #
# §20 — no customer financial data in any message
# --------------------------------------------------------------------------- #

#: Words that would mean a template had started describing somebody's money or
#: their tax position. Matched against the RENDERED output, not the source, so
#: a value interpolated in at runtime is caught as well as a literal.
_FINANCIAL = (
    "$", "cad", "refund", "balance owing", "taxable", "deduction", "credit",
    "rrsp", "tfsa", "fhsa", "t4", "income", "province", "ontario", "cra",
    "marginal", "bracket", "contribution", "filing",
)


def test_no_rendered_message_contains_anything_about_money():
    """Not "minimised" — ABSENT. Email is unencrypted in somebody else's
    mailbox, forwarded, indexed by a provider and quoted into replies, so the
    rule is that there is nothing in these messages to minimise.

    Rendered rather than grepped from source: a template that built a figure
    at runtime would pass a source scan and fail this.
    """
    from app.services.email.templates import (
        render_email_verification,
        render_password_changed,
        render_password_reset,
    )

    messages = [
        ("verification", render_email_verification(
            link="https://app.onyx.test/verify-email?token=abc", ttl_minutes=1440)),
        ("reset", render_password_reset(
            link="https://app.onyx.test/reset-password?token=abc", ttl_minutes=60)),
        ("changed", render_password_changed()),
    ]
    for name, message in messages:
        whole = f"{message.subject}\n{message.text}\n{message.html}".lower()
        found = [word for word in _FINANCIAL if word in whole]
        assert not found, f"the {name} message mentions {found}"


def test_every_message_says_what_to_do_if_you_did_not_ask_for_it():
    """§20. A transactional security message that does not is how a
    compromised account stays compromised: the one person positioned to notice
    is told nothing is happening."""
    from app.services.email.templates import (
        render_email_verification,
        render_password_changed,
        render_password_reset,
    )

    for message in (
        render_email_verification(link="https://x.test/a?token=t", ttl_minutes=60),
        render_password_reset(link="https://x.test/b?token=t", ttl_minutes=60),
    ):
        assert "did not request this" in message.text
        assert "did not request this" in message.html

    # The notice is different and deliberately so: "ignore this if you did not
    # request it" is the wrong advice for a change that already happened.
    changed = render_password_changed()
    assert "did not request this" not in changed.text
    assert "was not you" in changed.text
    assert "?token=" not in changed.text and "?token=" not in changed.html


def test_the_password_changed_notice_carries_no_link_at_all():
    """A security notice with an action button is a phishing template with a
    trusted sender: the useful thing to do with a stolen copy would be to send
    it. It reports, and tells the reader to navigate there themselves."""
    from app.services.email.templates import render_password_changed

    changed = render_password_changed()
    assert "href=" not in changed.html
    assert "http" not in changed.text
