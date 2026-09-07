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
from emalia.mail.oauth import (
    GMAIL_SCOPE,
    GOOGLE_ADC_ENV,
    OUTLOOK_SCOPE,
    AuthMethod,
    OAuthCredentials,
    ServiceAccountCredentials,
    TokenCredentials,
    default_token_path,
    env_credential,
)

__all__ = ["MailAccount", "ProviderPreset", "PROVIDER_PRESETS"]

SecurityMode = Literal["ssl", "starttls"]


@dataclass(frozen=True, slots=True)
class ProviderPreset:
    """Server endpoints for a known mail provider.

    Attributes:
        imap_host: IMAP server hostname.
        imap_port: IMAP server port.
        smtp_host: SMTP server hostname.
        smtp_port: SMTP server port.
        smtp_security: How TLS is established on the SMTP side.
        note: A caveat worth showing the operator during setup.
        oauth_scope: The scope an XOAUTH2 token needs for this provider, empty
            when the provider offers no OAuth path for IMAP and SMTP.
    """

    imap_host: str
    imap_port: int
    smtp_host: str
    smtp_port: int
    smtp_security: SecurityMode
    note: str = ""
    oauth_scope: str = ""


PROVIDER_PRESETS: dict[str, ProviderPreset] = {
    "gmail": ProviderPreset(
        imap_host="imap.gmail.com",
        imap_port=993,
        smtp_host="smtp.gmail.com",
        smtp_port=465,
        smtp_security="ssl",
        note=(
            "Needs an App Password with 2-Step Verification on, or an OAuth token from "
            "`emalia auth google login`. The account password will not work."
        ),
        oauth_scope=GMAIL_SCOPE,
    ),
    "outlook": ProviderPreset(
        imap_host="outlook.office365.com",
        imap_port=993,
        smtp_host="smtp-mail.outlook.com",
        smtp_port=587,
        smtp_security="starttls",
        note=(
            "Personal accounts need an App Password; most tenants have basic auth off "
            "entirely, leaving OAuth as the only way in."
        ),
        oauth_scope=OUTLOOK_SCOPE,
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

    Exactly one credential is carried: a `password` or an `oauth` grant. Both
    at once is rejected rather than silently preferred one way, because a
    leftover password beside a new OAuth token is the shape a half-finished
    migration takes, and quietly picking either one hides it.

    Attributes:
        address: The account's own email address, also the login username
            unless `username` is set.
        password: The password or app password. Never logged. Empty when
            `oauth` is set.
        imap_host: IMAP server hostname.
        imap_port: IMAP server port. 993 for implicit TLS.
        smtp_host: SMTP server hostname.
        smtp_port: SMTP server port.
        smtp_security: ``ssl`` for implicit TLS on connect, ``starttls`` to
            upgrade a plaintext connection.
        username: Login name, when it differs from `address`.
        display_name: The name shown to recipients in the ``From`` header.
        timeout: Socket timeout in seconds for both protocols.
        oauth: A token-based credential used over XOAUTH2 — either an
            `OAuthCredentials` refresh token or a `ServiceAccountCredentials`
            key. Empty when `password` is set.
    """

    address: str
    password: str = ""
    imap_host: str = ""
    imap_port: int = 993
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_security: SecurityMode = "ssl"
    username: str | None = None
    display_name: str | None = None
    timeout: float = 30.0
    oauth: TokenCredentials | None = None

    def __post_init__(self) -> None:
        if not self.address:
            raise ConfigurationError("MailAccount requires an address.")
        if self.password and self.oauth is not None:
            raise ConfigurationError(
                f"MailAccount for {self.address} has both a password and an OAuth grant. "
                "Supply one. Unset EMALIA_PASSWORD to use OAuth, or unset the "
                "EMALIA_OAUTH_* variables to use the password."
            )
        if not self.password and self.oauth is None:
            raise ConfigurationError(
                f"MailAccount for {self.address} has no credential. Either set "
                "EMALIA_PASSWORD to an app password, or run `emalia auth google login` "
                "and set EMALIA_OAUTH_TOKEN_FILE. See docs/authentication.md."
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

    @property
    def auth(self) -> AuthMethod:
        """Which mechanism this account authenticates with."""
        if isinstance(self.oauth, ServiceAccountCredentials):
            return "service_account"
        return "oauth" if self.oauth is not None else "password"

    @classmethod
    def for_provider(
        cls,
        provider: str,
        address: str,
        password: str = "",
        *,
        oauth: TokenCredentials | None = None,
        display_name: str | None = None,
        timeout: float = 30.0,
    ) -> MailAccount:
        """Build an account from a known provider's endpoints.

        Args:
            provider: A key of `PROVIDER_PRESETS`, case-insensitive.
            address: The mailbox address.
            password: The password or app password. Omit when passing `oauth`.
            oauth: An OAuth grant to use instead of a password.
            display_name: Optional ``From`` display name.
            timeout: Socket timeout in seconds.

        Returns:
            An account wired to that provider's servers.

        Raises:
            ConfigurationError: If the provider is not a known preset, or if
                neither credential is supplied.
        """
        preset = PROVIDER_PRESETS.get(provider.strip().lower())
        if preset is None:
            known = ", ".join(sorted(PROVIDER_PRESETS))
            raise ConfigurationError(f"Unknown provider {provider!r}. Known providers: {known}.")
        if oauth is not None and not preset.oauth_scope:
            raise ConfigurationError(
                f"Provider {provider!r} has no OAuth path for IMAP and SMTP. "
                "Use an app password, or set the hosts explicitly if you know otherwise."
            )
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
            oauth=oauth,
        )

    @classmethod
    def from_env(cls, prefix: str = "EMALIA_") -> MailAccount:
        """Build an account from environment variables.

        Reads ``<prefix>ADDRESS``, one credential, and either
        ``<prefix>PROVIDER`` or the explicit host and port variables
        ``<prefix>IMAP_HOST``, ``<prefix>IMAP_PORT``, ``<prefix>SMTP_HOST``,
        ``<prefix>SMTP_PORT``, ``<prefix>SMTP_SECURITY``.

        The credential is resolved in this order, and the first match wins:

        1. ``<prefix>OAUTH_CLIENT_ID``, ``<prefix>OAUTH_CLIENT_SECRET`` and
           ``<prefix>OAUTH_REFRESH_TOKEN`` together. The form for CI and
           containers: three opaque strings, no file to mount.
        2. ``<prefix>OAUTH_TOKEN_FILE``, a path to a token file.
        3. ``<prefix>AUTH=oauth``, meaning the token file
           `emalia auth google login` wrote at its default location.
        4. ``<prefix>PASSWORD``, an app password.

        Args:
            prefix: Variable-name prefix.

        Returns:
            The configured account.

        Raises:
            ConfigurationError: If no credential is set, if OAuth is only
                partly configured, or if neither a provider nor an IMAP host
                is given.
        """

        def env(name: str) -> str | None:
            return os.environ.get(f"{prefix}{name}") or None

        address = env("ADDRESS")
        if not address:
            raise ConfigurationError(f"Set {prefix}ADDRESS (a .env file is read on import).")
        password, oauth = cls._credential_from_env(prefix, address)

        display_name = env("DISPLAY_NAME")
        timeout = float(env("TIMEOUT") or 30.0)
        provider = env("PROVIDER")
        if provider:
            account = cls.for_provider(
                provider,
                address,
                password,
                oauth=oauth,
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
            oauth=oauth,
        )

    @staticmethod
    def _credential_from_env(prefix: str, address: str) -> tuple[str, TokenCredentials | None]:
        """Resolve exactly one credential from the environment.

        `<prefix>AUTH` names the method outright when set. Otherwise the method
        is inferred from what is present, in the order below. Inference never
        reaches for an ambient variable such as `GOOGLE_APPLICATION_CREDENTIALS`
        — that one is honoured only when `AUTH=service_account` asks for it, so
        a machine configured for unrelated Cloud work cannot end up
        authenticating a mailbox with a key nobody chose for the purpose.

        1. `<prefix>SERVICE_ACCOUNT_KEY` or `<prefix>SERVICE_ACCOUNT_FILE`
        2. `<prefix>OAUTH_CLIENT_ID` + `_CLIENT_SECRET` + `_REFRESH_TOKEN`
        3. `<prefix>OAUTH_TOKEN_FILE`
        4. `<prefix>PASSWORD`

        Args:
            prefix: Variable-name prefix.
            address: The mailbox address. Used in error messages, to stamp an
                OAuth grant for the mismatch warning, and as the `subject` a
                service account acts as.

        Returns:
            A `(password, token_credential)` pair with exactly one truthy
            element.

        Raises:
            ConfigurationError: If no credential is configured, if more than
                one is, or if the named method is not configured.
        """
        method = (os.environ.get(f"{prefix}AUTH") or "").strip().lower()
        known: tuple[str, ...] = ("password", "oauth", "service_account")
        if method and method not in known:
            raise ConfigurationError(
                f"{prefix}AUTH must be one of {', '.join(known)}, got {method!r}."
            )

        password = env_credential(prefix, "PASSWORD") or ""
        token: TokenCredentials | None = None

        if method != "password":
            token = ServiceAccountCredentials.from_env(
                prefix,
                subject=address,
                # Only an explicit request reaches for Google's machine-wide
                # variable; see the note above.
                allow_adc=method == "service_account",
            )
            if token is None and method != "service_account":
                token = OAuthCredentials.from_env(prefix)
            if token is None and method != "service_account":
                token_file = env_credential(prefix, "OAUTH_TOKEN_FILE")
                if token_file:
                    token = OAuthCredentials.from_file(token_file)
                elif method == "oauth":
                    token = OAuthCredentials.from_file(default_token_path())

        if method == "service_account" and token is None:
            raise ConfigurationError(
                f"{prefix}AUTH=service_account, but no key is configured. Set "
                f"{prefix}SERVICE_ACCOUNT_FILE to the key `gcloud iam service-accounts "
                f"keys create` wrote, or {prefix}SERVICE_ACCOUNT_KEY to its JSON, or "
                f"{GOOGLE_ADC_ENV} to the file."
            )

        if token is not None:
            if password:
                raise ConfigurationError(
                    f"Both {prefix}PASSWORD and a token credential are set for {address}. "
                    f"Unset {prefix}PASSWORD to use the token, or unset the "
                    f"{prefix}OAUTH_*/{prefix}SERVICE_ACCOUNT_* variables to use the "
                    "password. Note that for the default prefix a bare "
                    "GOOGLE_APP_PASSWORD counts as setting the password."
                )
            if isinstance(token, OAuthCredentials) and token.account is None:
                token = replace(token, account=address)
            return "", token

        if not password:
            raise ConfigurationError(
                f"No credential for {address}. Pick one:\n"
                f"  - an app password in {prefix}PASSWORD (or GOOGLE_APP_PASSWORD)\n"
                f"  - a service account key in {prefix}SERVICE_ACCOUNT_FILE, for a "
                "deployed system with no browser\n"
                f"  - `emalia auth google login`, then {prefix}AUTH=oauth\n"
                "See docs/authentication.md."
            )
        return password, None

    def redacted(self) -> dict[str, object]:
        """A dict of the account safe to log or show in `emalia check`."""
        summary: dict[str, object] = {
            "address": self.address,
            "login": self.login,
            "imap": f"{self.imap_host}:{self.imap_port}",
            "smtp": f"{self.smtp_host}:{self.smtp_port} ({self.smtp_security})",
            "auth": self.auth,
        }
        if self.oauth is not None:
            summary.update({f"  {k}": v for k, v in self.oauth.redacted().items()})
        else:
            summary["password"] = "***" if self.password else "(unset)"
        return summary
