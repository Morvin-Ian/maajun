import os
import tomllib
from pathlib import Path
from typing import get_args, get_origin

import tomlkit
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from maajun.limits import DEFAULT_REOPEN_AFTER_DAYS
from maajun.monitors.defaults import (
    DEFAULT_ERROR_PATTERN,
    DEFAULT_JSON_LEVEL_VALUES,
    DEFAULT_TRACEBACK_HEADERS,
)
from maajun.providers.base import ProviderType
from maajun.utils import PLACEHOLDER_REPO, is_valid_repo

LIST_SEP = ","
VALID_MODES = ("suggest", "fix", "automatic")
LEGACY_GITHUB_SCALARS = (
    "repo", "base_branch", "mode", "test_command", "verification_commands",
    "reproduction_command",
)
PER_REPO_FIELDS = (
    "base_branch", "mode", "log_files", "test_command", "verification_commands",
    "reproduction_command", "runtime_artifact_repo",
    "allow_public_runtime_artifacts",
)


class ConfigError(ValueError):
    """A config file that cannot be used as written."""


class Base(BaseModel):
    """Base model that re-validates on assignment so `config.set(...)` and
    direct attribute writes are checked against the field validators."""

    model_config = ConfigDict(validate_assignment=True)


def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", "~/.config")
    return Path(base).expanduser() / "maajun" / "config.toml"


def default_data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME", "~/.local/share")
    return Path(base).expanduser() / "maajun"


class AIProviderConfig(Base):
    provider: str = ProviderType.DEEPSEEK.value
    model: str | None = None
    # Model for the one-line "is this even a defect?" screen. Unset means the
    # provider's own base model, which is the cheap tier for all of them.
    triage_model: str | None = None
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


class DeploymentConfig(Base):
    """Where and how a repo runs, and where its runtime errors land.

    Deploy methods are not modelled; sinks are. However an app is started —
    gunicorn under systemd, docker compose, supervisor, nginx on the host or
    in a container — its errors end up in a file, in the journal, or on a
    container's stdout. Those three cover every combination.
    """

    path: str = ""  # the app's folder on the server
    port: int = 0  # what it listens on; 0 means unknown
    runs: str = ""  # free text: "docker compose", "systemd: kfl.service"
    stack: str = ""  # "Django 5 + gunicorn", from reading the code
    service_unit: str = ""  # exact systemd unit selected by discovery
    service_command: str = ""  # exact ExecStart command from the active unit
    proxy_kind: str = ""  # nginx, caddy, traefik, ...
    proxy_config_path: str = ""  # active configuration path on this host
    proxy_repo_path: str = ""  # repository path deployed as that configuration
    proxy_body_limit: str = ""  # effective/discovered request body boundary
    config_owner: str = ""  # repository or operator
    infra_repo: str = ""  # optional owner/name for deployment configuration
    log_files: list[str] = Field(default_factory=list)
    journald_units: list[str] = Field(default_factory=list)
    docker_containers: list[str] = Field(default_factory=list)
    # "none" is an explicit "this repo has no runtime source and that is
    # deliberate" — the one thing that silences the preflight failure.
    runtime: str = ""

    @field_validator("port")
    @classmethod
    def validate_port(cls, value: int) -> int:
        if value and not (1 <= value <= 65535):
            raise ValueError("port must be between 1 and 65535")
        return value

    @field_validator("runtime")
    @classmethod
    def validate_runtime(cls, value: str) -> str:
        if value and value != "none":
            raise ValueError('runtime must be "none" or left unset')
        return value

    @field_validator("config_owner")
    @classmethod
    def validate_config_owner(cls, value: str) -> str:
        if value not in ("", "repository", "operator"):
            raise ValueError('config_owner must be "repository", "operator", or unset')
        return value

    @field_validator("infra_repo")
    @classmethod
    def validate_infra_repo(cls, value: str) -> str:
        if value and not is_valid_repo(value):
            raise ValueError('infra_repo must be in "owner/name" form')
        return value

    def sources(self) -> list[tuple[str, str]]:
        """Every error source as (kind, target), in the order they are read."""
        return [
            *(("file", path) for path in self.log_files),
            *(("journald", unit) for unit in self.journald_units),
            *(("docker", container) for container in self.docker_containers),
        ]

    def describes_a_deployment(self) -> bool:
        """True once anything has been recorded — what `save` writes on."""
        return bool(
            self.path or self.port or self.runs or self.stack
            or self.service_unit or self.service_command
            or self.proxy_kind or self.proxy_config_path or self.proxy_repo_path
            or self.proxy_body_limit
            or self.config_owner or self.infra_repo
            or self.runtime or self.sources()
        )


