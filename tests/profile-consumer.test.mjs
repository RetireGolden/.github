import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { beforeEach, describe, it } from 'node:test';

beforeEach((context) => {
  context.mock.method(Date, 'now', () => Date.parse('2024-01-02T10:07:00Z'));
});

import {
  authorizeProfileReceipt,
  completionPullRequests,
  COMPLETION_CALLER_PATH,
  COMPLETION_WORKFLOW_NAME,
  ORG_COMPLETION_REUSABLE_PREFIX,
  ORG_REVIEW_REUSABLE_PREFIX,
  RECEIPT_MARKER_PREFIX,
  REVIEW_CALLER_PATH,
  REVIEW_WORKFLOW_NAME,
  TRUSTED_REVIEW_AUTHOR,
  TRUSTED_REVIEW_AUTHOR_ID,
  TRUSTED_REVIEW_AUTHOR_TYPE,
} from '../scripts/profile_consumer.mjs';

const OWNER = 'RetireGolden';
const REPO = 'example';
const REPOSITORY = { full_name: `${OWNER}/${REPO}` };
const DEFAULT_BRANCH = 'main';
const HEAD_SHA = 'a'.repeat(40);
const MAIN_SHA = 'b'.repeat(40);
const BASE_SHA = 'd'.repeat(40);
const REVIEW_PIN = 'c'.repeat(40);
const ORG_SHA = REVIEW_PIN;
const DIGEST = 'f'.repeat(64);
const REGISTRY_DIGEST = '0'.repeat(64);
const CONTEXT_DIGEST = '1'.repeat(64);
const REVIEW_RUN_ID = 100;
const COMPLETION_RUN_ID = 200;
const PULL_NUMBER = 7;
const REVIEW_WORKFLOW_ID = 11;
const COMPLETION_WORKFLOW_ID = 22;
const REVIEW_CALLER_SHA = '3'.repeat(40);
const COMPLETION_CALLER_SHA = '4'.repeat(40);
const PR_CREATED_AT = '2024-01-01T00:00:00Z';
const PROOF_STARTED = '2024-01-02T10:00:00Z';
const PROOF_COMPLETED = '2024-01-02T10:05:00Z';
const STATUS_CREATED = '2024-01-02T10:06:00Z';
const REVIEW_UPDATED = '2024-01-02T09:50:00Z';

const reviewCallerText = [
  'name: OpenRouter code review',
  `uses: ${ORG_REVIEW_REUSABLE_PREFIX}${REVIEW_PIN}`,
].join('\n');
const completionCallerText = [
  `name: ${COMPLETION_WORKFLOW_NAME}`,
  `uses: ${ORG_COMPLETION_REUSABLE_PREFIX}${ORG_SHA}`,
].join('\n');

function encodeContent(text) {
  const buffer = Buffer.from(text, 'utf8');
  return {
    type: 'file',
    sha: createHash('sha1').update(`blob ${buffer.length}\0`).update(buffer).digest('hex'),
    content: buffer.toString('base64'),
  };
}

function receiptObject(overrides = {}) {
  return {
    version: 1,
    repository: REPOSITORY.full_name,
    pr_number: PULL_NUMBER,
    head_sha: HEAD_SHA,
    policy_base_sha: BASE_SHA,
    policy_digest: DIGEST,
    profile: 'code',
    level: 'standard',
    trigger: 'baseline',
    registry_digest: REGISTRY_DIGEST,
    context_sha256: CONTEXT_DIGEST,
    required_models: ['openai/gpt-5'],
    successful_models: ['openai/gpt-5'],
    panel_status: 'complete',
    profile_satisfied: true,
    verdict: 'clean',
    scope: 'full-pr',
    mode: 'initial',
    run_url: `https://github.com/${REPOSITORY.full_name}/actions/runs/${REVIEW_RUN_ID}`,
    run_attempt: 1,
    ...overrides,
  };
}

function reviewBody(receipt, digest) {
  const raw = Buffer.from(JSON.stringify(receipt), 'utf8');
  const marker = Buffer.from(raw).toString('base64');
  return [
    '## OpenRouter pull-request review',
    '',
    '**Verdict:** `clean`',
    '**Scope:** `full-pr` (full-pr)',
    '**Mode:** `initial`',
    `**Commit:** \`${HEAD_SHA}\``,
    `${RECEIPT_MARKER_PREFIX}${marker} -->`,
    '**Profile:** satisfied',
    '',
    '### Lanes',
    '',
    '- lane ok',
    '',
    `[Workflow run](${receipt.run_url})`,
  ].join('\n');
}

