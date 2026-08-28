# LeanFaith Refocus — v1 Delivery Plan

> Status: DRAFT v2, pending owner approval (2026-08-28). Self-contained so any agent (claude code,
> codex, lemex) can pick up a track without conversation context. During Track B's docs reset this
> becomes the repo's living `PLAN.md`; the old 4,578-line `PLAN.md` is archived to `docs/archive/`.
>
> **Review log:**
> - v1 adversarially reviewed by codex exec (gpt-5.6-sol, high reasoning, 2026-08-28): 3 BLOCKER
>   + 16 MAJOR + 1 MINOR findings; verdict "direction right, do not start under the v1 ordering".
>   All incorporated in v2: golden partition freeze is Step 0 before any gold-touching work;
>   `final_test` sealed until one frozen recipe; P2 pruning gated on a D-2 replacement package;
>   Kimi branch transplant instead of merge; synthetic arithmetic must be proof-producing; 12,122
>   non-P01 unary rows salvaged; stratified ≥150/stratum audits; abstain-not-truncate eval;
>   strict-zero-shot vs gold-calibrated reporting tracks; S0 smoke-first + CPT contamination
>   screening; S1 anti-shortcut protocol; S2 replay ablation; public-only headline checkpoint.
> - v3 (same day): owner directed more ambition on deterministic data; a second codex
>   consultation produced **`TRANSFORM_CATALOG_V2.md`** (typed Lean Meta rewrite engine, evidence
>   classes P-DEF/P-SCHEMA/P-LEMMA/N-SEP/N-PROOF/F2-DIR, families P19–P38 + N19–N28, build
>   order, cap schedule). Track D targets raised to **300K committed / 750K stretch**, current
>   negative families demoted to silver until separator/witness-upgraded, and compute expanded to
>   the RunAI cluster (A100/H100/H200).

## Context (why this refocus)

The goal is **LeanFaith-v1: a lightweight Lean↔Lean semantic-consistency classifier** — binary
same-claim yes/no + confidence from an encoder cross-encoder + cross-entropy head; cheaper and
more transparent than an LLM judge. Data comes from (a) composable deterministic transforms that
preserve/break consistency, (b) SOTA models (via RCP/lemex, codex, claude) proposing realistic
transforms and judging pairs, (c) existing human-labeled benchmarks for golden train/eval labels.

Six weeks and ~350K lines in, three deep audits (2026-08-27/28) established:

**Real assets exist:**
- Trained **M1 packed cross-encoder** (ModernBERT-base, 1024-token budget): test pseudo-AUPRC
  0.887 / balanced-acc 0.859 on proxy labels. Checkpoint:
  `/storage/milikic/leanfaith/m1_proxy_training/firsthop_kimi_qwen_composition_8d815af_v1/model.safetensors`.
  Also M0 (dual-encoder, 0.603) and M2 (bidirectional, 0.684) — M1 wins decisively.
- **17,181-pair replay-verified proxy corpus** (ancestry-safe splits, 26.4% positive) at
  `/storage/milikic/leanfaith/experimental_mixed_supervision/firsthop_kimi_qwen1125_composition_f7b398af_v1/records.jsonl`.
- Working LeanInteract backend, 27,786 represented public mathlib statements, ~28 deterministic
  transform families, a working Sol(GPT-5.6)+Fable(Claude) dual-judge pipeline (excellent pilot
  agreement; only 17 pairs adjudicated).
- 17,873 fresh Qwen3.5/Kimi-K2 model-generated variants (blocked on a Lean-check rerun; the
  original corrupted-`.olean` crash no longer reproduces — see
  `reports/model_selection/generated_data_handoff_2026_08_27.md`).

**But zero citable numbers, because the project blocked itself:**
- **No benchmark evaluation was ever run.** No `evaluate` command exists among the 119 CLI
  commands. ProofNetVerif (3,752 human-labeled rows) was frozen into a contamination denylist and
  never scored against. GTED was named but never implemented.
- **The policy layer cannot be satisfied**: `src/leanfaith/models/data_readiness.py:530` hardcodes
  `human_gold_eligible: Literal[False]` — even completed human annotation would be refused. All
  artifacts carry `training_eligible=false`.
- ~60–70% of code/commits are provenance/attestation/replay machinery; the 240-pair human
  annotation campaign (Gate 5G) collected 0 labels.
- 12 stale git branches; `main` frozen at 2026-07-24; all real work on
  `milikic/lf022-weak-supervision-foundation` (165 commits ahead).

