from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from maajun.config import DeploymentConfig
from maajun.utils.commands import run_text

# Where a deployed checkout usually lives. These and their immediate children
# only; a filesystem-wide search costs more than asking.
CANDIDATE_ROOTS = ("/srv", "/opt", "/var/www", "/home", "~")

LOG_GLOBS = ("logs/*.log", "log/*.log", "*.log")

# A reverse proxy fronting the app: its errors (502s, upstream timeouts)
# never reach the app's own logger.
NGINX_LOG = "/var/log/nginx/error.log"

PROXY_HINTS = ("nginx", "caddy", "traefik", "apache")

# gunicorn -b 0.0.0.0:8000, uvicorn --port 8000, runserver 0.0.0.0:8000
PORT_IN_COMMAND = re.compile(
    r"(?:--port[= ]|-p[= ]|(?:-b|--bind|--host)[= ][\d.a-z\[\]:]*?:)(\d{2,5})\b"
)
PORT_IN_PUBLISH = re.compile(r":(\d{2,5})->")
NGINX_CONFIG_MARKER = re.compile(r"^# configuration file (.+):$")


@dataclass
class Container:
    name: str
    project: str = ""
    working_dir: str = ""
    ports: str = ""
    state: str = ""


@dataclass
class Unit:
    name: str
    working_dir: str = ""
    exec_start: str = ""


@dataclass
class Discovered:
    """What the probes found, plus how — the notes are shown to the user."""

    path: str = ""
    port: int = 0
    runs: str = ""
    service_unit: str = ""
    service_command: str = ""
    proxy_kind: str = ""
    proxy_config_path: str = ""
    proxy_repo_path: str = ""
    proxy_body_limit: str = ""
    config_owner: str = ""
    log_files: list[str] = field(default_factory=list)
    journald_units: list[str] = field(default_factory=list)
    docker_containers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def has_source(self) -> bool:
        return bool(self.log_files or self.journald_units or self.docker_containers)

    def merged_into(self, existing: DeploymentConfig) -> DeploymentConfig:
        """A copy of `existing` with what was found added.

        Additive: a path someone typed by hand is never overwritten by a
        guess, and a source already configured is not duplicated.
        """
        merged = existing.model_copy(deep=True)
        merged.path = existing.path or self.path
        merged.port = existing.port or self.port
        merged.runs = existing.runs or self.runs
        for name in (
            "service_unit", "service_command", "proxy_kind", "proxy_config_path",
            "proxy_repo_path", "proxy_body_limit", "config_owner",
        ):
            setattr(merged, name, getattr(existing, name) or getattr(self, name))
        for name in ("log_files", "journald_units", "docker_containers"):
            current = list(getattr(merged, name))
            current.extend(
                item for item in getattr(self, name) if item not in current
            )
            setattr(merged, name, current)
        return merged


def repo_name(repo: str) -> str:
    return repo.split("/")[-1]


def name_variants(repo: str) -> set[str]:
    """Names the same project might be spelled with on disk."""
    name = repo_name(repo).lower()
    return {name, name.replace("-", "_"), name.replace("_", "-")}


def looks_like(text: str, repo: str) -> bool:
    """Whether `text` mentions this project by any of its spellings."""
    lowered = text.lower()
    return any(variant in lowered for variant in name_variants(repo))


def git_remote(directory: Path) -> str:
    """The origin remote of a checkout, or "" — no git, no repo, no remote."""
    result = run_text(
        ["git", "-C", str(directory), "remote", "get-url", "origin"], timeout=5
    )
    return result.stdout.strip()


def candidate_directories(repo: str, hint: str = "") -> list[Path]:
    """Directories that might hold this repo's deployed checkout."""
    seen: list[Path] = []

    def add(path: Path) -> None:
        if path not in seen:
            seen.append(path)

    if hint:
        add(Path(hint).expanduser())
    cwd = Path.cwd()
    add(cwd)
    # Siblings of the current directory: checkouts sit side by side, in
    # workspace folders the system roots below do not cover.
    for variant in name_variants(repo):
        add(cwd.parent / variant)
    for root in CANDIDATE_ROOTS:
        base = Path(root).expanduser()
        if not base.is_dir():
            continue
        for variant in name_variants(repo):
            add(base / variant)
        try:
            # Immediate children only, and only for the small system roots —
            # /home can hold a lot of directories that are not deployments.
            if root not in ("/home", "~"):
                for child in sorted(base.iterdir()):
                    if child.is_dir():
                        add(child)
        except OSError:
            continue
    return [path for path in seen if path.is_dir()]


