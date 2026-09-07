"""Fixtures for the end-to-end suite.

Everything here talks to a real mail server. The whole directory is skipped
unless `EMALIA_E2E=1` is set, so `pytest` on a fresh clone stays offline.

Two safety rules govern this suite, and both are load-bearing:

1. Credentials come from `EMALIA_E2E_*`, never from the plain `EMALIA_*`
   variables. A developer with a configured production instance in their
   environment cannot start this suite against it by accident.
2. Every message the suite sends carries a per-run tag in its subject, and
   cleanup only ever touches messages matching that tag. The suite will not
   delete mail it did not create, even if it crashed on a previous run.

See `docs/e2e-testing.md` for the setup.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable, Iterator

import pytest

from emalia.config import EmaliaConfig, LLMSettings
from emalia.mail.accounts import MailAccount
from emalia.mail.client import MailClient
from emalia.mail.imap import SearchCriteria
from emalia.mail.models import EmailMessage
from emalia.security.policy import Policy

#: How long to wait for a message to show up in a mailbox. Self-addressed mail
#: is usually delivered in a few seconds; a cold Gmail connection occasionally
#: takes considerably longer.
DELIVERY_TIMEOUT = float(os.environ.get("EMALIA_E2E_TIMEOUT", "180"))
POLL_EVERY = 5.0


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() or None if value else None


def _enabled() -> bool:
    return _env("EMALIA_E2E") not in (None, "0", "false", "False")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip the whole directory unless the operator opted in."""
    if _enabled():
        return
    skip = pytest.mark.skip(reason="set EMALIA_E2E=1 and the EMALIA_E2E_* credentials to run")
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip)


# -- accounts -----------------------------------------------------------------


@pytest.fixture(scope="session")
def bot_account() -> MailAccount:
    """The mailbox under test.

    Returns:
        The account built from `EMALIA_E2E_ADDRESS` and friends.
    """
    address = _env("EMALIA_E2E_ADDRESS")
    password = _env("EMALIA_E2E_PASSWORD")
    if not address or not password:
        pytest.skip("EMALIA_E2E_ADDRESS and EMALIA_E2E_PASSWORD are required")
    return MailAccount.for_provider(
        _env("EMALIA_E2E_PROVIDER") or "gmail",
        address,
        password,
        display_name="Emalia E2E",
    )


@pytest.fixture(scope="session")
def peer_account() -> MailAccount:
    """A second mailbox that plays the human.

    The listener drops mail from its own address, which is correct and not
    something to work around, so a full inbound round trip needs a mailbox that
    is genuinely somebody else. Any provider will do; it is only ever used to
    send.

    Returns:
        The peer account.
    """
    address = _env("EMALIA_E2E_PEER_ADDRESS")
    password = _env("EMALIA_E2E_PEER_PASSWORD")
    if not address or not password:
        pytest.skip(
            "EMALIA_E2E_PEER_ADDRESS and EMALIA_E2E_PEER_PASSWORD are required for the "
            "inbound round trip; see docs/e2e-testing.md"
        )
    return MailAccount.for_provider(
        _env("EMALIA_E2E_PEER_PROVIDER") or "gmail",
        address,
        password,
        display_name="Emalia E2E Peer",
    )


# -- run identity and cleanup -------------------------------------------------


@pytest.fixture(scope="session")
def run_tag() -> str:
    """A tag unique to this run, carried in every subject the suite sends.

    Returns:
        Something like ``emalia-e2e-3f1c9a2b``.
    """
    return f"emalia-e2e-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def subject(run_tag: str) -> Callable[[str], str]:
    """Build a tagged subject line.

    Returns:
        A function turning ``"reply threading"`` into
        ``"[emalia-e2e-3f1c9a2b] reply threading"``.
    """

    def make(text: str) -> str:
        return f"[{run_tag}] {text}"

    return make


@pytest.fixture(scope="session")
def bot(
    bot_account: MailAccount,
    run_tag: str,
    request: pytest.FixtureRequest,
) -> Iterator[MailClient]:
    """A connected client for the mailbox under test, cleaned up afterwards.

    Yields:
        The client. On teardown every message tagged with this run's id is
        deleted from the mailbox, and nothing else is touched.
    """
    client = MailClient(bot_account, footer="")
    results = client.check()
    for protocol, result in results.items():
        if result != "ok":
            pytest.fail(f"{protocol.upper()} for {bot_account.address} is not usable: {result}")

    try:
        yield client
    finally:
        if request.config.getoption("--e2e-keep"):
            print(f"\n--e2e-keep: leaving messages tagged {run_tag} in place")
        else:
            _purge(client, run_tag)
        client.close()


