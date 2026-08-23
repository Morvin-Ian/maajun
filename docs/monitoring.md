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

# How and where this repo runs, and where its runtime errors land.
# Fill it in with `maajun discover --repo owner/name --save`.
[github.repos.deployment]
path = "/srv/myapp"           # the app's folder on the server
port = 8000                   # what it listens on
runs = "docker compose"       # free text: how it is started
log_files = ["/srv/myapp/logs/error.log", "/var/log/nginx/error.log"]
journald_units = ["myapp.service"]      # journalctl -u myapp.service
docker_containers = ["myapp-web-1"]     # docker logs myapp-web-1
# runtime = "none"            # this repo deliberately has no runtime source

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

[chat]
# max_usd_per_day = 5.0             # `maajun chat`'s own daily budget (0 = no cap)
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

### Runtime errors, however you deploy

Runtime errors — the 500s your users actually hit — are what maajun is for,
and every deployment sends them to one of three places:

| Sink | Config | Typical deployment |
|---|---|---|
| A **file** | `log_files` | Django `FileHandler`, gunicorn `--error-logfile`, nginx on the host, supervisor |
| The **journal** | `journald_units` | anything logging to stdout under systemd: gunicorn, uvicorn, nginx, supervisor |
| A **container's stdout** | `docker_containers` | docker, docker compose, an app container, an nginx container |

So there is no list of supported deploy methods to match — bare gunicorn,
systemd, docker, compose, nginx in a container, nginx alone, a mix of them.
Say where the errors land and maajun reads them. All three go through the
same detection: `error_pattern`, the traceback grouping, and the burst
settings under `[monitor]` apply whichever sink a repo uses.

Mixing is the normal case. An app in compose behind nginx on the host:

```toml
[github.repos.deployment]
path = "/srv/kfl"
port = 8000
runs = "docker compose"
docker_containers = ["kfl-web-1"]           # the app's own exceptions
log_files = ["/var/log/nginx/error.log"]    # 502s the app never sees
```

Each source belongs to the repo whose block it sits in, so it always knows
which app it watches and which repo to file against.

### Finding it for you

You never write that block by hand, and you are never asked for a path.
`setup` and `login` both work it out for every configured repo, and neither
can skip it — a setup that does not know where the errors land has not set
anything up. To re-run it later:

```bash
maajun discover                     # every repo, prints what it finds
maajun discover -r you/app --save   # write it into the config
```

`discover` probes **this machine** for each configured repo: the app's
folder (by the origin remote of the checkout, or the working directory a
compose container was built from), its port (from the published container
port or the unit's `ExecStart`), whether it runs under docker or systemd,
and which files, units, or containers its errors reach maajun through. It
explains how it found each one, and writes nothing unless you pass
`--save`. `maajun setup` runs the same probe and offers what it finds.

### Asking the code, not your memory

The host says what is *running*; only the code says whether a failed
request ever reaches a log at all. So `discover` also reads the repo with
the AI — the same tools the incident agent uses, read-only — and reports:

- the **stack** and **entrypoint** (recorded as `deployment.stack`, and
  given to the agent later so a fix is written against the real framework),
- the **files the logging config actually writes**, which become watched
  sources,
- **where errors are swallowed**: a bare `except: pass`, a 500 handler that
  logs nothing, a `FileHandler` pointed at a directory that is never
  created,
- the **log format**, so `error_pattern` and `json_level_field` can be
  matched to it — printed as commands to run, never changed silently,
- **where bugs are likely**, from reading the code.

```
Errors are logged to:
  • /srv/shop/logs/django-error.log
Errors that would be missed:
  • views.py:11 — except Exception: pass swallows database errors
  • settings.py:9 — FileHandler targets logs/ but nothing creates it
To catch them:
  add os.makedirs(BASE_DIR / 'logs', exist_ok=True) before LOGGING …
```

It costs a few cents per repo and needs a checkout to read, so run it on
the server (or pass `--path`). `--no-analyze` turns it off.

Because it reads the local docker and systemd, it has to run **on the
server** — the same place the daemon runs. A finding never overwrites
something you set by hand; it only fills in blanks and adds sources.

### A repo with no runtime source

`maajun status` **fails** for a repo that nothing watches for runtime
errors, and `maajun watch` warns about it on startup. Watching only GitHub
Actions is watching CI, not your users' requests — which is exactly how a
config ends up looking healthy while every 500 goes unreported.

If that is deliberate for a repo (a library, a CI-only project), say so and
the check passes:

```bash
maajun config github.deployment.runtime none -r you/app
```

### Log files

Point a repo's `deployment.log_files` at the files your app writes (or
`monitor.log_files` for a single-repo or local setup). The monitor
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