def find_folder(repo: str, hint: str = "") -> tuple[str, str]:
    """(folder, note) for the checkout of `repo`, by its origin remote."""
    candidates = candidate_directories(repo, hint)
    for directory in candidates:
        remote = git_remote(directory)
        if remote and repo.lower() in remote.lower():
            return str(directory), f"git remote of {directory} points at {repo}"
    # A folder named after the repo is a weaker signal, but a deployment
    # without git metadata (a copied release, a build image) is common.
    for directory in candidates:
        if directory.name.lower() in name_variants(repo):
            return str(directory), f"{directory} is named after the repo"
    return "", ""


def list_containers() -> list[Container]:
    """Every container docker knows about, stopped ones included."""
    result = run_text([
        "docker", "ps", "-a", "--format",
        "{{.Names}}\t{{.Label \"com.docker.compose.project\"}}\t"
        "{{.Label \"com.docker.compose.project.working_dir\"}}\t"
        "{{.Ports}}\t{{.State}}",
    ])
    if result.error:
        return []
    containers = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        fields = (line.split("\t") + [""] * 5)[:5]
        containers.append(Container(*(f.strip() for f in fields)))
    return containers


def matching_containers(
    containers: list[Container], repo: str, folder: str
) -> list[Container]:
    """Containers belonging to this repo, best signal first.

    A compose project whose working_dir is the app folder is conclusive; the
    project or container name matching the repo is a guess, but a good one.
    """
    folder_path = Path(folder).resolve() if folder else None

    def same_folder(container: Container) -> bool:
        if not folder_path or not container.working_dir:
            return False
        return Path(container.working_dir).resolve() == folder_path

    by_folder = [c for c in containers if same_folder(c)]
    if by_folder:
        return by_folder
    return [
        c for c in containers
        if looks_like(c.project, repo) or looks_like(c.name, repo)
    ]


def list_units() -> list[Unit]:
    """Every service unit with its working directory and command.

    One `systemctl show` with a pattern, rather than one call per unit.
    """
    result = run_text([
        "systemctl", "show", "*.service", "--no-pager",
        "-p", "Id", "-p", "WorkingDirectory", "-p", "ExecStart",
    ])
    if result.error:
        return []
    units = []
    for record in result.stdout.split("\n\n"):
        values: dict[str, str] = {}
        for line in record.splitlines():
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
        name = values.get("Id", "")
        if name:
            units.append(Unit(
                name=name,
                working_dir=values.get("WorkingDirectory", ""),
                exec_start=values.get("ExecStart", ""),
            ))
    return units


def matching_units(units: list[Unit], repo: str, folder: str) -> list[Unit]:
    """Units running this repo, best signal first."""
    folder_path = Path(folder).resolve() if folder else None

    def same_folder(unit: Unit) -> bool:
        if not folder_path or not unit.working_dir:
            return False
        try:
            return Path(unit.working_dir).resolve() == folder_path
        except OSError:
            return False

    by_folder = [unit for unit in units if same_folder(unit)]
    if by_folder:
        return by_folder
    return [
        unit for unit in units
        if looks_like(unit.name, repo)
        or (folder and folder in unit.exec_start)
    ]


def port_from_command(command: str) -> int:
    match = PORT_IN_COMMAND.search(command)
    return int(match.group(1)) if match else 0


def port_from_ports(ports: str) -> int:
    """The published host port from docker's "0.0.0.0:8000->8000/tcp"."""
    match = PORT_IN_PUBLISH.search(ports)
    return int(match.group(1)) if match else 0


def find_log_files(folder: str, proxied: bool) -> list[str]:
    """Log files under the app folder, plus the proxy's own error log."""
    found: list[str] = []
    if folder:
        base = Path(folder)
        for pattern in LOG_GLOBS:
            try:
                found.extend(
                    str(path) for path in sorted(base.glob(pattern))
                    if path.is_file() and str(path) not in found
                )
            except OSError:
                continue
    if proxied and Path(NGINX_LOG).exists() and NGINX_LOG not in found:
        found.append(NGINX_LOG)
    return found


