"""Production must not be able to reach the in-memory object store.

THE DEFECT THIS FILE CLOSES. `get_object_storage()` returned
`LocalObjectStorage` unconditionally — two module-level dicts — and the S3
adapter was a commented-out sketch. The document API on top of it was, and is,
fully implemented: it validates the content type, enforces a size ceiling,
passes through admission control, checks ownership, and hands back an upload
permit. So a production deployment would not have failed. It would have worked,
visibly, while:

  * every document vanished on restart and was invisible to every other task;
  * `AccountLifecycleService`'s document phase popped a key out of a dict,
    received `DELETED`, and finalized the purge — recording erasure of an
    object that had never been durably stored anywhere.

The second is why these are security tests rather than configuration tests. A
privacy ledger that says "erased" about a store nobody wrote to is worse than
having no ledger.

Two independent guards, because they protect different callers: `Settings`
refuses the configuration, and `build_object_storage` refuses the construction.
A process that builds its settings the normal way hits the first; a script or a
fixture that hands in a `Settings` some other way hits the second.
"""
from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.integrations.storage import (
    LocalObjectStorage,
    S3ObjectStorage,
    StorageMisconfigured,
    build_object_storage,
)

PROD = {
    "environment": "production",
    "jwt_secret": "j" * 40,
    "admission_identity_secret": "a" * 40,
}
S3_PROD = PROD | {
    "storage_provider": "s3",
    "s3_region": "ca-central-1",
    "s3_bucket_documents": "onyx-prod-documents",
    "s3_bucket_legislation": "onyx-prod-legislation",
}


# ------------------------------------------------- guard one: configuration --
def test_production_refuses_the_in_memory_store():
    with pytest.raises(ValidationError) as caught:
        Settings(**PROD)
    assert "ONYX_STORAGE_PROVIDER" in str(caught.value)


def test_production_requires_an_explicit_region():
    with pytest.raises(ValidationError) as caught:
        Settings(**(PROD | {"storage_provider": "s3"}))
    assert "ONYX_S3_REGION" in str(caught.value)


@pytest.mark.parametrize(
    "omitted,expected",
    [("s3_bucket_documents", "ONYX_S3_BUCKET_DOCUMENTS"),
     ("s3_bucket_legislation", "ONYX_S3_BUCKET_LEGISLATION")],
)
def test_production_requires_each_bucket_to_be_named(omitted, expected):
    """The compiled default is the hazard, not an empty string.

    `onyx-documents` is a plausible bucket name, so a deployment that never set
    the variable would pass any non-empty check and then read and write a
    bucket nobody chose — or none.
    """
    config = dict(S3_PROD)
    del config[omitted]
    with pytest.raises(ValidationError) as caught:
        Settings(**config)
    assert expected in str(caught.value)


def test_a_fully_configured_production_is_accepted():
    """The guard must be satisfiable. A check nothing can pass is a check
    somebody will delete."""
    assert Settings(**S3_PROD).storage_provider == "s3"


def test_development_keeps_the_local_store_without_ceremony():
    settings = Settings(environment="development")
    assert settings.storage_provider == "local"
    assert isinstance(build_object_storage(settings), LocalObjectStorage)


# -------------------------------------------------- guard two: construction --
def test_the_factory_refuses_local_in_production_even_if_settings_did_not():
    """The second layer, exercised by bypassing the first.

    `model_construct` skips validation, which is precisely the situation this
    guard exists for: a caller that assembles a `Settings` some other way must
    not be able to hand production an in-memory store.
    """
    smuggled = Settings.model_construct(
        environment="production", storage_provider="local",
    )
    with pytest.raises(StorageMisconfigured) as caught:
        build_object_storage(smuggled)
    assert "in-memory" in str(caught.value)


def test_an_unknown_provider_is_refused_rather_than_guessed():
    settings = Settings(environment="development", storage_provider="gcs")
    with pytest.raises(StorageMisconfigured) as caught:
        build_object_storage(settings)
    assert "gcs" in str(caught.value)


def test_the_s3_provider_builds_the_s3_adapter():
    assert isinstance(build_object_storage(Settings(**S3_PROD)), S3ObjectStorage)


