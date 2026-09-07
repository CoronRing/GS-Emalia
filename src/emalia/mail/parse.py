"""Turn raw RFC 822 bytes into `EmailMessage`.

Three jobs the original got wrong or skipped, handled here:

* Header decoding. ``=?utf-8?B?...?=`` encoded words were passed through
  verbatim, so non-ASCII subjects reached the model as mojibake.
* Charset fallbacks. Bodies were decoded as ``utf-8-sig`` unconditionally and
  raised on anything else; a latin-1 sender crashed the loop.
* Quoted-reply stripping, which was a `pass` stub.
"""

from __future__ import annotations

import re
from datetime import datetime
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

from emalia.mail.models import (
    Attachment,
    EmailAddress,
    EmailMessage,
    parse_address_list,
)

__all__ = [
    "parse_message",
    "decode_header_value",
    "html_to_text",
    "strip_quoted_reply",
]

# "On Mon, 3 Jun 2024 at 09:12, Alice <a@x.com> wrote:" and its many variants.
# Matching the trailing "wrote:" is what makes this reliable across clients;
# the date format between them is not worth chasing.
_ATTRIBUTION_RE = re.compile(
    r"^\s*(On\s.{0,200}?\bwrote:\s*$"
    r"|Le\s.{0,200}?\ba écrit\s*:\s*$"
    r"|Am\s.{0,200}?\bschrieb\s.{0,120}:\s*$"
    r"|El\s.{0,200}?\bescribió:\s*$)",
    re.IGNORECASE | re.MULTILINE,
)

# Outlook and several webmail clients use a separator line instead.
_SEPARATOR_RE = re.compile(
    r"^\s*(-{2,}\s*Original Message\s*-{2,}"
    r"|_{5,}"
    r"|-{3,}\s*Forwarded message\s*-{3,}"
    r"|From:\s.+?\bSent:\s)",
    re.IGNORECASE | re.MULTILINE,
)

_SIGNATURE_RE = re.compile(r"^-- \s*$", re.MULTILINE)


class _TextExtractor(HTMLParser):
    """Collect visible text from an HTML body.

    Deliberately not a full renderer. The goal is to give a model something
    readable when a sender's client emitted HTML only, not to reproduce layout.
    Script and style contents are dropped, and quoted-reply containers used by
    Gmail, Thunderbird and Apple Mail are skipped entirely.
    """

    _SKIP_TAGS = {"script", "style", "head", "title"}
    _QUOTE_CLASSES = ("gmail_quote", "moz-cite-prefix", "yahoo_quoted", "OutlookMessageHeader")
    _BREAK_TAGS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0
        self._quote_depth = 0
        self._tag_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._tag_stack.append(tag)
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._quote_depth:
            self._quote_depth += 1
            return
        attr_blob = " ".join(v or "" for k, v in attrs if k in ("class", "id", "type"))
        if any(marker in attr_blob for marker in self._QUOTE_CLASSES):
            self._quote_depth = 1
            return
        if tag == "blockquote":
            self._quote_depth = 1
            return
        if tag in self._BREAK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self._tag_stack and self._tag_stack[-1] == tag:
            self._tag_stack.pop()
        if tag in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._quote_depth:
            self._quote_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth or self._quote_depth:
            return
        self._chunks.append(data)

    def text(self) -> str:
        """The collected text, with runs of blank lines collapsed."""
        joined = "".join(self._chunks)
        joined = re.sub(r"[ \t\r\f\v]+", " ", joined)
        joined = re.sub(r"\n\s*\n\s*\n+", "\n\n", joined)
        return "\n".join(line.strip() for line in joined.splitlines()).strip()


def html_to_text(html: str) -> str:
    """Render an HTML body down to readable plain text.

    Args:
        html: The raw ``text/html`` part.

    Returns:
        Visible text with quoted-reply containers, scripts and styles removed.
        Returns the input unchanged if it cannot be parsed as HTML at all.
    """
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # A malformed body should degrade to "some text" rather than break the
        # whole poll loop; stripping tags crudely is good enough here.
        return re.sub(r"<[^>]+>", " ", html).strip()
    return parser.text()


def decode_header_value(raw: str | None) -> str:
    """Decode an RFC 2047 encoded-word header into plain text.

    Args:
        raw: The raw header value, or None.

    Returns:
        The decoded value, or an empty string when `raw` is falsy. A header
        that cannot be decoded is returned as-is rather than raising, since a
        broken subject line is not worth dropping a message over.
    """
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw))).strip()
    except Exception:
        return raw.strip()


def _raw_payload(part: Message) -> bytes | None:
    """The decoded bytes of a part, or None when it has none.

    `Message.get_payload(decode=True)` is typed as returning any of bytes, a
    nested `Message`, or None, so the narrowing has to be explicit.

    Args:
        part: The MIME part to read.

    Returns:
        The decoded payload bytes, or None for a part that carries no bytes.
    """
    payload = part.get_payload(decode=True)
    return payload if isinstance(payload, bytes) else None


