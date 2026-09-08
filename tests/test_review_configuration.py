"""Execute the trusted inline configuration and check both caller paths."""

import json
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
ACTION_PIN = '212775ffea22e806cddcb706c73a3df26fbcb6d0'
SCRIPT = next(step['run'] for step in CONFIG['steps'] if step.get('id') == 'config')
REGISTRY = json.dumps(
    json.loads((ROOT / 'review-profiles.json').read_text(encoding='utf-8')),
    separators=(',', ':'),
)
ROUTES = json.dumps(
    json.loads((ROOT / 'review-model-routes.json').read_text(encoding='utf-8')),
    separators=(',', ':'),
)


class ReviewConfigurationTests(unittest.TestCase):
    def run_config(self, repository, mode='off', *, follow_up=False, profiles=False):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'output'
            env = {
                **os.environ,
                'GITHUB_REPOSITORY': repository,
                'GITHUB_OUTPUT': str(output),
                'REVIEW_POLICY': mode,
                'FOLLOW_UP': 'true' if follow_up else 'false',
                'PROFILES_ENABLED': 'true' if profiles else 'false',
                'REVIEW_LEVEL': 'auto',
                'PROFILE_ROOT': str(ROOT),
            }
            proc = subprocess.run(
                [sys.executable, '-c', SCRIPT],
                env=env,
                capture_output=True,
                text=True,
            )
            return proc, output.read_text(encoding='utf-8') if output.exists() else ''

    def test_standing_rosters_and_follow_up_effort_are_preserved(self):
        cases = [
            ('RetireGolden', 'x-ai/grok-4.6,z-ai/glm-5.3-flash', 'high'),
            ('RetireGolden-Pro', 'x-ai/grok-4.6,z-ai/glm-5.3-flash', 'high'),
            ('RetireGolden-MCP', 'x-ai/grok-4.6,z-ai/glm-5.3-flash', 'high'),
            ('RetireGolden-Docs', 'z-ai/glm-5.3-flash', 'medium'),
            ('retiregolden.org', 'x-ai/grok-4.6,z-ai/glm-5.3-flash', 'high'),
            ('RetireBench', 'x-ai/grok-4.6,z-ai/glm-5.3-flash', 'medium'),
            ('unlisted', 'x-ai/grok-4.6', 'low'),
        ]
        for name, models, effort in cases:
            with self.subTest(repository=name):
                proc, output = self.run_config('RetireGolden/' + name)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn('models=' + models + '\n', output)
                self.assertIn('effort=' + effort + '\n', output)
                self.assertEqual(output.count('Pin-bump PRs:'), 1)
                self.assertIn('model_routes={}\n', output)
                self.assertNotIn('astra', output.lower())
        proc, output = self.run_config('RetireGolden/RetireGolden', follow_up=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('effort=low\n', output)

    def test_adopted_repos_no_longer_carry_inline_domain_guidance(self):
        for repo in ('RetireGolden', 'RetireGolden-Pro', 'RetireGolden-MCP', 'RetireGolden-Docs'):
            proc, output = self.run_config('RetireGolden/' + repo)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn('citable source', output)
            self.assertNotIn('release integrity', output)
            self.assertNotIn('tool/schema', output)
            self.assertNotIn('documentation accuracy', output)
        proc, output = self.run_config('RetireGolden/retiregolden.org')
        self.assertIn('public contracts', output)
        proc, output = self.run_config('RetireGolden/RetireBench')
        self.assertIn('Major issues', output)

    def test_profiles_enabled_with_base_reads_trusted_registry_and_blanks_legacy_inputs(self):
        proc, output = self.run_config('RetireGolden/RetireGolden', 'base', profiles=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('models=\n', output)
        self.assertIn('effort=\n', output)
        self.assertIn('review_profiles=' + REGISTRY + '\n', output)
        self.assertIn('model_routes=' + ROUTES + '\n', output)

    def test_profiles_require_base_policy_and_auto_level(self):
        proc, _ = self.run_config('RetireGolden/RetireGolden', profiles=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('profiles require review_policy=base', proc.stderr)

    def test_guidance_opt_in_does_not_change_roster_for_adopted_repos(self):
        for repo in ('RetireGolden', 'RetireGolden-Pro', 'RetireGolden-MCP', 'RetireGolden-Docs'):
            self.assertEqual(
                self.run_config('RetireGolden/' + repo)[1],
                self.run_config('RetireGolden/' + repo, 'base')[1],
            )
        proc, output = self.run_config('RetireGolden/RetireGolden', 'typo')
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(output, '')
        self.assertIn('review_policy must be off or base', proc.stderr)

    def test_configuration_uses_least_permissions_and_self_sha_bootstrap(self):
        self.assertEqual(CONFIG['permissions'], {'actions': 'read', 'contents': 'read'})
        source = next(step for step in CONFIG['steps'] if step.get('id') == 'source')
        self.assertIn('referenced_workflows', source['run'])
        self.assertIn('openrouter-code-review.yml@', source['run'])
        self.assertEqual(source['if'], 'inputs.review_profiles_enabled')
        self.assertNotIn('secrets.', str(CONFIG))
        self.assertNotIn('github.event.pull_request', SCRIPT)
        for repo in ('.github', 'cla-signatures', 'RetireBench-private'):
            self.assertIn("github.repository != 'RetireGolden/" + repo + "'", CONFIG['if'])

    def test_both_passes_consume_same_configuration_and_trusted_action(self):
        for job_id, review_name, scope, turns in (
            ('openrouter-first-pass', 'Run OpenRouter first-pass review', 'full-pr', '50'),
            (
                'openrouter-follow-up',
                'Run OpenRouter follow-up review',
                'latest-commit',
                "${{ inputs.review_profiles_enabled && '50' || '30' }}",
            ),
        ):
            job = JOBS[job_id]
            self.assertEqual(job['needs'], 'review-config')
            review = next(step for step in job['steps'] if step.get('name') == review_name)
            self.assertEqual(
                review['uses'],
                f'FlyOverCoderKY/openrouter-pr-review-action@{ACTION_PIN}',
            )
            for field in ('models', 'model_routes', 'custom_instructions'):
                self.assertEqual(
                    review['with'][field],
                    '${{ needs.review-config.outputs.' + field + ' }}',
                )
            self.assertEqual(review['with']['review_policy'], "${{ inputs.review_policy || 'off' }}")
            self.assertEqual(review['with']['review_scope'], scope)
            self.assertEqual(review['with']['max_tool_turns'], turns)
            self.assertEqual(review['timeout-minutes'], '25')
        self.assertEqual(JOBS['openrouter-first-pass']['concurrency']['cancel-in-progress'], 'false')
        self.assertEqual(JOBS['openrouter-follow-up']['concurrency']['cancel-in-progress'], 'true')

    def test_policy_is_opt_in_and_completion_gate_remains_separate(self):
        for trigger in ('workflow_call', 'workflow_dispatch'):
            self.assertEqual(WORKFLOW['on'][trigger]['inputs']['review_policy']['default'], 'off')
            self.assertEqual(WORKFLOW['on'][trigger]['inputs']['review_profiles_enabled']['default'], 'false')
        gate = JOBS['openrouter-first-pass-gate']
        self.assertNotIn('review-config', str(gate['needs']))
        self.assertIn('always()', gate['if'])


if __name__ == '__main__':
    unittest.main()
