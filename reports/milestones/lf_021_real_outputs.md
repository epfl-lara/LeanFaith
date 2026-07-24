# LF-021 — Real-output collection

**Status:** mechanical collection and Gate 5G complete; human prevalence
annotation and Gate 5 remain open  
**Date:** 2026-07-24  
**Scope:** Phase-5 raw collection, Lean validation, screening, and prevalence
frame preparation

## Current boundary

LF-021 completed 16 replay-verified tranches: 12 original and 4
post-exhaustion extension tranches. They contain 1,440 terminal invocations,
299 compile-and-benchmark-clear members, 49 duplicate members, and 250 unique
problem-aware eligible units. The immutable prevalence frame contains 240
unresolved `REVIEW` items over 31 strata.

The production scope is `three_family_collection_only`: 67 frame items are
from Goedel, 108 from Kimina, and 65 from StepFun; 149 are from the algebra
pool and 91 from the cross-domain pool. This supports a reduced-data ablation,
not confirmatory D4/D5 or clean held-out-generator claims.

Gate 5G is explicitly closed by `reports/gates/gate_5g.json`. Its exact mixed
lineage is
`lf021_gate5g_lineage:ddac5e106c92b263ed96c9974eeadcef25f4980f1e777b06bca75de133b0aa1d`
with SHA-256
`a2bb9dba960a7906057647162a6ba00e17f26d0aa89180940e9e6112138ca761`.
Canonical coverage and milestone evidence are
`reports/generation_coverage.md` and
`reports/milestones/phase_5_real_outputs.md`.

Compilation is not faithfulness. Gate 5G created and inspected no semantic
labels and grants no supervision eligibility. LF-021 and Gate 5 therefore
remain open solely for genuine human annotation, adjudication, prevalence
reporting, and the final human-label gate.

## Fail-closed foundation and offline smoke

The disabled production-facing foundation still passes:

```bash
uv run leanfaith collect-real-outputs --validate-foundation
```

The canonical offline replay remains:

```text
run_20260723T204528Z_405b9d57
```

It uses a deterministic hand-authored response, makes zero network/model
calls, and is permanently excluded from training, calibration, model
selection, scientific tables, release, Gate 5G, and Gate 5. Its role is only
to test provider/request/response/parse/Lean/screen/replay boundaries.

## Exact checkpoint qualifications

All three supervision-candidate families passed their own persisted,
cross-record-verified smoke qualification:

| Family | Exact checkpoint revision | Canonical bundle | Bundle SHA-256 |
|---|---|---|---|
| Kimina Autoformalizer 7B | `ddd47cb477d93b3ca990468e1c0d5ad6b60973dd` | `runs/lf021_local_qualification/kimina_autoformalizer_20260723T215416Z/bundle_manifest.json` | `27bd7d958b1e266b335d5c9f55e45341ee278f7c192f362dc307438481fb1312` |
| Goedel Formalizer V2 8B | `fe2d362d899601abe79d7d5e95eaa7fe9883a0cb` | `runs/lf021_local_qualification/goedel_formalizer_v2_20260723T221540Z/bundle_manifest.json` | `edcd06b1bf6e7a2a8b96deef947add2c412200d83f4d97797ace7d602293fc70` |
| StepFun Formalizer 7B | `fb0dc612761fecd64ebbc489c2a3417e9ea01968` | `runs/lf021_local_qualification/stepfun_final_v1_20260723T223533Z/bundle_manifest.json` | `34395af5629fc17dd07886fad2a0479fc12a1cff5da0d19903f1356a4bb4c520` |

Each terminal status is `qualified_smoke`; each bundle replays its exact
checkpoint, tokenizer, prompt, parser, runtime, fixture, screening, and
terminal lineage. Qualification is necessary activation evidence, but it
does not itself count as a research output or receive Gate credit.

ReForm-8B remains supervision-excluded and held out at revision
`1589c832cfad679a280b222e694b987a33befd26`.

## Frozen three-problem public pool

The first research pool contains three contributor-authored mathlib
docstrings that postdate all three participating checkpoint revisions. The
records are public, active-registry clear, reference-bound, and explicitly
ineligible for unseen/source-independent claims.

