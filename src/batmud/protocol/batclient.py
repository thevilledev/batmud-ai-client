"""Incremental parser for BatMUD's BatClient control codes.

The server wraps out-of-band data in ``ESC<nn`` ... ``ESC>nn`` tags, optionally
separating an argument from a body with ``ESC|``. Tags nest, so an open tag
stack is maintained and the enclosing tags decide the style and message channel
of the text between them.

The parser is fed arbitrary chunks and keeps all of its state between calls, so
a tag, a line or an escape sequence split across two reads parses identically
to one that is not. This is the property the previous regex-over-chunks
approach lacked.

Protocol reference: https://www.bat.org/forum/lofiversion/index.php/t477.html
Reference implementation: https://git.sr.ht/~lotheac/bcproxy (parser.c)
"""

from __future__ import annotations

import dataclasses
import logging
from enum import Enum, auto

from .events import (
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
    Span,
    SpellEffect,
    Style,
    Target,
    UnknownTag,
    Vitals,
)

log = logging.getLogger(__name__)

ESC = "\x1b"

BC_ENABLE = b"\x1bbc 1\n"
"""Switches the server into BatClient mode. Consumed out of band, so it is safe
to send before the login prompt."""

TAGS_WITH_ARGUMENT: frozenset[int] = frozenset(
    {6, 10, 20, 21, 30, 31, 41, 42, 50, 51, 52, 53, 54, 60, 61, 62, 63, 64, 70, 99}
)
"""Codes whose opening tag carries a payload. Everything else wraps body text
directly, e.g. ``ESC<22boldESC>22``."""

PROMPT_CHANNEL = "spec_prompt"
"""Sent roughly once a second as a keepalive. Only a real IAC GA turns it into
a prompt; otherwise it is dropped so the display is not flooded."""

_MAX_ARGUMENT = 4096
"""Guard against an unterminated tag swallowing the rest of the session."""

_PARTY_STATUS_FLAGS = (
    "creator",
    "formation",
    "member",
    "entry",
    "following",
    "leader",
    "linkdead",
    "resting",
    "idle",
    "invisible",
    "dead",
    "stunned",
    "unconscious",
)


class _State(Enum):
    TEXT = auto()
    ESCAPE = auto()
    OPEN_CODE = auto()
    CLOSE_CODE = auto()
    ANSI = auto()


@dataclasses.dataclass(slots=True)
class _OpenTag:
    code: int
    prev_style: Style
    prev_channel: str | None
    reading_argument: bool
    argument: list[str] = dataclasses.field(default_factory=list)
    applied: bool = False


