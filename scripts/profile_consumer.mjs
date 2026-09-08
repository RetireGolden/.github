/**
 * Standalone profile proof consumer for default-branch CI acceleration.
 *
 * Loaded from the trusted org pin via getContent; performs GitHub provenance and
 * receipt binding only (no policy parsing or artifact downloads).
 */

import { createHash } from 'node:crypto';

export const TRUSTED_REVIEW_AUTHOR = 'github-actions[bot]';
export const TRUSTED_REVIEW_AUTHOR_ID = 41898282;
export const TRUSTED_REVIEW_AUTHOR_TYPE = 'Bot';
export const PROFILE_STATUS_CONTEXT = 'openrouter-profile';
export const REVIEW_CALLER_PATH = '.github/workflows/openrouter-code-review.yml';
export const COMPLETION_CALLER_PATH = '.github/workflows/openrouter-profile-completion.yml';
export const REVIEW_WORKFLOW_NAME = 'OpenRouter code review';
export const COMPLETION_WORKFLOW_NAME = 'OpenRouter profile completion';
export const ORG_REVIEW_REUSABLE_PREFIX =
  'RetireGolden/.github/.github/workflows/openrouter-code-review.yml@';
export const ORG_COMPLETION_REUSABLE_PREFIX =
  'RetireGolden/.github/.github/workflows/openrouter-profile-completion.yml@';
export const RECEIPT_MARKER_PREFIX = '<!-- openrouter-review-plan:v1:';
export const REVIEW_HEADING = '## OpenRouter pull-request review';
export const MAX_PAGINATION_ITEMS = 1000;
export const MAX_RECEIPT_BYTES = 16 * 1024;
export const MAX_BODY_BYTES = 60_000;

const RECEIPT_KEYS = [
  'version',
  'repository',
  'pr_number',
  'head_sha',
  'policy_base_sha',
  'policy_digest',
  'profile',
  'level',
  'trigger',
  'registry_digest',
  'context_sha256',
  'required_models',
  'successful_models',
  'panel_status',
  'profile_satisfied',
  'verdict',
  'scope',
  'mode',
  'run_url',
  'run_attempt',
];

const SHA_RE = /^[0-9a-f]{40}$/;
const DIGEST_RE = /^[0-9a-f]{64}$/;
const REPOSITORY_RE = /^[^/\s]{1,100}\/[^/\s]{1,100}$/;
const PROFILE_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;
const MODEL_RE = /^[A-Za-z0-9][A-Za-z0-9._-]*\/[A-Za-z0-9][A-Za-z0-9._-]*(?::[A-Za-z0-9._-]+)?$/;
const RUN_ID_RE = /\/actions\/runs\/([1-9][0-9]*)$/;
const REVIEW_PIN_RE =
  /RetireGolden\/\.github\/\.github\/workflows\/openrouter-code-review\.yml@([0-9a-f]{40})(?![0-9a-f])/;
const DISPATCH_TITLE_RE = /^OpenRouter PR #([1-9][0-9]*): (auto|deep|cancel)$/;
const PROOF_JOB_RE = /^complete \/ profile #([1-9][0-9]*) ([0-9a-f]{64})$/;
const COMPLETION_EVENTS = new Set(['workflow_run', 'push', 'workflow_dispatch']);
const REVIEW_EVENTS = new Set(['pull_request', 'workflow_dispatch']);

function unauthorized(reason) {
  return { authorized: false, reason };
}

function authorized(reason) {
  return { authorized: true, reason };
}

function canonicalSha(value, label) {
  if (typeof value !== 'string' || !SHA_RE.test(value)) {
    throw new Error(`${label} is not a canonical SHA`);
  }
  return value.toLowerCase();
}

function positiveInt(value, label) {
  if (!Number.isInteger(value) || value <= 0) {
    throw new Error(`${label} must be a positive integer`);
  }
  return value;
}

