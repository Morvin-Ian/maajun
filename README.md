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
pull request with the result. In `automatic` mode it takes the fix path only
when deployment identity, targeted reproduction, and post-fix verification are
configured; otherwise it opens a read-only suggestion issue.

Nothing merges without your review, in any mode.

**Contents** — [Requirements](#requirements) · [Installation](#installation) ·
[Quick start](#quick-start) · [How it works](#how-it-works) ·
[Chat](#chat) · [Modes](#modes) ·
[Errors that are not bugs](#errors-that-are-not-bugs) ·
[AI providers](#ai-providers) ·
[Configuration](#configuration) · [Commands](#commands) ·
[Cost control](#cost-control) · [Security model](#security-model) ·
[Documentation](#documentation)

## Requirements

- Python 3.11 or newer
- An API key for one of DeepSeek, OpenAI, or Anthropic — see
  [AI providers](#ai-providers)
- Optional, to open issues and pull requests: GitHub access, set up with
  `maajun login` — the GitHub CLI, a token, or SSH keys

Runtime-error monitoring reads log files, `journalctl`, and `docker logs` on
the local machine, so the daemon has to run on the server your app is deployed
on.

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
`-m automatic` to let configured evidence choose between an issue and a fix
PR. `--dry-run` prints the analysis and its cost without touching GitHub.

### Monitor continuously

```bash
maajun setup             # provider key, GitHub, deployment, error sources
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
   a log file, a systemd unit's journal, or a container's stdout.
2. **Deduplicate** — each error is fingerprinted. The same error is never
   reported twice; repeat sightings bump a counter on the existing incident.
   One that goes quiet and comes back is reported again, as a regression.
3. **Triage** — an error that is a guard doing its job is passed over. A
   rejected login, input that failed validation, a rate limiter returning
   429: the code did what it was built to do, and there is nothing to fix.
   See [Errors that are not bugs](#errors-that-are-not-bugs).
4. **Analyze** — the agent reads the relevant source in a clone under
   `daemon.workdir` and produces a *what happened / root cause / suggested fix*
   report, including the commit that likely introduced the bug and how the app
   runs where it broke (folder, port, docker or systemd).
5. **Report** — an issue in `suggest` mode, or a branch, a test run, and a pull
   request in `fix` mode. `automatic` chooses between those paths from
   owner-controlled deployment and verification evidence. Always one or the
   other, and never empty: a report
   that comes back blank is re-asked once, then abandoned as a failed
   incident rather than filed as an empty issue.

The issue, the pull request, and the commit are all titled from the report's
own one-line finding — not from the log line that triggered the run. An
exception surfaces in one place and the defect is regularly in another, so
`KeyError: 'discount'` would send a reader to the file that raised rather
than the file that has to change. Titling from the analysis keeps the name of
a bug and the fix for it pointing at the same thing. `--dry-run` prints the
title it would file, so a mismatch is visible before anything is published.

Each incident records its token count and cost, viewable with
`maajun incidents`.

### Example report

The shape of a filed issue, abridged:

> ### \[maajun] cart/totals.py assumes promotions.apply() always writes a discount
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

An issue created in `suggest` mode can be promoted later, after a person has
reviewed the analysis:

```bash
maajun promote 9f3c1ab77e02d418
maajun promote https://github.com/you/shop/issues/29
```

Promotion reads the recorded issue, checks the current base branch again, and
opens a fix PR containing `Fixes <issue URL>`. It does not trust or blindly
apply the old suggested patch, change the repository's saved mode, close the
issue, merge the PR, or deploy it. If current code offers no fix, maajun leaves
the original issue open and saves the new analysis locally instead of creating
a duplicate issue or an empty PR.

A fix-mode run that writes the report but changes no code is asked once more
for the edit. If it still finds nothing in the repository that should differ,
the analysis is filed as an **issue** rather than a pull request with no diff
in it — a PR that looks like a fix until you open the Files tab wastes a
review. The issue says the fix was attempted, so it is not mistaken for
suggest mode.

Every pull request maajun opens therefore has a code change in it. The
committed report is not one, and neither is anything an earlier incident left
behind — the clone is reset to the base branch before each investigation, and
the commit is checked against that branch one last time before the branch is
pushed. Nothing to merge means an issue, not a push.

Work deliberately left outside a fix is not dumped into a generic companion
issue. Each follow-up must name a concrete code location, the change to make,
and an observable acceptance check. Maajun gives vague tasks one read-only
rewrite attempt, then files each valid task as its own specifically titled
issue linked to the PR (at most three). Missing evidence, environment notes,
unrelated test failures, and generic cleanup are not filed as follow-ups.

> ✅ **Tests pass** — `pytest -q`

A failing suite (`❌ Tests fail (exit 1)`) still opens the PR — a fix that
breaks the tests is exactly what a reviewer needs to see.

## Errors that are not bugs

Plenty of logged errors are the software working. A validator refusing a
malformed email, a 401 for a bad password, a rate limiter returning 429, a
404 for a row that was never there — every one is a guard doing exactly what
it was built to do. Filing a GitHub issue for those buries the errors that
are real, so maajun does not.

Two passes decide, and neither one deletes anything:

**Signatures, before any AI call.** An error named after its own intent —
`ValidationError`, `PermissionDenied`, `403 Forbidden`, `RateLimitExceeded`,
`CSRF`, `429 Too Many Requests`, `404 Not Found` — is closed without being
analyzed, so it costs nothing. Deliberately narrow: it can only recognise
errors that announce what they refused.

**The agent's verdict, after reading the code.** Every report opens with
`defect` or `by design`. That is what catches a guard specific to your
application — a paywall, a quota, a feature flag — which no shipped pattern
could know about. A `by design` verdict is not published: no issue, no PR,
no commit. A report that does not say, or says it cannot tell, is filed as a
defect. Silence never suppresses a report.

Both are visible and reversible:

```bash
maajun incidents --ignored     # what was passed over, and why
```

```toml
[monitor]
ignore_by_design = false                    # analyze everything, however obvious
ignore_patterns = ["PaywallError", "QuotaExceeded"]   # your own guards
```

`ignore_patterns` are regexes matched against the raw error, tried before the
shipped signatures. A pattern that does not compile is logged and skipped
rather than stopping the daemon.

A guard that fires on input that *should* have been accepted is a defect, and
so is one whose refusal escapes as an unhandled 500 — the check was intended,
crashing on it was not. The prompt says so explicitly, so "by design" is not
an exit from a hard investigation.

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
| `automatic` | Read-only unless the evidence gate is ready | Suggestion issue or verified fix PR |

Switch with `maajun config github.mode fix`, or per run with
`-m/--mode` on `watch` and `report`.

Automatic mode selects the fix path only when all three are present: an active
deployment identity (`deployment.path`, `runs`, or `service_command`), a
`reproduction_command`, and at least one `test_command` or
`verification_commands` entry. A known Python runtime mismatch keeps the run
read-only. The decision applies to one incident and never rewrites the saved
mode. A selected fix still has to pass the deployment-applicability,
verification, quality-review, and publication gates; failure produces an issue
or local report, never a merge or deployment.

Fix mode may still conclude that no code change is warranted. When it does,
you get an issue rather than a pull request whose only diff is the incident
report — the issue says why.

## AI providers

Three vendors and three gateways, all interchangeable. `maajun setup` lists
them grouped by kind, cheapest first, and defaults to the first:

```
  Vendors (their own models)
    1. deepseek
    2. openai
    3. anthropic

  Gateways (one key, many vendors' models)
    4. openrouter
    5. straitly
    6. bai

  > AI provider (number, or a name) [deepseek]:
```

| | DeepSeek | OpenAI | Anthropic |
|---|---|---|---|
| `ai.provider` | `deepseek` | `openai` | `anthropic` |
| Default model | `deepseek-v4-flash` | `gpt-4o-mini` | `claude-haiku-4-5` |
| With `ai.thinking_mode` | `deepseek-v4-pro` | `gpt-4o` | `claude-opus-5` |
| API key | [platform.deepseek.com](https://platform.deepseek.com) | [platform.openai.com](https://platform.openai.com/api-keys) | [console.anthropic.com](https://console.anthropic.com/settings/keys) |

### Choosing a model

`maajun setup` lists what the provider offers with its price and its role, so
the choice is made with the cost in view:

```
  Models:
    1. claude-haiku-4-5 — $1.00 in / $5.00 out per 1M tokens (default)
       The fastest and cheapest Claude.
    2. claude-sonnet-5 — $2.00 in / $10.00 out per 1M tokens
       Mid tier: more capable than Haiku, well under Opus in price.
    3. claude-opus-5 — $5.00 in / $25.00 out per 1M tokens (thinking_mode picks this)
       The most capable, and the dearest.

  > Model (number, or an id to use one not listed) [claude-haiku-4-5]:
```

Answer with a number or with any model id — a dated snapshot, or something
released after this build. Choosing the provider's own default leaves
`ai.model` unset, so the default moves when the provider replaces its cheap
tier. `maajun setup --model gpt-4o` skips the prompt.

Changing provider afterwards clears `ai.model`, because a model id belongs to
the provider it was chosen for:

```bash
maajun config ai.provider anthropic   # clears ai.model, and says so
maajun config ai.model claude-opus-5
```

### Gateways

`openrouter`, `straitly` and `bai` are gateways: one key reaching many
vendors' models.

| | OpenRouter | Straitly | BAI |
|---|---|---|---|
| `ai.provider` | `openrouter` | `straitly` | `bai` |
| Model ids | `anthropic/claude-opus-5` | `anthropic/claude-opus-5` | `gpt-5.2` |
| Models | [openrouter.ai/models](https://openrouter.ai/models) | [straitly.ai/models](https://straitly.ai/models) | [docs.b.ai](https://docs.b.ai) |
| API key | [openrouter.ai](https://openrouter.ai/settings/keys) | [straitly.ai](https://straitly.ai/) | [chat.b.ai](https://chat.b.ai) |

None has a default model — their catalogues change, and nothing here should
guess which one your key can reach — so `ai.model` is required.

Rather than ship a list that goes stale, `maajun setup` asks the gateway
itself: every one of them serves `GET /v1/models`, which names each model and
prices it. Vendor first, because a gateway carries hundreds:

```
  openrouter carries 396 models from 51 vendors:
    1. anthropic (32)
    2. deepseek (17)
    ...
  > Vendor (number, or a model id to skip ahead): 1

  Models:
    1. anthropic/claude-haiku-4.5 — $1.00 in / $5.00 out per 1M tokens
    2. anthropic/claude-opus-5 — $5.00 in / $25.00 out per 1M tokens
    ...
  > Model (number, or an id):
```

Either prompt also takes an id outright, and a gateway that cannot be reached
falls back to asking for one. `--model` skips the whole step:

```bash
maajun setup --provider openrouter --model anthropic/claude-opus-5
maajun setup --provider bai --model gpt-5.2
```

The prices in that list are the gateway's own — a reseller discounts some
models and gives others away — but the spend cap still costs a run from the
table below, which it reaches through both the vendor prefix and the dots a
gateway writes versions with: `anthropic/claude-haiku-4.5` is priced as
`claude-haiku-4-5`. A model with no entry there is costed at the dearest rate
maajun knows, and setup says so, alongside what the gateway quoted, when you
pick one.

Switch at any time — the key for each provider is stored separately, so moving
back and forth costs nothing:

```bash
maajun config ai.provider openai
maajun config ai.model gpt-4o        # override the default model
maajun provider-list                 # which providers have a key stored
```

DeepSeek, OpenAI, and all three gateways speak `/chat/completions`, so any other
compatible gateway, proxy, or self-hosted server works too — point
`ai.base_url` at it, and `ai.provider` still selects the request dialect and
model defaults.
Anthropic runs on the Messages API through the official SDK instead, and gets
an explicit cache breakpoint on every request: without one Anthropic caches
nothing, and each tool round would re-read the whole prompt at full price.

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
provider = "deepseek"         # or "openai", "anthropic"
# model = "gpt-4o-mini"       # provider default if omitted
# thinking_mode = true        # use the provider's reasoning model

# One [[github.repos]] entry per repository, added with `maajun add-repo`.
# Omit the section entirely for local mode.
[[github.repos]]
repo = "owner/name"
base_branch = "main"
mode = "suggest"  # or "fix" / "automatic"
# test_command = "pytest -q"  # verifies a fix-mode edit; result goes in the PR
# verification_commands = ["ruff check .", "mypy src"]  # each runs independently
# reproduction_command = "pytest -q tests/test_checkout_bug.py"  # fail before, pass after

# Where and how it runs, and where its runtime errors land. Any mix of the
# three sinks; fill it in with `maajun discover --save`.
[github.repos.deployment]
path = "/srv/myapp"
port = 8000
runs = "docker compose"
service_unit = "myapp.service"
service_command = "/srv/myapp/.venv/bin/uvicorn app:api --port 8000"
proxy_kind = "nginx"
proxy_config_path = "/etc/nginx/sites-available/api.example.com"
proxy_body_limit = "1m (nginx default; no active directive found)"
config_owner = "operator"  # active proxy config is not tracked here
# infra_repo = "owner/infrastructure"  # route operator work here
log_files = ["/var/log/nginx/error.log"]
docker_containers = ["myapp-web-1"]
# journald_units = ["myapp.service"]

[monitor]
log_files = ["/var/log/myapp/error.log"]
poll_interval = 30

[daemon]
workdir = "~/.local/share/maajun"   # clones, incident DB, state
# max_usd_per_day = 5.0             # 0 = no cap

[chat]
# max_usd_per_day = 5.0             # `maajun chat`'s own budget; 0 = no cap
```

At least one error source must be configured; the daemon refuses to start
with nothing to watch. See the
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

Most servers have no keyring. maajun does not stop for that: credentials go
in `~/.config/maajun/credentials.json`, a file only your user can read
(`chmod 600`), and setup says so before it asks for anything. To use a
keyring instead, install a backend for the environment maajun lives in —
setup prints the right command (`pipx inject maajun keyrings.alt`, or the
uv/pip equivalent). `maajun status` names wherever the credentials it has
are kept.

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

Costs are computed from published list prices, in USD per 1M tokens. Input is priced three ways: a prompt prefix the provider has seen
before is re-served from its cache far more cheaply than a fresh one, and
maajun counts each from the token counts the provider reports.

| Model | Input (fresh) | Input (cache hit) | Input (cache write) | Output |
|---|---|---|---|---|
| `deepseek-v4-flash` | $0.44 | $0.014 | $0.44 | $1.32 |
| `deepseek-v4-flash-vision-exp` | $0.44 | $0.014 | $0.44 | $1.32 |
| `deepseek-v4-pro` | $1.32 | $0.044 | $1.32 | $3.96 |
| `gpt-4o-mini` | $0.15 | $0.075 | $0.15 | $0.60 |
| `gpt-4o` | $2.50 | $1.25 | $2.50 | $10.00 |
| `claude-haiku-4-5` | $1.00 | $0.10 | $1.25 | $5.00 |
| `claude-sonnet-5` | $2.00 | $0.20 | $2.50 | $10.00 |
| `claude-opus-5` | $5.00 | $0.50 | $6.25 | $25.00 |

Anthropic is the one provider that charges to *write* the cache — 1.25x a
fresh token — so that column is counted separately rather than assumed free.

The DeepSeek rows are its **peak** rates. Off-peak it charges half of them,
and maajun applies that automatically from the clock: peak is 01:00–04:00 and
06:00–10:00 UTC on weekdays, and Saturday and Sunday in Beijing time (UTC+8,
so from 16:00 UTC Friday) are off-peak all day. OpenAI has no such schedule
and is never discounted.

Because an investigation resends a growing prompt on every tool round, most
of its input tokens are cache hits — so the same run costs a fraction of what
the cache-miss column suggests. `maajun incidents` and `--dry-run` report what
was actually billed.

On any provider's default model a single analysis costs cents, not dollars —
but measure your own workload with `--dry-run` rather than trusting an
estimate. Rates change; verify them against
[DeepSeek](https://api-docs.deepseek.com/quick_start/pricing),
[OpenAI](https://developers.openai.com/api/docs/pricing), or
[Anthropic](https://docs.claude.com/en/docs/about-claude/pricing) before
relying on the cap. A model with no entry is costed at the most expensive rate in the table
above, with no cache discount and no off-peak discount, so the cap errs
towards stopping early rather than overshooting. The same goes for a gateway
that reports no cache hits: every input token is charged at the miss rate.

## Security model

- **No shell access.** The agent has no bash tool in any mode; there is nothing
  to grant.
- **Scoped reads and writes.** Every file tool is confined to an explicit
  set of directories, enforced by the tool layer rather than asked for in a
  prompt. The daemon's agent sees only the clone it is analyzing; `maajun
  chat` sees the directory you launched it in, `daemon.workdir`, and the log
  files named in your config. `suggest` mode is read-only on top of that;
  `fix` mode may edit inside the clone, never your running application.
  `automatic` receives one of those same two policies after its evidence gate;
  it is not a third, broader permission set.

- **Secrets are refused, not merely unrequested.** `.env` files, SSH and TLS
  keys, `.netrc`, `.git-credentials`, anything under `.git/`, and maajun's own
  incident database cannot be opened by any tool, even when they sit inside an
  allowed directory. What a tool reads goes to your AI provider — and, from the
  daemon, into an issue or pull request.
- **Verification you control.** `test_command`, `verification_commands`, and
  `reproduction_command` come from your config, not from the model, so a fix
  cannot redirect its own verification. Every post-fix command runs even when
  an earlier one fails, and reproduction is reported before and after the edit.
- **Deployment-applicable fixes.** Discovery records the active systemd
  command, reverse-proxy configuration, and the effective Nginx request-body
  boundary when it can. A fix-path run will not publish a PR that changes an
  unmapped repository proxy file; it leaves an issue in the target repository
  instead.
- **Independent publication review.** Live fix-path diffs get one read-only
  review after owner-controlled verification, against the deployment, product
  boundary and behavioral tests. One correction is allowed and all configured
  checks run again; a still-blocked change is never pushed. Its issue labels
  the local change as an unpublished draft and titles the unresolved failure.
- **Infrastructure findings go to their owner.** When a withheld fix contains
  an unmapped operator-owned deployment edit and `deployment.infra_repo` is
  configured, Maajun files the review issue there. Passive events pass the same
  public/private visibility gate; Maajun never deploys or reloads services.
- **Runtime evidence is sanitised.** Credentials, cookies, provider tokens,
  request bodies, URL passwords, IP/email addresses, UUIDs and query values
  are redacted at ingestion and checked again before reports, issues and pull
  requests leave the investigation boundary.
- **Public runtime publication is opt-in.** Maajun verifies repository
  visibility with GitHub before publishing a passively caught incident.
  Public or unknown targets stay local unless public publication is explicitly
  enabled, or the issue can be routed to a configured private/internal runtime
  artifact repository. Manual reports and promotions remain owner-directed.
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
