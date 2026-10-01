"""Transactional account email delivery, without logging message bodies or secrets."""

import os
import smtplib
import ssl
from email.message import EmailMessage


def email_delivery_available():
    return bool(os.getenv("SMTP_HOST", "").strip() and os.getenv("SMTP_FROM", "").strip())


def send_account_email(recipient, subject, body):
    """An injectable delivery seam: success returns None; errors go to the caller."""
    if not email_delivery_available():
        raise RuntimeError("Email delivery is not configured")
    message = EmailMessage()
    message["From"] = os.environ["SMTP_FROM"]
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    use_ssl = os.getenv("SMTP_USE_SSL", "false").lower() == "true"
    port = int(os.getenv("SMTP_PORT", "465" if use_ssl else "587"))
    connection = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    options = {"timeout": 15}
    if use_ssl:
        options["context"] = ssl.create_default_context()
    with connection(os.environ["SMTP_HOST"], port, **options) as smtp:
        if not use_ssl:
            smtp.starttls(context=ssl.create_default_context())
        if os.getenv("SMTP_USER"):
            smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        smtp.send_message(message)
