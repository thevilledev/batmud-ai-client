"""Deterministic reactions that run before the planner.

These handle the things that need to happen immediately and identically every
time. They are ordered by priority and rate limited individually, so a reflex
firing does not turn into a loop. The planner is only consulted when no reflex
applies, which keeps latency and token spend off the critical path for the
decisions that never needed a model.

Which of these may fire on their own is decided by BatMUD's ``help robot``, not
by convenience. It explicitly permits triggers that "just cause you to appear
non-idle", so anti-idle is marked ``auto``. It explicitly forbids triggers that
"cause you to move around in the mud (in any way)", giving fleeing as the
example, so the escape and retreat reflexes always go through approval even
though they are the ones you would most want to be instant.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from ..config import Settings
from ..world.state import WorldState
from .safety import Decision, Source

_PRESS_RETURN = re.compile(r"press\s+(return|enter)\s+to\s+continue", re.IGNORECASE)
_PAGER = re.compile(r"(--\s*more\s*--|\[\s*more\s*\]|press\s+space|<return>)", re.IGNORECASE)


@dataclass(slots=True)
class ReflexContext:
    """Everything a reflex is allowed to look at."""

    state: WorldState
    settings: Settings
    prompt: str = ""
    changed: frozenset[str] = frozenset()
    now: float = 0.0


@dataclass(frozen=True, slots=True)
class Reflex:
    """A named rule. Lower ``priority`` runs first."""

    name: str
    priority: int
    apply: Callable[[ReflexContext], Decision | None]
    cooldown: float = 0.0


def _decision(command: str, reason: str, *, auto: bool = False) -> Decision:
    return Decision(command=command, source=Source.REFLEX, reason=reason, auto=auto)


def acknowledge_pager(context: ReflexContext) -> Decision | None:
    """Answer a pager or 'press return' prompt so output keeps flowing.

    Approved automatically even in co-pilot mode: this only advances the
    display and asking a human to confirm it every time would be noise.
    """
    prompt = context.prompt
    if not prompt:
        return None
    if _PRESS_RETURN.search(prompt) or _PAGER.search(prompt):
        return _decision("", "acknowledging pager prompt", auto=True)
    return None


def escape_when_critical(context: ReflexContext) -> Decision | None:
    """Propose leaving when health is critical, without asking the model.

    Proposed rather than sent: automatic movement is against the rules, so in
    co-pilot mode this still waits for you to press Enter.
    """
    state, safety = context.state, context.settings.safety
    if not state.hp.known or state.dead:
        return None
    if state.hp.fraction > safety.hp_emergency_fraction:
        return None
    command = safety.emergency_commands[0] if safety.emergency_commands else safety.retreat_command
    return _decision(
        command,
        f"health critical at {state.hp} "
        f"({state.hp.fraction:.0%} <= {safety.hp_emergency_fraction:.0%})",
    )


def retreat_when_losing(context: ReflexContext) -> Decision | None:
    """Propose disengaging when a fight is going badly."""
    state, safety = context.state, context.settings.safety
    if not state.hp.known or not state.in_combat:
        return None
    if state.hp.fraction > safety.hp_retreat_fraction:
        return None
    return _decision(
        safety.retreat_command,
        f"retreating at {state.hp} ({state.hp.fraction:.0%} in combat)",
    )


def anti_idle(context: ReflexContext) -> Decision | None:
    """Send something harmless before the game considers the character idle.

    Sent without approval: ``help robot`` lists appearing non-idle among the
    triggers that are allowed.
    """
    agent = context.settings.agent
    if not agent.anti_idle:
        return None
    idle = context.state.idle_seconds
    if idle < agent.anti_idle_seconds:
        return None
    return _decision("look", f"idle for {idle:.0f}s", auto=True)


DEFAULT_REFLEXES: tuple[Reflex, ...] = (
    Reflex("pager", 10, acknowledge_pager),
    Reflex("critical-health", 20, escape_when_critical, cooldown=5.0),
    Reflex("retreat", 30, retreat_when_losing, cooldown=8.0),
    Reflex("anti-idle", 90, anti_idle, cooldown=60.0),
)


class ReflexEngine:
    """Evaluates reflexes in priority order, respecting per-rule cooldowns."""

    def __init__(
        self,
        settings: Settings,
        reflexes: Iterable[Reflex] = DEFAULT_REFLEXES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.clock = clock
        self.reflexes = sorted(reflexes, key=lambda reflex: reflex.priority)
        self._fired: dict[str, float] = {}
        self.last_fired: str = ""

    def should_act(self, state: WorldState) -> bool:
        """Whether it is worth issuing any command at all right now.

        Commands sent mid-cast are discarded by the game, and a stunned or
        unconscious character cannot act, so both are simply skipped. Control
        codes 41, 42 and 54 make this knowable rather than guessed.
        """
        if state.busy:
            return False
        return not (state.stunned or state.unconscious)

    def evaluate(
        self, state: WorldState, prompt: str = "", changed: frozenset[str] = frozenset()
    ) -> Decision | None:
        now = self.clock()
        context = ReflexContext(
            state=state, settings=self.settings, prompt=prompt, changed=changed, now=now
        )
        for reflex in self.reflexes:
            if reflex.cooldown and now - self._fired.get(reflex.name, -1e9) < reflex.cooldown:
                continue
            decision = reflex.apply(context)
            if decision is None:
                continue
            self._fired[reflex.name] = now
            self.last_fired = reflex.name
            return decision
        return None


@dataclass(slots=True)
class LoopDetector:
    """Notices when the same commands keep being issued to no effect.

    This replaces the previous client's pattern matcher, which silently
    substituted a different command behind the model's back. Here a detected
    loop is reported to the planner as feedback so it can choose differently
    with the reason in hand.
    """

    window: int = 8
    repeats: int = 3
    _history: list[str] = field(default_factory=list)

    def record(self, command: str) -> None:
        self._history.append(command.strip().lower())
        if len(self._history) > self.window:
            del self._history[: -self.window]

    def clear(self) -> None:
        self._history.clear()

    def detect(self) -> str | None:
        """Describe the loop the recent commands form, if any."""
        for length in (1, 2, 3):
            needed = length * self.repeats
            if len(self._history) < needed:
                continue
            tail = self._history[-needed:]
            pattern = tail[:length]
            if all(tail[start : start + length] == pattern for start in range(0, needed, length)):
                return " -> ".join(pattern)
        return None
