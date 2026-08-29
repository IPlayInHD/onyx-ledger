"""Two BillShield tenants, seeded as the owner, for the Slice 2 security suite.

The suite acts as a ROLE and attempts real traffic, the way
`test_pd1_tenant_isolation.py` does. A test that went through the API would
prove the service remembered to scope its query — which is exactly the property
the database boundary exists so we can stop relying on.

Seeding runs as the owner because the owner is a superuser in the test
harness and can therefore write both tenants' rows without the policies under
test getting in the way. Everything the tests then assert is done as
`onyx_app_rw` or as `onyx_billshield_worker`, neither of which is a superuser.
"""
from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass, field

import psycopg2
import pytest

from tests.conftest import owner_dsn

#: The five tenant-derived tables. Discovered dynamically by the RLS tests from
#: the live catalogue; named here only where a test needs a fixed order.
TENANT_TABLES = (
    "billshield.bill",
    "billshield.extraction_run",
    "billshield.charge_candidate",
    "billshield.promotion_candidate",
    "billshield.job_outbox",
)

GLOBAL_TABLES = ("billshield.provider", "billshield.provider_category")

#: One valid locator list, canonical shape, reused wherever a test needs
#: evidence it is not itself testing.
EVIDENCE = '[{"page": 1, "x0": "0.100000", "y0": "0.200000", ' \
           '"x1": "0.900000", "y1": "0.300000"}]'


@contextlib.contextmanager
def owner_cursor():
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    try:
        yield conn.cursor()
    finally:
        conn.close()


@contextlib.contextmanager
def as_role(role: str, user_id: uuid.UUID | None = None):
    """Become `role`, optionally with a tenant GUC, and always roll back.

    `onyx_app_rw` and `onyx_billshield_worker` are both NOLOGIN, so `SET ROLE`
    is how a test acts as either. The GUC is transaction-local, exactly as
    `unit_of_work` sets it.
    """
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = False
    try:
        cur = conn.cursor()
        if user_id is not None:
            cur.execute("SELECT set_config('app.user_id', %s, true)", (str(user_id),))
        cur.execute(f"SET ROLE {role}")
        yield cur
    finally:
        conn.rollback()
        conn.close()


@dataclass
class Tenant:
    """One account with a full BillShield tree, seeded by the owner."""

    label: str
    user_id: uuid.UUID = field(init=False)
    bill_id: uuid.UUID = field(init=False)
    #: A second bill left at `upload_pending` — never finalized — so a test can
    #: use it without disturbing the extraction tree hanging off the first.
    spare_bill_id: uuid.UUID = field(init=False)
    file_sha256: str = field(init=False)
    run_id: uuid.UUID = field(init=False)
    charge_id: uuid.UUID = field(init=False)
    promotion_id: uuid.UUID = field(init=False)
    outbox_id: uuid.UUID = field(init=False)

    def seed(self, cur) -> None:
        cur.execute(
            "INSERT INTO identity.user_account (email) VALUES (%s) RETURNING id",
            (f"billshield-{self.label}-{uuid.uuid4().hex[:8]}@example.test",),
        )
        self.user_id = cur.fetchone()[0]

        # A finalized bill: the digest is deterministic per tenant so the
        # composite artifact binding is easy to reason about in assertions.
        self.file_sha256 = uuid.uuid5(uuid.NAMESPACE_OID, self.label).hex * 2
        cur.execute(
            "INSERT INTO billshield.bill (user_id, status, file_sha256, byte_size,"
            " artifact_format, page_count)"
            " VALUES (%s, 'needs_review', %s, 1024, 'pdf_native', 2) RETURNING id",
            (self.user_id, self.file_sha256),
        )
        self.bill_id = cur.fetchone()[0]

        cur.execute(
            "INSERT INTO billshield.bill (user_id) VALUES (%s) RETURNING id",
            (self.user_id,),
        )
        self.spare_bill_id = cur.fetchone()[0]

        cur.execute(
            "INSERT INTO billshield.extraction_run (bill_id, input_sha256, status,"
            " adapter_code, model_version, extraction_schema_version, currency,"
            " response_hash, completed_at, amount_due_value, amount_due_confidence,"
            " amount_due_evidence)"
            " VALUES (%s, %s, 'succeeded', 'fixture', 'fixture-1.0.0', '1.0.0',"
            " 'CAD', %s, now(), 89.99, 0.990000, %s::billshield.evidence_locators)"
            " RETURNING id",
            (self.bill_id, self.file_sha256, "a" * 64, EVIDENCE),
        )
        self.run_id = cur.fetchone()[0]

        cur.execute(
            "INSERT INTO billshield.charge_candidate (extraction_run_id, position,"
            " label_text, label_confidence, label_evidence, amount,"
            " amount_confidence, amount_evidence, kind, kind_confidence)"
            " VALUES (%s, 0, 'Monthly plan', 0.980000,"
            " %s::billshield.evidence_locators, 75.00, 0.990000,"
            " %s::billshield.evidence_locators, 'RECURRING_FIXED', 0.950000)"
            " RETURNING id",
            (self.run_id, EVIDENCE, EVIDENCE),
        )
        self.charge_id = cur.fetchone()[0]

        cur.execute(
            "INSERT INTO billshield.promotion_candidate (extraction_run_id, position,"
            " charge_position, expiry_date, expiry_confidence, expiry_evidence)"
            " VALUES (%s, 0, 0, DATE '2026-01-31', 0.900000,"
            " %s::billshield.evidence_locators) RETURNING id",
            (self.run_id, EVIDENCE),
        )
        self.promotion_id = cur.fetchone()[0]

        cur.execute(
            "INSERT INTO billshield.job_outbox (user_id, bill_id, task_code, dedupe_key)"
            " VALUES (%s, %s, 'EXTRACT_BILL', %s) RETURNING id",
            (self.user_id, self.bill_id, str(uuid.uuid4())),
        )
        self.outbox_id = cur.fetchone()[0]

    def delete(self, cur) -> None:
        cur.execute("DELETE FROM identity.user_account WHERE id = %s", (self.user_id,))


@pytest.fixture(scope="module")
def tenants():
    """Tenants A and B, torn down by the account cascade they rely on."""
    a, b = Tenant("a"), Tenant("b")
    with owner_cursor() as cur:
        a.seed(cur)
        b.seed(cur)
    yield a, b
    with owner_cursor() as cur:
        a.delete(cur)
        b.delete(cur)
