import pytest

from maajun.config import DeploymentConfig
from maajun.discovery import (
    Container,
    Discovered,
    Unit,
    discover,
    find_log_files,
    list_containers,
    list_units,
    matching_containers,
    matching_units,
    name_variants,
    port_from_command,
    port_from_ports,
    probe_container,
    probe_unit,
)
from maajun.monitors.shell import CommandOutput

PS_LINE = (
    "kfl-web-1\tkfl\t/srv/kfl\t0.0.0.0:8000->8000/tcp, [::]:8000->8000/tcp\trunning"
)

UNIT_RECORD = """\
ExecStart={ path=/srv/kfl/.venv/bin/gunicorn ; argv[]=gunicorn app.wsgi -b 0.0.0.0:8000 }
WorkingDirectory=/srv/kfl
Id=kfl.service

ExecStart={ path=/usr/sbin/sshd ; argv[]=/usr/sbin/sshd -D }
WorkingDirectory=
Id=ssh.service
"""


@pytest.fixture
def route(monkeypatch):
    """Stub run_text, dispatching on the binary and its arguments."""

    def install(handler):
        def fake(cmd, *, timeout=30.0):
            return handler(cmd) or CommandOutput()

        monkeypatch.setattr("maajun.discovery.run_text", fake)

    return install


# ---------------------------------------------------------------------------
# Name matching
# ---------------------------------------------------------------------------


def test_name_variants_covers_dash_and_underscore_spellings():
    """A repo named kenyan-fantasy-league is often kenyan_fantasy_league on disk."""
    assert name_variants("Morvin-Ian/kenyan-fantasy-league") == {
        "kenyan-fantasy-league", "kenyan_fantasy_league",
    }


# ---------------------------------------------------------------------------
# docker
# ---------------------------------------------------------------------------


def test_containers_are_parsed_from_the_tab_format(route):
    route(lambda cmd: CommandOutput(stdout=PS_LINE + "\n"))

    container = list_containers()[0]

    assert container.name == "kfl-web-1"
    assert container.project == "kfl"
    assert container.working_dir == "/srv/kfl"
    assert container.state == "running"


def test_no_docker_on_the_host_is_not_an_error(route):
    route(lambda cmd: CommandOutput(error="could not run docker: not found"))
    assert list_containers() == []


def test_the_compose_working_dir_beats_a_name_match(tmp_path):
    """Two projects can both be called "api"; only one was built here."""
    folder = tmp_path / "kfl"
    folder.mkdir()
    ours = Container(name="web-1", project="kfl", working_dir=str(folder))
    theirs = Container(name="kfl-lookalike", project="other")

    matched = matching_containers([ours, theirs], "me/kfl", str(folder))

    assert [c.name for c in matched] == ["web-1"]


def test_the_name_is_the_fallback_when_the_folder_is_unknown():
    containers = [
        Container(name="kfl-web-1", project="kfl"),
        Container(name="unrelated-db", project="shop"),
    ]

    matched = matching_containers(containers, "me/kfl", "")

    assert [c.name for c in matched] == ["kfl-web-1"]


def test_the_published_host_port_is_read_from_docker():
    assert port_from_ports("0.0.0.0:8000->8000/tcp, [::]:8000->8000/tcp") == 8000
    assert port_from_ports("") == 0


# ---------------------------------------------------------------------------
# systemd
# ---------------------------------------------------------------------------


def test_units_are_parsed_from_one_show_call(route):
    """One `systemctl show '*.service'`, not one call per unit."""
    route(lambda cmd: CommandOutput(stdout=UNIT_RECORD))

    units = list_units()

    assert [u.name for u in units] == ["kfl.service", "ssh.service"]
    assert units[0].working_dir == "/srv/kfl"


def test_a_unit_matches_on_its_working_directory(tmp_path):
    folder = tmp_path / "kfl"
    folder.mkdir()
    ours = Unit(name="web.service", working_dir=str(folder))
    theirs = Unit(name="ssh.service", working_dir="/")

    assert matching_units([ours, theirs], "me/kfl", str(folder)) == [ours]


@pytest.mark.parametrize("command,expected", [
    ("gunicorn app.wsgi -b 0.0.0.0:8000", 8000),
    ("uvicorn app:api --port 9000", 9000),
    ("gunicorn --bind 127.0.0.1:8080 app", 8080),
    ("python manage.py migrate", 0),
])
def test_the_port_is_read_out_of_the_start_command(command, expected):
    assert port_from_command(command) == expected


# ---------------------------------------------------------------------------
# Log files
# ---------------------------------------------------------------------------


def test_log_files_under_the_app_folder_are_found(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "error.log").write_text("")
    (tmp_path / "app.log").write_text("")
    (tmp_path / "notes.txt").write_text("")

    found = find_log_files(str(tmp_path), proxied=False)

    assert found == [str(tmp_path / "logs" / "error.log"), str(tmp_path / "app.log")]


def test_a_missing_folder_yields_no_log_files():
    assert find_log_files("/nonexistent/place", proxied=False) == []


# ---------------------------------------------------------------------------
# discover()
# ---------------------------------------------------------------------------


