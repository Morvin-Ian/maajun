from __future__ import annotations

import configparser
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

PYTHON_CHECKS = {
    "ruff": "ruff check .",
    "black": "black --check .",
    "isort": "isort --check-only .",
    "flake8": "flake8",
}

# (check, write) for the tools that rewrite code rather than just report on it.
PYTHON_FORMATTERS = {
    "ruff": ("ruff format --check .", "ruff format ."),
    "black": ("black --check .", "black ."),
    "isort": ("isort --check-only .", "isort ."),
}

PRETTIER_CONFIGS = (
    ".prettierrc",
    ".prettierrc.json",
    ".prettierrc.yml",
    ".prettierrc.yaml",
    ".prettierrc.toml",
    ".prettierrc.js",
    ".prettierrc.cjs",
    "prettier.config.js",
    "prettier.config.cjs",
    "prettier.config.mjs",
)

# A lockfile says which runner owns the project's dev tools.
PYTHON_RUNNERS = (("uv.lock", "uv run "), ("poetry.lock", "poetry run "))

NODE_MANAGERS = (
    ("pnpm-lock.yaml", "pnpm"),
    ("yarn.lock", "yarn"),
    ("bun.lockb", "bun"),
    ("bun.lock", "bun"),
)

# package.json scripts that check rather than fix, best first.
NODE_SCRIPTS = ("lint", "lint:check", "format:check", "fmt:check", "check")

# gofmt exits 0 even when it lists unformatted files.
GOFMT_CHECK = 'test -z "$(gofmt -l .)"'

REQUIREMENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


@dataclass(frozen=True)
class Check:
    """A command the project already defines, and the file that proves it."""

    command: str
    source: str


@dataclass(frozen=True)
class Formatter:
    """A formatter the project declares, in its report and rewrite forms."""

    check: str
    write: str
    source: str


def read_toml(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, ValueError):
        return {}


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def has_ini_section(path: Path, section: str) -> bool:
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, UnicodeDecodeError, configparser.Error):
        return False
    return parser.has_section(section)


def table(data: dict, *keys: str) -> dict:
    """Nested table lookup that tolerates a manifest of the wrong shape."""
    for key in keys:
        if not isinstance(data, dict):
            return {}
        data = data.get(key, {})
    return data if isinstance(data, dict) else {}


def requirement_names(entries) -> set[str]:
    """Distribution names pulled out of PEP 508 requirement strings."""
    names = set()
    if isinstance(entries, dict):
        entries = list(entries)
    if not isinstance(entries, list):
        return names
    for entry in entries:
        if not isinstance(entry, str):
            continue
        match = REQUIREMENT_NAME.match(entry.strip())
        if match:
            names.add(match.group().lower())
    return names


def python_dependencies(data: dict) -> set[str]:
    """Every declared dependency, whichever layout the project uses."""
    names = requirement_names(table(data, "project").get("dependencies"))
    for group in (
        table(data, "project", "optional-dependencies"),
        table(data, "dependency-groups"),
        table(data, "tool", "poetry", "dev-dependencies"),
    ):
        for entries in group.values():
            names |= requirement_names(entries)
    for group in table(data, "tool", "poetry", "group").values():
        names |= requirement_names(table(group, "dependencies"))
    return names


def python_runner(root: Path) -> str:
    for lockfile, prefix in PYTHON_RUNNERS:
        if (root / lockfile).exists():
            return prefix
    return ""


def python_tool_sources(root: Path) -> dict[str, str]:
    """Each Python lint/format tool the project declares, and where it says so."""
    data = read_toml(root / "pyproject.toml")
    tools = table(data, "tool")
    dependencies = python_dependencies(data)

    sources: dict[str, str] = {}
    for name in PYTHON_CHECKS:
        if name in tools:
            sources[name] = f"pyproject.toml [tool.{name}]"
        elif name in dependencies:
            sources[name] = "pyproject.toml dependencies"
    for filename in ("ruff.toml", ".ruff.toml"):
        if "ruff" not in sources and (root / filename).exists():
            sources["ruff"] = filename
    for filename in (".flake8", "setup.cfg", "tox.ini"):
        if "flake8" not in sources and has_ini_section(root / filename, "flake8"):
            sources["flake8"] = f"{filename} [flake8]"
    return sources