function githubMs(value, label) {
  if (typeof value !== 'string' || !value) {
    throw new Error(`${label} is missing`);
  }
  const normalized = value.endsWith('Z') ? `${value.slice(0, -1)}+00:00` : value;
  const parsed = Date.parse(normalized);
  if (!Number.isFinite(parsed)) {
    throw new Error(`${label} is not a valid timestamp`);
  }
  return parsed;
}

function decodeContent(data) {
  if (!data || typeof data !== 'object' || data.type !== 'file' || typeof data.content !== 'string') {
    throw new Error('caller content is invalid');
  }
  const text = Buffer.from(data.content.replace(/\n/g, ''), 'base64').toString('utf8');
  return { sha: canonicalSha(data.sha, 'caller blob'), text };
}

function extractReviewPin(text) {
  const matches = [...text.matchAll(new RegExp(REVIEW_PIN_RE.source, 'g'))].map((match) => match[1]);
  if (matches.length !== 1) {
    throw new Error('trusted caller must contain exactly one review workflow pin');
  }
  return matches[0];
}

async function readCallerBlob(github, owner, repo, ref) {
  const { data } = await github.request('GET /repos/{owner}/{repo}/contents/{path}', {
    owner,
    repo,
    path: REVIEW_CALLER_PATH,
    ref,
  });
  return decodeContent(data);
}

async function readCompletionCallerBlob(github, owner, repo, ref) {
  const { data } = await github.request('GET /repos/{owner}/{repo}/contents/{path}', {
    owner,
    repo,
    path: COMPLETION_CALLER_PATH,
    ref,
  });
  return decodeContent(data);
}

async function paginate(github, route, parameters, listKey) {
  const items = [];
  let page = 1;
  while (true) {
    const response = await github.request(route, { ...parameters, per_page: 100, page });
    const chunk = listKey ? response.data[listKey] : response.data;
    if (!Array.isArray(chunk)) {
      throw new Error('pagination response lacks a list');
    }
    if (items.length + chunk.length > MAX_PAGINATION_ITEMS) {
      throw new Error('pagination exceeds its item cap');
    }
    items.push(...chunk);
    if (chunk.length < 100) {
      return items;
    }
    page += 1;
  }
}

async function liveMainSha(github, owner, repo, defaultBranch) {
  const { data: repository } = await github.request('GET /repos/{owner}/{repo}', { owner, repo });
  const branch =
    typeof repository?.default_branch === 'string' && repository.default_branch
      ? repository.default_branch
      : defaultBranch;
  if (typeof branch !== 'string' || !branch) {
    throw new Error('live repository lacks a default branch');
  }
  const { data: ref } = await github.request('GET /repos/{owner}/{repo}/git/ref/{ref}', {
    owner,
    repo,
    ref: `heads/${branch}`,
  });
  return { branch, mainSha: canonicalSha(ref?.object?.sha, 'live default branch SHA') };
}

function sameRepository(run, repository) {
  return (
    run?.repository?.full_name === repository.full_name &&
    run?.head_repository?.full_name === repository.full_name
  );
}

function reusablePinned(run, expectedPath, expectedSha) {
  if (!Array.isArray(run?.referenced_workflows)) {
    return false;
  }
  return run.referenced_workflows.some(
    (entry) => entry?.path === expectedPath && entry?.sha === expectedSha,
  );
}

async function activeWorkflow(github, owner, repo, path) {
  const file = path.split('/').pop();
  const { data } = await github.request('GET /repos/{owner}/{repo}/actions/workflows/{workflow_id}', {
    owner,
    repo,
    workflow_id: file,
  });
  if (data?.path !== path || data?.state !== 'active' || !Number.isInteger(data?.id) || data.id <= 0) {
    throw new Error('live workflow is not the trusted active caller');
  }
  return data.id;
}

