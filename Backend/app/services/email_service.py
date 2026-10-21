from email.message import EmailMessage
from html import escape

import aiosmtplib

from app.config import settings
from app.observability import logger

_PURPOSE_COPY = {
    "verification": (
        "Your Grace sign-up code",
        "Confirm your email",
        "Enter this code to finish creating your Grace account.",
    ),
    "password_reset": (
        "Your Grace password reset code",
        "Reset your password",
        "Enter this code to choose a new Grace password.",
    ),
    "email_change": (
        "Confirm your new Grace email",
        "Confirm your new email",
        "Enter this code to confirm this address for your Grace account.",
    ),
}


async def send_otp_email(to_email: str, otp: str, purpose: str = "verification") -> None:
    username = settings.smtp_user or settings.smtp_email or ""
    password = settings.smtp_password or ""
    if not username or not password:
        if settings.is_production:
            raise RuntimeError("SMTP delivery is not configured.")
        logger.warning("otp_email_delivery_skipped smtp_not_configured")
        return

    subject, heading, intro = _PURPOSE_COPY.get(purpose, _PURPOSE_COPY["verification"])
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = username
    message["To"] = to_email
    message.set_content(
        f"{intro}\n\nYour code: {otp}\n\nIt expires in {settings.otp_expire_minutes} minutes. "
        "If you didn't ask for this, you can ignore this email."
    )
    message.add_alternative(
        f"""\
        <html>
          <body style="margin:0;background:#0b111a;color:#edf3ff;font-family:Arial,sans-serif">
            <div style="max-width:520px;margin:40px auto;padding:32px;background:#101a29;border-radius:16px">
              <p style="color:#8fb2ff;letter-spacing:.12em;font-size:12px">Grace</p>
              <h1 style="font-size:24px">{escape(heading)}</h1>
              <p>{escape(intro)} It expires in {settings.otp_expire_minutes} minutes.</p>
              <p style="padding:16px;text-align:center;background:#0b111a;border-radius:10px;
                        font-size:32px;letter-spacing:.35em;font-weight:bold">{escape(otp)}</p>
              <p style="color:#92a0b3;font-size:12px">If you did not request this code, you can ignore this email.</p>
            </div>
          </body>
        </html>
        """,
        subtype="html",
    )

    await aiosmtplib.send(
        message,
        hostname=settings.smtp_host,
        port=settings.smtp_port,
        start_tls=True,
        username=username,
        password=password,
        timeout=10,
    )
