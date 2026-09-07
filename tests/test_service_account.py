"""Offline tests for service account authentication and the shared env aliases.

The assertions that matter here are about the JWT: it is signed material sent
to Google, and a wrong `sub`, a missing `scope` or an over-long `exp` all fail
in ways whose error messages do not name the field at fault. Every assertion
below is checked by verifying the real signature with the matching public key,
so a change to the signing path cannot pass by producing plausible-looking
nonsense.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from emalia.errors import ConfigurationError, MailAuthError
from emalia.mail.accounts import MailAccount
from emalia.mail.oauth import (
    GMAIL_SCOPE,
    GOOGLE_ADC_ENV,
    GOOGLE_TOKEN_URI,
    OAuthCredentials,
    ServiceAccountCredentials,
)

SUBJECT = "assistant@example.com"


@pytest.fixture(scope="module")
def keypair() -> tuple[str, rsa.RSAPublicKey]:
    """A throwaway RSA key, generated once for the module.

    Returns:
        The private key in PEM form and the matching public key.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    return pem, key.public_key()


@pytest.fixture
def key_document(keypair: tuple[str, rsa.RSAPublicKey]) -> dict[str, str]:
    """The JSON `gcloud iam service-accounts keys create` writes."""
    return {
        "type": "service_account",
        "project_id": "emalia-test",
        "private_key_id": "abc123",
        "private_key": keypair[0],
        "client_email": "emalia@emalia-test.iam.gserviceaccount.com",
        "client_id": "109876543210",
        "token_uri": GOOGLE_TOKEN_URI,
    }


@pytest.fixture
def credentials(keypair: tuple[str, rsa.RSAPublicKey]) -> ServiceAccountCredentials:
    """Credentials signing with the throwaway key."""
    return ServiceAccountCredentials(
        client_email="emalia@emalia-test.iam.gserviceaccount.com",
        private_key=keypair[0],
        subject=SUBJECT,
        private_key_id="abc123",
        project_id="emalia-test",
    )


class _FakeResponse:
    """The context-manager shape `urlopen` returns."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def _segment(part: str) -> dict[str, Any]:
    """Decode one unpadded base64url JWT segment."""
    decoded: dict[str, Any] = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    return decoded


class TestAssertion:
    def test_signature_verifies_with_the_public_key(
        self, credentials: ServiceAccountCredentials, keypair: tuple[str, rsa.RSAPublicKey]
    ) -> None:
        header, claims, signature = credentials._assertion().split(".")
        signing_input = f"{header}.{claims}".encode("ascii")
        raw = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        # Raises InvalidSignature on mismatch, which is the assertion.
        keypair[1].verify(raw, signing_input, padding.PKCS1v15(), hashes.SHA256())

    def test_header_names_rs256_and_the_key(self, credentials: ServiceAccountCredentials) -> None:
        header = _segment(credentials._assertion().split(".")[0])
        assert header["alg"] == "RS256"
        # Without `kid` Google cannot tell which of an account's keys signed
        # this, which makes a rotation impossible to verify from the logs.
        assert header["kid"] == "abc123"

    def test_sub_is_the_mailbox_not_the_service_account(
        self, credentials: ServiceAccountCredentials
    ) -> None:
        # The whole of domain-wide delegation is in this one claim. With `iss`
        # alone the token would authorise the service account's own empty
        # mailbox and every IMAP call would silently see nothing.
        claims = _segment(credentials._assertion().split(".")[1])
        assert claims["sub"] == SUBJECT
        assert claims["iss"] == "emalia@emalia-test.iam.gserviceaccount.com"

    def test_scope_and_audience_are_set(self, credentials: ServiceAccountCredentials) -> None:
        claims = _segment(credentials._assertion().split(".")[1])
        assert claims["scope"] == GMAIL_SCOPE
        assert claims["aud"] == GOOGLE_TOKEN_URI

    def test_expiry_is_within_googles_one_hour_cap(
        self, credentials: ServiceAccountCredentials
    ) -> None:
        # Google rejects a longer assertion outright rather than clamping it.
        claims = _segment(credentials._assertion().split(".")[1])
        assert 0 < claims["exp"] - claims["iat"] <= 3600
        assert claims["iat"] <= time.time() + 1

    def test_an_unreadable_key_says_what_mangled_it(self) -> None:
        broken = ServiceAccountCredentials(
            client_email="a@b.iam.gserviceaccount.com",
            private_key="-----BEGIN PRIVATE KEY----- not actually a key -----END PRIVATE KEY-----",
            subject=SUBJECT,
        )
        with pytest.raises(MailAuthError, match="newlines"):
            broken._assertion()


class TestTokenExchange:
    def test_uses_the_jwt_bearer_grant(
        self, credentials: ServiceAccountCredentials, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent: list[bytes] = []

        def fake_urlopen(request: Any, timeout: float = 0) -> _FakeResponse:
            sent.append(request.data)
            return _FakeResponse({"access_token": "minted", "expires_in": 3600})

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        assert credentials.access_token() == "minted"
        assert b"urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer" in sent[0]
        assert b"assertion=" in sent[0]
        # No refresh token anywhere: there is nothing to expire, which is the
        # entire reason this credential exists.
        assert b"refresh_token" not in sent[0]

    def test_the_token_is_cached(
        self, credentials: ServiceAccountCredentials, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = 0

        def fake_urlopen(*_: object, **__: object) -> _FakeResponse:
            nonlocal calls
            calls += 1
            return _FakeResponse({"access_token": "minted", "expires_in": 3600})

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        credentials.access_token()
        credentials.access_token()
        # Signing an assertion is an RSA operation per call; caching keeps it
        # off every IMAP reconnect.
        assert calls == 1

    def test_missing_delegation_names_the_admin_console_page(
        self, credentials: ServiceAccountCredentials, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The first failure everyone hits. Google reports it as
        # `unauthorized_client` and says nothing about where to fix it.
        body = json.dumps(
            {"error": "unauthorized_client", "error_description": "Client is unauthorized"}
        )

        def fake_urlopen(*_: object, **__: object) -> None:
            raise urllib.error.HTTPError(
                GOOGLE_TOKEN_URI,
                401,
                "Unauthorized",
                {},
                _Body(body.encode()),  # type: ignore[arg-type]
            )

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        with pytest.raises(MailAuthError, match="Domain-wide delegation"):
            credentials.access_token()

    def test_a_personal_gmail_target_is_explained(
        self, credentials: ServiceAccountCredentials, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = json.dumps({"error": "invalid_grant", "error_description": "Invalid email"})

        def fake_urlopen(*_: object, **__: object) -> None:
            raise urllib.error.HTTPError(
                GOOGLE_TOKEN_URI,
                400,
                "Bad Request",
                {},
                _Body(body.encode()),  # type: ignore[arg-type]
            )

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        with pytest.raises(MailAuthError, match="personal @gmail.com"):
            credentials.access_token()


class _Body:
    """A minimal readable body for `HTTPError`."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data

    def close(self) -> None:
        return None


