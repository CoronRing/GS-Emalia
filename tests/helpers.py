"""Builders for test fixtures.

Kept out of `conftest.py` so test modules can import them directly; pytest
puts the tests directory on `sys.path`.
"""

from __future__ import annotations

from email.message import EmailMessage as MIMEMessage


def make_raw(
    *,
    sender: str = "Alice <alice@example.com>",
    to: str = "bot@example.com",
    subject: str = "hello",
    body: str = "Please list my notes.",
    message_id: str = "<msg-1@example.com>",
    html: str | None = None,
    attachments: list[tuple[str, bytes, str]] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> bytes:
    """Build raw RFC 822 bytes for a test message.

    Args:
        sender: The From header value.
        to: The To header value.
        subject: The subject line.
        body: The plain-text body.
        message_id: The Message-ID header.
        html: An optional HTML alternative body.
        attachments: ``(filename, data, content_type)`` triples to attach.
        extra_headers: Additional headers to set.

    Returns:
        The serialised message.
    """
    message = MIMEMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message["Message-ID"] = message_id
    message["Date"] = "Mon, 03 Jun 2026 09:12:00 +0000"
    for key, value in (extra_headers or {}).items():
        message[key] = value

    message.set_content(body)
    if html:
        message.add_alternative(html, subtype="html")
    for filename, data, content_type in attachments or []:
        maintype, _, subtype = content_type.partition("/")
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return message.as_bytes()