def test_discover_finds_a_dockerised_app(route, tmp_path):
    folder = tmp_path / "kfl"
    (folder / "logs").mkdir(parents=True)
    (folder / "logs" / "error.log").write_text("")
    ps_line = f"kfl-web-1\tkfl\t{folder}\t0.0.0.0:8000->8000/tcp\trunning"

    def handler(cmd):
        if cmd[0] == "docker":
            return CommandOutput(stdout=ps_line + "\n")
        if cmd[0] == "systemctl":
            return CommandOutput(stdout="")
        return CommandOutput(error="no git here")

    route(handler)

    found = discover("me/kfl", DeploymentConfig(path=str(folder)))

    assert found.path == str(folder)
    assert found.port == 8000
    assert found.runs == "docker compose"
    assert found.docker_containers == ["kfl-web-1"]
    assert found.log_files == [str(folder / "logs" / "error.log")]


def test_discover_takes_the_folder_from_the_container_when_it_cannot_find_one(
    route, tmp_path
):
    """A compose container knows where it was built from — better than a guess."""
    folder = tmp_path / "kfl"
    folder.mkdir()

    def handler(cmd):
        if cmd[0] == "docker":
            return CommandOutput(stdout=f"kfl-web-1\tkfl\t{folder}\t\trunning\n")
        return CommandOutput(error="nothing here")

    route(handler)

    found = discover("me/kfl")

    assert found.path == str(folder)
    assert any("folder from container" in note for note in found.notes)


def test_discover_finds_a_systemd_app(route, tmp_path):
    def handler(cmd):
        if cmd[0] == "systemctl":
            return CommandOutput(stdout=UNIT_RECORD.replace("/srv/kfl", str(tmp_path)))
        return CommandOutput(error="nothing here")

    route(handler)

    found = discover("me/kfl", DeploymentConfig(path=str(tmp_path)))

    assert found.journald_units == ["kfl.service"]
    assert found.port == 8000
    assert found.runs == "systemd: kfl.service"


def test_discover_reports_finding_nothing(route):
    route(lambda cmd: CommandOutput(error="nothing here"))

    found = discover("me/kfl")

    assert not found.has_source()
    assert any("no checkout found" in note for note in found.notes)


def test_an_nginx_container_pulls_in_the_proxy_error_log(route, tmp_path, monkeypatch):
    """502s and upstream timeouts never reach the app's own logger."""
    monkeypatch.setattr("maajun.discovery.NGINX_LOG", str(tmp_path / "nginx.log"))
    (tmp_path / "nginx.log").write_text("")

    def handler(cmd):
        if cmd[0] == "docker":
            return CommandOutput(stdout="kfl-nginx-1\tkfl\t\t\trunning\n")
        return CommandOutput(error="nothing here")

    route(handler)

    found = discover("me/kfl")

    assert str(tmp_path / "nginx.log") in found.log_files


# ---------------------------------------------------------------------------
# Merging into config
# ---------------------------------------------------------------------------


def test_merging_adds_without_overwriting_what_was_configured():
    existing = DeploymentConfig(
        path="/opt/mine", port=9999, docker_containers=["already"]
    )
    found = Discovered(
        path="/srv/guess", port=8000, runs="docker",
        docker_containers=["already", "new"],
    )

    merged = found.merged_into(existing)

    assert (merged.path, merged.port) == ("/opt/mine", 9999)
    assert merged.runs == "docker"  # was unset, so the finding fills it in
    assert merged.docker_containers == ["already", "new"]


def test_merging_does_not_mutate_the_original():
    existing = DeploymentConfig(docker_containers=["one"])

    Discovered(docker_containers=["two"]).merged_into(existing)

    assert existing.docker_containers == ["one"]


# ---------------------------------------------------------------------------
# Probing a configured source
# ---------------------------------------------------------------------------


def test_a_missing_unit_is_a_failure(route):
    route(lambda cmd: CommandOutput(stdout="LoadState=not-found\nActiveState=inactive\n"))

    ok, detail, warn = probe_unit("kfl.service")

    assert (ok, warn) == (False, False)
    assert "no such unit" in detail


def test_an_inactive_unit_is_only_a_warning(route):
    """Its journal is still readable; the app is just not running now."""
    route(lambda cmd: CommandOutput(stdout="LoadState=loaded\nActiveState=inactive\n"))

    ok, detail, warn = probe_unit("kfl.service")

    assert (ok, warn) == (False, True)
    assert detail == "unit is inactive"


def test_a_running_unit_passes(route):
    route(lambda cmd: CommandOutput(stdout="LoadState=loaded\nActiveState=active\n"))
    assert probe_unit("kfl.service") == (True, "", False)


def test_a_missing_container_is_a_failure_and_a_stopped_one_is_a_warning(route):
    route(lambda cmd: CommandOutput(error="docker exited 1: No such container: x"))
    ok, detail, warn = probe_container("x")
    assert (ok, warn) == (False, False)
    assert detail == "no such container"

    route(lambda cmd: CommandOutput(stdout="exited\n"))
    ok, detail, warn = probe_container("x")
    assert (ok, warn) == (False, True)
    assert detail == "container is exited"
