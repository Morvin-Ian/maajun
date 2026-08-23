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
`openai.py` has nothing to strip, `ox_alpha.py` is OpenRouter's endpoint and the one
free model. Point `ai.base_url` at a gateway to use any other compatible
endpoint.

`anthropic.py` is the exception: Claude speaks the Messages API, so it
subclasses `AIProvider` directly and translates both ways. The system prompt
is hoisted out of `messages` into its own parameter, assistant tool calls
become `tool_use` blocks, and the tool results the agent emits one apiece are
merged into the single user message Anthropic requires. Everything above the
provider layer keeps seeing OpenAI-shaped tool calls. It also sets a
`cache_control` breakpoint on every request — Anthropic caches only where it
is told to, so without one each tool round re-reads the whole prompt at full
price.

`ProviderType` declares the providers in the order setup offers them,
cheapest first, and `ProviderFactory.providers` repeats that order; setup
defaults to the first, so the ordering is the recommendation. A provider
class marked `free = True` is offered first and labelled as such.

`providers/pricing.py` costs each response by model, matching names as
prefixes so dated ids (`gpt-4o-2024-08-06`) resolve to their family. It is
load-bearing: the [spend cap](monitoring.md#capping-spend) decides whether to
analyze the next incident from these numbers, and an unpriced model logs a
warning rather than silently costing at the fallback rate.

Each model carries four rates: fresh input, cache-hit input, cache-write
input, and output. Every provider re-serves a prompt prefix it has already
seen far more cheaply than a fresh one, and every round of the tool loop
resends a growing prefix, so on a long investigation the cache-hit rate is
what most input tokens are actually billed at. The counts come from the
provider — `prompt_cache_hit_tokens` on DeepSeek,
`prompt_tokens_details.cached_tokens` on OpenAI,
`cache_read_input_tokens` / `cache_creation_input_tokens` on Anthropic —
flattened into the usage dict as `cached_tokens` and `cache_write_tokens` by
each provider's `usage_of`. A provider that reports nothing is charged in
full. Anthropic is the only one that bills for the write (1.25x fresh), which
is why that rate is tracked rather than assumed equal to the fresh one.

DeepSeek also prices by the clock: its published rates are peak, and
off-peak is half of them. `is_peak` reads the two UTC windows and the
Beijing-time weekend exemption, and `pricing_for` takes the moment as an
argument so the rule is testable rather than wired to the wall clock.

Anything the table does not recognise — including a gateway that never names
the model it ran — is costed at the dearest entry in it, derived from the
table rather than written down so adding a pricier model moves it too, and
with neither discount applied: both are claims about a model that could not
be identified. Every error here rounds the same way on purpose:
over-reporting pauses a daemon early, under-reporting lets it run past a cap
the user set.

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

Runtime errors reach maajun through one of three sinks, which is what
makes it deploy-method agnostic: however an app is started, its errors end
up in a file, in the journal, or on a container's stdout. All three are the
same *text*, so they share one reading of it — `LogStreamMonitor`
(`monitors/stream.py`) owns the whole parse: Python, Java, and Go stack
traces (including ones split across polls), lines matching the configured
error pattern, structured JSON logs matched on a level field, and the
carry-over that holds back text which may still be streaming. A subclass
supplies only `read_stream()`.

- **Log files** (`monitors/logfile.py`) — a byte offset into a file,
  surviving rotation (inode change) and truncation (size below offset).
- **journald** (`monitors/journald.py`) — `journalctl -u <unit> -o cat`,
  positioned by journalctl's own cursor file under `daemon.workdir`, so a
  restart resumes exactly where it stopped. A time window from startup
  stands in until the cursor exists, rather than replaying the journal.
- **docker** (`monitors/docker.py`) — `docker logs --since <last poll>`,
  reading the container's stderr as well as its stdout, since that is
  where an unhandled exception goes.
- **GitHub Actions** (`monitors/github_actions.py`) — polls for failed
  workflow runs, turning CI breakage into incidents. Failures are
  fingerprinted by commit SHA, so several red workflows on the same
  commit collapse into a single incident.

Neither journald nor docker asks for timestamps, on purpose: both prefix
every line, which would leave the indented lines of a traceback no longer
indented and so impossible to group into one incident.

The two shelling-out monitors share `CommandStreamMonitor`
(`monitors/shell.py`), which runs the command in a thread — `poll_once`
gathers every monitor at once, so a blocking read would stall all of them —
and reports an unreadable source once rather than every poll. The GitHub
Actions monitor is built on `HTTPPollMonitor`, which owns the HTTP client,
remembers which item ids it has already emitted, and swallows (but logs)
fetch failures. Either way a monitor that can't reach its source returns no
events rather than crashing the daemon.

Every monitor also inherits **burst thresholding** from the base class:
with `burst_threshold > 1`, events are buffered until N of them land
inside `burst_window_seconds` and then emitted together, so a one-off
blip never becomes a pull request.

## Layout

```
maajun/
  agent/        the tool-calling loop and its tools
  monitors/     error sources (files, journald, docker, Actions) + defaults
  providers/    chat_completions.py (the protocol) + one file per vendor,
                plus pricing.py
  daemon/       core (loop), reports (rendering), store, prompts, wiring
  chat/         the REPL, its memory, and the tools that drive the CLI
  vcs/          git workspace, GitHub client, API conventions
  cli/          one command per module, all registering on a shared Typer app
  auth.py       credentials: the OS keyring, then a gh login
  inspection.py reads a codebase to find how its errors surface
  config.py     the config models and TOML round-trip
  discovery.py  probes the host for how a repo is deployed
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
3. **Triage** — `triage.by_design` matches the raw error against signatures
   for guards that name their own intent (`ValidationError`,
   `PermissionDenied`, `429 Too Many Requests`, …) plus anything in
   `monitor.ignore_patterns`. A match closes the incident as `ignored` with
   its reason and never reaches the agent, so it costs nothing. The pass is
   narrow on purpose: anything needing to know the application belongs to
   the verdict in step 6, not here. `monitor.ignore_by_design = false` turns
   it off. Nothing is deleted — the row stays, which is also what stops the
   same error being re-examined on every poll.
4. **Analyze** — for a new fingerprint, the daemon syncs an isolated
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
5. **Insist on the edit** — fix mode that produced a report and no diff is
   asked once more. A pull request with nothing in it publishes nothing
   anyone can review, and the usual cause is the escape hatch in
   `FIX_PROMPT_SUFFIX` — "the right fix is outside this repository" — being
   taken for a finding that does have an in-repo fix. An environment
   variable no settings module defaults and no example env file documents is
   a change to the repository, not an exemption from one. The second answer
   replaces the report only if it is usable, so a model that edits the files
   and replies "done" does not cost the analysis. Suggest mode, dry runs and
   local mode are never asked: none of them has a branch to diff. A run that
   still changes nothing files an **issue** instead of a pull request — the
   report file used to be committed so there was always a diff to review, but
   that shipped pull requests that look like fixes until the Files tab says
   otherwise. `issue_body(unfixed=True)` marks it so it is not read as
   suggest mode.
6. **Check the report** — a blank answer, or one with none of the report's
   sections, is asked for once more and then abandoned: no issue, no PR,
   the incident marked failed. An empty artifact costs the reader more than
   it gives and hides that the run went wrong. A report with no one-line
   summary earns the same re-ask but never the abandonment: that is
   `headline_problem`, soft where `report_problem` is a gate, because a good
   analysis is worth more than a missing heading.
7. **Read the verdict** — every report opens with `defect` or `by design`.
   `reports.verdict` parses it, and `by design` stops the run: nothing is
   published, the incident is closed as `ignored` with the agent's own
   reason, and what the analysis cost is banked with `add_spend` because the
   round was billed either way. This is the pass that recognises a guard
   particular to the codebase, since the agent has read the code that
   raised. An absent or unparseable verdict is treated as a defect — silence
   must never suppress a report.
8. **Title it from the finding** — `reports.artifact_title` reads the
   report's own first heading and titles the issue, the pull request, and
   the commit with it, falling back to the raw error only when there is no
   usable heading. The alternative, titling from the log line, names the
   symptom: an exception surfaces at one file and line while the defect that
   has to change is at another, so the title would point a reader at the
   wrong place. A heading that is the unfilled template, or one that is just
   a section name because the model skipped the summary, is treated as
   absent. The commit subject is built from the same headline, so `git log`
   and the pull request cannot disagree.
9. **Publish** — depends on the repo's [mode](monitoring.md#modes). In
   `suggest` mode the report is filed as a GitHub **issue**: no branch, no
   commit, no push, because there is no diff to review. In `fix` mode the
   repo's `test_command` (if set) is run in the workspace first and its
   verdict is put at the top of the PR body — it comes from config, not from
   the model, since the agent has no shell access. The report is then
   committed as `docs/incidents/<fingerprint>.md` alongside the
   agent's edits, the branch is pushed, and a **pull request** is opened
   with the report as its body — reusing an existing PR for the branch
   rather than duplicating it. Fix mode always ends in a PR: when the agent
   changed no code, the committed report is the diff and the body says so,
   so the finding still lands in one reviewable place. With no
   `github.repo` configured the daemon
   runs in [local mode](monitoring.md#1-configure) instead: steps 1–3 are
   unchanged, but the report is written to `<workdir>/reports/` and no git
   or GitHub operation runs at all.
10. **Record** — the incident is marked processed with its PR URL, token
   counts, and USD cost. If any step fails, the incident is marked failed
   and the daemon moves on; one bad incident never kills the loop.

### On-demand reports (`maajun report`)

The same pipeline runs without a monitor. `maajun report "<description>"`
builds a synthetic `ErrorEvent` (source `manual`, fingerprinted from the
description), records it as an incident like any other, and feeds it
through the analyze/publish steps against a chosen repo, opening
a PR on a `maajun/report-<fingerprint>` branch. Detected incidents and
manual reports share the analyze/publish code path, so cost tracking
behaves identically. The one difference is dedup: the watch
loop skips a fingerprint it has already processed (step 2), whereas
`report` always re-investigates and updates its branch — a manual report is
an explicit request, not a passive observation.

### Progress feedback

Foreground commands that do slow work surface it instead of hanging
silently. `report` and `chat` drive a Rich `Live` spinner (`progress.py`)
whose phase label the daemon advances through a `progress` callback
(*preparing workspace → analyzing with AI → opening PR*). `watch` has no
spinner: it runs detached and its output is read out of a log file, where
an animation is noise — it prints one line per event through the daemon's
`on_notice` callback instead. Both callbacks are injected, so the daemon
core stays free of any terminal concerns.

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

## Running detached

`maajun watch` starts itself again with `--foreground` in a new session
(`daemon/service.py`), so the loop outlives the shell: stdout and stderr go
to `<workdir>/watch.log`, the pid to `<workdir>/watch.pid`. Credentials and
monitors are built *before* detaching, so a broken config fails in front of
the user rather than in a log file nobody is watching yet. `--stop` sends
`SIGTERM`, which the loop already handles by finishing the incident it is
on. A stale pid file — power loss mid-run — is cleaned up on the next
start rather than blocking it. Under a service manager, `--foreground` is
the mode to use: systemd supervises the process itself.

## Security posture

- The GitHub token is passed to git via `GIT_ASKPASS`, so it never
  appears in remote URLs, `.git/config`, or the process list.
- Provider keys live in the OS keyring and are read from nowhere else; no
  environment variable can inject one into a running daemon.
- The GitHub token comes from the keyring, or failing that from `gh auth
  token` — asked for once per process, and reported by `status` so the
  source is never a mystery. With `github.transport = "ssh"` the push uses
  the machine's own keys and the token stays with the API.
- The daemon's agent never touches your running application — it works
  in its own clone under the daemon workdir.
- `bash` is never available to the daemon's agent, in any mode.
- `run_maajun_command` reaches maajun's own subcommands and nothing else.
  It is not a shell: the command name is checked against the Typer tree,
  and the arguments are split with `shlex` rather than handed to one.
