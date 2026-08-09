"""ObjectStorage adapters.

`LocalObjectStorage` is an in-memory fake for dev/tests (also stashes the text
payload so the structured/OCR pipeline can run without a real bucket). A boto3
S3 adapter drops in behind the same `ObjectStorage` port for production.

THE SIZE BOUND IS PART OF THE PORT, NOT A COMMENT
-------------------------------------------------
Bytes never transit the API — a caller is handed a presigned URL and uploads
straight to the bucket — so the API has no opportunity to count them. What it
has is a `byte_size` the CLIENT declares, and a declaration is not a bound: a
caller that wants to store 30 MB under a 25 MB limit simply declares 1 MB.

Refusing an oversized DECLARATION is still worth doing (it fails fast and
cheaply, and it catches the honest client), but on its own it is a bound in
name only. The real one has to live wherever the bytes actually land. So
`presign_put` takes `max_bytes` and the store REFUSES a larger object, which
is the same contract S3 gives through a POST policy's `content-length-range`
condition — see the production adapter sketch below.
"""
from __future__ import annotations

from app.core.exceptions import ValidationError
from app.domain.ports import DeleteOutcome

_FAKE_BLOBS: dict[str, bytes] = {}
#: Per-key ceiling recorded when the upload was authorized.
_FAKE_LIMITS: dict[str, int] = {}


class ObjectTooLarge(ValidationError):
    """An upload exceeded the ceiling its presigned authorization carried.

    A `ValidationError` rather than an admission rejection: the caller is not
    being throttled, it sent something the platform will not store. Retrying
    later would not help, so a 429 with a Retry-After would be a lie.
    """


class LocalObjectStorage:
    """In-memory object store; presign URLs are opaque local references."""

    def presign_put(
        self, bucket: str, key: str, content_type: str, *, max_bytes: int
    ) -> str:
        """Authorize one upload, up to `max_bytes`.

        `max_bytes` is required, with no default. A default would mean a call
        site that forgot the bound still compiled and still looked correct, and
        the one place the limit is actually enforceable would be the one place
        it silently was not.
        """
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        _FAKE_LIMITS[f"{bucket}/{key}"] = max_bytes
        return f"local://{bucket}/{key}?op=put&max_bytes={max_bytes}"

    def presign_get(self, bucket: str, key: str) -> str:
        return f"local://{bucket}/{key}?op=get"

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


# Production adapter (requires boto3 + real credentials). Note that the size
# bound is a POLICY CONDITION, not something this process checks — S3 rejects
# the upload itself, so the ceiling holds even though the bytes never touch the
# application:
#
# class S3ObjectStorage:
#     def __init__(self, client, url_ttl=900): self.c, self.ttl = client, url_ttl
#     def presign_put(self, bucket, key, content_type, *, max_bytes):
#         post = self.c.generate_presigned_post(
#             Bucket=bucket, Key=key, ExpiresIn=self.ttl,
#             Fields={"Content-Type": content_type},
#             Conditions=[
#                 {"Content-Type": content_type},
#                 ["content-length-range", 1, max_bytes],
#             ])
#         return post["url"]
#     def presign_get(self, bucket, key):
#         return self.c.generate_presigned_url(
#             "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=self.ttl)
#     def delete(self, bucket, key):
#         # S3 DeleteObject is already idempotent — deleting a missing key
#         # returns 204 — so ALREADY_ABSENT cannot be distinguished without a
#         # preceding HEAD, and paying a round trip to learn it is not worth it.
#         # Report DELETED and let the lifecycle converge either way.
#         #
#         # VERSIONING: on a versioned bucket this writes a delete marker and the
#         # previous versions REMAIN. That is not erasure. Either versioning is
#         # off, or this must enumerate and delete every version. The repository
#         # cannot see the deployed bucket configuration —
#         # DEPLOYMENT_REVIEW_REQUIRED, recorded as PD-10.
#         try:
#             self.c.delete_object(Bucket=bucket, Key=key)
#             return DeleteOutcome.DELETED
#         except ClientError as exc:
#             # A closed code, never the provider message: Entry 11A proved
#             # exception text carries values a privacy store must not keep.
#             code = exc.response.get("Error", {}).get("Code", "")
#             if code in ("NoSuchKey", "NoSuchBucket"):
#                 return DeleteOutcome.ALREADY_ABSENT
#             if code in ("AccessDenied", "InvalidBucketName"):
#                 return DeleteOutcome.PERMANENT_FAILURE
#             return DeleteOutcome.RETRYABLE_FAILURE


def get_object_storage() -> LocalObjectStorage:
    # Swap for S3ObjectStorage when settings.s3_endpoint_url + creds are present.
    return LocalObjectStorage()
