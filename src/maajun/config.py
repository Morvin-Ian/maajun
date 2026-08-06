import os
import tomllib
from pathlib import Path
from typing import get_args, get_origin

import tomlkit
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from maajun.monitors.defaults import (
    DEFAULT_ERROR_PATTERN,
    DEFAULT_JSON_LEVEL_VALUES,
    DEFAULT_TRACEBACK_HEADERS,
)
from maajun.providers.base import ProviderType
from maajun.utils import PLACEHOLDER_REPO, is_valid_repo

_LIST_SEP = ","
LEGACY_GITHUB_SCALARS = ("repo", "base_branch", "mode", "test_command")
_PER_REPO_FIELDS = ("base_branch", "mode", "log_files", "test_command")


class ConfigError(ValueError):
    """A config file that cannot be used as written."""


STARTER_CONFIG = """\
# Maajun daemon configuration.

[ai]
provider = "deepseek"
# thinking_mode = true

# Repositories the daemon documents errors in and opens PRs against. One
# [[github.repos]] entry each, added with `maajun add-repo owner/name`.
# Optional: with no entries, maajun analyzes errors and writes reports under
# daemon.workdir instead of opening pull requests.
# [[github.repos]]
# repo = "owner/name"
# base_branch = "main"
# "suggest": a GitHub issue with the incident report and suggested fix.
# "fix": the agent also edits code in its isolated clone and opens a PR.
# mode = "suggest"
# Log files watched for this repo, on top of the global monitor.log_files.
# log_files = ["/var/log/myapp/error.log"]
# Run after a fix-mode edit to verify it; the result goes in the PR body.
# test_command = "pytest -q"

[monitor]
# Log files to watch for tracebacks and error lines.
log_files = ["/var/log/myapp/error.log"]
error_pattern = "\\\\b(ERROR|CRITICAL|FATAL)\\\\b"
poll_interval = 30

# Detection tuning (optional).
# json_level_field = "level"        # also match structured JSON logs
# json_level_values = "error,critical,fatal"
# burst_threshold = 1               # only report after N errors in the window
# burst_window_seconds = 60

# GitHub Actions — poll repos for failed workflow runs (optional).
# Uses the same GitHub token as everything else; nothing secret goes here.
# github_actions_repos = ["you/another-repo"]

[daemon]
# Where clones, the incident database, and state live.
# workdir = "~/.local/share/maajun"
# Local checkout to analyze when no repositories are configured (default: cwd).
# repo_path = "/srv/myapp"
# Stop analyzing once this much has been spent in a UTC day (0 = no cap).
# max_usd_per_day = 5.0            # default: 5.0
# Most incidents analyzed per poll cycle (0 = unlimited).
# max_incidents_per_cycle = 10
"""


class _Base(BaseModel):
    """Base model that re-validates on assignment so `config.set(...)` and
    direct attribute writes are checked against the field validators."""

    model_config = ConfigDict(validate_assignment=True)


def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", "~/.config")
    return Path(base).expanduser() / "maajun" / "config.toml"


def default_data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME", "~/.local/share")
    return Path(base).expanduser() / "maajun"


class AIProviderConfig(_Base):
    provider: str = ProviderType.DEEPSEEK.value
    model: str | None = None  
    api_key: str | None = None
    base_url: str | None = None
    temperature: float = 0.3
    max_tokens: int = 4096
    thinking_mode: bool = False

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, value: str) -> str:
        valid_providers = [p.value for p in ProviderType]
        if value not in valid_providers:
            raise ValueError(f'Provider must be one of: {", ".join(valid_providers)}')
        return value


class RepoConfig(_Base):
    repo: str = ""  # "owner/name"
    base_branch: str = "main"
    mode: str = "suggest"
    log_files: list[str] = Field(default_factory=list)
    test_command: str = ""

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, value: str) -> str:
        if value not in ("suggest", "fix"):
            raise ValueError('mode must be "suggest" or "fix"')
        return value

    @field_validator("repo")
    @classmethod
    def validate_repo(cls, value: str) -> str:
        if value and not is_valid_repo(value):
            raise ValueError('repo must be in "owner/name" form')
        return value


