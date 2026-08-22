from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from maajun.agent.core import Agent
from maajun.agent.tools import Sandbox, default_registry
from maajun.config import AIProviderConfig, Config
from maajun.providers.pricing import extract_usage

log = logging.getLogger(__name__)

# Findings are quoted back to the user, so one runaway string does not get to
# fill the terminal.
MAX_FINDING_CHARS = 300

# Files that name the stack, how it starts, and where it logs. Read locally
# and put in the prompt, so the model does not spend rounds finding them.
MANIFESTS = (
    "requirements.txt", "pyproject.toml", "Pipfile", "setup.py",
    "package.json", "go.mod", "Gemfile", "composer.json", "Cargo.toml",
)

RUNNERS = (
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    "compose.yml", "compose.yaml", "Procfile", "gunicorn.conf.py",
    "uwsgi.ini", "supervisord.conf",
)

LOGGING_GLOBS = (
    "*settings*.py", "*/settings*.py", "*/*/settings*.py",
    "*config*.py", "*/config*.py",
    "logging*.*", "*/logging*.*",
    "log*.json", "*/log*.js", "*/log*.ts",
)

# Marks a file as worth showing even if its name says nothing.
LOGGING_MARKERS = (
    "FileHandler", "logging.config", "dictConfig", "basicConfig",
    "createLogger", "winston", "pino", "monolog", "log.New(", "slog.",
)

SEARCHABLE = (".py", ".js", ".ts", ".mjs", ".rb", ".php", ".go", ".java", ".yaml", ".yml")

SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build",
    ".mypy_cache", ".ruff_cache", ".pytest_cache", "site-packages", "vendor",
}

MAX_SURVEY_CHARS = 14_000
MAX_FILE_CHARS = 3_000
MAX_SEARCHED_FILES = 300

# Enough rounds to fill a gap in the survey, not enough to browse the repo.
INSPECT_ROUNDS = 6


def interesting(root: Path, sandbox: Sandbox) -> list[Path]:
    """Files worth putting in front of the model, best first.

    Filtered by the same sandbox the agent's own tools use: this material
    goes to the provider, so a file that read_file would refuse — a .env, a
    private key — must not arrive by the back door instead.
    """
    found: list[Path] = []

    def add(path: Path) -> None:
        if path.is_file() and sandbox.readable(path) and path not in found:
            found.append(path)

    for name in MANIFESTS + RUNNERS:
        add(root / name)
    for pattern in LOGGING_GLOBS:
        for path in sorted(root.glob(pattern))[:4]:
            if not any(part in SKIP_DIRS for part in path.parts):
                add(path)

    searched = 0
    for path in sorted(root.rglob("*")):
        if searched >= MAX_SEARCHED_FILES:
            break
        if path.suffix not in SEARCHABLE or not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if not sandbox.readable(path):
            continue
        searched += 1
        try:
            head = path.read_text(errors="replace")[:MAX_FILE_CHARS]
        except OSError:
            continue
        if any(marker in head for marker in LOGGING_MARKERS):
            add(path)
    return found


def survey(root: Path, sandbox: Sandbox | None = None) -> str:
    """The repo's own answer to "what is this and where does it log?".

    Gathered locally, because every round the model spends looking for
    settings.py is a round it is not answering in.
    """
    sandbox = sandbox or Sandbox([root])
    parts = []
    listing = sorted(
        entry.name + ("/" if entry.is_dir() else "")
        for entry in root.iterdir()
        if entry.name not in SKIP_DIRS
    )
    parts.append("Top level: " + ", ".join(listing[:60]))

    total = len(parts[0])
    for path in interesting(root, sandbox):
        try:
            body = path.read_text(errors="replace")[:MAX_FILE_CHARS]
        except OSError:
            continue
        block = f"\n=== {path.relative_to(root)} ===\n{body}"
        if total + len(block) > MAX_SURVEY_CHARS:
            parts.append("\n[more files not shown — read them if you need them]")
            break
        parts.append(block)
        total += len(block)
    return "\n".join(parts)


INSPECT_PROMPT = """\
Error monitoring is being set up for the application at {path}. Work out how
its failures surface. The files that usually answer this are already below —
read them, and only use read_file/grep/glob if something essential is
missing. Be quick: one or two tool calls at most.

{material}

Answer these, from what you actually read:

- the stack and how it is served (gunicorn, uvicorn, node, php-fpm...)
- the entrypoint
- every file the logging config writes errors to — the exact path
- whether an unhandled request error reaches those files at all. A bare
  `except: pass`, a 500 handler that logs nothing, a handler pointed at a
  directory nothing creates, DEBUG-only logging: all mean errors are lost
- the port, if the code or its config says
- the log format: plain text, or JSON (and the key holding the level)

Answer with ONLY a JSON object, no prose and no code fence:

{{
  "stack": "e.g. Django 5 + gunicorn",
  "entrypoint": "path or command",
  "port": 8000,
  "log_files": ["exact paths the app writes errors to"],
  "log_format": "text" or "json",
  "json_level_field": "the key holding the level, or empty for text logs",
  "error_pattern": "regex matching this app's error lines, or empty",
  "logging_gaps": ["at most 3, one line each: where errors are lost"],
  "logging_advice": "the one config change that makes them land in a file",
  "risky_areas": ["at most 3, one line each: file:line — why"],
  "confidence": "high" or "medium" or "low"
}}

Use [] for anything you could not determine. Paths must be ones you saw in
the code: a wrong path becomes a file maajun watches forever and nothing
ever writes to.
"""

