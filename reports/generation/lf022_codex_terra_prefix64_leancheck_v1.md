# LF-022 GPT-5.6 Terra prefix-64 Lean check

Date: 2026-08-12

## Result

The bounded GPT-5.6 Terra proposer tranche completed 64/64 tasks at `xhigh`
reasoning and produced one provisional candidate per task. The separate
LeanInteract check then processed all 64 variants:

| Outcome | Count |
|---|---:|
| elaborates with proof placeholder | 53 |
| invalid | 11 |
| total | 64 |

A second invocation completed with `executed=0` and `reused=64`, preserving
the same outcome counts. This establishes exact persisted-result reuse for the
complete tranche.

## Bound artifacts

| Artifact | SHA-256 |
|---|---|
| proposer manifest | `722aad11f8f877a67ac039c663ac8858440666032aec6d14ef378b10166e2a54` |
| proposer tranche | `af299435b955265c055c0d58964e0dbe1dc24863772ac38f49dd1144a19c2d84` |
| Lean-check manifest | `2c7652c63bec26b118cd92070f12143ebd5ae64c2adcfa455f84aca1bec9a54b` |
| Lean-check records | `1487a7eb775f0c711422dc7cc38f57abb2be7a86c125a0a872b18144b64ef0a9` |

The Lean-check artifact is stored at:

```text
/storage/milikic/leanfaith/lf022_codex_scale_lean_checks/
  terra_9141ca0_prefix64_v1
```

## Scope

The candidates are provisional mutations. Elaboration verifies only that a
candidate is a valid Lean proposition in the pinned environment. It does not
establish the intended semantic relation to the source theorem. Accordingly,
the artifact records:

```text
semantic_labels_created = false
silver_records_created = false
training_eligible = false
evaluation_eligible = false
```

Semantic adjudication remains a separate step.
