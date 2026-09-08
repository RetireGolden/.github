"""Read-only receipt proof and current-policy checker for profile completion.

This module validates trusted workflow receipts and their paired context
artifacts.  It does not mutate GitHub state, dispatch workflows, or call paid
APIs.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from or_pr_review.errors import SchemaError
from or_pr_review.models import DEFAULT_JUDGE_MODEL, parse_model_routes
from or_pr_review.profile_evidence import (
    GateDecision,
    ReviewReceipt,
    VerifiedRunReceipt,
    canonical_receipt,
    evaluate_receipt,
    parse_receipt,
    parse_review_receipt,
)
from or_pr_review.review_context import ReviewContext, restore_context
from or_pr_review.review_plan import parse_review_profiles, resolve_review_plan
from or_pr_review.review_policy import resolve_policy

from scripts.profile_github import (
    ProfileGitHub,
    ProfileGitHubError,
    VerifiedRun,
    strict_json,
)

VerifySourceRun = Callable[[Mapping[str, Any]], VerifiedRun]

REVIEW_TITLE = "## OpenRouter pull-request review"
MARKER_PREFIX = "<!-- openrouter-review-plan:v1:"
RECEIPT_ARTIFACT_FILENAME = "review-receipt.json"
MAX_RECEIPT_ARTIFACT_BYTES = 16 * 1024
MAX_CONTEXT_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_REVIEWS = 1000
_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID = re.compile(r"/actions/runs/([1-9][0-9]*)\Z")


class ProfileCompletionError(RuntimeError):
    """Receipt evidence was unavailable, ambiguous, or failed trust bounds."""


@dataclass(frozen=True)
class VerifiedReview:
    receipt: ReviewReceipt
    context: ReviewContext
    completion: VerifiedRunReceipt
    source: VerifiedRun
    review_id: int


def receipt_artifact_name(run_id: int, attempt: int) -> str:
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
        raise ValueError("run id must be a positive integer")
    if (
        not isinstance(attempt, int)
        or isinstance(attempt, bool)
        or not 1 <= attempt <= 1000
    ):
        raise ValueError("run attempt must be from 1 through 1000")
    return f"openrouter-review-receipt-{run_id}-{attempt}"


def context_artifact_name(run_id: int, attempt: int) -> str:
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
        raise ValueError("run id must be a positive integer")
    if (
        not isinstance(attempt, int)
        or isinstance(attempt, bool)
        or not 1 <= attempt <= 1000
    ):
        raise ValueError("run attempt must be from 1 through 1000")
    return f"openrouter-review-context-{run_id}-{attempt}"


def _github_ms(value: Any, what: str) -> int:
    if not isinstance(value, str) or not value:
        raise ProfileCompletionError(f"{what} is missing")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise ProfileCompletionError(f"{what} is not a valid timestamp") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _completion_ms(run: Mapping[str, Any]) -> int:
    updated_ms = _github_ms(run.get("updated_at"), "updated_at")
    return (updated_ms // 1000) * 1000 + 999


def _run_id_from_url(url: str) -> int:
    match = _RUN_ID.search(url)
    if match is None:
        raise ProfileCompletionError(
            "run_url is not a source workflow-run URL for the repository"
        )
    return int(match.group(1))


def _decode_marker(body: str) -> bytes:
    if not isinstance(body, str):
        raise ProfileCompletionError("review body must be a string")
    lines = body.splitlines()
    if not lines or lines[0] != REVIEW_TITLE:
        raise ProfileCompletionError("review body lacks the trusted heading")
    marker_lines = [line for line in lines if line.startswith(MARKER_PREFIX)]
    if len(marker_lines) != 1:
        raise ProfileCompletionError(
            "review body must contain exactly one receipt marker"
        )
    marker = marker_lines[0]
    if not marker.endswith(" -->"):
        raise ProfileCompletionError("receipt marker is malformed")
    encoded = marker[len(MARKER_PREFIX) : -4]
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ProfileCompletionError("receipt marker is not strict base64") from error
    if len(decoded) > MAX_RECEIPT_ARTIFACT_BYTES:
        raise ProfileCompletionError("receipt marker exceeds its size bound")
    return decoded


def _marker_receipt(body: str) -> ReviewReceipt:
    try:
        return parse_receipt(_decode_marker(body))
    except SchemaError as error:
        raise ProfileCompletionError("receipt marker is not a valid receipt") from error


class ReceiptEvidence:
    """Read-only verified receipt collector and current-policy gate."""

    def __init__(
        self,
        client: ProfileGitHub,
        verify_source_callback: VerifySourceRun,
        allowed_job_names: Sequence[str],
        review_step_names: Sequence[str],
    ) -> None:
        if not isinstance(client, ProfileGitHub):
            raise TypeError("client must be a ProfileGitHub instance")
        if not callable(verify_source_callback):
            raise TypeError("verify_source_callback must be callable")
        if (
            not isinstance(allowed_job_names, Sequence)
            or isinstance(allowed_job_names, (str, bytes))
            or not allowed_job_names
            or any(not isinstance(name, str) or not name for name in allowed_job_names)
        ):
            raise ValueError(
                "allowed_job_names must be a non-empty sequence of job names"
            )
        if (
            not isinstance(review_step_names, Sequence)
            or isinstance(review_step_names, (str, bytes))
            or not review_step_names
            or any(not isinstance(name, str) or not name for name in review_step_names)
        ):
            raise ValueError(
                "review_step_names must be a non-empty sequence of step names"
            )
        self._client = client
        self._verify_source_callback = verify_source_callback
        self._allowed_jobs = tuple(dict.fromkeys(allowed_job_names))
        self._review_steps = list(review_step_names)

    @property
    def repository(self) -> str:
        return self._client.config.full_name

    def _repo_endpoint(self, suffix: str) -> str:
        return f"/repos/{self._client.config.owner}/{self._client.config.name}{suffix}"

    def collect(
        self,
        pr_number: int,
        reviews: Optional[Sequence[Mapping[str, Any]]] = None,
        *,
        relevant_runs: Optional[set[tuple[int, int]]] = None,
        include_latest: bool = True,
    ) -> tuple[VerifiedReview, ...]:
        if (
            not isinstance(pr_number, int)
            or isinstance(pr_number, bool)
            or pr_number <= 0
        ):
            raise ValueError("pull request number must be positive")
        repo = self._client.get_repo()
        if reviews is None:
            try:
                reviews = self._client.paginated(
                    self._repo_endpoint(f"/pulls/{pr_number}/reviews"),
                    max_items=MAX_REVIEWS,
                )
            except ProfileGitHubError as error:
                raise ProfileCompletionError(
                    "pull request reviews are unavailable"
                ) from error
        if not isinstance(reviews, Sequence) or isinstance(reviews, (str, bytes)):
            raise ValueError("reviews must be a sequence when supplied")
        verified_items: list[VerifiedReview] = []
        unavailable: list[tuple[int, Exception]] = []
        seen: dict[tuple[int, int], VerifiedReview] = {}
        latest_id = max(
            (
                review.get("id", 0)
                for review in reviews
                if isinstance(review, Mapping)
                and self._is_candidate(review, repo)
                and type(review.get("id")) is int
            ),
            default=0,
        )
        for review in reviews:
            if not isinstance(review, Mapping):
                raise ProfileCompletionError(
                    "pull request reviews contain a non-object entry"
                )
            if not self._is_candidate(review, repo):
                continue
            body = review.get("body")
            assert isinstance(body, str)
            review_id = review.get("id")
            if (
                not isinstance(review_id, int)
                or isinstance(review_id, bool)
                or review_id <= 0
            ):
                raise ProfileCompletionError("review lacks a positive id")
            if relevant_runs is not None and not (
                include_latest and review_id == latest_id
            ):
                try:
                    marker = self._marker_receipt(body)
                    key = (_run_id_from_url(marker.run_url), marker.run_attempt)
                except (SchemaError, ProfileCompletionError):
                    continue
                if key not in relevant_runs:
                    continue
            try:
                item = self._prove_review(body, review, pr_number, review_id)
            except (ProfileGitHubError, ProfileCompletionError) as error:
                unavailable.append((review_id, error))
                continue
            key = (item.source.run_id, item.source.attempt)
            previous = seen.get(key)
            if previous is not None:
                if canonical_receipt(previous.receipt) != canonical_receipt(
                    item.receipt
                ):
                    raise ProfileCompletionError(
                        "contradictory duplicate verified completion"
                    )
                continue
            seen[key] = item
            verified_items.append(item)
        # A later verified publication permits recovery from an older failed
        # publication. It does not settle pending requests: fold_requests still
        # checks those against their own head, origin and acceptance time.
        # An unavailable latest publication must never reveal an older clean one.
        newest_verified = max((item.review_id for item in verified_items), default=0)
        for review_id, error in unavailable:
            if include_latest and review_id >= newest_verified:
                raise ProfileCompletionError(
                    "latest review proof is unavailable or invalid"
                ) from error
        verified_items.sort(key=lambda item: item.review_id)
        return tuple(verified_items)

    def current_decision(
        self,
        verified: VerifiedReview,
        repo_root: Path | str,
        current_base_sha: str,
        current_head_sha: str,
        registry_raw: str | None,
        routes_raw: str | None,
        minimum: str = "standard",
        lane_ceiling: int = 1080,
        job_ceiling: int = 1320,
        tool_ceiling: int = 50,
    ) -> GateDecision:
        if type(verified) is not VerifiedReview:
            return GateDecision(False, "verified review is not validated")
        receipt = verified.receipt
        context = verified.context
        if type(current_head_sha) is not str or not _SHA.fullmatch(current_head_sha):
            return GateDecision(False, "head SHA mismatch")
        if receipt.head_sha != current_head_sha:
            return GateDecision(False, "head SHA mismatch")
        if type(minimum) is not str or minimum not in {"standard", "deep"}:
            return GateDecision(False, "invalid minimum level")
        carried_paths = self._carried_paths(context)
        try:
            current_policy = resolve_policy(
                Path(repo_root),
                current_base_sha,
                current_head_sha,
                carried_paths=carried_paths,
            )
        except Exception as exc:
            return GateDecision(False, f"current policy could not be resolved: {exc}")
        try:
            registry = parse_review_profiles(registry_raw)
            routes = parse_model_routes(routes_raw)
        except Exception as exc:
            return GateDecision(False, f"trusted registry or routes are invalid: {exc}")
        effective_minimum = (
            "deep"
            if minimum == "deep" or current_policy.minimum == "deep"
            else "standard"
        )
        try:
            plan = resolve_review_plan(
                registry,
                profile=current_policy.profile,
                minimum=effective_minimum,
                requested_level="deep" if effective_minimum == "deep" else "auto",
                mode=context.loop.mode,
                models=[],
                judge_model=DEFAULT_JUDGE_MODEL,
                routes=routes,
                effort="",
                max_tool_turns=tool_ceiling,
                job_budget_seconds=job_ceiling,
                lane_timeout_seconds=lane_ceiling,
            )
        except Exception as exc:
            return GateDecision(
                False, f"current review plan could not be resolved: {exc}"
            )
        required_models = tuple(lane.model for lane in plan.lanes if lane.required)
        return evaluate_receipt(
            receipt,
            repository=receipt.repository,
            pr_number=receipt.pr_number,
            head_sha=current_head_sha,
            policy_digest=current_policy.digest,
            registry_digest=plan.registry_digest,
            profile=current_policy.profile,
            minimum=effective_minimum,
            required_models=required_models,
        )

    @staticmethod
    def _carried_paths(context: ReviewContext) -> tuple[str, ...]:
        paths = {
            finding.file for finding in context.loop.prior_findings if finding.file
        }
        policy = context.collected.review_policy
        if policy is not None:
            paths.update(policy.changed_paths)
        return tuple(sorted(paths))

    def _is_candidate(self, review: Mapping[str, Any], repo: Mapping[str, Any]) -> bool:
        if not self._client.trusted_review(review, repo):
            return False
        body = review.get("body")
        if not isinstance(body, str) or not body.startswith(REVIEW_TITLE):
            return False
        return MARKER_PREFIX in body

    def _prove_review(
        self,
        body: str,
        review: Mapping[str, Any],
        pr_number: int,
        review_id: int,
    ) -> VerifiedReview:
        marker_receipt = self._marker_receipt(body)
        if body.count(f"[Workflow run]({marker_receipt.run_url})") != 1:
            raise ProfileCompletionError(
                "review body must contain the exact workflow run link"
            )
        commit_id = review.get("commit_id")
        if not isinstance(commit_id, str) or not _SHA.fullmatch(commit_id):
            raise ProfileCompletionError("review commit_id is invalid")
        if commit_id != marker_receipt.head_sha:
            raise ProfileCompletionError(
                "review commit_id does not match the receipt head SHA"
            )
        if (
            marker_receipt.repository != self.repository
            or marker_receipt.pr_number != pr_number
        ):
            raise ProfileCompletionError(
                "receipt does not match repository or pull request"
            )
        run_id = _run_id_from_url(marker_receipt.run_url)
        attempt = marker_receipt.run_attempt
        run = self._fetch_run(run_id, attempt)
        verified = self._verify_source(run)
        self._require_successful_run(verified)
        self._reviewing_job(verified)
        self._bind_source_heads(verified, marker_receipt.head_sha)
        receipt_bytes = self._client.artifact(
            verified,
            receipt_artifact_name(run_id, attempt),
            MAX_RECEIPT_ARTIFACT_BYTES,
            RECEIPT_ARTIFACT_FILENAME,
        )
        try:
            receipt = parse_review_receipt(body, receipt_bytes)
        except SchemaError as error:
            raise ProfileCompletionError(
                "receipt artifact does not match the review body"
            ) from error
        if canonical_receipt(receipt) != canonical_receipt(marker_receipt):
            raise ProfileCompletionError(
                "receipt marker does not match the trusted artifact"
            )
        self._bind_receipt_run(receipt, verified)
        context = self._load_context(verified, receipt)
        self._bind_context(context, receipt)
        completion_ms = _completion_ms(verified.run)
        if (
            context.execution is not None
            and completion_ms < context.execution.started_unix_ms
        ):
            raise ProfileCompletionError("completion precedes prepared execution start")
        completion = VerifiedRunReceipt(
            receipt, verified.run_id, verified.attempt, completion_ms
        )
        return VerifiedReview(receipt, context, completion, verified, review_id)

    def _marker_receipt(self, body: str) -> ReviewReceipt:
        return _marker_receipt(body)

    def _fetch_run(self, run_id: int, attempt: int) -> Mapping[str, Any]:
        if (
            not isinstance(attempt, int)
            or isinstance(attempt, bool)
            or not 1 <= attempt <= 1000
        ):
            raise ValueError("attempt must be from 1 through 1000")
        value = self._client._api(
            self._repo_endpoint(f"/actions/runs/{run_id}/attempts/{attempt}")
        )
        if not isinstance(value, Mapping):
            raise ProfileCompletionError("workflow run response is not an object")
        return value

    def _verify_source(self, run: Mapping[str, Any]) -> VerifiedRun:
        try:
            verified = self._verify_source_callback(run)
        except ProfileGitHubError as error:
            raise ProfileCompletionError(
                "workflow run failed trusted provenance checks"
            ) from error
        self._bound_verified_run(run, verified)
        return verified

    def _bound_verified_run(
        self, run: Mapping[str, Any], verified: VerifiedRun
    ) -> None:
        if type(verified) is not VerifiedRun:
            raise ProfileCompletionError(
                "source verification did not return a VerifiedRun"
            )
        run_id = run.get("id")
        attempt = run.get("run_attempt")
        head_sha = run.get("head_sha")
        event = run.get("event")
        if (
            not isinstance(run_id, int)
            or isinstance(run_id, bool)
            or run_id <= 0
            or not isinstance(attempt, int)
            or isinstance(attempt, bool)
            or not 1 <= attempt <= 1000
            or not isinstance(head_sha, str)
            or not _SHA.fullmatch(head_sha)
            or not isinstance(event, str)
        ):
            raise ProfileCompletionError("workflow run lacks required identity fields")
        if (
            verified.owner != self._client.config.owner
            or verified.repo != self._client.config.name
            or verified.run_id != run_id
            or verified.attempt != attempt
            or verified.head_sha != head_sha
            or verified.event != event
        ):
            raise ProfileCompletionError(
                "source verification returned mismatched run bounds"
            )
        try:
            expected_jobs = tuple(
                self._client.paginated(
                    self._repo_endpoint(
                        f"/actions/runs/{run_id}/attempts/{attempt}/jobs"
                    ),
                    "jobs",
                )
            )
        except ProfileGitHubError as error:
            raise ProfileCompletionError("workflow jobs are unavailable") from error
        if tuple(verified.jobs) != expected_jobs:
            raise ProfileCompletionError(
                "source verification returned jobs from a different attempt"
            )

    def _require_successful_run(self, verified: VerifiedRun) -> None:
        status = verified.run.get("status")
        conclusion = verified.run.get("conclusion")
        if status != "completed" or conclusion != "success":
            raise ProfileCompletionError("workflow run is not successful")

    def _reviewing_job(self, verified: VerifiedRun) -> str:
        matches = [
            job_name
            for job_name in self._allowed_jobs
            if sum(
                self._client.successful_steps(verified, job_name, [step])
                for step in self._review_steps
            )
            == 1
            and any(
                job.get("name") == job_name
                and job.get("status") == "completed"
                and job.get("conclusion") == "success"
                for job in verified.jobs
            )
        ]
        if len(matches) != 1:
            raise ProfileCompletionError(
                "verified completion lacks exactly one trusted producer job"
            )
        return matches[0]

    def _bind_source_heads(self, verified: VerifiedRun, receipt_head_sha: str) -> None:
        if verified.event == "pull_request":
            if verified.head_sha != receipt_head_sha:
                raise ProfileCompletionError(
                    "receipt head SHA does not match the verified pull request run"
                )
        elif verified.event == "workflow_dispatch":
            if verified.head_branch != self._client.config.default_branch:
                raise ProfileCompletionError(
                    "dispatch run is not on the default branch"
                )
        else:
            raise ProfileCompletionError("workflow run has an untrusted event")

    @staticmethod
    def _bind_receipt_run(receipt: ReviewReceipt, verified: VerifiedRun) -> None:
        if _run_id_from_url(receipt.run_url) != verified.run_id:
            raise ProfileCompletionError(
                "receipt run id does not match the verified workflow run"
            )
        if receipt.run_attempt != verified.attempt:
            raise ProfileCompletionError(
                "receipt run attempt does not match the verified workflow run"
            )
        expected_url = f"https://github.com/{verified.owner}/{verified.repo}/actions/runs/{verified.run_id}"
        if receipt.run_url != expected_url:
            raise ProfileCompletionError(
                "receipt run_url does not match the verified workflow run"
            )

    def _load_context(
        self, verified: VerifiedRun, receipt: ReviewReceipt
    ) -> ReviewContext:
        raw = self._client.artifact(
            verified,
            context_artifact_name(verified.run_id, verified.attempt),
            MAX_CONTEXT_ARTIFACT_BYTES,
        )
        try:
            envelope = strict_json(
                raw, max_bytes=MAX_CONTEXT_ARTIFACT_BYTES, max_depth=24
            )
        except ProfileGitHubError as error:
            raise ProfileCompletionError(
                "review context artifact is not valid JSON"
            ) from error
        if not isinstance(envelope, dict):
            raise ProfileCompletionError("review context artifact is not an object")
        digest = envelope.get("sha256")
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise ProfileCompletionError("review context artifact lacks a valid digest")
        if digest != receipt.context_sha256:
            raise ProfileCompletionError(
                "review context digest does not match the receipt"
            )
        try:
            return restore_context(envelope)
        except SchemaError as error:
            raise ProfileCompletionError(
                "review context artifact is invalid"
            ) from error

    @staticmethod
    def _bind_context(context: ReviewContext, receipt: ReviewReceipt) -> None:
        if context.repository != receipt.repository:
            raise ProfileCompletionError(
                "review context repository does not match the receipt"
            )
        if context.collected.pr_number != receipt.pr_number:
            raise ProfileCompletionError(
                "review context pull request does not match the receipt"
            )
        if context.collected.head_sha != receipt.head_sha:
            raise ProfileCompletionError(
                "review context head SHA does not match the receipt"
            )
        if context.loop.mode != receipt.mode:
            raise ProfileCompletionError(
                "review context mode does not match the receipt"
            )
        if context.collected.plan.scope != receipt.scope:
            raise ProfileCompletionError(
                "review context scope does not match the receipt"
            )
        execution = context.execution
        if execution is None:
            raise ProfileCompletionError("review context lacks prepared execution")
        plan = execution.plan
        if plan.profile != receipt.profile:
            raise ProfileCompletionError(
                "review context profile does not match the receipt"
            )
        if plan.level != receipt.level:
            raise ProfileCompletionError(
                "review context level does not match the receipt"
            )
        if plan.trigger != receipt.trigger:
            raise ProfileCompletionError(
                "review context trigger does not match the receipt"
            )
        if plan.registry_digest != receipt.registry_digest:
            raise ProfileCompletionError(
                "review context registry digest does not match the receipt"
            )
        if execution.source_run_url != receipt.run_url:
            raise ProfileCompletionError(
                "review context source run URL does not match the receipt"
            )
        if execution.run_attempt != receipt.run_attempt:
            raise ProfileCompletionError(
                "review context run attempt does not match the receipt"
            )
        required = tuple(lane.model for lane in plan.lanes if lane.required)
        if receipt.required_models != required:
            raise ProfileCompletionError(
                "receipt required models do not match the prepared plan"
            )
        plan_models = {lane.model for lane in plan.lanes}
        if not set(receipt.successful_models).issubset(plan_models):
            raise ProfileCompletionError(
                "receipt successful models are not subset of the prepared plan"
            )
        policy = context.collected.review_policy
        if policy is not None:
            if receipt.policy_digest != policy.digest:
                raise ProfileCompletionError(
                    "receipt policy digest does not match the review context"
                )
            if receipt.policy_base_sha != policy.base_sha:
                raise ProfileCompletionError(
                    "receipt policy base SHA does not match the review context"
                )
        elif receipt.policy_digest or receipt.policy_base_sha:
            raise ProfileCompletionError(
                "receipt claims policy metadata without review context policy"
            )
