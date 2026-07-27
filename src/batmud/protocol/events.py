"""Typed events produced by the protocol layer.

Everything the client knows about the game arrives as one of these. Events
originating from BatClient control codes carry authoritative server data; the
``Line`` and ``Prompt`` events carry ordinary text for display and for the
fallback parsers used when control codes are unavailable.

Control code reference:
https://www.bat.org/forum/lofiversion/index.php/t477.html
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# --- text styling -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Style:
    """Text attributes accumulated from the enclosing control code tags."""

    fg: str | None = None
    bg: str | None = None
    bold: bool = False
    italic: bool = False
    underline: bool = False
    blink: bool = False
    href: str | None = None
    command: str | None = None


DEFAULT_STYLE = Style()


@dataclass(frozen=True, slots=True)
class Span:
    """A run of text sharing a single style."""

    text: str
    style: Style = DEFAULT_STYLE


# --- events -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Line:
    """A complete line of game text.

    ``channel`` is set from control code 10 and is either a communication
    channel (``chan_sales``) or a game message class (``spec_battle``,
    ``spec_map``, ``spec_spell``, ``spec_skill``, ``spec_prompt``).
    """

    spans: tuple[Span, ...]
    channel: str | None = None

    @property
    def text(self) -> str:
        return "".join(span.text for span in self.spans)

    def __bool__(self) -> bool:
        return bool(self.text)


@dataclass(frozen=True, slots=True)
class Prompt:
    """The server signalled it is ready for input (IAC GA)."""

    text: str = ""


@dataclass(frozen=True, slots=True)
class Vitals:
    """Codes 50 and 51: health, spell and endurance points."""

    hp: int
    max_hp: int | None = None
    sp: int | None = None
    max_sp: int | None = None
    ep: int | None = None
    max_ep: int | None = None


@dataclass(frozen=True, slots=True)
class PlayerInfo:
    """Code 52: identity and experience."""

    name: str
    race: str
    level: int
    experience: int
    surname: str | None = None
    gender: int | None = None


@dataclass(frozen=True, slots=True)
class FreeExperience:
    """Code 53: unspent experience."""

    amount: int


@dataclass(frozen=True, slots=True)
class PlayerStatus:
    """Code 54: incapacitation flags."""

    unconscious: bool
    stunned: bool
    dead: bool

    @property
    def incapacitated(self) -> bool:
        return self.unconscious or self.stunned or self.dead


@dataclass(frozen=True, slots=True)
class PlayerLocation:
    """Code 60: position on the world map."""

    name: str
    race: str
    gender: int
    continent: str
    x: int
    y: int
    z: int

    @property
    def coordinates(self) -> tuple[str, int, int, int]:
        return (self.continent, self.x, self.y, self.z)


@dataclass(frozen=True, slots=True)
class PartyMemberLocation:
    """Code 61: a party member's formation position."""

    player: str
    x: int
    y: int


@dataclass(frozen=True, slots=True)
class PartyMemberStatus:
    """Code 62: a full party member update."""

    name: str
    race: str
    gender: int
    level: int
    hp: int
    max_hp: int
    sp: int
    max_sp: int
    ep: int
    max_ep: int
    party_name: str = ""
    place_x: int = 0
    place_y: int = 0
    flags: frozenset[str] = frozenset()
    party_experience: int = 0
    total_party_experience: int = 0
    party_seconds: int = 0
    party_created: str = ""


@dataclass(frozen=True, slots=True)
class PartyMemberLeft:
    """Code 63."""

    player: str


@dataclass(frozen=True, slots=True)
class SpellEffect:
    """Code 64: a status-affecting spell.

    ``time_left`` of 0 means the effect ended; -1 means the effect is measured
    in something other than time.
    """

    name: str
    time_left: int

    @property
    def expired(self) -> bool:
        return self.time_left == 0


class ActionKind(StrEnum):
    SPELL = "spell"
    SKILL = "skill"


@dataclass(frozen=True, slots=True)
class ActionProgress:
    """Codes 41 and 42: a spell or skill is being performed.

    ``rounds_left`` of 0 means the duration is unknown, not that it finished;
    completion is signalled by ``ActionCleared``.
    """

    kind: ActionKind
    name: str
    rounds_left: int


@dataclass(frozen=True, slots=True)
class ActionCleared:
    """Code 40: the spell/skill progress indicator was cleared."""


@dataclass(frozen=True, slots=True)
class Target:
    """Code 70: the current target and its health, to 5% accuracy.

    A name of ``0`` clears the target, which is normalised here to ``None``.
    """

    name: str | None
    health_percent: int = 0


@dataclass(frozen=True, slots=True)
class ConnectionSucceeded:
    """Code 05: login accepted."""


@dataclass(frozen=True, slots=True)
class ConnectionFailed:
    """Code 06: login rejected, with the server's reason."""

    reason: str


@dataclass(frozen=True, slots=True)
class ClearScreen:
    """Code 11: clear a window. ``window`` comes from an enclosing code 10."""

    window: str | None = None


@dataclass(frozen=True, slots=True)
class CustomInfo:
    """Code 99: arbitrary key/value relay, e.g. ``1 dex 300``."""

    name: str
    value: str
    numeric: bool = False


@dataclass(frozen=True, slots=True)
class UnknownTag:
    """A control code the parser does not model, kept for diagnostics."""

    code: int
    argument: str = ""


@dataclass(frozen=True, slots=True)
class Disconnected:
    """The transport closed."""

    reason: str = ""


Event = (
    Line
    | Prompt
    | Vitals
    | PlayerInfo
    | FreeExperience
    | PlayerStatus
    | PlayerLocation
    | PartyMemberLocation
    | PartyMemberStatus
    | PartyMemberLeft
    | SpellEffect
    | ActionProgress
    | ActionCleared
    | Target
    | ConnectionSucceeded
    | ConnectionFailed
    | ClearScreen
    | CustomInfo
    | UnknownTag
    | Disconnected
)
