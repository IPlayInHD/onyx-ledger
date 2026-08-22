"""Transactional email adapters, and the choice between them.

`CaptureEmailProvider` records messages in this process and sends nothing.
`SesEmailProvider` is the production adapter. `get_email_provider()` picks
between them from configuration and REFUSES the capture provider in production —
same two-layer shape as `ObjectStorage`, and for the same reason: a deployment
that silently captured its own password-reset mail would look healthy while
every customer locked out of their account stayed locked out.

WHEN A SEND FAILS, THE TOKEN SURVIVES. The ordering is: mint the token, commit
it, then send. The alternative — send first, commit after — puts a live link in
somebody's inbox that the database never heard of, so the one thing they can do
with the email is click a dead link. Committing first means a failed send leaves
a token nobody received, which the customer notices immediately and fixes with a
resend that supersedes it. Recoverable beats tidy.

That is also why `EmailDeliveryFailed` carries a closed `transient` flag rather
than a provider message: the caller's only real decision is whether asking again
could work.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.core.logging import get_logger
from app.domain.ports import RenderedEmail, TransactionalEmail

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.core.config import Settings

log = get_logger(__name__)


class EmailDeliveryFailed(RuntimeError):
    """A transactional message could not be handed to the provider.

    `transient` is the whole payload. A caller deciding what to do next needs
    to know whether asking again could work, and nothing else — certainly not
    the provider's message, which carries the sending identity, the region and
    sometimes an account id.
    """

    def __init__(self, kind: TransactionalEmail, *, transient: bool) -> None:
        self.kind = kind
        self.transient = transient
        super().__init__(
            f"{kind.value} delivery failed "
            f"({'transient' if transient else 'permanent'})"
        )


class EmailMisconfigured(RuntimeError):
    """The configured email provider cannot be built."""


# --------------------------------------------------------------------------- #
# Development and tests
# --------------------------------------------------------------------------- #

class CapturedEmail:
    """One message that would have been sent."""

    __slots__ = ("to", "kind", "message")

    def __init__(self, to: str, kind: TransactionalEmail, message: RenderedEmail):
        self.to = to
        self.kind = kind
        self.message = message

    def __repr__(self) -> str:  # pragma: no cover - diagnostics
        return f"<CapturedEmail {self.kind.value} to={self.to!r}>"


#: Process-local outbox. NOT a fixture and NOT reset automatically — see
#: `clear_captured_emails`.
_OUTBOX: list[CapturedEmail] = []


class CaptureEmailProvider:
    """Records the message and sends nothing. Never opens a socket."""

    def send(
        self, to: str, kind: TransactionalEmail, message: RenderedEmail
    ) -> None:
        _OUTBOX.append(CapturedEmail(to, kind, message))


def captured_emails(
    *, to: str | None = None, kind: TransactionalEmail | None = None
) -> list[CapturedEmail]:
    """What this process would have sent, oldest first.

    SCOPE THE QUERY. A test that asserts on `captured_emails()[-1]` is asserting
    about whatever ran before it as much as about itself, and this repository
    has a documented history of exactly that failure. Filter by recipient — the
    addresses are per-test and unique — rather than trusting the outbox to be
    empty.
    """
    found = _OUTBOX
    if to is not None:
        found = [m for m in found if m.to == to]
    if kind is not None:
        found = [m for m in found if m.kind is kind]
    return list(found)


def clear_captured_emails() -> None:
    """Empty the outbox. For a fixture that wants a known starting point."""
    _OUTBOX.clear()


# --------------------------------------------------------------------------- #
# Production
# --------------------------------------------------------------------------- #

#: SES conditions where sending again changes nothing until a human acts.
#: Short on purpose: anything unnamed is treated as transient, because a
#: recoverable resend costs a log line and a wrongly-permanent failure costs a
#: customer their account.
_PERMANENT_CODES = frozenset({
    "AccessDenied",
    "AccessDeniedException",
    "MessageRejected",
    "MailFromDomainNotVerifiedException",
    "AccountSuspendedException",
    "SendingPausedException",
    "AccountSendingPausedException",
    "ConfigurationSetDoesNotExistException",
    "BadRequestException",
})


def _error_code(exc: Exception) -> str:
    """The provider's closed error code, or "" for a transport failure."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            code = error.get("Code")
            if isinstance(code, str):
                return code
    return ""


