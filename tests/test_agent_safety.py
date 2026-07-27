"""Reflex and safety guard tests."""

from __future__ import annotations

import pytest

from batmud.agent.reflexes import LoopDetector, ReflexEngine
from batmud.agent.safety import (
    Budget,
    CommandGuard,
    Decision,
    RateLimiter,
    Source,
)
from batmud.config import Settings
from batmud.protocol.events import ActionKind, ActionProgress, Line, Span, Target, Vitals
from batmud.world.state import WorldState


def planner_command(command: str) -> Decision:
    return Decision(command=command, source=Source.PLANNER)


# --- command guard ----------------------------------------------------------


@pytest.fixture
def guard() -> CommandGuard:
    return CommandGuard(Settings().safety, secrets=("hunter2",))


def test_ordinary_commands_are_allowed(guard: CommandGuard) -> None:
    assert guard.check(planner_command("north")) is None
    assert guard.check(planner_command("kill orc")) is None


@pytest.mark.parametrize("command", ["quit", "QUIT", "suicide", "delete me", "reincarnate"])
def test_destructive_commands_are_refused(guard: CommandGuard, command: str) -> None:
    refusal = guard.check(planner_command(command))
    assert refusal is not None
    assert "denied command list" in refusal.reason


@pytest.mark.parametrize("command", ["give all to orc", "drop all", "sell all", "shout hello"])
def test_denied_patterns_are_refused(guard: CommandGuard, command: str) -> None:
    assert guard.check(planner_command(command)) is not None


def test_communication_is_off_by_default(guard: CommandGuard) -> None:
    refusal = guard.check(planner_command("tell someone hi"))
    assert refusal is not None
    assert "communication command" in refusal.reason


def test_communication_can_be_enabled() -> None:
    settings = Settings()
    permissive = settings.safety.model_copy(update={"allow_communication": True})
    assert CommandGuard(permissive).check(planner_command("say hello")) is None


def test_a_command_containing_the_password_is_refused(guard: CommandGuard) -> None:
    refusal = guard.check(planner_command("say my password is hunter2"))
    assert refusal is not None
    assert "secret" in refusal.reason


def test_embedded_newlines_are_refused(guard: CommandGuard) -> None:
    refusal = guard.check(planner_command("look\nquit"))
    assert refusal is not None
    assert "newline" in refusal.reason


def test_the_login_source_is_trusted_with_credentials(guard: CommandGuard) -> None:
    credential = Decision(command="hunter2", source=Source.LOGIN, secret=True)
    assert guard.check(credential) is None


def test_an_empty_command_is_allowed(guard: CommandGuard) -> None:
    assert guard.check(Decision(command="", source=Source.REFLEX)) is None


# --- rate limiting ----------------------------------------------------------


def test_minimum_interval_between_commands() -> None:
    now = [100.0]
    limiter = RateLimiter(max_per_minute=60, min_interval=1.0, clock=lambda: now[0])
    assert limiter.delay() == 0.0
    limiter.record()
    assert limiter.delay() == pytest.approx(1.0)
    now[0] += 0.4
    assert limiter.delay() == pytest.approx(0.6)
    now[0] += 0.6
    assert limiter.delay() == 0.0


def test_per_minute_cap() -> None:
    now = [0.0]
    limiter = RateLimiter(max_per_minute=3, min_interval=0.0, clock=lambda: now[0])
    for _ in range(3):
        limiter.record()
        now[0] += 1.0
    assert limiter.delay() == pytest.approx(57.0)
    now[0] += 60.0
    assert limiter.delay() == 0.0


# --- budget -----------------------------------------------------------------


def test_budget_with_no_caps_never_runs_out() -> None:
    budget = Budget()
    budget.record(1_000_000, 1_000_000, 100.0)
    assert budget.exhausted() is None


def test_budget_caps() -> None:
    assert Budget(max_requests=1, requests=1).exhausted() == "request cap reached (1)"
    tokens = Budget(max_total_tokens=10, input_tokens=6, output_tokens=6)
    assert tokens.exhausted() is not None
    spend = Budget(max_spend_usd=0.5, spend_usd=0.5)
    assert spend.exhausted() is not None


