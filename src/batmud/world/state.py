"""The client's model of the game.

Almost every field here is written directly from a BatClient control code, so
it is exactly what the server believes rather than what a regex guessed. Only
the room comes from text, and even that is corrected by the coordinates control
code 60 reports.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ..protocol.events import (
    ActionCleared,
    ActionProgress,
    ConnectionFailed,
    ConnectionSucceeded,
    CustomInfo,
    Disconnected,
    Event,
    FreeExperience,
    Line,
    PartyMemberLeft,
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
from .map import WorldMap
from .room import Room, RoomParser, normalise_direction

BATTLE_CHANNEL = "spec_battle"
COMBAT_TIMEOUT = 15.0
"""How long after the last battle message the client still considers itself in
combat, for the case where no target is set."""


@dataclass(slots=True)
class Vital:
    """A current/maximum pair."""

    current: int = 0
    maximum: int = 0

    @property
    def fraction(self) -> float:
        return self.current / self.maximum if self.maximum > 0 else 1.0

    @property
    def known(self) -> bool:
        return self.maximum > 0

    def __str__(self) -> str:
        return f"{self.current}/{self.maximum}" if self.known else str(self.current)


@dataclass(slots=True)
class WorldState:
    """Everything the client currently knows."""

    world_map: WorldMap | None = None
    clock: Callable[[], float] = time.monotonic

    hp: Vital = field(default_factory=Vital)
    sp: Vital = field(default_factory=Vital)
    ep: Vital = field(default_factory=Vital)

    name: str = ""
    surname: str | None = None
    race: str = ""
    level: int = 0
    experience: int = 0
    free_experience: int = 0
    gender: int | None = None

    unconscious: bool = False
    stunned: bool = False
    dead: bool = False

    continent: str = ""
    x: int = 0
    y: int = 0
    z: int = 0

    target: str | None = None
    target_health: int = 0
    effects: dict[str, int] = field(default_factory=dict)
    action: ActionProgress | None = None
    party: dict[str, PartyMemberStatus] = field(default_factory=dict)
    stats: dict[str, str] = field(default_factory=dict)

    room: Room | None = None
    previous_room_key: str | None = None
    last_prompt: str = ""
    recent_lines: list[str] = field(default_factory=list)
    max_recent_lines: int = 200

    connected: bool = False
    logged_in: bool = False
    login_failure: str = ""
    batclient_active: bool = False
    unknown_tags: set[int] = field(default_factory=set)

    _parser: RoomParser = field(default_factory=RoomParser)
    _pending_direction: str | None = None
    # None rather than 0.0: these are compared against ``clock()``, which is
    # monotonic and therefore relative to an arbitrary origin. On a freshly
    # booted machine a 0.0 sentinel reads as "a moment ago".
    _last_battle_at: float | None = None
    _last_event_at: float | None = None

    # --- derived ------------------------------------------------------------

    @property
    def in_combat(self) -> bool:
        if self.target is not None:
            return True
        if self._last_battle_at is None:
            return False
        return self.clock() - self._last_battle_at < COMBAT_TIMEOUT

    @property
    def busy(self) -> bool:
        """A spell or skill is mid-cast; sending now would waste the command."""
        return self.action is not None

    @property
    def incapacitated(self) -> bool:
        return self.unconscious or self.stunned or self.dead

    @property
    def coordinates(self) -> tuple[str, int, int, int] | None:
        return (self.continent, self.x, self.y, self.z) if self.continent else None

    @property
    def room_key(self) -> str | None:
        return self.room.key if self.room is not None else None

    @property
    def idle_seconds(self) -> float:
        if self._last_event_at is None:
            return 0.0
        return self.clock() - self._last_event_at

    # --- event application --------------------------------------------------

    def apply(self, event: Event) -> frozenset[str]:
        """Fold an event into the state, returning the names of what changed."""
        self._last_event_at = self.clock()
        match event:
            case Vitals():
                return self._apply_vitals(event)
            case PlayerInfo():
                return self._apply_player_info(event)
            case FreeExperience():
                self.free_experience = event.amount
                return frozenset({"experience"})
            case PlayerStatus():
                return self._apply_status(event)
            case PlayerLocation():
                return self._apply_location(event)
            case Target():
                return self._apply_target(event)
            case SpellEffect():
                return self._apply_effect(event)
            case ActionProgress():
                self.action = event
                return frozenset({"action"})
            case ActionCleared():
                changed = frozenset({"action"}) if self.action is not None else frozenset()
                self.action = None
                return changed
            case PartyMemberStatus():
                self.party[event.name] = event
                return frozenset({"party"})
            case PartyMemberLeft():
                self.party.pop(event.player, None)
                return frozenset({"party"})
            case CustomInfo():
                self.stats[event.name] = event.value
                return frozenset({"stats"})
            case Line():
                return self._apply_line(event)
            case Prompt():
                self.last_prompt = event.text
                return frozenset({"prompt"})
            case ConnectionSucceeded():
                self.logged_in = True
                self.login_failure = ""
                return frozenset({"login"})
            case ConnectionFailed():
                self.logged_in = False
                self.login_failure = event.reason
                return frozenset({"login"})
            case Disconnected():
                self.connected = False
                self.logged_in = False
                return frozenset({"connection"})
            case UnknownTag():
                self.unknown_tags.add(event.code)
                return frozenset()
            case _:
                return frozenset()

    def _apply_vitals(self, event: Vitals) -> frozenset[str]:
        self.batclient_active = True
        before = (self.hp.current, self.sp.current, self.ep.current)
        self.hp.current = event.hp
        if event.max_hp is not None:
            self.hp.maximum = event.max_hp
        if event.sp is not None:
            self.sp.current = event.sp
        if event.max_sp is not None:
            self.sp.maximum = event.max_sp
        if event.ep is not None:
            self.ep.current = event.ep
        if event.max_ep is not None:
            self.ep.maximum = event.max_ep
        after = (self.hp.current, self.sp.current, self.ep.current)
        return frozenset({"vitals"}) if before != after else frozenset()

    def _apply_player_info(self, event: PlayerInfo) -> frozenset[str]:
        self.batclient_active = True
        changed = {"player"}
        if event.level != self.level and self.level:
            changed.add("level")
        self.name = event.name
        self.surname = event.surname
        self.race = event.race
        self.level = event.level
        self.experience = event.experience
        self.gender = event.gender
        self.logged_in = True
        return frozenset(changed)

    def _apply_status(self, event: PlayerStatus) -> frozenset[str]:
        self.batclient_active = True
        before = (self.unconscious, self.stunned, self.dead)
        self.unconscious, self.stunned, self.dead = (
            event.unconscious,
            event.stunned,
            event.dead,
        )
        after = (self.unconscious, self.stunned, self.dead)
        return frozenset({"status"}) if before != after else frozenset()

    def _apply_location(self, event: PlayerLocation) -> frozenset[str]:
        self.batclient_active = True
        before = self.coordinates
        self.continent, self.x, self.y, self.z = event.coordinates
        self.logged_in = True
        return frozenset({"location"}) if before != self.coordinates else frozenset()

    def _apply_target(self, event: Target) -> frozenset[str]:
        self.batclient_active = True
        before = self.target
        self.target = event.name
        self.target_health = event.health_percent
        changed = {"target"}
        if (before is None) != (event.name is None):
            changed.add("combat")
        return frozenset(changed)

    def _apply_effect(self, event: SpellEffect) -> frozenset[str]:
        self.batclient_active = True
        if event.expired:
            self.effects.pop(event.name, None)
        else:
            self.effects[event.name] = event.time_left
        return frozenset({"effects"})

    def _apply_line(self, event: Line) -> frozenset[str]:
        text = event.text
        if event.channel == BATTLE_CHANNEL:
            self._last_battle_at = self.clock()

        self.recent_lines.append(text if event.channel is None else f"[{event.channel}] {text}")
        if len(self.recent_lines) > self.max_recent_lines:
            del self.recent_lines[: -self.max_recent_lines]

        room = self._parser.feed(text, event.channel, self.coordinates)
        if room is None:
            return frozenset()
        return self._enter_room(room)

    def _enter_room(self, room: Room) -> frozenset[str]:
        previous = self.room
        if previous is not None and previous.key == room.key:
            self.room = room
            return frozenset()

        self.previous_room_key = previous.key if previous is not None else None
        self.room = room
        if self.world_map is not None:
            self.world_map.record_room(room)
            if self.previous_room_key is not None and self._pending_direction is not None:
                self.world_map.record_move(
                    self.previous_room_key, self._pending_direction, room.key
                )
        self._pending_direction = None
        return frozenset({"room"})

    # --- outbound bookkeeping -----------------------------------------------

    def note_command(self, command: str) -> None:
        """Remember a movement so the next room can be linked to this one."""
        self._pending_direction = normalise_direction(command.strip().removeprefix("go "))

    def reset_room_parser(self) -> None:
        self._parser.reset()

    # --- reporting ----------------------------------------------------------

    def summary(self) -> str:
        """A compact snapshot for the planner and the status bar."""
        lines = [
            f"HP {self.hp} | SP {self.sp} | EP {self.ep}",
        ]
        if self.name:
            title = f"{self.name} the {self.race}, level {self.level}"
            lines.append(f"{title} ({self.experience:,} exp, {self.free_experience:,} free)")
        if self.coordinates is not None:
            lines.append(f"Position: {self.continent} ({self.x}, {self.y}, {self.z})")
        if self.room is not None:
            lines.append(f"Room: {self.room.describe()}")
        if self.target is not None:
            lines.append(f"Target: {self.target} at ~{self.target_health}% health")
        elif self.in_combat:
            lines.append("In combat (no target set)")
        if self.action is not None:
            lines.append(
                f"Busy: {self.action.kind.value} {self.action.name}"
                f" ({self.action.rounds_left} rounds left)"
            )
        flags = [
            name
            for name, value in (
                ("unconscious", self.unconscious),
                ("stunned", self.stunned),
                ("dead", self.dead),
            )
            if value
        ]
        if flags:
            lines.append("Status: " + ", ".join(flags))
        if self.effects:
            active = ", ".join(f"{name} ({left}s)" for name, left in sorted(self.effects.items()))
            lines.append(f"Effects: {active}")
        if self.party:
            members = ", ".join(
                f"{member.name} {member.hp}/{member.max_hp}" for member in self.party.values()
            )
            lines.append(f"Party: {members}")
        return "\n".join(lines)
