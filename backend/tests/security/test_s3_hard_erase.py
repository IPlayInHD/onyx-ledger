"""Versioned-bucket hard erasure: does "erased" mean the bytes are gone?

WHAT B2A CHANGES. B2 made the adapter refuse to call a delete marker an
erasure, which was correct and left both callers stuck: on a versioned bucket
the privacy phase could never complete, and ordinary customer deletion raised a
503 every time, permanently. `hard_erase` removes every version and every
marker for one key and then re-lists to prove it.

THE PROPERTY UNDER TEST, stated once:

    hard_erase returns DELETED
      IMPLIES
    no current version, no historical version and no delete marker remains
    for that key

Every test below is either an instance of that or an attempt to break it. The
three the entry names explicitly are `test_a_delete_marker_alone_is_not_an_erasure`
(A), `test_one_surviving_historical_version_is_not_an_erasure` (B), and
`test_every_version_and_marker_removed_is_an_erasure` (C).

Driven by `botocore.stub.Stubber`, so the parameters the adapter sends are
validated against the real S3 service model. No credentials, no network.
"""
from __future__ import annotations

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ConnectTimeoutError
from botocore.stub import Stubber

from app.domain.ports import DeleteOutcome
from app.integrations.storage import S3ObjectStorage

BUCKET = "onyx-test-documents"
KEY = "11111111-1111-7111-8111-111111111111/v2/22222222-2222-7222-8222-222222222222"
SUCCESS = (DeleteOutcome.DELETED, DeleteOutcome.ALREADY_ABSENT)


@pytest.fixture
def client():
    return boto3.client(
        "s3",
        region_name="ca-central-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",  # noqa: S106 - placeholder, not a credential
        aws_session_token="testing",  # noqa: S106
        config=Config(retries={"max_attempts": 1, "mode": "standard"}),
    )


def _versions(*version_ids: str, key: str = KEY) -> list[dict]:
    return [{"Key": key, "VersionId": v} for v in version_ids]


def _listing(versions=(), markers=(), truncated=False, **markers_kw) -> dict:
    page: dict = {"Versions": list(versions), "DeleteMarkers": list(markers),
                  "IsTruncated": truncated}
    page.update(markers_kw)
    return page


# ------------------------------------------------------------- happy paths --
def test_an_unversioned_object_is_erased_in_one_pass(client):
    """An unversioned bucket returns the object with VersionId "null", and
    deleting that version id removes it permanently — so the same code path
    covers both bucket types with no special case."""
    store = S3ObjectStorage(client)
    with Stubber(client) as stub:
        stub.add_response("list_object_versions", _listing(_versions("null")),
                          {"Bucket": BUCKET, "Prefix": KEY})
        stub.add_response(
            "delete_objects", {"Deleted": [{"Key": KEY, "VersionId": "null"}]},
            {"Bucket": BUCKET,
             "Delete": {"Objects": [{"Key": KEY, "VersionId": "null"}], "Quiet": False}},
        )
        stub.add_response("list_object_versions", _listing(),
                          {"Bucket": BUCKET, "Prefix": KEY})
        assert store.hard_erase(BUCKET, KEY) is DeleteOutcome.DELETED
        stub.assert_no_pending_responses()


def test_every_version_and_marker_removed_is_an_erasure(client):
    """NON-VACUITY (C): the positive case the other two are measured against.

    Three historical versions and two delete markers, all removed, listing
    empty afterwards.
    """
    store = S3ObjectStorage(client)
    versions = _versions("v1", "v2", "v3")
    markers = _versions("m1", "m2")
    with Stubber(client) as stub:
        stub.add_response("list_object_versions", _listing(versions, markers),
                          {"Bucket": BUCKET, "Prefix": KEY})
        stub.add_response(
            "delete_objects", {"Deleted": versions + markers},
            {"Bucket": BUCKET,
             "Delete": {"Objects": versions + markers, "Quiet": False}},
        )
        stub.add_response("list_object_versions", _listing(),
                          {"Bucket": BUCKET, "Prefix": KEY})
        assert store.hard_erase(BUCKET, KEY) is DeleteOutcome.DELETED


def test_an_object_with_no_versions_at_all_converges(client):
    """Nothing to erase is terminal success — a retry that finds the work done
    must converge, or the lifecycle never finishes."""
    store = S3ObjectStorage(client)
    with Stubber(client) as stub:
        stub.add_response("list_object_versions", _listing(),
                          {"Bucket": BUCKET, "Prefix": KEY})
        assert store.hard_erase(BUCKET, KEY) is DeleteOutcome.ALREADY_ABSENT


