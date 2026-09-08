"""Offline boundaries for profile_gate orchestration helpers.

The integration suite owns the real GitHub/action fixtures.  These tests keep
the gate's local trust decisions deterministic without executing a checkout or
calling the network.
"""

from __future__ import annotations

import os
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from or_pr_review.collect import CollectedReview, DiffPlan, Truncation
from or_pr_review.loop import LoopState
from or_pr_review.profile_evidence import (
    AcceptedRequest,
    GateDecision,
    PendingRequest,
    VerifiedRunReceipt,
    fold_requests,
    parse_receipt,
)
from or_pr_review.review_context import (
    PreparedExecution,
    freeze_context,
    freeze_runtime,
)
from or_pr_review.review_plan import ReviewLane, ReviewPlan
from or_pr_review.review_policy import PolicyFile, ResolvedPolicy
from scripts import profile_gate as gate
from scripts.profile_github import (
    ProfileGitHub,
    ProfileGitHubError,
    TrustedWorkflow,
    VerifiedRun,
)
from scripts.profile_requests import RequestHistory, parse_request


SHA_A = "a" * 40
SHA_B = "b" * 40
PIN = "c" * 40
DIGEST = "d" * 64
REGISTRY = "e" * 64
REPO = "RetireGolden/example"


def live_pr(head=SHA_A, base=SHA_B):
    return {
        "state": "open",
        "draft": False,
        "head": {"sha": head, "repo": {"full_name": REPO}},
        "base": {"sha": base, "ref": "main", "repo": {"full_name": REPO}},
    }


def frozen_context(*, run_id=101, attempt=2, head=SHA_A, base=SHA_B):
    """Create a real action envelope, rather than hand-building its digest."""
    policy = ResolvedPolicy(
        base,
        "code",
        "deep",
        (PolicyFile("REVIEW.md", base, "guidance", ("a.py",)),),
        (),
        ("a.py",),
        DIGEST,
    )
    diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"
    collected = CollectedReview(
        7,
        "title",
        "",
        head,
        "main",
        "topic",
        DiffPlan("full-pr", "full-pr", base, head, None),
        Truncation(diff, False, len(diff), len(diff), 300),
        "initial",
        ("a.py",),
        base,
        policy,
    )
    plan = ReviewPlan(
        "code",
        "deep",
        "manual",
        (ReviewLane("openai/gpt-5", True, "OpenAI", "priority"),),
        "openai/gpt-5",
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
        "reply",
        1_700_000_000_000,
        1_700_001_320_000,
        f"https://github.com/{REPO}/actions/runs/{run_id}",
        attempt,
    )
    return freeze_context(
        REPO, collected, LoopState("initial", 1), 50, execution=execution
    )


def clean_receipt(*, run_id, attempt, level="deep", head=SHA_A):
    return parse_receipt(
        json.dumps(
            {
                "version": 1,
                "repository": REPO,
                "pr_number": 7,
                "head_sha": head,
                "policy_base_sha": SHA_B,
                "policy_digest": DIGEST,
                "profile": "code",
                "level": level,
                "trigger": "manual" if level == "deep" else "baseline",
                "registry_digest": REGISTRY,
                "context_sha256": "f" * 64,
                "required_models": ["openai/gpt-5"],
                "successful_models": ["openai/gpt-5"],
                "panel_status": "complete",
                "profile_satisfied": True,
                "verdict": "clean",
                "scope": "full-pr",
                "mode": "initial",
                "run_url": f"https://github.com/{REPO}/actions/runs/{run_id}",
                "run_attempt": attempt,
            }
        ).encode()
    )


class CallerTrustTests(unittest.TestCase):
    def test_live_pr_age_cannot_outlive_artifact_retention(self):
        from datetime import datetime, timedelta, timezone

        client = Mock(
            config=TrustedWorkflow("RetireGolden", "example", "main", reusable_sha=PIN)
        )
        pr = live_pr()
        now = datetime.now(timezone.utc)
        for days, allowed in ((0, True), (24, True), (25, False), (90, False)):
            pr["created_at"] = (now - timedelta(days=days)).isoformat()
            client.get_pr.return_value = pr
            if allowed:
                self.assertIs(gate._live_pr(client, 7, "main"), pr)
            else:
                with self.assertRaisesRegex(gate.GateError, "25 days"):
                    gate._live_pr(client, 7, "main")

    def test_caller_pin_requires_one_literal_immutable_pin(self) -> None:
        body = (
            b"uses: RetireGolden/.github/.github/workflows/openrouter-code-review.yml@"
            + PIN.encode()
        )
        self.assertEqual(gate._caller_pin(body), PIN)
        with self.assertRaises(gate.GateError):
            gate._caller_pin(body + b"\n" + body)
        with self.assertRaises(gate.GateError):
            gate._caller_pin(b"uses: ${UNTRUSTED_PIN}")

    def test_receipt_adapter_checks_only_the_step_owned_by_each_job(self) -> None:
        client = ProfileGitHub(
            TrustedWorkflow("RetireGolden", "example", "main", reusable_sha=PIN),
            "token",
            lambda *_: None,
        )
        job = {
            "name": gate.REQUEST_JOBS[0],
            "conclusion": "success",
            "steps": [
                {
                    "name": "Run OpenRouter first-pass review",
                    "status": "completed",
                    "conclusion": "success",
                }
            ],
        }
        verified = VerifiedRun(
            "RetireGolden",
            "example",
            1,
            1,
            SHA_A,
            "pull_request",
            "topic",
            9,
            {},
            (job,),
        )
        adapter = gate._ReceiptStepAdapter(client, gate.REVIEW_STEPS_BY_JOB)
        self.assertTrue(
            adapter.successful_steps(verified, gate.REQUEST_JOBS[0], ["ignored"])
        )
        self.assertFalse(
            adapter.successful_steps(verified, gate.REQUEST_JOBS[1], ["ignored"])
        )

    def test_historical_verifier_rejects_pr_only_caller_blob(self) -> None:
        class FakeClient:
            config = TrustedWorkflow(
                "RetireGolden", "example", "main", reusable_sha=PIN
            )
            token = None
            transport = None
            timeout = 1

            def _content_blob(self, sha: str) -> str:
                return SHA_A if sha == SHA_B else "d" * 40

        client = FakeClient()
        caller = (
            b"uses: RetireGolden/.github/.github/workflows/openrouter-code-review.yml@"
            + PIN.encode()
        )
        with (
            patch.object(gate, "_source_workspace", return_value=gate.Path("fixture")),
            patch.object(gate, "_git", side_effect=[(SHA_A + "\n").encode()]),
            patch.object(gate, "_file_at", return_value=(SHA_A, caller)),
        ):
            verifier = gate._historical_verifier(client, "main", SHA_A)  # type: ignore[arg-type]
            with self.assertRaises(ProfileGitHubError):
                verifier({"head_sha": "e" * 40})


