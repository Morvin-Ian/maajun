# Command reference

Run `maajun <command> --help` for the authoritative options list.

## Monitoring

### `maajun setup`

The one command that configures everything. Writes
`~/.config/maajun/config.toml` (`-c/--config` for another location).

Three steps, of which only the first is required:

1. **AI provider** — picks the provider (skipped when only one is
   implemented) and stores a validated API key in the keyring.
2. **GitHub** *(optional)* — target repo, base branch, and
   [mode](monitoring.md#modes). Suggests the repo from your `origin`
   remote, and reports whether a token is already stored. Checks that the
   token authenticates *and* can push, so a misconfigured token fails here
   rather than at 3 a.m.
3. **Error sources** *(optional)* — log files, and GitHub Actions
   (reusing the GitHub token rather than asking for a second one).

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
| `--github-actions` | Watch the configured repos for failed workflow runs |
| `--non-interactive` | Never prompt; take everything from flags |
| `--reconfigure` | Ask again for credentials that are already stored |

`--non-interactive` never prompts, so it cannot store a credential that
isn't there yet — it exits non-zero if the provider has no key in the
keyring. Run `maajun setup` interactively once per machine, then use
`--non-interactive` to reconfigure repos and monitors unattended.

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

### `maajun add-repo REPO`

Add a repository to watch. Repositories are always `[[github.repos]]`
entries, one per repo, so this is how the first one gets configured too.
Re-adding a repo already in the list updates only the settings you pass,
leaving the rest of its entry alone and keeping its position — the first
repo is the one global `monitor.log_files` attach to, so order matters.

| Flag | Meaning |
|------|---------|
| `-b, --base-branch NAME` | Branch PRs target (new repos default to `main`) |
| `-m, --mode MODE` | `suggest` or `fix` (new repos default to `suggest`) |
| `-l, --log-files PATHS` | Comma-separated log paths for this repo (replaces the list) |
| `-c, --config PATH` | Config file location |

See [multiple repositories](monitoring.md#multiple-repositories).

### `maajun status`

Preflight check before `watch`: verifies the provider API key is stored,
the GitHub token is present and (over the network) authenticates and can
push to each configured repo, and that at least one monitor is
configured. Missing log files are reported as warnings (they may not
exist until the app first logs). Exits non-zero if any required check
fails, so it works in scripts and CI.

| Flag | Meaning |
|------|---------|
| `--no-network` | Skip the GitHub reachability checks (offline/fast) |
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
`--repo` to change a single repository instead. To add or remove a
repository, use [`add-repo`](#maajun-add-repo-repo): there is no
`github.repo` key, since a repository is an entry in the list rather than
a top-level setting.

### `maajun watch`

Run the monitoring daemon.

| Flag | Meaning |
|------|---------|
| `-c, --config PATH` | Config file (default `~/.config/maajun/config.toml`) |
| `--once` | One poll cycle, then exit (testing, cron) |
| `--dry-run` | Analyze errors but skip git/PR operations; nothing is persisted |
| `-m, --mode MODE` | Override the configured mode for this run (`suggest`/`fix`) |
| `-v, --verbose` | Debug logging |

The daemon exits gracefully on `SIGTERM`/`SIGINT`, finishing the
incident it is currently processing first. In `--once` mode it also
flushes any partial state a monitor is still holding (e.g. a traceback
that arrived mid-poll) before exiting, so nothing is lost.

An interactive run (a real TTY, no `-v`) shows a live status spinner —
`Watching for errors…`, switching to the analysis phases while it works,
and printing a line whenever a PR opens or an incident fails. `--verbose`
swaps the spinner for full debug logs, and non-interactive runs (piped
output, systemd) always log instead of animating.

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

The OS keyring (gnome-keyring / macOS Keychain), under the service name
`maajun` — and nowhere else. Environment variables are not read, so there
is exactly one place to look when `status` and the daemon disagree about a
credential.

Nothing else is a source — not environment variables, and not a
`gh auth login` session. maajun never shells out to another tool for a
credential: a borrowed token can change without maajun being told, and
`status` could not then vouch for what the daemon will push with.

maajun therefore needs a working keyring backend; on a headless server,
install one (`keyrings.alt`, `gnome-keyring`) before running `setup`.
