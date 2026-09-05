from maajun.config import DeploymentConfig
from maajun.daemon.fix_quality import (
    deployment_edit_problems,
    parse_quality_review,
    verification_problems,
)
from maajun.daemon.prompts import QUALITY_CORRECTION_SUFFIX, QUALITY_REVIEW_PROMPT
from maajun.daemon.verification import VerificationCheck, VerificationSummary
from maajun.vcs import CommandResult


def test_inactive_repository_proxy_config_is_blocked():
    deployment = DeploymentConfig(
        proxy_kind="nginx",
        proxy_config_path="/etc/nginx/sites-available/api.example.com",
        config_owner="operator",
    )

    problems = deployment_edit_problems(deployment, ["nginx.conf", "tests/test_nginx.py"])

    assert "active proxy configuration" in problems[0]
    assert "nginx.conf" in problems[0]


def test_mapped_repository_proxy_config_is_allowed():
    deployment = DeploymentConfig(
        proxy_config_path="/srv/app/deploy/nginx.conf",
        proxy_repo_path="deploy/nginx.conf",
        config_owner="repository",
    )

    assert deployment_edit_problems(deployment, ["deploy/nginx.conf"]) == []


def test_quality_review_fails_closed_on_an_unreadable_verdict():
    assert parse_quality_review("looks fine").passed is False
    assert parse_quality_review("PASS\nDeployment mapping is proven.").passed is True


def test_blocked_quality_review_keeps_an_actionable_issue_title():
    review = parse_quality_review(
        "BLOCK\n"
        "Issue title: Raise the active nginx request-body limit\n"
        "The proxy still rejects the request."
    )

    assert review.passed is False
    assert review.issue_title == "Raise the active nginx request-body limit"
    assert review.explanation == "The proxy still rejects the request."


def test_related_verification_failure_blocks_publication():
    summary = VerificationSummary(checks=(VerificationCheck(
        "ruff check .", CommandResult(1, "I001 import order"), unrelated=False
    ),))

    problems = verification_problems(summary)

    assert "ruff check ." in problems[0]
    assert "still fails" in problems[0]


def test_unrelated_verification_failure_does_not_block_publication():
    summary = VerificationSummary(checks=(VerificationCheck(
        "pytest -q", CommandResult(1, "legacy failure"), unrelated=True
    ),))

    assert verification_problems(summary) == []


def test_still_failing_reproduction_blocks_publication():
    summary = VerificationSummary(
        reproduction_command="pytest -q tests/test_upload.py",
        reproduction_after=CommandResult(1, "upload still fails"),
    )

    assert "reproduction still fails" in verification_problems(summary)[0]


def test_quality_review_names_the_upload_and_test_safety_boundaries():
    prompt = QUALITY_REVIEW_PROMPT.casefold()

    assert "entire untrusted payload" in prompt
    assert "limit plus one" in prompt
    assert "live/persistent storage" in prompt
    assert "syntax, import-order, lint, type" in prompt
    assert "owner-controlled verification" in prompt


def test_quality_correction_keeps_the_same_safety_boundaries():
    prompt = QUALITY_CORRECTION_SUFFIX.casefold()

    assert "bounded read or streaming" in prompt
    assert "temporary or mocked storage" in prompt
    assert "verification failures" in prompt
