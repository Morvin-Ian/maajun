# Monitoring guide

Maajun watches your error sources — local log files, Sentry, GitHub
Actions — and turns each new error into a pull request on GitHub. For
log files it must run on the server that writes them; the Sentry and
GitHub Actions monitors work from anywhere with network access.

## 1. Configure

```bash
maajun init    # writes ~/.config/maajun/config.toml
```

Full config reference:

```toml
[ai]
provider = "deepseek"
# model = "deepseek-v4-flash"  # provider default if omitted
# thinking_mode = true         # use the reasoning model (deepseek-v4-pro)
# temperature = 0.3
# max_tokens = 4096

[github]
repo = "owner/name"           # repository maajun opens PRs on
base_branch = "main"          # branch PRs target
mode = "suggest"              # "suggest" or "fix" — see Modes below

[monitor]
log_files = [                 # files to tail for errors
  "/var/log/myapp/error.log",
]
error_pattern = "\\b(ERROR|CRITICAL|FATAL)\\b"   # regex for error lines
poll_interval = 30            # seconds between polls

# Sentry — poll unresolved issues (optional)
# sentry_auth_token = "sntrys_..."
# sentry_org = "my-org"
# sentry_projects = ["my-project"]

# GitHub Actions — poll failed workflow runs (optional)
# github_actions_token = "github_pat_..."
# github_actions_repos = ["owner/name"]

[daemon]
workdir = "~/.local/share/maajun"   # clones, incident DB, state
# notify_webhook_urls = [           # Slack-compatible webhooks (optional)
#   "https://hooks.slack.com/services/...",
# ]
```

At least one error source must be configured — log files, Sentry, or
GitHub Actions; the daemon refuses to start with nothing to watch. Use
`--config /path/to/config.toml` on any command to point somewhere else.

## Error sources

### Log files

Point `monitor.log_files` at the files your app writes. The monitor
tails them incrementally (surviving rotation and truncation), recognizes
Python tracebacks — including ones split across polls — and lines
matching `error_pattern`. An ERROR line immediately followed by a
traceback (`logging.exception`) is merged into a single event.

### Sentry

Set `sentry_auth_token`, `sentry_org`, and `sentry_projects` to poll
each project's unresolved issues. Create the token at
**Sentry → Settings → Auth Tokens** with the `event:read` and
`project:read` scopes. Each Sentry issue becomes one incident,
fingerprinted by its Sentry short id (e.g. `PROJ-1A2B`), and the
incident details include the event count, users affected, and a link
back to Sentry.

This is the source to use when your app runs somewhere maajun can't
read logs — production servers, serverless, browsers.

### GitHub Actions

Set `github_actions_token` and `github_actions_repos` to poll each repo
for failed workflow runs. The token needs read access to the repos'
actions. A failure becomes an incident fingerprinted by the commit SHA,
so multiple workflows failing on the same commit produce a single
incident (one commit, one root cause), with the run details and a link
to the failed run.

## 2. Give it GitHub access

Create a **fine-grained personal access token** at
<https://github.com/settings/personal-access-tokens>:

- **Repository access**: only the repo in `github.repo`
- **Permissions**:
  - Contents: **Read and write** (push branches)
  - Pull requests: **Read and write** (open PRs)

Store it:

```bash
maajun github-login        # asks for the repo, then the token
```

`github-login` prompts for the target repository (visible input, saved
to `github.repo` in your config) and the token (hidden input), then
checks that the token authenticates *and* that it can push to that repo
— so misconfigured tokens fail here rather than at 3 a.m.

On a headless server without a keyring, use the environment instead —
env vars always take precedence:

```bash
export GITHUB_TOKEN=github_pat_...
export DEEPSEEK_API_KEY=sk-...
```

## 3. Run

```bash
maajun watch --dry-run       # analyze errors, but skip git/PR — test your config
maajun watch --once          # single poll cycle, then exit (cron)
maajun watch                 # continuous monitoring
maajun watch -v              # debug logging
```

### Dry run

`--dry-run` exercises everything except publishing: monitors poll, new
errors are deduplicated and analyzed by the AI, and the report and cost
are logged — but no branch, commit, or PR is created, and nothing is
recorded in the incident database. Drop the flag and the same errors
are processed for real. It's the safe way to verify a new config or
error source before letting maajun loose on your repo.

## Modes

| | `suggest` (default) | `fix` |
|---|---|---|
| Agent file access | Read-only | May edit files, but only inside its own clone |
| Agent shell access | None | None |
| PR contains | Incident report + suggested fix | Applied fix + incident report |
| You review | The suggestion | The actual diff |

Either way, **nothing merges without your review** — maajun only opens
the PR. Start with `suggest`; switch to `fix` once you trust the reports.

## What a PR looks like

- Branch: `maajun/incident-<fingerprint>`
- Title: `[maajun] KeyError: 'discount'`
- Body: the incident report — what happened, root cause with file/line
  references, suggested fix — plus the error source and fingerprint
- Committed file: `docs/incidents/<fingerprint>.md` (the same report, so
  incidents are documented in-repo even after the PR is closed)

## Notifications

Add Slack-compatible webhook URLs to `daemon.notify_webhook_urls` to get
a message whenever maajun opens a PR (with a link) or fails to process
an incident (with the reason). Discord works too via its
`/slack`-compatible webhook endpoint. Delivery failures are logged and
never interrupt the pipeline.

## Deduplication

Every error gets a stable fingerprint before any AI call:

- **Log errors** — a hash of the error text with volatile parts (line
  numbers, addresses, timestamps, ids) stripped, so the same crash
  repeating looks identical.
- **Sentry issues** — the Sentry short id.
- **CI failures** — the commit SHA.

Known fingerprints only increment a counter in the incident database —
one error, one PR, ever. The incident history lives in
`<workdir>/incidents.db` (SQLite); delete a row (or the file) to make
maajun treat an error as new again.

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
Environment=GITHUB_TOKEN=github_pat_...
Environment=DEEPSEEK_API_KEY=sk-...
# Or keep secrets out of the unit file:
# EnvironmentFile=/etc/maajun/secrets.env
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
and re-fetches current Sentry issues and CI failures. Deduplication
makes this harmless — already-processed errors are recognized and
skipped without any AI calls.

## Troubleshooting

- **"No GitHub token"** — run `maajun github-login` or set `GITHUB_TOKEN`.
- **"Token cannot push"** — the fine-grained PAT is missing Contents
  write access or doesn't cover the repo.
- **"No monitors configured"** — the `[monitor]` section defines no log
  files, Sentry settings, or GitHub Actions repos.
- **No PR for an error you expected** — check the fingerprint isn't
  already in `incidents.db` (`status=processed` means a PR exists;
  `failed` means the last attempt errored — check logs, fix the cause,
  delete the row to retry).
- **Nothing detected** — confirm the log path is right and your log
  format matches `error_pattern`, or that errors are Python tracebacks.
  For Sentry/GitHub Actions, run with `-v` and check for fetch errors —
  a monitor that can't reach its API logs the failure and returns
  nothing rather than crashing.
