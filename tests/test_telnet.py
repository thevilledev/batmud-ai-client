"""Telnet filter tests."""

from __future__ import annotations

from batmud.protocol.telnet import (
    DO,
    DONT,
    GA,
    IAC,
    SB,
    SE,
    WILL,
    WONT,
    Item,
    Marker,
    TelnetFilter,
    encode_command,
)
from conftest import split_every_way

NAWS = 31
ECHO = 1


def run(chunks: list[bytes]) -> tuple[list[Item], bytes]:
    telnet = TelnetFilter()
    items: list[Item] = []
    for chunk in chunks:
        items.extend(telnet.feed(chunk))
    return items, bytes(telnet.pending_replies)


def test_plain_text_passes_through() -> None:
    items, replies = run([b"hello world"])
    assert items == ["hello world"]
    assert replies == b""


def test_go_ahead_becomes_a_marker_in_stream_order() -> None:
    data = b"prompt> " + bytes((IAC, GA)) + b"after"
    items, _ = run([data])
    assert items == ["prompt> ", Marker.GO_AHEAD, "after"]


def test_escaped_iac_is_literal() -> None:
    items, _ = run([b"a" + bytes((IAC, IAC)) + b"b"])
    assert items == ["a\xffb"]


def test_offered_options_are_refused() -> None:
    _, replies = run([bytes((IAC, WILL, ECHO, IAC, DO, NAWS))])
    assert replies == bytes((IAC, DONT, ECHO, IAC, WONT, NAWS))


def test_refusals_need_no_reply() -> None:
    _, replies = run([bytes((IAC, WONT, ECHO, IAC, DONT, NAWS))])
    assert replies == b""


def test_subnegotiation_is_discarded() -> None:
    data = b"before" + bytes((IAC, SB, NAWS, 0, 80, 0, 24, IAC, SE)) + b"after"
    items, _ = run([data])
    assert items == ["beforeafter"]


def test_subnegotiation_containing_escaped_iac() -> None:
    data = bytes((IAC, SB, NAWS, IAC, IAC, 1, IAC, SE)) + b"tail"
    items, _ = run([data])
    assert items == ["tail"]


def test_latin1_decoding() -> None:
    items, _ = run(["Vill\xe9".encode("iso-8859-1")])
    assert items == ["Vill\xe9"]


def test_filter_is_split_invariant() -> None:
    data = (
        b"line one\r\n"
        + bytes((IAC, WILL, ECHO))
        + b"line two"
        + bytes((IAC, GA))
        + bytes((IAC, SB, NAWS, 0, 80, IAC, SE))
        + b"tail"
    )
    expected_items, expected_replies = run([data])
    for chunks in split_every_way(data):
        items, replies = run(chunks)
        # Chunking changes only where text runs are cut, never their content.
        assert _text(items) == _text(expected_items)
        assert _markers(items) == _markers(expected_items)
        assert replies == expected_replies


def _text(items: list[Item]) -> str:
    return "".join(item for item in items if isinstance(item, str))


def _markers(items: list[Item]) -> list[int]:
    """Marker positions expressed as a count of preceding characters."""
    positions: list[int] = []
    seen = 0
    for item in items:
        if isinstance(item, str):
            seen += len(item)
        else:
            positions.append(seen)
    return positions


def test_recorded_banner_yields_prompts(login_banner: bytes) -> None:
    items, _ = run([login_banner])
    assert Marker.GO_AHEAD in items
    assert "create a new character" in _text(items)


# --- outbound ---------------------------------------------------------------


def test_encode_command_appends_newline() -> None:
    assert encode_command("look") == b"look\n"


def test_encode_command_escapes_iac() -> None:
    assert encode_command("a\xffb") == b"a\xff\xffb\n"


def test_encode_command_transliterates_beyond_latin1() -> None:
    # A pasted typographic quote should still reach the game as something.
    assert encode_command("say \u201chi\u201d") == b'say "hi"\n'


def test_encode_command_keeps_latin1_accents() -> None:
    assert encode_command("Vill\xe9").startswith(b"Vill")