# ------------------------------------------------------------ non-vacuity --
def test_a_delete_marker_alone_is_not_an_erasure(client):
    """NON-VACUITY (A): the exact state B2 refused to call erasure.

    A delete marker on top, two historical versions underneath, and the marker
    removed. The current version is 'restored' rather than erased — the bytes
    are readable again — so this must not report DELETED.
    """
    store = S3ObjectStorage(client)
    survivors = _versions("v1", "v2")
    with Stubber(client) as stub:
        stub.add_response(
            "list_object_versions", _listing(survivors, _versions("m1")),
            {"Bucket": BUCKET, "Prefix": KEY})
        stub.add_response(
            "delete_objects", {"Deleted": _versions("m1")},
            {"Bucket": BUCKET,
             "Delete": {"Objects": survivors + _versions("m1"), "Quiet": False}},
        )
        # The provider still holds both historical versions.
        stub.add_response("list_object_versions", _listing(survivors),
                          {"Bucket": BUCKET, "Prefix": KEY})
        outcome = store.hard_erase(BUCKET, KEY)
    assert outcome not in SUCCESS, (
        "a key whose historical versions are still readable was reported as "
        "erased; the privacy phase would finalize the purge over them"
    )
    assert outcome is DeleteOutcome.RETRYABLE_FAILURE


def test_one_surviving_historical_version_is_not_an_erasure(client):
    """NON-VACUITY (B): three versions, two removed, one left.

    The near-miss is the dangerous case — almost all of a customer's document
    being gone reads like success in every metric except the one that matters.
    """
    store = S3ObjectStorage(client)
    everything = _versions("v1", "v2", "v3")
    with Stubber(client) as stub:
        stub.add_response("list_object_versions", _listing(everything),
                          {"Bucket": BUCKET, "Prefix": KEY})
        stub.add_response(
            "delete_objects",
            {"Deleted": _versions("v1", "v2"),
             "Errors": [{"Key": KEY, "VersionId": "v3", "Code": "InternalError"}]},
            {"Bucket": BUCKET, "Delete": {"Objects": everything, "Quiet": False}},
        )
        stub.add_response("list_object_versions", _listing(_versions("v3")),
                          {"Bucket": BUCKET, "Prefix": KEY})
        outcome = store.hard_erase(BUCKET, KEY)
    assert outcome not in SUCCESS
    assert outcome is DeleteOutcome.RETRYABLE_FAILURE


def test_a_permanent_batch_error_is_permanent(client):
    """AccessDenied on a version will not fix itself on the next run."""
    store = S3ObjectStorage(client)
    everything = _versions("v1", "v2")
    with Stubber(client) as stub:
        stub.add_response("list_object_versions", _listing(everything),
                          {"Bucket": BUCKET, "Prefix": KEY})
        stub.add_response(
            "delete_objects",
            {"Deleted": _versions("v1"),
             "Errors": [{"Key": KEY, "VersionId": "v2", "Code": "AccessDenied"}]},
            {"Bucket": BUCKET, "Delete": {"Objects": everything, "Quiet": False}},
        )
        stub.add_response("list_object_versions", _listing(_versions("v2")),
                          {"Bucket": BUCKET, "Prefix": KEY})
        assert store.hard_erase(BUCKET, KEY) is DeleteOutcome.PERMANENT_FAILURE


def test_a_reported_error_is_overruled_by_an_empty_listing(client):
    """The confirmation is the ground truth, in both directions.

    A per-entry error whose version is nevertheless gone by the time the
    provider is asked is not a failure — refusing to converge there would
    strand a lifecycle over a version that no longer exists.
    """
    store = S3ObjectStorage(client)
    everything = _versions("v1")
    with Stubber(client) as stub:
        stub.add_response("list_object_versions", _listing(everything),
                          {"Bucket": BUCKET, "Prefix": KEY})
        stub.add_response(
            "delete_objects",
            {"Errors": [{"Key": KEY, "VersionId": "v1", "Code": "InternalError"}]},
            {"Bucket": BUCKET, "Delete": {"Objects": everything, "Quiet": False}},
        )
        stub.add_response("list_object_versions", _listing(),
                          {"Bucket": BUCKET, "Prefix": KEY})
        assert store.hard_erase(BUCKET, KEY) is DeleteOutcome.DELETED


# --------------------------------------------------------------- isolation --
def test_a_sibling_key_under_the_same_prefix_is_never_touched(client):
    """`list_object_versions` takes a PREFIX, not a key.

    `.../doc` also returns `.../doc2` and `.../doc/child`. Deleting what the
    listing returned, unfiltered, would erase another customer's document
    because its key happened to sort underneath this one.
    """
    store = S3ObjectStorage(client)
    sibling = f"{KEY}2"
    child = f"{KEY}/thumbnail"
    with Stubber(client) as stub:
        stub.add_response(
            "list_object_versions",
            _listing(
                _versions("mine") + _versions("theirs", key=sibling)
                + _versions("childv", key=child)
            ),
            {"Bucket": BUCKET, "Prefix": KEY},
        )
        # Exactly one entry — the sibling and the child must not appear.
        stub.add_response(
            "delete_objects", {"Deleted": _versions("mine")},
            {"Bucket": BUCKET,
             "Delete": {"Objects": _versions("mine"), "Quiet": False}},
        )
        stub.add_response("list_object_versions", _listing(),
                          {"Bucket": BUCKET, "Prefix": KEY})
        assert store.hard_erase(BUCKET, KEY) is DeleteOutcome.DELETED
        stub.assert_no_pending_responses()


