# LeanFaith: A Lightweight, Calibrated, and Reference-Aware Metric for Autoformalization Faithfulness

**Working title:** *LeanFaith: A Lightweight, Calibrated, and Reference-Aware Metric for Autoformalization Faithfulness*
**Document purpose:** implementation specification for a coding agent and research roadmap for the project team  
**Status:** Gates 0 (internal research only), 1, 2, historical Gate 3 (`repr_v2`), current Gate-3 `repr_v3` revalidation, 4G, and the mechanical Gate 5G passed; `repr_v3` is scientifically validated on the unchanged frozen 10,000-record denominator, while historical `repr_v2` artifacts remain versioned historical evidence; the additive benchmark-signature and overlap freeze passed; LF-019 and LF-020 are complete; Gates 4A and 4B remain open; LF-021 completed 16 replay-verified scalable tranches (1,440 terminal invocations), yielding 299 compile-and-benchmark-clear members and 250 unique problem-aware eligible units; the production CSPRNG froze a 240-item, 31-stratum human prevalence frame and the reference-aware blinded two-annotator export is operationally materialized; LF-022 has 668 parsed public `G_open` provisional variants from Kimi/Qwen/GLM, of which the production four-worker LeanInteract checker confirms 493 elaborate with a placeholder and 175 are invalid; a resumable GPT-5.6 Sol audit is operational for public Lean-valid pairs but remains audit-only and creates no label, promotion, supervision, training, evaluation, or gate-credit record; all 16 deterministic unary shards over 27,786 public statements completed and a lower-trust content audit materialized 27,327 provisional pairs while the strict second Lean replay continues; Gate 5 remains open pending genuine human adjudication and Gates 6G/6 remain open pending promotion; the hardened fail-closed training-data audit remains `NOT_READY` with zero safe F1 labels, no frozen training inventory, and zero of four gold products
**Revision:** 4.1
**Last revised:** 2026-08-11
**Canonical filename:** `PLAN.md`  
**Primary Python–Lean interface:** [LeanInteract](https://github.com/augustepoiroux/LeanInteract)  
**Initial LeanInteract pin:** `lean-interact==0.11.4`  

### Revision 4.1 changes

1. Repositions LeanFaith after FormalRx as a lightweight, non-generative, calibrated Lean–Lean claim-relation metric, not the first learned autoformalization critic.
2. Makes FormalRx a mandatory verdict baseline only on the shared `N + candidate Lean` M4-NL task; M2/M3 use Lean–Lean baselines.
3. Adds sealed structural anti-shortcut quadrants and correct/equivalent-alternative/unrelated-reference value tests.
4. Migrates terminal relations to `equivalent`, `A_stronger`, `B_stronger`, `incomparable`, `unrelated`, and `ambiguous`; unresolved is a null relation routed to REVIEW.
5. Requires backward-compatible readers for legacy `incomparable_near_miss` and `unknown`, while new writers reject both spellings.
6. Specifies synchronous, weight-shared bidirectional matching and exactly swap-invariant/equivariant M2 outputs.
7. Replaces the ModernBERT-large default with a preregistered four-backbone pilot and no parameter ceiling.
8. Splits human gold into ancestry-disjoint `training_gold`, `selection_gold`, `calibration_gold`, and sealed `final_human_test` products.
9. Adds identifiable deterministic, SCI-conditioned, open-ended, real-output, and human-gold data arms with feasible family caps.
10. Records private `sft_classic` as internal-only with external transmission prohibited and a public-source replication profile required.
11. Separates immutable source-row identity from content hashes and makes question-first extraction with an explicitly unverified fallback route mandatory.
12. Replaces aggregate Gate-2 tolerances with per-row regression expectations, exact terminal accounting, and 100% deterministic replay.
13. Makes independent per-theorem LeanInteract requests the Gate-3 correctness primitive and fixes all representation denominators before execution.
14. Requires a binder-normalized identity fingerprint, alpha-invariance, collision, proof-leakage, and name-versus-inline audits without making graphs a blocker.
15. Keeps localization, generated repair, graph work, staffing, schedules, compensation, budgets, and hardware prescriptions outside the flagship path.
16. Records the fresh `repr_v3` Gate-3 revalidation on the unchanged frozen 5,000-mathlib plus 5,000-`sft_classic` denominator without rewriting the historical `repr_v2` decision.
17. Adds a post-Gate-4G deterministic-v2 research track with reserved narrow family scopes, evidence classes, family/mechanism holdouts, and unchanged v1/gate/promotion semantics.

---
## 0. How this document must be used

This document is the source of truth for the first implementation. A coding agent should not silently replace its core choices with alternatives.

The following rules are non-negotiable unless this plan is explicitly revised:

1. **All normal Python interaction with Lean must go through LeanInteract.** Do not build a parallel subprocess wrapper around `lake env lean`, the Lean LSP, or a custom JSON REPL client.
2. **The target is autoformalization faithfulness, not merely truth-level logical equivalence.** Two provable propositions are not automatically the same claim.
3. **Labels and evidence are separate.** A proof-search success, failed search, mutation intention, LLM vote, and human label are different fields. Never overwrite one with another.
4. **Failed proof search is never a negative semantic label.** It means only that the configured search failed.
5. **Automatically generated positive examples must be conservative, local, and auditable.** Broad simplification or arbitrary theorem proving is not sufficient evidence that two statements express the same claim.
6. **Automatically generated negative examples are provisional until verified.** A mutation intended to change meaning can accidentally preserve meaning.
7. **No variants of the same source theorem or natural-language problem may cross train, validation, calibration, and test boundaries.**
8. **External benchmarks and the final human test set remain frozen and evaluation-only.**
9. **The graph model is an extension, not a blocker.** The first complete model is text/structure based with cross-attention.
10. **Every pipeline command must be restartable, deterministic under a seed, and provenance preserving.**

A coding agent should implement one milestone at a time, satisfy its tests and acceptance gate, and avoid beginning later milestones while an earlier gate is failing.

---

## 1. Project objective

Build a calibrated learned metric that answers the following question:

> Does a candidate Lean 4 theorem statement faithfully express the same mathematical claim as the intended natural-language statement or a trusted reference formalization?

The finished system should support two related modes:

### 1.1 Lean–Lean mode

Input:

- trusted or source Lean theorem statement `A`;
- candidate Lean theorem statement `B`;
- their Lean environments or import contexts.

Output:

- probability that `A` and `B` express the same claim;
- directional relation between them;
- calibrated uncertainty and an abstention decision;
- optional symbolic evidence such as definitional equality or a checked proof attempt.

### 1.2 Natural-language–Lean mode

Input:

- natural-language theorem or problem statement `N`;
- candidate Lean theorem statement `C`;
- optional trusted reference Lean statement `R`.

Output:

- probability that `C` is faithful to `N`;
- calibrated acceptance, review, or rejection decision;
- optional reference-aware Lean–Lean score when `R` exists.

### 1.3 Required downstream demonstration

The project is not complete after reporting pair-classification metrics. It must show practical value in at least one autoformalization workflow:

1. generate several Lean statement candidates for one natural-language problem;
2. typecheck them;
3. score and rerank them with LeanFaith;
4. demonstrate an improvement in faithful top-1 selection over strong baselines;
5. demonstrate selective escalation to an expensive critic or human without requiring localization or repair generation from LeanFaith.

---

## 2. Research questions and testable hypotheses

### RQ1 — Can a learned model judge Lean theorem-statement faithfulness better than existing fixed metrics?

**Hypothesis H1:** A learned cross-attention model trained on certified conservative positives, typed near-miss negatives, real model outputs, and human labels will outperform edit-based metrics and proof-search-only metrics on a held-out human-labeled test set.

### RQ2 — Which training data actually transfers to real autoformalization failures?

**Hypothesis H2:** Deterministic transformations alone will overfit to transformation artifacts. Adding real autoformalizer outputs and model-proposed variants will materially improve performance on real-output-only evaluation.

### RQ3 — Does elaborated Lean structure add value beyond raw Lean tokens?

**Hypothesis H3:** An elaborated-signature view and compact structural features will improve hard-near-miss and out-of-domain performance over raw code alone. A full expression graph may add further value, but this is an ablation rather than an assumption.

### RQ4 — Can the score be calibrated well enough to support automated acceptance and abstention?

**Hypothesis H4:** Post-hoc calibration on a clean calibration split will produce useful high-precision acceptance thresholds and an uncertainty band that sends difficult cases to review.

### RQ5 — Does the metric improve autoformalization candidate selection?

**Hypothesis H5:** LeanFaith reranking will improve faithful@1 over first-compiling-candidate, typecheck-only, edit-based, BEq+-style, and LLM-judge baselines at lower marginal cost than repeatedly invoking frontier judges.

### RQ6 — Does the model generalize across generators and Lean projects?

**Hypothesis H6:** Training with source-project, mutation-family, and generator diversity will improve transfer to held-out autoformalizers and held-out libraries such as CSLib or Physlib.

---

## 3. Exact semantic target

### 3.1 Primary target: autoformalization claim faithfulness

The primary Lean–Lean target is whether two statements express the **same intended mathematical claim**, not merely whether both are true or mutually provable in a rich library.

Persisted resolution fields are:

```text
same_claim: true | false | null
resolution_outcome: same_claim | not_same_claim | ambiguous | unresolved
relation: equivalent | A_stronger | B_stronger |
          incomparable | unrelated | ambiguous | null
```

`null` never means false. It means either a terminal expert decision of genuine ambiguity or an unresolved process route, distinguished by `resolution_outcome`.

A same-claim positive may differ in theorem/binder names, formatting, harmless grouping, explicitly presented implicit arguments, approved notation wrappers, or another policy-listed reversible interface presentation. It may not change substantive domains, dependencies, hypotheses, quantifiers, operators, constants, bounds, casts, typeclasses, or conclusion strength.

### 3.2 F0, F1, and F2 are related but distinct

LeanFaith stores three levels:

| Level | Meaning | Role |
|---|---|---|
| **F0** | definitional/representation equivalence under a pinned environment | high-precision auxiliary evidence |
| **F1** | same intended mathematical claim | primary training/evaluation target |
| **F2** | truth-level relation such as both directional implications | auxiliary logical analysis only |

F0 may often imply F1 under an approved policy, but the resolver—not the checker alone—makes that promotion. F2 does **not** imply F1: two propositions can both be true, mutually derivable using powerful context, or collapse to tautologies while expressing different claims.

### 3.3 Directional relations

When `same_claim=false`, label the best-supported **claim-level** relation:

- `A_stronger`: after aligning corresponding binders, hypotheses, and mathematical roles, A makes a strictly stronger claim than B;
- `B_stronger`: the reverse;
- `incomparable`: materially related but neither is an accepted faithful restatement or one-way strengthening;
- `unrelated`: no substantive claim match;
- `ambiguous`: terminal expert/policy ambiguity.

`unknown` is not a semantic relation. Insufficient evidence uses `relation=null`,
`resolution_outcome=unresolved`, `quality_tier=unknown`,
`requires_adjudication=true`, and deployment decision `REVIEW`. `near_miss` is
retained only as transformation provenance or evaluation-slice metadata.

Do not define this field by mutual provability of the two closed theorem types. Closed propositions can collapse through theorem truth, vacuity, inconsistent assumptions, or ex falso. Whole-proposition directional proof search populates F2 only. A symbolic claim-strength certificate must use an explicit binder/hypothesis alignment and a policy-approved local comparison; otherwise relation is supplied by trusted annotation/resolution. Failed search is never evidence of nonimplication.

### 3.4 Natural-language–Lean faithfulness

For NL statement `N` and Lean candidate `C`, faithfulness requires that C captures the intended objects, domains, quantifiers, hypotheses, dependencies, conclusion, and expected answer without adding/removing substantive content. A reference Lean statement `R` is evidence, not infallible ground truth; suspected reference defects are explicitly labeled and adjudicated.

### 3.5 Operational review versus terminal ambiguity

Operational review route:

```text
same_claim = null
resolution_outcome = unresolved
relation = null
quality_tier = unknown
requires_adjudication = true
decision = REVIEW
```

Terminal ambiguity:

```text
same_claim = null
resolution_outcome = ambiguous
quality_tier = gold_human | benchmark
requires_adjudication = false
```

Terminal ambiguous items are masked from binary loss/metrics, evaluated in a three-class and abstention analysis, and retained in released audit data.

### 3.6 Policy edge cases that must be decided before labeling

The versioned semantic policy must include accepted/rejected examples for:

- extra unused universally quantified variables;
- redundant but mathematically meaningful hypotheses;
- vacuous implication and inconsistent assumptions;
- subtype/set/typeclass reformulations;
- coercions and domain embeddings;
- theorem-interface generalization/specialization;
- answer-only versus full theorem statements;
- simplification to reflexivity or `True`;
- notation expansion versus abstraction change;
- reference defects and genuinely ambiguous NL.

No coding agent or annotator may invent these decisions ad hoc.

---

## 4. Expected scientific contribution and novelty position

### 4.1 Closest work and mandatory comparisons

The project must be positioned against:

- **BEq** from Liu et al., *Rethinking and Improving Autoformalization* (ICLR 2025) and its Con-NF benchmark;
- **BEq+** from Poiroux et al., *Reliable Evaluation and Benchmarks for Statement Autoformalization* (EMNLP 2025) and ProofNetVerif;
- **FormalAlign** (ICLR 2025), a learned alignment/evaluation method;
- **CriticLean/CriticLeanGPT/CriticLeanBench** (arXiv:2507.06181), the closest learned NL→Lean faithfulness critic line;
- **FormalRx** (arXiv:2607.04655v1), the closest generative NL→Lean diagnostic critic: it emits verdict, SCI category, location, and correction; the paper reports verdict F1 `0.881` for joint training and `0.899` for progressive training;
- **GTED** (arXiv:2507.07399);
- **ASSESS/TransTED and EPLA** (arXiv:2509.22246);
- **Mathesis/LeanScorer/Gaokao-Formal** (arXiv:2506.07047);
- **ReForm's ConsistencyCheck**, an 859-item expert-annotated semantic-validation set;
- **The Faithfulness Gap/DriftBench** (arXiv:2606.16541);
- **COVCAL** (arXiv:2605.28365) for calibrated/risk-controlled Lean judging.

Typed generation is informed by Semantic Fusion (PLDI 2020), OpFuzz/type-aware operator mutation (OOPSLA 2020), TypeFuzz/generative type-aware mutation (OOPSLA 2021), and *Validating SMT Solvers for Correctness and Performance via Grammar-based Enumeration* (OOPSLA 2024; DOI `10.1145/3689795`). These motivate generation methodology, not same-claim labels.

FormalRx reports a random `8:1:1` item split after generating several variants
from positive seeds. Sibling variants crossing splits are therefore a plausible
risk, not a confirmed defect absent lineage identifiers. The current
`LARK-Lab/FormalRx-Test` card says diagnoses remain withheld and reports class
counts that differ from the paper; all comparisons must pin and inspect the
actual artifact. The public `LARK-Lab/FormalRx-8b` weights currently lack a
model card, so the prompt, decoding, parser, and checkpoint identity must be
reconstructed from primary artifacts and frozen before evaluation.

### 4.2 Defensible novelty claim

The project does not claim the first learned critic, taxonomy-conditioned
mutation pipeline, diagnostic model, localization method, or repair system.
Reference-aware comparison by itself is also not novel. The defensible core is:

1. a versioned benchmark/data pipeline combining conservative certified transformations, hard type-aware mutations, realistic autoformalizer outputs, multi-model weak supervision, and expert labels;
2. a lightweight non-autoregressive, calibrated Lean–Lean same-claim/relation model over raw and elaborated representations;
3. explicit evidence/label separation, F0/F1/F2 separation, and precision-controlled abstention;
4. ancestry-disjoint anti-shortcut tests over unseen generators, projects, transformations, adversarial minimal pairs, and post-cutoff novel items;
5. a quality–latency–memory–throughput Pareto evaluation and frozen reranking/selective-escalation demonstration.

### 4.3 Originality risk

The work is weak if it trains a binary classifier on LLM-produced labels and evaluates on the same synthetic family, or if reference-aware accuracy is explained by edit-distance and namespace shortcuts. It becomes publishable when data generation is independently audited, the closest learned critics receive identical inputs in direct comparisons, calibration is distribution-specific, the anti-shortcut panel succeeds, and real-output reranking improves under a sealed protocol.

---

## 5. System architecture

```text
Pinned sources/projects/providers
        │
        ▼
LeanInteract-backed extraction and validation
        │
        ├── ContextRecord / TheoremRecord
        └── RepresentationRecord (versioned multi-view)
        │
        ▼
Candidate generation
  ├── scoped conservative positives
  ├── typed provisional mutations
  ├── fresh multi-generator autoformalizations
  └── LLM controlled variants
        │
        ▼
Lean validation + deduplication + ancestry grouping
        │
        ▼
Evidence collection
  ├── typecheck / defeq
  ├── directional proof attempts
  ├── bounded counterexample certificates
  ├── transformation audits
  ├── blinded LLM judgments
  └── human annotations/adjudication
        │
        ▼
Resolved labels and quality tiers
        │
        ▼
Connected-component split freeze
        │
        ▼
Baselines and models
  ├── symbolic / edit / structural / learned critics
  ├── M0 dual encoder
  ├── M1 pair cross-encoder
  ├── M2 bidirectional cross-attention
  ├── M3 symbolic hybrid
  ├── M4 NL–Lean model
  └── M5 optional Expr-graph extension
        │
        ▼
Calibration, selective decisions, sealed evaluation
        │
        ▼
Candidate reranking and selective escalation
```

### 5.1 Artifact-boundary rules

- Every arrow is an immutable artifact boundary.
- Raw provider/Lean responses are preserved before parsing.
- A downstream stage references upstream IDs and never rewrites upstream records.
- Evidence records do not mutate labels; the resolver creates a new label artifact.
- Smoke artifacts carry `artifact_class=smoke` and are barred from releases/model selection.
- Test manifests are mounted read-only and never exposed to training, prompt development, or active learning.

---

## 6. Mandatory technology choices

### 6.1 Python and engineering stack

- Reference Python: `3.12.x`; project constraint `>=3.12,<3.13`.
- Dependency manager/lock: `uv` and `uv.lock`.
- Schemas: Pydantic v2, `extra="forbid"`.
- CLI: Typer.
- ML: PyTorch and Hugging Face Transformers.
- Tables: PyArrow/Parquet; compressed JSONL for streaming/raw interchange.
- Quality: Pytest, Ruff, pre-commit, mypy. Mypy is strict on core schemas, Lean boundary, transformations, labeling, splits, and model interfaces.
- Tracking: Weights & Biases by default, with offline/export mode.
- Data versioning: content-hash manifests through pilot; add DVC at `research_v1` without replacing manifests.
- Secrets (including `HF_TOKEN` for private Hugging Face sources) live in the environment or a secret manager, referenced by name from configs; `.env.example` documents required names without values.

### 6.2 Lean/mathlib lock constrained by LeanInteract

Pin `lean-interact==0.11.4` (MIT; package requirement Python ≥3.10, while this project standardizes on Python 3.12). It advertises support for Lean `v4.8.0-rc1` through `v4.31.0-rc1`; this is a binding compatibility constraint. As of this revision, stable Lean is `v4.31.0`, while mathlib development has moved to a `v4.32.0-rc1` toolchain, which is outside the advertised range.

Phase 0 must choose exactly one coherent toolchain mode:

1. **advertised-range mode (default):** pin an explicitly supported Lean release, preferably `v4.31.0-rc1`, and an exact mathlib commit/tag whose checked-in `lean-toolchain` matches it; or
2. **stable exception mode:** pin Lean `v4.31.0` and a matching exact mathlib commit/tag only after the complete compatibility probe passes and ADR-0001 records that stable `v4.31.0` is outside LeanInteract 0.11.4's advertised maximum.

Do not silently override a project's checked-in `lean-toolchain`, do not mix stable `v4.31.0` with an RC-pinned mathlib environment without an explicit tested migration, and never use mathlib master while it requires `v4.32.0-rc1`. Pin exact revisions for every external Lean project and never use a floating branch in a research run.

### 6.3 Mandatory LeanInteract boundary

All production Python→Lean interaction uses LeanInteract. It wraps the `augustepoiroux/repl` fork. Do not build a parallel `lake env lean`, LSP, or raw JSON REPL backend.

Required LeanInteract capabilities:

- `Command` and `FileCommand`;
- declarations and root goals;
- explicit `allow_sorry` on every validation call;
- optional InfoTree escalation;
- `LeanServerPool` or one server per worker;
- tested `LeanServer` path and optional experimental `AutoLeanServer` mode;
- project abstractions such as `LocalProject`.

A direct shell command is allowed only in a quarantined doctor/CI diagnostic and may not create semantic records or labels.

### 6.4 Encoder/tokenizer decision

No backbone is the scientific default and no hard parameter ceiling is imposed.
Freeze exact revisions of `answerdotai/ModernBERT-base`,
`answerdotai/ModernBERT-large`, the encoder branch of
`Salesforce/codet5p-220m`, and `microsoft/deberta-v3-large`; select among
eligible candidates by the preregistered §21.2 protocol. ModernBERT-base is a
smoke fallback only. “Lightweight” means non-autoregressive routine inference
and a measured quality–latency–memory–throughput Pareto position, not a
parameter threshold.

### 6.5 Annotation tooling

Use Argilla or Label Studio with a Lean-pair template. A thin Streamlit fallback is permitted only after a bounded integration spike documents that both existing tools fail required blinding/schema/export behavior.

### 6.6 Deliberately excluded planning content

This plan contains no staffing assignments, calendar estimates, annotator compensation, dollar caps, or hardware-envelope mandates. Runtime, memory, tokens, API calls, and provider-reported costs are measured outcomes rather than resource prescriptions.

---
## 7. Repository layout — single path authority

This tree is authoritative. A phase or backlog item may reference only a declared path or a child of a declared directory. Adding an alias requires updating this section and the path-consistency test first.

```text
leanfaith/
  PLAN.md
  README.md
  LICENSE
  CITATION.cff
  DATA_SOURCES.md
  pyproject.toml
  uv.lock
  lean-toolchain
  lakefile.toml
  lake-manifest.json
  .env.example
  .gitignore
  .pre-commit-config.yaml
  dvc.yaml                         # research_v1 onward
  dvc.lock                         # research_v1 onward

  policies/
    semantic_policy_v1.md
    error_ontology_v1.yaml
    label_resolution_v1.yaml
    transformation_promotion_v1.yaml
    benchmark_denylist_v1.yaml
    evidence_policy_v1.yaml
    split_policy_v1.yaml
    calibration_policy_v1.yaml
    preregistration_v1.yaml
    private_source_use.yaml
    model_selection.yaml
    formalrx_comparison.yaml
    formalrx_sci_crosswalk_v1.yaml

  examples/
    semantic_contract_v1.jsonl
    backend_requests/
    transformation_cases/
    annotation_traps/

  prompts/
    proposers/
    autoformalizers/
    judges/
    schemas/

  annotation/
    guidelines_v1.md
    codebook_v1.yaml
    templates/
    assignments/
    exports/
    adjudication/

  configs/
    environment.lock.yaml
    projects/
      fixtures.yaml
      mathlib.yaml
      cslib.yaml
      physlib.yaml
    sources/
      sft_classic.yaml
      sft_classic_numina.yaml
      lean_workbook.yaml
      mathlib.yaml
      proofnetverif.yaml
      cslib.yaml
      physlib.yaml
      public_replication.yaml
    generation/
      providers.yaml
      problem_pool_v1.yaml
      real_outputs_v1.yaml
      llm_variants_v1.yaml
    judges/
      weak_supervision.yaml
      primary_eval.yaml
    transformations/
      registry.yaml
      v1.yaml
      v2.yaml
      replacement_table_v1.yaml
      lf018_pre_scale_v1.yaml
      lf019_positive_fixtures_v1.yaml
      lf019_smoke_v1.yaml
      p01_alpha.yaml
      p02_binders.yaml
      p04_notation_lite.yaml
      n01_operator.yaml
      n02_quantifier.yaml
      n03_drop_hypothesis.yaml
      n07_literal_bound.yaml
      n10_nearby_theorem.yaml
      v2/
        p05_resolved_names.yaml
        p06_implicit_arguments.yaml
        p07_coercion_surface.yaml
        p08_type_ascription.yaml
        p09_projection_direct.yaml
        p10_constructor_direct.yaml
        p11_bounded_quantifier.yaml
        p12_proof_arrow_binder.yaml
        p13_restricted_eta.yaml
        p14_binder_permutation.yaml
        p15_root_iff_reversal.yaml
        p16_conjunction_reassociation.yaml
        p17_hypothesis_packing.yaml
        n11_bound_variable.yaml
        n12_implication_converse.yaml
        n13_witness_dependency.yaml
        n14_negation_scope.yaml
        n15_conjunct_omission.yaml
        n16_domain_guard.yaml
        n17_role_arguments.yaml
    evidence/
      portfolio_v1.yaml
      counterexample_v1.yaml
      sampling_v1.yaml
    annotation/
      tool.yaml
      pilot.yaml
      main.yaml
    splits/
      v0.yaml
    benchmarks/
      registry.yaml
      formalrx_lineages.yaml
    baselines/
      lexical.yaml
      beq.yaml
      beq_plus.yaml
      gted.yaml
      transted.yaml
      formalalign.yaml
      criticlean.yaml
      leanscorer.yaml
      covcal.yaml
      llm_judges.yaml
      formalrx.yaml
    models/
      backbone_registry.yaml
      backbone_pilot.yaml
      training_data_readiness_v1.yaml
      m0.yaml
      m1.yaml
      m2.yaml
      m3.yaml
      m4.yaml
      m5.yaml
    data/
      training_arms.yaml
    evaluation/
      primary.yaml
      external.yaml
      reranking.yaml

  LeanFaith/
    Main.lean
    Meta/
      Extract.lean
      Canonicalize.lean
      DefEq.lean
      ExprJson.lean
      Fingerprint.lean
      SemanticAtoms.lean
      ProofChecks.lean
      Counterexamples.lean
      AxiomAudit.lean
    Fixtures/
      Basic.lean
      Contexts.lean
      Invalid.lean

  src/leanfaith/
    __init__.py
    py.typed
    cli/
      __init__.py
      app.py
      doctor.py
      pipeline.py
      probe.py
      extract.py
      represent.py
      freeze_benchmarks.py
      generate_deterministic.py
      collect_real_outputs.py
      generate_llm_variants.py
      collect_evidence.py
      export_annotation.py
      import_annotation.py
      resolve_labels.py
      build_splits.py
      freeze_release.py
      run_baselines.py
      train.py
      calibrate.py
      evaluate.py
      rerank.py
    config/
      loading.py
      models.py
      paths.py
      logging.py
      hashing.py
    schemas/
      __init__.py
      enums.py
      ids.py
      source.py
      theorem.py
      variant.py
      pair.py
      evidence.py
      label.py
      nl_lean.py
      llm.py
      annotation.py
      manifest.py
      prediction.py
      migrations.py
    lean/
      __init__.py
      protocol.py
      leaninteract_backend.py
      project_registry.py
      response_normalization.py
      commands.py
      session_policy.py
      extraction.py
      extraction_regression.py
      typecheck.py
      proof_search.py
      counterexample.py
      axiom_audit.py
      cache.py
    sources/
      __init__.py
      base.py
      probe.py
      hf_sft_classic.py
      hf_sft_classic_numina.py
      lean_workbook.py
      proofnetverif.py
      repository.py
      mathlib.py
      cslib.py
      physlib.py
      llm_candidates.py
      gate2_sampling.py
    representations/
      __init__.py
      pipeline.py
      raw.py
      headless.py
      pretty.py
      explicit.py
      structural.py
      semantic_atoms.py
      operator_tree.py
      fingerprints.py
      audit.py
    transforms/
      __init__.py
      protocol.py
      registry.py
      promotion.py
      invariants.py
      diff.py
      positive/
      negative/
    generation/
      __init__.py
      providers.py
      problem_pool.py
      parser.py
      autoformalization.py
      variants.py
      retries.py
      token_accounting.py
    evidence/
      __init__.py
      sampling.py
      pipeline.py
      defeq.py
      proof_search.py
      counterexample.py
      certificates.py
    labeling/
      __init__.py
      llm_judges.py
      aggregation.py
      resolution.py
      quality.py
      conflicts.py
    annotation_support/
      __init__.py
      export.py
      import_.py
      blinding.py
      adjudication.py
      agreement.py
    datasets/
      __init__.py
      build.py
      ancestry.py
      connected_components.py
      deduplicate.py
      denylist.py
      splits.py
      sampling.py
      freeze.py
      cards.py
    generation/
      __init__.py
      config.py
      providers.py
      prompts.py
      problem_pool.py
      real_outputs.py
    baselines/
      __init__.py
      formalrx.py
    models/
      relation_head.py
      selection.py
      data_readiness.py
      m0_dual_encoder.py
      m1_cross_encoder.py
      m2_bidirectional_cross_attention.py
      m3_hybrid.py
      m4_nl_lean.py
      m5_graph.py
      heads.py
      losses.py
      calibration.py
      train.py
      inference.py
    evaluation/
    applications/

  scripts/
    00_doctor.py
    01_probe_sources.py
    02_extract_statements.py
    03_build_representations.py
    04_freeze_benchmark_denylist.py
    05_generate_deterministic.py
    06_collect_real_outputs.py
    07_generate_llm_variants.py
    08_collect_symbolic_evidence.py
    09_export_annotation.py
    10_import_annotation.py
    11_resolve_labels.py
    12_build_splits.py
    13_freeze_dataset.py
    14_run_baselines.py
    15_train.py
    16_calibrate.py
    17_evaluate.py
    18_rerank.py
    19_release_smoke.py

  tests/
    unit/
    integration/leaninteract/
    integration/sources/
    integration/transformations/
    property/
    golden/
    end_to_end/
    lean_fixtures/
    fixtures/
      gates/
        sft_classic_100_expected_v1.json

  data/
    source_manifests/
    benchmarks/
      frozen_ids.json
      frozen_ids.representations_v1.json
      source_registry.yaml
      manifests/
        representation_signatures_v1.json
    raw/
      sources/
      real_outputs/
      judgments/
    parsed/
      sources/
      real_outputs/
    extracted/
      theorems/
      failures/
    representations/
    generated/
      deterministic/
      llm/
    real_outputs/
      validated/
    evidence/
    labels/
      provisional/
      silver/
      resolved/
    human/
      pilot_raw/
      pilot_adjudicated/
      training_gold/
      selection_gold/
      calibration_gold/
      final_human_test/
    split_manifests/
      train.json
      validation.json
      calibration.json
      internal_test.json
      human_test.json
      benchmark_test.json
      real_output_test.json
      heldout_transform_test.json
      heldout_project_test.json
      heldout_generator_test.json
      adversarial_test.json
    releases/
      v0/
        train.parquet
        validation.parquet
        calibration.parquet
        internal_test.parquet
        manifests/
        DATA_CARD.md
      research_v1/

  artifacts/
    compatibility/
    golden/leaninteract/
    raw_lean_responses/
    evidence/
    predictions/
      baselines/
      final/
      reranking/
    checkpoints/
      m0/
      m1/
      m2/
      m3/
      m4/
      m5/
    calibration/
    release/
    figures/
    tables/

  reports/
    gates/
    milestones/
      phase_0_contract.md
      phase_1_leaninteract.md
      phase_2_extraction.md
      phase_3_representations.md
      phase_3_repr_v3_revalidation.md
      phase_4_transforms.md
      phase_5_real_outputs.md
      phase_6_llm_data.md
      phase_7_human_pilot.md
      phase_7b_main_annotation.md
      phase_8_dataset_v0.md
      phase_9_baselines.md
      phase_10_lean_lean_models.md
      phase_11_final_evaluation.md
      phase_12_nl_lean_reranking.md
      phase_13_graph.md
      phase_14_release.md
    compatibility/
      leaninteract_api.json
    source_probes/
    transformation_audits/
    label_audits/
    decisions/
    representation_collisions_mvp.md
    generation_coverage.md
    faithful_prevalence_design.md
    judge_calibration.md
    human_pilot.md
    annotation_main.md
    contamination_v0.md
    baselines.md
    tokenizer_audit.md
    model_selection.md
    evaluation_final.md
    external_benchmarks.md
    statistics_primary.md
    reranking.md
    graph_extension.md
    release_validation.md

  docs/
    adr/
      ADR-0001-environment-lock.md
      ADR-0002-annotation-platform.md
      ADR-0003-data-versioning.md
      ADR-0004-encoder-tokenizer.md
    schemas/
    leaninteract.md
    operations.md
    reproducibility.md
    limitations.md

  runs/
    .gitkeep
```

### 7.1 Canonical schema homes

| Record | Definition module |
|---|---|
| `ContextRecord`, `TheoremRecord`, `RepresentationRecord` | `src/leanfaith/schemas/theorem.py` |
| `VariantRecord`, transformation support records | `src/leanfaith/schemas/variant.py` |
| `PairRecord` | `src/leanfaith/schemas/pair.py` |
| `EvidenceRecord` and kind-specific values | `src/leanfaith/schemas/evidence.py` |
| `ResolvedLabel` | `src/leanfaith/schemas/label.py` |
| `ProblemPoolRecord`, `NLPLeanRecord` | `src/leanfaith/schemas/nl_lean.py` |
| `LLMCallRecord`, `LLMAttemptRecord` | `src/leanfaith/schemas/llm.py` |
| shared enums | `src/leanfaith/schemas/enums.py` |

Definitions are never duplicated; `schemas/__init__.py` may re-export them.

### 7.2 Phase-to-command map

| Phase | Typer command | Script |
|---|---|---|
| 0–1 | `leanfaith doctor` | `00_doctor.py` |
| 2 | `probe`, `extract`, `freeze-benchmarks` | `01`, `02`, `04` |
| 2 gate audit | `sample-gate2`, `sample-gate2-arrow`, `audit-extraction-regression`, `audit-extraction-replay`, `audit-gate2-scale`, `freeze-gate3-inputs` | stable `cli/pipeline.py` commands |
| 3 | `represent`, `audit-representations`, `audit-representation-replay`, `audit-alpha-invariance`, `audit-representation-cross-path` | `03_build_representations.py` plus stable audit commands |
| 4 | `generate-deterministic`, `close-gate4g` | `05_generate_deterministic.py` plus the fail-closed Gate-4G finalizer |
| 5 | `collect-real-outputs` | `06_collect_real_outputs.py` |
| 6 | `generate-llm-variants` | `07_generate_llm_variants.py` |
| 4–6 | `collect-evidence` | `08_collect_symbolic_evidence.py` |
| 7/7b | `export-annotation`, `import-annotation` | `09`, `10` |
| 8 | `resolve-labels`, `build-splits`, `freeze-release` | `11`, `12`, `13` |
| 9 | `run-baselines` | `14_run_baselines.py` |
| 10/12/13 | `train` | `15_train.py` |
| 10–12 | `calibrate`, `evaluate`, `rerank` | `16`, `17`, `18` |
| 14 | `release-smoke` | `19_release_smoke.py` |

A CI test extracts phase deliverable paths from §24 and verifies that each is declared above or is a child of a declared directory.

---
## 8. LeanInteract integration specification

### 8.1 Mandatory boundary and REPL identity

All production Python interaction with Lean uses [LeanInteract](https://github.com/augustepoiroux/LeanInteract), which uses its maintained `augustepoiroux/repl` fork. Code must not assume the upstream REPL API/wire format is interchangeable.

Only `src/leanfaith/lean/leaninteract_backend.py` imports LeanInteract directly. Higher layers use the protocol in Appendix A.5.

### 8.2 Binding version policy

```toml
[project]
requires-python = ">=3.12,<3.13"
dependencies = ["lean-interact==0.11.4"]
```

The package advertises Lean `v4.8.0-rc1` through `v4.31.0-rc1`. The doctor rejects project toolchains outside the verified range unless ADR-0001 records and Gate 1 tests a stable-`v4.31.0` exception. An upgrade requires a dedicated lock change, API-shape diff, golden-response diff, 1,000-record extraction/evidence comparison, and explicit cache/schema migration decision.

### 8.3 Project abstractions

| Situation | LeanInteract abstraction | Policy |
|---|---|---|
| checked-out pinned project | `LocalProject` | exact path/revision recorded |
| pinned external repository | `GitProject` when supported by pinned API | exact commit/tag |
| isolated dependency project | `TempRequireProject` | exact Lean/dependency revisions |
| generated Lake project | `TemporaryProject` | only when prior forms cannot express context |

Project setup/build failure aborts the batch as `SETUP_ERROR`; it does not emit semantic labels.

### 8.4 Canonical backend protocol mirror

**Appendix A.5 is the source of truth.** This section mirrors the contract for readability; LF-005 tests that both copies remain byte-equivalent at the code-block level.

```python
class LeanStatus(StrEnum):
    VALID = "valid"
    VALID_WITH_SORRY = "valid_with_sorry"
    INVALID = "invalid"
    TIMEOUT = "timeout"
    CRASH = "crash"
    SETUP_ERROR = "setup_error"
    UNSUPPORTED = "unsupported"
    INTERNAL_ERROR = "internal_error"

@dataclass(frozen=True, slots=True)
class LeanRequest:
    request_id: str
    context_id: str
    code: str | None = None
    file_path: Path | None = None
    declarations: bool = False
    root_goals: bool = False
    infotree: Literal["none", "substantive", "full"] = "none"
    allow_sorry: bool = False
    timeout_seconds: float = 30.0
    metadata: Mapping[str, str] = field(default_factory=dict)

@dataclass(frozen=True, slots=True)
class LeanResult:
    request_id: str
    request_hash: str
    context_id: str
    context_fingerprint: str
    status: LeanStatus
    messages: tuple[dict, ...] = ()
    sorries: tuple[dict, ...] = ()
    declarations: tuple[dict, ...] = ()
    root_goals: tuple[str, ...] = ()
    infotree: tuple[dict, ...] = ()
    elapsed_ms: int = 0
    raw_response_path: str | None = None
    infrastructure_error: str | None = None

class LeanBackend(Protocol):
    def run(self, request: LeanRequest) -> LeanResult: ...
    def run_batch(self, requests: Sequence[LeanRequest]) -> list[LeanResult]: ...
    def close(self) -> None: ...
```

Invariants:

- exactly one of `code`/`file_path` is present;
- one terminal result per request, in input order;
- request hash includes payload, context, timeout, `allow_sorry`, InfoTree level, method version, and `environment_schema_version`;
- raw response is saved before normalization;
- infrastructure states never become semantic labels.

### 8.5 Pinned API caveats

Phase 1 verifies every symbol before implementation. Expected 0.11.4 behavior:

- `CommandResponse`, `InfoTreeOptions`, `DeclarationInfo`, and `LeanError` are imported from `lean_interact.interface`, not assumed top-level exports;
- `lean_code_is_valid` defaults to `allow_sorry=True`, so every LeanFaith call passes it explicitly;
- `LeanServerPool.run_batch` can return a response, `LeanError`, or Python exception per item;
- `AutoLeanServer` is experimental and requires a tested `LeanServer` fallback;
- `memory_hard_limit_mb` is Linux-only and applies per REPL process.

### 8.6 Canonical status mapping

| Observation | Status |
|---|---|
| accepted with strict no-admission policy | `VALID` |
| statement valid only with explicit placeholder | `VALID_WITH_SORRY` |
| Lean diagnostic rejects code | `INVALID` |
| configured timeout | `TIMEOUT` |
| process/recovery failure | `CRASH` |
| project/build/import failure | `SETUP_ERROR` |
| unsupported toolchain/feature | `UNSUPPORTED` |
| unclassified adapter failure | `INTERNAL_ERROR` |

`statement_valid_with_placeholder` maps to `VALID_WITH_SORRY`. The adapter preserves diagnostics and exception type/message/trace digest.

### 8.7 Server and parallelization policy

- Use `LeanServerPool` for independent batches after pool tests pass.
- Use one `LeanServer`/approved `AutoLeanServer` per worker for context-grouped incremental workloads.
- Group by compatible context/prefix; never reuse state across incompatible contexts.
- Bound retries; a crash/timeout creates a terminal result and retry lineage.
- Doctor reports workers × configured per-process memory limit versus detected RAM when available; this is a safety check, not a hardware mandate.
- One-worker and multiworker runs must yield identical semantic IDs/statuses after normalization.

### 8.8 Statement validation and certificate validation

Statement extraction submits the exact header plus proof-stripped declaration with a placeholder and `allow_sorry=True`; success is `VALID_WITH_SORRY`. A certificate uses `allow_sorry=False`, rejects all admissions/unresolved metavariables, and runs dependency/axiom checks required by §16.

Never infer a negative semantic label from `INVALID`, timeout, crash, unsupported evidence, or failed proof search.

### 8.9 Declaration extraction

Repository files use `FileCommand(..., declarations=True)` through the backend. Dataset snippets use `Command(..., declarations=True)`. Save the complete raw response, then normalize returned declaration metadata/source ranges. Regex can locate candidates but never defines repository theorem boundaries.

### 8.10 InfoTree escalation

Default: `infotree="none"`. `substantive` and `full` are sanctioned only for representation derivation, structural evidence, bounded debugging, or a pre-registered experiment. The exact `InfoTreeOptions` construction is verified against 0.11.4 first.

### 8.11 Context identity

```text
context_fingerprint = SHA256(canonical context payload)
context_id = "ctx:" + context_fingerprint
```

The payload includes `environment_schema_version`, Lean/LeanInteract/REPL/project revisions, imports, namespace/open/scoped context, relevant options/notation, and normalized header. These values enter every backend cache key.

### 8.12 LeanInteract integration tests

Gate 1 requires:

1. import/signature introspection for every Appendix A symbol;
2. supported/unsupported toolchain checks;
3. valid, placeholder-valid, invalid, timeout, forced-crash, setup-error, and injected-internal-error normalization;
4. per-item batch exception/order preservation;
5. raw response persistence and cache-key determinism;
6. `Command` and `FileCommand` declaration extraction golden tests;
7. explicit `allow_sorry` behavior;
8. InfoTree none/substantive/full smoke where supported;
9. one-worker/multiworker equivalence;
10. experimental auto-server recovery and stable server fallback;
11. memory-product warning/platform handling;
12. no LeanInteract import outside the Lean boundary.

---

## 9. Data sources and source manifests

### 9.1 Canonical source registry

| Source | Canonical identity | Role/constraint |
|---|---|---|
| mathlib | `leanprover-community/mathlib4`, exact revision | main theorem inventory; toolchain constrained by §6.2 |
| private internal source | `formalmathatepfl/sft_classic` at `0bf9f424309f668c2c2dd214aef6ec5d1d5c042f` | verified private HF access; train 2,006,425/test 1,029,845; license undeclared; internal research only, nonredistributable, no external-provider transmission, not release eligible |
| public fallback 1 | `formalmathatepfl/sft_classic_numina` | current public sibling, 99,774-row scale; fields include `uuid`, `question`, `answer`, `lean_code`; verify pinned schema |
| public fallback 2 | `internlm/Lean-Workbook` | about 57k machine-generated/synthetic NL–Lean pairs; weak supervision/problem pool only |
| public fallback 3 | permitted `PAug/ProofNetVerif` train partition | only if manifest explicitly designates trainable rows |
| ProofNetVerif evaluation | `PAug/ProofNetVerif` | 3,752 rows; columns `id`, `nl_statement`, `lean4_src_header`, `lean4_formalization`, `lean4_prediction`, `correct`; frozen benchmark |
| CSLib | `https://github.com/leanprover/cslib`; root `Cslib`; `Cslib/**/*.lean` | small CS library; probe early, adapter at OOD phase; report absolute counts |
| Physlib | `https://github.com/leanprover-community/physlib`; root `Physlib` | physics OOD source; formerly PhysLean (itself formerly HepLean) and consolidated with Lean-QuantumInfo under the Physlib repository in 2026; pin the exact revision/toolchain |
| ReForm | `GuoxinChen/ReForm-32B`; fallback `GuoxinChen/ReForm-8B` | Apache-2.0, Qwen3-based specialized generators (arXiv:2510.24592); trained on Lean Workbook, so that overlap must be tagged |

Fixed NL-source fallback order (contingency only, since authenticated `sft_classic` access is expected to succeed):

```text
accessible sft_classic variant
→ sft_classic_numina
→ Lean-Workbook
→ permitted ProofNetVerif train partition
```

### 9.2 Gate 0 primary-source lock

The authenticated `sft_classic` probe is complete. Its project HF token remains
environment/secret-manager only and is never stored. Archive and keep canonical:

```text
resolved ID and immutable revision
access status and license/terms
split names/counts and full schema
a raw 100-row sample plus SHA256
adapter mapping/version
phase5_pool_candidate_count by source/domain/trust/dedup reason
```

Gate 0 fails without this artifact. Do not guess `sft_classic` fields from ProofNetVerif.

**Private-data boundary:** the current decision is fail-closed, not pending
approval. `sft_classic` content, including NL statements, may not be sent to any
external provider. Record `access_basis`, `institutional_policy_status`,
`license_status=undeclared`, `redistribution=false`,
`external_transmission=false`, and `release_eligibility=false`. Provider slots
remain disabled until the later Phase-5 ADR, which cannot relax this source
boundary without an explicit superseding authorization. Maintain an executable
public-source replication profile independent of `sft_classic`.

### 9.3 Verified/fallback mappings

ProofNetVerif:

```yaml
problem_id: id
nl_statement: nl_statement
lean_header: lean4_src_header
reference_lean: lean4_formalization
candidate_lean: lean4_prediction
source_label: correct
```

`sft_classic_numina`, rechecked at pinned revision:

```yaml
problem_id: uuid
nl_statement: question
solution_text: answer
reference_or_candidate_lean: lean_code
```

The adapter records whether `lean_code` is trusted, generated, or unknown; it does not infer trust from the column name.

### 9.4 Trusted NL problem pool

The Phase-5 problem-pool frame is the union of accessible rows from the selected `sft_classic`-family primary source that contain usable human-origin NL and Lean Workbook problems, followed by removal of benchmark-denylist IDs and exact/near duplicates. If the selected primary source is itself synthetic or its NL provenance is unknown, it is retained only under the corresponding trust tag rather than being silently upgraded.

A problem is eligible when source/revision/terms are fixed, the NL statement is nonempty and separated from solution text, its group ID is stable, and it is outside the denylist/near-duplicate registry. Store `nl_trust=trusted|synthetic|uncertain`; “trusted NL problem” means human-origin NL with verified provenance and no detected benchmark contamination. Lean Workbook problems are always marked synthetic and may contribute diversity/weak supervision but never satisfy a trusted-human-NL quota. The source probe reports `phase5_pool_candidate_count` before and after every eligibility/deduplication filter, broken down by source, domain, and trust.

### 9.5 Source manifest

Each source writes `data/source_manifests/<source>.json` containing source kind/ID/revision/retrieval date/license, adapter/schema versions, columns/splits/counts, sample/raw hashes, project toolchain where relevant, eligible Phase-5 count, external-API approval status where applicable, and notes. Raw partitions are append-only; adapter fixes create new parsed partitions.

### 9.6 MVP versus OOD source scope

Full MVP adapters: mathlib, the selected primary NL source, and ProofNetVerif. CSLib/Physlib receive exact revision/toolchain/root-module probes in Phase 2; their full adapters, extraction, labeling, and the `heldout_project_test` manifest are built during Phase 11 preparation on the strong-paper track (backlog LF-030), before test unsealing. Small-project percentages are always accompanied by item and ancestry-group counts.

---

## 10. Data lifecycle and immutable stages

```text
RAW → PARSED → ELABORATED → REPRESENTED → GENERATED → VALIDATED
    → EVIDENCE_COLLECTED → LABELED → SPLIT → FROZEN
```

Rules:

1. each stage reads immutable prior manifests and writes a new partition;
2. raw source/provider/Lean responses are saved before parsing;
3. every partition has input/config/code hashes, schema version, row counts, and checksums;
4. commands are idempotent and resumable by deterministic shard ID;
5. failures remain in explicit failure partitions;
6. same inputs/config either reproduce exact IDs/hashes or fail closed;
7. frozen evaluation data are read-only and unavailable to training/prompt-selection jobs;
8. collection may start early only under quarantine rules in §24.

Recommended key:

```text
{stage}/{source}/{source_revision}/{config_hash}/{shard_id}.{jsonl.zst|parquet}
```

---

## 11. Persistent schemas — canonical contracts

Pydantic v2 models use `extra="forbid"`, explicit schema versions, UTC timestamps, stable canonical-JSON IDs, and artifact hashes. Definitions live only in §7.1 modules.

### 11.1 Canonical enums

```python
class ResolutionOutcome(StrEnum):
    SAME_CLAIM = "same_claim"
    NOT_SAME_CLAIM = "not_same_claim"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"

class RelationLabel(StrEnum):
    EQUIVALENT = "equivalent"
    A_STRONGER = "A_stronger"
    B_STRONGER = "B_stronger"
    INCOMPARABLE = "incomparable"
    UNRELATED = "unrelated"
    AMBIGUOUS = "ambiguous"

class IntendedRelation(StrEnum):
    EQUIVALENT = "equivalent"
    A_STRONGER = "A_stronger"
    B_STRONGER = "B_stronger"
    NEAR_MISS = "near_miss"
    UNRELATED = "unrelated"
    UNKNOWN = "unknown"

class QualityTier(StrEnum):
    GOLD_HUMAN = "gold_human"
    GOLD_CONSERVATIVE_TRANSFORM = "gold_conservative_transform"
    GOLD_COUNTEREXAMPLE = "gold_counterexample"
    BENCHMARK = "benchmark"
    SILVER_CONSENSUS = "silver_consensus"
    PROVISIONAL = "provisional"
    UNKNOWN = "unknown"

class ValidationStatus(StrEnum):
    UNVALIDATED = "unvalidated"
    ELABORATES = "elaborates"
    ELABORATES_WITH_PLACEHOLDER = "elaborates_with_placeholder"
    INVALID = "invalid"
    TIMEOUT = "timeout"
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    QUARANTINED = "quarantined"

class TransformationFamilyStatus(StrEnum):
    GOLD_PROMOTED = "gold_promoted"
    SILVER = "silver"
    EXPERIMENTAL = "experimental"
    DISABLED = "disabled"

class EvidenceExecutionStatus(StrEnum):
    SUCCESS = "success"
    TIMEOUT = "timeout"
    ERROR = "error"
    UNSUPPORTED = "unsupported"
    ABSTAIN = "abstain"
    NOT_RUN = "not_run"

class SemanticLabelTargetKind(StrEnum):
    LEAN_PAIR = "lean_pair"
    NL_LEAN = "nl_lean"

class EvidenceTargetKind(StrEnum):
    THEOREM = "theorem"
    LEAN_PAIR = "lean_pair"
    NL_LEAN = "nl_lean"
    TRANSFORMATION_DRAFT = "transformation_draft"
    TRANSFORMATION_FAMILY = "transformation_family"
```

`near_miss` and intended `unknown` are generation/provenance values only.
`RelationLabel` has no `unknown`: unresolved labels persist `relation=null`.
Evidence statuses and evidence values may still use `unknown` where their own
contract permits it.

#### 11.1.1 Revision-4.1 relation migration

Schema version 2 writers emit only the six terminal `RelationLabel` values.
Readers accept schema-version-1 records through an explicit migration layer:

| Legacy value | Version-2 value | Additional migration state |
|---|---|---|
| `incomparable_near_miss` | `incomparable` | append `near_miss` to relation provenance/slice metadata |
| `unknown` | `null` | set `resolution_outcome=unresolved`, `same_claim=null`, `quality_tier=unknown`, `requires_adjudication=true` |

Migration is idempotent and preserves the original schema version and legacy
value in migration metadata. New-record validation rejects both legacy
spellings. Round-trip tests cover all mappings.

### 11.2 `ContextRecord`

Required fields:

```text
schema_version; environment_schema_version; context_id; context_fingerprint;
project kind/URI/revision/registry key; Lean/LeanInteract/REPL revisions;
ordered imports; namespace/open/scoped context; relevant options/notation;
normalized header text/hash
```

`context_id = "ctx:" + context_fingerprint`. Current writers expose no competing context hash field.

### 11.3 `TheoremRecord`

```text
schema_version; theorem_id; ancestry_id; root_ancestry_ids;
parent_theorem_ids; source identity/revision/split/record/file/range;
source_record_id; upstream UUID; raw-row/question/Lean-code hashes;
context_id; declaration kind/name/full name/ordinal; raw-declaration artifact pointer/hash;
proof-stripped declaration; non-model inline-elaboration source; declaration-info artifact;
Lean result ID; proposition-valued flag;
elaboration status/diagnostics; statement content hash;
NL source link; reference trust; metadata
```

It contains source/declaration identity only; normalized views live in `RepresentationRecord`. The immutable raw-source artifact is access-controlled for audit and extraction replay, but proof bodies are never admitted as representation fields or model inputs.
`inline_elaboration_source` may preserve commands preceding a dataset theorem so
Lean can recreate its local command context. It is execution-only, may contain
earlier proof text, and is categorically excluded from every representation
view, tokenizer input, and model artifact. `proof_stripped_declaration` remains
the sole source for `raw_proof_stripped` and `headless`.

### 11.4 `RepresentationRecord`

Key: `(theorem_id, normalization_version)`. Fields use §13.2 names verbatim:

```text
representation_id; theorem_id; normalization_version; context_id;
raw_proof_stripped; headless; signature_pp; signature_explicit;
alpha_structural; notation_light; semantic_atoms; operator_tree;
view_status per field; option_profile; content_hash; created_at
```

Optional failed views are null with explicit status; successful views are never overwritten.

### 11.5 `VariantRecord`

```text
variant_id; source_theorem_ids; generator_kind/id; config hash/seed;
prompt/raw-output artifacts; extracted statement;
intended_relation; intended_error_types; candidate_pool;
formalrx_sci_requested/validated/validation_status/proposer_family/validator_family;
transformation/inverse traces; validation_status; validation evidence ID;
derived theorem ID; quality_tier=provisional until resolution;
polarity_metadata=positive|negative|mixed|unknown; metadata
```

`validation_status` is execution state, not semantic truth. Final supervised quality comes from `ResolvedLabel`.

### 11.6 `PairRecord`

```text
pair_id; A/B theorem IDs; pair source; NL problem group;
split_group_ids: list[str]; generator/transformation family;
intended_relation; resolved_label_id; evidence_ids;
lexical/structural stats; split eligibility; metadata
```

`split_group_ids` is the sorted union of both sides' root ancestries plus the NL problem group and mandatory near-duplicate/benchmark groups. Split assignment uses union-find connected components (§19.5).

### 11.7 `EvidenceRecord`

```text
evidence_id; target_kind; target_id; kind;
status=success|timeout|error|unsupported|abstain|not_run;
value; method/config versions; raw artifact; created_at
```

`target_kind` uses `EvidenceTargetKind`; the referenced target must exist and match the evidence kind. This single target contract covers theorem checks, Lean–Lean pairs, NL–Lean judgments, transformation drafts, and family audits without nullable competing IDs.

Per-kind semantic outcomes live in `value`:

```text
proof: proved | not_proved
defeq: equal | not_equal
counterexample: found | not_found | unsupported
claim_alignment: {alignment_version, binder_map, premise_map,
                  conclusion_role_map, direction, certified|rejected|unsupported}
LLM/human: canonical answer/confidence/rationale/audit fields
```

`not_proved` and counterexample `not_found` never map to a negative label.

### 11.8 `ResolvedLabel`

```text
schema_version=2; label_id; target_kind=lean_pair|nl_lean; target_id;
same_claim: bool|null; resolution_outcome; relation: RelationLabel|null;
faithfulness_levels {F0_representation_equivalent,
F1_same_claim, F2_truth_equivalent}; truth_A_implies_B;
truth_B_implies_A; error_types; quality_tier; resolution_method;
evidence_ids_used; adjudication_notes; requires_adjudication;
train_eligibility; eval_eligibility; policy_version
```

`target_kind` uses `SemanticLabelTargetKind`. A Lean–Lean label targets one `PairRecord`; an NL–Lean label targets one `NLPLeanRecord`. The target record carries the reverse `resolved_label_id`; a schema/reference-integrity check requires a one-to-one link.

Invariants:

- `F1_same_claim == same_claim`;
- resolved yes/no outcomes match `same_claim`;
- ambiguous/unresolved require null;
- unresolved review route requires `relation=null`, `quality_tier=unknown`, and `requires_adjudication=true`;
- terminal human ambiguity uses `quality_tier=gold_human` (or benchmark when source-defined), no binary target;
- truth false requires accepted separating evidence; failed search produces null;
- F0/F2 derive mechanically from accepted representation/truth evidence.

### 11.9 `NLPLeanRecord`

```text
nl_lean_id; problem/group/source/revision; NL statement/trust;
candidate theorem/generator; reference theorem IDs;
reference_pairs [{reference_theorem_id, pair_id}];
resolved_label_id; evidence_ids; split_group_ids; metadata
```

The NL–Lean label is referenced, never duplicated, and its `ResolvedLabel` must have `target_kind=nl_lean` and `target_id=nl_lean_id`. Every candidate/reference comparison is a separate `PairRecord` whose own label, if present, has `target_kind=lean_pair`; relation-to-reference is therefore per reference rather than a single overloaded field.

### 11.10 Transformation support records

`Applicability`, `VariantDraft`, and `TransformationAudit` are defined once in §15.2 and serialized by `schemas/variant.py`. Draft/audit IDs survive promotion.

### 11.11 LLM call record

Store call/provider/model/family/role, exact revision/date, prompt template/render hashes, input IDs, decoding parameters, raw/parsed output, parse status, retries, tokens/provider cost when available, timestamps, supervision eligibility, private-source approval status, and denylist results.

### 11.12 ID determinism

IDs hash normalized UTF-8 canonical JSON with sorted keys. Timestamps, machine-local absolute paths, and mutable state never enter semantic IDs. Schema migrations write explicit old→new mapping manifests.

---
## 12. Theorem extraction and context reconstruction

### 12.1 Required products

Every accepted declaration yields immutable source identity, `ContextRecord`, `TheoremRecord`, initial required `RepresentationRecord`, raw/normalized Lean response pointers, diagnostics/exclusion reason, and optional NL/trust links.

### 12.2 Repository extraction

1. resolve pinned project/context;
2. submit files via `FileCommand(..., declarations=True)`;
3. save complete raw/normalized response;
4. select configured theorem/lemma proposition declarations;
5. recover raw text from returned source ranges;
6. replace proof using syntax/range-aware code;
7. revalidate with explicit `allow_sorry=True`;
8. write accepted or failure record.

### 12.3 Dataset-string extraction

For Hugging Face rows, immutable locator identity is independent of mutable
content:

```text
source_record_id = SHA256(
  "hf-row:v1" || dataset_id || immutable_revision || split || row_index
)
```

Persist `upstream_uuid`, `raw_row_hash`, `question_hash`, and `lean_code_hash`
separately. For `sft_classic`, preserve the complete fenced Lean block from
`question` and completed `lean_code`; attempt the proof-free question statement
first. Before attempting the `lean_code` fallback, replace its top-level theorem
proof with an explicit placeholder using the versioned, bracket/comment-aware
statement stripper; dataset proof tactics are never executed by the extraction
pipeline. Unsupported proof forms fail closed. If both stripped routes
elaborate, compare their binder-normalized fingerprints and record agreement.
A fallback-only theorem is valid Lean-only inventory with
`nl_pair_eligibility=unverified`; it is not trusted NL–Lean supervision. A
mismatching fallback never silently replaces an elaborating question statement.
For an accepted inline theorem, persist the reconstructed proof-stripped
declaration plus its required preceding command context separately as
`inline_elaboration_source`. Representation requests execute that field but
derive model-visible views only from the isolated proof-stripped declaration;
proof-sentinel fixtures enforce this separation.

Every row and every attempted declaration receives one terminal outcome.
Malformed/multiple fences, source non-elaboration, no declarations, missing or
invalid ranges, duplicate names, non-`Prop` declarations, import failure,
timeout, worker crash, revalidation failure, and unsupported structures are
persisted under distinct stable codes.

### 12.4 Proof stripping

Handle term proofs, `:= by`, existing admissions, modifiers/attributes, `where` blocks, nested `by`, and examples. Required before pilot: at least 100 difficult golden fixtures, signature-preservation property tests, proof-token leakage checks, and explicit failure records.

### 12.5 Context identity

Canonical payload:

```text
environment_schema_version
Lean/LeanInteract/REPL-fork versions
project URI/exact revision
ordered imports and namespace/open/scoped context
local notation/attributes and relevant options
normalized header text/hash
```

Then SHA256 and `ctx:` prefix as in §8.11. Changing payload schema increments `environment_schema_version` and invalidates caches.

### 12.6 Theorem and ancestry identity

Source theorem ID hashes `source_record_id`, declaration ordinal, extracted
signature hash, extraction-schema version, and registered context. Content
changes therefore do not change the row locator but do produce a new theorem
identity and a machine-readable old/new diff.

```text
ancestry_id = "anc:" + SHA256(source_record_id + declaration_identity)
```

One-parent variants inherit root ancestries; multi-parent variants use the sorted union and hash it for their derived ancestry; NL-only candidates receive a generator-call/problem-derived ancestry while the pair also includes the problem group. Ancestry never changes after labeling/splitting.

### 12.7 Filtering and scale checkpoints

Retain proposition-valued declarations that re-elaborate in registered context. Exclude with stable codes: unresolved metavariables, inaccessible context, non-propositions, unsupported syntax, configured length limits, or ambiguous selection.

- smoke: fixtures/source probes;
- pilot: ≥10,000 eligible statements for MVP identity/collision analysis;
- research_v1: repeat at ≥100,000 before large-scale identity claims.

“Reload” means reparsing immutable raw data reproduces byte-identical IDs/content hashes; re-elaboration is a separate nightly compatibility check.

Gate-closing reload requires 100% reproduction of source IDs, raw/content
hashes, parser outcomes, and final normalized terminal outcomes after the fixed
retry policy. The 99.5% re-elaboration rate is reported only as a non-gating
nightly stability metric.

---

## 13. Statement representations and normalization

### 13.1 Multi-view principle

No fully expanded string is universally canonical. Each theorem receives a versioned `RepresentationRecord`; expansion is bounded and environment-pinned.

### 13.2 Canonical view names

| Field | Definition | v0 |
|---|---|---:|
| `raw_proof_stripped` | source declaration with proof replaced | required |
| `headless` | name/proof/comments removed; cosmetic normalization | required |
| `signature_pp` | elaborator pretty-printed type | required |
| `signature_explicit` | pinned explicit-print helper output | optional until stable |
| `alpha_structural` | binder-ID/de Bruijn-normalized structure | optional v0; required before M5 |
| `notation_light` | whitelist-only notation expansions | optional |
| `semantic_atoms` | ordered/multiset substantive symbols/types/literals | required for promoted transforms |
| `operator_tree` | compact tree for GTED/TransTED | required before those baselines |

These names are used verbatim in the schema.

### 13.3 Invariants

Deterministic under lock; no proof-body leakage; theorem/binder renaming invariance where applicable; context retained; no unrestricted `simp`/proof search as normalization; explicit view failures; versioned atom extraction.

### 13.4 Pretty-print options

For `repr_v3`, both signature views are printed directly from the elaborated
`ConstantInfo.type` under a fresh `Options.empty` map. This is the canonical
path for public, private, and inline declarations. It prevents ambient core,
Mathlib, or future extension `pp.*` options from changing representation
bytes. Universe parameters are positionally renamed to `u_0`, `u_1`, ... before
printing so the resulting proposition text can be re-elaborated by the
symbolic-evidence aliases.

The two view-specific overrides are equivalent to:

```lean
set_option pp.fullNames true
set_option pp.proofs false
set_option pp.proofs.withType false
set_option pp.mvars false
-- signature_pp:
set_option pp.explicit false
set_option pp.universes false
-- signature_explicit:
set_option pp.explicit true
set_option pp.universes true
```

Legacy `#check` recovery profiles explicitly pin all 75 core Lean-4.31
`pp.*` options, but they are not authoritative representation producers
because imported libraries may register additional options. `pp.proofs=false`
may render proofs as `⋯`; this is expected and is never justification to
include proof payloads.

### 13.5 Structural representation

At minimum store node kind, full constant name, ordered application children, binder/scope links, literals, universe/type summary where useful, and source ranges when available. M5 may add binder-scope/type-of/shared-constant edges.

### 13.6 Semantic atoms and positive audits

Atoms include quantifiers, binder types/dependencies, structures/typeclasses, predicate/relation heads, constructors, casts/targets, literals, and hypothesis/conclusion heads. Every promoted positive stores an atom-diff audit; unexpected deletion/substitution routes to `semantic_rewrite_candidate` or quarantine.

### 13.7 Tokenizer audit

Compare the four §21.2 candidates on the exact frozen Gate-3 manifest. Report
Unicode/namespace fragmentation, per-semantic-section lengths, and whether
binders, typeclass binders, hypotheses, and conclusions survive at 512 and
1,024 tokens. Context-length eligibility and backbone selection follow §21.2;
special-token changes are separate preregistered ablations and cannot be made
after seeing selection results.

### 13.8 Representation ablations

### 13.9 Gate-3 orchestration and identity fingerprint

The canonical orchestration contract is:

```text
RepresentationBatch:
  context_id
  import_header
  ordered_theorem_inputs

RepresentationBatchResult:
  ordered_representation_records
  per_theorem_failures
```

Empty input returns an empty result and mixed contexts fail before Lean
execution. The MVP correctness primitive is one independent LeanInteract
request per theorem, computing that theorem's applicable views together and
retrying a failed view independently when needed. Multiple theorem checks are
not concatenated; recursive bisection is reserved for a later batching
optimization. Inline dataset statements are declared and inspected within the
same request. One theorem failure can never erase sibling results.

Every combined theorem request and every independently isolated view request
uses the same bounded top-level retry policy: `max_attempts=2`, with retries
only for `CRASH`, `INTERNAL_ERROR`, or `TIMEOUT`. `INVALID` and `SETUP_ERROR`
are terminal. Isolated view probes are distinct correctness requests, not
semantic retries, and each starts with exactly one `import Lean` followed by
the registered context imports. Any backend-internal one-shot server recovery
remains infrastructure recovery and cannot change these status semantics or
create a semantic label.

Scale orchestration processes those independent requests in bounded worker
chunks. Completed-chunk markers are bound to theorem inputs, context,
normalization version, code tree, code bundle, and relevant execution
configuration. Resume with any mismatch fails closed, and final partitions
are atomically merged in exact frozen-manifest order. When unfinished chunks
exist, the parent process performs one LeanInteract-owned project/REPL
preflight; chunk workers use the prepared environment with project and REPL
rebuilding disabled. The setup mode is bound into chunk hashes and final
manifests.

Normalization-version evidence is immutable and version-specific. The closed
`repr_v2` Gate-3 reports, configs, and artifacts remain historical evidence and
must never be rewritten or relabeled as `repr_v3`. Before `repr_v3` records may
be used as scientifically gate-validated inputs, the exact frozen 10,000-record
denominator must be rebuilt under `repr_v3` and pass the complete Gate-3 audit
and deterministic replay in new versioned artifacts. Focused tests and smoke
runs may validate implementation behavior, but cannot substitute for that
scale closure.

That version-specific closure passed on 2026-07-30. Both independent
`repr_v3` runs represented all 10,000 frozen records with all required views
at 100%, replayed every representation ID and content hash, passed 1,000/1,000
alpha-renaming cases and 500/500 name-versus-inline comparisons, and closed
all 152 enumerated lossy-view clusters. The immutable decision is
`reports/gates/gate_3_repr_v3.json`; full evidence is
`reports/milestones/phase_3_repr_v3_revalidation.md`. The two raw record files
differ only in the operational `created_at` field; no scientific field differs.
Historical `repr_v2` artifacts remain unchanged and are not silently promoted
to `repr_v3`.

Gate 3 also builds a non-model binder-normalized identity fingerprint from the
elaborated expression: local binders use de-Bruijn-style identities; binder
metadata and types are retained; universe placeholders are normalized; fully
qualified constants, literals, and application structure are retained; proof
or value fields are excluded. This fingerprint is required for identity,
alpha-invariance, near-duplicate, and collision audits. It is not the deferred
`alpha_structural` model view and is not a graph.

Compare raw, headless, signature, combinations, explicit signature, text+atoms/scalars, and M5 text+graph. H3 is assessed on hard near misses and held-out transformation/project slices, not aggregate accuracy alone.

---

## 14. Pair-label taxonomy and error ontology

### 14.1 Canonical labels

Only the §11 enums may be serialized. Proposer intentions, judge text, UI labels, and imported benchmarks map through explicit tables; they never create new persisted spellings.

### 14.2 Review routing and ambiguity

- **UNCERTAIN** is a process route, not a persisted semantic class: `same_claim=null`, `resolution_outcome=unresolved`, `quality_tier=unknown`, `requires_adjudication=true`;
- terminal ambiguity is a trusted semantic outcome: null + ambiguous + trusted human/benchmark tier + no pending adjudication;
- external spellings such as judge `uncertain` or UI `cannot assess yet` normalize to the UNCERTAIN route;
- review is not a model class;
- terminal ambiguity is masked from binary BCE and headline binary metrics.

### 14.3 E01–E30 ontology

| ID | Name |
|---|---|
| E01 | `missing_hypothesis` |
| E02 | `extra_hypothesis` |
| E03 | `vacuous_or_inconsistent_hypothesis` |
| E04 | `wrong_quantifier` |
| E05 | `wrong_quantifier_order` |
| E06 | `wrong_domain_or_type` |
| E07 | `wrong_codomain` |
| E08 | `wrong_typeclass_or_structure` |
| E09 | `wrong_constant_or_predicate` |
| E10 | `wrong_operator` |
| E11 | `wrong_inequality_strictness` |
| E12 | `wrong_equality_or_iff_direction` |
| E13 | `wrong_set_operation` |
| E14 | `wrong_function_direction_image_preimage_map_comap` |
| E15 | `wrong_cast_or_coercion` |
| E16 | `wrong_index_or_bound` |
| E17 | `wrong_numerical_constant` |
| E18 | `wrong_answer_value` |
| E19 | `special_case_only` |
| E20 | `overgeneralization` |
| E21 | `irrelevant_or_unbound_variable` |
| E22 | `omitted_dependency_between_binders` |
| E23 | `namespace_import_or_notation_mismatch` |
| E24 | `malformed_or_non_elaborating_statement` |
| E25 | `semantic_erasure_or_tautologization` |
| E26 | `formalizes_related_but_different_claim` |
| E27 | `reference_suspected_incorrect` |
| E28 | `ambiguous_natural_language` |
| E29 | `cosmetic_only` |
| E30 | `other` |

Only IDs are stored in `error_types`; names come from `policies/error_ontology_v1.yaml`.

### 14.4 Interface mappings

Proposer:

| Input | Intended relation | Default error |
|---|---|---|
| equivalent/same | `equivalent` | none/E29 |
| A stronger | `A_stronger` | policy-specific |
| B stronger | `B_stronger` | policy-specific |
| non-directional near miss | `near_miss` | E26 unless more specific |
| unrelated | `unrelated` | E26 |
| malformed/unknown | `unknown` | none |

Judge:

| Answer | Resolution candidate |
|---|---|
| `same_claim` | true/equivalent |
| `not_same_claim` + directional relation | false/relation |
| `ambiguous` | terminal-ambiguity candidate |
| `uncertain`/malformed | UNCERTAIN process route; no terminal label |

UI:

```text
same claim | not same claim | ambiguous | cannot assess yet
```

`cannot assess yet` creates the review route with a null relation. The
explanatory UI phrase “related, neither directional claim” serializes as
`incomparable`; `near_miss` remains separate slice/provenance metadata.

### 14.5 Quality tiers

| Tier | Meaning | Training | Final evaluation |
|---|---|---:|---:|
| `gold_human` | independent expert labels + adjudication | yes | yes |
| `gold_conservative_transform` | promoted conservative family/item | yes | diagnostic unless independently sampled |
| `gold_counterexample` | accepted kernel-checked separator/certificate | yes | yes/diagnostic by split policy |
| `benchmark` | external benchmark label | only when explicitly trainable | yes when held out |
| `silver_consensus` | promoted independent weak supervision | weighted | not sole final basis |
| `provisional` | intention/smoke/mining candidate | restricted | no |
| `unknown` | unresolved/insufficient evidence | no binary target | workflow analysis |

Terminal expert ambiguity can be `gold_human` while still lacking a binary target.

### 14.6 Resolution precedence and conflict policy

```text
human adjudication
> frozen benchmark policy
> promoted conservative certificate / accepted separator
> promoted independent consensus
> generation intention
```

This is decision precedence, not destructive overwrite. Conflicting strong evidence creates a conflict record and review route.

### 14.7 F0/F1/F2 checks

F1 equals `same_claim`; F0 derives from accepted definitional/representation evidence; F2 derives from accepted directional truth fields. F2 false requires separating evidence, not failed proof search. Impossible combinations fail schema validation unless an explicit policy exception is linked.

---

## 15. Deterministic transformation framework

### 15.1 v1 scope

Active v1 families:

```text
positives: P01 alpha, P02 binder/interface, P04-lite direct notation
negatives: N01 operator, N02 quantifier, N03 hypothesis deletion,
           N07 literal/bound/index, N10 nearby theorem
```

Other families remain registry stubs with `experimental` or `disabled`. Cosmetic P00 is for invariance tests and cannot dominate training.

### 15.2 Canonical transformation protocol

```python
@dataclass(frozen=True, slots=True)
class Applicability:
    applicable: bool
    reason_codes: tuple[str, ...]
    matched_nodes: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

@dataclass(frozen=True, slots=True)
class VariantDraft:
    draft_id: str
    source_theorem_ids: tuple[str, ...]
    rule_id: str
    rule_version: str
    family_id: str
    seed: int
    candidate_code: str
    intended_relation: IntendedRelation
    intended_error_types: tuple[str, ...]
    candidate_pool: str
    transformation_trace: tuple[dict, ...]
    inverse_trace: tuple[dict, ...] | None
    expected_atom_mapping: Mapping[str, str]
    expected_structural_diff: Mapping[str, object]
    generation_config_hash: str
    metadata: Mapping[str, object] = field(default_factory=dict)

@dataclass(frozen=True, slots=True)
class TransformationAudit:
    audit_id: str
    draft_id: str
    applicability: Applicability
    elaboration_evidence_id: str | None
    structural_diff_ok: bool | None
    atom_mapping_ok: bool | None
    inverse_or_roundtrip_ok: bool | None
    certificate_evidence_ids: tuple[str, ...]
    violation_codes: tuple[str, ...]
    recommended_validation_status: ValidationStatus
    recommended_quality_tier: QualityTier
    metadata: Mapping[str, object] = field(default_factory=dict)

class TransformationRule(Protocol):
    rule_id: str
    rule_version: str
    family_id: str
    polarity: str
    def assess(self, theorem, representation) -> Applicability: ...
    def generate(self, theorem, representation, seed: int) -> Sequence[VariantDraft]: ...
    def audit(self, source, source_representation, candidate,
              candidate_representation, draft) -> TransformationAudit: ...
```

String substitution is permitted only for cosmetic fixtures; real rules operate on Lean-aware syntax/structure.

### 15.3 Universal gates

Every draft references valid sources/reps; elaborates as a proposition; has no unresolved metavariables; differs under required hash; retains seed/config/trace; passes denylist/dedup/size checks; stores diagnostics/evidence; receives canonical validation status; remains semantically provisional until promotion.

`semantic_rewrite_candidate` is a `candidate_pool` value, not a status/tier.

### 15.4 Positive promotion

`gold_conservative_transform` requires:

1. family status `gold_promoted` and positive allowlist;
2. exact local structural diff;
3. approved semantic-atom mapping;
4. preserved binder dependencies, assumptions, literals, and conclusion heads;
5. round-trip/inverse or rule-specific certificate;
6. no source/candidate proof constants or admissions;
7. Gate 4A family audit;
8. no item-level quarantine violation.

Otherwise keep provisional or route to semantic rewrite/human review.

### 15.5 Active positives

- **P01:** capture-avoiding alpha renaming; alpha-structural identity required.
- **P02:** regroup typed binders and a narrow approved currying/uncurrying subset; dependency graph and round trip required.
- **P04-lite:** finite notation↔direct-form table with exact constants and rule-specific certificate/invariant.

Unrestricted `simp`, `ring_nf`, `linarith`, `omega`, `aesop`, theorem lookup, mutual provability, semantic fusion/equisatisfiability, or collapse to `True`/reflexivity is never sufficient automatic-positive evidence.

### 15.6 Active negatives

- **N01:** type-compatible relation/operator mutation.
- **N02:** quantifier/order/dependency mutation.
- **N03:** substantive hypothesis deletion with only the minimal type-correct
  syntactic adjustment required for re-elaboration; this is mutation
  construction, not generated theorem repair.
- **N07:** literal, bound, index, or argument-order mutation.
- **N10:** high-overlap nearby theorem/component substitution; both source ancestries enter split groups.

v1 N01 and N10 draw substitutions from a curated, versioned type-compatible replacement table (`configs/transformations/replacement_table_v1.yaml`) listing allowed replacements with type preconditions and expected E-codes. Environment-derived replacement indexing — proposing substitutions automatically from extracted signatures — is deferred along with the stubbed families.

### 15.7 Negative promotion routes

A mutation begins provisional and may become supervised only by exactly one route:

1. checked counterexample/separator in supported fragment → `gold_counterexample`;
2. accepted one-direction proof plus separator for the reverse → `gold_counterexample` with directional relation;
3. expert adjudication → `gold_human`;
4. independent consensus plus audited precision → `silver_consensus`.

No “high-confidence mutation” shortcut. Search `not_found`/`not_proved` does not qualify.

### 15.8 Family registry and audits

`configs/transformations/registry.yaml` stores family/version/polarity/profile/status, allowed intentions/errors, invariants/evidence, audit manifest, and policy decision.

- 50–100 blinded items: pilot/refinement, especially negatives; no gold promotion.
- ≥200 blinded items for positive gold promotion. Define family precision as `audited same_claim=true / all blindly audited, successfully elaborated family outputs selected by the frozen design`; terminal ambiguity, unresolved review, policy violation, or incorrect output is not counted as a success. Require point precision ≥99% **and** the lower endpoint of a two-sided 95% Clopper–Pearson exact interval ≥95%.
- Rule changes create new versions and reopen promotion.

### 15.9 Required tests

Applicability fixtures; deterministic seeded output; capture/dependency property tests; re-elaboration; round trips; atom/structural golden diffs; no self-promotion from intended labels; N10 two-ancestry grouping; disabled-family rejection; immutable audit-stat recomputation.

### 15.10 Additive deterministic-v2 expansion contract

The eight-family v1 profile and its accepted Gate-4G evidence are immutable.
Broader deterministic coverage is an additive research track, not a rewrite of
`configs/transformations/v1.yaml`, an edit to an accepted v1 rule, or a
retroactive change to `reports/gates/gate_4g.json`. A changed matcher,
allowlist, site-selection policy, inverse, or certificate always creates a new
family/version. New families begin disabled while their contracts and coverage
probes are reviewed, then may become `experimental`; none begins silver or
gold-promoted.

The v2 track addresses a known scientific limitation: three narrow
presentation positives and five controlled negative mechanisms validate the
pipeline, but cannot by themselves show generalization beyond transformation
artifacts. The positive deficit is especially important because real Lean
statements vary through expected-type elaboration, explicitness, coercions,
projections, constructors, bounded binders, and other interface presentations.

V2 uses the existing schemas and promotion routes:

- compilation, deterministic intention, exact-expression comparison, a local
  certificate, semantic-atom alignment, or a structural direction is evidence,
  never by itself a `ResolvedLabel`;
- `gold_conservative_transform` still requires §15.4 and the unchanged blinded
  Gate-4A family audit;
- negative or directional supervision still requires exactly one §15.7 route;
  a structural direction may be audit/provenance, but cannot introduce a new
  promotion route without a versioned policy revision;
- an interface change not already approved by the semantic policy remains
  provisional and human-audited; and
- v2 work must not delay LF-021 human labeling or authorize training from
  unresolved transformation outputs.

### 15.11 V2 evidence classes

Every candidate family declares one initial evidence class. The class controls
validation and review, not labels:

1. **E0 — exact elaborated-type presentation:** source and candidate
   independently elaborate through LeanInteract in one frozen context; after
   alpha-normalizing binder names, canonicalizing universe metavariable IDs,
   and removing source-position metadata only, their complete theorem-type
   expressions are identical. No unfolding, general definitional-equality
   search, simplifier, or tactic is allowed. A family-specific inverse and
   role-preserving semantic-atom alignment are also required.
2. **E1 — one certified local presentation step:** one explicitly matched
   beta/eta-like or macro step with a family-owned redex certificate and
   free-variable/dependency checks. It begins provisional even when certified.
3. **E2 — semantic-atom isomorphism:** atoms and roles align, but binder,
   connective, or hypothesis packaging changes. It remains a human-audited
   positive candidate unless a later semantic-policy version resolves it.
4. **D0 — certified semantic delta:** one family-owned atom, role, scope,
   dependency, guard, or argument-slot delta. The candidate remains
   provisional and any supervision uses §15.7.

For E0, identical elaborated-type hashes are an expected pair feature, not a
reason to discard the pair. All surfaces join one equivalence/ancestry
component. Cross-source collisions union the relevant split groups or cause
fail-closed exclusion before split assignment.

### 15.12 Candidate v2 portfolio

These IDs reserve design scope only. They are not active until their code,
config, focused tests, coverage report, and registry decision are accepted.

| Family | Class | Narrow initial scope |
|---|---|---|
| P05 resolved names | E0 | uniquely resolved global-name qualification/dequalification; reject shadowing, aliases, private/macro-generated names, and ambiguous suffixes |
| P06 implicit arguments | E0 | surface/omit ordinary non-instance implicits while preserving the application spine; exclude instance implicits, `autoParam`, `optParam`, coercion-inserted applications, and metavariables |
| P07 coercion surface | E0 | expose/hide one already-elaborated `Coe`/`CoeT`-style hop; exclude `CoeFun`, `CoeSort`, chains, proof obligations, and ambiguity |
| P08 type ascriptions | E0 | insert/remove one redundant, instantiated, source-printable term ascription with exact whole-expression identity |
| P09 projections | E0 | named/numeric projection syntax versus the identical direct projection node; exclude coercion fields, record updates, and ambiguous receivers |
| P10 constructors | E0 | anonymous constructor/known tuple surface versus the same constructor expression; restrict to one-to-one single-constructor mappings |
| P11 bounded quantifiers | E0 | one exact bounded `forall`/`exists` expansion or contraction with the same binder, membership instance, guard role, and body |
| P12 proof arrow/binder | E0 | `P -> Q` versus an unused explicit proof binder; exclude dependent, implicit, instance, and non-`Prop` domains |
| P13 restricted eta | E1 | one explicit nondependent function-eta step with a syntactic redex and free-variable certificate; no general definitional equality |
| P14 independent binder permutation | E2 | one adjacent explicit universal-binder swap with no dependency; proof, implicit, and instance binders excluded |
| P15 root `Iff` reversal | E2 | swap only distinct root-conclusion sides; never nested or under another connective |
| P16 conjunction reassociation | E2 | rotate only a top-level conjunctive conclusion while preserving exact atom order |
| P17 hypothesis packing | E2 | curry/pack two nondependent propositional hypotheses without reordering; exclude data and instance binders |
| N11 bound-variable substitution | D0 | replace one explicit occurrence with a distinct in-scope binder of the same alpha-normalized type |
| N12 implication converse | D0 | swap distinct root nondependent hypothesis/conclusion roles; exclude `Iff` and nested implication |
| N13 witness dependency | D0 | `forall x, exists y, R` versus `exists y, forall x, R` when the witness type is independent; record structural direction only |
| N14 negation scope | D0 | move one `Not` across one universal binder without dualization, De Morgan rewriting, or classical normalization |
| N15 conjunct omission | D0 | remove one distinct top-level conclusion conjunct; record structural direction and retained order |
| N16 domain guard removal | D0 | remove the exact bounded-quantifier membership guard; keep P11/N03/N15 ownership disjoint |
| N17 role-sensitive arguments | D0 | swap explicit, independent, same-typed slots from a manually audited head/position allowlist; deny symmetric heads and N01/N07 scope |

P14–P17 do not become automatic positives from logical equivalence or human
plausibility. P13 stays provisional pending its local certificate and blinded
audit. P05/P08 are lower-value surface families and must be capped and ablated
so they cannot dominate the positive pool.

### 15.13 V2 staging, audits, and shortcut controls

The first implementation tranche is P11, P06, P07, P09, P10, and P12. P05/P08
follow as controlled surface ablations. P13 and P14–P17 remain separate
provisional studies. N11 is the first negative expansion; N12–N17 follow only
after family-overlap ownership and E-code mappings are frozen.

Before enabling a family, freeze its source and elaborated-node matchers,
excluded cases, candidate-site ordering, seed behavior, inverse, allowed
expression difference, evidence class, overlap precedence, coverage
denominators, and audit strata. Every attempt stores all considered sites and
rejection reasons plus the selected span/node path, resolved constants and
binders, source/candidate validation, inverse/certificate result, atom
alignment or delta, hashes, and split/dedup outcome.

Each family additionally requires trace/certificate corruption tests,
registry-overlap tests, changed-span properties, clean source/candidate
LeanInteract elaboration, context-drift rejection, and component-level
split/dedup tests. Exact-expression utilities may be shared, but applicability
and certification remain family-owned; there is no generic equivalent-rewrite
executor.

Evaluation includes seen-family/unseen-ancestry, whole-family holdout, and
mechanism-superclass holdout slices. Table families also hold out complete
constants, coercion classes, projections, constructors, predicates, or role
pairs. Model inputs never contain family IDs, seeds, traces, certificates,
source paths, or transformation status. Report family-macro averages and
surface-cue diagnostics, cap descendants per source, and bootstrap by ancestry
component rather than generated pair.

Rejected automatic-positive shortcuts include unrestricted automation,
theorem lookup, mutual provability, general definitional-equality
normalization, broad unfolding/folding, algebraic/propositional normalization,
semantic fusion, tautological padding, equality-hypothesis rewriting,
arbitrary pretty-print/reparse, and equality orientation.

---

## 16. Counterexample and symbolic evidence pipeline

### 16.1 Sampling scope

Symbolic evidence is mandatory for evaluation/calibration pairs when applicable, gold-promotion candidates, gold-counterexample candidates, and a preregistered stratified training sample. It is not exhaustively run over all training pairs.

### 16.2 Evidence jobs

| Kind | Execution status | Semantic value |
|---|---|---|
| definitional equality | success/error/etc. | `equal|not_equal` |
| closed proof A→B / B→A | success/timeout/etc. | `proved|not_proved` for F2 only |
| binder-aligned claim certificate | success/unsupported/etc. | `certified|rejected|unsupported` + mapped direction |
| counterexample | success/unsupported/etc. | `found|not_found|unsupported` |

Only checked proof yields `proved`; only checked separator yields `found`. Negative search outcomes remain unknown.

### 16.3 Proposition isolation and comparison modes

Build helpers from proposition types, not imported theorem proof constants. Do not make either compared proof constant available to the tactic context. Audit used constants/axioms for accepted certificates.

Keep two evidence modes distinct:

1. **closed truth mode** proves the complete proposition A→B or B→A and can populate only F2;
2. **binder-aligned claim mode** records a versioned alignment of corresponding binders, premises, and conclusion roles, then proves a local mapped implication without exploiting an inconsistent whole-theorem premise.

Only the second mode may support `A_stronger`/`B_stronger`, and only when the alignment/diff passes the semantic policy. If alignment is unavailable or the proof uses vacuity/ex falso, keep the claim relation unknown or human-resolved even when closed truth mode succeeds.

### 16.4 Authoritative proof portfolio

`configs/evidence/portfolio_v1.yaml` is authoritative and versioned. It may include exact/assumption, constructors/intros, replayed `simp?`/`aesop?`, `omega`, `linarith`, `nlinarith`, `ring`, `norm_num`, approved domain tactics, and narrowly defined binder-alignment templates. Accept only replayed admission-free certificates. Alignment templates record binder/premise/conclusion maps and reject vacuity/ex-falso shortcuts. Tactic order, templates, and timeouts are part of the method version.

### 16.5 Bounded counterexamples

Restrict v1 to finite/bounded `Decidable` fragments. Prefer kernel `decide`. Persist domain/encoding/helper/theorem/result and axiom audit. Coverage is best effort; no broad counterexample gate exists.

### 16.6 Lower-trust native execution

`native_decide` is a distinct lower-trust evidence method because native execution bypasses ordinary kernel reduction and has had known soundness failures. Mathlib policy forbids relying on it for accepted library proofs, and Lean ≥4.29 can attach per-computation axioms. For every use record the Lean version and `#print axioms` (or pinned equivalent), store per-computation/other axioms, and never use it as the sole basis for a gold negative. It may mine/corroborate candidates only unless a later reviewed policy changes this.

### 16.7 Certificate acceptance

Require `allow_sorry=False`, no admissions/unresolved metavariables/forbidden axioms, no source/candidate proof constants, exact context, persisted code/result, axiom/dependency audit, pair/method link, and reproducibility after cache deletion.

### 16.8 Evidence-to-label policy

Defeq informs F0; closed whole-proposition proofs inform F2; binder-aligned certificates may support a directional claim relation; a checked separator can set a truth direction false and support F1=false under policy. None alone silently sets F1. A closed implication never directly assigns `A_stronger`/`B_stronger`, and mutual closed provability never directly assigns F1=true. Conflicts route to review.

### 16.9 Cache key

Include pair/theorem ID, kind/version, context ID, `environment_schema_version`, timeout, portfolio/config hash, and Lean/LeanInteract versions. A changed timeout/method is a new key.

---

## 17. LLM data generation with a very large token budget

### 17.1 Roles and boundary

Models may autoformalize, propose controlled variants, or judge. Model output/votes are evidence, never automatic gold.

### 17.2 Provider slots

`configs/generation/providers.yaml` declares:

```text
judge_A              frontier family A
judge_B              distinct frontier family B
broad_generator      high-capability general model
open_diversity       open-weight diversity model
specialized_generator ReForm-32B, ReForm-8B fallback
primary_eval_judge   family excluded from all training supervision
```

Exact IDs/revisions are frozen at run start. Secret credentials stay outside configs. Prompts containing private-source content are submitted only to providers covered by the §9.2 approval record.

### 17.3 Generator families

The Phase-5 generation checkpoint requires at least three materially distinct
successful families. A confirmatory D4/D5 run that both enforces the
per-family `G_real` cap and reserves a clean `heldout_generator_test` family
requires at least four successful families: at least three supervision-eligible
families plus one fully held-out family. A three-family collection is valid for
the collection checkpoint and source-ablation work, but is explicitly
`reduced_data_ablation` for the full-mixture and held-out-generator claims.
Candidate families: ReForm-32B/8B, Kimina-Autoformalizer-7B,
Goedel-Formalizer-V2, StepFun-Formalizer, Herald, ATLAS, and a capable
frontier family. Exact availability/license/overlap is probed first.

The reserved family contributes no output, judgment, pseudo-label, prompt
demonstration, or distilled signal to supervision.

### 17.4 Early real-output collection carve-out

Collection may begin after Gate 2, but outputs stay under raw/parsed quarantine until Phase 5/6 policies exist. For each trusted NL problem request multiple independent candidates across families/seeds. Preserve noncompiling outputs for failure analysis; only compiling proposition candidates enter semantic pair pools.

### 17.5 Prompt families

Version direct autoformalization, SCI-conditioned semantic mutation,
open-ended adversarial mutation, equivalent reformulation, stronger/weaker,
adversarial minimal edit, and critique/revise prompts. Require strict
machine-parsable output and retain raw failures. The flagship does not train a
localization or repair-generation task.

### 17.6 Validation/deduplication

Parse under versioned extractor; validate through LeanInteract; create variant/theorem/representation records; apply denylist/near-duplicate checks; deduplicate by raw/headless/alpha/problem IDs; retain generation lineage and failed attempts.

### 17.7 Blinded judges and circularity control

Judges see only registered Lean/NL views and allowed evidence condition—not proposer/family/intention/gold/other votes. Judge A/B are distinct. The primary LLM baseline family is excluded from all training-time supervision. Freeze a judge×supervision cross matrix before labels are produced.

### 17.8 Silver promotion

Require schema parse, family independence, no proposer–judge shortcut in primary data, canonical agreement, no conflict with accepted evidence, and audited stratum precision. Store `silver_consensus`; disagreement/low confidence/semantic-erasure suspicion/malformed output routes to review.

For SCI-conditioned generation, store the requested and validated values
without overwriting either:

```text
formalrx_sci_requested
formalrx_sci_validated
formalrx_sci_validation_status
formalrx_sci_proposer_family
formalrx_sci_validator_family
```

SCI and E01–E30 are provenance, annotation, and analysis metadata. They are
not flagship inputs or mandatory prediction heads. Proposer and validator are
materially distinct model families, and at least one proposer/judge family is
excluded from every training signal.

### 17.9 Capped stratified audit

Audit promoted silver strata with:

```text
base sample size = min(ceil(0.20 × promoted_silver_count), 1000)
```

Allocate preregistered per-stratum minimums **inside** that base sample. If the requested minima do not fit, coarsen/merge the promotion strata before sampling or keep the affected strata unpromoted; do not silently exceed the cap or waive a minimum. Oversample disagreement, near-threshold, rare domains/errors, and symbolic conflicts within the resulting design. Preserve inclusion propensities and report weighted/unweighted results.

### 17.10 Real-output prevalence

Before freezing `real_output_test`, human-label a 200–300-item stratified
sample across generator/domain from compiling candidates. Phase 5 freezes the
sampling frame and propensities; the annotation track supplies and adjudicates
the labels before final Gate 5 closure. Estimate faithful prevalence with
confidence intervals and sampling propensities. Use it for power/headroom,
test composition, and reranking expectations—not as an automatic training
shortcut.

### 17.11 Active learning and token policy

Prioritize model/judge/symbolic disagreement, high-similarity negatives, low-overlap positives, semantic erasure, rare categories, suspected references, and held-out-like behavior. Never inspect frozen tests. Spend tokens first on diversity, then validation/dedup, then independent judging, and only later repeated judging of informative residuals. Record tokens/calls/latency/retries/provider cost without dollar caps.

---
## 18. Human annotation and adjudication

### 18.1 Parallel tooling track

Annotation tooling/guidelines may start after Gate 3 in parallel with generation. Generation and promotion are separate checkpoints: candidates may exist before the pilot, but no family/silver stratum is promoted without required blinded labels.

### 18.2 Products and rounds

| Product | Purpose | Use |
|---|---|---|
| policy pilot | definitions/UI/mapping refinement | no final metric |
| `training_gold` | human-gold weight training | weight training only |
| `selection_gold` | backbone/mixture/representation/checkpoint choice | selection only |
| `calibration_gold` | final calibration/thresholds on real outputs | no weight training |
| `final_human_test` | primary claims | sealed evaluation |

All four products are ancestry/NL-problem connected-component disjoint.
`selection_gold` never enters gradient updates; `calibration_gold` is accessed
only after architecture/checkpoint selection; `final_human_test` remains
sealed. The legacy name `development_gold` appears only in the versioned
migration note and maps to separately frozen training and selection manifests.

Rounds: first 100 Lean–Lean workflow items; then cumulative 200–400 Lean–Lean plus 100–200 NL–Lean; then Phase 7b main campaign.

A ≥500-item final main-task test can support aggregate/95%-precision claims with intervals when enough accepts exist, but not precise rare-error/every-domain/99%-precision recall claims.

### 18.3 Sampling and propensities

Store stratum, inclusion probability, and design weight. Sample provenance/generator/domain/length/relation/similarity/error/symbolic strata. Final-test selection may not depend on any compared model. A fixed preregistered reference scorer may define difficulty only if excluded from comparisons. Include a simple-random compiling-real-output subpanel; deployment operating claims rest on it.

### 18.4 Blinded interface

Show proof-stripped statements, elaborated signatures, minimal context/import summary, and typecheck status. Optional structural/reference panels are explicitly marked. Hide generator, transformation, intention, prior votes, split, and model scores.

### 18.5 Fields

```text
same_claim: same_claim | not_same_claim | ambiguous | cannot_assess_yet
relation: equivalent | A_stronger | B_stronger |
          incomparable | unrelated | ambiguous
error_types: E01–E30 multi-label
confidence: 1–5
rationale: required for not-same/ambiguous
reference_issue: none | suspected | definite
```

`cannot_assess_yet` routes to unresolved review, not terminal ambiguity.

### 18.6 Protocol

Two independent expert labels; no discussion before first labels; adjudicate disagreement/low-confidence/policy triggers; preserve raw labels/rationales; resolve under versioned guideline; compute raw agreement, Cohen's κ, and per-category agreement. Guideline revisions occur only between rounds and trigger reevaluation of affected pilot examples.

Gate 7: κ≥0.60 and raw agreement≥80%. Failure triggers category/guideline revision and a new blinded pilot, never threshold lowering.

### 18.7 Audit tiers

- 50–100 blinded items: pilot/noisy negative-family refinement; no gold promotion.
- ≥200: positive-family gold promotion under Gate 4A point/Clopper–Pearson rule.
- Any rule-version change invalidates prior family promotion.

### 18.8 Ambiguity policy

Distinguish genuine ambiguity from annotator uncertainty. Genuine terminal ambiguity stays in the audit/evaluation set, masked from binary target and used for three-class/abstention analysis. Annotator uncertainty triggers further review.

### 18.9 Guideline inventory

Include vacuity, inconsistent/redundant hypotheses, directional strength, domains/typeclasses, implicit arguments/coercions/subtypes, semantic erasure, answer-only statements, reference errors, ambiguous NL, and notation/abstraction boundaries. Every E-code has accepted/rejected examples before Phase 7b.

---

## 19. Dataset construction, balancing, and contamination control

### 19.1 Build profiles

| Profile | Scope |
|---|---|
| smoke | fixtures/≤1,000 pairs; plumbing only |
| pilot | ≥10k statements; controlled generation/pilot labels |
| research_v1 | ≥100k statements when learning curves justify scale |

LF-018's pre-scale audit slice precedes the pilot profile.

### 19.2 Balance versus prevalence

Training may balance labels/relations/tier/family/error/source/domain/provenance/generator. Evaluation preserves or reweights to declared prevalence. Never imply a 50/50 deployment prior.

### 19.3 Sampling metadata

Precompute lexical/token/atom/tree similarity, binder/length/typeclass differences, symbolic outcomes, source/provenance. Store frame, stratum, inclusion probability, design weight, fixed reference scorer if used, and selection timestamp.

### 19.4 Denylist timing

Phase 2 writes `data/benchmarks/frozen_ids.json` before any Phase 4 generation, freezing source IDs, normalized-NL hashes, raw-text hashes, references/candidates, and provenance. Representation-dependent near-duplicate signatures (headless/signature/alpha hashes and retrieval indexes) are appended to the frozen registry at the end of Phase 3 — still before any Phase 4 generation; the append is additive and versioned, never a rewrite. Cross-split candidates appear in contamination reports.

### 19.5 Connected-component grouping

For each pair, `split_group_ids` is the union of both sides' root ancestries, NL problem group, and mandatory near-duplicate/benchmark groups. Build pair↔group edges, compute union-find connected components, and assign each component atomically. This handles multi-parent/N10/multiple-reference/multiple-candidate cases.

### 19.6 Split inventory and label sources

`human_test` is the serialized split name for the sealed `final_human_test` product; no second spelling is allowed in code. Every split manifest freezes `target_count`, realized eligible count, label source, sampling design, and its exact subset/disjoint relation to `human_test`.

| Split | Label source | Target-count rule | Relation to `human_test` | v0/track |
|---|---|---|---|---|
| `train` | permitted gold/silver under policy | build-profile/config count after component assignment | disjoint | mandatory |
| `validation` | `selection_gold` plus allowed non-human development labels | frozen selection fraction/component count | disjoint | mandatory |
| `calibration` | pair-level nondeployment development labels for method diagnostics only | frozen component count | disjoint | mandatory |
| `internal_test` | frozen internal gold/diagnostic labels | fixed in `configs/splits/v0.yaml` before model selection | disjoint | mandatory |
| `human_test` | sealed expert adjudication | ≥500 eligible main-task items unless preregistration declares the corresponding operating-point claim unsupported | identity | final claims |
| `benchmark_test` | frozen external benchmark labels | all eligible rows after environment migration/exclusions | disjoint after denylist/near-duplicate filtering | when adapters ready |
| `real_output_test` | expert labels on compiling real outputs, including a simple-random-sample subpanel | power-derived and frozen after the 200–300-item prevalence study | named subset of `human_test` by default; any disjoint design requires preregistered ADR | deployment claims |
| `heldout_transform_test` | blinded expert/audited labels for unseen families | all eligible or power-derived frozen count | subset of `human_test` when human-labeled; otherwise disjoint manifest | strong-paper |
| `heldout_project_test` | expert/accepted labels from unseen projects | all eligible with absolute item/group counts | disjoint project manifest | strong-paper |
| `heldout_generator_test` | expert labels from the supervision-excluded family | power-derived frozen count | named subset of `real_output_test`/`human_test` | H6 strong-paper |
| `adversarial_test` | expert minimal-pair/semantic-erasure labels | frozen curated frame count | named subset of `human_test` unless separately constructed | strong-paper |

Human/benchmark/real/held-out/adversarial splits are manifest-built, not random fractions. A row may belong to an explicitly declared reporting subset (for example `heldout_generator_test ⊆ real_output_test ⊆ human_test`) while having exactly one primary split assignment for training-access control; reporting-subset membership is stored separately from the primary split.

Deployment thresholds use `calibration_gold`, restricted to the real-output distribution and disjoint from all weight-training and `human_test` records. Pair-level `calibration` is development-only. CSLib/Physlib results always include absolute item and ancestry-group counts.

### 19.7 Frozen evaluation registry

Denylist at minimum:

```text
ProofNetVerif; ProofNet#; RLM25; Con-NF; EPLA; CriticLeanBench;
ConsistencyCheck; Gaokao-Formal; DriftBench; miniF2F variants
```

Store exact source/revision/IDs/hashes/near-duplicate signatures/usage. Test labels never influence training, active learning, calibration, or prompts.

Special rules:

- ReForm is trained on Lean Workbook; ReForm×Lean-Workbook items are not held-out-generator/source-independent evidence.
- Public benchmarks/mathlib may be in pretrained models; Risk R26 requires public-versus-post-cutoff-novel deltas.
- Any trainable ProofNetVerif partition is frozen/disjoint before benchmark claims.

### 19.8 Split manifests

Each stores record/component/group IDs, label source, source/project/generator, propensities, denylist status, hashes/version/config. `configs/splits/v0.yaml` contains random fractions only for train/validation/calibration/internal_test and manifest paths for all constructed tests, plus held-out generators/projects/transforms and mandatory/deferred status.

### 19.9 Reload definition

Reparsing immutable raw data must reproduce byte-identical IDs/content hashes for ≥99.5% of checked records, with all discrepancies explained. Re-elaboration is a separate nightly check.

---

## 20. Baselines

### 20.1 Simple/scalar

Exact/headless/signature equality; token/character edit; constant/atom overlap; generic embedding cosine; logistic/boosted scalar classifier. The scalar classifier is not M0.

### 20.2 Symbolic and naming

Typecheck; defeq; directional `portfolio_v1`; certificate-or-abstain; original **BEq** (Liu et al./Con-NF); **BEq+** (Poiroux et al./ProofNetVerif); symbolic ensemble. Always name the implementation/paper/config/protocol—never collapse BEq and BEq+.

### 20.3 Structural/learned critics

Operator-tree edit, GTED, ASSESS/TransTED, FormalAlign-compatible evaluator, CriticLean/CriticLeanGPT, LeanScorer/Mathesis condition, generic code cross-encoder, and FormalRx verdict. If code/protocol is unavailable, document it; do not publish an approximation under the method's name.

M0–M3 compare directly only with Lean–Lean methods receiving reference and
candidate Lean. FormalRx is a direct quality baseline only for M4-NL when both
systems receive exactly `N + candidate Lean` and share human labels.
Reference-aware M4 versus FormalRx is an explicitly unequal-information
systems comparison, not evidence of architectural superiority.

### 20.4 LLM judge conditions

Raw Lean; signature Lean; reference-free NL+Lean; reference-aware NL+reference+candidate; two-family consensus; judge+symbolic; fixed self-consistency; and a COVCAL-style risk-controlled Lean-as-judge condition when its released protocol is reproducible under the pinned environment. Primary judge family is supervision-free. Prompt development has a bounded preregistered variant/example/call allowance and never uses frozen tests. Any COVCAL comparison uses its own disjoint calibration data and reports coverage/risk assumptions rather than treating Lean execution as an infallible oracle.

### 20.5 Fair cost accounting

Report wall-clock/compute class, provider tokens/calls/cost, Lean requests and amortized evidence cost, cache/cold-start conditions, coverage/abstention. M3/symbolic methods include evidence collection cost; LLM and neural comparisons state batching/caching.

### 20.6 Common output

```json
{
  "record_id": "pair:... or nllean:...",
  "method": "...",
  "method_version": "...",
  "same_claim_probability": 0.0,
  "ambiguity_probability": 0.0,
  "decision": "ACCEPT | REVIEW | REJECT",
  "relation_scores": {},
  "optional_auxiliary_scores": {},
  "model_version": "...",
  "tokenizer_version": "...",
  "representation_version": "...",
  "calibration_version": "...",
  "elapsed_ms": 0,
  "cost": {},
  "evidence_ids": [],
  "config_hash": "..."
}
```

---

## 21. Model design

### 21.1 Fixed numbering

```text
M0 dual-encoder embedding model
M1 concatenated Lean-pair cross-encoder
M2 shared encoder + bidirectional cross-attention/matching
M3 M2 + symbolic/structural fusion
M4 NL–Lean faithfulness model
M5 optional text + elaborated Expr graph
```

### 21.2 Encoder decision

Freeze exact checkpoint and tokenizer revisions for:

- `answerdotai/ModernBERT-base`;
- `answerdotai/ModernBERT-large`;
- `Salesforce/codet5p-220m`, encoder branch only;
- `microsoft/deberta-v3-large`.

No candidate is the default and there is no parameter ceiling. ADR-0004 first
freezes the protocol and later records the winner without changing the rule.

Run a data-only tokenizer/context audit over the frozen Gate-3 10,000-theorem
manifest before training. Use 512 tokens only if every conclusion and at least
99% of all complete binder/typeclass-binder/hypothesis sets survive the frozen
section-budgeting policy. Otherwise use 1,024 and exclude candidates without
native support at that length unless their pretrained positional architecture
is unchanged. Preserve excluded examples under `long_input`.

Every eligible candidate receives the same 50,000 ancestry-disjoint training
pairs, or all pairs if fewer; 50/50 positive-negative batches; identical
representation content; equal example exposure/effective batch size; AdamW;
learning rates `{5e-6,1e-5,2e-5}`; weight decay `{0.01,0.1}`; one tuning seed;
and three independent confirmation seeds.

`selection_gold` must contain at least 100 faithful and 100 unfaithful
ancestry/NL groups and 50 groups for every class included in the confirmatory
relation metric. Estimate AUPRC and relation macro-F1 with a hierarchical
paired bootstrap over seeds and ancestry/NL groups. Use simultaneous one-sided
95% bounds across all candidates to account for selecting the empirical best.
Retain a candidate only when the simultaneous upper bound on its AUPRC deficit
is ≤0.01 and then the upper bound on its relation-macro-F1 deficit is ≤0.02.

Among survivors, select the highest median cached-reference batch-32
throughput. Differences below 5% break ties by lower peak memory, then fewer
loaded parameters, then lexicographically smaller model ID. Use 20 warmup and
100 timed batches in the identical frozen environment, report tokenization
separately and end-to-end, and include uncertainty intervals. ModernBERT-base
is only an implementation smoke fallback if the pilot cannot run; it is not a
scientific winner until selected by this rule.

### 21.3 M0

Shared separate encoders; normalized embeddings; symmetric head over cosine, absolute difference, and product; BCE plus optional contrastive/ranking loss. Used for retrieval/mining and low-cost baseline.

### 21.4 M1

Concatenate tagged A/B headless/signature views; predict same-claim, ambiguity,
relation, and optional masked F0/F2 auxiliaries. E01–E30 prediction is an
optional diagnostic ablation, disabled for the flagship.

### 21.5 M2 bidirectional matching

The backbone-pilot input bundle is exactly:

```text
[HEADLESS] ...
[SIGNATURE_EXPLICIT] ...
```

Encode A and B independently with one shared encoder. Apply exactly two
synchronous bidirectional matching layers: in layer `l`, both directional
updates read only `H_A^(l-1)` and `H_B^(l-1)`, and the A←B/B←A matcher
parameters are shared. Only the base reference encoding is cacheable;
candidate-dependent cross-attention is recomputed.

Build the same-claim and ambiguity inputs exclusively from commutative
features such as `mA+mB`, `abs(mA-mB)`, `mA*mB`, and symmetric alignment
statistics. Directional logits are swap-equivariant:

```text
A_stronger_logit = g(mA, mB)
B_stronger_logit = g(mB, mA)       # the same g
incomparable_logit = h_inc(symmetric_features)
unrelated_logit    = h_unr(symmetric_features)
```

Factor the distribution coherently:

```text
p_ambiguous
p_equivalent_given_nonambiguous
q_non_equivalent = softmax(A_stronger, B_stronger, incomparable, unrelated)

P(ambiguous)  = p_ambiguous
P(equivalent) = (1-p_ambiguous) * p_equivalent_given_nonambiguous
P(r)          = (1-p_ambiguous) * (1-p_equivalent_given_nonambiguous) * q[r]
same_claim_probability = P(equivalent)
```

Checkpoint tests require exact numerical-tolerance behavior under swapping:

```text
P_equivalent(A,B) == P_equivalent(B,A)
P_ambiguous(A,B) == P_ambiguous(B,A)
P_A_stronger(A,B) == P_B_stronger(B,A)
P_incomparable(A,B) == P_incomparable(B,A)
P_unrelated(A,B) == P_unrelated(B,A)
```

This is the intended decoder-like cross-attention, not autoregressive decoding.

### 21.6 M3 hybrid

Fuse defeq, directional proof/counterexample state, GTED/tree score, atom/constant overlap, binder/typeclass/context/lexical features with an explicit missingness mask for every evidence feature. Compare feature concatenation, calibrated meta-model, and certified-override→neural-fallback. Failed proof search is encoded unknown/missing evidence, not negative. `configs/models/m3.yaml` is mandatory.

### 21.7 Heads and ambiguity

- symmetric same-claim/equivalence head; ambiguous/unresolved masked from binary loss;
- separate symmetric ambiguity head;
- conditional six-terminal-class relation distribution with no `unknown` target;
- masked A→B/B→A auxiliaries;
- optional F0 auxiliary.

Primary training masks terminal ambiguity from BCE. A three-class model is a preregistered ablation.

### 21.8 Loss

```text
L = λeq BCE(same) + λamb BCE(ambiguity) + λrel CE(relation)
  + λdir masked directional BCE + λswap swap consistency
  + optional λrank ranking
```

Unknown fields are masked. Quality weights/loss weights select on validation only.

### 21.9 Curriculum

Start with promoted conservative/near-miss data; mix real outputs early; add silver with explicit weights; fine-tune on development gold. Compare deterministic-only, +LLM, +real, all weak, and weak+human. Provisional data are mining/pretraining only unless ablated.

### 21.10 Calibration/abstention

Two roles:

- pair-level `calibration`: compare methods during development;
- `calibration_gold`: fit final deployment thresholds on expert real-output distribution.

Select calibration by K-fold within `calibration_gold`, then refit/freeze. Headline ACCEPT target is ≥95% precision; 99%-precision recall exploratory. Split-conformal/selective-risk claims state exchangeability and no guarantee under generator/project shift; check generator-Mondrian behavior diagnostically.

### 21.11 M4

NL encoder + Lean encoder initialized from M2/M3 with joint/bidirectional attention. Support reference-free `N+C` and reference-aware `N+C` plus separate `R+C` PairRecords. Primary application claim is reference-free reranking.

### 21.12 M5

Use `alpha_structural`/Expr nodes (`forallE`, `lam`, `letE`, `app`, `const`, `fvar`, `bvar`, `sort`, `lit`, `proj`) and parent/child, binder-scope, type-of, application, shared-constant edges. Compare graph-only/text-only/late/cross-modal fusion. Retain only under H3 slice gains.

### 21.13 Hard-negative mining

Mine disagreements only from nonfrozen pools; independently verify; create a new dataset version. Never mine from final/external test labels or derived scores.

---

## 22. Training and experiment discipline

### 22.1 Manifests

Every run records dataset/split manifests; model/tokenizer/views/truncation; sampler/quality weights; optimizer/scheduler/seeds; precision/runtime; loss weights; calibration/evaluation protocol; code/environment revisions. No run mutates a frozen split.

### 22.2 Determinism/restartability

Seed all RNGs; record nondeterministic kernels; checkpoint optimizer/sampler; persist exact IDs; resume deterministically or create a new run ID.

### 22.3 Long inputs

Compare head+tail, section-budgeted binder/hypothesis/conclusion, atom-preserving, and long-context handling. Report truncation by label/source/slice; do not give one representation systematically more content without disclosure.

### 22.4 Hard aborts

Abort if connected components cross protected splits; exact/headless/alpha/near duplicates violate policy; denylisted or prohibited overlap enters supervision; primary judge family appears in weak labels; final-test items enter active learning/prompt logs; manifests disagree; test labels are mounted to training without evaluation-only flag.

### 22.5 Selection order

Weights/hyperparameters: validation. Calibration-method development: pair-level calibration. Final thresholds: K-fold `calibration_gold` real outputs. Final tests only after checkpoint/tokenizer/prompts/calibration/thresholds freeze. Decisions go to reports/decisions or ADRs.

### 22.6 Learning curves

Run multiple scales for deterministic-only, +LLM, +real, all weak, weak+human. H2 uses controlled architecture/training comparisons, not merely a larger final run.

### 22.7 Smoke exemption

`artifact_class=smoke` may use tiny provisional alpha pairs with `resolution_method=smoke_alpha_certificate` solely for plumbing. These artifacts cannot enter release, selection, calibration, or scientific tables.

### 22.8 Pretraining contamination metadata

For judges/encoders record public benchmark/mathlib exposure as unknown/suspected/documented. Risk R26 uses post-cutoff novel items and public-versus-novel deltas; lack of documentation is not evidence of no exposure.

---

## 23. Evaluation plan

### 23.1 Ambiguous policy

Gold terminal ambiguous items are excluded from binary metrics with counts/prevalence reported. Evaluate three-class macro-F1, abstention alignment (share routed REVIEW), and sensitivity with ambiguous→not-same. Unresolved review-route records are workflow volume, not gold test labels.

### 23.2 Headline metrics

Prevalence, AUROC, AUPRC, macro-F1, same-claim precision/recall/F1, Brier, ECE/reliability, and risk/coverage. Headline operating point: coverage/recall at 95% precision with Wilson or Clopper–Pearson interval and denominator. Recall at 99% precision is exploratory.

### 23.3 Relation/error metrics

Relation macro-F1/confusion; directional accuracy only on non-null certified/human truth; swap consistency; ambiguity separately. E01–E30 analysis is reported for data provenance/annotation and any explicitly optional diagnostic ablation, not as a flagship-head requirement.

### 23.4 Robustness

A/B swap, binder/theorem renaming, whitespace/comments, safe regrouping, unseen transforms, held-out projects/generators, long statements, low-overlap positives, high-overlap negatives, semantic-erasure traps, context changes, reference defects, ambiguous NL. Gate 6 swapped-order agreement ≥90% after direction remap.

The sealed anti-shortcut panel crosses faithfulness with structural distance:

| | structurally close | structurally distant |
|---|---:|---:|
| faithful | faithful-close | faithful-distant |
| unfaithful | unfaithful-close | unfaithful-distant |

It contains real autoformalizer outputs, human labels, equivalent alternative
references, same-reference candidate sets, ancestry-disjoint sources, and no
theorem-name/source-file leakage. Report every quadrant and run reference-value
ablations: Lean–Lean correct/equivalent-alternative/unrelated reference, and M4
`N+C`, `R+C`, and `N+R+C` on identical candidates.

### 23.5 Sampling/weighting

Store propensities. Report raw counts/groups plus unweighted/design-weighted metrics. Deployment operating claims use the SRS real-output subpanel or explicit deployment weights. Final sampling cannot depend on any compared model.

### 23.6 External registry

ProofNetVerif, ProofNet#, RLM25, Con-NF, EPLA, CriticLeanBench, ConsistencyCheck, Gaokao-Formal, DriftBench, FormalRx-Test, and compatible miniF2F variants. Pin the observed FormalRx-Test revision/schema/class counts and label availability rather than copying paper/card counts. Run released protocols where possible; document imports/exclusions/migrations/mappings. No test label affects selection/calibration/prompts.

### 23.7 Reranking

Generate K → Lean validate → score compiling → optional cluster → select/REVIEW. Compare first, first compiling, random, generator score, clean LLM judge, judge+symbolic, symbolic/reference methods, M4/M3. Metrics: faithful@1/@k, MRR/nDCG when graded, coverage at 95% precision, no-compiling, abstention, tokens/calls/Lean evidence/latency.

### 23.8 Selective escalation

On a frozen eligible subset, route REVIEW/REJECT cases to an expensive held-out
critic or human and measure final faithful coverage, escalation rate, latency,
and provider calls. LeanFaith does not localize errors or generate repairs.

### 23.9 Statistics and test-size limits

- bootstrap by ancestry connected component/NL problem, never pair;
- report block count/effective class counts;
- paired block bootstrap method differences;
- Holm–Bonferroni α=0.05 for the powered primary H1–H7 family;
- BH FDR q=0.10 for exploratory slices;
- report effect sizes/CIs;
- Wilson/Clopper–Pearson for operating points.

A roughly 500-item test supports aggregate/95%-precision claims only with enough accepts; it does not support precise rare-code/every-domain/99%-precision claims.

### 23.10 Judge circularity/calibration

Freeze judge×supervision matrix; primary comparison uses supervision-free family. Report same-family cells only as circular diagnostics. Pair calibration is development-only; deployment thresholds use `calibration_gold`. Conformal claims state exchangeability and shift caveats.

### 23.11 Preregistered H1–H7 targets

Default `policies/preregistration_v1.yaml`; changing before test unseal requires ADR/new version.

| Claim | Primary comparison | Success threshold |
|---|---|---|
| H1 | M2/M3 vs best fixed non-LLM structural/symbolic on human + real-output | ≥0.03 absolute macro-F1 or AUPRC gain on both; Holm-adjusted paired-block CI excludes 0 on real-output |
| H2 | same architecture full data vs deterministic-only on real-output | ≥0.03 macro-F1 and adjusted CI excludes 0 |
| H3 | representation augmentation on hard-near-miss/heldout-transform/heldout-project | positive on all available, mean gain ≥0.02; aggregate-only insufficient |
| H4/Gate10 | selected model calibration | ECE≤0.05; ACCEPT coverage≥40% at point estimate≥95% precision with interval reported |
| H5 | reranker vs first-compiling and strongest clean baseline | faithful@1 +0.05 vs first-compiling and positive paired-block CI vs strongest clean |
| H6 | held-out generator/project | +0.03 macro-F1 over best clean nontrained baseline on each powered setting |
| H7 (M4-NL only) | M4-NL vs FormalRx-8B on shared human-labeled `N+C` | simultaneous one-sided 95% lower bound for AUPRC difference ≥-0.02; efficiency superiority requires higher median throughput and lower measured peak memory |
| Gate6 | swapped LLM/silver audit | ≥90% agreement after remap |
| Gate7 | annotation pilot | κ≥0.60 and raw≥80%; otherwise revise/repeat |

Underpowered/deferred hypotheses are reported unsupported, not redefined.

H7 is confirmatory only if the frozen test has sufficient ancestry/NL groups
for the interval width to support a 0.02 margin. Otherwise it is descriptive.
No fixed throughput multiplier is required; report the measured ratio.

### 23.12 FormalRx verdict protocol

Direct comparison uses identical natural-language statement `N`, candidate
Lean `C`, and human aligned/misaligned labels for FormalRx-8B, available
1.7B/4B checkpoints, M4-NL, LeanScorer, and a held-out LLM judge.

Pin checkpoint/tokenizer SHAs, the paper prompt, parser, Transformers version,
decoding/stopping parameters, and adapter hash before final-test access.
Preferred probability extraction teacher-forces the exact continuations
`Aligned\n` and `Misaligned\n` after the fixed verdict prefix and normalizes
their sequence log probabilities. Validate the adapter against generated
verdicts before final-test access. If logits are unavailable, report only
discrete verdict metrics; do not manufacture probabilities.

Fit a separate calibrator for each continuous-score system on the same
`calibration_gold`. Primary metrics are AUPRC, accepted precision/coverage,
risk–coverage, and balanced accuracy; additionally report macro-F1, MCC,
AUROC, Brier, NLL, and ECE where supported. Bootstrap ancestry/NL groups.

Measure FormalRx full diagnostic generation and verdict-stop modes, LeanFaith
uncached pairs, and LeanFaith cached-reference reranking. In one frozen
environment and supported numeric precision, report batch 1 and 32, 20 warmup
and 100 timed batches, tokenization separately and end-to-end, latency,
throughput, peak memory, loaded parameters, and checkpoint size. Full
diagnostic generation is a systems cost reference; LeanFaith does not
implement category, localization, or correction outputs.

---
## 24. Implementation roadmap with hard stage gates

### 24.0 Gate semantics and permitted parallelism

A phase may produce quarantined artifacts before a later promotion gate, but those artifacts are not supervised/release-eligible. Every gate writes `reports/gates/gate_<id>.json` with input manifest hashes, checks, results, deviations, and decision.

Ordering carve-outs:

- real-output collection may start after Gate 2 but stays raw/parsed until Phase 5 policy;
- annotation tooling/guidelines may start after Gate 3 in parallel with Phases 4–6;
- positive/negative/silver promotion closes only after the relevant blinded pilot;
- smoke training is allowed only under `artifact_class=smoke`;
- a deterministic producer-shard content-audit merge may omit the merger's
  second full Lean replay only when its distinct manifest fixes
  `merge_replayed_with_lean=false`, `training_eligible=false`,
  `evaluation_eligible=false`, and `gate_credit=false`; it is limited to
  exploratory mining/smoke modeling and never substitutes for scientific merge;
- Phase 6 may be marked `deferred (strong-paper track)` by ADR for an MVP that consumes none of it;
- final tests remain sealed until model/prompt/calibration/threshold freeze.

### 24.0.1 Revision-4.1 next-execution order

1. freeze this Revision 4.1, schema migration, source identity, private-data,
   representation API, FormalRx, and backbone-pilot policies;
2. probe/pin backbone and available FormalRx artifacts;
3. close internal-only Gate 0 and rerun Gate 1, stopping on failure;
4. implement Gate-2 repairs, pass the per-row 100-row regression, then the
   frozen 20,000-row audit;
5. freeze the exact 5,000+5,000 Gate-3 manifest and close Gate 2;
6. implement Gate-3 isolation/fingerprint/audits, pass fixed regressions, then
   the exact 10,000-theorem audit and close Gate 3;
7. update README status and freeze the benchmark/FormalRx-lineage registry;
8. only then begin LF-016–LF-020;
9. collect real outputs/prevalence before LLM mutations and gold partitions;
10. resolve/split/freeze data, run baselines/pilot/M0–M3/calibration/sealed
    Lean–Lean evaluation, then M4 and direct FormalRx comparison;
11. run M5 only after stable text models and preregistered structural evidence.

Each gate is an explicit stop/go condition; a milestone report or fixture-only
run is not gate closure.

### Phase 0 — Lock semantics, sources, providers, and environment

**Tasks**

1. Approve/version all files in `policies/`.
2. Create exact project/source/provider/benchmark configs; no executable placeholder remains.
3. Canonicalize the verified private `sft_classic` revision, schema, counts, and archived 100-row sample; record its undeclared license and internal-only restrictions.
4. Disable unresolved external provider slots until the Phase-5 ADR; private-source content is never transmitted externally under the current policy.
5. Register benchmark identities/usage before generation.
6. Choose one §6.2 mode: a matching in-range Lean/mathlib pair (default) or a matching stable-`v4.31.0` exception pair after the full probe; reject silent toolchain overrides and mathlib `v4.32.0-rc1`.
7. Record environment schema, source choice, encoder pilot, annotation tool, and deferral policy in ADRs.
8. Implement `leanfaith doctor --write-lock`.

**Deliverables**

```text
policies/semantic_policy_v1.md
policies/error_ontology_v1.yaml
policies/label_resolution_v1.yaml
policies/transformation_promotion_v1.yaml
policies/benchmark_denylist_v1.yaml
policies/evidence_policy_v1.yaml
policies/split_policy_v1.yaml
policies/calibration_policy_v1.yaml
policies/preregistration_v1.yaml
policies/private_source_use.yaml
policies/model_selection.yaml
policies/formalrx_comparison.yaml
policies/formalrx_sci_crosswalk_v1.yaml
configs/environment.lock.yaml
configs/projects/
configs/sources/
configs/generation/providers.yaml
configs/benchmarks/registry.yaml
data/source_manifests/<primary_nl_source>.json
docs/adr/ADR-0001-environment-lock.md
reports/milestones/phase_0_contract.md
```

**Gate 0**

Gate 0 may close for internal research only after the source record contains:

```text
access_basis
institutional_policy_status
license_status = undeclared
redistribution = false
external_transmission = false
release_eligibility = false
```

The verified private revision is canonical, stale `probe_status` is removed,
unresolved providers are disabled, `sft_classic` content cannot be sent to an
external provider, and a public-source replication profile exists. Exact
Lean/project locks, benchmark registry, source schema/counts/sample, and pool
adequacy are recorded. FormalRx artifact availability does not block this
gate. Gate artifacts are finalized first, then the phase report, then its
hash, then the gate report; changing any input invalidates and regenerates the
gate.

### Phase 1 — LeanInteract backend vertical slice

**Tasks**

1. First task before any LeanInteract-importing code: executable API-shape verification of every Appendix A symbol/signature/field. (The LeanInteract-free protocol module of LF-005 may precede the probe.)
2. Implement Appendix A.5 protocol in `lean/protocol.py`.
3. Implement project registry/context identity/response normalization.
4. Implement `Command`/`FileCommand`, explicit `allow_sorry`, raw response storage, per-item exception normalization.
5. Implement stable `LeanServer` and tested experimental `AutoLeanServer` mode.
6. Implement pool/worker path, recovery, bounded retry, memory-product checks.
7. Build fixture/meta-helper smoke and §8.12 tests.

**Deliverables**

```text
src/leanfaith/lean/protocol.py
src/leanfaith/lean/leaninteract_backend.py
src/leanfaith/lean/project_registry.py
src/leanfaith/lean/response_normalization.py
src/leanfaith/lean/session_policy.py
src/leanfaith/cli/doctor.py
tests/integration/leaninteract/
tests/lean_fixtures/
artifacts/golden/leaninteract/
reports/compatibility/leaninteract_api.json
reports/milestones/phase_1_leaninteract.md
```

**Gate 1**

All §8.12 tests pass; every request has one terminal ordered result; explicit placeholder behavior works; toolchain/memory checks work; stable/approved experimental modes normalize equivalently; only the backend imports LeanInteract.

### Phase 2 — Source extraction and benchmark freeze

**Tasks**

1. Implement probe/manifest framework.
2. Implement full adapters for mathlib, selected primary NL source, ProofNetVerif.
3. Probe CSLib/Physlib revisions, roots, toolchains; defer full adapters.
4. Archive raw and parsed partitions.
5. Implement locator-only source IDs, question-first extraction, fallback eligibility, and complete row/declaration terminal accounting.
6. Extract/revalidate declarations through LeanInteract; persist every failure and compute context/theorem/ancestry/content IDs separately.
7. Compute Phase-5 pool adequacy.
8. Freeze benchmark IDs, normalized-NL hashes, and raw-text hashes before Phase 4 (representation-hash signatures are appended in Phase 3 per §19.4).
9. Run reload test and configure separate nightly re-elaboration.

**Deliverables**

```text
src/leanfaith/sources/
src/leanfaith/lean/extraction.py
configs/sources/
data/source_manifests/
data/raw/sources/
data/parsed/sources/
data/extracted/theorems/
data/extracted/failures/
data/benchmarks/frozen_ids.json
data/benchmarks/source_registry.yaml
reports/source_probes/
reports/milestones/phase_2_extraction.md
```

**Gate 2**

Unit tests cover locator identity, duplicate upstream UUIDs, content changes,
question-first routing, fallback-only NL ineligibility, question/fallback
mismatch, malformed/multiple/missing fences, deterministic declaration
selection, all execution failures, row/declaration terminal accounting,
non-null trust/source links, dirty-code rejection, and stable extraction,
benchmark-freezing, and representation CLI commands.

The immutable archived 100-row regression requires the registered input hash,
100 terminal outcomes, exactly 85 question-route theorem successes, 10
fallback-only theorem successes, and five terminal failures. One failure is
the elaborating but non-`Prop` `def` at archived row 64; the Gate must not
count it as a theorem merely to reproduce the earlier 96-row elaboration
union. A versioned per-row expected-outcome file controls changes; no success
or signature may drift silently.

The scale audit uses a frozen hash-selected 20,000-row sample stratified only
by pre-extraction fields: split, `valid`, recorded NL provenance, token/tactic
bands, docstring presence, and duplicate-UUID status. Extraction route is an
outcome, never a sampling stratum. Require 100% replay of source IDs, raw and
content hashes, parser outcomes, and final normalized outcomes after the
fixed retry policy; exact row/declaration reconciliation; nonempty
input/output/failure/config/environment/code hashes; and non-null trust/source
links on every accepted record. The 99.5% re-elaboration statistic remains a
separate non-gating nightly report.

Gate 2 closes only after unit/fixed/scale checks pass and an immutable manifest
freezes exactly 5,000 mathlib plus 5,000 `sft_classic` transform-eligible
theorems. No post-freeze denominator change is allowed. Gate-closing code is
clean or archived as a complete content-addressed source bundle, and the final
phase-report hash precedes the gate report. **STOP: LF-016 remains prohibited.**

Scale extraction uses content-bound completed-chunk markers. A resumed chunk
must match its frozen rows/files, context, adapter version, code tree, code
bundle, and relevant execution configuration exactly; otherwise resume fails
closed. Completed partitions merge only in frozen input order.

### Phase 3 — Representations and identity stress tests

**Tasks**

1. Implement required RepresentationRecord views/statuses.
2. Implement Lean helpers only through LeanInteract.
3. Implement semantic atoms/operator tree/serialization/hashes.
4. Implement `RepresentationBatch`/`RepresentationBatchResult`, per-theorem request isolation, inline inspection, and independent view retry.
5. Implement a property-test-only renamer distinct from P01 and the binder-normalized identity fingerprint.
6. Run the exact frozen 10,000-theorem manifest for collision/invariance/coverage audit with no denominator filtering.
7. Audit proof leakage, lossy collisions, name-versus-inline exact-type alias paths, non-gating pretty-print round-trip diagnostics, and context attachment.
8. Append representation-based near-duplicate signatures to the frozen benchmark registry (§19.4).
9. Start annotation tooling/guidelines after gate.

**Deliverables**

```text
src/leanfaith/representations/
LeanFaith/Meta/Extract.lean
LeanFaith/Meta/Fingerprint.lean
LeanFaith/Meta/SemanticAtoms.lean
data/representations/
tests/property/
reports/representation_collisions_mvp.md
reports/milestones/phase_3_representations.md
```

**Gate 3**

API/unit regressions require empty success, pre-execution mixed-context
rejection, valid-plus-nonexistent sibling preservation, deterministic
theorem/view request IDs, independent view failures, same-request inline
declaration/inspection, and explicit per-view failure records. The MVP uses
one independent LeanInteract request per theorem; it does not concatenate
multiple theorem checks. Recursive bisection is a later optimization only.

Fixed fixtures cover valid/nonexistent, malformed, empty/mixed context, inline
and name-based, identical-signature/different-proof, unique proof sentinel,
and alpha-renamed declarations. Proof differences must yield byte-identical
model views and the sentinel must occur in none.

On the exact frozen 10,000 records, per source and overall require:

```text
raw_proof_stripped 100%    headless 100%
signature_pp ≥99%          signature_explicit ≥99%
semantic_atoms ≥99%       operator_tree ≥98%
```

Every missing view remains an explicit failure/missingness record. Require
1,000/1,000 alpha-renaming fingerprint invariance; zero cryptographic or
canonical-alpha-byte collisions; all lossy-view clusters enumerated with
deterministic reason codes and `min(200, cluster_count)` manually audited;
zero proof leakage; 500 mathlib name-versus-inline comparisons using audit-only exact-type aliases, with identical alpha fingerprints and explicit signatures after first-occurrence normalization of Lean-generated ``u_<n>`` placeholders; and 100% representation-ID/hash replay. The pretty-printed explicit view is not treated as a lossless Lean serialization. Full `alpha_structural` and graph views remain deferred. The final
phase-report hash precedes the gate report. **STOP: LF-016 remains prohibited
until this gate and Gate 2 both close.**

For the current Gate-3 closure evidence, the frozen execution profile is one
worker, sequential Run A then Run B, chunk size 500, and a Linux per-REPL
`memory_hard_limit_mb` of 49,152. Both replay runs must be homogeneous under
the same code bundle and execution configuration; chunks from earlier failed
profiles cannot be reused. These values document this measured gate-closing
run and are not a universal worker-count, RAM, or hardware prescription.

The historical Gate-3 decision is preserved in `reports/gates/gate_3.json`.
The current `repr_v3` revalidation passed separately in
`reports/gates/gate_3_repr_v3.json`; it authorizes `repr_v3` for new scientific
artifacts without mutating or relabeling historical `repr_v2` records.

### Phase 4 — Deterministic generation and promotion

**Tasks**

1. Implement §15.2 protocol/common validation/audit.
2. Implement scoped P01/P02/P04-lite/N01/N02/N03/N07/N10; stubs cannot run.
3. Generate seeded drafts with applicability/failure logs.
4. Re-elaborate and create variant/theorem/representation/pair records.
5. Collect atom/structural/round-trip/certificate evidence.
6. Run bounded `decide`-able counterexample search best effort.
7. Export blinded audit samples.
8. Freeze family registry.

**Deliverables**

```text
src/leanfaith/transforms/
configs/transformations/registry.yaml
configs/transformations/v1.yaml
data/generated/deterministic/
data/evidence/
reports/transformation_audits/
reports/milestones/phase_4_transforms.md
```

**Gate 4G — generation**

Active families are deterministic, elaborating, provenance-complete; disabled stubs cannot execute; N10 includes both ancestries; no intention is resolved label; every candidate has validation/audit status.

Gate 4G closes only after one persisted integrated run exercises all eight
active v1 families, re-elaborates every accepted candidate, links every pair
to transformation-audit evidence, preserves N10's two source ancestries, and
passes deterministic replay and the smoke release/selection guards. The
integrated run must use at least ten accepted fixture-source statements, must
persist every expected failure, and must demonstrate zero protected-benchmark
overlap and zero connected-component split leakage. The LF-019 smoke
exemption may resolve only P01 alpha-renaming pairs through
`smoke_alpha_certificate`; P02, P04-lite, and all five negative-family pairs
remain unresolved. Intentions never become labels, no gold label or family
promotion is created, and the gate-closing report must bind the current
effective registry snapshot. A clean-checkout replay is required.

**Current status:** Gate 4G passed on 2026-07-23. The fail-closed report is
`reports/gates/gate_4g.json` (SHA-256
`5c1f2e86230a8b7ebf884d9f10369a504bc5cbda4bc472321076c178a2cf43f7`).
It binds two clean, semantically identical LF-019 replay runs and the finalized
milestones. Gates 4A and 4B remain open; no gold label or family promotion was
created.

**Gate 4A — positive gold promotion**

After annotation pilot, each family/version has blinded `n≥200`, point precision≥99%, 95% Clopper–Pearson lower bound≥95%, all invariants, no recurrent semantic erasure, and held-out source/domain audit. Else remain silver/experimental/disabled.

**Gate 4B — negative supervised promotion**

After pilot, each promoted item uses exactly one §15.7 route and corresponding tier. Family refinement audit is 50–100; no intention-confidence shortcut. Others remain provisional.

**Post-Gate-4G deterministic-v2 track**

The additive §15.10–§15.13 track may design, coverage-probe, implement, and
quarantine new family versions after LF-016–LF-020 and historical Gate 4G are
accepted. It does not reopen or rewrite the eight-family Gate-4G decision.
Before any v2 family is activated, a new version-specific generation addendum
must bind its profile/registry hashes, clean replay, failure accounting,
LeanInteract validation, overlap ownership, and split/denylist audits. The
addendum grants mechanical generation credit only; Gates 4A/4B and every
promotion route remain unchanged.

V2 configs and artifacts use new paths and IDs. They cannot be mixed into a v1
run, relabel a v1 pair, or reuse an accepted v1 audit as family-level evidence.
The v2 track is permitted in parallel with genuine LF-021 annotation because
all v2 outputs remain quarantined and it does not satisfy or block Gate 5.

### Phase 5 — Real autoformalization outputs and prevalence

**Tasks**

1. Build trusted problem pool excluding denylist/near duplicates.
2. Configure at least three distinct successful families for collection; use
   at least four for a confirmatory run that reserves one held-out while
   retaining three supervision-eligible families.
3. Generate multiple candidates/seeds/prompts; archive calls/retries.
4. Parse/Lean-validate/cluster/deduplicate.
5. Keep noncompiling only for failure analysis.
6. Build candidate-reference PairRecords and NLPLeanRecords.
7. Freeze the 200–300-item human prevalence sampling frame and propensities
   across generator/domain; the annotation track supplies adjudicated labels.
8. Report coverage/diversity/leakage/pool adequacy.

**Deliverables**

```text
configs/generation/problem_pool_v1.yaml
configs/generation/real_outputs_v1.yaml
data/raw/real_outputs/
data/parsed/real_outputs/
data/real_outputs/validated/
reports/generation_coverage.md
reports/faithful_prevalence_design.md
reports/milestones/phase_5_real_outputs.md
```

**Gate 5G — generation checkpoint**

At least three successful families; configured per-stratum unique-compiling
coverage met; calls replayable; benchmark/ReForm×Lean-Workbook checks pass;
and the prevalence frame, strata, and sampling propensities are frozen. If
only three families are available, the output is marked
`reduced_data_ablation` for the confirmatory D4/D5 and
`heldout_generator_test` claims.

**Gate 5 — final real-output gate**

Gate 5G has passed; the prevalence frame has 200–300 adjudicated human labels
and a frozen prevalence report; and downstream test/headroom planning binds
that report. A confirmatory D4/D5 or held-out-generator claim additionally
requires at least four successful families, with at least three
supervision-eligible families satisfying the per-family cap and one fully
reserved family absent from all supervision. Generation completion alone
cannot close the human-label portion of Gate 5.

**Current implementation status (2026-07-24):** LF-021 mechanical collection
and Gate 5G are complete. The exact mixed lineage covers 12 original and 4
post-exhaustion tranches, with 1,440 terminal invocations, 299
compile-and-benchmark-clear members, 49 duplicate members, and 250 unique
problem-aware eligible units. The frozen prevalence frame contains 240
unresolved `REVIEW` items over 31 strata: 67 Goedel, 108 Kimina, and 65
StepFun; 149 are from the algebra pool and 91 from the cross-domain pool. The
lineage is
`lf021_gate5g_lineage:ddac5e106c92b263ed96c9974eeadcef25f4980f1e777b06bca75de133b0aa1d`
with SHA-256
`a2bb9dba960a7906057647162a6ba00e17f26d0aa89180940e9e6112138ca761`.
Gate 5G established mechanical collection, replay, lineage, and benchmark
clearance only; it created and inspected no semantic labels and grants no
supervision eligibility. Supplemental public Kimi/Qwen/Codex qualifications
remain non-gating. The next scientific step is human annotation and
adjudication of the frozen frame, followed by the prevalence report and Gate
5 closure. Do not collect another tranche. Canonical evidence is in
`reports/generation_coverage.md`,
`reports/milestones/phase_5_real_outputs.md`, and
`reports/gates/gate_5g.json`.

### Phase 6 — LLM variants and silver supervision

May be deferred for MVP by ADR; no LLM-silver claim then.

**Tasks**

1. Implement canonical prompts/parsers.
2. Generate equivalent, directional, E-code, semantic-erasure, and minimal edits.
3. Validate/deduplicate normally.
4. Collect two-family blinded judgments, swapped copies, trap/calibration items.
5. Enforce family separation/cross matrix.
6. Retain disagreement and route to annotation.
7. Build promotion strata/capped audit.

**Deliverables**

```text
prompts/proposers/
prompts/judges/
configs/generation/llm_variants_v1.yaml
configs/judges/weak_supervision.yaml
data/generated/llm/
data/raw/judgments/
data/labels/silver/
reports/judge_calibration.md
reports/milestones/phase_6_llm_data.md
```

**Gate 6G — generation/judging**

All calls parse or retain failure; candidates validate/quarantine; order randomized; intentions remain provenance; primary eval judge absent from weak labels.

**Gate 6 — silver promotion**

After pilot, promoted strata satisfy consensus/audit policy; the §17.9 capped base sample `min(ceil(0.20×N),1000)` contains all feasible preregistered per-stratum minima and the required disagreement/threshold oversampling; swapped agreement≥90%; disagreement is retained; tier `silver_consensus` remains distinct from gold. A stratum whose minimum cannot fit the capped design is not promoted.

### Phase 7 — Annotation pilot and guideline freeze

**Tasks**

1. Complete Argilla/Label Studio integration or documented fallback.
2. Run first 100 Lean–Lean round and fix defects.
3. Run cumulative 200–400 Lean–Lean + 100–200 NL–Lean.
4. Double-label/adjudicate/preserve raw decisions.
5. Audit active transformation/silver strata blindly.
6. Freeze ambiguity/error/escalation rules and guideline.

**Deliverables**

```text
annotation/guidelines_v1.md
annotation/templates/
data/human/pilot_raw/
data/human/pilot_adjudicated/
reports/human_pilot.md
reports/milestones/phase_7_human_pilot.md
```

**Gate 7**

Raw agreement≥80%, κ≥0.60, required rationales/fields complete, recurring disagreement has rule/route, blinding and serialization pass. Failure means revise/repeat.

### Phase 7b — Main annotation campaign

**Tasks**

1. Freeze frames/propensities.
2. Produce ancestry-disjoint `training_gold` for weight training only.
3. Produce ancestry-disjoint `selection_gold` for model/data/representation/checkpoint selection only.
4. Produce real-output-only `calibration_gold` for calibrator/threshold fitting only.
5. Construct/seal `final_human_test`, including an SRS real-output subpanel.
6. Maintain reference/ambiguity audits.
7. Publish label-source/overlap manifests without exposing labels.

**Deliverables**

```text
data/human/training_gold/
data/human/selection_gold/
data/human/calibration_gold/
data/human/final_human_test/
annotation/adjudication/
reports/annotation_main.md
reports/milestones/phase_7b_main_annotation.md
```

**Gate 7b**

All records have groups/provenance/propensities/adjudication/canonical ambiguity. `training_gold`, `selection_gold`, `calibration_gold`, and `final_human_test` are connected-component disjoint and used only for their declared purposes. Calibration is restricted to the real-output distribution, final labels are sealed, and all subset/disjoint relations match §19.6 manifests.

### Phase 8 — Resolve labels, split, freeze dataset v0

**Tasks**

1. Deterministic resolver/conflict/review routing.
2. Mechanical F0/F1/F2 checks.
3. Union-find components from split groups.
4. Development random fractions and manifest-built tests.
5. Exact/near/denylist/judge/ReForm/final contamination checks.
6. Export gold-only, gold+silver, deterministic-only, real-output-only views.
7. Freeze manifests/checksums/schemas/data card.

**Deliverables**

```text
src/leanfaith/labeling/
src/leanfaith/datasets/
configs/splits/v0.yaml
data/labels/resolved/
data/split_manifests/
data/releases/v0/train.parquet
data/releases/v0/validation.parquet
data/releases/v0/calibration.parquet
data/releases/v0/internal_test.parquet
data/releases/v0/manifests/
data/releases/v0/DATA_CARD.md
reports/contamination_v0.md
reports/milestones/phase_8_dataset_v0.md
```

**Gate 8**

No connected/prohibited duplicate leakage; every supervised record has allowed tier/canonical label; ambiguous masking correct; evaluation manifests include source/propensity; all artifacts hash/round-trip/rebuild.

### Phase 9 — Baselines

**Tasks**

Implement simple/scalar/symbolic/structural/embedding; distinct BEq/BEq+;
reproducible GTED/TransTED/FormalAlign/CriticLean/LeanScorer; freeze the
FormalRx verdict adapter and primary LLM/judge+symbolic prompts; measure
quality/coverage/calibration/cost; emit common output. FormalRx predictions in
this phase are produced only for records with the shared `N+C` inputs and are
not a direct M0–M3 quality comparison.

**Deliverables**

```text
src/leanfaith/baselines/
configs/baselines/
artifacts/predictions/baselines/
reports/baselines.md
reports/milestones/phase_9_baselines.md
```

**Gate 9**

Common contract/frozen IDs; naming/provenance explicit; primary judge supervision-free; fair cost; reproducible without test-label access at prediction time.

### Phase 10 — M0–M3, calibration, model freeze

**Tasks**

1. Freeze the tokenizer/backbone pilot inputs and protocol in ADR-0004.
2. Run the four-candidate audit/pilot and record the deterministic winner.
3. Train M0/M1/M2/M3 using the selected backbone where applicable.
4. Include `configs/models/m3.yaml` and amortized evidence cost.
5. Run D0–D5 learning curves/hard-negative mining on nonfrozen pools.
6. Train ambiguity/relation heads, optional F0/F2 auxiliaries, and three-class ablation.
7. Run exact swap-invariance/equivariance and H3 slice tests.
8. Select models/checkpoints using `selection_gold` only.
9. K-fold `calibration_gold`, fit/freeze deployment calibration/thresholds.
10. Freeze checkpoint/tokenizer/prompts/policy before final tests.

**Deliverables**

```text
configs/models/m0.yaml
configs/models/m1.yaml
configs/models/m2.yaml
configs/models/m3.yaml
src/leanfaith/models/
artifacts/checkpoints/m0/
artifacts/checkpoints/m1/
artifacts/checkpoints/m2/
artifacts/checkpoints/m3/
artifacts/calibration/
reports/tokenizer_audit.md
reports/model_selection.md
reports/milestones/phase_10_lean_lean_models.md
```

**Gate 10**

Leakage/swap/ambiguity checks pass; selected model, tokenizer, prompts, calibration method, and thresholds are frozen. Development-only H2 analyses and the Gate-10/H4 calibration criteria in §23.11 are evaluated without final-test access. H1, the final H2 claim, and all sealed-test comparisons remain pending until Phase 11.

### Phase 11 — Sealed Lean–Lean/external evaluation

**Tasks**

Verify hashes/freeze; on the strong-paper track, first implement CSLib/Physlib adapters, extract/label held-out-project items, and freeze the `heldout_project_test` manifest before unsealing; run human/real/benchmark and available held-out/adversarial sets; apply ambiguity/weighting/group bootstrap/Holm/BH/test-size policy; run registered external suites; produce contamination/public-vs-novel/cost reports; no tuning.

**Deliverables**

```text
artifacts/predictions/final/
reports/evaluation_final.md
reports/external_benchmarks.md
reports/statistics_primary.md
reports/milestones/phase_11_final_evaluation.md
```

**Gate 11**

Predictions match frozen manifests/configs; ambiguity/weights/groups/corrections/exclusions explicit; no test-derived model/prompt/threshold change.

### Phase 12 — M4 and downstream reranking

**Tasks**

Build M4-NL on `N+C` and reference-aware M4 on `N+R+C`; train allowed data only; freeze problem/candidate manifests; compare §23.7 baselines; perform the §23.12 direct FormalRx verdict comparison only for shared `N+C`; run the reference-value experiment; measure prevalence/headroom/faithful@1/coverage/no-compiling/abstention/selective-escalation/cost.

**Deliverables**

```text
configs/models/m4.yaml
artifacts/checkpoints/m4/
artifacts/predictions/reranking/
reports/reranking.md
reports/selective_escalation.md
reports/milestones/phase_12_nl_lean_reranking.md
```

**Gate 12**

H5 and, when powered, H7 are evaluated as preregistered; generation/selection labels are disjoint; M4-NL has no reference leakage; FormalRx shares exact `N+C` inputs/labels; reference-aware comparison is marked unequal-information; prevalence/headroom/cost complete; failure is reported without candidate-set changes.

### Phase 13 — M5 Expr graph

**Tasks**

Stabilize graph extraction/cache; train graph-only/text+graph controlled conditions; evaluate hard/held-out/long/dependency slices; measure overhead/failures; retain/drop under H3.

**Deliverables**

```text
configs/models/m5.yaml
src/leanfaith/models/m5_graph.py
artifacts/checkpoints/m5/
reports/graph_extension.md
reports/milestones/phase_13_graph.md
```

**Gate 13**

Retain only if preregistered H3 slice rule passes with reproducible gains/coverage. Null result is valid and does not block core project.

### Phase 14 — Release and paper artifact

**Tasks**

Freeze code/environment/data/model/eval manifests; produce data/model cards, protocol, transformation catalog, guidelines, experiment registry, limitations/contamination statement; release only permitted content; run clean smoke/table reproduction; enable DVC for research_v1; verify end-to-end traceability.

**Deliverables**

```text
data/releases/research_v1/
artifacts/release/
docs/reproducibility.md
docs/limitations.md
reports/release_validation.md
reports/milestones/phase_14_release.md
```

**Gate 14**

Appendices G/H pass; clean reconstruction succeeds; every headline number traces to frozen predictions/config/manifests; licenses/terms respected; unsupported/deferred claims explicitly scoped.

---
## 25. Coding-agent operating contract

1. Work one backlog item/gate at a time; do not silently begin dependent work.
2. Read `PLAN.md`, relevant policy/config/schema, and previous gate report first.
3. Use LeanInteract exclusively for semantic Lean operations.
4. Add tests before or with implementation; preserve raw inputs/failures.
5. Never infer labels from intentions, compilation, failed proof search, or missing counterexamples.
6. Never alter canonical enums/paths/schemas without updating this plan, migrations, and tests.
7. Commands must be deterministic, resumable, manifest-writing, and fail closed on hash/schema mismatch.
8. No production path reads final labels during training/prompt selection.
9. Every implementation PR/change report states scope, files, commands, tests, artifacts, deviations, and next gate.
10. When blocked by missing external access/API, emit a structured blocked artifact and follow the declared fallback; do not fabricate data or APIs.
11. Direct shell Lean use is diagnostic-only and cannot become a hidden backend.
12. Smoke exemptions must carry `artifact_class=smoke` and release guards.
13. Deferred strong-paper phases require ADR naming blocked claims/artifacts/re-entry gate.
14. Do not add staffing/timeline/budget/hardware prescriptions.
15. Never send private-source content to an external provider without the §9.2 approval record.

---

## 26. Initial coding-agent backlog

Each item closes only with code, tests, artifacts, and acceptance evidence. Items are ordered; an item may not start before its predecessors' acceptance evidence exists. Parallel-track exceptions (matching §24.0): LF-021 may begin once LF-001–LF-013 are accepted (post-Gate-2 quarantined real-output collection); LF-023 may begin once LF-001–LF-015 are accepted (post-Gate-3 annotation tooling); and LF-031 may begin once LF-016–LF-020 and historical Gate 4G are accepted. LF-032 follows LF-031, LF-033 follows LF-032, and LF-034 follows LF-031 plus the frozen overlap/E-code design; this v2 track may run alongside LF-021–LF-030 but grants no labels, promotion, Gate-5 closure, or training readiness.

1. **LF-001 — scaffold/tooling:** pyproject/uv/Typer/Ruff/Pytest/mypy/pre-commit; strict core modules.
2. **LF-002 — config loader:** strict schemas, hashes, secret references (including `HF_TOKEN`), unknown-key failure.
3. **LF-003 — IDs/manifests:** canonical JSON, content hashes, run/output manifests, migration map.
4. **LF-004 — canonical records:** implement §11 modules, semantic/evidence target-kind integrity, reverse label links, and cross-record invariants.
5. **LF-005 — backend protocol:** exact Appendix A.5 contract; no LeanInteract imports above adapter (LeanInteract-free, so it may precede the LF-006 probe; the probe must precede LF-008).
6. **LF-006 — API probe:** introspect imports/signatures/defaults/fields; write compatibility artifact.
7. **LF-007 — project/context registry:** supported-range/toolchain/revision/context hash doctor.
8. **LF-008 — LeanInteract adapter:** Command/FileCommand/raw response/explicit placeholder/status mapping.
9. **LF-009 — server lifecycle:** stable/experimental/pool/retry/recovery/memory checks.
10. **LF-010 — source probe framework:** revisions/licenses/schema/sample/archive/fallback.
11. **LF-011 — MVP adapters:** mathlib, selected NL source, ProofNetVerif.
12. **LF-012 — declaration extraction:** ranges/proof strip/revalidation/failure records.
13. **LF-013 — benchmark freeze:** source IDs, normalized-NL and raw-text hashes before generation; representation-hash signatures appended by LF-014.
14. **LF-014 — representations:** required views/statuses/hashes/option profile; appends representation-based near-duplicate signatures to the benchmark registry (§19.4).
15. **LF-015 — semantic atoms/operator tree:** versioned helpers/golden tests.
16. **LF-016 — transform protocol:** Applicability/VariantDraft/Audit/registry/promotion.
17. **LF-017 — scoped positives:** P01/P02/P04-lite + invariants/round trips.
18. **LF-018 — scoped negatives:** N01/N02/N03/N07/N10 with the curated replacement table; produces the pre-scale audit slice.
19. **LF-019 — smoke vertical slice:** all-eight-family fixture→records→pair/evidence→P01-only smoke resolution→connected split→tiny model; release guard; runs only after LF-016–LF-018 exist.
20. **LF-020 — evidence pipeline:** defeq/directional/counterexample/certificate/axiom cache. Complete with a two-run, clean-cache semantic replay bound by `reports/evidence/lf020_smoke_replay_v1.json`; no labels or promotions were created.
21. **LF-021 — real-output collection:** mechanical collection and Gate 5G complete; 16 immutable tranches contain 1,440 terminal invocations, 299 compile-and-benchmark-clear members, 49 duplicates, and 250 unique problem-aware units. A 240-item, 31-stratum frame is frozen under the three-family reduced-scope policy. LF-021 remains open only for genuine human annotation, adjudication, prevalence reporting, and Gate 5 closure. No further collection tranche is authorized.
22. **LF-022 — LLM variants/judges:** prompts/parsers/family separation/call records.
23. **LF-023 — annotation integration:** blind templates/export/import/adjudication/agreement.
24. **LF-024 — resolver:** precedence/conflicts/review/F0-F2/quality tiers.
25. **LF-025 — split builder:** ancestry/group union-find/denylist/propensities/manifests.
26. **LF-026 — dataset freeze:** views/cards/checksums/rebuild and contamination report.
27. **LF-027 — baseline suite:** common output, symbolic/structural/critics/clean LLM.
28. **LF-028 — M0–M3:** tokenizer audit, models, heads, swap, hybrid, calibration development.
29. **LF-029 — M4/application:** NL–Lean, final calibration, frozen reranking and selective escalation.
30. **LF-030 — M5/external/release:** graph experiment, CSLib/Physlib adapters + `heldout_project_test` construction (strong-paper track), sealed suites, statistics, artifact assembly.
31. **LF-031 — deterministic-v2 contract:** freeze `configs/transformations/v2.yaml`, disabled candidate registry entries, E0/E1/E2/D0 certificate contracts, coverage probes, family-overlap ownership, held-out-mechanism design, and a generation-addendum schema; no new family becomes executable in this item.
32. **LF-032 — first v2 conservative positives:** implement P11/P06/P07/P09/P10/P12 as separately switchable experimental E0 families with exact whole-type identity, family-specific inverses, atom alignment, fail-closed context checks, and clean replay evidence. They remain provisional pending Gate 4A.
33. **LF-033 — secondary/provisional v2 positives:** implement and independently evaluate capped P05/P08 surface ablations, then separately study P13 and human-audited P14–P17 without introducing an automatic-positive shortcut or broadening an existing family version.
34. **LF-034 — v2 negative/directional candidates:** implement N11 first, then N12–N17 after overlap ownership and E-code mappings are accepted; store atom/role/scope/dependency deltas and structural direction only as evidence until an existing §15.7 promotion route resolves an item.

Acceptance for every LF item: declared paths exist; unit/integration/golden/property tests relevant to scope pass; mypy/Ruff pass on touched core; command writes manifest; failure paths tested; milestone/gate report updated; no forbidden label inference.

---

## 27. Test and continuous-integration strategy

### 27.1 Layers

- unit: schemas, IDs, mappings, configs, resolver, metrics;
- property: alpha/capture/dependency/ID/round-trip/swap invariants;
- golden: LeanInteract responses, extraction, representations, transforms, prompts;
- integration: fixture project, source samples, provider mocks, annotation import/export;
- end-to-end: smoke pipeline and restart/rebuild;
- compatibility: toolchain/API upgrade diffs;
- data audits: leakage, denylist, split groups, propensities, quality/promotion.

The LF-019 smoke slice is exempt from scientific-label prerequisites only under its explicit artifact restrictions.

### 27.2 CI tiers

1. PR-Python: no Lean; schemas/unit/mypy/Ruff.
2. PR-Lean: fixture project only; no mathlib.
3. nightly-mathlib: prebuilt mathlib container/cache; sample extraction/representation/evidence.
4. weekly-data: manifests/reload/splits/leakage/rebuild.
5. release: full environment/source/model/eval/reproduction checks.

No runner/hardware mandate is implied.

### 27.3 Required invariants

Even code fences; unique schema definitions; Appendix A.5/§8.4 parity; supported toolchain; explicit placeholder flag; one result/request; deterministic IDs; no proof leakage; no intention→label; no search-failure→negative; connected split isolation; primary judge separation; smoke release rejection; phase paths declared; no stale enum/path/model aliases.

### 27.4 Upgrade testing

Dependency/toolchain changes run API/golden diff, 1,000-record extraction/representation/evidence comparison, cache invalidation, and migration report. Pretty-printer change is accepted only with representation-version/hash change and deliberate regeneration.

---

## 28. Operational, measurement, and reproducibility controls

### 28.1 Canonical config inventory

The §7 tree is authoritative:

```text
environment.lock.yaml
projects/{fixtures,mathlib,cslib,physlib}.yaml
sources/{mathlib,sft_classic,sft_classic_numina,lean_workbook,
         proofnetverif,cslib,physlib}.yaml
generation/{providers,problem_pool,real_outputs,llm_variants}.yaml
judges/{weak_supervision,primary_eval}.yaml
transformations/{registry,v1,v2,replacement_table_v1,
                 lf018_pre_scale_v1,lf019_positive_fixtures_v1,
                 lf019_smoke_v1,
                 p01_alpha,p02_binders,p04_notation_lite,
                 n01_operator,n02_quantifier,n03_drop_hypothesis,
                 n07_literal_bound,n10_nearby_theorem}.yaml
transformations/v2/{p05_resolved_names,p06_implicit_arguments,
                    p07_coercion_surface,p08_type_ascription,
                    p09_projection_direct,p10_constructor_direct,
                    p11_bounded_quantifier,p12_proof_arrow_binder,
                    p13_restricted_eta,p14_binder_permutation,
                    p15_root_iff_reversal,p16_conjunction_reassociation,
                    p17_hypothesis_packing,n11_bound_variable,
                    n12_implication_converse,n13_witness_dependency,
                    n14_negation_scope,n15_conjunct_omission,
                    n16_domain_guard,n17_role_arguments}.yaml
evidence/{portfolio_v1,counterexample_v1,sampling_v1}.yaml
annotation/{tool,pilot,main}.yaml
splits/v0.yaml
benchmarks/registry.yaml
baselines/*.yaml
models/{m0,m1,m2,m3,m4,m5}.yaml
evaluation/{primary,external,reranking}.yaml
```

### 28.2 Run manifest

`runs/<run_id>/manifest.json` stores run/artifact class, command/code dirty state, config/input/output hashes, environment/context/project versions, seeds/server/workers/timeout/memory config, source/provider/model/prompt revisions, status counts/retries, token/call/cost/elapsed measurements, tracker ID/offline artifact, and parent/resume pointers.

### 28.3 Measurement

Record tokens/calls/retries/latency/cache hits/Lean requests/evidence attempts/provider cost and stage/stratum throughput. Scale increases require measured diversity/learning/coverage benefit, not merely available tokens.

### 28.4 Caches/partial results

Content-addressed keys include environment schema, context, exact input, method/config/version. Corruption/schema mismatch fails closed. Retries append lineage rather than overwrite raw failures.

### 28.5 Version control

Content-hash manifests remain authoritative. DVC starts at research_v1 for storage pointers. W&B default supports offline/export.

### 28.6 Privacy/licenses/terms

Store/release only permitted content; redact secrets/unneeded personal data; otherwise release IDs/hashes/adapters/traces/permitted metadata. Every source/model/provider manifest records redistribution status. Private or gated source content — including NL statements — is never sent to an external LLM provider without the §9.2 approval record naming the provider set and scope.

### 28.7 Failure rates

Report parser/elaboration/server/view/family/provider/tactic/source failure denominators. Never hide failures by filtering them before rate computation.

---

## 29. Risk register with triggers and mandatory responses

| ID | Risk/trigger | Mandatory response |
|---|---|---|
| R01 | synthetic→real gap | increase real-output/dev gold; report mixture ablation |
| R02 | transformation shortcut learning | hold out families; adversarial/minimal real tests |
| R03 | positive semantic erasure | quarantine family; atom/roundtrip audit; human review |
| R04 | accidental-equivalent negatives | keep provisional; allowed promotion routes only |
| R05 | proof-search incompleteness | encode unknown; never negative |
| R06 | truth/F1 collapse | proposition isolation; F0/F1/F2 checks |
| R07 | LLM label noise | independent families; audited silver; human conflicts |
| R08 | generator–judge circularity | supervision-free primary family; cross matrix |
| R09 | benchmark leakage | pre-generation denylist; connected/near-duplicate audit |
| R10 | ReForm/Lean-Workbook overlap | exclude held-out claims; tag overlap |
| R11 | reference defects | explicit E27/review/adjudication; reference not infallible |
| R12 | ambiguous NL | separate ambiguity target/eval; no binary coercion |
| R13 | Lean environment drift | exact lock/context/cache versions; nightly re-elaboration |
| R14 | LeanInteract API drift | API probe/golden/1k comparison/migration |
| R15 | experimental server instability | use stable server; preserve normalized contract |
| R16 | pool nondeterminism | one-vs-many parity; deterministic shards/order |
| R17 | representation drift/leakage | version views; proof-leak tests; regenerate deliberately |
| R18 | weak calibration under shift | real-output calibration; shift caveat/Mondrian diagnostic |
| R19 | insufficient test power | report denominators/CIs; demote unsupported claims |
| R20 | rare-category instability | absolute counts; exploratory FDR; no broad claim |
| R21 | provider/model unavailability | declared slot fallback; preserve family-separation rules |
| R22 | source access/license failure | fixed fallback order; blocked manifest; no fabrication |
| R23 | evidence cost explosion | mandatory/sampled policy; amortized cost reports |
| R24 | annotation-policy instability | pilot/repeat/freeze version; reannotate affected examples |
| R25 | custom annotation/tooling failure | existing platform first; documented fallback only |
| R26 | pretrained judge/encoder contamination | post-cutoff novel subset; public-vs-novel deltas; qualified claims |

### 29.1 Incident procedure

For any material data or evaluation incident:

1. freeze affected releases and runs;
2. write an incident record with discovery date, scope, root cause, and affected hashes under `reports/decisions/`;
3. patch code/policy with a new version;
4. regenerate affected artifacts from the earliest compromised stage;
5. rerun leakage and integrity checks;
6. update reports and paper numbers;
7. preserve the incident history rather than rewriting it away.

---

## 30. Decision and pivot criteria

### 30.1 Environment/source

If requested `sft_classic` cannot be verified even with authenticated access, use the fixed fallback. If stable Lean 4.31.0 fails the LeanInteract probe, use an explicitly supported in-range pair. Record before downstream manifests.

### 30.2 Deterministic data

Disable/demote families missing Gate 4A/4B; never lower precision criteria. If positives remain too narrow, expand only with a new audited family/version.

### 30.3 LLM silver

If audits/circularity fail, keep Phase 6 deferred or use data for mining only. Core MVP proceeds with deterministic+real+human.

### 30.4 Model

If M2 does not beat M1, retain the simpler model and analyze matching failure. If M3 gains vanish after evidence-cost control, do not claim hybrid advantage. M5 proceeds only after stable M2/M3.

### 30.5 Calibration

If Gate 10 ECE/coverage fails, deploy REVIEW-heavy policy, recalibrate on valid real-output data, or restrict claimed distribution; never tune on final test.

### 30.6 Power/OOD

Underpowered held-out/error groups receive descriptive intervals/counts and no H3/H6 claim; do not merge protected splits to manufacture power.

### 30.7 Application

If M4 fails faithful@1, test whether reference-aware clustering or review prioritization still has preregistered measurable value under a new sealed version.

---

## 31. Pre-registered experiment matrix

### 31.1 Core methods

| ID | Method | Inputs | Role |
|---|---|---|---|
| B0 | scalar classifier | lexical/atom/tree scalars | simple learned |
| S0 | defeq/proof/certificate | propositions | symbolic high precision |
| T0 | GTED/operator tree | trees | structural |
| T1 | ASSESS/TransTED | transformed trees | semantic-structural |
| C0 | CriticLean | released NL–Lean protocol | closest learned critic |
| J0 | held-out-family LLM judge | frozen prompt/views | clean LLM |
| J1 | same judge + symbolic | prompt/views/evidence | fairness |
| J2 | COVCAL-style risk-controlled judge | held-out judge + disjoint calibration | calibrated judge baseline |
| M0 | dual encoder | Lean text/signature | embedding/retrieval |
| M1 | concatenated pair | Lean pair | cross-encoder |
| M2 | separate sides + matching | Lean pair | main neural |
| M3 | M2 + structural/symbolic | sampled evidence | main hybrid |
| M4 | NL + Lean | optional reference branch | application |
| M5 | text + Expr graph | graph/text | extension |

All emit §20.6.

### 31.2 Data mixtures for H2

All arms share one positive pool and use 50/50 positive-negative batches.

| Arm | Negative-source composition |
|---|---|
| D0 | 100% `G_rule` |
| D1 | 50% `G_rule`, 50% `G_sci` |
| D2 | 50% `G_rule`, 50% `G_open` |
| D3 | 50% `G_rule`, 50% `G_real` |
| D4 | 20% `G_rule`, 25% `G_sci`, 25% `G_open`, 30% `G_real` |
| D5 | D4 plus `training_gold`, human-gold loss weight 2, no ancestry oversampling |

D4/D5 positive slots are 50% certified positives, 30% human/promoted faithful
real outputs, and 20% promoted LLM-proposed equivalent variants. A
deterministic family occupies at most 5% of negative slots. Use at least three
LLM proposer families, each at most 40% of combined `G_sci+G_open`, and at
least three real generator families, each at most 40% of `G_real`. At most
four unique variants from one ancestry appear per epoch; ancestry-normalized
total loss weight is one and duplicates add no weight. Missing required
sources produce a `reduced_data_ablation`, never silent substitution. Fixed
ratios are confirmatory; ratio sweeps are exploratory.

### 31.3 Representations for H3

Raw/headless/signature/explicit/text+atoms/scalars/M5 graph. Confirm H3 only by §23.11 gains on hard-near-miss, heldout-transform, heldout-project—not aggregate-only. Report extraction/truncation coverage.

### 31.4 Judge×supervision matrix

Primary cell: primary held-out family scoring a model with no labels from that family. Other-family labels are secondary; same-family labels are circular diagnostics only.

### 31.5 Calibration

Uncalibrated, temperature, vector, isotonic, beta, split-conformal/selective risk. K-fold inside calibration_gold; report ECE/Brier/risk-coverage/95%-precision coverage and exploratory 99% denominator/CI.

### 31.6 NL/application

M4 reference-free; M4 reference-aware; M3 reference Lean+candidate; frozen ensemble. Compare first/first-compiling/random/generator/J0/J1/J2/S0/M4/M3.

### 31.7 Hypothesis mapping

H1: M1–M3 vs B0/S0/T0/T1/J0. H2: D0–D5. H3: representation slices/M5. H4: calibration. H5: frozen reranking. H6: clean held-out generator/project. Every paper row maps to config/prediction/hypothesis or is exploratory.

---
## 32. Minimum viable project, strong-paper track, and stretch result

### 32.1 MVP critical path

```text
0 contracts/lock → 1 LeanInteract → 2 extraction/denylist
→ 3 representations → 4G generation
↘ annotation tooling after Gate 3
→ 5 real outputs/prevalence → 7/7b human products
→ 4A/4B promotion → 8 split/freeze → 9 baselines
→ 10 M0–M3/calibration → 11 sealed Lean–Lean eval
→ 12 minimal M4/reranking
```

Phase 6 may be deferred by ADR; M5 never blocks MVP.

### 32.2 MVP deliverable

LeanInteract extraction, versioned reps, scoped deterministic families, multi-generator real outputs, expert development/calibration/final labels, leakage-safe splits, strong baselines, calibrated M2/M3, and one frozen reranking result. Claims limited to observed projects/generators/power.

### 32.3 Strong-paper track

Promoted LLM silver; more diverse outputs; full external registry; held-out transformation/project/generator/adversarial tests; public-vs-novel contamination analysis; full M4 study/statistical matrix.

### 32.4 Stretch

Useful M5 gains, generator-stratified risk control, and a broadly reusable benchmark/generation release.

### 32.5 Deferral records

A deferred phase records reason, blocked hypotheses/tables/artifacts, and re-entry gate. It never masquerades as completion.

---

## 33. Paper claim boundaries

Permitted when tests pass: improved same-claim metrics on named human/real/external distributions; calibrated selective acceptance on matched real-output calibration distribution; improved frozen reranking; explicitly measured held-out transfer; controlled data/representation ablation contributions.

Prohibited or qualified:

- “logical equivalence oracle” or completeness;
- treating failed proof/counterexample search as semantic evidence;
- universal calibration under project/generator shift;
- unseen-benchmark claims when pretraining exposure is unknown;
- broad claims from tiny CSLib/rare groups without counts;
- human-level claims without identical protocol/items/ambiguity handling;
- graph benefit without H3;
- LLM-silver benefit when deferred/circular;
- synthetic-transform accuracy as real autoformalization faithfulness.

Every headline names task, distribution, label source, operating point, coverage, confidence interval, and exclusions.

---

## 34. Reference implementation policy for LeanInteract

### 34.1 Single adapter/cache identity

Appendix A.5 is canonical. One real v1 adapter maps it to LeanInteract. Request/cache keys include `environment_schema_version`, context/request hashes, Lean/LeanInteract/REPL/project versions, options, timeout, and method version.

### 34.2 Pin/range

Pin 0.11.4; advertised Lean range binds unless fully tested ADR exception. Record the `augustepoiroux/repl` revision/tag exposed by configuration/package.

### 34.3 Imports/API

Use top-level exported server/command/project objects as verified; import `CommandResponse`, `DeclarationInfo`, `InfoTreeOptions`, `LeanError` from `lean_interact.interface`. Verify every Appendix A symbol before implementation/after upgrade.

### 34.4 Caveats

Explicit `allow_sorry`; normalize per-item response/LeanError/Exception; Linux per-process memory limit; experimental AutoLeanServer with stable fallback; default no InfoTree with sanctioned escalation.

### 34.5 Upgrade

Change lock only; run introspection/fixture/golden/recovery/batch; 1k extraction/representation/evidence comparison; inspect support/fork changes; increment environment/schema/normalization versions when needed; regenerate affected caches; merge after gate review.

### 34.6 Shell exception

Direct shell Lean only in doctor/CI installation/build diagnostics. It cannot parse semantics, label, or become fallback without architectural revision.

---

## Appendix A — Canonical LeanInteract integration contract and examples

### A.1 Version/API caveat

Examples target `lean-interact==0.11.4`. Before backend work, Phase 1 introspects every import, constructor, method, response field, and default. A mismatch blocks implementation until lock/plan reconciliation. The advertised Lean range and `augustepoiroux/repl` fork are binding environment metadata.

### A.2 Imports

```python
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal, Mapping, Protocol, Sequence

from lean_interact import (
    AutoLeanServer,
    Command,
    FileCommand,
    LeanREPLConfig,
    LeanServer,
    LeanServerPool,
    LocalProject,
)
from lean_interact.interface import (
    CommandResponse,
    DeclarationInfo,
    InfoTreeOptions,
    LeanError,
)
```

The four interface types are not assumed top-level exports.

### A.3 Project/config

```python
project = LocalProject(directory=Path("/absolute/path/to/pinned/project"))
repl_config = LeanREPLConfig(
    project=project,
    memory_hard_limit_mb=None,  # Linux-only, per REPL process when set.
)
```

Doctor records project revision/toolchain, checks supported range, and reports worker count × configured per-process limit against detected RAM where available.

### A.4 Command/validation

```python
statement = """
import Mathlib

theorem leanfaith_probe (x y : Nat) : x + y = y + x := by
  omega
"""

server = LeanServer(repl_config)
raw_response = server.run(
    Command(cmd=statement, declarations=True, root_goals=True),
    timeout=30.0,
)
if isinstance(raw_response, LeanError):
    raise RuntimeError(f"Lean probe failed: {raw_response}")
response: CommandResponse = raw_response
strict_valid = response.lean_code_is_valid(allow_sorry=False)
placeholder_valid = response.lean_code_is_valid(allow_sorry=True)
```

Never rely on the permissive default. The Phase 1 probe verifies every keyword argument shown here (including `root_goals=` — `rootGoals` is only the wire-format serialization alias — and `server.run(..., timeout=)`). `FileCommand` use is implemented only after signature verification. Save raw response before declaration normalization.

### A.5 Canonical LeanFaith backend protocol

This subsection is the source of truth; §8.4 mirrors it.

```python
class LeanStatus(StrEnum):
    VALID = "valid"
    VALID_WITH_SORRY = "valid_with_sorry"
    INVALID = "invalid"
    TIMEOUT = "timeout"
    CRASH = "crash"
    SETUP_ERROR = "setup_error"
    UNSUPPORTED = "unsupported"
    INTERNAL_ERROR = "internal_error"

@dataclass(frozen=True, slots=True)
class LeanRequest:
    request_id: str
    context_id: str
    code: str | None = None
    file_path: Path | None = None
    declarations: bool = False
    root_goals: bool = False
    infotree: Literal["none", "substantive", "full"] = "none"
    allow_sorry: bool = False
    timeout_seconds: float = 30.0
    metadata: Mapping[str, str] = field(default_factory=dict)

@dataclass(frozen=True, slots=True)
class LeanResult:
    request_id: str
    request_hash: str
    context_id: str
    context_fingerprint: str
    status: LeanStatus
    messages: tuple[dict, ...] = ()
    sorries: tuple[dict, ...] = ()
    declarations: tuple[dict, ...] = ()
    root_goals: tuple[str, ...] = ()
    infotree: tuple[dict, ...] = ()
    elapsed_ms: int = 0
    raw_response_path: str | None = None
    infrastructure_error: str | None = None

class LeanBackend(Protocol):
    def run(self, request: LeanRequest) -> LeanResult: ...
    def run_batch(self, requests: Sequence[LeanRequest]) -> list[LeanResult]: ...
    def close(self) -> None: ...
```

Invariants: one of code/file; ordered one-result-per-input batch; hash includes request/context/method/environment schema; raw immutable response; placeholder status never silently strict; infrastructure status never semantic label.

### A.6 Normalization table

| Observation | Status |
|---|---|
| strict valid | `VALID` |
| placeholder-valid / `statement_valid_with_placeholder` | `VALID_WITH_SORRY` |
| Lean rejection | `INVALID` |
| timeout | `TIMEOUT` |
| process/recovery failure | `CRASH` |
| project setup failure | `SETUP_ERROR` |
| unsupported contract/toolchain | `UNSUPPORTED` |
| adapter exception | `INTERNAL_ERROR` |

`run_batch` can return response, `LeanError`, or Python exception per item; normalize independently and preserve order. Serialize safe exception type/message/trace digest.

### A.7 InfoTree/server policy

Map `none` to no request; `substantive`/`full` only after verified `InfoTreeOptions` construction. Representation/structural evidence are sanctioned escalation paths. `AutoLeanServer` remains experimental; tested `LeanServer` fallback shares the protocol. Callers do not manage REPL processes directly.

### A.8 API-shape report

Record package/distribution version, supported Lean range, constructor/method signatures, interface-type module paths, validation signature/default, response attributes, experimental server lifecycle, and memory-limit platform behavior under `artifacts/compatibility/`/`reports/compatibility/`.

### A.9 Directional certificate template

```lean
import Mathlib

section LeanFaithCertificate
variable {α : Type} [Preorder α]
def A (x y : α) : Prop := x < y
def B (x y : α) : Prop := x ≤ y

example (x y : α) : A x y → B x y := by
  intro h
  exact le_of_lt h
end LeanFaithCertificate
```

Failure to prove B→A is search failure only.

---

## Appendix B — Example locked configuration

### B.1 Environment/projects

```yaml
environment_schema_version: 1
python: {version: "3.12"}
lean_interact:
  package: lean-interact
  version: "0.11.4"
  advertised_lean_min: "v4.8.0-rc1"
  advertised_lean_max: "v4.31.0-rc1"
  repl_fork: "https://github.com/augustepoiroux/repl"
toolchain_lock:
  mode: advertised_range  # or stable_v4_31_exception after the complete probe
  accepted_lean: null      # Phase 0 writes the exact tested version
  mathlib_toolchain_must_match: true
  stable_v4_31_exception_adr: null
projects:
  mathlib:
    git_url: "https://github.com/leanprover-community/mathlib4"
    revision: "<exact revision written by Phase 0>"
    expected_toolchain: "<exact accepted toolchain>"
    root_module: Mathlib
    globs: ["Mathlib/**/*.lean"]
  cslib:
    git_url: "https://github.com/leanprover/cslib"
    revision: "<exact revision written by Phase 0>"
    root_module: Cslib
    globs: ["Cslib/**/*.lean"]
    role: probe_now_adapter_at_ood
  physlib:
    git_url: "https://github.com/leanprover-community/physlib"
    revision: "<exact revision written by Phase 0>"
    root_module: Physlib
    globs: ["Physlib/**/*.lean"]
    role: probe_now_adapter_at_ood
lean_backend:
  backend: leaninteract
  server_mode: pool
  workers: null  # resolved per run; no universal worker-count mandate
  timeout_seconds: 30
  memory_hard_limit_mb: null  # Linux-only; limit is per REPL process
  allow_sorry: false
  infotree: none  # escalate only under §8.10, including representation derivation
  save_raw_responses: true
```

The generated executable lock replaces null/angle-bracket explanatory values. Doctor rejects an unresolved mode/version, a mathlib `lean-toolchain` mismatch, or an out-of-range version without the tested exception ADR. It also reports memory-product/platform checks; no universal worker/RAM mandate.

### B.2 Sources

```yaml
sources:
  primary_nl_lean:
    requested_id: formalmathatepfl/sft_classic
    access_status: private_requires_hf_auth  # loads with the project HF token
    auth: {hf_token_env: HF_TOKEN}           # secret reference; value never stored
    required_probe: [resolved_id, revision, license, schema, archived_100_row_sample_hash]
    external_api_approved: null              # §9.2 approval decision written at Phase 0
    fallback_order:
      - formalmathatepfl/sft_classic_numina
      - internlm/Lean-Workbook
      - PAug/ProofNetVerif:train
  sft_classic_numina:
    dataset_id: formalmathatepfl/sft_classic_numina
    expected_columns: [uuid, question, answer, lean_code]
  lean_workbook:
    dataset_id: internlm/Lean-Workbook
    supervision_role: synthetic_weak_only
  proofnetverif:
    dataset_id: PAug/ProofNetVerif
    expected_columns: [id, nl_statement, lean4_src_header,
                       lean4_formalization, lean4_prediction, correct]
    role: frozen_external_benchmark
```

### B.3 Representations/evidence

```yaml
representations:
  normalization_version: repr_v3
  views: [raw_proof_stripped, headless, signature_pp, signature_explicit,
          alpha_structural, notation_light, semantic_atoms, operator_tree]
  pretty_options:
    source: "ConstantInfo.type"
    ambient_profile: "Options.empty; ambient core and extension pp.* values ignored"
    universe_parameter_policy: "positionally canonicalize to u_0,u_1,..."
    common:
      pp.fullNames: true
      pp.proofs: false
      pp.proofs.withType: false
      pp.mvars: false
    signature_pp: {pp.universes: false, pp.explicit: false}
    signature_explicit: {pp.universes: true, pp.explicit: true}
    legacy_check_profile: "all 75 Lean-4.31 core pp.* options pinned; non-authoritative"
  note: "An ellipsis under pp.proofs=false is expected."
evidence:
  sampling_policy: evidence_sampling_v1
  mandatory_for: [evaluation_pairs, calibration_pairs, gold_promotion_candidates]
  training_sample: {strategy: stratified}
  proof_search_portfolio: portfolio_v1
  illustrative_tactics: ["exact?", aesop, simp, omega]
  counterexample_search:
    scope: decidable_bounded_fragments_only
    kernel_decide_preferred: true
    native_decide_trust: lower_trust_never_sole_gold_negative
```

The tactic list is illustrative; §16.4/versioned portfolio is authoritative.

### B.4 Splits

```yaml
splits:
  split_version: split_v0
  connected_component_key_field: split_group_ids
  held_out_generators: []
  held_out_projects: [cslib, physlib]
  held_out_transformation_families: []
  manifests:
    train: data/split_manifests/train.json
    validation: data/split_manifests/validation.json
    calibration: data/split_manifests/calibration.json
    internal_test: data/split_manifests/internal_test.json
    human_test: data/split_manifests/human_test.json
    benchmark_test: data/split_manifests/benchmark_test.json
    real_output_test: data/split_manifests/real_output_test.json
    heldout_transform_test: data/split_manifests/heldout_transform_test.json
    heldout_project_test: data/split_manifests/heldout_project_test.json
    heldout_generator_test: data/split_manifests/heldout_generator_test.json
    adversarial_test: data/split_manifests/adversarial_test.json
```

V0 mandatory: train/validation/calibration/internal/human/benchmark-when-ready/real-output. Strong-paper held-out/adversarial manifests may be explicit deferred manifests in MVP.

---
## Appendix C — Canonical pair and routing examples

### C.1 Alpha restatement

```lean
-- A: ∀ (n : Nat), n + 0 = n
-- B: ∀ (k : Nat), k + 0 = k
```

```yaml
same_claim: true
resolution_outcome: same_claim
relation: equivalent
error_types: []
faithfulness_levels: {F0_representation_equivalent: true, F1_same_claim: true, F2_truth_equivalent: true}
quality_tier: gold_conservative_transform
resolution_method: p01_alpha_certificate
```

### C.2 Claim erasure

```lean
-- A: ∀ (n : Nat), Nat.Prime n → 1 < n
-- B: ∀ (n : Nat), True
```

```yaml
same_claim: false
resolution_outcome: not_same_claim
relation: incomparable
error_types: [E25]
faithfulness_levels: {F0_representation_equivalent: false, F1_same_claim: false, F2_truth_equivalent: true}
quality_tier: gold_human
```

### C.3 Missing premise plus tautology

```lean
-- A: ∀ (x : Real), x ≠ 0 → x / x = 1
-- B: ∀ (x : Real), x / x = x / x
```

```yaml
same_claim: false
resolution_outcome: not_same_claim
relation: incomparable
error_types: [E01, E25]
quality_tier: gold_human
```

### C.4 Strictness change under aligned binders

```lean
-- Compare proposition bodies in a shared local context (x y : Real).
-- A body: x < y
-- B body: x ≤ y
```

```yaml
same_claim: false
resolution_outcome: not_same_claim
relation: A_stronger
error_types: [E11]
quality_tier: gold_human
resolution_method: expert_binder_aligned_claim_comparison
```

The relation is claim-level under the recorded `x↔x, y↔y` alignment. It is not inferred by proving an implication between two closed, universally quantified theorem types.

### C.5 Extra irrelevant variable

```lean
-- A: ∀ (n : Nat), n + 0 = n
-- B: ∀ (n m : Nat), n + 0 = n
```

```yaml
same_claim: false
resolution_outcome: not_same_claim
relation: incomparable
error_types: [E21]
faithfulness_levels: {F0_representation_equivalent: false, F1_same_claim: false, F2_truth_equivalent: true}
quality_tier: gold_human
```

Under the project policy this is theorem-interface unfaithful even if truth conditions are unchanged.

### C.6 Wrong domain

```lean
-- A: ∀ (x : Real), 0 ≤ x ^ 2
-- B: ∀ (x : Nat), 0 ≤ x ^ 2
```

```yaml
same_claim: false
resolution_outcome: not_same_claim
relation: incomparable
error_types: [E06]
quality_tier: gold_human
```

### C.7 Suspected reference defect: review route

```yaml
same_claim: null
resolution_outcome: unresolved
relation: null
error_types: [E27]
faithfulness_levels: {F0_representation_equivalent: null, F1_same_claim: null, F2_truth_equivalent: null}
quality_tier: unknown
requires_adjudication: true
resolution_method: null
```

### C.8 Terminal expert ambiguity

```yaml
same_claim: null
resolution_outcome: ambiguous
relation: ambiguous
error_types: [E30]
faithfulness_levels: {F0_representation_equivalent: null, F1_same_claim: null, F2_truth_equivalent: null}
quality_tier: gold_human
requires_adjudication: false
resolution_method: expert_adjudication
```

---

## Appendix D — LLM proposer prompt contract

```text
SYSTEM
Propose diverse Lean 4 theorem-statement variants for a faithfulness dataset.
Do not provide proofs. Preserve compilability under supplied imports.
Return strict JSON only. Do not claim verification.

INTENDED RELATIONS
 equivalent | A_stronger | B_stronger | near_miss | unrelated | unknown

ERROR IDS
 E01 through E30 only.

INPUT
 imports: [IMPORTS]
 source_statement_id: [ID]
 source_statement: [LEAN]
 optional_natural_language: [NL_OR_NULL]

TASK
Produce [N] complete statements covering requested strata. Equivalent variants
must avoid mere formatting unless requested; negative variants should be
plausible type-correct autoformalization mistakes.

OUTPUT
{"variants":[{
  "candidate_lean":"...",
  "intended_relation":"...",
  "intended_error_types":["E01"],
  "edit_summary":"...",
  "confidence":0.0,
  "assumptions":[],
  "potential_ambiguity":null
}]}
```

Reject unknown enums/errors, missing fields, duplicate normalized candidates, proof bodies when prohibited, or text outside JSON. `near_miss` is intention-only; final relation requires resolution. Store prompt/model/parameters/tokens/retries/source provenance.

---

## Appendix E — Blinded LLM judge prompt contract

```text
SYSTEM
Judge whether two Lean theorem statements express the same intended mathematical
claim. Do not equate both true/provable with same claim. Check domains, binders,
hypotheses, dependencies, quantifiers, operators, constants, casts, bounds,
conclusion strength, vacuity, and irrelevant variables. Return strict JSON.

A: [LEAN_A]
B: [LEAN_B]
OPTIONAL_NL: [NL_OR_NULL]

SAME-CLAIM ANSWERS
 same_claim | not_same_claim | ambiguous | uncertain
RELATIONS
 equivalent | A_stronger | B_stronger | incomparable |
 unrelated | ambiguous | null
DIRECTIONAL
 yes | no | unknown
ERRORS
 E01 through E30 only

OUTPUT
{
 "same_claim_answer":"...",
 "relation":"...",
 "A_implies_B":"yes|no|unknown",
 "B_implies_A":"yes|no|unknown",
 "error_types":[],
 "confidence":0.0,
 "rationale":"at most three concise sentences",
 "needs_expert_review":true
}
```

Map same/not-same to weak votes; ambiguous to terminal-ambiguity candidate with no binary target; uncertain to no vote/review. No single vote creates gold. Judges never see proposer/intention/gold/symbolic result/other vote unless the registered evidence condition explicitly supplies symbolic evidence.

---

## Appendix F — Human annotation checklist

1. Confirm both statements' context/typecheck status or mark context defect.
2. Identify objects/domains/binders/hypotheses/dependencies/conclusion.
3. Ignore names/formatting/proof text/harmless alpha changes.
4. Do not use “both provable” as same-claim evidence.
5. Compare every side condition, quantifier/order, operator, constant, cast, index, set/domain, and extra/free binder.
6. Choose same/not-same, or terminal ambiguity only when genuinely irresolvable from context.
7. Choose canonical relation and all E-codes.
8. Mark source/reference defect and minimum missing context.
9. Use cannot-assess only for further adjudication.
10. Give concise syntax/semantic rationale.
11. Remain blind to generator/intention/judges/model/split.

Adjudicator reproduces context, inspects independent labels/evidence without treating failed search as negation, resolves outcome/relation/errors, records method/guideline/rationale, and requires another reviewer for frozen-label changes.

---

## Appendix G — Dataset release checklist

- exact Python/LeanInteract/Lean/REPL/project/source/model revisions and terms;
- supported-range/ADR checks pass;
- schemas validate and evidence/labels remain separate;
- F1 equals same_claim; every label/evidence target kind and ID resolves; NL records reference NL-targeted labels; reference comparisons are PairRecords;
- representations keyed by theorem+normalization;
- denylist frozen before generation;
- all ancestry/problem connected components isolated;
- near-duplicate/ReForm/judge-family/final-test audits pass;
- final sampling independent of compared models;
- promotion/audit/Clopper–Pearson rules pass;
- no failed search/not-found/infrastructure error becomes negative;
- Parquet/manifests/counts/hashes/card/licenses validate;
- private-source external-API approvals recorded where applicable;
- restricted payloads replaced with reproducible IDs/scripts when needed;
- clean smoke rebuild succeeds; plan/policies/prompts/configs/reports linked.

---

## Appendix H — Experiment completion checklist

1. experiment/hypothesis/config/code/data/environment IDs recorded;
2. protected evaluation/near duplicates excluded;
3. tokenizer ADR precedes non-smoke training;
4. seeds/selection/stopping frozen before final results;
5. evidence conditions report amortized cost;
6. primary judge supervision-free and prompt-development record frozen;
7. calibration selected by K-fold within calibration_gold and final thresholds use real-output distribution;
8. ambiguity binary exclusion + three-class/abstention/sensitivity reported;
9. group/problem bootstrap and group counts used;
10. Holm primary/BH exploratory applied;
11. propensities and design/deployment-weighted results reported;
12. deployment claims use SRS real-output panel;
13. H3 only under registered hard/OOD slice rule;
14. ECE/selective precision/coverage/subgroup counts/failures/raw predictions included;
15. public-vs-novel contamination deltas where available;
16. reranking reports prevalence/headroom/compilation/top-k/ties/abstention/latency/evidence cost;
17. smoke artifacts excluded from selection/release/tables;
18. report states supported/failed claims, deviations, exclusions.

---

## Appendix I — First executable vertical slice

This slice is backlog item LF-019 and runs only after LF-016–LF-018 exist.

Inputs: fixture project, at least ten accepted source statements, one
configured case for each active family
P01/P02/P04-lite/N01/N02/N03/N07/N10, at least one explicit expected-failure
case, and no protected benchmark.

```text
doctor/API probe → extract → Context/Theorem → Representation
→ all-eight-family drafts → Lean validation → Pair/Evidence
→ P01-only smoke alpha resolution; every other pair unresolved
→ connected smoke split
→ tiny nonproduction classifier → predictions/metrics/manifest
```

All artifacts:

```yaml
artifact_class: smoke
release_eligible: false
model_selection_eligible: false
```

Only P01 alpha pairs may use `quality_tier=provisional` with
`resolution_method=smoke_alpha_certificate`. P02, P04-lite, and all negative
pairs retain null semantic relations and remain unresolved; `near_miss` stays
provenance only. Acceptance requires deterministic extraction and
model-visible views; explicit placeholder behavior; complete
attempt→draft→audit→variant→pair lineage; evidence linked to every pair; all
eight active families executed; N10 dual ancestry; every candidate
re-elaborated; at least ten accepted fixture sources; persisted expected
failures; zero intention-to-label inference; zero gold labels and promotions;
zero protected-benchmark overlap; zero connected split leakage; deterministic
semantic replay; batch-failure isolation; a tiny plumbing-only classifier
whose predictions route to REVIEW; a clean-checkout run; and release,
calibration, model-selection, and scientific-table guards that reject every
smoke artifact. The accepted run and Gate 4G report must bind the current
registry snapshot.

---

## Appendix J — Selected references and implementation anchors

### J.1 Infrastructure/libraries

- LeanInteract: `https://github.com/augustepoiroux/LeanInteract`, `lean-interact==0.11.4`.
- REPL fork: `https://github.com/augustepoiroux/repl`.
- mathlib: `https://github.com/leanprover-community/mathlib4`.
- CSLib: `https://github.com/leanprover/cslib`, root `Cslib`.
- Physlib: `https://github.com/leanprover-community/physlib`, root `Physlib`; consolidates PhysLean (formerly HepLean) and Lean-QuantumInfo under the Physlib repository in 2026.

### J.2 Corpora/benchmarks

- private primary source `formalmathatepfl/sft_classic` (HF-token access; schema verified at probe);
- public `formalmathatepfl/sft_classic_numina`;
- synthetic `internlm/Lean-Workbook`;
- `PAug/ProofNetVerif`;
- ProofNet#, RLM25, Con-NF, EPLA, CriticLeanBench, ReForm ConsistencyCheck, Gaokao-Formal, DriftBench, miniF2F variants.
- `LARK-Lab/FormalRx-Test` at a pinned immutable revision; inputs are evaluation-only and labels may be unavailable, which disables but does not block the direct comparison.

### J.3 Methods

- Liu et al., *Rethinking and Improving Autoformalization* (ICLR 2025): BEq/Con-NF.
- Poiroux et al., *Reliable Evaluation and Benchmarks for Statement Autoformalization* (EMNLP 2025; arXiv:2406.07222): ProofNetVerif/BEq+.
- FormalAlign (arXiv:2410.10135; ICLR 2025).
- CriticLean/CriticLeanGPT/CriticLeanBench (arXiv:2507.06181).
- GTED (arXiv:2507.07399).
- ASSESS/TransTED/EPLA (arXiv:2509.22246).
- Mathesis/LeanScorer/Gaokao-Formal (arXiv:2506.07047).
- ReForm/ConsistencyCheck (arXiv:2510.24592; 859 expert-annotated validation items).
- *The Faithfulness Gap*/DriftBench (arXiv:2606.16541).
- COVCAL (arXiv:2605.28365).
- FormalRx, *Rectify and eXamine Semantic Failures in Autoformalization* (arXiv:2607.04655v1); pin `LARK-Lab/FormalRx-8b` and available 1.7B/4B checkpoints, paper prompt, and verdict parser.

### J.4 Candidate generators

`GuoxinChen/ReForm-32B` and `GuoxinChen/ReForm-8B` (Apache-2.0, Qwen3-based; arXiv:2510.24592), Kimina-Autoformalizer-7B, Goedel-Formalizer-V2, StepFun-Formalizer, Herald, ATLAS, plus provider slots. Resolve every non-ReForm exact ID/revision/license/interface/overlap during Phase 0/5. ReForm was trained on Lean Workbook; ReForm×Lean-Workbook is overlap-tagged and ineligible for held-out claims.

### J.5 Typed mutation/testing anchors

- Winterer, Zhang, and Su, *Validating SMT Solvers via Semantic Fusion* (PLDI 2020; DOI `10.1145/3385412.3385985`);
- Winterer, Zhang, and Su, *On the Unusual Effectiveness of Type-Aware Operator Mutations for Testing SMT Solvers* / OpFuzz (OOPSLA 2020; DOI `10.1145/3428261`);
- Park, Winterer, Zhang, and Su, *Generative Type-Aware Mutation for Testing SMT Solvers* / TypeFuzz (OOPSLA 2021; DOI `10.1145/3485529`);
- Winterer and Su, *Validating SMT Solvers for Correctness and Performance via Grammar-Based Enumeration* (OOPSLA 2024; DOI `10.1145/3689795`).

These motivate generation/testing discipline; they do not establish Lean same-claim labels.

### J.6 Reference hygiene

For every external artifact record immutable ID/revision/retrieval/license, verify schema/count at pin, mark human/synthetic/model/weak/adjudicated provenance, register train/eval/overlap before ingestion, freeze denylist signatures, and report unavailable artifacts rather than substituting similarly named resources.

---

# Final definition of project completion

LeanFaith is complete for the declared release track only when:

1. pinned Python/LeanInteract/Lean/REPL/projects pass API/range/fixture/recovery/batch/reproducibility checks;
2. all source/theorem/representation/variant/pair/evidence/label/NL records validate and reproduce immutable manifests;
3. semantic/F0-F2/relation/error/evidence/ambiguity/promotion policies are implemented and golden-tested;
4. benchmark-before-generation denylisting, connected groups, and near-duplicate audits show no protected leakage;
5. candidates are promoted only by allowed routes; failed searches never become negatives; required human development/calibration/final products exist;
6. M0–M3 and required clean symbolic/structural/LLM comparisons run; M4 provides the declared application; M5 only when graph claims are made;
7. deployment thresholds use real-output calibration_gold and Gate 10 targets are evaluated without final-test tuning;
8. sealed internal/human/benchmark/real and track-required held-out tests report group-aware uncertainty, weights, corrections, counts, abstention, and contamination caveats;
9. frozen reranking reports prevalence/headroom/compilation/top-k/coverage/latency/evidence cost against fair baselines;
10. clean release smoke reconstructs declared artifacts; smoke data are excluded; cards/provenance/licenses are complete;
11. every claim obeys §33 and every deferred/failed gate is explicit.

A scoped MVP/intermediate artifact may be released when named gates are missing, but claims must be limited and the full track must not be declared complete.
