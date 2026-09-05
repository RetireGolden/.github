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

On September 5, 2026, callers pinned to the Astra Flex trial revision add
`openai/gpt-6-astra` as a third review lane in RetireGolden and RetireGolden-Pro.
Grok 4.6 and GLM 5.3 Flash remain enabled. Only Astra is pinned to OpenAI Flex,
without provider fallbacks; the judge keeps its existing routing. Both initial
and follow-up reviews participate, retaining their existing effort settings.
Reviews starting at or after September 6 at 00:00 America/New_York
(04:00 UTC) automatically use the original two-lane roster. The trial does not
change the other repos' rosters. A caller can opt out early by restoring its
previous immutable workflow pin.

- [`profile/README.md`](profile/README.md) — the organization profile shown at
  [github.com/RetireGolden](https://github.com/RetireGolden).
