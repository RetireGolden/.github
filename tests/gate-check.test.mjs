import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  DEFAULT_GATE_CHECK_NAME,
  GITHUB_ACTIONS_APP_ID,
  normalizeGateInputs,
  publishGateCheck,
} from '../scripts/gate_check.mjs';

const HEAD = 'a'.repeat(40);
const MAIN = 'b'.repeat(40);
const REPOSITORY = 'RetireGolden/example';
const REVIEW_URL = `https://github.com/${REPOSITORY}/pull/7#pullrequestreview-99`;

function fixture({ run = {}, pr = {}, context = {}, existing = [] } = {}) {
  const calls = [];
  const state = {
    repo: { full_name: REPOSITORY, default_branch: 'main' },
    run: { id: 42, event: 'repository_dispatch', head_sha: MAIN, head_branch: 'main', repository: { full_name: REPOSITORY }, ...run },
    pr: { number: 7, head: { sha: HEAD }, base: { repo: { full_name: REPOSITORY } }, ...pr },
  };
  const github = {
    request: async (route, params = {}) => {
      calls.push([route, params]);
      switch (route) {
        case 'GET /repos/{owner}/{repo}':
          return { data: state.repo };
        case 'GET /repos/{owner}/{repo}/actions/runs/{run_id}':
          return { data: state.run };
        case 'GET /repos/{owner}/{repo}/pulls/{pull_number}':
          return { data: state.pr };
        case 'GET /repos/{owner}/{repo}/commits/{ref}/check-runs':
          return { data: { check_runs: existing } };
        case 'POST /repos/{owner}/{repo}/check-runs':
          return { data: { id: 501, head_sha: params.head_sha, name: params.name } };
        case 'PATCH /repos/{owner}/{repo}/check-runs/{check_run_id}':
          return { data: { id: params.check_run_id, head_sha: params.head_sha, name: params.name } };
        default:
          throw new Error(`unexpected route ${route}`);
      }
    },
  };
  const ctx = {
    repo: { owner: 'RetireGolden', repo: 'example' },
    ref: 'refs/heads/main',
    runId: 42,
    serverUrl: 'https://github.com',
    ...context,
  };
  const core = { info() {} };
  return { github, core, ctx, calls };
}

const inputs = (overrides = {}) => ({
  prNumber: '7', headSha: HEAD, conclusion: 'success', reviewUrl: REVIEW_URL, name: '', mode: '', ...overrides,
});

const writes = (calls) => calls.filter(([route]) => !route.startsWith('GET '));

test('default-branch dispatch publishes the gate result on the evaluated PR head', async () => {
  for (const event of ['repository_dispatch', 'workflow_dispatch']) {
    const f = fixture({ run: { event } });
    const result = await publishGateCheck(f.github, f.core, f.ctx, inputs());
    assert.deepEqual(result, { published: true, reason: 'published', checkRunId: 501 });
    const [[route, payload]] = writes(f.calls);
    assert.equal(route, 'POST /repos/{owner}/{repo}/check-runs');
    assert.equal(payload.name, DEFAULT_GATE_CHECK_NAME);
    assert.equal(payload.head_sha, HEAD);
    assert.notEqual(payload.head_sha, MAIN);
    assert.equal(payload.status, 'completed');
    assert.equal(payload.conclusion, 'success');
    assert.equal(payload.details_url, 'https://github.com/RetireGolden/example/actions/runs/42');
    assert.equal(payload.external_id, 'openrouter-first-pass-gate:42');
    assert.match(payload.output.summary, /pullrequestreview-99/);
    assert.match(payload.output.summary, new RegExp(event));
  }
});

test('a failed or missing gate conclusion publishes failure', async () => {
  for (const conclusion of ['failure', '', undefined, 'cancelled', 'SUCCESS']) {
    const f = fixture();
    await publishGateCheck(f.github, f.core, f.ctx, inputs({ conclusion }));
    const [[, payload]] = writes(f.calls);
    assert.equal(payload.conclusion, 'failure');
    assert.equal(payload.output.title, 'OpenRouter first-pass gate failed');
  }
});

