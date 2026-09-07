"""The single object that says what Emalia is allowed to do.

The original spread this across a `permission` dict, a `vip_list`, a
`_max_send_count`, a `_file_roots` string and several "need security check"
comments. One object means there is one place to audit, and tools can consult
it whether they are called by the agent or directly by another framework.
"""

from __future__ import annotations

import fnmatch
import logging
import time
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from emalia.errors import ConfigurationError, PermissionDeniedError, RateLimitError

__all__ = ["Policy", "Toolset", "SenderDecision", "DEFAULT_TOOLSETS", "DANGEROUS_TOOLSETS"]

logger = logging.getLogger(__name__)

Toolset = Literal["email", "file_read", "file_write", "http", "shell", "python"]

DEFAULT_TOOLSETS: tuple[Toolset, ...] = ("email", "file_read")

#: Toolsets that hand arbitrary code or commands to the host. These need
#: `allow_dangerous_tools` on top of being listed in `enabled_toolsets`, so
#: that a single copied config line cannot quietly turn on shell access.
DANGEROUS_TOOLSETS: frozenset[str] = frozenset({"shell", "python"})

SenderDecision = Literal["accept", "ignore"]


@dataclass(slots=True)
class Policy:
    """Limits on what the agent and the tools may do.

    Every field has a conservative default. Widening any of them is an
    explicit act by the operator.

    Attributes:
        allowed_senders: Glob patterns matched against the sender address, for
            example ``alice@example.com`` or ``*@mycompany.com``. Empty means
            nobody, unless `allow_any_sender` is set.
        blocked_senders: Glob patterns that are always refused, checked before
            the allowlist.
        allow_any_sender: Answer mail from anyone. Off by default. Turning it
            on makes the inbox a public, unauthenticated entry point to every
            enabled tool.
        allowed_recipients: Glob patterns the agent may address mail to.
            Empty means "only the person being replied to", which is what
            stops an injected instruction from mailing your files elsewhere.
        require_token: A shared secret that must appear in the subject line of
            an incoming message. A weak second factor, but it defeats casual
            `From` spoofing.
        enabled_toolsets: Which tool groups exist at all. A group not listed
            here is never registered, so the model cannot see or request it.
        allow_dangerous_tools: Required in addition to listing `shell` or
            `python` in `enabled_toolsets`.
        sandbox_roots: Directories the file tools may read from or write to.
            Empty means the file tools refuse everything.
        max_attachment_bytes: Per-attachment ceiling, inbound and outbound.
        max_total_attachment_bytes: Combined ceiling for one message.
        max_replies_per_run: Sends allowed in a single listener run. Negative
            for no limit.
        max_replies_per_hour: Rolling hourly send ceiling. Negative for no
            limit. This is what breaks a two-agent mail loop.
        max_tool_calls: Ceiling on tool calls in one agent invocation.
        command_timeout: Seconds a shell or Python tool may run.
        max_output_chars: Truncation ceiling for any tool's text output, so a
            large file cannot blow up a context window or an email body.
    """

    allowed_senders: list[str] = field(default_factory=list)
    blocked_senders: list[str] = field(default_factory=list)
    allow_any_sender: bool = False
    allowed_recipients: list[str] = field(default_factory=list)
    require_token: str | None = None

    enabled_toolsets: list[str] = field(default_factory=lambda: list(DEFAULT_TOOLSETS))
    allow_dangerous_tools: bool = False

    sandbox_roots: list[Path] = field(default_factory=list)

    max_attachment_bytes: int = 10 * 1024 * 1024
    max_total_attachment_bytes: int = 20 * 1024 * 1024

    max_replies_per_run: int = 50
    max_replies_per_hour: int = 30
    max_tool_calls: int = 25
    command_timeout: float = 60.0
    max_output_chars: int = 20_000

    _sent_times: deque[float] = field(default_factory=deque, repr=False, compare=False)
    _sent_this_run: int = field(default=0, repr=False, compare=False)
    _tool_calls: int = field(default=0, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.sandbox_roots = [Path(p).expanduser().resolve() for p in self.sandbox_roots]
        self.allowed_senders = [s.strip().lower() for s in self.allowed_senders if s.strip()]
        self.blocked_senders = [s.strip().lower() for s in self.blocked_senders if s.strip()]
        self.allowed_recipients = [s.strip().lower() for s in self.allowed_recipients if s.strip()]

    # -- construction ---------------------------------------------------------

    @classmethod
    def permissive(cls, sandbox_roots: Sequence[str | Path] = ()) -> Policy:
        """A policy for local experimentation on a machine you own.

        Answers anyone, enables the read and write file tools and HTTP, but
        still leaves shell and Python off. Not for a mailbox that receives
        mail from the internet.

        Args:
            sandbox_roots: Directories the file tools may touch.

        Returns:
            The policy.
        """
        return cls(
            allow_any_sender=True,
            enabled_toolsets=["email", "file_read", "file_write", "http"],
            sandbox_roots=[Path(p) for p in sandbox_roots],
        )

    def validate(self) -> list[str]:
        """Check the policy for configurations that would surprise an operator.

        Returns:
            Human-readable warnings. An empty list means nothing looks wrong.
            Genuinely invalid combinations raise instead; see `Raises`.

        Raises:
            ConfigurationError: If no sender can ever be accepted, or a
                dangerous toolset is enabled without `allow_dangerous_tools`.
        """
        warnings: list[str] = []

        if not self.allow_any_sender and not self.allowed_senders:
            raise ConfigurationError(
                "No sender can ever be accepted: allowed_senders is empty and "
                "allow_any_sender is off. Add at least one allowed sender pattern."
            )
        if self.allow_any_sender:
            warnings.append(
                "allow_any_sender is on: anyone who can email this address can drive "
                "every enabled tool. Only do this on a throwaway mailbox."
            )

        dangerous = DANGEROUS_TOOLSETS.intersection(self.enabled_toolsets)
        if dangerous and not self.allow_dangerous_tools:
            raise ConfigurationError(
                f"Toolset(s) {sorted(dangerous)} need allow_dangerous_tools=true as well. "
                "They hand arbitrary commands to the host machine."
            )
        if dangerous and self.allow_dangerous_tools:
            warnings.append(
                f"Toolset(s) {sorted(dangerous)} are live: an email can run arbitrary "
                "code on this machine. Pair this with a tight sender allowlist."
            )
        if dangerous and self.allow_any_sender:
            raise ConfigurationError(
                "allow_any_sender cannot be combined with the shell or python toolsets. "
                "That is a remote code execution endpoint with no authentication."
            )

        needs_files = {"file_read", "file_write"}.intersection(self.enabled_toolsets)
        if needs_files and not self.sandbox_roots:
            warnings.append(
                f"Toolset(s) {sorted(needs_files)} are enabled but sandbox_roots is "
                "empty, so every file operation will be refused."
            )
        for root in self.sandbox_roots:
            if not root.exists():
                warnings.append(f"Sandbox root does not exist: {root}")

        if not self.allowed_recipients:
            warnings.append(
                "allowed_recipients is empty, so the agent may only reply to whoever "
                "wrote in. This is the safe default."
            )
        if self.max_replies_per_hour < 0:
            warnings.append(
                "max_replies_per_hour is unlimited, so a mail loop with another "
                "autoresponder would run until you notice."
            )
        return warnings

    # -- sender gating --------------------------------------------------------

    def sender_decision(self, address: str, *, subject: str = "") -> tuple[SenderDecision, str]:
        """Decide whether to answer a message.

        The result is deliberately binary: accept, or ignore silently. There is
        no "reject with an explanation", because a bounce to a spoofed sender
        turns the mailbox into a way to mail arbitrary people on the
        operator's behalf.

        Args:
            address: The sender's bare email address.
            subject: The subject line, checked against `require_token`.

        Returns:
            A ``(decision, reason)`` pair. The reason is for the local log.
        """
        normalised = address.strip().lower()
        if not normalised:
            return "ignore", "no sender address"

        for pattern in self.blocked_senders:
            if fnmatch.fnmatch(normalised, pattern):
                return "ignore", f"sender matches blocklist entry {pattern!r}"

        if not self.allow_any_sender and not any(
            fnmatch.fnmatch(normalised, p) for p in self.allowed_senders
        ):
            return "ignore", "sender is not on the allowlist"

        if self.require_token and self.require_token not in subject:
            return "ignore", "subject does not carry the required token"

        return "accept", "allowed"

    def check_recipient(self, address: str, *, reply_target: str | None = None) -> None:
        """Assert the agent may send mail to an address.

        Args:
            address: The proposed recipient.
            reply_target: The address of whoever is being replied to. Always
                permitted, which is what makes an empty `allowed_recipients`
                usable rather than paralysing.

        Raises:
            PermissionDeniedError: If the address is not permitted.
        """
        normalised = address.strip().lower()
        if reply_target and normalised == reply_target.strip().lower():
            return
        if any(fnmatch.fnmatch(normalised, p) for p in self.allowed_recipients):
            return
        raise PermissionDeniedError(
            f"Not allowed to send mail to {address}. "
            "Only the sender of the message being handled can be addressed, "
            "unless allowed_recipients is widened in the policy."
        )

    # -- toolset gating -------------------------------------------------------

    def toolset_enabled(self, toolset: str) -> bool:
        """Whether a tool group should be registered.

        Args:
            toolset: The group name.

        Returns:
            True when the group is listed and, for dangerous groups, when
            `allow_dangerous_tools` is also set.
        """
        if toolset not in self.enabled_toolsets:
            return False
        return not (toolset in DANGEROUS_TOOLSETS and not self.allow_dangerous_tools)

    def require_toolset(self, toolset: str) -> None:
        """Assert a tool group is enabled, for tools called directly.

        Args:
            toolset: The group name.

        Raises:
            PermissionDeniedError: If the group is not enabled.
        """
        if not self.toolset_enabled(toolset):
            raise PermissionDeniedError(f"The {toolset!r} toolset is not enabled in this policy.")

    # -- rate limiting --------------------------------------------------------

    def check_send_allowed(self) -> None:
        """Assert another outgoing message is within the send limits.

        Raises:
            RateLimitError: If the per-run or per-hour ceiling is reached.
        """
        if 0 <= self.max_replies_per_run <= self._sent_this_run:
            raise RateLimitError(f"Reached the per-run send limit of {self.max_replies_per_run}.")
        if self.max_replies_per_hour >= 0:
            cutoff = time.monotonic() - 3600
            while self._sent_times and self._sent_times[0] < cutoff:
                self._sent_times.popleft()
            if len(self._sent_times) >= self.max_replies_per_hour:
                raise RateLimitError(
                    f"Reached the hourly send limit of {self.max_replies_per_hour}."
                )

    def record_send(self, count: int = 1) -> None:
        """Record that messages were sent, for the rate limiters.

        Args:
            count: How many messages went out.
        """
        now = time.monotonic()
        self._sent_this_run += count
        for _ in range(count):
            self._sent_times.append(now)

    def charge_tool_call(self) -> None:
        """Count one tool call against the per-request budget.

        Called by every tool through `emalia.tools.tool_result`. The counter is
        reset by `reset_tool_calls` before each incoming message, so the budget
        bounds one request rather than the daemon's whole lifetime.

        Raises:
            RateLimitError: If `max_tool_calls` has already been reached.
        """
        if 0 <= self.max_tool_calls <= self._tool_calls:
            raise RateLimitError(
                f"Reached the limit of {self.max_tool_calls} tool calls for this request. "
                "Answer with what you have."
            )
        self._tool_calls += 1

    def reset_tool_calls(self) -> None:
        """Clear the per-request tool-call counter."""
        self._tool_calls = 0

    @property
    def tool_calls_used(self) -> int:
        """How many tool calls have been made since the last reset."""
        return self._tool_calls

    def reset_run_counter(self) -> None:
        """Clear the per-run send counter. Called when a listener starts."""
        self._sent_this_run = 0

    @property
    def sent_this_run(self) -> int:
        """How many messages have gone out since the last run reset."""
        return self._sent_this_run

    # -- reporting ------------------------------------------------------------

    def describe(self) -> str:
        """A prose summary of the policy, for the agent's system prompt.

        Returns:
            Text listing enabled capabilities and hard limits, so the model
            explains refusals accurately instead of guessing at them.
        """
        lines = [f"Enabled capabilities: {', '.join(sorted(self.active_toolsets())) or 'none'}."]
        if self.sandbox_roots:
            roots = ", ".join(str(r) for r in self.sandbox_roots)
            lines.append(f"File access is limited to these directories and below: {roots}.")
        else:
            lines.append("There is no file access.")
        if self.allowed_recipients:
            lines.append(
                "Outgoing mail may only be addressed to the person you are replying to, "
                f"or to addresses matching: {', '.join(self.allowed_recipients)}."
            )
        else:
            lines.append("Outgoing mail may only be addressed to the person you are replying to.")
        lines.append(
            f"At most {self.max_tool_calls} tool calls per request, and command output "
            f"is truncated at {self.max_output_chars} characters."
        )
        return " ".join(lines)

    def active_toolsets(self) -> list[str]:
        """The tool groups that will actually be registered."""
        return [t for t in self.enabled_toolsets if self.toolset_enabled(t)]

    def as_dict(self) -> dict[str, object]:
        """A serialisable view, with no secrets in it.

        `require_token` is reported only as present or absent.
        """
        return {
            "allowed_senders": list(self.allowed_senders),
            "blocked_senders": list(self.blocked_senders),
            "allow_any_sender": self.allow_any_sender,
            "allowed_recipients": list(self.allowed_recipients),
            "require_token": bool(self.require_token),
            "enabled_toolsets": list(self.enabled_toolsets),
            "active_toolsets": self.active_toolsets(),
            "allow_dangerous_tools": self.allow_dangerous_tools,
            "sandbox_roots": [str(r) for r in self.sandbox_roots],
            "max_attachment_bytes": self.max_attachment_bytes,
            "max_replies_per_run": self.max_replies_per_run,
            "max_replies_per_hour": self.max_replies_per_hour,
            "max_tool_calls": self.max_tool_calls,
            "command_timeout": self.command_timeout,
        }


def truncate(text: str, limit: int) -> str:
    """Cut text to a character limit, saying so when it cuts.

    Args:
        text: The text to bound.
        limit: Maximum characters to keep.

    Returns:
        The text, with a marker appended when it was shortened.
    """
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n\n[truncated: {len(text) - limit} more characters]"


def normalise_patterns(values: Iterable[str] | str | None) -> list[str]:
    """Accept a comma-separated string or an iterable as a pattern list.

    Args:
        values: A comma-separated string, an iterable of patterns, or None.

    Returns:
        Cleaned, lowercased patterns with blanks dropped.
    """
    if values is None:
        return []
    if isinstance(values, str):
        parts: Iterable[str] = values.split(",")
    else:
        parts = values
    return [p.strip().lower() for p in parts if p and p.strip()]
