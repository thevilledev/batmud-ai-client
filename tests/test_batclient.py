"""Control code parser tests.

Every behavioural test is also run with the input chopped at every possible
offset, because the defect these replace was a parser that only worked when a
pattern happened to land inside a single read.
"""

from __future__ import annotations

import pytest

from batmud.protocol.batclient import BatClientParser
from batmud.protocol.events import (
    ActionCleared,
    ActionKind,
    ActionProgress,
    ClearScreen,
    ConnectionFailed,
    ConnectionSucceeded,
    CustomInfo,
    Event,
    FreeExperience,
    Line,
    PartyMemberLeft,
    PartyMemberLocation,
    PartyMemberStatus,
    PlayerInfo,
    PlayerLocation,
    PlayerStatus,
    Prompt,
    SpellEffect,
    Target,
    UnknownTag,
    Vitals,
)
from conftest import split_at, split_every_way

E = "\x1b"


def parse(text: str) -> list[Event]:
    parser = BatClientParser()
    events = parser.feed(text)
    events.extend(parser.flush())
    return events


def parse_in_pieces(text: str, pieces: list[str]) -> list[Event]:
    parser = BatClientParser()
    events: list[Event] = []
    for piece in pieces:
        events.extend(parser.feed(piece))
    events.extend(parser.flush())
    return events


def every_split(text: str) -> list[list[Event]]:
    """Parse the text once per possible split point, plus character by character."""
    results = [parse_in_pieces(text, [text[:i], text[i:]]) for i in range(len(text) + 1)]
    results.append(parse_in_pieces(text, list(text)))
    return results


def assert_split_invariant(text: str) -> list[Event]:
    """The parse must not depend on how the stream was chunked."""
    expected = parse(text)
    for index, result in enumerate(every_split(text)):
        assert result == expected, f"split {index} changed the parse"
    return expected


# --- plain text -------------------------------------------------------------


def test_plain_lines() -> None:
    events = assert_split_invariant("first\r\nsecond\r\n")
    assert [event.text for event in events if isinstance(event, Line)] == ["first", "second"]


def test_partial_line_is_held_until_flush() -> None:
    parser = BatClientParser()
    assert parser.feed("incomplete") == []
    assert [event.text for event in parser.flush() if isinstance(event, Line)] == ["incomplete"]


# --- vitals and player data -------------------------------------------------


def test_full_vitals() -> None:
    events = assert_split_invariant(f"{E}<50100 200 200 250 300 350{E}>50")
    assert events == [Vitals(100, 200, 200, 250, 300, 350)]


def test_partial_vitals_documented_arity() -> None:
    assert parse(f"{E}<51100 200 200{E}>51") == [Vitals(hp=100, sp=200, ep=200)]


def test_partial_vitals_as_shown_in_the_example() -> None:
    # Code 51's worked example carries six fields, not the documented three.
    assert parse(f"{E}<51102 1001 500 600 300 400{E}>51") == [Vitals(102, 1001, 500, 600, 300, 400)]


def test_player_info_five_fields() -> None:
    assert parse(f"{E}<52Killer orc 50 1 123456{E}>52") == [
        PlayerInfo(name="Killer", race="orc", level=50, experience=123456, gender=1)
    ]


def test_player_info_six_fields_includes_surname() -> None:
    assert parse(f"{E}<52Ulath Pulath coder 100 1 1345323{E}>52") == [
        PlayerInfo(
            name="Ulath",
            race="coder",
            level=100,
            experience=1345323,
            surname="Pulath",
            gender=1,
        )
    ]


def test_free_experience() -> None:
    assert parse(f"{E}<531345323{E}>53") == [FreeExperience(1345323)]


def test_player_status() -> None:
    assert parse(f"{E}<540 1 0{E}>54") == [PlayerStatus(False, True, False)]
    status = parse(f"{E}<540 0 0{E}>54")[0]
    assert isinstance(status, PlayerStatus)
    assert not status.incapacitated


def test_location() -> None:
    events = assert_split_invariant(f"{E}<60ulath coder 1 laenor 5100 5200 0{E}>60")
    assert events == [
        PlayerLocation(
            name="ulath", race="coder", gender=1, continent="laenor", x=5100, y=5200, z=0
        )
    ]


# --- combat and effects -----------------------------------------------------


def test_target_and_clearing_it() -> None:
    assert parse(f"{E}<70evilmonster 45{E}>70") == [Target("evilmonster", 45)]
    assert parse(f"{E}<700 0{E}>70") == [Target(None)]


