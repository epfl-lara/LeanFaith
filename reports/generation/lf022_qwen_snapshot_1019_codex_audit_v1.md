# LF-022 Qwen 1,019-candidate snapshot and 718-item Codex audit (v1)

## Status

This is a completed, hash-bound point-in-time report for the Qwen snapshot that
produced 1,019 candidate records for Lean checking and 718 completed Codex audit
opinions. The run completed at `2026-08-11T15:51:06Z`.

**These are audit-only observations.** They are not resolved semantic labels,
human gold, silver records, training items, evaluation items, promotion
evidence, or gate credit. A Lean-valid candidate merely elaborated after its
proof was replaced by a placeholder; that does not establish truth or
faithfulness. The Codex opinions came from one judge family in one AB
orientation, so they do not satisfy the two-family, swapped-order weak-consensus
contract.

## Bound immutable artifacts

| Artifact | SHA-256 | Role |
|---|---|---|
| `/storage/milikic/leanfaith/lf022_codex_audits/qwen3_5_397b_incremental/6472682bd2de081e9b007d82eb374556b2fb95ec29f0ea2c39ab2f42ca1582ee/gpt-5.6-sol_xhigh_v2/manifest.json` | `e6d1ea0ba1c391dd63896465cb77c2ed2f2b7bb71da708baacaba6ca570497f9` | Audit manifest: 718 eligible and completed, 717 invoked, 1 reused, 0 exhausted |
| `/storage/milikic/leanfaith/lf022_codex_audits/qwen3_5_397b_incremental/6472682bd2de081e9b007d82eb374556b2fb95ec29f0ea2c39ab2f42ca1582ee/gpt-5.6-sol_xhigh_v2/schemas/judge_response.9de1b73c98a5df344ac158f77ead4b1b6e118b4c2f5585335fd5a3bcf0dea4d4.schema.json` | `9de1b73c98a5df344ac158f77ead4b1b6e118b4c2f5585335fd5a3bcf0dea4d4` | Judge-response schema |
| `/storage/milikic/leanfaith/lf022_lean_checks/qwen3_5_397b_incremental/6472682bd2de081e9b007d82eb374556b2fb95ec29f0ea2c39ab2f42ca1582ee/manifest.json` | `f106d1a299c89eca5b5c82df562bb6522b42ca1de79b9d2caa91dd1a042d65e9` | Lean-check manifest: 1,019 candidate records |
| `/storage/milikic/leanfaith/lf022_lean_checks/qwen3_5_397b_incremental/6472682bd2de081e9b007d82eb374556b2fb95ec29f0ea2c39ab2f42ca1582ee/checks.jsonl` | `b53736c22fb7cc134081e33a9a4f7e5e1724d0e3f170b6930b05658ef0a2d6c4` | All 1,019 Lean-check records |
| `/storage/milikic/leanfaith/lf022_qwen_snapshot_1019_codex_audit_0e8d84c/findings.jsonl` | `8a20bf79ca24a9d524db497dacdf71b871d7404e00995fd0890c8b9c2186cd6f` | 718 compact audit findings |
| `/storage/milikic/leanfaith/lf022_qwen_snapshot_1019_codex_audit_0e8d84c/summary.json` | `8999a5aa8b734dcc5cc7a63886eb23be9ab31075ba6a6052c815df3248613603` | Machine-readable source summary |
| `/storage/milikic/leanfaith/lf022_qwen_snapshot_1019_codex_audit_0e8d84c/summary.md` | `564bf55a93b25816b01fd1a5d1d3c50f26a9c66131a9b494b1be6e01d5cd284f` | Source summary rendered for readers |
| `/storage/milikic/leanfaith/lf022_qwen_snapshot_1019_codex_audit_0e8d84c/run.log` | `34c04eb2ecadae7725cc2bae907c44d0c7d1ccb91149aec81285f9ce6358000e` | Completion log |
| `/storage/milikic/leanfaith/lf022_qwen_snapshot_1019_codex_audit_0e8d84c/run.status` | `6246b6cae6d1a8f743a1dc6fc0db0a1c98935dd17e33469e20e3abc073bfff1d` | Terminal status |

The verified response artifact-set digest recorded by the source summary is
`b88ab25256d257fdd6421ed2a155e3cf33c63e2f3acdde6d6e790aaf371cf717`.
The proposer family is `qwen3`, registered as
`Qwen/Qwen3.5-397B-A17B`; the judge is `gpt-5.6-sol` with `xhigh`
reasoning.

## Exact counts

