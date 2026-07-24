# LF-021 Public Research Problem-Pool Preflight v1

Status: **passed as a model-free preflight only**

This preflight establishes a small, reproducible public research problem pool
for later LF-021 generator execution. It does not execute a generator, create
semantic labels, estimate prevalence, or close Gate 5/Gate 5G.

## Result

| Profile | Records | Eligible | Three active-registry screens clear |
|---|---:|---:|---:|
| one-example preflight | 1 | 1 | 1 |
| three-record slice | 3 | 3 | 3 |

The one-example path completed before the three-record path. All three records
come from public mathlib docstring/theorem pairs first introduced after the
FormalRx submission cutoff. Every record retains
`formalrx_lineage:mathlib_docstring_theorem_pairs`; none is claimed to be
source-independent, unseen by model pretraining, or held out from FormalRx.

The first record's natural-language claim is an exact contiguous docstring span
covering only the inequality formalized by `Real.sin_ge_sub_cube`; the
best-constant sentence is excluded. All three claim spans and their mechanically
normalized statements are hash-bound and reverified.

## Lean execution

All reference statements elaborated through LeanInteract. The request includes
an executable guard requiring `Lean.versionString == "4.31.0-rc1"`; that guard
passed. The LeanInteract server-start banner is not used as the runtime
toolchain assertion.

- Context ID:
  `ctx:45a3a59bdf49fbdfb280f0a6508b4c03ba51b3a8189b211f87db8e0f6d60aaa8`
- Runtime-version guard request:
  `f4ada5854ae32911c1fba2c0abfb7f47f05609b351a12fbae8f175a8d86a0994`
- Execution mathlib revision:
  `d568c8c09630de097a046763c17b9ea99f95f950`
- Public source snapshot:
  `c368140668f5fa16a1bd977448c1f665d48c3df4`

## Benchmark screening

Each record ran all three active-registry screens:

1. problem identity and natural language;
2. reference Lean text;
3. reference representation.

All nine hit sets were empty.

- Frozen benchmark manifest:
  `4ffbd31dd0e10efb9dfe7e57fb815690f3e1d750e4640aeff959ca4cbdc911df`
- Active benchmark registry:
  `613441d568b51a231efe653a2622d01e1aa5d7b31eb29d6b6f9ff17bbe30feed`

## Immutable artifact hashes

- One-example manifest:
  `ecfa7f505dc3237ed912c6c9fa8f53d6dfab3b10cf7b9995f9d15d0b4b80555e`
- One-example report:
  `1afab8f40e04a20f48be6546cb28c2a24c30ef65e61d631cf57f6ddb2f836468`
- Three-record manifest:
  `e823da6c31c14136f1797419ed728fd901c5a27cf9ea398382bbd9ebd8d86d87`
- Three-record report:
  `527d2b4f7d5b5f995623f73889f8a0743d0a1e782c5efa0f1bd8b0116a8810a6`
- Implementation:
  `d5287140ea7379480a3320e975a38fd00a6ea797b3111de57635e0d060edbf03`
- Unit tests:
  `72850f1953d3bc474beea2d642237651c57a72f3b1cda37a1fbe7e5f90b7848f`
- CLI script:
  `adb5a972f1be86c1ccc656351f20ea2c71da2cf6862eafce02c7132d1418b55e`
- Raw public source records:
  `7a46b38e883eb4fc29edec94b575efff5280103ae6e83ea44205029a077b0000`

The complete one-example and three-record pipelines were rerun after the final
review fixes. Their compound replay hashes were byte-identical:

- One-example replay:
  `515a20372bbdcc9f1580e90c5547d55b885266d21338ab5d792bb6267625b5f4`
- Three-record replay:
  `135da2c9157760a2c127ff5c9400093500b2445349c608bfbbb7c577c8380a6d`

## Scope and remaining blockers

- No generator was executed and no semantic label was created.
- Three records are not a prevalence frame and cannot close Gate 5/Gate 5G.
- Pretraining contamination is unknown for every candidate generator family.
- References are textually derived and cross-elaborated, not kernel-compared
  across source snapshots.
- Evaluation registries without pinned artifacts remain protected by name but
  cannot yet be exact-hash screened.
- Every generator family still needs a frozen scientific run configuration and
  family-specific overlap record before research execution.

