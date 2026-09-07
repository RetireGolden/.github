"""Execute the failure-message shell from both reusable-workflow jobs."""

import os
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path

WORKFLOW = (
    Path(__file__).parents[1] / ".github/workflows/openrouter-code-review.yml"
).read_text(encoding="utf-8")
BLOCKS = WORKFLOW.split("      - name: Repair the status comment on failure\n")[1:]
GIT_BASH = Path("C:/Program Files/Git/bin/bash.exe")
BASH = str(GIT_BASH) if GIT_BASH.exists() else shutil.which("bash")


@unittest.skipUnless(BASH, "bash required to exercise the workflow shell")
class FailureStatusTests(unittest.TestCase):
    def render(self, block, review_url):
        script = textwrap.dedent(
            block.split("        run: |\n", 1)[1].split('          existing="', 1)[0]
        )
        return subprocess.run(
            [BASH, "-c", script + '\nprintf "%s" "$BODY"'],
            env={
                **os.environ,
                "PR_NUMBER": "651",
                "REVIEW_URL": review_url,
                "RUN_URL": "https://example.test/run",
            },
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def test_posted_error_review_is_linked(self):
        self.assertEqual(len(BLOCKS), 2)
        for block, step in zip(BLOCKS, ("openrouter_first", "openrouter_followup")):
            self.assertIn(f"steps.{step}.outputs.review_url", block)
            body = self.render(block, "https://example.test/review")
            self.assertIn("A review was posted", body)
            self.assertIn("https://example.test/review", body)
            self.assertIn("verdict and diagnostics", body)
            self.assertNotIn("no review was posted", body)

    def test_missing_output_does_not_assert_publication_failed(self):
        for block in BLOCKS:
            body = self.render(block, "")
            self.assertIn("publication could not be confirmed", body)
            self.assertNotIn("A review was posted", body)
            self.assertNotIn("no review was posted", body)
            self.assertIn("https://example.test/run", body)
