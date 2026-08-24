"""Fixtures for the daemon tests: a bare remote, and a daemon wired to it."""

import subprocess

import pytest

from daemon.fakes import FakeAgent, FakeGitHub, git
from maajun.config import (
    Config,
    DaemonConfig,
    GitHubConfig,
    MonitorConfig,
    RepoConfig,
)
from maajun.daemon.core import Daemon
from maajun.daemon.store import IncidentStore
from maajun.monitors import LogFileMonitor
from maajun.vcs import GitWorkspace


@pytest.fixture
def remote(tmp_path):
    """A bare repo standing in for GitHub, seeded with one commit on main."""
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)],
        check=True, capture_output=True,
    )
    seed = tmp_path / "seed"
    subprocess.run(
        ["git", "init", "-b", "main", str(seed)], check=True, capture_output=True
    )
    (seed / "main.py").write_text("items = []\n")
    git("add", "-A", cwd=seed)
    git("commit", "-m", "initial", cwd=seed)
    git("remote", "add", "origin", str(bare), cwd=seed)
    git("push", "origin", "main", cwd=seed)
    return bare


@pytest.fixture
def setup(tmp_path, remote):
    logfile = tmp_path / "app.log"
    logfile.write_text("")

    repo_config = RepoConfig(repo="owner/name", base_branch="main", mode="suggest")
    config = Config(
        github=GitHubConfig(repos=[repo_config]),
        monitor=MonitorConfig(log_files=[str(logfile)], poll_interval=1),
        daemon=DaemonConfig(workdir=str(tmp_path / "work")),
    )
    workspace = GitWorkspace(tmp_path / "work" / "ws", "owner/name", remote_url=str(remote))
    store = IncidentStore(tmp_path / "work" / "incidents.db")
    agent = FakeAgent()
    github = FakeGitHub()
    monitor = LogFileMonitor(logfile)
    daemon = Daemon(
        config,
        monitors=[monitor],
        store=store,
        workspaces={"owner/name": workspace},
        monitor_to_repo={id(monitor): repo_config},
        github=github,
        agent_factory_for_repo=lambda rc, ws: lambda: agent,
    )
    return daemon, logfile, agent, github, store, remote
