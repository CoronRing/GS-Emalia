"""Composing outgoing mail: threading, attachments, and Bcc handling."""

from __future__ import annotations

import zipfile
from email.message import EmailMessage as MIMEMessage
from io import BytesIO
from pathlib import Path

import pytest

from emalia.errors import ComposeError
from emalia.mail.compose import (
    build_forward,
    build_message,
    build_reply,
    forward_subject,
    load_attachment,
    reply_subject,
    save_attachments,
)
from emalia.mail.models import Attachment, EmailAddress, EmailMessage
from emalia.mail.smtp import SmtpSender

SENDER = EmailAddress(address="bot@example.com", name="Emalia")


class TestSubjects:
    def test_reply_prefix_is_added_once(self) -> None:
        assert reply_subject("hello") == "Re: hello"
        assert reply_subject("Re: hello") == "Re: hello"
        assert reply_subject("RE: hello") == "RE: hello"

    def test_forward_prefix_is_added_once(self) -> None:
        assert forward_subject("hello") == "Fwd: hello"
        assert forward_subject("Fwd: hello") == "Fwd: hello"
        assert forward_subject("Fw: hello") == "Fw: hello"

    def test_empty_subject_still_gets_a_prefix(self) -> None:
        assert reply_subject("") == "Re:"


class TestBuildMessage:
    def test_sets_the_expected_headers(self) -> None:
        message = build_message(
            sender=SENDER,
            to=["alice@example.com"],
            subject="hi",
            body="body text",
        )
        assert "bot@example.com" in message["From"]
        assert message["To"] == "alice@example.com"
        assert message["Subject"] == "hi"
        assert message["Message-ID"]
        assert message["Date"]

    def test_footer_is_appended_to_the_body(self) -> None:
        message = build_message(
            sender=SENDER,
            to=["alice@example.com"],
            subject="hi",
            body="body text",
            footer="-- \nsent by a robot",
        )
        assert "sent by a robot" in message.get_content()

    def test_no_recipients_is_an_error(self) -> None:
        with pytest.raises(ComposeError, match="at least one recipient"):
            build_message(sender=SENDER, to=[], subject="hi", body="x")

    def test_attachment_is_carried(self, tmp_path: Path) -> None:
        target = tmp_path / "note.txt"
        target.write_text("contents", encoding="utf-8")
        message = build_message(
            sender=SENDER,
            to=["alice@example.com"],
            subject="hi",
            body="see attached",
            attachments=[target],
        )
        names = [p.get_filename() for p in message.iter_attachments()]
        assert "note.txt" in names

    def test_oversized_attachment_is_refused(self, tmp_path: Path) -> None:
        target = tmp_path / "big.bin"
        target.write_bytes(b"x" * 1024)
        with pytest.raises(ComposeError, match="over the"):
            build_message(
                sender=SENDER,
                to=["alice@example.com"],
                subject="hi",
                body="x",
                attachments=[target],
                max_attachment_bytes=100,
            )

    def test_combined_attachment_limit_is_enforced(self, tmp_path: Path) -> None:
        for name in ("a.bin", "b.bin"):
            (tmp_path / name).write_bytes(b"x" * 400)
        with pytest.raises(ComposeError, match="Attachments total"):
            build_message(
                sender=SENDER,
                to=["alice@example.com"],
                subject="hi",
                body="x",
                attachments=[tmp_path / "a.bin", tmp_path / "b.bin"],
                max_attachment_bytes=1000,
                max_total_attachment_bytes=500,
            )


class TestDirectoryAttachments:
    def test_directory_is_zipped_with_relative_paths(self, tmp_path: Path) -> None:
        # The original zipped with paths relative to the loop's `dirs` list,
        # which produced unusable archive entries.
        source = tmp_path / "project"
        (source / "nested").mkdir(parents=True)
        (source / "top.txt").write_text("top", encoding="utf-8")
        (source / "nested" / "inner.txt").write_text("inner", encoding="utf-8")

        attachment = load_attachment(source)
        assert attachment.filename == "project.zip"
        with zipfile.ZipFile(BytesIO(attachment.data)) as archive:
            assert sorted(archive.namelist()) == ["nested/inner.txt", "top.txt"]

    def test_missing_path_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(ComposeError, match="not found"):
            load_attachment(tmp_path / "nope")


