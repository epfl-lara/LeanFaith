# LF-022 current exploratory LLM-data inventory v1

Snapshot time: `2026-08-11T05:28:57Z`

Status: **exploratory only**. This report separates generation, Lean checking, and Codex judgment. It does not convert any of them into a human label, resolved F1 label, silver record, training item, evaluation item, promotion, or gate credit.

## Completed, fixed artifacts

| Artifact | Model(s) | Generated variants | Lean-valid with placeholder | Lean-invalid | Codex-judged |
|---|---|---:|---:|---:|---:|
| Historical checked set | Kimi K2.7 Code, Qwen3.5 397B, GLM-5.2 | 668 | 493 | 175 | 493 |
| Kimi v4 prefix-256 | Kimi K2.7 Code | 248 | 201 | 47 | 201 |
| **Gross observation sum** | — | **916** | **694** | **222** | **694** |

The gross sum is not a unique-pair count. No cross-artifact deduplication was performed, and this report makes no assumption about overlap between source theorems, candidates, or normalized outputs in the two artifacts.

### Historical checked set: 668 generated variants

| Model | Generated | Lean-valid | Lean-invalid | Codex-judged |
|---|---:|---:|---:|---:|
| `Qwen/Qwen3.5-397B-A17B` | 439 | 310 | 129 | 310 |
| `moonshotai/Kimi-K2.7-Code` | 227 | 181 | 46 | 181 |
| `zai-org/GLM-5.2` | 2 | 2 | 0 | 2 |

The 493 audit-only Codex judgments contain 483 `not_same_claim`, 9 `same_claim`, and 1 unresolved/uncertain result. The relation readout is 57 `A_stronger`, 197 `B_stronger`, 9 `equivalent`, 229 `incomparable`, and 1 null unresolved relation. These are one-model audit findings, not semantic labels.

Evidence:

- Lean-check manifest: `/storage/milikic/leanfaith/lf022_lean_checks/rcp_5e672b9_v1/manifest.json` (`sha256:5b7bdf168d4158406b7ddefc6fa3e545346f9aba676608bb964be3d0882ad77e`)
- Lean checks: `/storage/milikic/leanfaith/lf022_lean_checks/rcp_5e672b9_v1/checks.jsonl` (`sha256:657ec8e8f0b5ec6557b138a06608e998a26bead8fbd7ac6cd8415c586b43cd92`)
- Codex audit manifest: `/storage/milikic/leanfaith/lf022_codex_audit/sol_xhigh_v2/manifest.json` (`sha256:b2866946d6a8285ddaff79a60c3d7f91520907aebf681165a931c1701f99f8c3`)
- Tracked audit summary: `reports/generation/lf022_codex_sol_xhigh_v2_summary.json` (`sha256:4f82f1b00f4f5dd4cd04b3c3c72946d37c54512f657d451d6d16dd33b7fe6d5c`)
- Tracked findings: `reports/generation/lf022_codex_sol_xhigh_v2_findings.jsonl` (`sha256:69f37377902c5328bb0fa87e8dd014f3f19be095a228f2b3d3d805dcef451824`)

### Kimi v4 prefix-256: 248 generated variants

The 256-task Kimi batch ended with 248 generated variants, 6 provider-exhausted tasks, and 2 parse failures. The operational QA passed exact offline replay, found 248 unique normalized outputs within this batch, and observed no duplicate normalized-output hashes. LeanInteract then accepted 201 variants with a placeholder and rejected 47. Codex judged exactly the 201 Lean-valid variants.

The 201 audit-only judgments contain 198 `not_same_claim`, 2 `same_claim`, and 1 `ambiguous` result. The relation readout is 12 `A_stronger`, 83 `B_stronger`, 2 `equivalent`, 103 `incomparable`, and 1 `ambiguous`. These are one-model audit findings, not semantic labels.

Evidence:

- Batch manifest: `/localhome/milikic/LeanFaith-kimi-641d13d/data/lf022_kimi_v4_scientific_641d13d/prefix_256/batch/batch_manifest.json` (`sha256:1e0d6c8b86224ddab31930756e755a9ea6a51970745322248c66f99c2c6ffb1f`)
- Operational QA: `/localhome/milikic/LeanFaith-kimi-qa-97c250b/data/lf022_kimi_v4_scientific_641d13d/prefix_256/batch/operational_qa_v1/qa_report.json` (`sha256:f2e088ea35f2d4a425c47abd78d6644c05f1268b2c46899a2d590d64a6734565`)
- 32-item QA sample: `/localhome/milikic/LeanFaith-kimi-qa-97c250b/data/lf022_kimi_v4_scientific_641d13d/prefix_256/batch/operational_qa_v1/reviewer_sample_v1.jsonl` (`sha256:036a39ff1f8bad92bfcb94a4a43b4d3251a6adc71be7ec8a8ccdad9828ca30ce`)
- Lean-check manifest: `/storage/milikic/leanfaith/lf022_lean_checks/kimi_v4_641d13d_prefix256_v1/manifest.json` (`sha256:8805cec56a157ce955354b63d54e9714948856e437ead076f59b3295656b253d`)
- Lean checks: `/storage/milikic/leanfaith/lf022_lean_checks/kimi_v4_641d13d_prefix256_v1/checks.jsonl` (`sha256:46972e934b26e9ee6df112a6e135223f83267b58e93ccde2be79e40d6ed54810`)
- Codex audit manifest: `/storage/milikic/leanfaith/lf022_codex_audits/kimi_v4_641d13d_prefix256_v1/manifest.json` (`sha256:b2ea47f15495f88e8d1bb5703c3e7622c9b7b5a221685c0ed7526d27bf402c17`)
- Tracked audit summary: `reports/generation/lf022_kimi_v4_prefix256_codex_sol_xhigh_audit_v1.json` (`sha256:f5a68222412aba5bb56236aa3d1400f5fc14882ce045018de69eabab49a6e6be`)
- Tracked findings: `reports/generation/lf022_kimi_v4_prefix256_codex_sol_xhigh_findings_v1.jsonl` (`sha256:9552a840db82eb9e0600d3e8dec15bc8467085027b658a648a8f9eeba8e79b69`)

## Live Qwen full-batch snapshot

This is a point-in-time operational snapshot, not a completed artifact and not part of the completed gross totals above.

At `2026-08-11T05:28:57Z`, the 9,207-task Qwen batch had 877 terminal records:

- 855 provisional variants created;
- 16 provider-exhausted (`output_budget_exhausted`);
- 5 `transport_unknown`;
- 1 proposer parse failure (`proof_bearing_candidate`);
- 8,330 tasks without terminal records.

The batch was still active. Its post-generation Lean check and Codex audit had not started, so their counts for this live pipeline snapshot are both zero. Some task identities may overlap historical Qwen work in the completed 668-record artifact; this report does not add the live count to completed totals and makes no deduplication assumption.

- Batch manifest: `/localhome/milikic/LeanFaith-rcp-5e672b9/data/lf022_qwen3_scientific_5e672b9/prefix_9207/batch/batch_manifest.json` (`sha256:b40557e0dc179bc6e9132242da62f7bd4d7afef782b3cea7c25c199ed7c5cf98`)
- Snapshot digest over sorted terminal task ID, terminal-file hash, status, error, and variant count: `938d649f25e776d91b437b53c7349ea9c26a2317efc8b5b9ce6a267fb462aee0`

## Queued expansion

The Kimi v4 9,207-task batch is frozen and queued behind Qwen; it has not generated data and is not counted above.

- Batch manifest: `/localhome/milikic/LeanFaith-kimi-641d13d/data/lf022_kimi_v4_scientific_641d13d/full_9207/batch/batch_manifest.json` (`sha256:09204ebb1e4b8caeb3e99cacde6e0f59146da38ef30562ec8a20f302145b3ed3`)

## Interpretation boundary

“Lean-valid with placeholder” means the candidate statement elaborated under the registered Lean environment when its theorem body was replaced by a placeholder. It does not mean the candidate expresses the same claim as its reference. “Codex-judged” means a GPT-5.6 Sol xhigh audit completed; it is useful exploratory quality evidence, but it is neither human gold nor automatically admissible weak supervision. Confirmatory training readiness remains governed by the separate fail-closed readiness policy.