class BatClientParser:
    """Turns a character stream into :mod:`batmud.protocol.events` events."""

    def __init__(self) -> None:
        self._state = _State.TEXT
        self._stack: list[_OpenTag] = []
        self._spans: list[Span] = []
        self._chars: list[str] = []
        self._style = Style()
        self._ansi = Style()
        self._channel: str | None = None
        self._line_channel: str | None = None
        self._digits = ""
        self._ansi_buffer = ""
        self._events: list[Event] = []
        self._pending_prompt: str | None = None

    # --- public API ---------------------------------------------------------

    def feed(self, text: str) -> list[Event]:
        """Consume a chunk and return the events it completed."""
        for char in text:
            self._consume(char)
        return self._drain()

    def go_ahead(self) -> list[Event]:
        """Handle IAC GA: whatever text is buffered is a prompt."""
        self._flush_span()
        text = "".join(span.text for span in self._spans)
        if not text and self._pending_prompt is not None:
            text = self._pending_prompt
        self._spans = []
        self._line_channel = None
        self._pending_prompt = None
        self._events.append(Prompt(text))
        return self._drain()

    def flush(self) -> list[Event]:
        """Emit any buffered partial line, e.g. when the connection closes."""
        self._flush_span()
        if self._spans:
            self._emit_line()
        return self._drain()

    @property
    def open_tags(self) -> tuple[int, ...]:
        return tuple(tag.code for tag in self._stack)

    # --- character dispatch -------------------------------------------------

    def _consume(self, char: str) -> None:
        match self._state:
            case _State.TEXT:
                self._consume_text(char)
            case _State.ESCAPE:
                self._consume_escape(char)
            case _State.OPEN_CODE:
                self._consume_code(char, opening=True)
            case _State.CLOSE_CODE:
                self._consume_code(char, opening=False)
            case _State.ANSI:
                self._consume_ansi(char)

    def _consume_text(self, char: str) -> None:
        if char == ESC:
            self._state = _State.ESCAPE
        elif char == "\n":
            if self._reading_argument():
                self._finish_argument()
            self._emit_line()
        elif char == "\r":
            pass
        else:
            self._append(char)

    def _consume_escape(self, char: str) -> None:
        if char == "<":
            self._state = _State.OPEN_CODE
            self._digits = ""
        elif char == ">":
            self._state = _State.CLOSE_CODE
            self._digits = ""
        elif char == "|":
            self._state = _State.TEXT
            if self._reading_argument():
                self._finish_argument()
        elif char == "[":
            self._state = _State.ANSI
            self._ansi_buffer = ""
        else:
            # A bare ESC that is not part of a sequence we understand. Drop the
            # ESC and treat the following character normally.
            self._state = _State.TEXT
            self._consume_text(char)

    def _consume_code(self, char: str, *, opening: bool) -> None:
        if char.isdigit():
            self._digits += char
            if len(self._digits) == 2:
                code = int(self._digits)
                self._state = _State.TEXT
                if opening:
                    self._open_tag(code)
                else:
                    self._close_tag(code)
            return

        # Malformed tag: recover by replaying the characters as literal text.
        log.debug("malformed control code %r, recovering as text", self._digits + char)
        self._state = _State.TEXT
        for literal in ("<" if opening else ">", *self._digits):
            self._append(literal)
        self._consume_text(char)

    def _consume_ansi(self, char: str) -> None:
        self._ansi_buffer += char
        if "@" <= char <= "~":
            self._state = _State.TEXT
            if char == "m":
                self._apply_sgr(self._ansi_buffer[:-1])

    # --- text accumulation --------------------------------------------------

    def _reading_argument(self) -> bool:
        return bool(self._stack) and self._stack[-1].reading_argument

    def _append(self, char: str) -> None:
        if self._reading_argument():
            tag = self._stack[-1]
            if len(tag.argument) >= _MAX_ARGUMENT:
                log.warning("control code %02d argument overflow, closing it", tag.code)
                self._finish_argument()
                self._chars.append(char)
                return
            tag.argument.append(char)
            return
        if self._channel is not None and self._line_channel is None:
            self._line_channel = self._channel
        self._chars.append(char)

    def _effective_style(self) -> Style:
        base, top = self._ansi, self._style
        return Style(
            fg=top.fg or base.fg,
            bg=top.bg or base.bg,
            bold=top.bold or base.bold,
            italic=top.italic or base.italic,
            underline=top.underline or base.underline,
            blink=top.blink or base.blink,
            href=top.href,
            command=top.command,
        )

    def _flush_span(self) -> None:
        if not self._chars:
            return
        self._spans.append(Span("".join(self._chars), self._effective_style()))
        self._chars = []

    def _set_style(self, style: Style) -> None:
        if style == self._style:
            return
        self._flush_span()
        self._style = style

    def _emit_line(self) -> None:
        self._flush_span()
        channel = self._line_channel or self._channel
        line = Line(spans=tuple(self._spans), channel=channel)
        self._spans = []
        self._line_channel = None
        if channel == PROMPT_CHANNEL:
            self._pending_prompt = line.text
        else:
            self._events.append(line)

    def _drain(self) -> list[Event]:
        events, self._events = self._events, []
        return events

    # --- tag handling -------------------------------------------------------

    def _open_tag(self, code: int) -> None:
        if self._reading_argument():
            # A nested tag implies the parent's argument ended.
            self._finish_argument()
        tag = _OpenTag(
            code=code,
            prev_style=self._style,
            prev_channel=self._channel,
            reading_argument=code in TAGS_WITH_ARGUMENT,
        )
        self._stack.append(tag)
        if not tag.reading_argument:
            self._apply(tag, "")

    def _finish_argument(self) -> None:
        tag = self._stack[-1]
        tag.reading_argument = False
        self._apply(tag, "".join(tag.argument))

    def _close_tag(self, code: int) -> None:
        index = next(
            (i for i in reversed(range(len(self._stack))) if self._stack[i].code == code),
            None,
        )
        if index is None:
            # The server is documented to emit extraneous closing tags.
            log.debug("ignoring unmatched closing tag %02d", code)
            return
        while len(self._stack) > index:
            tag = self._stack.pop()
            if tag.reading_argument:
                tag.reading_argument = False
                self._apply(tag, "".join(tag.argument))
            self._set_style(tag.prev_style)
            self._channel = tag.prev_channel

    def _apply(self, tag: _OpenTag, argument: str) -> None:
        """Apply a tag's effect once its argument is known."""
        if tag.applied:
            return
        tag.applied = True
        code, arg = tag.code, argument.strip()

        match code:
            case 0:
                self._reset_all()
            case 5:
                self._events.append(ConnectionSucceeded())
            case 6:
                self._events.append(ConnectionFailed(argument.strip()))
            case 10:
                self._channel = arg or None
            case 11:
                self._events.append(ClearScreen(window=self._channel))
            case 20:
                self._set_style(dataclasses.replace(self._style, fg=_colour(arg)))
            case 21:
                self._set_style(dataclasses.replace(self._style, bg=_colour(arg)))
            case 22:
                self._set_style(dataclasses.replace(self._style, bold=True))
            case 23:
                self._set_style(dataclasses.replace(self._style, italic=True))
            case 24:
                self._set_style(dataclasses.replace(self._style, underline=True))
            case 25:
                self._set_style(dataclasses.replace(self._style, blink=True))
            case 29:
                self._set_style(Style())
                self._ansi = Style()
            case 30:
                self._set_style(dataclasses.replace(self._style, href=arg or None))
            case 31:
                self._set_style(dataclasses.replace(self._style, command=arg or None))
            case 40:
                self._events.append(ActionCleared())
            case 41 | 42:
                kind = ActionKind.SPELL if code == 41 else ActionKind.SKILL
                event = _parse_action(kind, arg)
                if event is not None:
                    self._events.append(event)
            case 50 | 51:
                vitals = _parse_vitals(arg)
                if vitals is not None:
                    self._events.append(vitals)
            case 52:
                info = _parse_player_info(arg)
                if info is not None:
                    self._events.append(info)
            case 53:
                amount = _to_int(arg)
                if amount is not None:
                    self._events.append(FreeExperience(amount))
            case 54:
                status = _parse_player_status(arg)
                if status is not None:
                    self._events.append(status)
            case 60:
                location = _parse_location(arg)
                if location is not None:
                    self._events.append(location)
            case 61:
                member = _parse_party_location(arg)
                if member is not None:
                    self._events.append(member)
            case 62:
                party = _parse_party_status(arg)
                if party is not None:
                    self._events.append(party)
            case 63:
                if arg:
                    self._events.append(PartyMemberLeft(arg))
            case 64:
                effect = _parse_spell_effect(arg)
                if effect is not None:
                    self._events.append(effect)
            case 70:
                self._events.append(_parse_target(arg))
            case 99:
                custom = _parse_custom(arg)
                if custom is not None:
                    self._events.append(custom)
            case _:
                self._events.append(UnknownTag(code, arg))

    def _reset_all(self) -> None:
        self._set_style(Style())
        self._stack.clear()
        self._ansi = Style()
        self._channel = None

    # --- ANSI fallback ------------------------------------------------------

    def _apply_sgr(self, parameters: str) -> None:
        """Translate ANSI SGR into the base style layer.

        BatClient mode supplies its own colour codes, but the pre-login banner
        and any non-BatClient session still use ANSI.
        """
        codes = [_to_int(part) or 0 for part in (parameters or "0").split(";")]
        style = self._ansi
        for code in codes:
            match code:
                case 0:
                    style = Style()
                case 1:
                    style = dataclasses.replace(style, bold=True)
                case 3:
                    style = dataclasses.replace(style, italic=True)
                case 4:
                    style = dataclasses.replace(style, underline=True)
                case 5:
                    style = dataclasses.replace(style, blink=True)
                case 22:
                    style = dataclasses.replace(style, bold=False)
                case 23:
                    style = dataclasses.replace(style, italic=False)
                case 24:
                    style = dataclasses.replace(style, underline=False)
                case 25:
                    style = dataclasses.replace(style, blink=False)
                case 39:
                    style = dataclasses.replace(style, fg=None)
                case 49:
                    style = dataclasses.replace(style, bg=None)
                case _ if 30 <= code <= 37:
                    style = dataclasses.replace(style, fg=_ANSI_COLOURS[code - 30])
                case _ if 90 <= code <= 97:
                    style = dataclasses.replace(style, fg=_ANSI_BRIGHT[code - 90])
                case _ if 40 <= code <= 47:
                    style = dataclasses.replace(style, bg=_ANSI_COLOURS[code - 40])
                case _ if 100 <= code <= 107:
                    style = dataclasses.replace(style, bg=_ANSI_BRIGHT[code - 100])
        if style != self._ansi:
            self._flush_span()
            self._ansi = style


