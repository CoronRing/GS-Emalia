"""The interactive OAuth consent flow behind `emalia auth google login`.

Kept out of `emalia.mail` on purpose. The mail layer needs a refresh token and
nothing else; opening a browser and running a loopback web server is a setup-time
concern that a library embedding the toolkit should never have linked in.

The flow is the OAuth 2.0 authorization code grant with PKCE, against a loopback
redirect — what Google calls a Desktop client and what RFC 8252 recommends for
anything that is not a web server. No client secret ever leaves the machine in a
form a browser can see, and the authorization code is useless without the
verifier held in this process.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import logging
import os
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from emalia.errors import ConfigurationError
from emalia.mail.oauth import (
    DEFAULT_ENV_PREFIX,
    GMAIL_SCOPE,
    GOOGLE_AUTH_URI,
    GOOGLE_TOKEN_URI,
    OAuthCredentials,
    env_credential,
)

__all__ = ["ClientApp", "discover_client_secrets", "run_consent_flow"]

logger = logging.getLogger(__name__)

_EXCHANGE_TIMEOUT = 30.0

#: Long enough for a first-time consent screen, including the "Google hasn't
#: verified this app" interstitial and picking an account. Short enough that a
#: forgotten terminal does not hold a listening socket open all day.
_CONSENT_TIMEOUT = 300.0

_SUCCESS_PAGE = b"""<!doctype html>
<meta charset="utf-8"><title>Emalia</title>
<body style="font:16px system-ui;margin:4rem auto;max-width:32rem">
<h1>Authorised</h1>
<p>Emalia has the token it needs. You can close this tab and return to the terminal.</p>
</body>"""

_FAILURE_PAGE = b"""<!doctype html>
<meta charset="utf-8"><title>Emalia</title>
<body style="font:16px system-ui;margin:4rem auto;max-width:32rem">
<h1>Not authorised</h1>
<p>The consent screen returned an error. The terminal has the details.</p>
</body>"""


@dataclass(frozen=True, slots=True)
class ClientApp:
    """The OAuth client a token will be issued to.

    Attributes:
        client_id: The client identifier from the provider's console.
        client_secret: The client secret. For a Desktop client this is not
            genuinely confidential — Google documents it as such — but it is
            still stored with the token rather than printed.
        auth_uri: The provider's authorization endpoint.
        token_uri: The provider's token endpoint.
    """

    client_id: str
    client_secret: str
    auth_uri: str = GOOGLE_AUTH_URI
    token_uri: str = GOOGLE_TOKEN_URI

    @classmethod
    def from_client_secrets(cls, path: str | Path) -> ClientApp:
        """Read a `client_secret_*.json` downloaded from the Cloud Console.

        Both shapes are accepted. `installed` is what a Desktop client
        produces and what this flow is designed for; `web` works too as long
        as a loopback redirect URI has been registered on it.

        Args:
            path: The file to read.

        Returns:
            The client described by the file.

        Raises:
            ConfigurationError: If the file is missing, malformed, or holds
                neither an `installed` nor a `web` client.
        """
        target = Path(path).expanduser()
        try:
            document = json.loads(target.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigurationError(f"No client secrets file at {target}.") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError(f"Cannot read {target}: {exc}") from exc

        section: Any = None
        if isinstance(document, dict):
            section = document.get("installed") or document.get("web") or None
        if not isinstance(section, dict):
            raise ConfigurationError(
                f"{target} is not an OAuth client file. Expected a top-level "
                '"installed" or "web" object, which is what the Cloud Console '
                "downloads for a client ID."
            )

        client_id = str(section.get("client_id", ""))
        client_secret = str(section.get("client_secret", ""))
        if not client_id or not client_secret:
            raise ConfigurationError(f"{target} has no client_id or client_secret.")
        return cls(
            client_id=client_id,
            client_secret=client_secret,
            auth_uri=str(section.get("auth_uri") or GOOGLE_AUTH_URI),
            token_uri=str(section.get("token_uri") or GOOGLE_TOKEN_URI),
        )

    @classmethod
    def from_env(cls, prefix: str = DEFAULT_ENV_PREFIX) -> ClientApp | None:
        """Build a client from the environment, if the two values are set.

        Reads `<prefix>OAUTH_CLIENT_ID` and `<prefix>OAUTH_CLIENT_SECRET`, which
        for the default prefix also accept `GOOGLE_OAUTH_CLIENT_ID` and
        `GOOGLE_OAUTH_CLIENT_SECRET`.

        The Cloud Console offers the client's id and secret as text on the page
        as well as a JSON download, and copying the two strings is the more
        obvious of the two. Requiring the file would mean going back for a
        download that carries nothing extra.

        Args:
            prefix: Variable-name prefix.

        Returns:
            The client, or None when neither value is set.

        Raises:
            ConfigurationError: If one is set without the other.
        """
        client_id = env_credential(prefix, "OAUTH_CLIENT_ID")
        client_secret = env_credential(prefix, "OAUTH_CLIENT_SECRET")
        if not client_id and not client_secret:
            return None
        if not client_id or not client_secret:
            missing = "CLIENT_ID" if not client_id else "CLIENT_SECRET"
            raise ConfigurationError(
                f"{prefix}OAUTH_{missing} is not set. The consent flow needs both the "
                "client id and its secret."
            )
        return cls(
            client_id=client_id,
            client_secret=client_secret,
            auth_uri=env_credential(prefix, "OAUTH_AUTH_URI") or GOOGLE_AUTH_URI,
            token_uri=env_credential(prefix, "OAUTH_TOKEN_URI") or GOOGLE_TOKEN_URI,
        )


def discover_client_secrets() -> Path | None:
    """Look for an OAuth client file in the places one is likely to be.

    Saves the operator from passing `--client-secrets` when they have already
    run `gws auth setup` or downloaded a client into the project directory.

    Returns:
        The first candidate that exists, or None.
    """
    candidates: list[Path] = []

    override = os.environ.get("EMALIA_OAUTH_CLIENT_SECRETS")
    if override:
        candidates.append(Path(override).expanduser())

    # The Google Workspace CLI writes its client under a config root, and
    # `gws auth setup` creates one against a chosen Cloud project. Reusing it
    # means the whole setup can be done with tools the operator already has.
    # It uses ~/.config on every platform; Emalia follows the platform
    # convention, so both roots are searched.
    roots = [Path.home() / ".config"]
    if os.name == "nt":
        roots.append(Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming"))
    else:
        roots.append(Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"))
    for root in roots:
        candidates.append(root / "emalia" / "client_secret.json")
        candidates.append(root / "gws" / "client_secret.json")
    candidates.extend(sorted(Path.cwd().glob("client_secret*.json")))

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Captures the single redirect the consent screen sends back."""

    result: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 - the name is fixed by BaseHTTPRequestHandler
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        captured = {key: values[0] for key, values in query.items() if values}
        type(self).result.update(captured)

        body = _SUCCESS_PAGE if "code" in captured else _FAILURE_PAGE
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - signature is fixed
        """Silence the default stderr access log."""
        logger.debug("oauth callback: " + format, *args)


