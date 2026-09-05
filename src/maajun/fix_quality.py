from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from maajun.config import DeploymentConfig
from maajun.daemon.verification import VerificationSummary

PROXY_CONFIG_NAMES = {
    "caddyfile",
    "haproxy.cfg",
    "httpd.conf",
    "nginx.conf",
    "traefik.yml",
    "traefik.yaml",
}


def is_proxy_config(path: str) -> bool:
    candidate = Path(path)
    lowered = candidate.as_posix().lower()
    return (
        candidate.name.lower() in PROXY_CONFIG_NAMES
        or "/nginx/" in lowered
        or lowered.endswith((".nginx", ".nginx.conf"))
    )


def deployment_edit_problems(
    deployment: DeploymentConfig, changed_files: list[str]
) -> list[str]:
    """Reject proxy edits that are not connected to the active deployment."""
    if not deployment.proxy_config_path:
        return []
    proxy_edits = [path for path in changed_files if is_proxy_config(path)]
    if not proxy_edits:
        return []
    if deployment.proxy_repo_path:
        expected = deployment.proxy_repo_path.replace("\\", "/")
        unexpected = [path for path in proxy_edits if path.replace("\\", "/") != expected]
        if not unexpected:
            return []
    else:
        unexpected = proxy_edits
    return [
        "the active proxy configuration is "
        f"{deployment.proxy_config_path}, owned by {deployment.config_owner or 'operator'}, "
        f"but the fix edits {', '.join(unexpected)}; no deployment mapping proves "
        "those repository files reach production"
    ]


def verification_problems(
    verification: VerificationSummary | None,
) -> list[str]:
    """Owner-controlled failures that make a fix unsafe to publish."""
    if verification is None:
        return []
    problems = []
    reproduction = verification.reproduction_after
    if reproduction is not None:
        if reproduction.exit_code is None:
            problems.append(
                "the post-fix reproduction command was inconclusive: "
                f"{reproduction.output or 'no result'}"
            )
        elif reproduction.exit_code != 0:
            problems.append(
                "the configured reproduction still fails after the fix "
                f"(exit {reproduction.exit_code})"
            )
    for check in verification.checks:
        if check.unrelated:
            continue
        if check.result.exit_code is None:
            problems.append(
                f"verification command {check.command!r} was inconclusive: "
                f"{check.result.output or 'no result'}"
            )
        elif check.result.exit_code != 0:
            problems.append(
                f"verification command {check.command!r} still fails after "
                f"the repair round (exit {check.result.exit_code})"
            )
    return problems


@dataclass(frozen=True)
class QualityReview:
    passed: bool
    explanation: str
    issue_title: str = ""


def parse_quality_review(answer: str) -> QualityReview:
    lines = (answer or "").strip().splitlines()
    if not lines:
        return QualityReview(False, "the independent review returned no verdict")
    verdict = lines[0].strip().strip("`*# ").casefold()
    detail_lines = lines[1:]
    issue_title = ""
    for index, line in enumerate(detail_lines):
        if line.strip().casefold().startswith("issue title:"):
            issue_title = line.partition(":")[2].strip().strip("`*# ")
            detail_lines = [*detail_lines[:index], *detail_lines[index + 1:]]
            break
    explanation = "\n".join(detail_lines).strip()
    if verdict == "pass":
        return QualityReview(True, explanation)
    if verdict == "block":
        return QualityReview(
            False,
            explanation or "the independent review blocked it",
            issue_title,
        )
    return QualityReview(False, "the independent review returned an unreadable verdict")
