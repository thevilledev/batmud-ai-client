from textual.app import App, ComposeResult
from textual.containers import ScrollableContainer, Horizontal
from textual.widgets import Header, Footer, Static
from textual.reactive import reactive
from textual.message import Message
from rich.text import Text
import re
from datetime import datetime


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

    .title {
        width: 100%;
        height: 3;
        background: #001100;
        color: #00ff00;
        content-align: center middle;
        border-bottom: solid #00ff00;
    }

    GameOutput, AIDecisions {
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

    GameOutput:focus {
        border: none;
        background: #000000;
    }
    """

    TITLE = "BatMUD AI Client"
    BINDINGS = [("q", "quit", "Quit")]

    def __init__(self):
        super().__init__()
        self.game_output = GameOutput()
        self.ai_decisions = AIDecisions()
        self.is_exiting = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with ScrollableContainer(id="game-container", classes="panel"):
                yield Static("Game Output", classes="title", markup=False)
                yield self.game_output
            with ScrollableContainer(id="ai-container", classes="panel"):
                yield Static("AI Decisions", classes="title", markup=False)
                yield self.ai_decisions
        yield Footer()

    async def handle_game_update(self, message: GameUpdate) -> None:
        self.game_output.update_content(message.content)

    async def handle_ai_update(self, message: AIUpdate) -> None:
        self.ai_decisions.add_decision(message.content)

    def on_mount(self) -> None:
        """Handle mount event"""
        self.app.sub_title = "Connected to BatMUD"

    async def action_quit(self) -> None:
        """Quit the application"""
        self.is_exiting = True
        await self.shutdown()

    async def _on_key(self, event) -> None:
        """Handle key events"""
        if event.key == "q":
            self.is_exiting = True
            self.exit()  # Use exit() instead of shutdown()