class RepoConfig(Base):
    repo: str = ""  # "owner/name"
    base_branch: str = "main"
    mode: str = "suggest"
    log_files: list[str] = Field(default_factory=list)
    test_command: str = ""
    verification_commands: list[str] = Field(default_factory=list)
    reproduction_command: str = ""
    # Passive runtime evidence stays local when the target is public unless
    # explicitly allowed or routed to a non-public repository.
    runtime_artifact_repo: str = ""
    allow_public_runtime_artifacts: bool = False
    deployment: DeploymentConfig = Field(default_factory=DeploymentConfig)

    def post_fix_commands(self) -> list[str]:
        """Owner-configured commands, preserving legacy test_command and order."""
        commands = [self.test_command, *self.verification_commands]
        return list(dict.fromkeys(command for command in commands if command))

    def runtime_sources(self) -> list[tuple[str, str]]:
        """Every runtime error source for this repo, as (kind, target).

        `log_files` on the repo predates the deployment block and stays
        supported, so a config written before this exists keeps working.
        """
        sources = [("file", path) for path in self.log_files]
        for source in self.deployment.sources():
            if source not in sources:
                sources.append(source)
        return sources

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, value: str) -> str:
        if value not in VALID_MODES:
            raise ValueError('mode must be "suggest", "fix", or "automatic"')
        return value

    @field_validator("repo")
    @classmethod
    def validate_repo(cls, value: str) -> str:
        if value and not is_valid_repo(value):
            raise ValueError('repo must be in "owner/name" form')
        return value

    @field_validator("runtime_artifact_repo")
    @classmethod
    def validate_runtime_artifact_repo(cls, value: str) -> str:
        if value and not is_valid_repo(value):
            raise ValueError('runtime_artifact_repo must be in "owner/name" form')
        return value


# Per-repo fields that are a group of settings rather than one value, and so
# are addressed as github.<group>.<leaf>.
REPO_FIELD_GROUPS: dict[str, type[Base]] = {"deployment": DeploymentConfig}


def require_repo_scope(key: str) -> None:
    """Reject a group key that was given without --repo.

    A folder, a port, or a container name belongs to one deployment, so
    cascading it across every repo is never what was meant.
    """
    section, _, rest = key.partition(".")
    if section != "github":
        return
    group, _, leaf = rest.partition(".")
    if group in REPO_FIELD_GROUPS and leaf:
        raise ValueError(
            f"{key} describes one deployment, so it needs a repository: "
            f"pass --repo owner/name."
        )


class GitHubConfig(Base):
    """GitHub configuration: a list of repositories, each with its own settings. """

    repos: list[RepoConfig] = Field(default_factory=list)
    # How branches are pushed: "ssh", "https", or "auto" (token if there is
    # one, SSH keys otherwise). The API always needs a token either way.
    transport: str = "auto"

    @field_validator("transport")
    @classmethod
    def validate_transport(cls, value: str) -> str:
        if value not in ("auto", "ssh", "https"):
            raise ValueError('transport must be "auto", "ssh", or "https"')
        return value


