# LF-022 Codex audit summary

This is a hash-verified diagnostic summary. It creates no human or semantic
labels and contributes no training, evaluation, silver-promotion, or gate credit.

## Bound artifacts

- Summary ID: `lf022_codex_audit_summary:96b2f8c8a938308f24f5c18526a426389b52bdcccdf9850a635fbe6685378c46`
- Audit manifest SHA-256: `b2ea47f15495f88e8d1bb5703c3e7622c9b7b5a221685c0ed7526d27bf402c17`
- Lean checks SHA-256: `46972e934b26e9ee6df112a6e135223f83267b58e93ccde2be79e40d6ed54810`
- Verified response set SHA-256: `3814e5221f5dfad9288f28754c09129b64e81d860516ad783150e3ed8c2d5653`
- Compact findings SHA-256: `9552a840db82eb9e0600d3e8dec15bc8467085027b658a648a8f9eeba8e79b69`
- Judge: `gpt-5.6-sol` with reasoning `xhigh`

## Mechanical filtering

- Total generated candidates checked by Lean: 248
- Lean-valid candidates audited: 201
- Lean-invalid candidates: 47
- Completed Codex judgments: 201
- Lean outcomes: `elaborates_with_placeholder` 201, `invalid` 47

## Audit verdicts

- Same-claim answers: `ambiguous` 1, `not_same_claim` 198, `same_claim` 2
- Relations: `A_stronger` 12, `B_stronger` 83, `ambiguous` 1, `equivalent` 2, `incomparable` 103
- Directional implications: `A=no,B=no` 2, `A=no,B=yes` 163, `A=unknown,B=unknown` 4, `A=yes,B=no` 2, `A=yes,B=unknown` 1, `A=yes,B=yes` 29
- Error types: `E01` 27
- Needs expert review: 1
- Mean confidence: 0.987761

## By proposer family

| Proposer | Audited | Same claim | Not same | Ambiguous | Uncertain | Mean confidence |
|---|---:|---:|---:|---:|---:|---:|
| `moonshot_kimi_k2` | 201 | 2 | 198 | 1 | 0 | 0.987761 |

## Scientific status

These judgments are useful evidence about the quality of the generated pairs,
but they come from one judge family and one AB presentation. They therefore do
not satisfy LeanFaith's two-family, swapped-order weak-consensus contract and are
not human gold.
