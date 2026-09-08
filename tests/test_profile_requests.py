import hashlib
import io
import json
import re
import unittest
import zipfile
from collections import defaultdict

from or_pr_review.collect import CollectedReview, DiffPlan, Truncation
from or_pr_review.loop import LoopState
from or_pr_review.profile_evidence import VerifiedRunReceipt, parse_receipt
from or_pr_review.review_context import (
    PreparedExecution,
    freeze_context,
    freeze_runtime,
)
from or_pr_review.review_plan import ReviewLane, ReviewPlan
from or_pr_review.review_policy import PolicyFile, ResolvedPolicy

from scripts.profile_github import (
    HTTPResponse,
    ProfileGitHub,
    TrustedWorkflow,
    VerifiedRun,
)
from scripts.profile_requests import (
    ACCEPT_STEP,
    AlreadyAccepted,
    PRESERVE_STEP,
    ProfileRequestError,
    RequestHistory,
    RequestRecord,
    attempt_artifact_name,
    canonical_request,
    parse_request,
    primary_artifact_name,
)

SHA = "a" * 40
PIN = "b" * 40
PIN_OLD = "0" * 40
DIGEST = "c" * 64
REGISTRY = "d" * 64
CONTEXT = "e" * 64
BASE = "f" * 40
REPO = "RetireGolden/example"
PR = 9
JOB = "profile-gate"
REUSABLE_PATH = "RetireGolden/.github/.github/workflows/openrouter-code-review.yml"
RUN_URL = f"https://github.com/{REPO}/actions/runs/{{run_id}}"
ACCEPT_MS = 1_700_000_010_000
CARRY_MS = 1_700_000_011_000
CANCEL_MS = 1_700_000_012_000


def response(value, status=200, headers=None):
    body = value if isinstance(value, bytes) else json.dumps(value).encode()
    return HTTPResponse(status, headers or {}, body)


def zipped_request(record: RequestRecord) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("request.json", canonical_request(record))
    return stream.getvalue()


def record_payload(**changes):
    value = {
        "version": 1,
        "repository": REPO,
        "pr_number": PR,
        "run_id": 100,
        "run_attempt": 1,
        "accepted_at_ms": 1_700_000_010_000,
        "kind": "deep",
        "head_sha": SHA,
        "policy_digest": DIGEST,
        "registry_digest": REGISTRY,
        "profile": "code",
        "origin_run_id": None,
        "context_sha256": CONTEXT,
    }
    value.update(changes)
    return RequestRecord(**value)


def accept_jobs(accept_ms="2023-11-14T22:13:40Z", conclusion="success"):
    return [
        {
            "name": JOB,
            "conclusion": conclusion,
            "steps": [
                {
                    "name": ACCEPT_STEP,
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": accept_ms,
                },
                {
                    "name": PRESERVE_STEP,
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": accept_ms,
                },
            ],
        }
    ]


def valid_run(run_id=100, attempt=1, head_sha=SHA):
    return {
        "id": run_id,
        "run_attempt": attempt,
        "workflow_id": 9,
        "head_sha": head_sha,
        "head_branch": "topic",
        "event": "pull_request",
        "path": ".github/workflows/openrouter-code-review.yml",
        "repository": {"full_name": REPO},
        "head_repository": {"full_name": REPO},
        "referenced_workflows": [{"path": f"{REUSABLE_PATH}@{PIN}", "sha": PIN}],
        "status": "completed",
        "conclusion": "cancelled",
        "run_started_at": "2023-11-14T22:13:30Z",
    }


class FakeTransport:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def __call__(self, method, url, headers, timeout, cap, data):
        self.calls.append((method, url, dict(headers), cap, data))
        return self.handler(method, url, headers, cap, data)


