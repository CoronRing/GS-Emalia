"""The full round trip: a person sends mail, Emalia answers it.

This is the only test that exercises everything at once — delivery, polling,
the gate, the agent, the tools, the reply, the threading and the audit log —
so it is also the one that needs a second mailbox. The listener drops mail from
its own address, which is a loop guard worth keeping rather than working
around, so the sender has to be somebody else.

Requires `EMALIA_E2E_PEER_ADDRESS` and `EMALIA_E2E_PEER_PASSWORD` on top of the
usual credentials.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from emalia.config import EmaliaConfig
from emalia.mail.client import MailClient
from emalia.mail.models import EmailMessage
from emalia.runtime.listener import EmaliaListener

pytestmark = [pytest.mark.e2e, pytest.mark.network, pytest.mark.llm]


class TestFullRoundTrip:
    async def test_a_question_by_email_comes_back_answered(
        self,
        agent_config: EmaliaConfig,
        bot: MailClient,
        peer: MailClient,
        run_tag: str,
        subject: Callable[[str], str],
        wait_for_subject: Callable[..., EmailMessage],
    ) -> None:
        line = subject("round trip: code word")
        peer.send(
            to=bot.account.address,
            subject=line,
            body=(
                "Hello. There is a file called note.txt in the folder you can read. "
                "Please tell me the launch code word it mentions."
            ),
        )
        arrived = wait_for_subject(line, sender=peer.account.address)
        assert arrived.uid is not None

        listener = EmaliaListener(agent_config, client=bot)
        stats = await listener.arun_once()

        assert stats.handled == 1, stats.as_dict()
        assert stats.failed == 0, stats.as_dict()

        if peer.check()["imap"] != "ok":
            pytest.skip("the peer mailbox is send-only, so the reply cannot be verified there")

        reply = _wait_in(peer, f"Re: {line}")
        assert "HYACINTH" in reply.body.upper(), reply.body
        assert reply.in_reply_to == arrived.message_id
        # Outgoing replies say they are automated so the other end's
        # out-of-office does not answer back.
        assert reply.headers.get("auto-submitted", "").startswith("auto")

    async def test_the_message_is_marked_read_and_recorded(
        self,
        agent_config: EmaliaConfig,
        bot: MailClient,
        peer: MailClient,
        subject: Callable[[str], str],
        wait_for_subject: Callable[..., EmailMessage],
    ) -> None:
        line = subject("round trip: bookkeeping")
        peer.send(to=bot.account.address, subject=line, body="Say hello back, briefly.")
        arrived = wait_for_subject(line, sender=peer.account.address)
        assert arrived.uid is not None

        listener = EmaliaListener(agent_config, client=bot)
        await listener.arun_once()

        assert "\\Seen" in bot.fetch(arrived.uid).flags

        records = [
            json.loads(entry)
            for entry in agent_config.audit_path.read_text(encoding="utf-8").splitlines()
            if entry.strip()
        ]
        answered = [r for r in records if r.get("message_id") == arrived.message_id]
        assert answered, records
        assert answered[-1]["outcome"] in ("answered", "sent")

    async def test_the_same_message_is_not_answered_twice(
        self,
        agent_config: EmaliaConfig,
        bot: MailClient,
        peer: MailClient,
        subject: Callable[[str], str],
        wait_for_subject: Callable[..., EmailMessage],
    ) -> None:
        line = subject("round trip: dedupe")
        peer.send(to=bot.account.address, subject=line, body="Reply with one word: acknowledged.")
        arrived = wait_for_subject(line, sender=peer.account.address)
        assert arrived.uid is not None

        listener = EmaliaListener(agent_config, client=bot)
        first = await listener.arun_once()
        assert first.handled == 1, first.as_dict()

        # Put it back in the unread state the poller looks for. The Message-ID
        # is what stops a second answer, not the flag.
        bot.mark_unread(arrived.uid)
        second = await listener.arun_once()

        assert second.handled == 0, second.as_dict()
        assert second.ignored >= 1, second.as_dict()


class TestTheGateHoldsInProduction:
    async def test_mail_without_the_token_is_ignored(
        self,
        agent_config: EmaliaConfig,
        bot: MailClient,
        peer: MailClient,
        run_tag: str,
        wait_for_subject: Callable[..., EmailMessage],
    ) -> None:
        # An allowlisted sender, but no token in the subject. This is also what
        # protects the mailbox during a test run: real unread mail cannot be
        # answered by accident.
        line = f"untagged probe {run_tag[-6:]}"
        peer.send(to=bot.account.address, subject=line, body="Please reply to this.")
        wait_for_subject(line, sender=peer.account.address)

        listener = EmaliaListener(agent_config, client=bot)
        stats = await listener.arun_once()

        assert stats.handled == 0, stats.as_dict()

    async def test_dry_run_answers_nothing(
        self,
        agent_config: EmaliaConfig,
        bot: MailClient,
        peer: MailClient,
        subject: Callable[[str], str],
        wait_for_subject: Callable[..., EmailMessage],
    ) -> None:
        agent_config.dry_run = True
        line = subject("round trip: dry run")
        peer.send(to=bot.account.address, subject=line, body="Say something short.")
        arrived = wait_for_subject(line, sender=peer.account.address)
        assert arrived.uid is not None

        listener = EmaliaListener(agent_config, client=bot)
        stats = await listener.arun_once()

        assert stats.handled == 1, stats.as_dict()
        records = agent_config.audit_path.read_text(encoding="utf-8")
        assert "dry_run" in records

        if peer.check()["imap"] == "ok":
            peer.select("INBOX")
            subjects = [m.subject for m in peer.inbox(limit=15)]
            assert not any(s.startswith(f"Re: {line}") for s in subjects), subjects


def _wait_in(client: MailClient, fragment: str, *, timeout: float = 180.0) -> EmailMessage:
    """Poll another mailbox for a message whose subject contains `fragment`."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        client.select("INBOX")
        for message in client.inbox(limit=15):
            if fragment in (message.subject or ""):
                return message
        time.sleep(5.0)
    pytest.fail(f"No message containing {fragment!r} reached {client.account.address}")