class SesEmailProvider:
    """AWS SES v2.

    Takes the client rather than building one, so a test drives it through
    `botocore.stub.Stubber` against the real service model instead of a fake
    that agrees with whatever this adapter happens to do.
    """

    def __init__(self, client: Any, *, sender: str) -> None:
        self._c = client
        self._sender = sender

    def send(
        self, to: str, kind: TransactionalEmail, message: RenderedEmail
    ) -> None:
        try:
            self._c.send_email(
                FromEmailAddress=self._sender,
                Destination={"ToAddresses": [to]},
                Content={
                    "Simple": {
                        "Subject": {"Data": message.subject, "Charset": "UTF-8"},
                        "Body": {
                            "Text": {"Data": message.text, "Charset": "UTF-8"},
                            "Html": {"Data": message.html, "Charset": "UTF-8"},
                        },
                    }
                },
            )
        except Exception as exc:  # noqa: BLE001 - translated, never leaked
            code = _error_code(exc)
            transient = code not in _PERMANENT_CODES
            # The KIND and the CODE. Not the recipient — an address in a log
            # line is the personal value this whole subsystem is careful with
            # everywhere else — and not the provider message, which names the
            # sending identity and the region.
            log.warning(
                "transactional_email_failed",
                kind=kind.value,
                provider_code=code or "TRANSPORT",
                transient=transient,
            )
            raise EmailDeliveryFailed(kind, transient=transient) from None


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #

CAPTURE_PROVIDER = "capture"
SES_PROVIDER = "ses"
EMAIL_PROVIDERS = frozenset({CAPTURE_PROVIDER, SES_PROVIDER})


def build_email_provider(
    settings: Settings,
) -> CaptureEmailProvider | SesEmailProvider:
    """The provider this configuration asks for, or an exception.

    NO FALLBACK. Not "try SES, fall back to capture" — a deployment whose
    credentials are wrong would then accept registrations, mint verification
    tokens, file the mail in a list, and report success, while every customer
    waited for an email that was never going to arrive.
    """
    provider = settings.email_provider
    if provider not in EMAIL_PROVIDERS:
        raise EmailMisconfigured(
            f"unknown email provider {provider!r}; expected one of "
            f"{sorted(EMAIL_PROVIDERS)}"
        )

    if provider == CAPTURE_PROVIDER:
        if settings.is_production:
            raise EmailMisconfigured(
                "the capture email provider must never serve production; "
                "verification and password-reset mail would be filed in a list "
                "instead of delivered, and every locked-out customer would "
                "stay locked out"
            )
        return CaptureEmailProvider()

    # Imported here, not at module scope: this module is pulled in by services
    # that never send mail, and a top-level import would make the whole
    # application refuse to start in an environment that does not need boto3.
    import boto3
    from botocore.config import Config

    if not settings.email_sender_address:
        raise EmailMisconfigured("ONYX_EMAIL_SENDER_ADDRESS is required for ses")
    if settings.is_production and not settings.ses_region:
        raise EmailMisconfigured("ONYX_SES_REGION is required in production")

    client = boto3.client(
        "sesv2",
        region_name=settings.ses_region,
        config=Config(retries={"max_attempts": 3, "mode": "standard"}),
    )
    return SesEmailProvider(client, sender=settings.email_sender_address)


def get_email_provider() -> CaptureEmailProvider | SesEmailProvider:
    from app.core.config import get_settings

    return build_email_provider(get_settings())
