"""Exercise the actual review-selector jq from the reusable workflow.

Run: uv run --with jq python -m unittest discover -s tests
"""

import re
import unittest
from pathlib import Path

import jq


WORKFLOW = (
    Path(__file__).parents[1] / ".github/workflows/openrouter-code-review.yml"
).read_text()
SELECTOR = re.search(
    r"--jq '(\.\[\] \| select\(\s*\.user.login.*?\| @base64)'",
    WORKFLOW.split("find_review() {", 1)[1],
    re.S,
).group(1)


class ReviewSelectorTests(unittest.TestCase):
    def selected(
        self,
        mode="verify",
        scope="full-pr",
        ledger=True,
        author="github-actions[bot]",
        verdict="clean",
    ):
        body = (
            "## OpenRouter pull-request review\n"
            f"**Mode:** `{mode}`\n**Scope:** `{scope}` (full-pr)\n"
            f"**Verdict:** `{verdict}`\n**Commit:** `{'a' * 40}`\n"
        )
        if ledger:
            body += "<!-- openrouter-review-ledger:v1:fixture -->"
        return (
            jq.compile(SELECTOR)
            .input_value(
                [
                    {
                        "user": {"login": author},
                        "body": body,
                        "html_url": "https://example.test/review",
                    }
                ]
            )
            .all()
        )

    def test_full_pr_verify_is_completion_evidence(self):
        self.assertTrue(self.selected())

    def test_initial_completion_still_works(self):
        for verdict in ("clean", "issues", "partial"):
            self.assertTrue(
                self.selected(
                    mode="initial", verdict=verdict, ledger=verdict != "partial"
                )
            )

    def test_incremental_verify_cannot_replace_full_pr_evidence(self):
        self.assertFalse(self.selected(scope="latest-commit"))

    def test_verify_requires_ledger_and_bot_identity(self):
        self.assertFalse(self.selected(ledger=False))
        self.assertFalse(self.selected(author="contributor"))
        self.assertFalse(self.selected(verdict="error"))

    def test_active_run_and_head_checks_remain(self):
        self.assertIn('active="$(find_review "$REVIEW_URL" exact)"', WORKFLOW)
        self.assertIn('[ "$reviewed_sha" = "$HEAD_SHA" ] || continue', WORKFLOW)
        self.assertIn('[ "$ACTION_OUTCOME" = "success" ]', WORKFLOW)
        self.assertIn('if [ "$EVENT_ACTION" != "synchronize" ]; then', WORKFLOW)
        self.assertIn("gh api --paginate", WORKFLOW)

    def test_reset_is_explicit_and_manual_only(self):
        self.assertEqual(WORKFLOW.count("reset_review:\n"), 2)
        self.assertIn(
            "github.event_name == 'workflow_dispatch' && inputs.reset_review", WORKFLOW
        )


if __name__ == "__main__":
    unittest.main()
