"""A reconnecting IMAP session.

The original opened and authenticated a fresh connection for every single
operation, which cost about a second per call and made a ten-message batch
take longer than the poll interval that scheduled it. `ImapSession` keeps one
connection and reconnects on the aborts servers actually send when they drop an
idle client.

UIDs, not sequence numbers, are used throughout. Sequence numbers shift when
anything else touches the mailbox, so the original's `fetch(email_id, ...)`
could silently fetch the wrong message if mail arrived mid-batch.
"""

from __future__ import annotations

import imaplib
import logging
import re
import ssl
from collections.abc import Callable, Iterator, Sequence
from types import TracebackType
from typing import Literal

from emalia.errors import MailAuthError, MailConnectionError, MessageNotFoundError
from emalia.mail.accounts import MailAccount
from emalia.mail.folders import decode_folder, quote_folder
from emalia.mail.models import EmailMessage
from emalia.mail.oauth import AUTH_FAILURE_HINTS, xoauth2_string
from emalia.mail.parse import parse_message

__all__ = ["ImapSession", "SearchCriteria"]

logger = logging.getLogger(__name__)

_AUTH_HINTS = AUTH_FAILURE_HINTS


def _xoauth2_responder(user: str, access_token: str) -> Callable[[bytes | None], bytes]:
    """Build the callback `imaplib.IMAP4.authenticate` drives for XOAUTH2.

    `imaplib` base64-encodes whatever the callback returns, so the raw SASL
    string is what goes back. On failure the server does not close the
    exchange: it sends a challenge holding a JSON error and waits for an empty
    line before reporting NO. Returning the credential again there makes the
    exchange hang, so every call after the first answers empty.

    Args:
        user: The login address.
        access_token: A current access token.

    Returns:
        A callback suitable for `authenticate`.
    """
    sent = False

    def respond(_challenge: bytes | None) -> bytes:
        nonlocal sent
        if sent:
            return b""
        sent = True
        return xoauth2_string(user, access_token).encode("utf-8")

    return respond


FlagAction = Literal["add", "remove", "replace"]

_FLAG_ACTIONS: dict[str, str] = {
    "add": "+FLAGS",
    "remove": "-FLAGS",
    "replace": "FLAGS",
}

# Servers return flags inside the FETCH response line, e.g.
# b'12 (UID 34 FLAGS (\\Seen \\Answered) BODY[] {2048}'
_FLAGS_RE = re.compile(rb"FLAGS \(([^)]*)\)")

# A LIST response line: flags, the hierarchy delimiter, then the mailbox name.
# The name is the remainder rather than a token, because it may contain spaces.
_LIST_RE = re.compile(rb'^\([^)]*\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$', re.DOTALL)


