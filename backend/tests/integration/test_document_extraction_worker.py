"""Reading the document the customer actually uploaded.

THE BOUNDARY UNDER TEST IS NOT MOCKED. Bytes are written to the real object
store under the document's own key, and the extraction reads them back through
`ObjectStorage.get` exactly as the worker does. Stubbing that read would leave
the one step this entry adds — storage to text — untested, which is the step
that was missing in the first place.

Against real PostgreSQL with RLS live.
"""
import asyncio
import json
import re
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.security.jwt import decode_access_token
from app.database.models import (
    Document,
    DocumentExtraction,
    DocumentType,
    ExtractionField,
    UserAccount,
)
from app.database.privacy_session import dispose_all_engines
from app.database.session import unit_of_work
from app.integrations.storage import get_object_storage
from app.services.admission.limits import (
    ALLOWED_DOCUMENT_MIME_TYPES,
    MAX_EXTRACTION_TEXT_BYTES,
)
from app.services.document_processing.ocr import extract_fields
from app.services.document_processing.service import DocumentService
from app.services.document_processing.text import (
    REASON_EMPTY,
    REASON_NOT_TEXT,
    REASON_TOO_LARGE,
    REASON_UNSUPPORTED_MEDIA,
    UNREADABLE_TODAY,
    DocumentUnreadable,
    text_from,
)
from app.services.privacy.lifecycle import AccountLifecycleService
from tests.conftest import register_verified
from workers.tasks.documents import extract_document

T4_TEXT = b"""ACME MANUFACTURING LTD
Statement of Remuneration Paid
Box 14  Employment income      42,680.00
Box 16  CPP contributions       2,430.15
Box 18  EI premiums               743.60
Box 22  Income tax deducted     5,910.00
"""


async def _auth(client, email):
    token = await register_verified(client, email, "supersecret1")
    return {"Authorization": f"Bearer {token}"}


def _user_id(auth) -> uuid.UUID:
    """The owner, read from the credential the client actually holds.

    NOT from a privileged database peek. `docs.document` is FORCE row level
    security keyed on `app.user_id`, and `unit_of_work` leaves that GUC UNSET
    when it is given no user — deny-by-default, deliberately. So a userless
    session cannot see the row, which is correct behaviour and worth keeping
    intact; the test identifies the owner the same way the API does.
    """
    return uuid.UUID(decode_access_token(auth["Authorization"].split()[1])["sub"])


async def _uploaded(client, auth, *, mime_type: str, body: bytes, code: str = "T4"):
    """Register a document and put real bytes where its key says they are."""
    r = await client.post(
        "/api/v1/documents",
        headers=auth,
        json={
            "document_type_code": code,
            "filename": "slip.txt",
            "mime_type": mime_type,
            "tax_year": 2025,
        },
    )
    assert r.status_code == 201, r.text
    document_id = uuid.UUID(r.json()["document_id"])

    # The bucket and key are resolved server-side and never returned to a
    # client (routes.py is explicit about that), so the test reads them as the
    # OWNER to put bytes where the real upload would have landed them.
    user_id = _user_id(auth)
    async with unit_of_work(user_id=user_id, actor_type="user") as session:
        doc = await session.get(Document, document_id)
        assert doc is not None, "the document row is not visible to its own owner"
        bucket, key = doc.bucket, doc.object_key
    get_object_storage().put(bucket, key, body)
    return document_id, user_id


@pytest.mark.asyncio
async def test_a_stored_text_document_is_read_and_its_figures_extracted(client):
    """The step that did not exist: bytes in storage become extracted fields."""
    auth = await _auth(client, f"ext_{uuid.uuid4().hex[:8]}@test.ca")
    document_id, user_id = await _uploaded(client, auth, mime_type="text/plain", body=T4_TEXT)

    async with unit_of_work(user_id=user_id, actor_type="user") as session:
        extraction = await DocumentService(session).extract_from_storage(user_id, document_id)
        assert extraction.status == "processed", "a readable slip did not process"
        fields = {
            f.field_name: f.value_number
            for f in await session.scalars(
                select(ExtractionField).where(ExtractionField.extraction_id == extraction.id)
            )
        }

    # THE VALUE IS THE DOCUMENT'S, not the test's: nothing here supplied it.
    assert fields["employmentIncome"] == Decimal("42680.00"), fields
    assert extraction.confidence is not None and extraction.confidence > 0


