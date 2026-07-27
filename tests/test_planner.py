"""Planner, tool and memory tests. The model itself is stubbed."""

from __future__ import annotations

import json
from typing import Any

import pytest

from batmud.agent.memory import Memory
from batmud.agent.planner import LLMPlanner
from batmud.agent.safety import Budget, Source
from batmud.agent.tools import ToolContext, run_tool
from batmud.config import Settings
from batmud.llm.client import Completion, LLMClient, ToolCall
from batmud.protocol.events import Line, PlayerLocation, Span, Target, Vitals
from batmud.world.map import WorldMap
from batmud.world.room import Room
from batmud.world.state import WorldState


def line(text: str, channel: str | None = None) -> Line:
    return Line(spans=(Span(text),), channel=channel)


def state_in_room(*exits: str) -> WorldState:
    state = WorldState()
    state.apply(line("Village road"))
    state.apply(line(f"Obvious exits: {', '.join(exits)}"))
    return state


def context(state: WorldState, world_map: WorldMap | None = None) -> ToolContext:
    return ToolContext(state=state, memory=Memory(), world_map=world_map)


# --- tools: movement --------------------------------------------------------


def test_move_uses_the_short_command_form() -> None:
    outcome = run_tool("move", {"direction": "north"}, context(state_in_room("north")))
    assert outcome.ok and outcome.command == "n"


def test_move_through_a_missing_exit_is_rejected_with_the_alternatives() -> None:
    # The previous client silently substituted a different exit here, so the
    # model never found out. Now it gets told.
    outcome = run_tool("move", {"direction": "west"}, context(state_in_room("north", "south")))
    assert not outcome.ok
    assert outcome.command is None
    assert "no west exit" in outcome.feedback
    assert "north, south" in outcome.feedback


def test_move_accepts_abbreviations_and_rejects_nonsense() -> None:
    assert (
        run_tool("move", {"direction": "ne"}, context(state_in_room("northeast"))).command == "ne"
    )
    outcome = run_tool("move", {"direction": "sideways"}, context(state_in_room("north")))
    assert not outcome.ok and "not a direction" in outcome.feedback


def test_move_is_allowed_when_exits_are_unknown() -> None:
    # Before a room has been parsed there is nothing to validate against;
    # blocking movement here would strand the client.
    assert run_tool("move", {"direction": "north"}, context(WorldState())).command == "n"


def test_explore_routes_to_an_unused_exit() -> None:
    with WorldMap() as world:
        here = Room("Start", exits=frozenset({"north", "east"}), coordinates=("l", 0, 0, 0))
        there = Room("North", exits=frozenset({"south"}), coordinates=("l", 0, 1, 0))
        world.record_room(here)
        world.record_room(there)
        world.record_move(here.key, "north", there.key)

        state = WorldState(world_map=world)
        state.room = here
        outcome = run_tool("explore", {}, context(state, world))
        assert outcome.command == "e"


def test_explore_reports_when_everything_is_mapped() -> None:
    with WorldMap() as world:
        room = Room("Dead end", exits=frozenset(), coordinates=("l", 0, 0, 0))
        world.record_room(room)
        state = WorldState(world_map=world)
        state.room = room
        outcome = run_tool("explore", {}, context(state, world))
        assert not outcome.ok and "Every mapped exit" in outcome.feedback


def test_travel_to_returns_the_first_step_of_the_route() -> None:
    with WorldMap() as world:
        rooms = [
            Room(
                f"Room {index}", exits=frozenset({"north", "south"}), coordinates=("l", 0, index, 0)
            )
            for index in range(3)
        ]
        for room in rooms:
            world.record_room(room)
        world.record_move(rooms[0].key, "north", rooms[1].key)
        world.record_move(rooms[1].key, "north", rooms[2].key)

        state = WorldState(world_map=world)
        state.room = rooms[0]
        outcome = run_tool("travel_to", {"destination": "Room 2"}, context(state, world))
        assert outcome.command == "n"
        assert "2 steps" in outcome.feedback


