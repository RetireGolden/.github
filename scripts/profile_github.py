"""Small, fail-closed GitHub REST reader for profile-gate provenance.

This module deliberately contains data acquisition and structural validation only.
It neither evaluates profile evidence nor dispatches workflows, comments on pull
requests, or executes repository content.
"""

from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional


MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_ZIP_BYTES = 8 * 1024 * 1024
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
_SHA = re.compile(r"^[0-9a-f]{40}$")
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class ProfileGitHubError(RuntimeError):
    """GitHub data was unavailable, malformed, or did not meet a trust bound."""


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def _read_bounded(handle: Any, cap: int) -> bytes:
    data = handle.read(cap + 1)
    if not isinstance(data, bytes) or len(data) > cap:
        raise ProfileGitHubError("response exceeds its size limit")
    return data


def urllib_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    timeout: float,
    cap: int,
    data: Optional[bytes] = None,
) -> HTTPResponse:
    """Default transport.  It intentionally turns every redirect into a response."""
    request = urllib.request.Request(
        url, data=data, headers=dict(headers), method=method
    )
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            return HTTPResponse(
                response.status,
                dict(response.headers.items()),
                _read_bounded(response, cap),
            )
    except urllib.error.HTTPError as error:
        return HTTPResponse(
            error.code, dict(error.headers.items()), _read_bounded(error, cap)
        )
    except (urllib.error.URLError, OSError) as error:
        raise ProfileGitHubError("GitHub transport failed") from error


def _reject_surrogates(text: str) -> None:
    index = 0
    while index < len(text):
        code = ord(text[index])
        if 0xD800 <= code <= 0xDBFF:
            if index + 1 >= len(text) or not (0xDC00 <= ord(text[index + 1]) <= 0xDFFF):
                raise ProfileGitHubError("JSON contains an unpaired surrogate")
            index += 2
            continue
        if 0xDC00 <= code <= 0xDFFF:
            raise ProfileGitHubError("JSON contains an unpaired surrogate")
        index += 1


def _validate_json_unicode(item: Any) -> None:
    if isinstance(item, str):
        _reject_surrogates(item)
    elif isinstance(item, dict):
        for key, value in item.items():
            _validate_json_unicode(key)
            _validate_json_unicode(value)
    elif isinstance(item, list):
        for nested in item:
            _validate_json_unicode(nested)


def _validate_api_endpoint(endpoint: str) -> None:
    if (
        not isinstance(endpoint, str)
        or not endpoint.startswith("/")
        or endpoint.startswith("//")
    ):
        raise ProfileGitHubError("invalid GitHub API endpoint")
    if "#" in endpoint or "\r" in endpoint or "\n" in endpoint:
        raise ProfileGitHubError("invalid GitHub API endpoint")
    if any(ord(character) < 32 for character in endpoint):
        raise ProfileGitHubError("invalid GitHub API endpoint")


def _repository_full_name(value: Any) -> Optional[str]:
    if not isinstance(value, Mapping):
        return None
    full_name = value.get("full_name")
    return full_name if isinstance(full_name, str) else None


def _canonical_sha(value: Any) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ProfileGitHubError("SHA is not a canonical 40-character hex value")
    return value.lower()


