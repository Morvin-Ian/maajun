from __future__ import annotations

import re
from dataclasses import dataclass

MAX_FOLLOW_UP_ISSUES = 3

TASK_HEADING_RE = re.compile(r"^\s{0,3}###\s+(.+?)\s*#*\s*$", re.MULTILINE)
FIELD_RE = re.compile(
    r"^\s*[-*]\s+(Evidence|Change|Acceptance(?: criteria)?):\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
FOLLOW_UP_HEADING_RE = re.compile(
    r"^\s{0,3}#{1,2}\s+follow[\s-]?up\s*#*\s*$", re.IGNORECASE | re.MULTILINE
)
CODE_REFERENCE_RE = re.compile(r"`[^`\n]+`")

EMPTY_ANSWERS = ("none", "n/a", "nothing", "no follow")
ACTION_VERBS = {
    "add", "adopt", "avoid", "change", "configure", "consolidate", "cover",
    "document", "enforce", "extract", "fix", "guard", "handle", "implement",
    "migrate", "move", "prevent", "refactor", "remove", "rename", "replace",
    "require", "return", "support", "test", "update", "validate",
}
OBSERVABLE_WORDS = (
    "accept", "create", "fail", "log", "no longer", "pass", "raise", "reject",
    "remain", "return", "show", "test",
)
NOISE_PHRASES = (
    "already in this pr", "already included", "could not verify",
    "environment commentary", "environment issue", "future cleanup",
    "generic cleanup", "implemented in this pr", "missing evidence",
    "missing traceback", "more investigation", "traceback missing",
    "unrelated test", "unrelated verification",
)


@dataclass(frozen=True)
class FollowUpTask:
    title: str
    evidence: str
    change: str
    acceptance: str


@dataclass(frozen=True)
class InvalidFollowUp:
    text: str
    problems: tuple[str, ...]


@dataclass(frozen=True)
class ParsedFollowUps:
    tasks: tuple[FollowUpTask, ...] = ()
    invalid: tuple[InvalidFollowUp, ...] = ()


def strip_section_heading(text: str) -> str:
    return FOLLOW_UP_HEADING_RE.sub("", text or "", count=1).strip()


def is_empty_answer(text: str) -> bool:
    stripped = strip_section_heading(text).strip().strip("<>").lower()
    return not stripped or stripped.startswith(EMPTY_ANSWERS)


def task_blocks(text: str) -> list[tuple[str, str]]:
    body = strip_section_heading(text)
    matches = list(TASK_HEADING_RE.finditer(body))
    if not matches:
        return []
    return [
        (
            match.group(1).strip(),
            body[match.end() : matches[index + 1].start() if index + 1 < len(matches) else None],
        )
        for index, match in enumerate(matches)
    ]


def validate_task(title: str, body: str) -> FollowUpTask | InvalidFollowUp:
    fields: dict[str, str] = {}
    duplicates: set[str] = set()
    for match in FIELD_RE.finditer(body):
        name = match.group(1).lower().replace(" criteria", "")
        if name in fields:
            duplicates.add(name)
        fields[name] = match.group(2).strip()

    problems = []
    first_word = re.sub(r"[^a-z]", "", title.lower().split(maxsplit=1)[0]) if title else ""
    if len(title.split()) < 3 or first_word not in ACTION_VERBS:
        problems.append(
            "title must start with a concrete action verb and contain at least three words"
        )
    for name in ("evidence", "change", "acceptance"):
        if name not in fields:
            problems.append(f"missing {name} field")
    if duplicates:
        problems.append(f"duplicate fields: {', '.join(sorted(duplicates))}")
    if FIELD_RE.sub("", body).strip():
        problems.append("contains text outside the evidence, change, and acceptance fields")

    evidence = fields.get("evidence", "")
    change = fields.get("change", "")
    acceptance = fields.get("acceptance", "")
    if evidence and (len(evidence) < 20 or not CODE_REFERENCE_RE.search(evidence)):
        problems.append("evidence must explain a concrete backticked code location or symbol")
    if change and len(change) < 20:
        problems.append("change must be concrete enough to implement")
    if acceptance and (
        len(acceptance) < 15
        or not any(word in acceptance.lower() for word in OBSERVABLE_WORDS)
    ):
        problems.append("acceptance must name an observable result or test")
    combined = f"{title} {evidence} {change} {acceptance}".lower()
    noisy = [phrase for phrase in NOISE_PHRASES if phrase in combined]
    if noisy:
        problems.append(f"contains non-actionable context: {', '.join(noisy)}")

    if problems:
        return InvalidFollowUp(
            text=f"### {title}\n{body.strip()}", problems=tuple(problems)
        )
    return FollowUpTask(title, evidence, change, acceptance)


def parse_follow_ups(text: str) -> ParsedFollowUps:
    """Parse independent structured tasks, retaining valid and invalid items."""
    if is_empty_answer(text):
        return ParsedFollowUps()
    blocks = task_blocks(text)
    if not blocks:
        return ParsedFollowUps(invalid=(InvalidFollowUp(
            strip_section_heading(text),
            ("use one '### action' block per task",),
        ),))

    tasks = []
    invalid = []
    body = strip_section_heading(text)
    first_heading = TASK_HEADING_RE.search(body)
    if first_heading and body[:first_heading.start()].strip():
        invalid.append(InvalidFollowUp(
            body[:first_heading.start()].strip(),
            ("free-form text must be expressed as its own structured task",),
        ))
    seen: set[tuple[str, str]] = set()
    for title, body in blocks:
        result = validate_task(title, body)
        if isinstance(result, InvalidFollowUp):
            invalid.append(result)
            continue
        key = (result.title.casefold(), result.evidence.casefold())
        if key in seen:
            continue
        seen.add(key)
        tasks.append(result)
    return ParsedFollowUps(tuple(tasks), tuple(invalid))
