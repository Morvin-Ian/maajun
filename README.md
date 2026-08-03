# Maajun

[![CI](https://github.com/Morvin-Ian/maajun/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Morvin-Ian/maajun/actions/workflows/ci.yml?query=branch%3Amain)
[![PyPI](https://img.shields.io/pypi/v/maajun.svg)](https://pypi.org/project/maajun/)
[![Python](https://img.shields.io/pypi/pyversions/maajun.svg)](https://pypi.org/project/maajun/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/Morvin-Ian/maajun/blob/main/LICENSE)

**Error monitoring that files the bug report — and can write the fix.**

Maajun watches your error sources — local log files (e.g. your app's error
log on a VPS) and failed GitHub Actions runs — and when a new error
appears it investigates the code and documents the incident on GitHub:
in *suggest* mode it files an issue with the analysis and a suggested fix;
in *fix* mode it applies the fix on a branch, runs your test suite against
it, and opens a pull request with the result. It records what each analysis
cost, caps what it may spend per day, and never reports the same error twice.

```
error detected ──▶ fingerprint & dedup ──▶ AI reads your code
  (logs / CI)                                        │
   issue (suggest) ◀───────────────────────────────  ┤
   PR    (fix)     ◀──── branch + applied fix ───────┘
```

## What it actually files

An unhandled `KeyError` hits your error log at 3 a.m. Nobody is awake. By
morning there is an issue on the repo — this is the shape of it (`suggest`
mode, the default):

> ### \[maajun] KeyError: 'discount'
>
> # `KeyError: 'discount'` when a cart has no promotion applied
>
> ## What happened
>
> Checkout raised an unhandled `KeyError` for carts created before a
> promotion was attached. 41 requests hit it in 12 minutes; every one
> returned a 500 to the customer at the payment step.
>
> ## Root cause
>
> `cart/totals.py:88` reads `cart["discount"]` directly. The key is only
> written by `promotions.apply()` (`cart/promotions.py:23`), which is
> skipped entirely when no promotion matches — so the key is absent rather
> than zero. The `.get()` used one line above at `totals.py:87` is what
> makes the omission easy to miss on review.
>
> ## Likely cause commit
>
> `4f1c9ab` — *"only apply promotions when one matches"*. It added the
> early return in `promotions.apply()` that leaves `discount` unset; every
> caller before it could assume the key existed.
>
> ## Suggested fix
>
> Default the lookup, so an absent promotion means no discount:
>
> ```python
> -    discount = cart["discount"]
> +    discount = cart.get("discount", Decimal("0"))
> ```
>
> ---
>
> ## Error details
>
> ```
> Traceback (most recent call last):
>   File "/srv/shop/checkout/views.py", line 142, in post
>     total = compute_total(cart)
>   File "/srv/shop/cart/totals.py", line 88, in compute_total
>     discount = cart["discount"]
> KeyError: 'discount'
> ```
>
> - Source: `logfile:/var/log/shop/error.log`
> - First seen: 2026-08-03T03:14:22Z
> - Fingerprint: `9f3c1ab77e02d418`
> - Opened automatically by [maajun](https://github.com/Morvin-Ian/maajun).

In `fix` mode the same analysis arrives as a **pull request** instead: the
applied diff, the report committed as `docs/incidents/<fingerprint>.md`, and
your own test suite's verdict at the top of the body —

> ✅ **Tests pass** — `pytest -q`
> <details><summary>Output</summary>…</details>

— or `❌ **Tests fail** (exit 1)`, which still opens the PR, because
"this fix breaks the suite" is exactly what a reviewer needs to know.

**Nothing merges without your review**, in either mode.

## Install

```bash
uv tool install maajun     # or: pipx install maajun / pip install maajun
```

From source:

```bash
git clone https://github.com/Morvin-Ian/maajun && cd maajun && uv sync
```

## Quick start

Investigate something right now, without setting up any monitoring:

```bash
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
than one repo with `maajun add-repo <owner/name>`, re-check your wiring
with `maajun status`, and review what it did with `maajun incidents`.

The same error is never reported twice — repeat sightings only bump a counter.

## What it costs, and what it can do

Both questions people reasonably ask before pointing an AI daemon at their
repo with their API key in it:

**Spend is bounded by default.** `daemon.max_usd_per_day` starts at **$5**:
past that, maajun stops analyzing for the rest of the UTC day, warns once,
and keeps polling — skipped errors are picked up later, not dropped. Raise
it with `maajun config daemon.max_usd_per_day 20`, or set `0` for no cap.
Every incident's exact token count and cost is recorded and shown by
`maajun incidents`; `--dry-run` prints what an analysis *would* have cost
before you commit to anything. At DeepSeek's published rate ($0.27/$1.10 per
1M input/output tokens) an analysis is cents, not dollars — but measure your
own with `--dry-run` rather than trusting an estimate.

**The agent is deliberately small.** It has no shell access in any mode —
there is no bash tool to grant. In `suggest` mode it is strictly read-only;
in `fix` mode it may edit files *only* inside its own clone under
`daemon.workdir`, never your running application. Your `test_command` comes
from your config, not from the model, so verification can't be redirected.
The GitHub token is passed to git via `GIT_ASKPASS` and never lands in a
remote URL, `.git/config`, or the process list.

## Documentation

- [How it works](https://github.com/Morvin-Ian/maajun/blob/main/docs/architecture.md)
  — components, monitors, and the incident pipeline
- [Monitoring guide](https://github.com/Morvin-Ian/maajun/blob/main/docs/monitoring.md)
  — config reference, error sources (logs, GitHub Actions), cost tracking,
  running on a VPS
- [Command reference](https://github.com/Morvin-Ian/maajun/blob/main/docs/commands.md)
  — every CLI command and flag

## Supported AI providers

DeepSeek and OpenAI. Both speak the same wire protocol, so any compatible
gateway works too — point `ai.base_url` at it. Pick one during
`maajun setup`, or switch later with `maajun config ai.provider openai`.

## Development

```bash
uv sync            # installs dev dependencies (pytest, ruff)
uv run pytest      # run tests
uv run ruff check  # lint
```

## License

[MIT](https://github.com/Morvin-Ian/maajun/blob/main/LICENSE).
