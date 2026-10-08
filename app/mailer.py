"""Outgoing email via the admin-configured SMTP settings — used for alerts,
the weekly digest, and a test message. Plain stdlib smtplib.
"""

import smtplib
import ssl
from email.message import EmailMessage

from . import settings_store
from .logging_config import logger


class MailError(Exception):
    pass


def is_configured() -> bool:
    s = settings_store.get_smtp_settings()
    return bool(s["host"] and s["from"])


def send_email(to, subject: str, body: str, html: str = None):
    """Send one email to `to` (a string or list). Raises MailError on failure.
    `body` is plain text; `html` is optional HTML alternative."""
    s = settings_store.get_smtp_settings()
    if not s["host"] or not s["from"]:
        raise MailError("SMTP isn't configured (App Settings → Email).")
    recipients = [to] if isinstance(to, str) else [r for r in to if r]
    recipients = [r.strip() for r in recipients if r and r.strip()]
    if not recipients:
        raise MailError("No recipient email address.")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = s["from"]
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")

    try:
        if s["port"] == 465:
            with smtplib.SMTP_SSL(s["host"], s["port"], timeout=20, context=ssl.create_default_context()) as srv:
                if s["username"]:
                    srv.login(s["username"], s["password"])
                srv.send_message(msg)
        else:
            with smtplib.SMTP(s["host"], s["port"], timeout=20) as srv:
                if s["use_tls"]:
                    srv.starttls(context=ssl.create_default_context())
                if s["username"]:
                    srv.login(s["username"], s["password"])
                srv.send_message(msg)
    except Exception as e:
        raise MailError(f"Could not send email: {e}")
    logger.info("Email sent to %d recipient(s): %s", len(recipients), subject)


def test_send(to: str) -> tuple:
    """(ok, message) — send a test email to `to`."""
    try:
        send_email(to, "ERM Project Ledger — test email",
                   "This is a test email from the ERM Project Ledger. If you received it, SMTP is configured correctly.")
        return True, f"Test email sent to {to}."
    except MailError as e:
        return False, str(e)
