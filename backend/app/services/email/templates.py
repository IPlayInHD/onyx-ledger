"""The three transactional messages Onyx sends, rendered once.

WHAT IS NOT IN HERE, and will not be: a figure, a tax position, a document
name, a province, a filing status, an amount. Email is unencrypted in somebody
else's mailbox, forwarded, indexed by a provider, and quoted into replies. The
rule is not "minimise" — it is that none of these messages carries customer
financial data at all, so there is nothing to minimise.

The recipient's own address is the only personal value any of them contains,
and it is already in the envelope.

WHY BOTH TEXT AND HTML. A plain-text part is not a courtesy: a message that is
HTML-only is scored as spam by most filters, and a customer whose client blocks
HTML would otherwise receive a blank verification email and no way to act on it.

NOTHING UNTRUSTED IS INTERPOLATED. The only variable that reaches the HTML is a
link this application built from configured origin plus a token it generated —
never a display name, never a filename, never anything a customer typed. That is
why the escaping here is a guard rather than the mechanism: there should be no
untrusted value to escape in the first place, and `_escape` exists so that a
future template which acquires one cannot inject markup by accident.
"""
from __future__ import annotations

from html import escape as _escape

from app.domain.ports import RenderedEmail

#: The sender's own name in body copy. Not the From: header — that is
#: configuration, because it has to match a verified sending identity.
PRODUCT = "Onyx"

#: Every message ends with this. A transactional email that does not say what
#: to do when you did not ask for it is how a compromised account stays
#: compromised: the one person positioned to notice is the one being told
#: nothing is wrong.
_UNSOLICITED = (
    "If you did not request this, you can ignore this message — "
    "no change has been made to your account."
)

_SIGNOFF = f"— The {PRODUCT} team"

#: Deliberately plain. A transactional security message that looks like
#: marketing trains people to expect security mail to look like marketing,
#: which is precisely the instinct a phishing message relies on.
_HTML_SHELL = """\
<!doctype html>
<html lang="en"><body style="margin:0;padding:24px;background:#f6f5f2;
 font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;
 color:#1c1f22;line-height:1.55">
  <div style="max-width:520px;margin:0 auto;background:#ffffff;border-radius:12px;
   padding:32px;border:1px solid #e5e1d8">
    <p style="margin:0 0 20px;font-size:15px;font-weight:600;letter-spacing:.02em">{product}</p>
    {body}
    <p style="margin:28px 0 0;font-size:13px;color:#585f64">{unsolicited}</p>
    <p style="margin:16px 0 0;font-size:13px;color:#585f64">{signoff}</p>
  </div>
</body></html>
"""


def _shell(body_html: str) -> str:
    return _HTML_SHELL.format(
        product=_escape(PRODUCT),
        body=body_html,
        unsolicited=_escape(_UNSOLICITED),
        signoff=_escape(_SIGNOFF),
    )


def _button(link: str, label: str) -> str:
    safe = _escape(link, quote=True)
    return (
        f'<p style="margin:24px 0"><a href="{safe}" '
        'style="display:inline-block;background:#1c1f22;color:#ffffff;'
        'text-decoration:none;padding:12px 20px;border-radius:8px;'
        f'font-size:14px;font-weight:500">{_escape(label)}</a></p>'
        # The bare URL as well. A button is unusable in a text-only client and
        # invisible to anyone who has images or styles blocked, and a customer
        # who cannot see where a link goes is being asked to trust it blindly.
        f'<p style="margin:0;font-size:12px;color:#585f64;word-break:break-all">'
        f'{safe}</p>'
    )


def _hours(minutes: int) -> str:
    """A duration a person reads without converting it."""
    if minutes % 60 == 0 and minutes >= 60:
        hours = minutes // 60
        return "1 hour" if hours == 1 else f"{hours} hours"
    return "1 minute" if minutes == 1 else f"{minutes} minutes"