def test_travel_to_an_unmapped_place_is_rejected() -> None:
    with WorldMap() as world:
        room = Room("Start", exits=frozenset({"north"}), coordinates=("l", 0, 0, 0))
        world.record_room(room)
        state = WorldState(world_map=world)
        state.room = room
        outcome = run_tool("travel_to", {"destination": "Atlantis"}, context(state, world))
        assert not outcome.ok and "No mapped room" in outcome.feedback


# --- tools: combat and bookkeeping ------------------------------------------


def test_attack_defaults_to_the_current_target() -> None:
    state = WorldState()
    state.apply(Target("orc", 80))
    assert run_tool("attack", {}, context(state)).command == "kill orc"


def test_attack_without_a_target_is_rejected() -> None:
    outcome = run_tool("attack", {}, context(WorldState()))
    assert not outcome.ok and "none is currently set" in outcome.feedback


def test_remember_and_recall() -> None:
    ctx = context(state_in_room("north"))
    assert run_tool("remember", {"note": "shop sells rope"}, ctx).ok
    assert "shop sells rope" in run_tool("recall", {}, ctx).feedback


def test_remember_ignores_duplicates() -> None:
    ctx = context(WorldState())
    run_tool("remember", {"note": "same"}, ctx)
    assert "already recorded" in run_tool("remember", {"note": "same"}, ctx).feedback


def test_set_goal_keeps_the_previous_goal_underneath() -> None:
    ctx = context(WorldState())
    ctx.memory.set_goal("explore")
    run_tool("set_goal", {"goal": "buy a sword"}, ctx)
    assert ctx.memory.goal == "buy a sword"
    assert ctx.memory.pop_goal() == "explore"


def test_wait_produces_no_command() -> None:
    outcome = run_tool("wait", {"reason": "resting"}, context(WorldState()))
    assert outcome.ok and outcome.command is None


def test_raw_command_passes_through_but_rejects_newlines() -> None:
    assert (
        run_tool("raw_command", {"command": "buy rope"}, context(WorldState())).command
        == "buy rope"
    )
    assert not run_tool("raw_command", {"command": "look\nquit"}, context(WorldState())).ok


def test_an_unknown_tool_is_reported_with_the_alternatives() -> None:
    outcome = run_tool("teleport", {}, context(WorldState()))
    assert not outcome.ok
    assert "no tool called 'teleport'" in outcome.feedback
    assert "move" in outcome.feedback


# --- memory -----------------------------------------------------------------


def test_memory_round_trips_through_disk(tmp_path) -> None:
    path = tmp_path / "memory.json"
    memory = Memory(path=path, goal="explore")
    memory.remember("orc guards the bridge", room="Bridge")
    memory.set_goal("cross the bridge")
    memory.save()

    reloaded = Memory.load(path)
    assert reloaded.goal == "cross the bridge"
    assert reloaded.stack == ["explore"]
    assert reloaded.notes[0].text == "orc guards the bridge"
    assert reloaded.notes_for_room("Bridge")


def test_memory_load_tolerates_a_corrupt_file(tmp_path) -> None:
    path = tmp_path / "memory.json"
    path.write_text("{not json", encoding="utf-8")
    assert Memory.load(path, default_goal="explore").goal == "explore"


def test_memory_context_mentions_the_goal_and_room_notes() -> None:
    memory = Memory(goal="find the smith")
    memory.remember("smith is here", room="Forge")
    text = memory.context(room="Forge")
    assert "find the smith" in text
    assert "smith is here" in text


# --- planner ----------------------------------------------------------------


class StubLLM(LLMClient):
    """An LLMClient that replays scripted completions."""

    def __init__(self, completions: list[Completion]) -> None:
        settings = Settings().llm
        super().__init__(settings=settings, budget=Budget(), client=None)
        self.scripted = list(completions)
        self.requests: list[list[dict[str, Any]]] = []
        self.tool_choices: list[str] = []

    @property
    def available(self) -> bool:
        return bool(self.scripted)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        tool_choice: str = "auto",
    ) -> Completion:
        # Copied because the planner keeps appending to the list it passed in.
        self.requests.append(list(messages))
        self.tool_choices.append(tool_choice)
        return self.scripted.pop(0)


