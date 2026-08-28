# Deterministic V2 P14 recovery and resource/parity report

Captured at `2026-08-11T16:36:11Z` from code commit
`fd196dd3ef9cfce490151632b7121c69e62567b4`.

This is a point-in-time, audit-only report. It creates no semantic labels,
promotions, training eligibility, evaluation eligibility, or gate credit.

## Outcome

The exact legacy P14 recovery is valid and is accepted by the normal root
loader and the provisional-pair combiner. It exposes 1,888 provisional P14
observations. The operational schema-3 setting is `workers=1` with no
`memory_hard_limit_mb`.

The 12,288 MiB resource smokes and the incomplete unlimited worker-4 run are
diagnostics only. They are not admissible materialization roots.

## Exact legacy P14 recovery

Parent root:

`/storage/milikic/leanfaith/deterministic_v2/composition_second_hops/chain_8c18476_v1/full/p14`

Recovered root:

`/storage/milikic/leanfaith/deterministic_v2/composition_second_hops/chain_8c18476_v1/full/p14_recovered_fd196dd_v1`

The live parent still has exactly 5,087 files and tree hash
`126c9a1da9b59eb51b5c9f31c621a716ef9512e3ac629bbec21c8a112a47b89f`,
matching the recovery specification. Its run spec, manifest, and results hashes
also remain exactly bound by that specification.

The recovered root differs from the parent in exactly one canonical result:

- `results.jsonl` line 1066 changed;
- the corresponding journal location is `journal/batch_000016.jsonl` line 42;
- old result `v2e2_result:fe0b81b7f5582ae5b6dffbc0c1aa20cd3f978b0c3c01a687678c4be67e743e7c`
  had status `candidate_infrastructure_error`;
- replacement `v2e2_result:fa8f63eb5aa6e7c5c3f7514c11565e198b0177266c4ac37e50ddec04c0aa803a`
  has status `provisional_variant`;
- all other 3,940 result lines and all other 61 journal files are unchanged;
- the run spec is byte-identical to the parent run spec.

The recovery receipt records one candidate-validation Lean attempt. It used a
600-second timeout and returned `valid_with_sorry`. Its raw response has hash
`a4bd58d3a65fbe4207b71585d21860266a4a8c86e3e279c0b98ba34c9375d09a`.
The separate representation pipeline operation also returned
`valid_with_sorry`; it is not counted as a second candidate-validation
attempt.

The recovered manifest contains:

| Status | Count |
|---|---:|
| `provisional_variant` | 1,888 |
| `audit_quarantined` | 622 |
| `not_applicable` | 1,428 |
| `candidate_invalid` | 3 |
| Infrastructure error | 0 |

It has zero resolved labels, zero promoted items, and
`training_eligible=false`.

The normal loader accepted all 1,888 provisional observations. The existing
combiner audit is at:

`/storage/milikic/leanfaith/deterministic_v2/composition_second_hops/chain_8c18476_v1/audit/p14_recovery_combined_fd196dd_v1`

Its manifest hash is
`35be53204e6fad877c2fcb34cdc5d5e4ae758b25a7149562200d3712716249b3`.
Both its gross and unique outputs contain 1,888 records, with zero labels,
promotions, training eligibility, evaluation eligibility, or gate credit.

## Schema-3 bounded-memory parity smokes

The two roots are:

- worker 1: `/storage/milikic/leanfaith/deterministic_v2/composition_second_hops/chain_fd196dd_schema3_v1/smoke_parity/p14_w1`
- worker 4: `/storage/milikic/leanfaith/deterministic_v2/composition_second_hops/chain_fd196dd_schema3_v1/smoke_parity/p14_w4`

Both use `memory_hard_limit_mb=12288`, the same 64 inputs, two
infrastructure-only attempts, and fresh sessions between such attempts.

Both produced exactly:

- 21 `not_applicable` results;
- 43 `candidate_infrastructure_error` results;
- 86 raw error responses, two per applicable candidate;
- 86/86 raw errors containing Lean's `failed to create thread` failure.

Their `results.jsonl`, journal, and raw-response trees are byte-identical. The
common results hash is
`3bca6500d8221e3ba30f7530542b3857a924b0c0242f337d8c7196f38895c551`.
Their whole roots are not byte-identical because the run specs correctly differ
in `workers`, and the manifests therefore bind different run-spec hashes.

Both are intentionally non-admissible: the normal loader rejects the first
infrastructure result at line 1.

## Schema-3 unlimited worker-1 observations

The one-example resource probe is:

`/storage/milikic/leanfaith/deterministic_v2/composition_second_hops/chain_fd196dd_schema3_v1/smoke_resource_probe/p14_w1_nomem_1`

It completed with one `provisional_variant` and is accepted by the normal
loader.

The 64-example worker-1 root is:

`/storage/milikic/leanfaith/deterministic_v2/composition_second_hops/chain_fd196dd_schema3_v1/smoke_parity_nomem/p14_w1`

It completed and is accepted by the normal loader with:

- 32 `provisional_variant`;
- 21 `not_applicable`;
- 11 `audit_quarantined`;
- zero infrastructure errors.

It has zero labels, zero promotions, and `training_eligible=false`.

## Schema-3 unlimited worker-4 observation

The partial root is:

`/storage/milikic/leanfaith/deterministic_v2/composition_second_hops/chain_fd196dd_schema3_v1/smoke_parity_nomem/p14_w4`

It was terminated before publication. It has a run spec and 89 raw responses,
but no journal, results, or manifest. The normal loader therefore rejects it as
incomplete. At capture time no process for this run remained alive.

The partial raw tree contains 85 successful transport responses and four
`MemoryError` responses, including two retry files. During execution, operator
process monitoring observed three Lean REPL RSS values of approximately 34,
21, and 16 GiB and system memory use near 54 GiB before termination. These
figures are approximate operational observations; no persistent monitor log
was produced, and per-process RSS is not additive when processes share pages.

This partial directory is not an accepted root and must never enter a combine
or data release.

## Operational decision

Use:

```text
workers = 1
memory_hard_limit_mb = null
```

The 12,288 MiB limit reproducibly caused Lean thread-creation failures for all
43 applicable inputs. Unlimited worker 1 completed the same 64-input smoke
with zero infrastructure errors. Unlimited worker 4 failed to publish and
showed excessive concurrent REPL memory growth.

The machine-readable companion report contains every bound path and hash used
for these conclusions.
