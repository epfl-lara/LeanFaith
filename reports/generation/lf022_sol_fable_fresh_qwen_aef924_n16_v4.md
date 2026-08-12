# LF-022 fresh Sol/Fable adjudication: Qwen aef924 n16 v4

Date: 2026-08-12

## Outcome

Sixteen fresh, public, Lean-valid Qwen-proposed pairs completed the full
four-cell adjudication route. GPT-5.6 Sol at `xhigh` and Claude Fable 5 at
`max` each judged every pair in AB and BA presentation order.

- Sol: 32/32 completed and parsed in exactly 32 process attempts.
- Fable: 32/32 completed and parsed in exactly 32 process attempts.
- Total: 64/64 cells completed on their first provider attempt.
- Retries, exhausted cells, parser failures, infrastructure failures, and
  private-source transmissions: zero.
- No source judgment requested expert review.

Offline replay made zero provider calls and reproduced 64 terminals and 64
judgment-evidence records. Finalization produced one explicit candidate record
for each of the 16 input pairs:

| Finalizer status | Count |
|---|---:|
| `candidate_consensus` | 10 |
| `swap_inconsistent` | 5 |
| `disagreement` | 1 |

The ten raw consensuses contain nine `not_same_claim` verdicts and one
`same_claim` verdict. Their relation distribution is six `B_stronger`, two
`incomparable`, one `A_stronger`, and one `equivalent`.

## Immutable source and execution bindings

- Authoring ID:
  `lf022_sol_fable_authoring:8ede4500b46dd5dd9966d5f5bb2d4b2e84b64697510d891cf3fa582d40504af7`
- Authoring-manifest SHA-256:
  `53e540c36222a852566086fc84880bfccb365987b06fceee35a5c46be04eefd3`
- Batch ID:
  `lf022_weak_batch:12d662a1fe4d61c1171553c271f9ca98610f1ea742531ebf38f7a846d20e728b`
- Batch-spec SHA-256:
  `3a86735785a736e04b85106ce990a13b31639e3cd0c992644fd22dfe1a92a227`
- Dispatch-manifest SHA-256:
  `ddbbdcc0e7bdb499f6674e918284176913245647c43656908291d8d5a627eaf1`
- Sol run ID:
  `lf022_sol_run:efdeb6d05c51dd9fd1c9ba73c69128c1b64fda3f0e87a6e0b0590735b3a15ede`
- Sol run-manifest SHA-256:
  `edac87e4285cdeb6443aec84f4417bbda4bee26cd5ef8c2a8e3b0704f046b71b`
- Fable run ID:
  `lf022_fable_run:376db718bd97a8725630b28cdd6179f6bc7ccdc54a89a616740cfa99b7210923`
- Fable run-manifest SHA-256:
  `38e74cc9f45b2df5124f92953296aca2e2ed3e8b49163e8dff0d3d5dccdd2571`
- Execution ID:
  `lf022_weak_execution:059b68c61efec5fa48ce4c60132e0ce2d352d48c80f898103e392962a33088f5`
- Execution-manifest SHA-256:
  `4937c442b6d77376cc06027cf9b65ca813b8c6a6b02f7da59be933d0cc0ff870`
- Finalization ID:
  `lf022_weak_finalization:78337d7a7a9f8e452f08d8b724627f415ee25ea304b691c6648e845ca6070c16`
- Finalization-manifest SHA-256:
  `566321e7216423b35c22a7d20904962d6bfb965d487685cbe1d4960ed2558c5a`

Final artifact bindings:

```text
terminal records       7f37d3fb08e9da8b396f1ae420a0e31f399ef1e8dd1bab0e0717fa7f9b21acf8
calls                  99172676681d346f9275d1b1b4a973fc1671772fff0d4bdc114b34b403f2a302
attempts               ba411f04cba93fe58ca262e1a3c9338ec7eb08c1c9f9db590a3eb6a11142ebf0
judgment evidence      345bcac65c5eb22310145f2a0c8ce73e5255a24b77553793bc63f046f9b74091
weak candidates        2d8a756b11b7f2f93305a5d814bb441c7d039f0a098a2d0108009110030aa85b
```

