// Explicit wakes bridge GITHUB_TOKEN dispatches whose workflow_run events are suppressed.
// A wake is a hint only. The destination must wait for this run to finish and
// independently validate current workflow/artifact provenance before authorizing work.
export async function dispatchWorkflowWake(github, core, context, target, optional = false) {
  const allowed = new Set(['openrouter-profile-completion.yml', 'openrouter-ci-broker.yml']);
  if (!allowed.has(target)) throw new Error('unsupported completion wake target');
  if (context.eventName !== 'workflow_dispatch' || context.actor !== 'github-actions[bot]') {
    return core.info('Native completion events cover this run');
  }
  if (!Number.isSafeInteger(context.runId) || context.runId <= 0) {
    throw new Error('invalid completion source run');
  }
  const { owner, repo } = context.repo;
  const { data: repository } = await github.rest.repos.get({ owner, repo });
  if (repository.full_name !== `${owner}/${repo}` || !repository.default_branch) {
    throw new Error('invalid completion target repository');
  }
  if (context.ref !== `refs/heads/${repository.default_branch}`) {
    throw new Error('completion wakes require a default-branch dispatch');
  }
  let workflow;
  try {
    ({ data: workflow } = await github.rest.actions.getWorkflow({ owner, repo, workflow_id: target }));
  } catch (error) {
    if (optional && error?.status === 404) return core.info('This repository has no CI broker');
    throw error;
  }
  if (workflow.path !== `.github/workflows/${target}` || workflow.state !== 'active') {
    throw new Error('completion target is not the expected active workflow');
  }
  await github.rest.actions.createWorkflowDispatch({
    owner, repo, workflow_id: target, ref: repository.default_branch,
    inputs: { source_run_id: String(context.runId) },
  });
  core.info(`Dispatched ${target} to inspect source run ${context.runId}`);
}
