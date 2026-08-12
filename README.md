# LeanFaith

*A lightweight, calibrated, and reference-aware metric for autoformalization
faithfulness.*

LeanFaith builds a calibrated learned metric that judges whether a candidate Lean 4
theorem statement faithfully expresses the same mathematical claim as a
natural-language statement or a trusted reference formalization — a stricter target
than truth-level logical equivalence.

[PLAN.md](PLAN.md) is the authoritative specification (revision 4.1). Read it before
contributing: §7 is the single path authority, §25 is the coding-agent operating
contract, and §26 is the ordered implementation backlog.

## Development setup

Requirements: Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                    # create .venv and install pinned dependencies
uv run leanfaith --help
```

Install the git hooks once per clone:

```bash
uv run pre-commit install
```

## Quality checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

All four must pass in a clean checkout (PLAN.md §26, LF-001 acceptance).

## Secrets

Copy `.env.example` names into your environment or secret manager; never commit
values. `HF_TOKEN` is required for the private `formalmathatepfl/sft_classic`
dataset probe (PLAN.md §9.2).

## Status

- **Corrected mixed-proxy learning curve completed and exactly replayed
  (2026-08-12):** nine deterministic scalar models used exact 2,000-, 5,000-,
  and 9,313-record ancestry-atomic training prefixes from the 11,501-pair mixed
  corpus. Full-corpus pseudo-AUPRC reached 0.574008 on test and 0.552862 on
  validation, versus constant baselines 0.339623 and 0.324651; balanced accuracy
  was 0.804837 and 0.802865. The gain mostly saturates by 5,000 records, which
  supports moving to richer token encoders and harder composed pairs rather
  than repeatedly scaling this toy lexical model. The standalone verifier
  passed and a second full run replayed the immutable artifact exactly. These
  are machine-proxy diagnostics, not semantic-faithfulness results or model
  selection evidence. See
  `reports/model_selection/experimental_mixed_scalar_learning_curve_v2.md`.

- **First verified learning curve completed:** the opt-in scalar diagnostic
  fitted 18 deterministic models and froze 6,876 predictions over the 2,000-pair
  experimental corpus. At the full 1,260-component training budget it reached
  pseudo-AUPRC 0.8154 on validation and 0.8064 on test, versus constant-score
  AUPRC about 0.497. The artifact passed standalone verification and exact byte
  replay. This demonstrates end-to-end learnability of transformation
  intentions—and the risk of synthetic shortcuts—not semantic faithfulness;
  it is excluded from model selection, calibration, evaluation, and scientific
  claims. See
  `reports/model_selection/experimental_scalar_learning_curve_v1.md`.

- **First loader-ready experimental corpus frozen:** a clean, pushed producer
  revision materialized 2,000 public-mathlib Lean--Lean pairs: 1,000 E2
  intended-same-claim transformations and 1,000 D0 intended-near-miss
  transformations. The corpus contains 1,569 ancestry-connected components,
  has fixed train/validation/test splits, passed benchmark screening and exact
  byte replay, and includes a readable 20-pair sample. It is deliberately
  provisional machine supervision, not semantic gold: it requires explicit
  opt-in and is forbidden for model selection, calibration, evaluation, or
  scientific claims. See
  `reports/model_selection/experimental_machine_supervision_mathlib_2k_v1.md`.

- **Implemented:** LF-001 through LF-018, including the LeanInteract backend,
  source adapters, proof-free extraction, isolated multi-view
  representations, and the fail-closed transformation protocol/registry/
  promotion boundary plus all eight scoped deterministic transformation
  families.
- **Passed:** Gate 0 for internal research only and Gates 1, 2, and 3,
  including the frozen 20,000-row ingestion audit and exact 10,000-theorem
  representation audit. The current `repr_v3` implementation also passed a
  fresh, independent revalidation on the unchanged 5,000-mathlib plus
  5,000-`sft_classic` denominator: every required view reached 100%, semantic
  replay passed all 10,000 records, alpha invariance passed 1,000/1,000,
  cross-path comparison passed 500/500, and 152/152 lossy clusters were
  reviewed and closed. See `reports/gates/gate_3_repr_v3.json`. The historical
  `repr_v2` decision and artifacts remain preserved rather than relabelled.
- **Passed:** the additive benchmark representation-signature and overlap
  freeze over all 14,534 statements from the two locally resolved v1
  benchmarks. The active hash-only registry is
  `data/benchmarks/frozen_ids.representations_v1.json`.
- **Passed:** LF-017 positive implementation validation and 140 focused
  unit/property/live LeanInteract checks. All outputs remain provisional,
  with zero gold labels or promotions.
- **Passed:** LF-018 negative implementation validation and a persisted
  Lean-backed five-family pre-scale slice with complete provisional lineage,
  zero resolved labels, and zero promotions. See
  `reports/milestones/lf_018_scoped_negatives.md`.
- **Passed:** LF-019 and Gate 4G. Two clean, content-addressed runs exercised all
  eight active families and reproduced the same semantic fingerprint; the
  release and model-selection guards rejected every smoke artifact as required.
  The fail-closed gate report is `reports/gates/gate_4g.json` (SHA-256
  `5c1f2e86230a8b7ebf884d9f10369a504bc5cbda4bc472321076c178a2cf43f7`).
  Gates 4A and 4B remain open, with zero gold labels or family promotions. See
  `reports/milestones/lf_019_smoke_vertical_slice.md`.
- **Passed:** LF-020 symbolic evidence collection. Two independent empty-cache
  runs produced the same 40 terminal evidence jobs, 9 certificate/axiom audits,
  and enriched 8 smoke pairs with zero failures, unresolved links, new labels,
  or promotions. The self-hashed replay audit is
  `reports/evidence/lf020_smoke_replay_v1.json`; see
  `reports/milestones/lf_020_evidence_pipeline.md`.
- **Passed:** LF-021's mechanical Gate 5G collection checkpoint. The exact
  16-tranche lineage covers 1,440 terminal invocations and 299
  compile-and-benchmark-clear members. Problem-aware deduplication leaves 250
  eligible units, from which a production CSPRNG froze the 240-item,
  31-stratum human prevalence frame. Gate 5G is explicitly closed by
  `reports/gates/gate_5g.json` (SHA-256
  `f62a39478c589368c036644ddf5a4b4fd426ac0a49886218219846f825059332`).
  All retained candidates remain unresolved `REVIEW` records: compilation is
  not semantic faithfulness, no labels or supervision records were created,
  and Gate 5 remains open pending genuine human adjudication. See
  `reports/generation_coverage.md` and
  `reports/milestones/phase_5_real_outputs.md`.
- **Annotation bundles ready; authenticated human assignment still pending,
  not model training:** the exact 240-item frame
  has two independently randomized, reference-aware blinded bundles generated
  under the ignored `annotation/exports/lf021_prevalence_v1/` operational
  directory. The tracked codebook, template, and exporter show annotators only
  the natural-language claim plus proof-free reference Lean A and candidate
  Lean B views; private linkage and randomization keys are not committed.
  Production response import additionally requires a mode-0600,
  HMAC-authenticated pre-response assignment and an authenticated attestation
  binding the exact locked backend export. Test fixtures are explicitly
  non-human, non-gold, and non-training; a self-authored response file alone is
  rejected.
  The fail-closed readiness audit reports `NOT_READY`: there are currently zero
  human terminal labels, no promoted production LF-022 SCI/open data, no frozen
  training inventory, and none of the four purpose-restricted gold products.
  The audit now verifies full label/evidence/promotion/source lineage and cannot
  become ready from manifest presence alone. See
  `reports/model_selection/training_data_readiness_v1.md`. Training must not
  begin until that audit authorizes it.
- **Argilla integration validated, without claiming human labels:** the pinned
  self-hosted Argilla 2.8 deployment passed a disposable live integration run
  with two isolated annotator workspaces, direct peer-access denial, submitted
  response identity checks, exact HTTP-byte retention, and a separate
  adjudication workspace. The validator and production direct-fetch adapter
  cannot create semantic labels, gold labels, or training records. Real expert
  accounts, authenticated assignments, independent responses, adjudication,
  and Gate 5 closure are still pending. Argilla dependencies use the isolated
  lock under `annotation/platforms/argilla/`; the frozen root `uv.lock` remains
  byte-identical for LF-021 replay.
- **Qualified operationally, not scientifically:** LF-022 now has strict
  proposer/judge parsing, family separation, blinded swapped judging,
  candidate-only aggregation, and a complete public-source RCP smoke. The
  successful lineage used Kimi-K2.7-Code, Qwen3.5-397B, and GLM-5.2 for exactly
  five calls and replays offline. Two preceding, separately versioned
  fail-closed attempts are preserved as terminal artifacts. Every resulting
  record is smoke-quarantined and contributes zero labels, training examples,
  evaluation examples, silver promotion, or gate credit. Gates 6G and 6 remain
  open. A new non-executable allocation planner can bind exact public-source
  authorization, extraction, representation, benchmark-clearance, provider
  deployment, and family-separation artifacts for later production scaling;
  it authorizes no network call or label. See
  `reports/milestones/phase_6_llm_data.md`.
- **F1-corrected combined proxy corpus is frozen and replay-verified
  (2026-08-12):** the complete selectable deterministic first-hop projection
  and completed Kimi/Qwen Codex audits now form an immutable ancestry-split
  dataset with 11,501 records (9,313 train, 1,075 validation, 1,113 test). It
  contains 3,857 same-claim and 7,644 not-same-claim proxy targets, including
  1,174 LLM-judged signals. This successor recovered exactly 664 pairs that the
  historical v1 adapter had incorrectly rejected by treating truth-level F2
  implication opinions as constraints on F1 claim faithfulness; no old exact
  pair, signal, or target was removed or changed. Only two expert-review cases
  remain excluded. Independent external-input verification and exact replay
  passed. The artifact is allowed only for smoke training, learning curves,
  and proxy diagnostics; it is not human gold, semantic silver, confirmatory
  model-selection data, or evaluation data. See
  `reports/model_selection/experimental_mixed_supervision_firsthop_lf022_f1corrected_v2.md`.
- **Depth-two composition has enlarged the clean proxy corpus to 17,031 pairs
  (2026-08-12):** the complete receipt-bound deterministic composition export
  was admitted alongside the first-hop and completed Kimi/Qwen Codex signals.
  The authoritative clean-revision artifact contains 13,633 train, 1,604
  validation, and 1,794 test pairs; 4,538 same-claim and 12,493 not-same-claim
  proxy targets; and 6,469 ancestry-connected components. Its 17,044 signals
  comprise 10,336 deterministic first hops, 5,534 deterministic depth-two
  compositions, and 1,174 Codex judgments. A second complete run replayed the
  dataset ID and every output byte exactly. This is sufficient for the first
  token-encoder proxy run, but remains machine proxy supervision rather than
  human gold or confirmatory evaluation data. See
  `reports/model_selection/experimental_mixed_supervision_composition_v1.md`.
- **The first neural M0 proxy checkpoint is trained and verified
  (2026-08-12):** a shared ModernBERT-base dual encoder completed one frozen
  epoch over 5,920 balanced proxy examples from 3,868 ancestry components. The
  exact verifier passed for all inputs, tokenizer bindings, schedule,
  predictions, metrics, and the 596 MB safetensors checkpoint. Validation
  pseudo-AUPRC is 0.630011 and test pseudo-AUPRC is 0.603207, with balanced
  accuracies 0.695354 and 0.686989. These are machine-proxy diagnostics, not
  semantic-faithfulness estimates or model-selection results. See
  `reports/model_selection/experimental_m0_proxy_v1.md`.
- **The first packed-cross-encoder M1 checkpoint is trained and independently
  verified (2026-08-12):** M1 completed the same 185-step, 5,920-example
  balanced proxy schedule as M0, but jointly encodes each reference/candidate
  pair. Its validation pseudo-AUPRC is 0.907573 and test pseudo-AUPRC is
  0.887478, versus 0.630011 and 0.603207 for M0. Exact verification binds all
  9,304 predictions to the source records and schedule. This strong gain is on
  machine-proxy targets only; it is not yet a human-faithfulness result. See
  `reports/model_selection/experimental_m1_proxy_v1.md`.
- **The mixed proxy corpus now contains 17,181 pairs (2026-08-12):** a new
  disjoint 150-item Qwen tranche completed Lean checking and exact GPT-5.6 Sol
  audit verification, adding 147 not-same-claim and three same-claim proxy
  verdicts with no execution or parsing failures. The complete successor
  corpus contains 10,336 deterministic first-hop, 5,534 deterministic
  composition, and 1,324 single-judge proxy signals over 6,599 ancestry
  components. It exact-replays from a clean checkout, but remains machine
  proxy supervision rather than semantic gold. See
  `reports/model_selection/experimental_mixed_supervision_qwen1125_v1.md`.
- **The first bidirectional-matcher M2 checkpoint is independently replayed
  (2026-08-12):** the repaired clean run completed 186 optimizer steps over
  5,952 balanced proxy examples from 3,904 ancestry components. Validation
  pseudo-AUPRC is 0.715701 and test pseudo-AUPRC is 0.683699. A fresh-process
  verifier reconstructed the exact split-isolated batching and dynamic
  padding, replayed predictions and metrics, checked the final checkpoint, and
  passed the 64-pair swap-invariance audit with exactly zero probability
  difference. The older `3f5b23e` artifact is quarantined because it failed
  independent replay. These remain machine-proxy diagnostics, not semantic
  faithfulness results. See
  `reports/model_selection/experimental_m2_proxy_v1.md`.
- **Active data-production checkpoint (2026-08-10):** the preserved public
  LF-022 execution contains 668 parsed provisional variants.  The production
  four-worker LeanInteract checker has now processed every one: 493 elaborate
  with a proof placeholder and 175 are confirmed invalid.  By proposer, the
  mechanically valid counts are Qwen 310/439, Kimi 181/227, and GLM 2/2.
  These are useful Lean--Lean candidate pairs, but elaboration is not a
  semantic label.  The separate GPT-5.6 Sol audit has completed all 493 public
  valid pairs: it judged 483 not-same-claim, 9 same-claim, and 1 uncertain.
  Every stored input and response was replayed against its recorded hash.  This
  remains a single-family, one-orientation diagnostic audit rather than human
  gold or the registered two-family weak-consensus route; it creates no gold,
  silver, training, evaluation, or gate-credit records.  See
  `reports/generation/lf022_codex_sol_xhigh_v2_summary.md`. The canonical check
  manifest remains
  `/storage/milikic/leanfaith/lf022_lean_checks/rcp_5e672b9_v1/manifest.json`.
- **LF-022 checked data now has one authoritative deduplicated inventory:**
  four immutable checker partitions contribute 1,967 gross observations but
  only 1,502 canonical source/candidate pairs. Of these, 1,106 are Lean-valid,
  1,080 have a completed Codex audit, and 26 valid pairs remain unaudited.
  Repeated audits expose 51 pair-level core-judgment conflicts, so this is an
  audit inventory rather than resolved supervision. The exact output replayed
  byte-for-byte and remains ineligible for labels, training, evaluation, or
  gate credit. See
  `reports/generation/lf022_merged_checked_inventory_v1.md`.
- **Kimi-v4 challenge passed and its first bounded production tranche completed:** the
  frozen 16-item hard-case requalification produced 16/16 strict variants,
  with two transient HTTP-429 attempts successfully retried and no truncation,
  empty response, or parser failure. The route certifier replayed all sixteen
  terminals with zero network calls, and the resulting route-only eligibility
  was bound into a clean scientific admission. One ordinary generated theorem
  then passed exact offline replay and LeanInteract elaboration before the
  isolated 256-task tranche was launched and reached 256/256 terminal tasks:
  248 parsed variants, six exhausted-provider terminals, and two parse
  failures. Batch-scoped Lean checking now binds the exact content-addressed
  generation manifest, so historical executor outputs cannot leak into a
  tranche's mechanical validation. The prefix-256 operational QA replayed all
  256 terminals without network access. LeanInteract accepted 201 of the 248
  generated variants and rejected 47; the Codex audit covered all 201
  mechanically valid candidates. These records
  still create no label, promotion, training example, evaluation example, or
  gate credit. See `docs/lf022_public_generation.md` and
  `reports/generation/lf022_current_exploratory_inventory_v1.md`. A new
  content-addressed inventory replay now binds the complete Codex response
  artifact set, not merely its manifest; its final Kimi snapshot is
  `reports/generation/lf022_inventory_kimi_prefix256_v1.json`. A separately
  frozen, explicitly non-final Qwen snapshot captured 941 completed tasks and
  918 exact provisional variants, while the combined Kimi/Qwen point-in-time
  snapshot contained 1,166 distinct source/candidate pairs with zero exact
  cross-model duplication. Those live snapshots are inventory evidence only,
  not semantic labels or training admission.
- **First LF-022 judging inventory is inspectable and replay-stable:** all 201
  Lean-valid Kimi prefix-256 pairs are packaged as 201 unique judge-visible
  payloads. Deduplication binds Lean A, Lean B, and optional natural language;
  a relocated rebuild reproduced byte-identical records, sample, summary, and
  manifest. The inventory schedules 804 future calls for two independent judge
  families in both statement orders. The prior Codex verdict is retained only
  as a diagnostic, so this milestone still creates zero semantic labels,
  silver records, training examples, evaluation examples, or gate credit. See
  `reports/generation/lf022_supervision_candidates_kimi_prefix256_v2.md`.
- **LF-022 candidate admission no longer depends on a Codex audit:** candidate
  inventory schema v3 derives its unresolved dispatch set from exact public,
  Lean-valid checks and their bound Lean-check manifest. A complete
  replay-verified Codex audit may be attached as optional diagnostic metadata,
  but supplies zero weak-judge votes and cannot admit, exclude, or promote a
  pair. Historical schema-v2 inventories and reports remain replayable and are
  not rewritten. The first real direct materialization recovered all 201
  Lean-valid Kimi prefix-256 pairs and scheduled 804 future two-family
  swapped-order calls with no Codex dependency; see
  `reports/generation/lf022_supervision_candidates_kimi_prefix256_v3.md`.
- **The Qwen checked snapshot is now a real, replay-stable judging
  inventory:** a selected-only historical replay scans the complete 9,207-task
  frozen batch envelope but opens only the 1,046 selector-bound task bodies,
  while still reconstructing every selected attempt, provider request, wire
  response, raw response, LLM record, parsed output, variant, and terminal.
  The 1,019 checked variants yield **718 unique Lean-valid unresolved pairs**
  and 2,872 future two-family swapped-order judge calls. The build replayed
  exactly, completed in 16.37 seconds, and peaked at 159,672 KiB RSS. Combined
  with Kimi, the source-neutral schema-v3 queues now contain **919
  judge-ready pairs** and 3,676 future calls, but still create zero semantic
  labels, silver records, training records, evaluation records, or gate
  credit. See
  `reports/generation/lf022_supervision_candidates_qwen_snapshot1019_v3.md`.
- **LF-022 two-family batch execution now has a separate fail-closed live
  qualification boundary:** a self-contained, raw-first batch binds the
  canonical production family matrix, enforces proposer/judge/held-out role separation,
  prepares both statement orders for each of two independent judge families,
  resumes from canonical raw responses, and finalizes only non-trainable weak
  consensus candidates. The live boundary deterministically selects one public
  Qwen-proposed pair, admits exactly Kimi and DeepSeek in both statement orders,
  permits at most four serial calls after an explicit flag, and supports
  zero-network replay. It is route-smoke qualification only: it neither
  qualifies scale judging nor creates semantic labels, silver records, training
  data, evaluation data, or Gate credit. The commands are implemented and
  tested with injected transports; no live call follows from implementation or
  preparation alone. A separate offline authoring command now freezes the exact
  Qwen schema-v3 inventory, Kimi/DeepSeek judge contracts, held-out evaluator,
  production matrix/catalog, weak policy, and randomization-key hash into the
  generic prepared-batch input; the workflow no longer depends on a test-only
  handwritten spec. See `docs/lf022_public_generation.md`.
- **LF-024 diagnostic resolver core implemented; production resolution remains
  blocked:** the public resolver accepts semantic candidates only through an
  opaque, process-local `VerifiedCandidateSet` capability. No factory for a
  nonempty production capability exists yet. The diagnostic CLI therefore
  rejects every nonempty raw candidate partition; an explicitly empty
  partition still produces the exact unresolved `REVIEW` contract. The private
  diagnostic core retains deterministic precedence/conflict tests without
  becoming an authority boundary, and structural candidate-set verification
  cannot mint or coerce the capability. Every diagnostic label is forcibly
  ineligible for training and evaluation. The external run manifest is the
  commit marker; a strict output descriptor permits recognized orphaned output
  to be quarantined after the hard-crash publication gap. The production
  artifact-class guard remains enabled until typed authority replay, admission,
  re-resolution, and F0 certificate adapters are complete. This milestone
  creates **0 production semantic labels, 0 production evidence admissions,
  and 0 promotions**; see
  [`reports/milestones/lf_024_resolver_core.md`](reports/milestones/lf_024_resolver_core.md).
- **Deterministic unary scale-out materialized:** all 16 producer shards over
  the frozen 27,786-statement public universe completed. A separate
  content-audit merge at code revision `645a9a8` verified immutable inputs,
  journals, receipts, raw-response bindings, projections, identities, and
  lineage and materialized 27,327 provisional pairs. Its self-hashed manifest
  is `dda088624e25ee271a7ac8d013e8f63414188596a35c3d5c240ef8b72dfc268d`
  (file SHA-256
  `699e34ecd90547750520d7a680de7f39ffe981e0705c832c4071f1f0d82b95d2`).
  The stricter second full Lean replay continues independently. The content
  audit explicitly records `training_eligible=false`,
  `evaluation_eligible=false`, and `gate_credit=false`; no semantic label or
  promotion follows from generation or typechecking alone.
- **Deterministic-v2 public expansion is cleanly materialized:** duplicate
  byte-identical N15/N16 rerun roots were removed without losing any of the
  2,004 pre-N11 exact pair keys. The isolated N11 replay completed all 27,786
  source attempts without an infrastructure failure and added 233 provisional
  pairs. The final all-clean inventory therefore contains **2,237 exact
  source/candidate groups** across 11 unique roots, including 2,232 distinct
  candidate-code keys and 2,234 distinct alpha keys. The earlier 2,241 number
  and 2,006 gross count are preserved only as historical/superseded audit
  context. No count is a semantic label or confirmatory training admission.
  See `reports/transformation_audits/lf033_public_all_clean_inventory_v1.md`.
- **Private deterministic-v2 first full portfolio complete:** all configured
  deterministic-v2 families ran over the exact frozen 5,000-statement
  `sft_classic` Gate-3 subset. The fail-closed combination contains 5,497 exact
  source/candidate pairs, 5,496 distinct candidate-code hashes, and 5,495
  distinct alpha-normalized candidate fingerprints. Its reversed-input replay
  reproduced the same content identity. Only private-safe counts and hashes
  are tracked in
  `reports/transformation_audits/lf033_private_all_families_inventory_v1.json`;
  no private theorem text is committed. Every pair remains intention-only,
  unresolved, and ineligible for training, evaluation, promotion, or gate
  credit.
- **P12 deterministic expansion is fully materialized and replay-verified:** a
  separate P12 v1.1 matcher adds complex, visibly propositional root proof
  arrows without changing the accepted P12 v1.0 bytes or effective hash. Its
  read-only probe finds 89 opportunities in the frozen private 5,000 statements
  and 278 in the public 27,786 statements. Nested arrows, data-function
  domains, dependent binders, and unsupported syntax fail closed. Same-context
  LeanInteract re-elaboration plus the complete E0 identity audit accepted 82
  private and 99 public records: **181 provisional records / 179 distinct
  source-candidate text pairs**. A complete
  offline resume replay reproduced both result sets and manifests byte for
  byte. Two public attempts ended in `lean_crash` and remain infrastructure
  failures, not semantic negatives. See
  `reports/transformation_audits/p12_v110_scale_materialization_v1.md` and the
  inspectable public pairs in
  `reports/transformation_audits/p12_v110_public_examples_v1.md`.
- **Equality-root deterministic expansion and safe composition groundwork are
  implemented:** P18 equality symmetry and N18 equality-polarity runs completed
  over both the frozen 5,000 private statements and 27,786 public statements,
  producing 3,293 provisional variants before cross-family deduplication. The
  exact combiner correctly rejected two preserved infrastructure failures in
  the historical public P12 root. An isolated single-worker P12 replay then
  completed all 27,786 results with zero infrastructure failures, without
  weakening the admission rule. Recombining 30 exact public/private roots
  produced **11,208 unique exact source/candidate pairs** with zero exact
  duplicate excess. A separate immutable seed boundary admits only clean,
  certificate-backed P14--P18 positive outputs for a second deterministic hop.
  The depth-two chain auditor then admits only P14--P18 E2 or N11--N18 D0
  second hops, binds the exact positive certificates, and preserves complete
  ancestry and input hashes. This enables controlled P-to-P and P-to-N
  experiments while blocking N-to-anything and third-hop chains; it creates no
  labels or training eligibility. See
  `reports/transformation_audits/lf033_equality_composition_inventory_v1.md`.
- **Depth-two deterministic outputs are now cycle-aware:** the completed P14
  chain receipt contains 1,888 unique raw source/final-code pairs, but 1,333
  return to the original theorem's alpha identity. The immutable postprocessed
  inventory therefore identifies only 555 alpha-novel pairs while retaining
  the reversible cycles for audit. It replayed exactly and creates no label,
  promotion, training/evaluation eligibility, or gate credit. See
  `reports/generation/deterministic_v2_p14_unique_pair_inventory_v1.md`.
- **The complete depth-two export boundary is implemented and fail-closed:** a
  receipt-bound exporter requires all thirteen P14--P18/N11--N18 second-hop
  roots and reports the exact 65 supported first-hop/second-hop sequences. It
  exposes only alpha-novel pairs with one consistent mechanical intention,
  retains source-return cycles for audit, and quarantines any pair reached by
  both a P-to-P and P-to-N history. Original and final Lean text, ancestry,
  context, content hashes, alpha fingerprints, and public/private source policy
  are all replay-bound. Every exported pair remains provisional and contributes
  zero labels, promotions, training/evaluation eligibility, or gate credit.
  The exporter is ready; the live inventory intentionally waits for the full
  thirteen-family orchestration receipt rather than accepting partial roots.
- **Public LF-022 scale-out preparation is deterministic and still
  non-executable:** a pinned, progressively expandable mathlib file frame
  feeds exact extraction and representation runs. The production pool admits
  only public, denylist-clear, fully represented source theorems whose
  representation hashes and Lean contexts replay against the approved source
  revision. It requires 15,000 recomputed, distinct root ancestries and plans
  one `G_sci` plus one `G_open` task per source. The largest confirmatory arm
  needs 12,500 unique valid outputs from either distribution, so the plan has
  a 20% task buffer. This is not a yield guarantee: failed/duplicate outputs
  do not count, and capacity is recomputed over final connected split
  components. Source capacity creates no label and does not change the
  `NOT_READY` training decision.
- **Generation identities are frozen and DeepSeek is proposer-qualified:** the
  original matrix proposed Kimi-K2.7-Code, Qwen3.5-397B, and GLM-5.2. Because
  the live GLM route now fails with HTTP 403, versioned matrix v2 instead
  proposes Kimi, Qwen, and DeepSeek while preserving GLM as historical and
  non-proposer supervision evidence. DeepSeek passed one strict public
  proposer execution, exact offline replay, and route certification; the
  resulting eligibility remains provisional and creates no semantic label.
  The immutable 27,620-source pool was exact-reallocated onto matrix v2 and a
  separate admission was frozen. The guarded DeepSeek prefix completed all 32
  tasks, replayed them offline with zero network calls, and the four-worker
  LeanInteract check found 27 elaborating candidates and five invalid ones.
  Codex remains fully held out from generation and from weak-supervision vote
  roles; optional Codex audit metadata remains diagnostic-only. Gate 5 human
  adjudication is deferred, not silently replaced by model judgments.

Stable gate-facing commands are available through `leanfaith`:

```bash
uv run leanfaith freeze-code-bundle --help
uv run leanfaith sample-gate2 --help
uv run leanfaith sample-gate2-arrow --help
uv run leanfaith extract --help
uv run leanfaith freeze-mathlib-file-frame --help
uv run leanfaith audit-extraction-regression --help
uv run leanfaith audit-extraction-replay --help
uv run leanfaith audit-gate2-scale --help
uv run leanfaith freeze-benchmarks --help
uv run leanfaith freeze-gate3-inputs --help
uv run leanfaith represent --help
uv run leanfaith audit-representations --help
uv run leanfaith audit-representation-replay --help
uv run leanfaith audit-alpha-invariance --help
uv run leanfaith audit-representation-cross-path --help
uv run leanfaith append-benchmark-signatures --help
uv run leanfaith generate-deterministic --validate-only
uv run leanfaith generate-deterministic --validate-positives
uv run leanfaith generate-deterministic --validate-negatives
uv run leanfaith generate-deterministic --run-negative-pre-scale
uv run leanfaith generate-deterministic --run-smoke-vertical-slice
uv run leanfaith close-gate4g --help
uv run leanfaith collect-evidence --help
uv run leanfaith collect-real-outputs --validate-foundation
uv run leanfaith collect-real-outputs --run-offline-smoke
uv run leanfaith export-annotation --help
uv run leanfaith create-human-assignment --help
uv run leanfaith attest-human-submission --help
uv run leanfaith import-annotation --help
uv run leanfaith write-annotation-agreement --help
uv run leanfaith write-adjudication-queue --help
uv run leanfaith validate-lf022 --help
uv run leanfaith freeze-lf022-family-matrix
uv run leanfaith freeze-lf022-qwen-weak-batch-spec --help
uv run leanfaith prepare-lf022-weak-batch --help
uv run leanfaith replay-finalize-lf022-weak-batch --help
uv run leanfaith freeze-lf022-weak-live-smoke --help
uv run leanfaith prepare-lf022-weak-live-smoke --help
uv run leanfaith execute-lf022-weak-live-smoke --help
uv run leanfaith replay-lf022-weak-live-smoke --help
uv run leanfaith materialize-lf022-public-pool --help
uv run leanfaith lf022-rcp-smoke --help
uv run leanfaith freeze-lf022-proposer-admission --help
uv run leanfaith certify-lf022-proposer-route --help
uv run leanfaith make-lf022-public-batch-request --help
uv run leanfaith freeze-lf022-public-batch --help
uv run leanfaith run-lf022-public-batch --help
uv run leanfaith qa-lf022-prefix256 --help
uv run leanfaith check-lf022-provisional-lean --help
uv run leanfaith audit-lf022-codex --help
uv run leanfaith summarize-lf022-codex-audit --help
uv run leanfaith build-lf022-supervision-candidates --help
uv run leanfaith build-lf022-merged-checked-inventory --help
uv run leanfaith run-deterministic-shards --help
uv run leanfaith merge-deterministic-shards --help
uv run leanfaith probe-deterministic-v2-coverage --help
uv run leanfaith materialize-deterministic-v2-d0-scale --help
uv run leanfaith materialize-deterministic-v2-e2-scale --help
uv run leanfaith prepare-deterministic-composition-seeds --help
uv run leanfaith audit-deterministic-composition-chains --help
uv run leanfaith postprocess-deterministic-composition-unique-pairs --help
uv run leanfaith export-deterministic-composition-receipt --help
uv run leanfaith combine-deterministic-scale-passes --help
uv run leanfaith audit-training-readiness --report-only
```

After every launcher shard has completed, merge the whole run without manually
enumerating producer directories:

```bash
uv run leanfaith merge-deterministic-shards \
  --output-root /path/to/deterministic-run \
  --output-dir /path/to/deterministic-run/merged \
  --expected-shard-count 16