class RequestHistoryTests(unittest.TestCase):
    def setUp(self):
        self.config = TrustedWorkflow(
            "RetireGolden", "example", "main", reusable_sha=PIN
        )
        self.history = None
        self.transport = None

    def client(self, handler):
        self.transport = FakeTransport(handler)
        return ProfileGitHub(self.config, "secret", self.transport)

    def reader(self, handler, allowed=(JOB,), verify_historical_run=None):
        client = self.client(handler)
        self.history = RequestHistory(
            client, SHA, allowed, verify_historical_run=verify_historical_run
        )
        return self.history

    def provenance(self, jobs=None):
        def handler(method, url, headers, cap, data):
            if "/actions/workflows/" in url:
                return response(
                    {"id": 9, "path": self.config.caller_path, "state": "active"}
                )
            if "/contents/" in url:
                return response({"type": "file", "sha": SHA})
            if "/attempts/" in url and url.endswith("/jobs"):
                return response({"jobs": jobs or accept_jobs()})
            raise AssertionError(url)

        return handler

    def artifact_meta(
        self,
        artifact_id,
        run_id,
        name,
        created_at="2023-11-14T22:13:41Z",
        zip_bytes=b"",
        head_sha=SHA,
    ):
        digest = (
            "sha256:" + hashlib.sha256(zip_bytes).hexdigest() if zip_bytes else None
        )
        payload = {
            "id": artifact_id,
            "name": name,
            "expired": False,
            "created_at": created_at,
            "workflow_run": {"id": run_id, "head_sha": head_sha},
        }
        if digest is not None:
            payload["digest"] = digest
        return payload

    def install_run(
        self,
        store,
        run_id,
        attempt,
        record,
        *,
        jobs=None,
        head_sha=SHA,
        pin=PIN,
        artifact_name=None,
        index_primary=True,
        created_at="2023-11-14T22:13:41Z",
    ):
        run = valid_run(run_id, attempt, head_sha)
        run["referenced_workflows"] = [{"path": f"{REUSABLE_PATH}@{pin}", "sha": pin}]
        store["runs"][(run_id, attempt)] = run
        name = artifact_name or attempt_artifact_name(PR, attempt)
        zip_bytes = zipped_request(record)
        artifact_id = run_id * 10 + attempt
        meta = self.artifact_meta(
            artifact_id,
            run_id,
            name,
            created_at=created_at,
            zip_bytes=zip_bytes,
            head_sha=head_sha,
        )
        store["artifact_zips"][artifact_id] = zip_bytes
        store["artifacts"][run_id].append(meta)
        store["jobs"][(run_id, attempt)] = jobs or accept_jobs()
        if index_primary and name == primary_artifact_name(PR):
            store["index"].append(meta)
        store["latest_attempt"][run_id] = max(
            store["latest_attempt"].get(run_id, 0), attempt
        )

    def make_store(self):
        return {
            "runs": {},
            "artifacts": defaultdict(list),
            "artifact_zips": {},
            "jobs": {},
            "index": [],
            "last_artifact": None,
            "latest_attempt": {},
        }

    def store_handler(self, store):
        base = self.provenance()

        def handler(method, url, headers, cap, data):
            if "/actions/artifacts?name=" in url:
                return response({"artifacts": store["index"]})
            url = url.split("?", 1)[0]
            if "/actions/runs/" in url and url.endswith("/artifacts"):
                run_id = int(url.split("/actions/runs/")[1].split("/artifacts")[0])
                return response({"artifacts": store["artifacts"].get(run_id, [])})
            latest_match = re.match(r".*/actions/runs/(\d+)$", url)
            if latest_match:
                run_id = int(latest_match.group(1))
                attempt = store["latest_attempt"].get(run_id, 1)
                latest = dict(store["runs"][(run_id, attempt)])
                latest["run_attempt"] = attempt
                return response(latest)
            if (
                "/actions/runs/" in url
                and "/attempts/" in url
                and not url.endswith("/jobs")
            ):
                run_id = int(url.split("/actions/runs/")[1].split("/attempts/")[0])
                attempt = int(url.split("/attempts/")[1].split("?")[0].rstrip("/"))
                return response(store["runs"][(run_id, attempt)])
            if "/attempts/" in url and url.endswith("/jobs"):
                run_id = int(url.split("/actions/runs/")[1].split("/attempts/")[0])
                attempt = int(url.split("/attempts/")[1].split("/jobs")[0])
                return response({"jobs": store["jobs"][(run_id, attempt)]})
            if "/actions/artifacts/" in url and url.endswith("/zip"):
                artifact_id = int(url.split("/actions/artifacts/")[1].split("/zip")[0])
                store["last_artifact"] = artifact_id
                return response(
                    b"", 302, {"Location": "https://storage.example/presigned"}
                )
            if url.startswith("https://storage.example/"):
                return response(store["artifact_zips"][store["last_artifact"]])
            return base(method, url, headers, cap, data)

        return handler

    def context_envelope(self, *, head_sha=SHA, pr_number=PR):
        policy = ResolvedPolicy(
            BASE,
            "code",
            "deep",
            (PolicyFile("REVIEW.md", BASE, "guidance", ("a.py",)),),
            (),
            ("a.py",),
            DIGEST,
        )
        diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"
        collected = CollectedReview(
            pr_number,
            "title",
            "",
            head_sha,
            "main",
            "topic",
            DiffPlan("full-pr", "full-pr", BASE, head_sha, None),
            Truncation(diff, False, len(diff), len(diff), 300),
            "initial",
            ("a.py",),
            BASE,
            policy,
        )
        plan = ReviewPlan(
            "code",
            "deep",
            "manual",
            (ReviewLane("openai/gpt-5", True, "OpenAI", "priority"),),
            "openai/gpt-5.6-luna",
            "low",
            50,
            600,
            1320,
            REGISTRY,
            False,
        )
        execution = PreparedExecution(
            plan,
            freeze_runtime({"ROAST_LEVEL": "professional"}),
            "agent reply",
            1_700_000_000_000,
            1_700_001_320_000,
            RUN_URL.format(run_id=100),
            1,
        )
        return freeze_context(
            REPO, collected, LoopState("initial", 1), 50, execution=execution
        )

    def test_parse_and_canonical_round_trip(self):
        record = record_payload()
        raw = canonical_request(record)
        self.assertEqual(parse_request(raw), record)
        cancel = record_payload(kind="cancel", context_sha256="", origin_run_id=100)
        self.assertEqual(parse_request(canonical_request(cancel)), cancel)
        with self.assertRaises(ProfileRequestError):
            parse_request(json.dumps({"version": 1}).encode())

    def test_make_record_deep_carry_and_cancel(self):
        history = self.reader(lambda *args: response({}))
        envelope = self.context_envelope()
        deep = history.make_record(envelope, "deep", None, 100, 1, 10, REPO, PR)
        self.assertIsNone(deep.origin_run_id)
        self.assertEqual(deep.context_sha256, envelope["sha256"])
        pending = history.fold_history((deep,), ())
        carry_head = "1" * 40
        carry_env = self.context_envelope(head_sha=carry_head)
        carry = history.make_record(carry_env, "carry", pending, 101, 1, 20, REPO, PR)
        self.assertEqual(carry.origin_run_id, 100)
        cancel = history.make_record(envelope, "cancel", pending, 102, 1, 30, REPO, PR)
        self.assertEqual(cancel.context_sha256, "")
        self.assertEqual(cancel.origin_run_id, 100)

    def test_dispatch_request_binds_pr_head_independently_of_source_main(self):
        store = self.make_store()
        record = record_payload()
        self.install_run(
            store,
            100,
            1,
            record,
            head_sha=BASE,
            artifact_name=primary_artifact_name(PR),
        )
        store["runs"][(100, 1)].update(event="workflow_dispatch", head_branch="main")
        self.assertEqual(self.reader(self.store_handler(store)).load(PR), (record,))

    def test_load_first_acceptance_and_carry(self):
        store = self.make_store()
        deep = record_payload(run_id=100, accepted_at_ms=ACCEPT_MS)
        carry = record_payload(
            run_id=200,
            accepted_at_ms=CARRY_MS,
            kind="carry",
            head_sha="1" * 40,
            origin_run_id=100,
        )
        self.install_run(store, 100, 1, deep)
        self.install_run(store, 200, 1, carry, head_sha="1" * 40)
        records = self.reader(self.store_handler(store)).load(PR)
        self.assertEqual([item.kind for item in records], ["deep", "carry"])
        self.assertEqual(records[1].head_sha, "1" * 40)

    def test_load_cancel_and_canceled_job_still_counts(self):
        store = self.make_store()
        deep = record_payload(run_id=100, accepted_at_ms=ACCEPT_MS)
        cancel = record_payload(
            run_id=200,
            accepted_at_ms=CANCEL_MS,
            kind="cancel",
            context_sha256="",
            origin_run_id=100,
        )
        self.install_run(store, 100, 1, deep)
        self.install_run(
            store,
            200,
            1,
            cancel,
            jobs=accept_jobs(conclusion="cancelled"),
        )
        records = self.reader(self.store_handler(store)).load(PR)
        self.assertEqual([item.kind for item in records], ["deep", "cancel"])
        self.assertIsNone(self.history.fold_history(records, ()))

    def test_load_rejects_wrong_workflow_step_attempt_and_timing(self):
        deep = record_payload(run_id=100)

        store = self.make_store()
        self.install_run(store, 100, 1, deep)
        store["jobs"][(100, 1)] = [
            {
                "name": JOB,
                "conclusion": "success",
                "steps": [
                    {
                        "name": ACCEPT_STEP,
                        "status": "completed",
                        "conclusion": "failure",
                        "completed_at": "2023-11-14T22:13:40Z",
                    }
                ],
            }
        ]
        with self.assertRaises(ProfileRequestError):
            self.reader(self.store_handler(store)).load(PR)

        store = self.make_store()
        self.install_run(store, 100, 1, deep, index_primary=False)
        self.install_run(
            store,
            100,
            2,
            deep,
            artifact_name=attempt_artifact_name(PR, 2),
        )
        store["index"].append(self.artifact_meta(1000, 100, primary_artifact_name(PR)))
        with self.assertRaises(ProfileRequestError):
            self.reader(self.store_handler(store)).load(PR)

        store = self.make_store()
        late = record_payload(run_id=100, accepted_at_ms=1_900_000_000_000)
        self.install_run(store, 100, 1, late)
        with self.assertRaises(ProfileRequestError):
            self.reader(self.store_handler(store)).load(PR)

    def test_load_rejects_untrusted_workflow_caller(self):
        store = self.make_store()
        self.install_run(store, 100, 1, record_payload(run_id=100))
        history = RequestHistory(
            self.client(self.store_handler(store)), "d" * 40, (JOB,)
        )
        with self.assertRaises(ProfileRequestError):
            history.load(PR)

    def test_load_rejects_duplicate_index_expiry_and_pagination_overflow(self):
        store = self.make_store()
        deep = record_payload(run_id=100)
        self.install_run(store, 100, 1, deep)
        store["index"].append(store["index"][0])
        with self.assertRaises(ProfileRequestError):
            self.reader(self.store_handler(store)).load(PR)

        store["index"][0] = {**store["index"][0], "expired": True}
        with self.assertRaises(ProfileRequestError):
            self.reader(self.store_handler(store)).load(PR)

        page = {"count": 0}

        def overflow(method, url, headers, cap, data):
            if "/actions/artifacts?name=" in url:
                page["count"] += 1
                return response(
                    {
                        "artifacts": [
                            {
                                "id": offset,
                                "name": primary_artifact_name(PR),
                                "expired": False,
                                "workflow_run": {"id": offset},
                            }
                            for offset in range(
                                page["count"] * 100, page["count"] * 100 + 100
                            )
                        ]
                    }
                )
            raise AssertionError(url)

        with self.assertRaises(ProfileRequestError):
            self.reader(overflow).load(PR)

    def test_rerun_uses_additional_artifact_without_overwrite(self):
        store = self.make_store()
        first = record_payload(run_id=100, run_attempt=1, accepted_at_ms=ACCEPT_MS)
        second = record_payload(
            run_id=100,
            run_attempt=2,
            accepted_at_ms=CARRY_MS,
            kind="carry",
            origin_run_id=100,
        )
        self.install_run(store, 100, 1, first)
        self.install_run(
            store,
            100,
            2,
            second,
            artifact_name=attempt_artifact_name(PR, 2),
            index_primary=False,
        )
        records = self.reader(self.store_handler(store)).load(PR)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[1].run_attempt, 2)
        history = self.reader(self.store_handler(store))
        with self.assertRaises(AlreadyAccepted):
            history.choose_artifact_name(PR, 100, 1)
        self.assertEqual(
            history.choose_artifact_name(PR, 300, 1), primary_artifact_name(PR)
        )
        self.assertEqual(
            history.choose_artifact_name(PR, 100, 3),
            attempt_artifact_name(PR, 3),
        )

    def test_choose_artifact_name_returns_existing_record_when_present(self):
        store = self.make_store()
        deep = record_payload(run_id=100)
        self.install_run(store, 100, 1, deep)
        history = self.reader(self.store_handler(store))
        with self.assertRaises(AlreadyAccepted) as ctx:
            history.choose_artifact_name(PR, 100, 1)
        self.assertEqual(ctx.exception.record, deep)

    def test_fold_history_coalesces_same_clock_events(self):
        history = self.reader(lambda *args: response({}))
        deep = record_payload(run_id=1, accepted_at_ms=20)
        again = record_payload(run_id=2, accepted_at_ms=20, head_sha="2" * 40)
        pending = history.fold_history((deep, again), ())
        self.assertEqual(pending.origin_run_id, 2)
        receipt = parse_receipt(
            json.dumps(
                {
                    "version": 1,
                    "repository": REPO,
                    "pr_number": PR,
                    "head_sha": "2" * 40,
                    "policy_base_sha": BASE,
                    "policy_digest": DIGEST,
                    "profile": "code",
                    "level": "deep",
                    "trigger": "manual",
                    "registry_digest": REGISTRY,
                    "context_sha256": CONTEXT,
                    "required_models": ["vendor/required"],
                    "successful_models": ["vendor/required"],
                    "panel_status": "complete",
                    "profile_satisfied": True,
                    "verdict": "clean",
                    "scope": "full-pr",
                    "mode": "initial",
                    "run_url": "https://github.com/RetireGolden/example/actions/runs/2",
                    "run_attempt": 1,
                },
                separators=(",", ":"),
            ).encode()
        )
        completion = VerifiedRunReceipt(receipt, 2, 1, 30)
        self.assertIsNone(history.fold_history((deep, again), (completion,)))

    def test_load_rejects_unauthorized_repository_and_pr_mismatch(self):
        store = self.make_store()
        wrong_repo = record_payload(repository="evil/repo")
        self.install_run(store, 100, 1, wrong_repo)
        with self.assertRaises(ProfileRequestError):
            self.reader(self.store_handler(store)).load(PR)

        wrong_pr = record_payload(pr_number=99)
        store = self.make_store()
        self.install_run(store, 100, 1, wrong_pr)
        with self.assertRaises(ProfileRequestError):
            self.reader(self.store_handler(store)).load(PR)

    def test_make_record_rejects_cancel_without_pending(self):
        history = self.reader(lambda *args: response({}))
        with self.assertRaises(ProfileRequestError):
            history.make_record(
                self.context_envelope(), "cancel", None, 1, 1, 1, REPO, PR
            )

    def test_first_acceptance_on_attempt_2_uses_primary_name(self):
        store = self.make_store()
        record = record_payload(
            run_id=100, run_attempt=2, accepted_at_ms=ACCEPT_MS + 5000
        )
        store["runs"][(100, 1)] = valid_run(100, 1)
        store["jobs"][(100, 1)] = []
        store["latest_attempt"][100] = 1
        self.install_run(
            store,
            100,
            2,
            record,
            artifact_name=primary_artifact_name(PR),
        )
        store["runs"][(100, 2)]["run_started_at"] = "2023-11-14T22:13:35Z"
        records = self.reader(self.store_handler(store)).load(PR)
        self.assertEqual(records[0].run_attempt, 2)
        history = self.reader(self.store_handler(store))
        with self.assertRaises(AlreadyAccepted):
            history.choose_artifact_name(PR, 100, 2)

    def _historical_verifier(self, store, client):
        def verify_historical(run):
            pin = run["referenced_workflows"][0]["sha"]
            if pin == PIN_OLD:
                jobs = store["jobs"][(run["id"], run["run_attempt"])]
                return VerifiedRun(
                    self.config.owner,
                    self.config.name,
                    run["id"],
                    run["run_attempt"],
                    run["head_sha"],
                    run["event"],
                    run.get("head_branch", ""),
                    run["workflow_id"],
                    dict(run),
                    tuple(jobs),
                )
            return client.verify_run(run, SHA, require_success=False)

        return verify_historical

    def test_historical_callback_preserves_pending_across_pins(self):
        store = self.make_store()
        old = record_payload(run_id=100, accepted_at_ms=ACCEPT_MS)
        new = record_payload(
            run_id=200,
            accepted_at_ms=CARRY_MS,
            kind="carry",
            head_sha="1" * 40,
            origin_run_id=100,
        )
        self.install_run(store, 100, 1, old, pin=PIN_OLD)
        self.install_run(store, 200, 1, new, head_sha="1" * 40)
        client = self.client(self.store_handler(store))
        history = RequestHistory(
            client,
            SHA,
            (JOB,),
            verify_historical_run=self._historical_verifier(store, client),
        )
        records = history.load(PR)
        self.assertEqual([item.kind for item in records], ["deep", "carry"])
        pending = history.fold_history(records, ())
        self.assertEqual(pending.origin_run_id, 100)

    def test_missing_historical_callback_rejects_old_pin(self):
        store = self.make_store()
        self.install_run(store, 100, 1, record_payload(run_id=100), pin=PIN_OLD)
        with self.assertRaises(ProfileRequestError):
            self.reader(self.store_handler(store)).load(PR)

    def test_historical_callback_rejects_mismatched_bounds(self):
        store = self.make_store()
        self.install_run(store, 100, 1, record_payload(run_id=100), pin=PIN_OLD)

        def bad_callback(run):
            jobs = store["jobs"][(run["id"], run["run_attempt"])]
            return VerifiedRun(
                self.config.owner,
                self.config.name,
                run["id"] + 1,
                run["run_attempt"],
                run["head_sha"],
                run["event"],
                run.get("head_branch", ""),
                run["workflow_id"],
                dict(run),
                tuple(jobs),
            )

        with self.assertRaises(ProfileRequestError):
            self.reader(
                self.store_handler(store), verify_historical_run=bad_callback
            ).load(PR)

    def test_index_continuation_api_failure_is_unavailable(self):
        page = {"count": 0}

        def failing_continuation(method, url, headers, cap, data):
            if "/actions/artifacts?name=" in url:
                page["count"] += 1
                if page["count"] == 1:
                    return response(
                        {
                            "artifacts": [
                                {
                                    "id": offset,
                                    "name": primary_artifact_name(PR),
                                    "expired": False,
                                    "workflow_run": {"id": offset},
                                }
                                for offset in range(100)
                            ]
                        }
                    )
                return response(b"broken", status=503)
            raise AssertionError(url)

        with self.assertRaisesRegex(ProfileRequestError, "unavailable"):
            self.reader(failing_continuation).load(PR)

    def test_canceled_job_with_successful_accept_still_loads(self):
        store = self.make_store()
        deep = record_payload(run_id=100, accepted_at_ms=ACCEPT_MS)
        self.install_run(
            store,
            100,
            1,
            deep,
            jobs=accept_jobs(conclusion="cancelled"),
        )
        records = self.reader(self.store_handler(store)).load(PR)
        self.assertEqual(records[0].kind, "deep")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
