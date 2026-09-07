"""Email tools.

The group an agent needs to actually work an inbox: list, read, search, send,
reply, forward, flag, and save attachments.

Usable from any railtracks agent, not only Emalia's:

```python
import railtracks as rt
from emalia.mail import MailClient
from emalia.security import Policy
from emalia.tools import EmailTools

tools = EmailTools(MailClient.from_env(), Policy(allowed_senders=["*@me.com"]))
Agent = rt.agent_node("Inbox Agent", tool_nodes=tools.as_nodes(), llm=...)
```
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from emalia.errors import PermissionDeniedError, ToolExecutionError
from emalia.mail.client import MailClient
from emalia.mail.imap import SearchCriteria
from emalia.mail.models import summaries_to_text
from emalia.security.paths import resolve_in_sandbox
from emalia.security.policy import Policy
from emalia.tools.base import ToolGroup, tool_result

__all__ = ["EmailTools"]

logger = logging.getLogger(__name__)


class EmailTools(ToolGroup):
    """Mail tools bound to one account and one policy.

    Attributes:
        client: The mail client every tool acts through.
        policy: The policy every tool consults.
        reply_target: The address currently being answered. Set by the
            listener before each invocation, and always a permitted recipient.
            None outside a listener run.
        dry_run: Log outgoing mail instead of sending it.
    """

    toolset = "email"

    def __init__(
        self,
        client: MailClient,
        policy: Policy,
        *,
        reply_target: str | None = None,
        dry_run: bool = False,
    ) -> None:
        """
        Args:
            client: The mail client to act through.
            policy: The policy to enforce.
            reply_target: The address currently being answered, which is
                always an allowed recipient.
            dry_run: Log outgoing mail rather than sending it.
        """
        super().__init__(policy)
        self.client = client
        self.reply_target = reply_target
        self.dry_run = dry_run

    def bind_reply_target(self, address: str | None) -> None:
        """Set the address that counts as "the person being replied to".

        Args:
            address: The sender of the message currently being handled.
        """
        self.reply_target = address

    # -- internals ------------------------------------------------------------

    def _check_recipients(self, addresses: list[str]) -> None:
        for address in addresses:
            self.policy.check_recipient(address, reply_target=self.reply_target)

    def _record_send(self, recipients: list[str]) -> None:
        self.policy.record_send(1)
        logger.info("Tool sent mail to %s", ", ".join(recipients))

    @staticmethod
    def _split_addresses(value: str) -> list[str]:
        return [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]

    # -- tools ----------------------------------------------------------------

    def tools(self) -> list[Callable[..., str]]:
        """Every mail tool, as plain callables.

        Returns:
            The tool functions, in the order they are presented to the model.
        """
        return [
            self._list_inbox(),
            self._read_email(),
            self._search_email(),
            self._send_email(),
            self._reply_to_email(),
            self._forward_email(),
            self._mark_email(),
            self._list_folders(),
            self._save_attachments(),
        ]

    def _list_inbox(self) -> Callable[..., str]:
        policy = self.policy
        client = self.client

        @tool_result(policy)
        def list_inbox(limit: int = 10, unread_only: bool = False) -> str:
            """List recent messages in the inbox.

            Use this to see what is waiting before deciding what to read in
            full. Each line carries the uid needed by read_email.

            Args:
                limit: How many messages to list, newest first. Capped at 50.
                unread_only: Only list messages that have not been read.

            Returns:
                One block per message with uid, date, sender, subject and a
                short preview, or a line saying nothing matched.
            """
            summaries = client.summaries(
                limit=max(1, min(limit, 50)),
                unseen_only=unread_only,
            )
            return summaries_to_text(summaries)

        return list_inbox

    def _read_email(self) -> Callable[..., str]:
        policy = self.policy
        client = self.client

        @tool_result(policy)
        def read_email(uid: str, folder: str = "INBOX", include_quotes: bool = False) -> str:
            """Read one message in full, by its uid.

            Args:
                uid: The uid shown by list_inbox or search_email.
                folder: The mailbox holding the message.
                include_quotes: Keep the quoted history from earlier messages
                    in the thread. Off by default, since the quoted part is
                    usually noise.

            Returns:
                The message headers followed by its body and a list of
                attachment names.
            """
            message = client.fetch(uid, folder=folder, strip_quotes=not include_quotes)
            attachments = (
                ", ".join(f"{a.filename} ({a.size} bytes)" for a in message.attachments) or "none"
            )
            rendered = "\n".join(
                [
                    f"uid: {message.uid}",
                    f"from: {message.sender}",
                    f"to: {', '.join(str(a) for a in message.to) or '(none)'}",
                    f"date: {message.date.isoformat() if message.date else '(unknown)'}",
                    f"subject: {message.subject}",
                    f"attachments: {attachments}",
                    "",
                    message.body,
                ]
            )
            return rendered

        return read_email

    def _search_email(self) -> Callable[..., str]:
        policy = self.policy
        client = self.client

        @tool_result(policy)
        def search_email(
            query: str = "",
            sender: str = "",
            subject: str = "",
            since: str = "",
            folder: str = "INBOX",
            limit: int = 10,
        ) -> str:
            """Search a mailbox.

            All given criteria must match. Leave a field empty to ignore it.

            Args:
                query: Text to look for anywhere in the message.
                sender: Match against the From header, e.g. ``alice@`` or a
                    full address.
                subject: Text that must appear in the subject.
                since: Only messages on or after this date, formatted
                    ``DD-Mon-YYYY``, e.g. ``01-Jun-2026``.
                folder: The mailbox to search.
                limit: How many results to return, newest first. Capped at 50.

            Returns:
                One block per match, or a line saying nothing matched.
            """
            criteria = SearchCriteria()
            if query:
                criteria.text(query)
            if sender:
                criteria.from_(sender)
            if subject:
                criteria.subject(subject)
            if since:
                criteria.since(since)

            client.select(folder)
            uids = client.imap.search(criteria, limit=max(1, min(limit, 50)))
            summaries = [m.summary() for m in client.imap.fetch_many(uids)]
            return summaries_to_text(summaries)

        return search_email

    def _send_email(self) -> Callable[..., str]:
        client = self.client
        policy = self.policy
        check = self._check_recipients
        record = self._record_send
        split = self._split_addresses
        dry_run = self.dry_run

        @tool_result(policy)
        def send_email(
            to: str,
            subject: str,
            body: str,
            cc: str = "",
            attachment_paths: str = "",
        ) -> str:
            """Send a new email.

            Only addresses the policy permits can be reached. By default that
            is the person whose message you are handling and nobody else, so
            do not promise a user that mail will go to a third party without
            trying this first.

            Args:
                to: One recipient, or several separated by commas.
                subject: The subject line.
                body: The plain-text body.
                cc: Optional carbon-copy recipients, comma separated.
                attachment_paths: Optional file or directory paths to attach,
                    comma separated. Directories are zipped. Paths must be
                    inside the allowed directories.

            Returns:
                Confirmation naming the recipients, or the reason the send was
                refused.
            """
            recipients = split(to)
            copies = split(cc)
            if not recipients:
                raise ToolExecutionError("send_email needs at least one recipient in 'to'.")
            check(recipients + copies)
            policy.check_send_allowed()

            attachments = [
                resolve_in_sandbox(path, policy.sandbox_roots, must_exist=True)
                for path in split(attachment_paths)
            ]
            if dry_run:
                return (
                    f"[dry run] Would send {subject!r} to {', '.join(recipients)}"
                    f"{' cc ' + ', '.join(copies) if copies else ''}."
                )
            sent = client.send(
                to=recipients,
                subject=subject,
                body=body,
                cc=copies,
                attachments=attachments,
            )
            record(sent)
            return f"Sent {subject!r} to {', '.join(sent)}."

        return send_email

    def _reply_to_email(self) -> Callable[..., str]:
        client = self.client
        policy = self.policy
        record = self._record_send
        dry_run = self.dry_run

        @tool_result(policy)
        def reply_to_email(
            uid: str,
            body: str,
            folder: str = "INBOX",
            attachment_paths: str = "",
        ) -> str:
            """Reply to a message already in the mailbox, keeping the thread.

            This is for replying to some other message you found while
            working. Your answer to the person who wrote to you is the text
            you return at the end, not a call to this tool.

            Args:
                uid: The uid of the message to reply to.
                body: The plain-text reply body.
                folder: The mailbox holding the message.
                attachment_paths: Optional file or directory paths to attach,
                    comma separated.

            Returns:
                Confirmation naming the recipients, or the reason it was
                refused.
            """
            message = client.fetch(uid, folder=folder)
            target = message.reply_target
            if target is None:
                raise ToolExecutionError(f"Message {uid} has no address to reply to.")
            policy.check_recipient(target.address, reply_target=None)
            policy.check_send_allowed()

            attachments = [
                resolve_in_sandbox(path, policy.sandbox_roots, must_exist=True)
                for path in (p.strip() for p in attachment_paths.split(","))
                if path
            ]
            if dry_run:
                return f"[dry run] Would reply to {target.address} on uid {uid}."
            sent = client.reply(message, body, attachments=attachments)
            record(sent)
            return f"Replied to {', '.join(sent)} in thread {message.subject!r}."

        return reply_to_email

    def _forward_email(self) -> Callable[..., str]:
        client = self.client
        policy = self.policy
        check = self._check_recipients
        record = self._record_send
        split = self._split_addresses
        dry_run = self.dry_run

        @tool_result(policy)
        def forward_email(
            uid: str,
            to: str,
            note: str = "",
            folder: str = "INBOX",
            include_attachments: bool = True,
        ) -> str:
            """Forward a message to someone else.

            Args:
                uid: The uid of the message to forward.
                to: One recipient, or several separated by commas.
                note: Text placed above the forwarded content.
                folder: The mailbox holding the message.
                include_attachments: Carry the original's attachments along.

            Returns:
                Confirmation naming the recipients, or the reason it was
                refused.
            """
            recipients = split(to)
            if not recipients:
                raise ToolExecutionError("forward_email needs at least one recipient.")
            check(recipients)
            policy.check_send_allowed()

            message = client.fetch(uid, folder=folder, strip_quotes=False)
            if dry_run:
                return f"[dry run] Would forward uid {uid} to {', '.join(recipients)}."
            sent = client.forward(
                message,
                to=recipients,
                note=note,
                include_attachments=include_attachments,
            )
            record(sent)
            return f"Forwarded {message.subject!r} to {', '.join(sent)}."

        return forward_email

    def _mark_email(self) -> Callable[..., str]:
        client = self.client
        policy = self.policy

        @tool_result(policy)
        def mark_email(uid: str, state: str, folder: str = "INBOX") -> str:
            """Change a message's read or flagged state.

            Args:
                uid: The uid of the message, or several separated by commas.
                state: One of ``read``, ``unread``, ``flagged``, or
                    ``unflagged``.
                folder: The mailbox holding the message.

            Returns:
                Confirmation of what changed.
            """
            uids = [u.strip() for u in uid.split(",") if u.strip()]
            if not uids:
                raise ToolExecutionError("mark_email needs at least one uid.")
            client.select(folder)

            normalised = state.strip().lower()
            actions = {
                "read": lambda: client.imap.store_flags(uids, "\\Seen", action="add"),
                "unread": lambda: client.imap.store_flags(uids, "\\Seen", action="remove"),
                "flagged": lambda: client.imap.store_flags(uids, "\\Flagged", action="add"),
                "unflagged": lambda: client.imap.store_flags(uids, "\\Flagged", action="remove"),
            }
            action = actions.get(normalised)
            if action is None:
                raise ToolExecutionError(f"state must be one of {sorted(actions)}, got {state!r}.")
            action()
            return f"Marked {len(uids)} message(s) as {normalised}."

        return mark_email

    def _list_folders(self) -> Callable[..., str]:
        client = self.client
        policy = self.policy

        @tool_result(policy)
        def list_folders() -> str:
            """List every mailbox folder on the account.

            Returns:
                One folder name per line.
            """
            folders = client.list_folders()
            return "\n".join(folders) if folders else "No folders reported by the server."

        return list_folders

    def _save_attachments(self) -> Callable[..., str]:
        client = self.client
        policy = self.policy

        @tool_result(policy)
        def save_attachments(uid: str, directory: str, folder: str = "INBOX") -> str:
            """Save a message's attachments to a directory on this machine.

            Args:
                uid: The uid of the message whose attachments to save.
                directory: Where to write them. Must be inside the allowed
                    directories; it is created if it does not exist.
                folder: The mailbox holding the message.

            Returns:
                The paths written, or a note that the message had no
                attachments.
            """
            policy.require_toolset("file_write")
            target = resolve_in_sandbox(directory, policy.sandbox_roots)
            message = client.fetch(uid, folder=folder)
            if not message.attachments:
                return f"Message {uid} has no attachments."

            total = message.total_attachment_bytes()
            if total > policy.max_total_attachment_bytes:
                raise PermissionDeniedError(
                    f"Attachments total {total} bytes, over the "
                    f"{policy.max_total_attachment_bytes} byte limit."
                )
            written = client.save_attachments(message, target)
            return "Saved:\n" + "\n".join(str(p) for p in written)

        return save_attachments
