"""The production S3 adapter's contract, proven against botocore's own stubber.

WHY A STUBBER AND NOT A FAKE. A hand-written fake agrees with whatever the
adapter happens to do, which makes it a mirror rather than a test.
`botocore.stub.Stubber` validates the parameters the adapter sends against the
real S3 service model and replays responses and errors in the shape botocore
itself would raise — so a call the adapter gets wrong fails here rather than in
front of a bucket.

No credentials, no network, no bucket. The stubber intercepts before any socket
is opened, and the presign tests are pure local signing.

THE INVARIANT THIS FILE EXISTS FOR is the last one:
`AccountLifecycleService` finalizes a document purge on `DELETED` or
`ALREADY_ABSENT` and on nothing else, so every path through `delete` that is
not a proven erasure must return something else. An unrecognised provider error
mapped optimistically would turn "we do not know what happened" into a
permanent, audited claim that a customer's document was erased.
"""
from __future__ import annotations

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ConnectTimeoutError
from botocore.stub import ANY, Stubber

from app.domain.ports import DeleteOutcome, UploadAuthorization
from app.integrations.storage import LocalObjectStorage, S3ObjectStorage

BUCKET = "onyx-test-documents"
KEY = "11111111-1111-7111-8111-111111111111/v2/22222222-2222-7222-8222-222222222222"


@pytest.fixture
def client():
    """A real client with unusable credentials; the stubber answers every call."""
    return boto3.client(
        "s3",
        region_name="ca-central-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",  # noqa: S106 - not a credential, a placeholder
        aws_session_token="testing",  # noqa: S106
        config=Config(retries={"max_attempts": 1, "mode": "standard"}),
    )


# ------------------------------------------------------------------ bytes --
def test_put_sends_the_body_to_the_named_object(client):
    store = S3ObjectStorage(client)
    with Stubber(client) as stub:
        stub.add_response(
            "put_object", {},
            {"Bucket": BUCKET, "Key": KEY, "Body": b"hello"},
        )
        store.put(BUCKET, KEY, b"hello")
        stub.assert_no_pending_responses()


def test_put_applies_server_side_encryption_when_configured(client):
    store = S3ObjectStorage(client, sse_algorithm="aws:kms", sse_kms_key_id="key-1")
    with Stubber(client) as stub:
        stub.add_response(
            "put_object", {},
            {"Bucket": BUCKET, "Key": KEY, "Body": b"x",
             "ServerSideEncryption": "aws:kms", "SSEKMSKeyId": "key-1"},
        )
        store.put(BUCKET, KEY, b"x")
        stub.assert_no_pending_responses()


def test_put_sets_no_acl_at_all(client):
    """An object that must stay private is not the place to be naming grants.

    The bucket policy governs. `public-read` is one typo from `private`, and
    the way not to make that typo is to never type in that vocabulary — so the
    assertion is that the parameter is absent, not that it says the right thing.
    """
    store = S3ObjectStorage(client)
    sent: dict = {}
    with Stubber(client) as stub:
        stub.add_response("put_object", {}, {"Bucket": ANY, "Key": ANY, "Body": ANY})
        client.meta.events.register(
            "provide-client-params.s3.PutObject",
            lambda params, **_: sent.update(params),
        )
        store.put(BUCKET, KEY, b"x")
    assert "ACL" not in sent, f"the adapter sent an ACL: {sent.get('ACL')!r}"


def test_get_returns_the_object_bytes(client):
    from io import BytesIO

    from botocore.response import StreamingBody

    store = S3ObjectStorage(client)
    with Stubber(client) as stub:
        stub.add_response(
            "get_object",
            {"Body": StreamingBody(BytesIO(b"contents"), 8)},
            {"Bucket": BUCKET, "Key": KEY},
        )
        assert store.get(BUCKET, KEY) == b"contents"


def test_get_on_a_missing_object_agrees_with_the_local_store(client):
    """Two adapters behind one port must not disagree about a missing object.

    Whichever answer is chosen, it has to be the same one, or every caller's
    behaviour silently depends on which adapter is installed.
    """
    store = S3ObjectStorage(client)
    with Stubber(client) as stub:
        stub.add_client_error("get_object", service_error_code="NoSuchKey")
        s3_answer = store.get(BUCKET, KEY)
    local_answer = LocalObjectStorage().get(BUCKET, "never-written")
    assert s3_answer == local_answer == b""