def test_a_sibling_left_in_the_confirmation_does_not_block_convergence(client):
    """The same filter on the way out. A sibling still present is not this
    key's problem, and treating it as one would hang the lifecycle forever on
    an object it must not delete."""
    store = S3ObjectStorage(client)
    sibling = f"{KEY}2"
    with Stubber(client) as stub:
        stub.add_response("list_object_versions", _listing(_versions("mine")),
                          {"Bucket": BUCKET, "Prefix": KEY})
        stub.add_response(
            "delete_objects", {"Deleted": _versions("mine")},
            {"Bucket": BUCKET,
             "Delete": {"Objects": _versions("mine"), "Quiet": False}},
        )
        stub.add_response(
            "list_object_versions", _listing(_versions("theirs", key=sibling)),
            {"Bucket": BUCKET, "Prefix": KEY})
        assert store.hard_erase(BUCKET, KEY) is DeleteOutcome.DELETED


# -------------------------------------------------------------- pagination --
def test_versions_beyond_the_first_page_are_erased_too(client):
    """A truncated listing means the rest is on another page.

    Stopping at page one would delete most of a document's history and report
    success, which is the near-miss this whole file exists to make impossible.
    """
    store = S3ObjectStorage(client)
    page1, page2 = _versions("v1", "v2"), _versions("v3")
    with Stubber(client) as stub:
        stub.add_response(
            "list_object_versions",
            _listing(page1, truncated=True,
                     NextKeyMarker=KEY, NextVersionIdMarker="v2"),
            {"Bucket": BUCKET, "Prefix": KEY},
        )
        stub.add_response(
            "list_object_versions", _listing(page2),
            {"Bucket": BUCKET, "Prefix": KEY,
             "KeyMarker": KEY, "VersionIdMarker": "v2"},
        )
        stub.add_response(
            "delete_objects", {"Deleted": page1 + page2},
            {"Bucket": BUCKET,
             "Delete": {"Objects": page1 + page2, "Quiet": False}},
        )
        stub.add_response("list_object_versions", _listing(),
                          {"Bucket": BUCKET, "Prefix": KEY})
        assert store.hard_erase(BUCKET, KEY) is DeleteOutcome.DELETED
        stub.assert_no_pending_responses()


def test_more_versions_than_one_batch_are_split(client):
    """S3 accepts 1000 version ids per DeleteObjects call."""
    store = S3ObjectStorage(client)
    many = _versions(*(f"v{i}" for i in range(1001)))
    with Stubber(client) as stub:
        stub.add_response("list_object_versions", _listing(many),
                          {"Bucket": BUCKET, "Prefix": KEY})
        stub.add_response(
            "delete_objects", {"Deleted": many[:1000]},
            {"Bucket": BUCKET, "Delete": {"Objects": many[:1000], "Quiet": False}},
        )
        stub.add_response(
            "delete_objects", {"Deleted": many[1000:]},
            {"Bucket": BUCKET, "Delete": {"Objects": many[1000:], "Quiet": False}},
        )
        stub.add_response("list_object_versions", _listing(),
                          {"Bucket": BUCKET, "Prefix": KEY})
        assert store.hard_erase(BUCKET, KEY) is DeleteOutcome.DELETED
        stub.assert_no_pending_responses()


# ----------------------------------------------------------- provider faults --
def test_a_failed_enumeration_erases_nothing_and_reports_failure(client):
    """No listing means no version ids, and deleting nothing is not erasure."""
    store = S3ObjectStorage(client)
    with Stubber(client) as stub:
        stub.add_client_error("list_object_versions",
                              service_error_code="InternalError")
        assert store.hard_erase(BUCKET, KEY) is DeleteOutcome.RETRYABLE_FAILURE


def test_enumeration_denied_is_permanent(client):
    store = S3ObjectStorage(client)
    with Stubber(client) as stub:
        stub.add_client_error("list_object_versions",
                              service_error_code="AccessDenied")
        assert store.hard_erase(BUCKET, KEY) is DeleteOutcome.PERMANENT_FAILURE


def test_a_missing_bucket_converges(client):
    store = S3ObjectStorage(client)
    with Stubber(client) as stub:
        stub.add_client_error("list_object_versions",
                              service_error_code="NoSuchBucket")
        assert store.hard_erase(BUCKET, KEY) is DeleteOutcome.ALREADY_ABSENT


