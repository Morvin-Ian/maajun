# Monitoring guide

Maajun watches your error sources — local log files and GitHub Actions —
and turns each new error into a pull request on GitHub. For log files it
must run on the server that writes them (the usual VPS setup); the
GitHub Actions monitor works from anywhere with network access.

## 1. Configure

```bash
maajun setup   # interactive: API key, repo, mode, logs, GitHub Actions
```

`setup` prompts for the essentials and writes the config for you;
everything but the API key can be skipped with Enter. Afterwards you can
view or change any setting without opening the file:

```bash
maajun config                       # print the whole config
maajun config github.mode fix       # change one value (validated before save)
```

Full config reference:

```toml
[ai]
provider = "deepseek"
# model = "deepseek-v4-flash"  # provider default if omitted
# base_url = "https://gateway.internal/v1"  # OpenAI-compatible gateway
# thinking_mode = true         # use the reasoning model (deepseek-v4-pro)
# temperature = 0.3
# max_tokens = 4096

# One entry per repository — add them with `maajun add-repo owner/name`.
# Omit the section entirely for local mode.
[[github.repos]]
repo = "owner/name"           # repository maajun reports to
base_branch = "main"          # branch PRs target
mode = "suggest"              # "suggest" or "fix" — see Modes below
# log_files = ["/var/log/api/error.log"]   # watched for this repo only
# test_command = "pytest -q"  # verifies a fix-mode edit; result goes in the PR

[monitor]
log_files = [                 # files to tail for errors
  "/var/log/myapp/error.log",
]
error_pattern = "\\b(ERROR|CRITICAL|FATAL)\\b"   # regex for error lines
poll_interval = 30            # seconds between polls

# Detection tuning (optional) — see Tuning detection below
# json_level_field = "level"        # also match structured JSON logs
# json_level_values = "error,critical,fatal"
# traceback_headers = ["Traceback (most recent call last):", "panic:"]
# burst_threshold = 1               # only report after N errors in the window
# burst_window_seconds = 60

# GitHub Actions — poll failed workflow runs (optional).
# Uses the same GitHub token as everything else; no secret goes in this file.
# github_actions_repos = ["owner/name"]

[daemon]
workdir = "~/.local/share/maajun"   # clones, incident DB, state
# repo_path = "/srv/myapp"          # local checkout to analyze in local mode
# max_usd_per_day = 5.0             # stop analyzing past this daily spend (0 = no cap)
# max_incidents_per_cycle = 10      # bound one poll's burst (0 = unlimited)
```

At least one error source must be configured — log files or GitHub
Actions; the daemon refuses to start with nothing to watch. Use
`--config /path/to/config.toml` on any command to point somewhere else.

`ai.base_url` points maajun at any endpoint that speaks the OpenAI
`/chat/completions` protocol — a corporate proxy, a router, a self-hosted
server — while `ai.provider` still selects the request dialect and model
defaults. Setup validates your key against the gateway rather than the
vendor when it is set.

`github.repo` is optional. Left empty, maajun runs in **local mode**: it
still detects and analyzes errors, but writes each incident report to
`<workdir>/reports/<fingerprint>.md` instead of opening a pull request,
and forces `suggest` mode so the agent can never edit your working tree.
Set `daemon.repo_path` to choose which checkout it analyzes — the default
is the current directory.

### Multiple repositories

One daemon can watch several repositories, each with its own branch,
mode, and log files. Add them with `add-repo`:

```bash
maajun add-repo team/api -m fix -l /var/log/api/error.log
maajun add-repo team/web -l /var/log/web/error.log
```

Each entry maps its own log files to its own repo:

```toml
[[github.repos]]
repo = "team/api"
base_branch = "main"
mode = "fix"
log_files = ["/var/log/api/error.log"]

[[github.repos]]
repo = "team/web"
base_branch = "main"
mode = "suggest"
log_files = ["/var/log/web/error.log"]
```

