# Maajun

AI-powered developer assistant with two faces:

- **Chat** — talk to an AI in your terminal, with streamed responses and
  tools (it can read, search, and edit files, and run commands — with your
  permission).
- **Watch** — a monitoring daemon for your server. It tails your logs,
  and when your app throws a new error it investigates the code, documents
  the incident, and opens a pull request on GitHub — either with just the
  analysis and suggested fix (*suggest* mode) or with the fix applied
  (*fix* mode).

## Quick start: chat

```bash
uv sync           # or: pip install maajun
maajun login      # store your DeepSeek API key (interactive, input hidden)
maajun chat
```

Get a DeepSeek API key at [platform.deepseek.com](https://platform.deepseek.com).

## Quick start: error monitoring

```bash
maajun init            # writes ~/.config/maajun/config.toml — edit it
maajun github-login    # store a GitHub token (or: export GITHUB_TOKEN=...)
maajun watch --once    # dry run: one poll cycle
maajun watch           # keep monitoring
```

Point `monitor.log_files` at your app's log files and `github.repo` at the
repository maajun should open PRs on. Each new error becomes one PR:

```
error in log ──▶ fingerprint & dedup ──▶ AI analyzes your code
                                              │
   PR on GitHub ◀── branch + incident report ─┘
```

The same error never opens two PRs — repeat sightings only bump a counter.

## Documentation

- [How it works](docs/architecture.md) — components and the incident pipeline
- [Monitoring guide](docs/monitoring.md) — config reference, GitHub
  permissions, modes, running on a VPS
- [Command reference](docs/commands.md) — every CLI command and flag

## Supported AI providers

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
