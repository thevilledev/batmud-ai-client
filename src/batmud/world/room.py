"""Room and exit parsing.

Rooms are the one part of the game state the server does not hand over
structurally, so this is the only place text scraping remains. Everything it
produces is treated as a hint that the map layer can correct, rather than as
authoritative state.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

DIRECTIONS: tuple[str, ...] = (
    "north",
    "northeast",
    "east",
    "southeast",
    "south",
    "southwest",
    "west",
    "northwest",
    "up",
    "down",
    "in",
    "out",
)

ABBREVIATIONS: dict[str, str] = {
    "n": "north",
    "ne": "northeast",
    "e": "east",
    "se": "southeast",
    "s": "south",
    "sw": "southwest",
    "w": "west",
    "nw": "northwest",
    "u": "up",
    "d": "down",
}

SHORT_FORM: dict[str, str] = {
    "north": "n",
    "northeast": "ne",
    "east": "e",
    "southeast": "se",
    "south": "s",
    "southwest": "sw",
    "west": "w",
    "northwest": "nw",
    "up": "u",
    "down": "d",
    "in": "in",
    "out": "out",
}

OPPOSITES: dict[str, str] = {
    "north": "south",
    "south": "north",
    "east": "west",
    "west": "east",
    "northeast": "southwest",
    "southwest": "northeast",
    "northwest": "southeast",
    "southeast": "northwest",
    "up": "down",
    "down": "up",
    "in": "out",
    "out": "in",
}

# Channels that never contain room text.
_NON_ROOM_CHANNELS = ("spec_battle", "spec_spell", "spec_skill", "spec_news", "spec_prompt")

_EXITS_LINE = re.compile(
    r"""
    (?:
        there\s+(?:is|are)\s+(?P<count>no|one|two|three|four|five|six|seven|eight|nine|\d+)?
        \s*obvious\s+exits?
      | obvious\s+exits?
      | you\s+see\s+exits?
      | exits?
    )
    \s*[:.]?\s*
    (?P<exits>[^.\n]*)
    """,
    re.IGNORECASE | re.VERBOSE,
)

_DIRECTION_WORD = re.compile(
    r"\b(north(?:east|west)?|south(?:east|west)?|east|west|up|down|out|in|ne|nw|se|sw|[nsewud])\b",
    re.IGNORECASE,
)


def normalise_direction(word: str) -> str | None:
    """Map any spelling of a direction onto its canonical long form."""
    text = word.strip().lower()
    if text in DIRECTIONS:
        return text
    return ABBREVIATIONS.get(text)


def parse_exits(text: str) -> frozenset[str] | None:
    """Extract the exit set from a line, or ``None`` if it is not an exits line."""
    match = _EXITS_LINE.search(text)
    if match is None:
        return None
    if (match.group("count") or "").lower() == "no":
        return frozenset()
    body = match.group("exits") or ""
    found = {
        direction
        for word in _DIRECTION_WORD.findall(body)
        if (direction := normalise_direction(word)) is not None
    }
    if not found and (match.group("count") or "").lower() not in {"no", ""}:
        return frozenset()
    return frozenset(found) if found else frozenset()


@dataclass(frozen=True, slots=True)
class Room:
    """A room as described by the game."""

    title: str
    description: str = ""
    exits: frozenset[str] = frozenset()
    coordinates: tuple[str, int, int, int] | None = None

    @property
    def key(self) -> str:
        """Stable identity for this room.

        Outdoor rooms are identified by their world coordinates, which the
        server reports exactly via control code 60. Indoor rooms have no
        coordinates, so they fall back to a hash of what the game printed.
        """
        if self.coordinates is not None:
            continent, x, y, z = self.coordinates
            return f"{continent}:{x}:{y}:{z}"
        payload = "\n".join([self.title, self.description, ",".join(sorted(self.exits))])
        return "hash:" + hashlib.blake2b(payload.encode("utf-8"), digest_size=12).hexdigest()

    @property
    def exit_list(self) -> list[str]:
        return sorted(self.exits, key=DIRECTIONS.index)

    def describe(self) -> str:
        exits = ", ".join(self.exit_list) or "none"
        return f"{self.title} (exits: {exits})"


@dataclass(slots=True)
class RoomParser:
    """Assembles rooms from the line stream.

    A room is considered complete when its exits line arrives; the lines
    gathered since the previous room become its title and description.
    """

    _lines: list[str] = field(default_factory=list)
    _limit: int = 40

    def reset(self) -> None:
        self._lines.clear()

    def feed(
        self,
        text: str,
        channel: str | None = None,
        coordinates: tuple[str, int, int, int] | None = None,
    ) -> Room | None:
        """Consume a line, returning a room when one is complete."""
        if channel is not None and (channel.startswith("chan_") or channel in _NON_ROOM_CHANNELS):
            return None

        stripped = text.strip()
        exits = parse_exits(stripped)
        if exits is not None:
            room = self._build(exits, coordinates)
            self._lines.clear()
            return room

        if not stripped:
            # A blank line ends the previous room's block rather than joining
            # it to the next one.
            self._lines.clear()
            return None

        self._lines.append(stripped)
        if len(self._lines) > self._limit:
            self._lines.pop(0)
        return None

    def _build(
        self, exits: frozenset[str], coordinates: tuple[str, int, int, int] | None
    ) -> Room | None:
        if not self._lines:
            return None
        title = self._lines[0].rstrip(".")
        description = " ".join(self._lines[1:])
        return Room(title=title, description=description, exits=exits, coordinates=coordinates)
