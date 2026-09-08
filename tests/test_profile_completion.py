import hashlib
import io
import json
import subprocess
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest import mock

from or_pr_review.collect import CollectedReview, DiffPlan, Truncation
from or_pr_review.models import parse_model_routes
from or_pr_review.loop import Ledger, LedgerFinding, LoopState, encode_ledger
from or_pr_review.profile_evidence import canonical_receipt, parse_receipt
from or_pr_review.publish import render_review_parts
from or_pr_review.review_context import (
    PreparedExecution,
    freeze_context,
    freeze_runtime,
)
from or_pr_review.review_plan import parse_review_profiles, resolve_review_plan
from or_pr_review.review_policy import PolicyFile, ResolvedPolicy

from scripts.profile_completion import (
    ProfileCompletionError,
    ReceiptEvidence,
    VerifiedReview,
    context_artifact_name,
    receipt_artifact_name,
)
from scripts.profile_github import HTTPResponse, ProfileGitHub, TrustedWorkflow

SHA = "a" * 40
MAIN_SHA = "b" * 40
PIN = "c" * 40
BASE = "d" * 40
DIGEST = "e" * 64
REGISTRY_DIGEST = "f" * 64
CONTEXT_DIGEST = "0" * 64
REPO = "RetireGolden/example"
PR = 9
RUN_ID = 100
ATTEMPT = 1
REVIEW_ID = 501
JOB_INITIAL = "openrouter-first-pass"
JOB_VERIFY = "openrouter-follow-up"
REVIEW_STEP = "Run OpenRouter PR review"
REUSABLE_PATH = "RetireGolden/.github/.github/workflows/openrouter-code-review.yml"
RUN_URL = f"https://github.com/{REPO}/actions/runs/{RUN_ID}"
UPDATED_AT = "2023-11-14T22:13:45Z"
COMPLETION_MS = 1_700_000_025_999
GENERATION = "1" * 12
ROOT = Path(__file__).resolve().parents[1]
REGISTRY_RAW = (ROOT / "review-profiles.json").read_text(encoding="utf-8")
ROUTES_RAW = (ROOT / "review-model-routes.json").read_text(encoding="utf-8")


def response(value, status=200, headers=None):
    body = value if isinstance(value, bytes) else json.dumps(value).encode()
    return HTTPResponse(status, headers or {}, body)


class FakeTransport:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def __call__(self, method, url, headers, timeout, cap, data):
        self.calls.append((method, url, dict(headers), cap, data))
        return self.handler(method, url, headers, cap, data)


def review_jobs(job_name, *, conclusion="success", completed_at=UPDATED_AT):
    return [
        {
            "name": job_name,
            "conclusion": conclusion,
            "status": "completed",
            "steps": [
                {
                    "name": REVIEW_STEP,
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": completed_at,
                }
            ],
        }
    ]


def valid_run(
    *,
    run_id=RUN_ID,
    attempt=ATTEMPT,
    head_sha=SHA,
    event="pull_request",
    head_branch="topic",
):
    return {
        "id": run_id,
        "run_attempt": attempt,
        "workflow_id": 9,
        "head_sha": head_sha,
        "head_branch": head_branch,
        "event": event,
        "path": ".github/workflows/openrouter-code-review.yml",
        "repository": {"full_name": REPO},
        "head_repository": {"full_name": REPO},
        "referenced_workflows": [{"path": f"{REUSABLE_PATH}@{PIN}", "sha": PIN}],
        "status": "completed",
        "conclusion": "success",
        "updated_at": UPDATED_AT,
        "run_started_at": "2023-11-14T22:13:30Z",
    }


def zipped(name: str, payload: bytes) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, payload)
    return stream.getvalue()


def plan_for(*, level="standard", trigger="baseline", mode="initial"):
    registry = parse_review_profiles(REGISTRY_RAW)
    return resolve_review_plan(
        registry,
        profile="code",
        minimum=level,
        requested_level="deep" if level == "deep" else "auto",
        mode=mode,
        models=[],
        judge_model="openai/gpt-5.6-luna",
        routes=parse_model_routes(ROUTES_RAW),
        effort="",
        max_tool_turns=50,
        job_budget_seconds=1320,
        lane_timeout_seconds=1080,
    )


