"""Contract checks against the pinned reusable workflow YAML blocks."""

import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
REVIEW = yaml.load(
    (ROOT / '.github/workflows/openrouter-code-review.yml').read_text(encoding='utf-8'),
    Loader=yaml.BaseLoader,
)
COMPLETION = yaml.load(
    (ROOT / '.github/workflows/openrouter-profile-completion.yml').read_text(encoding='utf-8'),
    Loader=yaml.BaseLoader,
)
ACTION_PIN = '188cd5557765c858a37c1da78960cd353bcbcd60'


def job_steps(workflow, job_id):
    return workflow['jobs'][job_id]['steps']


def step_named(steps, name):
    return next(step for step in steps if step.get('name') == name)


class ProfileWorkflowContractTests(unittest.TestCase):
    def test_prepared_context_path_and_digest_are_shared_between_setup_and_review(self):
        for job_id in ('openrouter-first-pass', 'openrouter-follow-up'):
            steps = job_steps(REVIEW, job_id)
            setup = step_named(steps, 'Prepare shared review snapshot')
            review_name = (
                'Run OpenRouter first-pass review'
                if job_id == 'openrouter-first-pass'
                else 'Run OpenRouter follow-up review'
            )
            review = step_named(steps, review_name)
            self.assertEqual(
                setup['with']['review_context_output_file'],
                '${{ runner.temp }}/review-context.json',
            )
            self.assertEqual(
                review['with']['review_context_file'],
                "${{ inputs.review_profiles_enabled && format('{0}/review-context.json', runner.temp) || '' }}",
            )
            self.assertEqual(
                review['with']['review_context_sha256'],
                '${{ steps.profile_setup.outputs.review_context_sha256 }}',
            )

    def test_trusted_action_checkout_ref_matches_uses_pin(self):
        for job_id in ('openrouter-first-pass', 'openrouter-follow-up'):
            for step in job_steps(REVIEW, job_id):
                uses = step.get('uses', '')
                if uses.startswith('FlyOverCoderKY/openrouter-pr-review-action@'):
                    self.assertEqual(uses, f'FlyOverCoderKY/openrouter-pr-review-action@{ACTION_PIN}')
                if step.get('name') == 'Checkout trusted review contracts':
                    self.assertEqual(step['with']['ref'], ACTION_PIN)

    def test_accept_and_preserve_steps_match_request_reader_names(self):
        for job_id in ('openrouter-first-pass', 'openrouter-follow-up'):
            names = [step.get('name') for step in job_steps(REVIEW, job_id)]
            self.assertIn('Accept review request', names)
            self.assertIn('Preserve accepted review request', names)

    def test_request_upload_fails_closed_when_file_is_missing(self):
        for job_id in ('openrouter-first-pass', 'openrouter-follow-up'):
            preserve = step_named(job_steps(REVIEW, job_id), 'Preserve accepted review request')
            self.assertEqual(preserve['uses'], 'actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02')
            self.assertEqual(preserve['with']['if-no-files-found'], 'error')
            self.assertEqual(preserve['with']['retention-days'], '90')

    def test_first_pass_is_sacred_and_follow_up_cancels_older_runs(self):
        self.assertEqual(
            REVIEW['jobs']['openrouter-first-pass']['concurrency']['cancel-in-progress'],
            'false',
        )
        self.assertEqual(
            REVIEW['jobs']['openrouter-follow-up']['concurrency']['cancel-in-progress'],
            'true',
        )

    def test_cancel_skips_review_and_gate(self):
        gate_if = REVIEW['jobs']['openrouter-first-pass-gate']['if']
        self.assertIn("inputs.review_level != 'cancel'", gate_if)
        first_review = step_named(job_steps(REVIEW, 'openrouter-first-pass'), 'Run OpenRouter first-pass review')
        self.assertIn("steps.profile_request.outputs.skip_review != 'true'", first_review['if'])

    def test_completion_proof_job_name_is_quoted_and_includes_pr_and_digest(self):
        proof = COMPLETION['jobs']['proof']
        self.assertEqual(proof['name'], "profile #${{ matrix.pr_number }} ${{ matrix.receipt_digest }}")

    def test_publish_waits_for_completed_proof_before_status(self):
        publish = COMPLETION['jobs']['publish']
        self.assertEqual(publish['needs'], ['plan', 'proof'])
        self.assertIn('needs.plan.result == \'success\'', publish['if'])
        self.assertEqual(
            step_named(publish['steps'], 'Publish only completed current proofs')['run'],
            'python3 -P -m scripts.profile_gate publish',
        )

    def test_profile_gate_runs_only_through_trusted_pythonpath_and_private_modules(self):
        for workflow, job_ids in (
            (REVIEW, ('openrouter-first-pass', 'openrouter-follow-up')),
            (COMPLETION, ('plan', 'proof', 'publish')),
        ):
            for job_id in job_ids:
                for step in job_steps(workflow, job_id):
                    if step.get('run', '').startswith('python3 -P -m scripts.profile_gate'):
                        env = step.get('env', {})
                        self.assertIn('PYTHONPATH', env)
                        self.assertIn('.trusted-review-org', env['PYTHONPATH'])
                        self.assertIn('.trusted-review-action/src', env['PYTHONPATH'])
                        self.assertTrue(step['run'].startswith('python3 -P -m scripts.profile_gate'))


if __name__ == '__main__':
    unittest.main()
