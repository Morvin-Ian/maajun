# Maajun

**Error monitoring that files the bug report — and can write the fix.**

Maajun watches your error sources — local log files (e.g. your app's error
log on a VPS) and failed GitHub Actions runs — and when a new error
appears it investigates the code and documents the incident on GitHub:
in *suggest* mode it files an issue with the analysis and a suggested fix;
in *fix* mode it applies the fix on a branch, runs your test suite against
it, and opens a pull request with the result. It records what each analysis
cost, caps what it may spend per day, and never reports the same error twice.

## Quick start

Investigate something right now, without setting up any monitoring:

```bash
uv sync                                                # or: pip install maajun
maajun setup                                           # stores your API key
maajun report "Checkout 500s when the cart is empty"
```

That clones your repo, reads the code, and files an issue with a root-cause
report (`-m fix` opens a PR with the fix applied instead; `--dry-run`
just prints the analysis). Get a DeepSeek API key at
[platform.deepseek.com](https://platform.deepseek.com).

## Quick start: continuous monitoring

One command sets up everything:

```bash
maajun setup           # API key, GitHub, log files, GitHub Actions
maajun watch --dry-run # analyze errors without opening PRs — test your config
maajun watch           # keep monitoring
```

Only the API key is required. `setup` offers GitHub, log files, and GitHub
Actions in turn, and each is skippable with Enter — so a minimal install
is a key and a log path. It detects your repo from the git remote and
picks up an existing `GITHUB_TOKEN` or `gh auth login` session, and it
re-runs safely: every answer defaults to what you already have.

Without a GitHub repo, maajun still detects and analyzes errors — the
incident report is written under `daemon.workdir` instead of opening a
pull request. Add a repo whenever you want PRs:

```bash
maajun setup --repo you/yourapp    # or re-run 'maajun setup' interactively
```

For CI, every prompt has a flag, and secrets come from the environment so
they never reach shell history:

```bash
DEEPSEEK_API_KEY=... GITHUB_TOKEN=... \
  maajun setup --non-interactive --repo you/yourapp --logs /var/log/app/error.log
```

Tweak any setting later with `maajun config <key> <value>`, watch more
than one repo with `maajun add-repo <owner/name>`, and re-check your
wiring with `maajun status`. Each new error becomes one artifact:

```
error detected ──▶ fingerprint & dedup ──▶ AI analyzes your code
  (logs / CI)                                         │
   issue (suggest) ◀────────────────────────────────  ┤
   PR    (fix)     ◀──── branch + applied fix ────────┘
```

The same error is never reported twice — repeat sightings only bump a counter.

## Documentation

- [How it works](docs/architecture.md) — components, monitors, and the
  incident pipeline
- [Monitoring guide](docs/monitoring.md) — config reference, error
  sources (logs, GitHub Actions), cost tracking, running on a VPS
- [Command reference](docs/commands.md) — every CLI command and flag

## Development

```bash
uv sync            # installs dev dependencies (pytest, ruff)
uv run pytest      # run tests
uv run ruff check  # lint
```
