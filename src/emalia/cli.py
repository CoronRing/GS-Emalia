"""The `emalia` command line.

`check` is the one to run first. It verifies both mail servers and the model
credentials and prints the policy it resolved, which is where most setup
problems announce themselves.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import replace
from pathlib import Path
from typing import Annotated

import typer

from emalia import __version__
from emalia.auth import ClientApp
from emalia.config import EmaliaConfig
from emalia.errors import ConfigurationError, EmaliaError
from emalia.mail.oauth import (
    DEFAULT_MICROSOFT_TENANT,
    GOOGLE_PROVIDER,
    OAuthProvider,
    microsoft_provider,
)

app = typer.Typer(
    name="emalia",
    help="An agent that lives in your email, and the toolkit behind it.",
    no_args_is_help=True,
    add_completion=False,
)

# Options shared by several commands. The default belongs on the parameter, not
# inside `typer.Option`: passing one in both places is rejected, and the error
# it produces names neither the option nor the command.
ConfigOption = Annotated[
    Path | None,
    typer.Option(
        "--config",
        "-c",
        help="Path to emalia.toml. Defaults to ./emalia.toml when it exists.",
    ),
]
VerboseOption = Annotated[bool, typer.Option("--verbose", "-v", help="Log at DEBUG level.")]
DryRunOption = Annotated[
    bool,
    typer.Option("--dry-run", help="Do everything except send. Replies are logged."),
]


_STARTER_CONFIG = """\
# Emalia configuration. Safe to commit: secrets live in .env, never here.

instance_name = "Emalia"
poll_interval = 30.0
batch_size = 5

[llm]
provider = "anthropic"
model = "claude-sonnet-4-6"

[policy]
# Only these senders are answered. Globs are allowed: "*@mycompany.com".
# An empty list means nobody, which is the safe default.
allowed_senders = ["you@example.com"]

# Directories the file tools may touch. Everything else is refused.
sandbox_roots = []

# Available: email, file_read, file_write, http, shell, python.
# shell and python also need allow_dangerous_tools = true, and cannot be
# combined with allow_any_sender.
enabled_toolsets = ["email", "file_read"]

max_replies_per_hour = 30
max_tool_calls = 25
"""

_STARTER_ENV = """\
# Mail account. Most providers need an app password, not your login password.
EMALIA_ADDRESS=you@gmail.com
EMALIA_PASSWORD=your-app-password
EMALIA_PROVIDER=gmail

# Or authenticate with OAuth instead of a password, which is the only option
# on a Workspace or Microsoft 365 tenant that has app passwords turned off.
# Run `emalia auth google login`, then delete EMALIA_PASSWORD above and set:
# EMALIA_AUTH=oauth
#
# In CI, where there is no file to mount, supply the same grant directly:
# EMALIA_OAUTH_CLIENT_ID=
# EMALIA_OAUTH_CLIENT_SECRET=
# EMALIA_OAUTH_REFRESH_TOKEN=