def test_spell_effect_strips_padding() -> None:
    assert parse(f"{E}<64lay_on_hands 120{E}>64") == [SpellEffect("lay_on_hands", 120)]
    effect = parse(f"{E}<64blessing___ 0{E}>64")[0]
    assert isinstance(effect, SpellEffect)
    assert effect.name == "blessing"
    assert effect.expired


def test_action_progress() -> None:
    assert parse(f"{E}<41magic_missile 2{E}>41") == [
        ActionProgress(ActionKind.SPELL, "magic_missile", 2)
    ]
    assert parse(f"{E}<42bladed_fury 5{E}>42") == [
        ActionProgress(ActionKind.SKILL, "bladed_fury", 5)
    ]
    assert parse(f"{E}<40{E}>40") == [ActionCleared()]


# --- party ------------------------------------------------------------------


def test_party_member_location() -> None:
    assert parse(f"{E}<61ulath 1 1{E}>61") == [PartyMemberLocation("ulath", 1, 1)]


def test_party_member_left() -> None:
    assert parse(f"{E}<63ulath{E}>63") == [PartyMemberLeft("ulath")]


def test_full_party_status() -> None:
    payload = (
        "Killer orc 1 50 101 200 202 303 404 504 "
        "ekuva_ja_expaa 1 1 1 0 0 0 0 1 0 0 0 0 0 0 0 "
        "12345 100000 1234 Wed_Oct_31_15:57:52_2007"
    )
    events = parse(f"{E}<62{payload}{E}>62")
    assert events == [
        PartyMemberStatus(
            name="Killer",
            race="orc",
            gender=1,
            level=50,
            hp=101,
            max_hp=200,
            sp=202,
            max_sp=303,
            ep=404,
            max_ep=504,
            party_name="ekuva ja expaa",
            place_x=1,
            place_y=1,
            flags=frozenset({"creator", "leader"}),
            party_experience=12345,
            total_party_experience=100000,
            party_seconds=1234,
            party_created="Wed Oct 31 15:57:52 2007",
        )
    ]


def test_party_status_flags_follow_the_documented_bit_order() -> None:
    payload = "Foo orc 1 5 1 1 1 1 1 1 ekuva_meille 2 2 0 0 0 0 1 0 0 0 1 1 0 0 0 0 0 0 x"
    member = parse(f"{E}<62{payload}{E}>62")[0]
    assert isinstance(member, PartyMemberStatus)
    assert member.flags == frozenset({"following", "invisible", "idle"})
    assert (member.place_x, member.place_y) == (2, 2)


# --- connection results -----------------------------------------------------


def test_connection_success_and_failure() -> None:
    assert parse(f"{E}<05{E}>05") == [ConnectionSucceeded()]
    assert parse(f"{E}<06Incorrect password.{E}>06") == [ConnectionFailed("Incorrect password.")]


# --- styling ----------------------------------------------------------------


def test_foreground_colour_applies_to_the_body_only() -> None:
    events = assert_split_invariant(f"plain {E}<20FF0000{E}|red{E}>20 plain\n")
    line = events[0]
    assert isinstance(line, Line)
    assert line.text == "plain red plain"
    assert [(span.text, span.style.fg) for span in line.spans] == [
        ("plain ", None),
        ("red", "ff0000"),
        (" plain", None),
    ]


def test_nested_colours_restore_the_outer_style() -> None:
    text = f"{E}<20FFFFFF{E}|{E}<210000FF{E}|white on blue{E}>21back{E}>20\n"
    events = assert_split_invariant(text)
    line = events[0]
    assert isinstance(line, Line)
    assert [(span.text, span.style.fg, span.style.bg) for span in line.spans] == [
        ("white on blue", "ffffff", "0000ff"),
        ("back", "ffffff", None),
    ]


def test_bold_takes_no_argument() -> None:
    line = parse(f"{E}<22Test{E}>22\n")[0]
    assert isinstance(line, Line)
    assert line.text == "Test"
    assert line.spans[0].style.bold


def test_in_game_link_is_captured_as_a_command() -> None:
    line = parse(f"{E}<31north{E}|Go north{E}>31\n")[0]
    assert isinstance(line, Line)
    assert line.spans[0].style.command == "north"
    assert line.text == "Go north"


