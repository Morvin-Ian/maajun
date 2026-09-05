from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from maajun.agent.core import accumulate_usage
from maajun.config import RepoConfig
from maajun.daemon import reports
from maajun.daemon.fix_quality import (
    QualityReview,
    deployment_edit_problems,
    parse_quality_review,
    verification_problems,
)
from maajun.daemon.followups import (
    MAX_FOLLOW_UP_ISSUES,
    FollowUpTask,
    InvalidFollowUp,
    parse_follow_ups,
)
from maajun.daemon.modes import decide_run_mode
from maajun.daemon.prompts import (
    AUTOMATIC_MODE_SECTION,
    DEPLOYMENT_SECTION,
    FAILED_VERIFICATION_SUFFIX,
    FIX_PROMPT_SUFFIX,
    FOLLOW_UP_RETRY_SUFFIX,
    QUALITY_CORRECTION_SUFFIX,
    QUALITY_REVIEW_PROMPT,
    REGRESSION_SECTION,
    RETRY_SUFFIX,
    UNAPPLIED_FIX_SUFFIX,
)
from maajun.daemon.publication import choose_runtime_artifact_target
from maajun.daemon.reports import headline_problem, report_problem
from maajun.daemon.store import (
    ARTIFACT_IGNORED,
    ARTIFACT_ISSUE,
    ARTIFACT_PR,
    ARTIFACT_REPORT,
)
from maajun.daemon.verification import VerificationCheck, VerificationSummary
from maajun.discovery.runtime_env import verification_runtime_mismatch
from maajun.discovery.toolchain import Formatter, detect_formatters
from maajun.monitors import ErrorEvent
from maajun.providers.pricing import extract_usage
from maajun.utils import truncate_tail
from maajun.vcs import CommandResult, GitError, GitWorkspace

if TYPE_CHECKING:  # imported for typing only; core imports this module
    from maajun.daemon.core import Daemon, ProgressCallback

log = logging.getLogger(__name__)

# How much of a failing test run is pasted back for the repair round, taken
# from the end — a runner prints what failed last.
MAX_TEST_OUTPUT_IN_PROMPT = 8000

# A formatter runs over the whole checkout; long enough for a large repo,
# short enough not to stall the incident behind a missing binary.
FORMAT_TIMEOUT = 120


@dataclass(frozen=True)
class Plan:
    """What kind of run this is: what to call things, and what to skip.

    An incident and a manual report take the same pipeline and differ only in
    these, so they are one value rather than a dozen keyword arguments.
    """

    branch: str
    prompt: str
    subject_fallback: str
    commit_prefix: str
    dry_run_header: str
    dry_run_extra: tuple[str, ...] = ()
    forget_on_dry_run: bool = False
    blame_deploy: bool = False
    dry_run: bool = False
    sync_on_dry_run: bool = False
    issue_fallback: bool = True
    closes_issue_url: str = ""
    # True only for passively observed log events. Manual reports and issue
    # promotions are already deliberate owner publication decisions.
    runtime_event: bool = False


