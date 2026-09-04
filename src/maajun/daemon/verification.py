from __future__ import annotations

from dataclasses import dataclass

from maajun.vcs import CommandResult


@dataclass(frozen=True)
class VerificationCheck:
    command: str
    result: CommandResult
    unrelated: bool = False
    runtime_warning: str = ""


@dataclass(frozen=True)
class VerificationSummary:
    reproduction_command: str = ""
    reproduction_before: CommandResult | None = None
    reproduction_after: CommandResult | None = None
    checks: tuple[VerificationCheck, ...] = ()

    @property
    def configured(self) -> bool:
        return bool(self.reproduction_command or self.checks)
