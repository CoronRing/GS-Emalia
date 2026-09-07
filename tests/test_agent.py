"""The agent layer: model resolution, prompt rendering, and tool wiring."""

from __future__ import annotations

from pathlib import Path

import pytest

from emalia.agent import (
    UNTRUSTED_CLOSE,
    UNTRUSTED_OPEN,
    build_agent,
    render_incoming,
    render_system_prompt,
    resolve_llm,
)
from emalia.config import EmaliaConfig, LLMSettings
from emalia.errors import ConfigurationError
from emalia.mail.accounts import MailAccount
from emalia.mail.models import Attachment, EmailAddress, EmailMessage
from emalia.security.policy import Policy
from fakes import FakeMailClient


@pytest.fixture
def base_config(tmp_path: Path) -> EmaliaConfig:
    return EmaliaConfig(
        account=MailAccount.for_provider("gmail", "bot@example.com", "pw"),
        policy=Policy(
            allowed_senders=["alice@example.com"],
            sandbox_roots=[tmp_path],
            enabled_toolsets=["email", "file_read"],
        ),
        llm=LLMSettings(provider="anthropic", model="claude-sonnet-4-6"),
        state_dir=tmp_path / "state",
    )


class TestResolveLLM:
    def test_missing_key_is_reported_with_the_variable_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(ConfigurationError, match="ANTHROPIC_API_KEY"):
            resolve_llm(LLMSettings(provider="anthropic"))

    def test_unknown_provider_lists_the_valid_ones(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(ConfigurationError, match="Unknown LLM provider"):
            resolve_llm(LLMSettings(provider="mystery", api_key_env=""))

    def test_compatible_provider_needs_a_base_url(self) -> None:
        with pytest.raises(ConfigurationError, match="api_base"):
            resolve_llm(LLMSettings(provider="compatible", api_key_env=""))

    def test_a_known_provider_resolves_with_a_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        assert resolve_llm(LLMSettings(provider="anthropic", model="claude-sonnet-4-6"))


class TestSystemPrompt:
    def test_names_the_instance_and_the_mailbox(self, base_config: EmaliaConfig) -> None:
        prompt = render_system_prompt(base_config)
        assert "Emalia" in prompt
        assert "bot@example.com" in prompt

    def test_states_the_untrusted_content_rule(self, base_config: EmaliaConfig) -> None:
        prompt = render_system_prompt(base_config)
        assert UNTRUSTED_OPEN in prompt
        assert "forgery" in prompt
        assert "not instructions from your operator" in prompt

    def test_capabilities_are_generated_from_the_policy(
        self, base_config: EmaliaConfig, tmp_path: Path
    ) -> None:
        # The prompt must not be able to drift out of step with the tools that
        # actually exist, so it is rendered from the same object.
        prompt = render_system_prompt(base_config)
        assert "email" in prompt
        assert "file_read" in prompt
        assert str(tmp_path) in prompt

        base_config.policy.enabled_toolsets = ["email"]
        assert "file_read" not in render_system_prompt(base_config)

    def test_no_file_access_is_stated_plainly(self, base_config: EmaliaConfig) -> None:
        base_config.policy.sandbox_roots = []
        assert "no file access" in render_system_prompt(base_config)

    def test_house_rules_are_appended(self, base_config: EmaliaConfig) -> None:
        base_config.extra_instructions = "Always sign off as The Front Desk."
        assert "The Front Desk" in render_system_prompt(base_config)


class TestRenderIncoming:
    def test_metadata_sits_outside_the_delimiters(self) -> None:
        message = EmailMessage(
            uid="12",
            subject="a question",
            sender=EmailAddress(address="alice@example.com"),
            text="what is in my notes?",
            attachments=[Attachment(filename="x.csv", content_type="text/csv", data=b"a")],
        )
        rendered = render_incoming(message)
        header, _, body = rendered.partition(UNTRUSTED_OPEN)

        assert "alice@example.com" in header
        assert "a question" in header
        assert "x.csv" in header
        assert "what is in my notes?" in body
        assert rendered.rstrip().endswith(UNTRUSTED_CLOSE)

    def test_an_empty_body_is_described_rather_than_blank(self) -> None:
        message = EmailMessage(subject="(none)", sender=EmailAddress(address="a@b.com"))
        assert "no readable body" in render_incoming(message)


class TestBuildAgent:
    def test_builds_with_the_permitted_tools(
        self, base_config: EmaliaConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        agent = build_agent(base_config, client=FakeMailClient())
        names = {node.name() for node in agent.tool_nodes()}
        assert "list_inbox" in names
        assert "read_file" in names
        # Disabled groups must be absent from the schema entirely.
        assert not any("shell" in n for n in names)
        assert not any("write_file" in n for n in names)

    def test_extra_tools_are_attached(
        self, base_config: EmaliaConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

        def check_calendar(day: str) -> str:
            """Look up what is on the calendar.

            Args:
                day: The day to look up.

            Returns:
                What is scheduled.
            """
            return f"nothing on {day}"

        agent = build_agent(base_config, client=FakeMailClient(), extra_tools=[check_calendar])
        assert "check_calendar" in {node.name() for node in agent.tool_nodes()}

    def test_an_invalid_policy_stops_the_build(
        self, base_config: EmaliaConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        base_config.policy.enabled_toolsets = ["shell"]
        with pytest.raises(ConfigurationError, match="allow_dangerous_tools"):
            build_agent(base_config, client=FakeMailClient())
