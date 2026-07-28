# Phase 6 milestone: LLM variants and weak supervision

**Status:** foundation implemented; one public-source RCP smoke completed and
replay-verified; production collection is not admitted; Gate 6G and Gate 6
remain open

**Date:** 2026-07-28

**Scope:** LF-022 prompt, parsing, provisional-record, blinded-judgment, and
candidate-only aggregation boundaries

## Current scientific boundary

This milestone records an implementation foundation only. It does not record
an LLM data collection, a semantic-labeling result, or a silver-data
promotion.

| Quantity | Current value |
|---|---:|
| Live proposer attempts | 3 |
| Parsed proposer calls | 2 |
| Live weak-judge attempts | 5 |
| Parsed judgment evidence | 4 |
| Smoke-only validated variants | 2 |
| Semantic labels created by LF-022 | 0 |
| Smoke-only weak-consensus candidates | 1 |
| Promoted silver records | 0 |
| Train-eligible LF-022 records | 0 |
| Evaluation-eligible LF-022 records | 0 |

Accordingly:

- Gate 6G is **open**;
- Gate 6 is **open**;
- no `silver_consensus` claim is authorized;
- no LF-022 record may enter training, calibration, model selection, or
  evaluation; and
- LLM agreement is not human gold.

Gate 5 also remains open. Gate 5G established only mechanical real-output
collection and replay. The frozen 240-item prevalence frame still has zero
human terminal labels, so its items remain unresolved `REVIEW` records and
cannot serve as real-output semantic supervision.

## Implemented fail-closed foundation

The current LF-022 foundation includes:

- a versioned proposer template at
  `prompts/proposers/lean_variant_v1.txt`;
- a versioned blinded-pair judge template at
  `prompts/judges/lean_pair_blinded_v1.txt`;
- disabled-until-admitted generation configuration at
  `configs/generation/llm_variants_v1.yaml`;
- disabled-until-admitted weak-supervision configuration at
  `configs/judges/weak_supervision.yaml`;
- strict, finite, duplicate-key-free JSON parsing;
- public-source, external-transmission, and denylist checks before prompt
  rendering;
- proof-free single-declaration candidate validation;
- provisional `VariantRecord` materialization that preserves the proposer
  intention as provenance rather than a label;
- blinded A/B and B/A presentations with deterministic remapping to canonical
  pair order;
- explicit proposer, two-judge, and held-out-primary family separation;
- `EvidenceRecord` materialization for individual judgments;
- candidate-only aggregation in
  `WeakConsensusCandidateRecord`; and
- schema-enforced `semantic_label_created=false`,
  `silver_promoted=false`, `train_eligible=false`,
  `eval_eligible=false`, and `requires_adjudication=true`.

The provider bridge binds and verifies already-persisted generic LLM request
and raw-response lineage for generation and judge roles. Provider execution
must persist those artifacts before parsing. Parse failures remain terminal
artifacts rather than disappearing or being converted into semantic
negatives.

The general production components have no transport loop enabled. Both
canonical production configuration files set `live_calls_authorized: false`.
The separately versioned one-item RCP smoke admissions are strictly capped,
public-source-only, and smoke-quarantined.

## Public RCP smoke qualification

The qualification lineage is recorded at
`reports/generation/lf022_rcp_public_smoke_qualification_v1.json`.

It preserves two terminal, no-retry failures:

1. v1 exhausted Kimi's 4,096-token completion budget in hidden reasoning and
   returned no final content;
2. v2 generated and Lean-validated a candidate, but DeepSeek returned a
   malformed final JSON number, so strict parsing rejected the run.

Both failure manifests pass typed, network-free lineage replay. Replay
recomputes the run key from the bound preflight, validates the run lock,
provider requests, wire payloads, generic LLM call records, raw responses,
parse outcomes, and any partial variant/Lean-validation prefix. Neither run
created a semantic label, weak-consensus candidate, or eligible record.

The independently versioned v3 admission replaced only the failed judge
family with `Qwen/Qwen3.5-397B-A17B`. It completed exactly:

- one `moonshotai/Kimi-K2.7-Code` proposer call;
- two blinded Qwen A/B orientations;
- two blinded `zai-org/GLM-5.2` A/B orientations;
- one LeanInteract candidate validation;
- four judgment-evidence records; and
- one weak-consensus candidate requiring adjudication.

