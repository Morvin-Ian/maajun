# Command reference

Run `maajun <command> --help` for the authoritative options list.

## Monitoring

### `maajun setup`

The one command that configures everything. Writes
`~/.config/maajun/config.toml` (`-c/--config` for another location).

Three steps, of which only the first is required:

1. **AI provider** — picks the provider (skipped when only one is
   implemented), stores a validated API key in the keyring, and picks the
   model. A vendor's models are listed with their prices; a gateway's are
   read from its own `/v1/models` and offered by vendor.
2. **GitHub** *(optional)* — target repo, base branch, and
   [mode](monitoring.md#modes). Suggests the repo from your `origin`
   remote, and reports whether a token is already stored. Checks that the
   token authenticates *and* can push, so a misconfigured token fails here
   rather than at 3 a.m.
3. **Error sources** — always runs, for every configured repo, and asks
   nothing: it probes this host for how the app is deployed (folder, port,
   docker or systemd), reads the code to find where its errors surface, and
   records what it finds. No paths to remember and type — a path typed from
   memory is how a repo ends up watching a file nothing writes. If nothing
   turns up, it says so and gives the command to fix it on the server.

Press Enter to skip any optional step. Re-running is safe: every answer
defaults to your current configuration, and stored credentials are not
re-requested unless you pass `--reconfigure`. Setup ends by running the
`status` checks inline, so you see a verdict rather than another
instruction.

| Flag | Meaning |
|------|---------|
| `--provider NAME` | AI provider to use |
| `--repo OWNER/NAME` | Repository to open PRs on |
| `-b, --base-branch NAME` | Branch to open PRs against |
| `-m, --mode MODE` | `suggest` or `fix` |
| `--test-command CMD` | Command that verifies a fix-mode edit, e.g. `pytest -q` |
| `-l, --logs PATHS` | Comma-separated log files to watch |
| `--non-interactive` | Never prompt; take everything from flags |
| `--reconfigure` | Ask again for credentials that are already stored |

`--non-interactive` never prompts, so it cannot store a credential that
isn't there yet — it exits non-zero if the provider has no key in the
keyring. Run `maajun setup` interactively once per machine, then use
`--non-interactive` to reconfigure repos and monitors unattended.

**GitHub credentials** are settled by [`maajun login`](#maajun-login), and
setup runs the same chooser when there is no credential yet. An existing one
is reused without asking.

Setup ends by offering to start watching, so a fresh machine goes from
nothing to a running daemon in one command.

### `maajun incidents`

List handled incidents with status, sighting count, cost, and the issue or
PR each produced, plus today's spend against `daemon.max_usd_per_day`. A
**Repo** column appears once more than one repo has incidents — two repos'
issues can both render as `#1`, so the repo is what tells them apart.
Local-mode incidents show as `(local)`.

| Flag | Meaning |
|------|---------|
| `-n, --limit N` | How many to show (default 20) |
| `--failed` | Only incidents that failed 3 times and are no longer retried |
| `-r, --repo OWNER/NAME` | Only incidents attributed to this repo |
| `--forget FINGERPRINT` | Forget an incident, so the error is reported again if it returns |

`--forget` is for a fix you have merged: it drops maajun's record of that
incident, so the next occurrence is filed as new instead of bumping a
counter. Without it, a published incident is re-opened on its own if it goes
quiet for `daemon.reopen_after_days` and then comes back — see
[when a fixed bug comes back](monitoring.md#when-a-fixed-bug-comes-back).

### `maajun add-repo REPO`

Add a repository to watch. Repositories are always `[[github.repos]]`
entries, one per repo, so this is how the first one gets configured too.

The owner is optional once GitHub is authenticated — maajun knows the
account, so `add-repo myapp` records `<your-login>/myapp` and says which
slug it used. Without a login it refuses rather than guess an owner, since
a guess would file issues on someone else's repository.
Re-adding a repo already in the list updates only the settings you pass,
leaving the rest of its entry alone and keeping its position — the first
repo is the one global `monitor.log_files` attach to, so order matters.

| Flag | Meaning |
|------|---------|
| `-b, --base-branch NAME` | Branch PRs target (new repos default to `main`) |
| `-m, --mode MODE` | `suggest` or `fix` (new repos default to `suggest`) |
| `-l, --log-files PATHS` | Comma-separated log paths for this repo (replaces the list) |
| `--path DIR` | The app's folder on the server |
| `--port N` | Port the app listens on |
| `--runs TEXT` | How it runs, e.g. `docker compose` |
| `--journald-units NAMES` | Comma-separated systemd units to read the journal of |
| `--docker-containers NAMES` | Comma-separated containers to read logs from |
| `-c, --config PATH` | Config file location |

The deployment flags are usually easier to fill in with
[`discover`](#maajun-discover) than by hand.

See [multiple repositories](monitoring.md#multiple-repositories).

### `maajun login`

Choose how maajun reaches GitHub. It first says what is in use now, then
offers:

```
How should maajun reach GitHub?
  1. GitHub CLI (recommended)
     opens a browser login; maajun then stores nothing
  2. Personal access token
     paste a fine-grained token; stored in the OS keyring
  3. SSH keys for pushing
     use your keys for branches; still needs one of the above for the API
```

1. **GitHub CLI** — maajun runs `gh auth login` for you, handing over the
   terminal for its browser flow, then borrows that token. Nothing of
   maajun's own is stored. If `gh` is missing, it prints how to install it.
2. **Personal access token** — a hidden paste, kept in the OS keyring.
   Offered first when `gh` is not installed.
3. **SSH keys** — records `github.transport = "ssh"` so branches push over
   your keys, after checking GitHub actually accepts one. Issues and pull
   requests still go through the API, so it then asks which credential to
   use for that.

Either way, if your SSH keys work maajun prefers them for pushing and keeps
the token to the API. Set it by hand with
`maajun config github.transport ssh|https|auto`.

It finishes by checking push access to each configured repo and then
**working out where their errors land** — the same discovery `setup` runs,
because a repo maajun can push to but cannot read errors from is still a
repo nobody is watching.

| Flag | Meaning |
|------|---------|
| `-c, --config PATH` | Config file location |

### `maajun status`

Preflight check before `watch`: verifies the provider API key is stored,
the GitHub token is present and (over the network) authenticates and can
push to each configured repo, and that every repo has something watching
it for runtime errors. Sources are listed per repo and probed: a missing
systemd unit or container fails, a stopped one is a warning (its past logs
are still readable), and a missing log file is a warning too (it may not
exist until the app first logs). A repo with **no** runtime source fails,
unless it says `deployment.runtime = "none"` on purpose. Exits non-zero if
any required check fails, so it works in scripts and CI.

| Flag | Meaning |
|------|---------|
| `--no-network` | Skip the GitHub reachability checks, and probing docker/systemd |

| `-c, --config PATH` | Config file location |

### `maajun config [KEY] [VALUE]`

View or change settings without hand-editing TOML.

```bash
maajun config                      # print the whole config
maajun config github.mode          # print one value (secrets show as ***)
maajun config github.mode fix      # set a value
maajun config monitor.log_files /var/log/a.log,/var/log/b.log
maajun config github.test_command "pytest -q" -r team/api   # one repo only
```

| Flag | Meaning |
|------|---------|
| `-r, --repo OWNER/NAME` | Apply a `github.*` key to that repository only |
| `-c, --config PATH` | Config file location |

Keys use dot notation (`ai.*`, `github.repo/base_branch/mode/test_command`,
`monitor.*`, `daemon.*`). Values are type-checked and validated before
saving — an invalid value (e.g. an unknown mode) is rejected and the file
is left unchanged. Writes are comment-preserving: your comments and
formatting survive.

A `github.*` key that also exists per repo (`base_branch`, `mode`,
`test_command`, `log_files`) is written to **every** configured repo, so
one command still covers wanting the same setting everywhere. Use
`--repo` to change a single repository instead.

Deployment keys are addressed as `github.deployment.<name>`
(`path`, `port`, `runs`, `log_files`, `journald_units`,
`docker_containers`, `runtime`) and **require** `--repo`: a folder or a
port describes one deployment, so applying it to every repo is never what
was meant.

```bash
maajun config github.deployment.port 8000 -r team/api
maajun config github.deployment.docker_containers api-web-1,api-nginx-1 -r team/api
maajun config github.deployment.runtime none -r team/lib   # CI-only, on purpose
``` To add or remove a
repository, use [`add-repo`](#maajun-add-repo-repo): there is no
`github.repo` key, since a repository is an entry in the list rather than
a top-level setting.

### `maajun discover`

Find out how each configured repo is deployed on this machine, and where
its runtime errors land. Read-only unless you pass `--save`.

```bash
maajun discover                              # every repo, prints findings
maajun discover -r you/app --save            # write it into the config
maajun discover -r you/app --path /srv/app --save   # when the probe can't tell
```

| Flag | Meaning |
|------|---------|
| `-r, --repo OWNER/NAME` | Only probe this repository |
| `--save` | Write what was found into the config |
| `--no-analyze` | Skip the AI pass over the code (host probes only) |
| `--path DIR` | Where the app is deployed, when the probe cannot tell (needs `--repo`) |
| `-c, --config PATH` | Config file location |

It probes the app's folder, its port, whether it runs under docker or
systemd, and which files, units, or containers its errors reach maajun
through — printing how it found each one. Findings are additive: a value
you set by hand is never overwritten, and a source already configured is
not duplicated. Run it **on the server**, since it reads the local docker
and systemd.

It then **reads the code** (an AI pass, a few cents) to answer what the
host cannot: the stack, the entrypoint, which files the app's logging
config actually writes, and where errors are swallowed instead of logged —
a bare `except: pass`, a handler that returns 500 without logging. Those
log paths are recorded too, so what maajun watches comes from the code
rather than from memory. `--no-analyze` skips it.

That pass is built to be quick: maajun reads the manifest, the Dockerfile
or compose file, and the logging config itself **locally** and hands them
over with the question, so the model answers instead of spending calls
finding files. Its tool budget is capped at a few rounds for the same
reason.

### `maajun watch`

Run the monitoring daemon. **Detached by default**: the terminal comes
straight back and the daemon keeps working.

```bash
maajun watch                 # start in the background
maajun watch --status        # is it running? what has it done?
maajun watch --stop          # stop it
tail -f ~/.local/share/maajun/watch.log
```

| Flag | Meaning |
|------|---------|
| `-c, --config PATH` | Config file (default `~/.config/maajun/config.toml`) |
| `--stop` | Stop the daemon running in the background |
| `--status` | Say whether it is running, and show recent output |
| `-f, --foreground` | Stay attached to this terminal (systemd, containers, debugging) |
| `--once` | One poll cycle, then exit (testing, cron) — implies foreground |
| `--dry-run` | Analyze errors but skip git/PR operations; nothing is persisted |
| `-m, --mode MODE` | Override the configured mode for this run (`suggest`/`fix`) |
| `--backfill` | Also work through the errors already in the logs, once |
| `-v, --verbose` | Debug logging (implies foreground) |

**Starting on a log that already has errors in it.** Watching begins where
each source stands *now*: a log file is read from its end, a journal from
the moment maajun started, a container from the same. What is already there
happened before you asked maajun to watch, and filing an issue for each of
it is not what starting a monitor should mean.

`--backfill` says to read it anyway — the whole log file, the unit's whole
journal, the container's whole log — once, and then carry on normally. It
is worth knowing how much that is first: distinct *error shapes* is what
costs money, not lines, since fingerprinting strips digits and ids, so a
thousand repeats of one traceback are one analysis. Cap the first run if
you are unsure:

```bash
maajun config daemon.max_incidents_per_cycle 3
maajun watch --backfill
```

A log file's position is kept in `<workdir>/cursors`, so a restart carries
on exactly where it stopped instead of re-reading the file. A log rotated
or truncated while the daemon was down is read from the start, since none
of what it holds has been seen.

A background run writes everything to `<workdir>/watch.log` and its pid to
`<workdir>/watch.pid`; starting twice from the same workdir is refused
rather than doubling up. The log is rotated to `watch.log.1` at startup once
it passes 5 MB, so months of running cannot fill the disk. Credentials and monitors are checked *before*
detaching, so a misconfiguration fails in front of you instead of in a log
file nobody is watching yet.

There is no spinner: output is one line per event, which reads the same
live in a terminal and later in the log. `--stop` sends `SIGTERM`, which
the loop handles gracefully — it finishes the incident it is on first, so
no branch is left half-pushed. In `--once` mode it also flushes any partial
state a monitor is holding (a traceback that arrived mid-poll) before
exiting, so nothing is lost.

Under systemd, use `ExecStart=maajun watch --foreground` — the unit
supervises the process itself.

### `maajun report DESCRIPTION`

Investigate an issue **you describe** — rather than one a monitor detected
— and open a PR with the analysis. It runs the same pipeline as `watch`
(sync a clone → agent investigates → incident report → PR), triggered on
demand. Give it a bug report, a vague "checkout is broken", or a stack
trace pasted as the description; the agent reads the target repo and writes
a *what happened / root cause / suggested fix* report, and in `fix` mode
applies the fix too.

```bash
maajun report "Checkout button does nothing on mobile"
maajun report "KeyError: 'discount' in cart totals" -m fix
maajun report "Slow /search endpoint" --dry-run   # analyze only, no PR
```

| Flag | Meaning |
|------|---------|
| `-r, --repo OWNER/NAME` | Target repo. With multiple repos configured you're prompted to pick one when it's omitted; pass it explicitly for scripts |
| `-b, --base-branch NAME` | Branch to base the report on (default: the repo's configured branch) |
| `-m, --mode MODE` | Override the mode for this run (`suggest`/`fix`) |
| `--dry-run` | Analyze and print the report and cost, but skip git/PR |
| `--verbose` | Debug logging (replaces the progress spinner with logs) |
| `-c, --config PATH` | Config file location |

While it works, a live spinner shows the current phase — preparing the
workspace, analyzing with AI, opening the PR — then prints the PR link (or
the failure). The PR branch is `maajun/report-<fingerprint>`. Requires the
same setup as `watch` (a configured repo, a GitHub token, and a provider
key); run `maajun status` first if unsure.

### `maajun promote INCIDENT`

Turn an issue previously created by this Maajun installation into a fix PR.
`INCIDENT` may be its fingerprint (or an unambiguous prefix), its GitHub issue
URL, or its issue number. A bare number needs `--repo` when several configured
repositories have recorded incidents.

```bash
maajun promote 9f3c1ab77e02d418
maajun promote 29 --repo you/shop
maajun promote https://github.com/you/shop/issues/29 --dry-run
```

| Flag | Meaning |
|------|---------|
| `-r, --repo OWNER/NAME` | Disambiguate a fingerprint or issue number |
| `-b, --base-branch NAME` | Base the fix on this branch for this run only |
| `--dry-run` | Re-investigate and print the report without creating a branch or PR |
| `--verbose` | Show debug output |
| `-c, --config PATH` | Config file location |

Only recorded Maajun issues are accepted. Promotion fetches the issue for its
full report and error details, but treats that text as evidence rather than
instructions. It re-investigates the current checkout in fix mode without
changing the saved monitoring mode. The PR says `Fixes <issue URL>`, so the
issue closes only if an owner merges it. Promotion never merges or deploys.

If no code change is justified, no duplicate issue or empty PR is created; the
original issue remains open and the current report is written under Maajun's
data directory.

## Chat

### `maajun chat`

An interactive session that knows every command above, can run them for
you, and remembers what maajun has already done.

```bash
maajun chat
maajun chat --thinking            # use the provider's reasoning model
maajun chat --session 12          # carry an earlier session on
maajun chat -p "am I ready to watch?"   # one answer, no REPL
```

| Flag | Meaning |
|------|---------|
| `--provider NAME` | Override the configured AI provider for this session |
| `--thinking` | Use the provider's reasoning model |
| `-s, --session ID` | Carry an earlier session on: its transcript, cost and context |
| `-p, --prompt TEXT` | Answer one question and exit |
| `--verbose` | Debug logging |
| `-c, --config PATH` | Config file location |

Answers stream as they are written, and each tool call is shown as it runs.
With `-p` there is nobody to approve anything, so a command or edit that
would need a `y` is declined and reported rather than assumed.

**It knows the commands.** The command list is read from the CLI itself at
start-up, so it is never out of date — a command added to maajun is one
chat can describe and run the same day. Ask *"how do I watch a second
repo?"* and it answers from the real `--help`.

**It can carry them out.** Read-only commands (`status`, `incidents`,
`config <key>`, `provider-list`) run immediately. Anything that changes
configuration or opens a pull request shows you the exact command line and
waits for a `y`:

```
> put acme/api into fix mode and give it pytest as the test command

▸ Run: maajun config github.mode fix -r acme/api
  y = yes · a = always, for this tool · n = no · anything else = what to do instead
  Run it? (y/N): y
```

`a` stops asking for that tool for the rest of the session. Anything that
is not a yes, a no, or `a` is passed to the model as an instruction, so a
declined command can be redirected — *"use acme/web instead"* — rather than
just stopped.

`watch`, `reset`, and `sign-out` are never run from chat — the first would
hang the session, and the other two are too destructive to infer from a
sentence. Chat gives you the command to type instead. `setup` needs
`--non-interactive` (it cannot store a new API key that way).

It can also read your code (`read_file`, `grep`, `glob`, `list_dir`,
`git_status`) and edit files. An edit asks permission per file and shows you
the diff first.

Those tools reach three places and no others: the directory you ran `maajun
chat` in, `daemon.workdir`, and the log files listed in your config. A path
outside them is refused outright rather than approved with a warning — and
credential files (`.env`, `id_rsa`, `*.pem`, `.netrc`, `.git-credentials`),
anything under `.git/`, and maajun's own `incidents.db` are refused wherever
they sit. Ask it to read your `.env` and it will tell you it cannot. There is
no shell tool in any mode.

**It remembers.** Every incident maajun has handled — the error, the
analysis, the issue or PR it opened, what it cost — is searchable, as are
your past chat sessions:

```
> what did that checkout KeyError turn out to be?
> which PRs have you opened against acme/api this month?
> what did we decide about fix mode last week?
```

Both searches match on words in any order rather than on one exact phrase,
and both take a date range, so half-remembered references land.

Incidents analyzed before you upgraded are still listed, but only ones
handled from this version on carry their report text — older rows predate
the column and show the issue or PR link alone.

#### Slash commands

| Command | Meaning |
|---------|---------|
| `/help` | List these |
| `/commands` | Every maajun command with a one-line summary |
| `/sessions` | Recent chat sessions and their ids |
| `/history` | This session so far |
| `/cost` | What this session and all chats have cost |
| `/clear` | Forget this session's context; the record is kept and stays searchable |
| `/new` | Start a fresh session |
| `/resume <id>` | Carry an earlier session on, in place |
| `/model [name]` | Show or switch the model |
| `/provider [name]` | Show or switch the AI provider (a key must already be stored) |
| `/forget <id\|all>` | Delete a stored conversation, or all of them |
| `/exit` | Leave |

The prompt keeps a history across sessions (up-arrow) and completes slash
commands on Tab. A message that merely starts with a path — *"/var/log/app.log
is full of errors"* — is a message, not a command.

#### Cost

Chat spend is recorded per session and shown by `/cost`, including the
tokens a turn spent before it failed. `chat.max_usd_per_day` caps it —
$5.00 by default, separate from `daemon.max_usd_per_day`. Past the cap a
new question is refused with the command to raise it; a turn already
running is never cut off mid-sentence. Set it to `0` for no ceiling.

Sessions and messages live in the same database as the incidents
(`<daemon.workdir>/incidents.db`), which is what lets a single question
span both.

## Credentials

### `maajun provider-list`

Show each provider's support status and whether a key is stored.

### `maajun sign-out`

Clear all stored credentials (provider keys and the GitHub token).

### `maajun reset`

Wipe everything and start fresh: the config directory
(`~/.config/maajun`), the data directory (clones, the incident database,
and state — the configured `daemon.workdir` if you changed it), and all
credentials. Prompts for a typed `yes` first; `-f/--force` skips the
confirmation. This cannot be undone — run `maajun setup` afterwards to set
up again.

| Flag | Meaning |
|------|---------|
| `-f, --force` | Skip the confirmation prompt |

## Where secrets live

Provider API keys: the OS keyring (gnome-keyring / macOS Keychain), under
the service name `maajun`. Environment variables are never read.

Where there is no keyring — most servers — credentials go in
`~/.config/maajun/credentials.json` instead, and setup says so before asking
for anything. A JSON file created `chmod 600` inside a `chmod 700`
directory, opened at that mode rather than chmod'ed afterwards, so the
secret is never briefly world-readable. `maajun status` names the file when
it is in use, and `maajun sign-out` empties it.

That is a secret in the clear on disk, protected by its mode. It is also
exactly what the usual advice for a headless box — `keyrings.alt` — does,
with a package in front of it; maajun does it without the extra install and
says so out loud. Install a keyring backend if you want more than file
permissions.

The GitHub token: that keyring first, then `gh auth token` if the keyring
has none. Borrowing the `gh` login means a machine that already has one
needs no second credential and maajun stores nothing extra; `maajun status`
names which source is in use, so there is no doubt about what the daemon
pushes with. `maajun sign-out` clears maajun's own copy — a `gh` session is
not maajun's to end.

Pushing can avoid the token entirely: with `github.transport = "ssh"`,
branches go over your SSH keys and the token is used only for the API.

To use a keyring on a server instead, install a backend for the environment
maajun lives in — `pipx inject maajun keyrings.alt`, `uv tool install maajun
--with keyrings.alt`, or `pip install keyrings.alt`. `setup` prints whichever
of those applies. (`pip install` into the wrong environment is the usual
reason the next run fails identically.)
