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

    ctx = ssl.create_default_context()

    def via_ssl():
        with smtplib.SMTP_SSL(s["host"], s["port"], timeout=25, context=ctx) as srv:
            srv.ehlo()
            if s["username"]:
                srv.login(s["username"], s["password"])
            srv.send_message(msg)

    def via_starttls():
        with smtplib.SMTP(s["host"], s["port"], timeout=25) as srv:
            srv.ehlo()
            # Use STARTTLS whenever the server offers it (Gmail/O365 require it
            # on 587) or the admin asked for it — don't rely on the checkbox
            # alone, so a login is never attempted over plaintext.
            if s["use_tls"] or srv.has_extn("starttls"):
                srv.starttls(context=ctx)
                srv.ehlo()
            if s["username"]:
                srv.login(s["username"], s["password"])
            srv.send_message(msg)

    try:
        # Port 465 is implicit SSL; everything else is plain+STARTTLS.
        if s["port"] == 465:
            via_ssl()
        else:
            via_starttls()
    except smtplib.SMTPAuthenticationError:
        raise MailError("SMTP login failed — check the username/password (for Office 365/Gmail you usually need an app password).")
    except smtplib.SMTPServerDisconnected:
        raise MailError(
            "The mail server closed the connection. Common causes: a wrong "
            "app password, or the mail provider blocking this server's IP. "
            "Try port 465 (SSL) instead of 587, double-check the app password, "
            "or use your internal/Office 365 SMTP relay."
        )
    except (smtplib.SMTPConnectError, OSError) as e:
        raise MailError(f"Could not connect to {s['host']}:{s['port']} — {e}. Check the host/port and that the server is reachable.")
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
