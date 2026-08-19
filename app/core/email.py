"""Sending mail. Currently one message: prove you own this address.

Registration is gated on a university email domain, which establishes that an
address *could* belong to a student. It does not establish that the person
typing it owns it — without a verification step, anyone can register under a
classmate's address, and the classmate then cannot register at all. The link
sent from here is what closes that gap.

Failure policy
--------------
**A registration must never fail because a mail relay did.** The account row is
the valuable thing and it is already committed by the time this runs; a student
whose signup 500s because of an SMTP timeout has lost their account, whereas one
whose email is delayed can ask for another. So every send is best-effort, runs
after the response, and logs loudly rather than raising.

Unconfigured is a supported state, not an error. With no `SMTP_HOST` the link is
written to the log exactly as it was before this module existed, which is what
makes a laptop with no relay still a working development environment.
"""

from __future__ import annotations

import logging
from email.message import EmailMessage
from urllib.parse import quote

import aiosmtplib

from app.core.config import settings

logger = logging.getLogger("unitrack.email")


def verification_link(token: str) -> str:
    """Where the student clicks to activate.

    Points at the student app, not the API. A person reads this link; landing
    them on a JSON body would look like a broken site even though it worked.
    `quote` because a JWT is base64url — safe today, but a token format that
    ever grows a `+` or `/` would silently truncate at the query parser.
    """
    return f"{settings.verify_link_base}/verify?token={quote(token, safe='')}"


def reset_link(token: str) -> str:
    """Where the student clicks to choose a new password.

    Same origin as the verification link — the student app, not the API — for
    the same reason: a person reads it. `/reset-password` is a public route that
    reads the token from the query and posts it back with the new password.
    """
    return f"{settings.verify_link_base}/reset-password?token={quote(token, safe='')}"


def _build(to: str, name: str, link: str) -> EmailMessage:
    """Plain text and HTML. Mail clients that refuse HTML still get the link,
    and — more to the point — so does anyone reading it in a terminal."""
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = to
    message["Subject"] = "Confirm your UniTrack account"

    message.set_content(
        f"Hi {name},\n\n"
        "Confirm your email address to finish setting up your UniTrack account:\n\n"
        f"{link}\n\n"
        "The link is valid for two days. If you did not sign up, ignore this "
        "message — no account can be used until it is confirmed.\n"
    )
    message.add_alternative(
        f"""<!doctype html>
<html><body style="font-family:system-ui,-apple-system,'Segoe UI',sans-serif;
                   color:#1f2937;line-height:1.55">
  <p>Hi {name},</p>
  <p>Confirm your email address to finish setting up your UniTrack account.</p>
  <p><a href="{link}"
        style="display:inline-block;background:#1a3c8f;color:#fff;
               padding:11px 18px;border-radius:8px;text-decoration:none;
               font-weight:600">Confirm my email</a></p>
  <p style="color:#6b7280;font-size:13px">
    Or paste this into your browser:<br>
    <span style="word-break:break-all">{link}</span>
  </p>
  <p style="color:#6b7280;font-size:13px">
    The link is valid for two days. If you did not sign up, ignore this message
    — no account can be used until it is confirmed.
  </p>
</body></html>""",
        subtype="html",
    )
    return message


def _build_reset(to: str, name: str, link: str) -> EmailMessage:
    """Plain text and HTML, same shape as `_build`. A different subject and body
    because a "reset your password" mail arriving unbidden must tell the reader
    to ignore it — an unrequested one is the first sign of someone probing the
    account."""
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = to
    message["Subject"] = "Reset your UniTrack password"

    message.set_content(
        f"Hi {name},\n\n"
        "We received a request to reset your UniTrack password. Choose a new "
        "one here:\n\n"
        f"{link}\n\n"
        "The link is valid for one hour. If you did not ask for this, ignore "
        "this message — your password stays the same.\n"
    )
    message.add_alternative(
        f"""<!doctype html>
<html><body style="font-family:system-ui,-apple-system,'Segoe UI',sans-serif;
                   color:#1f2937;line-height:1.55">
  <p>Hi {name},</p>
  <p>We received a request to reset your UniTrack password.</p>
  <p><a href="{link}"
        style="display:inline-block;background:#1a3c8f;color:#fff;
               padding:11px 18px;border-radius:8px;text-decoration:none;
               font-weight:600">Choose a new password</a></p>
  <p style="color:#6b7280;font-size:13px">
    Or paste this into your browser:<br>
    <span style="word-break:break-all">{link}</span>
  </p>
  <p style="color:#6b7280;font-size:13px">
    The link is valid for one hour. If you did not ask for this, ignore this
    message — your password stays the same.
  </p>
</body></html>""",
        subtype="html",
    )
    return message


async def _deliver(message: EmailMessage, *, to: str, what: str, link: str) -> bool:
    """Hand a built message to the relay. Best effort, never raises.

    `what`/`link` exist only for the disabled-relay and failure logs, so a
    developer with no SMTP sees exactly what would have been sent — the
    behaviour the app had before this module existed.
    """
    if not settings.email_enabled:
        logger.info("email disabled — %s link for %s: %s", what, to, link)
        return False

    try:
        await aiosmtplib.send(
            message,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_user or None,
            password=settings.smtp_password or None,
            start_tls=settings.smtp_starttls,
            # Implicit TLS when STARTTLS is off, i.e. port 465. The alternative
            # is plaintext, which would put the relay password on the wire.
            use_tls=not settings.smtp_starttls,
            timeout=15,
        )
    except Exception:  # noqa: BLE001 — see the module docstring
        # The address is logged; the token in the link is not. That token is a
        # credential, and log aggregators are read by more people than a mailbox.
        logger.exception("could not send %s email to %s", what, to)
        return False

    logger.info("%s email sent to %s", what, to)
    return True


async def send_verification_email(*, to: str, name: str, token: str) -> bool:
    """Best effort. Returns whether it was actually handed to a relay.

    Never raises. Called from a background task after registration has already
    responded 201, so raising here would take down a request that has finished
    and leave nothing but a confusing traceback.
    """
    link = verification_link(token)
    return await _deliver(_build(to, name, link), to=to, what="verification", link=link)


async def send_password_reset_email(*, to: str, name: str, token: str) -> bool:
    """Best effort, never raises. Called from a background task after the
    endpoint has already answered 202, so a mail failure cannot change the
    reply the caller sees — which is what keeps the endpoint from leaking
    whether the address exists."""
    link = reset_link(token)
    return await _deliver(_build_reset(to, name, link), to=to, what="password-reset", link=link)
