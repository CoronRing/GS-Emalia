"""Assemble outgoing MIME messages.

Replies get proper ``In-Reply-To`` and ``References`` headers, which is what
makes a mail client thread them. The original set neither, so every answer
started a new conversation in the sender's client.
"""

from __future__ import annotations

import mimetypes
import os
import zipfile
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from email.message import EmailMessage as MIMEMessage
from email.utils import format_datetime, formataddr, make_msgid
from io import BytesIO
from pathlib import Path

from emalia.errors import ComposeError
from emalia.mail.models import Attachment, EmailAddress, EmailMessage

__all__ = [
    "AttachmentSource",
    "build_message",
    "build_reply",
    "build_forward",
    "load_attachment",
    "reply_subject",
    "forward_subject",
]

AttachmentSource = str | Path | Attachment

_DEFAULT_MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024


def reply_subject(subject: str) -> str:
    """Prefix a subject with ``Re:`` unless it already carries one."""
    stripped = subject.strip()
    if stripped.lower().startswith("re:"):
        return stripped
    return f"Re: {stripped}" if stripped else "Re:"


def forward_subject(subject: str) -> str:
    """Prefix a subject with ``Fwd:`` unless it already carries one."""
    stripped = subject.strip()
    if stripped.lower().startswith(("fwd:", "fw:")):
        return stripped
    return f"Fwd: {stripped}" if stripped else "Fwd:"


def _zip_directory(directory: Path) -> bytes:
    """Zip a directory into memory, preserving paths relative to it.

    The original used ``os.path.relpath(file_path, dirs)`` where ``dirs`` was
    the loop's list of subdirectory names, which produced nonsense archive
    paths. Relative paths here are taken against the directory being zipped.
    """
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(directory).as_posix())
    return buffer.getvalue()


def load_attachment(
    source: AttachmentSource,
    *,
    max_bytes: int = _DEFAULT_MAX_ATTACHMENT_BYTES,
) -> Attachment:
    """Read a path (or pass through an `Attachment`) as an attachment.

    Directories are zipped, matching the original's behaviour but with correct
    archive paths.

    Args:
        source: A file path, a directory path, or an already-built
            `Attachment`.
        max_bytes: Refuse anything larger than this once loaded or zipped.

    Returns:
        The loaded attachment.

    Raises:
        ComposeError: If the path does not exist, is neither a file nor a
            directory, or exceeds `max_bytes`.
    """
    if isinstance(source, Attachment):
        attachment = source
    else:
        path = Path(source).expanduser()
        if not path.exists():
            raise ComposeError(f"Attachment not found: {path}")
        if path.is_dir():
            data = _zip_directory(path)
            attachment = Attachment(
                filename=f"{path.name}.zip",
                content_type="application/zip",
                data=data,
            )
        elif path.is_file():
            guessed, _ = mimetypes.guess_type(path.name)
            attachment = Attachment(
                filename=path.name,
                content_type=guessed or "application/octet-stream",
                data=path.read_bytes(),
            )
        else:
            raise ComposeError(f"Not a regular file or directory: {path}")

    if attachment.size > max_bytes:
        raise ComposeError(
            f"Attachment {attachment.filename!r} is {attachment.size} bytes, "
            f"over the {max_bytes} byte limit."
        )
    return attachment


def _attach(message: MIMEMessage, attachment: Attachment) -> None:
    maintype, _, subtype = attachment.content_type.partition("/")
    message.add_attachment(
        attachment.data,
        maintype=maintype or "application",
        subtype=subtype or "octet-stream",
        filename=attachment.safe_filename(),
    )


def _format_from(sender: EmailAddress) -> str:
    return formataddr((sender.name or "", sender.address))


