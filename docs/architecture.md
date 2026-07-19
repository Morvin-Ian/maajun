# How Maajun works

## Components

```
src/maajun/
├── agent/            The AI agent
│   ├── core.py         Agent loop: model ⇄ tools until a final answer
│   └── tools/          read_file, edit_file, write_file, glob, grep,
│                       bash, list_dir, git_status
├── providers/        AI backends (DeepSeek today; OpenAI-compatible API)
├── monitors/         Error sources
│   ├── base.py         Monitor contract, fingerprinting, HTTPPollMonitor
│   ├── logfile.py      Tails local log files for tracebacks/error lines
│   ├── sentry.py       Polls the Sentry API for unresolved issues
│   └── github_actions.py  Polls GitHub for failed workflow runs
├── vcs/              Git workspace + GitHub REST client
├── daemon.py         The watch pipeline: monitor → analyze → PR
├── state.py          SQLite incident store (dedup, cost/token totals)
├── notifications.py  Webhook alerts (Slack-compatible)
├── costs.py          Token-count → USD pricing per model
├── config.py         TOML config (~/.config/maajun/config.toml)
├── auth.py           Secrets: OS keyring, env-var fallback
├── utils/            Shared helpers (timestamps, GitHub API headers)
└── cli.py            Typer CLI
```

## The agent loop

The agent sends the conversation plus tool definitions to the model. When
the model requests tool calls, the agent executes them, feeds the results
back, and repeats — up to 50 rounds — until the model answers in plain
text. Responses stream token-by-token, including during tool rounds.

Every response carries the model that produced it and its token usage, so
the daemon can price each incident accurately (see
[Cost tracking](monitoring.md#cost-tracking)).

### Provider resilience

API calls to the provider retry automatically on transient failures —
HTTP 429, 500, 502, 503, and connection errors — up to 3 attempts with
exponential backoff and jitter (capped at 30s between attempts).
Authentication errors and other client errors fail immediately; a bad key
is reported on the first call, not after a retry dance.

### Tool permissions

Tools are split into two classes:

| Class | Tools | Behavior |
|-------|-------|----------|
| Safe (read-only) | `read_file`, `glob`, `grep`, `list_dir`, `git_status` | Always allowed |
| Gated | `bash`, `edit_file`, `write_file` | Need approval per call |

Approval is an injectable async callback. Each context supplies its own
policy:

- **`maajun chat`** — asks you interactively before each gated call
  (`--auto-approve` skips the prompts).
- **`maajun watch`, suggest mode** — no callback, so every gated call is
  denied: the agent is strictly read-only.
- **`maajun watch`, fix mode** — file edits are approved only for paths
  inside the daemon's isolated workspace clone; `bash` is always denied.

A denied call is not an error: the model receives a message telling it
the user refused and not to retry, so it adapts (e.g. writes the fix as
a suggestion instead).

## Monitors

A monitor is anything that can answer "what new errors happened since I
last asked?" — the daemon just polls whichever ones the config enables
and treats their output identically. All three produce the same
normalized `ErrorEvent` (source, message, details, fingerprint), so the
rest of the pipeline doesn't care where an error came from.

- **Log files** (`monitors/logfile.py`) — tails files incrementally,
  surviving rotation and truncation. Recognizes Python tracebacks
  (including ones split across polls) and lines matching the configured
  error pattern. Requires maajun to run on the machine that writes the
  logs.
- **Sentry** (`monitors/sentry.py`) — polls the Sentry API for
  unresolved issues, so errors from *deployed* apps reach maajun without
  any shared filesystem. Each Sentry issue (already grouped and
  fingerprinted by Sentry) becomes one incident.
- **GitHub Actions** (`monitors/github_actions.py`) — polls for failed
  workflow runs, turning CI breakage into incidents. Failures are
  fingerprinted by commit SHA, so several red workflows on the same
  commit collapse into a single incident.

The Sentry and GitHub Actions monitors share a base class,
`HTTPPollMonitor`, which owns the HTTP client, remembers which item ids
it has already emitted, and swallows (but logs) fetch failures — a
monitor that can't reach its API returns no events rather than crashing
the daemon. Adding a new HTTP-polled source means implementing three
methods: fetch the items, identify one, convert one to an `ErrorEvent`.

## The incident pipeline (`maajun watch`)

1. **Detect** — monitors poll their error sources every
   `monitor.poll_interval` seconds. A failing monitor is logged and
   skipped; the others still run.
2. **Dedup** — every event gets a fingerprint. For log errors it's a
   hash of the error text with digits and hex addresses stripped, so the
   same crash at a different line number or timestamp is still the same
   incident. Sentry issues use their Sentry short id; CI failures use
   the commit SHA. Fingerprints live in a SQLite store; known ones just
   bump a counter.
3. **Analyze** — for a new fingerprint, the daemon syncs an isolated
   clone of your repo, creates a branch `maajun/incident-<fingerprint>`,
   and asks the agent to investigate. The agent reads the code with its
   safe tools and writes a structured report (what happened / root cause
   / suggested fix). In fix mode it may also edit files in the clone.
4. **Publish** — the report is committed as
   `docs/incidents/<fingerprint>.md`, the branch is pushed, and a pull
   request is opened with the report as its body. If the branch already
   has an open PR it is reused, never duplicated.
5. **Record & notify** — the incident is marked processed with its PR
   URL, token counts, and USD cost. If webhooks are configured, a
   notification is sent with a link to the PR. If any step fails, the
   incident is marked failed, a failure notification is sent, and the
   daemon moves on; one bad incident never kills the loop.

### Dry run

`maajun watch --dry-run` runs steps 1–3 but stops before touching git or
GitHub: the agent analyzes each new error and the report and would-be
cost are logged, but no branch, commit, or PR is created. Nothing is
persisted either — a later real run processes the same errors for real.

### Graceful shutdown

The daemon handles `SIGTERM` and `SIGINT`: it finishes the incident it
is currently processing, then exits cleanly instead of dying mid-push.
This is what makes `systemctl stop`/`restart` safe — an incident is
either fully published or untouched, never half-done.

## Security posture

- The GitHub token is passed to git via `GIT_ASKPASS`, so it never
  appears in remote URLs, `.git/config`, or the process list.
- Secrets live in the OS keyring; on headless servers, environment
  variables (`GITHUB_TOKEN`, `DEEPSEEK_API_KEY`) take precedence.
- The daemon's agent never touches your running application — it works
  in its own clone under the daemon workdir.
- `bash` is never available to the daemon's agent, in any mode.
