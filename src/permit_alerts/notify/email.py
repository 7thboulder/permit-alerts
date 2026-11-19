"""Email alerts over SMTP (Gmail with an app password works)."""

from __future__ import annotations

import html
from email.message import EmailMessage

import aiosmtplib

from ..config import Settings
from .base import Alert


def build_message(alert: Alert, sender: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = alert.title()
    msg["From"] = sender
    msg["To"] = sender
    msg["Bcc"] = ", ".join(alert.emails)  # members don't see each other's addresses
    msg.set_content(alert.text() + "\n\nSent by Permit Alerts.")
    rows = "".join(
        f'<li style="margin-bottom:8px">{html.escape(line)}<br>'
        f'<a href="{html.escape(item.booking_url)}">Book on Recreation.gov</a></li>'
        for line, item in zip(alert.lines(limit=50), alert.items)
    )
    more = f"<p>…and {len(alert.items) - 50} more.</p>" if len(alert.items) > 50 else ""
    msg.add_alternative(
        f"<h2 style='font-family:sans-serif'>{html.escape(alert.title())}</h2>"
        f"<ul style='font-family:sans-serif'>{rows}</ul>{more}"
        "<p style='color:#777;font-family:sans-serif'>Sent by Permit Alerts.</p>",
        subtype="html",
    )
    return msg


class EmailNotifier:
    name = "email"

    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def configured(self) -> bool:
        return self.settings.email_enabled

    async def send(self, alert: Alert) -> None:
        if not alert.emails or not self.settings.email_enabled:
            return
        s = self.settings
        await aiosmtplib.send(
            build_message(alert, s.smtp_from),
            hostname=s.smtp_host,
            port=s.smtp_port,
            username=s.smtp_username or None,
            password=s.smtp_password or None,
            start_tls=s.smtp_port == 587,
            use_tls=s.smtp_port == 465,
            timeout=20,
        )
