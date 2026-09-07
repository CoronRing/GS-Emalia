"""The Emalia exception hierarchy.

Every error Emalia raises deliberately derives from `EmaliaError`, so a caller
embedding the toolkit can catch one class and be sure nothing of ours escapes.
"""

from __future__ import annotations

__all__ = [
    "EmaliaError",
    "ConfigurationError",
    "MailConnectionError",
    "MailAuthError",
    "MessageNotFoundError",
    "ComposeError",
    "PolicyError",
    "PermissionDeniedError",
    "SandboxViolationError",
    "RateLimitError",
    "ToolExecutionError",
]


class EmaliaError(Exception):
    """Base class for every error raised by Emalia."""


class ConfigurationError(EmaliaError):
    """Configuration is missing, contradictory, or malformed."""


class MailConnectionError(EmaliaError):
    """A mail server could not be reached, or dropped the connection."""


class MailAuthError(MailConnectionError):
    """A mail server refused the credentials."""


class MessageNotFoundError(EmaliaError):
    """A requested UID or Message-ID is not in the mailbox."""


class ComposeError(EmaliaError):
    """A message could not be assembled, usually a bad or oversized attachment."""


class PolicyError(EmaliaError):
    """Base class for refusals that come from `emalia.security.Policy`."""


class PermissionDeniedError(PolicyError):
    """The policy forbids this action, sender, or recipient."""


class SandboxViolationError(PolicyError):
    """A path resolved outside every configured sandbox root."""


class RateLimitError(PolicyError):
    """A per-run or per-hour limit has been reached."""


class ToolExecutionError(EmaliaError):
    """A tool ran but failed.

    Tools convert this to a short message for the model rather than letting a
    traceback reach an email body.
    """