JSON_ONLY_RETRY = """\
That was not a JSON object I can read. Answer again with only the JSON
described above — no prose, no code fence, no trailing commas.
"""


@dataclass
class Inspection:
    """What the AI concluded about how this codebase fails."""

    stack: str = ""
    entrypoint: str = ""
    port: int = 0
    log_files: list[str] = field(default_factory=list)
    log_format: str = ""
    json_level_field: str = ""
    error_pattern: str = ""
    logging_gaps: list[str] = field(default_factory=list)
    logging_advice: str = ""
    risky_areas: list[str] = field(default_factory=list)
    confidence: str = ""
    cost_usd: float = 0.0

    def has_findings(self) -> bool:
        return bool(self.stack or self.log_files or self.logging_advice)


def strings(value: object) -> list[str]:
    """A JSON field as a list of non-empty strings, whatever shape it came in."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [
        str(item).strip()[:MAX_FINDING_CHARS]
        for item in value
        if str(item).strip()
    ]


def whole_number(value: object) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if 1 <= number <= 65535 else 0


def parse_json(text: str) -> dict:
    """The JSON object in a model's answer, or {} if there is not one.

    Models fence JSON, or add a sentence before it, often enough that
    demanding raw JSON and failing on anything else would waste real runs.
    """
    body = text.strip()
    if body.startswith("```"):
        body = body.split("```")[1] if "```" in body[3:] else body[3:]
        body = body.removeprefix("json").strip()
    start, end = body.find("{"), body.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        parsed = json.loads(body[start:end + 1])
    except json.JSONDecodeError:
        log.debug("inspection answer was not JSON: %s", body[:200])
        return {}
    return parsed if isinstance(parsed, dict) else {}


def absolute(paths: list[str], root: Path) -> list[str]:
    """Repo-relative paths as absolute ones, so they can be watched."""
    resolved = []
    for path in paths:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        text = str(candidate)
        if text not in resolved:
            resolved.append(text)
    return resolved


def build_inspection(data: dict, root: Path, cost: float) -> Inspection:
    return Inspection(
        stack=str(data.get("stack", "")).strip(),
        entrypoint=str(data.get("entrypoint", "")).strip(),
        port=whole_number(data.get("port")),
        log_files=absolute(strings(data.get("log_files")), root),
        log_format=str(data.get("log_format", "")).strip().lower(),
        json_level_field=str(data.get("json_level_field", "")).strip(),
        error_pattern=str(data.get("error_pattern", "")).strip(),
        logging_gaps=strings(data.get("logging_gaps")),
        logging_advice=str(data.get("logging_advice", "")).strip(),
        risky_areas=strings(data.get("risky_areas")),
        confidence=str(data.get("confidence", "")).strip().lower(),
        cost_usd=cost,
    )


def make_agent(ai: AIProviderConfig, root: Path) -> Agent:
    """A read-only agent confined to the app's own directory."""
    return Agent(
        Config(ai=ai),
        tools=default_registry(Sandbox([root])),
        approve=None,
        max_rounds=INSPECT_ROUNDS,
    )


async def inspect_repo(
    path: str | Path, ai: AIProviderConfig, agent: Agent | None = None
) -> Inspection:
    """Read the code at `path` and report how its errors should be caught."""
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"{root} is not a directory")

    agent = agent or make_agent(ai, root)
    try:
        return await run_inspection(agent, root)
    finally:
        await agent.aclose()


async def run_inspection(agent: Agent, root: Path) -> Inspection:
    """Ask, and insist on JSON once if the answer is not readable."""
    response = await agent.chat(
        INSPECT_PROMPT.format(path=root, material=survey(root))
    )

    _, _, cost = extract_usage(response.usage, getattr(response, "model", None))
    data = parse_json(response.content)
    if not data:
        # Truncating before parsing used to cut the object in half and lose a
        # perfectly good answer; a second ask is for a genuinely bad one.
        log.info("inspection answer was not JSON; asking once more")
        retry = await agent.chat(JSON_ONLY_RETRY)
        _, _, retry_cost = extract_usage(
            retry.usage, getattr(retry, "model", None)
        )
        cost += retry_cost
        data = parse_json(retry.content)
    if not data:
        return Inspection(cost_usd=cost)
    return build_inspection(data, root, cost)
