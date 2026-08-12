# LF-022 Sol/Fable live smoke: one public Qwen pair

## Outcome

The four-cell live smoke completed for one public Qwen-proposed Lean pair:
`gpt-5.6-sol` at `xhigh` and `claude-fable-5` at `max`, each in both AB and
BA order. Both judge families classified the pair as **not the same claim** and
identified the equality statement as content-stronger than the one-sided
inequality. Offline replay reused all four completed cells without another
provider call, and the finalizer produced one `candidate_consensus` record.

This is a pipeline and model-adjudication result and a route-level example of
candidate silver. It is **not** a human label, semantic gold, promoted or
training-eligible silver, training data, evaluation data, or gate credit.

## Bound authoring artifacts and overlap correction

- Authoring ID:
  `lf022_sol_fable_authoring:4594945cb2b318c2e2ffb9d283166e73911581c2b30bf6bfed6c516461613997`
- Authoring-manifest SHA-256:
  `5f119091bead8ca7bab8c486bd249ff9df69472152cd3422d3868c60e84aa19a`
- Batch ID:
  `lf022_weak_batch:83c82aaa03dedbf03a9b0579e0168be2ab1427b7b9d38428e9c541418d618b8a`
- Batch-spec SHA-256:
  `3b5ceff8e955c9e828fca9ae21a3e184090d2ef30ec2e04d8b00ac29956dee19`
- Dispatch-manifest SHA-256:
  `3b9b6781d7a345681bcc65fa9473dc258670361df507fea9627e9b7c810b7634`
- Candidate-records SHA-256:
  `d6a2b8c8fcc94de84fa82c2e00e9da0ad306d2cd9f55fd923f0e0841b1ec12f0`
- Selected pair:
  `pair:611ef7dfc557409e4b8919d6e76216dbe2e15045f3e91a9de9b4109ad5c700da`
- Source theorem lineage:
  `thm:ca1ebb706d379b2897b7191ed323bbac1cf9be057bafe4ab4f3feb3cc7a41d1a`

The authoring step bound two historical Sol/xhigh corpora containing 694 unique
pair IDs. Their combined exclusion-set SHA-256 is
`83b923750274cd351ee2833bdd8c55c8851c86f56b6593729555f3092bdc576f`.
The manifest records `historical_sol_xhigh_exclusion_complete=true` and
`selected_pairs_absent_from_historical_sol_xhigh=true`, but a subsequent
independent audit proved that this historical union was incomplete.

Specifically, the canonical 718-item Qwen Sol/xhigh audit contains this exact
pair and variant at line 247 of its findings artifact:

- Findings artifact SHA-256:
  `8a20bf79ca24a9d524db497dacdf71b871d7404e00995fd0890c8b9c2186cd6f`
- Audit summary SHA-256:
  `8999a5aa8b734dcc5cc7a63886eb23be9ab31075ba6a6052c815df3248613603`
- Audit manifest SHA-256:
  `e6d1ea0ba1c391dd63896465cb77c2ed2f2b7bb71da708baacaba6ca570497f9`
- Historical finding: `not_same_claim`, `A_stronger`, confidence `0.99`, with
  the same pair ID and variant
  `var:15701c057cc467d66682cbc215a578c678eed9db8ee802512714fa33f6a46d5a`.

Therefore the bundle-level freshness fields are superseded: the pair was
absent only from the two summaries bound during authoring, not from all prior
Sol/xhigh judgments. The smoke is valid for routing, capture, swapped-order
execution, replay, parsing, and finalization, but it is **not** a scientifically
Sol-unseen or fully independent judgment example and must not be counted in a
fresh-adjudication scale pool.

The corrected exhaustive history is registered at
`configs/generation/lf022_historical_sol_xhigh_registry_v1.json`. Its five
registered corpora contain **1,510 unique pair IDs across 1,339 source-theorem
lineages**. The registry deliberately retains the 718-pair Qwen snapshot even
though it is a redundant subset of the cumulative 975-pair Qwen corpus, so the
artifact lineage remains explicit. Future scientific selection excludes the
union of both historical pair IDs and historical theorem lineages. It also
scans completed Sol/Fable finalizations and excludes any pair or lineage that
has already received a completed dual-family judgment.

The canonical statements differ only in their conclusion:

```lean
-- A
f ×ˢ g = (Filter.map Prod.mk f).seq g

-- B
f ×ˢ g ≤ (Filter.map Prod.mk f).seq g
```

## Initial fail-closed runs and fixes

