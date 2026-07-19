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

### `maajun init`

Write a starter config to `~/.config/maajun/config.toml`
(`-c/--config` for another location). Asks before overwriting.

### `maajun github-login`

Set up GitHub access in one step: prompts for the target repository
(`owner/name`, shown as you type; the configured value is offered as the
default) and then a personal access token (input hidden). Validates that
the token authenticates and can push to that repo, saves the repo into
`github.repo` in the config file, and stores the token in the keyring.

### `maajun watch`

Run the monitoring daemon.

| Flag | Meaning |
|------|---------|
| `-c, --config PATH` | Config file (default `~/.config/maajun/config.toml`) |
| `--once` | One poll cycle, then exit (testing, cron) |
| `--dry-run` | Analyze errors but skip git/PR operations; nothing is persisted |
| `-v, --verbose` | Debug logging |

The daemon exits gracefully on `SIGTERM`/`SIGINT`, finishing the
incident it is currently processing first.

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

## Where secrets live

1. **Environment variables win**: `DEEPSEEK_API_KEY` (per provider,
   `<PROVIDER>_API_KEY`) and `GITHUB_TOKEN`. Use these on servers.
2. Otherwise the OS keyring (gnome-keyring / macOS Keychain), service
   name `maajun`.
