# Monitoring guide

Run maajun on the server where your app writes logs. It watches for new
errors and turns each one into a pull request on GitHub.

## 1. Configure

```bash
maajun init    # writes ~/.config/maajun/config.toml
```

Full config reference:

```toml
[ai]
provider = "deepseek"
# model = "deepseek-chat"     # provider default if omitted
# thinking_mode = true        # use the reasoning model (deepseek-reasoner)
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

[daemon]
workdir = "~/.local/share/maajun"   # clones, incident DB, state
```

Use `--config /path/to/config.toml` on any command to point somewhere else.

## 2. Give it GitHub access

Create a **fine-grained personal access token** at
<https://github.com/settings/personal-access-tokens>:

- **Repository access**: only the repo in `github.repo`
- **Permissions**:
  - Contents: **Read and write** (push branches)
  - Pull requests: **Read and write** (open PRs)

Store it:

```bash
maajun github-login        # interactive; validates the token first
```

`github-login` checks that the token authenticates *and* that it can push
to your configured repo, so misconfigured tokens fail here rather than at
3 a.m.

On a headless server without a keyring, use the environment instead —
env vars always take precedence:

```bash
export GITHUB_TOKEN=github_pat_...
export DEEPSEEK_API_KEY=sk-...
```

## 3. Run

```bash
maajun watch --once    # single poll cycle — good for testing and cron
maajun watch           # continuous monitoring
maajun watch -v        # debug logging
```

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

## Deduplication

Errors are fingerprinted with volatile parts (line numbers, addresses,
timestamps, ids) stripped, so the same crash repeating only increments a
counter in the incident database — one error, one PR, ever. The incident
history lives in `<workdir>/incidents.db` (SQLite); delete a row (or the
file) to make maajun treat an error as new again.

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

Note: after a restart the daemon re-reads watched logs from the start.
Deduplication makes this harmless — already-processed errors are
recognized and skipped without any AI calls.

## Troubleshooting

- **"No GitHub token"** — run `maajun github-login` or set `GITHUB_TOKEN`.
- **"Token cannot push"** — the fine-grained PAT is missing Contents
  write access or doesn't cover the repo.
- **No PR for an error you expected** — check the fingerprint isn't
  already in `incidents.db` (`status=processed` means a PR exists;
  `failed` means the last attempt errored — check logs, fix the cause,
  delete the row to retry).
- **Nothing detected** — confirm the log path is right and your log
  format matches `error_pattern`, or that errors are Python tracebacks.