function trustedBot(overrides = {}) {
  return {
    login: TRUSTED_REVIEW_AUTHOR,
    id: TRUSTED_REVIEW_AUTHOR_ID,
    type: TRUSTED_REVIEW_AUTHOR_TYPE,
    ...overrides,
  };
}

function reviewRun(overrides = {}) {
  return {
    id: REVIEW_RUN_ID,
    run_attempt: 1,
    workflow_id: REVIEW_WORKFLOW_ID,
    head_sha: HEAD_SHA,
    head_branch: 'topic',
    event: 'pull_request',
    path: REVIEW_CALLER_PATH,
    name: REVIEW_WORKFLOW_NAME,
    repository: REPOSITORY,
    head_repository: REPOSITORY,
    referenced_workflows: [{ path: `${ORG_REVIEW_REUSABLE_PREFIX}${REVIEW_PIN}`, sha: REVIEW_PIN }],
    status: 'completed',
    conclusion: 'success',
    updated_at: REVIEW_UPDATED,
    pull_requests: [{ number: PULL_NUMBER }],
    ...overrides,
  };
}

function completionRun(overrides = {}) {
  return {
    id: COMPLETION_RUN_ID,
    run_attempt: 1,
    workflow_id: COMPLETION_WORKFLOW_ID,
    head_sha: MAIN_SHA,
    head_branch: DEFAULT_BRANCH,
    event: 'push',
    path: COMPLETION_CALLER_PATH,
    name: COMPLETION_WORKFLOW_NAME,
    repository: REPOSITORY,
    head_repository: REPOSITORY,
    referenced_workflows: [{ path: `${ORG_COMPLETION_REUSABLE_PREFIX}${ORG_SHA}`, sha: ORG_SHA }],
    status: 'completed',
    conclusion: 'failure',
    ...overrides,
  };
}

