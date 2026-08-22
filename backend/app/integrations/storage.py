"""ObjectStorage adapters, and the choice between them.

`LocalObjectStorage` is an in-memory store for development and the test suite
(it also stashes the payload so the structured/OCR pipeline can run without a
bucket). `S3ObjectStorage` is the production adapter. `get_object_storage()`
picks between them from configuration and REFUSES the local one in production.

WHY THE CHOICE IS A CHOICE NOW
------------------------------
It used to not be. `get_object_storage()` returned `LocalObjectStorage`
unconditionally and the S3 adapter was a comment, so a production deployment
would have served customer documents out of one process's heap. The
availability consequences are obvious. The privacy consequence is the one that
matters: `AccountLifecycleService`'s document phase deletes through this port
and finalizes a purge only when the outcome is `DELETED` or `ALREADY_ABSENT`.
Against an in-memory dict every delete succeeds, so the phase would have
recorded erasure of objects that were never durably stored — a truthful-looking
record of something that never happened.

THE SIZE BOUND IS PART OF THE PORT, NOT A COMMENT
-------------------------------------------------
Bytes never transit the API — a caller is handed an upload authorization and
uploads straight to the bucket — so the API has no opportunity to count them.
What it has is a `byte_size` the CLIENT declares, and a declaration is not a
bound: a caller that wants to store 30 MB under a 25 MB limit simply declares
1 MB.

Refusing an oversized DECLARATION is still worth doing (it fails fast and
cheaply, and it catches the honest client), but on its own it is a bound in
name only. The real one has to live wherever the bytes actually land. So
`presign_put` takes `max_bytes` and the store REFUSES a larger object.

That requirement is why an upload authorization is a URL **and fields** rather
than a bare URL. S3 can only enforce a size ceiling through a POST policy's
`content-length-range` condition, and the policy travels in the form fields. An
earlier sketch of this adapter called `generate_presigned_post` and returned
`post["url"]` alone — which discards the policy, so the ceiling the port
promises would have quietly stopped existing the moment it met a real bucket.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.core.exceptions import ValidationError
from app.core.logging import get_logger
from app.domain.ports import DeleteOutcome, UploadAuthorization

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.core.config import Settings

log = get_logger(__name__)

_FAKE_BLOBS: dict[str, bytes] = {}
#: Per-key ceiling recorded when the upload was authorized.
_FAKE_LIMITS: dict[str, int] = {}


class ObjectTooLarge(ValidationError):
    """An upload exceeded the ceiling its authorization carried.

    A `ValidationError` rather than an admission rejection: the caller is not
    being throttled, it sent something the platform will not store. Retrying
    later would not help, so a 429 with a Retry-After would be a lie.
    """


class StorageMisconfigured(RuntimeError):
    """The configured storage provider cannot be built.

    Raised at construction rather than at first use, so a deployment that is
    wrong is wrong immediately and visibly instead of at the moment a customer
    uploads their first document.
    """


class LocalObjectStorage:
    """In-memory object store; upload URLs are opaque local references."""

    def presign_put(
        self, bucket: str, key: str, content_type: str, *, max_bytes: int
    ) -> UploadAuthorization:
        """Authorize one upload, up to `max_bytes`.

        `max_bytes` is required, with no default. A default would mean a call
        site that forgot the bound still compiled and still looked correct, and
        the one place the limit is actually enforceable would be the one place
        it silently was not.
        """
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        _FAKE_LIMITS[f"{bucket}/{key}"] = max_bytes
        return UploadAuthorization(
            url=f"local://{bucket}/{key}?op=put&max_bytes={max_bytes}"
        )

    def put(self, bucket: str, key: str, data: bytes) -> None:
        """Store the bytes, or refuse them.

        The check is against what ARRIVED, not against anything the caller said
        about it earlier. That is the entire point: a declaration can lie and a
        length cannot.
        """
        path = f"{bucket}/{key}"
        limit = _FAKE_LIMITS.get(path)
        if limit is not None and len(data) > limit:
            # The limit is named; the actual size is not echoed back. A caller
            # that can probe for the exact accepted size learns the platform's
            # configuration one binary search at a time.
            raise ObjectTooLarge(
                f"object exceeds the {limit} byte maximum for this upload"
            )
        _FAKE_BLOBS[path] = data

    def get(self, bucket: str, key: str) -> bytes:
        return _FAKE_BLOBS.get(f"{bucket}/{key}", b"")

    def delete(self, bucket: str, key: str) -> DeleteOutcome:
        """Remove one object, idempotently (PD-8).

        The distinction between DELETED and ALREADY_ABSENT is real information
        — it tells an operator whether a retry did the work or found it done —
        but both let the lifecycle advance. A deletion phase that failed
        because the object was already gone would never converge, and the one
        thing worse than a binary that outlives its document is a lifecycle
        that can never finish removing it.

        The per-key upload ceiling goes too. It is authorization state for an
        upload that can no longer happen, and leaving it behind would let a
        replayed presign inherit a bound from a deleted object.
        """
        path = f"{bucket}/{key}"
        _FAKE_LIMITS.pop(path, None)
        if _FAKE_BLOBS.pop(path, None) is None:
            return DeleteOutcome.ALREADY_ABSENT
        return DeleteOutcome.DELETED


# --------------------------------------------------------------------------- #
# Production
# --------------------------------------------------------------------------- #

#: Provider codes meaning "the object is not there". On S3 a DELETE of a
#: missing key returns 204 rather than one of these, so in practice these
#: surface from GET; they are mapped for DELETE too because an S3-compatible
#: store that answers differently must still converge rather than retry forever.
_ABSENT_CODES = frozenset({"NoSuchKey", "NoSuchBucket", "404", "NotFound"})

#: Codes where retrying changes nothing until a human changes something. Kept
#: deliberately short: anything not named here is treated as retryable, because
#: the cost of retrying a permanent failure is a log line and the cost of
#: giving up on a transient one is a document that never gets erased.
_PERMANENT_CODES = frozenset({
    "AccessDenied",
    "AllAccessDisabled",
    "AccountProblem",
    "InvalidAccessKeyId",
    "SignatureDoesNotMatch",
    "InvalidBucketName",
    "InvalidObjectState",
    "MethodNotAllowed",
    "UnauthorizedAccess",
})


def _error_code(exc: Exception) -> str:
    """The provider's closed error code, or "" for a transport-level failure.

    Only the CODE is ever read. `str(exc)` on a botocore error carries the
    bucket, the endpoint, sometimes a request id and an account hint — Entry
    11A established that provider exception text has no business reaching a
    privacy store or an application error, and a lifecycle keyed on a message
    string cannot be reasoned about.
    """
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            code = error.get("Code")
            if isinstance(code, str):
                return code
    return ""


class S3ObjectStorage:
    """AWS S3 (and S3-compatible) object storage.

    Constructed with a client rather than building one, so a test drives it
    through `botocore.stub.Stubber` against the real client contract instead of
    a hand-written fake that agrees with whatever the adapter happens to do.
    """

    #: Upload and download authorizations are short-lived. Fifteen minutes is
    #: long enough for a slow connection to finish one document and short
    #: enough that a leaked URL is a narrow window rather than a standing grant.
    URL_TTL_SECONDS = 900

    def __init__(
        self,
        client: Any,
        *,
        sse_algorithm: str | None = None,
        sse_kms_key_id: str | None = None,
        url_ttl_seconds: int = URL_TTL_SECONDS,
    ) -> None:
        self._c = client
        self._sse = sse_algorithm
        self._kms = sse_kms_key_id
        self._ttl = url_ttl_seconds

    # -- encryption ------------------------------------------------------- #
    def _encryption(self) -> dict[str, str]:
        """SSE parameters for a PUT, if this deployment pins one.

        Unset is the normal arrangement: the bucket carries default encryption
        and every PUT inherits it. Pinning it here is for a bucket whose
        default nobody can vouch for.
        """
        if not self._sse:
            return {}
        params = {"ServerSideEncryption": self._sse}
        if self._kms:
            params["SSEKMSKeyId"] = self._kms
        return params

    # -- upload ----------------------------------------------------------- #
    def presign_put(
        self, bucket: str, key: str, content_type: str, *, max_bytes: int
    ) -> UploadAuthorization:
        """A single-object POST policy carrying the size ceiling.

        `content-length-range` is the whole reason this is a POST rather than a
        presigned PUT: a presigned PUT URL cannot express a maximum size, so the
        ceiling would exist only in the docstring. S3 rejects an oversized body
        itself, which is what makes the bound real even though the bytes never
        pass through this process.

        NO ACL IS SET. The bucket's own policy governs, and an object that must
        stay private is not the place to start naming access-control grants —
        `public-read` is one typo away and there is no reason to be typing in
        that vocabulary at all.
        """
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        conditions: list[Any] = [
            {"Content-Type": content_type},
            ["content-length-range", 1, max_bytes],
        ]
        fields = {"Content-Type": content_type}
        encryption = self._encryption()
        for name, value in encryption.items():
            header = (
                "x-amz-server-side-encryption"
                if name == "ServerSideEncryption"
                else "x-amz-server-side-encryption-aws-kms-key-id"
            )
            fields[header] = value
            conditions.append({header: value})
        post = self._c.generate_presigned_post(
            Bucket=bucket,
            Key=key,
            Fields=fields,
            Conditions=conditions,
            ExpiresIn=self._ttl,
        )
        return UploadAuthorization(
            url=post["url"], fields=dict(post.get("fields") or {})
        )

    # -- bytes ------------------------------------------------------------ #
    def put(self, bucket: str, key: str, data: bytes) -> None:
        try:
            self._c.put_object(Bucket=bucket, Key=key, Body=data, **self._encryption())
        except Exception as exc:  # noqa: BLE001 - logged as a code, then re-raised
            # The CODE and nothing else. Not the key (it identifies one
            # customer's document), not the bucket, not the provider message.
            # The exception itself still propagates: `put` has no closed outcome
            # vocabulary the way `delete` does, and inventing one here would
            # change control flow for callers this entry has no business
            # touching. The API's error handler is what keeps the provider text
            # away from a customer.
            log.warning(
                "object_storage_put_failed",
                provider_code=_error_code(exc) or "TRANSPORT",
            )
            raise

    def get(self, bucket: str, key: str) -> bytes:
        """The object's bytes, or empty when it is not there.

        Empty-for-missing matches `LocalObjectStorage` deliberately: two
        adapters behind one port that disagree about a missing object would
        make every caller's behaviour depend on which one is installed, and
        that is a worse defect than the weak signal. Callers that need to tell
        empty from absent do not exist today; if one appears, the port grows an
        `exists` and both adapters implement it together.
        """
        try:
            response = self._c.get_object(Bucket=bucket, Key=key)
        except Exception as exc:  # noqa: BLE001 - translated below, never leaked
            code = _error_code(exc)
            if code in _ABSENT_CODES:
                return b""
            log.warning("object_storage_read_failed", provider_code=code or "TRANSPORT")
            raise
        body = response["Body"]
        try:
            return bytes(body.read())
        finally:
            close = getattr(body, "close", None)
            if close is not None:
                close()

    # -- erasure ---------------------------------------------------------- #
    def delete(self, bucket: str, key: str) -> DeleteOutcome:
        """Remove one object and report, in the port's closed vocabulary, what
        actually happened.

        THE RULE THIS FUNCTION EXISTS FOR: no unrecognised condition may ever
        produce a success outcome. `AccountLifecycleService` finalizes a purge
        on `DELETED` or `ALREADY_ABSENT` and on nothing else, so a mapping that
        guessed "probably fine" for an error it did not know would convert an
        unknown provider state into a permanent, audited claim that a
        customer's document had been erased. Everything unrecognised is
        therefore `RETRYABLE_FAILURE`: the phase stays incomplete, the object
        reference is kept, and the next run tries again.

        S3 RETURNS 204 FOR A MISSING KEY, so `DELETED` and `ALREADY_ABSENT`
        cannot be distinguished without a preceding HEAD. Both are terminal
        success for the lifecycle, and paying a round trip per object to
        colour an operator's log line is not worth it — see
        REMOTE_DELETE_CONFIRMATION_MODEL in the entry report.

        A DELETE MARKER IS NOT AN ERASURE. On a versioned bucket
        `delete_object` does not remove anything: it writes a marker and every
        previous version remains readable by anyone who can name it. S3 says so
        in the response, `DeleteMarker: true`, at no extra cost — so the one
        configuration where "the provider said the delete succeeded" and "the
        customer's bytes are gone" come apart is detected from the response we
        already have, and reported as a failure rather than as erasure. That is
        PD-10, which until now was a comment warning that this could happen.
        """
        try:
            response = self._c.delete_object(Bucket=bucket, Key=key)
        except Exception as exc:  # noqa: BLE001 - translated, never leaked
            code = _error_code(exc)
            if code in _ABSENT_CODES:
                return DeleteOutcome.ALREADY_ABSENT
            outcome = (
                DeleteOutcome.PERMANENT_FAILURE
                if code in _PERMANENT_CODES
                else DeleteOutcome.RETRYABLE_FAILURE
            )
            # The CODE, never the message, and never the key. The bucket and
            # key together identify one customer's document; the outcome and
            # the provider code are what an operator needs.
            log.warning(
                "object_storage_delete_failed",
                outcome=outcome.value,
                provider_code=code or "TRANSPORT",
            )
            return outcome

        if isinstance(response, dict) and response.get("DeleteMarker"):
            log.warning(
                "object_storage_delete_left_a_version",
                outcome=DeleteOutcome.PERMANENT_FAILURE.value,
                provider_code="VERSIONED_BUCKET",
            )
            return DeleteOutcome.PERMANENT_FAILURE
        return DeleteOutcome.DELETED


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #

LOCAL_PROVIDER = "local"
S3_PROVIDER = "s3"
STORAGE_PROVIDERS = frozenset({LOCAL_PROVIDER, S3_PROVIDER})


def build_object_storage(settings: Settings) -> LocalObjectStorage | S3ObjectStorage:
    """The store this configuration asks for, or an exception.

    THERE IS NO FALLBACK PATH. Not "try S3, fall back to local" — that pattern
    turns a credential problem into a silent downgrade, and the downgrade here
    is "customer documents are now in a dict that a restart empties". A
    deployment that cannot build its configured store must fail, loudly, at the
    point of construction.

    The production refusal is duplicated from `Settings._production_storage_is_real`
    on purpose. That validator protects a process that builds its settings the
    normal way; this one protects a process that hands in a `Settings` built
    some other way — a script, a fixture, a future caller. Two cheap checks, and
    neither is the only thing standing between production and an in-memory
    store.
    """
    provider = settings.storage_provider
    if provider not in STORAGE_PROVIDERS:
        raise StorageMisconfigured(
            f"unknown storage provider {provider!r}; expected one of "
            f"{sorted(STORAGE_PROVIDERS)}"
        )

    if provider == LOCAL_PROVIDER:
        if settings.is_production:
            raise StorageMisconfigured(
                "the in-memory object store must never serve production; "
                "customer documents would not survive a restart and a deletion "
                "phase would record erasure that never reached durable storage"
            )
        return LocalObjectStorage()

    # boto3 is imported HERE rather than at module import. The module is pulled
    # in by the domain services and by tests that never touch S3, and a
    # top-level import would make the whole application refuse to start if the
    # dependency were ever absent from an environment that does not need it.
    import boto3
    from botocore.config import Config

    if settings.is_production and not settings.s3_region:
        raise StorageMisconfigured("ONYX_S3_REGION is required for the s3 provider")

    client = boto3.client(
        "s3",
        region_name=settings.s3_region,
        endpoint_url=settings.s3_endpoint_url,
        # Retries are the provider's job, bounded. Beyond this the adapter
        # reports RETRYABLE_FAILURE and the lifecycle schedules another attempt,
        # which is a better place to wait than inside a request.
        config=Config(retries={"max_attempts": 3, "mode": "standard"}),
    )
    return S3ObjectStorage(
        client,
        sse_algorithm=settings.s3_sse_algorithm,
        sse_kms_key_id=settings.s3_sse_kms_key_id,
    )


def get_object_storage() -> LocalObjectStorage | S3ObjectStorage:
    from app.core.config import get_settings

    return build_object_storage(get_settings())
