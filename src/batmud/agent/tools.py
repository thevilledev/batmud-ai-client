"""The actions the planner is allowed to take.

The planner never emits a bare command string. It calls a tool, the tool is
validated against the world model, and an invalid call comes back as an error
the model can read and correct on the next turn.

This is the deliberate opposite of the previous client, which quietly rewrote
the model's command (substituting a different exit, inserting a ``peer``) so
the model never learned that what it asked for was impossible.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..world.map import WorldMap
from ..world.room import DIRECTIONS, SHORT_FORM, normalise_direction
from ..world.state import WorldState
from .memory import Memory


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """What running a tool produced.

    A ``command`` ends the planning turn by giving the session something to
    send. ``feedback`` alone means the model should think again with the extra
    information.
    """

    command: str | None = None
    feedback: str = ""
    ok: bool = True

    @classmethod
    def error(cls, feedback: str) -> ToolOutcome:
        return cls(feedback=feedback, ok=False)


@dataclass(slots=True)
class ToolContext:
    """What tools may read and write."""

    state: WorldState
    memory: Memory
    world_map: WorldMap | None = None


ToolFunction = Callable[[dict[str, Any], ToolContext], ToolOutcome]


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    run: ToolFunction

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _string(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


# --- movement ---------------------------------------------------------------


def _move(arguments: dict[str, Any], context: ToolContext) -> ToolOutcome:
    raw = str(arguments.get("direction", "")).strip()
    direction = normalise_direction(raw)
    if direction is None:
        return ToolOutcome.error(
            f"{raw!r} is not a direction. Valid directions: {', '.join(DIRECTIONS)}."
        )

    room = context.state.room
    if room is not None and room.exits and direction not in room.exits:
        available = ", ".join(room.exit_list) or "none"
        return ToolOutcome.error(
            f"There is no {direction} exit here. Exits from {room.title!r}: {available}."
        )
    return ToolOutcome(command=SHORT_FORM.get(direction, direction))


def _travel_to(arguments: dict[str, Any], context: ToolContext) -> ToolOutcome:
    destination = str(arguments.get("destination", "")).strip()
    if not destination:
        return ToolOutcome.error("travel_to needs a destination.")
    world_map, room_key = context.world_map, context.state.room_key
    if world_map is None or room_key is None:
        return ToolOutcome.error("The map is not available yet; move manually for now.")

    matches = world_map.find_by_title(destination)
    if not matches:
        return ToolOutcome.error(
            f"No mapped room matches {destination!r}. Use explore to find new ground."
        )

    for candidate in matches:
        path = world_map.path(room_key, candidate.key)
        if path is None:
            continue
        if not path:
            return ToolOutcome.error(f"You are already at {candidate.title!r}.")
        step = path[0]
        return ToolOutcome(
            command=SHORT_FORM.get(step, step),
            feedback=(
                f"Heading to {candidate.title!r}: {len(path)} steps "
                f"({'.'.join(path)}). First step {step}."
            ),
        )
    return ToolOutcome.error(f"{matches[0].title!r} is mapped but no route from here is known yet.")


def _explore(_: dict[str, Any], context: ToolContext) -> ToolOutcome:
    world_map, room_key = context.world_map, context.state.room_key
    if world_map is None or room_key is None:
        return ToolOutcome.error("The map is not available yet.")

    route = world_map.path_to_unexplored(room_key)
    if route is None:
        return ToolOutcome.error(
            "Every mapped exit has been used. Pick a direction with move instead."
        )
    path, direction = route
    step = path[0] if path else direction
    detail = (
        f"Unexplored exit {direction} is {len(path)} steps away."
        if path
        else f"Taking the unexplored {direction} exit from here."
    )
    return ToolOutcome(command=SHORT_FORM.get(step, step), feedback=detail)


# --- combat -----------------------------------------------------------------


def _attack(arguments: dict[str, Any], context: ToolContext) -> ToolOutcome:
    target = str(arguments.get("target", "")).strip()
    if not target:
        if context.state.target is None:
            return ToolOutcome.error("attack needs a target, and none is currently set.")
        target = context.state.target
    return ToolOutcome(command=f"kill {target}")


# --- bookkeeping ------------------------------------------------------------


def _remember(arguments: dict[str, Any], context: ToolContext) -> ToolOutcome:
    text = str(arguments.get("note", "")).strip()
    if not text:
        return ToolOutcome.error("remember needs a note.")
    room = context.state.room.title if context.state.room is not None else ""
    note = context.memory.remember(text, room=room)
    if note is None:
        return ToolOutcome(feedback="That note is already recorded.")
    context.memory.save()
    return ToolOutcome(feedback=f"Noted: {text}")


def _set_goal(arguments: dict[str, Any], context: ToolContext) -> ToolOutcome:
    goal = str(arguments.get("goal", "")).strip()
    if not goal:
        return ToolOutcome.error("set_goal needs a goal.")
    context.memory.set_goal(goal)
    context.memory.save()
    return ToolOutcome(feedback=f"Goal is now: {goal}")


def _recall(_: dict[str, Any], context: ToolContext) -> ToolOutcome:
    room = context.state.room.title if context.state.room is not None else ""
    notes = context.memory.context(room=room)
    return ToolOutcome(feedback=notes or "Nothing remembered yet.")


def _describe_surroundings(_: dict[str, Any], context: ToolContext) -> ToolOutcome:
    if context.world_map is None or context.state.room_key is None:
        return ToolOutcome(feedback="The map has nothing recorded yet.")
    return ToolOutcome(feedback=context.world_map.local_summary(context.state.room_key))


def _wait(arguments: dict[str, Any], _: ToolContext) -> ToolOutcome:
    reason = str(arguments.get("reason", "")).strip() or "no action needed"
    return ToolOutcome(feedback=f"Waiting: {reason}", command=None)


def _raw_command(arguments: dict[str, Any], _: ToolContext) -> ToolOutcome:
    """Escape hatch for the very large surface of commands BatMUD has.

    Not validated beyond the safety guard every command passes through, so it
    is described to the model as a last resort.
    """
    command = str(arguments.get("command", "")).strip()
    if not command:
        return ToolOutcome.error("raw_command needs a command.")
    if "\n" in command or "\r" in command:
        return ToolOutcome.error("A command cannot contain a newline.")
    return ToolOutcome(command=command)


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="move",
        description=(
            "Walk one step in a compass direction. Rejected if the room does "
            "not have that exit, so read the exits before calling it."
        ),
        parameters={
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": list(DIRECTIONS),
                    "description": "The exit to take.",
                }
            },
            "required": ["direction"],
        },
        run=_move,
    ),
    Tool(
        name="travel_to",
        description=(
            "Walk towards a room already on the map, matched by part of its "
            "name. Returns the first step of the route."
        ),
        parameters={
            "type": "object",
            "properties": {"destination": _string("Part of the room's name.")},
            "required": ["destination"],
        },
        run=_travel_to,
    ),
    Tool(
        name="explore",
        description="Head for the nearest exit that has never been taken.",
        parameters={"type": "object", "properties": {}},
        run=_explore,
    ),
    Tool(
        name="attack",
        description="Attack a creature. Defaults to the current target.",
        parameters={
            "type": "object",
            "properties": {"target": _string("The creature to attack.")},
        },
        run=_attack,
    ),
    Tool(
        name="look_around",
        description="Report what the map knows about the current surroundings.",
        parameters={"type": "object", "properties": {}},
        run=_describe_surroundings,
    ),
    Tool(
        name="remember",
        description="Store a durable note, for example a shop location or a dangerous room.",
        parameters={
            "type": "object",
            "properties": {"note": _string("What to remember.")},
            "required": ["note"],
        },
        run=_remember,
    ),
    Tool(
        name="recall",
        description="Read back the current goal and stored notes.",
        parameters={"type": "object", "properties": {}},
        run=_recall,
    ),
    Tool(
        name="set_goal",
        description="Replace the current goal. The previous goal is kept underneath.",
        parameters={
            "type": "object",
            "properties": {"goal": _string("The new goal.")},
            "required": ["goal"],
        },
        run=_set_goal,
    ),
    Tool(
        name="wait",
        description="Do nothing this turn, for example while resting or mid-fight.",
        parameters={
            "type": "object",
            "properties": {"reason": _string("Why no action is needed.")},
        },
        run=_wait,
    ),
    Tool(
        name="raw_command",
        description=(
            "Send any other BatMUD command verbatim. Use this for shopping, "
            "eating, resting, skills and spells. Last resort for movement."
        ),
        parameters={
            "type": "object",
            "properties": {"command": _string("The command to send.")},
            "required": ["command"],
        },
        run=_raw_command,
    ),
)

TOOLS_BY_NAME: dict[str, Tool] = {tool.name: tool for tool in TOOLS}


def schemas() -> list[dict[str, Any]]:
    return [tool.schema() for tool in TOOLS]


def run_tool(name: str, arguments: dict[str, Any], context: ToolContext) -> ToolOutcome:
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        known = ", ".join(sorted(TOOLS_BY_NAME))
        return ToolOutcome.error(f"There is no tool called {name!r}. Available tools: {known}.")
    return tool.run(arguments, context)
