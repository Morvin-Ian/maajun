# Maajun

[![CI](https://github.com/Morvin-Ian/maajun/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Morvin-Ian/maajun/actions/workflows/ci.yml?query=branch%3Amain)
[![PyPI](https://img.shields.io/pypi/v/maajun.svg)](https://pypi.org/project/maajun/)
[![Python](https://img.shields.io/pypi/pyversions/maajun.svg)](https://pypi.org/project/maajun/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/Morvin-Ian/maajun/blob/main/LICENSE)

Error monitoring that files the bug report — and can write the fix.

Maajun watches your error sources, investigates each new error against your
source code, and documents it on GitHub. In `suggest` mode it opens an issue
containing the root-cause analysis and a proposed patch. In `fix` mode it
applies the patch on a branch, runs your test suite against it, and opens a
pull request with the result.

Nothing merges without your review, in either mode.

**Contents** — [Requirements](#requirements) · [Installation](#installation) ·
[Quick start](#quick-start) · [How it works](#how-it-works) ·
[Modes](#modes) · [AI providers](#ai-providers) ·
[Configuration](#configuration) · [Commands](#commands) ·
[Cost control](#cost-control) · [Security model](#security-model) ·
[Documentation](#documentation)

## Requirements

- Python 3.11 or newer
- An API key for DeepSeek or OpenAI — see [AI providers](#ai-providers)
- Optional: a GitHub token, to open issues and pull requests

Log-file monitoring reads files on the local machine, so the daemon has to run
on the server that writes them. The GitHub Actions monitor works from anywhere
with network access.

## Installation

```bash
uv tool install maajun     # or: pipx install maajun / pip install maajun
```

From source:

```bash
git clone https://github.com/Morvin-Ian/maajun && cd maajun && uv sync
```

## Quick start

### Investigate one issue

No monitoring setup required — describe the problem and let maajun read the
code:

```bash
maajun setup                                    # pick a provider, store its key
maajun report "Checkout 500s when the cart is empty"
```

This clones the target repo, analyzes it, and files an issue with a root-cause
report. Add `-m fix` to open a pull request with the fix applied, or
`--dry-run` to print the analysis and its cost without touching GitHub.

### Monitor continuously

```bash
maajun setup             # provider key, GitHub, log files, GitHub Actions
maajun watch --dry-run   # analyze real errors without opening PRs
maajun watch             # run the daemon
```

`setup` asks for the provider and its key, then offers GitHub, log files, and
GitHub Actions in turn — each skippable with Enter, so a minimal install is a key and a log
path. It detects the repo from your `origin` remote, and re-runs safely: every
prompt defaults to your current configuration.

Every prompt also has a flag, so once a key is in the keyring the rest can be
reconfigured unattended:

```bash
maajun setup --non-interactive --repo you/yourapp --logs /var/log/app/error.log

maajun setup --non-interactive --provider openai --repo you/yourapp \
  --logs /var/log/app/error.log
```

`--non-interactive` cannot store a *new* API key — there is nowhere to prompt —
so run `maajun setup` interactively once per machine first.

Without a configured repo, maajun runs in **local mode**: errors are still
detected and analyzed, but each report is written to
`<workdir>/reports/<fingerprint>.md` instead of opening a pull request.

## How it works

1. **Detect** — a monitor picks up a new error from a log file or a failed
   GitHub Actions run.
2. **Deduplicate** — each error is fingerprinted. The same error is never
   reported twice; repeat sightings bump a counter on the existing incident.
3. **Analyze** — the agent reads the relevant source in a clone under
   `daemon.workdir` and produces a *what happened / root cause / suggested fix*
   report, including the commit that likely introduced the bug.
4. **Report** — an issue in `suggest` mode, or a branch, a test run, and a pull
   request in `fix` mode.

Each incident records its token count and cost, viewable with
`maajun incidents`.

### Example report

The shape of a filed issue, abridged:

> ### \[maajun] KeyError: 'discount'
>
> **What happened** — Checkout raised an unhandled `KeyError` for carts created
> before a promotion was attached. 41 requests hit it in 12 minutes; every one
> returned a 500 at the payment step.
>
> **Root cause** — `cart/totals.py:88` reads `cart["discount"]` directly. The
> key is only written by `promotions.apply()` (`cart/promotions.py:23`), which
> returns early when no promotion matches — so the key is absent rather than
> zero.
>
> **Likely cause commit** — `4f1c9ab` *"only apply promotions when one
> matches"*, which added that early return.
>
> **Suggested fix**
>
> ```python
> -    discount = cart["discount"]
> +    discount = cart.get("discount", Decimal("0"))
> ```
>
> Source: `logfile:/var/log/shop/error.log` · First seen:
> `2026-08-03T03:14:22Z` · Fingerprint: `9f3c1ab77e02d418`

In `fix` mode the same analysis arrives as a pull request: the applied diff, the
report committed as `docs/incidents/<fingerprint>.md`, and your test suite's
verdict at the top of the body.

> ✅ **Tests pass** — `pytest -q`

A failing suite (`❌ Tests fail (exit 1)`) still opens the PR — a fix that
breaks the tests is exactly what a reviewer needs to see.

## Modes

| Mode | Files | Output |
|------|-------|--------|
| `suggest` (default) | Read-only | Issue with analysis and a suggested patch |
| `fix` | Edits its own clone under `daemon.workdir` | Branch + test run + pull request |

Switch with `maajun config github.mode fix`, or per run with
`-m/--mode` on `watch` and `report`.

## AI providers

DeepSeek and OpenAI are both fully supported and interchangeable — pick either
during `maajun setup`.

| | DeepSeek | OpenAI |
|---|---|---|
| `ai.provider` | `deepseek` | `openai` |
| Default model | `deepseek-v4-flash` | `gpt-4o-mini` |
| With `ai.thinking_mode` | `deepseek-v4-pro` | `gpt-4o` |
| API key | [platform.deepseek.com](https://platform.deepseek.com) | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) |

Switch at any time — the key for each provider is stored separately, so moving
back and forth costs nothing:

```bash
maajun config ai.provider openai
maajun config ai.model gpt-4o        # override the default model
maajun provider-list                 # which providers have a key stored
```

Both speak the same `/chat/completions` protocol, so any compatible gateway,
proxy, or self-hosted server works too — point `ai.base_url` at it, and
`ai.provider` still selects the request dialect and model defaults.

## Configuration

Settings live in `~/.config/maajun/config.toml`; pass `-c/--config PATH` to any
command to use another file. `setup` writes it for you, and `maajun config`
edits it in place with validation and comment preservation:

```bash
maajun config                      # print the whole config (secrets masked)
maajun config github.mode          # print one value
maajun config github.mode fix      # set a value (every repo, if several)
maajun config github.mode fix -r team/api    # ...or just one of them
```

A minimal config:

```toml
[ai]
provider = "deepseek"         # or "openai"
# model = "gpt-4o-mini"       # provider default if omitted
# thinking_mode = true        # use the provider's reasoning model

# One [[github.repos]] entry per repository, added with `maajun add-repo`.
# Omit the section entirely for local mode.
[[github.repos]]
repo = "owner/name"
base_branch = "main"
mode = "suggest"
# test_command = "pytest -q"  # verifies a fix-mode edit; result goes in the PR

[monitor]
log_files = ["/var/log/myapp/error.log"]
poll_interval = 30
# github_actions_repos = ["owner/name"]

[daemon]
workdir = "~/.local/share/maajun"   # clones, incident DB, state
# max_usd_per_day = 5.0             # 0 = no cap
```

At least one error source — log files or GitHub Actions — must be configured;
the daemon refuses to start with nothing to watch. See the
[monitoring guide](https://github.com/Morvin-Ian/maajun/blob/main/docs/monitoring.md)
for every key, detection tuning, and multi-repo setups.

Credentials are never stored in this file. Keys and tokens are read only
from the OS keyring, under the service name `maajun` — `maajun setup` puts them
there. Nothing else is consulted — not environment variables, and not a
`gh auth login` session — so there is exactly one place to look when `status`
and the daemon disagree, and the token maajun pushes with cannot change without
maajun being told.

This means maajun needs a working keyring backend. On a headless server,
install one (`keyrings.alt`, `gnome-keyring`) before running `setup`.

## Commands

| Command | Purpose |
|---------|---------|
| `maajun setup` | Configure everything — provider key, GitHub, error sources |
| `maajun status` | Preflight check of credentials, repo access, and monitors |
| `maajun watch` | Run the monitoring daemon (`--once`, `--dry-run`, `-m`) |
| `maajun report "…"` | Investigate an issue you describe, on demand |
| `maajun incidents` | List handled incidents with repo, status, cost, and links |
| `maajun config [KEY] [VALUE]` | View or change settings (`--repo` for one repo) |
| `maajun add-repo OWNER/NAME` | Watch an additional repository |
| `maajun provider-list` | Show provider support and stored keys |
| `maajun sign-out` | Clear stored credentials |
| `maajun reset` | Delete all config, data, and credentials |

Run `maajun <command> --help` for the full flag list, or see the
[command reference](https://github.com/Morvin-Ian/maajun/blob/main/docs/commands.md).

## Cost control

`daemon.max_usd_per_day` defaults to **$5**. Past that, maajun stops analyzing
for the rest of the UTC day, warns once, and keeps polling — skipped errors are
picked up later, not dropped.

```bash
maajun config daemon.max_usd_per_day 20   # raise the cap; 0 disables it
```

Every incident's exact token count and cost is recorded and shown by
`maajun incidents`, and `--dry-run` prints what an analysis *would* have cost.

Costs are computed from published list prices, in USD per 1M input / output
tokens:

| Model | Input | Output |
|---|---|---|
| `deepseek-v4-flash` | $0.27 | $1.10 |
| `deepseek-v4-pro` | $1.10 | $4.40 |
| `gpt-4o-mini` | $0.15 | $0.60 |
| `gpt-4o` | $2.50 | $10.00 |

On either provider's default model a single analysis costs cents, not dollars —
but measure your own workload with `--dry-run` rather than trusting an
estimate. Rates change; verify them against
[DeepSeek](https://api-docs.deepseek.com/quick_start/pricing) or
[OpenAI](https://openai.com/api/pricing/) before relying on the cap, and note
that a model with no entry is priced at a deliberately conservative $1.00 /
$3.00 so the cap never overshoots.

## Security model

- **No shell access.** The agent has no bash tool in any mode; there is nothing
  to grant.
- **Scoped writes.** `suggest` mode is strictly read-only. `fix` mode may edit
  files only inside maajun's own clone under `daemon.workdir`, never your
  running application.
- **Verification you control.** `test_command` comes from your config, not from
  the model, so a fix cannot redirect its own verification.
- **Token hygiene.** The GitHub token is passed to git via `GIT_ASKPASS`; it
  never lands in a remote URL, `.git/config`, or the process list.

## Documentation

- [How it works](https://github.com/Morvin-Ian/maajun/blob/main/docs/architecture.md)
  — components, monitors, and the incident pipeline
- [Monitoring guide](https://github.com/Morvin-Ian/maajun/blob/main/docs/monitoring.md)
  — full config reference, error sources, cost tracking, running on a VPS
- [Command reference](https://github.com/Morvin-Ian/maajun/blob/main/docs/commands.md)
  — every CLI command and flag

## Development

```bash
uv sync            # install dev dependencies (pytest, ruff)
uv run pytest      # run tests
uv run ruff check  # lint
```

## License

[MIT](https://github.com/Morvin-Ian/maajun/blob/main/LICENSE).
