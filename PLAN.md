# LeanFaith: Foolproof Research and Implementation Plan

**Document version:** 2.0  
**Date:** 2026-07-10  
**Status:** Implementation specification; supersedes the previous plan  
**Primary objective:** Build a calibrated model that estimates whether a Lean 4 theorem statement is a faithful formalization of the same mathematical claim as a reference Lean statement and, ultimately, of a natural-language statement.  
**Primary engineering constraint:** All Python-to-Lean interaction must use [LeanInteract](https://github.com/augustepoiroux/LeanInteract). Direct `lean`, `lake`, or custom REPL subprocess management is prohibited outside one explicitly isolated fallback module.

---

## 0. Coding-agent execution contract

This document is intended to be executable by a coding agent. The agent must follow these rules.

1. **Work in milestone order.** Do not start model training before the data, label, split, and leakage gates pass.
2. **Treat every Lean result as structured evidence, not a Boolean.** Preserve status, messages, timeouts, crashes, environment fingerprints, code, and latency.
3. **Use LeanInteract for every Python-triggered Lean operation.** Use `Command`, `FileCommand`, `AutoLeanServer`, and `LeanServerPool` through a project-owned compatibility layer.
4. **Never infer non-equivalence from failed proof search.** A timeout, tactic failure, or inability to prove an implication means `unknown`, not `negative`.
5. **Never promote LLM consensus to gold truth.** LLM labels are weak supervision. Gold labels require human adjudication; certified transformations can provide high-confidence positive labels.
6. **Do not silently skip failures.** Every rejected or failed row goes to a machine-readable rejection table with a reason code.
7. **Do not leak theorem proofs into the learned metric.** Proofs may be used to check source validity or construct certificates, but proof bodies are not model inputs.
8. **Do not leak benchmark examples.** External benchmark pairs and human test pairs are forbidden in training, few-shot prompts, threshold tuning, and hard-negative mining.
9. **Do not erase semantic identifiers during normalization.** Local names may be alpha-normalized; global constants, domains, operators, casts, typeclass assumptions, and binder structure must remain recoverable.
10. **Pin and fingerprint everything.** Every artifact records Python lockfile hash, LeanInteract version, Lean version, project commit, `lake-manifest.json` hash, model ID/revision, prompt version, seed, and source snapshot.
11. **Every milestone ends with a gate.** A gate is a testable acceptance criterion. The agent stops and reports failures rather than building later stages on invalid artifacts.
12. **Every command is idempotent.** Rerunning a completed command either reuses a valid cache entry or reproduces the same content hashes.

### Non-negotiable scientific claims

The project must not claim that it learns unrestricted logical equivalence. The primary target is **autoformalization faithfulness / mathematical-claim equivalence**. Definitional equality, proof search, structural similarity, and truth-level implication are auxiliary signals and baselines.

---

## 1. Project summary

Current autoformalization evaluation commonly relies on some combination of:

- Lean elaboration/typechecking;
- exact or normalized string equality;
- proof-search-based checks such as BEq/BEq+;
- tree or graph edit similarity such as GTED-like metrics;
- general-purpose or specialized LLM judges;
- small human-evaluation sets.

Each signal captures only part of the target. A candidate can typecheck while omitting a hypothesis, changing a quantifier, weakening a conclusion, or formalizing a nearby but different fact. Conversely, two faithful formalizations can differ substantially in notation, binder order, definitions, coercions, or theorem interface.

LeanFaith will combine four data streams:

1. **Certified positive pairs:** content-preserving transformations with Lean-checkable certificates.
2. **Hard type-aware mutation candidates:** compiling, small-edit variants designed to alter mathematical content.
3. **Real autoformalization candidates:** fresh translations produced by multiple model families from natural-language inputs.
4. **Human-labeled examples:** expert judgments used for calibration, final evaluation, and correction of weak-label bias.

The project proceeds in two linked stages:

- **Stage A — reference Lean to candidate Lean:** learn whether two theorem statements express the same mathematical claim and diagnose stronger/weaker/near-miss relations.
- **Stage B — natural language to candidate Lean:** learn direct autoformalization faithfulness, using Stage A representations and real candidate groups.

The first model is a token-based pair model with full cross-statement interaction. A graph representation of elaborated Lean expressions is added only after the token baseline and data pipeline are stable.

---

## 2. Exact research target

### 2.1 Notation

For an environment `E`:

- `N`: a natural-language theorem/problem statement;
- `R`: a trusted or reference Lean theorem statement;
- `C`: a candidate Lean theorem statement;
- `type_E(S)`: the elaborated theorem type of statement `S` in `E`;
- `view_E(S)`: the collection of canonical representations extracted from `S` in `E`.

Every comparison is environment-relative. Two identical strings can elaborate differently under different imports, local notation, instances, or library versions.

### 2.2 Primary tasks

#### Task A: Lean–Lean claim faithfulness

Predict whether `C` expresses the same intended mathematical claim as `R`, while ignoring theorem names, local variable names, formatting, and harmless representational differences.

Recommended relation labels:

| Label | Meaning |
|---|---|
| `equivalent_content` | Same mathematical claim for autoformalization evaluation purposes |
| `candidate_stronger` | Candidate asserts more; informally `C` is stronger than `R` |
| `candidate_weaker` | Candidate asserts less; informally `C` is weaker than `R` |
| `overlapping_near_miss` | Substantial shared content, but neither is an acceptable faithful replacement |
| `unrelated` | Different claim or topic |
| `ill_typed` | Candidate does not elaborate in the required environment |
| `ambiguous` | Human judgment cannot resolve the intended relationship |
| `reference_problem` | Reference itself appears wrong, incomplete, or mismatched to the NL statement |
| `unknown` | Evidence is insufficient |

For relation terminology:

- `candidate_stronger` means that satisfying the candidate would normally satisfy the reference, but not conversely.
- `candidate_weaker` means that satisfying the reference would normally satisfy the candidate, but not conversely.

These are claim-level labels. Automated implication search may support them, but does not define them.

#### Task B: NL–Lean autoformalization faithfulness

Predict whether `C` faithfully formalizes `N` with the intended:

- objects and domains;
- quantifiers and dependency structure;
- hypotheses and side conditions;
- operators and relations;
- conclusion strength;
- scope and theorem interface.

#### Task C: downstream selection

Given `N` and a set of type-correct candidates `{C_1, …, C_k}`, rank candidates so that the most faithful candidate is selected first. This is the main applicability demonstration.

### 2.3 What faithfulness does and does not imply

Faithfulness does **not** collapse to either of the following:

- **Definitional equality.** Faithful statements may not be definitionally equal.
- **Truth-level logical equivalence.** Distinct true theorem statements may be mutually provable in a rich environment while expressing different claims.

Useful implications are one-way:

- Definitional equality is strong evidence for content equivalence.
- A certificate produced by an approved meaning-preserving transformation is strong evidence for content equivalence.
- Mutual proof search can be useful evidence, but can also succeed for the wrong reason or fail on equivalent statements.
- A failed proof attempt is never evidence of non-equivalence.

For NL evaluation, a trusted reference helps but is not absolute:

```text
reference faithful to N  +  candidate content-equivalent to reference
                       ⇒ candidate likely faithful to N
```

The reverse may fail when the NL statement is ambiguous, the reference is imperfect, or multiple mathematically faithful formalizations use different interfaces.

### 2.4 Required model output

The public scoring API must return a structured object, not only a scalar:

```json
{
  "p_faithful": 0.87,
  "p_equivalent_content": 0.85,
  "p_candidate_stronger": 0.04,
  "p_candidate_weaker": 0.06,
  "p_near_miss": 0.04,
  "p_unrelated": 0.01,
  "error_probabilities": {
    "missing_hypothesis": 0.08,
    "wrong_quantifier": 0.02,
    "wrong_domain": 0.03,
    "wrong_operator": 0.05
  },
  "confidence": 0.82,
  "decision": "accept",
  "abstained": false,
  "model_version": "...",
  "environment_fingerprint": "..."
}
```

For Lean–Lean evaluation, include optional directional auxiliary heads:

```json
{
  "p_a_implies_b_aux": 0.91,
  "p_b_implies_a_aux": 0.88
}
```

The suffix `aux` is intentional: these heads are diagnostic features, not the definition of claim equivalence.

### 2.5 Error taxonomy

The initial taxonomy must support multi-label annotation:

- `missing_hypothesis`
- `extra_hypothesis`
- `vacuous_or_impossible_hypothesis`
- `wrong_quantifier`
- `wrong_quantifier_scope`
- `wrong_binder_dependency`
- `wrong_domain_or_type`
- `wrong_constant_or_definition`
- `wrong_operator_or_relation`
- `wrong_numeric_literal`
- `wrong_variable_or_argument_order`
- `wrong_conclusion`
- `only_one_direction`
- `too_strong`
- `too_weak`
- `set_image_preimage_confusion`
- `function_composition_confusion`
- `cast_or_coercion_error`
- `missing_finiteness_nonzero_positivity_condition`
- `irrelevant_variable_or_assumption`
- `notation_only_difference`
- `binder_or_interface_only_difference`
- `reference_error`
- `ambiguous_natural_language`
- `other`

The taxonomy can be extended, but existing label meanings must never be silently changed.

---

## 3. Research questions and hypotheses

### RQ1 — Can a learned pair model predict faithfulness better than structural and proof-search metrics?

**H1:** A token model with bidirectional cross-statement interaction, trained on certified positives, difficult compiling mutations, real model outputs, and human labels, will improve discrimination on external human-labeled benchmarks.

### RQ2 — Which data sources drive real-world generalization?

**H2:** Deterministic transformations provide invariance and coverage, but real autoformalization candidates and human labels are required to close the synthetic-to-real gap.

### RQ3 — Does elaborated structure add value beyond source tokens?

**H3:** An elaborated-expression graph will improve performance on hard near-miss and out-of-domain examples after controlling for model capacity and training data.

### RQ4 — Does calibration make the metric operationally useful?

**H4:** Calibration and abstention will provide a high-precision acceptance region suitable for dataset filtering and reranking.

### RQ5 — Does the metric improve autoformalization systems?

**H5:** Reranking type-correct candidates with LeanFaith will improve semantic pass@1 relative to random selection, generation likelihood, structural metrics, BEq+, and LLM-judge baselines at comparable cost.

---

## 4. Deliverables and success criteria

### 4.1 Required deliverables

1. **Lean execution layer** built on LeanInteract, with stable internal interfaces, caching, error normalization, and concurrency.
2. **Theorem corpus** extracted from the user dataset, mathlib, and optional domain libraries, with environment fingerprints and multiple canonical views.
3. **Pair corpus** containing certified positives, mutation candidates, real autoformalization candidates, evidence records, and final labels.
4. **Human gold sets** separated into annotation calibration, model calibration, validation, and sealed test partitions.
5. **Token baseline model** with cross-statement attention, multi-task heads, calibration, and abstention.
6. **Graph extension** over elaborated Lean expressions.
7. **NL–Lean faithfulness model** or joint model for direct evaluation.
8. **Evaluation suite** including external benchmarks, robustness tests, OOD tests, calibration, and cost/latency.
9. **Reranking application** with a reproducible candidate-generation and selection experiment.
10. **Research package** containing code, manifests, dataset cards, model cards, prompts, experiment configs, and paper-ready tables.

### 4.2 Engineering gates

The following are hard gates, not aspirational targets:

- A 10,000-request LeanInteract stress test has fewer than 0.5% unresolved infrastructure failures after bounded retries.
- Every Lean request has an environment fingerprint and reproducible code payload.
- Repeated extraction under the same environment produces identical theorem and representation hashes.
- No pair labeled `non-equivalent` is based only on failed proof search.
- No external benchmark or sealed human-test cluster appears in training or prompt examples.
- Split validation reports zero exact duplicates and zero known source-cluster leakage across train/validation/test.
- Model evaluation reports calibration metrics and selective risk, not accuracy alone.

### 4.3 Paper-level targets

A strong result should demonstrate all of the following:

- statistically significant improvement over the strongest practical non-LLM baseline on external human labels;
- competitive or better performance than an LLM judge at materially lower inference cost or latency;
- improved semantic pass@1 in candidate reranking;
- robustness to held-out transformation families, generator models, theorem sources, and domains;
- evidence that elaborated graph structure helps on at least one difficult/OOD slice;
- useful calibrated abstention behavior.

Failure to beat all baselines does not invalidate the project if the dataset, analysis, or calibration findings are scientifically strong, but the downstream reranking result is the preferred headline.

---

## 5. System architecture

```text
Source registry
  ├── formalma.../sft_classic or local snapshot
  ├── mathlib4
  ├── CSLib / PhysLib / other Lean projects
  ├── external evaluation benchmarks
  └── LLM generation providers

        ↓

Environment manager
  ├── pinned project checkout
  ├── lean-toolchain fingerprint
  ├── lake-manifest fingerprint
  ├── LeanInteract configuration
  └── source-specific worker pool

        ↓

LeanInteract execution layer
  ├── Command: snippets, checks, certificates
  ├── FileCommand: per-file declarations and InfoTrees
  ├── AutoLeanServer: resilient single-server execution
  ├── LeanServerPool: parallel independent requests
  └── normalized results + content-addressed cache

        ↓

Theorem extraction and representation
  ├── raw source declaration
  ├── proof-free theorem signature
  ├── elaborated signature
  ├── alpha-canonical expression
  ├── selectively unfolded expression
  ├── operator/tree skeleton
  ├── optional expression graph
  └── source and environment metadata

        ↓

Candidate and pair generation
  ├── certified positive transformations
  ├── type-aware hard mutation candidates
  ├── fresh LLM translations
  ├── LLM-edited contrastive variants
  └── benchmark/reference-candidate pairs

        ↓

Evidence and labeling
  ├── elaboration status
  ├── transformation certificate
  ├── definitional equality result
  ├── scoped proof-search features
  ├── mutation provenance
  ├── LLM weak judgments
  ├── human judgments
  └── evidence aggregation + quality tier

        ↓

Dataset build
  ├── deduplication
  ├── contamination checks
  ├── grouped splits
  ├── family/model/domain holdouts
  └── frozen manifests

        ↓

Modeling
  ├── simple baselines
  ├── token pair cross-encoder
  ├── hybrid symbolic features
  ├── expression graph fusion
  ├── NL–Lean model
  └── calibration + abstention

        ↓

Evaluation and applications
  ├── external benchmark agreement
  ├── human gold agreement
  ├── calibration and selective risk
  ├── OOD and adversarial tests
  ├── candidate reranking
  └── repair/error hints
```

---

## 6. Mandatory LeanInteract integration

### 6.1 Decision

Python code must use LeanInteract as the sole normal interface to Lean 4. The previous custom `subprocess`-based `LeanRunner` design is removed.

As of this document date, the current PyPI release is `lean-interact==0.11.4`. Pin that exact version initially and update it only in a dedicated compatibility change that reruns all Lean integration tests. The lockfile is authoritative.

### 6.2 Why LeanInteract is the correct default

LeanInteract already provides the capabilities needed by this project:

- execution of Lean snippets and files;
- project configuration for local, Git-based, and temporary Lean projects;
- declaration extraction and InfoTrees;
- environment-state reuse and incremental elaboration;
- parallel elaboration where supported by the Lean version;
- `AutoLeanServer` restart and memory management;
- `LeanServerPool` for parallel command execution;
- structured messages, sorries, declarations, proof states, and validity checks;
- environment and proof-state pickling if later needed;
- an official BEq+ example and a scalable mathlib declaration-extraction example.

### 6.3 Approved LeanInteract components

| Operation | Required LeanInteract API | Notes |
|---|---|---|
| Check a generated statement | `Command` + `CommandResponse.lean_code_is_valid()` | Allow the one intentionally inserted `sorry`; reject all syntax/elaboration errors |
| Check a proof/certificate | `Command` + `lean_code_is_valid(allow_sorry=False)` | No `sorry` permitted anywhere in certificate code |
| Extract declarations from a file | `FileCommand(path=..., declarations=True)` | Path is relative to the configured project when possible |
| Extract structured elaboration | `Command`/`FileCommand(..., infotree="full")` | Persist only needed normalized fields plus raw response for debugging |
| Extract initial goals | `root_goals=True` | Useful for theorem interfaces and proof-search diagnostics |
| Resilient sequential execution | `AutoLeanServer` | Default for stateful workflows and debugging |
| Parallel independent execution | `LeanServerPool` | Default batch engine after stress testing |
| Existing checked-out project | `LocalProject` | Preferred production mode |
| Reproducible remote bootstrap | `GitProject` | Pin commit; convert to local snapshot for released experiments |
| Small isolated experiment | `TempRequireProject` or `TemporaryProject` | Suitable for smoke tests and benchmark-native environments |
| Interactive tactic stepping | `ProofStep` | Optional only; not a core certification dependency because tactic mode is experimental |

### 6.4 Project configuration policy

Use a separate `LeanREPLConfig` per distinct Lean project/environment.

Preferred production configuration:

```python
from lean_interact import LeanREPLConfig, LocalProject

project = LocalProject(
    directory="/absolute/path/to/pinned/project",
    auto_build=False,
)
config = LeanREPLConfig(
    project=project,
    verbose=False,
)
```

Rules:

- Build the project once in environment setup; set `auto_build=False` in workers after the build is verified.
- Do not specify both `project` and `lean_version`; LeanInteract infers the Lean version from the project.
- Use the source project's native `lean-toolchain` and `lake-manifest.json` unless a deliberate migration experiment is being run.
- Do not compare statements across different environments unless both have been re-elaborated successfully in one explicitly chosen canonical environment.
- Restart servers when imported files or custom Lean modules change. Incremental import caching can otherwise retain stale content.

### 6.5 Internal compatibility layer

No project module outside `src/leanfaith/lean/` may import LeanInteract directly. Create a stable internal protocol so LeanInteract upgrades do not affect the rest of the codebase.

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, Sequence


class LeanStatus(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    TIMEOUT = "timeout"
    CRASH = "crash"
    MEMORY_LIMIT = "memory_limit"
    SETUP_ERROR = "setup_error"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True)
class LeanMessageRecord:
    severity: str
    text: str
    start_line: int | None
    start_column: int | None
    end_line: int | None
    end_column: int | None


@dataclass(frozen=True)
class LeanExecutionResult:
    status: LeanStatus
    valid: bool | None
    allow_sorry: bool
    code_hash: str
    environment_fingerprint: str
    elapsed_ms: int
    messages: tuple[LeanMessageRecord, ...]
    declarations_json: tuple[dict, ...]
    infotree_json: tuple[dict, ...]
    raw_response_json: dict | None
    exception_type: str | None
    exception_message: str | None
    retry_count: int
    cache_hit: bool


class LeanBackend(Protocol):
    def check_code(
        self,
        *,
        code: str,
        allow_sorry: bool,
        timeout_s: float,
        declarations: bool = False,
        infotree: str | None = None,
        root_goals: bool = False,
    ) -> LeanExecutionResult: ...

    def check_many(self, requests: Sequence["LeanCodeRequest"]) -> list[LeanExecutionResult]: ...

    def extract_file(self, path: Path, *, timeout_s: float) -> LeanExecutionResult: ...
```

The concrete implementation is `LeanInteractBackend`. A fake backend is used in unit tests; integration tests use the real package.

### 6.6 LeanInteractBackend behavior

The backend must:

1. Build a `LeanREPLConfig` from an immutable environment specification.
2. Use `AutoLeanServer` for stateful single-server work.
3. Use `LeanServerPool` for independent batches after a pool-vs-single benchmark.
4. Normalize `CommandResponse`, `LeanError`, exceptions, and timeout/crash cases into `LeanExecutionResult`.
5. Preserve raw Pydantic model dumps for reproducibility and debugging.
6. Enforce bounded retries by error category.
7. Never return `False` for timeout/crash; return `valid=None` and an infrastructure status.
8. Use context managers so all servers are closed.
9. Assign unique generated theorem/declaration names derived from content hashes.
10. Ensure a statement check contains exactly the expected intentional `sorry` and no hidden source `sorry`.

Minimal implementation pattern:

```python
from lean_interact import AutoLeanServer, Command
from lean_interact.interface import CommandResponse, LeanError

with AutoLeanServer(config) as server:
    response = server.run(
        Command(
            cmd=code,
            declarations=True,
            infotree="full",
            root_goals=True,
        ),
        timeout=timeout_s,
        add_to_session_cache=False,
    )

    if isinstance(response, LeanError):
        ...
    elif isinstance(response, CommandResponse):
        valid = response.lean_code_is_valid(allow_sorry=allow_sorry)
        ...
```

### 6.7 State and leakage policy

For repeated candidate checks under one import/header context:

1. Submit the full import/header prefix once.
2. Cache that environment state only when it is small and repeatedly reused.
3. Submit each candidate from the same base environment, not from the preceding candidate's output environment.
4. Use unique names so declarations cannot collide.
5. Do not make the reference theorem constant available to a candidate unless the specific proof-based baseline requires it and the protocol records that fact.
6. Restart or rotate servers after a configurable number of requests or memory threshold.

LeanInteract's incremental elaboration is enabled by default and should be retained unless a reproducibility test finds a bug. Send complete snippets/files rather than manually splitting every command into tiny fragments.

### 6.8 Parallelization policy

Initial default:

- development and debugging: one `AutoLeanServer`;
- independent batch checks: `LeanServerPool` with a conservative worker count;
- per-file extraction over very large projects: either `LeanServerPool` or the official per-file process pattern, selected by benchmark;
- custom multiprocessing is allowed only if `LeanServerPool` is demonstrably insufficient.

Before scaling, benchmark `{1, 2, 4, 8, ...}` workers and record:

- requests/second;
- median and p95 latency;
- peak system memory;
- per-server memory;
- timeout/crash rate;
- cache hit rate;
- reproducibility of outputs.

`LeanServerPool.run_batch` may return exceptions alongside responses; the compatibility layer must normalize every item.

### 6.9 Memory and timeout policy

Use operation-specific timeout classes:

| Operation | Initial timeout | Retry policy |
|---|---:|---|
| Simple statement elaboration | 30 s | one retry on fresh server |
| File extraction | 180 s | one retry; then reject file with reason |
| Simple certificate | 60 s | one retry |
| Tactic portfolio / BEq+ | 60 s per tactic phase | no label from failure |
| Heavy normalization/export | 120 s | one retry |

These are configuration defaults, not constants. Tune from pilot distributions.

Use `AutoLeanServer` memory thresholds conservatively. If `memory_hard_limit_mb` is enabled, document that it is Linux-specific and benchmark the overhead. Never allow worker count × memory limit to exceed the machine budget.

### 6.10 Direct CLI fallback

A single module may exist:

```text
src/leanfaith/lean/fallback_cli.py
```

It may be used only for:

- diagnosing a LeanInteract compatibility bug;
- comparing LeanInteract behavior to `lake env lean` in integration tests;
- invoking a feature genuinely unavailable through the REPL after a documented decision.

Requirements:

- feature flag disabled by default;
- structured output identical to `LeanExecutionResult`;
- test proving normal production paths do not import or call it;
- every use logged with `backend="fallback_cli"`.

No other module may call `subprocess` for Lean.

### 6.11 LeanInteract acceptance gate

Milestone 1 cannot pass until all of these work in CI or a documented integration environment:

- import and version check for the pinned `lean-interact` package;
- `LocalProject` smoke test;
- valid and invalid statement checks;
- `allow_sorry=True` versus `False` behavior;
- declaration extraction using `declarations=True`;
- InfoTree extraction;
- file extraction using `FileCommand`;
- `AutoLeanServer` timeout recovery;
- `LeanServerPool` batch execution;
- stale-import test showing that server restart observes modified imported files;
- 10,000-request stress test with the required infrastructure-failure rate.


---

## 7. Repository and dependency layout

Use a standard Python package plus a small Lean project containing custom extraction and certification helpers.

```text
leanfaith/
  README.md
  AGENTS.md
  pyproject.toml
  uv.lock
  lean-toolchain
  lakefile.toml
  lake-manifest.json
  .python-version
  .env.example
  .gitignore
  LICENSE

  LeanFaith/
    Basic.lean
    ExportDeclaration.lean
    ExportExpr.lean
    NormalizeExpr.lean
    Certify.lean
    MutationHelpers.lean
    ProofFeatures.lean
    Tests.lean

  src/leanfaith/
    __init__.py
    cli.py

    config/
      models.py
      load.py
      defaults.py
      environment.py

    lean/
      protocol.py
      leaninteract_backend.py
      result_normalization.py
      environment_registry.py
      session.py
      cache.py
      names.py
      snippets.py
      fallback_cli.py

    data/
      schemas.py
      manifests.py
      io.py
      hashes.py
      dedup.py
      splits.py
      validation.py

    sources/
      base.py
      registry.py
      hf_sft_classic.py
      mathlib.py
      proofnetverif.py
      lean_workbook.py
      cslib.py
      physlib.py
      generic_lean_project.py

    representation/
      raw.py
      declaration.py
      canonical.py
      expr.py
      operator_tree.py
      graph.py
      constants.py

    transforms/
      base.py
      registry.py
      compose.py
      positive_alpha.py
      positive_binders.py
      positive_logic.py
      positive_notation.py
      positive_algebra.py
      positive_interface.py
      negative_operator.py
      negative_hypothesis.py
      negative_quantifier.py
      negative_scope.py
      negative_domain.py
      negative_constant.py
      negative_literal.py
      negative_nearby.py

    generation/
      providers.py
      prompts.py
      translators.py
      editors.py
      judges.py
      repair.py
      parsing.py
      quotas.py
      ledger.py

    evidence/
      certificates.py
      defeq.py
      proof_search.py
      counterexamples.py
      llm_labels.py
      human.py
      aggregation.py
      quality.py

    modeling/
      tokenizer.py
      pair_dataset.py
      pair_cross_encoder.py
      dual_cross_attention.py
      graph_encoder.py
      hybrid.py
      nl_lean.py
      heads.py
      losses.py
      calibration.py
      train.py
      inference.py

    evaluation/
      metrics.py
      slices.py
      baselines.py
      beq_plus.py
      external_benchmarks.py
      calibration.py
      reranking.py
      statistics.py
      reports.py

    service/
      schemas.py
      scorer.py
      reranker.py
      api.py

  configs/
    environments/
      leanfaith.yaml
      mathlib.yaml
      proofnetverif.yaml
      cslib.yaml
      physlib.yaml
    sources.yaml
    extraction.yaml
    representations.yaml
    transformations.yaml
    generation.yaml
    judging.yaml
    labeling.yaml
    splits.yaml
    train_pair.yaml
    train_graph.yaml
    train_nl_lean.yaml
    evaluate.yaml

  scripts/
    00_doctor.py
    01_extract.py
    02_build_representations.py
    03_generate_deterministic.py
    04_generate_llm_candidates.py
    05_collect_evidence.py
    06_export_human_annotation.py
    07_aggregate_labels.py
    08_build_splits.py
    09_train_pair.py
    10_train_graph.py
    11_train_nl_lean.py
    12_evaluate.py
    13_rerank.py

  tests/
    unit/
    integration/
    metamorphic/
    data_quality/
    model/
    golden/

  data/                 # ignored by Git
    raw/
    extracted/
    representations/
    candidates/
    evidence/
    annotations/
    datasets/
    splits/
    cache/
    reports/

  artifacts/            # ignored except tiny examples/manifests
    manifests/
    models/
    predictions/
    figures/
```

### 7.1 Python environment

Recommended baseline:

- Python 3.11;
- `uv` for dependency and lockfile management;
- `lean-interact==0.11.4` initially;
- Pydantic v2 for schemas;
- Hugging Face `datasets`/PyArrow for large corpora;
- PyTorch and Transformers for models;
- Typer for CLI;
- Ruff and Pyright or Mypy for static checks;
- Pytest and Hypothesis for tests;
- DuckDB or SQLite for indexes and cache metadata;
- Parquet/JSONL for portable artifacts.

The coding agent must not add a dependency without recording its purpose, license, and whether it is required or optional.

### 7.2 Lean project

The local `LeanFaith` Lean project should contain only logic that is easier or safer to implement inside Lean:

- export elaborated declarations and expression structure;
- normalize expressions under explicit policies;
- check definitional equality;
- generate or validate certificate obligations;
- provide small helper tactics/commands;
- expose structured JSON through commands/files executed with LeanInteract.

Python remains the orchestrator. Custom Lean executables may be built, but Python must invoke their relevant Lean code through LeanInteract unless the documented fallback policy applies.

---

## 8. Data sources and environment registry

### 8.1 Source priorities

Use this order:

1. **User corpus:** `formalmathatepfl/sft_classic` or the available local/private snapshot.
2. **mathlib4:** broad theorem-statement source for deterministic transformation generation.
3. **ProofNetVerif and related labeled benchmarks:** external validation/test only unless a benchmark explicitly provides an allowed training split.
4. **Fresh LLM translation outputs:** main realistic training signal.
5. **Lean Workbook or comparable NL–Lean sources:** additional paired statements.
6. **CSLib:** computer-science domain transfer.
7. **PhysLib and related physics projects:** physics domain transfer.
8. **Additional Lean projects:** only after the generic adapter and license review are stable.

### 8.2 Source registry

Every source is declared in `configs/sources.yaml` and materialized into a `SourceManifest`.

```yaml
sources:
  sft_classic:
    kind: huggingface_or_local
    dataset_id: formalmathatepfl/sft_classic
    revision: null
    local_path: null
    splits: [train]
    environment_id: sft_classic_native
    contains_nl: true
    contains_proofs: true
    intended_use: train
    license_status: review_required

  mathlib:
    kind: lean_project
    project_path: vendor/mathlib4
    git_commit: REQUIRED
    environment_id: mathlib_main
    contains_nl: false
    contains_proofs: true
    intended_use: train_and_ood

  proofnetverif:
    kind: huggingface
    dataset_id: PAug/ProofNetVerif
    revision: REQUIRED
    environment_id: proofnet_native
    contains_nl: true
    intended_use: sealed_external_eval
```

Required manifest fields:

- source name and version/revision;
- download/retrieval date;
- source license and redistribution status;
- native Lean environment;
- row count before and after filtering;
- schema mapping version;
- content hashes;
- intended split/use;
- known contamination risks.

### 8.3 Environment registry

Each environment has one immutable record:

```yaml
environment_id: mathlib_main
project_kind: local
project_path: /abs/path/to/mathlib4
project_git_commit: abcdef...
lean_toolchain_contents: leanprover/lean4:vX.Y.Z
lean_version: vX.Y.Z
lake_manifest_sha256: ...
leaninteract_version: 0.11.4
repl_revision: package_default
build_status: success
imports_policy: source_native
```

The environment fingerprint is the hash of all semantics-relevant fields. File timestamps are not part of the fingerprint; file contents and commits are.

### 8.4 Canonical-version policy

Do not force all sources into one Lean/mathlib version during the first extraction pass.

- Extract each source in its native pinned environment.
- Choose one canonical environment for the main token-model corpus after measuring migration success.
- Migrate a theorem only by re-elaborating it in the target environment and recording both source and target fingerprints.
- Keep non-migrated records for source-specific/OOD evaluation.
- Never pair statements from different environments unless the pair has been validated in one shared environment.

### 8.5 Data-license gate

Before any public release:

- verify source licenses;
- distinguish derived metadata from copied source text;
- respect model/provider terms for generated content;
- document whether theorem statements, NL text, prompts, and judge rationales may be redistributed;
- provide scripts and hashes when full data cannot be released.

License uncertainty does not block internal experimentation, but it blocks public dataset release.

---

## 9. Theorem extraction and canonical representations

### 9.1 Representation principle

There is no single ideal normalized string. Store multiple views and let experiments determine which combination is most useful.

Every valid theorem gets the following views:

1. **Raw source view** — exact declaration text as observed.
2. **Proof-free declaration view** — theorem/lemma declaration with body removed.
3. **Elaborated signature view** — LeanInteract `DeclarationInfo.signature` and related declaration fields.
4. **Surface-canonical view** — normalized whitespace/comments/theorem name/local binder names without semantic expansion.
5. **Expression-canonical view** — alpha-normalized elaborated `Expr` with stable JSON serialization.
6. **Selectively unfolded view** — only approved notation/coercion/definition expansions.
7. **Operator/tree skeleton** — compact structural representation for edit baselines.
8. **Expression graph** — optional later representation with typed nodes and binder edges.

### 9.2 Information to remove

The following may be erased or canonicalized:

- theorem/lemma declaration name;
- proof body;
- comments and docstrings;
- source positions and formatting;
- local variable and hypothesis names, replaced by deterministic de Bruijn-style or ordered IDs;
- universe variable names, while preserving universe structure;
- synthetic macro bookkeeping that does not affect elaborated meaning;
- generated declaration suffixes used only to avoid collisions.

### 9.3 Information to retain

The following must remain recoverable:

- fully qualified global constants and definitions;
- all binder types, order, explicit/implicit/instance status, and dependencies;
- quantifiers and proposition structure;
- typeclass assumptions;
- domains and codomains;
- operators and relations;
- casts/coercions, whether explicit or inserted, with a normalized representation;
- numeric literals;
- namespace-resolved identifiers;
- universe levels/constraints where semantically relevant;
- local notation resolution through the elaborated constant;
- source environment fingerprint.

Do not anonymize global constants into generic IDs without also preserving a stable mapping. Erasing `Nat.Prime`, `Set.Infinite`, or `Continuous` would erase the claim.

### 9.4 Selective expansion policy

Full recursive unfolding is forbidden because it can:

- explode sequence/graph size;
- erase useful high-level mathematical concepts;
- become version-sensitive;
- make distinct interfaces appear artificially similar.

Use three explicit profiles:

| Profile | Purpose | Expansion policy |
|---|---|---|
| `surface` | token model | no semantic unfolding; normalize syntax and names only |
| `semantic_compact` | main canonical view | expand notation/macros/coercion wrappers and a small reviewed whitelist |
| `diagnostic_expanded` | analysis only | deeper bounded unfolding with depth/node limits |

Every expanded node records its source constant. The whitelist and bounds are versioned configuration artifacts.

### 9.5 Expression JSON

Implement `LeanFaith.ExportExpr` to serialize the elaborated theorem type. A node record should support at least:

- `forallE`
- `lam`
- `app`
- `const`
- `fvar`
- `bvar`
- `mvar` — normally rejected after elaboration
- `sort`
- `lit`
- `letE`
- `proj`
- `mdata`

Required fields include node kind, child indices, constant name, literal value, binder kind, type edge, local-variable index, and pretty-printed diagnostic text. Serialization must be deterministic.

### 9.6 Graph representation

Graph nodes derive from expression nodes. Candidate edge types:

- syntax parent/child;
- function-to-argument position;
- binder-to-bound-use;
- expression-to-type;
- declaration-to-global-constant;
- same-global-constant across the pair;
- aligned local binder across the pair, when alignment is confident.

Do not build the graph model until expression export is deterministic and the token baseline is complete.

### 9.7 Representation invariants

Automated tests must verify:

- theorem-name changes do not change canonical hashes;
- local alpha-renaming does not change expression-canonical hashes;
- comments and whitespace do not change canonical hashes;
- changing `<` to `≤` changes semantic hashes;
- changing a domain from `Nat` to `Int` changes semantic hashes;
- binder dependency changes are visible;
- raw and canonical views round-trip to the same theorem record;
- repeated runs in the same environment produce byte-identical JSON.

---

## 10. Stable data schemas

Use Pydantic models with explicit schema versions. Store high-volume tables in Parquet; store exact Lean code and raw responses in compressed JSONL/blob storage referenced by content hash.

### 10.1 `EnvironmentFingerprint`

```python
class EnvironmentFingerprint(BaseModel):
    schema_version: Literal["1"] = "1"
    environment_id: str
    leaninteract_version: str
    lean_version: str
    repl_revision: str
    project_kind: str
    project_commit: str | None
    lean_toolchain_sha256: str
    lake_manifest_sha256: str | None
    project_content_sha256: str | None
    fingerprint: str
```

### 10.2 `TheoremRecord`

```python
class TheoremRecord(BaseModel):
    schema_version: Literal["1"] = "1"
    theorem_id: str
    source_id: str
    source_row_id: str | None
    source_split: str | None
    source_file: str | None
    source_range: dict | None
    full_name: str | None
    declaration_kind: str

    environment_fingerprint: str
    import_header: str
    namespace_context: str | None
    open_declarations: list[str]

    natural_language: str | None
    raw_declaration: str
    proof_free_declaration: str
    signature_pretty: str
    declaration_info_json_hash: str

    surface_canonical: str
    expr_canonical_hash: str
    semantic_compact_hash: str
    operator_tree_hash: str
    graph_hash: str | None

    global_constants: list[str]
    binder_count: int
    proposition_size: int
    token_count: int

    elaboration_status: str
    extraction_status: str
    has_source_proof: bool
    contains_sorry_in_source: bool

    source_license: str | None
    created_by_run_id: str
```

### 10.3 `PairRecord`

```python
class PairRecord(BaseModel):
    schema_version: Literal["1"] = "1"
    pair_id: str
    reference_theorem_id: str
    candidate_theorem_id: str
    natural_language_id: str | None
    environment_fingerprint: str

    origin: str
    transformation_family: str | None
    transformation_rule: str | None
    transformation_chain: list[str]
    generator_model: str | None
    generator_prompt_version: str | None

    proposed_relation: str | None
    final_relation: str
    p_faithful_label: float | None
    error_types: list[str]
    label_quality_tier: str
    label_version: str

    evidence_ids: list[str]
    cluster_id: str
    source_group_id: str
    split: str | None

    surface_edit_distance: float | None
    tree_edit_distance: float | None
    length_ratio: float | None
    difficulty_bucket: str | None
```

### 10.4 `EvidenceRecord`

```python
class EvidenceRecord(BaseModel):
    schema_version: Literal["1"] = "1"
    evidence_id: str
    pair_id: str
    evidence_type: str
    producer: str
    producer_version: str
    status: str
    value: dict
    confidence: float | None
    code_hash: str | None
    environment_fingerprint: str | None
    prompt_hash: str | None
    raw_artifact_hash: str | None
    created_by_run_id: str
```

Evidence types include:

- `elaboration_reference`
- `elaboration_candidate`
- `certified_transformation`
- `definitional_equality`
- `proof_search_a_to_b`
- `proof_search_b_to_a`
- `counterexample_attempt`
- `mutation_provenance`
- `llm_judgment`
- `human_annotation`
- `benchmark_label`

### 10.5 `CandidateGroup`

```python
class CandidateGroup(BaseModel):
    schema_version: Literal["1"] = "1"
    group_id: str
    natural_language_id: str
    reference_theorem_id: str | None
    environment_fingerprint: str
    candidate_theorem_ids: list[str]
    generator_run_ids: list[str]
    sealed_for_reranking_eval: bool
```

### 10.6 `HumanAnnotation`

```python
class HumanAnnotation(BaseModel):
    schema_version: Literal["1"] = "1"
    annotation_id: str
    pair_id: str
    anonymized_annotator_id: str
    relation: str
    faithful: bool | None
    error_types: list[str]
    confidence: int
    reference_problem: bool
    natural_language_ambiguous: bool
    notes: str | None
    seconds_spent: int | None
    guideline_version: str
```

### 10.7 Schema invariants

- IDs are content-derived where possible.
- A pair's two theorem records share one validation environment.
- `ill_typed` candidates cannot have `p_faithful_label=1`.
- `unknown` is never silently mapped to `unrelated`.
- A `certified_transformation` evidence record must include checkable certificate code or an approved definitional-equality result.
- A `gold_human` final label must reference at least two annotations and adjudication when they disagree.
- Source, generator, judge, and annotator identities are hidden from model input.

---

## 11. Source ingestion and theorem extraction

### 11.1 Generic extraction sequence

For each source item:

1. Read raw fields through a source adapter.
2. Identify native environment and import/header context.
3. Extract the target declaration without relying on regex alone.
4. Remove the proof body through declaration/signature information.
5. Submit the proof-free statement with one controlled `sorry` through LeanInteract.
6. Request declaration metadata and, where needed, InfoTrees.
7. Export the elaborated expression with the custom Lean module.
8. Construct all canonical views.
9. Validate invariants and content hashes.
10. Write `TheoremRecord` or a rejection record.

### 11.2 User dataset adapter

The exact schema of `formalmathatepfl/sft_classic` may differ by snapshot. The adapter must begin with schema discovery, not assumptions.

Required behavior:

- support Hugging Face and local Arrow/Parquet/JSONL paths;
- stream rows rather than loading the whole corpus into memory;
- print and save a schema report before extraction;
- detect candidate fields for NL text, source header/imports, theorem statement, and proof;
- require an explicit approved mapping after auto-detection;
- preserve the original row as a hash-addressed raw artifact;
- never modify source text in place;
- sample at least 100 rows for manual schema audit;
- report duplicate NL statements, duplicate Lean statements, missing headers, and mixed Lean versions.

A source adapter must expose:

```python
class SourceAdapter(Protocol):
    def inspect_schema(self) -> SourceSchemaReport: ...
    def iter_examples(self) -> Iterator[RawSourceExample]: ...
    def build_environment_spec(self, example: RawSourceExample) -> EnvironmentSpec: ...
    def extract_fields(self, example: RawSourceExample) -> ExtractedSourceFields: ...
```

### 11.3 mathlib extraction

Use LeanInteract's official declaration-extraction pattern as the starting point:

- discover `.lean` files while excluding `.git`, `.lake`, build directories, and lake packages;
- process each file with `FileCommand(path=..., declarations=True)`;
- serialize each `DeclarationInfo` record;
- filter to theorem/lemma declarations whose elaborated type is in `Prop`;
- keep the source file and range;
- use a pinned local project for final experiments.

Scale in phases:

1. 10 files for debugging;
2. 100 files for concurrency and cache tuning;
3. 1,000 files for failure taxonomy;
4. full target project.

Do not catch all exceptions and discard them silently. The official example is useful, but LeanFaith must record every failed file with normalized failure status and retry history.

### 11.4 CSLib and PhysLib

Use the generic Lean-project adapter after mathlib succeeds. For each project:

- pin a commit and native toolchain;
- build outside the worker pool;
- use `LocalProject`;
- extract declarations per file;
- maintain project-specific import and namespace information;
- reserve at least one project as a full OOD holdout.

### 11.5 Proof stripping

Regex-only removal of theorem bodies is prohibited in the final pipeline.

Preferred order:

1. use LeanInteract declaration metadata and source ranges;
2. reconstruct a new declaration from the elaborated signature;
3. use InfoTree/syntax ranges for the original source declaration;
4. only for a documented compatibility fallback, use a tested string utility behind a versioned adapter.

The canonical check form should use a generated name:

```lean
theorem leanfaith_check_<hash> <binders> : <conclusion> := by
  sorry
```

Before submission, reject source text containing unexpected `sorry`, `admit`, or placeholders outside the inserted body.

### 11.6 Proposition filter

V1 trains only on theorem/lemma declarations whose type is a proposition.

- If the declaration has a non-`Prop` type, label it `non_prop_declaration` and exclude it.
- Retain counts and examples for future extensions.
- Do not infer proposition status from the declaration keyword alone; verify in Lean.

### 11.7 Extraction failure taxonomy

At minimum:

- `missing_required_field`
- `unsupported_source_schema`
- `environment_setup_failure`
- `unknown_import`
- `parse_error`
- `elaboration_error`
- `unexpected_sorry`
- `multiple_target_declarations`
- `non_prop_declaration`
- `timeout`
- `server_crash`
- `memory_limit`
- `expr_export_error`
- `nondeterministic_representation`
- `internal_exception`

### 11.8 Extraction acceptance gate

Before pair generation:

- at least 10,000 valid theorem records from the main source(s);
- at least 1,000 mathlib declarations extracted through `FileCommand`;
- deterministic re-extraction hashes on a 1,000-record sample;
- fewer than 1% unexplained extraction failures;
- 100 manually reviewed records with correct NL, imports, statement, and signature linkage;
- no proof text present in model-facing fields.

---

## 12. Pair and candidate generation

### 12.1 General principle

Generate **contrastive sets**, not isolated random pairs. For a base theorem, create several variants with similar edit budgets:

- easy positive;
- hard positive;
- easy negative candidate;
- hard negative candidate;
- fresh LLM translation when NL is available.

Matching positive and negative pairs by length, edit distance, and transformation count reduces shortcut learning.

### 12.2 Transformation DSL

Every deterministic rule implements:

```python
class Transformation(Protocol):
    rule_id: str
    family: str
    intended_relation: str
    requires_nl: bool
    can_chain: bool

    def applicable(self, theorem: TheoremRecord, expr: ExprView) -> bool: ...
    def propose(self, theorem: TheoremRecord, rng: Random) -> list[VariantProposal]: ...
    def build_certificate(self, base: TheoremRecord, variant: VariantProposal) -> CertificatePlan | None: ...
```

`VariantProposal` must include:

- transformed source or expression;
- exact edited node IDs;
- before/after semantic objects;
- intended relation;
- expected difficulty;
- transformation seed;
- whether a certificate is expected;
- whether human/LLM review is required.

### 12.3 Certified positive families

#### P0 — Cosmetic invariance

- theorem-name changes;
- whitespace and formatting;
- comments/docstrings;
- harmless parenthesization;
- Unicode/ascii notation variants that elaborate identically.

Use sparingly. Cap at approximately 5–10% of positive training pairs.

#### P1 — Alpha and local-name invariance

- rename term variables;
- rename hypotheses;
- rename universe variables;
- change generated binder names;
- normalize local binder IDs.

Expected certificate: definitional equality or identical expression-canonical hash.

#### P2 — Independent binder/hypothesis reorder

Reorder only binders whose types do not depend on each other. Build a dependency DAG from the elaborated expression. A proposed permutation is legal only if it is a topological ordering.

Certificate requirements:

- both statements elaborate;
- generated forward and backward theorem obligations compile with `allow_sorry=False`;
- binder alignment is recorded.

#### P3 — Logical presentation rewrites

Examples, only when valid under the exact proposition structure:

- reassociation/commutation of `∧` or `∨`;
- currying and uncurrying of implication/conjunction;
- `¬ P` versus `P → False`;
- swapped sides of equality;
- controlled rewriting of iff directions;
- moving independent universal binders.

Use explicit trusted proof templates or named lemmas. Do not rely on unrestricted automation as the only provenance.

#### P4 — Notation and definitional presentation

Examples:

- set membership versus set-builder predicate;
- subset notation versus its definition;
- qualified versus opened names;
- equivalent arithmetic notation;
- explicit versus inferred coercions when the elaborated claim is unchanged;
- approved abbreviation expansion/compression.

Expected certificate: definitional equality or a small fixed proof template.

#### P5 — Algebraic and arithmetic normal forms

Examples:

- commutative/associative rearrangements;
- normalized polynomial forms;
- equivalent inequalities under trusted lemmas;
- `Even n` and an existential representation when a checked equivalence lemma is used.

Rules are domain-specific and must list required imports and certificate tactics. Keep proof-search time bounded.

#### P6 — Theorem-interface variants

Examples:

- grouped versus ungrouped binders;
- implicit versus explicit binder presentation when mathematical content is unchanged;
- `∀ x, ...` versus function-style binders;
- equivalent use of subtypes and explicit membership assumptions when certified.

Mark these with `binder_or_interface_only_difference` so experiments can decide whether they should count as fully faithful for a target benchmark.

### 12.4 Positive certificate policy

A positive pair can receive `certified_positive` only if one of these holds:

1. expression-canonical identity under an approved alpha/cosmetic policy;
2. Lean definitional equality check succeeds;
3. an approved transformation-specific certificate proves both directions without `sorry`;
4. a human gold annotation confirms equivalence.

A generic tactic portfolio proving `A ↔ B` is recorded as proof evidence but does not automatically elevate arbitrary pairs to certified content equivalence.

### 12.5 Transformation chaining

Create harder positives by composing 2–4 approved rules. Requirements:

- preserve the ordered rule chain;
- validate after each step;
- compose or recheck the final certificate;
- cap expression growth;
- avoid repeated inverse transformations;
- include held-out chains in validation/test.

### 12.6 Hard negative mutation families

Hard mutations must still elaborate. They are initially **negative candidates**, not automatic gold negatives.

#### N0 — Operator/relation mutation

Type-aware replacements such as:

- `<` ↔ `≤`;
- `=` ↔ `≠` where type-correct;
- `∈` ↔ `∉`;
- `⊆` ↔ `=`;
- `∧` ↔ `∨`;
- addition ↔ multiplication;
- image ↔ preimage;
- composition order changes.

Replacement candidates are selected by elaborated type compatibility, not text matching alone.

#### N1 — Hypothesis mutation

- delete a side condition;
- add an irrelevant condition;
- add an impossible/vacuous condition;
- negate a condition;
- weaken or strengthen a bound;
- change a typeclass assumption;
- move a hypothesis into or out of a quantifier scope.

Record whether the edit is expected to make the candidate stronger, weaker, or vacuous.

#### N2 — Quantifier and dependency mutation

- `∀` ↔ `∃`;
- move a quantifier across implication/conjunction;
- change binder order when dependency makes it non-equivalent;
- make a variable depend on the wrong earlier binder;
- change uniqueness or existence conditions.

#### N3 — Domain/type mutation

- `Nat` ↔ `Int` ↔ `Rat` ↔ `Real` where the statement still elaborates;
- set ↔ subtype;
- finite ↔ infinite structure assumptions;
- topological/algebraic structure changes;
- change a parameter's universe or container type.

Inserted casts must be made explicit in provenance.

#### N4 — Global constant mutation

Replace a constant with a type-compatible nearby constant, for example:

- `Continuous` ↔ `Measurable`;
- `Prime` ↔ `Irreducible` in an appropriate context;
- `closure` ↔ `interior`;
- `map` ↔ `comap`;
- `Injective` ↔ `Surjective`;
- a theorem-relevant function with a sibling from the same namespace.

Build candidate replacement sets from elaborated types, namespace proximity, documentation embeddings, and source co-occurrence. Human/LLM review remains necessary.

#### N5 — Literal and argument mutation

- increment/decrement numeric literals;
- change exponents;
- swap asymmetric arguments;
- duplicate/drop one argument;
- change a sign;
- alter an index or dimension.

#### N6 — Conclusion mutation

- replace only the conclusion while keeping context;
- select a nearby conclusion from another theorem;
- remove one direction of an iff;
- replace equality with approximate/order relation;
- negate the conclusion.

#### N7 — Nearby theorem substitution

Pair a theorem with a statement from the same file/namespace having similar constants and length but different content. This creates realistic hard negatives without a single synthetic edit.

### 12.7 Negative-validation policy

A hard mutation receives one of these statuses:

| Status | Meaning |
|---|---|
| `candidate_negative` | Intended meaning-changing edit; not yet sufficiently labeled |
| `weak_negative` | Multiple calibrated weak signals agree; usable with reduced training weight |
| `human_negative` | Human consensus/adjudication confirms unfaithfulness |
| `benchmark_negative` | External benchmark provides the label |
| `accidental_positive` | Mutation preserved content; move to positive/ambiguous pool |
| `unknown` | Insufficient evidence |

Important limitation: because theorem statements are often true propositions in a rich library, proving or disproving `A ↔ B` does not reliably capture claim identity. Counterexamples are useful only in suitable parameterized/decidable fragments. Mutation provenance and human semantics are central for negatives.

### 12.8 Contrast-set balancing

For each base theorem, attempt to construct pairs matched on:

- source token edit distance;
- canonical expression edit distance;
- statement length;
- changed-node depth;
- number of changed semantic slots;
- domain;
- transformation chain length.

This prevents the classifier from learning “large edit = negative” or “one-token edit = negative.”

### 12.9 Initial scale plan

Use stage gates rather than immediately spending the full token budget.

#### Pilot

- 5,000 base theorems;
- 2 positives and 3 mutation candidates per theorem;
- 2,000 NL problems with 4 fresh candidates each;
- 500 double-annotated pairs.

#### V1 dataset

- 100,000 base theorems;
- 2–4 certified positives per theorem;
- 4–8 hard mutation candidates per theorem;
- 20,000–50,000 NL problems with 8–16 fresh model candidates each;
- 2,000–5,000 human-labeled pairs across calibration/test.

#### Scale-up

Scale only after learning curves and error analysis show that more examples from a stream improve external validation. Do not spend billions of tokens generating duplicate low-information variants.


---

## 13. LLM-generated realistic data

### 13.1 LLM roles

Do not use one undifferentiated “LLM pipeline.” Separate four roles:

1. **Translator:** produces fresh Lean theorem statements from NL.
2. **Editor:** creates controlled equivalent or non-equivalent variants of an existing Lean statement.
3. **Compilation repairer:** fixes syntax/elaboration only, with strict instructions not to change mathematical content.
4. **Judge/critic:** assesses faithfulness and identifies errors.

Provider and model names are configuration values. The design supports ReForm-32B, GLM-family models, GPT-family models, Claude/Opus-family models, and future systems without code changes.

### 13.2 Fresh translation stream

For every selected NL statement:

1. Load the exact environment/import header.
2. Give the translator the NL statement, allowed imports, Lean version, and output schema.
3. Request theorem statement only, with no proof.
4. Sample multiple outputs across prompts, temperatures, and model families.
5. Parse the response into a declaration.
6. Remove comments and model-specific wrappers from the candidate view while preserving the raw output separately.
7. Check elaboration with LeanInteract.
8. Optionally run one or more compilation-only repair attempts.
9. Store raw and repaired candidates as different records linked by provenance.
10. Deduplicate by raw, surface-canonical, and expression-canonical hashes.
11. Send candidates to weak judges and the human-sampling policy.

A repaired candidate must never overwrite the raw candidate. Repair can change semantics even when instructed not to, so it receives separate judgments.

### 13.3 Translation prompts

Prompts must be versioned files. A base prompt should specify:

- output one Lean theorem/lemma statement and no proof;
- preserve every object, assumption, quantifier, side condition, and conclusion;
- use only available imports;
- avoid adding assumptions merely to make the theorem easier;
- avoid proving or simplifying the problem instead of formalizing it;
- output parseable JSON or a fenced Lean field according to the provider adapter.

Do not include sealed benchmark examples as few-shot demonstrations. Use synthetic or training-only examples.

### 13.4 Controlled LLM editing

For source statements with or without NL, request variants in explicit categories:

- faithful but syntactically distant;
- faithful using different definitions/notation;
- missing one assumption;
- too strong;
- too weak;
- wrong quantifier;
- wrong domain;
- wrong relation/operator;
- subtle binder-scope error;
- nearby but different theorem.

The model is a proposer only. Every proposal must elaborate and then enter the evidence pipeline.

### 13.5 Judge prompt and output

Each judge receives, according to the task:

- NL statement `N`;
- reference Lean `R`, when available;
- candidate Lean `C`;
- normalized pretty-printed versions;
- optional typechecking status;
- no generator identity;
- no labels from other judges.

Require structured JSON:

```json
{
  "relation": "equivalent_content",
  "faithful": true,
  "error_types": [],
  "confidence": 0.91,
  "reference_problem": false,
  "nl_ambiguous": false,
  "brief_rationale": "..."
}
```

Rationales are stored for audit but are not automatically used as model training text. The primary metric should not learn judge-specific prose artifacts.

### 13.6 Judge ensemble policy

Recommended default:

- two independent strong judges from different model families;
- a third tie-break judge when labels disagree or confidence is low;
- optional specialized formalization judge;
- deterministic/low-temperature judging;
- prompt-order randomization and statement-order swaps to detect bias.

The final weak label is produced by a calibrated aggregation model, not raw majority vote alone.

### 13.7 Human calibration of judges

Before using millions of weak labels:

1. sample a balanced set across sources, error types, generators, and difficulty;
2. obtain expert human labels;
3. estimate each judge's confusion matrix and calibration;
4. measure judge correlation, not only individual accuracy;
5. test order, verbosity, and reference-bias effects;
6. down-weight highly correlated judge families;
7. set an abstention rule for weak-label aggregation.

Repeat the calibration whenever a judge model or prompt version changes.

### 13.8 Preventing generator/judge shortcuts

- Strip model comments and boilerplate from model input.
- Normalize theorem names and formatting.
- Balance generator families across labels.
- Create a generator-model holdout split.
- Create a judge-model holdout audit where labels from one judge family are not used for training.
- Include faithful and unfaithful outputs from every major generator.
- Never encode provider/model IDs in model-facing features.
- Report performance by generator model to expose model-fingerprint learning.

### 13.9 Token and cost ledger

Every generation/judging call records:

- provider;
- model and revision/date;
- prompt template and hash;
- decoding parameters;
- input/output tokens;
- latency;
- cost estimate;
- retry count;
- raw response hash;
- parse status;
- safety/filter status;
- linked theorem/group IDs.

Use a global budget controller with per-stream quotas. Prefer active-learning and diversity sampling over indiscriminate generation.

### 13.10 LLM data gate

Before large-scale generation:

- at least two translators and two independent judges integrated;
- 1,000 candidate pilot completed;
- all raw outputs and token ledgers reproducible;
- typechecking pipeline handles parse/repair failures without silent loss;
- judge agreement and human-calibrated accuracy reported;
- no benchmark leakage in prompts;
- model-fingerprint classifier baseline measured; if it predicts labels well, rebalance or normalize data.

---

## 14. Evidence, labels, and human annotation

### 14.1 Evidence hierarchy

Keep evidence separate from final labels. Recommended quality tiers:

| Tier | Name | Typical source | Training use |
|---|---|---|---|
| A | `gold_human` | expert consensus/adjudication or trusted benchmark | full weight; calibration/eval depending split |
| B | `certified_positive` | alpha/defeq/approved bidirectional certificate | full positive weight |
| B | `gold_relation` | human stronger/weaker/near-miss relation | full weight |
| C | `high_confidence_weak` | calibrated multi-judge agreement plus provenance | reduced weight |
| D | `mutation_prior` | meaning-changing mutation proposal only | unlabeled/contrastive or very low weight |
| U | `unknown` | insufficient/conflicting evidence | exclude from supervised label loss; retain for active learning |

There is deliberately no broad `certified_negative` tier. Content non-equivalence of true theorem statements is not generally certifiable by failed logical equivalence.

### 14.2 Definitional equality evidence

Implement a Lean-side check over elaborated theorem types. Record:

- success/failure/error;
- transparency mode;
- normalization options;
- environment;
- elapsed time.

A success is high-precision positive evidence. A failure is only `not_defeq`, not a negative label.

### 14.3 Proof-search evidence

Run controlled checks for:

- reference-to-candidate;
- candidate-to-reference;
- official BEq/BEq+ baseline behavior;
- limited trusted tactic portfolios.

Required safeguards:

- use fresh generated names;
- record whether either theorem constant is available;
- check whether the target is independently provable without the source theorem;
- cap tactic time;
- preserve exact proof code/output;
- label timeout/failure as unknown;
- separate “official baseline replication” from “leakage-safe auxiliary feature.”

### 14.4 Counterexample evidence

Counterexamples are attempted only in suitable fragments:

- finite decidable domains;
- concrete arithmetic instantiations;
- small bounded structures;
- executable predicates with trusted evaluation.

A counterexample may show non-equivalence of parameterized predicates or expose a mutation, but absence of a counterexample proves nothing. Record search bounds and method.

### 14.5 Weak-supervision aggregation

Start with transparent baselines:

1. majority vote among calibrated judges;
2. confidence-weighted vote;
3. logistic/label model using evidence features;
4. optional Dawid–Skene/Snorkel-style model.

Features may include:

- judge labels/confidences;
- generator/editor provenance;
- transformation intended relation;
- typechecking status;
- defeq success;
- proof-search directional results;
- edit distances;
- source/domain.

Do not use external test labels to fit the label model. Validate aggregation on a dedicated human-labeled calibration set.

### 14.6 Human annotation interface

Annotators should see:

- NL statement, if available;
- reference Lean;
- candidate Lean;
- source-canonical and semantic-compact views;
- an optional structured diff with changed constants, binders, and operators;
- typechecking status;
- no generator/judge identities;
- no prefilled automated label.

Required responses:

1. faithful: `yes / no / unclear`;
2. relation category;
3. error types;
4. reference problem flag;
5. NL ambiguity flag;
6. confidence 1–5;
7. optional note.

### 14.7 Annotation protocol

- Train annotators on written guidelines and a qualification set.
- Use at least two independent annotations per item.
- Adjudicate disagreements with a third expert or panel.
- Randomize pair order for symmetric-equivalence items.
- Insert repeated consistency checks.
- Track time and confidence for quality analysis.
- Permit `unclear`; do not force noisy binary labels.
- Version guidelines and retain prior annotations when guidelines change.

### 14.8 Human set partitioning

Create distinct sets:

- `human_pilot`: improve instructions and taxonomy;
- `judge_calibration`: fit/assess weak-label aggregation;
- `model_calibration`: fit temperature/thresholds after training;
- `internal_validation`: model selection and error analysis;
- `sealed_test`: opened only for final experiments.

No annotation from `sealed_test` can influence prompts, labels, model selection, or thresholds.

### 14.9 Sampling for annotation

Use stratified and active sampling:

- equalize positive/negative/relation classes;
- include every major error type;
- include every generator and source;
- include high and low edit distance;
- oversample judge disagreements;
- oversample model uncertainty and baseline disagreements;
- reserve a random population sample for unbiased prevalence estimates.

### 14.10 Label gate

Before supervised training:

- evidence storage is complete and recomputable;
- at least 500 human pilot pairs with agreement analysis;
- weak-label aggregator evaluated against human labels;
- all unknowns preserved as unknown;
- label-quality tiers assigned by deterministic policy;
- a random audit of 200 certified positives passes;
- a random audit of 200 weak negatives reports acceptable noise or is down-weighted/excluded.

---

## 15. Dataset splitting, deduplication, and contamination control

### 15.1 Unit of splitting

Never split individual rows independently. Build a `cluster_id` that groups all variants connected to the same underlying claim.

Cluster signals:

- source row/problem ID;
- normalized NL hash;
- reference theorem ID;
- expression-canonical similarity;
- transformation ancestry;
- candidate group ID;
- source file/namespace;
- near-duplicate embedding/MinHash;
- known benchmark provenance.

Every member of a cluster belongs to one split.

### 15.2 Required split families

1. **IID grouped split:** cluster-safe split from main training distribution.
2. **Transformation-family holdout:** entire mutation/positive families absent from training.
3. **Generator holdout:** outputs from at least one generator model family absent from training.
4. **Source-project holdout:** CSLib/PhysLib or another project fully held out.
5. **Domain holdout:** selected mathematics domains held out by file/namespace taxonomy.
6. **Temporal/version holdout:** later library commit or source snapshot.
7. **External benchmarks:** untouched benchmark-native test sets.
8. **Human sealed test:** independent gold set.

### 15.3 Deduplication levels

Run all of:

- exact raw-string hash;
- proof-free surface hash;
- surface-canonical hash;
- expression-canonical hash;
- semantic-compact hash;
- approximate token MinHash;
- approximate operator-tree similarity;
- NL normalized hash and embedding similarity.

Exact canonical duplicates must not cross splits. Approximate duplicate thresholds are tuned on audited samples and reported.

### 15.4 Prompt contamination

- Maintain a registry of all few-shot examples.
- Hash and compare them against benchmark and sealed clusters.
- Keep benchmark names out of generic prompts when possible.
- Do not ask judges to recall benchmark labels.
- Record provider-side fine-tuning uncertainty as a limitation; evaluate robustness with newly created human examples.

### 15.5 Generator/judge contamination

- Separate generator and judge roles by model family in at least one experiment.
- Evaluate on a generator unseen during training.
- Evaluate weak-label distillation with the labeling judge excluded from baseline comparisons when possible.
- Never claim superiority over a judge using test labels produced solely by that same judge.

### 15.6 Split manifest

A frozen split is a versioned manifest containing:

- dataset version;
- all cluster IDs and split assignments;
- source/environment counts;
- label-tier counts;
- relation/error counts;
- duplicate-check report;
- contamination-check report;
- creation code commit;
- random seed;
- artifact hashes.

Changing a split creates a new dataset version.

### 15.7 Split gate

- zero exact canonical duplicate leakage;
- zero shared transformation ancestry across splits;
- zero benchmark cluster in training/prompts;
- documented approximate-duplicate audit;
- class/source/generator distribution report;
- all splits reproducible from the manifest.

---

## 16. Model plan

### 16.1 Stage 0: non-learned and simple learned baselines

Implement these before the main model:

- exact raw match;
- surface-canonical match;
- expression-canonical match;
- token edit similarity;
- bag-of-constants similarity;
- operator-tree edit metric;
- definitional equality;
- BEq/BEq+ replication;
- GTED/TransTED-style metric if implementation is available and reproducible;
- frozen embedding cosine baseline;
- logistic regression/gradient boosting over handcrafted features;
- LLM judge baselines with fixed prompts.

### 16.2 Stage 1: token pair model

The user's intended “decoder” is implemented as a **non-autoregressive cross-attention comparison module**, not a text-generating decoder.

Compare two architectures:

#### A. Concatenated cross-encoder

```text
[CLS] <REF_RAW> ... <REF_CANON> ... [SEP]
      <CAND_RAW> ... <CAND_CANON> ... [SEP]
```

A single transformer allows full attention between both statements.

#### B. Shared encoders plus bidirectional cross-attention

```text
R tokens → shared encoder → H_R ┐
                                ├→ cross-attention blocks → pair representation
C tokens → shared encoder → H_C ┘
```

Use decoder-style attention blocks only as comparison layers. There is no causal mask and no token-generation objective.

The baseline model should be selected from well-supported open encoder checkpoints after a small tokenizer/context/license audit. Architecture and checkpoint are configuration values, not hard-coded.

### 16.3 Input views

Initial ablations:

1. raw statement only;
2. surface-canonical only;
3. raw + surface-canonical;
4. semantic-compact only;
5. raw + semantic-compact;
6. raw + semantic-compact + symbolic scalar features.

Do not feed source proofs, generator IDs, judge rationales, final labels, or benchmark metadata.

### 16.4 Multi-task heads

Required heads:

- binary `faithful/equivalent_content`;
- multiclass relation;
- multi-label error type;
- optional directional implication auxiliary heads;
- optional difficulty/source adversarial head used only to reduce shortcut learning.

Suggested total loss:

```text
L = λ_eq L_equivalence
  + λ_rel L_relation
  + λ_err L_error_types
  + λ_dir L_directional_aux
  + λ_cons L_swap_consistency
  + λ_cal L_calibration_regularizer
```

Only examples with valid labels contribute to each head. For example, certified positives contribute to equivalence but may not contribute to directional heads.

### 16.5 Symmetry and swap consistency

Equivalence should be symmetric; stronger/weaker should swap.

Training and evaluation must include `(R, C)` and swapped `(C, R)` views. Add consistency constraints:

```text
p_equiv(R,C) ≈ p_equiv(C,R)
p_stronger(R,C) ≈ p_weaker(C,R)
p_weaker(R,C) ≈ p_stronger(C,R)
```

At inference, optionally average symmetric equivalence scores from both orders.

### 16.6 Label-quality weighting

Initial weights:

- human/benchmark gold: `1.0`;
- certified positive: `1.0` for positive equivalence head;
- high-confidence weak: tune in `[0.25, 0.75]`;
- mutation-prior only: no direct supervised class loss; use contrastive/ranking or active-learning pool;
- unknown: no class loss.

Run ablations rather than assuming more weak data is always helpful.

### 16.7 Curriculum

1. cosmetic/alpha positives versus easy negatives;
2. binder/logical positives and local hard mutations;
3. composed transformations;
4. LLM-edited pairs;
5. fresh autoformalization candidates;
6. human/benchmark fine-tuning with low learning rate;
7. calibration on a separate split.

Interleave old and new stages to avoid catastrophic forgetting.

### 16.8 Hard-negative mining

After the first model:

- find high-scoring known negatives;
- find low-scoring certified positives;
- find disagreements with BEq+/GTED/LLM judges;
- find high-uncertainty unlabeled pairs;
- send a stratified subset for human review;
- add only reviewed or sufficiently evidenced items to the next training version.

Never self-label hard examples solely from the model's own prediction.

### 16.9 Hybrid symbolic model

Add scalar/categorical features from:

- typechecking status;
- defeq result;
- proof-search directional results and timeouts;
- token/tree edit distances;
- constant overlap;
- binder and quantifier deltas;
- domain/type changes;
- statement lengths.

Fuse after the pair representation through a small MLP. Compare learned-only, symbolic-only, and hybrid versions.

### 16.10 Stage 2: expression graph model

After the token baseline:

1. encode each theorem expression graph with shared weights;
2. add cross-graph alignment/attention;
3. fuse graph and token pair representations;
4. keep the same output heads and split protocol.

Ablations:

- tree only;
- graph only;
- token only;
- token + graph;
- graph without type edges;
- graph without binder edges;
- graph with/without global-name embeddings.

The graph stage is successful only if gains persist on external/OOD data, not just synthetic transformations.

### 16.11 Stage 3: direct NL–Lean model

Architecture:

```text
NL encoder → H_N ┐
                 ├→ bidirectional cross-attention → faithfulness representation
Lean encoder → H_C┘
```

Possible extensions:

- tri-input `(N, R, C)` model when a reference is available;
- distillation from the Lean–Lean model for candidate pairs;
- joint contrastive alignment between NL and canonical Lean;
- error-type generation as a separate optional model, not required for the metric.

Training data must contain NL. Do not pretend mathlib-only synthetic pairs train direct NL faithfulness unless linked to generated/curated NL.

### 16.12 Calibration and abstention

Fit calibration only after model selection, on a dedicated calibration set.

Compare:

- temperature scaling;
- vector scaling;
- isotonic regression for the binary score;
- small ensembles if compute permits.

Report:

- Brier score;
- expected calibration error;
- adaptive calibration error;
- reliability diagrams;
- risk–coverage curves;
- precision at fixed coverage;
- coverage at fixed 95% or 99% precision.

Operational decisions:

- `accept`: high calibrated faithfulness probability;
- `reject`: high calibrated unfaithfulness probability;
- `review`: intermediate uncertainty;
- `abstain`: OOD or low-confidence condition.

### 16.13 OOD detection

Use simple signals first:

- encoder embedding distance to training data;
- source/domain classifier uncertainty;
- disagreement between model variants/order swaps;
- unusually high token/graph length;
- unseen global constants/domain distribution;
- calibration degradation by source.

OOD signals influence abstention, not the raw semantic label.

### 16.14 Model acceptance gate

A model can be called the V1 LeanFaith metric only when:

- it beats simple edit and canonical-match baselines on internal gold validation;
- swap-consistency errors are within a documented tolerance;
- calibration is materially better after scaling;
- no major source/generator slice collapses unnoticed;
- external benchmark evaluation is completed once, after model/config freeze;
- a model card documents training data, weak labels, limitations, and intended use.

---

## 17. Evaluation plan

### 17.1 Evaluation sets

Use:

- grouped internal IID validation/test;
- human sealed test;
- ProofNetVerif or the latest compatible external benchmark split;
- transformation-family holdout;
- generator-family holdout;
- math-domain holdout;
- CSLib/PhysLib project holdout;
- adversarial contrast sets;
- newly created post-training examples to reduce memorization concerns.

### 17.2 Classification metrics

Report at minimum:

- accuracy;
- macro/micro F1;
- per-class precision/recall/F1;
- AUROC;
- AUPRC, especially under realistic class prevalence;
- Matthews correlation coefficient;
- confusion matrix;
- calibration metrics;
- selective risk/coverage.

For multi-label error types:

- macro/micro F1;
- per-error average precision;
- top-k recall;
- exact-match rate where meaningful.

### 17.3 Pairwise/ranking metrics

For candidate groups:

- semantic pass@1;
- top-k faithful recall;
- mean reciprocal rank;
- NDCG when graded labels are available;
- pairwise ranking accuracy;
- oracle gap;
- performance versus number of candidates.

### 17.4 Robustness tests

- theorem-name/random-name changes;
- formatting changes;
- alpha renaming;
- added comments;
- statement order swaps;
- benign notation changes;
- adversarial one-token semantic changes;
- long statements;
- rare constants;
- changed environment/version;
- source-specific formatting.

### 17.5 Baseline comparison protocol

Every baseline receives the same pair set and environment where applicable. Report:

- predictive quality;
- coverage/abstention;
- CPU/GPU time;
- p50/p95 latency;
- memory;
- monetary cost for API judges;
- determinism/repeatability;
- implementation failures.

For LLM judges, use repeated runs or deterministic settings and report model/prompt versions. For BEq+, distinguish “not proven equivalent” from a predicted negative in analysis.

### 17.6 Statistical analysis

- bootstrap 95% confidence intervals over source/problem clusters;
- paired bootstrap or permutation tests for model comparisons;
- McNemar's test for paired binary errors where suitable;
- multiple-comparison correction for large ablation families;
- learning curves over data volume and label quality;
- inter-annotator agreement with confidence intervals.

Cluster by underlying NL/problem, not pair row, to avoid overstating sample size.

### 17.7 Error analysis

Produce a report grouped by:

- relation class;
- error type;
- mathematical domain;
- theorem length;
- binder/quantifier count;
- transformation family;
- generator;
- source project;
- edit-distance bucket;
- proof-search outcome;
- calibration bucket;
- model/baseline disagreement.

Every paper table should have linked example IDs and reproducible prediction artifacts.

### 17.8 Downstream reranking experiment

Protocol:

1. Select a sealed NL problem set.
2. Generate `k` candidates per problem from one or more autoformalizers.
3. Preserve all raw candidates.
4. Typecheck with LeanInteract.
5. Score compiling candidates with each metric/baseline.
6. Select top candidate.
7. Obtain benchmark/human semantic labels independently of the metric.
8. Report semantic pass@1 and cost.

Comparators:

- first candidate;
- random compiling candidate;
- generator log-probability;
- shortest/longest candidate;
- structural metric;
- BEq+ when reference exists;
- LLM judge;
- LeanFaith token model;
- LeanFaith hybrid/graph model;
- oracle.

Also evaluate whether LeanFaith can reduce judge cost by sending only uncertain cases to an LLM/human.

### 17.9 Applicability beyond reranking

Secondary applications:

- filter noisy autoformalization training data;
- prioritize human review;
- mine difficult contrastive examples;
- cluster semantically similar candidate translations;
- provide error-type hints for iterative repair;
- act as a reward signal after careful reward-hacking analysis.

Do not claim proof certification. The metric is probabilistic and should abstain on uncertain/OOD inputs.

---

## 18. Implementation roadmap with hard gates

### Milestone 0 — Repository, pins, and environment doctor

**Tasks**

- create the repository layout;
- initialize Python 3.11 and `uv`;
- pin `lean-interact==0.11.4` and create `uv.lock`;
- create the local Lean project and pin `lean-toolchain`/`lake-manifest.json`;
- add Ruff, type checking, Pytest, pre-commit, and CI;
- implement `leanfaith doctor`;
- implement environment and run manifests;
- document hardware/storage expectations.

**`doctor` must check**

- Python/package versions;
- `git`, `elan`, `lake`, and Lean availability;
- LeanInteract import/version;
- local project build state;
- `lean-toolchain` and manifest hashes;
- one `Command` execution;
- one declaration extraction;
- one `FileCommand` execution;
- write permission for data/cache paths;
- available CPU, RAM, disk, and optional GPU.

**Gate M0**

- `uv sync --frozen` succeeds;
- `lake build` succeeds;
- unit CI passes;
- `leanfaith doctor --json` reports success on a clean machine/environment.

### Milestone 1 — LeanInteract execution layer

**Tasks**

- implement protocols and result schemas;
- implement `LeanInteractBackend`;
- normalize messages, declarations, InfoTrees, exceptions, and timings;
- implement content-addressed cache;
- implement `AutoLeanServer` lifecycle;
- implement `LeanServerPool` batch mode;
- implement unique declaration names;
- implement retry policy;
- add fallback CLI module but leave disabled;
- adapt the official LeanInteract typecheck, BEq+, and declaration-extraction examples into tests/benchmarks with attribution.

**Tests**

- valid theorem;
- syntax error;
- type error;
- expected `sorry` accepted;
- certificate with `sorry` rejected;
- timeout and recovery;
- server crash simulation where possible;
- declarations and InfoTrees parsed;
- pool ordering and exception normalization;
- stale imported file requires restart;
- cache key changes with environment/package/code/options;
- no imports of `subprocess` outside fallback module.

**Gate M1**

- Section 6.11 acceptance gate passes;
- 10,000-request stress report committed as an artifact manifest;
- unresolved infrastructure failure rate below threshold.

### Milestone 2 — Source adapters and extraction

**Tasks**

- implement source registry and manifests;
- inspect `sft_classic` schema and require approved mapping;
- implement streaming user-dataset adapter;
- implement generic Lean-project file discovery;
- implement mathlib per-file declaration extraction;
- implement proof-free signature reconstruction;
- implement rejection records;
- extract pilot corpus;
- manually audit 100 examples.

**Gate M2**

- Section 11.8 extraction gate passes;
- source and environment reports are reproducible;
- no proof leakage into model-facing fields.

### Milestone 3 — Canonical representations

**Tasks**

- implement surface canonicalization;
- implement custom Lean expression JSON exporter;
- implement alpha normalization;
- implement selective expansion profiles;
- implement operator trees;
- implement constant and binder summaries;
- implement representation hashes;
- create metamorphic invariant tests.

**Gate M3**

- all Section 9.7 invariants pass;
- 1,000-record repeated export is byte-identical;
- representation failures have explicit reason codes;
- no unbounded expansion.

### Milestone 4 — Deterministic transformations and evidence

**Tasks**

- implement transformation DSL and registry;
- implement P0/P1 first;
- implement dependency-aware binder reordering;
- implement two logical and two notation/algebra positive rules;
- generate Lean certificates;
- implement initial N0/N1/N2/N5 mutations;
- enforce typechecking after every edit;
- implement contrast-set matching;
- store evidence and provenance;
- manually audit generated pairs.

**Gate M4**

- at least 10,000 certified positives;
- at least 20,000 compiling mutation candidates;
- zero certificate `sorry` usage;
- 200-positive audit passes;
- mutation accident rate measured, not assumed;
- transform-family unit and metamorphic tests pass.

### Milestone 5 — LLM generation and weak judging

**Tasks**

- implement provider-neutral generation interface;
- implement translator/editor/repairer/judge roles;
- version prompts and schemas;
- integrate at least two translator and two judge families;
- implement token/cost ledger;
- typecheck all candidates with LeanInteract;
- implement deduplication;
- run 1,000-candidate pilot;
- obtain human calibration labels;
- fit simple weak-label aggregators.

**Gate M5**

- Section 13.10 gate passes;
- weak-label performance and calibration reported;
- no judge output is treated as gold;
- no sealed benchmark example appears in prompts.

### Milestone 6 — Human annotation and label aggregation

**Tasks**

- finalize annotation guidelines;
- build annotation export/UI;
- conduct annotator qualification;
- complete pilot annotations;
- compute agreement;
- adjudicate disagreements;
- implement evidence aggregation and quality tiers;
- create active-learning sampler;
- freeze human calibration and sealed-test IDs.

**Gate M6**

- Section 14.10 gate passes;
- annotation guidelines and adjudication log are versioned;
- human sealed test is access-controlled/frozen.

### Milestone 7 — Dataset V1 and splits

**Tasks**

- scale deterministic and LLM candidate generation;
- run full deduplication;
- construct cluster IDs;
- build all required split families;
- run contamination audits;
- freeze Parquet/JSONL datasets and manifests;
- write dataset card.

**Gate M7**

- Section 15.7 gate passes;
- dataset can be rebuilt from source and run manifests;
- all counts/hashes are frozen;
- sealed test labels remain inaccessible to training code.

### Milestone 8 — Baselines and token model

**Tasks**

- implement all simple baselines;
- reproduce official LeanInteract BEq+ behavior on a sample;
- implement concatenated cross-encoder;
- implement shared-encoder bidirectional cross-attention model;
- implement multi-task heads and quality weighting;
- train on V1 splits;
- run calibration and slice analysis;
- perform hard-negative mining only through reviewed evidence.

**Gate M8**

- all baseline outputs stored under one evaluation schema;
- model acceptance gate in Section 16.14 passes;
- model card and reproducible training config exist;
- no sealed external evaluation was repeatedly tuned against.

### Milestone 9 — Graph and direct NL–Lean models

**Tasks**

- build graph tensors from deterministic expression JSON;
- train graph-only and fused models;
- run graph ablations;
- implement NL–Lean cross-attention model;
- train on real NL candidate groups and human labels;
- calibrate separately;
- compare reference-based, direct, and joint models.

**Gate M9**

- graph value assessed on external/OOD slices;
- direct NL–Lean model beats trivial NL/Lean embedding baselines;
- no claim of improvement without confidence intervals.

### Milestone 10 — Applicability and research release

**Tasks**

- run sealed reranking experiment;
- evaluate cost/latency;
- implement scorer/reranker service;
- create final ablations and error analysis;
- prepare release manifests, dataset/model cards, and documentation;
- write paper with limitations and negative results;
- archive exact code/data/model versions.

**Gate M10**

- downstream evaluation is independently labeled;
- final tables reproduce from scripts;
- release passes license/privacy review;
- all reported numbers link to immutable prediction artifacts.


---

## 19. CLI contracts, configuration, and artifacts

### 19.1 Required CLI

```text
leanfaith doctor
leanfaith env list
leanfaith env fingerprint <environment-id>
leanfaith extract --source <source-id> --limit <n>
leanfaith represent --dataset-version <id>
leanfaith check-code --environment <id> --file <path>
leanfaith generate deterministic --config <path>
leanfaith generate llm --config <path>
leanfaith evidence collect --dataset-version <id>
leanfaith annotation export --batch <id>
leanfaith labels aggregate --config <path>
leanfaith splits build --config <path>
leanfaith train pair --config <path>
leanfaith train graph --config <path>
leanfaith train nl-lean --config <path>
leanfaith evaluate --config <path>
leanfaith rerank --config <path>
leanfaith cache inspect
leanfaith cache purge --scope <safe-scope>
```

Every command supports:

- `--run-id` or automatic UUID;
- `--config-dump`;
- `--dry-run` where meaningful;
- `--resume`;
- `--force` with explicit warning;
- `--json-logs`;
- deterministic seed;
- output manifest path.

### 19.2 Run manifest

Each command writes:

```json
{
  "run_id": "...",
  "command": "extract",
  "started_at": "...",
  "finished_at": "...",
  "git_commit": "...",
  "dirty_worktree": false,
  "python_lock_sha256": "...",
  "leaninteract_version": "0.11.4",
  "environment_fingerprints": ["..."],
  "config_sha256": "...",
  "random_seeds": {"python": 0, "numpy": 0, "torch": 0},
  "input_artifacts": [{"path": "...", "sha256": "..."}],
  "output_artifacts": [{"path": "...", "sha256": "..."}],
  "counts": {"processed": 0, "accepted": 0, "rejected": 0},
  "failure_counts": {},
  "hardware": {},
  "status": "success"
}
```

### 19.3 Cache design

Use content-addressed keys:

```text
sha256(
  operation_name,
  leaninteract_version,
  environment_fingerprint,
  command_or_file_content_hash,
  request_options,
  custom_lean_module_hash,
  schema_version
)
```

Cache separately:

- Lean execution results;
- declaration extraction;
- expression exports;
- transformations;
- LLM calls;
- model predictions.

Requirements:

- atomic write then rename;
- corruption checksum;
- human-readable index in DuckDB/SQLite;
- no reuse across changed environment/module/schema;
- explicit cache-hit field in results;
- safe scoped purge, never blind `rm -rf` from user input.

### 19.4 Artifact naming

```text
<artifact-kind>/<dataset-or-run>/<version>/<shard-or-name>.<ext>
```

Examples:

```text
data/extracted/sft_classic/v1/theorems-00012.parquet
data/evidence/pairs-v1/evidence-00003.parquet
artifacts/models/pair-cross-attn/v1/checkpoint-best/
artifacts/predictions/proofnetverif/model-v1.parquet
artifacts/reports/reranking/run-<uuid>/summary.json
```

No mutable `latest` artifact is used in paper scripts; paper configs refer to immutable versions/hashes.

---

## 20. Testing, quality assurance, and observability

### 20.1 Test layers

#### Unit tests

- schema validation;
- hash stability;
- source-field mapping;
- normalization utilities;
- transformation applicability;
- label aggregation;
- split grouping;
- metric calculations.

Use a fake Lean backend; no network or Lean installation required.

#### LeanInteract integration tests

- real `Command` and `FileCommand` requests;
- declaration and InfoTree parsing;
- local/temporary project setup;
- timeouts and recovery;
- server pool;
- cache invalidation;
- custom Lean expression exporter.

#### Metamorphic tests

- approved positives preserve canonical/label expectations;
- semantic mutations alter expected nodes;
- swap consistency;
- transformation composition;
- proof certificates contain no placeholders.

#### Data-quality tests

- required fields and foreign keys;
- environment equality inside a pair;
- no duplicate leakage;
- label/evidence consistency;
- generator/judge IDs absent from model fields;
- proof bodies absent;
- source counts and rejection rates within expected ranges.

#### Model tests

- deterministic tiny-batch overfit;
- forward/backward shapes;
- masked loss by available labels;
- swap consistency;
- checkpoint save/load equality;
- calibration serialization;
- inference API schema.

#### End-to-end smoke test

A tiny checked-in fixture must execute:

```text
source rows
→ LeanInteract extraction
→ canonical views
→ one positive + one mutation
→ evidence records
→ split
→ tiny model train
→ prediction
→ reranking report
```

### 20.2 CI tiers

- **PR CI:** lint, typecheck, unit tests, tiny model tests.
- **Lean integration CI:** small pinned project and LeanInteract smoke tests; may be separate/cached.
- **Nightly CI:** mathlib sample extraction, pool stress, metamorphic suite.
- **Release CI:** full dataset validation, split contamination checks, model artifact verification.

### 20.3 Golden tests

Store small versioned golden artifacts for:

- LeanInteract response normalization;
- declaration extraction;
- expression JSON;
- surface/semantic canonical forms;
- certificate code;
- pair schema;
- model inference output.

When a golden file changes, the PR must explain the semantic reason.

### 20.4 Logging and monitoring

Structured logs include:

- run ID;
- operation and item ID;
- environment fingerprint;
- Lean status;
- timeout/retry;
- latency;
- cache hit;
- memory where available;
- source/transformation/generator without exposing it to model features.

Dashboards/reports should track:

- extraction throughput and rejection reasons;
- Lean p50/p95 latency and crash rate;
- transformation yield and accidental-positive rate;
- LLM parse/typecheck/repair rates;
- judge agreement;
- annotation disagreement;
- class/source/generator balance;
- model calibration and slice metrics;
- token and monetary cost.

### 20.5 Coding standards

- Python functions and public classes are typed.
- Pydantic models validate external data at boundaries.
- No bare `except` in production pipeline code.
- Exceptions include item/run/environment context.
- Randomness flows through explicit seeded generators.
- Large jobs support resume from shard manifests.
- Data writes are atomic.
- No secret/API key enters logs or artifacts.
- All third-party adapted code retains license and attribution.

### 20.6 Pull-request definition of done

Every implementation PR must include:

- scoped code change;
- tests for success and failure paths;
- documentation/config update;
- no new lint/type errors;
- sample command and expected artifact;
- migration note for schema changes;
- performance impact for hot paths;
- reproducibility note.

---

## 21. Risk register and mitigations

### Risk 1 — Synthetic-to-real gap

**Failure mode:** Excellent transformation accuracy, poor real autoformalization judgments.

**Mitigation:** Introduce fresh model candidates early; make real candidate groups and human labels dominate validation/test; report synthetic and real slices separately; use generator holdouts and active learning.

### Risk 2 — Negative-label noise

**Failure mode:** Type-aware mutations accidentally preserve content, or judge consensus is wrong.

**Mitigation:** Keep mutation proposals unlabeled until evidence; audit accident rates; use confidence weighting; support unknown; prioritize human review of hard cases; consider positive-unlabeled/robust-loss experiments.

### Risk 3 — Truth-level equivalence collapse

**Failure mode:** Proof search declares distinct true statements equivalent.

**Mitigation:** Define claim faithfulness explicitly; use proof search as evidence/baseline only; detect independent provability; evaluate on operator/domain/strength near misses.

### Risk 4 — Proof-search false negatives

**Failure mode:** Equivalent statements time out or exceed tactic capability.

**Mitigation:** Never treat failure as negative; store timeout separately; calibrate proof-feature use; include certified transformations beyond tactic reach.

### Risk 5 — Generator/model fingerprint shortcuts

**Failure mode:** Classifier recognizes which system or prompt produced a candidate.

**Mitigation:** normalize wrappers/names/formatting; balance labels per generator; generator holdout; train a fingerprint probe; adversarially remove source signal if necessary.

### Risk 6 — Environment drift

**Failure mode:** Results change after Lean/mathlib/LeanInteract updates.

**Mitigation:** immutable environment fingerprints; lockfiles; local pinned projects; compatibility PR for upgrades; golden tests; no cross-version cache reuse.

### Risk 7 — Stale incremental imports

**Failure mode:** LeanInteract reuses imported files after they changed.

**Mitigation:** hash custom Lean modules; restart server/pool on module hash change; stale-import integration test; no hot editing during a frozen run.

### Risk 8 — Hidden state leakage

**Failure mode:** A preceding command or theorem constant makes a candidate/proof check succeed.

**Mitigation:** branch every item from a clean/base environment; unique names; do not chain candidate environments; record available declarations; isolated rechecks for suspicious positives.

### Risk 9 — Over-normalization

**Failure mode:** Canonicalization removes semantically important distinctions.

**Mitigation:** retain raw and multiple views; whitelist expansion; semantic invariant tests; manual audit; never overwrite raw data.

### Risk 10 — Under-normalization

**Failure mode:** Model overweights theorem names, local names, or formatting.

**Mitigation:** alpha/cosmetic augmentation; canonical views; invariance tests; adversarial formatting evaluation.

### Risk 11 — Pool/OOM instability

**Failure mode:** Too many Lean servers exhaust memory and corrupt throughput.

**Mitigation:** benchmark worker counts; conservative defaults; AutoLeanServer memory thresholds; bounded retries; monitor peak memory; shard jobs.

### Risk 12 — Human annotation bottleneck

**Failure mode:** Gold data is too small or inconsistent.

**Mitigation:** detailed guidelines; structured diffs; active sampling; annotator qualification; allow ambiguous; use human time for calibration/test rather than easy synthetic examples.

### Risk 13 — Benchmark overfitting

**Failure mode:** Repeated evaluation drives decisions on one small benchmark.

**Mitigation:** sealed external run; internal validation; several holdouts; newly collected post-training examples; cluster bootstrap.

### Risk 14 — Graph stage consumes the project

**Failure mode:** Graph engineering delays the usable metric.

**Mitigation:** graph is Milestone 9; no graph work until token baseline and data gates pass; require external/OOD gain to justify complexity.

### Risk 15 — LLM budget waste

**Failure mode:** Billions of tokens produce redundant candidates and correlated judgments.

**Mitigation:** pilot learning curves; semantic dedup; diversity quotas; active learning; per-stream budget; stop streams with low marginal value.

### Risk 16 — Reward hacking in downstream use

**Failure mode:** An autoformalizer learns surface patterns that fool LeanFaith.

**Mitigation:** keep metric test sets sealed; adversarially generate model-targeted examples; periodic human auditing; ensemble with symbolic checks; do not initially optimize a generator directly against the metric.

---

## 22. First coding-agent sprint

The first sprint must implement only the execution foundation. Do not implement transformations or models yet.

### Sprint objective

Produce a reliable, tested LeanInteract-backed service capable of checking snippets and extracting theorem declarations from a pinned local Lean project.

### Ordered tasks

1. Create repository skeleton and `pyproject.toml`.
2. Pin Python 3.11 and `lean-interact==0.11.4`; generate `uv.lock`.
3. Create minimal Lean project with one test theorem file.
4. Implement `EnvironmentFingerprint`, `LeanStatus`, `LeanExecutionResult`, and request models.
5. Implement `leanfaith doctor` with JSON output.
6. Implement `LeanInteractBackend.check_code()` using `AutoLeanServer`.
7. Normalize `CommandResponse`, `LeanError`, timeout, crash, and unexpected exceptions.
8. Implement expected-`sorry` checks and proof-certificate `allow_sorry=False` mode.
9. Implement `extract_file()` using `FileCommand(..., declarations=True, infotree="full", root_goals=True)`.
10. Implement a basic content-addressed cache.
11. Implement `check_many()` with `LeanServerPool`.
12. Add local project, valid/invalid, timeout, declaration, InfoTree, pool, and cache tests.
13. Add stale-import integration test.
14. Add the disabled fallback CLI and a test that no normal path imports it.
15. Run and save a 1,000-request preliminary stress report; then prepare the 10,000-request M1 gate.
16. Write `README` usage examples and update `AGENTS.md` with the non-negotiable rules in Section 0.

### Exact sprint acceptance

The sprint is complete only when this works:

```bash
uv sync --frozen
lake build
uv run ruff check .
uv run pyright
uv run pytest -m "not slow"
uv run leanfaith doctor --json
uv run leanfaith check-code \
  --environment leanfaith_local \
  --file tests/fixtures/valid_theorem.lean
uv run leanfaith extract \
  --source tests_fixture_project \
  --limit 10
```

Expected artifacts:

- doctor report;
- environment manifest;
- normalized Lean execution JSON;
- 10 extracted declaration records;
- cache index;
- integration-test report;
- preliminary stress report.

The agent must not proceed to Milestone 2 if any Lean request can disappear without a result/rejection record.

---

## 23. Project-wide definition of done

The project is complete when:

- LeanInteract is the tested default interface for all Python–Lean operations;
- source extraction and canonicalization are deterministic and environment-pinned;
- data includes certified positives, realistic hard candidates, real translations, and human labels;
- evidence and final labels are separate and recomputable;
- all splits are cluster-safe and contamination-audited;
- a calibrated token pair model and direct NL–Lean model are evaluated;
- the graph extension has a fair ablation, whether positive or negative;
- external benchmark and sealed human results are reported once after freeze;
- reranking improves or clearly characterizes candidate selection;
- cost, latency, calibration, OOD behavior, and limitations are reported;
- code, manifests, prompts, model cards, dataset cards, and scripts reproduce the paper tables.

---

## 24. Resolved defaults and remaining decisions

### 24.1 Resolved defaults

- **Python–Lean library:** LeanInteract, mandatory.
- **Initial LeanInteract pin:** `0.11.4`.
- **Execution:** `AutoLeanServer` for stateful work; `LeanServerPool` for independent batches.
- **Project mode:** pinned `LocalProject` for production; temporary/Git projects for bootstrap and small tests.
- **Primary target:** autoformalization/claim faithfulness, not unrestricted logical equivalence.
- **First model:** token pair model with cross-statement attention; no autoregressive decoding.
- **Graph:** later ablation after token baseline.
- **Negatives:** not derived from proof failure; mutation proposals remain weak/unknown until evidence.
- **LLM judges:** weak supervision only.
- **External benchmarks:** sealed evaluation.
- **Normalization:** multiple views; no global full unfolding.
- **Splitting:** claim/source cluster, not rows.
- **Application:** candidate reranking is the primary demonstration.

### 24.2 Decisions to make after pilots

The following can be selected empirically without blocking the first coding sprint:

- canonical Lean/mathlib environment for the main mixed corpus;
- exact open encoder checkpoint and tokenizer;
- number of Lean workers by hardware;
- final positive/mutation/LLM data ratios;
- weak-label aggregation method;
- human annotation budget;
- graph architecture;
- public-release subset based on licenses;
- final service deployment mechanism.

For each, the default is the simplest option that passes the stated gate.

---

## 25. Key references and implementation anchors

### LeanInteract

- Repository and README: <https://github.com/augustepoiroux/LeanInteract>
- Documentation: <https://augustepoiroux.github.io/LeanInteract/>
- PyPI package: <https://pypi.org/project/lean-interact/>
- Performance and parallelization guide: <https://github.com/augustepoiroux/LeanInteract/blob/main/docs/user-guide/performance.md>
- Data extraction guide: <https://github.com/augustepoiroux/LeanInteract/blob/main/docs/user-guide/data-extraction.md>
- Existing/local/temporary project configuration: <https://github.com/augustepoiroux/LeanInteract/blob/main/docs/user-guide/custom-lean-configuration.md>
- Official mathlib declaration extraction example: <https://github.com/augustepoiroux/LeanInteract/blob/main/examples/extract_mathlib_decls.py>
- Official BEq+ example: <https://github.com/augustepoiroux/LeanInteract/blob/main/examples/beq_plus.py>
- Official typechecking example: <https://github.com/augustepoiroux/LeanInteract/blob/main/examples/type_check.py>

When adapting example code, retain the LeanInteract license/attribution and record the exact release or commit.

### Autoformalization evaluation

- BEq+, ProofNetVerif, ProofNet#, and related reliable-evaluation work.
- FormalAlign or equivalent learned alignment-evaluation work.
- GTED and subsequent structural/semantic tree-distance metrics.
- Human-labeled theorem-statement verification benchmarks.

### Mutation/data generation

- Type-aware operator mutation for SMT solvers, DOI `10.1145/3428261`.
- Generative type-aware mutation for SMT solvers, DOI `10.1145/3485529`.
- Grammar-based enumeration for SMT validation, DOI `10.1145/3689795`.

These papers motivate typed, valid, high-yield mutations. LeanFaith adapts the principle to theorem-statement contrast generation rather than solver differential testing.

---

## Appendix A. Example statement and certificate checks through LeanInteract

### A.1 Statement elaboration

```python
from lean_interact import AutoLeanServer, Command, LeanREPLConfig, LocalProject
from lean_interact.interface import CommandResponse, LeanError

project = LocalProject(directory="/path/to/leanfaith", auto_build=False)
config = LeanREPLConfig(project=project, verbose=False)

code = """
import Mathlib

theorem leanfaith_check_abc (n : Nat) : n = n := by
  sorry
"""

with AutoLeanServer(config) as server:
    response = server.run(Command(cmd=code, declarations=True), timeout=30)
    if isinstance(response, LeanError):
        raise RuntimeError(response.message)
    assert isinstance(response, CommandResponse)
    assert response.lean_code_is_valid(allow_sorry=True)
```

### A.2 Certificate validation

```python
certificate = """
import Mathlib

example (P Q : Prop) : (P ∧ Q) ↔ (Q ∧ P) := by
  constructor
  · rintro ⟨hP, hQ⟩
    exact ⟨hQ, hP⟩
  · rintro ⟨hQ, hP⟩
    exact ⟨hP, hQ⟩
"""

with AutoLeanServer(config) as server:
    response = server.run(Command(cmd=certificate), timeout=30)
    assert isinstance(response, CommandResponse)
    assert response.lean_code_is_valid(allow_sorry=False)
```

Production code wraps these calls in `LeanInteractBackend`; no pipeline code should reproduce ad hoc snippets directly.

---

## Appendix B. Example contrast set

Base:

```lean
theorem base (n : Nat) (h : 0 < n) : n ≤ n + 1 := by
  omega
```

Certified positive, alpha/interface change:

```lean
theorem positive (k : Nat) (hk : k > 0) : k ≤ 1 + k := by
  omega
```

Hard negative candidate, changed relation:

```lean
theorem negative_relation (n : Nat) (h : 0 < n) : n < n := by
  sorry
```

Hard negative candidate, dropped side condition in a theorem where it matters:

```lean
theorem negative_missing_condition (x : Real) : x / x = 1 := by
  sorry
```

The examples illustrate the pipeline rules:

- every candidate must elaborate as a statement;
- positive labels require a certificate or human label;
- mutation intent is recorded;
- a mutation is not automatically a gold negative;
- proof bodies shown in source examples are never model inputs.

---

## Appendix C. Minimum experiment matrix

| Experiment | Training data | Representation | Evaluation |
|---|---|---|---|
| E0 | none | exact/canonical string | all test sets |
| E1 | none | BEq+/proof features | compatible test sets |
| E2 | certified positives + audited negatives | raw tokens | internal + family holdout |
| E3 | E2 + weak LLM pairs | raw + canonical | internal + generator holdout |
| E4 | E3 + real translations | raw + canonical | human + external |
| E5 | E4 + human fine-tune | token cross-encoder | all sealed tests |
| E6 | E5 | token + symbolic | all sealed tests |
| E7 | E5 | graph only | OOD + external |
| E8 | E5 | token + graph | OOD + external |
| E9 | real NL candidate groups | NL–Lean | human + reranking |

Every experiment uses one immutable config and writes predictions for every item, including abstentions and infrastructure failures.

---

## Appendix D. Anti-shortcut checklist

Before training, answer `yes` to all:

- [ ] Theorem names and local binder names are normalized or augmented.
- [ ] Global constants and domains remain visible.
- [ ] Positive and negative pairs are matched by edit difficulty.
- [ ] Every generator produces both faithful and unfaithful examples.
- [ ] Generator identity is absent from inputs.
- [ ] Proof bodies are absent.
- [ ] Benchmark examples are absent from train and prompts.
- [ ] Unknown proof-search outcomes are not negative labels.
- [ ] Mutation proposals are not gold labels without evidence.
- [ ] Pair swaps are included and consistency is tested.
- [ ] Exact and approximate duplicate leakage checks pass.
- [ ] Calibration and sealed test sets are separate.

This checklist is a required artifact in every dataset/model release.

