from __future__ import annotations

import re
from pathlib import Path

from rich.console import Console

from maajun.config import RepoConfig
from maajun.monitors import ErrorEvent
from maajun.render import render
from maajun.utils import truncate, truncate_tail
from maajun.vcs import CommandResult

PROJECT_URL = "https://github.com/Morvin-Ian/maajun"

# Quoted verbatim, so cap them or GitHub rejects the body.
MAX_DETAILS_IN_BODY = 4000
MAX_TEST_OUTPUT = 3000

MAX_TITLE_CHARS = 80
MAX_COMMIT_SUBJECT_CHARS = 60

# The sections that make a report actionable. Not every one is required: a
# model that renames a heading should not cost a filed incident. Both
# spellings of the fix are here because the mode decides which is asked for.
REPORT_HEADINGS = ("what happened", "root cause", "suggested fix", "applied fix")

# Every heading the format asks for. A title is the report's summary, so a
# match here means the summary is missing and a section was read instead.
SECTION_HEADINGS = REPORT_HEADINGS + (
    "how to reproduce", "blast radius", "likely cause commit", "follow-up",
    "follow up", "error details", "verdict",
)

# A follow-up section saying the change is complete. An issue for one of
# these is worse than no issue.
NOTHING_TO_FOLLOW_UP = ("none", "n/a", "nothing", "no follow")

# Shorter than this is "None" with extra words, not a piece of work.
MIN_FOLLOW_UP_CHARS = 30

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


# A fenced block's contents; the tag line (```diff) is matched but dropped.
PATCH_FENCE_RE = re.compile(r"```[^\n`]*\n(.*?)```", re.DOTALL)

DIFF_SOURCE_RE = re.compile(r"^--- (?:a/|b/|/dev/null)", re.MULTILINE)
DIFF_DEST_RE = re.compile(r"^\+\+\+ (?:a/|b/|/dev/null)", re.MULTILINE)
DIFF_HUNK_RE = re.compile(r"^@@ -\d+", re.MULTILINE)


def looks_like_patch(block: str) -> bool:
    """Whether a fenced block parses as a unified diff, loosely.

    Stricter than git apply on purpose: prose or ordinary code that happens to
    start with "-" must never reach the working tree.
    """
    if block.startswith("diff --git"):
        return True
    return bool(
        DIFF_SOURCE_RE.search(block)
        and DIFF_DEST_RE.search(block)
        and DIFF_HUNK_RE.search(block)
    )


def extract_patches(report: str) -> list[str]:
    """The unified diffs in the report, in order, ready for `git apply`.

    A model that only describes the change usually leaves the exact patch
    behind anyway. Reading it out costs nothing.
    """
    patches = []
    for match in PATCH_FENCE_RE.finditer(report or ""):
        block = match.group(1).strip("\n")
        # `git apply` wants the final newline; a trimmed fence drops it.
        if block and looks_like_patch(block):
            patches.append(block + "\n")
    return patches


FOLLOW_UP_RE = re.compile(
    r"^\s{0,3}#{1,3}\s+follow[\s-]?up\s*#*\s*$", re.IGNORECASE | re.MULTILINE
)


def split_follow_up(report: str) -> tuple[str, str]:
    """The report without its "Follow-up" section, and that section's body.

    The pull request carries the change; what it left undone becomes an issue.
    In one body a reviewer cannot tell which lines the diff already covers,
    which is how a fix comes to read as a list of suggestions.
    """
    match = FOLLOW_UP_RE.search(report or "")
    if not match:
        return report, ""
    rest = report[match.end():]
    # The next heading closes the section, whatever its level.
    following = HEADING_RE.search(rest)
    if following:
        rest = rest[: following.start()]
    return report[: match.start()].rstrip() + "\n", rest.strip()


def worth_following_up(text: str) -> bool:
    """Whether a follow-up section names work rather than saying "None"."""
    stripped = text.strip().strip("<>").strip()
    if len(stripped) < MIN_FOLLOW_UP_CHARS:
        return False
    return not stripped.lower().startswith(NOTHING_TO_FOLLOW_UP)