async function verifyReviewRunProvenance(
  github,
  { owner, repo, repository, defaultBranch, reviewPin, trustedCallerSha },
  run,
) {
  if (!sameRepository(run, repository)) {
    throw new Error('review run is not from the trusted repository');
  }
  if (run.path !== REVIEW_CALLER_PATH) {
    throw new Error('review run path is not the trusted caller');
  }
  if (!REVIEW_EVENTS.has(run.event)) {
    throw new Error('review run event is not trusted');
  }
  if (run.event === 'workflow_dispatch' && run.head_branch !== defaultBranch) {
    throw new Error('dispatch review run is not on the default branch');
  }
  const workflowId = await activeWorkflow(github, owner, repo, REVIEW_CALLER_PATH);
  if (run.workflow_id !== workflowId) {
    throw new Error('review run workflow id does not match the active caller');
  }
  const expectedReusable = `${ORG_REVIEW_REUSABLE_PREFIX}${reviewPin}`;
  if (!reusablePinned(run, expectedReusable, reviewPin)) {
    throw new Error('review run lacks the exact trusted reusable workflow pin');
  }
  const headBlob = await readCallerBlob(github, owner, repo, run.head_sha);
  if (headBlob.sha !== trustedCallerSha) {
    throw new Error('review run caller blob differs from trusted default caller');
  }
}

async function verifyCompletionRunProvenance(
  github,
  { owner, repo, repository, defaultBranch, orgWorkflowSha, trustedCompletionCallerSha, liveMainSha },
  run,
) {
  if (!sameRepository(run, repository)) {
    throw new Error('completion run is not from the trusted repository');
  }
  if (run.path !== COMPLETION_CALLER_PATH || run.name !== COMPLETION_WORKFLOW_NAME) {
    throw new Error('completion run path or name is not trusted');
  }
  if (!COMPLETION_EVENTS.has(run.event)) {
    throw new Error('completion run event is not trusted');
  }
  if (run.head_branch !== defaultBranch) {
    throw new Error('completion run is not on the default branch');
  }
  if (canonicalSha(run.head_sha, 'completion head SHA') !== liveMainSha) {
    throw new Error('completion run is not at live default branch head');
  }
  const workflowId = await activeWorkflow(github, owner, repo, COMPLETION_CALLER_PATH);
  if (run.workflow_id !== workflowId) {
    throw new Error('completion run workflow id does not match the active caller');
  }
  const expectedReusable = `${ORG_COMPLETION_REUSABLE_PREFIX}${orgWorkflowSha}`;
  if (!reusablePinned(run, expectedReusable, orgWorkflowSha)) {
    throw new Error('completion run lacks the exact trusted reusable workflow pin');
  }
  const headBlob = await readCompletionCallerBlob(github, owner, repo, run.head_sha);
  if (headBlob.sha !== trustedCompletionCallerSha) {
    throw new Error('completion run caller blob differs from trusted default caller');
  }
}

function trustedReviewAuthor(user) {
  return (
    user?.login === TRUSTED_REVIEW_AUTHOR &&
    user?.id === TRUSTED_REVIEW_AUTHOR_ID &&
    user?.type === TRUSTED_REVIEW_AUTHOR_TYPE
  );
}

function strictReceiptObject(raw) {
  if (raw.length > MAX_RECEIPT_BYTES) {
    throw new Error('receipt exceeds its size bound');
  }
  let text;
  try {
    text = new TextDecoder('utf-8', { fatal: true }).decode(raw);
  } catch {
    throw new Error('receipt is not UTF-8');
  }
  let value;
  try {
    value = JSON.parse(text);
  } catch {
    throw new Error('receipt is not strict JSON');
  }
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('receipt must be an object');
  }
  const keys = Object.keys(value);
  if (keys.length !== RECEIPT_KEYS.length || !RECEIPT_KEYS.every((key) => keys.includes(key))) {
    throw new Error('receipt keys are not exactly receipt v1 keys');
  }
  return value;
}