def test_a_timeout_mid_erasure_is_retryable(client):
    """A transport failure carries no provider code, and its absence must not
    be read as success."""
    store = S3ObjectStorage(client)
    calls = {"n": 0}
    real_list = client.list_object_versions

    def _list(**kwargs):
        calls["n"] += 1
        return {"Versions": _versions("v1"), "DeleteMarkers": [],
                "IsTruncated": False}

    def _timeout(**kwargs):
        raise ConnectTimeoutError(endpoint_url="https://s3.ca-central-1.amazonaws.com")

    client.list_object_versions = _list
    client.delete_objects = _timeout
    try:
        assert store.hard_erase(BUCKET, KEY) is DeleteOutcome.RETRYABLE_FAILURE
    finally:
        client.list_object_versions = real_list


def test_an_unconfirmable_erasure_is_not_an_erasure(client):
    """The deletes succeeded and the confirming listing did not answer.

    The versions are very probably gone. "Very probably gone" is not what a
    privacy ledger records, so this stays incomplete and the next run confirms
    it — an idempotent second pass costs one listing.
    """
    store = S3ObjectStorage(client)
    with Stubber(client) as stub:
        stub.add_response("list_object_versions", _listing(_versions("v1")),
                          {"Bucket": BUCKET, "Prefix": KEY})
        stub.add_response(
            "delete_objects", {"Deleted": _versions("v1")},
            {"Bucket": BUCKET,
             "Delete": {"Objects": _versions("v1"), "Quiet": False}},
        )
        stub.add_client_error("list_object_versions",
                              service_error_code="InternalError")
        assert store.hard_erase(BUCKET, KEY) is DeleteOutcome.RETRYABLE_FAILURE


@pytest.mark.parametrize(
    "code", ["SomeCodeNobodyHasSeen", "ThrottledLikeThis", "", "TeapotError"],
)
def test_an_unrecognised_error_never_reports_erasure(client, code):
    """The mapping rule, carried over from the single-object adapter.

    No adapter can know every code a provider or an S3-compatible store might
    return. What it can guarantee is the direction of the guess: an unknown
    condition is not evidence that a customer's document was destroyed.
    """
    store = S3ObjectStorage(client)
    with Stubber(client) as stub:
        stub.add_client_error("list_object_versions", service_error_code=code)
        outcome = store.hard_erase(BUCKET, KEY)
    assert outcome is DeleteOutcome.RETRYABLE_FAILURE
    assert outcome not in SUCCESS


def test_no_provider_detail_escapes_into_the_outcome(client):
    store = S3ObjectStorage(client)
    with Stubber(client) as stub:
        stub.add_client_error(
            "list_object_versions",
            service_error_code="AccessDenied",
            service_message=(
                "User: arn:aws:sts::123456789012:assumed-role/onyx-api is not "
                "authorized to perform s3:ListBucketVersions on "
                "arn:aws:s3:::onyx-prod-documents"
            ),
        )
        outcome = store.hard_erase(BUCKET, KEY)
    rendered = f"{outcome!r} {outcome.value}"
    for leaked in ("123456789012", "onyx-prod-documents", "assumed-role"):
        assert leaked not in rendered


def test_hard_erase_never_raises_a_provider_exception(client):
    """The port's contract: an adapter translates, it does not propagate."""
    store = S3ObjectStorage(client)
    for code in ("AccessDenied", "NoSuchBucket", "SlowDown", "InternalError"):
        with Stubber(client) as stub:
            stub.add_client_error("list_object_versions", service_error_code=code)
            outcome = store.hard_erase(BUCKET, KEY)
        assert isinstance(outcome, DeleteOutcome)


def test_a_delete_marker_is_never_created_on_the_way_to_erasing(client):
    """Enumerate-first, not delete-then-clean.

    Calling `delete_object` first would write a delete marker on a versioned
    bucket — manufacturing one more thing to erase in order to find out that
    erasing was needed. The assertion is that the plain single-object delete is
    never called at all.
    """
    store = S3ObjectStorage(client)
    called: list[str] = []
    client.delete_object = lambda **kw: called.append("delete_object")

    with Stubber(client) as stub:
        stub.add_response("list_object_versions", _listing(_versions("v1")),
                          {"Bucket": BUCKET, "Prefix": KEY})
        stub.add_response(
            "delete_objects", {"Deleted": _versions("v1")},
            {"Bucket": BUCKET,
             "Delete": {"Objects": _versions("v1"), "Quiet": False}},
        )
        stub.add_response("list_object_versions", _listing(),
                          {"Bucket": BUCKET, "Prefix": KEY})
        store.hard_erase(BUCKET, KEY)

    assert called == [], "hard_erase wrote a delete marker on its way to erasing"