| Artifact | SHA-256 |
|---|---|
| `data/parsed/real_outputs/public_research_v1/problem_pool_records.jsonl` | `20dea442faa027a104d49e96ca336210d1310dfe3813bf47d2a8736a65d60da0` |
| `data/parsed/real_outputs/public_research_v1/problem_pool_manifest.json` | `e823da6c31c14136f1797419ed728fd901c5a27cf9ea398382bbd9ebd8d86d87` |
| `data/parsed/real_outputs/public_research_v1/context.json` | `26495faf51a57e6d81eb54d8caf32304c32676bddcf98a0024f3bdcdc8760244` |
| `configs/generation/local_research_source_matrix_v1.yaml` | `09d042b32bec38aa585b19388e116232852974acf97553671ae9df931cbb4cf1` |

## Completed nine-call research collection

The exact plan is:

```text
research_collection_plan:75e16a5cb7ba937463821c92ef612c25475d91e7af00fb38bc2c970fa3dc2393
```

Its root is:

```text
data/raw/real_outputs/public_research_v1/local_collection_v1/
75e16a5cb7ba937463821c92ef612c25475d91e7af00fb38bc2c970fa3dc2393
```

The collection completed all 9 requested problem × family × seed calls:

| Property | Result |
|---|---:|
| problems | 3 |
| families | 3 |
| raw calls requested | 9 |
| raw calls completed | 9 |
| unique raw-output hashes | 9 |
| semantic labels created | 0 |
| Gate-5G credit claimed | false |

Bound collection artifacts:

| Artifact | SHA-256 |
|---|---|
| `plan.json` | `8bf1a62f2b6ec705399f836095fbd02de51594ae764222a74f8dae0ffcdee2d0` |
| `manifest.json` | `3c3682f4aef9fe41cf7a648345776587cbbb31cdf7237ccef8077eb7b1accdab` |

One load/unload session was used per family. Every invocation has a persisted
request, provider boundary, model-attempt boundary, raw output, provider
lineage, terminal, and family-session start/end record. Re-executing the
completed plan performs no model load and verifies the immutable bytes.

## Versioned postprocessing results

### Strict v1 audit

The immutable v1 postprocessor deliberately accepted only the original
family output envelope:

```text
manifest_id =
research_postprocess_manifest:fd7896bf733d9b6dac632fb629b68c982a2afdff57a1391a47cb63b068fd628c
```

It reported:

```text
admitted_unresolved = 1
parse_failed = 8
```

This remains a valid strict-envelope audit. It was not overwritten.

### Lean-backed recovery v2

The v2 postprocessor adds a narrow, fail-closed recovery parser only after
registered operational envelope failures. It:

- selects only an unfenced completion or final Lean fence;
- permits only a small harmless preamble allowlist;
- requires exactly the expected theorem/lemma name;
- uses LeanInteract declaration ranges and `#check` output;
- reconstructs and revalidates a proof-free normalized statement; and
- never retries a genuinely Lean-invalid primary result.

Canonical result:

```text
manifest_id =
research_postprocess_v2_manifest:df53996b41e5372db9d084e5453130380a7c2e096192153aee8baee0adb63bff

input_binding_hash =
c5ef69ee5f82b8cbbf31d67bc0236b31cb2914caf41d5be7df375691c3b01f91
```

| Outcome | Count |
|---|---:|
| primary parser success | 1 |
| safe recovery success | 3 |
| parser/Lean-valid total | 4 |
| admitted unresolved | 3 |
| duplicate/screen rejected | 1 |
| genuine Lean-invalid | 5 |

Recovery statuses are `succeeded=3`, `failed=3`, `not_eligible=2`, and
`not_needed=1`. The three recovery failures and two ineligible primary
failures are actual Lean-invalid candidates; they were not converted into
semantic negatives.

Bound implementation/output hashes:

| Artifact | SHA-256 |
|---|---|
| `src/leanfaith/generation/local_output_recovery.py` | `34bd4757fdd06d119ccbc2c57692cf8e53c07447bbe2319ffc9dbec57b67d983` |
| `src/leanfaith/generation/research_postprocess_v2.py` | `35e810825781d78cff7367c9ca88333a37ecb4c3a37e7a15c73be9873cca2196` |
| `postprocess_v2/manifest.json` | `e99ab2330470dd7a094aa836b5e39619f3333450cadba9fe9474b92b5f9b9fb8` |

