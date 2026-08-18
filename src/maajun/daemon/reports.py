from __future__ import annotations

from pathlib import Path

from maajun.config import RepoConfig
from maajun.monitors import ErrorEvent
from maajun.utils import truncate
from maajun.vcs import CommandResult

PROJECT_URL = "https://github.com/Morvin-Ian/maajun"

# Error details are quoted verbatim; cap them so a runaway log line can't
# produce a body GitHub rejects.
MAX_DETAILS_IN_BODY = 4000
MAX_TEST_OUTPUT = 3000


def provenance(event: ErrorEvent) -> str:
    """Where this came from, so an artifact is traceable back to the event."""
    return (
        f"- Repo: `{event.repo}`\n" if event.repo else ""
    ) + (
        f"- Source: `{event.source}`\n"
        f"- First seen: {event.timestamp}\n"
        f"- Fingerprint: `{event.fingerprint}`\n"
        f"- Opened automatically by [maajun]({PROJECT_URL})."
    )


def issue_body(event: ErrorEvent, report: str, note: str = "") -> str:
    """Suggest mode's artifact: the analysis plus the raw error.

    `note` carries a leading remark — used when fix mode falls back to an
    issue because the analysis changed no code, so the reader is not left
    wondering why a fix-mode repo produced an issue.
    """
    return (
        (f"{note}\n\n" if note else "")
        + f"{report}\n\n---\n\n"
        f"## Error details\n\n```\n{event.details[:MAX_DETAILS_IN_BODY]}\n```\n\n"
        f"{provenance(event)}"
    )


def pr_body(
    repo_config: RepoConfig,
    event: ErrorEvent,
    report: str,
    verification: CommandResult | None = None,
) -> str:
    """Fix mode's artifact: the analysis, the test verdict, and provenance."""
    return (
        f"{report}\n\n---\n"
        "This PR contains the applied fix and the incident report.\n\n"
        f"{verification_section(repo_config, verification)}"
        f"{provenance(event)}"
    )


def verification_section(
    repo_config: RepoConfig, verification: CommandResult | None
) -> str:
    """A verdict on the fix, so the diff isn't reviewed on trust alone."""
    if verification is None:
        return (
            "> ⚠️ **Unverified** — no `test_command` is configured for this "
            "repo, so the fix was not tested.\n\n"
        )
    if verification.passed:
        headline = f"✅ **Tests pass** — `{repo_config.test_command}`"
    elif verification.exit_code is None:
        headline = f"⚠️ **Could not run** `{repo_config.test_command}`"
    else:
        headline = (
            f"❌ **Tests fail** (exit {verification.exit_code}) — "
            f"`{repo_config.test_command}`"
        )
    output = truncate(verification.output, MAX_TEST_OUTPUT, "\n… (truncated)")
    return (
        f"{headline}\n\n"
        f"<details><summary>Output</summary>\n\n"
        f"```\n{output or '(no output)'}\n```\n\n</details>\n\n"
    )


def report_markdown(event: ErrorEvent, report: str) -> str:
    """The standalone report file, committed in fix mode or written locally."""
    return (
        f"{report}\n\n---\n\n"
        f"## Error details\n\n```\n{event.details}\n```\n\n"
    ) + (
        f"- Repo: `{event.repo}`\n" if event.repo else ""
    ) + (
        f"- Source: `{event.source}`\n"
        f"- First seen: {event.timestamp}\n"
        f"- Fingerprint: `{event.fingerprint}`\n"
    )


def write_report_file(directory: Path, event: ErrorEvent, report: str) -> Path:
    """Write the report under `directory`, returning the path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{event.fingerprint}.md"
    path.write_text(report_markdown(event, report))
    return path


def print_dry_run(
    header: str,
    repo: str,
    report: str,
    usage: tuple[int, int, float],
    extra: tuple[str, ...] = (),
) -> None:
    prompt_tokens, completion_tokens, cost = usage
    bar = "=" * 60
    print(f"\n{bar}")
    print(f"DRY RUN — {header}")
    for line in extra:
        print(line)
    print(f"Repo: {repo}")
    print(f"{bar}\n")
    print(report)
    print(f"\n{bar}")
    print(
        f"Cost: {prompt_tokens} prompt + {completion_tokens} "
        f"completion tokens = ${cost:.4f}"
    )
    print(f"{bar}\n")
