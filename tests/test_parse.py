"""Parsing: headers, charsets, HTML, and quoted-reply stripping."""

from __future__ import annotations

from emalia.mail.models import EmailAddress, parse_address_list
from emalia.mail.parse import (
    decode_header_value,
    html_to_text,
    parse_message,
    strip_quoted_reply,
)
from helpers import make_raw


class TestAddressParsing:
    def test_parses_display_name_and_address(self) -> None:
        address = EmailAddress.parse("Alice Smith <Alice@Example.COM>")
        assert address.address == "alice@example.com"
        assert address.name == "Alice Smith"
        assert address.domain == "example.com"

    def test_parses_bare_address(self) -> None:
        assert EmailAddress.parse("bob@example.com").name is None

    def test_comma_inside_quoted_name_is_not_a_separator(self) -> None:
        addresses = parse_address_list('"Smith, Alice" <a@x.com>, bob@y.com')
        assert [a.address for a in addresses] == ["a@x.com", "bob@y.com"]
        assert addresses[0].name == "Smith, Alice"

    def test_empty_header_gives_no_addresses(self) -> None:
        assert parse_address_list(None) == []
        assert parse_address_list("  ") == []


class TestHeaderDecoding:
    def test_decodes_rfc2047_encoded_word(self) -> None:
        # The original passed these through raw, so non-ASCII subjects reached
        # the model as mojibake.
        assert decode_header_value("=?utf-8?B?SGVsbG8gV29ybGQ=?=") == "Hello World"

    def test_plain_header_is_unchanged(self) -> None:
        assert decode_header_value("Just a subject") == "Just a subject"

    def test_missing_header_is_empty(self) -> None:
        assert decode_header_value(None) == ""


class TestQuoteStripping:
    def test_removes_gmail_attribution_and_everything_after(self) -> None:
        body = (
            "Thanks, that worked.\n\n"
            "On Mon, 3 Jun 2026 at 09:12, Emalia <bot@example.com> wrote:\n"
            "> I have listed your notes.\n"
            "> There were four.\n"
        )
        assert strip_quoted_reply(body) == "Thanks, that worked."

    def test_removes_outlook_separator(self) -> None:
        body = "Please retry.\n\n-----Original Message-----\nFrom: bot\nOld content"
        assert strip_quoted_reply(body) == "Please retry."

    def test_removes_signature_when_asked(self) -> None:
        body = "Do the thing.\n\n-- \nAlice\nCTO"
        assert strip_quoted_reply(body) == "Do the thing."
        assert "CTO" in strip_quoted_reply(body, strip_signature=False)

    def test_keeps_original_when_stripping_leaves_nothing(self) -> None:
        # A reply with no new text should not become an empty prompt.
        body = "On Mon, 3 Jun 2026, Emalia wrote:\n> everything"
        assert strip_quoted_reply(body) == body.strip()

    def test_empty_input_is_empty(self) -> None:
        assert strip_quoted_reply("") == ""


class TestHtmlToText:
    def test_extracts_visible_text(self) -> None:
        assert "Hello there" in html_to_text("<html><body><p>Hello there</p></body></html>")

    def test_drops_script_and_style(self) -> None:
        text = html_to_text("<style>p{color:red}</style><script>alert(1)</script><p>Body</p>")
        assert "color" not in text
        assert "alert" not in text
        assert "Body" in text

    def test_drops_gmail_quote_container(self) -> None:
        html = '<div>New text</div><div class="gmail_quote">Old thread</div>'
        text = html_to_text(html)
        assert "New text" in text
        assert "Old thread" not in text

    def test_malformed_html_degrades_rather_than_raising(self) -> None:
        assert "text" in html_to_text("<div><p>text").lower()


class TestParseMessage:
    def test_parses_headers_and_body(self) -> None:
        message = parse_message(make_raw(), uid="7")
        assert message.uid == "7"
        assert message.message_id == "<msg-1@example.com>"
        assert message.subject == "hello"
        assert message.sender is not None
        assert message.sender.address == "alice@example.com"
        assert "list my notes" in message.body
        assert message.date is not None

    def test_parses_attachments(self) -> None:
        raw = make_raw(attachments=[("report.pdf", b"%PDF-1.4 fake", "application/pdf")])
        message = parse_message(raw)
        assert len(message.attachments) == 1
        attachment = message.attachments[0]
        assert attachment.filename == "report.pdf"
        assert attachment.content_type == "application/pdf"
        assert attachment.size == len(b"%PDF-1.4 fake")

    def test_falls_back_to_html_when_no_plain_part(self) -> None:
        raw = make_raw(body="", html="<p>Only HTML here</p>")
        message = parse_message(raw)
        assert "Only HTML here" in message.body

    def test_strips_quotes_by_default(self) -> None:
        body = "New question.\n\nOn Mon, 3 Jun 2026, bot wrote:\n> old"
        assert "old" not in parse_message(make_raw(body=body)).text
        assert "old" in parse_message(make_raw(body=body), strip_quotes=False).text

    def test_reply_target_prefers_reply_to(self) -> None:
        raw = make_raw(extra_headers={"Reply-To": "tickets@example.com"})
        target = parse_message(raw).reply_target
        assert target is not None
        assert target.address == "tickets@example.com"

    def test_summary_is_a_single_readable_line(self) -> None:
        summary = parse_message(make_raw(), uid="7").summary()
        line = summary.to_line()
        assert "uid=7" in line
        assert "alice@example.com" in line
        assert "hello" in line
