# Expanded mixed proxy corpus after the Qwen delta-150 audit

Date: 2026-08-12

Status: complete, clean-code frozen, independently audited, and exact-replay
verified.

This artifact adds 150 previously unseen, Lean-valid Qwen proposals judged by
GPT-5.6 Sol at `xhigh` reasoning to the prior mixed proxy corpus. It remains
machine proxy supervision: it contains no human-gold or promoted semantic
labels and is ineligible for scientific model selection, calibration,
evaluation, gate credit, or release claims.

## Frozen artifact

- Producer revision: `f7b398af365d0c24d8524aa1a5ce0fb53c83a813`
- Dataset ID:
  `experimental_mixed_supervision:5256ea41c1728cbfd56a5a6d3c5ffbe380b179f2fa7def9161264c7483e300b6`
- Root:
  `/storage/milikic/leanfaith/experimental_mixed_supervision/firsthop_kimi_qwen1125_composition_f7b398af_v1`
- Manifest SHA-256:
  `16591bea6f32bbe764d7e5be543c71ef76ecbe74efca1dd40c0fb8832023a45a`
- Records SHA-256:
  `cbb113c85c7fea00e0a53877d5f0a586db1c5399ea4107c0050c4ad443caccd1`
- Split assignments SHA-256:
  `67f893793969a8dc403a157b8a79c79ca7173293835e7328f55e9e1da4f1850a`

## Delta-150 audit

All 150 selected variants elaborated with a placeholder, completed on the
first Codex attempt, parsed under the frozen schema, and passed exact artifact
verification. The judge assigned:

- 147 `not_same_claim` and three `same_claim` verdicts;
- 20 `A_stronger`, 63 `B_stronger`, 63 `incomparable`, three `equivalent`,
  and one `unrelated` relation;
- zero ambiguous, uncertain, expert-review, incoherent, or failed records;
- mean confidence `0.987867` (range `0.90` to `1.00`).

The three equivalent verdicts conflict with the proposer's requested negative
polarity. They are retained as judge-proxy evidence rather than silently
forced to the proposal intention. The delta has zero variant-, audit-, pair-,
and exact source/candidate-key overlap with the previous corpus.

## Corpus counts

- 17,181 deduplicated pairs and 17,194 retained proxy signals
- 13,769 train, 1,621 validation, and 1,791 test pairs
- 4,541 same-claim and 12,640 not-same-claim proxy targets
- 10,336 deterministic first-hop signals
- 5,534 deterministic composition signals
- 1,324 completed single-Codex-judge signals
- 6,599 ancestry-connected split components
- zero gold labels, zero promoted silver records, and zero records eligible for
  scientific training

The full freezer was executed twice from a detached clean worktree. The second
run re-read and re-hashed all deterministic, composition, LLM-audit, benchmark,
source-theorem, and representation inputs and returned `replayed=true` with
the same dataset ID and byte-identical outputs.

