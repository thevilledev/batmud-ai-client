# BatMUD AI Client

An AI-powered client for BatMUD that uses Claude 3 Opus to play the game autonomously.

## Prerequisites

- Python 3.12+
- Anthropic API key

## Setup

1. Clone the repository

```bash
git clone https://github.com/thevilledev/batmud-ai-client.git
```

2. Install dependencies:

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

## License

MIT
