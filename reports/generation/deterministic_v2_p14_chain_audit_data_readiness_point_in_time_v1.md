# Deterministic-v2 P14 chain audit and data readiness — point in time v1

Captured at `2026-08-11T17:14:01Z` from repository commit
`fecdb3411a25f8d97c506e9748566c1f7b1ddff2`.

## Result

The recovered P14 second-hop artifact is complete and mechanically consistent:
1,888 unique depth-2 `P_to_P` chain records were recovered from 3,941 clean E2
positive seeds. Every second hop is `p14_independent_binder_permutation`; the
3,941 attempts reconcile to 1,888 provisional variants, 1,428 not-applicable
results, 622 audit quarantines, and 3 invalid candidates.

This is **not 1,888 novel training examples**. Comparing each final
alpha-identity fingerprint with its bound original seed gives:

| Result relative to original source | Count |
|---|---:|
| Alpha-novel | 555 |
| Returned to the source alpha identity | 1,333 |
| Total chains | 1,888 |

The 1,333 alpha cycles consist of 1,328 P14→P14, 2 P15→P14, and 3 P18→P14
chains. The 555 alpha-novel chains consist of 294 P14→P14, 29 P15→P14,
11 P16→P14, 1 P17→P14, and 220 P18→P14 chains. There are no exact
source-to-final content-hash returns, and all 1,888
`(original_source_theorem_id, final_candidate_code_hash)` keys are unique.
That syntactic uniqueness does not override the alpha-cycle result.

Every chain remains provisional and intention-only. The artifact creates zero
semantic labels, zero resolved labels, zero promotions, zero training-eligible
records, zero evaluation-eligible records, and zero gate credit.

## Bound artifact

| File | SHA-256 |
|---|---|
| `/storage/milikic/leanfaith/deterministic_v2/composition_second_hops/chain_8c18476_v1/audit/chains_p14_recovered_fd196dd_v1/manifest.json` | `d25b08560a616c82221bde18b69662f761390a6641e0d7d3e096bbe7dc7314e6` |
| `/storage/milikic/leanfaith/deterministic_v2/composition_second_hops/chain_8c18476_v1/audit/chains_p14_recovered_fd196dd_v1/chains.jsonl` | `e6de0fa715a7ceb6892e22e0753aea756dbe1f67df39775bed82b8a2c4ae3940` |
| `/storage/milikic/leanfaith/deterministic_v2/composition_seeds/all_public_private_p12_p18_n18_6ac2068_v2/manifest.json` | `dba3314d4f5e19f08c0115fc0dc7bb265712fd5622c123a60eec4223d2ab0d30` |
| `/storage/milikic/leanfaith/deterministic_v2/composition_seeds/all_public_private_p12_p18_n18_6ac2068_v2/seeds.jsonl` | `5f424037dc4a12495a417f569f7b32d3cb8fccfddb5ef35aafa4bb8482cd6543` |

The chain file hash matches its manifest, all 1,888 chain IDs are unique, all
seed bindings resolve, and every per-record eligibility/label/promotion field
matches the zero-credit policy. The second-hop root is bound by root ID
`detprov_root:359596c5c515b60feef03f1d4fe3aaed18bc165dd1ef7729c6b9d3ae3dfd91ce`
and tree hash
`631508e9e32c235c4064cc22528216f484ff884de41de72db4137fb4ee4e0bfc`.

## Current data-readiness inventory

| Stage | Current mechanically verified inventory | Training status |
|---|---:|---|
| Deterministic unary public/private union | 11,208 unique source–candidate pairs: 3,738 mathlib and 7,470 `sft_classic` | Audit-only |
| Clean deterministic positive composition seeds | 3,941 E2 seeds | Audit-only |
| Thirteen-family schema-3 composition smoke | 832 attempts; 145 provisional variants | Smoke-only |
| Recovered P14 composition chains | 1,888 chains, of which only 555 are alpha-novel | Audit-only |
| LF-021 real outputs | 1,440 invocations; 299 compiling and benchmark-clear observations; 49 duplicate members; 250 unique problem-aware units; 240-item unresolved frame | No semantic labels or supervision |
| LF-022 LLM variants | 1,967 gross checks; 1,502 unique source–candidate pair keys; 1,106 unique Lean-valid pair keys; 1,080 unique audited pair keys | Audit-only |
| Effective resolved training inventory | 0 safe F1 labels; 0 nonduplicate training records | **NOT READY** |

These rows overlap and represent different pipeline stages. They must not be
added together as a dataset size. In particular, “generated,” “Lean-valid,”
“audited,” “semantically resolved,” “promoted,” and “training-eligible” mean
different things.

### LF-022 identity caveat

LF-022 currently has 1,529 unique `variant_id` values but only 1,502 canonical
source–candidate pair keys. Among Lean-valid observations, 1,130 variant IDs
collapse to 1,106 canonical pair keys. This is expected because `variant_id`
includes generation lineage such as the LLM call and proposal index. The data
unit used for deduplication is instead the hash of sorted source theorem IDs
plus the candidate-code hash.

The four replayed checker partitions contain 668 legacy, 248 Kimi, 1,019 Qwen,
and 32 DeepSeek observations. All 1,967 check records replayed to their exact
source artifact, source line, variant ID, and candidate hash with zero binding
failures. However, duplicate-pair audits contain disagreements: 3 pair keys on
same-claim, 32 on relation, 20 on directional implication, and 51 on at least
one core judgment field. These are further reasons to keep the inventory
audit-only rather than interpret Codex judgments as resolved labels.

## Bound tracked summaries

| Summary | SHA-256 |
|---|---|
| `reports/transformation_audits/lf033_equality_composition_inventory_v1.json` | `0a853ac12084d141e23da9546a81c9c9a03c7971e39aaf874508645c5c573168` |
| `reports/generation/deterministic_v2_schema3_13_family_smoke_receipt_point_in_time_v1.json` | `08fbe7f204087a4dce07e9e0af04c0e3c04a1a257795db6807d62429a1688967` |
| `reports/generation/lf021_post_exhaustion_frame_v1/decisions/0574621043042ed62b486260de9bf633797f2471217f502be25b9aec3a46a19c.json` | `e780d3bc6150f2fafe576170be792b6f13e60d7ef35567912d27d03b63d8eabc` |
| `reports/model_selection/training_data_readiness_v1.json` | `8b20325087a094a2e27f5f30b98f9a81c710cd3e68a08c77820f9c9d7ba9d97a` |

The machine-readable companion report is
`reports/generation/deterministic_v2_p14_chain_audit_data_readiness_point_in_time_v1.json`.
