# .github

Organization-wide defaults for [RetireGolden](https://github.com/RetireGolden).

Manual OpenRouter reruns review the full PR and continue its existing review
ledger, including finding IDs and rebuttals. Without a ledger they seed an
initial review. The optional `reset_review` boolean explicitly starts over;
leave it false for ordinary reruns. Pushes retain their latest-commit scope.
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
[file format, offline lint/explain commands, and trust rules](https://github.com/FlyOverCoderKY/openrouter-pr-review-action/blob/93cc91130605bc17cb583c5a5e899591773e048c/docs/review-policy.md).
In this guidance rollout, `profile: code` and `profile: docs` are descriptive;
they do not select models. Matching deep-review requirements are rejected
before model calls until deep-profile orchestration is available.

One configuration job now supplies both opening and follow-up reviews. It
runs trusted inline workflow code without checkout, credentials, or repository
permissions. The standing model roster, effort, tool limits, and merge judge
remain the same. The expired Astra trial is no longer evaluated in this
revision; choosing a new baseline is a separate change.

During adoption, the existing repository-specific guidance remains a fallback
in the shared configuration. Remove that transitional text only after each
repository's replacement `REVIEW.md` is present on its target branch. Keep
organization-wide review standards, allowed models, credentials, and gates
in the pinned workflow. Repository policy cannot grant CI or merge authority.
Callers with CI trust helpers must follow their documented pin-migration path;
a new review workflow pin alone is not sufficient to update those helpers.

Local workflow contract checks:

```sh
uv run --with jq --with pyyaml python -m unittest discover -s tests
```

The historical Astra Flex third-lane trial ended on September 6, 2026 at
04:00 UTC (00:00 America/New_York). Its time-limited configuration remains
available in [the trial revision](https://github.com/RetireGolden/.github/commit/133c4a1).
This revision uses the standing rosters described above.

- [`profile/README.md`](profile/README.md) — the organization profile shown at
  [github.com/RetireGolden](https://github.com/RetireGolden).