_ANSI_COLOURS = (
    "000000",
    "800000",
    "008000",
    "808000",
    "000080",
    "800080",
    "008080",
    "c0c0c0",
)
_ANSI_BRIGHT = (
    "808080",
    "ff0000",
    "00ff00",
    "ffff00",
    "0000ff",
    "ff00ff",
    "00ffff",
    "ffffff",
)


# --- payload parsers --------------------------------------------------------
#
# The published specification and its own worked examples disagree on the field
# count of codes 51 and 52, so every parser below dispatches on how many fields
# actually arrived rather than trusting the documented arity.


def _to_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def _split_fields(argument: str) -> list[str]:
    """Split a payload into fields.

    The specification writes separators as ``%s: %s:`` while its examples use
    plain spaces, so a trailing colon is stripped from each field. Colons are
    not treated as separators outright because the party creation timestamp
    contains them (``Wed_Oct_31_15:57:52_2007``).
    """
    return [field.rstrip(":") for field in argument.split()]


def _ints(fields: list[str]) -> list[int] | None:
    values = [_to_int(field) for field in fields]
    if any(value is None for value in values):
        return None
    return [value for value in values if value is not None]


def _colour(argument: str) -> str | None:
    text = argument.strip().lstrip("#")
    if len(text) == 6 and all(char in "0123456789abcdefABCDEF" for char in text):
        return text.lower()
    return None