**Transforms audit summary:** 21 families implemented across three non-unified generations
(v1 registry: P01/P02/P04/N01/N02/N03/N07/N10; v2 runtimes: P05–P18/N11–N18; composition depth ≤3,
E2-positives-seeded, max one negative hop). Verification only proves the candidate elaborates
(`allow_sorry=True`) — the *label* is never checked; **6 mechanisms (P14, P15, P16, P18, N12, N18,
N11) carry 99.8% of the data**; v1 positives are trivial — P01 embeds a literal `lf_alpha_<hex>`
token (label leak), P04/N01/N07 have 2–4-entry replacement tables; E0 positives are byte-identical
after elaboration; N11 negatives often degenerate; N13/N14/N16/N17/P17 have near-zero yield; 16 of
30 error-taxonomy codes have no generator; P13 implemented but never wired; 27,327 unary pairs
blocked on a strict-replay design that was never coded. Tests: 3,210/3,222 pass (12 failures all
in LLM-judge/annotation test files).

**Golden human-labeled benchmarks available:**
- **EPLA/ASSESS** (github.com/XiaoyangLiu-sjtu/ASSESS): **1,247 pairs** (831 miniF2F + 416
  ProofNet), 7 expert annotators, `Provability` = equivalence label. Largest, cleanest.
- **BEq Human Equivalence** (github.com/Purewhite2019/rethinking_autoformalization,
  `data/human_equivalence/`): **200 expert-labeled binary pairs**. Note: BEq is itself built from
  ProofNet autoformalization results — independent annotators, NOT independent problems.
- **GTED** (on disk at `/localhome/milikic/lean_theorem_equivalence/GTED/experiment/*/human_evaluation.json`):
  **298 pairs** (57.4% positive) + published baseline numbers (BLEU/BEq/typecheck/majority-vote/GTED)
  alongside. Same research group as EPLA — not independent.
- **ProofNetVerif** (`PAug/ProofNetVerif` @ pinned revision 91183e5b, cached in
  `/storage/milikic/leanfaith/hf_cache/`): **3,752 rows** over only **361 underlying problems**;
  adapter at `src/leanfaith/sources/proofnetverif.py`. Non-typechecking candidates were
  auto-labeled incorrect → auxiliary/weak benchmark only.
- Auxiliary training-eligible (non-human): **ACE-Dataset** (HF `neurips-2026-submission-ACE/ACE-Dataset`,
  formally verified equal/nonequal Lean pairs).
- **Leakage rule**: EPLA/GTED/ProofNetVerif/BEq all derive from miniF2F/ProofNet — every split
  must group by underlying source problem id across datasets (`minif2f::<name>` / `proofnet::<name>`).

**Owner decisions (2026-08-27/28):**
1. v1 task = **Lean↔Lean only** (statement pair → consistent/inconsistent + confidence).
2. **Drop the human-annotation gate** — train on transform + model-adjudicated labels; evaluate on
   published golden sets.
3. **Prune in place** + delete superseded branches → one unified core on `main`.
4. **Done = trained checkpoint + benchmark numbers** (accuracy/F1/calibration vs. LLM-judge and
   published baselines).
5. **Golden data usage**: strictly held-out final test; the rest partitioned into golden-train /
   dev (tuning-calibration) portions, leakage-aware. Report both weak-supervision-only zero-shot
   and golden-fine-tuned results.
6. Ordinary reproducibility (config + seed + data hashes in a run manifest) replaces the
   gate/attestation regime for all new work.
7. **Staged training pipeline**: S0 encoder domain-adaptation on Lean code (with deliberate
   signature/view formatting) → S1 large-scale SFT on deterministic-transform data → S2 refinement
   on LLM-produced+verified data and possibly part of the golden labels → S3 evaluate/test.
8. **Much more data**: scale deterministic generation (including cheap procedurally generated
   arithmetic data) and LLM generation aggressively. **Trust by design**: well-designed generators
   + single Lean elaboration + stratified audit samples — no re-verifying every record.
9. **Two LLM data tracks**: (a) autoformalizers (codex / claude / lemex models) produce candidate
   formalizations on existing statements, judged single-pass by codex/claude; (b) LLMs (codex,
   claude, Kimi2.7-Code via lemex) produce semantic-consistency-preserving or -breaking
   transformations, prompted with golden few-shot examples (golden_train only) and a clear
   taxonomy — their self-label is trusted subject to typecheck + stratified audits.
10. **Maximize parallelism**: start long-running model/data jobs ASAP; independent tasks run
    concurrently across lemex/RCP, codex, claude code, and the local RTX 4090.
11. **The plan lives in the repo** (this file; becomes `PLAN.md` at docs reset).

**Compute:** larapc2 (this server, RTX 4090) hosts the repo and reaches RCP API models (via lemex
or directly). For anything heavier — training runs, direct LLM serving — the **RunAI cluster
(RCP) with A100s/H100s/H200s** is available; there is no compute scarcity. Division of labor:
4090 = local iteration, smokes, debugging; cluster = production S0/S1/S2 runs, parallel
ablations/multi-seed, and vLLM serving of the local autoformalizers (Goedel/Kimina/StepFun) at
scale, so generation never competes with training.