class MonitorConfig(Base):
    log_files: list[str] = Field(default_factory=list)
    error_pattern: str = DEFAULT_ERROR_PATTERN
    # A guard that refused bad input is not a bug. Off means every logged
    # error is analyzed, however obviously intended it looks.
    ignore_by_design: bool = True
    ignore_patterns: list[str] = Field(default_factory=list)
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

    @property
    def json_level_value_set(self) -> frozenset[str]:
        """json_level_values parsed into the set the monitors expect.

        Stored as a comma-separated string so `maajun config` can set it.
        """
        return frozenset(
            value.strip().lower()
            for value in self.json_level_values.split(LIST_SEP)
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


class DaemonConfig(Base):
    # Resolved per instance, not at import: frozen it ignores a later
    # XDG_DATA_HOME, and `reset` then deletes the real data directory.
    workdir: str = Field(default_factory=lambda: str(default_data_dir()))
    repo_path: str = ""
    max_usd_per_day: float = 5.0
    # The daily cap is only checked between incidents, and one investigation
    # can spend it all. Past this the tools go and the report is asked for.
    max_usd_per_incident: float = 1.0
    max_incidents_per_cycle: int = 10
    # Screen each new error with one cheap tool-less request before paying for
    # the investigation. Off investigates everything the signatures let past.
    screen_errors: bool = True
    # Screened-out errors are not incidents, so the cap above never counts
    # them. This one does.
    max_screens_per_cycle: int = 50
    # A published incident that goes quiet this long and comes back is
    # reported again, as a regression. 0 reports each error once, ever.
    reopen_after_days: float = DEFAULT_REOPEN_AFTER_DAYS


class ChatConfig(Base):
    max_usd_per_day: float = 5.0


class Config(Base):
    ai: AIProviderConfig = Field(default_factory=AIProviderConfig)
    github: GitHubConfig = Field(default_factory=GitHubConfig)
    monitor: MonitorConfig = Field(default_factory=MonitorConfig)
    daemon: DaemonConfig = Field(default_factory=DaemonConfig)
    chat: ChatConfig = Field(default_factory=ChatConfig)

    # Where this config was read from, so save() can write it back. Underscored,
    # or pydantic makes it a real field: validated, saved, listed by `config`.
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

        ai = table(doc, "ai")
        ai["provider"] = self.ai.provider
        set_or_del(ai, "model", self.ai.model)
        set_or_del(ai, "triage_model", self.ai.triage_model)
        set_or_del(ai, "base_url", self.ai.base_url)
        ai["temperature"] = self.ai.temperature
        ai["max_tokens"] = self.ai.max_tokens
        ai["thinking_mode"] = self.ai.thinking_mode

        github = table(doc, "github")
        for legacy in LEGACY_GITHUB_SCALARS:
            github.pop(legacy, None)
        set_if_customized(github, self.github, "transport")
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
                if repo_config.verification_commands:
                    repo_table["verification_commands"] = repo_config.verification_commands
                if repo_config.reproduction_command:
                    repo_table["reproduction_command"] = repo_config.reproduction_command
                if repo_config.runtime_artifact_repo:
                    repo_table["runtime_artifact_repo"] = repo_config.runtime_artifact_repo
                if repo_config.allow_public_runtime_artifacts:
                    repo_table["allow_public_runtime_artifacts"] = True
                # Attached only once there is something to say, or every
                # existing config grows an empty [github.repos.deployment].
                if repo_config.deployment.describes_a_deployment():
                    repo_table["deployment"] = deployment_table(
                        repo_config.deployment
                    )
                repos_table.append(repo_table)
            # Trailing blank line, or the table that follows in the document
            # ends up butted directly against the last repo entry.
            repos_table[-1].add(tomlkit.nl())
            github["repos"] = repos_table
        else:
            # Local mode: no repos at all, rather than a repo spelled "".
            github.pop("repos", None)

        monitor = table(doc, "monitor")
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
            "ignore_by_design",
            "ignore_patterns",
        ):
            set_if_customized(monitor, self.monitor, name)
        # Dropped when the Actions monitor was removed; cleaned out of configs
        # written before that so a stale key cannot look like a live setting.
        monitor.pop("github_actions_repos", None)

        daemon = table(doc, "daemon")
        daemon["workdir"] = self.daemon.workdir
        set_or_del(daemon, "repo_path", self.daemon.repo_path or None)
        set_if_customized(daemon, self.daemon, "max_usd_per_day")
        set_if_customized(daemon, self.daemon, "max_usd_per_incident")
        set_if_customized(daemon, self.daemon, "max_incidents_per_cycle")
        set_if_customized(daemon, self.daemon, "screen_errors")

        chat = table(doc, "chat")
        set_if_customized(chat, self.chat, "max_usd_per_day")
        if not chat:
            doc.pop("chat", None)

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


    def sources_by_repo(
        self, repos: "list[RepoConfig] | None" = None
    ) -> list[tuple["RepoConfig | None", list[tuple[str, str]]]]:
        """Every runtime error source, grouped by the repo it files against.

        One answer for both the daemon's monitors and what `status` reports,
        so they cannot drift. Global monitor.log_files still attach to the
        first repo. `repos` overrides the configured list for local mode,
        which runs against one synthetic entry.
        """
        repos = self.github.repos if repos is None else repos
        shared = [("file", path) for path in self.monitor.log_files]
        if not repos:
            return [(None, shared)]
        grouped: list[tuple[RepoConfig | None, list[tuple[str, str]]]] = []
        for index, repo_config in enumerate(repos):
            own = repo_config.runtime_sources()
            if index == 0:
                own = shared + [source for source in own if source not in shared]
            grouped.append((repo_config, own))
        return grouped

    def resolve(self, key: str) -> tuple[BaseModel, str]:
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
        elif section == "chat":
            obj = self.chat
        else:
            raise ValueError(
                f"Unknown config section: {section}. "
                "Expected one of: ai, github, monitor, daemon, chat."
            )

        field_name = rest[0]
        if field_name not in type(obj).model_fields:
            raise ValueError(f"Unknown field: {key}")
        return obj, field_name

    def resolve_repo_field(self, key: str) -> tuple[str, str]:
        """Validate a dotted key as a per-repo field.

        Returns (field, subfield). subfield is "" for a plain field, and the
        leaf name when the key descends into a group such as
        `github.deployment.port`.
        """
        section, _, rest = key.partition(".")
        if section != "github" or not rest:
            raise ValueError(f"--repo applies to github.* keys only; got '{key}'.")
        field_name, _, subfield = rest.partition(".")
        settable = [name for name in RepoConfig.model_fields if name != "repo"]
        if field_name not in settable:
            raise ValueError(
                f"Unknown per-repo field: {field_name}. "
                f'Expected one of: {", ".join(settable)}.'
            )

        group = REPO_FIELD_GROUPS.get(field_name)
        if group is None:
            if subfield:
                raise ValueError(f"Unknown per-repo field: {rest}.")
            return field_name, ""

        leaves = ", ".join(group.model_fields)
        if not subfield:
            raise ValueError(
                f"{key} is a group of settings, not a single value. "
                f"Set one of: {leaves}."
            )
        if subfield not in group.model_fields:
            raise ValueError(
                f"Unknown {field_name} field: {subfield}. "
                f"Expected one of: {leaves}."
            )
        return field_name, subfield

    def repo_target(self, repo: str, key: str) -> tuple[BaseModel, str]:
        """The model and field name a per-repo key writes to."""
        field_name, subfield = self.resolve_repo_field(key)
        entry = self.repo_entry(repo)
        if subfield:
            return getattr(entry, field_name), subfield
        return entry, field_name

    def repo_entry(self, repo: str) -> "RepoConfig":
        """The RepoConfig for `repo`, or a ValueError naming how to add it."""
        entry = next((rc for rc in self.github.repos if rc.repo == repo), None)
        if entry is None:
            raise ValueError(
                f"Repository '{repo}' is not configured. "
                f"Add it with 'maajun add-repo {repo}'."
            )
        return entry

    def per_repo_key(self, key: str) -> str | None:
        """The RepoConfig field a bare `github.<field>` key refers to, if any.

        `github.mode` and friends have no top-level scalar to write any more —
        they name a field that exists once per repository — so they are handled
        before resolve, which would otherwise reject them as unknown.
        """
        section, _, field_name = key.partition(".")
        if section != "github":
            return None
        return field_name if field_name in PER_REPO_FIELDS else None

    def set(self, key: str, value: str, repo: str | None = None) -> None:
        """Set a config value using dot notation (e.g. 'github.mode' = 'fix').

        With `repo`, the value is written to that repository's entry only.
        Without it, a per-repo github.* field is applied to every configured
        repository, so one command still covers the common case.

        The value is type-coerced and validated by the model's validators;
        an invalid value raises ValueError.
        """
        if repo is not None:
            obj, field_name = self.repo_target(repo, key)
            set_field(obj, field_name, value)
            return

        require_repo_scope(key)
        field_name = self.per_repo_key(key)
        if field_name is not None:
            if not self.github.repos:
                raise ValueError(
                    f"No repositories are configured, so {key} has nothing to "
                    "apply to. Add one with 'maajun add-repo <owner/name>'."
                )
            for repo_config in self.github.repos:
                set_field(repo_config, field_name, value)
            return

        obj, field_name = self.resolve(key)
        if obj is self.github and field_name == "repos":
            raise ValueError(
                "github.repos is a list of repositories, not a single value. "
                "Use 'maajun add-repo <owner/name>' to add one."
            )
        set_field(obj, field_name, value)
        if obj is self.ai and field_name == "provider":
            # A model belongs to the provider it was chosen for; sending
            # one provider's id to another only fails on the first real call.
            self.ai.model = None

    def get(self, key: str, repo: str | None = None) -> str:
        """Get a config value using dot notation. Secrets are masked."""
        if repo is not None:
            obj, field_name = self.repo_target(repo, key)
            return render_value(getattr(obj, field_name))

        require_repo_scope(key)
        field_name = self.per_repo_key(key)
        if field_name is not None:
            repos = self.github.repos
            if not repos:
                return ""
            if len(repos) == 1:
                return render_value(getattr(repos[0], field_name))
            # Several repos can disagree, so name which value belongs to which.
            return ", ".join(
                f"{rc.repo}={render_value(getattr(rc, field_name))}" for rc in repos
            )

        obj, field_name = self.resolve(key)
        if field_name == "api_key" and getattr(obj, field_name):
            return "***"
        return render_value(getattr(obj, field_name))


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


