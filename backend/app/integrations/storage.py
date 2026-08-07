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


def get_object_storage() -> LocalObjectStorage:
    # Swap for S3ObjectStorage when settings.s3_endpoint_url + creds are present.
    return LocalObjectStorage()