```

The command refuses active, incomplete, noncanonical, or mixed-lineage shard
sets. It delegates to the scientific merger, which replays Lean checks,
reconstructs provenance from the immutable source inventory, rejects duplicate
pairs/variants, and writes content-addressed outputs. Re-running the same
command safely resumes or verifies identical atomic outputs.

For exploratory data inspection while that full second Lean replay is still
running, the lower-trust path is deliberately separate:

```bash
uv run leanfaith generate-deterministic \
  --merge-scale-shards-provisional \
  --output-dir /path/to/deterministic-run/provisional-merged \
  --shard-output-dir /path/to/deterministic-run/shard_00 \
  --shard-output-dir /path/to/deterministic-run/shard_01
```

Repeat `--shard-output-dir` for the complete bound shard set. This command
still audits immutable inputs, journals, receipt chains, producer manifests,
raw Lean-response trees, partitions, record identities, and cross-record
lineage. It does **not** run the merger's second Lean replay. Its distinct
manifest permanently records `training_eligible=false`,
`evaluation_eligible=false`, and `gate_credit=false`; it is usable only for
exploratory mining and smoke modeling until the strict merge supersedes it.

`probe-deterministic-v2-coverage` is a read-only design probe. It reports broad
surface signals for the disabled P05–P17/N11–N17 portfolio without executing
Lean, emitting variants, or creating labels. A signal count is an upper bound,
not proof that the corresponding transformation is applicable.

The additive deterministic-v2 execution profiles are separate from that
frozen design registry. Their scale commands re-elaborate each emitted
candidate and rebuild its representations through LeanInteract. P15 root-Iff
reversal and P16 exact three-atom root-conjunction reassociation use separate
strict profiles but share the same profile-aware E2 scale command. Both are
persisted as E2 positive structural evidence only: even a clean result remains
provisional, unresolved, unpromoted, and unavailable to training until the
later label/promotion policy authorizes it. Select P16 with
`--profile configs/transformations/v2_e2_p16_experimental.yaml`.

The completed LF-032 public scale run tested six E0 families over 27,786
mathlib statements and retained 270 unique clean provisional pairs after exact
fail-closed replay. The counts, hashes, residual quarantines, and credit
boundary are recorded in
[`reports/transformation_audits/lf032_public_scale_audit.md`](reports/transformation_audits/lf032_public_scale_audit.md).
An exact read-only portfolio scan of the frozen 5,000-mathlib plus
5,000-`sft_classic` corpus is recorded in
[`reports/transformation_audits/lf033_lf034_private_opportunity_audit.md`](reports/transformation_audits/lf033_lf034_private_opportunity_audit.md);
it shows that the private half supplies most P14–P17 and N11–N17
opportunities and binds the queued private materialization to immutable input
hashes.

The default Codex/LLM pair-judge prompt is the reviewed v2 contract. It keeps
F1 claim faithfulness (`same_claim_answer`, `relation`) separate from F2
truth-level implication (`A_implies_B`, `B_implies_A`) and rejects incoherent
outputs rather than silently normalizing them. The immutable v1 prompt remains
available only for replaying its versioned artifacts.

Qwen3.5 and GLM-5.2 public production routes remain blocked until one exact
live proposer qualification per family succeeds and the persisted result is
certified offline. See [`docs/lf022_public_generation.md`](docs/lf022_public_generation.md).
Certification authorizes only a provider route; every generated theorem stays
unresolved, unvalidated, provisional, and ineligible for labels, training,
evaluation, or Gate credit.

Scientific-scale deterministic materialization uses separate sharded-unary and
global-N10 passes. Merge itself performs the mandatory exact Lean-backed
replay; its self-hashed replay audit is accounting metadata, not a trust
primitive. Treating both passes together additionally requires the
`combine-deterministic-scale-passes` compatibility manifest. `--fast-resume`
is retired. See
[`docs/deterministic_scale_operations.md`](docs/deterministic_scale_operations.md)
for the fail-closed execution and legacy-journal recovery contract.

Scale extraction and representation runs accept `--resume-work-dir`. Each
completed chunk is bound to its exact input, context, code tree, code bundle,
and relevant execution configuration; mismatched resume state fails closed.
When unfinished chunks exist, the parent process lets LeanInteract prepare
the pinned project and REPL once; chunk workers then reuse that prepared
environment with project and REPL rebuilding disabled. The setup mode is
included in chunk and final-manifest provenance.