function buildStore(options = {}) {
  const receipt = receiptObject(options.receiptOverrides);
  const raw = Buffer.from(JSON.stringify(receipt), 'utf8');
  const receiptDigest = createHash('sha256').update(raw).digest('hex');
  const body = reviewBody(receipt, receiptDigest);
  const review = {
    user: trustedBot(),
    commit_id: HEAD_SHA,
    body,
  };
  const proofJobName = `complete / profile #${PULL_NUMBER} ${receiptDigest}`;
  const statuses = options.statuses ?? [
    {
      id: 90,
      context: 'openrouter-profile',
      state: 'success',
      creator: trustedBot(),
      target_url: `https://github.com/${REPOSITORY.full_name}/actions/runs/${COMPLETION_RUN_ID}`,
      created_at: STATUS_CREATED,
    },
  ];
  const jobs = options.jobs ?? [
    {
      name: proofJobName,
      status: 'completed',
      conclusion: 'success',
      started_at: PROOF_STARTED,
      completed_at: PROOF_COMPLETED,
    },
  ];
  const reviewRuns = options.reviewRuns ?? [reviewRun()];
  const reviewCallerSha = options.reviewCallerSha ?? REVIEW_CALLER_SHA;
  const headCallerSha = options.headCallerSha ?? reviewCallerSha;
  const completionCallerSha = options.completionCallerSha ?? COMPLETION_CALLER_SHA;
  const mainSha = options.mainSha ?? MAIN_SHA;
  const headSha = options.headSha ?? HEAD_SHA;
  const baseSha = options.baseSha ?? MAIN_SHA;

  const reviewCaller = encodeContent(reviewCallerText);
  reviewCaller.sha = reviewCallerSha;
  const headCaller = encodeContent(reviewCallerText);
  headCaller.sha = headCallerSha;
  const completionCaller = encodeContent(completionCallerText);
  completionCaller.sha = completionCallerSha;

  const store = {
    receipt,
    receiptDigest,
    review,
    reviewRun: reviewRun(options.reviewRunOverrides),
    completionRun: completionRun(options.completionRunOverrides),
    jobs,
    statuses,
    reviewRuns,
    mainSha,
    headSha,
    baseSha,
    refreshMainSha: options.refreshMainSha,
    refreshBaseSha: options.refreshBaseSha,
    reviewCaller,
    headCaller,
    completionCaller,
    pullReads: 0,
  };

  const github = {
    request: async (route, params = {}) => {
      if (route === 'GET /repos/{owner}/{repo}') {
        return { data: { default_branch: DEFAULT_BRANCH } };
      }
      if (route === 'GET /repos/{owner}/{repo}/git/ref/{ref}') {
        const sha = store.refReads === 1 && store.refreshMainSha ? store.refreshMainSha : store.mainSha;
        store.refReads = (store.refReads ?? 0) + 1;
        return { data: { object: { sha } } };
      }
      if (route === 'GET /repos/{owner}/{repo}/contents/{path}') {
        if (params.path === REVIEW_CALLER_PATH) {
          const caller = params.ref === store.mainSha ? store.reviewCaller : store.headCaller;
          return { data: caller };
        }
        if (params.path === COMPLETION_CALLER_PATH) {
          return { data: store.completionCaller };
        }
        throw new Error(`unexpected content path ${params.path}`);
      }
      if (route === 'GET /repos/{owner}/{repo}/pulls/{pull_number}') {
        store.pullReads += 1;
        const resolvedBaseSha =
          store.pullReads > 1 && store.refreshBaseSha ? store.refreshBaseSha : store.baseSha;
        return {
          data: {
            state: 'open',
            draft: false,
            created_at: options.createdAt ?? PR_CREATED_AT,
            head: { sha: store.headSha, repo: REPOSITORY },
            base: { sha: resolvedBaseSha, ref: DEFAULT_BRANCH, repo: REPOSITORY },
          },
        };
      }
      if (route === 'GET /repos/{owner}/{repo}/commits/{ref}/statuses') {
        return { data: store.statuses };
      }
      if (route === 'GET /repos/{owner}/{repo}/actions/runs/{run_id}') {
        const requestedId = Number(params.run_id);
        if (requestedId !== store.completionRun.id) {
          return {
            data: {
              ...store.completionRun,
              id: requestedId,
              head_sha: '6'.repeat(40),
            },
          };
        }
        return { data: store.completionRun };
      }
      if (route === 'GET /repos/{owner}/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs') {
        return { data: { jobs: store.jobs } };
      }
      if (route === 'GET /repos/{owner}/{repo}/actions/workflows/{workflow_id}') {
        if (params.workflow_id === 'openrouter-code-review.yml') {
          return { data: { id: REVIEW_WORKFLOW_ID, path: REVIEW_CALLER_PATH, state: 'active' } };
        }
        if (params.workflow_id === 'openrouter-profile-completion.yml') {
          return {
            data: { id: COMPLETION_WORKFLOW_ID, path: COMPLETION_CALLER_PATH, state: 'active' },
          };
        }
      }
      if (route === 'GET /repos/{owner}/{repo}/actions/workflows/{workflow_id}/runs') {
        return { data: { workflow_runs: store.reviewRuns } };
      }
      throw new Error(`unhandled route ${route}`);
    },
  };

  return { github, store };
}

function authorizeContext(store, overrides = {}) {
  return {
    owner: OWNER,
    repo: REPO,
    repository: REPOSITORY,
    defaultBranch: DEFAULT_BRANCH,
    orgWorkflowSha: ORG_SHA,
    headSha: HEAD_SHA,
    pullNumber: PULL_NUMBER,
    review: store.review,
    reviewRun: store.reviewRun,
    ...overrides,
  };
}