def _parse_vitals(argument: str) -> Vitals | None:
    values = _ints(_split_fields(argument))
    if values is None:
        return None
    match values:
        case [hp, max_hp, sp, max_sp, ep, max_ep]:
            return Vitals(hp, max_hp, sp, max_sp, ep, max_ep)
        case [hp, sp, ep]:
            return Vitals(hp=hp, sp=sp, ep=ep)
        case [hp, max_hp]:
            return Vitals(hp=hp, max_hp=max_hp)
        case _:
            return None


def _parse_player_info(argument: str) -> PlayerInfo | None:
    fields = _split_fields(argument)
    surname: str | None = None
    if len(fields) == 6:
        name, surname, race, level, gender, experience = fields
    elif len(fields) == 5:
        name, race, level, gender, experience = fields
    else:
        return None
    numbers = _ints([level, gender, experience])
    if numbers is None:
        return None
    return PlayerInfo(
        name=name,
        race=race,
        level=numbers[0],
        gender=numbers[1],
        experience=numbers[2],
        surname=surname,
    )


def _parse_player_status(argument: str) -> PlayerStatus | None:
    values = _ints(_split_fields(argument))
    if values is None or len(values) != 3:
        return None
    return PlayerStatus(bool(values[0]), bool(values[1]), bool(values[2]))