class GitHubConfig(_Base):
    """GitHub configuration: a list of repositories, each with its own settings.

    A repository is always an entry in `repos`, even when there is only one.
    The older form — `repo`/`base_branch`/`mode` as scalars directly under
    [github] — is not read at all; reject_legacy_github refuses it at load.
    """

    repos: list[RepoConfig] = Field(default_factory=list)

    def get_all_repos(self) -> list[RepoConfig]:
        return self.repos


class MonitorConfig(_Base):
    log_files: list[str] = Field(default_factory=list)
    error_pattern: str = DEFAULT_ERROR_PATTERN
    poll_interval: float = 30.0

    # Log-line detection, applied to every logfile monitor.
    json_level_field: str = ""
    json_level_values: str = ",".join(sorted(DEFAULT_JSON_LEVEL_VALUES))
    traceback_headers: list[str] = Field(
        default_factory=lambda: list(DEFAULT_TRACEBACK_HEADERS)
    )
    # Emit nothing until burst_threshold events land within the window.
    burst_threshold: int = 1
    burst_window_seconds: float = 60.0
    # Repos to poll for failed workflow runs. 
    github_actions_repos: list[str] = Field(default_factory=list)

    @property
    def json_level_value_set(self) -> frozenset[str]:
        """json_level_values parsed into the set the monitors expect.

        Stored as a comma-separated string so `maajun config` can set it.
        """
        return frozenset(
            value.strip().lower()
            for value in self.json_level_values.split(_LIST_SEP)
            if value.strip()
        )

    def logfile_kwargs(self) -> dict:
        return dict(
            error_pattern=self.error_pattern,
            json_level_field=self.json_level_field,
            json_level_values=self.json_level_value_set,
            traceback_headers=tuple(self.traceback_headers),
            burst_threshold=self.burst_threshold,
            burst_window_seconds=self.burst_window_seconds,
        )


class DaemonConfig(_Base):
    workdir: str = str(default_data_dir())
    repo_path: str = ""
    max_usd_per_day: float = 5.0
    max_incidents_per_cycle: int = 10


