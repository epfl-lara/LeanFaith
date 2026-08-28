# Phase 3 — `repr_v3` frozen-denominator revalidation

**Updated:** 2026-07-30  
**Decision:** **PASS**  
**Scope:** current `repr_v3` only

This report closes the version-specific revalidation required after the
historical Gate-3 decision was completed under `repr_v2`. It does not rewrite,
rename, or invalidate the historical `repr_v2` gate report. It establishes that
the current `repr_v3` representation implementation is scientifically usable
on the same immutable Gate-3 denominator.

## Frozen denominator and execution identity

- Frozen input manifest:
  `/storage/milikic/leanfaith/gate3/frozen/gate3_inputs.json`,
  SHA-256
  `19f5c38ea15bbc72c97fe73be6f4a50d5491e3e27cdf024cc05889d4eb1471e3`.
- Frozen theorem partition:
  `/storage/milikic/leanfaith/gate3/frozen/gate3_inputs.theorems.jsonl`,
  SHA-256
  `8eb75ffa0b9233c5a91492fa181f604e3c098a6f3970799bcb0406f8b517f09e`.
- Denominator: exactly 10,000 theorems, comprising 5,000 mathlib and 5,000
  `sft_classic` records; no post-freeze filtering.
- Clean source revision:
  `df7baa31f3a6599227d046fb135af866cffa15a6`.
- Code-tree hash:
  `bac264620af32882d9401f476f8158150b1a28f8cd9e5eaa34cd751e7527e628`.
- Archived code bundle:
  `/storage/milikic/leanfaith/gate3/run_repr_v3_df7baa3/code_bundle/code_bundle_c4f2520883299b07ff98c8cefb395a6d52e6f0e57291bb66028406885e8b99c4.tar.gz`,
  SHA-256
  `c4f2520883299b07ff98c8cefb395a6d52e6f0e57291bb66028406885e8b99c4`.
- Representation config hash:
  `8feb5b8e2b8f174f01252cff7a64472208ac7204502caa8892406bbdfcd6d501`.
- Context hash:
  `b69bafa2af918b2d452466964e81c7a9e7783f2f44ecdfdd2da0587c52f8c63d`.
- Environment hash:
  `e447ac3a773b0d29ec75b51bcfa5318158399e9fe7459a650f9d0bfef9986298`.

Both scale runs used `repr_v3`, the same frozen inputs, the same clean code
bundle, one worker, 500-record chunks, and the recorded 49,152 MiB Linux
per-REPL hard limit. Those values document this execution and are not a
hardware prescription.

## Frozen-scale results

Run A and Run B each represented all 10,000 inputs with zero view failures.
Every threshold passed both overall and separately for mathlib and
`sft_classic`.

| View | mathlib | `sft_classic` | Overall | Required |
|---|---:|---:|---:|---:|
| `raw_proof_stripped` | 5,000/5,000 | 5,000/5,000 | 10,000/10,000 | 100% |
| `headless` | 5,000/5,000 | 5,000/5,000 | 10,000/10,000 | 100% |
| `signature_pp` | 5,000/5,000 | 5,000/5,000 | 10,000/10,000 | at least 99% |
| `signature_explicit` | 5,000/5,000 | 5,000/5,000 | 10,000/10,000 | at least 99% |
| `semantic_atoms` | 5,000/5,000 | 5,000/5,000 | 10,000/10,000 | at least 99% |
| `operator_tree` | 5,000/5,000 | 5,000/5,000 | 10,000/10,000 | at least 98% |

The two mechanical audits have `mechanical_pass=true`. Their embedded
`gate_pass=false` is expected: each was written before the separately required
lossy-collision review was attached. The terminal collision-closure artifact
has `manual_audit_status=complete`, `review_errors=[]`, and `gate_pass=true`.

## Determinism and invariance

- Semantic replay compared 10,000 Run-A records with 10,000 Run-B records and
  returned `ok=true`.
