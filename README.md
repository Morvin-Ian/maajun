# Maajun

AI-powered developer assistant with two faces:

- **Chat** — talk to an AI in your terminal, with streamed responses and
  tools (it can read, search, and edit files, and run commands — with your
  permission).
- **Watch** — a monitoring daemon. It watches your error sources — local
  log files (e.g. your app's error log on a VPS) and failed GitHub
  Actions runs — and when a new error appears it investigates the code,
  documents the incident, and opens a pull request on GitHub — either
  with just the analysis and suggested fix (*suggest* mode) or with the
  fix applied (*fix* mode). It can email you when a PR opens and records
  what each analysis cost. You can also file a report on demand with
  `maajun report "<what's wrong>"` — same investigation and PR, triggered
  by you instead of a monitor.

## Quick start: chat

```bash
uv sync           # or: pip install maajun
maajun login      # store your DeepSeek API key (interactive, input hidden)
maajun chat
```

Get a DeepSeek API key at [platform.deepseek.com](https://platform.deepseek.com).

## Quick start: error monitoring

```bash
maajun init            # interactive setup (provider, repo, mode, logs)
maajun github-login    # store a GitHub token for the target repo
maajun status          # check credentials, repo access, and log files
maajun watch --dry-run # analyze errors without opening PRs — test your config
maajun watch --once    # one real poll cycle
maajun watch           # keep monitoring
```

Or investigate something yourself, without waiting for a monitor:

```bash
maajun report "Checkout 500s when the cart is empty"   # analyze + open a PR
maajun report "Slow /search endpoint" --dry-run        # analyze only
```

Configure at least one error source — `monitor.log_files` to tail your
app's logs, or GitHub Actions repos to catch CI failures — and point
`github.repo` at the repository maajun should open PRs on. Tweak any
setting later with `maajun config <key> <value>`, and watch more than one
repo with `maajun add-repo <owner/name>`. Each new error becomes one PR:

```
error detected ──▶ fingerprint & dedup ──▶ AI analyzes your code
  (logs / CI)                                   │
   PR on GitHub ◀──── branch + incident report ─┘
```

The same error never opens two PRs — repeat sightings only bump a counter.

## Documentation

- [How it works](docs/architecture.md) — components, monitors, and the
  incident pipeline
- [Monitoring guide](docs/monitoring.md) — config reference, error
  sources (logs, GitHub Actions), email notifications, cost tracking,
  running on a VPS
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