describe('authorizeProfileReceipt', () => {
  it('authorizes a current valid proof', async () => {
    const { github, store } = buildStore();
    const result = await authorizeProfileReceipt(github, authorizeContext(store));
    assert.equal(result.authorized, true);
    assert.match(result.reason, /profile proof authorized/);
  });

  it('rejects expired PR evidence and an inconsistent initial base snapshot', async () => {
    for (const options of [{ createdAt: '2023-12-01T00:00:00Z' }, { baseSha: BASE_SHA }]) {
      const { github, store } = buildStore(options);
      const result = await authorizeProfileReceipt(github, authorizeContext(store));
      assert.equal(result.authorized, false);
    }
  });

  it('rejects a forged profile status target URL', async () => {
    const { github, store } = buildStore({
      statuses: [
        {
          id: 90,
          context: 'openrouter-profile',
          state: 'success',
          creator: trustedBot(),
          target_url: `https://github.com/${REPOSITORY.full_name}/actions/runs/999999`,
          created_at: STATUS_CREATED,
        },
      ],
    });
    const result = await authorizeProfileReceipt(github, authorizeContext(store));
    assert.equal(result.authorized, false);
    assert.match(result.reason, /live default branch head|completion run id/i);
  });

  it('rejects a forged profile status author', async () => {
    const { github, store } = buildStore({
      statuses: [
        {
          id: 90,
          context: 'openrouter-profile',
          state: 'success',
          creator: trustedBot({ id: 1 }),
          target_url: `https://github.com/${REPOSITORY.full_name}/actions/runs/${COMPLETION_RUN_ID}`,
          created_at: STATUS_CREATED,
        },
      ],
    });
    const result = await authorizeProfileReceipt(github, authorizeContext(store));
    assert.equal(result.authorized, false);
    assert.match(result.reason, /creator is not trusted/i);
  });

  it('rejects a body whose marker hash does not match the proof job', async () => {
    const { github, store } = buildStore();
    store.jobs[0].name = `complete / profile #${PULL_NUMBER} ${'9'.repeat(64)}`;
    const result = await authorizeProfileReceipt(github, authorizeContext(store));
    assert.equal(result.authorized, false);
    assert.match(result.reason, /confirmation job/i);
  });

  it('rejects a review run with the wrong org review pin', async () => {
    const { github, store } = buildStore({
      reviewRunOverrides: {
        referenced_workflows: [{ path: `${ORG_REVIEW_REUSABLE_PREFIX}${'1'.repeat(40)}`, sha: '1'.repeat(40) }],
      },
    });
    const result = await authorizeProfileReceipt(github, authorizeContext(store));
    assert.equal(result.authorized, false);
    assert.match(result.reason, /reusable workflow pin/i);
  });

  it('rejects a review run whose caller blob differs from default', async () => {
    const { github, store } = buildStore({ headCallerSha: '5'.repeat(40) });
    const result = await authorizeProfileReceipt(github, authorizeContext(store));
    assert.equal(result.authorized, false);
    assert.match(result.reason, /caller blob/i);
  });

  it('rejects a completion run not sourced from live default branch head', async () => {
    const { github, store } = buildStore({
      completionRunOverrides: { head_sha: '6'.repeat(40) },
    });
    const result = await authorizeProfileReceipt(github, authorizeContext(store));
    assert.equal(result.authorized, false);
    assert.match(result.reason, /live default branch head/i);
  });

  it('does not fall back to an older pending profile status', async () => {
    const { github, store } = buildStore({
      statuses: [
        {
          id: 100,
          context: 'openrouter-profile',
          state: 'pending',
          creator: trustedBot(),
          target_url: `https://github.com/${REPOSITORY.full_name}/actions/runs/${COMPLETION_RUN_ID}`,
          created_at: STATUS_CREATED,
        },
        {
          id: 50,
          context: 'openrouter-profile',
          state: 'success',
          creator: trustedBot(),
          target_url: `https://github.com/${REPOSITORY.full_name}/actions/runs/111`,
          created_at: '2024-01-01T00:00:00Z',
        },
      ],
    });
    const result = await authorizeProfileReceipt(github, authorizeContext(store));
    assert.equal(result.authorized, false);
    assert.match(result.reason, /pending/i);
  });

  it('rejects stale head, base, or main SHAs', async () => {
    const staleHead = buildStore({ headSha: '7'.repeat(40) });
    let result = await authorizeProfileReceipt(
      staleHead.github,
      authorizeContext(staleHead.store),
    );
    assert.equal(result.authorized, false);
    assert.match(result.reason, /head SHA mismatch/i);

    const staleBase = buildStore({ refreshBaseSha: '8'.repeat(40) });
    result = await authorizeProfileReceipt(staleBase.github, authorizeContext(staleBase.store));
    assert.equal(result.authorized, false);
    assert.match(result.reason, /identity changed/i);

    const staleMain = buildStore({ refreshMainSha: '9'.repeat(40) });
    result = await authorizeProfileReceipt(staleMain.github, authorizeContext(staleMain.store));
    assert.equal(result.authorized, false);
    assert.match(result.reason, /default branch changed/i);
  });

  it('rejects a missing or skipped proof job', async () => {
    const missing = buildStore({ jobs: [] });
    let result = await authorizeProfileReceipt(missing.github, authorizeContext(missing.store));
    assert.equal(result.authorized, false);

    const skipped = buildStore({
      jobs: [
        {
          name: `complete / profile #${PULL_NUMBER} ${missing.store.receiptDigest}`,
          status: 'completed',
          conclusion: 'skipped',
          started_at: PROOF_STARTED,
          completed_at: PROOF_COMPLETED,
        },
      ],
    });
    result = await authorizeProfileReceipt(skipped.github, authorizeContext(skipped.store));
    assert.equal(result.authorized, false);
  });

  it('rejects when org workflow SHA does not match the trusted review pin', async () => {
    const { github, store } = buildStore();
    const result = await authorizeProfileReceipt(
      github,
      authorizeContext(store, { orgWorkflowSha: 'e'.repeat(40) }),
    );
    assert.equal(result.authorized, false);
    assert.match(result.reason, /review pin/i);
  });

  it('rejects equal-second review and proof timestamps as a race', async () => {
    const { github, store } = buildStore({
      reviewRunOverrides: { updated_at: PROOF_STARTED },
    });
    const result = await authorizeProfileReceipt(github, authorizeContext(store));
    assert.equal(result.authorized, false);
    assert.match(result.reason, /finished after proof started/i);
  });

  it('blocks a new active review with empty pull_requests when head SHA matches', async () => {
    const { github, store } = buildStore({
      reviewRuns: [
        reviewRun(),
        reviewRun({
          id: 101,
          status: 'in_progress',
          conclusion: null,
          pull_requests: [],
          updated_at: '2024-01-02T09:55:00Z',
        }),
      ],
    });
    const result = await authorizeProfileReceipt(github, authorizeContext(store));
    assert.equal(result.authorized, false);
    assert.match(result.reason, /still active/i);
  });

  it('ignores unrelated pull_request runs with empty pull_requests', async () => {
    const { github, store } = buildStore({
      reviewRuns: [
        reviewRun(),
        reviewRun({
          id: 101,
          status: 'in_progress',
          conclusion: null,
          head_sha: '7'.repeat(40),
          pull_requests: [],
          updated_at: '2024-01-02T10:01:00Z',
        }),
      ],
    });
    const result = await authorizeProfileReceipt(github, authorizeContext(store));
    assert.equal(result.authorized, true);
  });

  it('allows an older branch-caller audit before a newer trusted profile proof', async () => {
    const { github, store } = buildStore({
      reviewRuns: [
        reviewRun(),
        reviewRun({ id: 101, referenced_workflows: [], updated_at: '2024-01-02T09:55:00Z' }),
      ],
    });
    const result = await authorizeProfileReceipt(github, authorizeContext(store));
    assert.equal(result.authorized, true);
  });

  for (const status of ['queued', 'in_progress', 'waiting', 'pending', 'requested']) {
    it(`blocks a matching untrusted ${status} run even when it predates the proof`, async () => {
      const { github, store } = buildStore({
        reviewRuns: [reviewRun(), reviewRun({
          id: 101, status, conclusion: null, referenced_workflows: [],
          updated_at: '2024-01-02T09:55:00Z',
        })],
      });
      const result = await authorizeProfileReceipt(github, authorizeContext(store));
      assert.equal(result.authorized, false);
      assert.match(result.reason, /still active/i);
    });
  }

  for (const updatedAt of [PROOF_STARTED, '2024-01-02T10:01:00Z', 'invalid']) {
    it(`blocks a matching untrusted completion at ${updatedAt}`, async () => {
      const { github, store } = buildStore({
        reviewRuns: [reviewRun(), reviewRun({
          id: 101, referenced_workflows: [], updated_at: updatedAt,
        })],
      });
      const result = await authorizeProfileReceipt(github, authorizeContext(store));
      assert.equal(result.authorized, false);
    });
  }

  it('does not attribute a workflow_dispatch on main via empty pull_requests', async () => {
    const { github, store } = buildStore({
      reviewRuns: [
        reviewRun(),
        reviewRun({
          id: 101,
          event: 'workflow_dispatch',
          head_branch: DEFAULT_BRANCH,
          head_sha: HEAD_SHA,
          status: 'in_progress',
          conclusion: null,
          pull_requests: [],
          updated_at: '2024-01-02T10:01:00Z',
        }),
      ],
    });
    const result = await authorizeProfileReceipt(github, authorizeContext(store));
    assert.equal(result.authorized, true);
  });

  it('rejects the wrong completion attempt jobs', async () => {
    const { github, store } = buildStore({
      completionRunOverrides: { run_attempt: 2 },
      jobs: [],
    });
    const original = github.request.bind(github);
    github.request = async (route, params = {}) => {
      if (
        route === 'GET /repos/{owner}/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs' &&
        params.attempt === 2
      ) {
        return { data: { jobs: [] } };
      }
      return original(route, params);
    };
    const result = await authorizeProfileReceipt(github, authorizeContext(store));
    assert.equal(result.authorized, false);
  });

  it('blocks when a new deep review is still active even without a receipt check path', async () => {
    const { github, store } = buildStore({
      reviewRuns: [
        reviewRun(),
        reviewRun({
          id: 101,
          status: 'in_progress',
          conclusion: null,
          display_title: `OpenRouter PR #${PULL_NUMBER}: deep`,
          updated_at: '2024-01-02T10:01:00Z',
        }),
      ],
    });
    const result = await authorizeProfileReceipt(github, authorizeContext(store));
    assert.equal(result.authorized, false);
    assert.match(result.reason, /still active/i);
  });

  it('blocks when a failed deep review completes after proof started', async () => {
    const { github, store } = buildStore({
      reviewRuns: [
        reviewRun(),
        reviewRun({
          id: 101,
          status: 'completed',
          conclusion: 'failure',
          display_title: `OpenRouter PR #${PULL_NUMBER}: deep`,
          updated_at: '2024-01-02T10:01:00Z',
        }),
      ],
    });
    const result = await authorizeProfileReceipt(github, authorizeContext(store));
    assert.equal(result.authorized, false);
    assert.match(result.reason, /completed after proof started/i);
  });

  it('ignores unrelated active review runs once provenance fails', async () => {
    const { github, store } = buildStore({
      reviewRuns: [
        reviewRun(),
        reviewRun({
          id: 101,
          status: 'in_progress',
          conclusion: null,
          display_title: 'OpenRouter PR #99: deep',
          pull_requests: [{ number: 99 }],
          updated_at: '2024-01-02T10:01:00Z',
        }),
      ],
    });
    const result = await authorizeProfileReceipt(github, authorizeContext(store));
    assert.equal(result.authorized, true);
  });
});