def _parse_location(argument: str) -> PlayerLocation | None:
    fields = _split_fields(argument)
    if len(fields) != 7:
        return None
    numbers = _ints([fields[2], fields[4], fields[5], fields[6]])
    if numbers is None:
        return None
    return PlayerLocation(
        name=fields[0],
        race=fields[1],
        gender=numbers[0],
        continent=fields[3],
        x=numbers[1],
        y=numbers[2],
        z=numbers[3],
    )


def _parse_party_location(argument: str) -> PartyMemberLocation | None:
    fields = _split_fields(argument)
    if len(fields) != 3:
        return None
    numbers = _ints(fields[1:])
    if numbers is None:
        return None
    return PartyMemberLocation(player=fields[0], x=numbers[0], y=numbers[1])


def _parse_party_status(argument: str) -> PartyMemberStatus | None:
    fields = _split_fields(argument)
    if len(fields) < 10:
        return None
    numbers = _ints(fields[2:10])
    if numbers is None:
        return None
    status = PartyMemberStatus(
        name=fields[0],
        race=fields[1],
        gender=numbers[0],
        level=numbers[1],
        hp=numbers[2],
        max_hp=numbers[3],
        sp=numbers[4],
        max_sp=numbers[5],
        ep=numbers[6],
        max_ep=numbers[7],
    )
    if len(fields) < 26:
        return status

    place = _ints(fields[11:13])
    flags = frozenset(
        name
        for name, value in zip(_PARTY_STATUS_FLAGS, fields[13:26], strict=False)
        if value != "0"
    )
    trailing = _ints(fields[26:29]) or [0, 0, 0]
    return dataclasses.replace(
        status,
        party_name=fields[10].replace("_", " "),
        place_x=place[0] if place else 0,
        place_y=place[1] if place else 0,
        flags=flags,
        party_experience=trailing[0],
        total_party_experience=trailing[1],
        party_seconds=trailing[2],
        party_created=fields[29].replace("_", " ") if len(fields) > 29 else "",
    )


def _parse_spell_effect(argument: str) -> SpellEffect | None:
    fields = _split_fields(argument)
    if len(fields) < 2:
        return None
    time_left = _to_int(fields[-1])
    if time_left is None:
        return None
    return SpellEffect(name=" ".join(fields[:-1]).rstrip("_"), time_left=time_left)


def _parse_action(kind: ActionKind, argument: str) -> ActionProgress | None:
    fields = _split_fields(argument)
    if len(fields) < 2:
        return None
    rounds = _to_int(fields[-1])
    if rounds is None:
        return None
    return ActionProgress(kind=kind, name=" ".join(fields[:-1]).rstrip("_"), rounds_left=rounds)


def _parse_target(argument: str) -> Target:
    fields = _split_fields(argument)
    if len(fields) < 2:
        return Target(name=None)
    health = _to_int(fields[-1]) or 0
    name = " ".join(fields[:-1])
    if name == "0":
        return Target(name=None)
    return Target(name=name, health_percent=health)


def _parse_custom(argument: str) -> CustomInfo | None:
    fields = argument.split(maxsplit=2)
    if len(fields) < 3:
        return None
    return CustomInfo(name=fields[1], value=fields[2], numeric=fields[0] == "1")
