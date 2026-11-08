"""Contract checks against the pinned reusable workflow YAML blocks."""

import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
GATE_CHECK = yaml.load(
    (ROOT / '.github/workflows/openrouter-review-gate-check.yml').read_text(encoding='utf-8'),
    Loader=yaml.BaseLoader,
)
GIT_BASH = Path('C:/Program Files/Git/bin/bash.exe')
BASH = str(GIT_BASH) if GIT_BASH.exists() else shutil.which('bash')
DEFAULT_REF = "github.ref == format('refs/heads/{0}', github.event.repository.default_branch)"
PR_ACTIONS = """contains(fromJSON('["opened","reopened","ready_for_review","synchronize"]'), github.event.action)"""
# Permissions every RetireGolden review caller grants today (top level, no job override).
LEGACY_CALLER_PERMISSIONS = {
    'actions': 'write', 'contents': 'read', 'pull-requests': 'write', 'statuses': 'write',
}
LEVEL = {'none': 0, 'read': 1, 'write': 2}
REVIEW = yaml.load(
    (ROOT / '.github/workflows/openrouter-code-review.yml').read_text(encoding='utf-8'),
    Loader=yaml.BaseLoader,
)
COMPLETION = yaml.load(
    (ROOT / '.github/workflows/openrouter-profile-completion.yml').read_text(encoding='utf-8'),
    Loader=yaml.BaseLoader,
)
ACTION_PIN = '0d6a05e41616281368c1f6bb7fb5a47ddf41375d'


def job_steps(workflow, job_id):
    return workflow['jobs'][job_id]['steps']


def step_named(steps, name):
    return next(step for step in steps if step.get('name') == name)


class ProfileWorkflowContractTests(unittest.TestCase):
    def test_bot_notifications_wait_for_all_source_jobs_and_use_trusted_source(self):
        for workflow, job_id, needs, source, target in (
            (REVIEW, 'notify-profile-completion',
             ['review-config', 'openrouter-first-pass', 'openrouter-follow-up', 'openrouter-first-pass-gate'],
             'review-config', 'openrouter-profile-completion.yml'),
            (COMPLETION, 'notify-ci-broker', ['plan', 'proof', 'publish'],
             'plan', 'openrouter-ci-broker.yml'),
        ):
            job = workflow['jobs'][job_id]
            self.assertEqual(job['needs'], needs)
            self.assertEqual(job['continue-on-error'], 'true')
            self.assertEqual(job['permissions'], {'actions': 'write', 'contents': 'read'})
            self.assertIn("github.event_name == 'workflow_dispatch'", job['if'])
            self.assertIn("github.actor == 'github-actions[bot]'", job['if'])
            self.assertIn('always()', job['if'])
            checkout = job['steps'][0]['with']
            self.assertEqual(checkout['repository'], 'RetireGolden/.github')
            self.assertEqual(checkout['ref'], '${{ needs.' + source + '.outputs.org_sha }}')
            self.assertEqual(checkout['persist-credentials'], 'false')
            self.assertIn(target, job['steps'][1]['with']['script'])
        self.assertIn('inputs.review_profiles_enabled', REVIEW['jobs']['notify-profile-completion']['if'])

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