@dataclass
class Investigation:
    """Analyze one incident and publish it. `run` is the whole story.

    The daemon owns the shared services — the store, the GitHub client, the
    config — and this borrows them for the length of one incident.
    """

    daemon: Daemon
    event: ErrorEvent
    repo_config: RepoConfig
    workspace: GitWorkspace
    plan: Plan
    progress: ProgressCallback

    agent: object | None = None
    report: str = ""
    model: str | None = None
    # Summed across asks: chat() reports one call, and the first is the
    # expensive one — the cap and the recorded cost must see both.
    spent: dict[str, int] = field(default_factory=dict)
    usage: tuple[int, int, float] = (0, 0, 0.0)
    title: str = ""
    commit_message: str = ""
    previous: dict | None = None
    # What this run produced, for the daemon to report to the CLI.
    artifact_kind: str | None = None
    ignored_reason: str = ""
    follow_up_source: str = ""
    reproduction_before: CommandResult | None = None
    # Formatters the untouched clone already satisfied, so rewriting with them
    # can only reach lines this fix introduced.
    format_baseline: tuple[Formatter, ...] = ()
    quality_block: str = ""
    quality_issue_title: str = ""
    route_quality_to_infrastructure: bool = False
    publication_block: str = ""
    run_mode: str = field(init=False, default="suggest")
    mode_reasons: tuple[str, ...] = field(init=False, default=())

    def __post_init__(self) -> None:
        decision = decide_run_mode(self.repo_config)
        self.run_mode = decision.effective
        self.mode_reasons = decision.reasons
        self.opens_pull_request = self.run_mode == "fix"
        # A dry run and local mode never branch, so there is no diff to want.
        self.applies_a_fix = (
            self.opens_pull_request
            and not self.plan.dry_run
            and not self.daemon.local_mode
        )

    # -- the run ------------------------------------------------------------

    async def run(self) -> str:
        """Returns the issue or PR URL, or "" when nothing was published."""
        await self.prepare()
        await self.reproduce_before_edit()
        await self.record_format_baseline()
        prompt = await self.build_prompt()
        self.progress("Analyzing with AI")
        agent_repo_config = self.repo_config
        if self.repo_config.mode != self.run_mode:
            agent_repo_config = self.repo_config.model_copy(deep=True)
            agent_repo_config.mode = self.run_mode
        if (
            self.plan.dry_run
            and self.plan.sync_on_dry_run
            and self.run_mode == "fix"
        ):
            # Dry runs may refresh the local clone for current evidence, but
            # they never grant the agent permission to edit it.
            agent_repo_config = self.repo_config.model_copy(deep=True)
            agent_repo_config.mode = "suggest"
        self.agent = self.daemon.agent_factory_for_repo(
            agent_repo_config, self.workspace
        )()
        try:
            await self.investigate(prompt)
            return await self.publish()
        finally:
            # One agent per incident; a watch run would leak their pools. It
            # lives past publishing because the repair round needs it.
            await self.agent.aclose()

    async def prepare(self) -> None:
        """Sync the clone, and branch it when there will be a diff."""
        if self.daemon.local_mode:
            return
        if self.plan.dry_run and not self.plan.sync_on_dry_run:
            return
        self.progress("Preparing workspace")
        # The agent reads code from the clone either way; only the effective
        # fix path needs a branch.
        await self.workspace.sync(self.repo_config.base_branch)
        if self.opens_pull_request and not self.plan.dry_run:
            await self.workspace.create_branch(
                self.plan.branch, self.repo_config.base_branch
            )

    async def build_prompt(self) -> str:
        """The plan's prompt, plus what this repo and this history add to it."""
        prompt = self.plan.prompt
        self.previous = self.previous_artifact()
        if self.previous:
            prompt += REGRESSION_SECTION.format(
                reported=self.previous["when"],
                url=self.previous["url"] or "no link recorded",
            )
        # After sync: there is no history to read until the clone exists.
        if self.plan.blame_deploy:
            prompt += await self.daemon.recent_commits_section(
                self.repo_config, self.workspace
            )
        prompt += deployment_section(
            self.repo_config, self.daemon.monitors_for(self.repo_config)
        )
        if self.repo_config.mode == "automatic":
            prompt += AUTOMATIC_MODE_SECTION.format(
                effective=self.run_mode,
                reasons="\n".join(f"- {reason}" for reason in self.mode_reasons),
            )
        if self.opens_pull_request and not (
            self.plan.dry_run and self.plan.sync_on_dry_run
        ):
            prompt += FIX_PROMPT_SUFFIX.format(workspace=self.workspace.path)
        return prompt

    def previous_artifact(self) -> dict | None:
        """What this incident produced last time, if it is one that came back.

        Read before publishing, because publishing overwrites the row it is
        stored in.
        """
        row = self.daemon.store.get(self.event.fingerprint, self.event.repo)
        if not row or not row["reopened_at"]:
            return None
        return {"url": row["previous_url"], "when": row["reopened_at"][:10]}

    async def investigate(self, prompt: str) -> None:
        """Ask until there is a report worth publishing, and a diff behind it.

        Everything billed is banked even when a round raises: the requests
        were made either way.
        """
        try:
            response = await self.ask(prompt)
            self.report = response.content.strip()
            self.model = getattr(response, "model", None)
            problem = report_problem(self.report) or headline_problem(self.report)
            if problem:
                # One more round rather than filing an empty artifact: the
                # usual cause is a model that answered conversationally.
                log.info("re-asking for a usable report: %s", problem)
                self.progress("Re-asking for the report")
                response = await self.ask(RETRY_SUFFIX.format(problem=problem))
                self.report = response.content.strip()
            if self.applies_a_fix and not await self.code_changes():
                await self.secure_the_edit()
        except BaseException:
            self.bank_spend()
            raise

    async def ask(self, message: str):
        """One turn, with what it cost added to the run's total."""
        response = await self.agent.chat(message)
        accumulate_usage(self.spent, response.usage)
        return response

    async def secure_the_edit(self) -> None:
        """Get fix mode's change onto the tree, cheapest way first.

        The free attempt first: a model that described the change usually left
        the patch in the report, and `git apply` costs nothing. Insisting is a
        whole round with the tool history resent — the dearest ask in the run.
        """
        if not await self.apply_reported_diff():
            await self.insist_on_the_edit()

    # -- publishing ---------------------------------------------------------

    async def publish(self) -> str:
        """File the report as whatever this run earned. Returns its URL."""
        self.usage = extract_usage(self.spent, self.model)
        if self.repo_config.mode == "automatic":
            self.report = reports.automatic_mode_report(
                self.report,
                effective_mode=self.run_mode,
                reasons=self.mode_reasons,
            )
        # Titled from the report, so what the issue is called and what it says
        # to fix are the same thing.
        self.title = reports.artifact_title(self.report, self.plan.subject_fallback)
        self.commit_message = reports.commit_subject(
            self.report, self.plan.subject_fallback, self.plan.commit_prefix
        )

        if reports.verdict(self.report) == reports.BY_DESIGN:
            if self.plan.dry_run or self.plan.issue_fallback:
                return self.close_as_intended()
            return self.save_local_report()

        problem = report_problem(self.report)
        if problem and not self.plan.dry_run:
            # Nothing is published: an issue or PR with no findings hides
            # that the run failed. What it cost is still banked, though.
            self.bank_spend()
            raise RuntimeError(f"the analysis produced no usable report ({problem})")

        if self.plan.dry_run:
            return self.print_dry_run()
        if self.daemon.local_mode:
            return self.save_local_report()

        if self.opens_pull_request and not await self.has_a_diff():
            # Asked twice and still nothing to merge, so the finding is only
            # a finding: a PR with no diff looks like a fix until you open it.
            log.info(
                "fix mode changed no code for fp=%s in repo=%s; filing the "
                "analysis as an issue instead of an empty pull request",
                self.event.fingerprint, self.repo_config.repo,
            )
            self.opens_pull_request = False
            if not self.plan.issue_fallback:
                return self.save_local_report()

        if self.opens_pull_request and not await self.runtime_pr_is_allowed():
            self.opens_pull_request = False
            self.report = reports.withheld_runtime_report(
                self.report, self.publication_block, drafted=True
            )

        if self.opens_pull_request:
            url = await self.open_pull_request()
            if url:
                self.record(url, self.plan.branch, ARTIFACT_PR)
                return url
            # The gate inside open_pull_request tripped: nothing was pushed
            # and nothing is open, so the analysis is still unpublished.
            self.opens_pull_request = False

        url = await self.file_issue()
        if not url:
            return self.save_local_report()
        self.record(url, "", ARTIFACT_ISSUE)
        return url

    async def runtime_pr_is_allowed(self) -> bool:
        if not self.plan.runtime_event:
            return True
        decision = await choose_runtime_artifact_target(
            self.daemon.github,
            self.repo_config.repo,
            allow_public=self.repo_config.allow_public_runtime_artifacts,
        )
        if decision.allowed:
            return True
        self.publication_block = decision.reason
        return False

    async def code_changes(self) -> list[str]:
        """The files this run changed that a reviewer would call a fix.

        `git status` alone answers a different question — whether the tree is
        dirty — and the report file makes it dirty by itself.
        """
        return reports.code_changes(await self.workspace.changed_files())

    async def has_a_diff(self) -> bool:
        """Whether there is something to review.

        Asked before the report file is written, and blind to it either way:
        a branch carrying only `docs/incidents/<fp>.md` is an analysis.
        """
        if await self.code_changes():
            return True
        # The insisted report gets the same free attempt: asked point blank, a
        # model that still only describes it hands the patch over while doing so.
        return await self.apply_reported_diff()

    async def open_pull_request(self) -> str:
        """Verify, repair what this change broke, and open the PR.

        Returns "" when the commit turned out to carry no fix, in which case
        nothing was pushed and the caller files the analysis as an issue.
        """
        follow_ups = await self.prepare_follow_ups()
        await self.apply_project_formatting()
        verification = await self.verified_fix()
        if self.should_review_fix():
            verification = await self.enforce_fix_quality(verification)
        if self.quality_block:
            self.restore_follow_up_source()
            self.withhold_fix()
            return ""
        follow_ups = self.finalize_follow_ups(follow_ups)
        self.progress("Opening PR")
        # Filed apart: a reviewer should not have to work out which lines of
        # the report the diff already covers.
        reports.write_report_file(
            self.workspace.path / reports.INCIDENT_REPORT_DIR, self.event, self.report
        )
        await self.workspace.commit_all(self.commit_message)
        if not await self.committed_code_changes():
            if self.follow_up_source:
                self.report = (
                    self.report.rstrip()
                    + "\n\n## Follow-up\n"
                    + self.follow_up_source.strip()
                    + "\n"
                )
            return ""
        # Past the gate, so the split is what gets published; before it, the
        # whole report — follow-up included — is still what the issue says.
        await self.workspace.push(self.plan.branch)
        url = await self.daemon.github.create_pull_request(
            self.repo_config.repo,
            head=self.plan.branch,
            base=self.repo_config.base_branch,
            title=self.title,
            body=reports.pr_body(
                self.repo_config, self.event, self.report, verification,
                previous_url=self.previous["url"] if self.previous else "",
                closes_issue_url=self.plan.closes_issue_url,
            ),
        )
        await self.file_follow_ups(follow_ups, url)
        return url

    async def committed_code_changes(self) -> list[str]:
        """The fix as the pull request's Files tab would show it.

        The last gate before a branch leaves the machine, and the only one
        that reads the commit rather than the working tree. Everything above
        decides whether a fix was made; this decides whether one is being
        published, so a path that reaches here with nothing to merge files an
        issue instead of pushing a branch nobody can review.
        """
        changed = reports.code_changes(
            await self.workspace.committed_files(self.repo_config.base_branch)
        )
        if not changed:
            log.warning(
                "the commit for fp=%s in repo=%s changes nothing but the "
                "report; not pushing it, and filing the analysis as an issue",
                self.event.fingerprint, self.repo_config.repo,
            )
        return changed

    async def file_issue(self) -> str:
        infrastructure_repo = (
            self.repo_config.deployment.infra_repo
            if self.route_quality_to_infrastructure else ""
        )
        target_repo = infrastructure_repo or self.repo_config.repo
        if infrastructure_repo:
            self.report = reports.infrastructure_route_report(
                self.report,
                application_repo=self.repo_config.repo,
                infrastructure_repo=infrastructure_repo,
            )
        if self.plan.runtime_event:
            decision = await choose_runtime_artifact_target(
                self.daemon.github,
                target_repo,
                allow_public=self.repo_config.allow_public_runtime_artifacts,
                fallback_repo=self.repo_config.runtime_artifact_repo,
            )
            if not decision.allowed:
                drafted = bool(self.publication_block)
                self.publication_block = decision.reason
                self.report = reports.withheld_runtime_report(
                    self.report,
                    decision.reason,
                    drafted=drafted,
                )
                return ""
            target_repo = decision.repository
            if decision.reason:
                self.report = reports.withheld_runtime_report(
                    self.report,
                    decision.reason,
                    drafted=bool(self.publication_block),
                )
        self.progress("Filing issue")
        return await self.daemon.github.create_issue(
            target_repo,
            title=self.title,
            body=reports.issue_body(
                self.event, self.report,
                previous_url=self.previous["url"] if self.previous else "",
                unfixed=(
                    self.run_mode == "fix"
                    and not self.quality_block
                    and not self.publication_block
                ),
                withheld=bool(self.quality_block),
            ),
        )

    def should_review_fix(self) -> bool:
        """Review fixes whose applicability depends on a recorded deployment."""
        return (
            self.applies_a_fix
            and self.repo_config.deployment.describes_a_deployment()
        )

    async def enforce_fix_quality(
        self, verification: VerificationSummary | None
    ) -> VerificationSummary | None:
        """Review once, correct once, then reverify and fail closed."""
        if not await self.code_changes():
            return verification
        first = await self.review_fix_quality(verification)
        if first.passed:
            return verification
        try:
            response = await self.ask(
                QUALITY_CORRECTION_SUFFIX.format(problems=first.explanation)
            )
        except Exception as error:
            log.exception("fix quality correction failed")
            reason = (
                f"{first.explanation}\n"
                f"the one allowed correction could not run: {error}"
            ).strip()
            await self.mark_quality_block(reason, first.issue_title)
            return verification
        self.keep_if_usable(response.content.strip())
        await self.apply_project_formatting()
        corrected = await self.verify()
        if corrected is not None:
            corrected, _ = await self.classify_failures(corrected)
        final = await self.review_fix_quality(corrected)
        if final.passed:
            return corrected
        await self.mark_quality_block(final.explanation, final.issue_title)
        return corrected

    async def mark_quality_block(self, explanation: str, issue_title: str) -> None:
        """Record a block and whether an unmapped infrastructure edit caused it."""
        self.quality_block = explanation
        self.quality_issue_title = issue_title
        changed = await self.code_changes()
        self.route_quality_to_infrastructure = bool(
            deployment_edit_problems(self.repo_config.deployment, changed)
        )

    async def review_fix_quality(
        self, verification: VerificationSummary | None
    ) -> QualityReview:
        changed = await self.code_changes()
        deterministic = [
            *deployment_edit_problems(self.repo_config.deployment, changed),
            *verification_problems(verification),
        ]
        diff_reader = getattr(self.workspace, "working_diff", None)
        diff = await diff_reader() if diff_reader else "(diff unavailable)"
        prompt = QUALITY_REVIEW_PROMPT.format(
            workspace=self.workspace.path,
            deployment=deployment_section(
                self.repo_config, self.daemon.monitors_for(self.repo_config)
            ) or "No deployment evidence recorded.",
            problems="\n".join(f"- {item}" for item in deterministic) or "None.",
            verification=truncate_tail(
                reports.verification_section(self.repo_config, verification),
                12_000,
                "… (earlier verification output truncated)\n",
            ),
            report=self.report,
            diff=truncate_tail(diff, 30_000, "… (earlier diff truncated)\n"),
        )
        read_only = self.repo_config.model_copy(deep=True)
        read_only.mode = "suggest"
        critic = self.daemon.agent_factory_for_repo(read_only, self.workspace)()
        critic.max_rounds = 1
        try:
            response = await critic.chat(prompt)
            accumulate_usage(self.spent, response.usage)
        except Exception as error:
            log.exception("independent fix review failed")
            explanation = f"independent review could not run: {error}"
            if deterministic:
                explanation = "\n".join([*deterministic, explanation])
            return parse_quality_review(f"BLOCK\n{explanation}")
        finally:
            await critic.aclose()
        review = parse_quality_review(response.content)
        if deterministic:
            reasons = "\n".join([*deterministic, review.explanation])
            return QualityReview(False, reasons.strip(), review.issue_title)
        return review

    def restore_follow_up_source(self) -> None:
        """Put deferred work back when no fix PR will be published."""
        if not self.follow_up_source:
            return
        self.report = (
            self.report.rstrip()
            + "\n\n## Follow-up\n"
            + self.follow_up_source.strip()
            + "\n"
        )

    def withhold_fix(self) -> None:
        """Turn a rejected local draft into an accurately titled issue."""
        self.opens_pull_request = False
        self.report = reports.withheld_fix_report(
            self.report, self.quality_block
        )
        issue_title = self.quality_issue_title or blocked_fix_title(
            self.event, self.repo_config
        )
        self.title = reports.artifact_title(
            f"# {issue_title}", self.plan.subject_fallback
        )

    def record(self, url: str, branch: str, kind: str) -> None:
        prompt_tokens, completion_tokens, cost = self.usage
        self.artifact_kind = kind
        self.daemon.store.mark_processed(
            self.event.fingerprint,
            self.event.repo,
            branch=branch,
            pr_url=url,
            cost_usd=cost,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            report_text=self.report,
            artifact_kind=kind,
        )
        log.info(
            "opened %s %s for fp=%s in repo=%s (cost: $%.4f, tokens: %d/%d)",
            "PR" if kind == ARTIFACT_PR else "issue",
            url, self.event.fingerprint, self.repo_config.repo, cost,
            prompt_tokens, completion_tokens,
        )

    # -- the rounds that fix mode may buy -----------------------------------

    async def insist_on_the_edit(self) -> None:
        """Ask once more for the edit fix mode was supposed to make.

        A report that only describes the change opens a pull request with
        nothing to review. The usual cause is the escape hatch in
        FIX_PROMPT_SUFFIX being taken for a finding that does have an in-repo
        fix — anything about an environment variable especially.

        The answer replaces the report only if it is usable: a model that
        edits the files and replies "done" should not cost the analysis.
        """
        log.info("fix mode changed nothing; asking once for the edit")
        self.progress("Asking for the edit")
        response = await self.ask(
            UNAPPLIED_FIX_SUFFIX.format(workspace=self.workspace.path)
        )
        self.keep_if_usable(response.content.strip())

    async def apply_reported_diff(self) -> bool:
        """Apply the diffs the report carries, when the agent never edited.

        Costs no model round. A patch that no longer fits leaves the tree
        untouched, so the issue fallback still fires.

        Returns True when something landed and there is a diff to review.
        """
        patches = reports.extract_patches(self.report)
        if not patches:
            return False
        self.progress("Applying the reported diff")
        try:
            await self.workspace.apply_patches(patches)
        except GitError as err:
            log.info(
                "the reported patch does not apply; leaving the tree alone: %s", err
            )
            return False
        log.info("applied %d patch(es) from the report", len(patches))
        return True

    async def verified_fix(self) -> VerificationSummary | None:
        """Run every configured check, with at most one repair round.

        A still-reproducing bug always earns the round. Other failures earn it
        only when they name an edited file, preserving the existing safeguard
        against spending a repair on a suite that was already red.
        """
        verification = await self.verify()
        if verification is None:
            return None
        verification, repairable = await self.classify_failures(verification)
        if not repairable:
            return verification
        try:
            await self.repair_failing_verification(repairable)
        except Exception:
            log.exception(
                "repair round failed fp=%s; publishing the fix as it stands",
                self.event.fingerprint,
            )
        final = await self.verify()
        if final is None:
            return None
        final, _ = await self.classify_failures(final)
        return final

    async def classify_failures(
        self, verification: VerificationSummary
    ) -> tuple[VerificationSummary, list[VerificationCheck]]:
        changed = await self.workspace.changed_files()
        repairable: list[VerificationCheck] = []
        reproduction = verification.reproduction_after
        if reproduction and reproduction.exit_code not in (0, None):
            repairable.append(VerificationCheck(
                verification.reproduction_command, reproduction
            ))

        checks = []
        for check in verification.checks:
            unrelated = (
                check.result.exit_code not in (0, None)
                and not blames_our_edits(check.result.output, changed)
            )
            unrelated = unrelated or bool(check.runtime_warning)
            classified = VerificationCheck(
                check.command, check.result, unrelated, check.runtime_warning
            )
            checks.append(classified)
            if check.result.exit_code not in (0, None) and not unrelated:
                repairable.append(classified)
        return VerificationSummary(
            reproduction_command=verification.reproduction_command,
            reproduction_before=verification.reproduction_before,
            reproduction_after=verification.reproduction_after,
            checks=tuple(checks),
        ), repairable

    async def repair_failing_verification(
        self, failures: list[VerificationCheck]
    ) -> None:
        """One round to let the agent fix its own fix before the PR ships.

        The suite output is what a reviewer would paste back anyway. The
        answer replaces the report only if it is usable, like
        insist_on_the_edit.
        """
        log.info(
            "%d verification command(s) failed after the edit; asking once for a repair",
            len(failures),
        )
        self.progress("Repairing the failing tests")
        response = await self.ask(
            FAILED_VERIFICATION_SUFFIX.format(
                failures="\n\n".join(
                    "`{command}` exited {status}:\n\n```\n{output}\n```".format(
                        command=failure.command,
                        status=failure.result.exit_code,
                        output=truncate_tail(
                            failure.result.output,
                            MAX_TEST_OUTPUT_IN_PROMPT,
                            "… (earlier output truncated)\n",
                        ),
                    )
                    for failure in failures
                ),
                workspace=self.workspace.path,
            )
        )
        self.keep_if_usable(response.content.strip())

    def keep_if_usable(self, second: str) -> None:
        """Take a follow-up answer as the report, unless it is not one."""
        if not report_problem(second):
            self.report = second

    async def record_format_baseline(self) -> None:
        """Which of the project's formatters the untouched clone satisfies.

        A repo that is already clean can be reformatted afterwards without
        touching a line the fix did not; one that is not stays untouched.
        """
        if not self.applies_a_fix:
            return
        clean = []
        for formatter in detect_formatters(self.workspace.path):
            result = await self.workspace.run_command(
                formatter.check, timeout=FORMAT_TIMEOUT
            )
            if result.exit_code == 0:
                clean.append(formatter)
                continue
            log.info(
                "%s in repo=%s does not satisfy %r (exit %s); leaving its "
                "formatting alone",
                formatter.source, self.repo_config.repo,
                formatter.check, result.exit_code,
            )
        self.format_baseline = tuple(clean)

    async def apply_project_formatting(self) -> None:
        """Format the edit the way the project formats everything else.

        Not a verification step: a failure is logged, never a reason to
        withhold a fix.
        """
        if not self.format_baseline:
            return
        self.progress("Formatting the change")
        for formatter in self.format_baseline:
            result = await self.workspace.run_command(
                formatter.write, timeout=FORMAT_TIMEOUT
            )
            log.info(
                "formatter %r exited %s in repo=%s",
                formatter.write, result.exit_code, self.repo_config.repo,
            )

    async def reproduce_before_edit(self) -> None:
        """Run the owner-supplied reproduction before the agent changes code."""
        if not self.applies_a_fix or not self.repo_config.reproduction_command:
            return
        self.progress("Reproducing issue")
        self.reproduction_before = await self.workspace.run_command(
            self.repo_config.reproduction_command
        )

    async def verify(self) -> VerificationSummary | None:
        """Run reproduction and every post-fix command independently.

        These are not agent capabilities: every command comes from owner config.
        Failures are reported in the PR rather than raised, and one failure
        never prevents later commands from running.
        """
        commands = self.repo_config.post_fix_commands()
        reproduce = self.repo_config.reproduction_command
        if not commands and not reproduce:
            return None
        reproduction_after = None
        if reproduce:
            self.progress("Checking reproduction")
            reproduction_after = await self.workspace.run_command(reproduce)
        checks = []
        for index, command in enumerate(commands, start=1):
            if command == self.repo_config.test_command:
                self.progress("Running tests")
            else:
                self.progress(f"Running verification {index}/{len(commands)}")
            result = await self.workspace.run_command(command)
            log.info(
                "verification command %r exited %s in repo=%s",
                command, result.exit_code, self.repo_config.repo,
            )
            checks.append(VerificationCheck(
                command,
                result,
                runtime_warning=verification_runtime_mismatch(
                    command, self.repo_config.deployment
                ),
            ))
        return VerificationSummary(
            reproduction_command=reproduce,
            reproduction_before=self.reproduction_before,
            reproduction_after=reproduction_after,
            checks=tuple(checks),
        )

    async def prepare_follow_ups(self) -> list[FollowUpTask]:
        """Validate deferred tasks and give invalid ones one read-only rewrite."""
        report, raw = reports.split_follow_up(self.report)
        self.follow_up_source = raw
        self.report = report
        parsed = parse_follow_ups(raw)
        tasks = list(parsed.tasks)
        if parsed.invalid:
            log.info(
                "asking once to rewrite %d invalid follow-up task(s) for fp=%s",
                len(parsed.invalid), self.event.fingerprint,
            )
            rewritten = await self.rewrite_follow_ups(parsed.invalid)
            retried = parse_follow_ups(rewritten)
            tasks.extend(retried.tasks)
            for invalid in retried.invalid:
                log.warning(
                    "skipping invalid follow-up for fp=%s after rewrite: %s",
                    self.event.fingerprint, "; ".join(invalid.problems),
                )

        return self.deduplicate_follow_ups(tasks)

    def finalize_follow_ups(
        self, prepared: list[FollowUpTask]
    ) -> list[FollowUpTask]:
        """Keep repair responses from putting follow-up prose back in the PR.

        A verification repair asks for the full report again, so it can repeat
        or regenerate the Follow-up section after the one allowed rewrite has
        already happened. At this point verification is complete: strip the
        section, retain any valid tasks, and skip invalid material without
        another model round.
        """
        report, raw = reports.split_follow_up(self.report)
        self.report = report
        if not raw:
            return prepared
        parsed = parse_follow_ups(raw)
        for invalid in parsed.invalid:
            log.warning(
                "skipping invalid follow-up for fp=%s after verification: %s",
                self.event.fingerprint, "; ".join(invalid.problems),
            )
        return self.deduplicate_follow_ups([*prepared, *parsed.tasks])

    def deduplicate_follow_ups(
        self, tasks: list[FollowUpTask]
    ) -> list[FollowUpTask]:
        unique = []
        seen = set()
        for task in tasks:
            key = (task.title.casefold(), task.evidence.casefold())
            if key in seen:
                continue
            seen.add(key)
            unique.append(task)
        if len(unique) > MAX_FOLLOW_UP_ISSUES:
            log.warning(
                "follow-up for fp=%s has %d valid tasks; filing the first %d",
                self.event.fingerprint, len(unique), MAX_FOLLOW_UP_ISSUES,
            )
        return unique[:MAX_FOLLOW_UP_ISSUES]

    async def rewrite_follow_ups(self, invalid: tuple[InvalidFollowUp, ...]) -> str:
        self.progress("Improving follow-up tasks")
        details = "\n\n".join(
            f"{item.text}\nProblems: {'; '.join(item.problems)}" for item in invalid
        )
        had_approve = hasattr(self.agent, "approve")
        previous_approve = getattr(self.agent, "approve", None)
        if had_approve:
            self.agent.approve = None
        try:
            response = await self.ask(FOLLOW_UP_RETRY_SUFFIX.format(invalid=details))
        finally:
            if had_approve:
                self.agent.approve = previous_approve
        return response.content.strip()

    async def file_follow_ups(
        self, follow_ups: list[FollowUpTask], pr_url: str
    ) -> list[str]:
        """File each deferred task independently. Failures never retract the PR.

        A sibling call site and missing regression fixture are separate pieces
        of work, so they get separate titles, evidence, and acceptance checks.
        """
        urls = []
        for index, follow_up in enumerate(follow_ups, start=1):
            self.progress(f"Filing follow-up issue {index}/{len(follow_ups)}")
            try:
                url = await self.daemon.github.create_issue(
                    self.repo_config.repo,
                    title=reports.follow_up_title(follow_up),
                    body=reports.follow_up_body(self.event, follow_up, pr_url),
                )
            except Exception:
                log.exception(
                    "could not file follow-up %d for fp=%s; %s still has the fix",
                    index, self.event.fingerprint, pr_url,
                )
                continue
            log.info("filed follow-up %s for the fix in %s", url, pr_url)
            urls.append(url)
        return urls

    # -- the endings that publish nothing -----------------------------------

    def close_as_intended(self) -> str:
        """Close an incident the agent judged to be working as designed.

        Nothing is published: an issue saying "the validator rejected invalid
        input" costs a reader more than it gives. The analysis is kept on the
        incident so the call can be reviewed, and what it cost is banked —
        the round was billed either way.
        """
        self.ignored_reason = reports.by_design_reason(self.report)
        self.artifact_kind = ARTIFACT_IGNORED
        prompt_tokens, completion_tokens, cost = self.usage
        log.info(
            "not a defect fp=%s repo=%s: %s", self.event.fingerprint,
            self.daemon.repo_label(self.repo_config), self.ignored_reason,
        )
        if self.plan.dry_run:
            reports.print_dry_run(
                self.plan.dry_run_header, self.daemon.repo_label(self.repo_config),
                self.report, self.usage,
                title="(not filed — working as intended)",
            )
            self.daemon.store.forget(self.event.fingerprint, self.event.repo)
            return ""
        self.daemon.store.add_spend(
            self.event.fingerprint, self.event.repo,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
        )
        self.daemon.store.mark_ignored(
            self.event.fingerprint, self.event.repo, reason=self.ignored_reason
        )
        return ""

    def print_dry_run(self) -> str:
        reports.print_dry_run(
            self.plan.dry_run_header, self.daemon.repo_label(self.repo_config),
            self.report, self.usage, self.plan.dry_run_extra, title=self.title,
        )
        if self.plan.forget_on_dry_run:
            self.daemon.store.forget(self.event.fingerprint, self.event.repo)
        return ""

    def save_local_report(self) -> str:
        """Write an incident report to disk. Returns the report path.

        The local-mode counterpart to opening a PR: same analysis, same
        recorded cost, but nothing leaves the machine.
        """
        self.progress("Writing report")
        self.artifact_kind = ARTIFACT_REPORT
        prompt_tokens, completion_tokens, cost = self.usage
        report_path = reports.write_report_file(
            self.daemon.report_dir, self.event, self.report
        )
        self.daemon.store.mark_processed(
            self.event.fingerprint,
            self.event.repo,
            branch="",
            pr_url=str(report_path),
            cost_usd=cost,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            report_text=self.report,
            artifact_kind=ARTIFACT_REPORT,
        )
        log.info(
            "wrote local report %s for fp=%s (cost: $%.4f, tokens: %d/%d)",
            report_path, self.event.fingerprint, cost,
            prompt_tokens, completion_tokens,
        )
        return str(report_path)

    def bank_spend(self) -> None:
        bank_spend(
            self.daemon.store, self.event, self.agent, already_counted=self.spent
        )


