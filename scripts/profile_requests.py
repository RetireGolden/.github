"""Bounded accepted-request history reader for immutable workflow evidence.

This module reads and validates acceptance artifacts only.  It does not publish
artifacts, mutate GitHub state, evaluate completion policy, or call paid APIs.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

from or_pr_review.errors import SchemaError
from or_pr_review.profile_evidence import (
    AcceptedRequest,
    PendingRequest,
    VerifiedRunReceipt,
    fold_requests,
)
from or_pr_review.review_context import restore_context

from scripts.profile_github import (
    ProfileGitHub,
    ProfileGitHubError,
    VerifiedRun,
    strict_json,
)

VerifyHistoricalRun = Callable[[Mapping[str, Any]], VerifiedRun]

REQUEST_VERSION = 1
MAX_REQUEST_BYTES = 16 * 1024
MAX_HISTORY_ITEMS = 1000
MAX_ATTEMPT_PROBE = 20
TIMESTAMP_TOLERANCE_MS = 5_000
ACCEPT_STEP = "Accept review request"
PRESERVE_STEP = "Preserve accepted review request"
ARTIFACT_FILENAME = "request.json"

_REQUEST_KEYS = frozenset(
    {
        "version",
        "repository",
        "pr_number",
        "run_id",
        "run_attempt",
        "accepted_at_ms",
        "kind",
        "head_sha",
        "policy_digest",
        "registry_digest",
        "profile",
        "origin_run_id",
        "context_sha256",
    }
)
_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_PROFILE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class ProfileRequestError(RuntimeError):
    """Accepted-request evidence was unavailable, ambiguous, or failed trust bounds."""


class AlreadyAccepted(ProfileRequestError):
    """The workflow run attempt already published an acceptance artifact."""

    def __init__(self, message: str, record: Optional["RequestRecord"] = None) -> None:
        super().__init__(message)
        self.record = record


@dataclass(frozen=True)
class RequestRecord:
    version: int
    repository: str
    pr_number: int
    run_id: int
    run_attempt: int
    accepted_at_ms: int
    kind: str
    head_sha: str
    policy_digest: str
    registry_digest: str
    profile: str
    origin_run_id: Optional[int]
    context_sha256: str


def primary_artifact_name(pr_number: int) -> str:
    if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number <= 0:
        raise ValueError("pull request number must be positive")
    return f"openrouter-request-{pr_number}"


def attempt_artifact_name(pr_number: int, attempt: int) -> str:
    if (
        not isinstance(attempt, int)
        or isinstance(attempt, bool)
        or not 1 <= attempt <= 1000
    ):
        raise ValueError("run attempt must be from 1 through 1000")
    if attempt == 1:
        return primary_artifact_name(pr_number)
    return f"{primary_artifact_name(pr_number)}-attempt-{attempt}"


def canonical_request(record: RequestRecord) -> bytes:
    if type(record) is not RequestRecord:
        raise ProfileRequestError("canonical request requires RequestRecord")
    validated = parse_request(canonical_request_bytes(record))
    return canonical_request_bytes(validated)


def canonical_request_bytes(record: RequestRecord) -> bytes:
    return json.dumps(
        _record_payload(record),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _record_payload(record: RequestRecord) -> dict[str, Any]:
    return {
        "version": record.version,
        "repository": record.repository,
        "pr_number": record.pr_number,
        "run_id": record.run_id,
        "run_attempt": record.run_attempt,
        "accepted_at_ms": record.accepted_at_ms,
        "kind": record.kind,
        "head_sha": record.head_sha,
        "policy_digest": record.policy_digest,
        "registry_digest": record.registry_digest,
        "profile": record.profile,
        "origin_run_id": record.origin_run_id,
        "context_sha256": record.context_sha256,
    }


def parse_request(raw: bytes) -> RequestRecord:
    value = strict_json(raw, max_bytes=MAX_REQUEST_BYTES, max_depth=16)
    if not isinstance(value, dict) or set(value) != _REQUEST_KEYS:
        raise ProfileRequestError(
            "request artifact keys are not exactly request v1 keys"
        )
    if type(value["version"]) is not int or value["version"] != REQUEST_VERSION:
        raise ProfileRequestError("unsupported request version")
    repository = value["repository"]
    if type(repository) is not str or not re.fullmatch(
        r"[^/\s]{1,100}/[^/\s]{1,100}", repository
    ):
        raise ProfileRequestError("repository must be owner/name")
    pr_number = value["pr_number"]
    if type(pr_number) is not int or isinstance(pr_number, bool) or pr_number <= 0:
        raise ProfileRequestError("pr_number must be positive")
    run_id = value["run_id"]
    run_attempt = value["run_attempt"]
    if (
        type(run_id) is not int
        or isinstance(run_id, bool)
        or run_id <= 0
        or type(run_attempt) is not int
        or isinstance(run_attempt, bool)
        or not 1 <= run_attempt <= 1000
    ):
        raise ProfileRequestError("run id and attempt are invalid")
    accepted_at_ms = value["accepted_at_ms"]
    if (
        type(accepted_at_ms) is not int
        or isinstance(accepted_at_ms, bool)
        or accepted_at_ms < 0
    ):
        raise ProfileRequestError("accepted_at_ms must be a non-negative integer")
    kind = value["kind"]
    if type(kind) is not str or kind not in {"deep", "carry", "cancel"}:
        raise ProfileRequestError("kind must be deep, carry, or cancel")
    head_sha = value["head_sha"]
    if type(head_sha) is not str or not _SHA.fullmatch(head_sha):
        raise ProfileRequestError("head_sha must be a lowercase full SHA")
    policy_digest = value["policy_digest"]
    if type(policy_digest) is not str or not (
        policy_digest == "" or _DIGEST.fullmatch(policy_digest)
    ):
        raise ProfileRequestError("policy_digest must be empty or a lowercase SHA-256")
    registry_digest = value["registry_digest"]
    if type(registry_digest) is not str or not _DIGEST.fullmatch(registry_digest):
        raise ProfileRequestError("registry_digest must be a lowercase SHA-256")
    profile = value["profile"]
    if type(profile) is not str or not _PROFILE.fullmatch(profile):
        raise ProfileRequestError("profile is invalid")
    origin_run_id = value["origin_run_id"]
    if origin_run_id is not None and (
        type(origin_run_id) is not int
        or isinstance(origin_run_id, bool)
        or origin_run_id <= 0
    ):
        raise ProfileRequestError("origin_run_id must be null or a positive integer")
    context_sha256 = value["context_sha256"]
    if type(context_sha256) is not str:
        raise ProfileRequestError("context_sha256 must be a string")
    if kind == "cancel":
        if context_sha256 != "":
            raise ProfileRequestError("cancel request must leave context_sha256 blank")
    elif not _DIGEST.fullmatch(context_sha256):
        raise ProfileRequestError("context_sha256 must be a lowercase SHA-256")
    record = RequestRecord(
        REQUEST_VERSION,
        repository,
        pr_number,
        run_id,
        run_attempt,
        accepted_at_ms,
        kind,
        head_sha,
        policy_digest,
        registry_digest,
        profile,
        origin_run_id,
        context_sha256,
    )
    if raw != canonical_request_bytes(record):
        raise ProfileRequestError("request artifact is not canonical JSON")
    as_event(record)
    return record


def as_event(record: RequestRecord) -> AcceptedRequest:
    if type(record) is not RequestRecord:
        raise ProfileRequestError("accepted event requires RequestRecord")
    return AcceptedRequest(
        record.run_id,
        record.run_attempt,
        record.accepted_at_ms,
        record.kind,
        record.head_sha,
        record.policy_digest,
        record.registry_digest,
        record.profile,
        record.origin_run_id,
    )


def _github_ms(value: Any, what: str) -> int:
    if not isinstance(value, str) or not value:
        raise ProfileRequestError(f"{what} is missing")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise ProfileRequestError(f"{what} is not a valid timestamp") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


class RequestHistory:
    """Read-only accepted-request history for one trusted repository."""

    def __init__(
        self,
        client: ProfileGitHub,
        current_default_caller_blob: str,
        allowed_job_names: Sequence[str],
        verify_historical_run: Optional[VerifyHistoricalRun] = None,
    ) -> None:
        if not isinstance(client, ProfileGitHub):
            raise TypeError("client must be a ProfileGitHub instance")
        if not isinstance(current_default_caller_blob, str) or not _SHA.fullmatch(
            current_default_caller_blob
        ):
            raise ValueError("current_default_caller_blob must be a full commit SHA")
        if (
            not isinstance(allowed_job_names, Sequence)
            or isinstance(allowed_job_names, (str, bytes))
            or not allowed_job_names
            or any(not isinstance(name, str) or not name for name in allowed_job_names)
        ):
            raise ValueError(
                "allowed_job_names must be a non-empty sequence of job names"
            )
        if verify_historical_run is not None and not callable(verify_historical_run):
            raise TypeError("verify_historical_run must be callable when provided")
        self._client = client
        self._caller_blob = current_default_caller_blob
        self._allowed_jobs = tuple(dict.fromkeys(allowed_job_names))
        self._verify_historical_run = verify_historical_run

    @property
    def repository(self) -> str:
        return self._client.config.full_name

    def _repo_endpoint(self, suffix: str) -> str:
        return f"/repos/{self._client.config.owner}/{self._client.config.name}{suffix}"

    def load(self, pr_number: int) -> Tuple[RequestRecord, ...]:
        if (
            not isinstance(pr_number, int)
            or isinstance(pr_number, bool)
            or pr_number <= 0
        ):
            raise ValueError("pull request number must be positive")
        primary = primary_artifact_name(pr_number)
        endpoint = self._repo_endpoint(f"/actions/artifacts?name={primary}")
        try:
            indexed = self._client.paginated(endpoint, "artifacts", MAX_HISTORY_ITEMS)
        except ProfileGitHubError as error:
            raise ProfileRequestError(
                "accepted-request index is unavailable"
            ) from error
        index_by_run: dict[int, Mapping[str, Any]] = {}
        for item in indexed:
            if not isinstance(item, Mapping):
                raise ProfileRequestError(
                    "accepted-request index has a non-object entry"
                )
            if item.get("expired") is True:
                raise ProfileRequestError("accepted-request index artifact expired")
            workflow_run = item.get("workflow_run")
            if not isinstance(workflow_run, Mapping):
                raise ProfileRequestError(
                    "accepted-request index lacks workflow run metadata"
                )
            run_id = workflow_run.get("id")
            if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
                raise ProfileRequestError(
                    "accepted-request index has an invalid run id"
                )
            if run_id in index_by_run:
                raise ProfileRequestError(
                    "accepted-request index is ambiguous for a workflow run"
                )
            index_by_run[run_id] = item

        records: list[RequestRecord] = []
        seen: dict[tuple[int, int], RequestRecord] = {}
        for run_id in sorted(index_by_run):
            artifacts = self._client.paginated(
                self._repo_endpoint(f"/actions/runs/{run_id}/artifacts"),
                "artifacts",
            )
            primary_meta, additional = self._request_artifacts_for_run(
                pr_number, artifacts
            )
            attempt_pairs: list[tuple[int, Mapping[str, Any]]] = []
            if primary_meta is not None:
                attempt_pairs.append(
                    (self._resolve_primary_attempt(run_id, primary_meta), primary_meta)
                )
            for attempt in sorted(additional):
                attempt_pairs.append((attempt, additional[attempt]))
            for attempt, metadata in attempt_pairs:
                run = self._fetch_run(run_id, attempt)
                verified = self._verify_run(run)
                record = self._prove_record(
                    verified,
                    metadata,
                    str(metadata.get("name")),
                    pr_number,
                )
                key = (record.run_id, record.run_attempt)
                previous = seen.get(key)
                if previous is not None and previous != record:
                    raise ProfileRequestError(
                        "contradictory duplicate accepted request"
                    )
                seen[key] = record
                records.append(record)
        records.sort(
            key=lambda item: (item.accepted_at_ms, item.run_id, item.run_attempt)
        )
        return tuple(records)

    def choose_artifact_name(
        self, pr_number: int, current_run_id: int, current_attempt: int
    ) -> str:
        if (
            not isinstance(current_run_id, int)
            or isinstance(current_run_id, bool)
            or current_run_id <= 0
        ):
            raise ValueError("current_run_id must be a positive integer")
        if (
            not isinstance(current_attempt, int)
            or isinstance(current_attempt, bool)
            or not 1 <= current_attempt <= 1000
        ):
            raise ValueError("current_attempt must be from 1 through 1000")
        primary = primary_artifact_name(pr_number)
        artifacts = self._client.paginated(
            self._repo_endpoint(f"/actions/runs/{current_run_id}/artifacts"),
            "artifacts",
        )
        has_primary = any(
            isinstance(item, Mapping)
            and item.get("name") == primary
            and item.get("expired") is False
            for item in artifacts
        )
        if has_primary:
            primary_meta = next(
                item for item in artifacts if item.get("name") == primary
            )
            primary_attempt = self._resolve_primary_attempt(
                current_run_id, primary_meta
            )
            name = (
                primary
                if primary_attempt == current_attempt
                else attempt_artifact_name(pr_number, current_attempt)
            )
        else:
            name = primary
        matches = [
            item
            for item in artifacts
            if isinstance(item, Mapping)
            and item.get("name") == name
            and item.get("expired") is False
        ]
        if matches:
            record = None
            try:
                run = self._fetch_run(current_run_id, current_attempt)
                verified = self._verify_run(run)
                record = self._prove_record(verified, matches[0], name, pr_number)
            except ProfileRequestError:
                record = None
            raise AlreadyAccepted(
                f"workflow run {current_run_id} attempt {current_attempt} already accepted a request",
                record,
            )
        return name

    def make_record(
        self,
        context_envelope: Mapping[str, Any],
        kind: str,
        pending: Optional[PendingRequest],
        run_id: int,
        run_attempt: int,
        accepted_at_ms: int,
        repository: str,
        pr_number: int,
    ) -> RequestRecord:
        if kind not in {"deep", "carry", "cancel"}:
            raise ProfileRequestError("kind must be deep, carry, or cancel")
        if (
            not isinstance(run_id, int)
            or isinstance(run_id, bool)
            or run_id <= 0
            or not isinstance(run_attempt, int)
            or isinstance(run_attempt, bool)
            or not 1 <= run_attempt <= 1000
            or not isinstance(accepted_at_ms, int)
            or isinstance(accepted_at_ms, bool)
            or accepted_at_ms < 0
        ):
            raise ProfileRequestError("run metadata is invalid")
        if type(repository) is not str or repository != self.repository:
            raise ProfileRequestError("repository mismatch")
        if (
            not isinstance(pr_number, int)
            or isinstance(pr_number, bool)
            or pr_number <= 0
        ):
            raise ProfileRequestError("pull request mismatch")
        try:
            context = restore_context(context_envelope)
        except SchemaError as error:
            raise ProfileRequestError("context envelope is invalid") from error
        if context.repository != repository or context.collected.pr_number != pr_number:
            raise ProfileRequestError(
                "context envelope does not match repository or pull request"
            )
        policy = context.collected.review_policy
        policy_digest = policy.digest if policy is not None else ""
        if kind == "cancel":
            if pending is None:
                raise ProfileRequestError("cancel requires an active pending request")
            origin_run_id = pending.origin_run_id
            if context.execution is not None:
                profile = context.execution.plan.profile
                registry_digest = context.execution.plan.registry_digest
            else:
                profile = pending.profile
                registry_digest = pending.registry_digest
            context_sha256 = ""
        else:
            if context.execution is None:
                raise ProfileRequestError("acceptance requires prepared execution")
            profile = context.execution.plan.profile
            registry_digest = context.execution.plan.registry_digest
            if not isinstance(
                context_envelope.get("sha256"), str
            ) or not _DIGEST.fullmatch(context_envelope["sha256"]):
                raise ProfileRequestError("context envelope lacks a valid digest")
            context_sha256 = context_envelope["sha256"]
            if kind == "deep" and pending is None:
                origin_run_id = None
            elif kind == "deep" and pending is not None:
                origin_run_id = pending.origin_run_id
            elif kind == "carry":
                if pending is None:
                    raise ProfileRequestError(
                        "carry requires an active pending request"
                    )
                origin_run_id = pending.origin_run_id
            else:
                raise ProfileRequestError("unsupported request kind")
        record = RequestRecord(
            REQUEST_VERSION,
            repository,
            pr_number,
            run_id,
            run_attempt,
            accepted_at_ms,
            kind,
            context.collected.head_sha,
            policy_digest,
            registry_digest,
            profile,
            origin_run_id,
            context_sha256,
        )
        as_event(record)
        return record

    def fold_history(
        self,
        records: Sequence[RequestRecord],
        completions: Sequence[VerifiedRunReceipt],
    ) -> Optional[PendingRequest]:
        return fold_requests([as_event(record) for record in records], completions)

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
            raise ProfileRequestError("workflow run response is not an object")
        return value

    def _verify_run(self, run: Mapping[str, Any]) -> VerifiedRun:
        if self._verify_historical_run is not None:
            try:
                verified = self._verify_historical_run(run)
            except ProfileGitHubError as error:
                raise ProfileRequestError(
                    "workflow run failed trusted provenance checks"
                ) from error
            self._bound_verified_run(run, verified)
            return verified
        try:
            return self._client.verify_run(
                run, self._caller_blob, require_success=False
            )
        except ProfileGitHubError as error:
            raise ProfileRequestError(
                "workflow run failed trusted provenance checks"
            ) from error

    def _bound_verified_run(
        self, run: Mapping[str, Any], verified: VerifiedRun
    ) -> None:
        if type(verified) is not VerifiedRun:
            raise ProfileRequestError(
                "historical verification did not return a VerifiedRun"
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
            raise ProfileRequestError("workflow run lacks required identity fields")
        if (
            verified.owner != self._client.config.owner
            or verified.repo != self._client.config.name
            or verified.run_id != run_id
            or verified.attempt != attempt
            or verified.head_sha != head_sha
            or verified.event != event
        ):
            raise ProfileRequestError(
                "historical verification returned mismatched run bounds"
            )

    def _resolve_primary_attempt(self, run_id: int, metadata: Mapping[str, Any]) -> int:
        latest = self._client._api(self._repo_endpoint(f"/actions/runs/{run_id}"))
        if not isinstance(latest, Mapping):
            raise ProfileRequestError("workflow run response is not an object")
        verified = self._verify_run(latest)
        # Run-level artifacts survive reruns. Read the bounded candidate from
        # this verified run, then prove its OWN attempt and successful steps
        # in _prove_record. Reading the index does not accept its contents.
        candidate = parse_request(
            self._client.artifact(
                verified,
                str(metadata.get("name")),
                MAX_REQUEST_BYTES,
                ARTIFACT_FILENAME,
            )
        )
        if candidate.run_id != run_id or candidate.run_attempt > verified.attempt:
            raise ProfileRequestError("primary artifact has invalid run attempt bounds")
        return candidate.run_attempt

    def _request_artifacts_for_run(
        self, pr_number: int, artifacts: Sequence[Any]
    ) -> tuple[Optional[Mapping[str, Any]], dict[int, Mapping[str, Any]]]:
        primary = primary_artifact_name(pr_number)
        primary_meta: Optional[Mapping[str, Any]] = None
        additional: dict[int, Mapping[str, Any]] = {}
        for item in artifacts:
            if not isinstance(item, Mapping):
                raise ProfileRequestError(
                    "workflow artifacts response has a non-object entry"
                )
            name = item.get("name")
            if not isinstance(name, str):
                continue
            if name == primary:
                if item.get("expired") is True:
                    raise ProfileRequestError("accepted-request artifact expired")
                if primary_meta is not None:
                    raise ProfileRequestError(
                        "workflow run has ambiguous accepted-request artifacts"
                    )
                primary_meta = item
            elif name.startswith(f"{primary}-attempt-"):
                suffix = name[len(f"{primary}-attempt-") :]
                if not suffix.isdigit():
                    continue
                attempt = int(suffix)
                if not 1 < attempt <= 1000:
                    continue
                if item.get("expired") is True:
                    raise ProfileRequestError("accepted-request artifact expired")
                previous = additional.get(attempt)
                if previous is not None:
                    raise ProfileRequestError(
                        "workflow run has ambiguous accepted-request artifacts"
                    )
                additional[attempt] = item
        if primary_meta is None and not additional:
            raise ProfileRequestError(
                "indexed workflow run lacks accepted-request artifacts"
            )
        return primary_meta, additional

    def _accepting_job(self, verified: VerifiedRun) -> str:
        matches = [
            job_name
            for job_name in self._allowed_jobs
            if self._client.successful_steps(
                verified, job_name, [ACCEPT_STEP, PRESERVE_STEP]
            )
        ]
        if len(matches) != 1:
            raise ProfileRequestError(
                "accepted request lacks exactly one trusted producer job"
            )
        return matches[0]

    def _prove_record(
        self,
        verified: VerifiedRun,
        metadata: Mapping[str, Any],
        artifact_name: str,
        pr_number: int,
    ) -> RequestRecord:
        if verified.run_id != metadata.get("workflow_run", {}).get(
            "id", verified.run_id
        ):
            raise ProfileRequestError(
                "artifact metadata does not match the verified workflow run"
            )
        job_name = self._accepting_job(verified)
        accept_completed_ms = self._accept_step_completed_ms(verified, job_name)
        run_started_ms = _github_ms(
            verified.run.get("run_started_at"), "run_started_at"
        )
        created_ms = _github_ms(metadata.get("created_at"), "artifact created_at")
        raw = self._client.artifact(
            verified, artifact_name, MAX_REQUEST_BYTES, ARTIFACT_FILENAME
        )
        record = parse_request(raw)
        if record.repository != self.repository or record.pr_number != pr_number:
            raise ProfileRequestError(
                "accepted request does not match repository or pull request"
            )
        if record.run_id != verified.run_id or record.run_attempt != verified.attempt:
            raise ProfileRequestError(
                "accepted request does not match the verified workflow run"
            )
        if verified.event == "pull_request" and record.head_sha != verified.head_sha:
            raise ProfileRequestError(
                "accepted request head SHA does not match the verified run"
            )
        if not isinstance(artifact_name, str) or not artifact_name:
            raise ProfileRequestError("artifact name is missing")
        expected_primary = primary_artifact_name(pr_number)
        expected_rerun = attempt_artifact_name(pr_number, record.run_attempt)
        if artifact_name == expected_primary:
            pass
        elif artifact_name == expected_rerun and record.run_attempt > 1:
            pass
        else:
            raise ProfileRequestError(
                "artifact name does not match the accepted run attempt"
            )
        if record.accepted_at_ms < run_started_ms:
            raise ProfileRequestError("accepted_at_ms precedes workflow run start")
        if record.accepted_at_ms > accept_completed_ms + TIMESTAMP_TOLERANCE_MS:
            raise ProfileRequestError("accepted_at_ms is after the trusted accept step")
        if created_ms < accept_completed_ms:
            raise ProfileRequestError(
                "artifact was created before the trusted accept step completed"
            )
        digest = metadata.get("digest")
        if digest is not None:
            if not isinstance(digest, str) or not re.fullmatch(
                r"sha256:[0-9a-f]{64}", digest
            ):
                raise ProfileRequestError("artifact digest is malformed")
        return record

    def _accept_step_completed_ms(self, verified: VerifiedRun, job_name: str) -> int:
        jobs = [job for job in verified.jobs if job.get("name") == job_name]
        if len(jobs) != 1:
            raise ProfileRequestError("trusted producer job is missing")
        steps = jobs[0].get("steps")
        if not isinstance(steps, list):
            raise ProfileRequestError("trusted producer job lacks steps")
        matches = [
            step
            for step in steps
            if isinstance(step, Mapping) and step.get("name") == ACCEPT_STEP
        ]
        if len(matches) != 1:
            raise ProfileRequestError("trusted accept step is missing")
        step = matches[0]
        if step.get("status") != "completed" or step.get("conclusion") != "success":
            raise ProfileRequestError("trusted accept step did not succeed")
        return _github_ms(step.get("completed_at"), "accept step completed_at")
