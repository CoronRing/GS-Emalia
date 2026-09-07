"""The listener loop: poll, gate, dispatch, reply.

This is the original's `main_loop` with the gaps filled. The order of the steps
matters and is not arbitrary:

1. Gate before parsing anything expensive, so unwanted mail costs nothing.
2. Dedupe on Message-ID before invoking a model, so a crash cannot bill twice.
3. Mark read only *after* a successful reply, so a crash mid-handling leaves
   the message to be picked up again rather than silently dropped. The
   original marked mail read at fetch time and lost it on any later failure.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import traceback
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import railtracks as rt

from emalia.agent import build_agent, render_incoming
from emalia.config import EmaliaConfig
from emalia.errors import EmaliaError, MailConnectionError, RateLimitError
from emalia.mail.client import MailClient
from emalia.mail.models import EmailMessage
from emalia.runtime.state import AuditLog, SeenStore
from emalia.tools.email_tools import EmailTools

__all__ = ["EmaliaListener", "ListenerStats"]

logger = logging.getLogger(__name__)

_FAILURE_BODY = (
    "Something went wrong while handling your message, and it could not be "
    "completed. The details were written to the log on the machine running "
    "this assistant. Try again, or rephrase what you asked for."
)


@dataclass(slots=True)
class ListenerStats:
    """Counters for one listener run.

    Attributes:
        started_at: When the run began.
        polls: How many poll cycles have completed.
        considered: Messages looked at, including ignored ones.
        handled: Messages answered successfully.
        ignored: Messages dropped by the sender gate or the deduplicator.
        failed: Messages that raised while being handled.
    """

    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    polls: int = 0
    considered: int = 0
    handled: int = 0
    ignored: int = 0
    failed: int = 0

    def as_dict(self) -> dict[str, object]:
        """A serialisable view of the counters."""
        return {
            "started_at": self.started_at.isoformat(),
            "polls": self.polls,
            "considered": self.considered,
            "handled": self.handled,
            "ignored": self.ignored,
            "failed": self.failed,
        }


class EmaliaListener:
    """Watches a mailbox and answers what arrives.

    Example:
        ```python
        config = EmaliaConfig.load()
        listener = EmaliaListener(config)
        listener.run()  # blocks until interrupted
        ```

    Attributes:
        config: The instance configuration.
        client: The mail client the listener and its tools share.
        stats: Counters for the current run.
        running: False once `stop` has been called.
    """

    def __init__(
        self,
        config: EmaliaConfig,
        *,
        client: MailClient | None = None,
        extra_tools: Sequence[object] = (),
    ) -> None:
        """
        Args:
            config: The instance configuration.
            client: A mail client to use. One is built from the config's
                account when omitted.
            extra_tools: Additional railtracks nodes or callables for the
                agent, the replacement for the original's custom tasks.

        Raises:
            ConfigurationError: If the policy is unusable, for instance
                because no sender could ever be accepted.
        """
        self.config = config
        self.client = client or MailClient(
            config.account,
            footer=config.effective_footer,
            max_attachment_bytes=config.policy.max_attachment_bytes,
            max_total_attachment_bytes=config.policy.max_total_attachment_bytes,
        )

        for warning in config.policy.validate():
            logger.warning("policy: %s", warning)

        self._email_tools = EmailTools(self.client, config.policy, dry_run=config.dry_run)
        agent = build_agent(
            config,
            client=self.client,
            extra_tools=extra_tools,
        )
        self._flow: rt.Flow[Any, Any] = rt.Flow(
            name=f"{config.instance_name} Flow", entry_point=agent
        )

        self._seen = SeenStore(config.seen_path)
        self._audit = AuditLog(config.audit_path)
        self.stats = ListenerStats()
        self.running = False
        self._stop_event: asyncio.Event | None = None

    # -- control --------------------------------------------------------------

    def stop(self) -> None:
        """Ask the loop to finish the message it is on and then exit."""
        self.running = False
        if self._stop_event is not None:
            self._stop_event.set()

    def run(self) -> ListenerStats:
        """Run the loop until `stop` is called or the process is interrupted.

        Returns:
            The run's counters.
        """
        return asyncio.run(self.arun())

    def run_once(self) -> ListenerStats:
        """Process at most one batch and return. Suits cron.

        Returns:
            The run's counters.
        """
        return asyncio.run(self.arun_once())

    # -- loops ----------------------------------------------------------------

    async def arun(self) -> ListenerStats:
        """Run the poll loop until stopped.

        Returns:
            The run's counters.
        """
        self.running = True
        self._stop_event = asyncio.Event()
        self.config.policy.reset_run_counter()
        self.stats = ListenerStats()
        logger.info(
            "%s is watching %s, polling every %.0fs (capabilities: %s)",
            self.config.instance_name,
            self.config.account.address,
            self.config.poll_interval,
            ", ".join(self.config.policy.active_toolsets()) or "none",
        )
        self._audit.record("started", detail=self.config.account.address)

        try:
            while self.running:
                started = asyncio.get_running_loop().time()
                await self._poll_once()
                elapsed = asyncio.get_running_loop().time() - started
                remaining = self.config.poll_interval - elapsed
                if remaining > 0 and self.running:
                    # A stop request wakes the loop immediately; the timeout is
                    # the ordinary path back round to the next poll.
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._stop_event.wait(), timeout=remaining)
        finally:
            self.running = False
            self.client.close()
            self._audit.record("stopped", detail=str(self.stats.as_dict()))
            logger.info("%s stopped: %s", self.config.instance_name, self.stats.as_dict())
        return self.stats

    async def arun_once(self) -> ListenerStats:
        """Process at most one batch.

        Returns:
            The run's counters.
        """
        self.running = True
        self._stop_event = asyncio.Event()
        self.config.policy.reset_run_counter()
        self.stats = ListenerStats()
        try:
            await self._poll_once()
        finally:
            self.running = False
            self.client.close()
        return self.stats

    async def _poll_once(self) -> None:
        """Fetch and handle one batch of unread mail."""
        self.stats.polls += 1
        try:
            messages = await asyncio.to_thread(
                self.client.unread,
                limit=self.config.batch_size,
                mark_read=False,
            )
        except MailConnectionError as exc:
            # A dropped connection is routine on a long-lived daemon. Log it
            # and let the next poll reconnect rather than tearing down the run.
            logger.warning("Poll failed, will retry next interval: %s", exc)
            self._audit.record("poll_failed", detail=str(exc))
            return

        if not messages:
            return

        logger.info("Found %d unread message(s)", len(messages))
        if self.config.max_concurrent > 1:
            semaphore = asyncio.Semaphore(self.config.max_concurrent)

            async def guarded(message: EmailMessage) -> None:
                async with semaphore:
                    await self._handle(message)

            await asyncio.gather(*(guarded(m) for m in messages))
        else:
            for message in messages:
                if not self.running:
                    break
                await self._handle(message)

    # -- handling -------------------------------------------------------------

    def _gate(self, message: EmailMessage) -> tuple[bool, str]:
        """Decide whether a message should be answered.

        Args:
            message: The parsed incoming message.

        Returns:
            An ``(accept, reason)`` pair.
        """
        sender = message.sender.address if message.sender else ""

        if sender and sender == self.config.account.address.lower():
            return False, "message is from this mailbox itself"

        # Standard auto-response headers. Answering an out-of-office reply is
        # how two robots end up mailing each other until someone notices.
        headers = message.headers
        if headers.get("auto-submitted", "").lower().startswith("auto"):
            return False, "message is marked auto-submitted"
        if headers.get("x-autoreply") or headers.get("x-autorespond"):
            return False, "message is an autoresponder"
        if headers.get("precedence", "").lower() in ("bulk", "list", "junk"):
            return False, "message has a bulk or list precedence"
        if headers.get("list-id") or headers.get("list-unsubscribe"):
            return False, "message came from a mailing list"

        if message.message_id and message.message_id in self._seen:
            return False, "message has already been handled"

        decision, reason = self.config.policy.sender_decision(sender, subject=message.subject)
        return decision == "accept", reason

    async def _handle(self, message: EmailMessage) -> None:
        """Gate, dispatch, and reply to one message."""
        self.stats.considered += 1
        sender = message.sender.address if message.sender else ""
        accepted, reason = self._gate(message)

        if not accepted:
            self.stats.ignored += 1
            logger.info("Ignoring message from %s: %s", sender or "(unknown)", reason)
            self._audit.record(
                "ignored",
                message_id=message.message_id,
                sender=sender,
                subject=message.subject,
                outcome=reason,
            )
            # Mark read so the same message is not reconsidered every poll.
            # No reply is sent: a bounce to a spoofed sender would turn this
            # mailbox into a way to mail strangers on the operator's behalf.
            await self._mark_read(message)
            if message.message_id:
                self._seen.add(message.message_id)
            return

        logger.info("Handling %r from %s", message.subject, sender)
        self.config.policy.reset_tool_calls()
        self._email_tools.bind_reply_target(sender)

        try:
            reply_body = await self._invoke(message)
        except RateLimitError as exc:
            self.stats.failed += 1
            logger.warning("Rate limit hit handling %s: %s", message.message_id, exc)
            self._audit.record(
                "rate_limited",
                message_id=message.message_id,
                sender=sender,
                subject=message.subject,
                outcome=str(exc),
            )
            return
        except Exception as exc:
            self.stats.failed += 1
            logger.error(
                "Failed to handle %s: %s\n%s",
                message.message_id,
                exc,
                traceback.format_exc(),
            )
            self._audit.record(
                "failed",
                message_id=message.message_id,
                sender=sender,
                subject=message.subject,
                outcome=type(exc).__name__,
                detail=str(exc),
            )
            await self._reply(message, _FAILURE_BODY, outcome="failure_notice")
            return

        await self._reply(message, reply_body, outcome="answered")
        self.stats.handled += 1

    async def _invoke(self, message: EmailMessage) -> str:
        """Run the agent over one message.

        Args:
            message: The parsed incoming message.

        Returns:
            The reply body the agent produced.
        """
        prompt = render_incoming(message)
        # A connection per invocation is what railtracks requires for
        # concurrent runs of one flow, and it costs nothing when serial.
        connection = self._flow.connect()
        response = await connection.ainvoke(prompt)
        text = getattr(response, "text", None)
        return text if isinstance(text, str) and text.strip() else str(response)

    async def _reply(self, message: EmailMessage, body: str, *, outcome: str) -> None:
        """Send a reply and record the outcome."""
        sender = message.sender.address if message.sender else ""
        if self.config.dry_run:
            logger.info("[dry run] Would reply to %s:\n%s", sender, body)
            self._audit.record(
                "dry_run",
                message_id=message.message_id,
                sender=sender,
                subject=message.subject,
                outcome=outcome,
            )
        else:
            try:
                self.config.policy.check_send_allowed()
                await asyncio.to_thread(self.client.reply, message, body)
                self.config.policy.record_send()
            except EmaliaError as exc:
                logger.error("Could not reply to %s: %s", sender, exc)
                self._audit.record(
                    "reply_failed",
                    message_id=message.message_id,
                    sender=sender,
                    subject=message.subject,
                    outcome=type(exc).__name__,
                    detail=str(exc),
                )
                return
            self._audit.record(
                "handled",
                message_id=message.message_id,
                sender=sender,
                subject=message.subject,
                outcome=outcome,
                tool_calls=self.config.policy.tool_calls_used,
            )

        await self._mark_read(message)
        if message.message_id:
            self._seen.add(message.message_id)

    async def _mark_read(self, message: EmailMessage) -> None:
        """Flag a message read, tolerating a server that refuses."""
        if not message.uid:
            return
        try:
            await asyncio.to_thread(self.client.mark_read, message.uid)
        except EmaliaError:
            logger.warning("Could not mark UID %s read", message.uid, exc_info=True)