function validateReceipt(value, bindings, raw) {
  if (value.version !== 1) {
    throw new Error('unsupported receipt version');
  }
  if (typeof value.repository !== 'string' || !REPOSITORY_RE.test(value.repository)) {
    throw new Error('repository must be owner/name');
  }
  if (!Number.isInteger(value.pr_number) || value.pr_number < 1) {
    throw new Error('pr_number must be positive');
  }
  if (typeof value.head_sha !== 'string' || !SHA_RE.test(value.head_sha)) {
    throw new Error('head_sha must be a lowercase full SHA');
  }
  if (typeof value.policy_digest !== 'string' || value.policy_digest.length > 64) {
    throw new Error('policy_digest is invalid');
  }
  if (typeof value.policy_base_sha !== 'string' || value.policy_base_sha.length > 40) {
    throw new Error('policy_base_sha is invalid');
  }
  if (typeof value.registry_digest !== 'string' || !DIGEST_RE.test(value.registry_digest)) {
    throw new Error('registry_digest must be a lowercase SHA-256');
  }
  if (typeof value.context_sha256 !== 'string' || !DIGEST_RE.test(value.context_sha256)) {
    throw new Error('context_sha256 must be a lowercase SHA-256');
  }
  if (typeof value.profile !== 'string' || !PROFILE_RE.test(value.profile)) {
    throw new Error('profile is invalid');
  }
  const levelTrigger = `${value.level}:${value.trigger}`;
  if (
    !['standard:baseline', 'deep:manual', 'deep:policy'].includes(levelTrigger) ||
    typeof value.level !== 'string' ||
    typeof value.trigger !== 'string'
  ) {
    throw new Error('level and trigger are incoherent');
  }
  const models = (field) => {
    const list = value[field];
    if (!Array.isArray(list) || list.length > 4 || list.length === 0) {
      throw new Error(`${field} must be a non-empty list of at most four models`);
    }
    const slugs = list.map((item) => {
      if (typeof item !== 'string' || !MODEL_RE.test(item)) {
        throw new Error(`${field} contains an invalid model slug`);
      }
      return item;
    });
    if (new Set(slugs).size !== slugs.length) {
      throw new Error(`${field} must not contain duplicates`);
    }
    return slugs;
  };
  const required = models('required_models');
  const successful = models('successful_models');
  if (!['complete', 'degraded', 'required_missing', 'error'].includes(value.panel_status)) {
    throw new Error('panel_status is invalid');
  }
  if (typeof value.profile_satisfied !== 'boolean') {
    throw new Error('profile_satisfied must be a boolean');
  }
  if (!['clean', 'issues', 'partial', 'error'].includes(value.verdict)) {
    throw new Error('verdict is invalid');
  }
  if (!['full-pr', 'latest-commit'].includes(value.scope)) {
    throw new Error('scope is invalid');
  }
  if (!['initial', 'verify'].includes(value.mode)) {
    throw new Error('mode is invalid');
  }
  const runUrl = value.run_url;
  const runUrlRe = new RegExp(
    `^https://[A-Za-z0-9][A-Za-z0-9.-]*/${value.repository.replace('/', '\\/')}/actions/runs/[1-9][0-9]*$`,
  );
  if (typeof runUrl !== 'string' || !runUrlRe.test(runUrl)) {
    throw new Error('run_url is not a source workflow-run URL for the repository');
  }
  if (!Number.isInteger(value.run_attempt) || value.run_attempt < 1 || value.run_attempt > 1000) {
    throw new Error('run_attempt must be from 1 through 1000');
  }

  if (value.verdict !== 'clean' || value.profile_satisfied !== true) {
    throw new Error('receipt is not a satisfied clean review');
  }
  if (!['complete', 'degraded'].includes(value.panel_status)) {
    throw new Error('panel status is not allowed');
  }
  if (!required.every((model) => successful.includes(model))) {
    throw new Error('satisfied receipt lacks required successful models');
  }

  const {
    repository: repositoryName,
    pullNumber,
    headSha,
    reviewRunUrl,
    reviewRunAttempt,
  } = bindings;
  if (value.repository !== repositoryName) {
    throw new Error('repository mismatch');
  }
  if (value.pr_number !== pullNumber) {
    throw new Error('pull request mismatch');
  }
  if (value.head_sha !== headSha) {
    throw new Error('head SHA mismatch');
  }
  if (value.run_url !== reviewRunUrl) {
    throw new Error('run_url mismatch');
  }
  if (value.run_attempt !== reviewRunAttempt) {
    throw new Error('run_attempt mismatch');
  }
  return { receiptDigest: createHash('sha256').update(raw).digest('hex'), receipt: value };
}