**Existing assets for the new stages:** Lean CPT corpus already merged — 469,585 rows at
`/storage/milikic/lean_cpt_updates/2026-08-12-curated-libraries/hf_cpt_dataset.jsonl` (base +
curated cslib/physlib/lean-pool additions; sources under `/storage/milikic/lean_project_corpus/`).
NL problem source config `configs/sources/lean_workbook.yaml` exists for the autoformalizer track.
Local autoformalizers (Goedel-Formalizer-v2-8B, Kimina-Autoformalizer-7B, StepFun-Formalizer-7B)
plus the LF-021 collection machinery already produced 1,440 real invocations. sft_classic private
data must never be sent to external LLM APIs (decided, fail-closed); deterministic transforms on
it are allowed.

## The refocused work

**Sequencing rule (from the adversarial review): the golden partition freeze is the FIRST
operation — nothing that touches gold examples or evaluates a model may start before it.**

**Step 0 (first, ~1–1.5d): Track A1 — golden ingestion + partition freeze.** Canonical pair
records, group union via underlying source ids, stratified allocation, frozen partition manifest +
contamination blocklists covering BOTH supervised corpora AND the CPT corpus. Until this lands: no
golden few-shot examples in any prompt, no model scoring on golden rows, no CPT launch.

**Parallel with Step 0 (safe — zero gold exposure):**
1. Track B step 1: branch unification (~1h; transplant the two Kimi QA files, not a wholesale
   branch merge).
2. Track D-0: launch the Qwen/Kimi Lean-check recovery **from an immutable tagged commit /
   separate worktree** so the long-running jobs never depend on a tree undergoing Track B surgery.
3. Track T-S0 prep: build + smoke-test the MLM runner (one short run → checkpoint reload → M1
   initialization from it); the full CPT run waits for the post-Step-0 screened corpus.
4. Track A2 harness engineering (pure code, no golden scoring yet).

**After Step 0:** D3 pilot (few-shots drawn ONLY from `golden_train`), D2 10-record per-provider
pilots before any large launch, S0 full run on the screened CPT corpus. **`final_test` stays
sealed** until one primary model + training recipe + calibration method + threshold + reporting
script are all frozen; every interim number — including the existing 8d815af checkpoint's — is
dev-only. Track B pruning batches trail behind, gated as described in Track B.

---

## Track A — Evaluation infrastructure + first real number (S3 tooling, built first; ~3–4 days)

New standalone package `src/leanfaith/eval/` with its own console script `leanfaith-eval`
(one-line edit to `pyproject.toml`). Nothing threads through the 9,001-line `cli/app.py`. Every
command emits a plain run manifest (git rev, config, seed, input/checkpoint SHA-256s).

**Verified reuse contract** (from code reading):
- M1 packed input = `"[REFERENCE]\n[HEADLESS]\n" + ref_headless + "\n[CANDIDATE]\n[HEADLESS]\n" + cand_headless`
  (`pack_m1_pair` in `src/leanfaith/models/m1_cross_encoder.py` + the `[HEADLESS]` marker from
  `_make_examples` in `src/leanfaith/models/m0_dual_encoder.py`); tokenize
  `padding=True, truncation=True, max_length=1024`.
- The checkpoint is a full module state dict: rebuild via `AutoModel` from the pinned ModernBERT
  snapshot (`/storage/milikic/models/hub/models--answerdotai--ModernBERT-base/snapshots/8949b909…`)
  + `build_m1_cross_encoder_module` + `load_state_dict(strict=True)`.
- Reuse: `normalize_headless` + `signature_near_dup_hash`
  (`src/leanfaith/representations/views.py`); `_tie_safe_average_precision`
  (m0_dual_encoder.py) for AUPRC; `parse_row` (`src/leanfaith/sources/proofnetverif.py`);
  union-find split helpers in `src/leanfaith/datasets/experimental_mixed_supervision.py`.

**A1. Golden ingestion + partition** (`eval/schema.py`, `eval/ingest.py`, `eval/partition.py`) —
Step 0 of the whole plan:
- Ingest EPLA (1,247, pin commit SHA), BEq (200), GTED (298, on disk), ProofNetVerif (3,752,
  cached snapshot) into **one canonical statement-pair record with multi-dataset membership
  masks** — a pair appearing in both EPLA and GTED keeps both memberships (published-slice numbers
  per dataset stay reportable), one label per source with disagreements audited. `group_key`
  unions through the underlying miniF2F/ProofNet source ids across ALL datasets (BEq included —
  it is ProofNet-derived and not independent at the problem level).
- Label ontology + provenance per row: `expert_human | auto_typecheck_fail | formal_verified`.
  **ProofNetVerif is an auxiliary/weak benchmark**: 361 underlying problems (groups up to 36
  rows), auto-labeled negatives — reported separately, never in the headline test, NOT used for
  temperature calibration unless a human-validated subset establishes label compatibility.