### Errors that are already in the log

Watching starts from where each source stands when maajun starts: the end
of a log file, and the present moment for a journal or a container. A log
that has been collecting errors for months does not become months of issues
the first time you run `maajun watch`.

To work through what is already there:

```bash
maajun watch --backfill
```

That reads the whole log file, the unit's whole journal, and the
container's whole log — once — and then carries on from the end as usual.
What it costs is one analysis per distinct *error shape*, not per line:
fingerprints ignore digits, hex and ids, so a thousand repeats of one
traceback are a single incident. `daemon.max_incidents_per_cycle` and
`daemon.max_usd_per_day` bound the first run if the backlog is unknown.

A log file's byte offset is kept in `<workdir>/cursors`, so a restart
resumes exactly where it stopped rather than re-reading the file — which
also means the errors written while the daemon was down are picked up when
it comes back. A file rotated or truncated in the meantime is read from the
start, since nothing in it has been seen.

### The journal and container logs

`journald_units` runs `journalctl -u <unit> -o cat` each poll. The position
is journalctl's own cursor file under `daemon.workdir/cursors`, so a daemon
restart resumes exactly where it stopped; until that file exists a time
window from startup stands in, rather than filing every historical error in
the journal at once.

`docker_containers` runs `docker logs --since <last poll> <name>`, reading
the container's stderr as well as its stdout — an unhandled exception goes
to stderr, so reading only stdout would miss every traceback.

Neither uses `-t` or the default journal format on purpose: both prefix
every line with a timestamp, which leaves the indented lines of a traceback
no longer indented and so impossible to group into one incident.

An unreadable source — no docker on the host, a container that no longer
exists, a unit name with a typo — is logged once and skipped, never once per
poll, and never at the cost of the other monitors. `maajun status` reports
it: a missing unit or container fails, a stopped one is a warning, since its
past logs are still readable.

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

Actions monitoring is *additional*, never a replacement: log monitors are
built from `monitor.log_files` and each repo's `log_files` regardless, and
enabling Actions never clears them. But it only sees CI, so a setup with
Actions and no log file never notices a failed request — `setup` and
`status` both say so when that is the configuration you end up with.

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

A headless server usually has no keyring, and maajun carries on regardless:
credentials go in `~/.config/maajun/credentials.json`, `chmod 600` and
owner-only, and setup says so before asking for anything. Install a keyring
backend if you would rather have one — setup prints the command for your
install.

The GitHub token is the exception to needing one at all: run
`maajun login`, pick the GitHub CLI, and maajun uses that session's token
without storing anything. If your SSH keys already work with GitHub, it
records `github.transport = "ssh"` so branches are pushed over them.

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
maajun watch                 # start watching, in the background
maajun watch --status        # running? what has it done?
maajun watch --stop          # stop it
maajun watch --dry-run       # analyze errors, but skip git/PR — test your config
maajun watch --once          # single poll cycle, then exit (cron)
maajun watch -f              # stay attached to this terminal
maajun watch -m fix          # override the configured mode for this run
```

`watch` detaches by default: the terminal comes back and the daemon keeps
working, logging to `<workdir>/watch.log`. Credentials and monitors are
checked before it detaches, so a broken config fails in front of you.
Starting twice from one workdir is refused. `--dry-run`, `--once` and
`-v` stay attached, since you are reading their output.

```bash
tail -f ~/.local/share/maajun/watch.log
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
omitted; pass `--repo owner/name` to skip the prompt in scripts. `--mode`
and `--dry-run` work the same as on `watch`, and `--base-branch` — which
`watch` has no equivalent for, since it takes each repo's branch from the
config — bases this one report on a different branch.

Each report is recorded in the same incident database as detected errors,
so it appears in `maajun incidents` — listed as `report` under **Caught
by** — with its tokens and cost counted against the same daily cap. Unlike
the watch loop, `report` is not dedup-gated: running it again on the same
description re-investigates and files a fresh issue (or, in `fix` mode,
updates the existing `maajun/report-<fingerprint>` branch and PR).

## Modes

| | `suggest` (default) | `fix` |
|---|---|---|
| Artifact | A GitHub **issue**, always | A **pull request**, always |
| Agent file access | Read-only | May edit files, but only inside its own clone |
| Agent shell access | None | None |
| Contains | Incident report + suggested fix | Applied fix + incident report |
| Verified | n/a — no diff | By `test_command`, if set |
| You review | The suggestion | The actual diff |

Suggest mode files an **issue**, not a pull request: an analysis with no
diff is an issue, and it is cheaper — the repo is cloned to read, but no
branch is created and nothing is pushed.

