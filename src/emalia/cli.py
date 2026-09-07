"""The `emalia` command line.

`check` is the one to run first. It verifies both mail servers and the model
credentials and prints the policy it resolved, which is where most setup
problems announce themselves.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Annotated

import typer

from emalia import __version__
from emalia.config import EmaliaConfig
from emalia.errors import EmaliaError

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
