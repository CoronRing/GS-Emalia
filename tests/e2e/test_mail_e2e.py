"""The mail toolkit against a real IMAP/SMTP server.

These are the tests the offline suite cannot write. Fakes agree with whatever
you assume about a protocol; a real server does not. Everything here is a
round trip: something is sent, it is waited for, and the thing that comes back
is inspected.

Run with `EMALIA_E2E=1` and the `EMALIA_E2E_*` credentials set. See
`docs/e2e-testing.md`.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from emalia.mail.client import MailClient
from emalia.mail.imap import SearchCriteria
from emalia.mail.models import EmailMessage

pytestmark = [pytest.mark.e2e, pytest.mark.network]


class TestConnection:
    def test_both_servers_accept_the_credentials(self, bot: MailClient) -> None:
        assert bot.check() == {"imap": "ok", "smtp": "ok"}

    def test_folders_include_the_inbox(self, bot: MailClient) -> None:
        folders = bot.list_folders()
        assert folders, "the account reported no mailboxes at all"
        assert any(name.upper().endswith("INBOX") for name in folders), folders

    def test_the_connection_is_reused_across_calls(self, bot: MailClient) -> None:
        # A fresh login per operation was the original's worst performance bug.
        # This asserts the session object survives, which is what avoids it.
        first = bot.imap
        bot.list_folders()
        bot.summaries(limit=1)
        assert bot.imap is first


class TestSendAndReceive:
    def test_round_trip_preserves_subject_and_body(
        self,
        bot: MailClient,
        subject: Callable[[str], str],
        wait_for_subject: Callable[..., EmailMessage],
    ) -> None:
        line = subject("round trip")
        body = "The quick brown fox. Accented: café, naïve, Größe. Symbols: — … ✓"

        bot.send(to=bot.account.address, subject=line, body=body)
        received = wait_for_subject(line)

        assert received.subject == line
        assert "café" in received.body
        assert "✓" in received.body
        assert received.sender is not None
        assert received.sender.address == bot.account.address

    def test_a_non_ascii_subject_survives_encoding(
        self,
        bot: MailClient,
        subject: Callable[[str], str],
        wait_for_subject: Callable[..., EmailMessage],
    ) -> None:
        # The header goes out RFC 2047 encoded and has to come back readable.
        # The original passed encoded words straight through to the user.
        line = subject("Grüße aus München — 日本語 ✉")
        bot.send(to=bot.account.address, subject=line, body="unicode subject test")

        received = wait_for_subject("Grüße aus München")
        assert received.subject == line
        assert "=?" not in received.subject

    def test_search_finds_the_message_by_subject(
        self,
        bot: MailClient,
        run_tag: str,
        subject: Callable[[str], str],
        wait_for_subject: Callable[..., EmailMessage],
    ) -> None:
        line = subject("searchable")
        bot.send(to=bot.account.address, subject=line, body="find me")
        wait_for_subject(line)

        bot.select("INBOX")
        uids = bot.search(SearchCriteria().subject(run_tag))
        assert uids, f"SEARCH SUBJECT {run_tag!r} matched nothing"
        assert all(run_tag in bot.fetch(uid).subject for uid in uids)

    def test_lookup_by_message_id(
        self,
        bot: MailClient,
        subject: Callable[[str], str],
        wait_for_subject: Callable[..., EmailMessage],
    ) -> None:
        line = subject("message id lookup")
        bot.send(to=bot.account.address, subject=line, body="by id")
        received = wait_for_subject(line)
        assert received.message_id

        found = bot.find_by_message_id(received.message_id)
        assert found.subject == line


class TestThreading:
    def test_a_reply_carries_the_threading_headers(
        self,
        bot: MailClient,
        subject: Callable[[str], str],
        wait_for_subject: Callable[..., EmailMessage],
    ) -> None:
        line = subject("threading")
        bot.send(to=bot.account.address, subject=line, body="original message")
        original = wait_for_subject(line)

        bot.reply(original, "This is the reply.")
        reply = wait_for_subject(f"Re: {line}")

        assert reply.in_reply_to == original.message_id
        assert original.message_id in reply.references
        assert reply.subject.startswith("Re: ")
        # Not "Re: Re: ...", which is what a naive implementation produces on
        # the second round.
        assert reply.subject.count("Re: ") == 1

    def test_the_quoted_original_is_stripped_from_the_reply_body(
        self,
        bot: MailClient,
        subject: Callable[[str], str],
        wait_for_subject: Callable[..., EmailMessage],
    ) -> None:
        line = subject("quote stripping")
        bot.send(to=bot.account.address, subject=line, body="First message in the thread.")
        original = wait_for_subject(line)

        bot.reply(original, "Only this sentence is new.")
        reply = wait_for_subject(f"Re: {line}")

        assert "Only this sentence is new." in reply.body
        assert "First message in the thread." not in reply.body

    def test_replying_sets_answered_on_the_original(
        self,
        bot: MailClient,
        subject: Callable[[str], str],
        wait_for_subject: Callable[..., EmailMessage],
    ) -> None:
        line = subject("answered flag")
        bot.send(to=bot.account.address, subject=line, body="flag me")
        original = wait_for_subject(line)

        bot.reply(original, "answered")
        wait_for_subject(f"Re: {line}")

        assert original.uid is not None
        refetched = bot.fetch(original.uid)
        assert "\\Answered" in refetched.flags


class TestAttachments:
    def test_a_file_survives_the_round_trip_byte_for_byte(
        self,
        bot: MailClient,
        tmp_path: Path,
        subject: Callable[[str], str],
        wait_for_subject: Callable[..., EmailMessage],
    ) -> None:
        payload = bytes(range(256)) * 40  # binary, not text, on purpose
        source = tmp_path / "payload.bin"
        source.write_bytes(payload)

        line = subject("attachment")
        bot.send(
            to=bot.account.address,
            subject=line,
            body="see attached",
            attachments=[str(source)],
        )
        received = wait_for_subject(line)

        assert [a.filename for a in received.attachments] == ["payload.bin"]

        destination = tmp_path / "saved"
        written = bot.save_attachments(received, destination)
        assert len(written) == 1
        assert written[0].read_bytes() == payload

    def test_a_directory_is_zipped_with_usable_paths(
        self,
        bot: MailClient,
        tmp_path: Path,
        subject: Callable[[str], str],
        wait_for_subject: Callable[..., EmailMessage],
    ) -> None:
        # The original built archive paths from the wrong variable, producing
        # zips whose members could not be extracted anywhere sensible.
        import zipfile

        folder = tmp_path / "bundle"
        (folder / "nested").mkdir(parents=True)
        (folder / "top.txt").write_text("top level", encoding="utf-8")
        (folder / "nested" / "inner.txt").write_text("nested", encoding="utf-8")

        line = subject("zipped directory")
        bot.send(
            to=bot.account.address,
            subject=line,
            body="see attached folder",
            attachments=[str(folder)],
        )
        received = wait_for_subject(line)

        assert len(received.attachments) == 1
        written = bot.save_attachments(received, tmp_path / "saved-zip")
        names = sorted(zipfile.ZipFile(written[0]).namelist())
        assert names == ["nested/inner.txt", "top.txt"]


class TestFlags:
    def test_read_unread_and_starred_round_trip(
        self,
        bot: MailClient,
        subject: Callable[[str], str],
        wait_for_subject: Callable[..., EmailMessage],
    ) -> None:
        line = subject("flags")
        bot.send(to=bot.account.address, subject=line, body="flag test")
        message = wait_for_subject(line)
        assert message.uid is not None
        uid = message.uid

        bot.mark_read(uid)
        assert "\\Seen" in bot.fetch(uid).flags

        # The original mapped "remove" onto +FLAGS, so this is the assertion
        # that would have caught it.
        bot.mark_unread(uid)
        assert "\\Seen" not in bot.fetch(uid).flags

        bot.flag(uid)
        assert "\\Flagged" in bot.fetch(uid).flags

    def test_fetching_does_not_mark_read_by_default(
        self,
        bot: MailClient,
        subject: Callable[[str], str],
        wait_for_subject: Callable[..., EmailMessage],
    ) -> None:
        line = subject("peek")
        bot.send(to=bot.account.address, subject=line, body="peek test")
        message = wait_for_subject(line)
        assert message.uid is not None

        bot.mark_unread(message.uid)
        bot.fetch(message.uid)
        assert "\\Seen" not in bot.fetch(message.uid).flags

    def test_unread_listing_reflects_the_flag(
        self,
        bot: MailClient,
        subject: Callable[[str], str],
        wait_for_subject: Callable[..., EmailMessage],
    ) -> None:
        line = subject("unread listing")
        bot.send(to=bot.account.address, subject=line, body="unread test")
        message = wait_for_subject(line)
        assert message.uid is not None

        bot.mark_unread(message.uid)
        assert any(line in m.subject for m in bot.unread(limit=25))

        bot.mark_read(message.uid)
        assert not any(line in m.subject for m in bot.unread(limit=25))


class TestDelete:
    def test_delete_removes_only_the_named_message(
        self,
        bot: MailClient,
        subject: Callable[[str], str],
        wait_for_subject: Callable[..., EmailMessage],
    ) -> None:
        keep_line = subject("delete: keep")
        drop_line = subject("delete: drop")
        bot.send(to=bot.account.address, subject=keep_line, body="keep me")
        bot.send(to=bot.account.address, subject=drop_line, body="delete me")
        wait_for_subject(keep_line)
        doomed = wait_for_subject(drop_line)
        assert doomed.uid is not None

        bot.delete(doomed.uid)

        bot.select("INBOX")
        remaining = [m.subject for m in bot.inbox(limit=25)]
        assert not any(drop_line in s for s in remaining)
        assert any(keep_line in s for s in remaining)