class Config(_Base):
    ai: AIProviderConfig = Field(default_factory=AIProviderConfig)
    github: GitHubConfig = Field(default_factory=GitHubConfig)
    monitor: MonitorConfig = Field(default_factory=MonitorConfig)
    daemon: DaemonConfig = Field(default_factory=DaemonConfig)

    _path: Path | None = None

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        """Load config from a TOML file; missing file yields defaults."""
        path = Path(path) if path else default_config_path()
        config = cls()
        config._path = path
        if not path.exists():
            return config
        with open(path, "rb") as f:
            data = tomllib.load(f)
        reject_legacy_github(data, path)
        loaded = cls.model_validate(data)
        loaded._path = path
        return loaded

    def save(self, path: str | Path | None = None) -> None:
        """Write the config to a TOML file.

        Uses tomlkit to round-trip an existing file: comments and formatting
        on keys the user has already written are preserved; every known field
        is (re)written so nothing is silently dropped.
        """
        path = Path(path) if path else (self._path or default_config_path())
        path.parent.mkdir(parents=True, exist_ok=True)

        if path.exists():
            doc = tomlkit.parse(path.read_text())
        else:
            doc = tomlkit.document()
            doc.add(tomlkit.comment("Maajun daemon configuration."))

        ai = _table(doc, "ai")
        ai["provider"] = self.ai.provider
        _set_or_del(ai, "model", self.ai.model)
        _set_or_del(ai, "base_url", self.ai.base_url)
        ai["temperature"] = self.ai.temperature
        ai["max_tokens"] = self.ai.max_tokens
        ai["thinking_mode"] = self.ai.thinking_mode

        github = _table(doc, "github")
        for legacy in LEGACY_GITHUB_SCALARS:
            github.pop(legacy, None)
        if self.github.repos:
            repos_table = tomlkit.aot()
            for repo_config in self.github.repos:
                repo_table = tomlkit.table()
                repo_table["repo"] = repo_config.repo
                repo_table["base_branch"] = repo_config.base_branch
                repo_table["mode"] = repo_config.mode
                if repo_config.log_files:
                    repo_table["log_files"] = repo_config.log_files
                if repo_config.test_command:
                    repo_table["test_command"] = repo_config.test_command
                repos_table.append(repo_table)
            # Trailing blank line, or the table that follows in the document
            # ends up butted directly against the last repo entry.
            repos_table[-1].add(tomlkit.nl())
            github["repos"] = repos_table
        else:
            # Local mode: no repos at all, rather than a repo spelled "".
            github.pop("repos", None)

        monitor = _table(doc, "monitor")
        monitor["log_files"] = self.monitor.log_files
        monitor["error_pattern"] = self.monitor.error_pattern
        monitor["poll_interval"] = self.monitor.poll_interval
        # Detection tuning is written only when it differs from the default, so
        # a simple config stays short — but a value the user set is never lost.
        for name in (
            "json_level_field",
            "json_level_values",
            "traceback_headers",
            "burst_threshold",
            "burst_window_seconds",
        ):
            _set_if_customized(monitor, self.monitor, name)
        if self.monitor.github_actions_repos:
            monitor["github_actions_repos"] = self.monitor.github_actions_repos
        else:
            monitor.pop("github_actions_repos", None)

        daemon = _table(doc, "daemon")
        daemon["workdir"] = self.daemon.workdir
        _set_or_del(daemon, "repo_path", self.daemon.repo_path or None)
        _set_if_customized(daemon, self.daemon, "max_usd_per_day")
        _set_if_customized(daemon, self.daemon, "max_incidents_per_cycle")

        path.write_text(tomlkit.dumps(doc))
        self._path = path

    def add_repo(self, repo: "RepoConfig") -> None:
        for index, existing in enumerate(self.github.repos):
            if existing.repo == repo.repo:
                self.github.repos = [
                    *self.github.repos[:index], repo, *self.github.repos[index + 1:]
                ]
                return
        self.github.repos = [*self.github.repos, repo]


    def _resolve(self, key: str) -> tuple[BaseModel, str]:
        """Map a dotted key to (owning model, field name). Raises ValueError
        for unknown sections/fields."""
        parts = key.split(".")
        if len(parts) < 2:
            raise ValueError(
                f"Invalid config key: {key}. Use dot notation (e.g., 'github.mode')."
            )
        section, rest = parts[0], parts[1:]

        if section == "ai":
            obj = self.ai
        elif section == "github":
            obj = self.github
        elif section == "monitor":
            obj = self.monitor
        elif section == "daemon":
            obj = self.daemon
        else:
            raise ValueError(
                f"Unknown config section: {section}. "
                "Expected one of: ai, github, monitor, daemon."
            )

        field_name = rest[0]
        if field_name not in type(obj).model_fields:
            raise ValueError(f"Unknown field: {key}")
        return obj, field_name

    def _resolve_repo_field(self, key: str) -> str:
        """Validate a dotted key as a per-repo field and return the field name."""
        section, _, field_name = key.partition(".")
        if section != "github" or not field_name:
            raise ValueError(f"--repo applies to github.* keys only; got '{key}'.")
        settable = [name for name in RepoConfig.model_fields if name != "repo"]
        if field_name not in settable:
            raise ValueError(
                f"Unknown per-repo field: {field_name}. "
                f'Expected one of: {", ".join(settable)}.'
            )
        return field_name

    def _repo_entry(self, repo: str) -> "RepoConfig":
        """The RepoConfig for `repo`, or a ValueError naming how to add it."""
        entry = next((rc for rc in self.github.repos if rc.repo == repo), None)
        if entry is None:
            raise ValueError(
                f"Repository '{repo}' is not configured. "
                f"Add it with 'maajun add-repo {repo}'."
            )
        return entry

    def _per_repo_key(self, key: str) -> str | None:
        """The RepoConfig field a bare `github.<field>` key refers to, if any.

        `github.mode` and friends have no top-level scalar to write any more —
        they name a field that exists once per repository — so they are handled
        before _resolve, which would otherwise reject them as unknown.
        """
        section, _, field_name = key.partition(".")
        if section != "github":
            return None
        return field_name if field_name in _PER_REPO_FIELDS else None

    def set(self, key: str, value: str, repo: str | None = None) -> None:
        """Set a config value using dot notation (e.g. 'github.mode' = 'fix').

        With `repo`, the value is written to that repository's entry only.
        Without it, a per-repo github.* field is applied to every configured
        repository, so one command still covers the common case.

        The value is type-coerced and validated by the model's validators;
        an invalid value raises ValueError.
        """
        if repo is not None:
            field_name = self._resolve_repo_field(key)
            _set_field(self._repo_entry(repo), field_name, value)
            return

        field_name = self._per_repo_key(key)
        if field_name is not None:
            if not self.github.repos:
                raise ValueError(
                    f"No repositories are configured, so {key} has nothing to "
                    "apply to. Add one with 'maajun add-repo <owner/name>'."
                )
            for repo_config in self.github.repos:
                _set_field(repo_config, field_name, value)
            return

        obj, field_name = self._resolve(key)
        if obj is self.github and field_name == "repos":
            raise ValueError(
                "github.repos is a list of repositories, not a single value. "
                "Use 'maajun add-repo <owner/name>' to add one."
            )
        _set_field(obj, field_name, value)

    def get(self, key: str, repo: str | None = None) -> str:
        """Get a config value using dot notation. Secrets are masked."""
        if repo is not None:
            field_name = self._resolve_repo_field(key)
            return _render_value(getattr(self._repo_entry(repo), field_name))

        field_name = self._per_repo_key(key)
        if field_name is not None:
            repos = self.github.repos
            if not repos:
                return ""
            if len(repos) == 1:
                return _render_value(getattr(repos[0], field_name))
            # Several repos can disagree, so name which value belongs to which.
            return ", ".join(
                f"{rc.repo}={_render_value(getattr(rc, field_name))}" for rc in repos
            )

        obj, field_name = self._resolve(key)
        if field_name == "api_key" and getattr(obj, field_name):
            return "***"
        return _render_value(getattr(obj, field_name))


