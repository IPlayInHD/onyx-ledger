"""One description of what a production `Settings` requires.

WHY THIS IS A MODULE AND NOT THREE DICTS. Several security tests end with the
same control — "and a real production configuration IS accepted" — whose whole
job is to prove the guard above it is not passing because every production
Settings raises. That control breaks whenever production gains a NEW
requirement, and it has now broken three times:

  · Entry B2 made production refuse the in-memory object store, and the two
    secret guards in `test_admission_preauth` failed on their control line
    rather than on what they test.
  · B2A tightened it again.
  · B3 made production refuse the capture email provider, and this time it was
    four assertions in `test_object_storage_fail_closed` — a file about storage,
    failing on email.

Each time the fix was to copy the new fields into another dict. B2's own
comment predicted this: "the next requirement is added once, not hunted down in
two assertions that look unrelated to it." Three copies later, here is the one
place.

A test overrides the field it is about and inherits the rest:

    Settings(**production_settings(storage_provider="local"))   # expect refusal
    Settings(**production_settings())                           # expect success

The secrets are obvious fakes of legal length. They are not credentials and
must never be made to look like ones.
"""
from __future__ import annotations

from typing import Any

#: Every field production requires, at a value production would accept.
#: Extend this — and nothing else — when a new production guard lands.
PRODUCTION_BASELINE: dict[str, Any] = {
    "environment": "production",

    # --- secrets, each guarded against its compiled dev default -------------
    "jwt_secret": "j" * 48,
    "admission_identity_secret": "x" * 48,

    # --- object storage (B2) ------------------------------------------------
    "storage_provider": "s3",
    "s3_region": "ca-central-1",
    "s3_bucket_documents": "onyx-prod-documents",
    "s3_bucket_legislation": "onyx-prod-legislation",

    # --- transactional email (B3) -------------------------------------------
    "email_provider": "ses",
    "email_sender_address": "no-reply@onyx.example.ca",
    "ses_region": "ca-central-1",
    "app_public_url": "https://app.onyx.example.ca",
}


def production_settings(**overrides: Any) -> dict[str, Any]:
    """The baseline with `overrides` applied. `None` REMOVES a key.

    Removing rather than nulling matters for the "production requires X"
    tests: a field absent from the constructor falls back to its compiled
    default, which is the deployment mistake being guarded against — somebody
    who never set the variable, not somebody who set it to nothing.
    """
    config = dict(PRODUCTION_BASELINE)
    for key, value in overrides.items():
        if value is None:
            config.pop(key, None)
        else:
            config[key] = value
    return config