def render_value(val) -> str:
    """A config value as the CLI shows it: lists joined, None as empty."""
    if isinstance(val, list):
        if val and isinstance(val[0], RepoConfig):
            return ", ".join(repo_config.repo for repo_config in val)
        return ", ".join(str(v) for v in val)
    return "" if val is None else str(val)


def table(parent, name: str):
    """Get an existing tomlkit table or create and attach a new one."""
    node = parent.get(name)
    if node is None:
        node = tomlkit.table()
        parent[name] = node
    return node


def set_or_del(table, name: str, value) -> None:
    """Set a key when value is truthy, otherwise remove it from the table."""
    if value:
        table[name] = value
    else:
        table.pop(name, None)


def set_if_customized(table, model: BaseModel, name: str) -> None:
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


def deployment_table(deployment: "DeploymentConfig"):
    """A [github.repos.deployment] sub-table holding only what is set.

    Built fresh rather than round-tripped: `save` rebuilds the whole repos
    array of tables, so anything not written here is lost.
    """
    node = tomlkit.table()
    for name in (
        "path", "port", "runs", "stack", "runtime", "service_unit",
        "service_command", "proxy_kind", "proxy_config_path", "proxy_repo_path",
        "proxy_body_limit", "config_owner", "infra_repo",
    ):
        set_or_del(node, name, getattr(deployment, name))
    for name in ("log_files", "journald_units", "docker_containers"):
        set_or_del(node, name, getattr(deployment, name))
    return node