- All 10,000 representation IDs and content hashes replay exactly.
- The two raw record-partition SHA-256 values differ only because
  `created_at` is intentionally different in every record. Removing that
  operational field makes all remaining fields equal record by record; the
  canonical replay stream then has SHA-256
  `3e8196934ef40e8f6c8a5b01352d2751d9358d3b9159dc3db446c1b4d199100f`.
  The raw files have equal byte lengths; no scientific field differs.
- The property-only renamer passed 1,000/1,000 alpha-invariance cases.
- The name-versus-inline audit passed 500/500 selected mathlib theorems.
  Raw `signature_explicit` text was identical for 477; all 500 were identical
  after the preregistered first-occurrence normalization of generated
  `u_<n>` universe placeholders.
- Both mechanical audits report zero cryptographic/canonical-alpha collisions,
  zero proof-leakage errors, zero missing or unexpected theorem IDs, zero
  duplicate representation/theorem IDs, zero normalization errors, and zero
  content/context/source-view errors.

## Lossy-view collision closure

The current `repr_v3` audits enumerate 152 lossy clusters:

- 141 `semantic_atoms` clusters with
  `semantic_atom_projection_loss`;
- 11 `signature_pp` clusters with `pretty_print_erasure`.

All 152 were rechecked under `repr_v3`; all have disposition
`expected_lossy_projection`. The 141 semantic-atom keys exactly match their
prior terminal technical reviews. For the 11 pretty-print clusters, theorem
identities and alpha fingerprints match the prior reviews while view hashes
changed with the corrected canonical `repr_v3` printer. No cluster indicates a
cryptographic, canonicalization, proof-leakage, or implementation defect.

## Immutable evidence

All paths below are rooted at
`/storage/milikic/leanfaith/gate3/run_repr_v3_df7baa3`.

| Artifact | SHA-256 |
|---|---|
| `run_a/manifests/gate3.json` | `69d70157681442a06bcecd4d97715ff102a59bd73c04028f06d74928b064ab78` |
| `run_a/records/gate3.jsonl` | `cd0a706448a1df2effab245ca175aa918c34f28f3f840ed666361f84397602ae` |
| `run_a/failures/gate3.jsonl` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `run_b/manifests/gate3.json` | `569834943103f2a7bbfb790ec1e06f49398dadbb7abbe765e6eddfc1868f9735` |
| `run_b/records/gate3.jsonl` | `d1947ba5ec7f7f62ba2c2462a4fe116ff54f3ebe54b5eee26b4e098c9398d27c` |
| `run_b/failures/gate3.jsonl` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `audits/run_a_mechanical.json` | `3075c09f29a0477e5daf65652dc427aa2b6eb6ceb7e67949997fd6c0d4252b1d` |
| `audits/run_b_mechanical.json` | `7c3faad885d8c188fc13c550d942cc4c0f05bed190bf45cb4f49ced3747fd1a2` |
| `audits/run_a_run_b_replay.json` | `8b0facd7ca5ee09cbeadae0320a7d97d6f69461aa25cb6fb43c77770623fccbb` |
| `audits/alpha_invariance_1000.json` | `aae627729b578d28bcb7758cad2462cfaa616b6657085506ac32b1da04229f17` |
| `audits/cross_path_500.json` | `5f0a7c6c532b92f3c48e47042341839e85ff638469cef9cd7dbc8f59c6f0dbf2` |
| `audits/manual_collision_reviews.jsonl` | `3bddc7e8c041f304afff89466879d9810c3094b1f1cb67b0a90cc6cd24f66e86` |
| `audits/collision_closure.json` | `ea74649b5c8b2cd3676f31b6fe51c5a644a17c3199defe1c9e995e10f3e76993` |

## Decision

The current `repr_v3` implementation passes the immutable-denominator,
per-source coverage, identity, deterministic replay, alpha-invariance,
cross-path, collision, and proof-leakage requirements. `repr_v3` is therefore
scientifically Gate-3 validated for new artifacts.

Historical `repr_v2` artifacts remain versioned historical evidence. This
decision does not silently relabel or rewrite them; downstream artifacts that
require `repr_v3` must reference or regenerate current representation records.
