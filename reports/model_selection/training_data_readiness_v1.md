# Training-data readiness audit v1

**Status:** `NOT_READY`  
**Mode:** `confirmatory`  
**Audit ID:** `training_data_readiness_v1:1ba13cb487e9b5af147b3b4624c2a1ebdaef18c42767aa04c920116f3711d62a`

## Separation of claims

- Prevalence frame adequate for human annotation: **TRUE** (240 items, 3 generator families).
- Human terminal prevalence labels: **0**; prevalence estimate ready: **FALSE**.
- Confirmatory flagship training/model selection ready: **FALSE**.
- Reduced-data ablation ready: **FALSE**.

Mechanical compilation and Gate 5G admission are not semantic labels and are not counted as training supervision.

## Current inventory

- Effective nonduplicate training records: 0
- Safe F1 labels: 0
- Unsafe F1 labels: 0
- LF-022 SCI/open artifacts present: FALSE
- Generator holdout manifest valid: FALSE
- Human gold products present: 0/4

## Blockers

- `CALIBRATION_GOLD_MISSING` — The required calibration_gold readiness manifest is absent. Observed: absent. Required: data/human/calibration_gold/readiness_manifest_v1.json.
- `CONFIRMATORY_TRAINING_NOT_READY` — The preregistered 50,000-pair confirmatory pilot cannot start. Observed: 0 effective training inventory records. Required: 50000 ancestry-controlled pairs per D0-D5 arm.
- `D5_HUMAN_GOLD_CONTRACT_INVALID` — D5 must include training_gold at loss weight 2 with no ancestry oversampling. Observed: no D5 record is bound to training_gold. Required: training_gold present; human-gold D5 loss weight 2; ordinary D5 weight 1; ancestry oversampling disabled.
- `FINAL_HUMAN_TEST_MISSING` — The required final_human_test readiness manifest is absent. Observed: absent. Required: data/human/final_human_test/readiness_manifest_v1.json.
- `GENERATOR_HOLDOUT_MANIFEST_MISSING` — The frozen generator-family holdout manifest is absent. Observed: absent. Required: data/releases/v0/manifests/generator_holdout_v1.json.
- `HUMAN_GOLD_ADMISSION_DISABLED` — Current operator attestations authenticate integrity only and cannot admit human-gold labels. Observed: origin_assurance=operator_attested, backend_origin_verified=false, human_gold_eligible=false. Required: a future registered backend adapter and independently verified backend-origin trust record under a revised admission policy.
- `LF022_ARTIFACTS_MISSING` — SCI-conditioned/open-ended LF-022 data do not yet exist. Observed: data/generated/llm/G_open/readiness_manifest_v1.json, data/generated/llm/G_sci/readiness_manifest_v1.json. Required: both registered, production LF-022 readiness manifests.
- `PREVALENCE_HUMAN_LABELS_MISSING` — Frame adequacy does not establish faithful prevalence; genuine human terminal labels are still required. Observed: 0 human terminal labels; human-label artifact is absent: data/human/prevalence_frame/adjudicated_labels_v1.jsonl. Required: 240 separately persisted, adjudicated human terminal labels bound to immutable frame IDs.
- `SELECTION_GOLD_MISSING` — The required selection_gold readiness manifest is absent. Observed: absent. Required: data/human/selection_gold/readiness_manifest_v1.json.
- `TRAINING_GOLD_MISSING` — The required training_gold readiness manifest is absent. Observed: absent. Required: data/human/training_gold/readiness_manifest_v1.json.
- `TRAINING_INVENTORY_MISSING` — No frozen per-pair training-readiness inventory exists. Observed: absent. Required: data/releases/v0/manifests/training_readiness_inventory_v1.jsonl.
- `TRAINING_LINEAGE_INVALID` — Training label bases do not replay from PairRecord, ResolvedLabel, evidence, promotion, theorem, and source-manifest lineage. Observed: missing lineage artifact data/releases/v0/manifests/training_theorem_records_v1.jsonl; missing lineage artifact data/releases/v0/manifests/training_pair_records_v1.jsonl; missing lineage artifact data/releases/v0/manifests/training_resolved_labels_v1.jsonl; missing lineage artifact data/releases/v0/manifests/training_evidence_records_v1.jsonl; missing lineage artifact data/releases/v0/manifests/training_promotion_decisions_v1.jsonl; missing lineage artifact data/releases/v0/manifests/training_annotation_records_v1.jsonl; missing lineage artifact data/releases/v0/manifests/training_adjudication_records_v1.jsonl; missing lineage artifact data/releases/v0/manifests/training_split_assignments_v1.jsonl. Required: complete cross-record lineage with a mechanically justified label basis.

## Safety boundary

- Model execution performed: false
- Semantic labels created by this audit: false
- Compilation-derived F1 labels: false
- Proof-search-derived F1 labels: false
- LLM-agreement-derived F1 labels: false
