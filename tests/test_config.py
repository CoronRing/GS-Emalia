"""Configuration loading and account resolution."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from emalia.config import EmaliaConfig, LLMSettings
from emalia.errors import ConfigurationError
from emalia.mail.accounts import PROVIDER_PRESETS, MailAccount


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every EMALIA_* variable so a developer's own .env cannot leak in."""
    for key in list(os.environ):
        if key.startswith("EMALIA_"):
            monkeypatch.delenv(key, raising=False)


class TestMailAccount:
    def test_provider_preset_fills_in_the_endpoints(self) -> None:
        account = MailAccount.for_provider("gmail", "me@gmail.com", "pw")
        assert account.imap_host == "imap.gmail.com"
        assert account.smtp_port == 465
        assert account.smtp_security == "ssl"

    def test_starttls_providers_are_covered(self) -> None:
        # The original only spoke implicit TLS, which ruled these out.
        for name in ("outlook", "icloud"):
            assert PROVIDER_PRESETS[name].smtp_security == "starttls"

    def test_unknown_provider_names_the_known_ones(self) -> None:
        with pytest.raises(ConfigurationError, match="Known providers"):
            MailAccount.for_provider("hotmail", "me@x.com", "pw")

    def test_missing_credential_is_rejected(self) -> None:
        with pytest.raises(ConfigurationError, match="no credential"):
            MailAccount(address="me@x.com", password="", imap_host="imap.x.com")

    def test_smtp_host_is_derived_from_imap_when_omitted(self) -> None:
        account = MailAccount(address="me@x.com", password="pw", imap_host="imap.x.com")
        assert account.smtp_host == "smtp.x.com"

    def test_login_defaults_to_the_address(self) -> None:
        account = MailAccount(address="me@x.com", password="pw", imap_host="imap.x.com")
        assert account.login == "me@x.com"
        assert (
            MailAccount(
                address="me@x.com", password="pw", imap_host="imap.x.com", username="other"
            ).login
            == "other"
        )

    def test_redacted_never_shows_the_password(self) -> None:
        account = MailAccount.for_provider("gmail", "me@gmail.com", "hunter2")
        rendered = str(account.redacted())
        assert "hunter2" not in rendered
        assert "***" in rendered

    def test_from_env_reads_the_provider_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EMALIA_ADDRESS", "me@gmail.com")
        monkeypatch.setenv("EMALIA_PASSWORD", "pw")
        monkeypatch.setenv("EMALIA_PROVIDER", "gmail")
        assert MailAccount.from_env().imap_host == "imap.gmail.com"

    def test_from_env_allows_host_overrides_on_a_preset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EMALIA_ADDRESS", "me@gmail.com")
        monkeypatch.setenv("EMALIA_PASSWORD", "pw")
        monkeypatch.setenv("EMALIA_PROVIDER", "gmail")
        monkeypatch.setenv("EMALIA_IMAP_HOST", "imap.internal.example")
        assert MailAccount.from_env().imap_host == "imap.internal.example"

    def test_from_env_requires_credentials(self) -> None:
        with pytest.raises(ConfigurationError, match="EMALIA_ADDRESS"):
            MailAccount.from_env()

    def test_from_env_requires_a_provider_or_a_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EMALIA_ADDRESS", "me@x.com")
        monkeypatch.setenv("EMALIA_PASSWORD", "pw")
        with pytest.raises(ConfigurationError, match="EMALIA_PROVIDER"):
            MailAccount.from_env()


class TestLLMSettings:
    def test_key_env_follows_the_provider(self) -> None:
        assert LLMSettings(provider="anthropic").key_env == "ANTHROPIC_API_KEY"
        assert LLMSettings(provider="openai").key_env == "OPENAI_API_KEY"

    def test_a_provider_needing_no_key_reports_present(self) -> None:
        assert LLMSettings(provider="ollama").key_present()

    def test_key_presence_reflects_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert not LLMSettings(provider="anthropic").key_present()
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        assert LLMSettings(provider="anthropic").key_present()


