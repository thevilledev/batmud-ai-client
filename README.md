# BatMUD AI Client

An AI-powered client for BatMUD that uses your LLM of choice through OpenRouter API to play the game autonomously. Features a retro-style terminal interface.

<img src="images/bat.png" width="65%" height="65%">

## Features

- Terminal User Interface (TUI) with split views:
  - Game output (left panel)
  - AI decisions with timestamps (right panel)
  - Debug logs view (toggle with 'l')
- Autonomous gameplay:
  - Character creation and login support
  - Intelligent combat handling
  - Environment exploration
- Debugging tools:
  - Real-time log viewing
  - Pause functionality to inspect state
  - Optional file logging
  - Configurable log levels

## Prerequisites

- Python 3.12+
- OpenRouter API key

## Setup

1. Clone the repository:
```bash
git clone https://github.com/thevilledev/batmud-ai-client.git
cd batmud-ai-client
```

2. Create and activate a virtual environment (recommended):
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Set environment variables:

```bash
# Required
export OPENROUTER_API_KEY=<your-openrouter-api-key>
export OPENROUTER_MODEL=<model-name>         # Optional, defaults to anthropic/claude-3-opus-20240229

# For character creation mode (default)
export BATMUD_NAME_PREFIX=<your-name-prefix>  # Default: "claude"
export BATMUD_PASSWORD=<your-password>        # Default: "simakuutio"

# For login mode
export BATMUD_CHARACTER=<your-character-name>  # Required for login mode
export BATMUD_PASSWORD=<your-password>         # Password for your character
```

## Usage

Basic start with character creation (default):
```bash
python main.py
```

Login with existing character:
```bash
python main.py --mode login
```

With debugging options:
```bash
python main.py --log-file logs/batmud.log --log-level DEBUG
```

With specific model:
```bash
python main.py --model google/gemini-pro
```

### Command Line Options

| Option | Description | Default |
|--------|-------------|---------|
| `--mode <mode>` | Mode to run in (`create` or `login`) | `create` |
| `--log-file <path>` | Enable file logging to specified path | Disabled |
| `--log-level <level>` | Set logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL) | INFO |
| `--model <model>` | OpenRouter model to use (e.g. anthropic/claude-3-opus-20240229, google/gemini-pro) | anthropic/claude-3-opus-20240229 |

### Controls

| Key | Action | Description |
|-----|--------|-------------|
| `q` | Quit | Exit the application |
| `p` | Pause/Resume | Toggle AI actions (game output still updates) |
| `l` | Logs | Toggle debug logs view |

## Debugging

1. **Real-time Logs**
   - Press 'l' to view logs in the application
   - Shows stdout/stderr and internal events
   - Timestamps for all entries

2. **File Logging**
   - Enable with `--log-file` flag
   - Set verbosity with `--log-level`
   - Useful for post-mortem analysis

3. **Pause Mode**
   - Press 'p' to pause AI actions
   - Game state updates continue
   - Useful for inspecting behavior

## Performance Tuning

- Adjust `game_state_length` (default: 500 characters) in `main.py` to control context size
- Lower values reduce token usage but may impact AI decision quality
- Monitor token usage through logs when debugging

## API limitations

By default this client uses Claude through OpenRouter API (using OpenAI SDK for compatibility). The token limits and pricing depend on your OpenRouter subscription. You may tune the `game_state_length` in `main.py` to reduce the amount of context saved (default: 500 characters).

## Credits

Thanks [@errnoh](https://github.com/errnoh) for the idea!

## License

MIT
