"""Mail account credentials and server endpoints.

The original hardcoded Gmail and left "support common smtp and imap other than
gmail" as a TODO. `MailAccount.for_provider` covers the providers people
actually use, and `MailAccount` itself accepts explicit hosts for everything
else.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Literal

from emalia.errors import ConfigurationError

__all__ = ["MailAccount", "ProviderPreset", "PROVIDER_PRESETS"]

SecurityMode = Literal["ssl", "starttls"]


@dataclass(frozen=True, slots=True)
class ProviderPreset:
    """Server endpoints for a known mail provider."""

    imap_host: str
    imap_port: int
    smtp_host: str
    smtp_port: int
    smtp_security: SecurityMode
    note: str = ""


PROVIDER_PRESETS: dict[str, ProviderPreset] = {
    "gmail": ProviderPreset(
        imap_host="imap.gmail.com",
        imap_port=993,
        smtp_host="smtp.gmail.com",
        smtp_port=465,
        smtp_security="ssl",
        note="Requires an App Password with 2FA enabled; the account password will not work.",
    ),
    "outlook": ProviderPreset(
        imap_host="outlook.office365.com",
        imap_port=993,
        smtp_host="smtp-mail.outlook.com",
        smtp_port=587,
        smtp_security="starttls",
        note="Personal accounts need an App Password; many tenants disable basic auth entirely.",
    ),
    "yahoo": ProviderPreset(
        imap_host="imap.mail.yahoo.com",
        imap_port=993,
        smtp_host="smtp.mail.yahoo.com",
        smtp_port=465,
        smtp_security="ssl",
        note="Requires an App Password.",
    ),
    "icloud": ProviderPreset(
        imap_host="imap.mail.me.com",
        imap_port=993,
        smtp_host="smtp.mail.me.com",
        smtp_port=587,
        smtp_security="starttls",
        note="Requires an app-specific password.",
    ),
    "fastmail": ProviderPreset(
        imap_host="imap.fastmail.com",
        imap_port=993,
        smtp_host="smtp.fastmail.com",
        smtp_port=465,
        smtp_security="ssl",
        note="Create an app password scoped to mail.",
    ),
    "zoho": ProviderPreset(
        imap_host="imap.zoho.com",
        imap_port=993,
        smtp_host="smtp.zoho.com",
        smtp_port=465,
        smtp_security="ssl",
    ),
    "proton": ProviderPreset(
        imap_host="127.0.0.1",
        imap_port=1143,
        smtp_host="127.0.0.1",
        smtp_port=1025,
        smtp_security="starttls",
        note="Goes through the local Proton Mail Bridge, which must be running.",
    ),
}


def _parse_security(raw: str | None, prefix: str) -> SecurityMode | None:
    """Validate an SMTP security mode read from the environment.

    Args:
        raw: The variable's value, or None when it is unset.
        prefix: The variable-name prefix, for the error message.

    Returns:
        The validated mode, or None when the variable is unset.

    Raises:
        ConfigurationError: If the value is neither ``ssl`` nor ``starttls``.
    """
    if not raw:
        return None
    normalised = raw.strip().lower()
    if normalised == "ssl":
        return "ssl"
    if normalised == "starttls":
        return "starttls"
    raise ConfigurationError(f"{prefix}SMTP_SECURITY must be 'ssl' or 'starttls', got {raw!r}.")


@dataclass(frozen=True, slots=True)
class MailAccount:
    """Everything needed to log in to one mailbox.

    Attributes:
        address: The account's own email address, also the login username
            unless `username` is set.
        password: The password or app password. Never logged.
        imap_host: IMAP server hostname.
        imap_port: IMAP server port. 993 for implicit TLS.
        smtp_host: SMTP server hostname.
        smtp_port: SMTP server port.
        smtp_security: ``ssl`` for implicit TLS on connect, ``starttls`` to
            upgrade a plaintext connection.
        username: Login name, when it differs from `address`.
        display_name: The name shown to recipients in the ``From`` header.
        timeout: Socket timeout in seconds for both protocols.
    """

    address: str
    password: str
    imap_host: str
    imap_port: int = 993
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_security: SecurityMode = "ssl"
    username: str | None = None
    display_name: str | None = None
    timeout: float = 30.0

    def __post_init__(self) -> None:
        if not self.address:
            raise ConfigurationError("MailAccount requires an address.")
        if not self.password:
            raise ConfigurationError(
                f"MailAccount for {self.address} has no password. "
                "Set EMALIA_PASSWORD or pass password= explicitly."
            )
        if not self.imap_host:
            raise ConfigurationError(
                f"MailAccount for {self.address} has no imap_host. "
                "Pass one, or use MailAccount.for_provider(...)."
            )
        if not self.smtp_host:
            object.__setattr__(self, "smtp_host", self.imap_host.replace("imap", "smtp", 1))

    @property
    def login(self) -> str:
        """The username to authenticate with."""
        return self.username or self.address

    @classmethod
    def for_provider(
        cls,
        provider: str,
        address: str,
        password: str,
        *,
        display_name: str | None = None,
        timeout: float = 30.0,
    ) -> MailAccount:
        """Build an account from a known provider's endpoints.

        Args:
            provider: A key of `PROVIDER_PRESETS`, case-insensitive.
            address: The mailbox address.
            password: The password or app password.
            display_name: Optional ``From`` display name.
            timeout: Socket timeout in seconds.

        Returns:
            An account wired to that provider's servers.

        Raises:
            ConfigurationError: If the provider is not a known preset.
        """
        preset = PROVIDER_PRESETS.get(provider.strip().lower())
        if preset is None:
            known = ", ".join(sorted(PROVIDER_PRESETS))
            raise ConfigurationError(f"Unknown provider {provider!r}. Known providers: {known}.")
        return cls(
            address=address,
            password=password,
            imap_host=preset.imap_host,
            imap_port=preset.imap_port,
            smtp_host=preset.smtp_host,
            smtp_port=preset.smtp_port,
            smtp_security=preset.smtp_security,
            display_name=display_name,
            timeout=timeout,
        )

    @classmethod
    def from_env(cls, prefix: str = "EMALIA_") -> MailAccount:
        """Build an account from environment variables.

        Reads ``<prefix>ADDRESS``, ``<prefix>PASSWORD``, and either
        ``<prefix>PROVIDER`` or the explicit host and port variables
        ``<prefix>IMAP_HOST``, ``<prefix>IMAP_PORT``, ``<prefix>SMTP_HOST``,
        ``<prefix>SMTP_PORT``, ``<prefix>SMTP_SECURITY``.

        Args:
            prefix: Variable-name prefix.

        Returns:
            The configured account.

        Raises:
            ConfigurationError: If required variables are missing, or if
                neither a provider nor an IMAP host is given.
        """

        def env(name: str) -> str | None:
            return os.environ.get(f"{prefix}{name}") or None

        address = env("ADDRESS")
        password = env("PASSWORD")
        if not address or not password:
            raise ConfigurationError(
                f"Set {prefix}ADDRESS and {prefix}PASSWORD (a .env file is read on import)."
            )

        display_name = env("DISPLAY_NAME")
        timeout = float(env("TIMEOUT") or 30.0)
        provider = env("PROVIDER")
        if provider:
            account = cls.for_provider(
                provider,
                address,
                password,
                display_name=display_name,
                timeout=timeout,
            )
            # Explicit host overrides still win over the preset, so a
            # self-hosted Gmail-shaped setup does not need a new preset.
            return replace(
                account,
                imap_host=env("IMAP_HOST") or account.imap_host,
                imap_port=int(env("IMAP_PORT") or account.imap_port),
                smtp_host=env("SMTP_HOST") or account.smtp_host,
                smtp_port=int(env("SMTP_PORT") or account.smtp_port),
                smtp_security=_parse_security(env("SMTP_SECURITY"), prefix)
                or account.smtp_security,
            )

        imap_host = env("IMAP_HOST")
        if not imap_host:
            known = ", ".join(sorted(PROVIDER_PRESETS))
            raise ConfigurationError(
                f"Set {prefix}PROVIDER (one of: {known}) or {prefix}IMAP_HOST."
            )
        security = _parse_security(env("SMTP_SECURITY"), prefix) or "ssl"
        return cls(
            address=address,
            password=password,
            imap_host=imap_host,
            imap_port=int(env("IMAP_PORT") or 993),
            smtp_host=env("SMTP_HOST") or "",
            smtp_port=int(env("SMTP_PORT") or (465 if security == "ssl" else 587)),
            smtp_security=security,
            username=env("USERNAME"),
            display_name=display_name,
            timeout=timeout,
        )

    def redacted(self) -> dict[str, object]:
        """A dict of the account safe to log or show in `emalia check`."""
        return {
            "address": self.address,
            "login": self.login,
            "imap": f"{self.imap_host}:{self.imap_port}",
            "smtp": f"{self.smtp_host}:{self.smtp_port} ({self.smtp_security})",
            "password": "***" if self.password else "(unset)",
        }
