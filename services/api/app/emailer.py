"""Transactional email delivery for verification and password recovery."""

from email.message import EmailMessage
import smtplib

from . import config
from .logging_setup import log


class EmailDeliveryError(RuntimeError):
    """Raised when the configured email transport cannot deliver a message."""


def send_otp(*, recipient: str, code: str, purpose: str) -> None:
    """Send an OTP without persisting or logging the plaintext code by default."""
    subject = "Project Atlas verification code" if purpose == "email_verify" else "Project Atlas password reset"
    action = "verify your email address" if purpose == "email_verify" else "reset your password"
    message = EmailMessage()
    message["From"] = config.SMTP_FROM or "Project Atlas"
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(
        f"Your Project Atlas code to {action} is: {code}\n\n"
        f"This code expires in {config.OTP_TTL_MIN} minutes and can be used once.\n"
        "If you did not request this message, you can safely ignore it."
    )

    if config.EMAIL_MODE == "console":
        fields = {"recipient": recipient, "purpose": purpose, "delivery": "console"}
        if config.EMAIL_DEV_LOG_CODE:
            fields["dev_code"] = code
        log("info", "email challenge generated", **fields)
        return

    if config.EMAIL_MODE != "smtp":
        raise EmailDeliveryError(f"unsupported EMAIL_MODE: {config.EMAIL_MODE}")

    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=15) as client:
            client.ehlo()
            if config.SMTP_USE_TLS:
                client.starttls()
                client.ehlo()
            client.login(config.SMTP_USER, config.SMTP_PASSWORD)
            client.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        log("error", "email delivery failed", recipient=recipient, purpose=purpose,
            error=f"{type(exc).__name__}: {exc}"[:200])
        raise EmailDeliveryError("email delivery failed") from exc

    log("info", "email challenge sent", recipient=recipient, purpose=purpose)
