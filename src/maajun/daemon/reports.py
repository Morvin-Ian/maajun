from __future__ import annotations

import re
from pathlib import Path

from maajun.config import RepoConfig
from maajun.monitors import ErrorEvent
from maajun.utils import truncate
from maajun.vcs import CommandResult

PROJECT_URL = "https://github.com/Morvin-Ian/maajun"

# Quoted verbatim, so cap them or GitHub rejects the body.
MAX_DETAILS_IN_BODY = 4000
MAX_TEST_OUTPUT = 3000

MAX_TITLE_CHARS = 80
MAX_COMMIT_SUBJECT_CHARS = 60

# The sections that make a report actionable. Not every one is required: a
# model that renames a heading should not cost a filed incident.
REPORT_HEADINGS = ("what happened", "root cause", "suggested fix")

# Every heading the format asks for. A title is the report's summary, so a
# match here means the summary is missing and a section was read instead.
SECTION_HEADINGS = REPORT_HEADINGS + (
    "how to reproduce", "blast radius", "likely cause commit", "applied fix",
    "error details", "verdict",
)

HEADING_RE = re.compile(r"^\s{0,3}#{1,3}\s+(.+?)\s*#*\s*$", re.MULTILINE)

# A heading that is the template echoed back rather than filled in.
PLACEHOLDER_RE = re.compile(r"^<.*>$")


def headline(report: str) -> str:
    """The report's own one-line summary of the defect, or "".

    This is what an issue or pull request is titled with. The alternative —
    the raw log line that triggered the run — names the symptom, and the
    symptom is regularly in a different place from the defect the report
    goes on to describe. Titling with the finding keeps the two in step.
    """
    match = HEADING_RE.search(report or "")
    if not match:
        return ""
    text = strip_markdown(match.group(1))
    # An unfilled template line, or a report that opens straight into its
    # sections — titling with either is worse than falling back.
    if PLACEHOLDER_RE.match(text) or text.lower().strip(":") in SECTION_HEADINGS:
        return ""
    return text


def strip_markdown(text: str) -> str:
    text = re.sub(r"`+", "", text)
    text = re.sub(r"\*\*|__", "", text)
    return text.strip()


def artifact_title(report: str, fallback: str) -> str:
    """The issue or pull request title: the report's finding, or `fallback`."""
    return f"[maajun] {truncate(headline(report) or fallback, MAX_TITLE_CHARS)}"


def commit_subject(report: str, fallback: str, prefix: str) -> str:
    """The commit subject, naming the same defect as the title."""
    summary = truncate(headline(report) or fallback, MAX_COMMIT_SUBJECT_CHARS)
    return f"{prefix} {summary}"


# What the report concluded the error is. "by design" means the code did what
# it is built to do and there is nothing to fix.
BY_DESIGN = "by design"
DEFECT = "defect"

# The line under the heading, but never the next heading: a verdict section
# left empty must read as absent rather than as whatever follows it.
VERDICT_RE = re.compile(
    r"^\s{0,3}#{1,3}\s*verdict\s*:?\s*$\n+(?!\s{0,3}#)(.+?)$",
    re.MULTILINE | re.IGNORECASE,
)


def verdict(report: str) -> str:
    """BY_DESIGN, DEFECT, or "" when the report did not say.

    The signatures in triage.py can only recognise an error named after its
    own intent. This is the pass that catches a guard particular to the
    application — a paywall, a quota, a feature flag — because the agent has
    read the code that raised it. An absent or unreadable verdict is not
    by-design: silence must not suppress a report.
    """
    match = VERDICT_RE.search(report or "")
    if not match:
        return ""
    line = strip_markdown(match.group(1)).lower()
    if line.startswith(BY_DESIGN) or line.startswith("by-design"):
        return BY_DESIGN
    if line.startswith(DEFECT):
        return DEFECT
    return ""


def by_design_reason(report: str) -> str:
    """The report's one-line justification, for the incident record."""
    match = VERDICT_RE.search(report or "")
    if not match:
        return BY_DESIGN
    line = strip_markdown(match.group(1))
    return truncate(line, MAX_TITLE_CHARS) or BY_DESIGN


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


def regression_note(previous_url: str) -> str:
    """A line saying this was reported before, or "" when it is new.

    Filed at the top: whether a bug is back is the first thing a reader
    needs, and it changes what they look at in the diff.
    """
    if not previous_url:
        return ""
    return (
        "> ⚠️ **This was reported before and has come back.** "
        f"The earlier report: {previous_url}\n\n"
    )


def issue_body(event: ErrorEvent, report: str, previous_url: str = "") -> str:
    """Suggest mode's artifact: the analysis plus the raw error."""
    return (
        regression_note(previous_url)
        + f"{report}\n\n---\n\n"
        f"## Error details\n\n```\n{event.details[:MAX_DETAILS_IN_BODY]}\n```\n\n"
        f"{provenance(event)}"
    )


def pr_body(
    repo_config: RepoConfig,
    event: ErrorEvent,
    report: str,
    verification: CommandResult | None = None,
    *,
    code_changed: bool = True,
    previous_url: str = "",
) -> str:
    """Fix mode's artifact: the analysis, the test verdict, and provenance."""
    if code_changed:
        summary = "This PR contains the applied fix and the incident report."
        verdict = verification_section(repo_config, verification)
    else:
        # Still a pull request, so the finding is reviewed in one place —
        # but the reader has to know the diff is the report, not a fix.
        summary = (
            "⚠️ **Analysis only — no code change.** The investigation is "
            "below and in the report file; the fix still has to be written.\n"
            "The suggested fix section says what to change."
        )
        verdict = ""
    return (
        regression_note(previous_url)
        + f"{report}\n\n---\n"
        f"{summary}\n\n"
        f"{verdict}"
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
        verdict = f"✅ **Tests pass** — `{repo_config.test_command}`"
    elif verification.exit_code is None:
        verdict = f"⚠️ **Could not run** `{repo_config.test_command}`"
    else:
        verdict = (
            f"❌ **Tests fail** (exit {verification.exit_code}) — "
            f"`{repo_config.test_command}`"
        )
    output = truncate(verification.output, MAX_TEST_OUTPUT, "\n… (truncated)")
    return (
        f"{verdict}\n\n"
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
    title: str = "",
) -> None:
    prompt_tokens, completion_tokens, cost = usage
    bar = "=" * 60
    print(f"\n{bar}")
    print(f"DRY RUN — {header}")
    for line in extra:
        print(line)
    print(f"Repo: {repo}")
    # The title is derived from the report below, so a dry run is where a
    # mismatch between the two is caught before anything is filed.
    if title:
        print(f"Would be titled: {title}")
    print(f"{bar}\n")
    print(report)
    print(f"\n{bar}")
    print(
        f"Cost: {prompt_tokens} prompt + {completion_tokens} "
        f"completion tokens = ${cost:.4f}"
    )
    print(f"{bar}\n")
