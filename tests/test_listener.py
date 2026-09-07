"""The listener: gating, deduplication, replies, and failure handling.

The agent is stubbed out. These tests are about the loop's decisions, not about
what a model would say.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from emalia.config import EmaliaConfig, LLMSettings
from emalia.mail.accounts import MailAccount
from emalia.mail.models import EmailAddress, EmailMessage
from emalia.runtime.listener import EmaliaListener
from emalia.runtime.state import AuditLog, SeenStore
from emalia.security.policy import Policy
from fakes import FakeMailClient


class StubFlow:
    """Stands in for `rt.Flow`, recording prompts and returning a fixed reply."""

    def __init__(self, reply: str = "Done.", error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.prompts: list[str] = []

    def connect(self) -> StubFlow:
        return self

    async def ainvoke(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        return self.reply


def make_config(tmp_path: Path, **policy_kwargs: object) -> EmaliaConfig:
    """A config whose state lives under `tmp_path`."""
    defaults: dict[str, object] = {
        "allowed_senders": ["alice@example.com"],
        "enabled_toolsets": ["email"],
        "sandbox_roots": [tmp_path / "files"],
    }
    defaults.update(policy_kwargs)
    (tmp_path / "files").mkdir(exist_ok=True)
    return EmaliaConfig(
        account=MailAccount.for_provider("gmail", "bot@example.com", "pw"),
        policy=Policy(**defaults),  # type: ignore[arg-type]
        llm=LLMSettings(),
        state_dir=tmp_path / "state",
        poll_interval=1.0,
    )


def make_listener(
    config: EmaliaConfig,
    mailbox: FakeMailClient,
    flow: StubFlow,
    monkeypatch: pytest.MonkeyPatch,
) -> EmaliaListener:
    """Build a listener with the agent replaced by `flow`."""
    monkeypatch.setattr("emalia.runtime.listener.build_agent", lambda *a, **k: object())
    monkeypatch.setattr("emalia.runtime.listener.rt.Flow", lambda **k: flow)
    return EmaliaListener(config, client=mailbox)


def message(
    *,
    uid: str = "1",
    sender: str = "alice@example.com",
    subject: str = "hello",
    message_id: str = "<m1@example.com>",
    body: str = "list my notes",
    headers: dict[str, str] | None = None,
) -> EmailMessage:
    return EmailMessage(
        uid=uid,
        message_id=message_id,
        subject=subject,
        sender=EmailAddress(address=sender),
        to=[EmailAddress(address="bot@example.com")],
        text=body,
        headers=headers or {},
    )


class TestGating:
    async def test_allowlisted_sender_is_answered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mailbox = FakeMailClient([message()])
        flow = StubFlow("Here are your notes.")
        listener = make_listener(make_config(tmp_path), mailbox, flow, monkeypatch)

        stats = await listener.arun_once()

        assert stats.handled == 1
        assert len(mailbox.sent) == 1
        assert mailbox.sent[0]["body"] == "Here are your notes."
        assert mailbox.sent[0]["to"] == ["alice@example.com"]

    async def test_unlisted_sender_gets_no_reply_at_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Answering a spoofed sender would let the mailbox be used to mail
        # strangers on the operator's behalf, so silence is the correct output.
        mailbox = FakeMailClient([message(sender="mallory@evil.example")])
        listener = make_listener(make_config(tmp_path), mailbox, StubFlow(), monkeypatch)

        stats = await listener.arun_once()

        assert stats.ignored == 1
        assert stats.handled == 0
        assert mailbox.sent == []
        # It is still marked read, so it is not reconsidered on every poll.
        assert "\\Seen" in mailbox.flags["1"]

    async def test_mail_from_the_account_itself_is_dropped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mailbox = FakeMailClient([message(sender="bot@example.com")])
        config = make_config(tmp_path, allowed_senders=["*@example.com"])
        listener = make_listener(config, mailbox, StubFlow(), monkeypatch)

        stats = await listener.arun_once()

        assert stats.ignored == 1
        assert mailbox.sent == []

    @pytest.mark.parametrize(
        "headers",
        [
            {"auto-submitted": "auto-replied"},
            {"precedence": "bulk"},
            {"list-id": "<announce.example.com>"},
            {"x-autoreply": "yes"},
        ],
    )
    async def test_autoresponders_and_lists_are_dropped(
        self,
        headers: dict[str, str],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Answering these is how two robots mail each other until someone
        # notices the bill.
        mailbox = FakeMailClient([message(headers=headers)])
        listener = make_listener(make_config(tmp_path), mailbox, StubFlow(), monkeypatch)

        stats = await listener.arun_once()

        assert stats.ignored == 1
        assert mailbox.sent == []

    async def test_required_token_is_enforced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = make_config(tmp_path, require_token="OPEN-SESAME")
        mailbox = FakeMailClient([message(subject="do a thing")])
        listener = make_listener(config, mailbox, StubFlow(), monkeypatch)
        assert (await listener.arun_once()).ignored == 1

        mailbox2 = FakeMailClient(
            [message(subject="do a thing OPEN-SESAME", message_id="<m2@example.com>")]
        )
        listener2 = make_listener(config, mailbox2, StubFlow(), monkeypatch)
        assert (await listener2.arun_once()).handled == 1


class TestDeduplication:
    async def test_the_same_message_id_is_only_answered_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = make_config(tmp_path)
        first = FakeMailClient([message()])
        assert (
            await make_listener(config, first, StubFlow(), monkeypatch).arun_once()
        ).handled == 1

        # A restart that finds the same message still unread must not re-answer.
        second = FakeMailClient([message()])
        stats = await make_listener(config, second, StubFlow(), monkeypatch).arun_once()

        assert stats.handled == 0
        assert stats.ignored == 1
        assert second.sent == []

    async def test_the_seen_set_survives_a_restart(self, tmp_path: Path) -> None:
        store = SeenStore(tmp_path / "seen.json")
        store.add("<a@b>")
        assert "<a@b>" in SeenStore(tmp_path / "seen.json")

    def test_the_seen_set_is_bounded(self, tmp_path: Path) -> None:
        store = SeenStore(tmp_path / "seen.json", capacity=3)
        for index in range(5):
            store.add(f"<{index}@x>")
        assert len(store) == 3
        assert "<0@x>" not in store
        assert "<4@x>" in store

    def test_a_corrupt_seen_file_starts_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "seen.json"
        path.write_text("{ not json", encoding="utf-8")
        assert len(SeenStore(path)) == 0


class TestPromptConstruction:
    async def test_the_body_is_wrapped_in_untrusted_delimiters(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from emalia.agent import UNTRUSTED_CLOSE, UNTRUSTED_OPEN

        mailbox = FakeMailClient([message(body="please read notes.txt")])
        flow = StubFlow()
        await make_listener(make_config(tmp_path), mailbox, flow, monkeypatch).arun_once()

        prompt = flow.prompts[0]
        assert UNTRUSTED_OPEN in prompt
        assert UNTRUSTED_CLOSE in prompt
        # The sender-controlled text sits inside the delimiters; the metadata
        # the runtime vouched for sits outside them.
        body_section = prompt.split(UNTRUSTED_OPEN)[1]
        assert "please read notes.txt" in body_section
        assert "From: alice@example.com" in prompt.split(UNTRUSTED_OPEN)[0]


class TestFailureHandling:
    async def test_a_failing_agent_gets_a_short_notice_not_a_traceback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mailbox = FakeMailClient([message()])
        flow = StubFlow(error=RuntimeError("the model exploded at /srv/secret/path.py"))
        listener = make_listener(make_config(tmp_path), mailbox, flow, monkeypatch)

        stats = await listener.arun_once()

        assert stats.failed == 1
        assert len(mailbox.sent) == 1
        body = mailbox.sent[0]["body"]
        assert "Something went wrong" in body
        assert "/srv/secret/path.py" not in body
        assert "Traceback" not in body

    async def test_a_dropped_connection_does_not_end_the_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mailbox = FakeMailClient([message()])
        mailbox.fail_next_poll = True
        listener = make_listener(make_config(tmp_path), mailbox, StubFlow(), monkeypatch)

        stats = await listener.arun_once()

        assert stats.considered == 0
        assert stats.failed == 0


class TestDryRun:
    async def test_dry_run_sends_nothing_but_still_marks_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = make_config(tmp_path)
        config.dry_run = True
        mailbox = FakeMailClient([message()])
        listener = make_listener(config, mailbox, StubFlow(), monkeypatch)

        stats = await listener.arun_once()

        assert stats.handled == 1
        assert mailbox.sent == []
        assert "\\Seen" in mailbox.flags["1"]


class TestAuditLog:
    async def test_handled_and_ignored_are_both_recorded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mailbox = FakeMailClient(
            [
                message(uid="1", message_id="<m1@x>"),
                message(uid="2", message_id="<m2@x>", sender="mallory@evil.example"),
            ]
        )
        config = make_config(tmp_path)
        listener = make_listener(config, mailbox, StubFlow(), monkeypatch)
        await listener.arun_once()

        events = [r["event"] for r in AuditLog(config.audit_path).tail(20)]
        assert "handled" in events
        assert "ignored" in events

    def test_tail_of_a_missing_log_is_empty(self, tmp_path: Path) -> None:
        assert AuditLog(tmp_path / "nope.jsonl").tail() == []


class TestToolBudgetReset:
    async def test_the_tool_budget_resets_between_messages(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Without the reset a long-running listener would eventually refuse
        # every tool call it was ever asked to make.
        config = make_config(tmp_path)
        config.policy.charge_tool_call()
        mailbox = FakeMailClient([message()])
        listener = make_listener(config, mailbox, StubFlow(), monkeypatch)

        await listener.arun_once()

        assert config.policy.tool_calls_used == 0