def python_checks(root: Path) -> list[Check]:
    sources = python_tool_sources(root)
    prefix = python_runner(root)
    checks = [
        Check(prefix + command, sources[name])
        for name, command in PYTHON_CHECKS.items()
        if name in sources
    ]
    if "format" in table(read_toml(root / "pyproject.toml"), "tool", "ruff"):
        checks.append(
            Check(f"{prefix}ruff format --check .", "pyproject.toml [tool.ruff.format]")
        )
    return checks


def python_formatters(root: Path) -> list[Formatter]:
    sources = python_tool_sources(root)
    prefix = python_runner(root)
    return [
        Formatter(prefix + check, prefix + write, sources[name])
        for name, (check, write) in PYTHON_FORMATTERS.items()
        if name in sources
    ]


def node_formatters(root: Path) -> list[Formatter]:
    package = read_json(root / "package.json")
    dev = package.get("devDependencies")
    dev = dev if isinstance(dev, dict) else {}
    source = ""
    if "prettier" in package:
        source = "package.json prettier"
    elif "prettier" in dev:
        source = "package.json devDependencies.prettier"
    else:
        for filename in PRETTIER_CONFIGS:
            if (root / filename).exists():
                source = filename
                break
    if not source:
        return []
    return [Formatter("npx prettier --check .", "npx prettier --write .", source)]


def go_formatters(root: Path) -> list[Formatter]:
    if not (root / "go.mod").exists():
        return []
    return [Formatter(GOFMT_CHECK, "gofmt -w .", "go.mod")]


def rust_formatters(root: Path) -> list[Formatter]:
    if not (root / "Cargo.toml").exists():
        return []
    return [Formatter("cargo fmt --check", "cargo fmt", "Cargo.toml")]


def node_manager(root: Path) -> str:
    for lockfile, manager in NODE_MANAGERS:
        if (root / lockfile).exists():
            return manager
    return "npm"


def node_checks(root: Path) -> list[Check]:
    data = read_json(root / "package.json")
    if not data:
        return []
    scripts = data.get("scripts")
    scripts = scripts if isinstance(scripts, dict) else {}
    manager = node_manager(root)
    for name in NODE_SCRIPTS:
        if name in scripts:
            return [Check(f"{manager} run {name}", f"package.json scripts.{name}")]

    dev = data.get("devDependencies")
    dev = dev if isinstance(dev, dict) else {}
    checks = []
    if "eslint" in dev:
        checks.append(Check("npx eslint .", "package.json devDependencies.eslint"))
    if "prettier" in dev:
        checks.append(
            Check("npx prettier --check .", "package.json devDependencies.prettier")
        )
    return checks


def go_checks(root: Path) -> list[Check]:
    if not (root / "go.mod").exists():
        return []
    return [Check(GOFMT_CHECK, "go.mod")]


def rust_checks(root: Path) -> list[Check]:
    if not (root / "Cargo.toml").exists():
        return []
    return [Check("cargo fmt --check", "Cargo.toml")]


def detect_checks(root: Path | None) -> list[Check]:
    """Check commands implied by the manifests in `root`, best first."""
    if root is None or not root.is_dir():
        return []
    found: dict[str, Check] = {}
    for detector in (python_checks, node_checks, go_checks, rust_checks):
        try:
            checks = detector(root)
        except OSError:
            continue
        for check in checks:
            found.setdefault(check.command, check)
    return list(found.values())


def detect_formatters(root: Path | None) -> list[Formatter]:
    """Formatters the manifests in `root` declare, best first.

    Commands are repo-wide: the caller runs one only after proving the
    untouched checkout satisfies it, so a rewrite reaches nothing else.
    """
    if root is None or not root.is_dir():
        return []
    found: dict[str, Formatter] = {}
    for detector in (
        python_formatters, node_formatters, go_formatters, rust_formatters
    ):
        try:
            formatters = detector(root)
        except OSError:
            continue
        for formatter in formatters:
            found.setdefault(formatter.write, formatter)
    return list(found.values())