def strict_json(
    raw: bytes, *, max_bytes: int = MAX_JSON_BYTES, max_depth: int = 24
) -> Any:
    """Decode bounded UTF-8 JSON, refusing duplicate keys and non-finite values."""
    if (
        not isinstance(raw, bytes)
        or isinstance(max_bytes, bool)
        or len(raw) > max_bytes
    ):
        raise ProfileGitHubError("JSON exceeds its size limit")
    if not isinstance(max_depth, int) or isinstance(max_depth, bool) or max_depth < 0:
        raise ValueError("max_depth must be a non-negative integer")
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError as error:
        raise ProfileGitHubError("JSON is not valid UTF-8") from error
    _reject_surrogates(text)

    def pairs(entries: list[tuple[str, Any]]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key, value in entries:
            if key in output:
                raise ProfileGitHubError("JSON has a duplicate object key")
            output[key] = value
        return output

    def reject_constant(value: str) -> Any:
        raise ProfileGitHubError("JSON has a non-finite number")

    try:
        value = json.loads(
            text, object_pairs_hook=pairs, parse_constant=reject_constant
        )
    except ProfileGitHubError:
        raise
    except RecursionError as error:
        raise ProfileGitHubError("JSON nesting exceeds its limit") from error
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ProfileGitHubError("invalid JSON") from error

    def depth(item: Any, level: int) -> None:
        if level > max_depth:
            raise ProfileGitHubError("JSON nesting exceeds its limit")
        if isinstance(item, dict):
            for nested in item.values():
                depth(nested, level + 1)
        elif isinstance(item, list):
            for nested in item:
                depth(nested, level + 1)

    depth(value, 0)
    _validate_json_unicode(value)
    return value


@dataclass(frozen=True)
class TrustedWorkflow:
    owner: str
    name: str
    default_branch: str
    caller_path: str = ".github/workflows/openrouter-code-review.yml"
    reusable_path: str = (
        "RetireGolden/.github/.github/workflows/openrouter-code-review.yml"
    )
    reusable_sha: str = "a6a690b82fa76bbda4334b87dd179551534d183b"
    bot_login: str = "github-actions[bot]"
    bot_id: int = 41898282
    bot_type: str = "Bot"

    def __post_init__(self) -> None:
        if not _LOGIN.fullmatch(self.owner) or not _REPOSITORY.fullmatch(self.name):
            raise ValueError("owner and repository name must be GitHub path components")
        if not self.default_branch or not _SHA.fullmatch(self.reusable_sha):
            raise ValueError("invalid immutable workflow configuration")

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True)
class VerifiedRun:
    owner: str
    repo: str
    run_id: int
    attempt: int
    head_sha: str
    event: str
    head_branch: str
    workflow_id: int
    run: Mapping[str, Any]
    jobs: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)


Transport = Callable[
    [str, str, Mapping[str, str], float, int, Optional[bytes]], HTTPResponse
]


