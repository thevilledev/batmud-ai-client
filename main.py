import telnetlib3
import anthropic
import os
import sys
from typing import Optional
import asyncio
import re

class BatMudClient:
    def __init__(self):
        self.host = "batmud.bat.org"
        self.port = 2023
        self.claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        self.telnet: Optional[telnetlib3.Telnet] = None
        self.game_state = ""
        self.last_response = ""
        self.name_prefix = os.getenv("BATMUD_NAME_PREFIX", "claude")
        self.password = os.getenv("BATMUD_PASSWORD", "simakuutio")

    async def connect(self):
        """Establish connection to BatMUD server"""
        try:
            reader, writer = await telnetlib3.open_connection(self.host, self.port)
            self.telnet = (reader, writer)
            print(f"Connected to {self.host}:{self.port}")
        except Exception as e:
            print(f"Failed to connect: {e}")
            sys.exit(1)

    async def read_game_output(self, timeout=0.1):
        """Read output from the game server"""
        try:
            reader, writer = self.telnet
            complete_data = ""
            
            # Keep reading while there's data available
            while True:
                data = await reader.read(1024)
                if not data:
                    break
                complete_data += data
                
                # Check if we've reached the end of available data
                if reader.at_eof():
                    break
                
                # Quick check if more data is immediately available
                await asyncio.sleep(0.1)
                if not reader._buffer:  # Using internal buffer check
                    break
            
            if complete_data:
                self.game_state += complete_data
                # Print raw output with ANSI color codes intact
                print("\nGame output:", complete_data, flush=True)
                return complete_data
                
        except EOFError:
            print("Connection closed by server")
            return None
        return ""

    async def send_command(self, command: str):
        """Send a command to the game server"""
        try:
            reader, writer = self.telnet
            writer.write(f"{command}\n")
            await writer.drain()
            print(f"\nSent command: {command}")
            await asyncio.sleep(0.5)  # Give the game time to process
        except Exception as e:
            print(f"Error sending command: {e}")

    async def get_claude_response(self):
        """Get Claude's decision based on current game state"""
        # Strip ANSI codes from game state before sending to Claude
        clean_state = re.sub(r'\x1b\[[0-9;]*[mGKH]', '', self.game_state[-2000:])
        
        prompt = f"""You are playing BatMUD, a text-based multiplayer game. 
Based on the current game state, decide what action to take next.
If the game asks to create a character, respond with "create character". Set name to "{self.name_prefix}" and a random string of four letters.
If the game asks for a password, respond with "{self.password}".
Explore the world. If you are confronted with a monster, kill it.
Current game state:
{clean_state}  # Only use last 2000 chars to stay within context

Previous action taken:
{self.last_response}

Respond with only the command to execute, no explanation."""

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
            command = response.content[0].text.strip()
            self.last_response = command
            return command
        except Exception as e:
            print(f"Error getting Claude response: {e}")
            return None

    async def game_loop(self):
        """Main game loop"""
        await self.connect()
        
        while True:
            # Read game output
            output = await self.read_game_output()
            if output is None:  # Connection closed
                break

            # Get and execute Claude's decision
            if output:
                command = await self.get_claude_response()
                if command:
                    await self.send_command(command)
            
            # Add small delay to prevent overwhelming the server
            await asyncio.sleep(1)

async def main():
       
    client = BatMudClient()
    try:
        await client.game_loop()
    except KeyboardInterrupt:
        print("\nExiting...")
    finally:
        if client.telnet:
            reader, writer = client.telnet
            writer.close()  # Just close the writer
            await asyncio.sleep(0.1)  # Small delay to allow the connection to close gracefully

if __name__ == "__main__":
    asyncio.run(main())
