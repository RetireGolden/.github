"""Explicit completion wakes must wait for terminal, trusted source runs."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from scripts import profile_gate as gate


class ProfileWakeTests(unittest.TestCase):
    def client(self, states):
        return SimpleNamespace(config=SimpleNamespace(owner="RetireGolden", name="example"),
                               _api=Mock(side_effect=[{"status": value} for value in states]))

    def test_waits_for_source_before_planning(self):
        client = self.client(["in_progress", "completed"])
        verify = Mock()
        with patch.dict(os.environ, {"SOURCE_RUN_ID": "42", "GITHUB_RUN_ID": "43"}), patch.object(gate.time, "sleep") as sleep:
            gate._wait_for_source_run(client, verify)
        self.assertEqual(verify.call_count, 2)
        sleep.assert_called_once()

    def test_untrusted_source_fails_before_wait_or_status_mutation(self):
        client = self.client(["in_progress"])
        with patch.dict(os.environ, {"SOURCE_RUN_ID": "42", "GITHUB_RUN_ID": "43"}), patch.object(gate.time, "sleep") as sleep:
            with self.assertRaisesRegex(gate.GateError, "untrusted"):
                gate._wait_for_source_run(client, Mock(side_effect=gate.GateError("untrusted")))
        sleep.assert_not_called()

    def test_terminal_source_does_not_sleep(self):
        with patch.dict(os.environ, {"SOURCE_RUN_ID": "42", "GITHUB_RUN_ID": "43"}), patch.object(gate.time, "sleep") as sleep:
            gate._wait_for_source_run(self.client(["completed"]), Mock())
        sleep.assert_not_called()

    def test_wait_is_bounded(self):
        with patch.dict(os.environ, {"SOURCE_RUN_ID": "42", "GITHUB_RUN_ID": "43"}), patch.object(gate.time, "monotonic", side_effect=[0, 90]), patch.object(gate.time, "sleep") as sleep:
            with self.assertRaisesRegex(gate.GateError, "wake deadline"):
                gate._wait_for_source_run(self.client(["in_progress"]), Mock())
        sleep.assert_not_called()

    def test_self_dependency_and_unknown_states_are_rejected(self):
        with patch.dict(os.environ, {"SOURCE_RUN_ID": "42", "GITHUB_RUN_ID": "42"}):
            with self.assertRaisesRegex(gate.GateError, "itself"):
                gate._wait_for_source_run(self.client([]), Mock())
        with patch.dict(os.environ, {"SOURCE_RUN_ID": "42", "GITHUB_RUN_ID": "43"}):
            with self.assertRaisesRegex(gate.GateError, "invalid status"):
                gate._wait_for_source_run(self.client(["mystery"]), Mock())
