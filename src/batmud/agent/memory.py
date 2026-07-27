"""Goals and notes that outlive a single decision.

The previous client sent 500 characters of recent output per request and
nothing else, so it could not pursue anything across two decisions. This holds
the current goal, a small stack of goals beneath it, and notes the planner
chose to keep, persisted as JSON between runs.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

MAX_NOTES = 200


@dataclass(frozen=True, slots=True)
class Note:
    """Something the planner asked to remember."""

    text: str
    created: float = field(default_factory=time.time)
    room: str = ""

    def describe(self) -> str:
        return f"{self.text} [{self.room}]" if self.room else self.text


@dataclass(slots=True)
class Memory:
    """Persistent goal stack and notes."""

    path: Path | None = None
    goal: str = ""
    stack: list[str] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)

    # --- goals --------------------------------------------------------------

    def set_goal(self, goal: str) -> None:
        """Replace the current goal."""
        goal = goal.strip()
        if not goal or goal == self.goal:
            return
        if self.goal:
            self.stack.append(self.goal)
            del self.stack[:-10]
        self.goal = goal

    def pop_goal(self) -> str:
        """Return to the previous goal, e.g. once a detour is finished."""
        self.goal = self.stack.pop() if self.stack else ""
        return self.goal

    # --- notes --------------------------------------------------------------

    def remember(self, text: str, room: str = "") -> Note | None:
        text = text.strip()
        if not text:
            return None
        if any(note.text == text for note in self.notes[-20:]):
            return None
        note = Note(text=text, room=room)
        self.notes.append(note)
        del self.notes[:-MAX_NOTES]
        return note

    def recent_notes(self, limit: int = 10) -> list[Note]:
        return self.notes[-limit:]

    def notes_for_room(self, room: str, limit: int = 5) -> list[Note]:
        if not room:
            return []
        return [note for note in self.notes if note.room == room][-limit:]

    def context(self, room: str = "", limit: int = 10) -> str:
        """The part of memory worth spending tokens on."""
        lines: list[str] = []
        if self.goal:
            lines.append(f"Current goal: {self.goal}")
        if self.stack:
            lines.append("Goals underneath: " + " < ".join(reversed(self.stack[-3:])))
        here = self.notes_for_room(room)
        if here:
            lines.append("Notes about this room:")
            lines.extend(f"  - {note.text}" for note in here)
        general = [note for note in self.recent_notes(limit) if note not in here]
        if general:
            lines.append("Recent notes:")
            lines.extend(f"  - {note.describe()}" for note in general)
        return "\n".join(lines)

    # --- persistence --------------------------------------------------------

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "goal": self.goal,
            "stack": self.stack,
            "notes": [asdict(note) for note in self.notes],
        }
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    @classmethod
    def load(cls, path: Path | None, default_goal: str = "") -> Memory:
        memory = cls(path=path, goal=default_goal)
        if path is None or not path.is_file():
            return memory
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            log.warning("could not read memory from %s: %s", path, error)
            return memory

        memory.goal = str(payload.get("goal") or default_goal)
        memory.stack = [str(item) for item in payload.get("stack", []) if item]
        memory.notes = [
            Note(
                text=str(entry.get("text", "")),
                created=float(entry.get("created", 0.0)),
                room=str(entry.get("room", "")),
            )
            for entry in payload.get("notes", [])
            if entry.get("text")
        ]
        return memory