@pytest.mark.asyncio
async def test_extracted_figures_confirm_into_document_backed_income(client):
    """And the provenance claim is now TRUE: a document really did supply this."""
    auth = await _auth(client, f"conf_{uuid.uuid4().hex[:8]}@test.ca")
    document_id, user_id = await _uploaded(client, auth, mime_type="text/plain", body=T4_TEXT)

    async with unit_of_work(user_id=user_id, actor_type="user") as session:
        await DocumentService(session).extract_from_storage(user_id, document_id)

    r = await client.post(
        f"/api/v1/documents/{document_id}/confirm", headers=auth, json={"tax_year": 2025}
    )
    assert r.status_code == 200, r.text
    assert r.json()["created"]["income"] == 1, r.text

    r = await client.get("/api/v1/financials/income", headers=auth, params={"tax_year": 2025})
    assert r.status_code == 200, r.text
    amounts = [row["amount"] for row in r.json()]
    assert any(Decimal(str(a)) == Decimal("42680.00") for a in amounts), amounts


@pytest.mark.asyncio
async def test_a_media_type_onyx_cannot_read_is_recorded_as_failed(client):
    """NOT as an empty successful read.

    A PDF is accepted by the upload path and cannot be turned into text yet.
    Running the regexes over empty text would produce zero fields at zero
    confidence — indistinguishable from having read the slip and found nothing.
    """
    auth = await _auth(client, f"pdf_{uuid.uuid4().hex[:8]}@test.ca")
    document_id, user_id = await _uploaded(
        client, auth, mime_type="application/pdf", body=b"%PDF-1.7\n%stub\n"
    )

    async with unit_of_work(user_id=user_id, actor_type="user") as session:
        with pytest.raises(DocumentUnreadable) as caught:
            await DocumentService(session).extract_from_storage(user_id, document_id)
        assert caught.value.reason == REASON_UNSUPPORTED_MEDIA

    async with unit_of_work(user_id=user_id, actor_type="user") as session:
        doc = await session.get(Document, document_id)
        assert doc is not None and doc.status == "failed", "an unread document looks processed"
        extraction = await session.scalar(
            select(DocumentExtraction).where(DocumentExtraction.document_id == document_id)
        )
        assert extraction is not None, "the refusal was not recorded at all"
        assert extraction.status == "failed"
        assert REASON_UNSUPPORTED_MEDIA in extraction.engine, extraction.engine
        # …and no fields were invented for it.
        fields = list(
            await session.scalars(
                select(ExtractionField).where(ExtractionField.extraction_id == extraction.id)
            )
        )
        assert fields == [], "an unreadable document produced extraction fields"


@pytest.mark.asyncio
async def test_a_document_onyx_could_not_read_cannot_be_confirmed(client):
    """`confirm` takes the NEWEST extraction, and a refusal is now one.

    A failed extraction carries no fields, so confirmation could not have
    invented a `document_backed` row from it — the loop has nothing to iterate.
    What it WOULD have done is return `{"income": 0, "expense": 0}`: a silent
    success meaning "confirmed", for a document Onyx never managed to open.
    """
    auth = await _auth(client, f"nc_{uuid.uuid4().hex[:8]}@test.ca")
    document_id, user_id = await _uploaded(
        client, auth, mime_type="application/pdf", body=b"%PDF-1.7\n%stub\n"
    )

    async with unit_of_work(user_id=user_id, actor_type="user") as session:
        with pytest.raises(DocumentUnreadable):
            await DocumentService(session).extract_from_storage(user_id, document_id)

    r = await client.post(
        f"/api/v1/documents/{document_id}/confirm", headers=auth, json={"tax_year": 2025}
    )
    assert r.status_code == 422, r.text   # this repo maps ValidationError to 422
    assert "nothing to confirm" in r.text

    # and no financial row was created for it
    r = await client.get("/api/v1/financials/income", headers=auth, params={"tax_year": 2025})
    assert r.json() == [], r.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,reason",
    [
        (b"\xff\xfe\x00\x01not text at all", REASON_NOT_TEXT),
        (b"   \n\t  ", REASON_EMPTY),
    ],
    ids=["undecodable bytes", "empty document"],
)
async def test_unreadable_text_is_refused_rather_than_guessed_at(client, body, reason):
    auth = await _auth(client, f"bad_{uuid.uuid4().hex[:8]}@test.ca")
    document_id, user_id = await _uploaded(client, auth, mime_type="text/plain", body=body)

    async with unit_of_work(user_id=user_id, actor_type="user") as session:
        with pytest.raises(DocumentUnreadable) as caught:
            await DocumentService(session).extract_from_storage(user_id, document_id)
        assert caught.value.reason == reason


