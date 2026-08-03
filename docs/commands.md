# Command reference

Run `maajun <command> --help` for the authoritative options list.

## Chat

### `maajun chat`

Interactive chat session with tool use and streamed responses.

| Flag | Meaning |
|------|---------|
| `-p, --provider NAME` | AI provider to use (default: pick from configured) |
| `--thinking` | Use the provider's reasoning model |
| `--auto-approve` | Run gated tools (bash, file edits) without asking |

In-chat commands: `/clear` (new session), `/history`, `/quit`.

By default you're prompted before every `bash`, `edit_file`, or
`write_file` call, with the tool's arguments shown. Denying is safe —
the model is told and adapts.

## Monitoring

### `maajun setup`

The one command that configures everything. Writes
`~/.config/maajun/config.toml` (`-c/--config` for another location).

Four steps, of which only the first is required:

1. **AI provider** — picks the provider (skipped when only one is
   implemented) and stores a validated API key in the keyring.
2. **GitHub** *(optional)* — target repo, base branch, and
   [mode](monitoring.md#modes). Suggests the repo from your `origin`
   remote, and reports whether a token already exists in `$GITHUB_TOKEN`,
   the keyring, or `gh auth login`. Checks that the token authenticates
   *and* can push, so a misconfigured token fails here rather than at
   3 a.m.
3. **Error sources** *(optional)* — log files, GitHub Actions (reusing
   the GitHub token rather than asking for a second one), and Sentry.
4. **Email notifications** *(optional)*.

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
| `-l, --logs PATHS` | Comma-separated log files to watch |
| `--github-actions` | Watch the configured repos for failed workflow runs |
| `--sentry ORG/PROJECT` | Sentry project to monitor |
| `--non-interactive` | Never prompt; use flags and the environment |
| `--reconfigure` | Ask again for credentials that are already stored |

In `--non-interactive` mode secrets are read from the environment
(`DEEPSEEK_API_KEY`, `GITHUB_TOKEN`, `MAAJUN_SENTRY_TOKEN`) rather than
from flags, so they never land in shell history.

### `maajun init`

Writes a starter config without the credential steps. `maajun setup` is
the recommended entry point; `init --no-interactive` is still useful to
drop a fully commented template and edit it by hand.

### `maajun add-repo REPO`

Add another repository to watch, which turns on **multi-repo mode**. The
first call migrates an existing single-repo config into a `[[github.repos]]`
list; re-adding the same repo updates its entry rather than duplicating it.

| Flag | Meaning |
|------|---------|
| `-b, --base-branch NAME` | Branch PRs target (default `main`) |
| `-m, --mode MODE` | `suggest` or `fix` (default `suggest`) |
| `-l, --log-files PATHS` | Comma-separated log paths for this repo |
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
```

Keys use dot notation (`ai.*`, `github.repo/base_branch/mode`,
`monitor.*`, `daemon.workdir`, `daemon.email.*`). Values are type-checked
and validated before saving — an invalid value (e.g. an unknown mode) is
rejected and the file is left unchanged. Setting `github.mode` also
updates every repo in a multi-repo config. Writes are comment-preserving:
your comments and formatting survive. To add or manage repositories in
multi-repo mode, use [`add-repo`](#maajun-add-repo-repo).

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

### `maajun login`

Store an AI provider API key interactively (input hidden), then validate
it against the provider's API.

### `maajun provider-list`

Show each provider's support status and whether a key is stored.

### `maajun config-set-key PROVIDER [KEY]`

Non-interactive key storage. Omit `KEY` to be prompted securely —
passing it as an argument leaves it in your shell history.

### `maajun config-remove-key PROVIDER`

Remove a stored provider key.

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

1. **Environment variables win**: `DEEPSEEK_API_KEY` (per provider,
   `<PROVIDER>_API_KEY`) and `GITHUB_TOKEN`. Use these on servers.
2. Otherwise the OS keyring (gnome-keyring / macOS Keychain), service
   name `maajun`.