class TestReplyThreading:
    def _original(self) -> EmailMessage:
        return EmailMessage(
            uid="12",
            message_id="<original@example.com>",
            subject="question",
            sender=EmailAddress(address="alice@example.com"),
            to=[EmailAddress(address="bot@example.com")],
            references=["<older@example.com>"],
            text="please help",
        )

    def test_reply_carries_threading_headers(self) -> None:
        reply = build_reply(self._original(), sender=SENDER, body="here you go")
        assert reply["In-Reply-To"] == "<original@example.com>"
        assert reply["References"] == "<older@example.com> <original@example.com>"
        assert reply["Subject"] == "Re: question"
        assert reply["To"] == "alice@example.com"

    def test_reply_marks_itself_auto_replied(self) -> None:
        # Well-behaved autoresponders skip these, which is half of not getting
        # into a loop with another robot.
        reply = build_reply(self._original(), sender=SENDER, body="x")
        assert reply["Auto-Submitted"] == "auto-replied"

    def test_reply_all_copies_the_others_but_not_us(self) -> None:
        original = self._original()
        original.cc = [EmailAddress(address="carol@example.com")]
        original.to.append(EmailAddress(address="dave@example.com"))
        reply = build_reply(original, sender=SENDER, body="x", reply_all=True)
        assert "carol@example.com" in reply["To"]
        assert "dave@example.com" in reply["To"]
        assert reply["To"].count("bot@example.com") == 0

    def test_explicit_recipients_override_everything(self) -> None:
        reply = build_reply(
            self._original(),
            sender=SENDER,
            body="x",
            to=["vetted@example.com"],
        )
        assert reply["To"] == "vetted@example.com"

    def test_no_reply_address_is_an_error(self) -> None:
        orphan = EmailMessage(subject="x", message_id="<a@b>")
        with pytest.raises(ComposeError, match="no usable reply address"):
            build_reply(orphan, sender=SENDER, body="x")


class TestForward:
    def test_forward_quotes_the_original(self) -> None:
        original = EmailMessage(
            message_id="<a@b>",
            subject="report",
            sender=EmailAddress(address="alice@example.com"),
            text="the body",
        )
        forwarded = build_forward(original, sender=SENDER, to=["bob@example.com"], note="fyi")
        content = forwarded.get_content()
        assert "fyi" in content
        assert "Forwarded message" in content
        assert "the body" in content
        assert forwarded["Subject"] == "Fwd: report"


class TestBccPrivacy:
    def test_bcc_is_stripped_before_transmission(self) -> None:
        # Sending the header verbatim would disclose blind recipients to
        # everyone else on the message.
        message = build_message(
            sender=SENDER,
            to=["alice@example.com"],
            subject="hi",
            body="x",
            bcc=["secret@example.com"],
        )
        recipients = SmtpSender.envelope_recipients(message)
        assert "secret@example.com" in [r.lower() for r in recipients]

        del message["Bcc"]
        assert "Bcc" not in message

    def test_envelope_recipients_deduplicate(self) -> None:
        message = MIMEMessage()
        message["To"] = "alice@example.com, Alice@Example.com"
        message["Cc"] = "bob@example.com"
        assert len(SmtpSender.envelope_recipients(message)) == 2


class TestSaveAttachments:
    def test_writes_files_and_sanitises_names(self, tmp_path: Path) -> None:
        message = EmailMessage(
            attachments=[
                Attachment(
                    filename="../../escape.txt",
                    content_type="text/plain",
                    data=b"payload",
                )
            ]
        )
        written = save_attachments(message, tmp_path)
        assert len(written) == 1
        # The traversal must not have escaped the destination directory.
        assert written[0].parent == tmp_path.resolve()
        assert written[0].read_bytes() == b"payload"

    def test_existing_files_are_not_clobbered(self, tmp_path: Path) -> None:
        message = EmailMessage(
            attachments=[Attachment(filename="a.txt", content_type="text/plain", data=b"one")]
        )
        first = save_attachments(message, tmp_path)[0]
        second = save_attachments(message, tmp_path)[0]
        assert first != second
        assert first.exists() and second.exists()

    def test_inline_parts_are_skipped_by_default(self, tmp_path: Path) -> None:
        message = EmailMessage(
            attachments=[
                Attachment(
                    filename="logo.png",
                    content_type="image/png",
                    data=b"\x89PNG",
                    inline=True,
                )
            ]
        )
        assert save_attachments(message, tmp_path) == []
        assert len(save_attachments(message, tmp_path, include_inline=True)) == 1