def build_message(
    *,
    sender: EmailAddress,
    to: Sequence[EmailAddress] | Sequence[str],
    subject: str,
    body: str,
    html_body: str | None = None,
    cc: Sequence[EmailAddress] | Sequence[str] = (),
    bcc: Sequence[EmailAddress] | Sequence[str] = (),
    attachments: Iterable[AttachmentSource] = (),
    footer: str | None = None,
    headers: dict[str, str] | None = None,
    max_attachment_bytes: int = _DEFAULT_MAX_ATTACHMENT_BYTES,
    max_total_attachment_bytes: int = _DEFAULT_MAX_ATTACHMENT_BYTES,
) -> MIMEMessage:
    """Assemble a new outgoing message.

    Args:
        sender: The ``From`` address.
        to: Recipients, as addresses or bare strings.
        subject: The subject line.
        body: The plain-text body.
        html_body: An optional HTML alternative.
        cc: Carbon-copy recipients.
        bcc: Blind carbon-copy recipients. Set as a header for the SMTP
            envelope and stripped before transmission by `SmtpSender`.
        attachments: Paths or `Attachment` objects to attach.
        footer: Text appended to the body after a blank line.
        headers: Extra headers to set verbatim.
        max_attachment_bytes: Per-attachment size ceiling.
        max_total_attachment_bytes: Combined size ceiling.

    Returns:
        A `email.message.EmailMessage` ready to hand to `SmtpSender`.

    Raises:
        ComposeError: If there are no recipients, or an attachment is missing
            or over a size limit.
    """
    recipients = [a if isinstance(a, EmailAddress) else EmailAddress.parse(a) for a in to]
    if not recipients:
        raise ComposeError("An outgoing message needs at least one recipient.")

    full_body = f"{body}\n\n{footer}" if footer else body

    message = MIMEMessage()
    message["From"] = _format_from(sender)
    message["To"] = ", ".join(str(a) for a in recipients)
    if cc:
        message["Cc"] = ", ".join(
            str(a if isinstance(a, EmailAddress) else EmailAddress.parse(a)) for a in cc
        )
    if bcc:
        message["Bcc"] = ", ".join(
            str(a if isinstance(a, EmailAddress) else EmailAddress.parse(a)) for a in bcc
        )
    message["Subject"] = subject
    message["Date"] = format_datetime(datetime.now(UTC))
    message["Message-ID"] = make_msgid(domain=sender.domain or None)
    for key, value in (headers or {}).items():
        if key in message:
            del message[key]
        message[key] = value

    message.set_content(full_body)
    if html_body:
        html_with_footer = f"{html_body}<p>{footer}</p>" if footer else html_body
        message.add_alternative(html_with_footer, subtype="html")

    total = 0
    for source in attachments:
        attachment = load_attachment(source, max_bytes=max_attachment_bytes)
        total += attachment.size
        if total > max_total_attachment_bytes:
            raise ComposeError(
                f"Attachments total {total} bytes, over the "
                f"{max_total_attachment_bytes} byte limit."
            )
        _attach(message, attachment)

    return message


def build_reply(
    original: EmailMessage,
    *,
    sender: EmailAddress,
    body: str,
    html_body: str | None = None,
    attachments: Iterable[AttachmentSource] = (),
    footer: str | None = None,
    reply_all: bool = False,
    to: Sequence[EmailAddress] | Sequence[str] | None = None,
    max_attachment_bytes: int = _DEFAULT_MAX_ATTACHMENT_BYTES,
    max_total_attachment_bytes: int = _DEFAULT_MAX_ATTACHMENT_BYTES,
) -> MIMEMessage:
    """Assemble a threaded reply to a message.

    Args:
        original: The message being replied to.
        sender: The ``From`` address.
        body: The plain-text reply body.
        html_body: An optional HTML alternative.
        attachments: Paths or `Attachment` objects to attach.
        footer: Text appended after a blank line.
        reply_all: Also copy the original's other recipients, minus `sender`.
        to: Override the recipients entirely. Callers enforcing a recipient
            policy pass the vetted list here.
        max_attachment_bytes: Per-attachment size ceiling.
        max_total_attachment_bytes: Combined size ceiling.

    Returns:
        A MIME message carrying `In-Reply-To` and `References`, so clients
        thread it under the original.

    Raises:
        ComposeError: If no reply target can be determined.
    """
    if to is not None:
        recipients = [a if isinstance(a, EmailAddress) else EmailAddress.parse(a) for a in to]
    else:
        target = original.reply_target
        if target is None:
            raise ComposeError(
                f"Message {original.message_id or original.uid} has no usable reply address."
            )
        recipients = [target]
        if reply_all:
            seen = {sender.address, target.address}
            for address in original.recipients():
                if address.address not in seen:
                    recipients.append(address)
                    seen.add(address.address)

    references = list(original.references)
    if original.message_id and original.message_id not in references:
        references.append(original.message_id)

    headers: dict[str, str] = {}
    if original.message_id:
        headers["In-Reply-To"] = original.message_id
    if references:
        headers["References"] = " ".join(references)
    # Tells well-behaved autoresponders not to answer us, which is the other
    # half of not getting stuck in a two-robot mail loop.
    headers["Auto-Submitted"] = "auto-replied"

    from emalia.mail.parse import decode_header_value

    return build_message(
        sender=sender,
        to=recipients,
        subject=reply_subject(decode_header_value(original.subject)),
        body=body,
        html_body=html_body,
        attachments=attachments,
        footer=footer,
        headers=headers,
        max_attachment_bytes=max_attachment_bytes,
        max_total_attachment_bytes=max_total_attachment_bytes,
    )


