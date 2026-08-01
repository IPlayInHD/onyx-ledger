"""ObjectStorage adapters.

`LocalObjectStorage` is an in-memory fake for dev/tests (also stashes the text
payload so the structured/OCR pipeline can run without a real bucket). A boto3
S3 adapter drops in behind the same `ObjectStorage` port for production.
"""
from __future__ import annotations

_FAKE_BLOBS: dict[str, bytes] = {}


class LocalObjectStorage:
    """In-memory object store; presign URLs are opaque local references."""

    def presign_put(self, bucket: str, key: str, content_type: str) -> str:
        return f"local://{bucket}/{key}?op=put"

    def presign_get(self, bucket: str, key: str) -> str:
        return f"local://{bucket}/{key}?op=get"

    def put(self, bucket: str, key: str, data: bytes) -> None:
        _FAKE_BLOBS[f"{bucket}/{key}"] = data

    def get(self, bucket: str, key: str) -> bytes:
        return _FAKE_BLOBS.get(f"{bucket}/{key}", b"")


# Production adapter (requires boto3 + real credentials):
#
# class S3ObjectStorage:
#     def __init__(self, client, url_ttl=900): self.c, self.ttl = client, url_ttl
#     def presign_put(self, bucket, key, content_type):
#         return self.c.generate_presigned_url(
#             "put_object",
#             Params={"Bucket": bucket, "Key": key, "ContentType": content_type},
#             ExpiresIn=self.ttl)
#     def presign_get(self, bucket, key):
#         return self.c.generate_presigned_url(
#             "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=self.ttl)


def get_object_storage() -> LocalObjectStorage:
    # Swap for S3ObjectStorage when settings.s3_endpoint_url + creds are present.
    return LocalObjectStorage()
