"""The email seam: capture, SES, and the configuration that refuses to guess.

NO TEST HERE OPENS A SOCKET. The capture provider never had one, and the SES
adapter is driven through `botocore.stub.Stubber`, which validates every
parameter against the real service model — so a request this adapter builds
wrongly is rejected by the same schema AWS would reject it with, without an
account, a credential, or a packet.
"""
from __future__ import annotations

import pytest
from botocore.exceptions import ClientError
from botocore.stub import ANY, Stubber

from app.core.config import Settings
from app.domain.ports import RenderedEmail, TransactionalEmail
from app.integrations.email import (
    CAPTURE_PROVIDER,
    SES_PROVIDER,
    CaptureEmailProvider,
    EmailDeliveryFailed,
    EmailMisconfigured,
    SesEmailProvider,
    build_email_provider,
    captured_emails,
)

MESSAGE = RenderedEmail(subject="s", text="t", html="<p>h</p>")
TO = "recipient@example.ca"
SENDER = "no-reply@onyx.example.ca"

#: Everything a production Settings needs besides the email fields under test.
#: Same reason as `PRODUCTION_BASELINE` in test_admission_preauth: a control
#: that constructs a production Settings breaks whenever production gains a
#: requirement, and hunting it down in five assertions is worse than naming it.
NON_EMAIL_PRODUCTION = {
    "environment": "production",
    "jwt_secret": "j" * 48,
    "admission_identity_secret": "x" * 48,
    "storage_provider": "s3",
    "s3_region": "ca-central-1",
    "s3_bucket_documents": "onyx-prod-documents",
    "s3_bucket_legislation": "onyx-prod-legislation",
}
REAL_EMAIL = {
    "email_provider": SES_PROVIDER,
    "email_sender_address": SENDER,
    "ses_region": "ca-central-1",
    "app_public_url": "https://app.onyx.example.ca",
}


def _ses(client) -> SesEmailProvider:
    return SesEmailProvider(client, sender=SENDER)


def _sesv2():
    import boto3

    return boto3.client(
        "sesv2", region_name="ca-central-1",
        aws_access_key_id="test", aws_secret_access_key="test",
    )


# --------------------------------------------------------------------------- #
# capture
# --------------------------------------------------------------------------- #

def test_the_capture_provider_records_what_it_was_given():
    address = "capture-records@example.ca"
    CaptureEmailProvider().send(address, TransactionalEmail.PASSWORD_RESET, MESSAGE)

    found = captured_emails(to=address)
    assert len(found) == 1
    assert found[0].kind is TransactionalEmail.PASSWORD_RESET
    assert found[0].message is MESSAGE


def test_the_outbox_is_queryable_by_recipient_and_kind():
    """Scoped queries are the point: `[-1]` would depend on what ran before."""
    a, b = "scope-a@example.ca", "scope-b@example.ca"
    provider = CaptureEmailProvider()
    provider.send(a, TransactionalEmail.EMAIL_VERIFICATION, MESSAGE)
    provider.send(b, TransactionalEmail.PASSWORD_RESET, MESSAGE)

    assert [m.to for m in captured_emails(to=a)] == [a]
    assert captured_emails(to=a, kind=TransactionalEmail.PASSWORD_RESET) == []
    assert len(captured_emails(to=b, kind=TransactionalEmail.PASSWORD_RESET)) == 1


# --------------------------------------------------------------------------- #
# SES: the request it builds
# --------------------------------------------------------------------------- #

def test_ses_sends_both_a_text_and_an_html_part():
    """An HTML-only transactional message is scored as spam, and is unreadable
    in a client that blocks HTML — which for a verification email means a blank
    message and no way to act on it."""
    client = _sesv2()
    with Stubber(client) as stub:
        stub.add_response(
            "send_email",
            {"MessageId": "m-1"},
            {
                "FromEmailAddress": SENDER,
                "Destination": {"ToAddresses": [TO]},
                "Content": {
                    "Simple": {
                        "Subject": {"Data": "s", "Charset": "UTF-8"},
                        "Body": {
                            "Text": {"Data": "t", "Charset": "UTF-8"},
                            "Html": {"Data": "<p>h</p>", "Charset": "UTF-8"},
                        },
                    }
                },
            },
        )
        _ses(client).send(TO, TransactionalEmail.PASSWORD_RESET, MESSAGE)
        stub.assert_no_pending_responses()


def test_the_configured_sender_is_the_one_used():
    """Not the recipient's domain, not a default: a From that is not a verified
    sending identity is rejected by SES, and guessing one fails in production
    only."""
    client = _sesv2()
    with Stubber(client) as stub:
        stub.add_response(
            "send_email", {"MessageId": "m-2"},
            {"FromEmailAddress": SENDER, "Destination": ANY, "Content": ANY},
        )
        SesEmailProvider(client, sender=SENDER).send(
            TO, TransactionalEmail.EMAIL_VERIFICATION, MESSAGE
        )
        stub.assert_no_pending_responses()


# --------------------------------------------------------------------------- #
# SES: failure translation
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "code,transient",
    [
        ("TooManyRequestsException", True),    # throttling — ask again
        ("LimitExceededException", True),      # sending quota — ask again
        ("ServiceUnavailable", True),          # provider down
        ("InternalServiceErrorException", True),
        ("AccessDeniedException", False),      # credentials — a human must act
        ("MessageRejected", False),            # the message itself
        ("MailFromDomainNotVerifiedException", False),
        ("AccountSuspendedException", False),
        ("SendingPausedException", False),
    ],
)
def test_ses_failures_become_one_error_carrying_only_retryability(code, transient):
    """A caller's only real decision is whether asking again could work.

    Unnamed codes are TRANSIENT on purpose: a recoverable resend costs a log
    line, and a wrongly-permanent failure costs a customer their account.
    """
    client = _sesv2()
    with Stubber(client) as stub:
        stub.add_client_error("send_email", service_error_code=code)
        with pytest.raises(EmailDeliveryFailed) as raised:
            _ses(client).send(TO, TransactionalEmail.PASSWORD_RESET, MESSAGE)
    assert raised.value.transient is transient
    assert raised.value.kind is TransactionalEmail.PASSWORD_RESET