def reject_legacy_github(data: dict, path: Path) -> None:
    """Refuse the pre-multi-repo [github] shape rather than ignoring it.

    Raised at load time so the failure names the file and the fix, instead of
    surfacing later as a daemon that mysteriously opens no pull requests.
    """
    github = data.get("github")
    if not isinstance(github, dict):
        return
    present = [key for key in LEGACY_GITHUB_SCALARS if key in github]
    if not present:
        return

    keys = ", ".join(present)
    verb = "is" if len(present) == 1 else "are"
    repo = github.get("repo")
    if repo:
        fix = f"Run: maajun add-repo {repo}"
    else:
        # repo = "" was how the old format spelled local mode; that is now
        # simply the absence of any [[github.repos]] entry.
        fix = (
            "Delete those keys — local mode is now just a config with no "
            "[[github.repos]] entry."
        )
    raise ConfigError(
        f"{path}: [github] {keys} {verb} the old single-repo format, which is "
        f"no longer supported.\n{fix}"
    )


def _render_value(val) -> str:
    """A config value as the CLI shows it: lists joined, None as empty."""
    if isinstance(val, list):
        if val and isinstance(val[0], RepoConfig):
            return ", ".join(repo_config.repo for repo_config in val)
        return ", ".join(str(v) for v in val)
    return "" if val is None else str(val)


def _table(parent, name: str):
    """Get an existing tomlkit table or create and attach a new one."""
    node = parent.get(name)
    if node is None:
        node = tomlkit.table()
        parent[name] = node
    return node


