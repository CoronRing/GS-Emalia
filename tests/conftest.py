"""Shared fixtures for the offline suite.

Nothing here touches a network. The fakes stand in for IMAP and SMTP so the
whole suite runs offline, which is what makes it usable in CI.

`tests/e2e/` is the exception: it talks to a real mail server and is skipped
unless `EMALIA_E2E=1` is set. See `docs/e2e-testing.md`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from emalia.config import EmaliaConfig, LLMSettings
from emalia.mail.accounts import MailAccount
from emalia.mail.models import EmailAddress, EmailMessage
from emalia.security.policy import Policy


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register options used by the end-to-end suite.

    It has to live here rather than in `tests/e2e/conftest.py`: pytest reads
    `pytest_addoption` only from the conftest files it loads before collection
    starts, and a subdirectory's conftest is not one of them.
    """
    parser.addoption(
        "--e2e-keep",
        action="store_true",
        default=False,
        help="Leave the messages the e2e suite sends in the mailbox, for debugging.",
    )


@pytest.fixture
def account() -> MailAccount:
    """A Gmail-shaped account with dummy credentials."""
    return MailAccount.for_provider("gmail", "bot@example.com", "app-password")


@pytest.fixture
def policy(tmp_path: Path) -> Policy:
    """A policy that answers one sender and sandboxes to a temp directory."""
    return Policy(
        allowed_senders=["alice@example.com"],
        sandbox_roots=[tmp_path],
        enabled_toolsets=["email", "file_read", "file_write"],
    )


@pytest.fixture
def config(account: MailAccount, policy: Policy, tmp_path: Path) -> EmaliaConfig:
    """A complete config wired to the temp directory."""
    return EmaliaConfig(
        account=account,
        policy=policy,
        llm=LLMSettings(provider="anthropic", model="claude-sonnet-4-6"),
        state_dir=tmp_path / "state",
    )


@pytest.fixture
def incoming() -> EmailMessage:
    """A parsed inbound message from the allowlisted sender."""
    return EmailMessage(
        uid="12",
        message_id="<msg-1@example.com>",
        subject="hello",
        sender=EmailAddress(address="alice@example.com", name="Alice"),
        to=[EmailAddress(address="bot@example.com")],
        text="Please list my notes.",
    )
