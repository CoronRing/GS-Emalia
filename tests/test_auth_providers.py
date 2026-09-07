"""Provider records, per-provider token paths, and OAuth client resolution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from emalia.auth import ClientApp
from emalia.cli import _resolve_client
from emalia.errors import ConfigurationError
from emalia.mail.oauth import (
    DEFAULT_MICROSOFT_TENANT,
    GMAIL_SCOPE,
    GOOGLE_PROVIDER,
    OAUTH_PROVIDERS,
    OUTLOOK_SCOPE,
    default_token_path,
    microsoft_provider,
)


class TestProviders:
    def test_both_providers_are_registered(self) -> None:
        assert set(OAUTH_PROVIDERS) == {"google", "microsoft"}

    def test_each_key_matches_its_record(self) -> None:
        for key, provider in OAUTH_PROVIDERS.items():
            assert provider.key == key

    def test_google_carries_the_mail_scope(self) -> None:
        assert GOOGLE_PROVIDER.scope == GMAIL_SCOPE
        assert GOOGLE_PROVIDER.auth_uri.startswith("https://accounts.google.com/")

    def test_microsoft_requests_offline_access(self) -> None:
        """Without it Microsoft returns no refresh token at all."""
        provider = microsoft_provider()
        assert provider.scope == OUTLOOK_SCOPE
        assert "offline_access" in provider.scope

    def test_microsoft_defaults_to_the_common_tenant(self) -> None:
        provider = microsoft_provider()
        assert f"/{DEFAULT_MICROSOFT_TENANT}/" in provider.auth_uri
        assert f"/{DEFAULT_MICROSOFT_TENANT}/" in provider.token_uri

    def test_microsoft_honours_a_named_tenant(self) -> None:
        """A single-tenant registration is rejected by the common endpoint."""
        provider = microsoft_provider("contoso.onmicrosoft.com")
        assert "/contoso.onmicrosoft.com/" in provider.auth_uri
        assert "/contoso.onmicrosoft.com/" in provider.token_uri

    def test_every_endpoint_is_https(self) -> None:
        for provider in OAUTH_PROVIDERS.values():
            assert provider.auth_uri.startswith("https://")
            assert provider.token_uri.startswith("https://")
            assert provider.revoke_url.startswith("https://")


class TestTokenPaths:
    def test_providers_do_not_share_a_token_file(self) -> None:
        """Authorising both on one machine must not overwrite the first."""
        assert default_token_path("google") != default_token_path("microsoft")

    def test_google_keeps_its_established_filename(self) -> None:
        assert default_token_path().name == "google_oauth.json"
        assert default_token_path("google").name == "google_oauth.json"

    def test_microsoft_has_its_own_filename(self) -> None:
        assert default_token_path("microsoft").name == "microsoft_oauth.json"

    def test_an_unknown_provider_still_gets_a_path(self) -> None:
        assert default_token_path("fastmail").name == "fastmail_oauth.json"

    def test_the_environment_override_wins_for_every_provider(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        target = tmp_path / "token.json"
        monkeypatch.setenv("EMALIA_OAUTH_TOKEN_FILE", str(target))
        assert default_token_path("google") == target
        assert default_token_path("microsoft") == target


class TestClientAppFromEnv:
    def test_reads_the_emalia_pair(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EMALIA_OAUTH_CLIENT_ID", "id-1")
        monkeypatch.setenv("EMALIA_OAUTH_CLIENT_SECRET", "secret-1")
        client = ClientApp.from_env()
        assert client is not None
        assert (client.client_id, client.client_secret) == ("id-1", "secret-1")

    def test_reads_the_google_aliases(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "id-2")
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret-2")
        client = ClientApp.from_env()
        assert client is not None
        assert client.client_id == "id-2"

    def test_returns_none_when_nothing_is_set(self) -> None:
        assert ClientApp.from_env() is None

    def test_half_a_pair_is_reported_rather_than_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Silently falling through would send the operator hunting elsewhere."""
        monkeypatch.setenv("EMALIA_OAUTH_CLIENT_ID", "id-3")
        with pytest.raises(ConfigurationError, match="EMALIA_OAUTH_CLIENT_SECRET"):
            ClientApp.from_env()

    def test_a_separate_prefix_cannot_see_the_shared_aliases(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The e2e prefix exists to be isolated; aliases must not leak into it."""
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "id-4")
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret-4")
        assert ClientApp.from_env("EMALIA_E2E_") is None


class TestResolveClient:
    def test_flags_win_over_everything(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("EMALIA_OAUTH_CLIENT_ID", "from-env")
        monkeypatch.setenv("EMALIA_OAUTH_CLIENT_SECRET", "env-secret")
        client = _resolve_client("from-flag", "flag-secret", None)
        assert client is not None
        assert client.client_id == "from-flag"

    def test_half_a_flag_pair_is_an_error(self) -> None:
        with pytest.raises(ConfigurationError, match="--client-secret"):
            _resolve_client("only-an-id", None, None)
        with pytest.raises(ConfigurationError, match="--client-id"):
            _resolve_client(None, "only-a-secret", None)

    def test_a_named_file_beats_the_environment(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("EMALIA_OAUTH_CLIENT_ID", "from-env")
        monkeypatch.setenv("EMALIA_OAUTH_CLIENT_SECRET", "env-secret")
        path = tmp_path / "client_secret.json"
        path.write_text(
            json.dumps({"installed": {"client_id": "from-file", "client_secret": "file-secret"}}),
            encoding="utf-8",
        )
        client = _resolve_client(None, None, path)
        assert client is not None
        assert client.client_id == "from-file"

    def test_falls_back_to_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EMALIA_OAUTH_CLIENT_ID", "from-env")
        monkeypatch.setenv("EMALIA_OAUTH_CLIENT_SECRET", "env-secret")
        client = _resolve_client(None, None, None)
        assert client is not None
        assert client.client_id == "from-env"

    def test_returns_none_when_there_is_nothing_to_find(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Discovery must not reach a real client_secret.json on the machine."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setenv("APPDATA", str(tmp_path))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        assert _resolve_client(None, None, None) is None