class Expression:
    """Tiny evaluator for the GitHub expression subset used in job conditions."""

    import re as _re
    TOKEN = _re.compile(r"\s*(\|\||&&|==|!=|!|\(|\)|,|'(?:[^']|'')*'|[A-Za-z_][A-Za-z0-9_.-]*)")

    def __init__(self, text, context):
        text = text.strip()
        if text.startswith('${{') and text.endswith('}}'):
            text = text[3:-2]
        self.tokens, position = [], 0
        while position < len(text.rstrip()):
            match = self.TOKEN.match(text, position)
            if not match:
                raise ValueError(f'cannot parse {text[position:]!r}')
            self.tokens.append(match.group(1))
            position = match.end()
        self.index, self.context = 0, context

    def evaluate(self):
        value = self.or_()
        if self.index != len(self.tokens):
            raise ValueError(f'trailing tokens {self.tokens[self.index:]}')
        return value

    def peek(self):
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def take(self, expected=None):
        token = self.peek()
        if expected is not None and token != expected:
            raise ValueError(f'expected {expected}, found {token}')
        self.index += 1
        return token

    @staticmethod
    def truthy(value):
        return value not in (None, '', 0, False)

    def or_(self):
        value = self.and_()
        while self.peek() == '||':
            self.take()
            right = self.and_()
            value = value if self.truthy(value) else right
        return value

    def and_(self):
        value = self.compare()
        while self.peek() == '&&':
            self.take()
            right = self.compare()
            value = right if self.truthy(value) else value
        return value

    def compare(self):
        value = self.unary()
        while self.peek() in ('==', '!='):
            operator = self.take()
            right = self.unary()
            left_cmp = value.lower() if isinstance(value, str) else value
            right_cmp = right.lower() if isinstance(right, str) else right
            value = (left_cmp == right_cmp) == (operator == '==')
        return value

    def unary(self):
        if self.peek() == '!':
            self.take()
            return not self.truthy(self.unary())
        return self.primary()

    def primary(self):
        token = self.take()
        if token == '(':
            value = self.or_()
            self.take(')')
            return value
        if token.startswith("'"):
            return token[1:-1].replace("''", "'")
        if token in ('true', 'false'):
            return token == 'true'
        if self.peek() == '(':
            self.take('(')
            args = []
            while self.peek() != ')':
                args.append(self.or_())
                if self.peek() == ',':
                    self.take()
            self.take(')')
            return self.call(token, args)
        value = self.context
        for part in token.split('.'):
            value = value.get(part) if isinstance(value, dict) else None
        return value

    def call(self, name, args):
        if name in ('always', 'success'):
            return True
        if name == 'cancelled':
            return False
        if name == 'format':
            return args[0].replace('{0}', str(args[1]))
        if name == 'fromJSON':
            import json
            return json.loads(args[0])
        if name == 'contains':
            return args[1] in args[0]
        raise ValueError(name)


def caller_context(event_name, action=None, *, ref='refs/heads/main', draft=False, number=7, **inputs):
    pull_request = None if event_name.endswith('dispatch') else {'draft': draft, 'number': number}
    return {
        'github': {
            'event_name': event_name,
            'ref': ref,
            'repository': 'RetireGolden/example',
            'actor': inputs.pop('actor', 'maintainer'),
            'event': {
                'action': action,
                'pull_request': pull_request,
                'repository': {'default_branch': 'main'},
            },
        },
        'inputs': {'review_level': 'auto', 'review_profiles_enabled': True, **inputs},
        'needs': {'review-config': {'result': 'success'}},
    }


def runs(job_id, context, workflow=None):
    return bool(Expression((workflow or REVIEW)['jobs'][job_id]['if'], context).evaluate())


