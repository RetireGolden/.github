"""Fail-closed CLI orchestration for the OpenRouter profile gate.

This module intentionally does not execute checkout content.  The only local
Git operations are object reads against the inert, full-depth source checkout;
GitHub remains the authority for repository, pull-request, and run identity.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from or_pr_review.errors import SchemaError
from or_pr_review.profile_evidence import (
    PendingRequest,
    canonical_receipt,
)
from or_pr_review.review_context import restore_context
from or_pr_review.review_plan import parse_review_profiles
from or_pr_review.models import parse_model_routes
from or_pr_review.review_policy import resolve_policy

from scripts.profile_completion import (
    ProfileCompletionError,
    ReceiptEvidence,
    VerifiedReview,
)
from scripts.profile_github import (
    ProfileGitHub,
    ProfileGitHubError,
    TrustedWorkflow,
    VerifiedRun,
    strict_json,
)
from scripts.profile_requests import (
    AlreadyAccepted,
    ProfileRequestError,
    RequestHistory,
    canonical_request,
)


CALLER_PATH = ".github/workflows/openrouter-code-review.yml"
REQUEST_JOBS = (
    "review / OpenRouter first-pass review",
    "review / OpenRouter follow-up review",
)
REVIEW_STEPS_BY_JOB = {
    "review / OpenRouter first-pass review": ("Run OpenRouter first-pass review",),
    "review / OpenRouter follow-up review": ("Run OpenRouter follow-up review",),
}
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_STATE_BYTES = 64 * 1024
MAX_MATRIX_ITEMS = 100
MAX_COMMIT_STATUSES = 100
REFRESH_MARKER_PREFIX = "Refresh requested "
# Context artifacts are retained for 30 days. Bound the whole PR lifecycle
# below that horizon so automatic expiry cannot erase accepted requests.
MAX_PR_AGE_SECONDS = 25 * 24 * 60 * 60
SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
TITLE = re.compile(r"\AOpenRouter PR #([1-9][0-9]*): (auto|deep|cancel)\Z")
PIN = re.compile(
    r"RetireGolden/\.github/\.github/workflows/openrouter-code-review\.yml@([0-9a-f]{40})(?![0-9a-f])"
)


class GateError(RuntimeError):
    """An input, provenance proof, or race check did not meet gate bounds."""


def _env(name: str, *, required: bool = True) -> str:
    value = os.environ.get(name, "")
    if required and not value:
        raise GateError(f"{name} is required")
    if "\r" in value or "\n" in value or len(value) > 8192:
        raise GateError(f"{name} is malformed")
    return value


def _positive(value: str, name: str) -> int:
    if not value.isdecimal() or int(value) <= 0:
        raise GateError(f"{name} must be a positive integer")
    return int(value)


def _sha(value: str, name: str) -> str:
    if not SHA.fullmatch(value):
        raise GateError(f"{name} must be a lowercase full SHA")
    return value


def _digest(value: str, name: str) -> str:
    if not DIGEST.fullmatch(value):
        raise GateError(f"{name} must be a SHA-256 digest")
    return value


def _read(path: Path, cap: int) -> bytes:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise GateError(f"cannot read {path.name}") from error
    if len(data) > cap:
        raise GateError(f"{path.name} exceeds its size limit")
    return data


def _json_file(path: Path, cap: int) -> Any:
    try:
        return strict_json(_read(path, cap), max_bytes=cap, max_depth=24)
    except ProfileGitHubError as error:
        raise GateError(f"{path.name} is not valid bounded JSON") from error


def _output(values: Mapping[str, Any]) -> None:
    target = _env("GITHUB_OUTPUT")
    lines: list[str] = []
    for name, value in values.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise GateError("invalid output name")
        if isinstance(value, (dict, list)):
            rendered = json.dumps(
                value, separators=(",", ":"), sort_keys=True, ensure_ascii=True
            )
        elif isinstance(value, bool):
            rendered = "true" if value else "false"
        else:
            rendered = str(value)
        if "\r" in rendered or "\n" in rendered or len(rendered) > 32768:
            raise GateError("output is malformed or exceeds its size limit")
        lines.append(f"{name}={rendered}\n")
    try:
        with open(target, "a", encoding="utf-8", newline="\n") as handle:
            handle.writelines(lines)
    except OSError as error:
        raise GateError("cannot write GITHUB_OUTPUT") from error


def _git(source: Path, args: Sequence[str], *, cap: int = 2 * 1024 * 1024) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(source), "-c", "core.hooksPath=/dev/null", *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GateError("trusted source Git object lookup failed") from error
    if result.returncode or len(result.stdout) > cap:
        raise GateError("trusted source Git object lookup failed")
    return result.stdout


def _source_workspace() -> Path:
    path = Path(_env("SOURCE_WORKSPACE")).resolve()
    if not path.is_dir():
        raise GateError("SOURCE_WORKSPACE is not a directory")
    return path


def _file_at(source: Path, commit: str, path: str = CALLER_PATH) -> tuple[str, bytes]:
    object_name = (
        _git(source, ["rev-parse", "--verify", f"{commit}:{path}"])
        .decode("ascii", "strict")
        .strip()
    )
    blob = _sha(object_name, "trusted source caller blob")
    content = _git(source, ["cat-file", "blob", blob], cap=MAX_FILE_BYTES)
    return blob, content


def _caller_pin(content: bytes) -> str:
    try:
        text = content.decode("utf-8", "strict")
    except UnicodeDecodeError as error:
        raise GateError("trusted caller file is not UTF-8") from error
    pins = PIN.findall(text)
    if len(pins) != 1:
        raise GateError(
            "trusted caller must contain exactly one immutable reusable workflow pin"
        )
    return pins[0]


def _repository_parts() -> tuple[str, str]:
    value = _env("GITHUB_REPOSITORY")
    parts = value.split("/")
    if len(parts) != 2 or not all(parts):
        raise GateError("GITHUB_REPOSITORY must be owner/name")
    return parts[0], parts[1]


def _client() -> ProfileGitHub:
    owner, name = _repository_parts()
    placeholder = _sha(_env("ORG_WORKFLOW_SHA"), "ORG_WORKFLOW_SHA")
    # default_branch is replaced with the GitHub-authoritative value before use.
    return ProfileGitHub(
        TrustedWorkflow(owner, name, "main", reusable_sha=placeholder),
        _env("GITHUB_TOKEN"),
    )


def _live_main(client: ProfileGitHub) -> tuple[str, str]:
    repo = client.get_repo()
    branch = repo.get("default_branch") if isinstance(repo, Mapping) else None
    if not isinstance(branch, str) or not branch or "\n" in branch or "\r" in branch:
        raise GateError("live repository lacks a valid default branch")
    reference = client._api(
        f"/repos/{client.config.owner}/{client.config.name}/git/ref/heads/{branch}"
    )
    obj = reference.get("object") if isinstance(reference, Mapping) else None
    sha = obj.get("sha") if isinstance(obj, Mapping) else None
    return branch, _sha(sha if isinstance(sha, str) else "", "live default branch SHA")


def _configured_client(
    client: ProfileGitHub, default_branch: str, pin: str
) -> ProfileGitHub:
    return ProfileGitHub(
        dataclasses.replace(
            client.config, default_branch=default_branch, reusable_sha=pin
        ),
        client.token,
        client.transport,
        client.timeout,
    )


def _current_trust(
    client: ProfileGitHub, *, allow_pin_mismatch: bool = False
) -> tuple[ProfileGitHub, str, str, bytes]:
    """Return current config, main SHA, caller blob, and locally attested caller bytes."""
    branch, main_sha = _live_main(client)
    api_blob = client._content_blob(main_sha)
    local_blob, content = _file_at(_source_workspace(), main_sha)
    if local_blob != api_blob:
        raise GateError(
            "trusted source checkout does not contain the live default caller blob"
        )
    pin = _caller_pin(content)
    expected = _sha(_env("ORG_WORKFLOW_SHA"), "ORG_WORKFLOW_SHA")
    if pin != expected and not allow_pin_mismatch:
        raise GateError("current default caller does not use ORG_WORKFLOW_SHA")
    return _configured_client(client, branch, pin), main_sha, api_blob, content


def _historical_verifier(
    client: ProfileGitHub, default_branch: str, main_sha: str
) -> Callable[[Mapping[str, Any]], VerifiedRun]:
    """Verify a run against a caller blob actually reachable from live main.

    GitHub supplies the run-head caller blob.  The inert checkout only proves that
    identical blob was in the bounded history of the GitHub-attested main commit.
    """
    source = _source_workspace()
    lines = _git(
        source, ["log", "--format=%H", "--max-count=1001", main_sha, "--", CALLER_PATH]
    ).splitlines()
    if len(lines) > 1000:
        raise GateError("caller history exceeds its retention bound")
    candidates: dict[str, bytes] = {}
    for raw in lines:
        commit = raw.decode("ascii", "strict")
        if not SHA.fullmatch(commit):
            raise GateError("trusted source history contains an invalid commit")
        blob, content = _file_at(source, commit)
        candidates.setdefault(blob, content)
    if not candidates:
        raise GateError("live default history lacks the trusted caller")

    def verify(run: Mapping[str, Any]) -> VerifiedRun:
        head = run.get("head_sha") if isinstance(run, Mapping) else None
        if not isinstance(head, str) or not SHA.fullmatch(head):
            raise ProfileGitHubError("workflow run lacks a full commit SHA")
        actual_blob = client._content_blob(head)
        content = candidates.get(actual_blob)
        if content is None:
            raise ProfileGitHubError(
                "run caller blob is not reachable from trusted default history"
            )
        pin = _caller_pin(content)
        historical = _configured_client(client, default_branch, pin)
        return historical.verify_run(run, actual_blob, require_success=False)

    return verify


class _ReceiptStepAdapter(ProfileGitHub):
    """Present the legacy receipt collector with each producer's own step.

    ``ReceiptEvidence`` currently asks every allowed job to satisfy every
    supplied step.  The caller workflow has one distinct review step in each
    producer job, so adapt that interface here until its contract accepts a
    single allowed named step in the exact successful job.
    """

    def __init__(
        self, client: ProfileGitHub, steps_by_job: Mapping[str, Sequence[str]]
    ) -> None:
        # Keep the real client transport and configuration.  Delegating only
        # through ``__getattr__`` would not be enough: inherited API helpers
        # (for artifacts, reviews, and runs) resolve on this instance.
        super().__init__(client.config, client.token, client.transport, client.timeout)
        self._steps_by_job = {
            name: tuple(steps) for name, steps in steps_by_job.items()
        }

    def successful_steps(
        self, verified_run: VerifiedRun, job_name: str, step_names: list[str]
    ) -> bool:
        expected = self._steps_by_job.get(job_name)
        if expected is None:
            return False
        return super().successful_steps(verified_run, job_name, list(expected))


def _receipt_evidence(
    client: ProfileGitHub, verifier: Callable[[Mapping[str, Any]], VerifiedRun]
) -> ReceiptEvidence:
    adapter = _ReceiptStepAdapter(client, REVIEW_STEPS_BY_JOB)
    # The adapter maps this compatibility marker to the exact per-job list.
    return ReceiptEvidence(adapter, verifier, REQUEST_JOBS, ("review producer step",))


def _history_and_evidence(
    client: ProfileGitHub,
    default_branch: str,
    main_sha: str,
    current_blob: str,
) -> tuple[RequestHistory, ReceiptEvidence, ReceiptEvidence]:
    historical = _historical_verifier(client, default_branch, main_sha)
    history = RequestHistory(
        client, current_blob, REQUEST_JOBS, verify_historical_run=historical
    )
    old_receipts = _receipt_evidence(client, historical)
    current = _receipt_evidence(
        client, lambda run: client.verify_run(run, current_blob)
    )
    return history, old_receipts, current


def _live_pr(
    client: ProfileGitHub, number: int, default_branch: str
) -> Mapping[str, Any]:
    pr = client.get_pr(number)
    created = pr.get("created_at") if isinstance(pr, Mapping) else None
    try:
        created_at = datetime.fromisoformat(created.replace("Z", "+00:00"))
        if created_at.tzinfo is None:
            raise ValueError("missing timezone")
        age = datetime.now(timezone.utc).timestamp() - created_at.timestamp()
    except (AttributeError, TypeError, ValueError) as error:
        raise GateError("pull request creation time is unavailable") from error
    if age < -300 or age >= MAX_PR_AGE_SECONDS:
        raise GateError(
            "profile evidence retention requires a PR younger than 25 days; open a replacement PR"
        )
    head = pr.get("head") if isinstance(pr, Mapping) else None
    base = pr.get("base") if isinstance(pr, Mapping) else None
    repo = head.get("repo") if isinstance(head, Mapping) else None
    base_repo = base.get("repo") if isinstance(base, Mapping) else None
    if (
        pr.get("state") != "open"
        or pr.get("draft") is True
        or not isinstance(head, Mapping)
        or not isinstance(base, Mapping)
        or not isinstance(repo, Mapping)
        or repo.get("full_name") != client.config.full_name
        or not isinstance(base_repo, Mapping)
        or base_repo.get("full_name") != client.config.full_name
        or base.get("ref") != default_branch
    ):
        raise GateError(
            "pull request is not an open same-repository non-draft default-branch PR"
        )
    _sha(
        head.get("sha") if isinstance(head.get("sha"), str) else "",
        "live pull request head SHA",
    )
    _sha(
        base.get("sha") if isinstance(base.get("sha"), str) else "",
        "live pull request base SHA",
    )
    return pr


def _pr_shas(pr: Mapping[str, Any]) -> tuple[str, str]:
    head, base = pr["head"], pr["base"]
    return _sha(head["sha"], "live pull request head SHA"), _sha(
        base["sha"], "live pull request base SHA"
    )


def _configs() -> tuple[str, str]:
    root = Path(_env("PROFILE_ROOT")).resolve()
    registry = _read(root / "review-profiles.json", 1024 * 1024).decode(
        "utf-8", "strict"
    )
    routes = _read(root / "review-model-routes.json", 1024 * 1024).decode(
        "utf-8", "strict"
    )
    try:
        parse_review_profiles(registry)
        parse_model_routes(routes)
    except Exception as error:
        raise GateError("trusted review profile configuration is invalid") from error
    return registry, routes


def _run_url(client: ProfileGitHub, run_id: int) -> str:
    return f"https://github.com/{client.config.full_name}/actions/runs/{run_id}"


def _current_run(client: ProfileGitHub) -> Mapping[str, Any]:
    run_id = _positive(_env("GITHUB_RUN_ID"), "GITHUB_RUN_ID")
    attempt = _positive(_env("GITHUB_RUN_ATTEMPT"), "GITHUB_RUN_ATTEMPT")
    run = client._api(
        f"/repos/{client.config.owner}/{client.config.name}/actions/runs/{run_id}/attempts/{attempt}"
    )
    if (
        not isinstance(run, Mapping)
        or run.get("id") != run_id
        or run.get("run_attempt") != attempt
    ):
        raise GateError("current workflow run identity changed")
    return run


def _verify_begin_run(
    client: ProfileGitHub, current_blob: str, *, pin_matches_main: bool
) -> bool:
    run = _current_run(client)
    if not pin_matches_main:
        if _env("REVIEW_LEVEL") != "auto":
            raise GateError("current run is not trusted by the current default caller")
        head = run.get("head_sha")
        if not isinstance(head, str) or not SHA.fullmatch(head):
            raise GateError("current run has no valid head SHA")
        try:
            candidate_blob = client._content_blob(head)
            migration = _configured_client(
                client,
                client.config.default_branch,
                _sha(_env("ORG_WORKFLOW_SHA"), "ORG_WORKFLOW_SHA"),
            )
            migration.verify_run(run, candidate_blob)
        except ProfileGitHubError as error:
            raise GateError("migration audit run has invalid provenance") from error
        return True
    try:
        client.verify_run(run, current_blob)
        return False
    except ProfileGitHubError:
        # Migration has no gate authority. It only admits an auto audit run which
        # still proves the reusable pin and is deliberately never persisted.
        if _env("REVIEW_LEVEL") != "auto":
            raise GateError("current run is not trusted by the current default caller")
        head = run.get("head_sha")
        if not isinstance(head, str) or not SHA.fullmatch(head):
            raise GateError("current run has no valid head SHA")
        try:
            candidate_blob = client._content_blob(head)
            migration = _configured_client(
                client,
                client.config.default_branch,
                _sha(_env("ORG_WORKFLOW_SHA"), "ORG_WORKFLOW_SHA"),
            )
            migration.verify_run(run, candidate_blob)
        except ProfileGitHubError as error:
            raise GateError("migration audit run has invalid provenance") from error
        return True


def _run_plausible_pr(run: Mapping[str, Any], number: int) -> bool:
    """Pre-verify PR filter using attested metadata only; never authoritative."""
    title = run.get("display_title")
    match = TITLE.fullmatch(title) if isinstance(title, str) else None
    if match and int(match.group(1)) == number:
        return True
    pulls = run.get("pull_requests")
    if isinstance(pulls, list):
        return any(
            isinstance(item, Mapping)
            and isinstance(item.get("number"), int)
            and item["number"] == number
            for item in pulls
        )
    return False


def _verified_review_run_matches(
    run: Mapping[str, Any], number: int, level: str
) -> bool:
    title = run.get("display_title")
    match = TITLE.fullmatch(title) if isinstance(title, str) else None
    if match and int(match.group(1)) == number and match.group(2) == level:
        return True
    pulls = run.get("pull_requests")
    if isinstance(pulls, list) and len(pulls) == 1 and isinstance(pulls[0], Mapping):
        pr_number = pulls[0].get("number")
        if isinstance(pr_number, int) and pr_number == number:
            return True
    return False


def _trusted_status_creator(creator: Any, client: ProfileGitHub) -> bool:
    return (
        isinstance(creator, Mapping)
        and creator.get("login") == client.config.bot_login
        and creator.get("id") == client.config.bot_id
        and creator.get("type") == client.config.bot_type
    )


def _commit_statuses(client: ProfileGitHub, head: str) -> list[Any]:
    return client.paginated(
        f"/repos/{client.config.owner}/{client.config.name}/commits/{head}/statuses",
        max_items=MAX_COMMIT_STATUSES,
    )


def _refresh_marker_description(fingerprint: str) -> str:
    description = REFRESH_MARKER_PREFIX + _digest(fingerprint, "refresh fingerprint")
    if len(description) > 140:
        raise GateError("refresh marker description exceeds its size limit")
    return description


def _refresh_identity_fingerprint(
    review: VerifiedReview,
    repo_root: Path,
    base: str,
    head: str,
    registry: str,
    routes: str,
) -> str:
    carried_paths = ReceiptEvidence._carried_paths(review.context)
    try:
        current_policy = resolve_policy(
            repo_root, base, head, carried_paths=carried_paths
        )
    except Exception as error:
        raise GateError(
            "current policy could not be resolved for refresh dedupe"
        ) from error
    material = f"{current_policy.digest}\n{registry}\n{routes}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _refresh_marker_present(client: ProfileGitHub, head: str, fingerprint: str) -> bool:
    expected = _refresh_marker_description(fingerprint)
    for status in _commit_statuses(client, head):
        if (
            not isinstance(status, Mapping)
            or status.get("context") != "openrouter-profile"
        ):
            continue
        if not _trusted_status_creator(status.get("creator"), client):
            continue
        if status.get("description") == expected:
            return True
    return False


def _state_path() -> Path:
    path = Path(_env("REQUEST_STATE_FILE")).resolve()
    parent = path.parent
    if not parent.is_dir() or path.name in {"", ".", ".."}:
        raise GateError("REQUEST_STATE_FILE is invalid")
    return path


def _write_state(value: Mapping[str, Any]) -> str:
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    if len(raw) > MAX_STATE_BYTES:
        raise GateError("request state exceeds its size limit")
    path = _state_path()
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
    except FileExistsError as error:
        raise GateError("request state already exists") from error
    except OSError as error:
        raise GateError("cannot write request state") from error
    return str(path)


def _load_state() -> Mapping[str, Any]:
    value = _json_file(_state_path(), MAX_STATE_BYTES)
    required = {
        "repository",
        "pr_number",
        "head_sha",
        "base_sha",
        "run_id",
        "run_attempt",
        "request_kind",
        "audit_only",
        "pending",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise GateError("request state has invalid keys")
    if (
        value["repository"] != _env("GITHUB_REPOSITORY")
        or not isinstance(value["pr_number"], int)
        or value["pr_number"] <= 0
    ):
        raise GateError("request state identity is invalid")
    for key in ("head_sha", "base_sha"):
        _sha(value[key] if isinstance(value[key], str) else "", f"request state {key}")
    if (
        not isinstance(value["run_id"], int)
        or isinstance(value["run_id"], bool)
        or value["run_id"] <= 0
        or not isinstance(value["run_attempt"], int)
        or isinstance(value["run_attempt"], bool)
        or not 1 <= value["run_attempt"] <= 1000
    ):
        raise GateError("request state workflow run is invalid")
    if (
        value["request_kind"] not in {"deep", "carry", "cancel", "none", "noop"}
        or type(value["audit_only"]) is not bool
    ):
        raise GateError("request state kind is invalid")
    if value["pending"] is not None and not isinstance(value["pending"], Mapping):
        raise GateError("request state pending request is invalid")
    return value


def _pending_dict(pending: Optional[PendingRequest]) -> Optional[dict[str, Any]]:
    return None if pending is None else dataclasses.asdict(pending)


def _request_pending(value: Optional[Mapping[str, Any]]) -> Optional[PendingRequest]:
    if value is None:
        return None
    try:
        pending = PendingRequest(**dict(value))
    except (TypeError, ValueError) as error:
        raise GateError("request state pending request is invalid") from error
    if (
        not isinstance(pending.origin_run_id, int)
        or pending.origin_run_id <= 0
        or not isinstance(pending.last_accepted_run_id, int)
        or pending.last_accepted_run_id <= 0
        or not isinstance(pending.last_accepted_run_attempt, int)
        or not 1 <= pending.last_accepted_run_attempt <= 1000
    ):
        raise GateError("request state pending request is invalid")
    _sha(pending.head_sha, "request state pending head SHA")
    if pending.policy_digest:
        _digest(pending.policy_digest, "request state pending policy digest")
    _digest(pending.registry_digest, "request state pending registry digest")
    if not isinstance(pending.profile, str) or not pending.profile:
        raise GateError("request state pending profile is invalid")
    return pending


def begin() -> None:
    level = _env("REVIEW_LEVEL")
    if level not in {"auto", "deep", "cancel"}:
        raise GateError("REVIEW_LEVEL must be auto, deep, or cancel")
    bare, main_sha, current_blob, _ = _current_trust(_client(), allow_pin_mismatch=True)
    pin_matches_main = bare.config.reusable_sha == _sha(
        _env("ORG_WORKFLOW_SHA"), "ORG_WORKFLOW_SHA"
    )
    audit_only = _verify_begin_run(
        bare, current_blob, pin_matches_main=pin_matches_main
    )
    event = _env("GITHUB_EVENT_NAME")
    if level in {"deep", "cancel"}:
        if (
            event != "workflow_dispatch"
            or _env("GITHUB_REF") != f"refs/heads/{bare.config.default_branch}"
        ):
            raise GateError(
                "explicit deep and cancel require default-branch workflow_dispatch"
            )
        actor = os.environ.get("GITHUB_TRIGGERING_ACTOR", "") or _env("GITHUB_ACTOR")
        if not bare.maintainer(actor):
            raise GateError("explicit deep and cancel require a maintainer")
        if not pin_matches_main:
            raise GateError("explicit deep and cancel refuse a caller-pin migration")
        if audit_only:
            raise GateError(
                "explicit deep and cancel refuse an audit-only migration run"
            )
    number = _positive(_env("PR_NUMBER"), "PR_NUMBER")
    pr = _live_pr(bare, number, bare.config.default_branch)
    head, base = _pr_shas(pr)
    supplied_head = os.environ.get("HEAD_SHA", "")
    if supplied_head and _sha(supplied_head, "HEAD_SHA") != head:
        raise GateError("HEAD_SHA does not match the live pull request")
    if not audit_only:
        run = _current_run(bare)
        title = run.get("display_title")
        match = TITLE.fullmatch(title) if isinstance(title, str) else None
        if not match or int(match.group(1)) != number or match.group(2) != level:
            raise GateError(
                "review run title does not match the requested pull request and level"
            )
    history, receipts, _ = _history_and_evidence(
        bare, bare.config.default_branch, main_sha, current_blob
    )
    records = history.load(number)
    completions = (
        receipts.collect(
            number,
            relevant_runs={(record.run_id, record.run_attempt) for record in records},
            include_latest=False,
        )
        if records
        else ()
    )
    pending = history.fold_history(
        records, tuple(item.completion for item in completions)
    )
    if level == "cancel":
        kind = "cancel" if pending is not None else "noop"
    elif level == "deep":
        kind = "deep"
    elif pending is not None:
        kind = "carry"
    else:
        kind = "none"
    if audit_only:
        kind = "none"
    state_file = _write_state(
        {
            "repository": bare.config.full_name,
            "pr_number": number,
            "head_sha": head,
            "base_sha": base,
            "run_id": _positive(_env("GITHUB_RUN_ID"), "GITHUB_RUN_ID"),
            "run_attempt": _positive(_env("GITHUB_RUN_ATTEMPT"), "GITHUB_RUN_ATTEMPT"),
            "request_kind": kind,
            "audit_only": audit_only,
            "pending": _pending_dict(pending),
        }
    )
    _output(
        {
            "review_level": "deep"
            if kind in {"deep", "carry"} or (audit_only and pending is not None)
            else "auto",
            "request_kind": kind,
            "skip_review": kind in {"cancel", "noop"},
            "head_sha": head,
            "state_file": state_file,
            "audit_only": audit_only,
        }
    )


def _context() -> Mapping[str, Any]:
    path = Path(_env("REVIEW_CONTEXT_FILE"))
    raw = _read(path, MAX_FILE_BYTES)
    digest = _digest(_env("REVIEW_CONTEXT_SHA256"), "REVIEW_CONTEXT_SHA256")
    value = strict_json(raw, max_bytes=MAX_FILE_BYTES, max_depth=24)
    if not isinstance(value, Mapping) or value.get("sha256") != digest:
        raise GateError("review context envelope digest is invalid")
    try:
        restore_context(value)
    except SchemaError as error:
        raise GateError("review context envelope is invalid") from error
    return value


def accept() -> None:
    state = _load_state()
    if state["audit_only"]:
        _output({"request_file": "", "request_artifact": ""})
        return
    bare, main_sha, current_blob, _ = _current_trust(_client())
    if state["run_id"] != _positive(_env("GITHUB_RUN_ID"), "GITHUB_RUN_ID") or state[
        "run_attempt"
    ] != _positive(_env("GITHUB_RUN_ATTEMPT"), "GITHUB_RUN_ATTEMPT"):
        raise GateError("workflow run changed after setup")
    pr = _live_pr(bare, state["pr_number"], bare.config.default_branch)
    head, base = _pr_shas(pr)
    if head != state["head_sha"] or base != state["base_sha"]:
        raise GateError("pull request changed after setup")
    context = _context()
    restored = restore_context(context)
    if (
        restored.repository != bare.config.full_name
        or restored.collected.pr_number != state["pr_number"]
        or restored.collected.head_sha != head
    ):
        raise GateError("review context does not bind to the live pull request")
    execution = restored.execution
    if (
        execution is None
        or execution.source_run_url != _run_url(bare, state["run_id"])
        or execution.run_attempt != state["run_attempt"]
    ):
        raise GateError("review context does not bind to this workflow run attempt")
    policy = restored.collected.review_policy
    if policy is None or policy.base_sha != base:
        raise GateError(
            "review context policy base does not bind to the live pull request"
        )
    kind = state["request_kind"]
    if kind in {"none", "noop"}:
        _output({"request_file": "", "request_artifact": ""})
        return
    history, _, _ = _history_and_evidence(
        bare, bare.config.default_branch, main_sha, current_blob
    )
    pending = _request_pending(state["pending"])
    run_id, attempt = state["run_id"], state["run_attempt"]
    # An explicit deep request supersedes an outstanding obligation.  Passing
    # no pending origin makes RequestHistory record it as a fresh deep root;
    # automatic carries retain the existing origin.
    record_pending = None if kind == "deep" else pending
    record = history.make_record(
        context,
        kind,
        record_pending,
        run_id,
        attempt,
        int(time.time() * 1000),
        bare.config.full_name,
        state["pr_number"],
    )
    try:
        name = history.choose_artifact_name(state["pr_number"], run_id, attempt)
    except AlreadyAccepted as error:
        old = error.record
        if old is None or any(
            getattr(old, key) != getattr(record, key)
            for key in (
                "repository",
                "pr_number",
                "run_id",
                "run_attempt",
                "kind",
                "head_sha",
                "policy_digest",
                "registry_digest",
                "profile",
                "origin_run_id",
                "context_sha256",
            )
        ):
            raise GateError(
                "existing accepted request does not match current bounds"
            ) from error
        _output({"request_file": "", "request_artifact": ""})
        return
    # This is deliberately before workflow artifact upload.  The request reader
    # later requires both acceptance and preservation steps before accepting it.
    bare.create_status(
        head, "pending", _run_url(bare, run_id), "OpenRouter profile review accepted"
    )
    directory = (
        _state_path().parent / "openrouter-profile-gate" / str(run_id) / str(attempt)
    )
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    request_path = directory / "request.json"
    try:
        fd = os.open(str(request_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(canonical_request(record))
    except FileExistsError as error:
        raise GateError("request output already exists") from error
    except OSError as error:
        raise GateError("cannot write request output") from error
    _output({"request_file": str(request_path), "request_artifact": name})


def _receipt_digest(item: VerifiedReview) -> str:
    return hashlib.sha256(canonical_receipt(item.receipt)).hexdigest()


def _active_matching_run(
    client: ProfileGitHub,
    verifier: Callable[[Mapping[str, Any]], VerifiedRun],
    number: int,
    head: str,
) -> bool:
    runs = client.paginated(
        f"/repos/{client.config.owner}/{client.config.name}/actions/workflows/openrouter-code-review.yml/runs",
        "workflow_runs",
        1000,
    )
    for run in runs:
        if not isinstance(run, Mapping) or run.get("status") == "completed":
            continue
        if not _run_plausible_pr(run, number) and not (
            run.get("event") == "pull_request" and run.get("head_sha") == head
        ):
            continue
        try:
            verified = verifier(run)
        except ProfileGitHubError:
            # This run cannot provide review evidence, but it is still doing
            # work for the PR. Defer additional paid work until it finishes.
            return True
        if _run_plausible_pr(verified.run, number) or (
            verified.event == "pull_request" and verified.head_sha == head
        ):
            return True
    return False


def _dispatch(client: ProfileGitHub, number: int) -> None:
    endpoint = f"/repos/{client.config.owner}/{client.config.name}/actions/workflows/openrouter-code-review.yml/dispatches"
    payload = json.dumps(
        {
            "ref": client.config.default_branch,
            "inputs": {"pr_number": str(number), "review_level": "auto"},
        },
        separators=(",", ":"),
    ).encode("ascii")
    response = client._send("POST", "https://api.github.com" + endpoint, data=payload)
    if response.status != 204 or response.body:
        raise GateError(
            "workflow dispatch did not return the expected empty success response"
        )


def _dispatch_identity_refresh(
    client: ProfileGitHub,
    number: int,
    head: str,
    base: str,
    registry: str,
    routes: str,
    review: VerifiedReview,
    verifier: Callable[[Mapping[str, Any]], VerifiedRun],
) -> None:
    if _active_matching_run(client, verifier, number, head):
        return
    fingerprint = _refresh_identity_fingerprint(
        review, _source_workspace(), base, head, registry, routes
    )
    if _refresh_marker_present(client, head, fingerprint):
        return
    run_id = _positive(_env("GITHUB_RUN_ID"), "GITHUB_RUN_ID")
    client.create_status(
        head,
        "pending",
        _run_url(client, run_id),
        _refresh_marker_description(fingerprint),
    )
    try:
        _dispatch(client, number)
    except GateError:
        raise


def _identity_change(reason: str) -> bool:
    # These are the exact identity mismatch reasons returned by
    # ``evaluate_receipt`` after the current policy and plan have resolved.
    return reason in {
        "policy digest mismatch",
        "registry digest mismatch",
        "profile mismatch",
    }


def _is_current_receipt_source(
    client: ProfileGitHub, review: VerifiedReview, current_blob: str
) -> bool:
    """Keep historical proof separate from the stronger current-producer proof."""
    if client._content_blob(review.source.head_sha) != current_blob:
        return False
    expected = f"{client.config.reusable_path}@{client.config.reusable_sha}"
    references = review.source.run.get("referenced_workflows")
    return isinstance(references, list) and any(
        isinstance(item, Mapping)
        and item.get("path") == expected
        and item.get("sha") == client.config.reusable_sha
        for item in references
    )


def _plan_one(
    client: ProfileGitHub,
    main_sha: str,
    current_blob: str,
    number: int,
    *,
    auto_dispatch: bool,
    mutate: bool,
) -> Optional[dict[str, Any]]:
    pr = _live_pr(client, number, client.config.default_branch)
    head, base = _pr_shas(pr)
    registry, routes = _configs()
    history, historical_receipts, current_receipts = _history_and_evidence(
        client, client.config.default_branch, main_sha, current_blob
    )
    records = history.load(number)
    all_reviews = historical_receipts.collect(
        number,
        relevant_runs={(record.run_id, record.run_attempt) for record in records},
    )
    pending = history.fold_history(
        records, tuple(item.completion for item in all_reviews)
    )
    minimum = "deep" if pending is not None else "standard"
    # A historical collector must see old producer pins so it can settle old
    # obligations.  For present-day proof, the newest receipt for this exact
    # head must itself come from the current producer.  Do not silently fall
    # back to an older clean receipt if a newer one has stale provenance.
    candidates = [item for item in all_reviews if item.receipt.head_sha == head]
    latest = candidates[-1] if candidates else None
    if latest is not None:
        if not _is_current_receipt_source(client, latest, current_blob):
            if mutate:
                client.create_status(
                    head,
                    "failure",
                    _run_url(client, _positive(_env("GITHUB_RUN_ID"), "GITHUB_RUN_ID")),
                    "OpenRouter profile evidence is not current",
                )
            return None
        decision = current_receipts.current_decision(
            latest, _source_workspace(), base, head, registry, routes, minimum=minimum
        )
        if decision.satisfied:
            if pending is not None:
                if mutate:
                    client.create_status(
                        head,
                        "pending",
                        _run_url(
                            client, _positive(_env("GITHUB_RUN_ID"), "GITHUB_RUN_ID")
                        ),
                        "OpenRouter profile obligation is unsettled",
                    )
                return None
            return {
                "pr_number": number,
                "head_sha": head,
                "base_sha": base,
                "receipt_digest": _receipt_digest(latest),
            }
        if auto_dispatch and pending is None and _identity_change(decision.reason):
            _dispatch_identity_refresh(
                client,
                number,
                head,
                base,
                registry,
                routes,
                latest,
                lambda run: client.verify_run(run, current_blob),
            )
        if mutate:
            client.create_status(
                head,
                "failure",
                _run_url(client, _positive(_env("GITHUB_RUN_ID"), "GITHUB_RUN_ID")),
                "OpenRouter profile evidence is not current",
            )
        return None
    if mutate:
        client.create_status(
            head,
            "pending" if pending is not None else "failure",
            _run_url(client, _positive(_env("GITHUB_RUN_ID"), "GITHUB_RUN_ID")),
            "OpenRouter profile receipt is missing",
        )
    return None


def _source_pr(
    client: ProfileGitHub, verifier: Callable[[Mapping[str, Any]], VerifiedRun]
) -> int:
    run_id = _positive(_env("SOURCE_RUN_ID"), "SOURCE_RUN_ID")
    run = client._api(
        f"/repos/{client.config.owner}/{client.config.name}/actions/runs/{run_id}"
    )
    if not isinstance(run, Mapping):
        raise GateError("source run is invalid")
    verified = verifier(run)
    title = verified.run.get("display_title")
    match = TITLE.fullmatch(title) if isinstance(title, str) else None
    if match:
        return int(match.group(1))
    pulls = verified.run.get("pull_requests")
    if (
        not isinstance(pulls, list)
        or len(pulls) != 1
        or not isinstance(pulls[0], Mapping)
    ):
        raise GateError("source run does not identify exactly one pull request")
    return _positive(str(pulls[0].get("number", "")), "source pull request number")


def plan(
    *, one_pr: Optional[int] = None, auto_dispatch: bool = True, mutate: bool = True
) -> list[dict[str, Any]]:
    bare, main_sha, current_blob, _ = _current_trust(_client())
    if _sha(_env("GITHUB_SHA"), "GITHUB_SHA") != main_sha:
        raise GateError(
            "completion workflow is not running at live default branch head"
        )
    historical = _historical_verifier(bare, bare.config.default_branch, main_sha)
    if one_pr is None and os.environ.get("SWEEP_OPEN_PRS") == "true":
        # Validate event provenance before using the event to wake a sweep.
        if os.environ.get("SOURCE_RUN_ID"):
            _source_pr(bare, historical)
        rows = bare.paginated(
            f"/repos/{bare.config.owner}/{bare.config.name}/pulls?state=open&base={bare.config.default_branch}",
            max_items=100,
        )
        numbers = []
        for row in rows:
            if not isinstance(row, Mapping) or type(row.get("number")) is not int:
                raise GateError("open pull request listing is invalid")
            if row.get("draft") is not True:
                numbers.append(row["number"])
    elif one_pr is not None:
        numbers = [one_pr]
    elif os.environ.get("PR_NUMBER"):
        numbers = [_positive(_env("PR_NUMBER"), "PR_NUMBER")]
    elif os.environ.get("SOURCE_RUN_ID"):
        numbers = [_source_pr(bare, historical)]
    else:
        rows = bare.paginated(
            f"/repos/{bare.config.owner}/{bare.config.name}/pulls?state=open&base={bare.config.default_branch}",
            max_items=100,
        )
        numbers = []
        for row in rows:
            if not isinstance(row, Mapping) or not isinstance(row.get("number"), int):
                raise GateError("open pull request listing is invalid")
            numbers.append(row["number"])
    matrix = []
    for number in sorted(set(numbers)):
        try:
            item = _plan_one(
                bare,
                main_sha,
                current_blob,
                number,
                auto_dispatch=auto_dispatch,
                mutate=mutate,
            )
        except (
            GateError,
            ProfileGitHubError,
            ProfileRequestError,
            ProfileCompletionError,
        ) as error:
            if one_pr is not None or not mutate:
                raise
            # An expired or malformed PR cannot starve proofs for other PRs.
            pr = bare.get_pr(number)
            head = _sha(pr["head"]["sha"], "failed proof head SHA")
            bare.create_status(
                head,
                "failure",
                _run_url(bare, _positive(_env("GITHUB_RUN_ID"), "GITHUB_RUN_ID")),
                "OpenRouter profile proof unavailable; inspect workflow",
            )
            print(f"Profile proof for PR #{number} failed: {error}", file=sys.stderr)
            continue
        if item is not None:
            matrix.append(item)
    _output({"matrix": matrix, "has_work": bool(matrix)})
    return matrix


def confirm() -> None:
    number = _positive(_env("PR_NUMBER"), "PR_NUMBER")
    expected = _digest(_env("EXPECTED_RECEIPT_DIGEST"), "EXPECTED_RECEIPT_DIGEST")
    matrix = plan(one_pr=number, auto_dispatch=False, mutate=False)
    if len(matrix) != 1 or matrix[0]["receipt_digest"] != expected:
        raise GateError("receipt is no longer eligible for confirmation")


def _proof_matrix() -> list[Mapping[str, Any]]:
    raw = _env("PROOF_MATRIX_JSON")
    try:
        value = strict_json(raw.encode("utf-8"), max_bytes=32768, max_depth=8)
    except ProfileGitHubError as error:
        raise GateError("PROOF_MATRIX_JSON is invalid") from error
    if not isinstance(value, list) or len(value) > MAX_MATRIX_ITEMS:
        raise GateError("proof matrix is invalid or exceeds its size limit")
    seen: set[int] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {
            "pr_number",
            "head_sha",
            "base_sha",
            "receipt_digest",
        }:
            raise GateError("proof matrix item has invalid keys")
        number = item["pr_number"]
        if not isinstance(number, int) or number <= 0 or number in seen:
            raise GateError("proof matrix pull request is invalid")
        seen.add(number)
        _sha(item["head_sha"], "proof matrix head SHA")
        _sha(item["base_sha"], "proof matrix base SHA")
        _digest(item["receipt_digest"], "proof matrix receipt digest")
    return value


def publish() -> None:
    bare, main_sha, current_blob, _ = _current_trust(_client())
    if _sha(_env("GITHUB_SHA"), "GITHUB_SHA") != main_sha:
        raise GateError("publish workflow is not running at live default branch head")
    run = _current_run(bare)
    jobs = bare.paginated(
        f"/repos/{bare.config.owner}/{bare.config.name}/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs",
        "jobs",
    )
    failures: list[str] = []
    for item in _proof_matrix():
        number, expected_head, expected_base, digest = (
            item["pr_number"],
            item["head_sha"],
            item["base_sha"],
            item["receipt_digest"],
        )
        head_for_failure = expected_head
        try:
            pr = _live_pr(bare, number, bare.config.default_branch)
            head, base = _pr_shas(pr)
            head_for_failure = head
            if head != expected_head or base != expected_base:
                raise GateError("pull request changed after planning")
            name = f"complete / profile #{number} {digest}"
            matches = [
                job
                for job in jobs
                if isinstance(job, Mapping) and job.get("name") == name
            ]
            if (
                len(matches) != 1
                or matches[0].get("status") != "completed"
                or matches[0].get("conclusion") != "success"
            ):
                raise GateError(
                    "required confirmation job did not succeed exactly once"
                )
            fresh = _plan_one(
                bare, main_sha, current_blob, number, auto_dispatch=False, mutate=False
            )
            if fresh != item:
                raise GateError("profile obligations changed after confirmation")
            if _live_main(bare)[1] != main_sha:
                raise GateError("default branch changed before publication")
            bare.create_status(
                head,
                "success",
                _run_url(bare, int(run["id"])),
                "OpenRouter profile gate passed",
            )
        except (
            GateError,
            ProfileGitHubError,
            ProfileRequestError,
            ProfileCompletionError,
        ) as error:
            # The failure status is deliberately attached only to the currently
            # live head; publishing never falls back to an older success.
            try:
                bare.create_status(
                    head_for_failure,
                    "failure",
                    _run_url(bare, int(run["id"])),
                    "OpenRouter profile confirmation is missing or stale",
                )
            except ProfileGitHubError:
                pass
            failures.append(f"PR #{number}: {error}")
    if failures:
        raise GateError(
            "profile publication failed for one or more pull requests: "
            + "; ".join(failures)
        )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in {
        "begin",
        "accept",
        "plan",
        "confirm",
        "publish",
    }:
        print(
            "usage: profile_gate.py begin|accept|plan|confirm|publish", file=sys.stderr
        )
        return 2
    try:
        {
            "begin": begin,
            "accept": accept,
            "plan": plan,
            "confirm": confirm,
            "publish": publish,
        }[args[0]]()
        return 0
    except (
        GateError,
        ProfileGitHubError,
        ProfileRequestError,
        ProfileCompletionError,
        SchemaError,
        ValueError,
    ) as error:
        message = str(error).replace("\r", " ").replace("\n", " ")[:500]
        print(f"profile gate: {message or 'validation failed'}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