The full manifest replays locally. The variant, every evidence record, and the
consensus candidate carry `artifact_class=smoke` plus explicit false
supervision/training/evaluation/gate-credit flags. The primary evaluation
judge was not called. Raw public smoke lineages are tracked only because they
are small and required for offline replay; production raw collections remain
ignored.

From a clean checkout, replay the three lineages without network access using
their exact versioned configs:

```bash
uv run leanfaith lf022-rcp-smoke \
  --config configs/generation/lf022_rcp_public_smoke_v1.yaml \
  --replay-failure-manifest data/raw/llm_variants/lf022_rcp_public_smoke_v1/ba820b59f24090eaa93c8d205e732e7a5c78f9413ae78c325be5e8273a82734a/failure_manifest.json
uv run leanfaith lf022-rcp-smoke \
  --config configs/generation/lf022_rcp_public_smoke_v2.yaml \
  --replay-failure-manifest data/raw/llm_variants/lf022_rcp_public_smoke_v2/f1ce60d318fa59c61da08302cfc33c03b3629e17e57854b810e385d426b35ce6/failure_manifest.json
uv run leanfaith lf022-rcp-smoke \
  --config configs/generation/lf022_rcp_public_smoke_v3.yaml \
  --replay-manifest data/raw/llm_variants/lf022_rcp_public_smoke_v3/61e201acc254d89cb5e9686bd56a7f4e03c0ea2f8169ae39e22cc31be48a0589/manifest.json
```

## Private-source and benchmark boundary

External LF-022 prompts accept only source material that is:

1. explicitly public;
2. explicitly permitted for external transmission; and
3. cleared against the frozen benchmark denylist.

Content from private `formalmathatepfl/sft_classic` is forbidden from external
transmission. This foundation does not weaken Gate 0's internal-only source
policy.

## Required work before the first production call

No production proposer call is authorized until a separately versioned
admission artifact binds:

- the provider family and exact model/revision;
- a public, denylist-clear input pool;
- the prompt hash, parser ID, and decoding configuration;
- the immutable raw-artifact directory; and
- the proposer-family cap and held-out-family policy.

No production judge call is authorized until a separate judge-admission
artifact also binds:

- two distinct weak-judge families;
- a distinct proposer family;
- a fourth primary evaluation family excluded from all weak supervision;
- the frozen judge-by-supervision matrix;
- both A/B and B/A presentations; and
- immutable request, response, parse-failure, and evidence paths.

The required one-item public-source smoke now verifies raw persistence,
terminal failure persistence, parsing, Lean validation, provisional variant
materialization, blinded swapped judging, evidence persistence,
candidate-only aggregation, and offline replay. This operational
qualification does not itself authorize or constitute production scale-out.

## Gate 6G work still required

Gate 6G cannot close until admitted live collection establishes that:

- every call either parses or retains an explicit failure artifact;
- all candidates validate or enter an explicit quarantine/failure outcome;
- order randomization and swap remapping replay exactly;
- proposer intentions remain provenance only;
- the two weak judges and proposer are distinct families;
- the primary evaluation judge family supplied no training-time supervision;
- disagreement, ambiguity, uncertainty, and swap inconsistency are retained;
  and
- the complete live lineage is immutable and reproducible.

No Gate 6G report exists yet.

## Gate 6 work still required

Gate 6 is a later promotion gate. It additionally requires:

- the human annotation pilot and frozen promotion policy;
- the capped, stratified audit specified in the plan;
- swapped-order agreement of at least 90% after directional remapping;
- retention and reporting of disagreement;
- auditable promotion strata; and
- a strict distinction between `silver_consensus` and human gold.

Until those conditions are met, even unanimous two-family judgments remain
non-trainable weak-consensus candidates requiring adjudication.

## Relationship to training readiness

The current training-readiness audit remains `NOT_READY`. This LF-022
foundation changes the software inventory, but it does not satisfy any of
the following missing scientific artifacts:

- promoted production `G_sci` data;
- promoted production `G_open` data;
- adjudicated prevalence labels;
- `training_gold`;
- `selection_gold`;
- `calibration_gold`;
- `final_human_test`; or
- a frozen training-readiness inventory.

The readiness audit must be regenerated only after actual artifacts exist; it
must not infer readiness from the presence of prompts, parsers, or unit
tests.