def _nginx_context_body_limit(text: str, port: int) -> str:
    """The closest request-body limit around the proxy_pass for ``port``."""
    # Comments cannot affect block structure and may contain example directives.
    clean = "\n".join(line.partition("#")[0] for line in text.splitlines())
    tokens = re.split(r"([{};])", clean)
    stack: list[dict[str, str]] = [{"header": "root", "limit": ""}]
    statement = ""
    matched: list[dict[str, str]] | None = None
    target = re.compile(rf"\bproxy_pass\s+https?://[^;\s]*:{port}\b")
    for token in tokens:
        if token == "{":
            stack.append({"header": statement.strip(), "limit": ""})
            statement = ""
        elif token == ";":
            directive = " ".join(statement.split())
            statement = ""
            limit = re.match(r"client_max_body_size\s+([^\s;]+)", directive)
            if limit:
                stack[-1]["limit"] = limit.group(1)
            if target.search(directive):
                matched = list(stack)
        elif token == "}":
            statement = ""
            if len(stack) > 1:
                stack.pop()
        else:
            statement += token
    if not matched:
        return ""
    return next((node["limit"] for node in reversed(matched) if node["limit"]), "")


def nginx_proxy_for_port(port: int) -> tuple[str, str]:
    """The active nginx file and best-known request-body limit for the app."""
    if not port:
        return "", ""
    result = run_text(["nginx", "-T"], timeout=10)
    text = f"{result.stdout}\n{result.stderr}"
    current = ""
    sections: dict[str, list[str]] = {}
    matched_config = ""
    target = re.compile(rf"proxy_pass\s+https?://[^;\s]*:{port}\b")
    for line in text.splitlines():
        marker = NGINX_CONFIG_MARKER.match(line.strip())
        if marker:
            current = marker.group(1)
            sections.setdefault(current, [])
            continue
        if current:
            sections[current].append(line)
        if current and not matched_config and target.search(line):
            matched_config = current
    if not matched_config:
        return "", ""
    try:
        path = str(Path(matched_config).resolve())
    except OSError:
        path = matched_config
    config_text = "\n".join(sections[matched_config])
    limit = _nginx_context_body_limit(config_text, port)
    if not limit:
        main_limits = set(re.findall(
            r"\bclient_max_body_size\s+([^\s;]+)",
            "\n".join(sections.get("/etc/nginx/nginx.conf", [])),
        ))
        if len(main_limits) == 1:
            limit = f"{main_limits.pop()} (inherited from nginx.conf)"
        elif not main_limits:
            limit = "1m (nginx default; no active directive found)"
        else:
            limit = "unresolved (multiple inherited nginx directives)"
    return path, limit


def nginx_config_for_port(port: int) -> str:
    """The active nginx file whose block proxies to the app's detected port."""
    return nginx_proxy_for_port(port)[0]


def repo_path_for_host_config(config_path: str, folder: str) -> str:
    """Repository-relative path when an active host config lives in the checkout."""
    if not config_path or not folder:
        return ""
    try:
        return str(Path(config_path).resolve().relative_to(Path(folder).resolve()))
    except (OSError, ValueError):
        return ""


