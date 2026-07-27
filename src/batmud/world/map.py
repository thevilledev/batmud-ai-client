"""Persistent world map.

Rooms and the moves between them are stored in SQLite so knowledge survives
restarts. Outdoor rooms are keyed by the exact coordinates control code 60
reports, which makes the world map reliable in a way that scraping room
descriptions never was.
"""

from __future__ import annotations

import sqlite3
import time
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .room import OPPOSITES, Room

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    key         TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    exits       TEXT NOT NULL DEFAULT '',
    continent   TEXT,
    x           INTEGER,
    y           INTEGER,
    z           INTEGER,
    visits      INTEGER NOT NULL DEFAULT 0,
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS edges (
    from_key  TEXT NOT NULL,
    direction TEXT NOT NULL,
    to_key    TEXT NOT NULL,
    inferred  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (from_key, direction)
);

CREATE INDEX IF NOT EXISTS edges_to ON edges (to_key);
CREATE INDEX IF NOT EXISTS rooms_coords ON rooms (continent, x, y, z);
"""


@dataclass(frozen=True, slots=True)
class MappedRoom:
    key: str
    title: str
    description: str
    exits: frozenset[str]
    coordinates: tuple[str, int, int, int] | None
    visits: int

    def describe(self) -> str:
        exits = ", ".join(sorted(self.exits)) or "none"
        return f"{self.title} (exits: {exits})"


class WorldMap:
    """A graph of rooms backed by SQLite."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.database = Path(path) if path is not None else None
        if self.database is not None:
            self.database.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.database) if self.database else ":memory:")
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> WorldMap:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # --- writing ------------------------------------------------------------

    def record_room(self, room: Room) -> str:
        """Insert or refresh a room and return its key."""
        now = time.time()
        continent, x, y, z = room.coordinates or (None, None, None, None)
        exits = ",".join(sorted(room.exits))
        self._db.execute(
            """
            INSERT INTO rooms (key, title, description, exits, continent, x, y, z,
                               visits, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                title       = excluded.title,
                description = excluded.description,
                exits       = excluded.exits,
                continent   = COALESCE(excluded.continent, rooms.continent),
                x           = COALESCE(excluded.x, rooms.x),
                y           = COALESCE(excluded.y, rooms.y),
                z           = COALESCE(excluded.z, rooms.z),
                visits      = rooms.visits + 1,
                last_seen   = excluded.last_seen
            """,
            (room.key, room.title, room.description, exits, continent, x, y, z, now, now),
        )
        self._db.commit()
        return room.key

    def record_move(self, from_key: str, direction: str, to_key: str) -> None:
        """Record an observed move, replacing any inferred edge."""
        self._db.execute(
            """
            INSERT INTO edges (from_key, direction, to_key, inferred) VALUES (?, ?, ?, 0)
            ON CONFLICT(from_key, direction) DO UPDATE SET to_key = excluded.to_key, inferred = 0
            """,
            (from_key, direction, to_key),
        )
        self._infer_reverse(from_key, direction, to_key)
        self._db.commit()

    def _infer_reverse(self, from_key: str, direction: str, to_key: str) -> None:
        """Assume the way back, but only where the destination admits that exit.

        MUD exits are not reliably symmetric, so the inferred flag lets an
        observed move overwrite this later.
        """
        opposite = OPPOSITES.get(direction)
        if opposite is None:
            return
        destination = self.get(to_key)
        if destination is None or opposite not in destination.exits:
            return
        self._db.execute(
            """
            INSERT INTO edges (from_key, direction, to_key, inferred) VALUES (?, ?, ?, 1)
            ON CONFLICT(from_key, direction) DO NOTHING
            """,
            (to_key, opposite, from_key),
        )

    # --- reading ------------------------------------------------------------

    def get(self, key: str) -> MappedRoom | None:
        row = self._db.execute("SELECT * FROM rooms WHERE key = ?", (key,)).fetchone()
        return _to_room(row) if row is not None else None

    def __len__(self) -> int:
        row = self._db.execute("SELECT COUNT(*) AS n FROM rooms").fetchone()
        return int(row["n"])

    def neighbours(self, key: str) -> dict[str, str]:
        rows = self._db.execute(
            "SELECT direction, to_key FROM edges WHERE from_key = ?", (key,)
        ).fetchall()
        return {row["direction"]: row["to_key"] for row in rows}

    def unexplored_exits(self, key: str) -> list[str]:
        """Exits the room advertises that no recorded move has used."""
        room = self.get(key)
        if room is None:
            return []
        known = self.neighbours(key)
        return sorted(direction for direction in room.exits if direction not in known)

    def find_by_title(self, fragment: str, limit: int = 10) -> list[MappedRoom]:
        rows = self._db.execute(
            "SELECT * FROM rooms WHERE title LIKE ? ORDER BY visits DESC LIMIT ?",
            (f"%{fragment}%", limit),
        ).fetchall()
        return [_to_room(row) for row in rows]

    def find_by_coordinates(self, continent: str, x: int, y: int, z: int = 0) -> MappedRoom | None:
        row = self._db.execute(
            "SELECT * FROM rooms WHERE continent = ? AND x = ? AND y = ? AND z = ?",
            (continent, x, y, z),
        ).fetchone()
        return _to_room(row) if row is not None else None

    # --- navigation ---------------------------------------------------------

    def path(self, from_key: str, to_key: str, *, max_nodes: int = 20_000) -> list[str] | None:
        """Shortest sequence of directions between two rooms."""
        if from_key == to_key:
            return []
        for path, key in self._walk(from_key, max_nodes=max_nodes):
            if key == to_key:
                return path
        return None

    def path_to_unexplored(
        self, from_key: str, *, max_nodes: int = 20_000
    ) -> tuple[list[str], str] | None:
        """Route to the nearest room with an unused exit.

        Returns the directions to walk and the direction to take on arrival.
        """
        if (direction := _first(self.unexplored_exits(from_key))) is not None:
            return [], direction
        for path, key in self._walk(from_key, max_nodes=max_nodes):
            if (direction := _first(self.unexplored_exits(key))) is not None:
                return path, direction
        return None

    def _walk(self, start: str, *, max_nodes: int) -> Iterator[tuple[list[str], str]]:
        """Breadth-first traversal yielding ``(directions, room_key)``."""
        seen = {start}
        queue: deque[tuple[list[str], str]] = deque([([], start)])
        while queue and len(seen) < max_nodes:
            path, key = queue.popleft()
            for direction, neighbour in sorted(self.neighbours(key).items()):
                if neighbour in seen:
                    continue
                seen.add(neighbour)
                step = ([*path, direction], neighbour)
                yield step
                queue.append(step)

    # --- reporting ----------------------------------------------------------

    def local_summary(self, key: str, *, radius: int = 2, limit: int = 12) -> str:
        """A short description of the surroundings for the planner's context."""
        room = self.get(key)
        if room is None:
            return "Current room is not mapped yet."

        lines = [f"You are at: {room.describe()}"]
        if room.coordinates is not None:
            continent, x, y, z = room.coordinates
            lines.append(f"World position: {continent} ({x}, {y}, {z})")

        unexplored = self.unexplored_exits(key)
        lines.append("Unexplored exits here: " + (", ".join(unexplored) or "none"))

        nearby: list[str] = []
        for path, neighbour_key in self._walk(key, max_nodes=limit * 8):
            if len(path) > radius:
                break
            neighbour = self.get(neighbour_key)
            if neighbour is not None:
                nearby.append(f"  {'.'.join(path)} -> {neighbour.title}")
            if len(nearby) >= limit:
                break
        if nearby:
            lines.append("Nearby rooms:")
            lines.extend(nearby)
        return "\n".join(lines)


def _first(values: list[str]) -> str | None:
    return values[0] if values else None


def _to_room(row: sqlite3.Row) -> MappedRoom:
    coordinates = None
    if row["continent"] is not None and row["x"] is not None:
        coordinates = (row["continent"], row["x"], row["y"], row["z"])
    exits = frozenset(part for part in row["exits"].split(",") if part)
    return MappedRoom(
        key=row["key"],
        title=row["title"],
        description=row["description"],
        exits=exits,
        coordinates=coordinates,
        visits=row["visits"],
    )
