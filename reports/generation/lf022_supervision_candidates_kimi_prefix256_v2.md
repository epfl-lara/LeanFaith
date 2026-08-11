# LF-022 provisional supervision candidate inventory

This report contains public, Lean-valid source/candidate pairs from one exact
replay-verified Codex audit. Codex remains a diagnostic-only audit and contributes
zero weak-supervision votes. No semantic label, silver record, training record,
evaluation record, or gate credit is created here.

## Exact logical input bindings

- Collection: `kimi_v4_prefix256_codex_complete_v2`
- Lean checks SHA-256: `46972e934b26e9ee6df112a6e135223f83267b58e93ccde2be79e40d6ed54810`
- Codex audit manifest SHA-256: `b2ea47f15495f88e8d1bb5703c3e7622c9b7b5a221685c0ed7526d27bf402c17`
- Verified response set SHA-256: `a3c5940fb48077951e9f5f86781f8ecd873835cfc1627450c5e6373676b82085`
- Logical input binding SHA-256: `fcbac8066958762be82b6701e31da8161eef0728bddf6bbbae8d56321e26fbad`
- Inventory ID: `lf022_supervision_inventory:30fa1a924680e62f04f99e5a5d5a67a59425e1eabdfc28cacaadda531a7ce04b`
- Manifest SHA-256: `6ae47cc6a6ef11ec63e793bd64ac35bae90feb2b9bd883a992b4f691582c63ff`

## Candidate counts

- Replay-verified records: 201
- Exact unique judge-visible payloads: 201
- Exact duplicate records retained but not dispatched: 0
- Ready for later two-family judging: 201
- Future judge calls required: 804
- Dispatch statuses: `ready_for_two_family_judging` 201
- Prior Codex diagnostic verdicts: `ambiguous` 1, `not_same_claim` 198, `same_claim` 2

The deduplication key includes Lean A, Lean B, and the optional natural-language
statement. Identical Lean pairs with different natural-language inputs therefore
remain distinct.

Each dispatch-eligible pair still requires four independent blinded calls:
`judge_A:AB`, `judge_A:BA`, `judge_B:AB`, and `judge_B:BA`.
The configured weak judges are distinct from the proposer and from the reserved
primary evaluation judge. Human-pilot and promotion audits remain blockers.

## Current operational materialization

These locations are operational pointers and do not enter the inventory's
content identity:

- [Public 10-record sample](/storage/milikic/leanfaith/lf022_supervision_candidates/kimi_v4_prefix256_v2/public_sample.jsonl)
- [Complete 201-record inventory](/storage/milikic/leanfaith/lf022_supervision_candidates/kimi_v4_prefix256_v2/candidates.jsonl)
- [Manifest](/storage/milikic/leanfaith/lf022_supervision_candidates/kimi_v4_prefix256_v2/manifest.json)