class BeginMigrationTests(unittest.TestCase):
    def test_migration_is_audit_only_and_never_uses_main_pin(self) -> None:
        run = {"id": 1, "run_attempt": 1, "head_sha": SHA_B}
        calls: list[str] = []

        class Client:
            config = TrustedWorkflow(
                "RetireGolden", "example", "main", reusable_sha=SHA_A
            )

            def _api(self, endpoint: str):
                return run

            def _content_blob(self, sha: str) -> str:
                return SHA_B

            def verify_run(self, value, blob):
                calls.append(blob)
                return SimpleNamespace()

        env = {
            "GITHUB_RUN_ID": "1",
            "GITHUB_RUN_ATTEMPT": "1",
            "REVIEW_LEVEL": "auto",
            "ORG_WORKFLOW_SHA": PIN,
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(gate, "_configured_client", return_value=Client()),
        ):
            self.assertTrue(
                gate._verify_begin_run(Client(), SHA_A, pin_matches_main=False)
            )
        self.assertEqual(calls, [SHA_B])

    def test_explicit_migration_refuses_before_authority(self) -> None:
        class Client:
            config = TrustedWorkflow(
                "RetireGolden", "example", "main", reusable_sha=SHA_A
            )

            def _api(self, endpoint: str):
                return {"id": 1, "run_attempt": 1, "head_sha": SHA_B}

        with patch.dict(
            os.environ,
            {"GITHUB_RUN_ID": "1", "GITHUB_RUN_ATTEMPT": "1", "REVIEW_LEVEL": "deep"},
            clear=True,
        ):
            with self.assertRaises(gate.GateError):
                gate._verify_begin_run(Client(), SHA_A, pin_matches_main=False)

    def test_same_pin_blob_mismatch_is_audit_only(self) -> None:
        run = {"id": 1, "run_attempt": 1, "head_sha": SHA_B}
        calls: list[str] = []

        class Client:
            config = TrustedWorkflow(
                "RetireGolden", "example", "main", reusable_sha=PIN
            )

            def _api(self, endpoint: str):
                return run

            def _content_blob(self, sha: str) -> str:
                return SHA_B

            def verify_run(self, value, blob):
                calls.append(blob)
                if blob == SHA_A:
                    raise ProfileGitHubError("caller blob mismatch")
                return SimpleNamespace()

        env = {
            "GITHUB_RUN_ID": "1",
            "GITHUB_RUN_ATTEMPT": "1",
            "REVIEW_LEVEL": "auto",
            "ORG_WORKFLOW_SHA": PIN,
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(gate, "_configured_client", return_value=Client()),
        ):
            self.assertTrue(
                gate._verify_begin_run(Client(), SHA_A, pin_matches_main=True)
            )
        self.assertEqual(calls, [SHA_A, SHA_B])

        with (
            patch.dict(os.environ, {**env, "REVIEW_LEVEL": "deep"}, clear=True),
            patch.object(gate, "_configured_client", return_value=Client()),
        ):
            with self.assertRaises(gate.GateError):
                gate._verify_begin_run(Client(), SHA_A, pin_matches_main=True)

    def test_current_caller_match_is_not_audit_only(self) -> None:
        class Client:
            config = TrustedWorkflow(
                "RetireGolden", "example", "main", reusable_sha=PIN
            )

            def _api(self, endpoint: str):
                return {"id": 1, "run_attempt": 1, "head_sha": SHA_A}

            def verify_run(self, value, blob):
                return SimpleNamespace()

        env = {
            "GITHUB_RUN_ID": "1",
            "GITHUB_RUN_ATTEMPT": "1",
            "REVIEW_LEVEL": "auto",
            "ORG_WORKFLOW_SHA": PIN,
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(
                gate._verify_begin_run(Client(), SHA_A, pin_matches_main=True)
            )


class PublishProofTests(unittest.TestCase):
    def test_proof_matrix_rejects_duplicate_and_noncanonical_fields(self) -> None:
        duplicate = (
            '[{"pr_number":1,"head_sha":"'
            + SHA_A
            + '","base_sha":"'
            + SHA_B
            + '","receipt_digest":"'
            + ("d" * 64)
            + '"},{"pr_number":1,"head_sha":"'
            + SHA_A
            + '","base_sha":"'
            + SHA_B
            + '","receipt_digest":"'
            + ("e" * 64)
            + '"}]'
        )
        with patch.dict(os.environ, {"PROOF_MATRIX_JSON": duplicate}, clear=True):
            with self.assertRaises(gate.GateError):
                gate._proof_matrix()

    def test_dynamic_job_name_is_exact(self) -> None:
        digest = "d" * 64
        jobs = [
            {
                "name": f"complete / profile #7 {digest}",
                "status": "completed",
                "conclusion": "success",
            }
        ]
        self.assertEqual(
            [
                item
                for item in jobs
                if item["name"] == f"complete / profile #7 {digest}"
            ],
            jobs,
        )
        self.assertEqual(
            [
                item
                for item in jobs
                if item["name"] == f"complete / profile #7 {digest[:-1]}x"
            ],
            [],
        )


class GateCommandTests(unittest.TestCase):
    def setUp(self):
        # These command tests inject _current_trust; avoid evaluating a real
        # environment-backed client before that injected boundary is called.
        client_patch = patch.object(gate, "_client", return_value=Mock())
        client_patch.start()
        self.addCleanup(client_patch.stop)
        source_patch = patch.object(
            gate, "_source_workspace", return_value=Path(__file__).parents[1]
        )
        source_patch.start()
        self.addCleanup(source_patch.stop)

    def test_begin_explicit_deep_supersedes_pending_request(self) -> None:
        pending = PendingRequest(
            origin_run_id=11,
            head_sha=SHA_A,
            policy_digest=DIGEST,
            registry_digest=REGISTRY,
            profile="code",
            last_accepted_run_id=11,
            last_accepted_run_attempt=1,
        )
        history = Mock()
        history.load.return_value = ()
        history.fold_history.return_value = pending
        bare = SimpleNamespace(
            config=TrustedWorkflow("RetireGolden", "example", "main", reusable_sha=PIN),
            maintainer=lambda _: True,
        )
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state.json"
            captured = {}
            env = {
                "REVIEW_LEVEL": "deep",
                "GITHUB_EVENT_NAME": "workflow_dispatch",
                "GITHUB_REF": "refs/heads/main",
                "GITHUB_ACTOR": "maintainer",
                "PR_NUMBER": "7",
                "GITHUB_RUN_ID": "101",
                "GITHUB_RUN_ATTEMPT": "2",
                "ORG_WORKFLOW_SHA": PIN,
                "REQUEST_STATE_FILE": str(state),
                "GITHUB_REPOSITORY": REPO,
            }
            with (
                patch.dict(os.environ, env, clear=True),
                patch.object(
                    gate, "_current_trust", return_value=(bare, SHA_B, SHA_A, b"")
                ),
                patch.object(gate, "_verify_begin_run", return_value=False),
                patch.object(
                    gate,
                    "_current_run",
                    return_value={"display_title": "OpenRouter PR #7: deep"},
                ),
                patch.object(gate, "_live_pr", return_value=live_pr()),
                patch.object(
                    gate,
                    "_history_and_evidence",
                    return_value=(history, Mock(collect=Mock(return_value=())), Mock()),
                ),
                patch.object(gate, "_output", side_effect=captured.update),
            ):
                gate.begin()
            self.assertEqual(captured["request_kind"], "deep")
            self.assertEqual(
                json.loads(state.read_text(encoding="utf-8"))["request_kind"], "deep"
            )

    def test_accept_uses_real_envelope_and_writes_canonical_request_json(self) -> None:
        envelope = frozen_context()
        history_client = ProfileGitHub(
            TrustedWorkflow("RetireGolden", "example", "main", reusable_sha=PIN),
            "token",
            lambda *_: None,
        )
        history = RequestHistory(history_client, SHA_A, gate.REQUEST_JOBS)
        pending = {
            "origin_run_id": 11,
            "head_sha": SHA_A,
            "policy_digest": DIGEST,
            "registry_digest": REGISTRY,
            "profile": "code",
            "last_accepted_run_id": 11,
            "last_accepted_run_attempt": 1,
        }
        bare = SimpleNamespace(
            config=TrustedWorkflow("RetireGolden", "example", "main", reusable_sha=PIN),
            create_status=Mock(),
            paginated=Mock(),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context_path, state_path = root / "context.json", root / "state.json"
            context_path.write_text(
                json.dumps(envelope, separators=(",", ":")), encoding="utf-8"
            )
            state_path.write_text(
                json.dumps(
                    {
                        "repository": REPO,
                        "pr_number": 7,
                        "head_sha": SHA_A,
                        "base_sha": SHA_B,
                        "run_id": 101,
                        "run_attempt": 2,
                        "request_kind": "deep",
                        "audit_only": False,
                        "pending": pending,
                    }
                ),
                encoding="utf-8",
            )
            captured = {}
            env = {
                "GITHUB_REPOSITORY": REPO,
                "GITHUB_RUN_ID": "101",
                "GITHUB_RUN_ATTEMPT": "2",
                "ORG_WORKFLOW_SHA": PIN,
                "REQUEST_STATE_FILE": str(state_path),
                "REVIEW_CONTEXT_FILE": str(context_path),
                "REVIEW_CONTEXT_SHA256": envelope["sha256"],
            }
            with (
                patch.dict(os.environ, env, clear=True),
                patch.object(
                    gate, "_current_trust", return_value=(bare, SHA_B, SHA_A, b"")
                ),
                patch.object(gate, "_live_pr", return_value=live_pr()),
                patch.object(
                    gate,
                    "_history_and_evidence",
                    return_value=(history, Mock(), Mock()),
                ),
                patch.object(
                    history,
                    "choose_artifact_name",
                    return_value="openrouter-request-7-attempt-2",
                ),
                patch.object(gate, "_output", side_effect=captured.update),
            ):
                gate.accept()
            request_path = Path(captured["request_file"])
            self.assertEqual(request_path.name, "request.json")
            self.assertEqual(request_path.parent.name, "2")
            record = parse_request(request_path.read_bytes())
            self.assertEqual(record.kind, "deep")
            self.assertIsNone(record.origin_run_id)
            self.assertEqual(record.context_sha256, envelope["sha256"])

    def test_fold_does_not_let_old_standard_or_equal_time_completion_clear_deep_carry(
        self,
    ) -> None:
        deep = AcceptedRequest(11, 1, 10, "deep", SHA_A, DIGEST, REGISTRY, "code", None)
        carry = AcceptedRequest(12, 1, 20, "carry", SHA_B, DIGEST, REGISTRY, "code", 11)
        old_standard = VerifiedRunReceipt(
            clean_receipt(run_id=11, attempt=1, level="standard"), 11, 1, 20
        )
        pending = fold_requests((deep, carry), (old_standard,))
        self.assertIsNotNone(pending)
        self.assertEqual(
            (pending.origin_run_id, pending.last_accepted_run_id, pending.head_sha),
            (11, 12, SHA_B),
        )

        replacement = AcceptedRequest(
            13, 1, 20, "deep", SHA_A, DIGEST, REGISTRY, "code", None
        )
        completed_old = VerifiedRunReceipt(
            clean_receipt(run_id=11, attempt=1), 11, 1, 20
        )
        pending = fold_requests((deep, replacement), (completed_old,))
        self.assertIsNotNone(pending)
        self.assertEqual(pending.origin_run_id, 13)

    def test_plan_dispatches_when_current_policy_identity_changed(self) -> None:
        receipt = SimpleNamespace(head_sha=SHA_A)
        review = SimpleNamespace(
            receipt=receipt, completion=None, context=SimpleNamespace()
        )
        historical = Mock()
        historical.collect.return_value = (review,)
        history = Mock()
        history.load.return_value = ()
        history.fold_history.return_value = None
        current = Mock()
        current.current_decision.return_value = GateDecision(False, "profile mismatch")
        client = SimpleNamespace(
            config=TrustedWorkflow("RetireGolden", "example", "main", reusable_sha=PIN),
            create_status=Mock(),
            paginated=Mock(return_value=[]),
        )
        with (
            patch.dict(os.environ, {"GITHUB_RUN_ID": "101"}, clear=True),
            patch.object(gate, "_live_pr", return_value=live_pr()),
            patch.object(gate, "_configs", return_value=("{}", "{}")),
            patch.object(
                gate,
                "_history_and_evidence",
                return_value=(history, historical, current),
            ),
            patch.object(gate, "_is_current_receipt_source", return_value=True),
            patch.object(gate, "_active_matching_run", return_value=False),
            patch.object(gate, "_refresh_identity_fingerprint", return_value="f" * 64),
            patch.object(gate, "_refresh_marker_present", return_value=False),
            patch.object(gate, "_dispatch") as dispatch,
        ):
            self.assertIsNone(
                gate._plan_one(
                    client, SHA_B, SHA_A, 7, auto_dispatch=True, mutate=False
                )
            )
        dispatch.assert_called_once_with(client, 7)

    def test_plan_never_succeeds_while_pending_obligation_unsettled(self) -> None:
        pending = PendingRequest(
            origin_run_id=11,
            head_sha=SHA_A,
            policy_digest=DIGEST,
            registry_digest=REGISTRY,
            profile="code",
            last_accepted_run_id=12,
            last_accepted_run_attempt=1,
        )
        receipt = SimpleNamespace(head_sha=SHA_A)
        review = SimpleNamespace(
            receipt=receipt, completion=None, context=SimpleNamespace()
        )
        historical = Mock()
        historical.collect.return_value = (review,)
        history = Mock()
        history.load.return_value = ()
        history.fold_history.return_value = pending
        current = Mock()
        current.current_decision.return_value = GateDecision(True, "ok")
        client = SimpleNamespace(
            config=TrustedWorkflow("RetireGolden", "example", "main", reusable_sha=PIN),
            create_status=Mock(),
        )
        with (
            patch.dict(os.environ, {"GITHUB_RUN_ID": "101"}, clear=True),
            patch.object(gate, "_live_pr", return_value=live_pr()),
            patch.object(gate, "_configs", return_value=("{}", "{}")),
            patch.object(
                gate,
                "_history_and_evidence",
                return_value=(history, historical, current),
            ),
            patch.object(gate, "_is_current_receipt_source", return_value=True),
        ):
            self.assertIsNone(
                gate._plan_one(client, SHA_B, SHA_A, 7, auto_dispatch=True, mutate=True)
            )
        client.create_status.assert_called_once()
        self.assertEqual(client.create_status.call_args.args[1], "pending")

    def test_plan_skips_auto_dispatch_when_pending(self) -> None:
        pending = PendingRequest(
            origin_run_id=11,
            head_sha=SHA_A,
            policy_digest=DIGEST,
            registry_digest=REGISTRY,
            profile="code",
            last_accepted_run_id=12,
            last_accepted_run_attempt=1,
        )
        receipt = SimpleNamespace(head_sha=SHA_A)
        review = SimpleNamespace(
            receipt=receipt, completion=None, context=SimpleNamespace()
        )
        historical = Mock()
        historical.collect.return_value = (review,)
        history = Mock()
        history.load.return_value = ()
        history.fold_history.return_value = pending
        current = Mock()
        current.current_decision.return_value = GateDecision(False, "profile mismatch")
        client = SimpleNamespace(
            config=TrustedWorkflow("RetireGolden", "example", "main", reusable_sha=PIN)
        )
        with (
            patch.object(gate, "_live_pr", return_value=live_pr()),
            patch.object(gate, "_configs", return_value=("{}", "{}")),
            patch.object(
                gate,
                "_history_and_evidence",
                return_value=(history, historical, current),
            ),
            patch.object(gate, "_is_current_receipt_source", return_value=True),
            patch.object(gate, "_dispatch_identity_refresh") as refresh,
        ):
            gate._plan_one(client, SHA_B, SHA_A, 7, auto_dispatch=True, mutate=False)
        refresh.assert_not_called()

    def test_registry_preserves_legacy_lane_and_job_budgets(self) -> None:
        from or_pr_review.harness import DEFAULT_LANE_TIMEOUT_SECONDS
        from or_pr_review.review_plan import parse_review_profiles

        raw = (Path(__file__).parents[1] / "review-profiles.json").read_text(
            encoding="utf-8"
        )
        parse_review_profiles(raw)
        for profile in json.loads(raw)["profiles"].values():
            for panel in profile.values():
                self.assertEqual(
                    panel["lane_timeout_seconds"], DEFAULT_LANE_TIMEOUT_SECONDS
                )
                self.assertEqual(panel["job_budget_seconds"], 1320)

    def test_active_search_cap_counts_completed_race_snapshots(self) -> None:
        completed = {"id": 7, "run_attempt": 1, "status": "completed"}
        client = SimpleNamespace(
            config=TrustedWorkflow("RetireGolden", "example", "main", reusable_sha=PIN),
            paginated=Mock(
                side_effect=_active_run_pages({"queued": [completed] * 1000})
            ),
        )
        with self.assertRaisesRegex(
            ProfileGitHubError, "search reached its result bound"
        ):
            gate._active_workflow_runs(client)

    def test_active_matching_run_skips_unrelated_untrusted_runs(self) -> None:
        unrelated = {
            "id": 99,
            "run_attempt": 1,
            "status": "in_progress",
            "display_title": "OpenRouter PR #99: auto",
        }
        client = SimpleNamespace(
            config=TrustedWorkflow("RetireGolden", "example", "main", reusable_sha=PIN),
            paginated=Mock(side_effect=_active_run_pages({"in_progress": [unrelated]})),
        )

        def verifier(_run):
            raise ProfileGitHubError("untrusted")

        self.assertFalse(gate._active_matching_run(client, verifier, 7, SHA_A))

    def test_active_matching_untrusted_run_defers_additional_spend(self) -> None:
        matching = {
            "id": 7,
            "run_attempt": 1,
            "status": "queued",
            "display_title": "OpenRouter PR #7: auto",
        }
        client = SimpleNamespace(
            config=TrustedWorkflow("RetireGolden", "example", "main", reusable_sha=PIN),
            paginated=Mock(side_effect=_active_run_pages({"queued": [matching]})),
        )

        def verifier(_run):
            raise ProfileGitHubError("untrusted")

        self.assertTrue(gate._active_matching_run(client, verifier, 7, SHA_A))

    def test_active_matching_run_queries_only_active_statuses(self) -> None:
        calls: list[str] = []

        def paginated(endpoint: str, list_key: str, max_items: int):
            calls.append(endpoint)
            return []

        client = SimpleNamespace(
            config=TrustedWorkflow("RetireGolden", "example", "main", reusable_sha=PIN),
            paginated=paginated,
        )
        gate._active_matching_run(client, lambda _run: verified_run(), 7, SHA_A)
        self.assertEqual(len(calls), len(gate.ACTIVE_WORKFLOW_RUN_STATUSES))
        for index, status in enumerate(gate.ACTIVE_WORKFLOW_RUN_STATUSES):
            self.assertIn(f"status={status}", calls[index])
        self.assertTrue(all("status=" in call for call in calls))

    def test_active_matching_run_blocks_each_active_status_despite_completed_history(
        self,
    ) -> None:
        for status in gate.ACTIVE_WORKFLOW_RUN_STATUSES:
            matching = {
                "id": 42,
                "run_attempt": 1,
                "status": status,
                "display_title": "OpenRouter PR #7: auto",
            }
            client = SimpleNamespace(
                config=TrustedWorkflow(
                    "RetireGolden", "example", "main", reusable_sha=PIN
                ),
                paginated=Mock(side_effect=_active_run_pages({status: [matching]})),
            )

            def verifier(_run):
                raise ProfileGitHubError("untrusted")

            self.assertTrue(
                gate._active_matching_run(client, verifier, 7, SHA_A),
                status,
            )

    def test_active_matching_run_ignores_completed_snapshot_race(self) -> None:
        completed = {
            "id": 7,
            "run_attempt": 1,
            "status": "completed",
            "display_title": "OpenRouter PR #7: auto",
        }
        client = SimpleNamespace(
            config=TrustedWorkflow("RetireGolden", "example", "main", reusable_sha=PIN),
            paginated=Mock(side_effect=_active_run_pages({"in_progress": [completed]})),
        )

        def verifier(_run):
            raise ProfileGitHubError("untrusted")

        self.assertFalse(gate._active_matching_run(client, verifier, 7, SHA_A))

    def test_active_workflow_runs_queries_each_status_with_full_per_query_cap(
        self,
    ) -> None:
        caps: list[int] = []

        def paginated(endpoint: str, list_key: str, max_items: int):
            caps.append(max_items)
            return []

        client = SimpleNamespace(
            config=TrustedWorkflow("RetireGolden", "example", "main", reusable_sha=PIN),
            paginated=paginated,
        )
        gate._active_workflow_runs(client)
        self.assertEqual(len(caps), len(gate.ACTIVE_WORKFLOW_RUN_STATUSES))
        self.assertTrue(all(cap == gate.MAX_ACTIVE_WORKFLOW_RUNS for cap in caps))

    def test_active_workflow_runs_fails_closed_when_cap_reached_before_later_status(
        self,
    ) -> None:
        unrelated = [
            {
                "id": index,
                "run_attempt": 1,
                "status": "requested",
                "display_title": f"OpenRouter PR #{index}: auto",
            }
            for index in range(1, gate.MAX_ACTIVE_WORKFLOW_RUNS + 1)
        ]
        target = {
            "id": 9999,
            "run_attempt": 1,
            "status": "in_progress",
            "display_title": "OpenRouter PR #7: auto",
        }

        def paginated(endpoint: str, list_key: str, max_items: int):
            if "status=requested" in endpoint:
                return unrelated
            if "status=in_progress" in endpoint:
                return [target]
            return []

        client = SimpleNamespace(
            config=TrustedWorkflow("RetireGolden", "example", "main", reusable_sha=PIN),
            paginated=paginated,
        )
        with self.assertRaisesRegex(
            ProfileGitHubError, "active workflow search reached its result bound"
        ):
            gate._active_workflow_runs(client)

    def test_active_workflow_runs_dedupes_same_run_across_statuses(self) -> None:
        matching = {
            "id": 7,
            "run_attempt": 2,
            "status": "queued",
            "display_title": "OpenRouter PR #7: auto",
        }

        def paginated(endpoint: str, list_key: str, max_items: int):
            if "status=queued" in endpoint:
                return [matching]
            if "status=in_progress" in endpoint:
                return [{**matching, "status": "in_progress"}]
            return []

        client = SimpleNamespace(
            config=TrustedWorkflow("RetireGolden", "example", "main", reusable_sha=PIN),
            paginated=paginated,
        )
        collected = gate._active_workflow_runs(client)
        self.assertEqual(len(collected), 1)
        self.assertEqual(collected[0]["id"], 7)
        self.assertEqual(collected[0]["run_attempt"], 2)

    def test_active_workflow_runs_fails_closed_on_malformed_matching_record(
        self,
    ) -> None:
        malformed = {
            "id": "not-an-int",
            "run_attempt": 1,
            "status": "queued",
            "display_title": "OpenRouter PR #7: auto",
        }
        client = SimpleNamespace(
            config=TrustedWorkflow("RetireGolden", "example", "main", reusable_sha=PIN),
            paginated=Mock(side_effect=_active_run_pages({"queued": [malformed]})),
        )
        with self.assertRaisesRegex(
            ProfileGitHubError, "workflow run identity is invalid"
        ):
            gate._active_workflow_runs(client)

    def test_active_matching_run_dedupes_same_run_across_statuses(self) -> None:
        matching = {
            "id": 7,
            "run_attempt": 2,
            "status": "queued",
            "display_title": "OpenRouter PR #7: auto",
        }
        calls: list[str] = []

        def paginated(endpoint: str, list_key: str, max_items: int):
            calls.append(endpoint)
            if "status=queued" in endpoint:
                return [matching]
            if "status=in_progress" in endpoint:
                return [{**matching, "status": "in_progress"}]
            return []

        client = SimpleNamespace(
            config=TrustedWorkflow("RetireGolden", "example", "main", reusable_sha=PIN),
            paginated=paginated,
        )

        def verifier(_run):
            raise ProfileGitHubError("untrusted")

        self.assertTrue(gate._active_matching_run(client, verifier, 7, SHA_A))
        self.assertEqual(len(calls), len(gate.ACTIVE_WORKFLOW_RUN_STATUSES))

    def test_active_matching_run_old_active_rerun_still_blocks(self) -> None:
        old = {
            "id": 3,
            "run_attempt": 1,
            "status": "in_progress",
            "display_title": "OpenRouter PR #7: auto",
            "created_at": "2020-01-01T00:00:00Z",
        }
        client = SimpleNamespace(
            config=TrustedWorkflow("RetireGolden", "example", "main", reusable_sha=PIN),
            paginated=Mock(side_effect=_active_run_pages({"in_progress": [old]})),
        )

        def verifier(_run):
            raise ProfileGitHubError("untrusted")

        self.assertTrue(gate._active_matching_run(client, verifier, 7, SHA_A))

    def test_active_matching_run_ignores_unbounded_completed_history(self) -> None:
        completed_history = [
            {
                "id": index,
                "run_attempt": 1,
                "status": "completed",
                "display_title": "OpenRouter PR #7: auto",
            }
            for index in range(5000)
        ]

        def paginated(endpoint: str, list_key: str, max_items: int):
            if "status=" not in endpoint:
                return completed_history
            return []

        client = SimpleNamespace(
            config=TrustedWorkflow("RetireGolden", "example", "main", reusable_sha=PIN),
            paginated=paginated,
        )

        def verifier(_run):
            raise ProfileGitHubError("untrusted")

        self.assertFalse(gate._active_matching_run(client, verifier, 7, SHA_A))

        client = SimpleNamespace(
            config=TrustedWorkflow("RetireGolden", "example", "main", reusable_sha=PIN),
            paginated=Mock(
                side_effect=ProfileGitHubError("pagination exceeds its item cap")
            ),
        )
        with self.assertRaises(ProfileGitHubError):
            gate._active_matching_run(client, lambda _run: verified_run(), 7, SHA_A)

    def test_refresh_dispatch_is_once_per_identity(self) -> None:
        review = SimpleNamespace(context=SimpleNamespace())
        client = SimpleNamespace(
            config=TrustedWorkflow("RetireGolden", "example", "main", reusable_sha=PIN),
            create_status=Mock(),
        )
        marker_state = {"present": False}

        def marker_present(*_args, **_kwargs):
            return marker_state["present"]

        def create_status(*_args, **_kwargs):
            marker_state["present"] = True

        client.create_status = Mock(side_effect=create_status)
        with (
            patch.dict(os.environ, {"GITHUB_RUN_ID": "101"}, clear=True),
            patch.object(gate, "_active_matching_run", return_value=False),
            patch.object(gate, "_refresh_identity_fingerprint", return_value="f" * 64),
            patch.object(gate, "_refresh_marker_present", side_effect=marker_present),
            patch.object(gate, "_dispatch") as dispatch,
        ):
            gate._dispatch_identity_refresh(
                client, 7, SHA_A, SHA_B, "{}", "{}", review, lambda _run: verified_run()
            )
            gate._dispatch_identity_refresh(
                client, 7, SHA_A, SHA_B, "{}", "{}", review, lambda _run: verified_run()
            )
        dispatch.assert_called_once()

    def test_refresh_dispatch_allows_distinct_policy_identity(self) -> None:
        review = SimpleNamespace(context=SimpleNamespace())
        client = SimpleNamespace(
            config=TrustedWorkflow("RetireGolden", "example", "main", reusable_sha=PIN),
            create_status=Mock(),
        )
        fingerprints = iter(["f" * 64, "e" * 64])

        with (
            patch.dict(os.environ, {"GITHUB_RUN_ID": "101"}, clear=True),
            patch.object(gate, "_active_matching_run", return_value=False),
            patch.object(
                gate,
                "_refresh_identity_fingerprint",
                side_effect=lambda *_args, **_kwargs: next(fingerprints),
            ),
            patch.object(gate, "_refresh_marker_present", return_value=False),
            patch.object(gate, "_dispatch") as dispatch,
        ):
            gate._dispatch_identity_refresh(
                client, 7, SHA_A, SHA_B, "{}", "{}", review, lambda _run: verified_run()
            )
            gate._dispatch_identity_refresh(
                client,
                7,
                SHA_A,
                SHA_B,
                '{"v":2}',
                "{}",
                review,
                lambda _run: verified_run(),
            )
        self.assertEqual(dispatch.call_count, 2)

    def test_refresh_marker_suppresses_repeat_without_second_dispatch(self) -> None:
        review = SimpleNamespace(context=SimpleNamespace())
        client = SimpleNamespace(
            config=TrustedWorkflow("RetireGolden", "example", "main", reusable_sha=PIN),
            create_status=Mock(),
        )
        with (
            patch.dict(os.environ, {"GITHUB_RUN_ID": "101"}, clear=True),
            patch.object(gate, "_active_matching_run", return_value=False),
            patch.object(gate, "_refresh_identity_fingerprint", return_value="f" * 64),
            patch.object(gate, "_refresh_marker_present", return_value=True),
            patch.object(gate, "_dispatch") as dispatch,
        ):
            gate._dispatch_identity_refresh(
                client, 7, SHA_A, SHA_B, "{}", "{}", review, lambda _run: verified_run()
            )
        dispatch.assert_not_called()
        client.create_status.assert_not_called()

    def test_plan_refuses_newer_stale_source_without_falling_back_to_old_clean(
        self,
    ) -> None:
        old = SimpleNamespace(receipt=SimpleNamespace(head_sha=SHA_A), completion=None)
        stale = SimpleNamespace(
            receipt=SimpleNamespace(head_sha=SHA_A), completion=None
        )
        historical = Mock()
        historical.collect.return_value = (old, stale)
        history = Mock()
        history.load.return_value = ()
        history.fold_history.return_value = None
        current = Mock()
        client = SimpleNamespace(
            config=TrustedWorkflow("RetireGolden", "example", "main", reusable_sha=PIN),
            create_status=Mock(),
        )
        with (
            patch.dict(os.environ, {"GITHUB_RUN_ID": "101"}, clear=True),
            patch.object(gate, "_live_pr", return_value=live_pr()),
            patch.object(gate, "_configs", return_value=("{}", "{}")),
            patch.object(
                gate,
                "_history_and_evidence",
                return_value=(history, historical, current),
            ),
            patch.object(gate, "_is_current_receipt_source", return_value=False),
        ):
            self.assertIsNone(
                gate._plan_one(client, SHA_B, SHA_A, 7, auto_dispatch=True, mutate=True)
            )
        current.current_decision.assert_not_called()
        client.create_status.assert_called_once()

    def test_source_pr_accepts_dispatch_run_on_main_while_pr_head_differs(self) -> None:
        run = {"id": 99}
        verified = VerifiedRun(
            "RetireGolden",
            "example",
            99,
            1,
            SHA_B,
            "workflow_dispatch",
            "main",
            9,
            {"display_title": "OpenRouter PR #7: deep"},
            (),
        )
        client = SimpleNamespace(
            config=TrustedWorkflow("RetireGolden", "example", "main", reusable_sha=PIN),
            _api=Mock(return_value=run),
        )
        with patch.dict(os.environ, {"SOURCE_RUN_ID": "99"}, clear=True):
            self.assertEqual(gate._source_pr(client, lambda value: verified), 7)

    def test_confirm_and_publish_require_current_exact_successful_proof(self) -> None:
        digest = "f" * 64
        matrix = [
            {
                "pr_number": 7,
                "head_sha": SHA_A,
                "base_sha": SHA_B,
                "receipt_digest": digest,
            }
        ]
        with (
            patch.dict(
                os.environ,
                {"PR_NUMBER": "7", "EXPECTED_RECEIPT_DIGEST": digest},
                clear=True,
            ),
            patch.object(gate, "plan", return_value=matrix) as planned,
        ):
            gate.confirm()
        planned.assert_called_once_with(one_pr=7, auto_dispatch=False, mutate=False)

        bare = SimpleNamespace(
            config=TrustedWorkflow("RetireGolden", "example", "main", reusable_sha=PIN),
            create_status=Mock(),
            paginated=Mock(),
        )
        run = {"id": 200, "run_attempt": 1}
        success_job = {
            "name": f"complete / profile #7 {digest}",
            "status": "completed",
            "conclusion": "success",
        }
        env = {
            "GITHUB_SHA": SHA_B,
            "PROOF_MATRIX_JSON": json.dumps(matrix),
            "GITHUB_RUN_ID": "200",
            "GITHUB_RUN_ATTEMPT": "1",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(
                gate, "_current_trust", return_value=(bare, SHA_B, SHA_A, b"")
            ),
            patch.object(gate, "_current_run", return_value=run),
            patch.object(bare, "paginated", return_value=[success_job]),
            patch.object(gate, "_plan_one", return_value=matrix[0]),
            patch.object(gate, "_live_main", return_value=("main", SHA_B)),
            patch.object(gate, "_live_pr", return_value=live_pr()),
        ):
            gate.publish()
        self.assertEqual(bare.create_status.call_args.args[1], "success")

        for fresh, current_main in ((None, SHA_B), (matrix[0], "8" * 40)):
            bare.create_status.reset_mock()
            with (
                patch.dict(os.environ, env, clear=True),
                patch.object(
                    gate, "_current_trust", return_value=(bare, SHA_B, SHA_A, b"")
                ),
                patch.object(gate, "_current_run", return_value=run),
                patch.object(bare, "paginated", return_value=[success_job]),
                patch.object(gate, "_plan_one", return_value=fresh),
                patch.object(gate, "_live_main", return_value=("main", current_main)),
                patch.object(gate, "_live_pr", return_value=live_pr()),
            ):
                with self.assertRaises(gate.GateError):
                    gate.publish()
            self.assertEqual(bare.create_status.call_args.args[1], "failure")

        bare.create_status.reset_mock()
        stale_pr = live_pr(head="9" * 40)
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(
                gate, "_current_trust", return_value=(bare, SHA_B, SHA_A, b"")
            ),
            patch.object(gate, "_current_run", return_value=run),
            patch.object(bare, "paginated", return_value=[]),
            patch.object(gate, "_live_pr", return_value=stale_pr),
        ):
            with self.assertRaises(gate.GateError):
                gate.publish()
        self.assertEqual(bare.create_status.call_args.args[0], "9" * 40)
        self.assertEqual(bare.create_status.call_args.args[1], "failure")

    def test_publish_attempts_all_matrix_items_before_aggregate_failure(self) -> None:
        digest_a = "a" * 64
        digest_b = "b" * 64
        matrix = [
            {
                "pr_number": 3,
                "head_sha": SHA_A,
                "base_sha": SHA_B,
                "receipt_digest": digest_a,
            },
            {
                "pr_number": 7,
                "head_sha": SHA_A,
                "base_sha": SHA_B,
                "receipt_digest": digest_b,
            },
        ]
        bare = SimpleNamespace(
            config=TrustedWorkflow("RetireGolden", "example", "main", reusable_sha=PIN),
            create_status=Mock(),
            paginated=Mock(),
        )
        run = {"id": 200, "run_attempt": 1}
        success_job = {
            "name": f"complete / profile #7 {digest_b}",
            "status": "completed",
            "conclusion": "success",
        }
        env = {
            "GITHUB_SHA": SHA_B,
            "PROOF_MATRIX_JSON": json.dumps(matrix),
            "GITHUB_RUN_ID": "200",
            "GITHUB_RUN_ATTEMPT": "1",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(
                gate, "_current_trust", return_value=(bare, SHA_B, SHA_A, b"")
            ),
            patch.object(gate, "_current_run", return_value=run),
            patch.object(bare, "paginated", return_value=[success_job]),
            patch.object(gate, "_live_pr", return_value=live_pr()),
            patch.object(gate, "_plan_one", return_value=matrix[1]),
            patch.object(gate, "_live_main", return_value=("main", SHA_B)),
        ):
            with self.assertRaises(gate.GateError) as raised:
                gate.publish()
        self.assertIn("PR #3", str(raised.exception))
        self.assertNotIn("PR #7:", str(raised.exception))
        self.assertEqual(bare.create_status.call_count, 2)
        self.assertEqual(bare.create_status.call_args_list[0].args[1], "failure")
        self.assertEqual(bare.create_status.call_args_list[1].args[1], "success")


def verified_run():
    return VerifiedRun(
        "RetireGolden", "example", 1, 1, SHA_A, "workflow_dispatch", "main", 9, {}, ()
    )


def _active_run_pages(by_status: dict[str, list[dict]]):
    """Return only status-filtered workflow-run pages for active-run tests."""

    def paginated(endpoint: str, list_key: str, max_items: int):
        if "status=" not in endpoint:
            raise AssertionError(f"unfiltered workflow run request: {endpoint}")
        status = endpoint.rsplit("status=", 1)[-1]
        if status not in gate.ACTIVE_WORKFLOW_RUN_STATUSES:
            raise AssertionError(f"unexpected workflow run status filter: {status}")
        return list(by_status.get(status, ()))

    return paginated


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