def _set_or_del(table, name: str, value) -> None:
    """Set a key when value is truthy, otherwise remove it from the table."""
    if value:
        table[name] = value
    else:
        table.pop(name, None)


def _set_if_customized(table, model: BaseModel, name: str) -> None:
    """Write a field only when it differs from its default.

    Keeps generated configs free of a dozen tuning keys nobody touched, while
    guaranteeing anything the user did set survives a save/load round trip.
    """
    value = getattr(model, name)
    default = type(model).model_fields[name].get_default(call_default_factory=True)
    if value == default:
        table.pop(name, None)
    else:
        table[name] = value


def _set_field(obj: BaseModel, field_name: str, value: str) -> None:
    """Coerce a string to a Pydantic field's type and assign it.

    validate_assignment on the model runs the field validators, so an
    invalid value surfaces as a ValueError the CLI can print.
    """
    field_info = type(obj).model_fields.get(field_name)
    if field_info is None:
        raise ValueError(f"Unknown field: {field_name}")

    annotation = field_info.annotation
    # Unwrap Optional[T] / T | None down to the type we need to coerce to.
    inner_types = [a for a in get_args(annotation) if a is not type(None)]
    field_type = inner_types[0] if inner_types else annotation

    try:
        if get_origin(annotation) is list or annotation is list:
            coerced: object = [
                item.strip() for item in value.split(_LIST_SEP) if item.strip()
            ]
        elif field_type is bool:
            coerced = value.strip().lower() in ("true", "1", "yes", "on")
        elif field_type is int:
            coerced = int(value)
        elif field_type is float:
            coerced = float(value)
        else:
            coerced = value
        setattr(obj, field_name, coerced)
    except (ValueError, ValidationError) as e:
        raise ValueError(_first_error(e)) from e


def _first_error(exc: Exception) -> str:
    """Render a Pydantic ValidationError as a single readable line."""
    if isinstance(exc, ValidationError):
        errors = exc.errors()
        if errors:
            return errors[0].get("msg", str(exc))
    return str(exc)


def render_config(config: "Config") -> str:
    """Build the `maajun config` overview as a Rich-markup string.

    Returned as text (not printed) so the CLI owns the console and this stays
    a pure, testable rendering of the config's current state.
    """
    parts = [
        "\n[bold cyan]\\[ai][/bold cyan]",
        f'  provider = [green]"{config.ai.provider}"[/green]',
    ]
    if config.ai.model:
        parts.append(f'  model = [green]"{config.ai.model}"[/green]')
    parts.append(f"  temperature = [green]{config.ai.temperature}[/green]")
    parts.append(f"  max_tokens = [green]{config.ai.max_tokens}[/green]")
    if config.ai.thinking_mode:
        parts.append("  thinking_mode = [green]true[/green]")

    parts.append("\n[bold cyan]\\[github][/bold cyan]")
    if config.github.repos:
        for repo_config in config.github.repos:
            parts.append("\n  [dim]\\[\\[github.repos]][/dim]")
            parts.append(f'    repo = [green]"{repo_config.repo}"[/green]')
            parts.append(f'    base_branch = [green]"{repo_config.base_branch}"[/green]')
            parts.append(f'    mode = [green]"{repo_config.mode}"[/green]')
            if repo_config.test_command:
                parts.append(
                    f'    test_command = [green]"{repo_config.test_command}"[/green]'
                )
            if repo_config.log_files:
                parts.append(f"    log_files = [green]{repo_config.log_files}[/green]")
    else:
        parts.append(
            f'  [dim]no repositories — add one with[/dim] '
            f"maajun add-repo [yellow]{PLACEHOLDER_REPO}[/yellow]"
        )

    parts.append("\n[bold cyan]\\[monitor][/bold cyan]")
    parts.append(f"  log_files = [green]{config.monitor.log_files}[/green]")
    parts.append(f"  poll_interval = [green]{config.monitor.poll_interval}[/green]")

    parts.append("\n[dim]Use 'maajun config <key> <value>' to set a value.[/dim]")
    return "\n".join(parts)