### Generation and Lean checking

| Quantity | Count |
|---|---:|
| Selected terminal execution tasks | 1,046 |
| Candidate records checked | 1,019 |
| Unique variant IDs | 1,019 |
| Elaborates with placeholder | 718 |
| Invalid | 301 |

The 1,019 records are bound by ordered-variant digest
`1f743f6e968bb970c686670a6c44d931c9579c1d53a6e5636cd2191ea9593860`.
No denominator filtering is performed in this report.

### Codex audit opinions on the 718 Lean-valid candidates

| Field | Counts |
|---|---|
| Same claim | `same_claim` 16; `not_same_claim` 701; `ambiguous` 1 |
| Relation | `equivalent` 16; `A_stronger` 98; `B_stronger` 286; `incomparable` 316; `unrelated` 1; `ambiguous` 1 |
| Directional implication | `A=no,B=yes` 569; `A=yes,B=yes` 125; `A=yes,B=no` 10; `A=no,B=no` 7; `A=unknown,B=unknown` 5; `A=yes,B=unknown` 2 |
| Error tags | `E01` 145; `E03` 1; `E08` 1; `E10` 2; `E11` 1; `E17` 1 |
| Needs expert review | 1 |

All 718 eligible items completed. Of those, 717 invoked a new judge run and 1
reused the completed smoke result. Mean reported confidence is `0.988287`, with
minimum `0.90` and maximum `0.99`. These confidence values are uncalibrated
judge self-reports, not probabilities of correctness.

## First-data preview

The examples below were selected for compactness and outcome variety. They are
not random or statistically representative. A is the mathlib reference; B is
the generated Qwen candidate. Every verdict is explicitly a Codex audit
opinion.

### 1. Equivalent definitional presentation

```lean
-- A
theorem tendsto_diag : ∀ {α : Type u_0} {f : Filter α},
  Filter.Tendsto (fun i => (i, i)) f (f ×ˢ f)

-- B
theorem tendsto_diag : ∀ {α : Type u_0} {f : Filter α},
  Filter.map (fun i => (i, i)) f ≤ f ×ˢ f
```

- Audit opinion: `same_claim`, `equivalent`, confidence `0.99`.
- Audit item: `lf022_codex_audit_item:61c3fb2d444bb9cb3a05d41ef7b6e4cb392ceef76d597765953f72bd7999dbbb`.
- Bound input SHA-256: `9e58ae1266347571daef7683d11d2cb051f08d056daf68a20d14c3295385522f`.
- Plain-language reading: `Tendsto` is represented by the corresponding
  filter-map inequality, so this is a useful non-cosmetic positive candidate.

### 2. Dropped direction of an equivalence

```lean
-- A
theorem nat_iff : ∀ {f : ℕ → ℕ}, Primrec f ↔ Nat.Primrec f

-- B
theorem nat_iff : ∀ {f : ℕ → ℕ}, Primrec f → Nat.Primrec f
```

- Audit opinion: `not_same_claim`, `A_stronger`, confidence `0.99`.
- Audit item: `lf022_codex_audit_item:1a6b0b8c08ae9f5ddc69b219f784a7252ef678f66ab12fcfe971b7968937dc08`.
- Bound input SHA-256: `4bde95e21ef03ca4c30307baceb905b2cdcdddce05d1a200cd33131b9b82ff7d`.
- Plain-language reading: B keeps only one direction and therefore loses part
  of the reference claim.

### 3. Enlarged domain makes the candidate stronger and false

```lean
-- A
theorem eq_zero : ∀ (n : Fin 1), n = 0

-- B
theorem eq_zero : ∀ (n : Fin 2), n = 0
```

- Audit opinion: `not_same_claim`, `B_stronger`, confidence `0.99`.
- Audit item: `lf022_codex_audit_item:b84bac4c9122118bdbcd4b859a8f30886b7607f100f32265ebd3c763e91e270e`.
- Bound input SHA-256: `058214a95d94f616c6e563a7dc4ea3f2454c852a22eac9bcf3e0a737bb8a6526`.
- Plain-language reading: every element of `Fin 1` is zero, but `Fin 2` also
  contains one. B makes a broader and generally false assertion while still
  elaborating as a well-formed proposition.

## Scientific boundary

This artifact is useful as the first stable view of the expanded Qwen data and
as a diagnostic of proposer and judge behavior. It does not authorize admission
into the resolver or any training partition. Promotion still requires the
separate typed authority, swapped-order, independent-family, and/or human-label
policies defined by LeanFaith.
