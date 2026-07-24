# Training-data readiness audit v1

**Status:** `NOT_READY`  
**Mode:** `confirmatory`  
**Audit ID:** `training_data_readiness_v1:1281fab6d3ed0b8d24006eaebb1599a85fbef6813d868cc539c8aab30a16cbc2`

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
- Human gold products present: 0/4

## Blockers

- `CALIBRATION_GOLD_MISSING` — The required calibration_gold readiness manifest is absent. Observed: absent. Required: data/human/calibration_gold/readiness_manifest_v1.json.
- `CONFIRMATORY_TRAINING_NOT_READY` — The preregistered 50,000-pair confirmatory pilot cannot start. Observed: 0 effective training inventory records. Required: 50000 ancestry-controlled pairs per D0-D5 arm.
- `FINAL_HUMAN_TEST_MISSING` — The required final_human_test readiness manifest is absent. Observed: absent. Required: data/human/final_human_test/readiness_manifest_v1.json.
- `LF022_ARTIFACTS_MISSING` — SCI-conditioned/open-ended LF-022 data do not yet exist. Observed: data/generated/llm/G_sci/readiness_manifest_v1.json, data/generated/llm/G_open/readiness_manifest_v1.json. Required: all registered LF-022 readiness artifacts.
- `PREVALENCE_HUMAN_LABELS_MISSING` — Frame adequacy does not establish faithful prevalence; genuine human terminal labels are still required. Observed: 0 human terminal labels; human-label artifact is absent: data/human/prevalence_frame/adjudicated_labels_v1.jsonl. Required: 240 separately persisted, adjudicated human terminal labels bound to immutable frame IDs.
- `SELECTION_GOLD_MISSING` — The required selection_gold readiness manifest is absent. Observed: absent. Required: data/human/selection_gold/readiness_manifest_v1.json.
- `TRAINING_GOLD_MISSING` — The required training_gold readiness manifest is absent. Observed: absent. Required: data/human/training_gold/readiness_manifest_v1.json.
- `TRAINING_INVENTORY_MISSING` — No frozen per-pair training-readiness inventory exists. Observed: absent. Required: data/releases/v0/manifests/training_readiness_inventory_v1.jsonl.

## Safety boundary

- Model execution performed: false
- Semantic labels created by this audit: false
- Compilation-derived F1 labels: false
- Proof-search-derived F1 labels: false
- LLM-agreement-derived F1 labels: false
