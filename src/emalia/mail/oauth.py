"""Token-based credentials for IMAP and SMTP, via the XOAUTH2 SASL mechanism.

Password authentication is simple and still works, but it is being withdrawn.
Google issues app passwords only to accounts with 2-Step Verification and lets
a Workspace admin disable them for a whole domain; Microsoft has already turned
basic auth off for most tenants. XOAUTH2 is the replacement both of them
implement, and it is the only mechanism this module speaks.

Two credentials produce a token, and they suit opposite situations:

- `OAuthCredentials` holds a refresh token from a user who clicked through a
  consent screen. Right for a workstation, wrong for a server: obtaining one
  needs a browser, and on an unverified app it expires after seven days.
- `ServiceAccountCredentials` signs a JWT with a private key and asks Google to
  mint a token for a mailbox in a Workspace domain. **No browser, ever, and no
  expiry** — the key is the credential until you rotate it, which `gcloud` can
  do without a human. This is the one for a deployed system.

Nothing here is Google-specific except the default endpoints and the
service-account grant. Any provider issuing a refresh token and accepting
XOAUTH2 works by pointing `token_uri` at its endpoint, which is why the fields
are plain strings rather than a provider enum.

The standard library covers all of it except the RS256 signature a service
account needs, which is imported lazily so that `import emalia.mail` still
costs nothing on the two paths that do not sign anything.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from emalia.errors import ConfigurationError, MailAuthError

__all__ = [
    "AUTH_FAILURE_HINTS",
    "DEFAULT_MICROSOFT_TENANT",
    "GMAIL_SCOPE",
    "GOOGLE_AUTH_URI",
    "GOOGLE_PROVIDER",
    "GOOGLE_TOKEN_URI",
    "OAUTH_PROVIDERS",
    "OUTLOOK_SCOPE",
    "AuthMethod",
    "OAuthCredentials",
    "OAuthProvider",
    "ServiceAccountCredentials",
    "TokenCredentials",
    "default_token_path",
    "microsoft_provider",
    "xoauth2_string",
]

logger = logging.getLogger(__name__)

#: How Emalia authenticates to a mail server.
AuthMethod = Literal["password", "oauth", "service_account"]

#: What to tell the operator when a server rejects each kind of credential. The
#: failures have completely different fixes, and a single generic message sent
#: people hunting for an app password they were right not to have.
AUTH_FAILURE_HINTS: dict[str, str] = {
    "password": (
        "Most providers require an app password rather than the account password, "
        "and some have withdrawn password auth entirely."
    ),
    "oauth": (
        "The access token was refreshed but the server still refused it. Check that the "
        "grant carries the full mailbox scope and that IMAP is enabled on the account; "
        "a Workspace or Microsoft 365 admin can disable either."
    ),
    "service_account": (
        "A token was minted for this mailbox but the server refused it. The usual cause "
        "is that domain-wide delegation is authorised for a narrower scope than "
        "https://mail.google.com/, or that IMAP is off for the user in the Admin console."
    ),
}


@runtime_checkable
class TokenCredentials(Protocol):
    """What the IMAP and SMTP sessions need from any token-based credential.

    Deliberately narrow. The two implementations obtain a token in completely
    different ways, and neither the mail sessions nor `MailAccount` should have
    to know which one they hold.
    """

    def access_token(self, *, force_refresh: bool = False) -> str:
        """Return a bearer token valid for the next few minutes."""
        ...

    def redacted(self) -> dict[str, object]:
        """Return a summary with no recoverable secret in it."""
        ...


GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"

#: Full mailbox access. Gmail's IMAP and SMTP endpoints accept nothing narrower
#: — the read-only Gmail API scopes do not grant IMAP at all.
GMAIL_SCOPE = "https://mail.google.com/"

#: The equivalent for Microsoft identity platform accounts. `offline_access` has
#: to be requested explicitly there or no refresh token comes back.
OUTLOOK_SCOPE = (
    "offline_access https://outlook.office.com/IMAP.AccessAsUser.All "
    "https://outlook.office.com/SMTP.Send"
)

#: Microsoft's endpoints are per-tenant, so these carry a `{tenant}` field.
MICROSOFT_AUTH_TEMPLATE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
MICROSOFT_TOKEN_TEMPLATE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

#: `common` admits both personal Microsoft accounts and work or school ones.
#: A single-tenant app registration must use its own directory id instead, and
#: `organizations` excludes personal accounts.
DEFAULT_MICROSOFT_TENANT = "common"

#: Refresh this many seconds before the server's stated expiry. A token that
#: expires between the refresh check and the LOGIN that uses it produces a
#: confusing authentication failure rather than a retry.
_EXPIRY_MARGIN = 120.0

_REFRESH_TIMEOUT = 30.0

#: RFC 7523. What a signed assertion is exchanged for a token under.
_JWT_BEARER_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"

#: The variable prefix a normal deployment uses.
DEFAULT_ENV_PREFIX = "EMALIA_"

#: Provider-standard names accepted as fallbacks for Emalia's own, so a machine
#: already holding these does not need them copied under a second set of names.
#:
#: Every alias is unambiguous about what it is for. The bare `GOOGLE_CLIENT_ID`
#: and `GOOGLE_CLIENT_SECRET` are deliberately **not** here: they are the
#: generic names for any Google OAuth client, including a "sign in with Google"
#: on an unrelated service, and reading a mail credential out of them would be
#: a genuine surprise on a machine that does several things.
#:
#: Consulted **only for the default prefix**. A suite running under its own
#: prefix, such as the end-to-end one, must not be able to reach a shared
#: credential — that isolation is the whole reason it has a separate prefix.
SHARED_ENV_ALIASES: dict[str, tuple[str, ...]] = {
    "PASSWORD": ("GOOGLE_APP_PASSWORD",),
    "OAUTH_CLIENT_ID": ("GOOGLE_OAUTH_CLIENT_ID",),
    "OAUTH_CLIENT_SECRET": ("GOOGLE_OAUTH_CLIENT_SECRET",),
    "OAUTH_REFRESH_TOKEN": ("GOOGLE_OAUTH_REFRESH_TOKEN",),
    "SERVICE_ACCOUNT_FILE": ("GOOGLE_SERVICE_ACCOUNT_FILE",),
    "SERVICE_ACCOUNT_KEY": ("GOOGLE_SERVICE_ACCOUNT_KEY",),
}

#: Where Google's own tooling expects a service account key. Honoured only when
#: `<prefix>AUTH=service_account` asks for it explicitly: it is frequently set
#: machine-wide for unrelated Cloud work, and silently authenticating a mailbox
#: with whatever it happens to point at would be a poor surprise.
GOOGLE_ADC_ENV = "GOOGLE_APPLICATION_CREDENTIALS"


def env_credential(prefix: str, name: str) -> str | None:
    """Read `<prefix><name>`, falling back to a provider-standard alias.

    Args:
        prefix: The instance's variable prefix.
        name: The unprefixed variable name, such as `PASSWORD`.

    Returns:
        The first non-blank value found, or None. A variable set to whitespace
        counts as unset, since that is what an empty CI secret expands to.
    """

    def read(variable: str) -> str | None:
        value = os.environ.get(variable)
        return value.strip() or None if value else None

    direct = read(f"{prefix}{name}")
    if direct or prefix != DEFAULT_ENV_PREFIX:
        return direct
    for alias in SHARED_ENV_ALIASES.get(name, ()):
        found = read(alias)
        if found:
            logger.debug("Read %s from the shared alias %s", name, alias)
            return found
    return None


def _b64url(payload: dict[str, Any]) -> bytes:
    """Encode a JWT segment as unpadded base64url.

    Args:
        payload: The header or claim set.

    Returns:
        The encoded segment, without the `=` padding JWS forbids.
    """
    compact = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(compact).rstrip(b"=")


def _sign_rs256(signing_input: bytes, private_key: str) -> str:
    """Sign a JWT with an RSA key.

    `cryptography` is imported here rather than at module scope so that the two
    credential types that never sign anything keep `emalia.mail` free of it.
    It is the one thing in this module the standard library cannot do.

    Args:
        signing_input: The `header.claims` bytes to sign.
        private_key: The RSA private key in PEM form.

    Returns:
        The signature, unpadded base64url.

    Raises:
        MailAuthError: If `cryptography` is absent or the key is unusable.
    """
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except ImportError as exc:
        raise MailAuthError(
            "Service account authentication needs the `cryptography` package to sign "
            "its assertion. Install it with `pip install emalia[gcp]`."
        ) from exc

    try:
        key = serialization.load_pem_private_key(private_key.encode("utf-8"), password=None)
    except (ValueError, TypeError) as exc:
        raise MailAuthError(
            "The service account private key could not be read. If it came from an "
            "environment variable, its newlines were probably lost: the PEM body must "
            "keep its line breaks, or be given with literal \\n escapes."
        ) from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise MailAuthError(
            f"The service account key is a {type(key).__name__}, not an RSA key. "
            "Google issues RS256 keys; this file is something else."
        )

    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")


def xoauth2_string(user: str, access_token: str) -> str:
    """Build the SASL initial response for the XOAUTH2 mechanism.

    The format is fixed by Google's specification and used unchanged by
    Microsoft: `user=<address>^Aauth=Bearer <token>^A^A`, where `^A` is
    Control-A. Callers pass the result to `imaplib` or `smtplib`, both of which
    apply the base64 layer themselves.

    Args:
        user: The login address to authenticate as.
        access_token: A current access token, not a refresh token.

    Returns:
        The unencoded SASL string.
    """
    return f"user={user}\x01auth=Bearer {access_token}\x01\x01"


@dataclass(frozen=True, slots=True)
class OAuthProvider:
    """One identity provider's OAuth endpoints and mail scope.

    The consent flow itself is provider-agnostic — it is plain RFC 6749 with
    PKCE — so everything that differs between Google and Microsoft is gathered
    here rather than branched on at each call site.

    Attributes:
        key: The CLI sub-command name, e.g. ``google``.
        label: How to name the provider in messages to the operator.
        auth_uri: The authorization endpoint.
        token_uri: The token endpoint.
        scope: The scope granting IMAP and SMTP access to a mailbox.
        revoke_url: Where a human goes to withdraw the grant. Deleting the
            local token file does not do this, and saying so is the difference
            between a rotated credential and one that is merely misplaced.
        token_filename: Basename under the config directory, so two providers
            authorised on one machine do not overwrite each other.
    """

    key: str
    label: str
    auth_uri: str
    token_uri: str
    scope: str
    revoke_url: str
    token_filename: str


GOOGLE_PROVIDER = OAuthProvider(
    key="google",
    label="Google",
    auth_uri=GOOGLE_AUTH_URI,
    token_uri=GOOGLE_TOKEN_URI,
    scope=GMAIL_SCOPE,
    revoke_url="https://myaccount.google.com/permissions",
    token_filename="google_oauth.json",
)


def microsoft_provider(tenant: str = DEFAULT_MICROSOFT_TENANT) -> OAuthProvider:
    """Build the Microsoft provider for one tenant.

    Args:
        tenant: ``common``, ``organizations``, ``consumers``, or a directory
            id. A single-tenant app registration is rejected by ``common`` and
            must name its own directory id here.

    Returns:
        The provider record for that tenant.
    """
    return OAuthProvider(
        key="microsoft",
        label="Microsoft",
        auth_uri=MICROSOFT_AUTH_TEMPLATE.format(tenant=tenant),
        token_uri=MICROSOFT_TOKEN_TEMPLATE.format(tenant=tenant),
        scope=OUTLOOK_SCOPE,
        revoke_url="https://account.microsoft.com/privacy/app-access",
        token_filename="microsoft_oauth.json",
    )


#: Providers reachable from `emalia auth <name> login`, keyed by sub-command.
OAUTH_PROVIDERS: dict[str, OAuthProvider] = {
    "google": GOOGLE_PROVIDER,
    "microsoft": microsoft_provider(),
}


def default_token_path(provider: str = "google") -> Path:
    """Where `emalia auth <provider> login` stores its token by default.

    Follows the platform convention rather than dropping a dotfile in the
    working directory, so the credential is not picked up by a stray `docker
    build` context or committed by accident.

    Args:
        provider: A key of `OAUTH_PROVIDERS`. An unknown name gets a file of
            its own rather than an error, since the path is only a default.

    Returns:
        `%APPDATA%\\emalia\\<provider>_oauth.json` on Windows,
        `$XDG_CONFIG_HOME/emalia/<provider>_oauth.json` elsewhere.
        `EMALIA_OAUTH_TOKEN_FILE` overrides both, for every provider.
    """
    override = os.environ.get("EMALIA_OAUTH_TOKEN_FILE")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    known = OAUTH_PROVIDERS.get(provider)
    filename = known.token_filename if known else f"{provider}_oauth.json"
    return base / "emalia" / filename


def _post_token_request(
    token_uri: str,
    fields: dict[str, str],
    *,
    explain: Callable[[str, str], str | None],
) -> dict[str, Any]:
    """POST a form to a token endpoint and decode the reply.

    Shared by both credential types: the grant differs, the transport and the
    failure modes do not.

    Args:
        token_uri: The endpoint to call.
        fields: Form fields for the grant.
        explain: Maps an `(error_code, description)` pair to a message naming
            the likely cause, or None to fall back to a generic one.

    Returns:
        The decoded token response.

    Raises:
        MailAuthError: If the endpoint is unreachable or refuses the grant.
    """
    request = urllib.request.Request(
        token_uri,
        data=urllib.parse.urlencode(fields).encode("ascii"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_REFRESH_TIMEOUT) as response:
            decoded: dict[str, Any] = json.loads(response.read().decode("utf-8"))
            return decoded
    except urllib.error.HTTPError as exc:
        code, detail = _decode_error(exc)
        raise MailAuthError(
            explain(code, detail) or f"The token endpoint refused the request: {detail}"
        ) from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise MailAuthError(f"Cannot reach the token endpoint {token_uri}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise MailAuthError(
            f"The token endpoint {token_uri} returned a non-JSON response."
        ) from exc


def _decode_error(exc: urllib.error.HTTPError) -> tuple[str, str]:
    """Pull the OAuth error code and description out of a failed response.

    Args:
        exc: The HTTP error raised by the token endpoint.

    Returns:
        An `(error_code, description)` pair. Both fall back to the status line
        when the body is not the documented JSON.
    """
    try:
        body: dict[str, Any] = json.loads(exc.read().decode("utf-8"))
    except (json.JSONDecodeError, ValueError, OSError):
        return "", f"HTTP {exc.code}"
    code = str(body.get("error", ""))
    return code, str(body.get("error_description", "")) or code or f"HTTP {exc.code}"


def _explain_refresh_failure(code: str, detail: str) -> str | None:
    """Explain a refused refresh-token grant.

    `invalid_grant` is by far the most common failure and its bare text says
    nothing about the cause, so the message names the four things that actually
    produce it.

    Args:
        code: The OAuth error code.
        detail: The endpoint's description.

    Returns:
        A message naming the likely cause, or None for the generic one.
    """
    if code != "invalid_grant":
        return None
    return (
        "The OAuth refresh token was rejected (invalid_grant). The usual causes, in "
        "order of likelihood: the consent screen is still in Testing status, which "
        "expires refresh tokens after seven days; the account's password was changed, "
        "which revokes every mail-scoped grant; access was revoked at the provider; or "
        "the token belongs to a different client. Run `emalia auth google login` to "
        f"issue a new one, or move to a service account to stop needing one. "
        f"Server said: {detail}"
    )


def _explain_assertion_failure(code: str, detail: str) -> str | None:
    """Explain a refused service-account assertion.

    Missing domain-wide delegation is the failure everyone hits first, and
    Google reports it as `unauthorized_client` with no mention of the Admin
    console page that fixes it.

    Args:
        code: The OAuth error code.
        detail: The endpoint's description.

    Returns:
        A message naming the likely cause, or None for the generic one.
    """
    if code == "unauthorized_client":
        return (
            "The service account is not authorised to impersonate this mailbox "
            "(unauthorized_client). Domain-wide delegation has not been granted, or it "
            "was granted for a different scope. In the Admin console go to Security > "
            "Access and data control > API controls > Domain-wide delegation, add the "
            "service account's numeric client ID, and list the scope "
            f"https://mail.google.com/ exactly. Server said: {detail}"
        )
    if code == "invalid_grant":
        return (
            "The service account could not act as this mailbox (invalid_grant). Either "
            "the address does not exist in the domain, or it is outside the Workspace "
            "organisation the service account belongs to. Domain-wide delegation cannot "
            f"reach a personal @gmail.com account. Server said: {detail}"
        )
    if code == "invalid_client":
        return (
            "The service account key was rejected (invalid_client). The key has probably "
            f"been deleted or disabled; create a new one and swap it in. Server said: {detail}"
        )
    return None


def _store(cache: _AccessToken, body: dict[str, Any]) -> str:
    """Record a token response in the cache and return the token.

    Args:
        cache: The credential's access-token cache.
        body: A decoded token-endpoint response.

    Returns:
        The new access token.

    Raises:
        MailAuthError: If the response carried no token.
    """
    token = body.get("access_token")
    if not token:
        raise MailAuthError("The token endpoint returned no access_token.")
    lifetime = float(body.get("expires_in", 3600))
    cache.value = str(token)
    cache.expires_at = time.time() + lifetime
    logger.debug("Obtained an access token, valid for %.0fs", lifetime)
    return cache.value


@dataclass(slots=True)
class _AccessToken:
    """The short-lived half of a credential, cached in memory only.

    Access tokens last an hour and are never written to disk. A process that
    restarts refreshes from the refresh token, which costs one HTTP round trip.
    """

    value: str = ""
    expires_at: float = 0.0

    def usable(self) -> bool:
        """Whether the cached token is present and not about to expire."""
        return bool(self.value) and time.time() < self.expires_at - _EXPIRY_MARGIN


@dataclass(frozen=True, slots=True)
class OAuthCredentials:
    """A refresh token and the client it belongs to.

    This is the long-lived credential. It is exchanged for a short-lived access
    token on demand and the result is cached for the process's lifetime.

    Attributes:
        client_id: The OAuth client the token was issued to.
        client_secret: The client's secret. Desktop clients are not confidential
            and Google documents the secret as non-sensitive, but it is still
            redacted everywhere Emalia prints a credential.
        refresh_token: The long-lived grant. Never logged.
        token_uri: The provider's token endpoint.
        scope: Space-separated scopes the token carries, recorded for
            diagnostics. Not sent on refresh; the grant already fixes them.
        account: The address the token was issued for, when known. Used only to
            warn about a mismatch with the mailbox being opened.
    """

    client_id: str
    client_secret: str
    refresh_token: str
    token_uri: str = GOOGLE_TOKEN_URI
    scope: str = GMAIL_SCOPE
    account: str | None = None
    _token: _AccessToken = field(
        default_factory=_AccessToken, repr=False, compare=False, hash=False
    )

    def __post_init__(self) -> None:
        missing = [
            name
            for name in ("client_id", "client_secret", "refresh_token")
            if not getattr(self, name)
        ]
        if missing:
            raise ConfigurationError(
                f"OAuthCredentials is missing {', '.join(missing)}. "
                "Run `emalia auth google login`, or set the EMALIA_OAUTH_* variables."
            )

    # -- obtaining an access token --------------------------------------------

    def access_token(self, *, force_refresh: bool = False) -> str:
        """Return a usable access token, refreshing it if necessary.

        Args:
            force_refresh: Refresh even when the cached token still looks
                valid. Used after a server rejects a token Emalia believed was
                current, which happens when the grant is revoked mid-session.

        Returns:
            A bearer token, valid for at least `_EXPIRY_MARGIN` seconds.

        Raises:
            MailAuthError: If the provider refuses to refresh the grant.
        """
        if not force_refresh and self._token.usable():
            return self._token.value
        return self._refresh()

    def _refresh(self) -> str:
        """Exchange the refresh token for a new access token.

        Returns:
            The new access token.

        Raises:
            MailAuthError: If the endpoint is unreachable or refuses the grant.
        """
        body = _post_token_request(
            self.token_uri,
            {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            },
            explain=_explain_refresh_failure,
        )
        return _store(self._token, body)

    # -- sources ---------------------------------------------------------------

    @classmethod
    def from_env(cls, prefix: str = "EMALIA_") -> OAuthCredentials | None:
        """Build credentials from environment variables, if they are set.

        Reads ``<prefix>OAUTH_CLIENT_ID``, ``<prefix>OAUTH_CLIENT_SECRET`` and
        ``<prefix>OAUTH_REFRESH_TOKEN``, with optional
        ``<prefix>OAUTH_TOKEN_URI`` and ``<prefix>OAUTH_SCOPE``.

        This is the form to use in CI and in a container: three opaque strings
        that a secret store can hold, with no file to mount.

        For the default prefix each variable also accepts its provider-standard
        name — `GOOGLE_OAUTH_CLIENT_ID` and friends — so a machine already
        holding them does not need a second copy under Emalia's names.

        Args:
            prefix: Variable-name prefix.

        Returns:
            The credentials, or None when none of the three variables is set.

        Raises:
            ConfigurationError: If some but not all of the three are set, which
                is a typo rather than a decision to use password auth.
        """
        client_id = env_credential(prefix, "OAUTH_CLIENT_ID")
        client_secret = env_credential(prefix, "OAUTH_CLIENT_SECRET")
        refresh_token = env_credential(prefix, "OAUTH_REFRESH_TOKEN")
        present = [bool(client_id), bool(client_secret), bool(refresh_token)]

        if not any(present):
            return None
        if not all(present):
            names = [f"{prefix}OAUTH_{n}" for n in ("CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN")]
            missing = [name for name, ok in zip(names, present, strict=True) if not ok]
            hint = ""
            if not refresh_token:
                hint = (
                    " A client ID and secret identify the application, not any mailbox; "
                    "the refresh token is the part that authorises one. Get it with "
                    "`emalia auth google login --print-env`, or use a service account "
                    "and need no token at all."
                )
            raise ConfigurationError(
                f"OAuth is partly configured: {', '.join(missing)} not set. "
                f"Set all three, or unset the others to use {prefix}PASSWORD instead.{hint}"
            )

        assert client_id and client_secret and refresh_token  # narrowed by `all(present)`
        return cls(
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=refresh_token,
            token_uri=env_credential(prefix, "OAUTH_TOKEN_URI") or GOOGLE_TOKEN_URI,
            scope=env_credential(prefix, "OAUTH_SCOPE") or GMAIL_SCOPE,
            account=os.environ.get(f"{prefix}ADDRESS") or None,
        )

    @classmethod
    def from_file(cls, path: str | Path | None = None) -> OAuthCredentials:
        """Load credentials from a token file written by `emalia auth google`.

        The file uses Google's `authorized_user` shape, the same one
        `gcloud auth application-default login` writes, so a token obtained by
        other tooling can be dropped in unchanged.

        Args:
            path: The file to read. Defaults to `default_token_path()`.

        Returns:
            The stored credentials.

        Raises:
            ConfigurationError: If the file is absent, unreadable, or not a
                credential document.
        """
        target = Path(path) if path else default_token_path()
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigurationError(
                f"No OAuth token at {target}. Run `emalia auth google login` first."
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError(f"Cannot read the OAuth token at {target}: {exc}") from exc

        if not isinstance(data, dict):
            raise ConfigurationError(f"{target} does not contain a credential object.")
        scopes = data.get("scopes")
        return cls(
            client_id=str(data.get("client_id", "")),
            client_secret=str(data.get("client_secret", "")),
            refresh_token=str(data.get("refresh_token", "")),
            token_uri=str(data.get("token_uri") or GOOGLE_TOKEN_URI),
            scope=" ".join(scopes) if isinstance(scopes, list) else str(data.get("scope") or ""),
            account=str(data["account"]) if data.get("account") else None,
        )

    def save(self, path: str | Path | None = None) -> Path:
        """Write the credentials to a token file, readable only by this user.

        Args:
            path: Where to write. Defaults to `default_token_path()`.

        Returns:
            The path written.

        Raises:
            ConfigurationError: If the file cannot be written.
        """
        target = Path(path) if path else default_token_path()
        document = {
            "type": "authorized_user",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
            "token_uri": self.token_uri,
            "scopes": self.scope.split() if self.scope else [],
        }
        if self.account:
            document["account"] = self.account

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Create with the restrictive mode rather than relaxing it after the
            # fact, so the secret is never briefly world-readable. Windows
            # ignores the mode and inherits the parent ACL instead.
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(document, handle, indent=2)
                handle.write("\n")
        except OSError as exc:
            raise ConfigurationError(f"Cannot write the OAuth token to {target}: {exc}") from exc
        return target

    # -- diagnostics -----------------------------------------------------------

    def redacted(self) -> dict[str, object]:
        """A summary safe to print in `emalia check` or a log line.

        The client id is shown in part because it is the field that identifies
        which OAuth client a token came from, and telling two apart is the
        common debugging need. The secret and the refresh token never appear.

        Returns:
            A dict with no recoverable secret in it.
        """
        # ASCII only: a Windows console on a legacy code page renders a real
        # ellipsis as a replacement character, which reads like corruption in
        # the one output people copy into a bug report.
        head = self.client_id.split(".")[0][:12]
        return {
            "client_id": f"{head}..." if head else "(unset)",
            "client_secret": "***",
            "refresh_token": "***",
            "token_uri": self.token_uri,
            "scope": self.scope,
            "account": self.account or "(unrecorded)",
        }


@dataclass(frozen=True, slots=True)
class ServiceAccountCredentials:
    """A Google service account key, used to act as a mailbox in a domain.

    The credential a deployed system wants. Unlike a refresh token it is
    obtained without a browser, never expires on its own, and is rotated by
    creating a new key and deleting the old one — all of which `gcloud` does
    without a human present.

    It works by domain-wide delegation: a Workspace administrator authorises
    the service account's client ID for a specific scope, after which the
    account may mint tokens for any mailbox in that domain. That authorisation
    is the security boundary, and it is deliberately not something the holder
    of the key can grant itself.

    It does not work for a personal `@gmail.com` address. There is no domain
    and therefore no administrator to delegate anything.

    Attributes:
        client_email: The service account's own address.
        private_key: The RSA private key, in PEM form. Never logged.
        subject: The mailbox to act as. Every token is minted for this address
            and grants nothing anywhere else.
        private_key_id: Identifies which key signed the assertion, so a
            rotation can be traced.
        token_uri: Google's token endpoint.
        scope: Space-separated scopes to request. Must be covered by what the
            administrator delegated.
        project_id: The Cloud project the key came from. Diagnostics only.
        client_id: The service account's numeric OAuth client ID. Not used to
            authenticate, but it is the value the Admin console asks for when
            delegation is granted, and it appears nowhere else a person is
            likely to look.
    """

    client_email: str
    private_key: str
    subject: str
    private_key_id: str = ""
    token_uri: str = GOOGLE_TOKEN_URI
    scope: str = GMAIL_SCOPE
    project_id: str = ""
    client_id: str = ""
    _token: _AccessToken = field(
        default_factory=_AccessToken, repr=False, compare=False, hash=False
    )

    def __post_init__(self) -> None:
        missing = [
            name for name in ("client_email", "private_key", "subject") if not getattr(self, name)
        ]
        if missing:
            raise ConfigurationError(
                f"ServiceAccountCredentials is missing {', '.join(missing)}. "
                "The key file supplies the first two; `subject` is the mailbox to act "
                "as, which Emalia takes from the account address."
            )

    # -- obtaining an access token --------------------------------------------

    def access_token(self, *, force_refresh: bool = False) -> str:
        """Return a usable access token, minting a new one if necessary.

        Args:
            force_refresh: Mint a new token even when the cached one still
                looks valid.

        Returns:
            A bearer token for `subject`, valid for at least `_EXPIRY_MARGIN`
            seconds.

        Raises:
            MailAuthError: If Google refuses to mint a token.
        """
        if not force_refresh and self._token.usable():
            return self._token.value
        body = _post_token_request(
            self.token_uri,
            {"grant_type": _JWT_BEARER_GRANT, "assertion": self._assertion()},
            explain=_explain_assertion_failure,
        )
        return _store(self._token, body)

    def _assertion(self) -> str:
        """Build and sign the JWT that asks for a token on `subject`'s behalf.

        Returns:
            The signed assertion, in compact JWS form.

        Raises:
            MailAuthError: If the private key cannot be loaded or signed with.
        """
        now = int(time.time())
        header = {"alg": "RS256", "typ": "JWT"}
        if self.private_key_id:
            header["kid"] = self.private_key_id
        claims = {
            "iss": self.client_email,
            # `sub` is what makes this delegation rather than a plain service
            # identity: the token comes back scoped to this mailbox.
            "sub": self.subject,
            "scope": self.scope,
            "aud": self.token_uri,
            "iat": now,
            # Google caps an assertion at one hour and rejects a longer one
            # outright rather than clamping it.
            "exp": now + 3600,
        }
        signing_input = b".".join((_b64url(header), _b64url(claims)))
        return f"{signing_input.decode('ascii')}.{_sign_rs256(signing_input, self.private_key)}"

    # -- sources ---------------------------------------------------------------

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        *,
        subject: str,
        scope: str = GMAIL_SCOPE,
    ) -> ServiceAccountCredentials:
        """Load a service account key file.

        Reads the JSON that `gcloud iam service-accounts keys create` writes,
        which is also what the Cloud Console downloads.

        Args:
            path: The key file.
            subject: The mailbox to act as.
            scope: Space-separated scopes to request.

        Returns:
            The loaded credentials.

        Raises:
            ConfigurationError: If the file is absent, unreadable, or is some
                other kind of credential.
        """
        target = Path(path).expanduser()
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigurationError(f"No service account key at {target}.") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError(
                f"Cannot read the service account key at {target}: {exc}"
            ) from exc

        if not isinstance(data, dict) or data.get("type") != "service_account":
            found = data.get("type", "no type field") if isinstance(data, dict) else "not an object"
            raise ConfigurationError(
                f"{target} is not a service account key ({found}). A file with "
                '"type": "authorized_user" is an OAuth token, which belongs in '
                "EMALIA_OAUTH_TOKEN_FILE instead."
            )
        return cls(
            client_email=str(data.get("client_email", "")),
            private_key=str(data.get("private_key", "")),
            subject=subject,
            private_key_id=str(data.get("private_key_id", "")),
            token_uri=str(data.get("token_uri") or GOOGLE_TOKEN_URI),
            scope=scope,
            project_id=str(data.get("project_id", "")),
            client_id=str(data.get("client_id", "")),
        )

    @classmethod
    def from_env(
        cls, prefix: str = DEFAULT_ENV_PREFIX, *, subject: str, allow_adc: bool = False
    ) -> ServiceAccountCredentials | None:
        """Build service account credentials from the environment, if configured.

        Reads `<prefix>SERVICE_ACCOUNT_FILE`, a path to the key. With
        `allow_adc`, `GOOGLE_APPLICATION_CREDENTIALS` is accepted too — gated
        because that variable is frequently set machine-wide for unrelated
        Cloud work.

        `<prefix>SERVICE_ACCOUNT_KEY` is read as an alternative holding the
        key's JSON inline, for a secret store that has no filesystem to write
        to.

        Args:
            prefix: Variable-name prefix.
            subject: The mailbox to act as.
            allow_adc: Also consult `GOOGLE_APPLICATION_CREDENTIALS`.

        Returns:
            The credentials, or None when nothing is configured.

        Raises:
            ConfigurationError: If a configured key cannot be read or is not a
                service account key.
        """
        scope = env_credential(prefix, "OAUTH_SCOPE") or GMAIL_SCOPE

        inline = env_credential(prefix, "SERVICE_ACCOUNT_KEY")
        if inline:
            try:
                data = json.loads(inline)
            except json.JSONDecodeError as exc:
                raise ConfigurationError(
                    f"{prefix}SERVICE_ACCOUNT_KEY is not valid JSON. It should hold the "
                    "whole key file, not a path to it."
                ) from exc
            if not isinstance(data, dict) or data.get("type") != "service_account":
                raise ConfigurationError(
                    f"{prefix}SERVICE_ACCOUNT_KEY does not contain a service account key."
                )
            return cls(
                client_email=str(data.get("client_email", "")),
                # A key pasted through a shell or a CI secret box routinely
                # arrives with its newlines escaped. Restoring them here is the
                # difference between working and an unreadable-PEM error.
                private_key=str(data.get("private_key", "")).replace("\\n", "\n"),
                subject=subject,
                private_key_id=str(data.get("private_key_id", "")),
                token_uri=str(data.get("token_uri") or GOOGLE_TOKEN_URI),
                scope=scope,
                project_id=str(data.get("project_id", "")),
                client_id=str(data.get("client_id", "")),
            )

        path = env_credential(prefix, "SERVICE_ACCOUNT_FILE")
        if not path and allow_adc:
            path = os.environ.get(GOOGLE_ADC_ENV) or None
        if not path:
            return None
        return cls.from_file(path, subject=subject, scope=scope)

    # -- diagnostics -----------------------------------------------------------

    def redacted(self) -> dict[str, object]:
        """A summary safe to print in `emalia check` or a log line.

        The key id is shown in full: it is not a secret, and it is the only way
        to tell which key a running daemon is actually using after a rotation.

        Returns:
            A dict with no recoverable secret in it.
        """
        return {
            "type": "service account (domain-wide delegation)",
            "client_email": self.client_email,
            "acting as": self.subject,
            "private_key": "***",
            "private_key_id": self.private_key_id or "(unrecorded)",
            "delegation client_id": self.client_id or "(unrecorded)",
            "project_id": self.project_id or "(unrecorded)",
            "scope": self.scope,
        }