| Judge | Initial run | Observed failure | Root cause and correction |
|---|---|---|---|
| Sol | `lf022_sol_run:60c682e8cffb02d6b70b71c8ce3ef883a9ac527558db785bc8ae3bf8fd390e5d` | Both cells exhausted after two attempts; all four processes exited `-25` (`SIGXFSZ`) with empty stdout/stderr. | The bounded child process inherited a Codex home containing a 587,190,272-byte `logs_2.sqlite`, while its file-size limit was 16,777,217 bytes. The retry used an isolated private run-scoped `CODEX_HOME`/`HOME` with only the required authentication material. |
| Fable | `lf022_fable_run:3d353a1d720f0e6e6463650a7ac1e694cfdd884ec7f4f732f9409e60ffe4ffb2` | Four zero-exit provider outputs were quarantined; both cells ended `secret_redacted`. | The generic redactor mistook the JSON metadata key `api_key_source` for a provider-token value. The matcher was narrowed so metadata keys are retained while actual token values, bearer tokens, environment secrets, and proxy credentials remain redacted. |

The initial roots remain immutable diagnostic evidence; no failed output was
admitted into the finalized batch.

## Successful live runs and offline replay

| Judge | Live run | Live result | Offline replay | Replay result |
|---|---|---|---|---|
| Sol | `lf022_sol_run:2ab083783fbbb8343e6d2c28cc178bb1d1763ed0a65e6a8429304cd74bade921` | 2/2 cells completed; two process attempts | `lf022_sol_run:a9693ca16683b89a7b0fde64488b1aac0b0ecc81df7a67fa2c9923c16a1a17ff` | 0 invoked, 2 reused |
| Fable | `lf022_fable_run:fdc4d28a37c2e188c054a4febffde0bfba0ebd8bf08ab7731326d3e04f3f8b99` | 2/2 cells completed; two process attempts | `lf022_fable_run:92f9773267c054dfc4a8ffd8794eafb4199353f11b4383dedb4c1bd51b161957` | 0 invoked, 2 reused |

Both live manifests record `private_source_content_transmitted=false`. All live
and replay manifests record `semantic_labels_created=false`,
`silver_records_created=false`, `training_eligible=false`,
`evaluation_eligible=false`, and `gate_credit_claimed=false`.

## Four judgments

The table below reports each raw orientation-specific answer before the
finalizer maps BA relations back to canonical A/B orientation.

| Judge | Order | Same claim | Relation in presented order | Confidence | F2 directions | Error metadata |
|---|---|---|---|---:|---|---|
| Sol | AB | `not_same_claim` | `A_stronger` | 0.99 | yes / yes | none |
| Sol | BA | `not_same_claim` | `B_stronger` | 0.99 | yes / yes | none |
| Fable | AB | `not_same_claim` | `A_stronger` | 0.95 | yes / yes | `E01` |
| Fable | BA | `not_same_claim` | `B_stronger` | 0.90 | yes / yes | `E01` |

All four rationales distinguish F1 content from F2 material implication: the
equality is stronger content, while both truth-level directions are judged
`yes` because the equality is independently provable in the ambient Mathlib
context. The error-code disagreement is retained as exploratory metadata and
does not affect the verdict/relation consensus.

## Finalized candidate status

Finalization parsed four evidence records and produced one candidate:

- Finalization ID:
  `lf022_weak_finalization:4aa122f248672be580f4a3cebeaa4b4cff7d2ca4490dcf6fdea7d816a8608fdb`
- Finalization-manifest SHA-256:
  `32f65da5999f453c0024264cf97c8832d75f9e3c577278538fab4c08f03d3e1a`
- Judgment-evidence SHA-256:
  `579bd18248bdab39d87b0c39bc52f3b105355dd219a57bd1aa89b06bf622d37e`
- Candidate SHA-256:
  `a372c4be23e32c0b86f8494bc230d4d9193315a40e5335ed00293408819d4919`
- Candidate ID:
  `weak_consensus:f2184f47629fad45c6a7ccaaa8af2c5902ffd9202bc135243c59edc613362d4f`
- Canonical candidate verdict: `not_same_claim`, `A_stronger`, confidence
  `0.95`, `requires_adjudication=true`.
- Promotion blockers: `human_pilot_not_bound`, `promotion_audit_missing`, and
  `silver_not_promoted`.

Therefore the smoke validates live dual-family, swapped-order collection,
fail-closed capture, replay, parsing, orientation normalization, and candidate
consensus creation. It does not validate label accuracy at scale and cannot be
used for training or scientific evaluation without the separate promotion
policy and audits. A fresh, order-consistent Sol/xhigh plus Fable/max agreement
is model-adjudicated candidate silver until that policy promotes it; model
agreement alone never makes it human gold.
