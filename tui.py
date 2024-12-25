from textual.app import App, ComposeResult
from textual.containers import ScrollableContainer, Horizontal
from textual.widgets import Header, Footer, Static
from textual.reactive import reactive
from textual.message import Message
from textual.screen import ModalScreen, Screen
from rich.text import Text
import re
from datetime import datetime
import sys
import logging
from logging import Handler

# Create logger for TUI
tui_logger = logging.getLogger('TUI')


class TUILogHandler(Handler):
    """Custom logging handler that writes to our TUI log view"""

    def __init__(self, log_output):
        super().__init__()
        self.log_output = log_output

    def emit(self, record):
        try:
            msg = self.format(record)
            self.log_output.add_log(f"{record.levelname}: {msg}")
        except Exception:
            self.handleError(record)


class GameOutput(Static):
    def __init__(self):
        super().__init__("")
        self.text_content = []
        self.max_lines = 5000

    def update_content(self, new_content: str):
        # Strip ANSI codes
        clean_content = re.sub(r'\x1b\[[0-9;]*[mGKH]', '', new_content)

        # Split and add new lines, preserving empty lines for formatting
        new_lines = clean_content.split('\n')
        self.text_content.extend(new_lines)

        # Keep buffer size manageable
        if len(self.text_content) > self.max_lines:
            self.text_content = self.text_content[-self.max_lines:]

        # Update display
        content = '\n'.join(self.text_content)
        self.update(Text(content, style="bold #00ff00"))

        # Force scroll to bottom
        if self.parent:
            self.parent.scroll_end(animate=False)