- Partition by GROUP with **deterministic stratified allocation** (greedy over
  dataset/source/label/generator strata — pure hashing over few, uneven groups cannot deliver the
  promised balance): all BEq groups → `final_test` (kept whole); remaining expert-labeled groups
  ≈50/25/25 final_test/dev/golden_train by stratified assignment. Freeze as
  `data/benchmarks/golden_partition_v1.json` with tests proving zero group intersection + stable
  counts/hashes. Expected final_test ≈ 950–1,000 expert pairs.
- Emit contamination blocklists (exact + normalized + source-identity) for BOTH supervised corpus
  building AND the S0 CPT corpus.

**A2. Eval harness** (`eval/m1_runtime.py`, `eval/metrics.py`, `eval/harness.py`,
`eval/report.py`, `eval/cli.py`):
- `leanfaith-eval evaluate --checkpoint … --partition dev|final_test` → predictions.jsonl +
  metrics.json (accuracy, balanced-acc, P/R/F1, AUPRC, ROC-AUC, Brier, NLL, ECE + reliability,
  per-source and per-provenance breakdowns, group-level bootstrap CIs) + markdown report.
- **Overlength policy: abstain, don't truncate.** Truncation can delete the distinguishing
  fragment and invalidate the label — pairs >1024 tokens are abstained on; the primary metric is
  coverage-aware (accuracy/F1 + coverage). A truncated-scoring variant may appear only as a
  clearly-labeled secondary line.
- **Two reporting tracks** for every model: (i) strict zero-shot — uncalibrated probability, fixed
  0.5 threshold, no golden data consumed; (ii) gold-calibrated — dev-fit temperature + threshold,
  no weight update. Predeclared primary metric; per-source floors (an aggregate must not hide a
  collapsed source); group-bootstrap CIs on all comparisons.
- Key tests: `test_m1_packing_parity.py` (harness packed string == `pack_m1_pair` on a
  training-path example) + partition-leakage property test.

**A3. Baselines** (`eval/baselines.py`, `eval/llm_judge.py`): always-majority;
normalized-string identity; typecheck-only (GTED/PNV metadata; overnight LeanInteract compile pass
for EPLA/BEq); token-overlap threshold tuned on dev; single-cell LLM judge (reuse
`prompts/judges/lean_pair_blinded_v2.txt` + `parse_blinded_judge_output` from
`src/leanfaith/generation/weak_supervision.py`, one orientation); cite GTED's published numbers on
the GTED subset.

**First real number = existing 8d815af checkpoint scored on `dev`. `final_test` stays sealed**
until the end-state comparison set is frozen, then opens exactly once for the full frozen
comparison — all ablations and interim variants live on dev forever.

---

## Track B — Repo unification and prune (~3–4 focused days total; step 1 ≈ 1 hour)

Grounded in an AST-level import graph of all 314 src files (transitive closure of the keep-list);
every deletion below verified unreferenced by kept code.

**B-Step 1 — branch unification (first, ~1h):**
1. Safety tags: `pre-refocus-20260828` on the current branch + `archive/<branch>` tags on all 12
   side branches; push tags.
2. **Transplant the Kimi QA fix — no wholesale branch merge**: the useful delta of
   `milikic/kimi-prefix256-qa-fix` is exactly two files (`generation/lf022_prefix256_qa.py` +141
   and its test +77 — the terminal-reference compatibility shim needed to rerun QA over the 17,873
   pending Qwen/Kimi variants); the branch's 9 commits contain two revert pairs and divergent
   history. Checkout those two files' final state (or cherry-pick the minimal commits). Acceptance:
   `pytest tests/unit/test_lf022_prefix256_qa.py`. Long-running D-0 jobs then launch from an
   immutable tag/worktree, never from the tree undergoing surgery.
3. **Unify onto `main` by direct merge** (`git merge --no-ff`): main has 0 unique commits, solo
   repo — a 165-commit PR adds ceremony without a reviewer. Never squash. Push.
4. Delete all 12 superseded side branches, local + remote (archive tags keep heads reachable).
   Verified: the 3 non-matching patch-ids across the 11 branches all exist on the current branch
   under identical subjects as modified cherry-picks.

**B-Step 2 — docs reset (same day, ~0.5d, directly on main):**
- `git mv PLAN.md docs/archive/PLAN-2026-08-frozen.md`; promote this file to `PLAN.md`.
- README: keep lines 1–46; move the 660-line Status section to
  `docs/archive/README-status-2026-08.md`.
- `annotation/` → `docs/archive/annotation/` (archive, never delete data). `reports/` stays in
  place (inert) with one README line marking it historical.

