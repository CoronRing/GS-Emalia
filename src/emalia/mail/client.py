"""`MailClient`: one object for reading and sending mail.

This is the public face of the toolkit. It has no dependency on `railtracks`,
an LLM, or the rest of Emalia, so it is usable on its own:

```python
from emalia.mail import MailClient

with MailClient.from_env() as mail:
    for message in mail.inbox(limit=5):
        print(message.subject)
    mail.send(to="alice@example.com", subject="hi", body="from a script")
```
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from email.message import EmailMessage as MIMEMessage
from pathlib import Path
from types import TracebackType

from emalia.errors import MessageNotFoundError
from emalia.mail.accounts import MailAccount
from emalia.mail.compose import (
    AttachmentSource,
    build_forward,
    build_message,
    build_reply,
    save_attachments,
)
from emalia.mail.imap import ImapSession, SearchCriteria
from emalia.mail.models import EmailAddress, EmailMessage, EmailSummary
from emalia.mail.smtp import SmtpSender

__all__ = ["MailClient"]

logger = logging.getLogger(__name__)


class MailClient:
    """Read and send mail for one account.

    Connections are opened lazily on first use and reused. Using the client as
    a context manager guarantees they are closed.

    Attributes:
        account: The mailbox this client acts as.
        footer: Text appended to every outgoing body. None for no footer.
        max_attachment_bytes: Per-attachment size ceiling for sends.
        max_total_attachment_bytes: Combined size ceiling for sends.
    """

    def __init__(
        self,
        account: MailAccount,
        *,
        footer: str | None = None,
        max_attachment_bytes: int = 20 * 1024 * 1024,
        max_total_attachment_bytes: int = 20 * 1024 * 1024,
    ) -> None:
        """
        Args:
            account: The account to authenticate as.
            footer: Text appended to every outgoing body.
            max_attachment_bytes: Per-attachment size ceiling.
            max_total_attachment_bytes: Combined size ceiling.
        """
        self.account = account
        self.footer = footer
        self.max_attachment_bytes = max_attachment_bytes
        self.max_total_attachment_bytes = max_total_attachment_bytes
        self._imap: ImapSession | None = None
        self._smtp: SmtpSender | None = None

    @classmethod
    def from_env(cls, prefix: str = "EMALIA_", **kwargs: object) -> MailClient:
        """Build a client from `EMALIA_*` environment variables.

        Args:
            prefix: Environment variable prefix.
            **kwargs: Forwarded to the constructor.

        Returns:
            A configured client.

        Raises:
            ConfigurationError: If required variables are missing.
        """
        return cls(MailAccount.from_env(prefix), **kwargs)  # type: ignore[arg-type]

    # -- lifecycle ------------------------------------------------------------

    @property
    def imap(self) -> ImapSession:
        """The IMAP session, connecting on first access."""
        if self._imap is None:
            self._imap = ImapSession(self.account)
            self._imap.connect()
        return self._imap

    @property
    def smtp(self) -> SmtpSender:
        """The SMTP sender, connecting on first access."""
        if self._smtp is None:
            self._smtp = SmtpSender(self.account)
            self._smtp.connect()
        return self._smtp

    def close(self) -> None:
        """Close both connections."""
        if self._imap is not None:
            self._imap.close()
            self._imap = None
        if self._smtp is not None:
            self._smtp.close()
            self._smtp = None

    def __enter__(self) -> MailClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def check(self) -> dict[str, str]:
        """Verify that both servers accept the credentials.

        Returns:
            A dict with an ``imap`` and an ``smtp`` key, each either ``ok`` or
            the failure message. Never raises, so `emalia check` can report
            both results rather than only the first failure.
        """
        results: dict[str, str] = {}
        try:
            ImapSession(self.account).__enter__().close()
            results["imap"] = "ok"
        except Exception as exc:
            results["imap"] = str(exc)
        try:
            SmtpSender(self.account).__enter__().close()
            results["smtp"] = "ok"
        except Exception as exc:
            results["smtp"] = str(exc)
        return results

    # -- reading --------------------------------------------------------------

    def select(self, folder: str, *, readonly: bool = False) -> None:
        """Switch the mailbox subsequent reads operate on.

        Args:
            folder: The mailbox name.
            readonly: Open with ``EXAMINE`` so fetches do not set ``\\Seen``.
        """
        self.imap.select(folder, readonly=readonly)

    def list_folders(self) -> list[str]:
        """Every mailbox name on the account."""
        return self.imap.list_folders()

    def search(
        self,
        criteria: SearchCriteria | str | None = None,
        *,
        limit: int | None = None,
        folder: str | None = None,
    ) -> list[str]:
        """Search for message UIDs.

        Args:
            criteria: A `SearchCriteria`, a raw IMAP expression, or None for
                every message.
            limit: Return at most this many UIDs, newest first.
            folder: Search this mailbox instead of the selected one.

        Returns:
            Matching UIDs, newest first.
        """
        if folder:
            self.select(folder)
        return self.imap.search(criteria, limit=limit)

    def fetch(
        self,
        uid: str,
        *,
        mark_read: bool = False,
        strip_quotes: bool = True,
        folder: str | None = None,
    ) -> EmailMessage:
        """Fetch one message by UID.

        Args:
            uid: The IMAP UID.
            mark_read: Set ``\\Seen`` as a side effect.
            strip_quotes: Remove quoted reply history from the body.
            folder: Fetch from this mailbox instead of the selected one.

        Returns:
            The parsed message.

        Raises:
            MessageNotFoundError: If the UID is not in the mailbox.
        """
        if folder:
            self.select(folder)
        return self.imap.fetch(uid, mark_read=mark_read, strip_quotes=strip_quotes)

    def inbox(
        self,
        *,
        limit: int = 20,
        unseen_only: bool = False,
        mark_read: bool = False,
    ) -> list[EmailMessage]:
        """Fetch recent inbox messages.

        Args:
            limit: How many messages to fetch, newest first.
            unseen_only: Restrict to unread mail.
            mark_read: Set ``\\Seen`` on what is fetched.

        Returns:
            The parsed messages, newest first.
        """
        self.select("INBOX")
        criteria = SearchCriteria()
        if unseen_only:
            criteria.unseen()
        uids = self.imap.search(criteria, limit=limit)
        return list(self.imap.fetch_many(uids, mark_read=mark_read))

    def unread(self, *, limit: int = 20, mark_read: bool = False) -> list[EmailMessage]:
        """Fetch unread inbox messages, newest first.

        Args:
            limit: How many to fetch.
            mark_read: Set ``\\Seen`` on what is fetched.

        Returns:
            The parsed messages.
        """
        return self.inbox(limit=limit, unseen_only=True, mark_read=mark_read)

    def summaries(
        self,
        *,
        limit: int = 20,
        unseen_only: bool = False,
        folder: str = "INBOX",
    ) -> list[EmailSummary]:
        """Fetch compact summaries rather than full messages.

        Args:
            limit: How many messages to summarise.
            unseen_only: Restrict to unread mail.
            folder: The mailbox to read.

        Returns:
            One summary per message, newest first.
        """
        self.select(folder)
        criteria = SearchCriteria()
        if unseen_only:
            criteria.unseen()
        uids = self.imap.search(criteria, limit=limit)
        return [m.summary() for m in self.imap.fetch_many(uids)]

    def find_by_message_id(self, message_id: str, *, folder: str = "INBOX") -> EmailMessage:
        """Look a message up by its ``Message-ID`` header.

        Args:
            message_id: The header value, with or without angle brackets.
            folder: The mailbox to search.

        Returns:
            The parsed message.

        Raises:
            MessageNotFoundError: If nothing in the mailbox has that ID.
        """
        normalised = message_id if message_id.startswith("<") else f"<{message_id}>"
        self.select(folder)
        uids = self.imap.search(SearchCriteria().header("Message-ID", normalised), limit=1)
        if not uids:
            raise MessageNotFoundError(f"No message with Message-ID {normalised} in {folder}.")
        return self.imap.fetch(uids[0])

    # -- flags ----------------------------------------------------------------

    def mark_read(self, uids: Sequence[str] | str) -> list[str]:
        """Set ``\\Seen`` on messages."""
        return self.imap.mark_read(uids)

    def mark_unread(self, uids: Sequence[str] | str) -> list[str]:
        """Clear ``\\Seen`` on messages."""
        return self.imap.mark_unread(uids)

    def flag(self, uids: Sequence[str] | str) -> list[str]:
        """Set ``\\Flagged``, which clients render as a star."""
        return self.imap.store_flags(uids, "\\Flagged", action="add")

    def move(self, uids: Sequence[str] | str, destination: str) -> list[str]:
        """Move messages to another mailbox."""
        return self.imap.move(uids, destination)

    def delete(self, uids: Sequence[str] | str, *, expunge: bool = True) -> list[str]:
        """Flag messages ``\\Deleted`` and expunge them.

        Deliberately absent from the ``email`` toolset: the agent can read,
        send, flag and move mail, but destroying it is a decision that stays
        with the person running the process.

        Args:
            uids: One UID or several.
            expunge: Issue ``EXPUNGE`` afterwards.

        Returns:
            The UIDs acted on.
        """
        return self.imap.delete(uids, expunge=expunge)

    # -- sending --------------------------------------------------------------

    @property
    def _sender_address(self) -> EmailAddress:
        return EmailAddress(address=self.account.address, name=self.account.display_name)

    def send(
        self,
        *,
        to: str | Sequence[str] | Sequence[EmailAddress],
        subject: str,
        body: str,
        html_body: str | None = None,
        cc: Sequence[str] | Sequence[EmailAddress] = (),
        bcc: Sequence[str] | Sequence[EmailAddress] = (),
        attachments: Iterable[AttachmentSource] = (),
        footer: str | None = None,
    ) -> list[str]:
        """Compose and send a new message.

        Args:
            to: One recipient or several.
            subject: The subject line.
            body: The plain-text body.
            html_body: An optional HTML alternative.
            cc: Carbon-copy recipients.
            bcc: Blind carbon-copy recipients, stripped from the headers before
                transmission.
            attachments: Paths or `Attachment` objects. Directories are zipped.
            footer: Override the client's default footer. Pass ``""`` for none.

        Returns:
            The addresses the message was submitted for.

        Raises:
            ComposeError: If there are no recipients, or an attachment is
                missing or over a size limit.
            MailConnectionError: If the server refuses the message.
        """
        recipients = [to] if isinstance(to, str) else list(to)
        message = build_message(
            sender=self._sender_address,
            to=recipients,  # type: ignore[arg-type]
            subject=subject,
            body=body,
            html_body=html_body,
            cc=cc,
            bcc=bcc,
            attachments=attachments,
            footer=self.footer if footer is None else (footer or None),
            max_attachment_bytes=self.max_attachment_bytes,
            max_total_attachment_bytes=self.max_total_attachment_bytes,
        )
        return self._deliver(message)

    def reply(
        self,
        original: EmailMessage,
        body: str,
        *,
        html_body: str | None = None,
        attachments: Iterable[AttachmentSource] = (),
        reply_all: bool = False,
        to: Sequence[str] | Sequence[EmailAddress] | None = None,
        footer: str | None = None,
        mark_answered: bool = True,
    ) -> list[str]:
        """Reply to a message, in thread.

        Args:
            original: The message being replied to.
            body: The plain-text reply body.
            html_body: An optional HTML alternative.
            attachments: Paths or `Attachment` objects to attach.
            reply_all: Also copy the original's other recipients.
            to: Override the recipients entirely. Callers enforcing a recipient
                policy pass the vetted list here.
            footer: Override the client's default footer.
            mark_answered: Set ``\\Answered`` on the original after a
                successful send.

        Returns:
            The addresses the reply was submitted for.

        Raises:
            ComposeError: If no reply address can be determined.
            MailConnectionError: If the server refuses the message.
        """
        message = build_reply(
            original,
            sender=self._sender_address,
            body=body,
            html_body=html_body,
            attachments=attachments,
            footer=self.footer if footer is None else (footer or None),
            reply_all=reply_all,
            to=to,  # type: ignore[arg-type]
            max_attachment_bytes=self.max_attachment_bytes,
            max_total_attachment_bytes=self.max_total_attachment_bytes,
        )
        recipients = self._deliver(message)
        if mark_answered and original.uid:
            try:
                self.imap.mark_answered(original.uid)
            except Exception:
                # The reply is already out; failing to set a cosmetic flag is
                # not worth surfacing as an error to the caller.
                logger.debug("Could not set \\Answered on UID %s", original.uid, exc_info=True)
        return recipients

    def forward(
        self,
        original: EmailMessage,
        *,
        to: str | Sequence[str] | Sequence[EmailAddress],
        note: str = "",
        include_attachments: bool = True,
        footer: str | None = None,
    ) -> list[str]:
        """Forward a message.

        Args:
            original: The message to forward.
            to: One recipient or several.
            note: Text placed above the forwarded content.
            include_attachments: Carry the original's attachments along.
            footer: Override the client's default footer.

        Returns:
            The addresses the forward was submitted for.
        """
        recipients = [to] if isinstance(to, str) else list(to)
        message = build_forward(
            original,
            sender=self._sender_address,
            to=recipients,  # type: ignore[arg-type]
            note=note,
            include_attachments=include_attachments,
            footer=self.footer if footer is None else (footer or None),
            max_attachment_bytes=self.max_attachment_bytes,
            max_total_attachment_bytes=self.max_total_attachment_bytes,
        )
        return self._deliver(message)

    def _deliver(self, message: MIMEMessage) -> list[str]:
        recipients = self.smtp.send(message)
        logger.info("Sent %r to %s", message["Subject"], ", ".join(recipients))
        return recipients

    # -- attachments ----------------------------------------------------------

    def save_attachments(
        self,
        message: EmailMessage,
        directory: str | Path,
        *,
        overwrite: bool = False,
        include_inline: bool = False,
    ) -> list[Path]:
        """Write a message's attachments to a directory.

        Args:
            message: The message whose attachments to write.
            directory: Destination directory, created if missing.
            overwrite: Replace existing files instead of adding a suffix.
            include_inline: Also write parts marked ``inline``.

        Returns:
            The paths written.
        """
        return save_attachments(
            message,
            directory,
            overwrite=overwrite,
            include_inline=include_inline,
        )
