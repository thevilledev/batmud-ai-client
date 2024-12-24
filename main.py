import telnetlib3
import anthropic
import os
import sys
from typing import Optional
import asyncio
import re
from tui import BatMudTUI, GameUpdate, AIUpdate
from functools import partial
from textual.message import Message


class BatMudClient:
    def __init__(self):
        self.host = "batmud.bat.org"
        self.port = 2023
        self.claude = anthropic.Anthropic(
            api_key=os.getenv("ANTHROPIC_API_KEY"))
        self.telnet: Optional[telnetlib3.Telnet] = None
        self.game_state = ""
        self.last_response = ""
        self.name_prefix = os.getenv("BATMUD_NAME_PREFIX", "claude")
        self.password = os.getenv("BATMUD_PASSWORD", "simakuutio")
        self.game_state_length = 2000
        self.tui = BatMudTUI()
        self.message_queue = asyncio.Queue()

    async def connect(self):
        """Establish connection to BatMUD server"""
        try:
            await self.message_queue.put(GameUpdate("Starting BatMUD AI Client...\n"))
            await self.message_queue.put(GameUpdate(f"Connecting to {self.host}:{self.port}...\n"))

            reader, writer = await telnetlib3.open_connection(
                self.host,
                self.port,
                encoding='utf-8',
                connect_minwait=0.05
            )

            self.telnet = (reader, writer)
            await self.message_queue.put(GameUpdate(f"Successfully connected to {self.host}:{self.port}\n"))

            # Try to read initial game data
            initial_data = await reader.read(1024)
            if initial_data:
                await self.message_queue.put(GameUpdate(initial_data))
                print(f"Initial game data: {initial_data!r}")

        except Exception as e:
            await self.message_queue.put(GameUpdate(f"Failed to connect: {e}\n"))
            return False  # Return False instead of sys.exit()
        return True

    async def read_game_output(self, timeout=0.1):
        """Read output from the game server"""
        try:
            reader, writer = self.telnet
            data = await reader.read(1024)  # Remove timeout for testing

            if data:
                print(
                    f"DEBUG: Raw telnet data received: {
                        data!r}")  # Debug print
                self.game_state += data
                await self.message_queue.put(GameUpdate(data))
                return data

        except Exception as e:
            print(f"Error reading game output: {e}")
            return None

        return ""

    async def send_command(self, command: str):
        """Send a command to the game server"""
        try:
            reader, writer = self.telnet
            writer.write(f"{command}\n")
            await writer.drain()
            await self.message_queue.put(AIUpdate(f"Command: {command}"))
            await asyncio.sleep(0.5)
        except Exception as e:
            await self.message_queue.put(AIUpdate(f"Error sending command: {e}"))

    async def get_claude_response(self):
        """Get Claude's decision based on current game state"""
        # Strip ANSI codes from game state before sending to Claude
        clean_state = re.sub(
            r'\x1b\[[0-9;]*[mGKH]', '', self.game_state[-self.game_state_length:])

        prompt = f"""You are playing BatMUD, a text-based multiplayer game.
Based on the current game state, decide what action to take next.
If the game asks to create a character, set name to "{self.name_prefix}" followed by a random string of four letters. The name should be all lowercase and without spaces.
If the game asks for a password, respond with "{self.password}".
If you are asked with a multiple choice question, respond with the number of the choice you want to select.
Explore the world. If you are confronted with a monster, kill it.
Current game state:
{clean_state}  # Limited context length

Previous action taken:
{self.last_response}

Respond with only the command to execute, no explanation."""

        max_retries = 3
        retry_delay = 1  # seconds

        for attempt in range(max_retries):
            try:
                response = await asyncio.to_thread(
                    lambda: self.claude.messages.create(
                        model="claude-3-opus-20240229",
                        max_tokens=100,
                        temperature=0.7,
                        messages=[{
                            "role": "user",
                            "content": prompt
                        }]
                    )
                )
                if not response.content:
                    await self.message_queue.put(AIUpdate(f"Empty response from Claude (attempt {attempt + 1}/{max_retries})"))
                    if attempt < max_retries - 1:
                        await asyncio.sleep(retry_delay)
                        continue
                    return None

                command = response.content[0].text.strip()
                self.last_response = command
                await self.message_queue.put(AIUpdate(f"AI Decision: {command}"))
                return command
            except Exception as e:
                await self.message_queue.put(AIUpdate(f"Error getting response (attempt {attempt + 1}/{max_retries}): {e}"))
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay)
                    continue
                return None

    async def process_messages(self):
        """Process messages from the queue and update the TUI"""
        while True:
            try:
                message = await self.message_queue.get()
                print(
                    f"DEBUG: Processing message type: {
                        type(message)}")  # Debug print

                if isinstance(message, GameUpdate):
                    print(
                        f"DEBUG: Game update content: {
                            message.content!r}")  # Debug print
                    await self.tui.handle_game_update(message)
                elif isinstance(message, AIUpdate):
                    print(
                        f"DEBUG: AI update content: {
                            message.content!r}")  # Debug print
                    print(f"DEBUG: Sending AI update to TUI...")  # Debug print
                    await self.tui.handle_ai_update(message)
                    print(f"DEBUG: AI update sent to TUI")  # Debug print
                else:
                    print(f"DEBUG: Unknown message type: {type(message)}")

                self.message_queue.task_done()
            except Exception as e:
                print(f"Error in process_messages: {e}")
                import traceback
                traceback.print_exc()
            await asyncio.sleep(0.1)

    async def game_loop(self):
        """Main game loop"""
        if not await self.connect():
            return

        message_processor = asyncio.create_task(self.process_messages())

        # Get initial AI response after connection
        print("Getting initial AI response...")
        initial_command = await self.get_claude_response()
        if initial_command:
            print(f"Sending initial command: {initial_command}")
            await self.send_command(initial_command)

        try:
            while not self.tui.is_exiting:
                # Read game output with a shorter timeout
                output = await self.read_game_output(timeout=0.1)
                if output is None:  # Connection closed
                    break

                # If we got output, get AI response
                if output:
                    print(f"Got game output, length: {len(output)}")
                    command = await self.get_claude_response()
                    if command:
                        print(f"Sending command: {command}")
                        await self.send_command(command)

                # Small delay to prevent CPU thrashing
                await asyncio.sleep(0.1)
        except Exception as e:
            print(f"Error in game loop: {e}")
        finally:
            message_processor.cancel()


async def main():
    client = BatMudClient()

    try:
        # Start the TUI first
        tui_task = asyncio.create_task(client.tui.run_async())

        # Give the TUI a moment to initialize
        await asyncio.sleep(1)

        # Start the game loop
        game_task = asyncio.create_task(client.game_loop())

        # Wait for the game task to complete or the TUI to exit
        while True:
            if game_task.done():
                break
            if client.tui.is_exiting:
                game_task.cancel()
                break
            await asyncio.sleep(0.1)

    except KeyboardInterrupt:
        print("\nGracefully shutting down...")
    except Exception as e:
        print(f"\nError: {e}")
    finally:
        if client.telnet:
            reader, writer = client.telnet
            writer.close()
        if not client.tui.is_exiting:
            client.tui.exit()  # Use exit() instead of shutdown()
        print("Connection closed.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nExiting...")
    except Exception as e:
        print(f"\nFatal error: {e}")
    sys.exit(0)