# ----------------------------------------------------------------- delete --
def test_a_successful_delete_is_deleted(client):
    store = S3ObjectStorage(client)
    with Stubber(client) as stub:
        stub.add_response("delete_object", {}, {"Bucket": BUCKET, "Key": KEY})
        assert store.delete(BUCKET, KEY) is DeleteOutcome.DELETED


def test_a_delete_marker_is_not_an_erasure(client):
    """PD-10, detected from the response instead of warned about in a comment.

    On a versioned bucket `delete_object` removes nothing: it writes a marker
    and every previous version stays readable by anyone who can name it. The
    provider reports success. If the adapter passed that through, the privacy
    lifecycle would finalize the purge and the bytes would still be there —
    the single configuration where "the delete succeeded" and "the document is
    gone" come apart.

    S3 says so in the response at no extra cost, so no HEAD is needed to
    notice.
    """
    store = S3ObjectStorage(client)
    with Stubber(client) as stub:
        stub.add_response(
            "delete_object", {"DeleteMarker": True, "VersionId": "v2"},
            {"Bucket": BUCKET, "Key": KEY},
        )
        outcome = store.delete(BUCKET, KEY)
    assert outcome is DeleteOutcome.PERMANENT_FAILURE
    assert outcome not in (DeleteOutcome.DELETED, DeleteOutcome.ALREADY_ABSENT), (
        "a delete marker was reported as erasure; the privacy lifecycle would "
        "finalize a purge over surviving object versions"
    )


def test_a_missing_object_converges_instead_of_retrying_forever(client):
    store = S3ObjectStorage(client)
    with Stubber(client) as stub:
        stub.add_client_error("delete_object", service_error_code="NoSuchKey")
        assert store.delete(BUCKET, KEY) is DeleteOutcome.ALREADY_ABSENT


def test_permission_denied_is_permanent(client):
    store = S3ObjectStorage(client)
    with Stubber(client) as stub:
        stub.add_client_error("delete_object", service_error_code="AccessDenied")
        assert store.delete(BUCKET, KEY) is DeleteOutcome.PERMANENT_FAILURE


def test_a_timeout_is_retryable(client):
    """A transport failure carries no provider code at all — the adapter must
    not read `str(exc)` to find one, and must not treat the absence as success.
    """
    store = S3ObjectStorage(client)

    def _timeout(*_args, **_kwargs):
        raise ConnectTimeoutError(endpoint_url="https://s3.ca-central-1.amazonaws.com")

    client.delete_object = _timeout
    assert store.delete(BUCKET, KEY) is DeleteOutcome.RETRYABLE_FAILURE


def test_a_server_error_is_retryable(client):
    store = S3ObjectStorage(client)
    with Stubber(client) as stub:
        stub.add_client_error("delete_object", service_error_code="InternalError")
        assert store.delete(BUCKET, KEY) is DeleteOutcome.RETRYABLE_FAILURE


@pytest.mark.parametrize(
    "code",
    ["SomeCodeNobodyHasSeen", "ThrottledLikeThis", "", "TeapotError"],
)
def test_an_unrecognised_error_never_reports_erasure(client, code):
    """THE central mapping rule.

    The adapter cannot know every code a provider or an S3-compatible store
    might return. What it can guarantee is the direction of the guess: an
    unknown condition is not evidence that a customer's document was destroyed,
    so it must never produce an outcome the lifecycle treats as erasure.
    """
    store = S3ObjectStorage(client)
    with Stubber(client) as stub:
        stub.add_client_error("delete_object", service_error_code=code)
        outcome = store.delete(BUCKET, KEY)
    assert outcome is DeleteOutcome.RETRYABLE_FAILURE
    assert outcome not in (DeleteOutcome.DELETED, DeleteOutcome.ALREADY_ABSENT)


