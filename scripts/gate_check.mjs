// Publishes the first-pass gate result as a check run on the reviewed PR head.
//
// Default-branch dispatch runs (workflow_dispatch, repository_dispatch) attach
// their native job checks to the default-branch tip, so a required
// `review / openrouter-first-pass-gate` check never reaches the PR head. This
// helper repeats the gate's own conclusion on the exact head the gate evaluated.
// Pull-request runs (including pull_request_target) already attach their native
// job checks to the PR head; in `auto` mode they are left alone to avoid
// duplicate check runs with the same name.

export const DEFAULT_GATE_CHECK_NAME = 'review / openrouter-first-pass-gate';
export const GITHUB_ACTIONS_APP_ID = 15368;
const SHA_RE = /^[0-9a-f]{40}$/;
const PR_RE = /^[1-9][0-9]{0,9}$/;
const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9 ._/#:()-]{0,99}$/;
const MODES = new Set(['auto', 'always']);
const PULL_REQUEST_EVENTS = new Set(['pull_request', 'pull_request_target']);

function fail(message) {
  throw new Error(message);
}

export function normalizeGateInputs({ prNumber, headSha, conclusion, reviewUrl, name, mode }, repository) {
  const number = String(prNumber ?? '');
  if (!PR_RE.test(number) || !Number.isSafeInteger(Number(number))) fail('pr_number must be a positive integer');
  const sha = String(headSha ?? '');
  if (!SHA_RE.test(sha)) fail('head_sha must be a lowercase full commit SHA');
  const checkName = name === undefined || name === '' ? DEFAULT_GATE_CHECK_NAME : String(name);
  if (!NAME_RE.test(checkName)) fail('gate_check_name is invalid');
  const publishMode = mode === undefined || mode === '' ? 'auto' : String(mode);
  if (!MODES.has(publishMode)) fail('publish_mode must be auto or always');
  // Anything other than an explicit success is a failed gate (fail closed).
  const result = conclusion === 'success' ? 'success' : 'failure';
  const reviewPattern = new RegExp(
    `^https://github\\.com/${repository.replace(/[.]/g, '\\.')}/pull/${number}#pullrequestreview-[1-9][0-9]*$`,
  );
  const review = typeof reviewUrl === 'string' && reviewPattern.test(reviewUrl) ? reviewUrl : '';
  return { prNumber: Number(number), headSha: sha, conclusion: result, reviewUrl: review, name: checkName, mode: publishMode };
}

async function existingCheckRun(github, { owner, repo, headSha, name, externalId }) {
  for (let page = 1; page <= 10; page += 1) {
    const { data } = await github.request('GET /repos/{owner}/{repo}/commits/{ref}/check-runs', {
      owner, repo, ref: headSha, check_name: name, filter: 'all', per_page: 100, page,
    });
    const runs = Array.isArray(data?.check_runs) ? data.check_runs : fail('check-run listing is invalid');
    const match = runs.find((run) => run?.external_id === externalId && run?.app?.id === GITHUB_ACTIONS_APP_ID);
    if (match) return match;
    if (runs.length < 100) return null;
  }
  fail('check-run listing exceeds its bound');
}

/**
 * Returns { published, reason, checkRunId? }. Never publishes for a PR head
 * other than the one the gate evaluated, and only from default-branch runs.
 */
export async function publishGateCheck(github, core, context, inputs) {
  const { owner, repo } = context.repo;
  const repository = `${owner}/${repo}`;
  const gate = normalizeGateInputs(inputs, repository);
  const { data: repoData } = await github.request('GET /repos/{owner}/{repo}', { owner, repo });
  const defaultBranch = repoData?.default_branch;
  if (repoData?.full_name !== repository || typeof defaultBranch !== 'string' || !defaultBranch) {
    fail('repository identity is invalid');
  }
  // A PR-branch caller (pull_request) cannot borrow this publisher: only
  // workflows that GitHub ran from the default branch may publish.
  if (context.ref !== `refs/heads/${defaultBranch}`) {
    return skip(core, 'only default-branch callers publish head-bound gate checks');
  }
  if (!Number.isSafeInteger(context.runId) || context.runId <= 0) fail('invalid workflow run id');
  const { data: run } = await github.request('GET /repos/{owner}/{repo}/actions/runs/{run_id}', {
    owner, repo, run_id: context.runId,
  });
  if (run?.id !== context.runId || run?.repository?.full_name !== repository) fail('workflow run identity is invalid');
  // Native job checks attach to the run's head. For PR events that is already
  // the PR head; for default-branch dispatches it is the default-branch tip.
  const nativeCoversHead = PULL_REQUEST_EVENTS.has(run.event) || run.head_sha === gate.headSha;
  if (gate.mode === 'auto' && nativeCoversHead) {
    return skip(core, 'the native gate job check already reports on the pull request head');
  }
  const { data: pr } = await github.request('GET /repos/{owner}/{repo}/pulls/{pull_number}', {
    owner, repo, pull_number: gate.prNumber,
  });
  if (pr?.number !== gate.prNumber || pr?.base?.repo?.full_name !== repository) {
    fail('pull request does not belong to this repository');
  }
  if (pr.head?.sha !== gate.headSha) {
    // The newer head is evaluated by its own run; never move this result onto it.
    return skip(core, `pull request head moved from ${gate.headSha}; leaving the newer head to its own run`);
  }
  const runUrl = `${context.serverUrl}/${repository}/actions/runs/${context.runId}`;
  const externalId = `openrouter-first-pass-gate:${context.runId}`;
  const passed = gate.conclusion === 'success';
  const lines = [
    passed
      ? `The OpenRouter first-pass gate passed for \`${gate.headSha}\`.`
      : `The OpenRouter first-pass gate did not pass for \`${gate.headSha}\`.`,
    '',
    `Evaluated by [workflow run ${context.runId}](${runUrl}) (${run.event}).`,
  ];
  if (gate.reviewUrl) lines.push(`Review: ${gate.reviewUrl}`);
  const summary = lines.join('\n');
  const payload = {
    owner,
    repo,
    name: gate.name,
    head_sha: gate.headSha,
    status: 'completed',
    conclusion: gate.conclusion,
    completed_at: new Date().toISOString(),
    details_url: runUrl,
    external_id: externalId,
    output: {
      title: passed ? 'OpenRouter first-pass gate passed' : 'OpenRouter first-pass gate failed',
      summary,
    },
  };
  const existing = await existingCheckRun(github, { owner, repo, headSha: gate.headSha, name: gate.name, externalId });
  let data;
  if (existing) {
    ({ data } = await github.request('PATCH /repos/{owner}/{repo}/check-runs/{check_run_id}', {
      ...payload, check_run_id: existing.id,
    }));
  } else {
    ({ data } = await github.request('POST /repos/{owner}/{repo}/check-runs', payload));
  }
  if (data?.head_sha !== gate.headSha || data?.name !== gate.name) fail('check run response does not match the request');
  core.info(`Published ${gate.name}=${gate.conclusion} on ${gate.headSha} (check run ${data.id}).`);
  return { published: true, reason: 'published', checkRunId: data.id };
}

function skip(core, reason) {
  core.info(`Gate check not published: ${reason}.`);
  return { published: false, reason };
}