Immediate and standalone `--verify-only` replay both pass. Every admitted
record has:

```text
same_claim = null
relation = null
resolution_outcome = unresolved
quality_tier = unknown
requires_adjudication = true
decision = REVIEW
supervision_eligible = false
```

## Expanded 40-problem Algebra tranche

A second public pool was frozen and used for scalable local collection:

| Artifact | SHA-256 |
|---|---|
| `data/parsed/real_outputs/gate3_docstrings_operational_v1/problem_pool_records.jsonl` | `6f45ebf158fcbb23f7b9833f2c433fb335cda7e66c061c0436aab68702330c6f` |
| `data/parsed/real_outputs/gate3_docstrings_operational_v1/problem_pool_manifest.json` | `229cf1dfcc7c8eee0de839c62b6beb708678ff2a2bea876803b704033de324d7` |

All 40 records:

- use contributor-authored mathlib docstrings;
- strictly postdate the newest participating checkpoint;
- pass the active benchmark screens;
- bind a no-`sorry` reference check;
- are authorized only for the pinned local models;
- hide the reference from the generator; and
- created no semantic label or Gate claim when frozen.

The pool is explicitly **Algebra-only**, not cross-domain evidence. Its
nonsemantic source-path proxies are Group 11, BigOperators 9, Category 5,
AffineMonoid 4, Algebra 4, Exact 3, and other 4.

The frozen `algebra_s0` collector-v2 plan completed all 120 problem × family ×
seed invocations. Postprocess-v3 admitted 23 benchmark-clear, alpha-unique
unresolved REVIEW candidates and recorded 97 parse failures. Family admission
counts were Goedel 9, Kimina 7, and StepFun 7. Its immutable manifest is:

```text
research_postprocess_v3_manifest:273c635963e8a0fe50cf835ddc32fab6e62e1a5764a6b1d403ab1900b7c785b6
```

## Mandatory cross-domain tranche

The separately curated 20-problem public cross-domain pool covers operational
source-path proxies for Analysis, Combinatorics, Geometry, Number Theory,
Probability, and Topology. These proxies are coverage metadata, not semantic
gold.

The frozen `cross_domain_s0` collector-v3 plan completed all 60 problem ×
family × seed invocations with `raw_collected` status. Postprocess-v4 admitted
7 benchmark-clear, alpha-unique unresolved REVIEW candidates, recorded 52
parse failures and 1 explicit materialization failure, and replayed exactly.
Family admission counts were Goedel 0, Kimina 4, and StepFun 3.

| Artifact | Identity / SHA-256 |
|---|---|
| collector-v3 plan | `research_collection_plan_v3:b5080892f0b71e43735dfe3a1f3bf4e227f7988c362196ea7a09ea703db3846c` |
| collector-v3 manifest | `research_collection_manifest_v3:654fbb69a1bee7d5b7cde3d4159402759756b058495e5bd1030902e1707dcbc9` / `e75a2d38f7511b4ebe46054fd84b4eccb8e17a9f12253867756b579171a4b819` |
| postprocess-v4 manifest | `research_postprocess_v4_manifest:f7df4ea666de96d4a74a0c331a505a844ad6e25b7592125db4afc296a6f34383` / `1b379edc653f661af28c2ce3d341b85368b38cb5bca00e29cfcb7e4e88cdf2a8` |

The compilation-only expansion decision over both completed tranches is:

```text
lf021_expansion_decision:4e89b908916de794221493de0d254a649ed5e4b76fc9bf5e773da831bbf733cc
action = collect_next_tranche
next_tranche = algebra_s1
unique_compiling = 30
```

The decision inspected no semantic labels and created no supervision or Gate
credit.

## Second Algebra and cross-domain tranches

The tranche narratives and intermediate expansion decisions below are
retained as historical execution evidence. Every listed
`collect_next_tranche` action was executed and superseded by the final
`preferred_eligible_stop` decision. They are not current instructions.