def test_there_is_no_fallback_from_a_broken_s3_to_local(monkeypatch):
    """NON-VACUITY (A): prove the absence of `try S3 / except: local`.

    The pattern is common and it is the one thing this entry must not contain:
    it converts a credential or endpoint problem into a silent downgrade, and
    the downgrade is "customer documents now live in a dict". So the client
    constructor is made to fail and the requirement is that the failure
    PROPAGATES — anything returned here would be the fallback.
    """
    import boto3

    def _explode(*_args, **_kwargs):
        raise RuntimeError("no credentials in the chain")

    monkeypatch.setattr(boto3, "client", _explode)

    with pytest.raises(RuntimeError) as caught:
        build_object_storage(Settings(**S3_PROD))
    assert "no credentials" in str(caught.value)


def test_a_failed_s3_build_returns_no_store_at_all(monkeypatch):
    """The same guard stated as the property that matters: nothing usable comes
    back. A test that only asserted `raises` would still pass if the code
    returned a local store on some other path."""
    import boto3

    monkeypatch.setattr(
        boto3, "client",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("endpoint unreachable")),
    )
    result = None
    try:
        result = build_object_storage(Settings(**S3_PROD))
    except RuntimeError:
        pass
    assert result is None, f"a broken S3 configuration produced {type(result).__name__}"


# ------------------------------------------------------------- credentials --
def test_settings_has_nowhere_to_put_a_long_lived_aws_key():
    """Credentials come from the platform chain — task role, instance profile,
    web identity. A settings field for a static key is a place for one to be
    committed, so there must not be one to fill in.
    """
    fields = set(Settings.model_fields)
    for forbidden in (
        "aws_access_key_id", "aws_secret_access_key", "aws_session_token",
        "s3_access_key", "s3_secret_key", "storage_credentials",
    ):
        assert forbidden not in fields, (
            f"Settings.{forbidden} invites a long-lived credential into "
            "configuration; boto3 resolves the chain itself"
        )


#: A credential being PASSED, not a credential being discussed. Matching the
#: bare identifier caught `config.py`, whose comment explains why no such field
#: exists — a test that cannot tell a prohibition from a violation is worse
#: than no test.
_INLINE_CREDENTIAL = re.compile(
    r"""aws_(?:access_key_id|secret_access_key|session_token)["']?\s*[=:]""",
)


def test_the_credential_matcher_is_not_vacuous():
    """Guards the test below, which greps a tree that should never match.

    An assertion whose pattern silently stopped matching anything would pass
    forever while checking nothing.
    """
    assert _INLINE_CREDENTIAL.search('client("s3", aws_access_key_id="AKIA...")')
    assert _INLINE_CREDENTIAL.search('{"aws_secret_access_key": secret}')
    assert not _INLINE_CREDENTIAL.search(
        "adding `aws_access_key_id` to this file would create a place for a key"
    )


def test_no_source_file_configures_boto_with_an_inline_key():
    from pathlib import Path

    app = Path(__file__).resolve().parents[2] / "app"
    offenders = [
        str(path) for path in app.rglob("*.py")
        if _INLINE_CREDENTIAL.search(path.read_text())
    ]
    assert not offenders, f"inline AWS credentials in {offenders}"


# --------------------------------------------------------------- port shape --
def test_the_port_offers_no_unused_download_minter():
    """`presign_get` had no caller anywhere in `app/` or `workers/`.

    An unused download-URL minter is not free surface: it is a way to hand out
    object access with no authorization check in front of it, waiting for
    someone to reach for it. Removed rather than implemented twice.
    """
    from app.domain.ports import ObjectStorage

    assert not hasattr(ObjectStorage, "presign_get")
    for adapter in (LocalObjectStorage, S3ObjectStorage):
        assert not hasattr(adapter, "presign_get"), (
            f"{adapter.__name__} still mints download URLs nobody asked for"
        )


def test_the_port_operations_are_the_ones_callers_use():
    """A closed surface, so an adapter cannot half-implement one nobody noticed."""
    from app.domain.ports import ObjectStorage

    declared = {
        name for name in vars(ObjectStorage)
        if not name.startswith("_") and callable(getattr(ObjectStorage, name, None))
    }
    assert declared == {"presign_put", "put", "get", "delete"}, declared
