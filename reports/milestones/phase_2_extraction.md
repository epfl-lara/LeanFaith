# Phase 2 — Ingestion and benchmark freeze (Revision 4.1)

**Updated:** 2026-07-18
**Decision:** **PASS**
**Authorization:** LF-016 remains blocked until Gate 3 passes.

Gate 2 establishes deterministic, fully accounted theorem-statement ingestion
and freezes the exact 10,000-theorem denominator for Gate 3. Non-elaborating
and non-Prop rows remain explicit failures rather than being called theorems.

## Prerequisites

- Gate 0: `pass_internal_research_only`; SHA-256
  `4ca346600b867324ef39632922e339f19275297e7a362cefe6fc470feb684e9c`.
- Gate 1: `pass`; SHA-256
  `638690d5f49be3d6673b2b01fca800fd89d04584b605b0a4328eedc4b7095971`.
- Doctor: 7/7, zero warnings; SHA-256
  `587fd3dae6c251c31244deb429d437dc241b94a8695ddb9b0485df7240dca706`.
- Current verification: 460 pytest tests, Ruff lint/format, strict mypy over
  59 source files, API probe, doctor, and Lean fixtures all passed.

The private `sft_classic` source remains internal-research-only and cannot be
redistributed or sent to external providers.

## Immutable 100-row regression

Input SHA-256:
`9913ae837d021d6e9857659346fe47088762c3ab19dc378551e77a5bc0be38cd`.
Expected-outcome SHA-256:
`7aaf607c2dbff00f37242fa75015c6ddc7625c60e6e27be602592c17f40cead8`.

| Outcome | Rows |
|---|---:|
| accepted question theorem | 85 |
| accepted `lean_code` fallback | 10 |
| elaborating non-Prop definition | 1 |
| source non-elaboration | 4 |
| **total** | **100** |

Thus 95 rows yield accepted propositions. The apparent 86th question-route
elaboration is `def halfRoundDown`, not a proposition. The artifact has 97
declarations, 95 accepted declarations, two declaration failures, four
partial-declaration diagnostics, and seven failure records. Every route and
signature matches the per-row oracle.

Artifact hashes:

- manifest `317dbbc00857db543b9773b4b54f81f40d870dbd6d23265856097aae83606bc8`;
- theorems `ad2c3bf8509d21a09c88ad7715255391f94cb9283c987aa888e61160ae520ed2`;
- failures `5edad96bdf5e913e775f999f8b655b5b30562caf9500e84318b0057b4afb7946`.

## Frozen 20,000-row `sft_classic` audit

Pre-extraction-stratified sample SHA-256:
`de589184690baa7ac89d5a3c542702db793dced493d04aca9b5e92d0079dc41d`;
sampling-manifest SHA-256:
`12258cd258d8ef50ba2082d009c0ef82d8678dd9c8980629fe1450c93f1288c5`.

Both independent executions reconcile exactly:

| Item | Count |
|---|---:|
| input / accepted / failed rows | 20,000 / 18,643 / 1,357 |
| theorem / failure records | 18,669 / 1,584 |
| declarations seen / accepted / failed-skipped | 18,896 / 18,669 / 227 |
| partial-declaration diagnostics | 1,266 |
| question / fallback accepted rows | 16,867 / 1,776 |
| non-Prop / source-failure rows | 95 / 1,262 |

Audits A and B are byte-identical, `ok=true`, with no errors; SHA-256
`fd6115b6547e37615f39b3d64f9dd704e28c48cfa49088079f914d839fe4f425`.
The normalized replay is `ok=true`, compares 18,669 theorem and 1,584 failure
records per side with zero errors; SHA-256
`3ced74e5a00734766c5bfb7dcaab49a65826e08a94d8c4335e0855f31df27de5`.

Run A hashes: manifest
`0674aafe8f2954e1bb667b00cd8db42aede078f066214ee4542e2f228ad2790e`,
theorems `d5840eca5eca9f348547df64a7432bab3d343bff155a03e7ba327479e11f6e4c`,
failures `d4e7010859f0f2a448857ca84e1897a79db22c72ed615941bc0a0c89fb4d4611`.
Run B hashes: manifest
`e4ddeb055c708bb53a36662ace15c0cd85349e3b8e9bf0e549f900214e891381`,
theorems `ec343c5fc468641dc729eb6d57b4557c5d2a75a8402a4bd2c9742841d8f31cc0`,
failures `d4e7010859f0f2a448857ca84e1897a79db22c72ed615941bc0a0c89fb4d4611`.

The theorem files differ bytewise only in allowed operational timestamps;
normalized replay requires exact semantic and terminal-outcome equality. The
Run-A code bundle SHA-256 is
`21769ae87ef126cf725a32abf20c0e97561af1dbc0fa0865d653b2f9258c882d`.
It includes an unused `extract_run.py.orig` backup that was never imported or
executed. The deviation is disclosed; the file is absent from the current
tree and the Gate-3 bundle.

## Pinned mathlib extraction

The pinned 400-file extraction has 400 explicit file outcomes: 281 accepted,
51 with no accepted proposition, 66 non-elaborating, and two elaborating with
no declarations. Its 9,999 declaration outcomes are 6,397 accepted and 3,602
failed/skipped; all 3,670 failure records reconcile. There are 6,386
transform-eligible propositions; 11 accepted `autoParam` signatures are
explicitly excluded. All 401 input and all output checksums verify.

- manifest `e38138d23b964d3b409ac03c878f4761c88ea2d8258480d493c976eae4fb9c23`;
- theorems `3a2e49b9481ab903fba6d2b0a28e54fa4d1cbb75b331296aae7b77251644323e`;
- failures `632404b0b7379c82c3011f734af30c635b153229ade698ece7fdea1e200e4e0f`;
- integrity audit `243c3e055a7da561a5cba52d6cadd221aabde12504a1d1755558a2f8f23b86f6`;
- code bundle `b78ac2357280f114fa2b523ea6cc38c61e4b589c2d0311426e79ae5fde20642e`.

All extracted theorem records use context
`ctx:0cd06826b8767b3bc951c0eb00c802424af95785b558f9f8a61f18694a86c4ce`.

## Exact Gate-3 denominator

| Source | Input | Eligible | Selected |
|---|---:|---:|---:|
| mathlib | 6,397 | 6,386 | 5,000 |
| `sft_classic` | 18,669 | 17,933 | 5,000 |
| **total selected** |  |  | **10,000** |

The schema-v2 freeze uses `gate3_equal_source_hash_order_v1`. It has one
context, no duplicate/cross-source theorem IDs, and permits no denominator
change after freezing.

- manifest SHA-256
  `19f5c38ea15bbc72c97fe73be6f4a50d5491e3e27cdf024cc05889d4eb1471e3`;
- exact 10,000-row partition SHA-256
  `8eb75ffa0b9233c5a91492fa181f604e3c098a6f3970799bcb0406f8b517f09e`.

## Decision

All Gate-2 unit, fixed-regression, scale, accounting, replay, source-scale,
equal-source-freeze, immutable-denominator, prerequisite, and archived-code
requirements pass. Gate 2 is closed. Gate 3 must run on the exact frozen
partition; LF-016 remains unauthorized until Gate 3 closes.