def render_email_verification(*, link: str, ttl_minutes: int) -> RenderedEmail:
    window = _hours(ttl_minutes)
    text = (
        f"Confirm your email address\n\n"
        f"You created an {PRODUCT} account with this address. Confirm it to "
        f"finish setting up your account:\n\n"
        f"{link}\n\n"
        f"This link works once and expires in {window}.\n\n"
        f"{_UNSOLICITED}\n\n{_SIGNOFF}\n"
    )
    html = _shell(
        '<p style="margin:0;font-size:17px;font-weight:600">Confirm your email address</p>'
        f'<p style="margin:12px 0 0;font-size:14px">You created an {_escape(PRODUCT)} '
        "account with this address. Confirm it to finish setting up your account.</p>"
        + _button(link, "Confirm email address")
        + f'<p style="margin:20px 0 0;font-size:13px;color:#585f64">'
          f"This link works once and expires in {_escape(window)}.</p>"
    )
    return RenderedEmail(
        subject=f"Confirm your {PRODUCT} email address", text=text, html=html
    )


def render_password_reset(*, link: str, ttl_minutes: int) -> RenderedEmail:
    window = _hours(ttl_minutes)
    text = (
        f"Reset your password\n\n"
        f"Someone asked to reset the password for the {PRODUCT} account using "
        f"this address. If it was you, choose a new password here:\n\n"
        f"{link}\n\n"
        f"This link works once and expires in {window}. Your current password "
        f"stays active until you choose a new one.\n\n"
        f"{_UNSOLICITED}\n\n{_SIGNOFF}\n"
    )
    html = _shell(
        '<p style="margin:0;font-size:17px;font-weight:600">Reset your password</p>'
        f'<p style="margin:12px 0 0;font-size:14px">Someone asked to reset the password '
        f"for the {_escape(PRODUCT)} account using this address. If it was you, "
        "choose a new one.</p>"
        + _button(link, "Choose a new password")
        + f'<p style="margin:20px 0 0;font-size:13px;color:#585f64">'
          f"This link works once and expires in {_escape(window)}. Your current "
          "password stays active until you choose a new one.</p>"
    )
    return RenderedEmail(subject=f"Reset your {PRODUCT} password", text=text, html=html)


def render_password_changed() -> RenderedEmail:
    """Sent AFTER the change, and carrying no link.

    A security notice with an action button is a phishing template with a
    trusted sender. If this message could be acted on, the useful thing to do
    with a stolen one would be to send it. It reports, and tells the reader
    where to go on their own.
    """
    text = (
        f"Your password was changed\n\n"
        f"The password for your {PRODUCT} account was changed just now, and "
        f"every signed-in device was signed out.\n\n"
        f"If this was you, nothing more is needed.\n\n"
        f"If it was not you, someone else may have access to your email. Reset "
        f"your password from the {PRODUCT} sign-in page — reach it by typing "
        f"the address yourself rather than following a link — and secure your "
        f"email account.\n\n{_SIGNOFF}\n"
    )
    html = _shell(
        '<p style="margin:0;font-size:17px;font-weight:600">Your password was changed</p>'
        f'<p style="margin:12px 0 0;font-size:14px">The password for your '
        f"{_escape(PRODUCT)} account was changed just now, and every signed-in "
        "device was signed out.</p>"
        '<p style="margin:12px 0 0;font-size:14px">If this was you, nothing more '
        "is needed.</p>"
        '<p style="margin:12px 0 0;font-size:14px">If it was not you, someone else '
        f"may have access to your email. Reset your password from the "
        f"{_escape(PRODUCT)} sign-in page — reach it by typing the address "
        "yourself rather than following a link — and secure your email account.</p>"
    )
    # No `_UNSOLICITED` line: "ignore this if you did not request it" is exactly
    # the wrong advice for a change that has already happened.
    return RenderedEmail(
        subject=f"Your {PRODUCT} password was changed",
        text=text,
        html=html.replace(
            f'<p style="margin:28px 0 0;font-size:13px;color:#585f64">'
            f"{_escape(_UNSOLICITED)}</p>",
            "",
        ),
    )