def set_field(obj: BaseModel, field_name: str, value: str) -> None:
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
                item.strip() for item in value.split(LIST_SEP) if item.strip()
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
        raise ValueError(first_error(e)) from e


def first_error(exc: Exception) -> str:
    """Render a Pydantic ValidationError as a single readable line."""
    if isinstance(exc, ValidationError):
        errors = exc.errors()
        if errors:
            return errors[0].get("msg", str(exc))
    return str(exc)


def render_deployment(deployment: "DeploymentConfig") -> list[str]:
    """Rich-markup lines for one repo's deployment, empty when it has none."""
    if not deployment.describes_a_deployment():
        return []
    lines = ["\n    [dim]\\[github.repos.deployment][/dim]"]
    for name in (
        "path", "runs", "stack", "runtime", "service_unit", "service_command",
        "proxy_kind", "proxy_config_path", "proxy_repo_path", "proxy_body_limit",
        "config_owner", "infra_repo",
    ):
        value = getattr(deployment, name)
        if value:
            lines.append(f'      {name} = [green]"{value}"[/green]')
    if deployment.port:
        lines.append(f"      port = [green]{deployment.port}[/green]")
    for name in ("log_files", "journald_units", "docker_containers"):
        value = getattr(deployment, name)
        if value:
            lines.append(f"      {name} = [green]{value}[/green]")
    return lines


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
            if repo_config.verification_commands:
                parts.append(
                    "    verification_commands = "
                    f"[green]{repo_config.verification_commands}[/green]"
                )
            if repo_config.reproduction_command:
                parts.append(
                    "    reproduction_command = "
                    f'[green]"{repo_config.reproduction_command}"[/green]'
                )
            if repo_config.log_files:
                parts.append(f"    log_files = [green]{repo_config.log_files}[/green]")
            parts.extend(render_deployment(repo_config.deployment))
    else:
        parts.append(
            f'  [dim]no repositories — add one with[/dim] '
            f"maajun add-repo [yellow]{PLACEHOLDER_REPO}[/yellow]"
        )

    parts.append("\n[bold cyan]\\[monitor][/bold cyan]")
    parts.append(f"  log_files = [green]{config.monitor.log_files}[/green]")
    parts.append(f"  poll_interval = [green]{config.monitor.poll_interval}[/green]")

    # The spend caps were settable but invisible here, so the one number that
    # decides how deep an investigation may go could not be read back.
    parts.append("\n[bold cyan]\\[daemon][/bold cyan]")
    parts.append(f"  max_usd_per_day = [green]{config.daemon.max_usd_per_day}[/green]")
    parts.append(
        "  max_usd_per_incident = "
        f"[green]{config.daemon.max_usd_per_incident}[/green]"
    )
    parts.append(
        f"  max_incidents_per_cycle = [green]{config.daemon.max_incidents_per_cycle}[/green]"
    )
    parts.append(f"  screen_errors = [green]{str(config.daemon.screen_errors).lower()}[/green]")

    parts.append("\n[dim]Use 'maajun config <key> <value>' to set a value.[/dim]")
    return "\n".join(parts)