The generic collector-v4 execution completed `algebra_s1` with 120/120
`raw_collected` terminals. Postprocess-v5 replayed exactly and reported:

```text
research_postprocess_v5_manifest:1d3e6fee3c9b56b82bc0e912361d9486fb82bcfb0c3c7b4b37462ba19a0953af
admitted_unresolved = 22
parse_failed = 98
```

Family admissions were Goedel 7, Kimina 9, and StepFun 6. Its independent raw
and postprocess audits found no lineage, replay, label, supervision, or Gate
defect.

The subsequently selected `cross_domain_s1` tranche completed 60/60
`raw_collected` terminals. Postprocess-v5 replayed exactly and reported:

```text
research_postprocess_v5_manifest:7631d31786f35436e0508fd8ce612365ba0630775c6a7d9950b6362395272509
admitted_unresolved = 10
parse_failed = 49
materialization_failed = 1
```

Family admissions were Goedel 1, Kimina 7, and StepFun 2. The frozen
compilation-only decision over the exact four-tranche prefix is:

```text
lf021_expansion_decision:024550993e73ef6532a29d0ec1a029b90c74de796e2863a99bde1c9405857365
action = collect_next_tranche
next_tranche = algebra_s2
unique_compiling = 56
```

It inspected no semantic labels and replayed byte-for-byte.

## Supplemental remote generator qualifications

Three remote routes were qualified on the same public, reference-hidden
one-problem boundary:

- `moonshotai/Kimi-K2.7-Code` through EPFL RCP;
- `Qwen/Qwen3.6-35B-A3B` through EPFL RCP; and
- `gpt-5.6-terra` with `xhigh` reasoning through `codex exec`.

Each valid qualification bundle contains one theorem-generation call and a
statement that LeanInteract accepted operationally with the temporary `sorry`
used only as an elaboration harness. The full Kimi history also contains one
earlier K2.7 call in the explicitly superseded v1 bundle, so it contains two
K2.7 qualification calls in total; K2.6 received none. Kimi K2.6 is the
same-family fallback, all Qwen checkpoints count as one Qwen family, and the
Codex route is an OpenAI-family proposer. These artifacts are supplemental
only: they create no semantic label, supervision admission, Gate credit,
held-out claim, or unseen-data claim, and they do not alter the frozen local
expansion stopping rule.

The combined independent audit is
`reports/generation/lf021_remote_one_problem_qualifications_combined_audit_v1.json`
(SHA-256
`e815141eab7493a90d966a1d617df6739ea0f5f6ec6b52ab24317a5e057496f0`).
It passed replay, call-count, guard, family-accounting, and secret checks. It
also records one non-blocking provenance defect without rewriting history:
the bound Qwen config's `frozen_at` value is later than the actual request,
although filesystem evidence and exact hashes establish that those bytes were
bound before execution. Future remote configs must use observed chronology.

## Completed production lineage

The final collection contains 12 original and 4 extension tranches. The
label-blind extension policy terminated with:

```text
lf021_post_exhaustion_extension_decision_v1:ec7522abdd2219a2e9f2587f9ec4fa2818b3fd5f31237aae6a5ca63e55bd0cca
action = preferred_eligible_stop
```

The production frame was frozen by:

```text
lf021_extended_frame_freeze_decision_v1:0574621043042ed62b486260de9bf633797f2471217f502be25b9aec3a46a19c
```

The exact lineage is:

```text
lf021_gate5g_lineage:ddac5e106c92b263ed96c9974eeadcef25f4980f1e777b06bca75de133b0aa1d
sha256 = a2bb9dba960a7906057647162a6ba00e17f26d0aa89180940e9e6112138ca761
```

It binds all 1,440 terminal invocations, 299 compile-and-benchmark-clear
members, 49 duplicates, 250 unique problem-aware units, and the frozen
240-item, 31-stratum frame.

## Next scientific step

Do not execute another generation tranche. The label-blind collection policy
terminated with `preferred_eligible_stop`, and the 240-item frame is
immutable. Export that exact frame for blinded human annotation, adjudicate
all terminal outcomes, run the bound prevalence estimator, and close Gate 5
only if its human-label and reporting requirements pass. Until then, every
frame item remains unresolved `REVIEW` and supervision-ineligible.