def follow_up_title(report: str, fallback: str) -> str:
    return (
        "[maajun] Follow-up: "
        f"{truncate(headline(report) or fallback, MAX_TITLE_CHARS)}"
    )


def follow_up_body(event: ErrorEvent, follow_up: str, pr_url: str) -> str:
    """The companion issue: what the fix left for later, and where the fix is.

    A link is enough — GitHub cross-references the mention, so the pull
    request shows the issue too.
    """
    return (
        f"The fix for this is in {pr_url}.\n\n"
        "These are the parts it deliberately left alone, filed here so the "
        "pull request stays reviewable and none of it is lost:\n\n"
        f"{follow_up}\n\n---\n\n"
        f"{provenance(event)}"
    )


# A report shorter than this has never been a real finding — it is a refusal,
# an apology, or a one-line guess.
MIN_REPORT_CHARS = 200


def report_problem(report: str) -> str:
    """Why a report is not worth filing, or "" when it is.

    An issue or pull request with no findings costs the reader more than it
    gives, and hides that the run failed — so this gates publishing.
    """
    if not report:
        return "it was empty"
    if len(report) < MIN_REPORT_CHARS:
        return f"it was {len(report)} characters long"
    lowered = report.lower()
    found = [h for h in REPORT_HEADINGS if h in lowered]
    if len(found) < 2:
        return "it has none of the report's sections"
    return ""


def headline_problem(report: str) -> str:
    """Why a report cannot be titled, or "" when it can.

    Soft, unlike report_problem: it earns one re-ask but never fails an
    incident. A good analysis is worth more than a missing heading, and the
    fallback title is the raw error, which is where this started.
    """
    if not headline(report):
        return "it has no one-line summary to title the issue with"
    return ""


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


def issue_body(
    event: ErrorEvent,
    report: str,
    previous_url: str = "",
    *,
    unfixed: bool = False,
) -> str:
    """The analysis plus the raw error.

    Suggest mode's only artifact, and fix mode's when the investigation found
    nothing in the repository to change — `unfixed` says which, so a reader
    knows the fix was attempted rather than never asked for.
    """
    note = (
        "> **No code change.** Fix mode investigated this and found nothing "
        "in the repository to change; the analysis is below.\n\n"
        if unfixed else ""
    )
    return (
        regression_note(previous_url)
        + note
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
    previous_url: str = "",
    unrelated_failure: bool = False,
) -> str:
    """Fix mode's artifact: the analysis, the test verdict, and provenance.

    Only ever built for a run that changed code. One that changed none files
    an issue instead — a pull request with no diff looks like a fix until the
    Files tab says otherwise.
    """
    return (
        regression_note(previous_url)
        + f"{report}\n\n---\n"
        "This PR contains the applied fix and the incident report.\n\n"
        f"{verification_section(repo_config, verification, unrelated_failure)}"
        f"{provenance(event)}"
    )


def verification_section(
    repo_config: RepoConfig,
    verification: CommandResult | None,
    unrelated_failure: bool = False,
) -> str:
    """A verdict on the fix, so the diff isn't reviewed on trust alone.

    `unrelated_failure` says the suite failed without naming anything this
    change touched, so no repair was attempted. A reviewer who is not told
    reads a pre-existing red suite as a fact about the fix.
    """
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
        if unrelated_failure:
            verdict += (
                "\n\n> The failure names none of the files this change "
                "touches, so the suite was most likely already red. No repair "
                "was attempted; the output is below."
            )
    # The tail: what failed is printed last, which is what the details block
    # was opened for.
    output = truncate_tail(
        verification.output, MAX_TEST_OUTPUT, "… (earlier output truncated)\n"
    )
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
    # The report is markdown. Rendered, because this is the copy a person
    # reads to decide whether to file it — the file on disk and the issue
    # body keep the source.
    render(Console(), report)
    print(f"\n{bar}")
    print(
        f"Cost: {prompt_tokens} prompt + {completion_tokens} "
        f"completion tokens = ${cost:.4f}"
    )
    print(f"{bar}\n")
