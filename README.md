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
[Chat](#chat) · [Modes](#modes) · [AI providers](#ai-providers) ·
[Configuration](#configuration) · [Commands](#commands) ·
[Cost control](#cost-control) · [Security model](#security-model) ·
[Documentation](#documentation)

## Requirements

- Python 3.11 or newer
- An API key for DeepSeek or OpenAI — see [AI providers](#ai-providers)
- Optional, to open issues and pull requests: GitHub access, set up with
  `maajun login` — the GitHub CLI, a token, or SSH keys

Runtime-error monitoring reads log files, `journalctl`, and `docker logs` on
the local machine, so the daemon has to run on the server your app is deployed
on. The GitHub Actions monitor works from anywhere with network access.

## Installation

```bash
uv tool install maajun     # or: pipx install maajun
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
maajun setup             # provider key, GitHub, deployment, GitHub Actions
maajun status            # what is watched, and what is not
maajun watch             # watch in the background; the terminal comes back
maajun watch --status    # what has it done?
maajun watch --stop      # stop it
```

`maajun login` sets up GitHub access on its own, if you would rather do that
first — it offers the GitHub CLI (it runs the login for you), a token, or
your SSH keys, and then works out where each repo's errors land.

`setup` asks for the provider and its key, then offers GitHub and the error
sources in turn. For each repo it probes **this machine** for how the app is
deployed — its folder, its port, and whether its errors land in a log file, a
systemd unit's journal, or a container's stdout — and offers what it finds, so
the runtime errors your users hit are watched without you typing a path from
memory. It detects the repo from your `origin` remote, and re-runs safely:
every prompt defaults to your current configuration.

However you deploy — bare gunicorn, systemd, docker, compose, nginx in a
container or on the host — errors land in one of those three sinks, and
`maajun discover` finds which:

```bash
maajun discover                     # print what it finds, change nothing
maajun discover -r you/app --save   # record it
```

It also **reads the code** to answer what the host cannot: the stack, the
entrypoint, which files the logging config really writes, and where errors
are swallowed instead of logged — a bare `except: pass`, a 500 handler that
logs nothing, a log directory nothing creates. What maajun watches then
comes from the code rather than from memory.

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

1. **Detect** — a monitor picks up a new error where that repo's errors land:
   a log file, a systemd unit's journal, a container's stdout, or a failed
   GitHub Actions run.
2. **Deduplicate** — each error is fingerprinted. The same error is never
   reported twice; repeat sightings bump a counter on the existing incident.
   One that goes quiet and comes back is reported again, as a regression.
3. **Analyze** — the agent reads the relevant source in a clone under
   `daemon.workdir` and produces a *what happened / root cause / suggested fix*
   report, including the commit that likely introduced the bug and how the app
   runs where it broke (folder, port, docker or systemd).
4. **Report** — an issue in `suggest` mode, or a branch, a test run, and a pull
   request in `fix` mode. Always one or the other, and never empty: a report
   that comes back blank is re-asked once, then abandoned as a failed
   incident rather than filed as an empty issue.

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

## Chat

`maajun chat` is a conversational front end to everything above. It knows
every command — the list is read from the CLI itself, so it is never out of
date — and it remembers every incident maajun has handled.

```bash
maajun chat
```

Ask it how to do something and it answers from the real `--help`. Ask it to
*do* the thing and it runs the command, showing you the exact line first if
it changes anything:

```
> watch acme/web as well, in fix mode, verified with pytest

▸ Run: maajun add-repo acme/web -m fix
  Run it? (y/N): y
```

Answers stream as they are written, and each command or file edit is shown
as it runs. `maajun chat -p "..."` answers one question and exits, for a
script or a keybinding.

Read-only commands run straight away. `watch`, `reset`, and `sign-out` are
never run from chat — it hands you the command instead. And because every
incident, pull request, and issue is already in the database, so is the
history:

```
> what did that checkout KeyError turn out to be?
> which PRs have you opened against acme/api this month?
```

Past chat sessions are searchable too — by words in any order, over any
date range — and chat spend is recorded (`/cost`) and capped by
`chat.max_usd_per_day`, separately from the daemon's budget.
See the [command reference](https://github.com/Morvin-Ian/maajun/blob/main/docs/commands.md#maajun-chat)
for the slash commands and the full permission model.

## Modes

| Mode | Files | Output |
|------|-------|--------|
| `suggest` (default) | Read-only | Issue with analysis and a suggested patch |
| `fix` | Edits its own clone under `daemon.workdir` | Branch + test run + pull request |

Switch with `maajun config github.mode fix`, or per run with
`-m/--mode` on `watch` and `report`.

Fix mode may still conclude that no code change is warranted. When it does,
you get an issue rather than a pull request whose only diff is the incident
report — the issue says why.

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

# Where and how it runs, and where its runtime errors land. Any mix of the
# three sinks; fill it in with `maajun discover --save`.
[github.repos.deployment]
path = "/srv/myapp"
port = 8000
runs = "docker compose"
log_files = ["/var/log/nginx/error.log"]
docker_containers = ["myapp-web-1"]
# journald_units = ["myapp.service"]

[monitor]
log_files = ["/var/log/myapp/error.log"]
poll_interval = 30
# github_actions_repos = ["owner/name"]

[daemon]
workdir = "~/.local/share/maajun"   # clones, incident DB, state
# max_usd_per_day = 5.0             # 0 = no cap

[chat]
# max_usd_per_day = 5.0             # `maajun chat`'s own budget; 0 = no cap
```

At least one error source — log files or GitHub Actions — must be configured;
the daemon refuses to start with nothing to watch. See the
[monitoring guide](https://github.com/Morvin-Ian/maajun/blob/main/docs/monitoring.md)
for every key, detection tuning, and multi-repo setups.

Credentials are never stored in this file. Provider API keys are read only
from the OS keyring, under the service name `maajun` — `maajun setup` puts
them there. Environment variables are never consulted.

The GitHub token is read from that keyring, and failing that from your
GitHub CLI session — a credential you already manage, which maajun borrows
rather than copies. `maajun login` picks between them, and `maajun status`
says which is in use, so there is no doubt about what the daemon will push
with. Branches can go over SSH instead, keeping the token to the API.

Most servers have no keyring. `maajun setup` finds that out **before** it
asks for anything, and offers the choice: keep credentials in
`~/.config/maajun/credentials.json`, a file only your user can read
(`chmod 600`), or install a keyring backend and start again — with the
command for the way you installed maajun (`pipx inject maajun keyrings.alt`,
`uv tool install maajun --with keyrings.alt`, `pip install keyrings.alt`).
It never writes a secret to disk without being told to, and `maajun status`
says where the ones it has are kept.

## Commands

| Command | Purpose |
|---------|---------|
| `maajun setup` | Configure everything — provider key, GitHub, error sources |
| `maajun login` | Choose how to reach GitHub: the CLI, a token, or SSH keys |
| `maajun status` | Preflight check of credentials, repo access, and monitors |
| `maajun discover` | Find how each repo is deployed and where its errors land (`--save`) |
| `maajun watch` | Watch in the background (`--status`, `--stop`, `-f`, `--dry-run`) |
| `maajun report "…"` | Investigate an issue you describe, on demand |
| `maajun chat` | Ask maajun anything, have it run commands, recall past work |
| `maajun incidents` | List handled incidents with repo, status, cost, and links |
| `maajun config [KEY] [VALUE]` | View or change settings (`--repo` for one repo) |
| `maajun add-repo REPO` | Watch another repository (`myapp`, or `owner/myapp`) |
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
maajun config chat.max_usd_per_day 20     # the same, for `maajun chat`
```

`maajun chat` has its own $5 daily cap. Past it a new question is refused
with the command to raise it; an answer already being written is never cut
off part-way.

Every incident's exact token count and cost is recorded and shown by
`maajun incidents`, and `--dry-run` prints what an analysis *would* have cost.

Costs are computed from published list prices, in USD per 1M input / output
tokens:

| Model | Input | Output |
|---|---|---|
| `deepseek-v4-flash` | $0.14 | $0.28 |
| `deepseek-v4-pro` | $0.435 | $0.87 |
| `gpt-4o-mini` | $0.15 | $0.60 |
| `gpt-4o` | $2.50 | $10.00 |

On either provider's default model a single analysis costs cents, not dollars —
but measure your own workload with `--dry-run` rather than trusting an
estimate. Rates change; verify them against
[DeepSeek](https://api-docs.deepseek.com/quick_start/pricing) or
[OpenAI](https://developers.openai.com/api/docs/pricing) before relying on the
cap. A model with no entry is costed at the most expensive rate in the table
above, so the cap errs towards stopping early rather than overshooting.

DeepSeek bills cached input at a fraction of these rates. Maajun costs every
input token at the cache-miss rate, so a repetitive workload will report
somewhat more than it actually spends.

## Security model

- **No shell access.** The agent has no bash tool in any mode; there is nothing
  to grant.
- **Scoped reads and writes.** Every file tool is confined to an explicit
  set of directories, enforced by the tool layer rather than asked for in a
  prompt. The daemon's agent sees only the clone it is analyzing; `maajun
  chat` sees the directory you launched it in, `daemon.workdir`, and the log
  files named in your config. `suggest` mode is read-only on top of that;
  `fix` mode may edit inside the clone, never your running application.

- **Secrets are refused, not merely unrequested.** `.env` files, SSH and TLS
  keys, `.netrc`, `.git-credentials`, anything under `.git/`, and maajun's own
  incident database cannot be opened by any tool, even when they sit inside an
  allowed directory. What a tool reads goes to your AI provider — and, from the
  daemon, into an issue or pull request.
- **Verification you control.** `test_command` comes from your config, not from
  the model, so a fix cannot redirect its own verification.
- **Chat proposes, you approve.** `maajun chat` runs read-only commands freely,
  but anything that writes config or opens a pull request shows the exact
  command line and waits for a yes — or for a reason not to, which is passed
  on as an instruction rather than a bare refusal. A file edit shows its diff and the
  absolute path, flagged when it falls outside the project directory and
  `daemon.workdir`. `reset` and `sign-out` it will not run at all.
  `run_maajun_command` reaches maajun's own subcommands and nothing else —
  it is not a shell.
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
