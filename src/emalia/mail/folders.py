"""Modified UTF-7 encoding for IMAP mailbox names.

IMAP predates the decision that protocols should just carry UTF-8. RFC 3501
§5.1.3 instead defines a variant of UTF-7 for mailbox names: printable ASCII
stands for itself, everything else is BASE64-encoded UTF-16BE shifted in with
``&`` and out with ``-``. The BASE64 alphabet is modified too, using ``,``
instead of ``/`` so that the result survives the ``/`` hierarchy delimiter, and
dropping ``=`` padding.

The practical consequence is that a mailbox any non-English speaker is likely
to have — ``Wysłane``, ``已发送``, ``Gelöscht`` — is not selectable by its
literal name. Python ships a ``utf-7`` codec, but it is the unmodified RFC 2152
one and produces ``+`` where IMAP requires ``&``, so it cannot be used here.

Servers that advertise the ``UTF8=ACCEPT`` capability accept raw UTF-8, but
enough do not that encoding unconditionally is the portable choice: an
already-ASCII name passes through these functions unchanged.
"""

from __future__ import annotations

import base64
import binascii

__all__ = ["decode_folder", "encode_folder", "quote_folder"]

# Printable ASCII, the range RFC 3501 lets stand for itself.
_PRINTABLE_MIN = 0x20
_PRINTABLE_MAX = 0x7E


def _encode_run(run: str) -> str:
    """BASE64-encode one run of non-ASCII characters, shift markers included."""
    encoded = base64.b64encode(run.encode("utf-16-be")).decode("ascii")
    return "&" + encoded.rstrip("=").replace("/", ",") + "-"


def _decode_run(run: str) -> str:
    """Decode the BASE64 payload between an ``&`` and its closing ``-``."""
    payload = run.replace(",", "/")
    payload += "=" * (-len(payload) % 4)
    try:
        raw = base64.b64decode(payload, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError(f"Not valid modified BASE64: &{run}-") from exc
    if len(raw) % 2:
        raise ValueError(f"Truncated UTF-16 sequence: &{run}-")
    return raw.decode("utf-16-be")


def encode_folder(name: str) -> str:
    """Encode a mailbox name to modified UTF-7 for the wire.

    Args:
        name: The mailbox name as a user would write it, e.g. ``Wysłane``.

    Returns:
        The name in modified UTF-7. Pure-ASCII names containing no ``&`` are
        returned unchanged, so this is safe to apply to every name.

    Example:
        >>> encode_folder("INBOX")
        'INBOX'
        >>> encode_folder("Wysłane")
        'Wys&AUI-ane'
        >>> encode_folder("R&D")
        'R&-D'
    """
    out: list[str] = []
    run: list[str] = []
    for char in name:
        if _PRINTABLE_MIN <= ord(char) <= _PRINTABLE_MAX:
            if run:
                out.append(_encode_run("".join(run)))
                run = []
            # An ampersand is the shift character, so it escapes as '&-'.
            out.append("&-" if char == "&" else char)
        else:
            run.append(char)
    if run:
        out.append(_encode_run("".join(run)))
    return "".join(out)


def decode_folder(name: str) -> str:
    """Decode a mailbox name the server sent in modified UTF-7.

    Args:
        name: The name as it arrived, e.g. ``Wys&AUI-ane``.

    Returns:
        The decoded name.

    Raises:
        ValueError: If a shift sequence is unterminated or its payload is not
            valid modified BASE64. Callers listing mailboxes should catch this
            and fall back to the raw name rather than failing the whole listing
            over one malformed entry.

    Example:
        >>> decode_folder("Wys&AUI-ane")
        'Wysłane'
        >>> decode_folder("R&-D")
        'R&D'
    """
    out: list[str] = []
    index = 0
    length = len(name)
    while index < length:
        char = name[index]
        if char != "&":
            out.append(char)
            index += 1
            continue
        end = name.find("-", index + 1)
        if end == -1:
            raise ValueError(f"Unterminated shift sequence in mailbox name: {name!r}")
        payload = name[index + 1 : end]
        # '&-' is the escape for a literal ampersand, not an empty sequence.
        out.append("&" if not payload else _decode_run(payload))
        index = end + 1
    return "".join(out)


def quote_folder(name: str) -> str:
    """Encode a mailbox name and wrap it as an IMAP quoted string.

    Encoding runs first, so the result is always ASCII and only the quote and
    backslash characters need escaping. Quoting matters independently of
    encoding: mailbox names containing spaces are common, ``[Gmail]/All Mail``
    among them.

    Args:
        name: The mailbox name as a user would write it.

    Returns:
        A quoted, escaped, modified-UTF-7 mailbox name ready to concatenate
        into an IMAP command.
    """
    escaped = encode_folder(name).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