**B-Step 3 — prune batches** (each on a short-lived branch, merged to main same day; after each
batch: grep for deleted-module imports must be empty → `pytest --collect-only` → full `pytest`):
- **P1 (~1d, ~30K src + ~15K test LOC):** independent leaf packages: `annotation_support/`
  (6,265), `labeling/` (4,992 — datasets/ verified NOT to import it), `evidence/` (2,990 — corpus
  build verified NOT to use it), `evaluation/` (2,007 — the LF-021 prevalence gate, NOT benchmark
  eval), `baselines/` (61), `release/` (176), `transforms/gate4g.py` (741),
  `cli/smoke_vertical.py` (2,124), 10 CLI wrapper files (~5,240), their ~22 app.py commands, dead
  configs/policies, annotation scripts. Kills the argilla test failure.
  **`models/data_readiness.py` (3,479): port-then-delete** — extract its mechanical
  contamination/provenance/split checks into `corpus2` first, prove the replacement on one corpus
  build, then delete the module (the obsolete policy verdicts die with it).
- **P2 (~1d, ~50K src + ~15K test LOC), GATED on D-2's replacement:** the LF-021 research/gate
  cluster in `generation/` — `research_collection*`, `research_postprocess*`, `research_overlap*`,
  `post_exhaustion_*`, `gate3_*`, `gate5g*`, `local_qualification` (3,406),
  `rcp_*qualification*`, `lf022_rcp_smoke_v1` (3,371), `lf022_weak_live_smoke`,
  `lf022_admission_freeze`, etc., `schemas/gate5g*.py`, gate2/3/5g app.py commands, scripts 09–35.
  **This cluster contains the autoformalizer collection machinery Track D-2 still needs** (e.g.
  `research_collection_v4/v5` + scripts 21+): first extract a small tested
  collection/postprocessing package for D-2, run a 10-record end-to-end pilot through it, THEN
  delete the legacy implementation. P2 does not run in parallel with D-2. Also retain the
  transform registry/family code needed to interpret the unary pool until the D-1 salvage decision
  is made.
- **P3 (~0.5d, ~8K):** remaining LF-022 dead limbs (`lf022_weak_batch_spec`,
  `lf022_diagnostic_subpool`, `lf022_family_matrix_freeze`, `lf022_pool_reallocation` + commands
  + tests).
- **P4 (deferred until the 17,873-variant reconciliation ships):** `lf022_merged_inventory` +
  `lf022_inventory_snapshot` (they produce the frozen-inventory artifacts the
  supervision-candidate builder consumes as file inputs) and thinning of the
  kimi_v4/route-qualification chain (~15–25K more).

