"""Offline tests for the OAuth credential layer.

Nothing here reaches the network. The token endpoint is replaced at the
`urllib.request.urlopen` seam, which is the only place `emalia.mail.oauth`
talks to one, so the refresh logic, the caching, and the error messages are all
exercised against known responses.
"""

from __future__ import annotations

import io
import json
import os
import time
import urllib.error
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from emalia.errors import ConfigurationError, MailAuthError
from emalia.mail.accounts import MailAccount
from emalia.mail.imap import _xoauth2_responder
from emalia.mail.oauth import (
    GMAIL_SCOPE,
    GOOGLE_TOKEN_URI,
    OAuthCredentials,
    default_token_path,
    xoauth2_string,
)

# The three variables that make up the CI form of the credential.
ENV_NAMES = (
    "EMALIA_OAUTH_CLIENT_ID",
    "EMALIA_OAUTH_CLIENT_SECRET",
    "EMALIA_OAUTH_REFRESH_TOKEN",
)


class _FakeResponse:
    """The context-manager shape `urlopen` returns, holding a fixed body."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None


@pytest.fixture
def credentials() -> OAuthCredentials:
    """A credential with placeholder material, never sent anywhere."""
    return OAuthCredentials(
        client_id="123-abc.apps.googleusercontent.com",
        client_secret="secret-value",
        refresh_token="refresh-value",
    )


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep a developer's real OAuth variables out of the assertions."""
    for name in (*ENV_NAMES, "EMALIA_AUTH", "EMALIA_OAUTH_TOKEN_FILE", "EMALIA_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    yield


class TestXoauth2String:
    def test_matches_the_specified_format(self) -> None:
        # Control-A separators and a double terminator. Getting this wrong
        # produces an authentication failure with no useful server message.
        assert (
            xoauth2_string("me@gmail.com", "tok") == "user=me@gmail.com\x01auth=Bearer tok\x01\x01"
        )

    def test_carries_the_access_token_not_the_refresh_token(
        self, credentials: OAuthCredentials
    ) -> None:
        built = xoauth2_string("me@gmail.com", "access-value")
        assert "access-value" in built
        assert credentials.refresh_token not in built


class TestRefresh:
    def test_exchanges_the_refresh_token_and_caches_the_result(
        self, credentials: OAuthCredentials, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[bytes] = []

        def fake_urlopen(request: Any, timeout: float = 0) -> _FakeResponse:
            calls.append(request.data)
            return _FakeResponse({"access_token": "fresh", "expires_in": 3600})

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

        assert credentials.access_token() == "fresh"
        assert credentials.access_token() == "fresh"
        # The second call is served from cache: a refresh per connection would
        # add a round trip to every reconnect the IMAP session makes.
        assert len(calls) == 1
        assert b"grant_type=refresh_token" in calls[0]

    def test_force_refresh_ignores_the_cache(
        self, credentials: OAuthCredentials, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        issued = iter(["first", "second"])
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_, **__: _FakeResponse({"access_token": next(issued), "expires_in": 3600}),
        )
        assert credentials.access_token() == "first"
        assert credentials.access_token(force_refresh=True) == "second"

    def test_a_token_near_expiry_is_refreshed_early(
        self, credentials: OAuthCredentials, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A token valid for another 30 seconds is not usable: the LOGIN that
        # uses it can easily land after it dies.
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_, **__: _FakeResponse({"access_token": "short", "expires_in": 30}),
        )
        credentials.access_token()
        assert credentials._token.usable() is False

    def test_a_fresh_token_is_usable(
        self, credentials: OAuthCredentials, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_, **__: _FakeResponse({"access_token": "long", "expires_in": 3600}),
        )
        credentials.access_token()
        assert credentials._token.usable() is True
        assert credentials._token.expires_at > time.time()

    def test_invalid_grant_explains_the_seven_day_expiry(
        self, credentials: OAuthCredentials, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The single most common failure, and the bare error text says nothing
        # about the cause. This assertion exists so the explanation cannot be
        # dropped without a test going red.
        body = json.dumps({"error": "invalid_grant", "error_description": "Bad Request"})

        def fake_urlopen(*_: object, **__: object) -> None:
            raise urllib.error.HTTPError(
                GOOGLE_TOKEN_URI,
                400,
                "Bad Request",
                {},
                io.BytesIO(body.encode()),  # type: ignore[arg-type]
            )

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        with pytest.raises(MailAuthError, match="seven days"):
            credentials.access_token()

    def test_an_unreachable_endpoint_is_not_reported_as_bad_credentials(
        self, credentials: OAuthCredentials, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_urlopen(*_: object, **__: object) -> None:
            raise urllib.error.URLError("no route to host")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        with pytest.raises(MailAuthError, match="Cannot reach"):
            credentials.access_token()


class TestImapResponder:
    def test_answers_once_then_stays_quiet(self) -> None:
        # On failure Gmail sends a challenge holding a JSON error and waits for
        # an empty line before reporting NO. Answering with the credential
        # again there makes the exchange hang instead of failing.
        respond = _xoauth2_responder("me@gmail.com", "tok")
        assert respond(None) == b"user=me@gmail.com\x01auth=Bearer tok\x01\x01"
        assert respond(b'{"status":"400"}') == b""
        assert respond(b"") == b""


class TestValidation:
    @pytest.mark.parametrize("missing", ["client_id", "client_secret", "refresh_token"])
    def test_incomplete_credentials_are_rejected(self, missing: str) -> None:
        fields = {"client_id": "a", "client_secret": "b", "refresh_token": "c"}
        fields[missing] = ""
        with pytest.raises(ConfigurationError, match=missing):
            OAuthCredentials(**fields)


class TestFromEnv:
    def test_returns_none_when_nothing_is_set(self) -> None:
        assert OAuthCredentials.from_env() is None

    def test_reads_the_three_variables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name, value in zip(ENV_NAMES, ("id", "secret", "refresh"), strict=True):
            monkeypatch.setenv(name, value)
        loaded = OAuthCredentials.from_env()
        assert loaded is not None
        assert (loaded.client_id, loaded.refresh_token) == ("id", "refresh")
        assert loaded.token_uri == GOOGLE_TOKEN_URI

    def test_a_partial_set_is_an_error_not_a_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Silently falling back to password auth on a typo would send a
        # password to a server the operator meant to reach with a token.
        monkeypatch.setenv("EMALIA_OAUTH_CLIENT_ID", "id")
        monkeypatch.setenv("EMALIA_OAUTH_REFRESH_TOKEN", "refresh")
        with pytest.raises(ConfigurationError, match="CLIENT_SECRET"):
            OAuthCredentials.from_env()

    def test_whitespace_only_values_count_as_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ENV_NAMES:
            monkeypatch.setenv(name, "   ")
        assert OAuthCredentials.from_env() is None


class TestTokenFile:
    def test_round_trips_through_the_authorized_user_shape(self, tmp_path: Path) -> None:
        original = OAuthCredentials(
            client_id="id",
            client_secret="secret",
            refresh_token="refresh",
            scope=GMAIL_SCOPE,
            account="me@gmail.com",
        )
        written = original.save(tmp_path / "token.json")
        document = json.loads(written.read_text(encoding="utf-8"))

        # The shape google-auth and gcloud both write, so a token obtained by
        # other tooling drops in unchanged.
        assert document["type"] == "authorized_user"
        assert document["scopes"] == [GMAIL_SCOPE]

        loaded = OAuthCredentials.from_file(written)
        assert loaded.refresh_token == "refresh"
        assert loaded.account == "me@gmail.com"

    def test_a_missing_file_says_what_to_run(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="emalia auth google login"):
            OAuthCredentials.from_file(tmp_path / "absent.json")

    def test_a_non_credential_file_is_rejected(self, tmp_path: Path) -> None:
        target = tmp_path / "token.json"
        target.write_text("[]", encoding="utf-8")
        with pytest.raises(ConfigurationError, match="credential object"):
            OAuthCredentials.from_file(target)

    @pytest.mark.skipif(os.name == "nt", reason="Windows inherits the parent ACL, not a mode")
    def test_the_file_is_not_readable_by_others(self, tmp_path: Path) -> None:
        written = OAuthCredentials(
            client_id="id", client_secret="secret", refresh_token="refresh"
        ).save(tmp_path / "token.json")
        assert written.stat().st_mode & 0o077 == 0

    def test_the_default_path_is_not_the_working_directory(self) -> None:
        # A token in the CWD lands in a docker build context or a git add -A.
        assert default_token_path().parent != Path.cwd()


class TestRedaction:
    def test_no_secret_survives(self, credentials: OAuthCredentials) -> None:
        rendered = str(credentials.redacted())
        assert credentials.client_secret not in rendered
        assert credentials.refresh_token not in rendered

    def test_the_client_is_still_identifiable(self, credentials: OAuthCredentials) -> None:
        # Telling two OAuth clients apart is the common debugging need, so a
        # fragment of the id is kept deliberately.
        assert "123-abc" in str(credentials.redacted()["client_id"])


class TestMailAccountIntegration:
    def test_an_oauth_account_needs_no_password(self, credentials: OAuthCredentials) -> None:
        account = MailAccount.for_provider("gmail", "me@gmail.com", oauth=credentials)
        assert account.auth == "oauth"
        assert account.password == ""

    def test_a_password_account_still_works(self) -> None:
        assert MailAccount.for_provider("gmail", "me@gmail.com", "pw").auth == "password"

    def test_both_credentials_at_once_is_rejected(self, credentials: OAuthCredentials) -> None:
        with pytest.raises(ConfigurationError, match="both a password and an OAuth grant"):
            MailAccount(
                address="me@x.com",
                password="pw",
                imap_host="imap.x.com",
                oauth=credentials,
            )

    def test_a_provider_without_an_oauth_path_refuses_one(
        self, credentials: OAuthCredentials
    ) -> None:
        with pytest.raises(ConfigurationError, match="no OAuth path"):
            MailAccount.for_provider("zoho", "me@zoho.com", oauth=credentials)

    def test_redacted_reports_the_method_and_leaks_nothing(
        self, credentials: OAuthCredentials
    ) -> None:
        account = MailAccount.for_provider("gmail", "me@gmail.com", oauth=credentials)
        rendered = str(account.redacted())
        assert "'auth': 'oauth'" in rendered
        assert credentials.refresh_token not in rendered

    def test_from_env_prefers_the_oauth_triple(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EMALIA_ADDRESS", "me@gmail.com")
        monkeypatch.setenv("EMALIA_PROVIDER", "gmail")
        for name, value in zip(ENV_NAMES, ("id", "secret", "refresh"), strict=True):
            monkeypatch.setenv(name, value)
        account = MailAccount.from_env()
        assert account.auth == "oauth"
        assert account.oauth is not None
        # The address is stamped on so a mismatch can be reported later.
        assert account.oauth.account == "me@gmail.com"

    def test_from_env_reads_a_token_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, credentials: OAuthCredentials
    ) -> None:
        written = credentials.save(tmp_path / "token.json")
        monkeypatch.setenv("EMALIA_ADDRESS", "me@gmail.com")
        monkeypatch.setenv("EMALIA_PROVIDER", "gmail")
        monkeypatch.setenv("EMALIA_OAUTH_TOKEN_FILE", str(written))
        assert MailAccount.from_env().auth == "oauth"

    def test_from_env_rejects_a_password_beside_a_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, credentials: OAuthCredentials
    ) -> None:
        written = credentials.save(tmp_path / "token.json")
        monkeypatch.setenv("EMALIA_ADDRESS", "me@gmail.com")
        monkeypatch.setenv("EMALIA_PROVIDER", "gmail")
        monkeypatch.setenv("EMALIA_OAUTH_TOKEN_FILE", str(written))
        monkeypatch.setenv("EMALIA_PASSWORD", "leftover")
        with pytest.raises(ConfigurationError, match="Unset EMALIA_PASSWORD"):
            MailAccount.from_env()

    def test_from_env_with_no_credential_names_every_option(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EMALIA_ADDRESS", "me@gmail.com")
        monkeypatch.setenv("EMALIA_PROVIDER", "gmail")
        with pytest.raises(ConfigurationError) as caught:
            MailAccount.from_env()
        # Someone hitting this has no idea which of the three they want, so all
        # three are named with the situation each one suits.
        message = str(caught.value)
        assert "EMALIA_PASSWORD" in message
        assert "EMALIA_SERVICE_ACCOUNT_FILE" in message
        assert "emalia auth google login" in message

    def test_the_e2e_prefix_is_isolated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The suite's separate prefix has to hold for OAuth too, or a
        # configured production grant becomes reachable from the test suite.
        for name, value in zip(ENV_NAMES, ("id", "secret", "refresh"), strict=True):
            monkeypatch.setenv(name, value)
        monkeypatch.setenv("EMALIA_E2E_ADDRESS", "test@gmail.com")
        monkeypatch.setenv("EMALIA_E2E_PROVIDER", "gmail")
        monkeypatch.setenv("EMALIA_E2E_PASSWORD", "app-password")
        account = MailAccount.from_env("EMALIA_E2E_")
        assert account.auth == "password"
