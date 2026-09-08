import { test } from 'node:test';
import assert from 'node:assert/strict';
import { dispatchWorkflowWake } from '../scripts/workflow_wakeup.mjs';

function fixture(overrides = {}) {
  const calls = [];
  const context = { repo: { owner: 'RetireGolden', repo: 'example' }, eventName: 'workflow_dispatch', actor: 'github-actions[bot]', ref: 'refs/heads/main', runId: 42, ...overrides };
  const github = { rest: {
    repos: { get: async () => ({ data: { full_name: 'RetireGolden/example', default_branch: 'main' } }) },
    actions: {
      getWorkflow: async ({ workflow_id }) => ({ data: { path: `.github/workflows/${workflow_id}`, state: 'active' } }),
      createWorkflowDispatch: async args => calls.push(args),
    },
  } };
  return { github, context, calls, core: { info() {} } };
}

test('bot dispatch explicitly wakes the default-branch proof validator with source identity only', async () => {
  const f = fixture();
  await dispatchWorkflowWake(f.github, f.core, f.context, 'openrouter-profile-completion.yml');
  assert.deepEqual(f.calls, [{ owner: 'RetireGolden', repo: 'example', workflow_id: 'openrouter-profile-completion.yml', ref: 'main', inputs: { source_run_id: '42' } }]);
});
test('human and pull-request runs retain native completion events without duplicate dispatches', async () => {
  for (const overrides of [{ actor: 'maintainer' }, { eventName: 'pull_request' }]) {
    const f = fixture(overrides);
    await dispatchWorkflowWake(f.github, f.core, f.context, 'openrouter-profile-completion.yml');
    assert.deepEqual(f.calls, []);
  }
});
test('off-default, invalid source and unsupported target cannot dispatch', async () => {
  for (const overrides of [{ ref: 'refs/heads/feature' }, { runId: 0 }]) {
    const f = fixture(overrides);
    await assert.rejects(dispatchWorkflowWake(f.github, f.core, f.context, 'openrouter-profile-completion.yml'));
    assert.deepEqual(f.calls, []);
  }
  const f = fixture();
  await assert.rejects(dispatchWorkflowWake(f.github, f.core, f.context, 'release.yml'));
});
test('missing optional broker is a noop; missing required validator and permission failures remain visible', async () => {
  for (const [status, optional, succeeds] of [[404, true, true], [404, false, false], [403, true, false]]) {
    const f = fixture();
    f.github.rest.actions.getWorkflow = async () => { throw Object.assign(new Error('lookup failed'), { status }); };
    const result = dispatchWorkflowWake(f.github, f.core, f.context, 'openrouter-ci-broker.yml', optional);
    if (succeeds) await result; else await assert.rejects(result);
    assert.deepEqual(f.calls, []);
  }
});
test('wrong workflow path and disabled workflows fail closed', async () => {
  for (const workflow of [{ path: '.github/workflows/release.yml', state: 'active' }, { path: '.github/workflows/openrouter-ci-broker.yml', state: 'disabled_manually' }]) {
    const f = fixture();
    f.github.rest.actions.getWorkflow = async () => ({ data: workflow });
    await assert.rejects(dispatchWorkflowWake(f.github, f.core, f.context, 'openrouter-ci-broker.yml', true));
    assert.deepEqual(f.calls, []);
  }
});