class TrustedCallerEventContractTests(unittest.TestCase):
    JOBS = ('review-config', 'openrouter-first-pass', 'openrouter-follow-up', 'openrouter-first-pass-gate')

    def selected(self, context):
        return {job for job in self.JOBS if runs(job, context)}

    def test_legacy_pull_request_and_dispatch_selection_is_unchanged(self):
        for action in ('opened', 'reopened', 'ready_for_review'):
            self.assertEqual(
                self.selected(caller_context('pull_request', action, ref='refs/pull/7/merge')),
                {'review-config', 'openrouter-first-pass', 'openrouter-first-pass-gate'},
            )
        self.assertEqual(
            self.selected(caller_context('pull_request', 'synchronize', ref='refs/pull/7/merge')),
            {'review-config', 'openrouter-follow-up', 'openrouter-first-pass-gate'},
        )
        # A pull_request draft still reaches only the gate, which reports the draft.
        self.assertEqual(
            self.selected(caller_context('pull_request', 'opened', ref='refs/pull/7/merge', draft=True)),
            {'openrouter-first-pass-gate'},
        )
        # workflow_dispatch from any ref keeps its former job selection.
        for ref in ('refs/heads/main', 'refs/heads/topic'):
            self.assertEqual(
                self.selected(caller_context('workflow_dispatch', ref=ref)),
                {'review-config', 'openrouter-first-pass', 'openrouter-first-pass-gate'},
            )
        self.assertEqual(self.selected(caller_context('workflow_dispatch', review_level='cancel')),
                         {'review-config', 'openrouter-first-pass'})

    def test_pull_request_target_keeps_first_pass_and_follow_up_split(self):
        for action in ('opened', 'reopened', 'ready_for_review'):
            self.assertEqual(
                self.selected(caller_context('pull_request_target', action)),
                {'review-config', 'openrouter-first-pass', 'openrouter-first-pass-gate'},
            )
        self.assertEqual(
            self.selected(caller_context('pull_request_target', 'synchronize')),
            {'review-config', 'openrouter-follow-up', 'openrouter-first-pass-gate'},
        )

    def test_pull_request_target_requires_default_base_and_review_actions(self):
        # A PR into another branch would run that branch's caller.
        self.assertEqual(self.selected(caller_context('pull_request_target', 'opened', ref='refs/heads/release')), set())
        self.assertEqual(self.selected(caller_context('pull_request_target', 'synchronize', ref='refs/heads/release')), set())
        # Other PR actions never publish a failing gate on the head.
        for action in ('labeled', 'edited', 'closed', None):
            self.assertEqual(self.selected(caller_context('pull_request_target', action)), set())
        self.assertEqual(
            self.selected(caller_context('pull_request_target', 'opened', draft=True)),
            {'openrouter-first-pass-gate'},
        )

    def test_repository_dispatch_is_a_default_branch_first_pass(self):
        # The dispatch event type is free-form; even "synchronize" stays a first pass.
        for event_type in ('openrouter-review', 'synchronize'):
            self.assertEqual(
                self.selected(caller_context('repository_dispatch', event_type)),
                {'review-config', 'openrouter-first-pass', 'openrouter-first-pass-gate'},
            )
        self.assertEqual(self.selected(caller_context('repository_dispatch', 'x', ref='refs/heads/topic')), set())
        self.assertEqual(
            self.selected(caller_context('repository_dispatch', 'x', review_level='cancel')),
            {'review-config', 'openrouter-first-pass'},
        )

    def test_untrusted_events_select_no_job(self):
        for event in ('push', 'workflow_run', 'issue_comment', 'pull_request_review'):
            self.assertEqual(self.selected(caller_context(event, 'opened')), set())

    def test_no_scan_repositories_still_skip_review_jobs(self):
        for repo in ('RetireGolden/.github', 'RetireGolden/cla-signatures', 'RetireGolden/RetireBench-private'):
            for event in ('pull_request_target', 'repository_dispatch'):
                context = caller_context(event, 'opened')
                context['github']['repository'] = repo
                self.assertFalse(runs('review-config', context))
                self.assertFalse(runs('openrouter-first-pass', context))

    def test_bot_dispatches_wake_profile_completion(self):
        for event, actor, expected in (
            ('workflow_dispatch', 'github-actions[bot]', True),
            ('repository_dispatch', 'github-actions[bot]', True),
            ('repository_dispatch', 'maintainer', False),
            ('pull_request_target', 'github-actions[bot]', False),
        ):
            with self.subTest(event=event, actor=actor):
                self.assertEqual(runs('notify-profile-completion', caller_context(event, 'opened', actor=actor)), expected)

    def test_review_workflow_requests_no_permission_beyond_current_callers(self):
        def within(permissions):
            for scope, level in permissions.items():
                self.assertLessEqual(LEVEL[level], LEVEL[LEGACY_CALLER_PERMISSIONS.get(scope, 'none')], scope)

        within(REVIEW['permissions'])
        for job_id, job in REVIEW['jobs'].items():
            with self.subTest(job=job_id):
                within(job.get('permissions', REVIEW['permissions']))
        self.assertNotIn('checks', str(REVIEW['permissions']))
        self.assertFalse(any('checks' in job.get('permissions', {}) for job in REVIEW['jobs'].values()))

    def test_gate_check_entrypoint_holds_the_only_checks_permission(self):
        self.assertEqual(
            GATE_CHECK['permissions'],
            {'actions': 'read', 'checks': 'write', 'contents': 'read', 'pull-requests': 'read'},
        )
        self.assertEqual(list(GATE_CHECK['jobs']), ['publish'])
        job = GATE_CHECK['jobs']['publish']
        self.assertEqual(job['permissions'], GATE_CHECK['permissions'])
        inputs = GATE_CHECK['on']['workflow_call']['inputs']
        self.assertEqual(inputs['gate_check_name']['default'], 'review / openrouter-first-pass-gate')
        self.assertEqual(inputs['publish_mode']['default'], 'auto')
        self.assertEqual(inputs['head_sha']['required'], 'true')
        self.assertEqual(inputs['pr_number']['required'], 'true')
        source, checkout, publish = job['steps']
        self.assertIn('openrouter-review-gate-check.yml@', source['run'])
        self.assertIn('referenced_workflows', source['run'])
        self.assertEqual(checkout['with'], {
            'repository': 'RetireGolden/.github',
            'ref': '${{ steps.source.outputs.sha }}',
            'path': '.trusted-review-org',
            'persist-credentials': 'false',
        })
        self.assertIn('.trusted-review-org/scripts/gate_check.mjs', publish['with']['script'])
        self.assertNotEqual(job['name'], 'openrouter-first-pass-gate')

    def test_gate_outputs_flow_to_the_gate_check_entrypoint(self):
        outputs = REVIEW['on']['workflow_call']['outputs']
        for name in ('pr_number', 'head_sha', 'conclusion', 'review_url'):
            self.assertEqual(outputs['gate_' + name]['value'],
                             '${{ jobs.openrouter-first-pass-gate.outputs.' + name + ' }}')
            self.assertEqual(REVIEW['jobs']['openrouter-first-pass-gate']['outputs'][name],
                             '${{ steps.gate.outputs.' + name + ' }}')
        gate = step_named(job_steps(REVIEW, 'openrouter-first-pass-gate'), 'Evaluate OpenRouter first-pass gate')
        self.assertEqual(gate['id'], 'gate')
        self.assertIn("trap '", gate['run'])
        self.assertIn('echo "head_sha=$HEAD_SHA" >> "$GITHUB_OUTPUT"', gate['run'])

    def test_dispatch_event_type_is_not_a_pull_request_action(self):
        env = REVIEW['jobs']['openrouter-first-pass-gate']['env']
        self.assertEqual(
            env['EVENT_ACTION'],
            "${{ (github.event_name == 'pull_request' || github.event_name == 'pull_request_target') && github.event.action || '' }}",
        )
        for event, action, expected in (
            ('pull_request', 'synchronize', 'synchronize'),
            ('pull_request_target', 'synchronize', 'synchronize'),
            ('repository_dispatch', 'synchronize', ''),
            ('workflow_dispatch', None, ''),
        ):
            self.assertEqual(Expression(env['EVENT_ACTION'], caller_context(event, action)).evaluate(), expected)
        self.assertEqual(env['EXPECTED_HEAD_SHA'], '${{ inputs.expected_head_sha }}')

    def test_repository_dispatch_reviews_the_exact_checked_out_head(self):
        steps = job_steps(REVIEW, 'openrouter-first-pass')
        expression = "${{ github.event_name == 'repository_dispatch' && steps.head.outputs.sha || '' }}"
        for name in ('Prepare shared review snapshot', 'Run OpenRouter first-pass review'):
            self.assertEqual(step_named(steps, name)['with']['head_sha'], expression)
        resolve = step_named(steps, 'Resolve PR head SHA')
        self.assertEqual(resolve['env']['EXPECTED_HEAD_SHA'], '${{ inputs.expected_head_sha }}')
        self.assertIn('[ "$HEAD_SHA" != "$EXPECTED_HEAD_SHA" ]', resolve['run'])
        context = caller_context('repository_dispatch', 'x')
        context['steps'] = {'head': {'outputs': {'sha': 'a' * 40}}}
        self.assertEqual(Expression(expression, context).evaluate(), 'a' * 40)
        context['github']['event_name'] = 'pull_request'
        self.assertEqual(Expression(expression, context).evaluate(), '')

    def test_secret_holding_review_jobs_never_execute_checkout_content(self):
        for job_id in ('openrouter-first-pass', 'openrouter-follow-up'):
            steps = job_steps(REVIEW, job_id)
            names = [step.get('name') for step in steps]
            checkout = step_named(steps, 'Checkout PR head')
            self.assertEqual(checkout['with']['persist-credentials'], 'false')
            self.assertEqual(checkout['with']['submodules'], 'false')
            self.assertEqual(checkout['with']['lfs'], 'false')
            self.assertEqual(checkout['with']['ref'], '${{ steps.head.outputs.sha }}')
            guard = names.index('Refuse reserved trusted paths in the reviewed checkout')
            self.assertEqual(guard, names.index('Checkout PR head') + 1)
            self.assertLess(guard, names.index('Checkout trusted profile coordinator'))
            for step in steps:
                uses = step.get('uses', '')
                self.assertFalse(uses.startswith('./'), step.get('name'))
                if uses:
                    self.assertRegex(uses, r'@[0-9a-f]{40}$')
                run = step.get('run', '')
                for forbidden in ('npm ', 'npx ', 'pnpm ', 'yarn ', 'pip install', 'make ', 'bash ./', 'sh ./', 'source ', '. ./'):
                    self.assertNotIn(forbidden, run, step.get('name'))
                for line in run.splitlines():
                    if 'python' in line and 'python3 -P' not in line and 'shell: python' not in line:
                        self.fail(f'{step.get("name")}: {line}')
            for step in steps:
                if step.get('uses', '').startswith('actions/checkout@') and step.get('with', {}).get('repository'):
                    self.assertEqual(step['with']['persist-credentials'], 'false')

    def test_reserved_path_guard_rejects_seeded_or_symlinked_trusted_paths(self):
        if not BASH:
            self.skipTest('bash required')
        script = step_named(job_steps(REVIEW, 'openrouter-first-pass'),
                            'Refuse reserved trusted paths in the reviewed checkout')['run']
        for setup in ('', 'mkdir .trusted-review-org', 'touch .trusted-review-action', 'mkdir elsewhere && ln -s elsewhere .trusted-review-org'):
            with self.subTest(setup=setup), tempfile.TemporaryDirectory() as folder:
                if setup:
                    subprocess.run([BASH, '-c', setup], cwd=folder, check=True)
                result = subprocess.run([BASH, '-c', script], cwd=folder, capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, setup == '', result.stdout + result.stderr)


