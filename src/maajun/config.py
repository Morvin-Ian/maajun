import os
import tomllib
from pathlib import Path
from typing import get_args, get_origin

import tomlkit
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from maajun.monitors.logfile import (
    DEFAULT_ERROR_PATTERN,
    DEFAULT_JSON_LEVEL_VALUES,
    DEFAULT_TRACEBACK_HEADERS,
)
from maajun.providers.base import ProviderType
from maajun.utils import PLACEHOLDER_REPO, is_valid_repo

_LIST_SEP = ","

STARTER_CONFIG = """\
# Maajun daemon configuration.

[ai]
provider = "deepseek"
# thinking_mode = true

[github]
# Repository the daemon documents errors in and opens PRs against.
repo = "owner/name"
base_branch = "main"
# "suggest": PRs contain only the incident report and suggested fix.
# "fix": the agent may also change code inside its isolated workspace.
mode = "suggest"

[monitor]
poll_interval = 30

# Monitors can be configured two ways:
#
# 1) Legacy shorthand (still supported):
#    log_files = ["/var/log/myapp/error.log"]
#    error_pattern = "\\\\b(ERROR|CRITICAL|FATAL)\\\\b"
#    github_actions_token = "github_pat_..."
#    github_actions_repos = ["you/another-repo"]
#
# 2) Declarative instances (recommended for multiple types):
#    [[monitor.instances]]
#    type = "logfile"
#    path = "/var/log/myapp/error.log"
#    error_pattern = "\\\\b(ERROR|CRITICAL|FATAL)\\\\b"

[daemon]
# Where clones, the incident database, and state live.
# workdir = "~/.local/share/maajun"

# Email notifications when a PR opens or an incident fails (optional).
# [daemon.email]
# smtp_host = "smtp.gmail.com"
# smtp_port = 587                    # 465 for implicit TLS, else STARTTLS
# username = "you@example.com"
# password = ""                      # or set MAAJUN_SMTP_PASSWORD
# from_addr = "you@example.com"
# to_addrs = ["you@example.com"]
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
    model: str | None = None  # None -> provider default
    api_key: str | None = None
    temperature: float = 0.3
    max_tokens: int = 4096
    thinking_mode: bool = False

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, v: str) -> str:
        valid_providers = [p.value for p in ProviderType]
        if v not in valid_providers:
            raise ValueError(f'Provider must be one of: {", ".join(valid_providers)}')
        return v


class RepoConfig(_Base):
    """Configuration for a single repository."""
    repo: str = ""  # "owner/name"
    base_branch: str = "main"
    # "suggest": the PR contains only the analysis report.
    # "fix": the agent may also edit code inside the workspace.
    mode: str = "suggest"
    # Log files watched for this repo, in addition to the global
    # monitor.log_files (which attach to the first configured repo).
    log_files: list[str] = Field(default_factory=list)

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        if v not in ("suggest", "fix"):
            raise ValueError('mode must be "suggest" or "fix"')
        return v

    @field_validator("repo")
    @classmethod
    def validate_repo(cls, v: str) -> str:
        if v and not is_valid_repo(v):
            raise ValueError('repo must be in "owner/name" form')
        return v


class GitHubConfig(_Base):
    """GitHub configuration. Supports single repo (legacy) or multiple repos."""
    repo: str = ""  # Legacy: single repo "owner/name"
    base_branch: str = "main"
    mode: str = "suggest"
    # Multiple repos with per-repo log file mapping (supersedes the scalars above).
    repos: list[RepoConfig] = Field(default_factory=list)

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        if v not in ("suggest", "fix"):
            raise ValueError('mode must be "suggest" or "fix"')
        return v

    @field_validator("repo")
    @classmethod
    def validate_repo(cls, v: str) -> str:
        if v and not is_valid_repo(v):
            raise ValueError('repo must be in "owner/name" form')
        return v

    def get_all_repos(self) -> list[RepoConfig]:
        """Get all configured repos, normalizing legacy single-repo format."""
        if self.repos:
            return self.repos
        if self.repo:
            return [RepoConfig(
                repo=self.repo,
                base_branch=self.base_branch,
                mode=self.mode,
            )]
        return []


class MonitorInstanceConfig(_Base):
    """One declaratively configured monitor: `[[monitor.instances]]`.

    `type` selects a registered monitor class and every other key is passed
    to its constructor, so this model stays open (extra="allow") rather than
    duplicating each monitor's signature.
    """

    type: str
    repo: str = ""
    model_config = ConfigDict(extra="allow")

    def monitor_kwargs(self) -> dict:
        """Constructor kwargs for the monitor: everything but type/repo."""
        return dict(self.model_extra or {})


class MonitorConfig(_Base):
    log_files: list[str] = Field(default_factory=list)
    error_pattern: str = DEFAULT_ERROR_PATTERN
    poll_interval: float = 30.0

    # Log-line detection. Applied to every logfile monitor built from the
    # log_files shorthand; per-instance overrides go in [[monitor.instances]].
    json_level_field: str = ""
    json_level_values: str = ",".join(sorted(DEFAULT_JSON_LEVEL_VALUES))
    traceback_headers: list[str] = Field(
        default_factory=lambda: list(DEFAULT_TRACEBACK_HEADERS)
    )
    # Emit nothing until burst_threshold events land within the window.
    burst_threshold: int = 1
    burst_window_seconds: float = 60.0
    # Requires the optional watchdog extra: pip install 'maajun[watchdog]'
    use_watchdog: bool = False

    github_actions_token: str = ""
    github_actions_repos: list[str] = Field(default_factory=list)

    instances: list[MonitorInstanceConfig] = Field(default_factory=list)

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
        """Detection settings shared by every shorthand logfile monitor."""
        return dict(
            error_pattern=self.error_pattern,
            json_level_field=self.json_level_field,
            json_level_values=self.json_level_value_set,
            traceback_headers=tuple(self.traceback_headers),
            burst_threshold=self.burst_threshold,
            burst_window_seconds=self.burst_window_seconds,
            use_watchdog=self.use_watchdog,
        )


class EmailConfig(_Base):
    """SMTP settings for notification emails. Enabled when smtp_host,
    from_addr, and to_addrs are all set."""

    smtp_host: str = ""
    smtp_port: int = 587  # 465 -> implicit TLS, otherwise STARTTLS
    username: str = ""
    password: str = ""  # or set MAAJUN_SMTP_PASSWORD in the environment
    from_addr: str = ""
    to_addrs: list[str] = Field(default_factory=list)


class DaemonConfig(_Base):
    workdir: str = str(default_data_dir())
    email: EmailConfig = Field(default_factory=EmailConfig)


class Config(_Base):
    ai: AIProviderConfig = Field(default_factory=AIProviderConfig)
    github: GitHubConfig = Field(default_factory=GitHubConfig)
    monitor: MonitorConfig = Field(default_factory=MonitorConfig)
    daemon: DaemonConfig = Field(default_factory=DaemonConfig)

    _path: Path | None = None

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        """Load config from a TOML file; missing file yields defaults."""
        path = path or default_config_path()
        config = cls()
        config._path = path
        if not path.exists():
            return config
        with open(path, "rb") as f:
            data = tomllib.load(f)
        loaded = cls.model_validate(data)
        loaded._path = path
        return loaded

    def save(self, path: Path | None = None) -> None:
        """Write the config to a TOML file.

        Uses tomlkit to round-trip an existing file: comments and formatting
        on keys the user has already written are preserved; every known field
        is (re)written so nothing is silently dropped.
        """
        path = path or self._path or default_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        if path.exists():
            doc = tomlkit.parse(path.read_text())
        else:
            doc = tomlkit.document()
            doc.add(tomlkit.comment("Maajun daemon configuration."))

        ai = _table(doc, "ai")
        ai["provider"] = self.ai.provider
        _set_or_del(ai, "model", self.ai.model)
        ai["temperature"] = self.ai.temperature
        ai["max_tokens"] = self.ai.max_tokens
        ai["thinking_mode"] = self.ai.thinking_mode

        github = _table(doc, "github")
        if self.github.repos:
            # Multi-repo mode: replace any legacy scalar keys with an
            # array-of-tables so the two representations never coexist.
            for legacy in ("repo", "base_branch", "mode"):
                github.pop(legacy, None)
            repos_table = tomlkit.aot()
            for repo_config in self.github.repos:
                repo_table = tomlkit.table()
                repo_table["repo"] = repo_config.repo
                repo_table["base_branch"] = repo_config.base_branch
                repo_table["mode"] = repo_config.mode
                if repo_config.log_files:
                    repo_table["log_files"] = repo_config.log_files
                repos_table.append(repo_table)
            github["repos"] = repos_table
        else:
            github.pop("repos", None)
            github["repo"] = self.github.repo or PLACEHOLDER_REPO
            github["base_branch"] = self.github.base_branch
            github["mode"] = self.github.mode

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
            "use_watchdog",
        ):
            _set_if_customized(monitor, self.monitor, name)
        _set_or_del(monitor, "github_actions_token", self.monitor.github_actions_token or None)
        if self.monitor.github_actions_repos:
            monitor["github_actions_repos"] = self.monitor.github_actions_repos
        else:
            monitor.pop("github_actions_repos", None)
        if self.monitor.instances:
            instances = tomlkit.aot()
            for instance in self.monitor.instances:
                instance_table = tomlkit.table()
                instance_table["type"] = instance.type
                if instance.repo:
                    instance_table["repo"] = instance.repo
                for key, value in instance.monitor_kwargs().items():
                    instance_table[key] = value
                instances.append(instance_table)
            monitor["instances"] = instances
        else:
            monitor.pop("instances", None)

        daemon = _table(doc, "daemon")
        daemon["workdir"] = self.daemon.workdir
        email = self.daemon.email
        if email.smtp_host:
            email_table = _table(daemon, "email")
            email_table["smtp_host"] = email.smtp_host
            email_table["smtp_port"] = email.smtp_port
            email_table["username"] = email.username
            email_table["from_addr"] = email.from_addr
            email_table["to_addrs"] = email.to_addrs

        path.write_text(tomlkit.dumps(doc))
        self._path = path

    def add_repo(self, repo: "RepoConfig") -> None:
        """Append a repo, migrating a legacy single-repo config into the list.

        Replaces an existing entry with the same `repo` name instead of
        duplicating it.
        """
        if not self.github.repos and self.github.repo:
            self.github.repos = [RepoConfig(
                repo=self.github.repo,
                base_branch=self.github.base_branch,
                mode=self.github.mode,
            )]
            self.github.repo = ""
        existing = [rc for rc in self.github.repos if rc.repo != repo.repo]
        self.github.repos = [*existing, repo]

    # -- dot-notation get/set ------------------------------------------------

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
            if rest[0] == "email":
                obj, rest = self.daemon.email, rest[1:]
                if not rest:
                    raise ValueError("Specify a field, e.g. 'daemon.email.smtp_host'.")
            else:
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

    def set(self, key: str, value: str) -> None:
        """Set a config value using dot notation (e.g. 'github.mode' = 'fix').

        The value is type-coerced and validated by the model's validators;
        an invalid value raises ValueError.
        """
        obj, field_name = self._resolve(key)
        _set_field(obj, field_name, value)
        # Keep per-repo modes aligned when the top-level mode is set.
        if obj is self.github and field_name == "mode":
            for rc in self.github.repos:
                rc.mode = value

    def get(self, key: str) -> str:
        """Get a config value using dot notation. Secrets are masked."""
        obj, field_name = self._resolve(key)
        if field_name in ("api_key", "password") and getattr(obj, field_name):
            return "***"
        val = getattr(obj, field_name)
        if isinstance(val, list):
            if val and isinstance(val[0], RepoConfig):
                return ", ".join(rc.repo for rc in val)
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

    ann = field_info.annotation
    non_none = [a for a in get_args(ann) if a is not type(None)]
    base = non_none[0] if non_none else ann

    try:
        if get_origin(ann) is list or ann is list:
            coerced: object = [v.strip() for v in value.split(_LIST_SEP) if v.strip()]
        elif base is bool:
            coerced = value.strip().lower() in ("true", "1", "yes", "on")
        elif base is int:
            coerced = int(value)
        elif base is float:
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
        for rc in config.github.repos:
            parts.append("\n  [dim]\\[\\[github.repos]][/dim]")
            parts.append(f'    repo = [green]"{rc.repo}"[/green]')
            parts.append(f'    base_branch = [green]"{rc.base_branch}"[/green]')
            parts.append(f'    mode = [green]"{rc.mode}"[/green]')
            if rc.log_files:
                parts.append(f"    log_files = [green]{rc.log_files}[/green]")
    elif config.github.repo:
        parts.append(f'  repo = [green]"{config.github.repo}"[/green]')
        parts.append(f'  base_branch = [green]"{config.github.base_branch}"[/green]')
        parts.append(f'  mode = [green]"{config.github.mode}"[/green]')
    else:
        parts.append(
            f'  repo = [yellow]"{PLACEHOLDER_REPO}"[/yellow] [dim](not configured)[/dim]'
        )

    parts.append("\n[bold cyan]\\[monitor][/bold cyan]")
    parts.append(f"  log_files = [green]{config.monitor.log_files}[/green]")
    parts.append(f"  poll_interval = [green]{config.monitor.poll_interval}[/green]")

    parts.append("\n[dim]Use 'maajun config <key> <value>' to set a value.[/dim]")
    return "\n".join(parts)