class SearchCriteria:
    """Build an IMAP SEARCH expression without hand-writing the grammar.

    Only the subset that maps cleanly onto the tool surface is exposed. Values
    are quoted, so a subject containing a quote or a space cannot break out of
    its search term.
    """

    def __init__(self) -> None:
        self._terms: list[str] = []

    @staticmethod
    def _quote(value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    def unseen(self) -> SearchCriteria:
        """Match only messages without the ``\\Seen`` flag."""
        self._terms.append("UNSEEN")
        return self

    def seen(self) -> SearchCriteria:
        """Match only messages with the ``\\Seen`` flag."""
        self._terms.append("SEEN")
        return self

    def from_(self, value: str) -> SearchCriteria:
        """Match on the ``From`` header."""
        self._terms.append(f"FROM {self._quote(value)}")
        return self

    def to(self, value: str) -> SearchCriteria:
        """Match on the ``To`` header."""
        self._terms.append(f"TO {self._quote(value)}")
        return self

    def subject(self, value: str) -> SearchCriteria:
        """Match on the ``Subject`` header."""
        self._terms.append(f"SUBJECT {self._quote(value)}")
        return self

    def body(self, value: str) -> SearchCriteria:
        """Match text anywhere in the body."""
        self._terms.append(f"BODY {self._quote(value)}")
        return self

    def text(self, value: str) -> SearchCriteria:
        """Match text anywhere in headers or body."""
        self._terms.append(f"TEXT {self._quote(value)}")
        return self

    def since(self, date: str) -> SearchCriteria:
        """Match messages on or after a date, formatted ``DD-Mon-YYYY``."""
        self._terms.append(f"SINCE {date}")
        return self

    def before(self, date: str) -> SearchCriteria:
        """Match messages before a date, formatted ``DD-Mon-YYYY``."""
        self._terms.append(f"BEFORE {date}")
        return self

    def header(self, name: str, value: str) -> SearchCriteria:
        """Match an arbitrary header."""
        self._terms.append(f"HEADER {self._quote(name)} {self._quote(value)}")
        return self

    def build(self) -> str:
        """Render the criteria. Empty criteria become ``ALL``."""
        return " ".join(self._terms) if self._terms else "ALL"

    def __str__(self) -> str:
        return self.build()


class ImapSession:
    """A logged-in IMAP connection, usable as a context manager.

    Example:
        ```python
        with ImapSession(account) as imap:
            for uid in imap.search(SearchCriteria().unseen()):
                message = imap.fetch(uid)
        ```

    Attributes:
        account: The account this session authenticates as.
        folder: The mailbox currently selected.
    """

    def __init__(
        self,
        account: MailAccount,
        *,
        folder: str = "INBOX",
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        """
        Args:
            account: Credentials and IMAP endpoint.
            folder: The mailbox to select on connect.
            ssl_context: A custom TLS context. Defaults to
                `ssl.create_default_context`, which verifies certificates.
        """
        self.account = account
        self.folder = folder
        self._ssl_context = ssl_context or ssl.create_default_context()
        self._conn: imaplib.IMAP4_SSL | None = None
        self._readonly = False

    # -- connection lifecycle -------------------------------------------------

    def connect(self) -> None:
        """Open the connection, authenticate, and select the folder.

        Raises:
            MailAuthError: If the server rejects the credentials.
            MailConnectionError: If the server cannot be reached.
        """
        try:
            conn = imaplib.IMAP4_SSL(
                host=self.account.imap_host,
                port=self.account.imap_port,
                ssl_context=self._ssl_context,
                timeout=self.account.timeout,
            )
        except (OSError, imaplib.IMAP4.error) as exc:
            raise MailConnectionError(
                f"Cannot reach IMAP {self.account.imap_host}:{self.account.imap_port}: {exc}"
            ) from exc

        try:
            if self.account.oauth is not None:
                conn.authenticate(
                    "XOAUTH2",
                    _xoauth2_responder(self.account.login, self.account.oauth.access_token()),
                )
            else:
                conn.login(self.account.login, self.account.password)
        except imaplib.IMAP4.error as exc:
            conn.logout()
            raise MailAuthError(
                f"IMAP {self.account.auth} authentication failed for {self.account.login}. "
                f"{_AUTH_HINTS[self.account.auth]} "
                f"Server said: {exc}"
            ) from exc

        self._conn = conn
        self.select(self.folder, readonly=self._readonly)

    def close(self) -> None:
        """Log out, ignoring errors from an already-dead connection."""
        if self._conn is None:
            return
        try:
            self._conn.logout()
        except (imaplib.IMAP4.error, OSError):
            logger.debug("IMAP logout failed on an already-closed connection", exc_info=True)
        finally:
            self._conn = None

    def __enter__(self) -> ImapSession:
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def _live(self) -> imaplib.IMAP4_SSL:
        if self._conn is None:
            self.connect()
        assert self._conn is not None
        return self._conn

    def _reconnect(self) -> imaplib.IMAP4_SSL:
        """Drop the current connection and build a fresh one."""
        logger.info("IMAP connection lost, reconnecting to %s", self.account.imap_host)
        self._conn = None
        self.connect()
        assert self._conn is not None
        return self._conn

    def _command(self, name: str, *args: object) -> tuple[str, list[bytes]]:
        """Run one IMAP command, reconnecting once if the server aborted.

        Args:
            name: The `imaplib` method to call, e.g. ``uid`` or ``select``.
            *args: Arguments forwarded to that method.

        Returns:
            The ``(status, response)`` pair from `imaplib`.

        Raises:
            MailConnectionError: If the command fails after one reconnect, or
                the server returns a non-OK status.
        """
        for attempt in (1, 2):
            conn = self._live if attempt == 1 else self._reconnect()
            try:
                status, response = getattr(conn, name)(*args)
            except imaplib.IMAP4.abort:
                if attempt == 2:
                    raise MailConnectionError(f"IMAP {name} aborted twice; giving up.") from None
                continue
            except (imaplib.IMAP4.error, OSError) as exc:
                raise MailConnectionError(f"IMAP {name} failed: {exc}") from exc

            if status.upper() != "OK":
                raise MailConnectionError(f"IMAP {name} returned {status}: {response!r}")
            return status, response
        raise MailConnectionError(f"IMAP {name} failed unexpectedly.")

    # -- mailbox operations ---------------------------------------------------

    def select(self, folder: str, *, readonly: bool = False) -> None:
        """Select a mailbox.

        Args:
            folder: The mailbox name as a user writes it, e.g. ``INBOX``,
                ``[Gmail]/All Mail`` or ``Wysłane``. Non-ASCII names are
                encoded to modified UTF-7 on the way out.
            readonly: Open with ``EXAMINE`` so fetches do not set ``\\Seen``.
        """
        # `self.folder` holds the decoded name: it is what error messages and
        # `EmailMessage.folder` show, and what a reconnect re-selects.
        self.folder = folder
        self._readonly = readonly
        self._command("select", quote_folder(folder), readonly)

    @staticmethod
    def _folder_name(entry: object) -> str | None:
        """Pull the mailbox name out of one LIST response entry.

        Args:
            entry: One element of the response list. Servers send a line of
                bytes, or a ``(line, literal)`` tuple when the name needs a
                literal — which is exactly what happens to the non-ASCII names
                this method exists to preserve.

        Returns:
            The name still in modified UTF-7, or None if the entry is not a
            mailbox line.
        """
        if isinstance(entry, tuple):
            # b'(\\HasNoChildren) "/" {7}' paired with the name itself.
            literal = entry[1] if len(entry) > 1 else None
            if isinstance(literal, bytes):
                return literal.decode("ascii", errors="replace")
            return None
        if not isinstance(entry, bytes):
            return None
        match = _LIST_RE.match(entry)
        if match is None:
            return None
        name = match.group("name").decode("ascii", errors="replace").strip()
        if name.startswith('"') and name.endswith('"') and len(name) > 1:
            name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        return name

    def list_folders(self) -> list[str]:
        """List every mailbox name on the account.

        Returns:
            Mailbox names decoded from modified UTF-7, so a mailbox shows up
            as ``Wysłane`` rather than ``Wys&AUI-ane``. Entries the server
            reports in an unexpected shape are skipped rather than raising, and
            a name that will not decode is returned raw rather than dropped.
        """
        _, response = self._command("list")
        folders: list[str] = []
        for entry in response:
            raw = self._folder_name(entry)
            if raw is None:
                continue
            try:
                folders.append(decode_folder(raw))
            except ValueError:
                logger.warning("Mailbox name %r is not valid UTF-7; listing it raw.", raw)
                folders.append(raw)
        return folders

    def search(
        self,
        criteria: SearchCriteria | str | None = None,
        *,
        limit: int | None = None,
        newest_first: bool = True,
    ) -> list[str]:
        """Search the selected mailbox.

        Args:
            criteria: A `SearchCriteria`, a raw IMAP expression, or None for
                ``ALL``.
            limit: Return at most this many UIDs.
            newest_first: Return the highest UIDs first. Servers return
                ascending order, which is rarely what a caller wants.

        Returns:
            Matching UIDs as strings.
        """
        expression = str(criteria) if criteria is not None else "ALL"
        _, response = self._command("uid", "SEARCH", None, expression)
        raw = response[0] if response and response[0] else b""
        uids = [part.decode() for part in raw.split()]
        if newest_first:
            uids.reverse()
        return uids[:limit] if limit else uids

    def _fetch_raw(self, uid: str, *, peek: bool) -> tuple[bytes, tuple[str, ...]]:
        section = "BODY.PEEK[]" if peek else "BODY[]"
        _, response = self._command("uid", "FETCH", uid, f"({section} FLAGS)")

        raw: bytes | None = None
        flags: tuple[str, ...] = ()
        for part in response:
            if isinstance(part, tuple) and len(part) >= 2:
                header, payload = part[0], part[1]
                if isinstance(payload, bytes):
                    raw = payload
                if isinstance(header, bytes):
                    match = _FLAGS_RE.search(header)
                    if match:
                        flags = tuple(match.group(1).decode().split())
            elif isinstance(part, bytes):
                match = _FLAGS_RE.search(part)
                if match:
                    flags = tuple(match.group(1).decode().split())

        if raw is None:
            raise MessageNotFoundError(f"No message with UID {uid} in {self.folder}.")
        return raw, flags

    def fetch(
        self,
        uid: str,
        *,
        mark_read: bool = False,
        strip_quotes: bool = True,
    ) -> EmailMessage:
        """Fetch and parse one message by UID.

        Args:
            uid: The IMAP UID.
            mark_read: Set ``\\Seen`` as a side effect. Defaults to False, the
                opposite of the original, because a listener that marks mail
                read before it has successfully answered loses that mail on a
                crash.
            strip_quotes: Remove quoted reply history from the body.

        Returns:
            The parsed message.

        Raises:
            MessageNotFoundError: If the UID is not in the selected mailbox.
        """
        raw, flags = self._fetch_raw(uid, peek=not mark_read)
        return parse_message(
            raw,
            uid=uid,
            flags=flags,
            folder=self.folder,
            strip_quotes=strip_quotes,
        )

    def fetch_many(
        self,
        uids: Sequence[str],
        *,
        mark_read: bool = False,
        strip_quotes: bool = True,
    ) -> Iterator[EmailMessage]:
        """Fetch several messages, yielding as each arrives.

        A message that fails to fetch is logged and skipped rather than
        aborting the batch, so one corrupt message cannot stall a poll loop.

        Args:
            uids: The UIDs to fetch, in the order to yield them.
            mark_read: Set ``\\Seen`` on each fetched message.
            strip_quotes: Remove quoted reply history from bodies.

        Yields:
            Each parsed message.
        """
        for uid in uids:
            try:
                yield self.fetch(uid, mark_read=mark_read, strip_quotes=strip_quotes)
            except (MessageNotFoundError, MailConnectionError):
                logger.warning("Skipping UID %s: could not be fetched", uid, exc_info=True)

    def store_flags(
        self,
        uids: Sequence[str] | str,
        flag: str,
        *,
        action: FlagAction = "add",
    ) -> list[str]:
        """Add, remove, or replace flags on messages.

        Args:
            uids: One UID or several.
            flag: The flag to apply, e.g. ``\\Seen`` or ``\\Flagged``.
            action: ``add``, ``remove``, or ``replace``.

        Returns:
            The UIDs acted on.

        Raises:
            ValueError: If `action` is not one of the three valid values.
        """
        if action not in _FLAG_ACTIONS:
            raise ValueError(f"action must be one of {sorted(_FLAG_ACTIONS)}, got {action!r}.")
        uid_list = [uids] if isinstance(uids, str) else list(uids)
        if not uid_list:
            return []
        self._command("uid", "STORE", ",".join(uid_list), _FLAG_ACTIONS[action], f"({flag})")
        return uid_list

    def mark_read(self, uids: Sequence[str] | str) -> list[str]:
        """Set ``\\Seen`` on messages."""
        return self.store_flags(uids, "\\Seen", action="add")

    def mark_unread(self, uids: Sequence[str] | str) -> list[str]:
        """Clear ``\\Seen`` on messages."""
        return self.store_flags(uids, "\\Seen", action="remove")

    def mark_answered(self, uids: Sequence[str] | str) -> list[str]:
        """Set ``\\Answered``, which clients render as a reply arrow."""
        return self.store_flags(uids, "\\Answered", action="add")

    def move(self, uids: Sequence[str] | str, destination: str) -> list[str]:
        """Copy messages to another mailbox and delete the originals.

        ``MOVE`` is not universally supported, so this is the portable
        copy-then-flag-deleted-then-expunge form.

        Args:
            uids: The UIDs to move.
            destination: The target mailbox name.

        Returns:
            The UIDs moved.
        """
        uid_list = [uids] if isinstance(uids, str) else list(uids)
        if not uid_list:
            return []
        joined = ",".join(uid_list)
        self._command("uid", "COPY", joined, quote_folder(destination))
        self.store_flags(uid_list, "\\Deleted", action="add")
        self._command("expunge")
        return uid_list

    def delete(self, uids: Sequence[str] | str, *, expunge: bool = True) -> list[str]:
        """Flag messages ``\\Deleted`` and, by default, expunge them.

        On providers that keep a trash mailbox (Gmail among them) an expunge
        from ``INBOX`` moves the mail to trash rather than destroying it, but
        that is the provider's behaviour and not a guarantee this method makes.
        Prefer `move` to an explicit trash folder when you want the mail
        recoverable.

        Args:
            uids: One UID or several.
            expunge: Issue ``EXPUNGE`` afterwards. Pass False to leave the
                messages flagged so a later expunge removes them in one go.

        Returns:
            The UIDs acted on.
        """
        uid_list = self.store_flags(uids, "\\Deleted", action="add")
        if uid_list and expunge:
            self.expunge()
        return uid_list

    def expunge(self) -> None:
        """Permanently remove every message flagged ``\\Deleted``."""
        self._command("expunge")

    def count(self, criteria: SearchCriteria | str | None = None) -> int:
        """Count messages matching criteria in the selected mailbox."""
        return len(self.search(criteria))