test('an existing check from the same run is updated instead of duplicated', async () => {
  const f = fixture({
    existing: [
      { id: 7, external_id: 'openrouter-first-pass-gate:41', app: { id: GITHUB_ACTIONS_APP_ID } },
      { id: 8, external_id: 'openrouter-first-pass-gate:42', app: { id: 999 } },
      { id: 9, external_id: 'openrouter-first-pass-gate:42', app: { id: GITHUB_ACTIONS_APP_ID } },
    ],
  });
  const result = await publishGateCheck(f.github, f.core, f.ctx, inputs({ conclusion: 'failure' }));
  assert.equal(result.checkRunId, 9);
  const [[route, payload]] = writes(f.calls);
  assert.equal(route, 'PATCH /repos/{owner}/{repo}/check-runs/{check_run_id}');
  assert.equal(payload.check_run_id, 9);
  assert.equal(payload.conclusion, 'failure');
});

test('a configurable name keeps the required check contract', async () => {
  const f = fixture();
  await publishGateCheck(f.github, f.core, f.ctx, inputs({ name: 'openrouter / openrouter-first-pass-gate' }));
  assert.equal(writes(f.calls)[0][1].name, 'openrouter / openrouter-first-pass-gate');
});

test('pull-request runs leave the head to their native job check in auto mode', async () => {
  for (const run of [
    { event: 'pull_request_target', head_sha: HEAD, head_branch: 'topic' },
    { event: 'pull_request_target', head_sha: MAIN, head_branch: 'topic' },
    { event: 'pull_request', head_sha: HEAD, head_branch: 'topic' },
    // A dispatch whose default-branch tip equals the PR head is already covered.
    { event: 'repository_dispatch', head_sha: HEAD, head_branch: 'main' },
  ]) {
    const f = fixture({ run });
    const result = await publishGateCheck(f.github, f.core, f.ctx, inputs());
    assert.equal(result.published, false);
    assert.deepEqual(writes(f.calls), []);
  }
  const f = fixture({ run: { event: 'pull_request_target', head_sha: HEAD, head_branch: 'topic' } });
  const result = await publishGateCheck(f.github, f.core, f.ctx, inputs({ mode: 'always' }));
  assert.equal(result.published, true);
});

test('non-default refs never publish, so branch callers cannot borrow it', async () => {
  for (const ref of ['refs/pull/7/merge', 'refs/heads/topic', '']) {
    const f = fixture({ context: { ref } });
    const result = await publishGateCheck(f.github, f.core, f.ctx, inputs({ mode: 'always' }));
    assert.equal(result.published, false);
    assert.deepEqual(f.calls.map(([route]) => route), ['GET /repos/{owner}/{repo}']);
  }
});

test('a moved PR head is left to its own run', async () => {
  const f = fixture({ pr: { head: { sha: 'c'.repeat(40) } } });
  const result = await publishGateCheck(f.github, f.core, f.ctx, inputs());
  assert.equal(result.published, false);
  assert.match(result.reason, /head moved/);
  assert.deepEqual(writes(f.calls), []);
});

test('forged numbers, heads, names, modes and foreign PRs fail closed', async () => {
  for (const overrides of [
    { prNumber: '0' }, { prNumber: '7/../8' }, { prNumber: '' }, { headSha: HEAD.toUpperCase() },
    { headSha: 'main' }, { name: 'x'.repeat(101) }, { name: 'bad\nname' }, { mode: 'force' },
  ]) {
    const f = fixture();
    await assert.rejects(publishGateCheck(f.github, f.core, f.ctx, inputs(overrides)));
    assert.deepEqual(writes(f.calls), []);
  }
  for (const pr of [{ number: 8 }, { base: { repo: { full_name: 'fork/example' } } }]) {
    const f = fixture({ pr });
    await assert.rejects(publishGateCheck(f.github, f.core, f.ctx, inputs()));
    assert.deepEqual(writes(f.calls), []);
  }
  const f = fixture({ run: { id: 43 } });
  await assert.rejects(publishGateCheck(f.github, f.core, f.ctx, inputs()));
});

test('review links outside this PR are dropped from the summary', () => {
  for (const reviewUrl of [
    'https://github.com/RetireGolden/example/pull/8#pullrequestreview-1',
    'https://evil.example/RetireGolden/example/pull/7#pullrequestreview-1',
    'javascript:alert(1)',
  ]) {
    assert.equal(normalizeGateInputs(inputs({ reviewUrl }), REPOSITORY).reviewUrl, '');
  }
  assert.equal(normalizeGateInputs(inputs(), REPOSITORY).reviewUrl, REVIEW_URL);
});
