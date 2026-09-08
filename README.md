# .github

Organization-wide defaults for [RetireGolden](https://github.com/RetireGolden).

Manual OpenRouter reruns review the full PR and continue its existing review
ledger, including finding IDs and rebuttals. Without a ledger they seed an
initial review. The optional `reset_review` boolean explicitly starts over;
leave it `false` for ordinary reruns. Pushes retain their latest-commit scope.
The stable completion gate recognizes a completed full-PR verification as
well as an initial review, while retaining current-head and run-output checks.
This completion gate is separate from each repository's clean-review CI gate.

When reading reviews through GitHub APIs, paginate reviews, inline comments,
and issue comments, and read every continuation part of a multipart review.
Match the bot identity and explicit reviewed commit rather than assuming the
first API page or a successful workflow means the current head is clean.

## Target-branch review guidance

Callers can opt in with `with: { review_policy: base }` after advancing their
immutable reusable-workflow pin to this revision or a reviewed successor.
The default is `off`. The action reads hierarchical `REVIEW.md` files from
the PR's immutable target-branch tip, freezes their scopes, and publishes a
policy source and digest alongside the existing review receipt. A policy file
added in a PR becomes authoritative only after it merges.

Use concise prose to describe repository contracts, with nested files for
component-specific invariants. See the action's
[file format, offline lint/explain commands, and trust rules](https://github.com/FlyOverCoderKY/openrouter-pr-review-action/blob/6ef4bf920243c202c9a59276e4e4e5f016157a90/docs/review-policy.md).
In this guidance rollout, `profile: code` and `profile: docs` are descriptive;
they do not select models by themselves.

## Trusted review profiles (optional)

`review_profiles_enabled` defaults to `false` on every consumer until the full
migration lands. Turning it on is optional and requires **both**:

1. A thin caller that forwards `review_profiles_enabled: true` and
   `review_policy: base` into the review reusable workflow.
2. A matching default-branch completion caller that installs the profile gate
   CI helper and invokes `openrouter-profile-completion.yml` at the same org
   workflow pin.

The trusted registry in `review-profiles.json` is workflow configuration, not
repository policy and not merge authority. It does not grant CI or bypass
branch protection.

### Baseline panels

| Profile | Standard (auto) | Deep (maintainer dispatch) |
| --- | --- | --- |
| `code` | Grok required, GLM optional | Grok required, GLM optional, Astra/Flex required |
| `docs` | GLM required | GLM required, Astra/Flex required |

`review-model-routes.json` supplies provider routing for exact lane slugs (for
example Astra via `openai/flex`). Deep raises the minimum panel; it retains
the finding ledger and continues a full-PR review. There is no automatic risk
classifier yet — deep is requested explicitly.

Maintainers on the default branch may `workflow_dispatch` with
`review_level: auto`, `deep`, or `cancel`. Ordinary reruns keep
`reset_review: false` so the ledger continues. Profile proof binds provenance,
the published receipt, and the current target-branch base SHA; stale evidence
fails closed.

A `review:deep` label handler is **not available yet**. Dispatching
`review_level: deep` is only an action input until repository integration ships.

For this workflow, an authorized maintainer means a collaborator with GitHub
`write`, `maintain`, or `admin` permission. Profile-enabled reviews reject
`reset_review: true`; they always retain their findings history.

Completion events sweep open PRs under a serialized planning job. Policy
refresh dispatches are limited to one request per PR head and effective
policy/configuration identity. If that dispatch fails, use a manual rerun;
automatic completion events do not repeatedly spend on the same refresh.

### Artifact retention and expiry

Prepared review context and receipts are retained for **30 days**. Accepted
review-request artifacts are retained for **90 days**. Missing or expired
artifacts fail closed. Profile gating requires PRs to be **younger than 25
days**, keeping their complete request history within the evidence window.
For an older PR, open a replacement PR and obtain a fresh review. Reopening
the same PR does not reset its age. Retain artifacts for the configured
durations; deleting evidence early cannot authorize a merge.

### Thin review caller (example)

Pin the org reusable workflow to a reviewed 40-character commit after merge.
Replace `<ORG_WORKFLOW_SHA>` with that pin (unknown until merge).

```yaml
name: OpenRouter code review
run-name: OpenRouter PR #${{ github.event.pull_request.number || inputs.pr_number }}: ${{ inputs.review_level || 'auto' }}

on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
  workflow_dispatch:
    inputs:
      pr_number: { required: true, type: string }
      review_level: { default: auto, type: choice, options: [auto, deep, cancel] }

permissions:
  contents: read
  pull-requests: write
  statuses: write

jobs:
  review:
    uses: RetireGolden/.github/.github/workflows/openrouter-code-review.yml@<ORG_WORKFLOW_SHA>
    secrets:
      OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}
    permissions:
      contents: read
      pull-requests: write
      statuses: write
    with:
      pr_number: ${{ github.event.pull_request.number || inputs.pr_number }}
      review_level: ${{ inputs.review_level || 'auto' }}
      review_profiles_enabled: false
      review_policy: off
```

Minimum secret: `OPENROUTER_API_KEY` passed explicitly (never `secrets: inherit`).

Branch protection should require `review / openrouter-first-pass-gate`.

### Thin completion caller (example)

Runs only on the default branch. Never executes PR code.

```yaml
name: OpenRouter profile completion

on:
  workflow_run:
    workflows: [OpenRouter code review]
    types: [completed]
  push:
    branches: [main]
  workflow_dispatch:
    inputs:
      pr_number: { required: false, type: string }

permissions:
  actions: write
  contents: read
  pull-requests: read
  statuses: write

jobs:
  complete:
    if: github.event_name != 'workflow_run' || github.event.workflow_run.conclusion == 'success'
    uses: RetireGolden/.github/.github/workflows/openrouter-profile-completion.yml@<ORG_WORKFLOW_SHA>
    with:
      pr_number: ${{ inputs.pr_number || '' }}
      source_run_id: ${{ github.event_name == 'workflow_run' && github.event.workflow_run.id || '' }}
```

The completion consumer job id must be `complete` so proof job names resolve
as `complete / profile #<n> <digest>`.

### Security boundary

Profile orchestration uses inert full-depth default-branch checkout plus
pinned org/action checkouts. Review and completion workflows do not execute
untrusted PR code or forward product-repo deploy secrets into the review run.

Local workflow contract checks:

```sh
# Obtain the pinned public action contracts outside product source.
git clone --no-checkout https://github.com/FlyOverCoderKY/openrouter-pr-review-action .trusted-review-action
git -C .trusted-review-action checkout 6ef4bf920243c202c9a59276e4e4e5f016157a90
export PYTHONPATH="$PWD/.trusted-review-action/src"
uv run --with jq --with pyyaml python -m unittest discover -s tests
node --test tests/profile-consumer.test.mjs
```

`scripts/profile_consumer.mjs` is the shared Node validator used by existing
default-branch CI helpers. Load it through the GitHub contents API from the
same immutable organization pin as the two workflows. Its
`authorizeProfileReceipt` export verifies the receipt's proof job and checks
for active or newer review runs before authorizing CI. The
`completionPullRequests` export lets brokers map a default-branch completion
run to the PRs whose individual proof jobs succeeded. These are additional
checks; keep the existing clean-ledger and CI authorization checks.

## Limitations

- `review_profiles_enabled` defaults to `false`; gates are not automatically
  installed in product repositories.
- Optional profiles require coordinated review and completion callers plus the
  CI helper pin-migration path documented for your repo.
- Label-based deep requests are not wired yet.
- Artifact expiry fails closed; PRs aged 25 days require a replacement PR.
- No automatic risk classifier selects deep reviews.
- Adopted repositories now keep domain guidance in target-branch `REVIEW.md`;
  inline workflow guidance for those repos is intentionally empty.
- This is a hobby-project org template — advance pins deliberately and read
  upstream action docs before changing model rosters.