def build_forward(
    original: EmailMessage,
    *,
    sender: EmailAddress,
    to: Sequence[EmailAddress] | Sequence[str],
    note: str = "",
    footer: str | None = None,
    include_attachments: bool = True,
    max_attachment_bytes: int = _DEFAULT_MAX_ATTACHMENT_BYTES,
    max_total_attachment_bytes: int = _DEFAULT_MAX_ATTACHMENT_BYTES,
) -> MIMEMessage:
    """Assemble a forward of a message.

    Args:
        original: The message being forwarded.
        sender: The ``From`` address.
        to: Recipients of the forward.
        note: Text placed above the forwarded content.
        footer: Text appended after a blank line.
        include_attachments: Carry the original's attachments along.
        max_attachment_bytes: Per-attachment size ceiling.
        max_total_attachment_bytes: Combined size ceiling.

    Returns:
        A MIME message containing the quoted original.
    """
    header_block = "\n".join(
        [
            "---------- Forwarded message ----------",
            f"From: {original.sender or '(unknown)'}",
            f"Date: {original.date.isoformat() if original.date else '(unknown)'}",
            f"Subject: {original.subject}",
            f"To: {', '.join(str(a) for a in original.to) or '(unknown)'}",
        ]
    )
    body = (
        f"{note}\n\n{header_block}\n\n{original.body}"
        if note
        else f"{header_block}\n\n{original.body}"
    )

    return build_message(
        sender=sender,
        to=to,
        subject=forward_subject(original.subject),
        body=body,
        attachments=list(original.attachments) if include_attachments else [],
        footer=footer,
        max_attachment_bytes=max_attachment_bytes,
        max_total_attachment_bytes=max_total_attachment_bytes,
    )


def save_attachments(
    message: EmailMessage,
    directory: str | os.PathLike[str],
    *,
    overwrite: bool = False,
    include_inline: bool = False,
) -> list[Path]:
    """Write a message's attachments to disk.

    Filenames are sanitised through `Attachment.safe_filename`, so a sender
    cannot escape `directory` by naming a part ``../../autorun.inf``. Callers
    are still responsible for checking `directory` against a sandbox.

    Args:
        message: The message whose attachments to write.
        directory: Destination directory, created if missing.
        overwrite: Replace an existing file instead of adding a numeric suffix.
        include_inline: Also write parts marked ``inline``, which are usually
            embedded images rather than real attachments.

    Returns:
        The paths written, in attachment order.
    """
    target = Path(directory).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for attachment in message.attachments:
        if attachment.inline and not include_inline:
            continue
        destination = target / attachment.safe_filename()
        if destination.exists() and not overwrite:
            stem, suffix = destination.stem, destination.suffix
            index = 1
            while destination.exists():
                destination = target / f"{stem}_{index}{suffix}"
                index += 1
        destination.write_bytes(attachment.data)
        written.append(destination)
    return written
