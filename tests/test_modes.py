from maajun.config import DeploymentConfig, RepoConfig
from maajun.modes import decide_run_mode


def test_automatic_mode_stays_read_only_without_owner_evidence():
    decision = decide_run_mode(RepoConfig(repo="owner/app", mode="automatic"))

    assert decision.effective == "suggest"
    assert "deployment identity" in decision.reasons[0]
    assert any("reproduction" in reason for reason in decision.reasons)
    assert any("post-fix" in reason for reason in decision.reasons)


def test_automatic_mode_attempts_a_fix_with_complete_evidence():
    repo = RepoConfig(
        repo="owner/app",
        mode="automatic",
        reproduction_command="pytest -q tests/test_bug.py",
        verification_commands=["pytest -q", "ruff check ."],
        deployment=DeploymentConfig(
            path="/srv/app",
            service_command="/srv/app/.venv/bin/python -m app",
        ),
    )

    decision = decide_run_mode(repo)

    assert decision.effective == "fix"
    assert decision.applies_fix is True


def test_automatic_mode_rejects_a_different_verification_runtime():
    repo = RepoConfig(
        repo="owner/app",
        mode="automatic",
        reproduction_command="/srv/app/.venv/bin/pytest tests/test_bug.py",
        verification_commands=["/tmp/checks/.venv/bin/pytest -q"],
        deployment=DeploymentConfig(
            path="/srv/app",
            service_command="/srv/app/.venv/bin/python -m app",
        ),
    )

    decision = decide_run_mode(repo)

    assert decision.effective == "suggest"
    assert any("active service uses /srv/app/.venv" in item for item in decision.reasons)


def test_explicit_modes_are_never_reinterpreted():
    assert decide_run_mode(RepoConfig(mode="suggest")).effective == "suggest"
    assert decide_run_mode(RepoConfig(mode="fix")).effective == "fix"
