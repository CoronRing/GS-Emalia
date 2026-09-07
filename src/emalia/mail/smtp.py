"""SMTP sending.

Supports both implicit TLS (port 465) and STARTTLS (port 587). The original
only handled `SMTP_SSL`, which ruled out Outlook and iCloud.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage as MIMEMessage
from email.utils import getaddresses
from types import TracebackType

from emalia.errors import MailAuthError, MailConnectionError
from emalia.mail.accounts import MailAccount
from emalia.mail.oauth import AUTH_FAILURE_HINTS, xoauth2_string

__all__ = ["SmtpSender"]

logger = logging.getLogger(__name__)


class SmtpSender:
    """A logged-in SMTP connection, usable as a context manager.

    Example:
        ```python
        with SmtpSender(account) as smtp:
            smtp.send(message)
        ```

    Attributes:
        account: The account this sender authenticates as.
    """

    def __init__(
        self,
        account: MailAccount,
        *,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        """
        Args:
            account: Credentials and SMTP endpoint.
            ssl_context: A custom TLS context. Defaults to
                `ssl.create_default_context`, which verifies certificates.
        """
        self.account = account
        self._ssl_context = ssl_context or ssl.create_default_context()
        self._conn: smtplib.SMTP | None = None

    def connect(self) -> None:
        """Open the connection and authenticate.

        Raises:
            MailAuthError: If the server rejects the credentials.
            MailConnectionError: If the server cannot be reached.
        """
        try:
            if self.account.smtp_security == "ssl":
                conn: smtplib.SMTP = smtplib.SMTP_SSL(
                    host=self.account.smtp_host,
                    port=self.account.smtp_port,
                    context=self._ssl_context,
                    timeout=self.account.timeout,
                )
            else:
                conn = smtplib.SMTP(
                    host=self.account.smtp_host,
                    port=self.account.smtp_port,
                    timeout=self.account.timeout,
                )
                conn.ehlo()
                conn.starttls(context=self._ssl_context)
                conn.ehlo()
        except (OSError, smtplib.SMTPException) as exc:
            raise MailConnectionError(
                f"Cannot reach SMTP {self.account.smtp_host}:{self.account.smtp_port}: {exc}"
            ) from exc

        try:
            if self.account.oauth is not None:
                # `auth` reads the server's advertised mechanisms, which are
                # only known after EHLO. `login` does that itself; `auth` does
                # not, and on an implicit-TLS connection nothing has sent one
                # yet.
                conn.ehlo_or_helo_if_needed()
                token = self.account.oauth.access_token()
                conn.auth(
                    "XOAUTH2",
                    lambda challenge=None: xoauth2_string(self.account.login, token),
                    initial_response_ok=True,
                )
            else:
                conn.login(self.account.login, self.account.password)
        except smtplib.SMTPAuthenticationError as exc:
            conn.quit()
            raise MailAuthError(
                f"SMTP {self.account.auth} authentication failed for {self.account.login}. "
                f"{AUTH_FAILURE_HINTS[self.account.auth]} "
                f"Server said: {exc}"
            ) from exc
        except smtplib.SMTPException as exc:
            conn.quit()
            raise MailConnectionError(f"SMTP login failed: {exc}") from exc

        self._conn = conn

    def close(self) -> None:
        """Close the connection, ignoring errors from a dead one."""
        if self._conn is None:
            return
        try:
            self._conn.quit()
        except (smtplib.SMTPException, OSError):
            logger.debug("SMTP quit failed on an already-closed connection", exc_info=True)
        finally:
            self._conn = None

    def __enter__(self) -> SmtpSender:
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @staticmethod
    def envelope_recipients(message: MIMEMessage) -> list[str]:
        """Collect every address the message should actually go to.

        Args:
            message: The assembled MIME message.

        Returns:
            The addresses from To, Cc and Bcc, deduplicated, in header order.
        """
        raw = []
        for header in ("To", "Cc", "Bcc"):
            raw.extend(message.get_all(header, []))
        seen: set[str] = set()
        recipients: list[str] = []
        for _, address in getaddresses(raw):
            normalised = address.strip().lower()
            if normalised and normalised not in seen:
                seen.add(normalised)
                recipients.append(address)
        return recipients

    def send(self, message: MIMEMessage) -> list[str]:
        """Send an assembled message.

        ``Bcc`` is read for the envelope and then removed from the headers, so
        blind recipients stay blind. Sending it verbatim, as the original's
        `send_message` path did, discloses them to everyone on the message.

        Args:
            message: The MIME message to send.

        Returns:
            The addresses the message was submitted for.

        Raises:
            MailConnectionError: If the server refuses the message, or the
                connection drops mid-send.
        """
        recipients = self.envelope_recipients(message)
        if not recipients:
            raise MailConnectionError("Message has no recipients in To, Cc or Bcc.")

        if "Bcc" in message:
            del message["Bcc"]

        for attempt in (1, 2):
            if self._conn is None:
                self.connect()
            assert self._conn is not None
            try:
                self._conn.send_message(
                    message,
                    from_addr=self.account.address,
                    to_addrs=recipients,
                )
                return recipients
            except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError):
                # Providers drop idle SMTP sessions aggressively; one silent
                # reconnect is the difference between a working daemon and one
                # that fails its first send after a quiet hour.
                self._conn = None
                if attempt == 2:
                    raise MailConnectionError("SMTP connection dropped twice; giving up.") from None
            except smtplib.SMTPException as exc:
                raise MailConnectionError(f"SMTP send failed: {exc}") from exc
        raise MailConnectionError("SMTP send failed unexpectedly.")
