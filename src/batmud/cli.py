"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .config import Settings, load_settings
from .protocol.telnet import PLAIN_PORT, TLS_PORT
from .session.login import LoginMode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="batmud",
        description="An AI co-pilot for BatMUD.",
        epilog=(
            "Co-pilot mode is the default: the agent proposes commands and you "
            "approve them. BatMUD's 'help robot' forbids letting a script play "
            "while you are not present, so --autonomous is opt-in."
        ),
    )
    parser.add_argument("--version", action="version", version=f"batmud {__version__}")
    parser.add_argument("-c", "--config", type=Path, help="path to a TOML config file")

    session = parser.add_argument_group("session")
    session.add_argument(
        "--mode",
        choices=[mode.value for mode in LoginMode],
        default=LoginMode.LOGIN.value,
        help="log in with an existing character or create a new one (default: login)",
    )
    session.add_argument("--character", help="character name")
    session.add_argument("--host", help="server hostname")
    session.add_argument(
        "--port", type=int, help=f"server port ({TLS_PORT} TLS, {PLAIN_PORT} plain)"
    )
    session.add_argument(
        "--no-tls",
        action="store_true",
        help=f"connect in plaintext, implying port {PLAIN_PORT} unless --port is given",
    )
    session.add_argument(
        "--no-batclient",
        action="store_true",
        help="do not enable the BatClient control code protocol (degrades to text parsing)",
    )

    agent = parser.add_argument_group("agent")
    agent.add_argument("--model", help="model slug, e.g. anthropic/claude-sonnet-4")
    agent.add_argument("--goal", help="the objective given to the planner")
    agent.add_argument(
        "--autonomous",
        action="store_true",
        help="send commands without approval (against BatMUD's rules for unattended play)",
    )
    agent.add_argument(
        "--max-spend", type=float, metavar="USD", help="stop planning after this much"
    )
    agent.add_argument("--max-requests", type=int, help="stop planning after this many requests")

    output = parser.add_argument_group("output")
    output.add_argument("--data-dir", type=Path, help="where the map and memory are kept")
    output.add_argument("--log-file", type=Path, help="also write logs to this file")
    output.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="log verbosity (default: INFO)",
    )
    output.add_argument(
        "--print-config",
        action="store_true",
        help="show the resolved configuration and exit",
    )
    return parser


def overrides_from(args: argparse.Namespace) -> dict[str, Any]:
    """Turn parsed flags into a nested settings overlay."""
    connection: dict[str, Any] = {}
    if args.host:
        connection["host"] = args.host
    if args.no_tls:
        connection["tls"] = False
        connection["port"] = args.port if args.port else PLAIN_PORT
    elif args.port:
        connection["port"] = args.port
    if args.no_batclient:
        connection["batclient"] = False

    character: dict[str, Any] = {}
    if args.character:
        character["name"] = args.character

    llm: dict[str, Any] = {}
    if args.model:
        llm["model"] = args.model
    if args.max_spend is not None:
        llm["max_spend_usd"] = args.max_spend
    if args.max_requests is not None:
        llm["max_requests"] = args.max_requests

    agent: dict[str, Any] = {}
    if args.goal:
        agent["goal"] = args.goal
    if args.autonomous:
        agent["autonomous"] = True

    overrides: dict[str, Any] = {}
    for key, value in (
        ("connection", connection),
        ("character", character),
        ("llm", llm),
        ("agent", agent),
    ):
        if value:
            overrides[key] = value
    if args.data_dir:
        overrides["data_dir"] = args.data_dir
    if args.log_file:
        overrides["log_file"] = args.log_file
    if args.log_level:
        overrides["log_level"] = args.log_level
    return overrides


def configure_logging(settings: Settings) -> None:
    """Send logs to a file if asked. The UI installs its own handler.

    Nothing is written to stdout or stderr: that would draw over the interface,
    which is why the previous client had to redirect both streams globally.
    """
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logger = logging.getLogger("batmud")
    logger.setLevel(level)
    logger.propagate = False

    if settings.log_file is not None:
        settings.log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(settings.log_file, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s %(message)s"))
        logger.addHandler(handler)


def check_ready(settings: Settings, mode: LoginMode) -> str | None:
    """Return the reason the session cannot start, if there is one."""
    if mode is LoginMode.LOGIN and not settings.character.name:
        return (
            "No character name. Pass --character, set BATMUD_CHARACTER, "
            "or use --mode create to make a new one."
        )
    if not settings.character.password.get_secret_value():
        return "No password. Set BATMUD_PASSWORD or put it in the config file."
    return None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        settings = load_settings(args.config, overrides_from(args))
    except (FileNotFoundError, ValueError) as error:
        print(f"batmud: {error}", file=sys.stderr)
        return 2

    if args.print_config:
        print(settings.model_dump_json(indent=2))
        return 0

    mode = LoginMode(args.mode)
    if (problem := check_ready(settings, mode)) is not None:
        print(f"batmud: {problem}", file=sys.stderr)
        return 2

    configure_logging(settings)
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    from .tui.app import BatMudApp

    BatMudApp(settings, mode=mode).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