class TestLoading:
    def test_reads_a_key_file(self, tmp_path: Path, key_document: dict[str, str]) -> None:
        path = tmp_path / "key.json"
        path.write_text(json.dumps(key_document), encoding="utf-8")
        loaded = ServiceAccountCredentials.from_file(path, subject=SUBJECT)
        assert loaded.client_email == key_document["client_email"]
        assert loaded.subject == SUBJECT
        assert loaded.project_id == "emalia-test"

    def test_an_oauth_token_file_is_rejected_by_name(self, tmp_path: Path) -> None:
        # The two file shapes look similar enough to be confused, and the
        # generic "could not load" would send someone regenerating a fine key.
        path = tmp_path / "token.json"
        path.write_text(json.dumps({"type": "authorized_user"}), encoding="utf-8")
        with pytest.raises(ConfigurationError, match="EMALIA_OAUTH_TOKEN_FILE"):
            ServiceAccountCredentials.from_file(path, subject=SUBJECT)

    def test_inline_json_restores_escaped_newlines(
        self, monkeypatch: pytest.MonkeyPatch, key_document: dict[str, str]
    ) -> None:
        # A key pasted into a CI secret box routinely arrives with \n escaped.
        # Without the repair the PEM is unreadable and the error blames the key.
        mangled = dict(key_document)
        mangled["private_key"] = key_document["private_key"].replace("\n", "\\n")
        monkeypatch.setenv("EMALIA_SERVICE_ACCOUNT_KEY", json.dumps(mangled))
        loaded = ServiceAccountCredentials.from_env(subject=SUBJECT)
        assert loaded is not None
        loaded._assertion()  # would raise if the PEM were still mangled

    def test_returns_none_when_nothing_is_configured(self) -> None:
        assert ServiceAccountCredentials.from_env(subject=SUBJECT) is None


class TestApplicationDefaultCredentials:
    def test_is_ignored_unless_asked_for(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key_document: dict[str, str]
    ) -> None:
        # This variable is routinely set machine-wide for unrelated Cloud work.
        # Picking it up on inference would authenticate a mailbox with a key
        # nobody chose for the purpose.
        path = tmp_path / "key.json"
        path.write_text(json.dumps(key_document), encoding="utf-8")
        monkeypatch.setenv(GOOGLE_ADC_ENV, str(path))
        monkeypatch.setenv("EMALIA_ADDRESS", SUBJECT)
        monkeypatch.setenv("EMALIA_PROVIDER", "gmail")
        monkeypatch.setenv("EMALIA_PASSWORD", "app-password")
        assert MailAccount.from_env().auth == "password"

    def test_is_used_when_asked_for(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key_document: dict[str, str]
    ) -> None:
        path = tmp_path / "key.json"
        path.write_text(json.dumps(key_document), encoding="utf-8")
        monkeypatch.setenv(GOOGLE_ADC_ENV, str(path))
        monkeypatch.setenv("EMALIA_ADDRESS", SUBJECT)
        monkeypatch.setenv("EMALIA_PROVIDER", "gmail")
        monkeypatch.setenv("EMALIA_AUTH", "service_account")
        account = MailAccount.from_env()
        assert account.auth == "service_account"
        assert isinstance(account.oauth, ServiceAccountCredentials)
        # The mailbox address becomes the impersonated subject with no
        # separate variable to keep in step with it.
        assert account.oauth.subject == SUBJECT