def _decode_payload(part: Message) -> str:
    """Decode one text part, trying the declared charset then sane fallbacks."""
    payload = _raw_payload(part)
    if payload is None:
        content = part.get_payload()
        return content if isinstance(content, str) else ""
    charsets = [part.get_content_charset(), "utf-8", "utf-8-sig", "cp1252", "latin-1"]
    for charset in charsets:
        if not charset:
            continue
        try:
            return payload.decode(charset).replace("﻿", "")
        except (UnicodeDecodeError, LookupError):
            continue
    # latin-1 above cannot fail, but a bad charset name can; be explicit.
    return payload.decode("utf-8", errors="replace").replace("﻿", "")


def strip_quoted_reply(text: str, *, strip_signature: bool = True) -> str:
    """Remove quoted history from a reply body.

    The original left this as a stub, so every reply re-sent the entire thread
    to the model. That costs tokens and, more importantly, re-presents the
    agent's own earlier output as if it were new instructions from the user,
    which is exactly the shape of a prompt-injection foothold.

    Args:
        text: A plain-text body.
        strip_signature: Also cut at the ``-- `` signature delimiter.

    Returns:
        Only the text the sender actually typed this time. If stripping would
        leave nothing, the original text is returned instead, on the grounds
        that a noisy body beats an empty one.
    """
    if not text:
        return ""

    candidates = [len(text)]
    for pattern in (_ATTRIBUTION_RE, _SEPARATOR_RE):
        match = pattern.search(text)
        if match:
            candidates.append(match.start())
    if strip_signature:
        match = _SIGNATURE_RE.search(text)
        if match:
            candidates.append(match.start())

    trimmed = text[: min(candidates)]

    # Drop trailing runs of ">" quoted lines that no attribution introduced.
    lines = trimmed.splitlines()
    while lines and (not lines[-1].strip() or lines[-1].lstrip().startswith(">")):
        lines.pop()
    trimmed = "\n".join(lines).strip()

    return trimmed or text.strip()


def _walk_parts(message: Message) -> tuple[str, str | None, list[Attachment]]:
    """Split a MIME tree into plain text, HTML, and attachments."""
    text_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[Attachment] = []

    if not message.is_multipart():
        content = _decode_payload(message)
        if message.get_content_type() == "text/html":
            return "", content, []
        return content, None, []

    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        disposition = (part.get("Content-Disposition") or "").strip().lower()
        content_type = part.get_content_type()
        filename = part.get_filename()
        is_attachment = disposition.startswith("attachment") or (
            filename is not None and not disposition.startswith("inline")
        )

        if not is_attachment and content_type == "text/plain":
            text_parts.append(_decode_payload(part))
        elif not is_attachment and content_type == "text/html":
            html_parts.append(_decode_payload(part))
        else:
            payload = _raw_payload(part)
            if payload is None:
                continue
            content_id = (part.get("Content-ID") or "").strip().strip("<>") or None
            attachments.append(
                Attachment(
                    filename=decode_header_value(filename) or f"part{len(attachments)}",
                    content_type=content_type,
                    data=payload,
                    content_id=content_id,
                    inline=disposition.startswith("inline"),
                )
            )

    return (
        "\n".join(p for p in text_parts if p).strip(),
        "\n".join(html_parts) if html_parts else None,
        attachments,
    )


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None


def _pick_sender(message: Message) -> EmailAddress | None:
    """Choose the author address from the headers that may carry it."""
    for header in ("Sender", "From", "Return-Path", "Reply-To"):
        addresses = parse_address_list(decode_header_value(message.get(header)))
        if addresses:
            return addresses[0]
    return None


def parse_message(
    raw: bytes,
    *,
    uid: str | None = None,
    flags: tuple[str, ...] = (),
    folder: str = "INBOX",
    strip_quotes: bool = True,
) -> EmailMessage:
    """Parse raw message bytes into an `EmailMessage`.

    Args:
        raw: The full RFC 822 message as fetched from IMAP.
        uid: The IMAP UID the message was fetched under.
        flags: IMAP flags reported alongside the fetch.
        folder: The mailbox the message came from.
        strip_quotes: Remove quoted reply history from the plain-text body.
            Turn this off when you need the verbatim body, for example when
            archiving.

    Returns:
        The parsed message. Parsing never raises on malformed input; missing
        or undecodable pieces come back empty rather than blowing up a poll
        loop over one bad message.
    """
    message = message_from_bytes(raw)
    text, html, attachments = _walk_parts(message)
    if strip_quotes and text:
        text = strip_quoted_reply(text)

    references = (message.get("References") or "").split()

    return EmailMessage(
        uid=uid,
        message_id=(message.get("Message-ID") or "").strip() or None,
        subject=decode_header_value(message.get("Subject")),
        sender=_pick_sender(message),
        to=parse_address_list(decode_header_value(message.get("To"))),
        cc=parse_address_list(decode_header_value(message.get("Cc"))),
        bcc=parse_address_list(decode_header_value(message.get("Bcc"))),
        reply_to=parse_address_list(decode_header_value(message.get("Reply-To"))),
        date=_parse_date(message.get("Date")),
        text=text,
        html=html,
        attachments=attachments,
        flags=flags,
        folder=folder,
        in_reply_to=(message.get("In-Reply-To") or "").strip() or None,
        references=references,
        headers={k.lower(): v for k, v in message.items()},
    )
