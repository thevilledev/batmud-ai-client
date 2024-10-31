# BatMUD AI Client

An AI-powered client for BatMUD that uses Claude 3 Opus to play the game autonomously.

<img src="images/bat.png" width="65%" height="65%">

## Prerequisites

- Python 3.12+
- Anthropic API key

## Setup

1. Clone the repository

```bash
git clone https://github.com/thevilledev/batmud-ai-client.git
```

2. Install dependencies (`venv` recommended):

```bash
pip install -r requirements.txt
```

3. Set environment variables:

```bash
export ANTHROPIC_API_KEY=<your-anthropic-api-key>
export BATMUD_NAME_PREFIX=<your-name-prefix>
export BATMUD_PASSWORD=<your-password>
```

## Running the client

```bash
python main.py
```

The client will automatically:
- Connect to BatMUD
- Create a character if needed
- Explore the world
- Engage in combat

Press `Ctrl+C` to exit.

## API limitations

Claude 3.5 Opus has a token limit of 1M tokens. You may tune the `game_state_length` in `main.py` to reduce the amount of context saved.

Token caching might be useful to reduce the number of tokens sent to Claude.

When playing, always monitor the token usage in the console.

## License

MIT