function parseReceiptMarker(body, bindings) {
  if (typeof body !== 'string' || body.length > MAX_BODY_BYTES) {
    throw new Error('review body is invalid');
  }
  const lines = body.split(/\r?\n/);
  if (lines[0] !== REVIEW_HEADING) {
    throw new Error('review body lacks the trusted heading');
  }
  const commitIndex = lines.findIndex((line) => line.startsWith('**Commit:** `'));
  const lanesIndex = lines.findIndex((line) => line === '### Lanes');
  if (commitIndex < 0 || lanesIndex < 0 || commitIndex >= lanesIndex) {
    throw new Error('review body layout is invalid');
  }
  const markerLines = lines
    .map((line, index) => ({ line, index }))
    .filter(({ line }) => line.startsWith(RECEIPT_MARKER_PREFIX));
  if (markerLines.length !== 1) {
    throw new Error('review body must contain exactly one receipt marker');
  }
  const { line: marker, index: markerIndex } = markerLines[0];
  if (!(commitIndex < markerIndex && markerIndex < lanesIndex) || !marker.endsWith(' -->')) {
    throw new Error('receipt marker is not after Commit and before Lanes');
  }
  const encoded = marker.slice(RECEIPT_MARKER_PREFIX.length, -4);
  if (!/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(encoded)) {
    throw new Error('receipt marker is not strict base64');
  }
  let raw;
  try {
    raw = Buffer.from(encoded, 'base64');
    if (Buffer.from(raw).toString('base64') !== encoded) {
      throw new Error('invalid base64');
    }
  } catch {
    throw new Error('receipt marker is not strict base64');
  }
  const receiptObject = strictReceiptObject(raw);
  const { receiptDigest, receipt } = validateReceipt(receiptObject, bindings, raw);
  const workflowLink = `[Workflow run](${receipt.run_url})`;
  if (body.split(workflowLink).length - 1 !== 1) {
    throw new Error('review body must contain the exact workflow run link');
  }
  return { raw, receiptDigest, receipt };
}

async function livePullRequest(github, owner, repo, pullNumber, defaultBranch, repository) {
  const { data: pr } = await github.request('GET /repos/{owner}/{repo}/pulls/{pull_number}', {
    owner,
    repo,
    pull_number: pullNumber,
  });
  if (
    pr.state !== 'open' ||
    pr.draft === true ||
    pr.head?.repo?.full_name !== repository.full_name ||
    pr.base?.repo?.full_name !== repository.full_name ||
    pr.base?.ref !== defaultBranch
  ) {
    throw new Error('pull request is not an open same-repository default-branch PR');
  }
  return {
    pr,
    headSha: canonicalSha(pr.head?.sha, 'live pull request head SHA'),
    baseSha: canonicalSha(pr.base?.sha, 'live pull request base SHA'),
    createdAt: pr.created_at,
  };
}