@unittest.skipUnless(BASH, 'bash required to exercise the workflow shell')
class PullRequestContextStepTests(unittest.TestCase):
    SCRIPT = next(step['run'] for step in REVIEW['jobs']['review-config']['steps'] if step.get('id') == 'pr')

    def resolve(self, **env):
        with tempfile.TemporaryDirectory() as folder:
            output, summary = Path(folder) / 'output', Path(folder) / 'summary'
            result = subprocess.run(
                [BASH, '-c', self.SCRIPT],
                env={**os.environ, 'GITHUB_OUTPUT': str(output), 'GITHUB_STEP_SUMMARY': str(summary),
                     'GITHUB_SHA': 'b' * 40, **env},
                capture_output=True, text=True,
            )
            values = dict(line.split('=', 1) for line in output.read_text().splitlines()) if output.exists() else {}
            return result, values

    def test_pull_request_events_bind_the_triggering_pr_and_pass(self):
        for event in ('pull_request', 'pull_request_target'):
            for action, expected in (('opened', 'first-pass'), ('ready_for_review', 'first-pass'), ('synchronize', 'follow-up')):
                with self.subTest(event=event, action=action):
                    result, values = self.resolve(EVENT_NAME=event, EVENT_ACTION=action, EVENT_PR_NUMBER='7',
                                                  INPUT_PR_NUMBER='7', EVENT_HEAD_SHA='a' * 40)
                    self.assertEqual(result.returncode, 0, result.stdout)
                    self.assertEqual((values['pr_number'], values['pass'], values['event_head_sha']), ('7', expected, 'a' * 40))

    def test_forged_or_missing_pr_numbers_fail_closed(self):
        for env in (
            {'EVENT_NAME': 'pull_request_target', 'EVENT_ACTION': 'opened', 'EVENT_PR_NUMBER': '7', 'INPUT_PR_NUMBER': '8'},
            {'EVENT_NAME': 'repository_dispatch', 'EVENT_ACTION': 'openrouter-review'},
            {'EVENT_NAME': 'repository_dispatch', 'INPUT_PR_NUMBER': '7/../../x'},
            {'EVENT_NAME': 'repository_dispatch', 'INPUT_PR_NUMBER': '07'},
            {'EVENT_NAME': 'workflow_dispatch', 'INPUT_PR_NUMBER': '-1'},
            {'EVENT_NAME': 'push', 'INPUT_PR_NUMBER': '7'},
            {'EVENT_NAME': 'repository_dispatch', 'INPUT_PR_NUMBER': '7', 'EXPECTED_HEAD_SHA': 'A' * 40},
            {'EVENT_NAME': 'repository_dispatch', 'INPUT_PR_NUMBER': '7', 'EXPECTED_HEAD_SHA': 'main'},
        ):
            with self.subTest(env=env):
                result, values = self.resolve(**env)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('pr_number', values)

    def test_dispatches_are_first_passes_named_by_inputs(self):
        result, values = self.resolve(EVENT_NAME='repository_dispatch', EVENT_ACTION='synchronize',
                                      INPUT_PR_NUMBER='12', EXPECTED_HEAD_SHA='c' * 40)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual((values['pr_number'], values['pass']), ('12', 'first-pass'))
        # repository_dispatch never falls back to unmapped payload fields.
        result, _ = self.resolve(EVENT_NAME='repository_dispatch', DISPATCH_PR_NUMBER='12')
        self.assertNotEqual(result.returncode, 0)
        # workflow_dispatch keeps its github.event.inputs fallback.
        result, values = self.resolve(EVENT_NAME='workflow_dispatch', DISPATCH_PR_NUMBER='12')
        self.assertEqual((result.returncode, values.get('pr_number')), (0, '12'))