def bundle_for(
    *,
    level="standard",
    trigger="baseline",
    mode="initial",
    scope="full-pr",
    head_sha=SHA,
    run_id=RUN_ID,
    attempt=ATTEMPT,
    required=None,
    successful=None,
    policy_digest=DIGEST,
    policy_base=BASE,
    registry_digest=None,
    context_envelope=None,
):
    plan = plan_for(level=level, trigger=trigger, mode=mode)
    run_url = f"https://github.com/{REPO}/actions/runs/{run_id}"
    plan_required = tuple(lane.model for lane in plan.lanes if lane.required)
    if required is None:
        required = plan_required
    if successful is None:
        successful = required
    if registry_digest is None:
        registry_digest = plan.registry_digest
    policy = ResolvedPolicy(
        policy_base,
        "code",
        "deep" if level == "deep" else "standard",
        (PolicyFile("REVIEW.md", policy_base, "guidance", ("src/a.py",)),),
        (),
        ("src/a.py",),
        policy_digest,
    )
    diff = "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-old\n+new\n"
    collected = CollectedReview(
        PR,
        "title",
        "",
        head_sha,
        "main",
        "topic",
        DiffPlan(
            scope,
            "full-pr" if scope == "full-pr" else "commit-range",
            BASE if scope != "full-pr" else BASE,
            head_sha,
            None,
        ),
        Truncation(diff, False, len(diff), len(diff), 600),
        mode,
        ("src/a.py",),
        policy_base,
        policy,
    )
    execution = PreparedExecution(
        plan,
        freeze_runtime({"ROAST_LEVEL": "professional"}),
        "agent reply",
        1_700_000_000_000,
        1_700_001_320_000,
        run_url,
        attempt,
    )
    envelope = context_envelope or freeze_context(
        REPO, collected, LoopState(mode, 1), plan.max_tool_turns, execution=execution
    )
    receipt_dict = {
        "version": 1,
        "repository": REPO,
        "pr_number": PR,
        "head_sha": head_sha,
        "policy_base_sha": policy_base,
        "policy_digest": policy_digest,
        "profile": plan.profile,
        "level": plan.level,
        "trigger": plan.trigger,
        "registry_digest": registry_digest,
        "context_sha256": envelope["sha256"],
        "required_models": list(required),
        "successful_models": list(successful),
        "panel_status": "complete",
        "profile_satisfied": True,
        "verdict": "clean",
        "scope": scope,
        "mode": mode,
        "run_url": run_url,
        "run_attempt": attempt,
    }
    hidden_marker = encode_ledger(
        Ledger(1, (), head_sha, GENERATION), repo=REPO, pr_number=PR
    )
    body = render_review_parts(
        collected=collected,
        lanes=[],
        issues=[],
        verdict="clean",
        run_url=run_url,
        reviewed_sha=head_sha,
        hidden_marker=hidden_marker,
        receipt=receipt_dict,
    )[0]
    receipt = parse_receipt(
        canonical_receipt(
            parse_receipt(json.dumps(receipt_dict, separators=(",", ":")).encode())
        )
    )
    return {
        "body": body,
        "receipt": receipt,
        "receipt_bytes": canonical_receipt(receipt),
        "context_envelope": envelope,
        "context_bytes": json.dumps(envelope, separators=(",", ":")).encode(),
        "plan": plan,
        "collected": collected,
    }


