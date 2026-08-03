# How Maajun works

## The agent loop

The agent sends the conversation plus tool definitions to the model. When
the model requests tool calls, the agent executes them, feeds the results
back, and repeats — up to 50 rounds — until the model answers in plain
text. Responses stream token-by-token, including during tool rounds.

Every response carries the model that produced it and its token usage, so
the daemon can price each incident accurately (see
[Cost tracking](monitoring.md#cost-tracking)).

### Providers

Every supported provider speaks the `/chat/completions` protocol, so
`ChatCompletionsProvider` holds all the behavior — retries, streaming, tool
serialization, response parsing — and a vendor module is only an endpoint, a
pair of model names, and any content quirks to strip: `deepseek.py` removes
DeepSeek's DSML tool-call markup, `openai.py` has nothing to strip. Point
`ai.base_url` at a gateway to use any other compatible endpoint.

Pricing costs each response by model, matching names as prefixes so dated ids
(`gpt-4o-2024-08-06`) resolve to their family. It is load-bearing: the
[spend cap](monitoring.md#capping-spend) decides whether to analyze the next
incident from these numbers, and an unpriced model logs a warning rather than
silently costing at the fallback rate.

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
| Gated | `edit_file`, `write_file` | Need approval per call |

Approval is an injectable async callback. Each context supplies its own
policy:

- **`maajun watch`, suggest mode** — no callback, so every gated call is
  denied: the agent is strictly read-only.
- **`maajun watch`, fix mode** — file edits are approved only for paths
  inside the daemon's isolated workspace clone.

A denied call is not an error: the model receives a message telling it
the user refused and not to retry, so it adapts (e.g. writes the fix as
a suggestion instead).

## Monitors

A monitor is anything that can answer "what new errors happened since I
last asked?" — the daemon just polls whichever ones the config enables
and treats their output identically. They all produce the same normalized
`ErrorEvent` (source, message, details, fingerprint), so the rest of the
pipeline doesn't care where an error came from.

- **Log files** (`monitors/logfile.py`) — tails files incrementally,
  surviving rotation and truncation. Recognizes Python, Java, and Go
  stack traces (including ones split across polls), lines matching the
  configured error pattern, and structured JSON logs matched on a level
  field. Requires maajun to run on the machine that writes the logs —
  this is how failing requests on a VPS are detected, via the traceback
  the app logs when a request errors.
- **GitHub Actions** (`monitors/github_actions.py`) — polls for failed
  workflow runs, turning CI breakage into incidents. Failures are
  fingerprinted by commit SHA, so several red workflows on the same
  commit collapse into a single incident.

The GitHub Actions monitor is built on `HTTPPollMonitor`, which owns the
HTTP client, remembers which item ids it has already emitted, and
swallows (but logs) fetch failures — a monitor that can't reach its API
returns no events rather than crashing the daemon. Adding a new
HTTP-polled source means implementing three methods: fetch the items,
identify one, convert one to an `ErrorEvent`.

Every monitor also inherits **burst thresholding** from the base class:
with `burst_threshold > 1`, events are buffered until N of them land
inside `burst_window_seconds` and then emitted together, so a one-off
blip never becomes a pull request.

## The incident pipeline (`maajun watch`)

1. **Detect** — monitors poll their error sources every
   `monitor.poll_interval` seconds. A failing monitor is logged and
   skipped; the others still run.
2. **Dedup** — every event gets a fingerprint. For log errors it's a
   hash of the error text with digits and hex addresses stripped, so the
   same crash at a different line number or timestamp is still the same
   incident. CI failures use the commit SHA. Fingerprints live in a
   SQLite store; known ones just bump a counter. An incident whose last
   attempt *failed* is retried on a later poll, up to three attempts —
   otherwise one transient GitHub 502 would blacklist that error forever.
   Before each incident the daemon also checks the
   [daily spend cap](monitoring.md#capping-spend) and the per-cycle limit;
   anything deferred is forgotten so a later poll picks it up.
3. **Analyze** — for a new fingerprint, the daemon syncs an isolated
   clone of the event's repo, creates a branch
   `maajun/incident-<fingerprint>`, and asks the agent to investigate.
   The agent reads the code with its safe tools and writes a structured
   report (what happened / root cause / likely cause commit / suggested
   fix) — the last few commits on the base branch are handed to it so the
   report can name the deploy that probably introduced the error. In fix mode it
   may also edit files in the clone. The clone is synced in both modes —
   the agent reads the code from it — but only fix mode branches. In a
   [multi-repo](monitoring.md#multiple-repositories) config each monitor
   is bound to a repo, so its errors are analyzed against — and open PRs
   on — the right one, each with its own clone, branch, and mode.
4. **Publish** — depends on the repo's [mode](monitoring.md#modes). In
   `suggest` mode the report is filed as a GitHub **issue**: no branch, no
   commit, no push, because there is no diff to review. In `fix` mode the
   repo's `test_command` (if set) is run in the workspace first and its
   verdict is put at the top of the PR body — it comes from config, not from
   the model, since the agent has no shell access. The report is then
   committed as `docs/incidents/<fingerprint>.md` alongside the
   agent's edits, the branch is pushed, and a **pull request** is opened
   with the report as its body — reusing an existing PR for the branch
   rather than duplicating it. With no `github.repo` configured the daemon
   runs in [local mode](monitoring.md#1-configure) instead: steps 1–3 are
   unchanged, but the report is written to `<workdir>/reports/` and no git
   or GitHub operation runs at all.
5. **Record** — the incident is marked processed with its PR URL, token
   counts, and USD cost. If any step fails, the incident is marked failed
   and the daemon moves on; one bad incident never kills the loop.

### On-demand reports (`maajun report`)

The same pipeline runs without a monitor. `maajun report "<description>"`
builds a synthetic `ErrorEvent` (source `manual`, fingerprinted from the
description) and feeds it through steps 3–5 against a chosen repo, opening
a PR on a `maajun/report-<fingerprint>` branch. Detected incidents and
manual reports share the analyze/publish code path, so cost tracking
behaves identically. The one difference is dedup: the watch
loop skips a fingerprint it has already processed (step 2), whereas
`report` always re-investigates and updates its branch — a manual report is
an explicit request, not a passive observation.

### Progress feedback

Foreground commands that do slow work surface it instead of hanging
silently. `report` and interactive `watch` drive a Rich `Live` spinner
(`progress.py`) whose phase label the daemon advances through a `progress`
callback (*preparing workspace → analyzing with AI → opening PR*), and the
daemon emits PR-opened / failure lines through an `on_notice` callback the
CLI styles and prints. Both callbacks are injected, so the daemon core
stays free of any terminal concerns; `--verbose` and non-interactive runs
fall back to plain logging.

### Dry run

`maajun watch --dry-run` runs steps 1–3 but stops before touching git or
GitHub: the agent analyzes each new error and the report and would-be
cost are logged, but no branch, commit, or PR is created. Nothing is
persisted either — a later real run processes the same errors for real.
`maajun report --dry-run` does the same for a manual report.

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