def test_budget_summary() -> None:
    budget = Budget()
    budget.record(100, 20, 0.001)
    assert "1 requests" in budget.summary()
    assert "100 in" in budget.summary()
    assert "$0.0010" in budget.summary()


# --- reflexes ---------------------------------------------------------------


def engine(**safety: object) -> ReflexEngine:
    settings = Settings()
    if safety:
        settings = settings.model_copy(update={"safety": settings.safety.model_copy(update=safety)})
    return ReflexEngine(settings, clock=lambda: 0.0)


def test_pager_prompts_are_acknowledged_without_approval() -> None:
    decision = engine().evaluate(WorldState(), "[Press RETURN to continue]")
    assert decision is not None
    assert decision.command == ""
    assert decision.auto


def test_more_pager_is_acknowledged() -> None:
    assert engine().evaluate(WorldState(), "--More--") is not None


def test_critical_health_flees_immediately() -> None:
    state = WorldState()
    state.apply(Vitals(10, 200))
    decision = engine().evaluate(state)
    assert decision is not None
    assert decision.command == "flee"
    assert "critical" in decision.reason
    assert not decision.auto, "escaping is gameplay and still needs approval in co-pilot mode"


def test_retreat_only_applies_in_combat() -> None:
    state = WorldState()
    state.apply(Vitals(60, 200))
    assert engine().evaluate(state) is None

    state.apply(Target("orc", 100))
    decision = engine().evaluate(state)
    assert decision is not None and decision.command == "flee"


def test_healthy_characters_get_no_reflex() -> None:
    state = WorldState()
    state.apply(Vitals(200, 200))
    state.apply(Target("orc", 100))
    assert engine().evaluate(state) is None


def test_unknown_health_does_not_trigger_a_reflex() -> None:
    # Before any control code arrives the maximum is unknown; guessing here
    # would make the client flee on connect.
    assert engine().evaluate(WorldState()) is None


def test_cooldowns_stop_a_reflex_repeating() -> None:
    now = [0.0]
    settings = Settings()
    reflexes = ReflexEngine(settings, clock=lambda: now[0])
    state = WorldState()
    state.apply(Vitals(10, 200))
    assert reflexes.evaluate(state) is not None
    assert reflexes.evaluate(state) is None
    now[0] += 10.0
    assert reflexes.evaluate(state) is not None


def test_no_commands_while_a_spell_is_being_cast() -> None:
    state = WorldState()
    state.apply(ActionProgress(ActionKind.SPELL, "magic_missile", 2))
    assert not engine().should_act(state)


def test_no_commands_while_stunned() -> None:
    state = WorldState()
    state.stunned = True
    assert not engine().should_act(state)
    state.stunned = False
    assert engine().should_act(state)


def test_anti_idle_after_the_configured_delay() -> None:
    now = [1000.0]
    state = WorldState(clock=lambda: now[0])
    state.apply(Line(spans=(Span("hello"),)))
    reflexes = ReflexEngine(Settings(), clock=lambda: now[0])
    assert reflexes.evaluate(state) is None
    now[0] += 300.0
    decision = reflexes.evaluate(state)
    assert decision is not None and decision.command == "look"
    # help robot explicitly permits triggers that only keep you non-idle.
    assert decision.auto


# --- loop detection ---------------------------------------------------------


def test_a_repeated_single_command_is_a_loop() -> None:
    loops = LoopDetector()
    for _ in range(3):
        loops.record("north")
    assert loops.detect() == "north"


def test_a_repeated_pair_is_a_loop() -> None:
    loops = LoopDetector()
    for _ in range(3):
        loops.record("north")
        loops.record("south")
    assert loops.detect() == "north -> south"


def test_varied_commands_are_not_a_loop() -> None:
    loops = LoopDetector()
    for command in ("north", "look", "east", "kill orc", "south"):
        loops.record(command)
    assert loops.detect() is None


def test_clearing_resets_detection() -> None:
    loops = LoopDetector()
    for _ in range(3):
        loops.record("north")
    loops.clear()
    assert loops.detect() is None
