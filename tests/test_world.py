"""World model tests: room parsing, the persistent map, and state folding."""

from __future__ import annotations

import pytest

from batmud.protocol.events import (
    ActionCleared,
    ActionKind,
    ActionProgress,
    Line,
    PlayerInfo,
    PlayerLocation,
    PlayerStatus,
    Span,
    SpellEffect,
    Target,
    Vitals,
)
from batmud.world.map import WorldMap
from batmud.world.room import Room, RoomParser, normalise_direction, parse_exits
from batmud.world.state import WorldState


def line(text: str, channel: str | None = None) -> Line:
    return Line(spans=(Span(text),), channel=channel)


# --- exits ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("There are two obvious exits: north and south.", {"north", "south"}),
        ("There is one obvious exit: north.", {"north"}),
        ("There are no obvious exits.", set()),
        ("Obvious exits: north, south, up", {"north", "south", "up"}),
        ("You see exits: ne, sw, down", {"northeast", "southwest", "down"}),
        ("Exits: n s e w", {"north", "south", "east", "west"}),
        (
            "There are four obvious exits: northeast, northwest, southeast and southwest.",
            {"northeast", "northwest", "southeast", "southwest"},
        ),
    ],
)
def test_parse_exits(text: str, expected: set[str]) -> None:
    assert parse_exits(text) == frozenset(expected)


def test_non_exit_lines_are_not_mistaken_for_exits() -> None:
    assert parse_exits("The orc hits you hard.") is None
    assert parse_exits("You go north.") is None


def test_normalise_direction() -> None:
    assert normalise_direction("NE") == "northeast"
    assert normalise_direction("northwest") == "northwest"
    assert normalise_direction("d") == "down"
    assert normalise_direction("banana") is None


# --- room parsing -----------------------------------------------------------


def test_room_is_emitted_when_the_exits_line_arrives() -> None:
    parser = RoomParser()
    assert parser.feed("Village road") is None
    assert parser.feed("A dusty road runs through the village.") is None
    room = parser.feed("There are two obvious exits: north and south.")
    assert room is not None
    assert room.title == "Village road"
    assert room.description == "A dusty road runs through the village."
    assert room.exits == frozenset({"north", "south"})


def test_combat_channel_lines_are_not_room_text() -> None:
    parser = RoomParser()
    parser.feed("Village road")
    parser.feed("The orc hits you.", "spec_battle")
    parser.feed("Gore sells a sword", "chan_sales")
    room = parser.feed("There is one obvious exit: north.")
    assert room is not None
    assert room.title == "Village road"
    assert room.description == ""


def test_room_identity_prefers_coordinates() -> None:
    outdoor = Room("Plains", "Grass.", frozenset({"north"}), coordinates=("laenor", 5100, 5200, 0))
    assert outdoor.key == "laenor:5100:5200:0"

    indoor = Room("Shop", "A shop.", frozenset({"out"}))
    same = Room("Shop", "A shop.", frozenset({"out"}))
    other = Room("Shop", "A different shop.", frozenset({"out"}))
    assert indoor.key == same.key
    assert indoor.key != other.key
    assert indoor.key.startswith("hash:")


# --- map --------------------------------------------------------------------


@pytest.fixture
def world_map() -> WorldMap:
    with WorldMap() as world:
        yield world


def test_recording_a_room_is_idempotent_and_counts_visits(world_map: WorldMap) -> None:
    room = Room("Plains", exits=frozenset({"north"}), coordinates=("laenor", 1, 1, 0))
    world_map.record_room(room)
    world_map.record_room(room)
    assert len(world_map) == 1
    stored = world_map.get(room.key)
    assert stored is not None and stored.visits == 2


def test_pathfinding_across_recorded_moves(world_map: WorldMap) -> None:
    keys = []
    for index in range(4):
        room = Room(
            f"Room {index}",
            exits=frozenset({"north", "south"}),
            coordinates=("laenor", 0, index, 0),
        )
        world_map.record_room(room)
        keys.append(room.key)
    for index in range(3):
        world_map.record_move(keys[index], "north", keys[index + 1])

    assert world_map.path(keys[0], keys[3]) == ["north", "north", "north"]
    assert world_map.path(keys[0], keys[0]) == []
    assert world_map.path(keys[0], "nowhere") is None


def test_reverse_edges_are_inferred_only_where_the_exit_exists(world_map: WorldMap) -> None:
    start = Room("Start", exits=frozenset({"north"}), coordinates=("l", 0, 0, 0))
    two_way = Room("Two way", exits=frozenset({"south"}), coordinates=("l", 0, 1, 0))
    one_way = Room("One way", exits=frozenset({"east"}), coordinates=("l", 1, 0, 0))
    for room in (start, two_way, one_way):
        world_map.record_room(room)

    world_map.record_move(start.key, "north", two_way.key)
    assert world_map.neighbours(two_way.key) == {"south": start.key}

    world_map.record_move(start.key, "east", one_way.key)
    assert world_map.neighbours(one_way.key) == {}


