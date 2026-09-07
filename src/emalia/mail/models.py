"""Plain data structures describing mail.

Nothing in this module touches a network or a MIME tree. `emalia.mail.parse`
turns raw bytes into these; `emalia.mail.compose` turns these back into MIME.
Keeping the middle layer free of `email.message.Message` means callers never
have to learn the stdlib email API to use the toolkit.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

__all__ = [
    "Attachment",
    "EmailAddress",
    "EmailMessage",
    "EmailSummary",
    "Flag",
    "parse_address_list",
]

Flag = Literal["\\Seen", "\\Answered", "\\Flagged", "\\Deleted", "\\Draft"]

_ADDRESS_RE = re.compile(r"<([^<>]+)>")


@dataclass(frozen=True, slots=True)
class EmailAddress:
    """A single mail participant.

    Attributes:
        address: The bare address, e.g. ``alice@example.com``. Always lowercase.
        name: The display name, if the header carried one.
    """

    address: str
    name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "address", self.address.strip().lower())

    @classmethod
    def parse(cls, raw: str) -> EmailAddress:
        """Parse one address header value.

        Args:
            raw: A header fragment such as ``Alice <alice@example.com>`` or a
                bare address.

        Returns:
            The parsed address. An unparseable value is kept verbatim as the
            address so that nothing is silently dropped.
        """
        raw = raw.strip()
        match = _ADDRESS_RE.search(raw)
        if match:
            name = raw[: match.start()].strip().strip('"').strip()
            return cls(address=match.group(1), name=name or None)
        return cls(address=raw)

    @property
    def domain(self) -> str:
        """The part after the ``@``, or an empty string if there is none."""
        _, _, domain = self.address.partition("@")
        return domain

    def __str__(self) -> str:
        return f"{self.name} <{self.address}>" if self.name else self.address


def parse_address_list(raw: str | None) -> list[EmailAddress]:
    """Split a comma-separated address header into individual addresses.

    Commas inside a quoted display name are not separators, so the split is
    done on commas that fall outside both quotes and angle brackets.

    Args:
        raw: The raw header value, or None.

    Returns:
        The addresses found, in header order. Empty if `raw` is falsy.
    """
    if not raw:
        return []
    parts: list[str] = []
    buffer: list[str] = []
    in_quotes = False
    in_angle = False
    for char in raw:
        if char == '"':
            in_quotes = not in_quotes
        elif char == "<" and not in_quotes:
            in_angle = True
        elif char == ">" and not in_quotes:
            in_angle = False
        if char == "," and not in_quotes and not in_angle:
            parts.append("".join(buffer))
            buffer = []
            continue
        buffer.append(char)
    parts.append("".join(buffer))
    return [EmailAddress.parse(part) for part in parts if part.strip()]


@dataclass(slots=True)
class Attachment:
    """One attached part of a message.

    `data` is held in memory. Large messages are bounded by the fetch-time
    size limit in `emalia.mail.imap`, not here.

    Attributes:
        filename: The name declared by the sender, sanitised of path
            separators by `safe_filename`.
        content_type: The declared MIME type, e.g. ``application/pdf``.
        data: The decoded bytes.
        content_id: The ``Content-ID`` value for inline parts, without angle
            brackets. None for ordinary attachments.
        inline: True when the part was marked ``inline`` rather than
            ``attachment``.
    """

    filename: str
    content_type: str
    data: bytes
    content_id: str | None = None
    inline: bool = False

    @property
    def size(self) -> int:
        """Size of the decoded payload in bytes."""
        return len(self.data)

    def safe_filename(self) -> str:
        """Return a filename safe to join onto a directory path.

        Strips directory components and characters that are illegal on Windows,
        which is the case the original code missed: a sender could name an
        attachment ``../../autorun.inf``.

        Returns:
            A basename with no separators, never empty.
        """
        name = self.filename.replace("\\", "/").rsplit("/", 1)[-1]
        name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name).strip(". ")
        return name or "attachment.bin"


@dataclass(slots=True)
class EmailMessage:
    """A parsed message.

    Attributes:
        uid: The IMAP UID as a string, when the message came from a mailbox.
            None for messages built locally for sending.
        message_id: The ``Message-ID`` header. The de-duplication key.
        subject: The decoded subject, never None (empty string when absent).
        sender: The best available author address, preferring ``Sender`` then
            ``From`` then ``Return-Path``.
        to, cc, bcc: Recipient lists.
        reply_to: The ``Reply-To`` list, empty when the header is absent.
        date: The parsed ``Date`` header, timezone-aware when the header had a
            zone. None if absent or unparseable.
        text: The ``text/plain`` body, already charset-decoded.
        html: The ``text/html`` body, if one was present.
        attachments: Non-body parts.
        flags: IMAP flags as reported at fetch time.
        folder: The mailbox the message was fetched from.
        in_reply_to: The ``In-Reply-To`` header.
        references: The ``References`` header, split on whitespace.
        headers: Every header, lowercased keys, for anything not modelled here.
    """

    uid: str | None = None
    message_id: str | None = None
    subject: str = ""
    sender: EmailAddress | None = None
    to: list[EmailAddress] = field(default_factory=list)
    cc: list[EmailAddress] = field(default_factory=list)
    bcc: list[EmailAddress] = field(default_factory=list)
    reply_to: list[EmailAddress] = field(default_factory=list)
    date: datetime | None = None
    text: str = ""
    html: str | None = None
    attachments: list[Attachment] = field(default_factory=list)
    flags: tuple[str, ...] = ()
    folder: str = "INBOX"
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def reply_target(self) -> EmailAddress | None:
        """The address a reply should go to.

        ``Reply-To`` wins when present, which is what mailing lists and
        ticketing systems expect; otherwise the author.
        """
        if self.reply_to:
            return self.reply_to[0]
        return self.sender

    @property
    def body(self) -> str:
        """The most useful body text available.

        Prefers ``text/plain``. Falls back to a text rendering of the HTML part
        so that HTML-only senders are not seen as empty messages.
        """
        if self.text.strip():
            return self.text
        if self.html:
            from emalia.mail.parse import html_to_text

            return html_to_text(self.html)
        return ""

    def summary(self) -> EmailSummary:
        """Compact view suitable for putting in front of a model."""
        return EmailSummary(
            uid=self.uid,
            message_id=self.message_id,
            subject=self.subject,
            sender=str(self.sender) if self.sender else "",
            date=self.date.isoformat() if self.date else "",
            preview=" ".join(self.body.split())[:200],
            attachment_names=[a.filename for a in self.attachments],
            flags=list(self.flags),
        )

    def total_attachment_bytes(self) -> int:
        """Sum of every attachment payload."""
        return sum(a.size for a in self.attachments)

    def recipients(self) -> list[EmailAddress]:
        """Every address the message was addressed to, across To, Cc and Bcc."""
        return [*self.to, *self.cc, *self.bcc]


@dataclass(frozen=True, slots=True)
class EmailSummary:
    """A one-screen description of a message, for listings and model input."""

    uid: str | None
    message_id: str | None
    subject: str
    sender: str
    date: str
    preview: str
    attachment_names: list[str]
    flags: list[str]

    def to_line(self) -> str:
        """Render as a single human- and model-readable line."""
        marks = []
        if "\\Seen" not in self.flags:
            marks.append("unread")
        if self.attachment_names:
            marks.append(f"{len(self.attachment_names)} attachment(s)")
        suffix = f" [{', '.join(marks)}]" if marks else ""
        return (
            f"uid={self.uid} | {self.date} | from {self.sender} | "
            f"{self.subject or '(no subject)'}{suffix}\n    {self.preview}"
        )


def summaries_to_text(summaries: Iterable[EmailSummary] | Sequence[EmailSummary]) -> str:
    """Render a batch of summaries as the text a tool returns to a model.

    Args:
        summaries: The summaries to render, in display order.

    Returns:
        One block per message, or a clear "no messages" line when empty.
    """
    lines = [s.to_line() for s in summaries]
    if not lines:
        return "No messages matched."
    return "\n".join(lines)