def tool_completion(name: str, **arguments: Any) -> Completion:
    return Completion(tool_calls=(ToolCall(id="call_1", name=name, arguments=arguments),))


def make_planner(completions: list[Completion], **kwargs: Any) -> tuple[LLMPlanner, StubLLM]:
    llm = StubLLM(completions)
    planner = LLMPlanner(settings=Settings(), llm=llm, memory=Memory(), **kwargs)
    return planner, llm


async def test_planner_turns_a_tool_call_into_a_command() -> None:
    planner, _ = make_planner([tool_completion("move", direction="north")])
    decision = await planner.decide(state_in_room("north"), trigger="room")
    assert decision is not None
    assert decision.command == "n"
    assert decision.source is Source.PLANNER
    assert "move(direction='north')" in decision.reason


async def test_a_rejected_tool_call_is_fed_back_and_retried() -> None:
    planner, llm = make_planner(
        [
            tool_completion("move", direction="west"),
            tool_completion("move", direction="north"),
        ]
    )
    decision = await planner.decide(state_in_room("north"), trigger="room")
    assert decision is not None and decision.command == "n"

    second_request = llm.requests[1]
    tool_reply = next(message for message in second_request if message["role"] == "tool")
    assert tool_reply["content"].startswith("ERROR:")
    assert "no west exit" in tool_reply["content"]


async def test_wait_yields_no_command() -> None:
    planner, _ = make_planner([tool_completion("wait", reason="resting")])
    assert await planner.decide(state_in_room("north"), trigger="idle") is None


async def test_prose_without_a_tool_call_is_never_sent_to_the_game() -> None:
    planner, _ = make_planner(
        [Completion(content="I would go north."), Completion(content="Still thinking.")]
    )
    assert await planner.decide(state_in_room("north"), trigger="room") is None
    assert "without calling a tool" in planner.last_error


async def test_the_model_is_required_to_call_a_tool() -> None:
    # A turn that produces prose is a wasted turn, so 'auto' is not enough:
    # the provider is told a tool call is mandatory.
    planner, llm = make_planner([tool_completion("move", direction="north")])
    await planner.decide(state_in_room("north"), trigger="room")
    assert llm.tool_choices == ["required"]


async def test_a_truncated_reply_says_so_instead_of_looking_like_prose() -> None:
    planner, _ = make_planner(
        [
            Completion(content="I would go", finish_reason="length"),
            Completion(content="north", finish_reason="length"),
        ]
    )
    assert await planner.decide(state_in_room("north"), trigger="room") is None
    assert "finish_reason=length" in planner.last_error


async def test_a_working_turn_clears_the_previous_failure() -> None:
    # The status panel reads last_error, so leaving a stale one there reports a
    # healthy planner as broken for the rest of the session.
    planner, _ = make_planner(
        [Completion(content="thinking"), Completion(content="still thinking")]
    )
    state = state_in_room("north")
    assert await planner.decide(state, trigger="room") is None
    assert planner.last_error

    planner.llm.scripted.append(tool_completion("move", direction="north"))  # type: ignore[attr-defined]
    assert await planner.decide(state, trigger="room") is not None
    assert planner.last_error == ""


async def test_informational_tools_do_not_end_the_turn() -> None:
    planner, _ = make_planner(
        [tool_completion("recall"), tool_completion("move", direction="north")]
    )
    decision = await planner.decide(state_in_room("north"), trigger="room")
    assert decision is not None and decision.command == "n"


async def test_the_planner_is_unavailable_without_an_api_key() -> None:
    planner = LLMPlanner(
        settings=Settings(), llm=LLMClient(settings=Settings().llm), memory=Memory()
    )
    assert not planner.available
    assert "no API key" in planner.unavailable_reason
    assert await planner.decide(state_in_room("north"), trigger="room") is None