@pytest.mark.asyncio
async def test_the_refusal_never_carries_the_document_contents(client):
    """A decode error's own message quotes the offending bytes.

    Those bytes are a fragment of somebody's tax slip, and an exception message
    travels into logs and task failure records.
    """
    auth = await _auth(client, f"leak_{uuid.uuid4().hex[:8]}@test.ca")
    secret = b"SALARY-9F3A2B-SECRET"
    document_id, user_id = await _uploaded(
        client, auth, mime_type="text/plain", body=secret + b"\xff\xfe"
    )

    async with unit_of_work(user_id=user_id, actor_type="user") as session:
        with pytest.raises(DocumentUnreadable) as caught:
            await DocumentService(session).extract_from_storage(user_id, document_id)

    rendered = f"{caught.value!r} {caught.value!s} {caught.value.__cause__!r}"
    assert "SALARY-9F3A2B" not in rendered, rendered
    assert "\\xff" not in rendered, rendered


@pytest.mark.asyncio
async def test_another_tenants_document_is_not_extractable(client):
    """Two locks: the explicit owner comparison, and FORCE row level security."""
    owner_auth = await _auth(client, f"own_{uuid.uuid4().hex[:8]}@test.ca")
    document_id, _owner = await _uploaded(client, owner_auth, mime_type="text/plain", body=T4_TEXT)

    stranger_auth = await _auth(client, f"str_{uuid.uuid4().hex[:8]}@test.ca")
    stranger_id = _user_id(stranger_auth)

    from app.core.exceptions import NotFound

    async with unit_of_work(user_id=stranger_id, actor_type="user") as session:
        with pytest.raises(NotFound):
            await DocumentService(session).extract_from_storage(stranger_id, document_id)


@pytest.mark.asyncio
async def test_extraction_can_run_twice_in_one_process(client):
    """A worker calls this many times. The second call must behave like the first."""
    auth = await _auth(client, f"twice_{uuid.uuid4().hex[:8]}@test.ca")
    document_id, user_id = await _uploaded(client, auth, mime_type="text/plain", body=T4_TEXT)

    for attempt in (1, 2):
        async with unit_of_work(user_id=user_id, actor_type="user") as session:
            extraction = await DocumentService(session).extract_from_storage(user_id, document_id)
            assert extraction.status == "processed", f"call {attempt} did not process"


@pytest.mark.asyncio
async def test_the_box_number_patterns_do_not_survive_a_realistic_slip_layout():
    """A RECORDED PRE-EXISTING LIMITATION of the certified extractor, pinned.

    `SLIP_MAP` matches a box number and then allows at most 12 non-digit
    characters before the amount. A real T4 line puts the box LABEL in that
    gap — "Box 16  CPP contributions       2,430.15" spends 26 characters
    there — so every box-number pattern misses. Only `employmentIncome`
    survives, and not by its box 14 pattern either: by its "employment income"
    label fallback, which is the only fallback any T4 field has.

    NOT FIXED HERE. Changing these regexes changes what the already-certified
    `POST /documents/{id}/process` endpoint extracts, which is a governed
    extraction-behaviour change and not what "start the extraction worker"
    asked for. The consequence today is contained: of the four T4 fields, only
    `employmentIncome` appears in `FIELD_TARGET`, so the three that miss reach
    no financial row even when they do match.

    This test exists so the limitation cannot be forgotten. When the patterns
    are fixed it will fail, and that failure is the prompt to delete it.
    """
    extracted, confidence = extract_fields("T4", text=T4_TEXT.decode())

    assert extracted == {"employmentIncome": Decimal("42680.00")}, extracted
    assert confidence > 0, "the one field that did match reported no confidence"

    # The gap, measured rather than asserted from memory.
    for box in ("16", "18", "22"):
        line = next(x for x in T4_TEXT.decode().splitlines() if f"Box {box}" in x)
        gap = re.search(r"Box \d+([^0-9]*)", line).group(1)
        assert len(gap) > 12, f"box {box} would now match; the limitation is gone"


