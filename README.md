# Maajun

AI-powered developer assistant. Chat with an AI directly from your terminal, with streamed responses.

## Setup

```bash
# Install dependencies
uv sync

# Set up your API key (interactive, input hidden)
maajun login
```

Get a DeepSeek API key at [platform.deepseek.com](https://platform.deepseek.com).

## Usage

```bash
# Start a chat session
maajun chat

# Specify a provider
maajun chat -p deepseek

# Enable the provider's reasoning mode (DeepSeek: deepseek-reasoner)
maajun chat --thinking
```

**Chat commands:**
- `/clear` — start a new session
- `/history` — show conversation history
- `/quit` — exit

## Key Management

```bash
maajun provider-list             # see provider status
maajun login                     # add or replace a key interactively
maajun config-set-key deepseek   # non-interactive alternative (prompts for the key)
maajun config-remove-key openai  # remove a provider's key
maajun sign-out                  # clear all keys
```

Keys are stored in your OS keyring (gnome-keyring on Linux). Avoid passing the key
as a command-line argument — it would be saved in your shell history.

## Supported Providers

| Provider | Status |
|----------|--------|
| DeepSeek | Supported |
| OpenAI | Coming soon |
| Anthropic | Coming soon |

## Development

```bash
uv sync            # installs dev dependencies (pytest, ruff)
uv run pytest      # run tests
uv run ruff check  # lint
```
