# How Maajun works

## The agent loop

The agent sends the conversation plus tool definitions to the model. When
the model requests tool calls, the agent executes them, feeds the results
back, and repeats — up to 50 rounds — until the model answers in plain
text. Responses stream token-by-token, including during tool rounds.

Every response carries the model that produced it and its token usage, so
the daemon can price each incident accurately (see
[Cost tracking](monitoring.md#cost-tracking)). Usage is summed across
*every* round, not just the one that produced the final text: each tool
round is a separately billed request that resends the whole conversation,
so counting only the last one would under-report a tool-heavy analysis
several times over — and the [spend cap](monitoring.md#capping-spend)
decides from these numbers.

Usage is also readable after the fact, with `take_usage()`: a turn that
dies on round thirty still spent what the first twenty-nine cost, and the
caller reads the total from a `finally` rather than from a response it
never got.

A finished turn keeps its tool calls and their results in history, so a
follow-up question does not re-read a file the previous answer just read.
Only the newest turn keeps them — older rounds collapse back to the
conversation, which is what the user is still talking about, and context
stays bounded.

Two ceilings keep a long tool loop inside the context window. Each tool
result is capped (and says how much was cut, so the model does not read a
truncated file as a complete one), and the request itself is trimmed
oldest-first once it grows too large. Trimming never drops the system
prompt, and never leaves a tool result at the front of the request — the
API rejects one with no matching tool call ahead of it. Without these, a
grep-heavy analysis walked past the context limit and failed the incident.

### Providers

Every supported provider speaks the `/chat/completions` protocol, so
`ChatCompletionsProvider` (`providers/chat_completions.py`) holds all the
behavior — retries, streaming, tool serialization, response parsing — and a
vendor module is only an endpoint, a pair of model names, and any content
quirks to strip: `deepseek.py` removes DeepSeek's DSML tool-call markup,
`openai.py` has nothing to strip. Point `ai.base_url` at a gateway to use any
other compatible endpoint.

`providers/pricing.py` costs each response by model, matching names as
prefixes so dated ids (`gpt-4o-2024-08-06`) resolve to their family. It is
load-bearing: the [spend cap](monitoring.md#capping-spend) decides whether to
analyze the next incident from these numbers, and an unpriced model logs a
warning rather than silently costing at the fallback rate.

Anything the table does not recognise — including a gateway that never names
the model it ran — is costed at the dearest entry in it, derived from the
table rather than written down so adding a pricier model moves it too. Every
error here rounds the same way on purpose: over-reporting pauses a daemon
early, under-reporting lets it run past a cap the user set.

### Provider resilience

API calls to the provider retry automatically on transient failures —
HTTP 429, 500, 502, 503, and connection errors — up to 3 attempts with
exponential backoff and jitter (capped at 30s between attempts). The backoff
sits *between* attempts only: sleeping after the last one just made the
caller wait up to 30s for an error it was always going to get. A streamed
response is closed on the way out even when the round raises or the caller
stops reading, so its connection goes back to the pool rather than waiting
on the collector.
Authentication errors and other client errors fail immediately; a bad key
is reported on the first call, not after a retry dance.

### Tool permissions

Tools are split into two classes:

| Class | Tools | Behavior |
|-------|-------|----------|
| Safe (read-only) | `read_file`, `glob`, `grep`, `list_dir`, `git_status` | Always allowed |
| Gated | `edit_file`, `write_file`, `run_maajun_command` | Need approval per call |

Approval is an injectable async callback. Each context supplies its own
policy:

- **`maajun watch`, suggest mode** — no callback, so every gated call is
  denied: the agent is strictly read-only.
- **`maajun watch`, fix mode** — file edits are approved only for paths
  inside the daemon's isolated workspace clone.
- **`maajun chat`** — read-only CLI commands are approved automatically;
  anything that writes prints the exact command line and waits for the
  user; `watch`, `reset`, and `sign-out` are refused outright.

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

## Layout

```
maajun/
  agent/        the tool-calling loop and its tools
  monitors/     error sources (log files, GitHub Actions) + shared defaults
  providers/    chat_completions.py (the protocol) + one file per vendor,
                plus pricing.py
  daemon/       core (loop), reports (rendering), store, prompts, wiring
  chat/         the REPL, its memory, and the tools that drive the CLI
  vcs/          git workspace, GitHub client, API conventions
  cli/          one command per module, all registering on a shared Typer app
  auth.py       credentials, read only from the OS keyring
  config.py     the config models and TOML round-trip
```

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

## Chat (`maajun chat`)

Chat is the same agent loop with a different tool set, a different system
prompt, and a permission policy that asks instead of deciding. It adds
three groups of tools:

| Group | Tools | Reads |
|-------|-------|-------|
| CLI | `list_maajun_commands`, `maajun_command_help`, `run_maajun_command` | the live Typer tree |
| Incidents | `search_incidents`, `get_incident`, `incident_stats` | the incidents table |
| Recall | `search_conversations`, `recall_session` | the chat tables |

**The command surface is generated, never listed.** `command_index()` walks
the Typer group and reads each subcommand's short help; the system prompt
embeds the result. A command added to the CLI is one chat can describe and
run immediately, with nothing to update — `settings.py` previously carried
a hand-written list of commands for its welcome panel and it had already
drifted.

**Commands run in-process on a worker thread**, through the same Click
tree, with stdout and stderr captured and stdin swapped for an empty
stream. The thread is not optional: `status`, `report` and `setup` each
call `asyncio.run()` internally, which refuses to nest inside the loop the
tool call is already running on — inline, the two most useful commands in
the index answered with a `RuntimeError`. It also keeps a long `report`
from blocking the rest of the session. A subprocess would
need `maajun` on `PATH`, which it is not under `uv run` or an unactivated
venv, and would not see the state the session already loaded. The empty
stdin matters: `prompt_line` falls back to `console.input()` once
`isatty()` is false, which raises `EOFError` rather than hanging a session
with nobody at the keyboard. Note that with `standalone_mode=False` Click
*returns* a `typer.Exit` code rather than raising it, so the return value
is what determines success.

Whether `config` reads or writes is decided by running the CLI's own parser
with `resilient_parsing=True` and checking whether `value` came back set —
`config github.mode -c f.toml` has two non-flag tokens and still only
reads.

### Memory

Chat sessions and messages live in `incidents.db` alongside the incidents,
so one question can span both — "what did we decide about that KeyError"
is a conversation lookup and an incident lookup. Both `IncidentStore` and
`ChatMemory` open the file through `store.connect()`, which runs the
migration ladder.

Recall tools are bound to the running session id and exclude it: the live
conversation is already in the agent's context, and returning it as a
search hit would just pay for it twice.

Messages are indexed for full-text search (FTS5, kept current by
triggers). Substring `LIKE` only ever matched a query that appeared
verbatim and contiguously, so "checkout KeyError" found nothing — which is
exactly how anyone refers back to a past error. Terms are quoted, ANDed and
prefix-matched, so punctuation in a traceback cannot be read as FTS
syntax. A SQLite built without FTS5 skips the migration and falls back to
`LIKE` rather than failing to open — but the skip is not permanent: bumping
`user_version` past it would have marked the file current with no index and
no way back, so `connect()` probes for the index on every open and builds it
the first time maajun runs somewhere FTS5 exists. The rebuild reads from
`chat_messages`, so conversations recorded while unindexed become searchable
too.

Chat spend is accumulated per session — including what a failed turn
spent — and bounded by `chat.max_usd_per_day`, checked before a question is
sent. The daemon's cap is separate: one bounds an unattended process, the
other an interactive one, and neither truncates work already in flight.

### The session loop

The REPL holds a single `asyncio.Runner` for its lifetime and every turn
runs on it. `asyncio.run()` per turn closed the loop underneath the
provider's HTTP client, and the pooled keep-alive connection it left behind
raised `Event loop is closed` on the first request of every later turn —
retried transparently, so the cost was a wasted request and a backoff sleep
on every question rather than a visible failure.

Answers stream: the spinner is a Live region that stops the moment anything
is printed, so an approval prompt is never drawn over by an animation, and
tool calls are announced before they run rather than only after.

The spinner also comes down for `run_maajun_command`. That tool runs the CLI
in-process on a worker thread and captures it by swapping `sys.stdout`, which
is process-wide; Rich resolves `sys.stdout` on every write, so a spinner left
running would paint its animation into the capture buffer and hand the escape
codes to the model as part of the command's output. The session passes a
`quiet` scope down to the tool for exactly that window, and a lock keeps two
captures from overlapping.

### Schema migrations

The database records its version in `PRAGMA user_version`, and
`store.connect()` applies any pending migrations at open, each in its own
transaction so an interrupted upgrade resumes rather than half-applies. A
file written by a *newer* maajun is refused instead of being rewritten.

The `BEGIN` is issued explicitly rather than leaning on `with conn`. Under
sqlite3's legacy transaction control — still the default — a transaction is
opened implicitly before DML and *not* before DDL, so every `CREATE`, `ALTER`
and `DROP` autocommitted on its own and `with conn` committed nothing that
mattered. The atomicity above was a claim, not a fact: killed between
migration 1's `RENAME` and its `INSERT ... SELECT`, the upgrade left an empty
incidents table beside an orphaned `incidents_outdated`.

This replaced a hard failure that told the user to delete the database —
acceptable while the schema was settled, not once chat needed to add to
it. Migration 1 rebuilds an outdated incidents table by copying rows across
whatever columns it has, which covers both a missing column and the
pre-multi-repo primary key.

### The sandbox

`ToolRegistry` refuses a call whose `path` falls outside its `Sandbox`
before the executor runs, so the boundary cannot be forgotten by the next
file tool somebody adds. The tool's *schema* decides whether it takes a
path, not the arguments — `grep` and `list_dir` default to the working
directory, which would otherwise be a way out whenever the sandbox is not
the cwd. Paths are resolved first, so a symlink is checked at its
destination, and `..` in a glob pattern is rejected because the pattern is
expanded after the root has been approved.

Two things are refused wherever they sit, allowed root or not: files whose
whole purpose is to hold a credential (`.env`, `id_rsa`, `*.pem`, `.netrc`,
`.git-credentials`, …), and maajun's own `incidents.db`, which holds every
incident and every conversation anyone has had with it. Reading either one
would put it in front of the AI provider, and from the daemon into a public
issue.

That is two gates, not one, because a tool can open a file it was never
handed. `ToolRegistry.off_limits` checks the path in the *arguments*; a tool
marked `walks_files` is also passed the `Sandbox` and must put every path it
discovers through `Sandbox.readable`. `grep` is the case that matters: it is
pointed at a directory it is allowed into and then reads the whole tree under
it, so with only the argument gate a refused `read_file .env` came straight
back as matched lines from `grep`. `readable` judges the resolved path, so a
symlink planted in the workspace cannot be used to read its target outside,
and grep says how many files it skipped — a silent omission would invite the
model to go looking for another way in.

The roots are the narrowest thing that still works: the daemon gets the one
workspace it is analyzing; chat gets the working directory, `daemon.workdir`,
and the configured log files named one by one — `/var/log` is not the
project.

## Security posture

- The GitHub token is passed to git via `GIT_ASKPASS`, so it never
  appears in remote URLs, `.git/config`, or the process list.
- Secrets live in the OS keyring and are read from nowhere else — no
  environment variables, and no shelling out to `gh` — so a credential
  cannot be injected into a running daemon's process environment, and the
  token it pushes with cannot change without maajun being told.
- The daemon's agent never touches your running application — it works
  in its own clone under the daemon workdir.
- `bash` is never available to the daemon's agent, in any mode.
- `run_maajun_command` reaches maajun's own subcommands and nothing else.
  It is not a shell: the command name is checked against the Typer tree,
  and the arguments are split with `shlex` rather than handed to one.