class ReceiptEvidenceTests(unittest.TestCase):
    def test_new_verified_publication_recovers_from_old_failed_publication(self):
        store = self.make_store()
        old = bundle_for(run_id=100)
        new = bundle_for(run_id=101)
        self.install_bundle(store, old, run_id=100)
        self.install_bundle(store, new, run_id=101)
        store["runs"][(100, 1)]["conclusion"] = "failure"
        reviews = [
            self.review_record(old, review_id=501),
            self.review_record(new, review_id=502),
        ]
        self.assertEqual(
            [item.source.run_id for item in self.reader(store).collect(PR, reviews)],
            [101],
        )
        # A failed newest publication must not expose the previous clean result.
        store["runs"][(100, 1)]["conclusion"] = "success"
        store["runs"][(101, 1)]["conclusion"] = "failure"
        with self.assertRaises(ProfileCompletionError):
            self.reader(store).collect(PR, reviews)

    def test_actual_alternative_producer_steps_require_one_successful_job(self):
        store = self.make_store()
        bundle = bundle_for()
        self.install_bundle(store, bundle)
        client = self.client(self.store_handler(store))
        names = ("Run OpenRouter first-pass review", "Run OpenRouter follow-up review")
        store["jobs"][(RUN_ID, ATTEMPT)][0]["steps"][0]["name"] = names[0]
        reader = ReceiptEvidence(
            client,
            lambda run: client.verify_run(run, SHA),
            (JOB_INITIAL, JOB_VERIFY),
            names,
        )
        self.assertEqual(len(reader.collect(PR, [self.review_record(bundle)])), 1)
        store["jobs"][(RUN_ID, ATTEMPT)][0]["conclusion"] = "failure"
        with self.assertRaises(ProfileCompletionError):
            reader.collect(PR, [self.review_record(bundle)])

    def test_history_verifies_only_request_sources_and_allows_failed_request_retry(
        self,
    ):
        store = self.make_store()
        old, new = bundle_for(run_id=100), bundle_for(run_id=101)
        self.install_bundle(store, old, run_id=100)
        self.install_bundle(store, new, run_id=101)
        reviews = [
            self.review_record(old, review_id=501),
            self.review_record(new, review_id=502),
        ]
        reader = self.reader(store)
        self.assertEqual(
            [
                item.source.run_id
                for item in reader.collect(PR, reviews, relevant_runs=set())
            ],
            [101],
        )
        store["runs"][(100, 1)]["conclusion"] = "failure"
        self.assertEqual(
            reader.collect(PR, reviews, relevant_runs={(100, 1)}, include_latest=False),
            (),
        )
        # An absent completion does not clear the accepted request; the fold
        # remains responsible for carrying it onto the next review.

    def setUp(self):
        self.config = TrustedWorkflow(
            "RetireGolden", "example", "main", reusable_sha=PIN
        )
        self.store = None
        self.transport = None

    def client(self, handler):
        self.transport = FakeTransport(handler)
        return ProfileGitHub(self.config, "secret", self.transport)

    def make_store(self):
        return {
            "runs": {},
            "artifacts": {},
            "artifact_zips": {},
            "jobs": {},
            "repo": {"full_name": REPO},
        }

    def install_bundle(
        self,
        store,
        bundle,
        *,
        run_id=RUN_ID,
        attempt=ATTEMPT,
        head_sha=SHA,
        event="pull_request",
        head_branch="topic",
        job_name=JOB_INITIAL,
    ):
        run = valid_run(
            run_id=run_id,
            attempt=attempt,
            head_sha=head_sha,
            event=event,
            head_branch=head_branch,
        )
        store["runs"][(run_id, attempt)] = run
        store["jobs"][(run_id, attempt)] = review_jobs(job_name)
        receipt_name = receipt_artifact_name(run_id, attempt)
        context_name = context_artifact_name(run_id, attempt)
        receipt_zip = zipped("review-receipt.json", bundle["receipt_bytes"])
        context_zip = zipped("context.json", bundle["context_bytes"])
        store["artifacts"][run_id] = [
            {
                "id": run_id * 10 + 1,
                "name": receipt_name,
                "expired": False,
                "digest": "sha256:" + hashlib.sha256(receipt_zip).hexdigest(),
                "workflow_run": {"id": run_id, "head_sha": head_sha},
            },
            {
                "id": run_id * 10 + 2,
                "name": context_name,
                "expired": False,
                "digest": "sha256:" + hashlib.sha256(context_zip).hexdigest(),
                "workflow_run": {"id": run_id, "head_sha": head_sha},
            },
        ]
        store["artifact_zips"][run_id * 10 + 1] = receipt_zip
        store["artifact_zips"][run_id * 10 + 2] = context_zip

    def provenance(self, store):
        def handler(method, url, headers, cap, data):
            url = url.split("?", 1)[0]
            if "/actions/workflows/" in url:
                return response(
                    {"id": 9, "path": self.config.caller_path, "state": "active"}
                )
            if "/contents/" in url:
                return response({"type": "file", "sha": SHA})
            if url.endswith("/repos/RetireGolden/example"):
                return response(store["repo"])
            if "/attempts/" in url and url.endswith("/jobs"):
                run_id = int(url.split("/actions/runs/")[1].split("/attempts/")[0])
                attempt = int(url.split("/attempts/")[1].split("/jobs")[0])
                return response({"jobs": store["jobs"][(run_id, attempt)]})
            raise AssertionError(url)

        return handler

    def store_handler(self, store):
        base = self.provenance(store)

        def handler(method, url, headers, cap, data):
            url = url.split("?", 1)[0]
            if "/pulls/" in url and url.endswith("/reviews"):
                return response([])
            if "/repos/RetireGolden/example" == url.split("?", 1)[0].replace(
                "https://api.github.com", ""
            ):
                return response(store["repo"])
            if (
                "/actions/runs/" in url
                and "/attempts/" in url
                and not url.endswith("/jobs")
            ):
                run_id = int(url.split("/actions/runs/")[1].split("/attempts/")[0])
                attempt = int(url.split("/attempts/")[1].split("?")[0].rstrip("/"))
                return response(store["runs"][(run_id, attempt)])
            if "/actions/runs/" in url and url.endswith("/artifacts"):
                run_id = int(url.split("/actions/runs/")[1].split("/artifacts")[0])
                return response({"artifacts": store["artifacts"].get(run_id, [])})
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

    def reader(
        self, store, *, verify_historical_run=None, allowed=(JOB_INITIAL, JOB_VERIFY)
    ):
        client = self.client(self.store_handler(store))

        def verify_source(run):
            if verify_historical_run is not None:
                verified = verify_historical_run(run)
            else:
                verified = client.verify_run(run, SHA, require_success=True)
            return verified

        return ReceiptEvidence(client, verify_source, allowed, [REVIEW_STEP])

    def review_record(self, bundle, *, review_id=REVIEW_ID):
        return {
            "id": review_id,
            "user": {
                "login": self.config.bot_login,
                "id": self.config.bot_id,
                "type": self.config.bot_type,
            },
            "commit_id": bundle["receipt"].head_sha,
            "body": bundle["body"],
        }

    def test_collect_initial_standard_and_verify_deep(self):
        for level, trigger, mode, scope, job in (
            ("standard", "baseline", "initial", "full-pr", JOB_INITIAL),
            ("deep", "manual", "initial", "full-pr", JOB_INITIAL),
            ("standard", "baseline", "verify", "latest-commit", JOB_VERIFY),
        ):
            with self.subTest(level=level, mode=mode):
                store = self.make_store()
                bundle = bundle_for(
                    level=level, trigger=trigger, mode=mode, scope=scope
                )
                self.install_bundle(store, bundle, job_name=job)
                reader = self.reader(store)
                items = reader.collect(PR, [self.review_record(bundle)])
                self.assertEqual(len(items), 1)
                item = items[0]
                self.assertIsInstance(item, VerifiedReview)
                self.assertEqual(item.receipt.level, level)
                self.assertEqual(item.completion.completed_at_ms, COMPLETION_MS)

    def test_collect_dispatch_accepts_default_branch_source_head(self):
        store = self.make_store()
        bundle = bundle_for(head_sha=SHA)
        self.install_bundle(
            store,
            bundle,
            head_sha=MAIN_SHA,
            event="workflow_dispatch",
            head_branch="main",
        )
        items = self.reader(store).collect(PR, [self.review_record(bundle)])
        self.assertEqual(items[0].source.head_sha, MAIN_SHA)
        self.assertEqual(items[0].receipt.head_sha, SHA)

    def test_collect_dispatch_preserves_settled_disputes_without_accepting_open_findings(self):
        for include_open in (False, True):
            with self.subTest(include_open=include_open):
                store = self.make_store()
                bundle = bundle_for(head_sha=SHA, mode="verify")
                findings = [LedgerFinding("r1-1", "risk", "src/a.py", 1, "Rebutted", "Evidence", "disputed")]
                if include_open:
                    findings.append(LedgerFinding("r1-2", "bug", "src/b.py", 2, "Unresolved", "Evidence", "open"))
                lines = bundle["body"].splitlines()
                lines[1] = encode_ledger(Ledger(3, tuple(findings), SHA, GENERATION), repo=REPO, pr_number=PR)
                bundle["body"] = "\n".join(lines)
                self.install_bundle(store, bundle, head_sha=MAIN_SHA, event="workflow_dispatch", head_branch="main")
                if include_open:
                    with self.assertRaises(ProfileCompletionError):
                        self.reader(store).collect(PR, [self.review_record(bundle)])
                else:
                    items = self.reader(store).collect(PR, [self.review_record(bundle)])
                    self.assertEqual(len(items), 1)
                    self.assertEqual(items[0].receipt.verdict, "clean")
                    self.assertEqual(items[0].receipt.head_sha, SHA)

    def test_collect_dispatch_accepts_equal_source_and_pr_head(self):
        store = self.make_store()
        bundle = bundle_for(head_sha=SHA)
        self.install_bundle(
            store,
            bundle,
            head_sha=SHA,
            event="workflow_dispatch",
            head_branch="main",
        )
        items = self.reader(store).collect(PR, [self.review_record(bundle)])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].source.head_sha, SHA)
        self.assertEqual(items[0].receipt.head_sha, SHA)

    def test_collect_ignores_legacy_review_without_marker(self):
        store = self.make_store()
        bundle = bundle_for()
        self.install_bundle(store, bundle)
        legacy = self.review_record(bundle)
        legacy["body"] = legacy["body"].split("<!-- openrouter-review-plan:v1:", 1)[0]
        items = self.reader(store).collect(PR, [legacy])
        self.assertEqual(items, ())

    def test_collect_rejects_forged_marker_body_and_missing_artifacts(self):
        store = self.make_store()
        bundle = bundle_for()
        self.install_bundle(store, bundle)
        reader = self.reader(store)

        bad_body = self.review_record(bundle)
        bad_body["body"] = bad_body["body"].replace(
            bundle["receipt"].head_sha, "f" * 40, 1
        )
        with self.assertRaises(ProfileCompletionError):
            reader.collect(PR, [bad_body])

        store = self.make_store()
        self.install_bundle(store, bundle)
        store["artifacts"][RUN_ID] = [store["artifacts"][RUN_ID][1]]
        with self.assertRaises(ProfileCompletionError):
            self.reader(store).collect(PR, [self.review_record(bundle)])

        store = self.make_store()
        self.install_bundle(store, bundle)
        store["artifacts"][RUN_ID][0]["expired"] = True
        with self.assertRaises(ProfileCompletionError):
            self.reader(store).collect(PR, [self.review_record(bundle)])

    def test_collect_rejects_wrong_repo_head_required_models_and_failed_job(self):
        store = self.make_store()
        bundle = bundle_for()
        self.install_bundle(store, bundle)
        bad = self.review_record(bundle)
        bad["commit_id"] = "f" * 40
        with self.assertRaises(ProfileCompletionError):
            self.reader(store).collect(PR, [bad])

        store = self.make_store()
        bundle = bundle_for()
        self.install_bundle(store, bundle, head_sha="1" * 40)
        with self.assertRaises(ProfileCompletionError):
            self.reader(store).collect(PR, [self.review_record(bundle)])

        store = self.make_store()
        bundle = bundle_for(
            required=("openai/gpt-6-astra",), successful=("openai/gpt-6-astra",)
        )
        self.install_bundle(store, bundle)
        with self.assertRaises(ProfileCompletionError):
            self.reader(store).collect(PR, [self.review_record(bundle)])

        store = self.make_store()
        bundle = bundle_for()
        self.install_bundle(store, bundle)
        store["jobs"][(RUN_ID, ATTEMPT)] = review_jobs(
            JOB_INITIAL, conclusion="failure"
        )
        with self.assertRaises(ProfileCompletionError):
            self.reader(store).collect(PR, [self.review_record(bundle)])

    def test_collect_dedupes_identical_run_and_rejects_marker_artifact_mismatch(self):
        store = self.make_store()
        bundle = bundle_for()
        self.install_bundle(store, bundle)
        items = self.reader(store).collect(
            PR,
            [
                self.review_record(bundle, review_id=1),
                self.review_record(bundle, review_id=2),
            ],
        )
        self.assertEqual(len(items), 1)

        store = self.make_store()
        first = bundle_for()
        second = bundle_for(head_sha="2" * 40)
        self.install_bundle(store, first)
        mismatched = self.review_record(second)
        with self.assertRaises(ProfileCompletionError):
            self.reader(store).collect(PR, [mismatched])

        store = self.make_store()
        bundle = bundle_for()
        tampered = dict(bundle["context_envelope"])
        tampered["sha256"] = "1" * 64
        bundle = dict(bundle)
        bundle["context_envelope"] = tampered
        bundle["context_bytes"] = json.dumps(tampered, separators=(",", ":")).encode()
        self.install_bundle(store, bundle)
        with self.assertRaises(ProfileCompletionError):
            self.reader(store).collect(PR, [self.review_record(bundle)])

        store = self.make_store()
        bundle = bundle_for()
        tampered = dict(bundle["context_envelope"])
        tampered["sha256"] = "1" * 64
        bundle = dict(bundle)
        bundle["context_envelope"] = tampered
        bundle["context_bytes"] = json.dumps(tampered, separators=(",", ":")).encode()
        self.install_bundle(store, bundle)
        with self.assertRaises(ProfileCompletionError):
            self.reader(store).collect(PR, [self.review_record(bundle)])

    def test_current_decision_accepts_matching_policy_and_registry(self):
        store = self.make_store()
        bundle = bundle_for(level="deep", trigger="manual")
        self.install_bundle(store, bundle)
        verified = self.reader(store).collect(PR, [self.review_record(bundle)])[0]
        evidence = ReceiptEvidence(
            self.client(lambda *args: response({})),
            lambda run: None,
            (JOB_INITIAL,),
            [REVIEW_STEP],
        )
        with mock.patch("scripts.profile_completion.resolve_policy") as resolve_policy:
            resolve_policy.return_value = bundle["collected"].review_policy
            decision = evidence.current_decision(
                verified,
                ROOT,
                BASE,
                SHA,
                REGISTRY_RAW,
                ROUTES_RAW,
                minimum="deep",
            )
        self.assertTrue(decision.satisfied)

    def test_current_decision_rejects_changed_registry_or_policy(self):
        store = self.make_store()
        bundle = bundle_for(level="deep", trigger="manual")
        self.install_bundle(store, bundle)
        verified = self.reader(store).collect(PR, [self.review_record(bundle)])[0]
        evidence = ReceiptEvidence(
            self.client(lambda *args: response({})),
            lambda run: None,
            (JOB_INITIAL,),
            [REVIEW_STEP],
        )
        with mock.patch("scripts.profile_completion.resolve_policy") as resolve_policy:
            resolve_policy.return_value = bundle["collected"].review_policy
            changed_registry = json.loads(REGISTRY_RAW)
            changed_registry["profiles"]["code"]["standard"]["lanes"][0]["model"] = (
                "openai/gpt-6-astra"
            )
            self.assertFalse(
                evidence.current_decision(
                    verified,
                    ROOT,
                    BASE,
                    SHA,
                    json.dumps(changed_registry, separators=(",", ":")),
                    ROUTES_RAW,
                    minimum="deep",
                ).satisfied
            )
            resolve_policy.return_value = replace(
                bundle["collected"].review_policy,
                digest="9" * 64,
            )
            self.assertFalse(
                evidence.current_decision(
                    verified,
                    ROOT,
                    BASE,
                    SHA,
                    REGISTRY_RAW,
                    ROUTES_RAW,
                    minimum="deep",
                ).satisfied
            )

    def test_current_decision_policy_deep_rejects_standard_receipt(self):
        store = self.make_store()
        bundle = bundle_for(level="standard", trigger="baseline")
        self.install_bundle(store, bundle)
        verified = self.reader(store).collect(PR, [self.review_record(bundle)])[0]
        evidence = ReceiptEvidence(
            self.client(lambda *args: response({})),
            lambda run: None,
            (JOB_INITIAL,),
            [REVIEW_STEP],
        )
        with mock.patch("scripts.profile_completion.resolve_policy") as resolve_policy:
            resolve_policy.return_value = replace(
                bundle["collected"].review_policy,
                minimum="deep",
            )
            decision = evidence.current_decision(
                verified,
                ROOT,
                BASE,
                SHA,
                REGISTRY_RAW,
                ROUTES_RAW,
                minimum="standard",
            )
        self.assertFalse(decision.satisfied)
        self.assertEqual(
            decision.reason, "standard receipt cannot satisfy deep minimum"
        )

    def test_current_decision_policy_deep_accepts_deep_receipt_with_unchanged_required_models(
        self,
    ):
        store = self.make_store()
        bundle = bundle_for(level="deep", trigger="manual")
        self.install_bundle(store, bundle)
        verified = self.reader(store).collect(PR, [self.review_record(bundle)])[0]
        evidence = ReceiptEvidence(
            self.client(lambda *args: response({})),
            lambda run: None,
            (JOB_INITIAL,),
            [REVIEW_STEP],
        )
        with mock.patch("scripts.profile_completion.resolve_policy") as resolve_policy:
            resolve_policy.return_value = replace(
                bundle["collected"].review_policy,
                minimum="deep",
            )
            decision = evidence.current_decision(
                verified,
                ROOT,
                BASE,
                SHA,
                REGISTRY_RAW,
                ROUTES_RAW,
                minimum="standard",
            )
        self.assertTrue(decision.satisfied)

    def test_current_decision_deep_receipt_can_satisfy_standard_minimum(self):
        store = self.make_store()
        bundle = bundle_for(level="deep", trigger="manual")
        self.install_bundle(store, bundle)
        verified = self.reader(store).collect(PR, [self.review_record(bundle)])[0]
        evidence = ReceiptEvidence(
            self.client(lambda *args: response({})),
            lambda run: None,
            (JOB_INITIAL,),
            [REVIEW_STEP],
        )
        with mock.patch("scripts.profile_completion.resolve_policy") as resolve_policy:
            resolve_policy.return_value = bundle["collected"].review_policy
            decision = evidence.current_decision(
                verified,
                ROOT,
                BASE,
                SHA,
                REGISTRY_RAW,
                ROUTES_RAW,
                minimum="standard",
            )
        self.assertTrue(decision.satisfied)

    def test_current_decision_uses_carried_paths_with_real_git_fixture(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            repo.mkdir()
            subprocess.run(
                ["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True
            )
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=repo,
                check=True,
            )
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            review = repo / "REVIEW.md"
            review.write_text(
                "```review-policy\n"
                '{"version": 1, "review": {"profile": "code", "minimum": "standard"}}\n'
                "```\n",
                encoding="utf-8",
            )
            (repo / "src").mkdir()
            (repo / "src/a.py").write_text("old\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "."], cwd=repo, check=True, capture_output=True
            )
            subprocess.run(
                ["git", "commit", "-m", "base"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            base_sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            (repo / "src/a.py").write_text("new\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "src/a.py"], cwd=repo, check=True, capture_output=True
            )
            subprocess.run(
                ["git", "commit", "-m", "head"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            head_sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            from or_pr_review.review_policy import resolve_policy

            resolved = resolve_policy(
                repo, base_sha, head_sha, carried_paths=("src/a.py",)
            )
            store = self.make_store()
            bundle = bundle_for(
                head_sha=head_sha, policy_base=base_sha, policy_digest=resolved.digest
            )
            self.install_bundle(store, bundle, head_sha=head_sha)
            verified = self.reader(store).collect(PR, [self.review_record(bundle)])[0]
            decision = ReceiptEvidence(
                self.client(lambda *args: response({})),
                lambda run: None,
                (JOB_INITIAL,),
                [REVIEW_STEP],
            ).current_decision(
                verified,
                repo,
                base_sha,
                head_sha,
                REGISTRY_RAW,
                ROUTES_RAW,
                minimum="standard",
            )
            self.assertTrue(decision.satisfied)
            self.assertEqual(resolved.changed_paths, ("src/a.py",))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
