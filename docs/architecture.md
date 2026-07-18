# How Maajun works

## Components

```
src/maajun/
├── agent/          The AI agent
│   ├── core.py       Agent loop: model ⇄ tools until a final answer
│   └── tools/        read_file, edit_file, write_file, glob, grep,
│                     bash, list_dir, git_status
├── providers/      AI backends (DeepSeek today; OpenAI-compatible API)
├── monitors/       Error sources (log file tailing)
├── vcs/            Git workspace + GitHub REST client
├── daemon.py       The watch pipeline: monitor → analyze → PR
├── state.py        SQLite incident store (dedup)
├── config.py       TOML config (~/.config/maajun/config.toml)
├── auth.py         Secrets: OS keyring, env-var fallback
└── cli.py          Typer CLI
```

## The agent loop

The agent sends the conversation plus tool definitions to the model. When
the model requests tool calls, the agent executes them, feeds the results
back, and repeats — up to 50 rounds — until the model answers in plain
text. Responses stream token-by-token, including during tool rounds.

### Tool permissions

Tools are split into two classes:

| Class | Tools | Behavior |
|-------|-------|----------|
| Safe (read-only) | `read_file`, `glob`, `grep`, `list_dir`, `git_status` | Always allowed |
| Gated | `bash`, `edit_file`, `write_file` | Need approval per call |

Approval is an injectable async callback. Each context supplies its own
policy:

- **`maajun chat`** — asks you interactively before each gated call
  (`--auto-approve` skips the prompts).
- **`maajun watch`, suggest mode** — no callback, so every gated call is
  denied: the agent is strictly read-only.
- **`maajun watch`, fix mode** — file edits are approved only for paths
  inside the daemon's isolated workspace clone; `bash` is always denied.

A denied call is not an error: the model receives a message telling it
the user refused and not to retry, so it adapts (e.g. writes the fix as
a suggestion instead).

## The incident pipeline (`maajun watch`)

1. **Detect** — monitors poll error sources on an interval. The log file
   monitor tails files incrementally, surviving log rotation and
   truncation. It recognizes Python tracebacks (including ones split
   across polls) and lines matching the error pattern. A `logging.exception`
   pair — an ERROR line immediately followed by a traceback — is merged
   into a single event.
2. **Dedup** — every event gets a fingerprint: a hash of the error text
   with digits and hex addresses stripped, so the same crash at a
   different line number or timestamp is still the same incident.
   Fingerprints live in a SQLite store; known ones just bump a counter.
3. **Analyze** — for a new fingerprint, the daemon syncs an isolated
   clone of your repo, creates a branch `maajun/incident-<fingerprint>`,
   and asks the agent to investigate. The agent reads the code with its
   safe tools and writes a structured report (what happened / root cause
   / suggested fix). In fix mode it may also edit files in the clone.
4. **Publish** — the report is committed as
   `docs/incidents/<fingerprint>.md`, the branch is pushed, and a pull
   request is opened with the report as its body. If the branch already
   has an open PR it is reused, never duplicated.
5. **Record** — the incident is marked processed with its PR URL. If any
   step fails, it is marked failed and the daemon moves on; one bad
   incident never kills the loop.

## Security posture

- The GitHub token is passed to git via `GIT_ASKPASS`, so it never
  appears in remote URLs, `.git/config`, or the process list.
- Secrets live in the OS keyring; on headless servers, environment
  variables (`GITHUB_TOKEN`, `DEEPSEEK_API_KEY`) take precedence.
- The daemon's agent never touches your running application — it works
  in its own clone under the daemon workdir.
- `bash` is never available to the daemon's agent, in any mode.