@pytest.fixture(scope="session")
def peer(peer_account: MailAccount) -> Iterator[MailClient]:
    """A connected client for the peer mailbox.

    Yields:
        The client. Sending only, so nothing is cleaned up on that side beyond
        closing the connection.
    """
    client = MailClient(peer_account, footer="")
    if client.check()["smtp"] != "ok":
        pytest.fail(f"SMTP for the peer {peer_account.address} is not usable")
    try:
        yield client
    finally:
        client.close()


def _purge(client: MailClient, run_tag: str) -> None:
    """Delete every message this run created, and only those."""
    removed = 0
    for folder in ("INBOX", "[Gmail]/Sent Mail", "Sent"):
        try:
            client.select(folder)
        except Exception:
            continue  # The folder does not exist on this provider.
        try:
            uids = client.search(SearchCriteria().subject(run_tag))
        except Exception:
            continue
        if not uids:
            continue
        # Re-read the subjects rather than trusting SEARCH: on providers with
        # fuzzy subject matching a broad match would otherwise delete mail the
        # suite did not send.
        confirmed = [uid for uid in uids if run_tag in (client.imap.fetch(uid).subject or "")]
        if confirmed:
            client.delete(confirmed)
            removed += len(confirmed)
    if removed:
        print(f"\nCleaned up {removed} message(s) tagged {run_tag}")


# -- waiting ------------------------------------------------------------------


@pytest.fixture
def wait_for_subject(bot: MailClient) -> Callable[..., EmailMessage]:
    """Poll the mailbox until a message with a given subject fragment arrives.

    Returns:
        A function taking the fragment and returning the parsed message, or
        failing the test after `DELIVERY_TIMEOUT` seconds.
    """

    def wait(
        fragment: str,
        *,
        folder: str = "INBOX",
        timeout: float = DELIVERY_TIMEOUT,
        sender: str | None = None,
    ) -> EmailMessage:
        deadline = time.monotonic() + timeout
        attempts = 0
        while time.monotonic() < deadline:
            attempts += 1
            for message in _recent(bot, folder):
                if fragment not in (message.subject or ""):
                    continue
                if sender and (not message.sender or message.sender.address != sender.lower()):
                    continue
                return message
            time.sleep(POLL_EVERY)
        pytest.fail(
            f"No message with {fragment!r} in {folder} after {timeout:.0f}s "
            f"({attempts} polls). Delivery may be slow, or the send never happened."
        )

    return wait


def _recent(client: MailClient, folder: str, limit: int = 25) -> list[EmailMessage]:
    """The newest messages in a folder, tolerating a mailbox that is empty."""
    try:
        client.select(folder)
        uids = client.search(limit=limit)
    except Exception:
        return []
    return [client.imap.fetch(uid) for uid in uids]


# -- agent configuration ------------------------------------------------------


@pytest.fixture(scope="session")
def llm_settings() -> LLMSettings:
    """The model the agent tests run against.

    Returns:
        Settings from `EMALIA_E2E_LLM_PROVIDER` and `EMALIA_E2E_LLM_MODEL`,
        defaulting to Anthropic.
    """
    settings = LLMSettings(
        provider=_env("EMALIA_E2E_LLM_PROVIDER") or "anthropic",
        model=_env("EMALIA_E2E_LLM_MODEL") or "claude-sonnet-4-6",
    )
    if not settings.key_present():
        pytest.skip(f"{settings.key_env} is not set, so the model cannot be reached")
    return settings


@pytest.fixture
def agent_config(
    bot_account: MailAccount,
    llm_settings: LLMSettings,
    run_tag: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> EmaliaConfig:
    """A config for an agent that may read the mailbox and one temp directory.

    Two containment choices matter here, because these tests run against a
    mailbox that may hold real mail:

    - `require_token` is set to this run's tag, so the listener answers only
      messages this suite sent. Unread mail from a colleague is ignored even
      if their address is somehow allowlisted.
    - `sandbox_roots` and `state_dir` are pytest temp directories, so no
      prompt can reach anything outside them.

    Returns:
        The config.
    """
    sandbox = tmp_path_factory.mktemp("sandbox")
    (sandbox / "note.txt").write_text(
        "The launch code word is HYACINTH and the release date is 4 March.\n",
        encoding="utf-8",
    )
    return EmaliaConfig(
        account=bot_account,
        policy=Policy(
            allowed_senders=[_env("EMALIA_E2E_PEER_ADDRESS") or bot_account.address],
            sandbox_roots=[sandbox],
            enabled_toolsets=["email", "file_read"],
            require_token=run_tag,
            max_tool_calls=8,
            max_replies_per_hour=20,
        ),
        llm=llm_settings,
        instance_name="Emalia E2E",
        state_dir=tmp_path_factory.mktemp("state"),
        poll_interval=5.0,
        footer="",
    )