Canonical artifact root:

```text
/storage/milikic/leanfaith/lf022_weak_batches/
  sol_fable_fresh_qwen_aef924_n16_v4/batch/
```

## Agreement audit

Both judge families were verdict-order-consistent on all 16 pairs. The stricter
semantic projection also compares the relation and both directional F2
answers:

| Check | Consistent | Inconsistent |
|---|---:|---:|
| Sol AB versus BA verdict | 16 | 0 |
| Fable AB versus BA verdict | 16 | 0 |
| Sol full semantic projection | 12 | 4 |
| Fable full semantic projection | 15 | 1 |
| Either family inconsistent on full projection | 11 | 5 |

The five full-projection failures are disjoint by family: four Sol-only and
one Fable-only. Of the 11 pairs that were internally order-consistent for both
families, all 11 had cross-family verdict agreement and ten had complete
cross-family relation/F2 agreement. The remaining pair is the finalizer's one
`disagreement` record.

## Strict model-silver promotion

The separate fail-closed promotion verifier replayed the immutable batch,
calls, evidence, prompt/config bindings, freshness exclusions, and all four
canonicalized judgments per pair. Under
`sol_fable_abba_model_adjudicated_training_silver_v1`, it admitted eight
records and retained eight explicit rejections.

Promoted labels:

| `same_claim` | Relation | Count |
|---|---|---:|
| `false` | `B_stronger` | 6 |
| `false` | `A_stronger` | 1 |
| `false` | `incomparable` | 1 |

The minimum four-cell self-reported confidence distribution among promotions
is 0.92 for one record, 0.93 for one record, and 0.95 for six records.

Rejection reasons are non-exclusive:

| Rejection reason | Incidence |
|---|---:|
| `canonical_semantic_disagreement` | 6 |
| `weak_candidate_not_consensus` | 6 |
| `weak_candidate_value_missing` | 6 |
| `judgment_confidence_below_policy` | 5 |

The eight rejected records comprise three with all four reasons, three with
the first three reasons, and two otherwise-consensual records rejected only
for falling below the 0.90 confidence floor.

- Promotion manifest ID:
  `model_silver_manifest:9a7202bdc59cfcdcbab0fe0d21fde624c3d9ffb8cc540c9561b6e4beb547903c`
- Promotion-manifest SHA-256:
  `9c22e043d49a8be1da206e5a0bf8a9ecc24fe1cc6b4cb1f0952dbad699fec607`
- Promotion-policy SHA-256:
  `45020b59532b1014ec062f61170a1fca9d66ae6e088ddd32a0e9daa7b11519df`
- Promotions SHA-256:
  `1ea7e4f5d3afcfc05da678092ad20e29cf880b1ebd213c088b36494eca541e71`
- Rejections SHA-256:
  `9a8e0d9b578428e8e31d73df713b43e36eb04688e72d1d319b2d9a657952a484`

Promotion artifact root:

```text
/storage/milikic/leanfaith/lf022_model_silver/
  qwen_aef924_fresh_n16_v1/
```

## Scientific boundary

The eight admitted records are model-adjudicated `silver_consensus` records
eligible only for the declared weak-training arm. They are **not human gold**
and are ineligible for calibration, model selection, sealed evaluation, or
Gate-6 human-audit credit. The promotion manifest records
`human_gold_eligible=false`, `calibration_eligibility=false`,
`selection_eligibility=false`, `eval_eligibility=false`,
`resolved_label_created=false`, and `gate_6_human_audit_claimed=false`.

This batch therefore supplies eight bounded training-only labels and eight
auditable non-admissions; it does not supply scientific evaluation labels.
This is record-level eligibility only. Gate 6M has not yet been formally
closed, so the batch does not by itself authorize a scientific weak-training
run.
