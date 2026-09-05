"""Resolve a saved monitoring mode into one incident's editing policy."""

from __future__ import annotations

from dataclasses import dataclass

from maajun.config import RepoConfig
from maajun.verification_runtime import verification_runtime_mismatch


@dataclass(frozen=True)
class ModeDecision:
    requested: str
    effective: str
    reasons: tuple[str, ...] = ()

    @property
    def applies_fix(self) -> bool:
        return self.effective == "fix"


def decide_run_mode(repo_config: RepoConfig) -> ModeDecision:
    """Choose whether this run may edit, without changing saved configuration."""
    if repo_config.mode != "automatic":
        return ModeDecision(repo_config.mode, repo_config.mode)

    deployment = repo_config.deployment
    commands = repo_config.post_fix_commands()
    reasons = []
    if not (deployment.path or deployment.runs or deployment.service_command):
        reasons.append("no active deployment identity is recorded")
    if not repo_config.reproduction_command:
        reasons.append("no before/after reproduction command is configured")
    if not commands:
        reasons.append("no post-fix verification command is configured")

    runtime_commands = [repo_config.reproduction_command, *commands]
    mismatches = [
        verification_runtime_mismatch(command, deployment)
        for command in runtime_commands if command
    ]
    mismatches = [message for message in mismatches if message]
    if mismatches:
        reasons.append(mismatches[0])

    if reasons:
        return ModeDecision("automatic", "suggest", tuple(reasons))
    return ModeDecision(
        "automatic",
        "fix",
        (
            "deployment identity, targeted reproduction, and post-fix "
            "verification are configured",
        ),
    )
