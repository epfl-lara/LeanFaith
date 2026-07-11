# Source probes — 2026-07-10 (Phase 0 task 3 / Gate 0 evidence)

All probes ran through the LF-010 framework (`HFDatasetProber`/`GitRepositoryProber`
with real clients); manifests + 100-row canonical-JSONL samples archived under
`data/source_manifests/` and `data/raw/sources/<source>/` (local, gitignored —
this report carries only metadata per §28.6). Public probes ran anonymously
(`token=False`); only `sft_classic` used the project `HF_TOKEN`.

## Primary source: formalmathatepfl/sft_classic — VERIFIED (authenticated)

- **Revision (pinned):** `0bf9f424309f668c2c2dd214aef6ec5d1d5c042f` (private, not gated; last modified 2026-05-07)
- **License:** none declared on the card (private internal dataset; redistribution treated as prohibited until the owner states otherwise)
- **Splits:** train 2,006,425 · test 1,029,845 (3,036,270 rows — ~30x the public sibling)
- **Schema (12 columns):** `uuid, data_source, question, answer, proof_plan, valid, proof_repair, lean_code, token_count, tactic_count, lean_score, lean_rank`
- **Sample:** 100 rows archived; sha256 `9913ae837d021d6e…`
- **Structure finding:** `question` is a *prompt-wrapped, proof-stripped Lean statement*: a fixed instruction line, then a ```lean4 block containing header (`import Mathlib`/`Aesop`, `set_option maxHeartbeats 0`, `open …`), a `/-- NL problem statement -/` docstring, and the theorem signature ending `:= by sorry`. `lean_code` holds the completed proof; `valid` marks typecheck success. So the NL statement is extractable from the docstring and the *statement-only* Lean from the question block — exactly the §9.4 pool shape.
- **Provenance finding (per-row `data_source`, first-2000 distribution):** `Goedel-LM/SFT_dataset_v2` ≈48%, `Goedel-LM/Goedel-Pset-v1` ≈33%, `formalmathatepfl/solved_problems_finetuning_iter1` ≈16%, `AI-MO/NuminaMath-LEAN` ≈1.8%, `uw-math-ai/APRIL` ≈0.8%. NL provenance is therefore **mixed** (largely autoformalization-derived from NuminaMath-family problems). Per §9.4 the adapter must tag `nl_trust` per `data_source`; nothing is silently upgraded to trusted-human-NL. Manifest records `nl_trust: uncertain` at source level.
- **§9.2 boundary:** `external_api_approved: null` — no provider may receive this content until the approval decision names a provider set and scope.

## Fallbacks / benchmark

- **sft_classic_numina** `9ba1be2e988c…` (public): **identical 12-column schema** to sft_classic (the plan's 4-column expectation `uuid/question/answer/lean_code` was a subset); splits train 33,027 · test 66,747 (total 99,774 — matches the plan's scale figure, but split across train/test with test > train). Adapter mapping must use the full schema; §9.3 mapping remains valid as a projection.
- **lean_workbook** `2e066e310b2c…` (public, apache-2.0): **25,214 rows** — the plan's "about 57k" does not match this revision's HF `train` split. Columns: `id, status, tactic, state_before, state_after, natural_language_statement, answer, formal_statement`. NL in `natural_language_statement`, statement in `formal_statement`; always `nl_trust=synthetic` (§9.4).
- **proofnetverif** `91183e5b12d6…` (public, MIT): splits valid 2,300 · test 1,452 (total 3,752 — matches the plan exactly); columns match §9.3 verbatim. Frozen benchmark; evaluation only.

## Lean projects (toolchains verified at pinned revisions)

| project | revision | checked-in toolchain | status |
|---|---|---|---|
| mathlib | d568c8c09630… (tag v4.31.0-rc1) | leanprover/lean4:v4.31.0-rc1 | matches lock; checkout + full build cache at `/storage/milikic/leanfaith/mathlib4` (7.2G, 8,477 cache files) |
| cslib | 2f677bfc8ef7… | leanprover/lean4:v4.31.0-rc1 | in range; probe-only until Phase 11 |
| physlib | f5242c99d796… | leanprover/lean4:v4.30.0 | in range; probe-only until Phase 11 |

## Plan-discrepancy register

1. `sft_classic_numina` schema is 12 columns (plan §9.1 listed 4) and is split train/test.
2. `internlm/Lean-Workbook` is 25,214 rows at the pinned revision (plan: "about 57k").
3. `sft_classic` NL lives inside Lean docstrings of prompt-wrapped questions; the
   plan's assumption of a separable NL field holds only after unwrapping (adapter
   requirement recorded for LF-011).

## Phase-5 pool adequacy (preliminary)

Raw candidate frame before eligibility/dedup: sft_classic train 2,006,425 rows with
per-row provenance tags + Lean-Workbook 25,214 synthetic problems. Exact
`phase5_pool_candidate_count` before/after eligibility and near-duplicate filters
(per §9.4) is computed by the LF-011 adapter run over the full dataset and will be
appended to the manifest; the raw scale exceeds the ≥10k pilot and ≥100k
research_v1 thresholds by orders of magnitude.
