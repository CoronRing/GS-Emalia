"""In-memory stand-ins for the mail transport.

`FakeMailClient` implements the surface `emalia.tools` and
`emalia.runtime` actually use, so the suite exercises real policy and real
control flow without a server anywhere.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from emalia.errors import MessageNotFoundError
from emalia.mail.models import EmailMessage, EmailSummary


class FakeMailClient:
    """A `MailClient` substitute backed by a list of messages.

    Attributes:
        messages: The mailbox contents, newest first.
        sent: Every outgoing message recorded as a dict.
        flags: UID to set-of-flags, as tools change them.
        folder: The mailbox currently selected.
    """

    def __init__(self, messages: list[EmailMessage] | None = None) -> None:
        """
        Args:
            messages: Initial mailbox contents, newest first.
        """
        self.messages = messages or []
        self.sent: list[dict[str, Any]] = []
        self.flags: dict[str, set[str]] = {}
        self.folder = "INBOX"
        self.closed = False
        self.fail_next_poll = False

    # -- reading --------------------------------------------------------------

    def select(self, folder: str, *, readonly: bool = False) -> None:
        self.folder = folder

    def list_folders(self) -> list[str]:
        return ["INBOX", "Sent", "Archive"]

    def _find(self, uid: str) -> EmailMessage:
        for message in self.messages:
            if message.uid == uid:
                return message
        raise MessageNotFoundError(f"No message with UID {uid}.")

    def fetch(
        self,
        uid: str,
        *,
        mark_read: bool = False,
        strip_quotes: bool = True,
        folder: str | None = None,
    ) -> EmailMessage:
        return self._find(uid)

    def unread(self, *, limit: int = 20, mark_read: bool = False) -> list[EmailMessage]:
        if self.fail_next_poll:
            from emalia.errors import MailConnectionError

            self.fail_next_poll = False
            raise MailConnectionError("simulated connection drop")
        return [m for m in self.messages if "\\Seen" not in self.flags.get(m.uid or "", set())][
            :limit
        ]

    def inbox(
        self, *, limit: int = 20, unseen_only: bool = False, mark_read: bool = False
    ) -> list[EmailMessage]:
        return self.messages[:limit]

    def summaries(
        self, *, limit: int = 20, unseen_only: bool = False, folder: str = "INBOX"
    ) -> list[EmailSummary]:
        return [m.summary() for m in self.messages[:limit]]

    # -- flags ----------------------------------------------------------------

    def mark_read(self, uids: str | list[str]) -> list[str]:
        uid_list = [uids] if isinstance(uids, str) else list(uids)
        for uid in uid_list:
            self.flags.setdefault(uid, set()).add("\\Seen")
        return uid_list

    @property
    def imap(self) -> FakeMailClient:
        """The fake stands in for its own IMAP session."""
        return self

    def search(self, criteria: object = None, *, limit: int | None = None) -> list[str]:
        return [m.uid for m in self.messages[: limit or len(self.messages)] if m.uid]

    def fetch_many(self, uids: list[str], **kwargs: object) -> list[EmailMessage]:
        return [self._find(uid) for uid in uids]

    def store_flags(self, uids: list[str] | str, flag: str, *, action: str = "add") -> list[str]:
        uid_list = [uids] if isinstance(uids, str) else list(uids)
        for uid in uid_list:
            current = self.flags.setdefault(uid, set())
            if action == "add":
                current.add(flag)
            elif action == "remove":
                current.discard(flag)
            else:
                self.flags[uid] = {flag}
        return uid_list

    def mark_answered(self, uids: list[str] | str) -> list[str]:
        return self.store_flags(uids, "\\Answered", action="add")

    # -- sending --------------------------------------------------------------

    def send(
        self,
        *,
        to: str | list[str],
        subject: str,
        body: str,
        html_body: str | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        attachments: list[object] | None = None,
        footer: str | None = None,
    ) -> list[str]:
        recipients = [to] if isinstance(to, str) else list(to)
        self.sent.append(
            {
                "kind": "send",
                "to": recipients,
                "cc": list(cc or []),
                "subject": subject,
                "body": body,
                "attachments": [str(a) for a in (attachments or [])],
            }
        )
        return recipients

    def reply(
        self,
        original: EmailMessage,
        body: str,
        *,
        to: list[str] | None = None,
        **kwargs: object,
    ) -> list[str]:
        target = original.reply_target
        recipients = to or ([target.address] if target else [])
        self.sent.append(
            {
                "kind": "reply",
                "to": list(recipients),
                "subject": original.subject,
                "body": body,
                "in_reply_to": original.message_id,
            }
        )
        return list(recipients)

    def forward(
        self,
        original: EmailMessage,
        *,
        to: str | list[str],
        note: str = "",
        include_attachments: bool = True,
        footer: str | None = None,
    ) -> list[str]:
        recipients = [to] if isinstance(to, str) else list(to)
        self.sent.append(
            {
                "kind": "forward",
                "to": recipients,
                "subject": original.subject,
                "note": note,
            }
        )
        return recipients

    def save_attachments(
        self, message: EmailMessage, directory: str | Path, **kwargs: object
    ) -> list[Path]:
        from emalia.mail.compose import save_attachments as real

        return real(message, directory)

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> FakeMailClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