def bank_spend(store, event: ErrorEvent, agent, already_counted=None) -> None:
    """Record what an agent spent against its incident, whatever happened.

    Every round is billed, so a run that dies on the thirtieth still owes for
    twenty-nine, and a screen that says "investigate" owes for itself. Both
    have to reach the day's total or the cap watches the wrong number.

    Best-effort: losing the cost figure is no reason to lose the original
    exception. `already_counted` carries earlier asks in the same run, which
    the agent has since cleared off itself.
    """
    try:
        spent = dict(already_counted or {})
        accumulate_usage(spent, agent.take_usage())
        prompt_tokens, completion_tokens, cost = extract_usage(spent, agent.model)
        store.add_spend(
            event.fingerprint,
            event.repo,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
        )
    except Exception:
        log.debug("could not record the spend of a failed analysis", exc_info=True)


def blames_our_edits(output: str, changed: list[str]) -> bool:
    """Whether a failing test run names any file this run changed.

    Stands in for a baseline run, which cannot be done safely — the edit would
    have to come off the tree and go back on. A failure the edit caused names
    the edited file; one that names none belongs to a suite that was already
    red, and a repo in that state would buy a repair round on every incident
    forever.

    Matched on the file name, not the path, so a runner printing relative
    paths still counts. That errs towards paying for the round, which is the
    old behaviour.
    """
    if not changed:
        return False
    lowered = output.lower()
    return any(Path(path).name.lower() in lowered for path in changed)