# Model credentials. Match the provider set in emalia.toml.
ANTHROPIC_API_KEY=
"""


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _load(config_path: Path | None) -> EmaliaConfig:
    try:
        return EmaliaConfig.load(config_path)
    except EmaliaError as exc:
        typer.secho(f"Configuration error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(f"emalia {__version__}")


@app.command()
def init(
    directory: Annotated[Path, typer.Argument(help="Where to write the starter files.")] = Path(
        "."
    ),
    force: Annotated[bool, typer.Option("--force", help="Overwrite existing files.")] = False,
) -> None:
    """Write a starter emalia.toml and .env."""
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    for name, content in (("emalia.toml", _STARTER_CONFIG), (".env", _STARTER_ENV)):
        path = directory / name
        if path.exists() and not force:
            typer.secho(f"{path} already exists, leaving it alone.", fg=typer.colors.YELLOW)
            continue
        path.write_text(content, encoding="utf-8")
        written.append(path)

    for path in written:
        typer.secho(f"Wrote {path}", fg=typer.colors.GREEN)
    if written:
        typer.echo(
            "\nNext: fill in .env, add your address to allowed_senders in "
            "emalia.toml, then run `emalia check`."
        )
    if (directory / ".env") in written:
        typer.secho(
            "Add .env to your .gitignore. It will hold your mail password.",
            fg=typer.colors.YELLOW,
        )


@app.command()
def check(
    config: ConfigOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Verify mail servers, model credentials, and the policy."""
    _configure_logging(verbose)
    settings = _load(config)

    typer.secho(f"Instance: {settings.instance_name}", bold=True)
    for key, value in settings.account.redacted().items():
        typer.echo(f"  {key}: {value}")

    failures: list[str] = []

    if settings.account.oauth is not None:
        # Checked before the servers are touched. An expired grant and a
        # disabled mailbox both surface as a login failure otherwise, and they
        # have nothing to do with each other.
        label = "Delegation" if settings.account.auth == "service_account" else "OAuth grant"
        typer.secho(f"\n{label}", bold=True)
        try:
            settings.account.oauth.access_token(force_refresh=True)
        except EmaliaError as exc:
            typer.secho(f"  token: {exc}", fg=typer.colors.RED)
            failures.append(settings.account.auth)
        else:
            typer.secho("  token: ok", fg=typer.colors.GREEN)

    typer.secho("\nMail servers", bold=True)
    from emalia.mail.client import MailClient

    results = MailClient(settings.account).check()
    for protocol, result in results.items():
        ok = result == "ok"
        typer.secho(
            f"  {protocol}: {result}",
            fg=typer.colors.GREEN if ok else typer.colors.RED,
        )
        if not ok:
            failures.append(protocol)

    typer.secho("\nModel", bold=True)
    typer.echo(f"  provider: {settings.llm.provider}")
    typer.echo(f"  model: {settings.llm.model}")
    if settings.llm.key_present():
        typer.secho("  credentials: ok", fg=typer.colors.GREEN)
    else:
        typer.secho(f"  credentials: {settings.llm.key_env} is not set", fg=typer.colors.RED)
        failures.append("llm")

    typer.secho("\nPolicy", bold=True)
    try:
        warnings = settings.policy.validate()
    except EmaliaError as exc:
        typer.secho(f"  invalid: {exc}", fg=typer.colors.RED)
        failures.append("policy")
        warnings = []
    else:
        typer.secho("  valid", fg=typer.colors.GREEN)
    for key, value in settings.policy.as_dict().items():
        typer.echo(f"  {key}: {value}")
    for warning in warnings:
        typer.secho(f"  warning: {warning}", fg=typer.colors.YELLOW)

    if failures:
        typer.secho(
            f"\n{len(failures)} check(s) failed: {', '.join(failures)}", fg=typer.colors.RED
        )
        raise typer.Exit(code=1)
    typer.secho("\nEverything checks out.", fg=typer.colors.GREEN, bold=True)


@app.command()
def run(
    config: ConfigOption = None,
    dry_run: DryRunOption = False,
    verbose: VerboseOption = False,
) -> None:
    """Watch the mailbox and answer what arrives."""
    _configure_logging(verbose)
    settings = _load(config)
    if dry_run:
        settings.dry_run = True
        typer.secho("Dry run: nothing will actually be sent.", fg=typer.colors.YELLOW)

    from emalia.runtime.listener import EmaliaListener

    try:
        listener = EmaliaListener(settings)
    except EmaliaError as exc:
        typer.secho(f"Cannot start: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    typer.secho(
        f"{settings.instance_name} is watching {settings.account.address}. Ctrl-C to stop.",
        fg=typer.colors.GREEN,
    )
    try:
        stats = listener.run()
    except KeyboardInterrupt:
        listener.stop()
        typer.echo("\nStopping.")
        return
    typer.echo(f"Done: {stats.as_dict()}")