class TestConfigLoading:
    def _base_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EMALIA_ADDRESS", "bot@example.com")
        monkeypatch.setenv("EMALIA_PASSWORD", "pw")
        monkeypatch.setenv("EMALIA_PROVIDER", "gmail")

    def test_loads_without_a_config_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._base_env(monkeypatch)
        config = EmaliaConfig.load()
        assert config.account.address == "bot@example.com"
        assert config.instance_name == "Emalia"

    def test_reads_a_toml_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._base_env(monkeypatch)
        path = tmp_path / "emalia.toml"
        path.write_text(
            'instance_name = "Jeeves"\n'
            "poll_interval = 60.0\n\n"
            "[llm]\n"
            'provider = "openai"\n'
            'model = "gpt-5"\n\n'
            "[policy]\n"
            'allowed_senders = ["alice@example.com"]\n'
            "max_tool_calls = 7\n",
            encoding="utf-8",
        )
        config = EmaliaConfig.load(path)
        assert config.instance_name == "Jeeves"
        assert config.poll_interval == 60.0
        assert config.llm.provider == "openai"
        assert config.policy.allowed_senders == ["alice@example.com"]
        assert config.policy.max_tool_calls == 7

    def test_environment_beats_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._base_env(monkeypatch)
        path = tmp_path / "emalia.toml"
        path.write_text('instance_name = "FromFile"\n', encoding="utf-8")
        monkeypatch.setenv("EMALIA_INSTANCE_NAME", "FromEnv")
        assert EmaliaConfig.load(path).instance_name == "FromEnv"

    def test_overrides_beat_everything(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._base_env(monkeypatch)
        monkeypatch.setenv("EMALIA_INSTANCE_NAME", "FromEnv")
        assert EmaliaConfig.load(instance_name="Explicit").instance_name == "Explicit"

    def test_a_named_missing_file_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="not found"):
            EmaliaConfig.load(tmp_path / "nope.toml")

    def test_malformed_toml_is_reported_clearly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._base_env(monkeypatch)
        path = tmp_path / "emalia.toml"
        path.write_text("this is not = = toml\n", encoding="utf-8")
        with pytest.raises(ConfigurationError, match="not valid TOML"):
            EmaliaConfig.load(path)

    def test_credentials_in_the_config_file_are_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Secrets belong in the environment; a committable file must not be
        # able to hold one by accident.
        self._base_env(monkeypatch)
        path = tmp_path / "emalia.toml"
        path.write_text('[account]\npassword = "hunter2"\n', encoding="utf-8")
        with pytest.raises(ConfigurationError, match="Credentials belong in the environment"):
            EmaliaConfig.load(path)

    def test_unknown_policy_key_is_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._base_env(monkeypatch)
        path = tmp_path / "emalia.toml"
        path.write_text("[policy]\nallow_everything = true\n", encoding="utf-8")
        with pytest.raises(ConfigurationError, match="Unknown \\[policy\\] keys"):
            EmaliaConfig.load(path)

    def test_toolsets_can_come_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._base_env(monkeypatch)
        monkeypatch.setenv("EMALIA_TOOLSETS", "email, file_read, http")
        assert EmaliaConfig.load().policy.enabled_toolsets == ["email", "file_read", "http"]

    def test_state_paths_sit_under_the_state_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._base_env(monkeypatch)
        config = EmaliaConfig.load(state_dir=tmp_path / "state")
        assert config.seen_path.parent == config.state_dir
        assert config.audit_path.parent == config.state_dir

    def test_bad_intervals_are_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._base_env(monkeypatch)
        with pytest.raises(ConfigurationError, match="poll_interval"):
            EmaliaConfig.load(poll_interval=0.1)
        with pytest.raises(ConfigurationError, match="batch_size"):
            EmaliaConfig.load(batch_size=0)

    def test_the_default_footer_names_the_instance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._base_env(monkeypatch)
        config = EmaliaConfig.load(instance_name="Jeeves")
        assert "Jeeves" in config.effective_footer
        assert EmaliaConfig.load(footer="").effective_footer == ""