def deployment_section(repo_config: RepoConfig, watching: list[str]) -> str:
    """The deployment facts for the prompt, or "" when none are recorded.

    Omitted entirely rather than half-filled: a report that says "port 0" or
    invents a folder is worse than one that does not mention the deployment.
    `watching` is the monitors actually attached to this repo, so the prompt
    describes what is running rather than what the config hoped for.
    """
    deployment = repo_config.deployment
    facts = []
    if deployment.path:
        facts.append(f"- Folder on the server: {deployment.path}")
    if deployment.port:
        facts.append(f"- Listens on port: {deployment.port}")
    if deployment.runs:
        facts.append(f"- Started by: {deployment.runs}")
    if deployment.service_unit:
        facts.append(f"- Active service unit: {deployment.service_unit}")
    if deployment.service_command:
        facts.append(f"- Active service command: {deployment.service_command}")
    if deployment.proxy_kind:
        facts.append(f"- Reverse proxy: {deployment.proxy_kind}")
    if deployment.proxy_config_path:
        facts.append(
            f"- Active proxy configuration: {deployment.proxy_config_path}"
        )
    if deployment.proxy_repo_path:
        facts.append(
            f"- Repository path deployed as proxy config: {deployment.proxy_repo_path}"
        )
    elif deployment.proxy_config_path:
        facts.append(
            "- Active proxy configuration is not mapped to this repository"
        )
    if deployment.proxy_body_limit:
        facts.append(
            f"- Active proxy request-body limit: {deployment.proxy_body_limit}"
        )
    if deployment.config_owner:
        facts.append(f"- Deployment configuration owner: {deployment.config_owner}")
    if deployment.infra_repo:
        facts.append(f"- Infrastructure repository: {deployment.infra_repo}")
    if deployment.stack:
        facts.append(f"- Built with: {deployment.stack}")
    if watching:
        facts.append(f"- Errors are read from: {', '.join(watching)}")
    if not facts:
        return ""
    return DEPLOYMENT_SECTION.format(facts="\n".join(facts))


def blocked_fix_title(event: ErrorEvent, repo_config: RepoConfig) -> str:
    """A deterministic fallback title for a withheld deployment fix."""
    proxy = repo_config.deployment.proxy_kind or "reverse proxy"
    if "too large body" in event.details.casefold():
        return f"Raise the active {proxy} request-body limit"
    return f"Resolve the active {proxy} failure before publishing a code fix"