def test_unexplored_exits_and_routing_to_them(world_map: WorldMap) -> None:
    start = Room("Start", exits=frozenset({"north"}), coordinates=("l", 0, 0, 0))
    far = Room("Far", exits=frozenset({"south", "east"}), coordinates=("l", 0, 1, 0))
    world_map.record_room(start)
    world_map.record_room(far)
    world_map.record_move(start.key, "north", far.key)

    assert world_map.unexplored_exits(start.key) == []
    assert world_map.unexplored_exits(far.key) == ["east"]
    assert world_map.path_to_unexplored(start.key) == (["north"], "east")


def test_map_survives_a_reopen(tmp_path) -> None:
    path = tmp_path / "world.sqlite"
    room = Room("Plains", exits=frozenset({"north"}), coordinates=("laenor", 1, 1, 0))
    with WorldMap(path) as world:
        world.record_room(room)
    with WorldMap(path) as world:
        assert len(world) == 1
        assert world.get(room.key) is not None


# --- state ------------------------------------------------------------------


def test_vitals_come_from_the_control_code() -> None:
    state = WorldState()
    assert "vitals" in state.apply(Vitals(100, 200, 50, 60, 70, 80))
    assert str(state.hp) == "100/200"
    assert state.hp.fraction == 0.5
    assert state.sp.current == 50 and state.ep.maximum == 80


def test_player_info_and_level_change() -> None:
    state = WorldState()
    state.apply(PlayerInfo(name="Killer", race="orc", level=50, experience=1, gender=1))
    assert state.logged_in
    assert state.name == "Killer" and state.level == 50
    assert "level" in state.apply(
        PlayerInfo(name="Killer", race="orc", level=51, experience=2, gender=1)
    )


def test_target_transitions_signal_combat() -> None:
    state = WorldState()
    assert "combat" in state.apply(Target("orc", 90))
    assert state.in_combat
    assert "combat" in state.apply(Target(None))
    assert not state.in_combat


def test_battle_channel_keeps_combat_alive_without_a_target() -> None:
    now = [1000.0]
    state = WorldState(clock=lambda: now[0])
    state.apply(line("The orc hits you.", "spec_battle"))
    assert state.in_combat
    now[0] += 30.0
    assert not state.in_combat


def test_busy_while_casting() -> None:
    state = WorldState()
    state.apply(ActionProgress(ActionKind.SPELL, "magic_missile", 2))
    assert state.busy
    state.apply(ActionCleared())
    assert not state.busy


def test_effects_are_dropped_when_they_expire() -> None:
    state = WorldState()
    state.apply(SpellEffect("blessing", 120))
    assert state.effects == {"blessing": 120}
    state.apply(SpellEffect("blessing", 0))
    assert state.effects == {}


def test_status_flags() -> None:
    state = WorldState()
    state.apply(PlayerStatus(unconscious=False, stunned=True, dead=False))
    assert state.incapacitated and state.stunned


def test_movement_links_rooms_on_the_map() -> None:
    with WorldMap() as world:
        state = WorldState(world_map=world)
        state.apply(PlayerLocation("me", "orc", 1, "laenor", 5100, 5200, 0))
        state.apply(line("Village road"))
        state.apply(line("There is one obvious exit: north."))
        first = state.room_key

        state.note_command("n")
        state.apply(PlayerLocation("me", "orc", 1, "laenor", 5100, 5201, 0))
        state.apply(line("Village square"))
        state.apply(line("There is one obvious exit: south."))
        second = state.room_key

        assert first != second
        assert world.neighbours(str(first)) == {"north": second}
        assert world.path(str(first), str(second)) == ["north"]


def test_revisiting_the_same_room_is_not_a_change() -> None:
    state = WorldState()
    state.apply(line("Village road"))
    assert "room" in state.apply(line("There is one obvious exit: north."))
    state.apply(line("Village road"))
    assert "room" not in state.apply(line("There is one obvious exit: north."))


def test_summary_reports_what_is_known() -> None:
    state = WorldState()
    state.apply(Vitals(50, 200, 10, 60, 70, 80))
    state.apply(PlayerInfo(name="Killer", race="orc", level=50, experience=1234, gender=1))
    state.apply(PlayerLocation("Killer", "orc", 1, "laenor", 5100, 5200, 0))
    state.apply(Target("orc", 45))
    summary = state.summary()
    assert "HP 50/200" in summary
    assert "Killer the orc, level 50" in summary
    assert "laenor (5100, 5200, 0)" in summary
    assert "orc at ~45% health" in summary


def test_batclient_detection() -> None:
    state = WorldState()
    assert not state.batclient_active
    state.apply(Vitals(1, 2))
    assert state.batclient_active