class TestMailAccountIntegration:
    def test_reports_the_service_account_method(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key_document: dict[str, str]
    ) -> None:
        path = tmp_path / "key.json"
        path.write_text(json.dumps(key_document), encoding="utf-8")
        monkeypatch.setenv("EMALIA_ADDRESS", SUBJECT)
        monkeypatch.setenv("EMALIA_PROVIDER", "gmail")
        monkeypatch.setenv("EMALIA_SERVICE_ACCOUNT_FILE", str(path))
        account = MailAccount.from_env()
        assert account.auth == "service_account"
        assert account.password == ""

    def test_redacted_hides_the_private_key(
        self, credentials: ServiceAccountCredentials, keypair: tuple[str, rsa.RSAPublicKey]
    ) -> None:
        rendered = str(credentials.redacted())
        assert "PRIVATE KEY" not in rendered
        assert keypair[0] not in rendered
        # The key id is not a secret and is the only way to see which key a
        # running daemon actually loaded after a rotation.
        assert "abc123" in rendered

    def test_asking_for_a_method_that_is_not_configured_is_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EMALIA_ADDRESS", SUBJECT)
        monkeypatch.setenv("EMALIA_PROVIDER", "gmail")
        monkeypatch.setenv("EMALIA_AUTH", "service_account")
        monkeypatch.setenv("EMALIA_PASSWORD", "app-password")
        with pytest.raises(ConfigurationError, match="no key is configured"):
            MailAccount.from_env()

    def test_an_unknown_method_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EMALIA_ADDRESS", SUBJECT)
        monkeypatch.setenv("EMALIA_PROVIDER", "gmail")
        monkeypatch.setenv("EMALIA_AUTH", "oauth2")
        with pytest.raises(ConfigurationError, match="must be one of"):
            MailAccount.from_env()


class TestSharedAliases:
    def test_google_app_password_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EMALIA_ADDRESS", SUBJECT)
        monkeypatch.setenv("EMALIA_PROVIDER", "gmail")
        monkeypatch.setenv("GOOGLE_APP_PASSWORD", "abcdefghijklmnop")
        account = MailAccount.from_env()
        assert account.auth == "password"
        assert account.password == "abcdefghijklmnop"

    def test_the_prefixed_name_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EMALIA_ADDRESS", SUBJECT)
        monkeypatch.setenv("EMALIA_PROVIDER", "gmail")
        monkeypatch.setenv("GOOGLE_APP_PASSWORD", "shared")
        monkeypatch.setenv("EMALIA_PASSWORD", "specific")
        assert MailAccount.from_env().password == "specific"

    def test_google_oauth_names_are_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EMALIA_ADDRESS", SUBJECT)
        monkeypatch.setenv("EMALIA_PROVIDER", "gmail")
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "id")
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret")
        monkeypatch.setenv("GOOGLE_OAUTH_REFRESH_TOKEN", "refresh")
        account = MailAccount.from_env()
        assert account.auth == "oauth"
        assert isinstance(account.oauth, OAuthCredentials)
        assert account.oauth.client_id == "id"

    def test_a_client_without_a_refresh_token_says_why_it_is_not_enough(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The exact state someone lands in after creating an OAuth client and
        # assuming the two values it gave them are a credential.
        monkeypatch.setenv("EMALIA_ADDRESS", SUBJECT)
        monkeypatch.setenv("EMALIA_PROVIDER", "gmail")
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "id")
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret")
        with pytest.raises(ConfigurationError, match="identify the application, not any mailbox"):
            MailAccount.from_env()

    def test_aliases_are_invisible_to_a_custom_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The e2e suite's isolation from a production credential is the reason
        # it has its own prefix at all; a shared alias must not undo that.
        monkeypatch.setenv("GOOGLE_APP_PASSWORD", "shared")
        monkeypatch.setenv("EMALIA_E2E_ADDRESS", "test@gmail.com")
        monkeypatch.setenv("EMALIA_E2E_PROVIDER", "gmail")
        with pytest.raises(ConfigurationError, match="No credential"):
            MailAccount.from_env("EMALIA_E2E_")