def test_code_zero_resets_everything() -> None:
    events = parse(f"{E}<20FF0000{E}|red{E}<00{E}>00plain\n")
    line = next(event for event in events if isinstance(event, Line))
    assert [(span.text, span.style.fg) for span in line.spans] == [
        ("red", "ff0000"),
        ("plain", None),
    ]


def test_ansi_sgr_still_works_for_non_batclient_text() -> None:
    line = parse("\x1b[1;31mdanger\x1b[0m safe\n")[0]
    assert isinstance(line, Line)
    assert line.text == "danger safe"
    assert line.spans[0].style.bold and line.spans[0].style.fg == "800000"
    assert not line.spans[1].style.bold


# --- channels ---------------------------------------------------------------


def test_channel_tags_the_line() -> None:
    events = assert_split_invariant(f"{E}<10chan_sales{E}|Gore sells a sword{E}>10\n")
    line = events[0]
    assert isinstance(line, Line)
    assert line.channel == "chan_sales"
    assert line.text == "Gore sells a sword"


def test_clear_screen_carries_the_window_from_the_enclosing_channel() -> None:
    assert parse(f"{E}<10map{E}|{E}<11{E}>11{E}>10") == [ClearScreen("map")]


def test_spec_prompt_is_dropped_until_a_go_ahead_arrives() -> None:
    parser = BatClientParser()
    keepalive = f"{E}<10spec_prompt{E}|hp:100 sp:50{E}>10\n"
    assert parser.feed(keepalive) == []
    assert parser.feed(keepalive) == []
    assert parser.go_ahead() == [Prompt("hp:100 sp:50")]


def test_go_ahead_turns_a_partial_line_into_a_prompt() -> None:
    parser = BatClientParser()
    assert parser.feed("Please enter your choice or name: ") == []
    assert parser.go_ahead() == [Prompt("Please enter your choice or name: ")]


# --- robustness -------------------------------------------------------------


def test_unmatched_closing_tag_is_ignored() -> None:
    # The server is documented to emit extraneous closing tags.
    events = parse(f"{E}>20hello\n")
    assert [event.text for event in events if isinstance(event, Line)] == ["hello"]


def test_unknown_code_does_not_swallow_text() -> None:
    events = parse(f"{E}<77visible{E}>77\n")
    assert UnknownTag(77) in events
    assert [event.text for event in events if isinstance(event, Line)] == ["visible"]


def test_malformed_code_is_recovered_as_text() -> None:
    events = parse(f"{E}<xy\n")
    assert [event.text for event in events if isinstance(event, Line)] == ["<xy"]


def test_newline_terminates_a_runaway_argument() -> None:
    # An unterminated data tag must not consume the rest of the session.
    events = parse(f"{E}<50100 200\nafter\n")
    assert Vitals(hp=100, max_hp=200) in events
    assert [event.text for event in events if isinstance(event, Line)] == ["", "after"]


def test_custom_info() -> None:
    assert parse(f"{E}<991 dex 300{E}>99") == [CustomInfo("dex", "300", numeric=True)]


@pytest.mark.parametrize("index", range(0, 40))
def test_interleaved_stream_is_split_invariant(index: int) -> None:
    stream = (
        f"You hit the orc.\r\n{E}<50100 200 50 60 70 80{E}>50"
        f"{E}<70orc 45{E}>70{E}<10spec_battle{E}|The orc hits you.{E}>10\r\n"
    )
    parser = BatClientParser()
    events: list[Event] = []
    for piece in (stream[:index], stream[index:]):
        events.extend(parser.feed(piece))
    events.extend(parser.flush())
    assert Vitals(100, 200, 50, 60, 70, 80) in events
    assert Target("orc", 45) in events
    lines = [event for event in events if isinstance(event, Line)]
    assert [(line.text, line.channel) for line in lines] == [
        ("You hit the orc.", None),
        ("The orc hits you.", "spec_battle"),
    ]


def test_recorded_login_banner_parses_identically_at_any_chunk_size(
    login_banner: bytes,
) -> None:
    from batmud.protocol.telnet import Connection

    def run(chunks: list[bytes]) -> list[Event]:
        connection = Connection()
        events: list[Event] = []
        for chunk in chunks:
            events.extend(connection.feed(chunk))
        return events

    expected = run([login_banner])
    for chunks in split_every_way(login_banner):
        assert run(chunks) == expected
    assert run(split_at(login_banner, len(login_banner) // 2)) == expected

    prompts = [event for event in expected if isinstance(event, Prompt)]
    assert prompts, "the recorded banner ends in IAC GA"
    assert "enter your choice or name" in prompts[-1].text
