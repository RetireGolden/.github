"""Execute the trusted inline configuration and check both caller paths."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
WORKFLOW = yaml.load(
    (ROOT / '.github/workflows/openrouter-code-review.yml').read_text(encoding='utf-8'),
    Loader=yaml.BaseLoader,
)
JOBS = WORKFLOW['jobs']
CONFIG = JOBS['review-config']
SCRIPT = CONFIG['steps'][0]['run']


class ReviewConfigurationTests(unittest.TestCase):
    def run_config(self, repository, mode='off'):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'output'
            proc = subprocess.run(
                [sys.executable, '-c', SCRIPT],
                env={**os.environ, 'GITHUB_REPOSITORY': repository,
                     'GITHUB_OUTPUT': str(output), 'REVIEW_POLICY': mode},
                capture_output=True, text=True,
            )
            return proc, output.read_text(encoding='utf-8') if output.exists() else ''

    def test_standing_rosters_and_domain_fallbacks_are_preserved(self):
        cases = [
            ('RetireGolden', 'x-ai/grok-4.6,z-ai/glm-5.3-flash', 'high', 'citable source'),
            ('RetireGolden-Pro', 'x-ai/grok-4.6,z-ai/glm-5.3-flash', 'high', 'release integrity'),
            ('RetireGolden-MCP', 'x-ai/grok-4.6,z-ai/glm-5.3-flash', 'high', 'tool/schema'),
            ('RetireGolden-Docs', 'z-ai/glm-5.3-flash', 'medium', 'documentation accuracy'),
            ('retiregolden.org', 'x-ai/grok-4.6,z-ai/glm-5.3-flash', 'high', 'public contracts'),
            ('RetireBench', 'x-ai/grok-4.6,z-ai/glm-5.3-flash', 'medium', 'Major issues'),
            ('unlisted', 'x-ai/grok-4.6', 'low', 'obvious defects'),
        ]
        for name, models, effort, guidance in cases:
            with self.subTest(repository=name):
                proc, output = self.run_config('RetireGolden/' + name)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn('models=' + models + '\n', output)
                self.assertIn('effort=' + effort + '\n', output)
                self.assertIn(guidance, output)
                self.assertEqual(output.count('Pin-bump PRs:'), 1)
                self.assertIn('model_routes={}\n', output)
                self.assertNotIn('astra', output)

    def test_guidance_opt_in_does_not_change_roster_or_lose_fallback(self):
        for repo in ('RetireGolden', 'RetireGolden-Pro', 'RetireGolden-MCP', 'RetireGolden-Docs'):
            self.assertEqual(self.run_config('RetireGolden/' + repo)[1],
                             self.run_config('RetireGolden/' + repo, 'base')[1])
        proc, output = self.run_config('RetireGolden/RetireGolden', 'typo')
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(output, '')
        self.assertIn('review_policy must be off or base', proc.stderr)

    def test_configuration_has_no_checkout_secret_or_permissions(self):
        self.assertEqual(CONFIG['permissions'], {})
        self.assertEqual(len(CONFIG['steps']), 1)
        self.assertNotIn('uses', CONFIG['steps'][0])
        self.assertNotIn('secrets.', str(CONFIG))
        for repo in ('.github', 'cla-signatures', 'RetireBench-private'):
            self.assertIn("github.repository != 'RetireGolden/" + repo + "'", CONFIG['if'])

    def test_both_passes_consume_same_configuration_and_trusted_action(self):
        for job_id, scope, turns in (
            ('openrouter-first-pass', 'full-pr', '50'),
            ('openrouter-follow-up', 'latest-commit', '30'),
        ):
            job = JOBS[job_id]
            self.assertEqual(job['needs'], 'review-config')
            actions = [step for step in job['steps']
                       if step.get('uses', '').startswith('FlyOverCoderKY/')]
            self.assertEqual(len(actions), 1)
            action = actions[0]
            self.assertEqual(action['uses'], 'FlyOverCoderKY/openrouter-pr-review-action@'
                             '93cc91130605bc17cb583c5a5e899591773e048c')
            for field in ('models', 'model_routes', 'custom_instructions'):
                self.assertEqual(action['with'][field], '${{ needs.review-config.outputs.' + field + ' }}')
            self.assertEqual(action['with']['review_policy'], "${{ inputs.review_policy || 'off' }}")
            self.assertEqual(action['with']['review_scope'], scope)
            self.assertEqual(action['with']['max_tool_turns'], turns)
            self.assertEqual(action['timeout-minutes'], '25')
        self.assertEqual(JOBS['openrouter-first-pass']['concurrency']['cancel-in-progress'], 'false')
        self.assertEqual(JOBS['openrouter-follow-up']['concurrency']['cancel-in-progress'], 'true')

    def test_policy_is_opt_in_and_completion_gate_remains_separate(self):
        for trigger in ('workflow_call', 'workflow_dispatch'):
            self.assertEqual(WORKFLOW['on'][trigger]['inputs']['review_policy']['default'], 'off')
        gate = JOBS['openrouter-first-pass-gate']
        self.assertNotIn('review-config', str(gate['needs']))
        self.assertIn('always()', gate['if'])


if __name__ == '__main__':
    unittest.main()