Fix mode always ends in a **pull request**. When the agent concludes no
code change is warranted, the PR carries the incident report alone and says
so at the top — so the finding is still in one reviewable place, rather
than the mode quietly producing something else.

**Nothing is ever filed empty.** A report that comes back blank, or with
none of its sections filled in, is asked for once more; if it is still
unusable, no issue or PR is created and the incident is recorded as failed.
An artifact with nothing in it costs the reader more than it gives, and
hides that the run went wrong — `maajun incidents` shows it instead.

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

### When a fixed bug comes back

An error that is still happening is reported once: every sighting bumps its
counter, and nothing new is filed. Spamming an unfixed bug helps nobody.

An error that **stopped and started again** is a different thing — someone
fixed it, and the fix did not hold — so maajun reports it again. The rule is
a quiet gap: a published incident that goes `daemon.reopen_after_days`
(7 by default) without being seen and then happens again is re-opened. The
new issue or pull request says so at the top and links the earlier one, the
incident keeps its history (`first_seen`, the total count), and the agent is
told to check whether an earlier fix was reverted, incomplete, or papered
over a symptom rather than explaining it as new.

```toml
[daemon]
reopen_after_days = 7.0   # 0 reports each error once, ever
```

If you would rather not wait out the gap — you have merged the fix and want
to hear immediately if it returns — forget the incident:

```bash
maajun incidents --forget <fingerprint>
```

That drops maajun's record of it, so the next occurrence is treated as new.
A partial fingerprint is enough, and `--repo` settles it when two
repositories share one.

## Errors that are not bugs

A logged error is not automatically a defect. A validator refusing bad
input, a 401 for a wrong password, a rate limiter returning 429 — the code
did what it is built to do. maajun closes those as `ignored` instead of
filing them, in two passes:

1. **Signatures**, matched against the raw error before any AI call, so an
   obvious guard costs nothing. They cover errors named after their own
   intent: `ValidationError`, `AuthenticationFailed`, `PermissionDenied`,
   `403 Forbidden`, `CSRF`, `RateLimitExceeded`, `429 Too Many Requests`,
   `404 Not Found`.
2. **The agent's verdict**, read from the `## Verdict` line of the report.
   This is the one that recognises a guard specific to your application,
   because the agent has read the code that raised it. A report with no
   verdict, or one that says it cannot tell, is filed as a defect.

```bash
maajun incidents --ignored     # what was passed over, and why
```

Nothing is deleted: the incident row stays, which is also what keeps the
same error from being re-examined on every poll. Tune it in `[monitor]`:

| Setting | Default | Effect |
|---|---|---|
| `ignore_by_design` | `true` | `false` analyzes every logged error |
| `ignore_patterns` | `[]` | Extra regexes, tried before the shipped ones |

```bash
maajun config monitor.ignore_by_design false
```

An error the signatures wrongly pass over shows up in `--ignored` rather than
vanishing, so a bad match is visible. If one is wrong for your codebase,
narrow it with `ignore_by_design = false` and lean on the agent's verdict
instead.

## Cost tracking

Each processed incident records the prompt/completion token counts and
the USD cost of its analysis in `incidents.db` (`prompt_tokens`,
`completion_tokens`, `cost_usd` columns), priced by the model that
actually ran. Prompt tokens the provider served from its prefix cache are
priced at its cache-hit rate rather than in full — most of an
investigation's input, since every tool round resends a growing prefix —
tokens Anthropic stored into its cache at the write rate, and DeepSeek's
off-peak half price applied from the clock. The `prompt_tokens` column is the
total either way; only `cost_usd` reflects the split. On Ox Alpha every row
records zero, because nothing is billed. The cost is also logged when the PR opens, and `--dry-run`
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

`maajun chat` is budgeted separately, by `chat.max_usd_per_day` (also
`5.0`): it sums what today's chat sessions cost before each question and
refuses a new one past the cap. An answer in progress is never truncated,
and the tokens a failed turn spent are counted too — they were billed.


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
ExecStart=/home/deploy/.local/bin/maajun watch --foreground
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now maajun
journalctl -u maajun -f
```

`--foreground` matters here: systemd supervises the process itself, and a
daemon that detached would look like one that exited. maajun's own
background mode is for a shell, not for a service manager.

The daemon shuts down gracefully on `SIGTERM`/`SIGINT`: it finishes the
incident it is currently processing before exiting, so
`systemctl stop`/`restart` never leaves a half-pushed branch.

Note: after a restart each source resumes from its own cursor — a log
file's byte offset, journald's own cursor — so nothing is re-read and
nothing written during the restart is lost. CI failures are re-fetched;
deduplication makes that overlap harmless, since already-processed errors
are recognized and skipped without any AI calls.

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

