# LF-022 Codex audit summary

This is a hash-verified diagnostic summary. It creates no human or semantic
labels and contributes no training, evaluation, silver-promotion, or gate credit.

## Bound artifacts

- Summary ID: `lf022_codex_audit_summary:b39c9dc66a43db2a98dca5c0e4b9eddd31c7e68437dc406ab79f9579e29e0328`
- Audit manifest SHA-256: `b2866946d6a8285ddaff79a60c3d7f91520907aebf681165a931c1701f99f8c3`
- Lean checks SHA-256: `657ec8e8f0b5ec6557b138a06608e998a26bead8fbd7ac6cd8415c586b43cd92`
- Verified response set SHA-256: `c9835730ce718e2e491bf1282769c98e8cba222cd040e57302e82e58ad70c1b5`
- Judge: `gpt-5.6-sol` with reasoning `xhigh`

## Mechanical filtering

- Total generated candidates checked by Lean: 668
- Lean-valid candidates audited: 493
- Lean-invalid candidates: 175
- Completed Codex judgments: 493
- Lean outcomes: `elaborates_with_placeholder` 493, `invalid` 175

## Audit verdicts

- Same-claim answers: `not_same_claim` 483, `same_claim` 9, `uncertain` 1
- Relations: `A_stronger` 57, `B_stronger` 197, `equivalent` 9, `incomparable` 229, `null` 1
- Directional implications: `A=no,B=no` 2, `A=no,B=yes` 389, `A=unknown,B=unknown` 10, `A=unknown,B=yes` 3, `A=yes,B=no` 8, `A=yes,B=unknown` 2, `A=yes,B=yes` 79
- Error types: `E01` 97, `E03` 1, `E09` 1
- Needs expert review: 1
- Mean confidence: 0.988479

## By proposer family

| Proposer | Audited | Same claim | Not same | Ambiguous | Uncertain | Mean confidence |
|---|---:|---:|---:|---:|---:|---:|
| `glm5` | 2 | 0 | 2 | 0 | 0 | 0.990000 |
| `moonshot_kimi_k2` | 181 | 1 | 180 | 0 | 0 | 0.988453 |
| `qwen3` | 310 | 8 | 301 | 0 | 1 | 0.988484 |

## Scientific status

These judgments are useful evidence about the quality of the generated pairs,
but they come from one judge family and one AB presentation. They therefore do
not satisfy LeanFaith's two-family, swapped-order weak-consensus contract and are
not human gold.