async function latestProfileStatus(github, owner, repo, headSha, repository) {
  const statuses = await paginate(
    github,
    'GET /repos/{owner}/{repo}/commits/{ref}/statuses',
    { owner, repo, ref: headSha },
  );
  const ordered = [...statuses].sort((left, right) => {
    const leftId = Number.isInteger(left.id) ? left.id : -Infinity;
    const rightId = Number.isInteger(right.id) ? right.id : -Infinity;
    return rightId - leftId;
  });
  const latest = ordered.find((status) => status.context === PROFILE_STATUS_CONTEXT);
  if (!latest) {
    throw new Error('openrouter-profile status is missing');
  }
  if (!trustedReviewAuthor(latest.creator)) {
    throw new Error('openrouter-profile status creator is not trusted');
  }
  if (latest.state === 'pending') {
    throw new Error('openrouter-profile status is pending');
  }
  if (latest.state !== 'success') {
    throw new Error('openrouter-profile status is not successful');
  }
  const target = latest.target_url;
  const expectedPrefix = `https://github.com/${repository.full_name}/actions/runs/`;
  if (typeof target !== 'string' || !target.startsWith(expectedPrefix)) {
    throw new Error('openrouter-profile status target is not a trusted run URL');
  }
  const match = RUN_ID_RE.exec(target);
  if (!match) {
    throw new Error('openrouter-profile status target is not a trusted run URL');
  }
  return { status: latest, runId: Number(match[1]) };
}

async function exactAttemptJobs(github, owner, repo, runId, attempt) {
  return paginate(github, 'GET /repos/{owner}/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs', {
    owner,
    repo,
    run_id: runId,
    attempt,
  }, 'jobs');
}

function proofJobFor(jobs, pullNumber, receiptDigest) {
  const expectedName = `complete / profile #${pullNumber} ${receiptDigest}`;
  const matches = jobs.filter((job) => job?.name === expectedName);
  if (matches.length !== 1) {
    throw new Error('required confirmation job did not succeed exactly once');
  }
  const job = matches[0];
  if (job.status !== 'completed' || job.conclusion !== 'success') {
    throw new Error('required confirmation job did not succeed exactly once');
  }
  const startedAt = githubMs(job.started_at, 'proof started_at');
  const completedAt = githubMs(job.completed_at, 'proof completed_at');
  if (completedAt < startedAt) {
    throw new Error('proof job timestamps are invalid');
  }
  return { job, startedAt, completedAt };
}

function runMatchesPull(run, pullNumber, headSha) {
  if (run.event === 'workflow_dispatch') {
    const titleMatch =
      typeof run.display_title === 'string' ? DISPATCH_TITLE_RE.exec(run.display_title) : null;
    return Boolean(titleMatch && Number(titleMatch[1]) === pullNumber);
  }
  const titleMatch =
    typeof run.display_title === 'string' ? DISPATCH_TITLE_RE.exec(run.display_title) : null;
  if (titleMatch && Number(titleMatch[1]) === pullNumber) {
    return true;
  }
  if (Array.isArray(run.pull_requests)) {
    if (run.pull_requests.length === 0) {
      return run.head_sha === headSha;
    }
    return run.pull_requests.some(
      (entry) => Number.isInteger(entry?.number) && entry.number === pullNumber,
    );
  }
  return run.head_sha === headSha;
}

async function reviewRaceBlocksAuthorization(
  github,
  context,
  { pullNumber, headSha, createdAt, proofStartedAt, currentReviewRun },
) {
  const { owner, repo } = context;
  if (currentReviewRun.status !== 'completed') {
    throw new Error('current review run is not completed');
  }
  const currentUpdatedAt = githubMs(currentReviewRun.updated_at, 'current review updated_at');
  if (currentUpdatedAt >= proofStartedAt) {
    throw new Error('current review run finished after proof started');
  }

  const runs = await paginate(
    github,
    'GET /repos/{owner}/{repo}/actions/workflows/{workflow_id}/runs',
    {
      owner,
      repo,
      workflow_id: 'openrouter-code-review.yml',
      created: `>=${createdAt}`,
    },
    'workflow_runs',
  );

  for (const run of runs) {
    if (!runMatchesPull(run, pullNumber, headSha)) {
      continue;
    }
    // This scan only invalidates proof; it never authorizes a review. The
    // authoritative review and completed proof have already passed provenance.
    // Older branch-caller audits are expected during a workflow-pin migration;
    // they are not races once they finish before that trusted proof starts.
    // Any matching active or later-completing run still blocks authorization,
    // including runs that would fail provenance themselves.
    if (run.status !== 'completed') {
      throw new Error('a newer matching review run is still active');
    }
    const updatedAt = githubMs(run.updated_at, 'review run updated_at');
    if (updatedAt >= proofStartedAt) {
      throw new Error('a matching review run completed after proof started');
    }
  }
}