@unittest.skipUnless(BASH, 'bash required to exercise the workflow shell')
class GateOutputShellTests(unittest.TestCase):
    """The gate records the evaluated head and its conclusion for check publishing."""

    SCRIPT = textwrap.dedent(step_named(job_steps(REVIEW, 'openrouter-first-pass-gate'),
                                        'Evaluate OpenRouter first-pass gate')['run'])

    def evaluate(self, *, live_head='a' * 40, repository='RetireGolden/example', **env):
        with tempfile.TemporaryDirectory() as folder:
            bin_dir, output = Path(folder) / 'bin', Path(folder) / 'output'
            bin_dir.mkdir()
            fake = bin_dir / 'gh'
            fake.write_text('#!/bin/bash\nprintf "%s\\n" "$FAKE_LIVE_HEAD"\n', newline='\n')
            fake.chmod(0o755)
            result = subprocess.run(
                [BASH, '-c', f'export PATH="$(cygpath -u "{bin_dir}" 2>/dev/null || echo "{bin_dir}"):$PATH"; ' + self.SCRIPT],
                env={**os.environ, 'GITHUB_OUTPUT': str(output), 'GITHUB_REPOSITORY': repository,
                     'FAKE_LIVE_HEAD': live_head, 'PR_NUMBER': '7', 'IS_DRAFT': 'false', 'EVENT_ACTION': '',
                     'EVENT_HEAD_SHA': '', 'EXPECTED_HEAD_SHA': '', 'ACTION_OUTCOME': 'failure', 'VERDICT': '',
                     'REVIEW_URL': '', 'FIRST_PASS_RESULT': 'failure', 'RUN_URL': 'https://example.test/run', **env},
                capture_output=True, text=True,
            )
            lines = output.read_text().splitlines() if output.exists() else []
            return result, dict(line.split('=', 1) for line in lines), lines

    def test_failed_fresh_first_pass_publishes_failure_for_the_live_head(self):
        result, values, lines = self.evaluate()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(values, {'pr_number': '7', 'head_sha': 'a' * 40, 'conclusion': 'failure'})
        self.assertEqual(lines[-1], 'conclusion=failure')

    def test_stale_dispatch_head_fails_for_the_live_head(self):
        result, values, _ = self.evaluate(EXPECTED_HEAD_SHA='c' * 40, ACTION_OUTCOME='success', VERDICT='clean')
        self.assertEqual(result.returncode, 1)
        self.assertIn('not the dispatched', result.stdout)
        self.assertEqual((values['head_sha'], values['conclusion']), ('a' * 40, 'failure'))

    def test_invalid_inputs_never_name_a_head(self):
        for env in ({'PR_NUMBER': '7/../x'}, {'IS_DRAFT': 'true'}, {'live_head': 'not-a-sha'}):
            with self.subTest(env=env):
                result, values, _ = self.evaluate(**env)
                self.assertEqual(result.returncode, 1)
                self.assertNotIn('head_sha', values)
                self.assertEqual(values['conclusion'], 'failure')

    def test_no_scan_repository_succeeds_without_a_head(self):
        result, values, _ = self.evaluate(repository='RetireGolden/.github')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(values, {'conclusion': 'success'})


if __name__ == '__main__':
    unittest.main()