# =============================================================================
# The Celery task itself
#
# SYNCHRONOUS, like a real worker: `run_task` OWNS the event loop and disposes
# every engine on the way out, so the task body cannot be awaited from inside a
# running loop. Seeding therefore goes through `asyncio.run` and the engines are
# disposed either side, exactly as test_integrity_scheduler_task.py does — a
# pool bound to a dead loop is the failure mode that pattern exists to avoid.
# =============================================================================
def _seed(mime_type: str, body: bytes, code: str = "T4") -> tuple[uuid.UUID, uuid.UUID]:
    """A user and a stored document, without the HTTP path.

    The HTTP path is already covered above. This seeds directly so the task can
    be driven from a synchronous test.
    """

    async def _go() -> tuple[uuid.UUID, uuid.UUID]:
        async with unit_of_work(actor_type="system") as s:
            user = UserAccount(email=f"wk_{uuid.uuid4().hex[:8]}@test.ca", status="active")
            s.add(user)
            await s.flush()
            user_id = user.id
            dtype = await s.scalar(select(DocumentType).where(DocumentType.code == code))
            assert dtype is not None, f"document type {code} is not seeded"
            dtype_id = dtype.id

        async with unit_of_work(user_id=user_id, actor_type="user") as s:
            bucket = get_settings().s3_bucket_documents
            doc = Document(
                user_id=user_id, document_type_id=dtype_id, tax_year=2025,
                bucket=bucket, object_key="", mime_type=mime_type, status="uploaded",
            )
            s.add(doc)
            await s.flush()
            doc.object_key = f"{user_id}/v2/{doc.id}"
            await s.flush()
            document_id, key = doc.id, doc.object_key

        get_object_storage().put(bucket, key, body)
        # INSIDE this loop, while its connections are still alive. Disposing
        # from a later loop means closing connections whose loop is gone, which
        # raises "Event loop is closed" out of asyncpg's teardown — a test that
        # seeds twice hits it and a test that seeds once does not.
        await dispose_all_engines()
        return document_id, user_id

    return asyncio.run(_go())


def _run_task(*args, **kwargs):
    """No disposal here on purpose.

    `run_task` owns the loop and disposes every engine in its own `finally` —
    that is the contract the worker-runtime entry established. If this needed a
    dispose either side, that contract would be broken and the worker would be
    leaking pools into whatever ran next.
    """
    return extract_document.run(*args, **kwargs)


def test_the_task_is_registered_and_routed_to_the_queue_that_was_already_waiting():
    """The route existed and pointed at nothing. This is what it points at now.

    REGISTERED FOR A REAL WORKER, not for this process. The import at the top
    of this file has already run the registering decorator here, so asserting
    against this process's `celery_app.tasks` would pass with `include`
    emptied entirely — which is exactly how the task stayed declared, routed,
    and unregistered in production for as long as it did (integration plan
    §4.2). The evidence instead comes from a fresh subprocess that imports
    only `workers.celery_app` and performs the worker's own default-module
    loading, the shape a real `celery -A workers.celery_app worker` start has.
    """
    from tests.security.test_celery_registration import clean_worker_start_evidence
    from workers.celery_app import celery_app

    assert extract_document.name == "workers.tasks.documents.extract_document"
    assert extract_document.name in clean_worker_start_evidence()["registered"], (
        "the task this file exercises is not registered by a clean worker "
        "start — its module is missing from celery_app include, so the "
        "documents queue is consumed by workers that reject every message"
    )
    assert celery_app.conf.task_routes["workers.tasks.documents.*"] == {"queue": "documents"}


def test_the_task_extracts_a_stored_document_end_to_end():
    document_id, user_id = _seed("text/plain", T4_TEXT)

    result = _run_task(str(document_id), str(user_id))

    assert result["status"] == "processed", result
    assert result["document_id"] == str(document_id)


