"""Shared proxy errors must retain ownership and a stable outage identity."""

import pytest

from maajun.config import Config, DeploymentConfig, GitHubConfig, RepoConfig
from maajun.daemon.wiring import build_monitors
from maajun.monitors import LogFileMonitor, fingerprint


def refusal(server="api.example.com", port=8002, path="/dashboard", client="203.0.113.2"):
    return (
        "2026/10/11 05:46:36 [error] 123#123: *45 connect() failed "
        "(111: Connection refused) while connecting to upstream, "
        f'client: {client}, server: {server}, request: "GET {path} HTTP/1.1", '
        f'upstream: "http://127.0.0.1:{port}{path}", host: "attacker.example.com"'
    )


def test_upstream_outage_groups_paths_and_volatile_metadata():
    first = refusal(path="/dashboard")
    second = refusal(path="/.env?secret=value", client="198.51.100.4").replace(
        "2026/10/11 05:46:36 [error] 123#123: *45",
        "2026/10/12 07:12:41 [error] 789#789: *999",
    )
    assert fingerprint(first) == fingerprint(second)


@pytest.mark.parametrize(
    "other",
    [
        refusal(server="writer.example.com"),
        refusal(port=8003),
        refusal().replace("127.0.0.1", "127.0.0.2"),
        refusal().replace("Connection refused", "Permission denied"),
        refusal().replace("http://", "https://"),
    ],
)
def test_distinct_upstream_outages_are_not_merged(other):
    assert fingerprint(refusal()) != fingerprint(other)


def test_non_connection_errors_keep_request_paths():
    one = refusal().replace("connect() failed", "recv() failed")
    two = one.replace("/dashboard", "/upload")
    assert fingerprint(one) != fingerprint(two)


async def test_shared_file_only_emits_configured_server_and_application_errors(tmp_path):
    path = tmp_path / "nginx.log"
    path.write_text("")
    monitor = LogFileMonitor(path, proxy_server_names=["API.EXAMPLE.COM."])
    path.write_text(
        "\n".join(
            [
                refusal(server="writer.example.com"),
                refusal(),
                refusal().replace("server: api.example.com, ", ""),
                "ERROR application database failed",
                "INFO next",
            ]
        )
        + "\n"
    )
    events = await monitor.poll()
    events += await monitor.flush()
    assert len(events) == 2
    assert "server: api.example.com" in events[0].details
    assert events[1].message == "ERROR application database failed"
    assert "203.0.113.2" not in events[0].details


async def test_shared_log_routes_to_correct_repo_and_roundtrips_config(tmp_path, fake_keyring):
    path = tmp_path / "nginx.log"
    path.write_text("")
    config = Config(
        github=GitHubConfig(
            repos=[
                RepoConfig(
                    repo="owner/api",
                    deployment=DeploymentConfig(
                        log_files=[str(path)],
                        proxy_server_names=["api.example.com"],
                    ),
                ),
                RepoConfig(
                    repo="owner/writer",
                    deployment=DeploymentConfig(
                        log_files=[str(path)],
                        proxy_server_names=["writer.example.com"],
                    ),
                ),
            ]
        )
    )
    config.daemon.workdir = str(tmp_path / "workdir")
    config_path = tmp_path / "config.toml"
    config.save(config_path)
    loaded = Config.load(config_path)
    assert loaded.github.repos[0].deployment.proxy_server_names == ["api.example.com"]
    monitors, owners = build_monitors(loaded, loaded.github.repos)
    path.write_text(refusal() + "\n" + refusal(server="writer.example.com") + "\n")
    for monitor in monitors:
        events = await monitor.poll()
        events += await monitor.flush()
        assert len(events) == 1
        expected = (
            "api.example.com" if owners[id(monitor)].repo == "owner/api" else "writer.example.com"
        )
        assert f"server: {expected}" in events[0].details
    assert monitors[0].cursor_file != monitors[1].cursor_file
    restarted, _ = build_monitors(loaded, loaded.github.repos)
    for monitor in restarted:
        assert await monitor.poll() == []


async def test_unscoped_legacy_monitor_keeps_all_servers(tmp_path):
    path = tmp_path / "nginx.log"
    path.write_text("")
    monitor = LogFileMonitor(path)
    path.write_text(refusal() + "\n" + refusal(server="writer.example.com") + "\n")
    assert len(await monitor.poll()) == 2


def test_repeated_refusal_updates_existing_incident_instead_of_refiling(tmp_path):
    from maajun.daemon.store import IncidentStore
    from maajun.monitors import ErrorEvent

    store = IncidentStore(tmp_path / "incidents.db")
    try:
        first = ErrorEvent(
            source="logfile:nginx",
            message="upstream refused",
            details=refusal(path="/dashboard"),
            repo="owner/api",
        )
        assert store.record(first)
        store.mark_processed(first.fingerprint, repo=first.repo, branch="", pr_url="existing")
        repeated = ErrorEvent(
            source="logfile:nginx",
            message="upstream refused",
            details=refusal(path="/.env"),
            repo="owner/api",
        )
        assert not store.record(repeated)
        assert store.get(first.fingerprint, first.repo)["count"] == 2
        different = ErrorEvent(
            source="logfile:nginx",
            message="upstream refused",
            details=refusal(port=8003),
            repo="owner/api",
        )
        assert store.record(different)
    finally:
        store.close()


def test_proxy_scope_is_saved_when_logs_use_legacy_repo_field(tmp_path):
    config = Config(
        github=GitHubConfig(
            repos=[
                RepoConfig(
                    repo="owner/api",
                    log_files=["/var/log/nginx/error.log"],
                    deployment=DeploymentConfig(proxy_server_names=["api.example.com"]),
                )
            ]
        )
    )
    path = tmp_path / "config.toml"
    config.save(path)
    assert Config.load(path).github.repos[0].deployment.proxy_server_names == ["api.example.com"]


async def test_shared_cursor_migrates_normalized_file_alias_without_losing_unread_errors(
    tmp_path,
    fake_keyring,
):
    path = tmp_path / "nginx.log"
    path.write_text("")
    raw_alias = str(tmp_path) + "/./nginx.log"
    cursor_dir = tmp_path / "workdir" / "cursors"
    old = LogFileMonitor(raw_alias, cursor_dir=cursor_dir)
    assert await old.poll() == []
    assert old.cursor_file.exists()
    path.write_text(refusal() + "\n" + refusal(server="writer.example.com") + "\n")
    config = Config(
        github=GitHubConfig(
            repos=[
                RepoConfig(
                    repo="owner/api",
                    log_files=[raw_alias, str(path)],
                    deployment=DeploymentConfig(proxy_server_names=["api.example.com"]),
                ),
                RepoConfig(
                    repo="owner/writer",
                    log_files=[str(tmp_path) + "//nginx.log"],
                    deployment=DeploymentConfig(proxy_server_names=["writer.example.com"]),
                ),
            ]
        )
    )
    config.daemon.workdir = str(tmp_path / "workdir")
    monitors, _ = build_monitors(config, config.github.repos)
    assert len(monitors) == 2
    for monitor in monitors:
        assert monitor.cursor_file.exists()
        assert len(await monitor.poll()) == 1