@app.command()
def once(
    config: ConfigOption = None,
    dry_run: DryRunOption = False,
    verbose: VerboseOption = False,
) -> None:
    """Handle one batch of unread mail, then exit. Suits cron."""
    _configure_logging(verbose)
    settings = _load(config)
    settings.dry_run = settings.dry_run or dry_run

    from emalia.runtime.listener import EmaliaListener

    try:
        stats = EmaliaListener(settings).run_once()
    except EmaliaError as exc:
        typer.secho(f"Failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(str(stats.as_dict()))
    if stats.failed:
        raise typer.Exit(code=1)


@app.command()
def send(
    to: Annotated[str, typer.Argument(help="Recipient address.")],
    subject: Annotated[str, typer.Option("--subject", "-s", help="Subject line.")],
    body: Annotated[
        str | None,
        typer.Option("--body", "-b", help="Body text. Read from stdin when omitted."),
    ] = None,
    attach: Annotated[
        list[Path] | None,
        typer.Option("--attach", "-a", help="File or directory to attach. Repeatable."),
    ] = None,
    config: ConfigOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Send one email from the configured account.

    Bypasses the agent entirely, so it is the quickest way to prove the SMTP
    side of the setup works.
    """
    _configure_logging(verbose)
    settings = _load(config)
    text = body if body is not None else sys.stdin.read()

    from emalia.mail.client import MailClient

    try:
        with MailClient(settings.account, footer=settings.effective_footer) as mail:
            recipients = mail.send(
                to=to,
                subject=subject,
                body=text,
                attachments=[str(p) for p in (attach or [])],
            )
    except EmaliaError as exc:
        typer.secho(f"Send failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.secho(f"Sent to {', '.join(recipients)}", fg=typer.colors.GREEN)


@app.command()
def inbox(
    limit: Annotated[int, typer.Option("--limit", "-n", help="How many to list.")] = 10,
    unread: Annotated[bool, typer.Option("--unread", help="Only unread messages.")] = False,
    config: ConfigOption = None,
    verbose: VerboseOption = False,
) -> None:
    """List recent messages. Another quick way to prove IMAP works."""
    _configure_logging(verbose)
    settings = _load(config)

    from emalia.mail.client import MailClient
    from emalia.mail.models import summaries_to_text

    try:
        with MailClient(settings.account) as mail:
            typer.echo(summaries_to_text(mail.summaries(limit=limit, unseen_only=unread)))
    except EmaliaError as exc:
        typer.secho(f"Could not read the mailbox: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


auth_app = typer.Typer(
    name="auth",
    help="Obtain and inspect OAuth credentials for the mailbox.",
    no_args_is_help=True,
)
google_app = typer.Typer(
    name="google",
    help="OAuth for Gmail and Google Workspace mailboxes.",
    no_args_is_help=True,
)
microsoft_app = typer.Typer(
    name="microsoft",
    help="OAuth for Outlook.com and Microsoft 365 mailboxes.",
    no_args_is_help=True,
)
auth_app.add_typer(google_app)
auth_app.add_typer(microsoft_app)
app.add_typer(auth_app)

# Every provider's login takes the same options, so they are declared once here
# rather than repeated down each sub-command's signature.
AuthAddressOption = Annotated[
    str | None,
    typer.Option("--address", "-a", help="Mailbox to authorise. Preselected on the screen."),
]
ClientSecretsOption = Annotated[
    Path | None,
    typer.Option("--client-secrets", help="OAuth client JSON. Auto-discovered when omitted."),
]
ClientIdOption = Annotated[
    str | None,
    typer.Option("--client-id", help="OAuth client id, instead of a client JSON file."),
]
ClientSecretOption = Annotated[
    str | None,
    typer.Option("--client-secret", help="OAuth client secret, paired with --client-id."),
]
TokenFileOption = Annotated[
    Path | None,
    typer.Option("--token-file", help="Token path. Defaults to the config dir."),
]
ScopeOption = Annotated[
    str | None,
    typer.Option("--scope", help="Override the requested scope. Rarely needed."),
]
PortOption = Annotated[
    int,
    typer.Option("--port", help="Loopback port for the redirect. 0 picks a free one."),
]
BrowserOption = Annotated[
    bool,
    typer.Option("--browser/--no-browser", help="Open the consent URL, or just print it."),
]
PrintEnvOption = Annotated[
    bool,
    typer.Option("--print-env", help="Also print the credential as environment variables."),
]
RefreshOption = Annotated[
    bool,
    typer.Option("--refresh", help="Actually exchange the token, proving it still works."),
]
TenantOption = Annotated[
    str,
    typer.Option("--tenant", help="Entra directory id, or common/organizations/consumers."),
]
app.add_typer(auth_app)


def _resolve_client(
    client_id: str | None, client_secret: str | None, client_secrets: Path | None
) -> ClientApp | None:
    """Find the OAuth client to run the consent flow against.

    Explicit beats implicit throughout: flags, then a named file, then the
    environment, then whatever happens to be lying in a config directory.

    Args:
        client_id: `--client-id`, if given.
        client_secret: `--client-secret`, if given.
        client_secrets: `--client-secrets`, if given.

    Returns:
        The client, or None when nothing is configured.

    Raises:
        ConfigurationError: If a file is named but unusable, or only one half
            of an id/secret pair is supplied.
    """
    from emalia.auth import discover_client_secrets

    if client_id or client_secret:
        if not client_id or not client_secret:
            missing = "--client-id" if not client_id else "--client-secret"
            raise ConfigurationError(f"{missing} is also required.")
        typer.echo("Using the OAuth client from the command line")
        return ClientApp(client_id=client_id, client_secret=client_secret)

    if client_secrets:
        typer.echo(f"Using the OAuth client at {client_secrets}")
        return ClientApp.from_client_secrets(client_secrets)

    from_env = ClientApp.from_env()
    if from_env is not None:
        typer.echo("Using the OAuth client from the environment")
        return from_env

    discovered = discover_client_secrets()
    if discovered is not None:
        typer.echo(f"Using the OAuth client at {discovered}")
        return ClientApp.from_client_secrets(discovered)
    return None


def _run_login(
    provider: OAuthProvider,
    *,
    address: str | None,
    client_secrets: Path | None,
    client_id: str | None,
    client_secret: str | None,
    token_file: Path | None,
    scope: str | None,
    port: int,
    open_browser: bool,
    print_env: bool,
    verbose: bool,
) -> None:
    """Shared body of `emalia auth <provider> login`.

    The consent flow is plain RFC 6749 with PKCE, so the only things that vary
    between providers are the endpoints and scope carried on `provider`.
    """
    _configure_logging(verbose)
    from emalia.auth import run_consent_flow
    from emalia.mail.oauth import default_token_path

    try:
        client = _resolve_client(client_id, client_secret, client_secrets)
    except EmaliaError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc
    if client is None:
        typer.secho(
            "No OAuth client to authorise against. Supply one of:\n"
            "  --client-id and --client-secret\n"
            "  EMALIA_OAUTH_CLIENT_ID and EMALIA_OAUTH_CLIENT_SECRET in the environment\n"
            "  --client-secrets path/to/client_secret.json\n"
            "Create the client first if you have none: see docs/authentication.md "
            f"for the {provider.label} console steps.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    # A client JSON file carries its own endpoints, but flags and environment
    # variables do not, so the provider is what settles them either way.
    client = replace(client, auth_uri=provider.auth_uri, token_uri=provider.token_uri)

    try:
        credentials = run_consent_flow(
            client,
            scope=scope or provider.scope,
            login_hint=address,
            port=port,
            open_browser=open_browser,
        )
        written = credentials.save(token_file or default_token_path(provider.key))
    except EmaliaError as exc:
        typer.secho(f"Login failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    granted = credentials.account or "(the account you selected)"
    typer.secho(f"Authorised {granted}", fg=typer.colors.GREEN, bold=True)
    typer.echo(f"Token written to {written}")
    if address and credentials.account and credentials.account.lower() != address.lower():
        typer.secho(
            f"You asked for {address} but consented as {credentials.account}. "
            "The token will only open that second mailbox.",
            fg=typer.colors.YELLOW,
        )

    if print_env:
        typer.echo("\nFor CI, where there is no file to mount:\n")
        typer.echo(f"EMALIA_OAUTH_CLIENT_ID={credentials.client_id}")
        typer.echo(f"EMALIA_OAUTH_CLIENT_SECRET={credentials.client_secret}")
        typer.echo(f"EMALIA_OAUTH_REFRESH_TOKEN={credentials.refresh_token}")
        typer.echo(f"EMALIA_OAUTH_TOKEN_URI={credentials.token_uri}")
        typer.secho(
            "\nThose are secrets. Put them in a secret store, not in a shell history.",
            fg=typer.colors.YELLOW,
        )
    else:
        typer.echo(
            "\nNext: set EMALIA_AUTH=oauth (and remove EMALIA_PASSWORD), then run "
            "`emalia check`. Add --print-env to get the CI form."
        )


@google_app.command("login")
def auth_google_login(
    address: AuthAddressOption = None,
    client_secrets: ClientSecretsOption = None,
    client_id: ClientIdOption = None,
    client_secret: ClientSecretOption = None,
    token_file: TokenFileOption = None,
    scope: ScopeOption = None,
    port: PortOption = 0,
    open_browser: BrowserOption = True,
    print_env: PrintEnvOption = False,
    verbose: VerboseOption = False,
) -> None:
    """Authorise a Google mailbox and store the refresh token.

    Needs an OAuth client to authorise against, which is the one part no CLI
    can create for you. `docs/authentication.md` has the console steps.

    The client can come from `--client-id` and `--client-secret`, from the
    `EMALIA_OAUTH_CLIENT_ID` and `EMALIA_OAUTH_CLIENT_SECRET` variables (or
    their `GOOGLE_OAUTH_*` aliases), or from a client JSON file.
    """
    _run_login(
        GOOGLE_PROVIDER,
        address=address,
        client_secrets=client_secrets,
        client_id=client_id,
        client_secret=client_secret,
        token_file=token_file,
        scope=scope,
        port=port,
        open_browser=open_browser,
        print_env=print_env,
        verbose=verbose,
    )


@microsoft_app.command("login")
def auth_microsoft_login(
    address: AuthAddressOption = None,
    tenant: TenantOption = DEFAULT_MICROSOFT_TENANT,
    client_secrets: ClientSecretsOption = None,
    client_id: ClientIdOption = None,
    client_secret: ClientSecretOption = None,
    token_file: TokenFileOption = None,
    scope: ScopeOption = None,
    port: PortOption = 0,
    open_browser: BrowserOption = True,
    print_env: PrintEnvOption = False,
    verbose: VerboseOption = False,
) -> None:
    """Authorise an Outlook.com or Microsoft 365 mailbox.

    Register an application in Entra ID first, add a Mobile and desktop
    redirect of `http://localhost`, and grant the delegated permissions
    `IMAP.AccessAsUser.All`, `SMTP.Send` and `offline_access`.
    `docs/authentication.md` has the steps.

    A single-tenant registration must pass its directory id as `--tenant`; the
    default admits both personal and work accounts.
    """
    _run_login(
        microsoft_provider(tenant),
        address=address,
        client_secrets=client_secrets,
        client_id=client_id,
        client_secret=client_secret,
        token_file=token_file,
        scope=scope,
        port=port,
        open_browser=open_browser,
        print_env=print_env,
        verbose=verbose,
    )


def _run_status(provider: OAuthProvider, token_file: Path | None, refresh: bool) -> None:
    """Shared body of `emalia auth <provider> status`."""
    from emalia.mail.oauth import OAuthCredentials, default_token_path

    target = token_file or default_token_path(provider.key)
    try:
        credentials = OAuthCredentials.from_file(target)
    except EmaliaError as exc:
        typer.secho(str(exc), fg=typer.colors.YELLOW)
        raise typer.Exit(code=1) from exc

    typer.secho(f"Token file: {target}", bold=True)
    for key, value in credentials.redacted().items():
        typer.echo(f"  {key}: {value}")

    if not refresh:
        typer.echo("\nAdd --refresh to check the grant is still live.")
        return
    try:
        credentials.access_token(force_refresh=True)
    except EmaliaError as exc:
        typer.secho(f"\nRefresh failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.secho("\nRefresh: ok", fg=typer.colors.GREEN)


@google_app.command("status")
def auth_google_status(token_file: TokenFileOption = None, refresh: RefreshOption = False) -> None:
    """Show the stored Google token, and optionally prove it still refreshes."""
    _run_status(GOOGLE_PROVIDER, token_file, refresh)


@microsoft_app.command("status")
def auth_microsoft_status(
    token_file: TokenFileOption = None, refresh: RefreshOption = False
) -> None:
    """Show the stored Microsoft token, and optionally prove it still refreshes."""
    _run_status(microsoft_provider(), token_file, refresh)


@google_app.command("service-account")
def auth_google_service_account(
    key: Annotated[
        Path,
        typer.Option("--key", "-k", help="The service account key JSON."),
    ],
    address: Annotated[
        str,
        typer.Option("--address", "-a", help="The mailbox the account should act as."),
    ],
    check: Annotated[
        bool,
        typer.Option("--check", help="Mint a token, proving delegation is actually in place."),
    ] = False,
) -> None:
    """Inspect a service account key and show what to authorise it for.

    The credential a deployed system wants: no browser, no expiry, and rotated
    by `gcloud` rather than by a person. It needs a Google Workspace domain and
    a one-time authorisation by an administrator, which this command prints the
    exact values for.
    """
    from emalia.mail.oauth import GMAIL_SCOPE, ServiceAccountCredentials

    try:
        credentials = ServiceAccountCredentials.from_file(key, subject=address)
    except EmaliaError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    typer.secho("Service account", bold=True)
    for name, value in credentials.redacted().items():
        typer.echo(f"  {name}: {value}")

    typer.secho("\nAuthorise it once, as a Workspace administrator", bold=True)
    typer.echo("  admin.google.com > Security > Access and data control")
    typer.echo("  > API controls > Domain-wide delegation > Add new")
    typer.echo(f"\n  Client ID: {credentials.client_id or '(not in the key file)'}")
    typer.echo(f"  Scopes:    {GMAIL_SCOPE}")
    typer.secho(
        "\nThe Client ID is the numeric one above, not the service account's email address.",
        fg=typer.colors.YELLOW,
    )

    if not check:
        typer.echo("\nAdd --check to confirm the delegation is live.")
        return
    try:
        credentials.access_token(force_refresh=True)
    except EmaliaError as exc:
        typer.secho(f"\nToken request failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.secho(f"\nDelegation is live: minted a token for {address}.", fg=typer.colors.GREEN)
    typer.echo(
        f"\nNext: set EMALIA_ADDRESS={address}, EMALIA_SERVICE_ACCOUNT_FILE={key}, "
        "and remove EMALIA_PASSWORD."
    )


def _run_logout(provider: OAuthProvider, token_file: Path | None) -> None:
    """Shared body of `emalia auth <provider> logout`."""
    from emalia.mail.oauth import default_token_path

    target = token_file or default_token_path(provider.key)
    if not target.exists():
        typer.echo(f"No token at {target}.")
        return
    target.unlink()
    typer.secho(f"Deleted {target}", fg=typer.colors.GREEN)
    typer.echo(f"The grant still exists at {provider.revoke_url}.")


@google_app.command("logout")
def auth_google_logout(token_file: TokenFileOption = None) -> None:
    """Delete the stored Google token.

    Local only. It does not revoke the grant, which is done at
    https://myaccount.google.com/permissions.
    """
    _run_logout(GOOGLE_PROVIDER, token_file)


@microsoft_app.command("logout")
def auth_microsoft_logout(token_file: TokenFileOption = None) -> None:
    """Delete the stored Microsoft token.

    Local only. It does not revoke the grant, which is done at
    https://account.microsoft.com/privacy/app-access.
    """
    _run_logout(microsoft_provider(), token_file)


@app.command()
def audit(
    count: Annotated[int, typer.Option("--count", "-n", help="How many records.")] = 20,
    config: ConfigOption = None,
) -> None:
    """Show recent entries from the audit log."""
    settings = _load(config)
    from emalia.runtime.state import AuditLog

    records = AuditLog(settings.audit_path).tail(count)
    if not records:
        typer.echo(f"No audit records at {settings.audit_path}.")
        return
    for record in records:
        typer.echo(
            f"{record.get('at', '')}  {record.get('event', ''):<12} "
            f"{record.get('sender') or '-':<30} {record.get('outcome', '')}"
        )


def main() -> None:
    """Entry point for the `emalia` console script."""
    app()


if __name__ == "__main__":
    main()