def test_an_unnamed_provider_code_is_treated_as_transient():
    client = _sesv2()
    with Stubber(client) as stub:
        stub.add_client_error("send_email", service_error_code="SomethingNewIn2027")
        with pytest.raises(EmailDeliveryFailed) as raised:
            _ses(client).send(TO, TransactionalEmail.EMAIL_VERIFICATION, MESSAGE)
    assert raised.value.transient is True


def test_a_transport_failure_with_no_code_is_transient():
    """A timeout or a refused connection carries no `response` at all."""

    class Dead:
        def send_email(self, **_):
            raise OSError("connection reset")

    with pytest.raises(EmailDeliveryFailed) as raised:
        _ses(Dead()).send(TO, TransactionalEmail.PASSWORD_CHANGED, MESSAGE)
    assert raised.value.transient is True


def test_no_provider_exception_escapes_the_adapter():
    """The port's contract. A caller that had to catch `ClientError` would be
    coupled to botocore, and one keyed on `str(e)` cannot be reasoned about."""
    client = _sesv2()
    with Stubber(client) as stub:
        stub.add_client_error("send_email", service_error_code="MessageRejected")
        with pytest.raises(EmailDeliveryFailed):
            try:
                _ses(client).send(TO, TransactionalEmail.PASSWORD_RESET, MESSAGE)
            except ClientError:  # pragma: no cover - the failure this forbids
                pytest.fail("a botocore exception reached the caller")


def test_the_failure_carries_no_provider_message():
    """`str(exc)` reaches logs and sometimes operators. A SES message names the
    sending identity, the region and sometimes the account id."""
    client = _sesv2()
    with Stubber(client) as stub:
        stub.add_client_error(
            "send_email",
            service_error_code="MessageRejected",
            service_message=f"Email address is not verified: {SENDER} in ca-central-1",
        )
        with pytest.raises(EmailDeliveryFailed) as raised:
            _ses(client).send(TO, TransactionalEmail.PASSWORD_RESET, MESSAGE)
    rendered = str(raised.value)
    assert SENDER not in rendered
    assert "ca-central-1" not in rendered
    assert TO not in rendered


# --------------------------------------------------------------------------- #
# selection — PRODUCTION_EMAIL_CAPTURE_FALLBACK_POSSIBLE = NO
# --------------------------------------------------------------------------- #

def test_production_refuses_the_capture_provider():
    """The whole point of the two-layer guard.

    A deployment that captured its own password-reset mail would report every
    send as a success while every locked-out customer stayed locked out — the
    failure is invisible from inside the system and total from outside it.
    """
    settings = Settings(**NON_EMAIL_PRODUCTION, **{**REAL_EMAIL,
                                                   "email_provider": SES_PROVIDER})
    # Reach past the settings validator to prove the BUILDER refuses too: the
    # two layers must both hold, because a Settings can be constructed in a
    # test, a script or a worker without going through production validation.
    object.__setattr__(settings, "email_provider", CAPTURE_PROVIDER)
    with pytest.raises(EmailMisconfigured, match="capture"):
        build_email_provider(settings)


def test_production_settings_will_not_even_construct_with_capture():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="ONYX_EMAIL_PROVIDER"):
        Settings(**NON_EMAIL_PRODUCTION, **{**REAL_EMAIL,
                                            "email_provider": CAPTURE_PROVIDER})


@pytest.mark.parametrize(
    "missing,expected",
    [
        ("email_sender_address", "ONYX_EMAIL_SENDER_ADDRESS"),
        ("ses_region", "ONYX_SES_REGION"),
        ("app_public_url", "ONYX_APP_PUBLIC_URL"),
    ],
)
def test_production_refuses_an_incomplete_email_configuration(missing, expected):
    """Fail closed at startup, not at the first customer who forgets a password."""
    from pydantic import ValidationError

    config = {**NON_EMAIL_PRODUCTION, **REAL_EMAIL, missing: None}
    with pytest.raises(ValidationError, match=expected):
        Settings(**config)


def test_production_requires_an_https_public_url():
    """A verification link is a single-use credential in transit. Over http it
    is readable by anything between the customer and the product."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="https"):
        Settings(**NON_EMAIL_PRODUCTION, **{**REAL_EMAIL,
                                            "app_public_url": "http://app.onyx.ca"})


def test_a_complete_production_email_configuration_is_accepted():
    """The control: every assertion above is "production refuses this", which
    a Settings that refused everything would satisfy completely."""
    settings = Settings(**NON_EMAIL_PRODUCTION, **REAL_EMAIL)
    assert settings.is_production
    assert settings.email_provider == SES_PROVIDER


def test_an_unknown_provider_name_is_refused_rather_than_defaulted():
    settings = Settings(email_provider=CAPTURE_PROVIDER)
    object.__setattr__(settings, "email_provider", "sendgrid")
    with pytest.raises(EmailMisconfigured, match="sendgrid"):
        build_email_provider(settings)


def test_development_gets_the_capture_provider():
    assert isinstance(
        build_email_provider(Settings(email_provider=CAPTURE_PROVIDER)),
        CaptureEmailProvider,
    )