export async function authorizeProfileReceipt(
  github,
  {
    owner,
    repo,
    repository,
    defaultBranch,
    orgWorkflowSha,
    headSha,
    pullNumber,
    review,
    reviewRun,
  },
) {
  try {
    positiveInt(pullNumber, 'pullNumber');
    const expectedHead = canonicalSha(headSha, 'headSha');
    canonicalSha(orgWorkflowSha, 'orgWorkflowSha');
    if (!repository?.full_name) {
      return unauthorized('repository is missing');
    }
    if (!trustedReviewAuthor(review?.user)) {
      return unauthorized('review author is not trusted');
    }
    if (review.commit_id !== expectedHead) {
      return unauthorized('review commit_id does not match head SHA');
    }
    if (!reviewRun || !Number.isInteger(reviewRun.id) || reviewRun.id <= 0) {
      return unauthorized('review run is missing');
    }
    const reviewRunUrl = `https://github.com/${repository.full_name}/actions/runs/${reviewRun.id}`;
    const reviewRunAttempt = positiveInt(reviewRun.run_attempt, 'review run attempt');
    const { receiptDigest, receipt } = parseReceiptMarker(review.body ?? '', {
      repository: repository.full_name,
      pullNumber,
      headSha: expectedHead,
      reviewRunUrl,
      reviewRunAttempt,
    });

    const { mainSha, branch } = await liveMainSha(github, owner, repo, defaultBranch);
    const trustedReviewCaller = await readCallerBlob(github, owner, repo, mainSha);
    const reviewPin = extractReviewPin(trustedReviewCaller.text);
    if (reviewPin !== orgWorkflowSha) {
      return unauthorized('org workflow SHA does not match trusted review pin');
    }
    const trustedCompletionCaller = await readCompletionCallerBlob(github, owner, repo, mainSha);
    const livePr = await livePullRequest(github, owner, repo, pullNumber, branch, repository);
    if (livePr.headSha !== expectedHead) {
      return unauthorized('live pull request head SHA mismatch');
    }
    if (livePr.baseSha !== mainSha) {
      return unauthorized('live pull request base does not match current default branch');
    }
    const ageMs = Date.now() - githubMs(livePr.createdAt, 'pull request creation');
    if (ageMs < -300_000 || ageMs >= 25 * 24 * 60 * 60 * 1000) {
      return unauthorized('profile evidence requires a PR younger than 25 days');
    }

    const provenanceContext = {
      owner,
      repo,
      repository,
      defaultBranch: branch,
      reviewPin,
      trustedCallerSha: trustedReviewCaller.sha,
      orgWorkflowSha,
      trustedCompletionCallerSha: trustedCompletionCaller.sha,
      liveMainSha: mainSha,
    };

    await verifyReviewRunProvenance(github, provenanceContext, reviewRun);
    if (reviewRun.status !== 'completed' || reviewRun.conclusion !== 'success') {
      return unauthorized('review run is not successful');
    }
    if (receipt.run_url !== reviewRunUrl || receipt.run_attempt !== reviewRunAttempt) {
      return unauthorized('receipt does not bind to the supplied review run');
    }

    const { runId: completionRunId, status: profileStatus } = await latestProfileStatus(
      github,
      owner,
      repo,
      livePr.headSha,
      repository,
    );
    const { data: completionRun } = await github.request(
      'GET /repos/{owner}/{repo}/actions/runs/{run_id}',
      { owner, repo, run_id: completionRunId },
    );
    if (!Number.isInteger(completionRun?.id) || completionRun.id !== completionRunId) {
      return unauthorized('completion run id does not match profile status');
    }
    await verifyCompletionRunProvenance(github, provenanceContext, completionRun);
    if (completionRun.status !== 'completed') {
      return unauthorized('completion run is not completed');
    }

    const attempt = positiveInt(completionRun.run_attempt, 'completion run attempt');
    const jobs = await exactAttemptJobs(github, owner, repo, completionRunId, attempt);
    const { startedAt: proofStartedAt, completedAt: proofCompletedAt } = proofJobFor(
      jobs,
      pullNumber,
      receiptDigest,
    );

    const statusCreatedAt = githubMs(profileStatus.created_at, 'status created_at');
    if (statusCreatedAt <= proofCompletedAt) {
      return unauthorized('published profile status predates proof completion');
    }

    await reviewRaceBlocksAuthorization(github, provenanceContext, {
      pullNumber,
      headSha: livePr.headSha,
      createdAt: livePr.createdAt,
      proofStartedAt,
      currentReviewRun: reviewRun,
    });

    const refreshedMain = await liveMainSha(github, owner, repo, branch);
    const refreshedPr = await livePullRequest(github, owner, repo, pullNumber, branch, repository);
    if (refreshedMain.mainSha !== mainSha) {
      return unauthorized('default branch changed during authorization');
    }
    if (refreshedPr.headSha !== expectedHead || refreshedPr.baseSha !== livePr.baseSha) {
      return unauthorized('pull request identity changed during authorization');
    }

    return authorized(`profile proof authorized for PR #${pullNumber} at ${expectedHead}`);
  } catch (error) {
    const reason =
      error instanceof Error && error.message ? error.message : 'profile authorization failed';
    return unauthorized(reason.replace(/[\r\n]/g, ' ').slice(0, 500));
  }
}