describe('completionPullRequests', () => {
  it('returns only PR numbers from successful completed proof jobs', async () => {
    const { github, store } = buildStore();
    const digest = store.receiptDigest;
    store.jobs = [
      {
        name: `complete / profile #7 ${digest}`,
        status: 'completed',
        conclusion: 'success',
        started_at: PROOF_STARTED,
        completed_at: PROOF_COMPLETED,
      },
      {
        name: `complete / profile #8 ${digest}`,
        status: 'completed',
        conclusion: 'failure',
        started_at: PROOF_STARTED,
        completed_at: PROOF_COMPLETED,
      },
      {
        name: `complete / profile #9 ${digest}`,
        status: 'completed',
        conclusion: 'skipped',
        started_at: PROOF_STARTED,
        completed_at: PROOF_COMPLETED,
      },
      {
        name: 'complete / unrelated',
        status: 'completed',
        conclusion: 'success',
        started_at: PROOF_STARTED,
        completed_at: PROOF_COMPLETED,
      },
    ];
    const result = await completionPullRequests(github, {
      owner: OWNER,
      repo: REPO,
      repository: REPOSITORY,
      defaultBranch: DEFAULT_BRANCH,
      orgWorkflowSha: ORG_SHA,
      run: store.completionRun,
    });
    assert.deepEqual(result, [7]);
  });

  it('returns an empty list when completion provenance fails', async () => {
    const { github, store } = buildStore({
      completionRunOverrides: {
        referenced_workflows: [{ path: `${ORG_COMPLETION_REUSABLE_PREFIX}${'2'.repeat(40)}`, sha: '2'.repeat(40) }],
      },
    });
    const result = await completionPullRequests(github, {
      owner: OWNER,
      repo: REPO,
      repository: REPOSITORY,
      defaultBranch: DEFAULT_BRANCH,
      orgWorkflowSha: ORG_SHA,
      run: store.completionRun,
    });
    assert.deepEqual(result, []);
  });
});