def test_the_task_result_never_carries_the_document_contents():
    """A Celery return value is written to the result backend.

    The numbers on a tax slip are the most sensitive thing in the product, and
    the object key is infrastructure the customer never sees.
    """
    document_id, user_id = _seed("text/plain", T4_TEXT)

    rendered = json.dumps(_run_task(str(document_id), str(user_id)))

    for forbidden in ("42,680", "42680", "2,430.15", "ACME", "v2/", str(user_id)):
        assert forbidden not in rendered, f"{forbidden!r} travelled in the task result"


def test_an_unreadable_media_type_ends_the_task_rather_than_retrying_it():
    """A PDF will not become readable on the fourth attempt."""
    document_id, user_id = _seed("application/pdf", b"%PDF-1.7\n%stub\n")

    result = _run_task(str(document_id), str(user_id))

    assert result["status"] == "failed"
    assert result["reason"] == REASON_UNSUPPORTED_MEDIA


def test_a_document_the_enqueued_user_does_not_own_is_absent_not_an_error():
    """RLS scopes the lookup, so a mismatched pair simply finds nothing."""
    document_id, _owner = _seed("text/plain", T4_TEXT)
    _other_document, stranger_id = _seed("text/plain", T4_TEXT)

    result = _run_task(str(document_id), str(stranger_id))

    assert result["status"] == "absent", result


def test_the_task_refuses_a_document_for_an_account_being_deleted():
    """THE QUEUE RACE, for this task specifically.

    Extraction was published before the account asked to be erased and reaches
    a worker afterwards. Celery `revoke` cannot close that — a reserved task is
    already past the queue, `acks_late` redelivers it after a restart, and an
    offline worker never sees the broadcast. Extraction writes an extraction
    row, its fields and a document status, so without this check the deletion
    would be racing new user data into the database it is trying to empty.

    NON-VACUOUS BY CONSTRUCTION: the same document, the same bytes and the same
    task extract successfully in `test_the_task_extracts_a_stored_document_end_
    to_end`. The only difference here is the lifecycle row.
    """
    document_id, user_id = _seed("text/plain", T4_TEXT)

    async def _request_deletion() -> None:
        async with unit_of_work(user_id=user_id, actor_type="user") as s:
            await AccountLifecycleService(s).request_deletion(user_id)
        await dispose_all_engines()

    asyncio.run(_request_deletion())

    assert _run_task(str(document_id), str(user_id)) == {
        "document_id": str(document_id), "status": "refused",
    }

    async def _nothing_was_written() -> list:
        async with unit_of_work(user_id=user_id, actor_type="user") as s:
            rows = list(await s.scalars(
                select(DocumentExtraction).where(
                    DocumentExtraction.document_id == document_id)
            ))
        await dispose_all_engines()
        return rows

    assert asyncio.run(_nothing_was_written()) == [], (
        "the refusal still wrote an extraction for a deleting account"
    )


# =============================================================================
# The capability gap is a LIST, not a surprise
# =============================================================================
def test_every_accepted_upload_type_is_either_readable_or_a_named_gap():
    """The guard that keeps `UNREADABLE_TODAY` honest.

    The upload allow-list and this module are edited by different people for
    different reasons. Accepting a new media type without deciding whether it
    can be read would produce a silent, unlisted refusal at extraction time —
    a customer uploading a format Onyx advertised as accepted, and being told
    nothing useful about why nothing came back.

    Neither half is allowed to drift: an entry listed as an unreadable gap that
    has since become readable is stale in the other direction, and is caught
    here too.
    """
    for media in sorted(ALLOWED_DOCUMENT_MIME_TYPES):
        readable = True
        try:
            text_from(media, b"probe")
        except DocumentUnreadable as refused:
            readable = refused.reason != REASON_UNSUPPORTED_MEDIA

        assert readable != (media in UNREADABLE_TODAY), (
            f"{media} is accepted for upload but is "
            f"{'readable and still listed as a gap' if readable else 'not readable and not listed in UNREADABLE_TODAY'}"
        )


def test_text_larger_than_the_extraction_bound_is_refused_before_it_is_decoded():
    """The bound belongs wherever text is produced, not only where a client
    supplies it — a 25 MB upload is inside the storage limit."""
    with pytest.raises(DocumentUnreadable) as caught:
        text_from("text/plain", b"9" * (MAX_EXTRACTION_TEXT_BYTES + 1))
    assert caught.value.reason == REASON_TOO_LARGE