**Keep-list surprises the import graph forced (do NOT delete):**
`lf022_dual_judge_authorization_v1.py` (the Fable judge fail-closed-validates its artifacts),
`lf022_model_silver_promotion_v1.py` (the judgments→corpus bridge), the entangled LF-022 execution
cluster (`lf022_execution`, `lf022_executor`, `lf022_codex_audit` — imported by the corpus
builder, `lf022_extraction_reuse` — imported by `transforms/scale_materializer`,
`lf022_historical_replay` — imported by Kimi QA, both judges' deps), `config/code_bundle.py`.
Note the inversion: `transforms/scale_materializer.py` imports `cli/pipeline.py` +
`cli/transformations.py` — those two CLI modules are library code.

**CLI:** delete ~74 of 119 app.py commands with the pruned modules (app.py 9,001 → ~5,300 lines).
Defer splitting app.py into sub-apps until after P3 (avoids conflicts with Track A, which adds its
own `leanfaith-eval` script and never touches app.py).

**Failing tests:** post-P1 the remaining 11 failures (`test_claude_fable_judge_v1.py` 7,
`test_codex_sol_judge_v1.py` 4) guard KEPT judge code — triage on unified main (likely
authorization-binding drift), do not delete.

**Net effect:** ≈128K LOC removed in week one (≈90K src = 38%, ≈34K tests, ~4K scripts, 4.6K
PLAN/README archived), rising toward ~150K (43%) after P4. Fail-closed scaffolding *inside* kept
modules is a rewrite, not surgery — explicitly out of scope for now.

**Interlock with Track A:** no name clash (science creates `src/leanfaith/eval/`, prune deletes
`src/leanfaith/evaluation/`), and Track A never edits `cli/app.py`.

---

## Track D — Data engine at scale (deterministic: **300K committed / 750K stretch**, gated on the Meta engine + a 5–10K-source pilot; + 20–50K LLM pairs)

(Targets revised twice: the plan review capped surface-parser generation at 50K/100K based on
measured yields — 2,237 clean pairs from 27,786 statements
(`reports/transformation_audits/lf033_public_all_clean_inventory_v1.md`); the owner then asked
for real ambition, and a dedicated Codex transform-catalog consultation (2026-08-28, saved as
**`TRANSFORM_CATALOG_V2.md`** — the design of record for this track) concluded 300K/750K is
defensible over 100–150K source statements **if** generation moves off surface parsing onto a
**typed Lean Meta rewrite engine** and the catalog expands to ~40–45 families. Raw inventory
estimate 0.8–1.5M candidates before caps.)

**Label contract — evidence classes** (from `TRANSFORM_CATALOG_V2.md`): every deterministic pair
carries a class: `P-DEF` (whole-type defeq), `P-SCHEMA` (exact instance of a proved logical
equivalence), `P-LEMMA` (one pinned equality/iff theorem at one certified occurrence) for
positives; `N-SEP` (separating valuation over the exact Boolean skeleton) or `N-PROOF`
(Lean-checked witness/refutation) for trusted negatives; `F2-DIR` for directional-only evidence
(never binary gold). A failed proof search never counts as `N-PROOF`. On top: stratified
sequential audits — provider × claimed label × family strata, **150 records per important
stratum, ≥300 for high-volume/synthetic strata**, until the one-sided 95% error upper bound is
below tolerance; LLM self-labels audited by a DIFFERENT provider; failing strata quarantined;
stratum precision stored as a training weight. No replay, no re-verification loops, no dual-cell
judging by default.

**D-1a. Typed Lean Meta rewrite engine (prerequisite):** a Lean-side transformation command over
typed `Expr` (local-context reconstruction, `inferType`/`isProp`/instance synthesis/defeq in
Lean, certificate emission: Expr hashes, path, instantiated theorem/instance hashes, expected
relation class), with an independent audit path that reconstructs the expected candidate — the
generator is never its own sole witness. The current Python surface parser is *why* most families
yield zero. Build this first; every high-value family below rides on it.

**D-0. Recover the blocked 17,873 Qwen/Kimi variants (day 1, long-running):** the handoff's steps
1–5 (`reports/model_selection/generated_data_handoff_2026_08_27.md`) — Kimi nonterminals, fresh
Qwen Lean-check root, Kimi Lean checks, freeze counts, dedup. Launch from an immutable
tag/worktree. The kimi-prefix256 QA shim (transplanted in B-Step 1) supports this.

**D-1. Deterministic scale-up (the bulk; full catalog + rationale in `TRANSFORM_CATALOG_V2.md`):**
- **Extraction scale-up**: rerun extraction/representation over the full mathlib universe (8,112
  files inventoried, 1,200 attempted so far; plus cslib/physlib after small per-source pilots —
  pin per-source toolchain/header context; plus the 18,668 private statements — deterministic
  transforms allowed, external LLM transmission barred). Target: 100–150K source statements.
- **Catalog expansion — ~20 new positive families (P19–P38) + ~10 new negatives (N19–N28)** on
  the Meta engine. Build order (yield+hardness per engineering day): P23 proof-binder
  currying/uncurrying → P24 independent hypothesis permutation → P20 definition fold/unfold →
  P21 β/ζ abstraction → P32 theorem-backed AC rewrites → P26+P27 material implication +
  contrapositive → P34 whitelisted single-lemma rewrites → N21/N22 Boolean-skeleton separator
  negatives; then a 5–10K-source pilot gates the rest (P30/P31 quantifier motion, N25/N27
  witness-heavy). Highest training value: P34, P20, P33, P23, P36, P27, P30/31, P32, N21, N26 —
  all resistant to token-overlap shortcuts.
- **Current-21 rework** (per the consultation): keep P02/P04/P11/P14/P15/P16/P18 (generalizing
  root-only to certified nested occurrences); supersede P12+P17→P23, P13→P22, P05–P10 surface
  parsers→Meta; quarantine P01 output until regenerated markerless; **demote the current
  negatives (N01/N02/N03/N07/N10/N11/N12/N13–N17/N18) to silver/directional until each is
  upgraded with a separator (`N-SEP`) or witness (`N-PROOF`)** — a certified syntactic delta is
  not a certificate of semantic non-equivalence.
- **Proof-producing synthesis (N28 + arithmetic templates)**: generated source statements are
  PROVEN once (`norm_num`/`decide`/`omega`/constructed proofs, kernel-checked, axiom-audited —
  elaboration alone accepts well-typed false statements); near-misses carry stored refutations.
  Synthetic sources capped at 15% committed / 20% stretch; per-template × numeric-range caps
  ≤0.5%. Every statement is fresh — no ancestry collision with mathlib or benchmarks.
- **Composition**: depth mix 45/35/20 (d1/d2/d3), max one negative hop, final certificate
  recomputed after composition (never just polarity-flag multiplication), cycle check against
  EVERY prior path state, separators transported/regenerated through subsequent positive hops;
  merge the existing 4,031 depth-3 pairs
  (`/storage/milikic/leanfaith/deterministic_v2/composition_third_hop_audits/frontier_084859ee_five_families_v2/`).
- **Caps** (anti-shortcut): family ≤8%, mechanism superclass ≤15%, exact template ≤2% (neutral
  wrappers ≤1%), exact rewrite lemma ≤0.5%, source ancestry ≤4 direct + 4 composed; swapped
  orientation is a training-time augmentation, not a new pair; stop stretch expansion if
  lexical-only balanced accuracy rises.
- Carried fixes: drop degenerate N11s and byte-identical E0 positives. **Unary pool salvage**:
  only P01 carries the `lf_alpha` leak (15,205 rows at
  `/storage/milikic/leanfaith/deterministic_scale/run_76de447_public_schema4_v1/unary/provisional_merged`);
  quarantine P01 and re-audit + reassemble the remaining **12,122 rows from
  P02/P04/N01/N02/N03/N07** under the new evidence-class schema.
- Committed 300K mix: 75K definitional/binder positives, 55K classical/logical positives, 35K
  theorem-backed positives, 65K separator negatives, 35K proof-certified negatives, 30K
  proof-producing synthetic, 5K cleaned legacy → **≈55%/45% pos/neg**.

**D-2. LLM track 1 — autoformalizer candidates (realistic distribution):** candidate
formalizations of existing statements/problems: local autoformalizers (Goedel/Kimina/StepFun via
the collection package extracted from the LF-021 machinery — see the P2 gate — served at scale
with vLLM on cluster A100/H100s, no 4090 contention), lemex models
(Kimi2.7-Code, GLM5.2, DeepSeekV4 — pattern per
`/localhome/milikic/annotate_numina/run_reasoning_direct.py`), codex, and claude, over public
sources (mathlib informalization round-trips, lean_workbook problems; miniF2F/ProofNet excluded
via the golden blocklist). Typecheck → pair with reference → **single-pass judge** (codex or
claude, one orientation, escalate to a second judge + reversed orientation only on
parse-fail/low-confidence/ambiguous — ~1.2–1.5 cells/pair vs the old 4). 10-record per-provider
resume-tested pilots before any large launch.

**D-3. LLM track 2 — LLM transforms (trusted self-labels):** prompt codex, claude, and lemex
models to rewrite a given Lean statement with an *explicitly chosen* consistency-preserving or
-breaking transformation, using golden few-shot examples **drawn ONLY from `golden_train`** (never
dev/final_test — this is why A1 must land first) + our own family taxonomy and clear semantic
definitions. The generation IS the label: trust it, subject to typecheck + the stratified audit
protocol (audit judge ≠ generator provider). Pilot AFTER the A1 freeze: 10 records per provider
first (resume-tested), then 200 statements × 3 providers to calibrate per-model trust before
scaling; models whose audit stratum disappoints get demoted to judged-mode or dropped.

**D-4. Corpus assembly** (`src/leanfaith/corpus2/`): merge D-1/D-2/D-3 + ACE-Dataset auxiliary
(≤20%, admitted only after replaying its claimed equivalence/non-equivalence certificates under
the pinned toolchain), dedup by ancestry + normalized representation (one statement can reach the
same final Expr via several paths — bucket counts are not additive), screen against the golden
blocklist, union-find ancestry splits; the ported data_readiness mechanical checks run here, plus
diversity bins (token overlap, length ratio, GTED distance, family, depth, domain). Deliver as
**staged corpora**: `corpus-S1` (deterministic bulk, 300K committed / 750K stretch) and
`corpus-S2` (LLM-produced, higher realism), each tagged public/private per source and carrying
its evidence class. Rerun the lexical toy model on each as a **shortcut canary** (the old corpus
scored 0.80 balanced-acc lexically; the target is well below that).

---

## Track T — Staged training pipeline (S0 → S3)

**S0. Encoder domain adaptation:** MLM continued-pretraining of ModernBERT-base on the Lean CPT
corpus (469,585 rows at
`/storage/milikic/lean_cpt_updates/2026-08-12-curated-libraries/hf_cpt_dataset.jsonl`). Order:
**(a)** build + smoke the MLM runner first (short run → checkpoint reload → M1 initialization
from the adapted encoder) — no runner exists in the repo today; **(b)** screen the CPT corpus
against the A1 golden blocklists (exact + normalized + source-identity — MLM exposure to
final-test theorem text is still test exposure), recording excluded counts; **(c)** freeze a
held-out Lean MLM validation slice; **(d)** full run. **Tokenizer frozen** (fixed 50,368 vocab;
`[REFERENCE]`/`[CANDIDATE]`/`[HEADLESS]` stay ordinary multi-piece strings — no vocab resize).
**Signature setup**: mix full source-file chunks with statement/signature chunks rendered in the
exact packed-pair view the classifier consumes. Smoke on the 4090; **production run on a cluster
A100/H100** (~1–3 epochs, 1024 ctx, bf16 — no GPU contention with generation). Compute is not
scarce, so once the base pipeline works, an S0+S1 pass on **ModernBERT-large** is a cheap headline
upgrade option (same tokenizer, same M1 wrapper) — base remains the iteration vehicle.
Deliverable: `modernbert-lean-v1` + masked-token accuracy vs. stock on the frozen validation slice.

**S1. Large-scale SFT on deterministic data:** train the M1 packed cross-encoder (same
architecture; new `src/leanfaith/train2/trainer.py`, ~300 lines reusing
`build_m1_cross_encoder_module` + the collator verbatim) from `modernbert-lean-v1` on corpus-S1
(300K-committed scale, ≈55/45 pos/neg; multi-epoch, AdamW 2e-5, bf16, early stop on val AUPRC;
cluster GPUs). **Anti-shortcut protocol:**
balanced or explicitly-weighted batches; source-paired positives/negatives where possible;
family/template/provider holdout splits + leave-one-mechanism-out eval; **train with swapped
orientations (or average both directional logits) and report swap disagreement** — packing is
directional but same-claim is symmetric. Ablation for free: the same run from stock ModernBERT
quantifies the S0 gain.

**S2. Refinement on realistic data:** continue training on corpus-S2 mixed with corpus-S1 replay.
**Replay fraction decided by a small ablation (0/10/25/50%) with fixed source quotas, not
guessed**; explicit optimizer policy (fresh AdamW state, lower LR) recorded in the config;
selection on golden dev + retained S1 mechanism-holdout performance; catastrophic forgetting
reported. Optionally a final light pass including `golden_train` (~1.2–1.8K pairs, LR 1e-5, ≤3
epochs) as the golden-fine-tuned variant. Temperature calibration on `dev` (gold-calibrated track
only). Headline comparisons carry multi-seed + group-bootstrap uncertainty — the replay-ablation
arms and seeds run in parallel on the cluster.

**Data-scope policy:** the **headline checkpoint trains on public data only** (keeps release
optionality and avoids the private-data redistribution bind recorded in existing manifests); a
private-augmented variant (deterministic transforms on the 18,668 private statements — never sent
to external APIs) is reported as an ablation. Every corpus manifest records license /
redistribution status per source.

**S3. Evaluate and test** (Track A harness): all development and selection on `dev`. When the
comparison set is frozen (primary model + recipe + calibration + threshold + reporting script),
`final_test` opens **once** for: S1/S2 zero-shot (strict track), gold-calibrated, and
golden-fine-tuned variants + the old 8d815af checkpoint (starting baseline) + all trivial
baselines + the single-cell LLM judge → one consolidated report
`reports/eval/leanfaith_v1_golden_benchmarks.md` with per-source breakdowns and CIs.

**Done / iterate thresholds** — milestone criteria for the project, not claims of scientific
superiority (those rest on the CIs and per-source floors). Context: GTED metric ≈0.66–0.70 acc;
majority-vote-8 LLM ≈0.70 acc:
- **Done**: fine-tuned ≥0.72 balanced-acc, ≥0.65 F1, ECE ≤0.08 post-calibration, ≥5 pts over every
  trivial baseline. Zero-shot (weak-supervision-only, strict track) ≥0.70 = headline result;
  ≥0.65 = strong.
- **Iterate** on the data engine (more D-2/D-3 volume, new transform families guided by the
  per-source error breakdown) if zero-shot <0.60 and fine-tuned <0.70.
- **Stop-loss**: ship "golden-fine-tuned encoder + honest negative result on pure weak
  supervision" if a second data loop doesn't lift zero-shot.

---

## Verification

- Track A: packing-parity unit test + partition-leakage property test green; `leanfaith-eval
  evaluate` runs end-to-end on dev; baselines reproduce sane values (majority ≈ prevalence;
  identity ≈ near-zero recall on golden).
- Track B: after each prune batch, import sweep (`pytest --collect-only`) + surviving unit suite
  green; final full run + one pipeline smoke (extract → transform → corpus sample → M1 forward
  pass); P2 only after the D-2 replacement package passes its 10-record pilot.
- Track D: every pair carries an evidence class (`P-DEF`/`P-SCHEMA`/`P-LEMMA`/`N-SEP`/`N-PROOF`;
  `F2-DIR` never labels binary gold); the Meta-engine audit path reconstructs expected candidates
  independently of the generator; stratified audit results recorded per stratum with error-rate
  upper bounds; every corpus carries a run manifest (inputs, hashes, seeds); shortcut canary
  below 0.70 on corpus-S1/S2; golden blocklist screen reports zero hits; synthetic sources carry
  kernel-checked proofs.
- Track T: S0 masked-LM sanity beats the stock snapshot on the frozen validation slice; the S1
  stock-vs-adapted ablation and swap-disagreement metric are recorded; CPT contamination exclusion
  counts recorded; `final_test` opened exactly once, for the frozen comparison set.