export async function completionPullRequests(
  github,
  { owner, repo, repository, defaultBranch, orgWorkflowSha, run },
) {
  try {
    canonicalSha(orgWorkflowSha, 'orgWorkflowSha');
    if (!repository?.full_name || !run || !Number.isInteger(run.id) || run.id <= 0) {
      return [];
    }
    const { mainSha, branch } = await liveMainSha(github, owner, repo, defaultBranch);
    const trustedCompletionCaller = await readCompletionCallerBlob(github, owner, repo, mainSha);
    const trustedReviewCaller = await readCallerBlob(github, owner, repo, mainSha);
    const reviewPin = extractReviewPin(trustedReviewCaller.text);
    await verifyCompletionRunProvenance(
      github,
      {
        owner,
        repo,
        repository,
        defaultBranch: branch,
        orgWorkflowSha,
        trustedCompletionCallerSha: trustedCompletionCaller.sha,
        liveMainSha: mainSha,
        reviewPin,
        trustedCallerSha: trustedReviewCaller.sha,
      },
      run,
    );
    if (run.status !== 'completed') {
      return [];
    }
    const attempt = positiveInt(run.run_attempt, 'completion run attempt');
    const jobs = await exactAttemptJobs(github, owner, repo, run.id, attempt);
    const numbers = new Set();
    for (const job of jobs) {
      if (job?.status !== 'completed' || job?.conclusion !== 'success') {
        continue;
      }
      const match = typeof job.name === 'string' ? PROOF_JOB_RE.exec(job.name) : null;
      if (match) {
        numbers.add(Number(match[1]));
      }
    }
    return [...numbers].sort((left, right) => left - right);
  } catch {
    return [];
  }
}