class AIDecisions(Static):
    def __init__(self):
        super().__init__("")
        self.decisions = []
        self.max_decisions = 500

    def add_decision(self, decision: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.decisions.append(f"[{timestamp}] {decision}")

        # Keep only last N decisions
        if len(self.decisions) > self.max_decisions:
            self.decisions = self.decisions[-self.max_decisions:]

        # Update display
        content = '\n'.join(self.decisions)
        self.update(Text(content, style="bold #00ff00"))

        # Force scroll to bottom
        if self.parent:
            self.parent.scroll_end(animate=False)


class GameUpdate(Message):
    """Message for game updates"""

    def __init__(self, content: str) -> None:
        self.content = content
        super().__init__()


class AIUpdate(Message):
    """Message for AI updates"""

    def __init__(self, content: str) -> None:
        self.content = content
        super().__init__()


class PauseModal(ModalScreen):
    """A modal dialog that appears when the game is paused"""

    DEFAULT_CSS = """
    PauseModal {
        align: center middle;
    }

    .modal-container {
        width: 30;
        height: 7;
        border: solid #00ff00;
        background: rgba(0, 17, 0, 0.8);
        color: #00ff00;
        padding: 1;
        content-align: center middle;
        text-align: center;
    }
    """

    def compose(self) -> ComposeResult:
        with ScrollableContainer(classes="modal-container"):
            yield Static("PAUSED\n\nPress 'p' to resume")

    def on_key(self, event) -> None:
        if event.key == "p":
            self.app.pop_screen()


class LogOutput(Static):
    def __init__(self):
        super().__init__("")
        self.log_content = []
        self.max_lines = 1000

    def add_log(self, message: str):
        # Split multi-line messages and add each line separately
        for line in message.splitlines():
            if line.strip():  # Only add non-empty lines
                timestamp = datetime.now().strftime("%H:%M:%S")
                self.log_content.append(f"[{timestamp}] {line}")

        # Keep buffer size manageable
        if len(self.log_content) > self.max_lines:
            self.log_content = self.log_content[-self.max_lines:]

        # Update display with all content
        content = '\n'.join(self.log_content)
        self.update(Text(content, style="bold #00ff00"))

        # Force scroll to bottom
        if self.parent and hasattr(self.parent, "scroll_end"):
            self.parent.scroll_end(animate=False)


class LogView(Screen):
    """A screen that shows log output"""

    BINDINGS = [("l", "toggle_logs", "Hide Logs")]

    CSS = """
    Screen {
        background: #000000;
        layers: base overlay;
    }

    Header {
        dock: top;
        background: #001100;
        color: #00ff00;
        border-bottom: solid #00ff00;
        height: 3;
    }

    Footer {
        dock: bottom;
        background: #001100;
        color: #00ff00;
        border-top: solid #00ff00;
        height: 3;
    }

    #log-container {
        width: 100%;
        height: 100%;
        background: #000000;
        border: solid #00ff00;
        overflow-y: scroll;
    }

    .title {
        width: 100%;
        height: 3;
        background: #001100;
        color: #00ff00;
        content-align: center middle;
        border-bottom: solid #00ff00;
    }

    LogOutput {
        width: 100%;
        height: auto;
        background: #000000;
        color: #00ff00;
        padding: 1;
        border: none;
        scrollbar-background: #001100;
        scrollbar-color: #00ff00;
        scrollbar-size: 1 1;
        margin: 0;
    }
    """

    def __init__(self):
        super().__init__()
        self.log_output = LogOutput()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with ScrollableContainer(id="log-container"):
            yield Static("Debug Logs", classes="title", markup=False)
            yield self.log_output
        yield Footer()

    async def action_toggle_logs(self) -> None:
        """Toggle log view"""
        self.app.pop_screen()


class BatMudTUI(App):
    CSS = """
    Screen {
        background: #000000;
        layers: base overlay;
    }

    Header {
        dock: top;
        background: #001100;
        color: #00ff00;
        border-bottom: solid #00ff00;
        height: 3;
    }

    Footer {
        dock: bottom;
        background: #001100;
        color: #00ff00;
        border-top: solid #00ff00;
        height: 3;
    }

    Horizontal {
        height: 100%;
        width: 100%;
        background: #000000;
    }

    .panel {
        height: 100%;
        border: solid #00ff00;
        background: #000000;
    }

    #game-container {
        width: 65%;
        margin-right: 1;
        overflow-y: scroll;
    }

    #ai-container {
        width: 35%;
        margin-left: 1;
        overflow-y: scroll;
    }

    #log-container {
        width: 100%;
        overflow-y: scroll;
    }

    .title {
        width: 100%;
        height: 3;
        background: #001100;
        color: #00ff00;
        content-align: center middle;
        border-bottom: solid #00ff00;
    }

    .paused {
        color: #ff0000;
        text-style: bold;
    }

    GameOutput, AIDecisions, LogOutput {
        width: 100%;
        height: auto;
        background: #000000;
        color: #00ff00;
        padding: 1;
        border: none;
        scrollbar-background: #001100;
        scrollbar-color: #00ff00;
        scrollbar-size: 1 1;
        margin: 0;
    }

    GameOutput:focus, LogOutput:focus {
        border: none;
        background: #000000;
    }

    ModalScreen {
        background: rgba(0, 0, 0, 0.5);
    }
    """

    TITLE = "BatMUD AI Client"
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("p", "pause", "Pause/Resume"),
        ("l", "toggle_logs", "Show Logs")
    ]

    def __init__(self):
        super().__init__()
        self.game_output = GameOutput()
        self.ai_decisions = AIDecisions()
        self.log_view = LogView()
        self.is_exiting = False
        self.is_paused = False
        self.header = None

        # Set up logging
        self.setup_logging()

    def setup_logging(self):
        """Set up logging to write to our log view"""
        # Create and configure root logger
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG)

        # Create our custom handler
        tui_handler = TUILogHandler(self.log_view.log_output)
        tui_handler.setLevel(logging.DEBUG)

        # Create formatter
        formatter = logging.Formatter('%(message)s')
        tui_handler.setFormatter(formatter)

        # Add handler to root logger
        root_logger.addHandler(tui_handler)

        # Redirect stdout and stderr to logger
        sys.stdout = LoggerWriter(logging.INFO)
        sys.stderr = LoggerWriter(logging.ERROR)

    def compose(self) -> ComposeResult:
        self.header = Header(show_clock=True)
        yield self.header
        with Horizontal():
            with ScrollableContainer(id="game-container", classes="panel"):
                yield Static("Game Output", classes="title", markup=False)
                yield self.game_output
            with ScrollableContainer(id="ai-container", classes="panel"):
                yield Static("AI Decisions", classes="title", markup=False)
                yield self.ai_decisions
        yield Footer()

    def update_header(self):
        """Update header text based on pause state"""
        if self.is_paused:
            self.header.sub_title = Text(
                "PAUSED - Press 'p' to resume", style="bold red")
            self.title = "BatMUD AI Client [PAUSED]"
        else:
            self.header.sub_title = Text("Connected to BatMUD", style="green")
            self.title = "BatMUD AI Client"

    async def handle_game_update(self, message: GameUpdate) -> None:
        self.game_output.update_content(message.content)

    async def handle_ai_update(self, message: AIUpdate) -> None:
        self.ai_decisions.add_decision(message.content)

    def on_mount(self) -> None:
        """Handle mount event"""
        self.update_header()

    async def action_quit(self) -> None:
        """Quit the application"""
        self.is_exiting = True
        # Restore stdout and stderr
        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__
        self.exit()

    async def _on_key(self, event) -> None:
        """Handle key events"""
        if event.key == "q":
            self.is_exiting = True
            # Restore stdout and stderr
            sys.stdout = sys.__stdout__
            sys.stderr = sys.__stderr__
            self.exit()

    async def action_pause(self) -> None:
        """Toggle pause state"""
        self.is_paused = not self.is_paused
        self.update_header()
        tui_logger.info("Game %s", "PAUSED" if self.is_paused else "RESUMED")

    async def action_toggle_logs(self) -> None:
        """Toggle log view"""
        await self.push_screen(self.log_view)


class LoggerWriter:
    """A class to redirect stdout/stderr to our logger"""

    def __init__(self, level):
        self.level = level
        self.logger = logging.getLogger()
        self.line_buffer = []

    def write(self, message):
        if message.strip():  # Only log non-empty messages
            self.logger.log(self.level, message.rstrip())

    def flush(self):
        pass