def run_consent_flow(
    app: ClientApp,
    *,
    scope: str = GMAIL_SCOPE,
    identity_scopes: str = "openid email",
    login_hint: str | None = None,
    port: int = 0,
    open_browser: bool = True,
    timeout: float = _CONSENT_TIMEOUT,
) -> OAuthCredentials:
    """Run the authorization code flow and return a refresh token.

    Args:
        app: The OAuth client to authorise against.
        scope: Space-separated scopes to request.
        identity_scopes: Extra scopes requested so the response identifies the
            account that actually consented. These are not sensitive and add no
            verification burden, and without them a token authorised against
            the wrong Google account is indistinguishable from a correct one
            until the first IMAP login fails. Pass an empty string to omit.
        login_hint: An address to preselect on the consent screen. Only a hint;
            the operator can still pick another account, which is why the
            address is read back from the grant rather than assumed.
        port: Loopback port to listen on. 0 lets the OS choose a free one.
        open_browser: Open the consent URL automatically. False just prints it,
            which is what a headless or remote shell needs.
        timeout: Seconds to wait for the redirect before giving up.

    Returns:
        Credentials carrying the new refresh token.

    Raises:
        ConfigurationError: If consent is refused, times out, or the code
            cannot be exchanged.
    """
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    state = secrets.token_urlsafe(32)
    requested = " ".join(dict.fromkeys((scope + " " + identity_scopes).split()))

    _CallbackHandler.result = {}
    # 127.0.0.1, never 0.0.0.0: the authorization code is a bearer credential
    # for the length of this exchange and nothing off this machine should be
    # able to deliver one.
    server = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.timeout = timeout
    redirect_uri = f"http://127.0.0.1:{server.server_port}/"

    parameters = {
        "client_id": app.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": requested,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        # Without both of these Google returns a refresh token only on the very
        # first consent for a given client and account, and silently omits it
        # every time after, which looks like a bug in the flow rather than a
        # documented behaviour.
        "access_type": "offline",
        "prompt": "consent",
    }
    if login_hint:
        parameters["login_hint"] = login_hint
    consent_url = f"{app.auth_uri}?{urllib.parse.urlencode(parameters)}"

    opened = webbrowser.open(consent_url) if open_browser else False
    if not opened:
        print(f"Open this URL to authorise Emalia:\n\n  {consent_url}\n")

    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    thread.join(timeout)
    server.server_close()

    captured = _CallbackHandler.result
    if not captured:
        raise ConfigurationError(
            f"No response from the consent screen within {timeout:.0f}s. "
            "If the browser did not open, rerun with --no-browser and open the URL yourself."
        )
    if "error" in captured:
        raise ConfigurationError(
            f"Consent was refused: {captured['error']}. "
            f"{captured.get('error_description', '')}".strip()
        )
    if not secrets.compare_digest(captured.get("state", ""), state):
        raise ConfigurationError(
            "The consent redirect carried the wrong state value; the response did not "
            "come from the request Emalia made. Nothing was saved."
        )
    code = captured.get("code")
    if not code:
        raise ConfigurationError("The consent redirect carried no authorization code.")

    tokens = _exchange_code(app, code=code, redirect_uri=redirect_uri, verifier=verifier)
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise ConfigurationError(
            "Google returned an access token but no refresh token, so the grant would "
            "stop working within the hour. This happens when a previous grant for this "
            "client is still active: revoke it at "
            "https://myaccount.google.com/permissions and run login again."
        )

    return OAuthCredentials(
        client_id=app.client_id,
        client_secret=app.client_secret,
        refresh_token=str(refresh_token),
        token_uri=app.token_uri,
        scope=str(tokens.get("scope") or requested),
        account=_address_from_id_token(tokens.get("id_token")) or login_hint,
    )