Each monitor's errors open PRs on the repo it's attached to. Any global
`monitor.log_files` attach to the **first** configured repo, as does a
GitHub Actions monitor whose repo isn't in the list — that fallback is
logged as a warning, since a typo in the slug would otherwise misfile
every CI failure without a trace.

`[[github.repos]]` is the only supported form, including for a single
repository. A config using the older scalars (`repo`, `base_branch`,
`mode` directly under `[github]`) is rejected at startup with the
`add-repo` command that fixes it — rather than loading as "no repo
configured" and quietly writing reports to disk.

Two repos may list the **same** log file, and each gets its own issue or
PR for what it finds there. Incidents are recorded per repo, so a
traceback that both services share is not swallowed as a duplicate of the
first one seen — see [deduplication](#deduplication).

To change one repo's settings later, pass `--repo` to `config`; without
it, a `github.*` key applies to every configured repo:

```bash
maajun config github.mode fix                          # all repos
maajun config github.test_command "pytest -q" -r team/api   # one repo
```

### Which repo an error belongs to

Errors are attributed to a repo when they are recorded, not guessed from
the log path later. The repo appears in:

- `maajun incidents` — a **Repo** column once more than one repo has
  incidents, and `--repo owner/name` to filter
- the `repo` column of `incidents.db`
- the issue/PR body and the report file, as a `Repo:` line beside
  `Source:` — the source names the log file or workflow that *saw* the
  error, which with a shared log file is not the same thing
- `watch`'s notices (`New error in team/api: …`) and the daemon log

## Error sources

### Log files

Point `monitor.log_files` at the files your app writes. The monitor
tails them incrementally (surviving rotation and truncation), recognizes
stack traces — including ones split across polls — and lines matching
`error_pattern`. An ERROR line followed within a line or two by a
traceback (`logging.exception`) is merged into a single event.

Stack traces are recognized for Python (`Traceback (most recent call
last):`), Java (`Exception in thread`, `Caused by:`), and Go (`panic:`,
`goroutine`). Override the list with `traceback_headers` for another
language or a custom format.

This is how maajun catches **request errors on a VPS**: run maajun on
the same server as your app, and point it at the log your app's
exceptions land in. A request that 500s is detected as soon as the
framework logs it — for Django/Flask/FastAPI that's the app's error log
(any `logging.exception` traceback), and watching the web server's error
log (e.g. `/var/log/nginx/error.log`, gunicorn's `--error-logfile`)
works too since `error_pattern` is a plain regex you can adapt to any
log format. Errors that are swallowed without being logged are invisible
— make sure unhandled exceptions actually reach a file.

### Tuning detection

`error_pattern` matches `ERROR|CRITICAL|FATAL` by default. Warnings are
deliberately excluded: every match costs an AI call and a pull request,
so opt in explicitly if you want them.

```toml
[monitor]
error_pattern = "\\b(ERROR|CRITICAL|FATAL|WARNING)\\b"
```

**Structured logs.** Set `json_level_field` to match one-JSON-object-per-line
logs on their level field, instead of hoping the regex hits:

```toml
json_level_field = "level"                  # or "severity", "levelname", …
json_level_values = "error,critical,fatal"  # levels that count as errors
```

The regex still applies to lines that aren't valid JSON, so a mixed log
works.

**Noisy, self-recovering errors.** `burst_threshold` holds events back
until N of them land inside `burst_window_seconds`, so a single blip is
ignored and only a genuinely repeating error is reported. The whole burst
is reported once the threshold is reached:

```toml
burst_threshold = 5
burst_window_seconds = 300   # 5 errors within 5 minutes
```

`--once` always flushes an incomplete burst rather than discarding it.

### GitHub Actions

Set `github_actions_repos` to poll each repo for failed workflow runs. It
uses the same GitHub token as everything else — read from the keyring,
never written into the config file — and that token needs
read access to the repos' actions. A failure becomes an incident
fingerprinted by the workflow *and* the commit, with the run details and a
link to the failed run. Two workflows failing on one commit are therefore
two incidents — "the linter failed" and "the tests failed" are different
problems with different fixes, and keying on the commit alone meant only
the first one polled was ever reported. Re-running the same workflow on the
same commit is still one incident.

`maajun setup --github-actions` wires this up using the GitHub token you
already stored, rather than asking for a second one.

## 2. Give it GitHub access

Create a **fine-grained personal access token** at
<https://github.com/settings/personal-access-tokens>:

- **Repository access**: only the repo in `github.repo`
- **Permissions**:
  - Contents: **Read and write** (push branches)
  - Pull requests: **Read and write** (open PRs)

Store it:

```bash
maajun setup               # asks for the repo, then the token
```

`setup` suggests the repository from your `origin` remote, reuses a token
already in the keyring, and otherwise prompts for one (hidden input). It
then checks that the
token authenticates *and* that it can push to that repo — so
misconfigured tokens fail here rather than at 3 a.m.

GitHub is optional. Skip it and maajun still detects and analyzes
errors, writing each incident report under `daemon.workdir/reports`
instead of opening a pull request. Set `daemon.repo_path` to choose which
local checkout it analyzes (the default is the current directory).

maajun reads credentials only from the OS keyring, so a headless server
needs a keyring backend installed before `setup` can store anything:

```bash
pip install keyrings.alt        # or install gnome-keyring
maajun setup                    # then store the key as usual
```

## 3. Run

Check everything is wired up first:

```bash
maajun status                # provider key? token? repo push access? logs?
```

`status` verifies your API key and GitHub token are present, that the
token can push to each configured repo, and that a monitor is set — so a
misconfiguration surfaces here rather than at 3 a.m. It exits non-zero on
failure (handy in a deploy script); add `--no-network` to skip the
GitHub round-trips.

```bash
maajun watch --dry-run       # analyze errors, but skip git/PR — test your config
maajun watch --once          # single poll cycle, then exit (cron)
maajun watch                 # continuous monitoring
maajun watch -m fix          # override the configured mode for this run
maajun watch -v              # debug logging
```

### Dry run

`--dry-run` exercises everything except publishing: monitors poll, new
errors are deduplicated and analyzed by the AI, and the report and cost
are logged — but no branch, commit, or PR is created, and nothing is
recorded in the incident database. Drop the flag and the same errors
are processed for real. It's the safe way to verify a new config or
error source before letting maajun loose on your repo.

## On-demand reports

You don't have to wait for a monitor to catch something. `maajun report`
investigates an issue **you describe** and reports the analysis —
the same clone → investigate → report → PR pipeline as `watch`, run on
demand against the same configured repo, mode, and credentials.

```bash
maajun report "Checkout 500s when the cart is empty"
maajun report "KeyError: 'discount' in cart totals" -m fix
maajun report "Investigate the slow /search endpoint" --dry-run
```

Pass a plain description, a bug-tracker summary, or a pasted stack trace.
The agent reads the target repo with its safe tools and writes the usual
*what happened / root cause / suggested fix* report; in `fix` mode it also
edits the clone. A live spinner shows each phase (preparing the workspace,
analyzing, filing the issue) and finishes with the link. With multiple
repos configured, `report` prompts you to pick one when `--repo` is
omitted; pass `--repo owner/name` to skip the prompt in scripts. `--mode`,
`--base-branch`, and `--dry-run` work the same as on `watch`.

Each report is recorded in the same incident database as detected errors,
so its tokens and cost are tracked alongside them. Unlike the watch loop,
`report` is not dedup-gated — running it again on the same description
re-investigates and files a fresh issue (or, in `fix` mode, updates the
existing `maajun/report-<fingerprint>` branch and PR).

## Modes

| | `suggest` (default) | `fix` |
|---|---|---|
| Artifact | A GitHub **issue** | A **pull request** on a branch |
| Agent file access | Read-only | May edit files, but only inside its own clone |
| Agent shell access | None | None |
| Contains | Incident report + suggested fix | Applied fix + incident report |
| Verified | n/a — no diff | By `test_command`, if set |
| You review | The suggestion | The actual diff |

Suggest mode files an **issue**, not a pull request. A PR whose diff
changes nothing still lands in the review queue and triggers CI, which is
noise — an analysis is an issue. It's also cheaper: suggest mode clones the
repo to read it, but creates no branch and pushes nothing.

Either way, **nothing merges without your review**. Start with `suggest`;
switch to `fix` once you trust the reports.

### Verifying a fix

A fix-mode diff nobody ran is a diff reviewed on trust. Set `test_command`
and maajun runs it in the workspace after the agent's edits, then puts the
verdict at the top of the PR body:

```bash
maajun config github.test_command "pytest -q"
```

```
✅ Tests pass — `pytest -q`      ❌ Tests fail (exit 1) — `pytest -q`
```

Either way the PR still opens — "this fix breaks the suite" is precisely
what a reviewer needs to know, and suppressing the PR would bury the
analysis with it. Output is collapsed in a `<details>` block, truncated at
3 000 characters, with a 10-minute timeout. With no `test_command` the PR is
labelled **Unverified**.

The command comes from your config, never from the model: the agent has no
shell access in either mode, so verification cannot be redirected to run
something else.

## What you get

**`suggest` mode — an issue:**

- Title: `[maajun] KeyError: 'discount'`
- Body: the incident report — what happened, root cause with file/line
  references, suggested fix — then the raw error details, source, and
  fingerprint

**`fix` mode — a pull request:**

- Branch: `maajun/incident-<fingerprint>` (detected errors) or
  `maajun/report-<fingerprint>` (on-demand `maajun report`)
- Title: `[maajun] KeyError: 'discount'`
- Diff: the applied fix, plus `docs/incidents/<fingerprint>.md` so the
  incident stays documented in-repo after the PR is closed
- Body: the incident report, including an *Applied fix* section

## Deduplication

Every error gets a stable fingerprint before any AI call:

- **Log errors** — a hash of the error text with volatile parts (line
  numbers, addresses, timestamps, ids) stripped, so the same crash
  repeating looks identical.
- **CI failures** — the commit SHA.

Known fingerprints only increment a counter in the incident database —
one error, one PR, ever.

The fingerprint is scoped to the repo the error was attributed to, so the
key is `(fingerprint, repo)`. Two services that share a library and hit
the identical traceback each get their own issue; without the repo in the
key, whichever repo was polled first claimed the error and the rest were
dropped as already known. Local-mode incidents record an empty repo.

The incident history lives in `<workdir>/incidents.db` (SQLite); delete a
row (or the file) to make maajun treat an error as new again. It also holds
your `maajun chat` sessions, which is what lets chat answer questions about
past incidents and past conversations together.

A database written by an older version of maajun is **migrated at open**,
keeping the history — the schema version is tracked in `PRAGMA
user_version`, and each migration commits separately so an interrupted
upgrade resumes rather than half-applies. One written by a *newer* maajun
is refused rather than rewritten; upgrade, or point `daemon.workdir`
somewhere else.

Incidents handled from this version on also store their analysis text, so
`maajun chat` can recall a root cause without going back to GitHub. Rows
that predate the column keep their issue or PR link and show no report.

## Cost tracking

Each processed incident records the prompt/completion token counts and
the USD cost of its analysis in `incidents.db` (`prompt_tokens`,
`completion_tokens`, `cost_usd` columns), priced by the model that
actually ran. The cost is also logged when the PR opens, and `--dry-run`
logs what an analysis would have cost. To audit spend:

```bash
sqlite3 ~/.local/share/maajun/incidents.db \
  "SELECT COUNT(*), SUM(cost_usd) FROM incidents WHERE status='processed'"
```

### Capping spend

Tracking is not a limit. A daemon left running against a log that starts
emitting a novel error every minute would keep paying for analyses, so
**`max_usd_per_day` defaults to `5.0`** — maajun stops analyzing after $5 of
spend in a UTC day rather than running up an open-ended bill while you sleep.
Change or remove the ceiling:

```bash
maajun config daemon.max_usd_per_day 20   # raise it
maajun config daemon.max_usd_per_day 0    # 0 = no cap
```

Before each incident the daemon sums today's `cost_usd` (UTC day) and, if
the cap is reached, stops analyzing, warns once, and keeps polling. Errors
skipped this way are left at `status=new` — recorded, but not published —
and picked up on the next poll after the cap resets or is raised, so nothing
is silently lost. They keep counting sightings while they wait, so the
eventual report says how long the error had really been happening.
`--dry-run` ignores the cap, since that is an explicit interactive request.

`max_incidents_per_cycle` (default 10) bounds a single poll the way the cap
bounds the day: fifty novel errors arriving at once would otherwise be fifty
back-to-back AI calls. The remainder is picked up on the next poll.

### Reviewing what it did

```bash
maajun incidents              # status, cost, and links, newest first
maajun incidents --failed     # only those that failed 3 times and stopped
```

The summary shows today's spend against your cap and the all-time total, so
neither cost tracking nor the cap needs a sqlite3 query to inspect.

## Running under systemd

`/etc/systemd/system/maajun.service`:

```ini
[Unit]
Description=Maajun error monitor
After=network-online.target

[Service]
Type=simple
User=deploy
ExecStart=/home/deploy/.local/bin/maajun watch
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now maajun
journalctl -u maajun -f
```

The daemon shuts down gracefully on `SIGTERM`/`SIGINT`: it finishes the
incident it is currently processing before exiting, so
`systemctl stop`/`restart` never leaves a half-pushed branch.

Note: after a restart the daemon re-reads watched logs from the start
and re-fetches current CI failures. Deduplication makes this harmless —
already-processed errors are recognized and skipped without any AI
calls.

## Troubleshooting

Run `maajun status` first — it checks credentials, repo push access, and
log files in one shot and points at whatever is missing.

- **"No GitHub token"** — run `maajun setup` and provide one.
- **"Token cannot push"** — the fine-grained PAT is missing Contents
  write access or doesn't cover the repo.
- **Nothing analyzed after a while** — look for a "spend cap reached"
  warning: `daemon.max_usd_per_day` pauses analysis until the next UTC day.
  `maajun incidents` shows the spend, and `maajun incidents --failed` shows
  anything that failed three times and is no longer retried.
- **A log file exists but nothing is detected** — `maajun status` now fails
  on an unreadable log. The usual cause is a root-owned file and a non-root
  daemon.
- **"No monitors configured"** — the `[monitor]` section defines no log
  files and no GitHub Actions repos.
- **"A repo is configured but there is no GitHub token"** — you asked for
  PRs without credentials. Run `maajun setup`, or clear `github.repo` to
  fall back to local reports.
- **No PR for an error you expected** — check the fingerprint isn't
  already in `incidents.db`. `status=processed` means an artifact exists
  (`artifact_kind` says whether it was a `pr`, an `issue`, or a local
  `report`); `failed` means the last attempt errored — check logs, fix the
  cause, delete the row to retry. `new` means recorded but not yet
  published: either the spend cap deferred it, or the daemon stopped
  mid-analysis. Either way it is picked up the next time the error is seen.
- **An issue where you expected a PR** — in `fix` mode, an analysis that
  changes no code files an issue instead. The issue says so at the top.
- **Nothing detected** — confirm the log path is right and your log
  format matches `error_pattern` (warnings are *not* matched by default),
  or that errors are recognized stack traces. For JSON logs, set
  `json_level_field`. If `burst_threshold` is above 1, a single error is
  held back on purpose until the threshold is met. For HTTP monitors, run
  with `-v` and check for fetch errors — a monitor that can't reach its
  API logs the failure and returns nothing rather than crashing.

