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
- **Generation identities are frozen but scientific execution remains
  blocked:** Kimi-K2.7-Code, Qwen3.5-397B, and GLM-5.2 are the proposed
  proposer families; DeepSeek is the required fourth supervision family and
  Codex is fully held out. The offline freeze preserves the failed DeepSeek
  structured-output qualification instead of silently authorizing it. Gate 5
  human adjudication and a separately reviewed execution admission are still
  required.

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
uv run leanfaith materialize-lf022-public-pool --help
uv run leanfaith lf022-rcp-smoke --help
uv run leanfaith certify-lf022-proposer-route --help
uv run leanfaith make-lf022-public-batch-request --help
uv run leanfaith freeze-lf022-public-batch --help
uv run leanfaith run-lf022-public-batch --help
uv run leanfaith combine-deterministic-scale-passes --help
uv run leanfaith audit-training-readiness --report-only
```

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