def _exchange_code(
    app: ClientApp, *, code: str, redirect_uri: str, verifier: str
) -> dict[str, Any]:
    """Trade the authorization code for tokens.

    Args:
        app: The OAuth client.
        code: The authorization code from the redirect.
        redirect_uri: The same redirect URI the code was issued for.
        verifier: The PKCE verifier generated for this request.

    Returns:
        The token endpoint's decoded response.

    Raises:
        ConfigurationError: If the endpoint refuses or cannot be reached.
    """
    payload = urllib.parse.urlencode(
        {
            "client_id": app.client_id,
            "client_secret": app.client_secret,
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
    ).encode("ascii")
    request = urllib.request.Request(
        app.token_uri,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_EXCHANGE_TIMEOUT) as response:
            decoded: dict[str, Any] = json.loads(response.read().decode("utf-8"))
            return decoded
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise ConfigurationError(f"The token exchange failed: HTTP {exc.code} {detail}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise ConfigurationError(f"Cannot reach {app.token_uri}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"{app.token_uri} returned a non-JSON response.") from exc


def _address_from_id_token(id_token: object) -> str | None:
    """Read the email claim out of an ID token, without verifying it.

    Verification is deliberately skipped: the token arrived over TLS directly
    from the endpoint this process just called, and the claim is used only to
    label the stored credential. Nothing is authorised on the strength of it.

    Args:
        id_token: The `id_token` field of a token response, if present.

    Returns:
        The mailbox address, or None when absent or unreadable.
    """
    if not isinstance(id_token, str) or id_token.count(".") != 2:
        return None
    payload = id_token.split(".")[1]
    padding = "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + padding))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(claims, dict):
        return None
    # Google always sends `email`. Microsoft sends it for personal accounts but
    # frequently only a UPN for work ones, which is the mailbox address anyway.
    for claim in ("email", "preferred_username", "upn"):
        value = claims.get(claim)
        if value:
            return str(value)
    return None