def discover(repo: str, existing: DeploymentConfig | None = None) -> Discovered:
    """Probe this machine for how `repo` is deployed."""
    existing = existing or DeploymentConfig()
    result = Discovered()

    folder, note = (
        (existing.path, f"{existing.path} is already configured")
        if existing.path
        else find_folder(repo)
    )
    result.path = folder
    if note:
        result.notes.append(note)

    all_containers = list_containers()
    containers = matching_containers(all_containers, repo, folder)
    if not folder:
        # A compose container knows the folder it was built from, which is
        # more reliable than guessing at directory names.
        for container in containers:
            if container.working_dir and Path(container.working_dir).is_dir():
                folder = container.working_dir
                result.path = folder
                result.notes.append(
                    f"folder from container {container.name}: {folder}"
                )
                # Now that the folder is known, siblings in the same compose
                # project count too, whatever they are named.
                containers = matching_containers(all_containers, repo, folder)
                break

    for container in containers:
        result.docker_containers.append(container.name)
        detail = f"container {container.name}"
        if container.project:
            detail += f" (compose project {container.project})"
        if container.state and container.state != "running":
            detail += f" — {container.state}"
        result.notes.append(detail)
        result.port = result.port or port_from_ports(container.ports)

    units = matching_units(list_units(), repo, folder)
    for unit in units:
        result.journald_units.append(unit.name)
        result.notes.append(f"systemd unit {unit.name}")
        result.port = result.port or port_from_command(unit.exec_start)
    if units:
        # Several units can share one checkout. Record the one that supplied the
        # discovered listening port, not whichever systemd listed first.
        selected_unit = next(
            (
                unit for unit in units
                if result.port and port_from_command(unit.exec_start) == result.port
            ),
            units[0],
        )
        result.service_unit = selected_unit.name
        result.service_command = selected_unit.exec_start

    if not result.path:
        # Said last, because a container may have supplied the folder above.
        result.notes.append(
            "no checkout found — pass --path to say where it is deployed"
        )

    result.proxy_config_path, result.proxy_body_limit = nginx_proxy_for_port(
        result.port
    )
    if result.proxy_config_path:
        result.proxy_kind = "nginx"
        result.proxy_repo_path = repo_path_for_host_config(
            result.proxy_config_path, folder
        )
        result.config_owner = (
            "repository" if result.proxy_repo_path else "operator"
        )
        result.notes.append(
            f"active nginx config {result.proxy_config_path} "
            f"({result.config_owner}-owned)"
        )
        if result.proxy_body_limit:
            result.notes.append(
                f"active nginx request-body limit {result.proxy_body_limit}"
            )

    proxied = bool(result.proxy_config_path) or any(
        looks_like_proxy(c.name) or looks_like_proxy(c.project) for c in containers
    ) or any(looks_like_proxy(unit.name) for unit in units)
    result.log_files = find_log_files(folder, proxied)
    for path in result.log_files:
        result.notes.append(f"log file {path}")

    if containers and units:
        result.runs = "docker + systemd"
    elif containers:
        result.runs = "docker compose" if containers[0].project else "docker"
    elif units:
        result.runs = f"systemd: {units[0].name}"
    elif result.log_files:
        result.runs = "process writing a log file"
    return result


def looks_like_proxy(text: str) -> bool:
    lowered = text.lower()
    return any(hint in lowered for hint in PROXY_HINTS)


def probe_unit(unit: str) -> tuple[bool, str, bool]:
    """(ok, detail, warn) for a systemd unit maajun means to read."""
    result = run_text(
        ["systemctl", "show", "-p", "LoadState", "-p", "ActiveState", unit],
        timeout=10,
    )
    if result.error:
        return False, "systemctl is not available on this host", False
    values = dict(
        line.partition("=")[::2] for line in result.stdout.splitlines() if "=" in line
    )
    if values.get("LoadState") != "loaded":
        return False, f"no such unit ({values.get('LoadState', 'unknown')})", False
    active = values.get("ActiveState", "")
    if active != "active":
        # Its journal is still readable, so this is not a failure.
        return False, f"unit is {active}", True
    return True, "", False


def probe_container(container: str) -> tuple[bool, str, bool]:
    """(ok, detail, warn) for a container maajun means to read logs from."""
    result = run_text(
        ["docker", "inspect", "-f", "{{.State.Status}}", container], timeout=10
    )
    if result.error:
        if "no such" in result.error.lower():
            return False, "no such container", False
        if "could not run docker" in result.error:
            return False, "docker is not available on this host", False
        return False, result.error, False
    state = result.stdout.strip()
    if state != "running":
        # Past logs are still readable; new errors are not coming.
        return False, f"container is {state}", True
    return True, "", False


def probe_source(kind: str, target: str) -> tuple[bool, str, bool]:
    """(ok, detail, warn) for one runtime source, by kind."""
    if kind == "journald":
        return probe_unit(target)
    if kind == "docker":
        return probe_container(target)
    return True, "", False  # files are checked by log_file_check