async def test_an_exhausted_budget_stops_the_planner() -> None:
    llm = LLMClient(settings=Settings().llm, budget=Budget(max_requests=1, requests=1))
    llm.client = object()  # type: ignore[assignment]
    planner = LLMPlanner(settings=Settings(), llm=llm, memory=Memory())
    assert not planner.available
    assert "request cap" in planner.unavailable_reason


async def test_context_carries_state_map_and_feedback() -> None:
    with WorldMap() as world:
        planner, llm = make_planner([tool_completion("move", direction="north")], world_map=world)
        state = state_in_room("north")
        state.world_map = world
        state.apply(Vitals(120, 200, 30, 60, 40, 80))
        state.apply(PlayerLocation("Killer", "orc", 1, "laenor", 5100, 5200, 0))
        world.record_room(state.room)  # type: ignore[arg-type]

        await planner.decide(state, trigger="room", feedback="Command 'quit' was refused")

        prompt = llm.requests[0][-1]["content"]
        assert "HP 120/200" in prompt
        assert "laenor (5100, 5200, 0)" in prompt
        assert "Command 'quit' was refused" in prompt
        assert "untrusted" in prompt


async def test_secrets_never_reach_the_model() -> None:
    planner, llm = make_planner([tool_completion("move", direction="north")], secrets=("hunter2",))
    state = state_in_room("north")
    state.apply(line("Someone shouts: my password is hunter2"))
    await planner.decide(state, trigger="room")

    prompt = llm.requests[0][-1]["content"]
    assert "hunter2" not in prompt
    assert "********" in prompt


async def test_warns_the_model_when_control_codes_are_absent() -> None:
    planner, llm = make_planner([tool_completion("move", direction="north")])
    await planner.decide(state_in_room("north"), trigger="room")
    assert "BatClient protocol is not active" in llm.requests[0][-1]["content"]


async def test_history_gives_the_next_turn_continuity() -> None:
    planner, llm = make_planner(
        [
            tool_completion("move", direction="north"),
            tool_completion("move", direction="north"),
        ]
    )
    state = state_in_room("north")
    await planner.decide(state, trigger="room")
    await planner.decide(state, trigger="room")

    roles = [message["role"] for message in llm.requests[1]]
    assert roles == ["system", "assistant", "tool", "user"]


async def test_rate_limiting_skips_a_decision_point() -> None:
    settings = Settings()
    limited = settings.llm.model_copy(update={"max_requests_per_minute": 1})
    planner, _ = make_planner(
        [
            tool_completion("move", direction="north"),
            tool_completion("move", direction="north"),
        ]
    )
    planner.settings = settings.model_copy(update={"llm": limited})
    planner.limiter.max_per_minute = 1  # type: ignore[union-attr]

    state = state_in_room("north")
    assert await planner.decide(state, trigger="room") is not None
    assert await planner.decide(state, trigger="room") is None


# --- completion parsing -----------------------------------------------------


def test_invalid_tool_json_becomes_empty_arguments() -> None:
    from batmud.llm.client import _parse_arguments

    assert _parse_arguments('{"direction": "north"}') == {"direction": "north"}
    assert _parse_arguments("{not json") == {}
    assert _parse_arguments(None) == {}
    assert _parse_arguments("[1, 2]") == {}


def test_assistant_message_serialises_tool_calls() -> None:
    from batmud.agent.planner import _assistant_message

    message = _assistant_message(tool_completion("move", direction="north"))
    assert message["tool_calls"][0]["function"]["name"] == "move"
    assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == {"direction": "north"}


@pytest.mark.parametrize("field", ["prompt_tokens", "completion_tokens"])
def test_usage_is_recorded_against_the_budget(field: str) -> None:
    budget = Budget()
    budget.record(
        input_tokens=10 if field == "prompt_tokens" else 0,
        output_tokens=5 if field == "completion_tokens" else 0,
    )
    assert budget.requests == 1
    assert budget.total_tokens > 0