class ProfileGitHub:
    """Bounded GitHub reader.  ``transport`` is injectable for offline callers/tests."""

    def __init__(
        self,
        config: TrustedWorkflow,
        token: Optional[str] = None,
        transport: Optional[Transport] = None,
        timeout: float = 60,
    ) -> None:
        self.config = config
        self.token = token
        self.transport = transport or urllib_transport
        self.timeout = timeout

    def _headers(
        self, *, authorization: bool, json_body: bool = False
    ) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if authorization and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _send(
        self,
        method: str,
        url: str,
        *,
        authorization: bool = True,
        cap: int = MAX_JSON_BYTES,
        data: Optional[bytes] = None,
    ) -> HTTPResponse:
        try:
            response = self.transport(
                method,
                url,
                self._headers(authorization=authorization, json_body=data is not None),
                self.timeout,
                cap,
                data,
            )
        except ProfileGitHubError:
            raise
        except Exception as error:
            raise ProfileGitHubError("GitHub transport failed") from error
        if (
            not isinstance(response, HTTPResponse)
            or not isinstance(response.status, int)
            or not isinstance(response.body, bytes)
        ):
            raise ProfileGitHubError("transport returned an invalid response")
        if len(response.body) > cap:
            raise ProfileGitHubError("response exceeds its size limit")
        return response

    def _api(
        self,
        endpoint: str,
        *,
        method: str = "GET",
        payload: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        _validate_api_endpoint(endpoint)
        encoded = (
            None
            if payload is None
            else json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode(
                "ascii"
            )
        )
        response = self._send(method, "https://api.github.com" + endpoint, data=encoded)
        if response.status < 200 or response.status >= 300:
            raise ProfileGitHubError(f"GitHub API returned HTTP {response.status}")
        return strict_json(response.body)

    def paginated(
        self, endpoint: str, list_key: Optional[str] = None, max_items: int = 1000
    ) -> list[Any]:
        if (
            not isinstance(max_items, int)
            or isinstance(max_items, bool)
            or max_items <= 0
        ):
            raise ValueError("max_items must be a positive integer")
        _validate_api_endpoint(endpoint)
        parsed = urllib.parse.urlsplit(endpoint)
        if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
            raise ProfileGitHubError("pagination endpoint must be an API path")
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if any(key in {"page", "per_page"} for key, _ in query):
            raise ProfileGitHubError("pagination controls are owned by this reader")
        result: list[Any] = []
        page = 1
        while True:
            rendered = urllib.parse.urlencode(
                [*query, ("per_page", "100"), ("page", str(page))]
            )
            payload = self._api(
                urllib.parse.urlunsplit(("", "", parsed.path, rendered, ""))
            )
            if list_key is None:
                values = payload
            else:
                if not isinstance(payload, dict) or list_key not in payload:
                    raise ProfileGitHubError(
                        "pagination response lacks its required list field"
                    )
                values = payload[list_key]
            if not isinstance(values, list):
                raise ProfileGitHubError("pagination response list has the wrong type")
            if len(result) + len(values) > max_items:
                raise ProfileGitHubError("pagination exceeds its item cap")
            result.extend(values)
            if len(values) < 100:
                return result
            # At the exact cap we still fetch one empty continuation page.  A nonempty
            # response is an overflow; a failed continuation cannot be treated as an end.
            page += 1

    def _content_blob(self, ref: str) -> str:
        endpoint = "/repos/{}/{}/contents/{}?{}".format(
            self.config.owner,
            self.config.name,
            urllib.parse.quote(self.config.caller_path, safe="/"),
            urllib.parse.urlencode({"ref": ref}),
        )
        value = self._api(endpoint)
        if not isinstance(value, dict) or value.get("type") != "file":
            raise ProfileGitHubError("caller content response is invalid")
        return _canonical_sha(value.get("sha"))

    def verify_run(
        self,
        run: Mapping[str, Any],
        current_default_caller_blob: str,
        require_success: bool = False,
    ) -> VerifiedRun:
        """Verify live workflow identity and the exact-head caller blob.

        ``current_default_caller_blob`` is the already-trusted *caller file blob SHA*,
        not a pull-request head SHA.  Callers obtain/refresh that value from the default
        branch outside this immutable-config reader.
        """
        if not isinstance(run, Mapping):
            raise ProfileGitHubError("invalid run or trusted caller blob SHA")
        try:
            trusted_blob = _canonical_sha(current_default_caller_blob)
        except ProfileGitHubError as error:
            raise ProfileGitHubError(
                "invalid run or trusted caller blob SHA"
            ) from error
        run_id, attempt, workflow_id = (
            run.get("id"),
            run.get("run_attempt"),
            run.get("workflow_id"),
        )
        head_sha = run.get("head_sha")
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
            for value in (run_id, attempt, workflow_id)
        ):
            raise ProfileGitHubError(
                "run id, attempt, and workflow id must be positive integers"
            )
        if attempt > 1000:
            raise ProfileGitHubError("run attempt exceeds supported limit")
        try:
            head_sha = _canonical_sha(head_sha)
        except ProfileGitHubError as error:
            raise ProfileGitHubError("run lacks a full commit SHA") from error
        if (
            _repository_full_name(run.get("repository")) != self.config.full_name
            or _repository_full_name(run.get("head_repository"))
            != self.config.full_name
        ):
            raise ProfileGitHubError("workflow run is not from the trusted repository")
        path = run.get("path")
        event = run.get("event")
        if not isinstance(path, str) or path != self.config.caller_path:
            raise ProfileGitHubError("workflow run has an untrusted caller or event")
        if not isinstance(event, str) or event not in {
            "pull_request",
            "workflow_dispatch",
        }:
            raise ProfileGitHubError("workflow run has an untrusted caller or event")
        head_branch = run.get("head_branch")
        if event == "workflow_dispatch":
            if (
                not isinstance(head_branch, str)
                or head_branch != self.config.default_branch
            ):
                raise ProfileGitHubError("dispatch run is not on the default branch")
        elif head_branch is not None and not isinstance(head_branch, str):
            raise ProfileGitHubError("workflow run has an invalid head branch")
        status = run.get("status")
        conclusion = run.get("conclusion")
        if require_success:
            if status != "completed" or conclusion != "success":
                raise ProfileGitHubError("workflow run is not successful")

        workflow_file = self.config.caller_path.rsplit("/", 1)[-1]
        workflow = self._api(
            f"/repos/{self.config.owner}/{self.config.name}/actions/workflows/{urllib.parse.quote(workflow_file, safe='')}"
        )
        if (
            not isinstance(workflow, dict)
            or workflow.get("id") != workflow_id
            or workflow.get("path") != self.config.caller_path
            or workflow.get("state") != "active"
        ):
            raise ProfileGitHubError("live workflow is not the trusted active caller")
        referenced = run.get("referenced_workflows")
        expected_path = f"{self.config.reusable_path}@{self.config.reusable_sha}"
        if not isinstance(referenced, list):
            raise ProfileGitHubError(
                "run lacks the exact trusted reusable workflow pin"
            )
        pinned = False
        for item in referenced:
            if not isinstance(item, Mapping):
                continue
            item_path = item.get("path")
            item_sha = item.get("sha")
            if (
                item_path == expected_path
                and isinstance(item_sha, str)
                and _SHA.fullmatch(item_sha)
                and item_sha == self.config.reusable_sha
            ):
                pinned = True
                break
        if not pinned:
            raise ProfileGitHubError(
                "run lacks the exact trusted reusable workflow pin"
            )
        if self._content_blob(head_sha) != trusted_blob:
            raise ProfileGitHubError(
                "exact-head caller blob differs from trusted default caller"
            )
        jobs = self.paginated(
            f"/repos/{self.config.owner}/{self.config.name}/actions/runs/{run_id}/attempts/{attempt}/jobs",
            "jobs",
        )
        if not all(isinstance(job, Mapping) for job in jobs):
            raise ProfileGitHubError("workflow jobs response has non-object entries")
        return VerifiedRun(
            self.config.owner,
            self.config.name,
            run_id,
            attempt,
            head_sha,
            event,
            head_branch if isinstance(head_branch, str) else "",
            workflow_id,
            dict(run),
            tuple(jobs),
        )

    @staticmethod
    def _safe_artifact_url(location: str) -> str:
        parsed = urllib.parse.urlsplit(location)
        host = parsed.hostname
        if (
            parsed.scheme != "https"
            or not host
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ProfileGitHubError("artifact redirect is not a safe HTTPS URL")
        normalized_host = host.rstrip(".").lower()
        if normalized_host == "localhost" or normalized_host.endswith(".localhost"):
            raise ProfileGitHubError("artifact redirect targets localhost")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            # Do not let non-canonical numeric IPv4 spellings reach DNS/urllib.
            if all(character.isdigit() or character == "." for character in host):
                raise ProfileGitHubError("artifact redirect has an invalid IP literal")
            return location
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_unspecified
        ):
            raise ProfileGitHubError("artifact redirect targets a non-public address")
        return location

    def _artifact_zip(self, artifact_id: int) -> bytes:
        source = f"https://api.github.com/repos/{self.config.owner}/{self.config.name}/actions/artifacts/{artifact_id}/zip"
        first = self._send("GET", source, cap=MAX_ZIP_BYTES)
        if first.status not in {301, 302, 303, 307, 308}:
            raise ProfileGitHubError("artifact endpoint did not return a redirect")
        location = next(
            (
                value
                for key, value in first.headers.items()
                if key.lower() == "location"
            ),
            None,
        )
        if not isinstance(location, str):
            raise ProfileGitHubError("artifact redirect lacks a location")
        destination = self._safe_artifact_url(location)
        second = self._send("GET", destination, authorization=False, cap=MAX_ZIP_BYTES)
        if second.status < 200 or second.status >= 300:
            raise ProfileGitHubError("artifact download failed")
        return second.body

    def artifact(
        self,
        verified_run: VerifiedRun,
        name: str,
        max_bytes: int,
        expected_filename: Optional[str] = None,
    ) -> bytes:
        if not isinstance(verified_run, VerifiedRun):
            raise TypeError("artifact retrieval requires a VerifiedRun")
        if (
            verified_run.owner != self.config.owner
            or verified_run.repo != self.config.name
        ):
            raise ProfileGitHubError("VerifiedRun belongs to another repository")
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(max_bytes, int)
            or isinstance(max_bytes, bool)
            or not 0 < max_bytes <= MAX_ARTIFACT_BYTES
        ):
            raise ValueError("invalid artifact size bound")
        artifacts = self.paginated(
            f"/repos/{self.config.owner}/{self.config.name}/actions/runs/{verified_run.run_id}/artifacts",
            "artifacts",
        )
        matches = [
            item
            for item in artifacts
            if isinstance(item, Mapping)
            and item.get("name") == name
            and item.get("expired") is False
        ]
        if len(matches) != 1:
            raise ProfileGitHubError("expected exactly one unexpired named artifact")
        metadata = matches[0]
        workflow_run = metadata.get("workflow_run")
        if workflow_run is not None:
            if (
                not isinstance(workflow_run, Mapping)
                or ("id" in workflow_run and workflow_run["id"] != verified_run.run_id)
                or (
                    "head_sha" in workflow_run
                    and workflow_run["head_sha"] != verified_run.head_sha
                )
            ):
                raise ProfileGitHubError(
                    "artifact metadata does not match the verified run"
                )
        artifact_id = metadata.get("id")
        if (
            not isinstance(artifact_id, int)
            or isinstance(artifact_id, bool)
            or artifact_id <= 0
        ):
            raise ProfileGitHubError("artifact has an invalid id")
        compressed = self._artifact_zip(artifact_id)
        digest = metadata.get("digest")
        if digest is not None:
            if not isinstance(digest, str) or not re.fullmatch(
                r"sha256:[0-9a-f]{64}", digest
            ):
                raise ProfileGitHubError("artifact digest is malformed")
            if hashlib.sha256(compressed).hexdigest() != digest[7:]:
                raise ProfileGitHubError("artifact ZIP digest does not match")
        try:
            with zipfile.ZipFile(io.BytesIO(compressed)) as archive:
                entries = archive.infolist()
                if len(entries) != 1:
                    raise ProfileGitHubError(
                        "artifact ZIP must contain exactly one file"
                    )
                entry = entries[0]
                filename = entry.filename
                path = pathlib_path(filename)
                if (
                    entry.is_dir()
                    or not filename
                    or "\x00" in filename
                    or path is None
                    or (entry.external_attr >> 16) & 0o170000 == 0o120000
                    or entry.flag_bits & 1
                ):
                    raise ProfileGitHubError("artifact ZIP has an unsafe entry")
                if expected_filename is not None and filename != expected_filename:
                    raise ProfileGitHubError("artifact filename does not match")
                if entry.file_size > max_bytes:
                    raise ProfileGitHubError(
                        "artifact is larger than its uncompressed limit"
                    )
                with archive.open(entry, "r") as source:
                    content = source.read(max_bytes + 1)
                if len(content) > max_bytes or len(content) != entry.file_size:
                    raise ProfileGitHubError(
                        "artifact is truncated or exceeds its uncompressed limit"
                    )
                return content
        except ProfileGitHubError:
            raise
        except (OSError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
            raise ProfileGitHubError("artifact is not a valid safe ZIP") from error

    def successful_steps(
        self, verified_run: VerifiedRun, job_name: str, step_names: list[str]
    ) -> bool:
        if not isinstance(verified_run, VerifiedRun):
            raise TypeError("step validation requires a VerifiedRun")
        jobs = [job for job in verified_run.jobs if job.get("name") == job_name]
        if len(jobs) != 1 or jobs[0].get("conclusion") == "skipped":
            return False
        steps = jobs[0].get("steps")
        if not isinstance(steps, list) or len(set(step_names)) != len(step_names):
            return False
        for name in step_names:
            found = [
                step
                for step in steps
                if isinstance(step, Mapping) and step.get("name") == name
            ]
            if (
                len(found) != 1
                or found[0].get("status") != "completed"
                or found[0].get("conclusion") != "success"
            ):
                return False
        return True

    def trusted_review(
        self,
        review: Mapping[str, Any],
        repo: Mapping[str, Any],
        prhead: Optional[str] = None,
    ) -> bool:
        if (
            not isinstance(review, Mapping)
            or not isinstance(repo, Mapping)
            or repo.get("full_name") != self.config.full_name
        ):
            return False
        user = review.get("user")
        commit = review.get("commit_id")
        body = review.get("body")
        if (
            not isinstance(user, Mapping)
            or user.get("login") != self.config.bot_login
            or user.get("id") != self.config.bot_id
            or user.get("type") != self.config.bot_type
        ):
            return False
        if (
            not isinstance(commit, str)
            or not _SHA.fullmatch(commit)
            or (prhead is not None and commit != prhead)
        ):
            return False
        return isinstance(body, str) and len(body) <= 60000

    def maintainer(self, login: str) -> bool:
        if not isinstance(login, str) or not _LOGIN.fullmatch(login):
            return False
        endpoint = f"/repos/{self.config.owner}/{self.config.name}/collaborators/{urllib.parse.quote(login, safe='')}/permission"
        _validate_api_endpoint(endpoint)
        response = self._send("GET", "https://api.github.com" + endpoint)
        if response.status == 404:
            return False
        if response.status < 200 or response.status >= 300:
            raise ProfileGitHubError(f"GitHub API returned HTTP {response.status}")
        value = strict_json(response.body)
        return isinstance(value, Mapping) and value.get("permission") in {
            "write",
            "maintain",
            "admin",
        }

    def get_pr(self, number: int) -> Mapping[str, Any]:
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            raise ValueError("pull request number must be positive")
        value = self._api(
            f"/repos/{self.config.owner}/{self.config.name}/pulls/{number}"
        )
        if not isinstance(value, Mapping):
            raise ProfileGitHubError("pull request response is not an object")
        return value

    def get_repo(self) -> Mapping[str, Any]:
        value = self._api(f"/repos/{self.config.owner}/{self.config.name}")
        if not isinstance(value, Mapping):
            raise ProfileGitHubError("repository response is not an object")
        return value

    def create_status(
        self, full_sha: str, state: str, target_url: str, description: str
    ) -> Mapping[str, Any]:
        if (
            not isinstance(full_sha, str)
            or not _SHA.fullmatch(full_sha)
            or state not in {"pending", "success", "failure", "error"}
        ):
            raise ValueError("invalid status parameters")
        expected = re.compile(
            r"^https://github\.com/"
            + re.escape(self.config.full_name)
            + r"/actions/runs/[1-9][0-9]*$"
        )
        if (
            not isinstance(target_url, str)
            or not expected.fullmatch(target_url)
            or not isinstance(description, str)
            or not 0 < len(description) <= 140
        ):
            raise ValueError("status target or description is invalid")
        value = self._api(
            f"/repos/{self.config.owner}/{self.config.name}/statuses/{full_sha}",
            method="POST",
            payload={
                "state": state,
                "target_url": target_url,
                "description": description,
                "context": "openrouter-profile",
            },
        )
        if not isinstance(value, Mapping):
            raise ProfileGitHubError("status response is not an object")
        return value


def pathlib_path(filename: str) -> Optional[tuple[str, ...]]:
    """Return safe POSIX archive components, or None for traversal/absolute paths."""
    if (
        not isinstance(filename, str)
        or "\\" in filename
        or filename.startswith("/")
        or re.match(r"^[A-Za-z]:", filename)
    ):
        return None
    parts = filename.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return None
    return tuple(parts)
