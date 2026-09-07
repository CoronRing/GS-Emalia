"""Modified UTF-7 mailbox names, and the IMAP plumbing that applies them."""

from __future__ import annotations

import pytest

from emalia.mail.folders import decode_folder, encode_folder, quote_folder
from emalia.mail.imap import ImapSession

# (plain, wire). The fourth is RFC 3501's own worked example, which is the one
# case that exercises a multi-character run and the ',' for '/' substitution
# together.
ROUNDTRIP: list[tuple[str, str]] = [
    ("INBOX", "INBOX"),
    ("[Gmail]/All Mail", "[Gmail]/All Mail"),
    ("Sent Items", "Sent Items"),
    ("~peter/mail/台北/日本語", "~peter/mail/&U,BTFw-/&ZeVnLIqe-"),
    ("Wysłane", "Wys&AUI-ane"),
    ("Gelöscht", "Gel&APY-scht"),
    ("Entwürfe", "Entw&APw-rfe"),
    ("已发送", "&XfJT0ZAB-"),
    ("Αρχείο", "&A5EDwQPHA7UDrwO,-"),
]


@pytest.mark.parametrize(("plain", "wire"), ROUNDTRIP)
def test_encode_matches_the_wire_form(plain: str, wire: str) -> None:
    assert encode_folder(plain) == wire


@pytest.mark.parametrize(("plain", "wire"), ROUNDTRIP)
def test_decode_matches_the_plain_form(plain: str, wire: str) -> None:
    assert decode_folder(wire) == plain


@pytest.mark.parametrize(("plain", "_wire"), ROUNDTRIP)
def test_roundtrip(plain: str, _wire: str) -> None:
    assert decode_folder(encode_folder(plain)) == plain


def test_ascii_names_pass_through_untouched() -> None:
    """The encoder is safe to apply unconditionally, which is why it is."""
    for name in ("INBOX", "Drafts", "a/b/c", "[Gmail]/Sent Mail", "x" * 200):
        assert encode_folder(name) == name


def test_ampersand_is_escaped_not_encoded() -> None:
    """'&' is the shift character, so a literal one becomes '&-'."""
    assert encode_folder("R&D") == "R&-D"
    assert encode_folder("A&B&C") == "A&-B&-C"
    assert decode_folder("R&-D") == "R&D"
    assert decode_folder("A&-B&-C") == "A&B&C"


def test_ampersand_next_to_encoded_text() -> None:
    mixed = "R&D/研究"
    assert decode_folder(encode_folder(mixed)) == mixed


def test_adjacent_runs_share_one_shift() -> None:
    """Two non-ASCII characters in a row are one BASE64 run, not two."""
    assert encode_folder("äö") == "&AOQA9g-"


@pytest.mark.parametrize(
    "malformed",
    [
        "&AUI",  # no closing '-'
        "&$$$-",  # not BASE64
        "&AU-",  # odd byte count, so not whole UTF-16 code units
    ],
)
def test_malformed_input_raises(malformed: str) -> None:
    with pytest.raises(ValueError):
        decode_folder(malformed)


def test_quote_folder_encodes_and_quotes() -> None:
    assert quote_folder("INBOX") == '"INBOX"'
    assert quote_folder("[Gmail]/All Mail") == '"[Gmail]/All Mail"'
    assert quote_folder("Wysłane") == '"Wys&AUI-ane"'


def test_quote_folder_escapes_quotes_and_backslashes() -> None:
    """Both are IMAP quoted-string metacharacters and legal in a name."""
    assert quote_folder('a"b') == '"a\\"b"'
    assert quote_folder("a\\b") == '"a\\\\b"'


class _FakeSession:
    """Records the arguments an `ImapSession` would put on the wire."""

    # `list_folders` reaches for this through `self`, so the fake borrows the
    # real one rather than reimplementing the parser it is meant to exercise.
    _folder_name = staticmethod(ImapSession._folder_name)

    def __init__(self, response: list[object] | None = None) -> None:
        self.calls: list[tuple[object, ...]] = []
        self._response = response or []
        self.folder = "INBOX"
        self._readonly = False

    def _command(self, name: str, *args: object) -> tuple[str, list[object]]:
        self.calls.append((name, *args))
        return "OK", self._response


def test_select_encodes_the_folder_but_keeps_the_plain_name() -> None:
    session = _FakeSession()
    ImapSession.select(session, "Wysłane")  # type: ignore[arg-type]

    assert session.calls == [("select", '"Wys&AUI-ane"', False)]
    # The attribute is what error messages and a reconnect use, so it stays
    # readable rather than holding the wire form.
    assert session.folder == "Wysłane"


def test_move_encodes_the_destination() -> None:
    session = _FakeSession()
    session.store_flags = lambda *a, **k: None  # type: ignore[attr-defined]
    ImapSession.move(session, ["7"], "Gelöscht")  # type: ignore[arg-type]

    assert ("uid", "COPY", "7", '"Gel&APY-scht"') in session.calls


def test_list_folders_decodes_names() -> None:
    session = _FakeSession(
        [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren) "/" "Wys&AUI-ane"',
            b'(\\HasNoChildren) "/" "[Gmail]/All Mail"',
        ]
    )
    assert ImapSession.list_folders(session) == [  # type: ignore[arg-type]
        "INBOX",
        "Wysłane",
        "[Gmail]/All Mail",
    ]


def test_list_folders_reads_unquoted_names() -> None:
    session = _FakeSession([b'(\\HasNoChildren) "/" INBOX'])
    assert ImapSession.list_folders(session) == ["INBOX"]  # type: ignore[arg-type]


def test_list_folders_reads_literals() -> None:
    """A non-ASCII name is exactly what a server sends as a literal."""
    session = _FakeSession(
        [
            (b'(\\HasNoChildren) "/" {10}', b"Wys&AUI-ane"),
            b"",
        ]
    )
    assert ImapSession.list_folders(session) == ["Wysłane"]  # type: ignore[arg-type]


def test_list_folders_keeps_an_undecodable_name_rather_than_dropping_it() -> None:
    """One bad entry must not silently shrink the listing."""
    session = _FakeSession(
        [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren) "/" "&broken"',
        ]
    )
    assert ImapSession.list_folders(session) == ["INBOX", "&broken"]  # type: ignore[arg-type]


def test_list_folders_skips_entries_that_are_not_mailbox_lines() -> None:
    session = _FakeSession([b'(\\HasNoChildren) "/" "INBOX"', b"", None, b"garbage"])
    assert ImapSession.list_folders(session) == ["INBOX"]  # type: ignore[arg-type]


def test_list_folders_handles_a_nil_delimiter() -> None:
    """Servers send NIL for a flat namespace."""
    session = _FakeSession([b'(\\Noselect) NIL "INBOX"'])
    assert ImapSession.list_folders(session) == ["INBOX"]  # type: ignore[arg-type]