def test_no_delete_path_raises_a_provider_exception(client):
    """Provider exceptions must not escape the adapter (port contract).

    A lifecycle that had to catch `ClientError` would be coupled to botocore,
    and a lifecycle keyed on `str(e)` cannot be reasoned about at all.
    """
    store = S3ObjectStorage(client)
    for code in ("AccessDenied", "NoSuchBucket", "SlowDown", "NoSuchKey"):
        with Stubber(client) as stub:
            stub.add_client_error("delete_object", service_error_code=code)
            outcome = store.delete(BUCKET, KEY)  # must not raise
        assert isinstance(outcome, DeleteOutcome)


def test_a_provider_error_leaks_nothing_into_the_outcome(client):
    """The closed outcome is the whole return value.

    Entry 11A established that provider exception text carries the bucket, the
    endpoint and sometimes an account hint. None of it may ride back to a
    caller that will store the result in a privacy ledger.
    """
    store = S3ObjectStorage(client)
    with Stubber(client) as stub:
        stub.add_client_error(
            "delete_object",
            service_error_code="AccessDenied",
            service_message=(
                "User: arn:aws:sts::123456789012:assumed-role/onyx-api/i-0abc "
                "is not authorized to perform s3:DeleteObject on "
                "arn:aws:s3:::onyx-prod-documents/secret-key"
            ),
        )
        outcome = store.delete(BUCKET, KEY)
    rendered = f"{outcome!r} {outcome.value}"
    for leaked in ("123456789012", "onyx-prod-documents", "assumed-role", "secret-key"):
        assert leaked not in rendered, f"{leaked!r} escaped in the outcome"


# ------------------------------------------------------------ upload permit --
def test_the_upload_permit_carries_the_size_ceiling(client):
    """The reason a permit is a URL and fields rather than a bare URL.

    S3 enforces a maximum object size only through a POST policy's
    `content-length-range` condition. An adapter that returned
    `generate_presigned_post(...)["url"]` and dropped the fields would discard
    the policy — the ceiling in the port's docstring would stop existing the
    moment it met a real bucket.
    """
    import base64
    import json

    store = S3ObjectStorage(client)
    permit = store.presign_put(BUCKET, KEY, "application/pdf", max_bytes=25_000_000)

    assert isinstance(permit, UploadAuthorization)
    assert permit.url.startswith("https://"), permit.url
    assert permit.fields, "the permit carries no fields, so it carries no policy"

    policy = json.loads(base64.b64decode(permit.fields["policy"]))
    ranges = [c for c in policy["conditions"]
              if isinstance(c, list) and c and c[0] == "content-length-range"]
    assert ranges == [["content-length-range", 1, 25_000_000]], (
        f"the policy does not bound the object size: {policy['conditions']}"
    )


def test_the_upload_permit_expires(client):
    import base64
    import json

    store = S3ObjectStorage(client)
    permit = store.presign_put(BUCKET, KEY, "application/pdf", max_bytes=1024)
    policy = json.loads(base64.b64decode(permit.fields["policy"]))
    assert policy["expiration"], "a permit with no expiry is a standing grant"


def test_the_upload_permit_pins_the_content_type(client):
    import base64
    import json

    store = S3ObjectStorage(client)
    permit = store.presign_put(BUCKET, KEY, "application/pdf", max_bytes=1024)
    policy = json.loads(base64.b64decode(permit.fields["policy"]))
    assert {"Content-Type": "application/pdf"} in policy["conditions"]


def test_an_unbounded_upload_cannot_be_authorized(client):
    store = S3ObjectStorage(client)
    for bad in (0, -1):
        with pytest.raises(ValueError):
            store.presign_put(BUCKET, KEY, "application/pdf", max_bytes=bad)


def test_both_adapters_return_the_same_permit_type(client):
    """One port, one return type. A caller must not have to ask which store it
    is talking to before it can read the answer."""
    s3 = S3ObjectStorage(client).presign_put(BUCKET, KEY, "application/pdf", max_bytes=99)
    local = LocalObjectStorage().presign_put(BUCKET, KEY, "application/pdf", max_bytes=99)
    assert type(s3) is type(local) is UploadAuthorization
