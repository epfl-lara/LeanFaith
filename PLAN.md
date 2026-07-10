# LeanFaith: Foolproof Research and Implementation Plan

**Working title:** *Learning a Calibrated Faithfulness Metric for Lean 4 Autoformalization*  
**Document purpose:** implementation specification for a coding agent and research roadmap for the project team  
**Status:** coding-agent ready after project-lead approval of the semantic policy examples  
**Revision:** 3.0  
**Last revised:** 2026-07-10  
**Primary Python–Lean interface:** [LeanInteract](https://github.com/augustepoiroux/LeanInteract)  
**Initial LeanInteract pin:** `lean-interact==0.11.4`  

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
- likely error categories;
- calibrated uncertainty and an abstention decision;
- optional symbolic evidence such as definitional equality or a checked proof attempt.

### 1.2 Natural-language–Lean mode

Input:

- natural-language theorem or problem statement `N`;
- candidate Lean theorem statement `C`;
- optional trusted reference Lean statement `R`.

Output:

- probability that `C` is faithful to `N`;
- likely mismatch categories;
- calibrated acceptance, review, or rejection decision;
- optional reference-aware Lean–Lean score when `R` exists.

### 1.3 Required downstream demonstration

The project is not complete after reporting pair-classification metrics. It must show practical value in at least one autoformalization workflow:

1. generate several Lean statement candidates for one natural-language problem;
2. typecheck them;
3. score and rerank them with LeanFaith;
4. demonstrate an improvement in faithful top-1 selection over strong baselines;
5. optionally use predicted error types to prompt a repair model.

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

**Hypothesis H6:** Training with source-project, mutation-family, and generator diversity will improve transfer to held-out autoformalizers and held-out libraries such as CSLib or PhysLib.

---

## 3. Exact semantic target

### 3.1 The core label: same mathematical claim

The primary binary target is:

```text
same_claim(A, B) ∈ {yes, no, ambiguous}
```

`same_claim(A, B) = yes` means that, under the adopted annotation policy, the statements encode the same intended mathematical assertion while allowing non-substantive representational differences.

Allowed differences normally include:

- theorem and binder names;
- comments and whitespace;
- harmless binder grouping;
- explicit versus implicit presentation when elaboration yields the same content;
- standard notation versus its direct elaborated form;
- reordering of independent binders or hypotheses;
- logically transparent interface changes such as currying and uncurrying;
- selected local, reversible reformulations judged to preserve the claim.

Disallowed differences include:

- changing a quantifier;
- changing quantifier order when dependencies or meaning change;
- changing a domain, codomain, structure, coercion, or typeclass assumption;
- dropping or adding a substantive hypothesis;
- making a theorem vacuous;
- changing strict versus non-strict inequality;
- changing a constant, predicate, set operation, bound, index, or numerical value;
- proving only a special case or an overgeneralization;
- replacing the requested assertion by a theorem that is merely also true.

### 3.2 Five notions that must never be conflated

| Notion | Meaning | Use |
|---|---|---|
| `well_typed` | the statement elaborates in its intended environment | gate and feature |
| `defeq` | Lean considers the elaborated types definitionally equal | high-precision evidence/baseline |
| `truth_entails` | Lean proves one proposition from the other under a specified proof policy | directional auxiliary signal |
| `truth_equiv` | Lean proves both directions | auxiliary signal; not the target |
| `same_claim` / `faithful` | the statements represent the same intended assertion | primary target |

A theorem statement can be provable and still be an unfaithful translation. For example, both

```lean
theorem a (n : Nat) : n < n + 1 := by sorry
```

and

```lean
theorem b (n : Nat) : n ≤ n + 1 := by sorry
```

are true, but the second does not faithfully translate an informal statement that explicitly requires strict inequality.

### 3.3 Faithfulness policy levels

Store three policy levels, even if the first model predicts only the middle one:

```text
F0 representation-equivalent
   alpha-renaming, formatting, direct elaboration/notation variants

F1 same-claim equivalent                     ← primary target
   non-substantive, local, reversible reformulations accepted by policy

F2 truth-level equivalent
   mutually derivable propositions, possibly with substantial semantic erasure
```

The model’s main `p_same_claim` targets F1. `defeq` and proof-search features provide evidence about F0/F2 but do not define F1.

### 3.4 Conservative treatment of simplification

Do **not** automatically label broad `simp`, `ring`, `linarith`, theorem lookup, or normalization outputs as same-claim positives. A transformation can turn a meaningful assertion into a tautology while preserving truth. Examples produced by broad semantic simplification belong in an audited `semantic_rewrite_candidate` pool.

Automatically admitted positive transformations must be local, invertible or round-trip checkable, and preserve a defined inventory of semantic atoms unless a rule explicitly specifies an accepted atom mapping.

### 3.5 Directional relation labels

Every non-ambiguous Lean–Lean pair should additionally receive one of:

```text
equivalent
A_stronger
B_stronger
incomparable_near_miss
unrelated
```

The relation is about the mathematical claim under the annotation policy. Proof-search directional results are stored separately.

### 3.6 Natural-language faithfulness

For a natural-language statement `N` and Lean candidate `C`:

```text
faithful(N, C) = yes
```

only when the candidate preserves the intended objects, domains, assumptions, quantifiers, side conditions, and conclusion. A reference Lean statement is useful but not infallible. The data model must permit:

- `reference_trusted = true | false | uncertain`;
- human disagreement with a reference;
- faithful candidates that differ from a flawed or overly specific reference.

---

## 4. Expected scientific contribution

The strong paper version should make four contributions:

1. **Data:** a large Lean 4 statement-pair corpus combining conservative certified positives, typed near misses, real autoformalization outputs, model-proposed variants, and expert labels.
2. **Method:** a calibrated symmetric multi-task cross-attention model for same-claim equivalence, directional relation, and error classification.
3. **Evaluation:** leakage-resistant splits and comparison with string, structural, proof-based, learned, and LLM-judge baselines on real and human-labeled data.
4. **Application:** improved autoformalization reranking and actionable mismatch diagnostics.

The project’s novelty should be framed around the **data construction and evidence discipline**, not simply “a classifier for two Lean strings.”

---

## 5. System architecture

```text
Source datasets and Lean projects
  ├── formalmathatepfl/sft_classic or local variant
  ├── mathlib4
  ├── CSLib / PhysLib / other Lean projects
  ├── ProofNetVerif and other external benchmarks
  └── LLM-generated autoformalization candidates
          │
          ▼
Ingestion and context reconstruction
  ├── source adapter
  ├── imports / opens / namespace / options
  ├── raw declaration and NL statement
  └── immutable source manifest
          │
          ▼
LeanInteract elaboration layer
  ├── Command / FileCommand
  ├── declaration extraction
  ├── typechecking
  ├── InfoTree and root-goal extraction when needed
  ├── incremental elaboration
  └── parallel AutoLeanServer / LeanServerPool execution
          │
          ▼
Multi-view representation
  ├── raw proof-stripped Lean
  ├── headless Lean
  ├── elaborated signature
  ├── explicit/canonical pretty-print
  ├── structural JSON / expression graph
  ├── constants, binders, scope, and context fingerprint
  └── semantic-atom inventory
          │
          ▼
Pair and candidate generation
  ├── conservative deterministic positives
  ├── typed deterministic near misses
  ├── nearby-theorem pairs
  ├── LLM-proposed variants
  └── real NL→Lean samples from multiple generators
          │
          ▼
Evidence collection
  ├── elaboration status
  ├── defeq attempt
  ├── directional proof-search attempts
  ├── counterexample search in supported fragments
  ├── multi-model blinded judging
  └── human annotation
          │
          ▼
Label resolution and quality tiers
  ├── gold certified/reviewed
  ├── gold human/benchmark
  ├── silver consensus
  ├── provisional mutation intention
  └── unknown/abstain
          │
          ▼
Leakage-safe datasets
  ├── training
  ├── model-selection validation
  ├── calibration
  ├── human test
  ├── benchmark test
  └── OOD and held-out-family tests
          │
          ▼
Models and baselines
  ├── symbolic and edit baselines
  ├── raw/normalized cross-encoder
  ├── shared encoder + bidirectional cross-attention
  ├── hybrid symbolic model
  ├── NL–Lean transfer model
  └── optional graph encoder
          │
          ▼
Applications
  ├── candidate reranking
  ├── candidate clustering
  ├── confidence-based acceptance/abstention
  ├── repair hints
  └── active data acquisition
```

---

## 6. Mandatory technology choices

### 6.1 Python

- Use Python `3.12.x` as the initial reference runtime and encode it as `>=3.12,<3.13` in `pyproject.toml`. LeanInteract itself supports older Python versions, but the project should use one frozen minor version for reproducibility.
- `uv` for environment and lockfile management.
- Pydantic v2 for persistent schemas.
- Typer or Click for CLIs.
- PyTorch and Hugging Face Transformers for models.
- PyArrow/Parquet for analytical tables; JSONL for transparent streaming interchange.
- Pytest, Ruff, mypy or Pyright, and pre-commit.

### 6.2 Lean and mathlib

- Pin an exact Lean toolchain and exact mathlib commit.
- Record the toolchain and project commit in every generated artifact.
- Use a local Lean project containing project-specific meta helpers.
- Never silently update mathlib in an existing experiment run.

### 6.3 LeanInteract

LeanInteract is the mandatory Python–Lean boundary. Initial implementation must pin `lean-interact==0.11.4`; a later upgrade requires a compatibility test and an explicit lockfile change.

Use LeanInteract for:

- Lean project setup through `LocalProject`, `GitProject`, `TempRequireProject`, or `TemporaryProject`;
- snippet execution with `Command`;
- source-file execution with `FileCommand`;
- typechecking using `CommandResponse.lean_code_is_valid`;
- declaration extraction with `declarations=True`;
- root goals and InfoTrees when needed;
- long-running batch robustness through `AutoLeanServer`;
- independent batch execution through `LeanServerPool` or the documented one-server-per-worker pattern;
- incremental elaboration of commands sharing an import/header prefix.

A direct shell invocation of Lean is allowed only in a quarantined diagnostic script or CI sanity check, never as the normal production backend.

### 6.4 Experiment tracking and data versioning

- Use immutable run manifests and content hashes as the minimum.
- Use Weights & Biases, MLflow, or an equivalent tracker for model runs.
- Use DVC, lakeFS, object-store manifests, or another versioned data mechanism once the corpus exceeds local-development size.
- Store exact prompt templates and model identifiers for every LLM call.

---

## 7. Repository layout

```text
leanfaith/
  README.md
  PROJECT_PLAN.md
  pyproject.toml
  uv.lock
  lean-toolchain
  lakefile.toml
  lake-manifest.json
  .env.example
  .gitignore
  .pre-commit-config.yaml

  LeanFaith/
    Main.lean
    Meta/
      Canonicalize.lean
      DefEq.lean
      ExprJson.lean
      SemanticAtoms.lean
      ProofChecks.lean
      Counterexamples.lean

  src/leanfaith/
    __init__.py
    cli.py

    config/
      models.py
      loading.py
      paths.py
      logging.py

    schemas/
      enums.py
      source.py
      theorem.py
      pair.py
      evidence.py
      llm.py
      annotation.py
      manifest.py

    lean/
      backend.py
      leaninteract_backend.py
      projects.py
      sessions.py
      commands.py
      responses.py
      extraction.py
      normalization.py
      typecheck.py
      proof_search.py
      counterexample.py
      cache.py

    data_sources/
      base.py
      hf_sft_classic.py
      mathlib.py
      proofnetverif.py
      lean_workbook.py
      cslib.py
      physlib.py
      llm_candidates.py

    transforms/
      base.py
      registry.py
      invariants.py
      positive/
        alpha.py
        binders.py
        interface.py
        notation.py
        propositional.py
      negative/
        operator.py
        hypothesis.py
        quantifier.py
        domain.py
        conclusion.py
        coercion.py
        nearby.py

    generation/
      providers.py
      prompts.py
      variants.py
      autoformalization.py
      retries.py
      budgets.py

    labeling/
      evidence_pipeline.py
      llm_judges.py
      aggregation.py
      quality.py
      human_export.py
      adjudication.py

    datasets/
      build.py
      deduplicate.py
      splits.py
      sampling.py
      freeze.py

    features/
      lexical.py
      structural.py
      symbolic.py
      graph.py

    baselines/
      string.py
      edit.py
      gted.py
      beq_plus_leaninteract.py
      llm_judge.py
      learned.py

    models/
      tokenization.py
      data.py
      cross_encoder.py
      dual_cross_attention.py
      heads.py
      losses.py
      calibration.py
      graph_encoder.py
      nl_lean.py
      train.py
      inference.py

    evaluation/
      metrics.py
      slices.py
      calibration.py
      robustness.py
      reranking.py
      statistics.py
      reports.py

    applications/
      score_pair.py
      score_nl_lean.py
      cluster.py
      rerank.py
      repair.py
      api.py

  configs/
    projects/
    sources/
    transforms/
    generation/
    labeling/
    datasets/
    models/
    evaluation/

  scripts/
    00_doctor.py
    01_probe_source.py
    02_extract.py
    03_normalize.py
    04_generate_deterministic.py
    05_generate_llm.py
    06_collect_evidence.py
    07_export_human_annotation.py
    08_resolve_labels.py
    09_build_splits.py
    10_run_baselines.py
    11_train.py
    12_calibrate.py
    13_evaluate.py
    14_rerank.py

  tests/
    unit/
    integration/
    lean_fixtures/
    golden/

  data/                 # ignored except tiny fixtures
    manifests/
    raw/
    extracted/
    normalized/
    generated/
    evidence/
    labeled/
    splits/
    frozen/
    reports/
```

The old idea of a generic custom `LeanRunner` should be removed. `LeanInteractBackend` is the single production implementation of the backend contract.


---

## 8. LeanInteract integration specification

### 8.1 Why LeanInteract is the default

LeanInteract already provides the capabilities this project needs:

- Python access to the Lean REPL;
- `Command` and `FileCommand` requests;
- structured declarations, messages, sorries, tactics, and InfoTrees;
- project abstractions for local, Git, and temporary Lean projects;
- incremental elaboration for repeated prefixes;
- `AutoLeanServer` recovery behavior;
- `LeanServerPool` and documented parallelization patterns;
- an existing BEq+ example and scalable mathlib declaration-extraction example.

Reimplementing these layers would create unnecessary correctness, caching, timeout, and compatibility risk.

### 8.2 Version policy

Initial pins:

```toml
[project]
requires-python = ">=3.12,<3.13"
dependencies = [
  "lean-interact==0.11.4",
  # remaining dependencies pinned through uv.lock
]
```

Also record:

```text
lean_interact_version
lean_version
mathlib_commit
project_git_commit
leanfaith_git_commit
```

in every run manifest and every persisted Lean response record.

Upgrade procedure:

1. create an upgrade branch;
2. change only the LeanInteract pin and lockfile initially;
3. run the full Lean integration test suite;
4. compare golden declaration and typecheck payloads;
5. run a 1,000-record extraction and evidence smoke benchmark;
6. document payload or performance changes;
7. merge only after deterministic equivalence of required fields or an intentional schema migration.

### 8.3 Project types

Use the correct LeanInteract project abstraction for each setting:

| Setting | LeanInteract project | Policy |
|---|---|---|
| Main LeanFaith repository | `LocalProject` | primary development and custom meta helpers |
| Fixed external Lean repository | `GitProject` | pin exact commit/tag; never use a floating branch in experiments |
| Isolated benchmark or smoke test | `TempRequireProject` | pin Lean version and dependency revisions |
| Fine-grained generated Lake project | `TemporaryProject` | use only when dependency graph cannot be expressed otherwise |

The main production runs should use a previously built local project. Before constructing a server, CI and the doctor command must verify that `lake build` succeeds.

### 8.4 Backend abstraction

Only the backend module may import LeanInteract directly. Higher-level code depends on a narrow protocol:

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

@dataclass(frozen=True)
class LeanCheckRequest:
    context_id: str
    code: str
    allow_sorry: bool
    timeout_s: float
    request_declarations: bool = False
    request_root_goals: bool = False
    request_infotree: str | None = None

@dataclass(frozen=True)
class LeanCheckResult:
    request_hash: str
    valid: bool
    timed_out: bool
    crashed: bool
    messages: tuple[dict, ...]
    sorries: tuple[dict, ...]
    declarations: tuple[dict, ...]
    root_goals: tuple[str, ...]
    infotree: tuple[dict, ...]
    elapsed_ms: int
    environment_fingerprint: str
    raw_response_path: str

class LeanBackend(Protocol):
    def check(self, request: LeanCheckRequest) -> LeanCheckResult: ...
    def check_many(self, requests: Sequence[LeanCheckRequest]) -> list[LeanCheckResult]: ...
    def extract_file(self, path: Path, context_id: str) -> list[dict]: ...
    def close(self) -> None: ...
```

`LeanInteractBackend` implements this protocol. A fake backend is used for pure unit tests. Do not create a second real backend during the MVP.

### 8.5 Server choice

Use:

- `AutoLeanServer` for long-running extraction, typechecking, proof attempts, and generation validation;
- `LeanServer` only for short deterministic cases where automatic recovery is unnecessary and the simpler lifecycle is useful;
- `LeanServerPool` for batches of independent requests when its scheduling model fits;
- one `AutoLeanServer` per worker for custom grouped scheduling.

### 8.6 Session and prefix-reuse policy

Incremental elaboration is most useful when many commands share a header. Therefore:

1. derive a `context_fingerprint` from Lean version, project revision, imports, namespace/open declarations, options, and local notation;
2. group requests by `context_fingerprint`;
3. send full commands, including the common header, to the same server;
4. allow LeanInteract to reuse the common prefix;
5. do not manually micromanage environment IDs unless a feature specifically requires it;
6. restart a server whenever imported project files change, because cached imports may otherwise remain stale;
7. treat project directories as immutable during production runs.

For generated variants sharing a source theorem context, process the source and all its variants in one group. This both improves speed and reduces context inconsistency.

### 8.7 Parallel execution policy

Preferred order:

1. `LeanServerPool` for simple independent batches;
2. threaded or asynchronous workers when Python mainly waits on REPL subprocesses;
3. multiprocessing with `spawn` when isolation or CPU-side postprocessing requires it.

Rules:

- instantiate `LeanREPLConfig` once before creating workers;
- each worker owns its own server instance;
- never share a mutable `LeanServer` instance across processes;
- set a conservative global worker count based on measured memory, not only CPU count;
- configure per-request timeout and per-server memory limits where supported;
- save partial results continuously;
- retry infrastructure failures, not deterministic Lean errors;
- cap retries and preserve all failed attempts.

### 8.8 Typechecking

For candidate statements, submit the full source header plus a proof-stripped declaration ending in `:= by sorry` or the dataset’s equivalent incomplete form.

A candidate is considered statement-well-typed when:

```python
isinstance(response, CommandResponse)
and response.lean_code_is_valid(allow_sorry=True)
```

The result must distinguish:

```text
valid_with_sorry
valid_without_sorry
lean_error
repl_error
timeout
server_crash
context_setup_error
```

Do not collapse these into a single boolean in persistent data.

### 8.9 Declaration extraction

Use:

```python
FileCommand(path=relative_path, declarations=True)
```

for source repositories and:

```python
Command(cmd=code, declarations=True)
```

for generated snippets.

Persist the relevant `DeclarationInfo` fields:

```text
pp
range
scope
name
full_name
kind
modifiers
signature.pp
signature.constants
signature.range
binders
optional type
optional value
```

Use `range` to recover the exact source substring when needed. Do not use regex as the primary declaration parser for repository files. Regex may be used only to recover simple dataset headers before Lean elaboration confirms the declaration.

### 8.10 InfoTrees and custom structural extraction

LeanInteract can return InfoTrees, but the project should use them only where they add information not already present in declarations.

Default extraction request:

```text
declarations=True
root_goals=False
infotree=None
```

Escalate to `InfoTreeOptions.full` or `substantive` only for:

- debugging parser/elaboration edge cases;
- deriving structural features unavailable from declarations;
- validating source spans;
- studying tactic/proof metadata in a separate analysis.

For a canonical Lean `Expr` representation, implement small Lean-side meta helpers in `LeanFaith/Meta/` and invoke them through LeanInteract `Command` or `FileCommand`. Do not bypass LeanInteract to run these helpers.

### 8.11 Proof-search attempts

Proof-search requests are ordinary LeanInteract commands with strict policies:

- construct proposition types from statement signatures;
- never grant the proof of both compared theorems as ambient constants;
- prohibit source/candidate theorem constants in a certificate;
- run each direction separately;
- capture success, failure, timeout, exact proof text where available, and used constants when possible;
- save the tactic portfolio and timeout in the evidence record.

The LeanInteract BEq+ example may be adapted as a baseline, with attribution and a pinned source commit. Because it is example code rather than a guaranteed stable package API, place the adaptation under `baselines/`, preserve the MIT notice, and cover it with golden tests.

### 8.12 Error handling

Classify errors before retrying:

| Error | Retry? | Action |
|---|---:|---|
| Lean syntax/type error | no | persist diagnostic; mark candidate invalid |
| deterministic tactic failure | no | persist as failed search |
| timeout | once or policy-based | restart/recover server; then persist timeout |
| server crash/connection abort | yes, bounded | use `AutoLeanServer`; preserve both attempts |
| JSON/protocol error | yes, bounded | restart server and preserve raw payload |
| project setup/build error | no batch retry | fail the run before processing data |
| out of memory | bounded after reducing load | lower workers or memory-heavy request size |

No exception may silently turn into an empty success list. Every attempted record receives a terminal status.

### 8.13 LeanInteract integration tests

Required tests:

1. typecheck a valid theorem with `sorry`;
2. reject an ill-typed theorem;
3. distinguish `allow_sorry=True` and `False`;
4. extract a declaration and verify its full name, kind, signature, constants, binders, and source range;
5. process a file through `FileCommand`;
6. validate incremental reuse does not change semantic results;
7. recover from a forced timeout or server restart;
8. run a four-request batch through the chosen parallel mechanism;
9. pin and report LeanInteract/Lean/mathlib versions;
10. compare a stored golden response after dependency upgrades.

The Lean layer milestone is not complete until all ten pass in CI and locally.

---

## 9. Data sources and source manifests

### 9.1 Primary sources

| Source | Main role | Priority |
|---|---|---:|
| `formalmathatepfl/sft_classic` or the user’s local/private variant | large source of theorem statements, proofs, and possibly NL–Lean pairs | 1 |
| mathlib4 | broad and high-quality formal statement source | 1 |
| ProofNetVerif | external real-output evaluation and benchmark adapter | 1 |
| Real outputs from ReForm-style and frontier autoformalizers | deployment-distribution training and evaluation | 1 |
| Lean Workbook or similar NL–Lean datasets | additional NL–Lean pairs | 2 |
| CSLib | computer-science OOD source | 2 |
| PhysLib and related projects | physics OOD source | 2 |
| Additional Lean projects | later robustness studies | 3 |

### 9.2 Source adapter contract

```python
class SourceAdapter(Protocol):
    source_name: str

    def probe(self) -> SourceProbe: ...
    def iter_examples(self, split: str | None = None) -> Iterable[RawSourceExample]: ...
    def source_manifest(self) -> SourceManifest: ...
```

`probe()` must inspect and report:

- available columns or file layout;
- row count if available;
- likely NL field;
- Lean code/formalization field;
- source header/import field;
- proof field;
- example identifiers;
- license or access restriction metadata;
- sample parse and typecheck rate.

Do not write the full adapter until the probe output is saved and reviewed.

### 9.3 `sft_classic` adapter

The exact schema may differ between a public dataset and the user’s copy. The adapter must therefore use an explicit mapping configuration rather than guessing silently.

Example:

```yaml
source: formalmathatepfl/sft_classic
fields:
  id: id
  nl_statement: problem
  lean_header: lean4_src_header
  lean_code: lean4_formalization
  proof: lean4_proof
on_missing:
  nl_statement: null
  proof: infer_from_lean_code
```

Probe procedure:

1. load only metadata and a small sample;
2. print columns and types;
3. save 100 untouched examples;
4. test configured field mappings;
5. typecheck 100 proof-stripped statements;
6. report exact success/failure counts;
7. block full extraction if the success rate is unexpectedly low.

### 9.4 Repository-source adapter

For mathlib, CSLib, and PhysLib:

1. pin the exact Git commit;
2. instantiate a LeanInteract `GitProject` or use a checked-out `LocalProject`;
3. enumerate `.lean` files excluding build/cache directories;
4. call `FileCommand(..., declarations=True)` per file;
5. filter declaration kinds to theorem-like `Prop` declarations;
6. preserve file path, range, namespace, scope, signature, constants, and project revision;
7. write file-level failure records rather than silently dropping failed files.

### 9.5 Source manifest

Every source ingest produces an immutable manifest:

```json
{
  "source_name": "mathlib4",
  "source_version": "<commit>",
  "adapter_version": "<git commit or semantic version>",
  "lean_version": "...",
  "lean_interact_version": "0.11.4",
  "retrieved_at": "...",
  "license": "...",
  "configured_splits": ["..."],
  "raw_count": 0,
  "parsed_count": 0,
  "elaborated_count": 0,
  "failure_counts": {},
  "sample_hash": "...",
  "config_hash": "..."
}
```

### 9.6 Licensing and release discipline

Before releasing a combined dataset:

- record each source license and redistribution constraints;
- separate releasable derived metadata from non-releasable source text where required;
- provide scripts to reconstruct data from original sources when direct redistribution is not allowed;
- do not send private dataset contents to external LLM APIs without permission.

---

## 10. Data lifecycle and immutable stages

Every record moves through explicit stages:

```text
RAW
  → PARSED
  → ELABORATED
  → NORMALIZED
  → GENERATED
  → VALIDATED
  → EVIDENCE_COLLECTED
  → LABELED
  → SPLIT
  → FROZEN
```

Rules:

1. A stage reads an immutable prior stage and writes a new output partition.
2. No stage edits prior artifacts in place.
3. Every output partition has a manifest, configuration hash, code commit, input manifest hash, row count, and checksum.
4. Commands are idempotent: rerunning with the same inputs/configuration either reuses the exact output or fails on a checksum mismatch.
5. Partial shards are resumable.
6. Corrupt or failed records remain visible in a failure partition.
7. Frozen test data are write-protected by convention and CI checks.

Recommended artifact key:

```text
{stage}/{source}/{source_version}/{config_hash}/{shard_id}.{jsonl|parquet}
```

---

## 11. Persistent schemas

Use Pydantic models with schema versions. Store large structured fields in Parquet or compressed JSONL. The examples below omit some convenience fields but include all conceptual requirements.

### 11.1 `ContextRecord`

```json
{
  "schema_version": 1,
  "context_id": "ctx:sha256...",
  "project_kind": "local | git | temporary",
  "project_uri": "...",
  "project_revision": "...",
  "lean_version": "...",
  "lean_interact_version": "0.11.4",
  "imports": ["Mathlib"],
  "open_declarations": ["open Set"],
  "namespace_stack": ["..."],
  "options": {},
  "local_notation": [],
  "header_text": "...",
  "header_hash": "..."
}
```

### 11.2 `TheoremRecord`

```json
{
  "schema_version": 1,
  "theorem_id": "theorem:sha256...",
  "source_name": "...",
  "source_example_id": "...",
  "source_split": "...",
  "source_project_revision": "...",
  "source_file": "...",
  "source_range": {"start": [1, 0], "end": [3, 10]},
  "context_id": "ctx:...",
  "declaration_kind": "theorem",
  "name": "foo",
  "full_name": "Namespace.foo",
  "raw_declaration": "...",
  "proof_stripped_declaration": "... := by sorry",
  "headless_statement": "...",
  "signature_pp": "...",
  "signature_constants": ["..."],
  "binders": {},
  "scope": {},
  "explicit_signature_pp": "...",
  "structural_json_path": "...",
  "semantic_atoms": ["..."],
  "nl_statement": "... or null",
  "reference_trusted": "true | false | uncertain | not_applicable",
  "elaboration_status": "valid_with_sorry",
  "diagnostics": [],
  "normalization_version": "...",
  "content_hash": "...",
  "metadata": {}
}
```

### 11.3 `VariantRecord`

```json
{
  "schema_version": 1,
  "variant_id": "variant:sha256...",
  "source_theorem_id": "...",
  "generator_kind": "deterministic | llm_variant | autoformalizer | nearby_theorem",
  "generator_id": "positive.alpha.v1 | model/provider/version",
  "generation_config_hash": "...",
  "seed": 17,
  "prompt_hash": "... or null",
  "raw_output": "...",
  "extracted_statement": "...",
  "intended_relation": "equivalent | A_stronger | B_stronger | near_miss | unknown",
  "intended_error_types": [],
  "transformation_trace": [],
  "inverse_trace": [],
  "validation_status": "...",
  "theorem_record_id": "... after elaboration",
  "metadata": {}
}
```

### 11.4 `PairRecord`

The pair record references labels and evidence; it does not merge them into one overloaded field.

```json
{
  "schema_version": 1,
  "pair_id": "pair:sha256...",
  "a_theorem_id": "...",
  "b_theorem_id": "...",
  "pair_source": "deterministic | llm | real_output | benchmark | human_curated",
  "source_group_id": "original theorem or NL problem id",
  "generator_group_id": "...",
  "intended_relation": "... or null",
  "resolved_label_id": "label:... or null",
  "evidence_ids": ["evidence:..."],
  "lexical_stats": {},
  "structural_stats": {},
  "split_eligibility": {},
  "metadata": {}
}
```

### 11.5 `EvidenceRecord`

```json
{
  "schema_version": 1,
  "evidence_id": "evidence:sha256...",
  "pair_id": "...",
  "kind": "typecheck | defeq | proof_A_to_B | proof_B_to_A | counterexample | llm_judgment | human_annotation | transformation_certificate",
  "status": "success | failure | timeout | error | abstain",
  "value": {},
  "method_version": "...",
  "config_hash": "...",
  "raw_artifact_path": "...",
  "created_at": "..."
}
```

### 11.6 `ResolvedLabel`

```json
{
  "schema_version": 1,
  "label_id": "label:sha256...",
  "pair_id": "...",
  "same_claim": "yes | no | ambiguous",
  "relation": "equivalent | A_stronger | B_stronger | incomparable_near_miss | unrelated | ambiguous",
  "truth_A_implies_B": "yes | no | unknown",
  "truth_B_implies_A": "yes | no | unknown",
  "error_types": [],
  "quality_tier": "gold_human | gold_conservative_transform | gold_counterexample | benchmark | silver_consensus | provisional | unknown",
  "resolution_method": "...",
  "adjudication_notes": "...",
  "eligible_for_training": true,
  "eligible_for_final_evaluation": false
}
```

### 11.7 `NLPLeanRecord`

```json
{
  "schema_version": 1,
  "nl_pair_id": "nlpair:sha256...",
  "problem_id": "...",
  "source_name": "...",
  "nl_statement": "...",
  "reference_theorem_ids": ["..."],
  "candidate_theorem_id": "...",
  "candidate_generator": "...",
  "faithful": "yes | no | ambiguous | unlabeled",
  "relation_to_reference": "...",
  "error_types": [],
  "label_quality": "...",
  "evidence_ids": [],
  "split_group_id": "problem:..."
}
```

### 11.8 LLM call records

Store every call with:

```text
provider
model ID and version/date if available
role: proposer | autoformalizer | judge
prompt template ID and hash
fully rendered prompt hash
sampling parameters
response text or secure artifact pointer
parsed output
parse status
retry lineage
input record IDs
cost/token counts
request timestamp
```

This is required for reproducibility and for studying judge/model-family leakage.

---

## 12. Theorem extraction and context reconstruction

### 12.1 Extraction outputs

For each theorem-like declaration, produce:

1. source text and source range;
2. proof-stripped declaration;
3. headless theorem statement;
4. LeanInteract `DeclarationInfo` payload;
5. elaborated signature and constants;
6. binder and scope information;
7. optional explicit/canonical representation;
8. optional structural JSON;
9. exact context fingerprint;
10. typecheck diagnostics;
11. source and version metadata;
12. natural-language statement when available.

### 12.2 Repository extraction

For each source file:

1. run `FileCommand(path=..., declarations=True)` through LeanInteract;
2. persist the complete response before filtering;
3. iterate declarations;
4. select `theorem`, `lemma`, and other proposition-valued declaration kinds approved by configuration;
5. use the declaration range to recover raw text;
6. reconstruct proof-stripped text using source range plus Lean-aware boundaries;
7. verify the reconstructed declaration separately with `Command`;
8. store any discrepancy as an extraction failure.

Do not assume every `DeclarationInfo.value` is available or suitable for proof stripping. Raw source and signature are separate views.

### 12.3 Dataset-string extraction

Dataset rows may contain imports, namespaces, multiple declarations, code fences, explanatory text, or proofs. Use this sequence:

1. sanitize Markdown fences without modifying Lean tokens inside the code;
2. obtain the configured header and formalization fields;
3. run the entire snippet with `declarations=True`;
4. select the intended theorem by configured rule, usually the last theorem-like declaration or a named declaration;
5. use returned declaration metadata and source range;
6. produce proof-stripped code;
7. rerun proof-stripped code and require valid elaboration with `allow_sorry=True`;
8. mark ambiguous multi-declaration rows for manual adapter rules rather than guessing.

Regex is permitted only as a preliminary helper. LeanInteract elaboration is the authority.

### 12.4 Proof stripping

Proof stripping must handle:

- `:= by ...`;
- `:= term`;
- `where` blocks;
- `termination_by` and unrelated declaration syntax;
- declarations already ending in `:= by sorry`;
- examples and declarations without a name;
- attributes and modifiers;
- nested `by` tokens inside statement syntax.

Use declaration ranges and, where necessary, InfoTree/syntax information. Build a golden fixture suite of at least 100 difficult declarations from diverse projects.

### 12.5 Context fingerprint

The same Lean text can mean something different under another environment. Compute:

```text
context_fingerprint = SHA256(
  lean_version
  + project_revision
  + ordered_imports
  + namespace/open declarations
  + local notation
  + relevant options
)
```

Pairs may be compared across contexts, but the model and evidence pipeline must know the difference. For typechecking generated variants, default to the source theorem’s exact context.

### 12.6 Filtering

For the MVP, keep only declarations whose elaborated result is a proposition. Exclude or flag:

- definitions returning data;
- syntax/macro declarations;
- generated auxiliary declarations;
- declarations with unresolved metavariables;
- statements requiring unavailable private context;
- statements exceeding configured size limits;
- duplicates under canonical hash.

Do not discard them silently; record counts and reasons.


---

## 13. Statement representations and normalization

### 13.1 No single canonical string is sufficient

The project should not search for one aggressively expanded theorem string. Expansion can expose hidden assumptions but can also explode size, erase useful abstractions, and create version-sensitive output. Instead, store multiple synchronized views.

### 13.2 Required views

| View | Description | Default model use |
|---|---|---:|
| `raw_proof_stripped` | original declaration with proof replaced | yes, ablation/input |
| `headless` | theorem name and proof removed; cosmetic whitespace normalized | yes |
| `signature_pp` | LeanInteract elaborated signature | yes |
| `signature_explicit` | custom meta pretty-print with explicit binders/universes/coercions according to pinned options | yes after stable |
| `alpha_structural` | binder-ID/de Bruijn normalized structural JSON | features/graph |
| `notation_light` | only whitelisted notation/definition expansions | optional |
| `semantic_atoms` | substantive constants/operators/types extracted from structure | invariants/features |
| `operator_tree` | compact tree for GTED-style baseline | baseline/feature |

### 13.3 Normalization levels

```text
N0 raw proof-stripped declaration
N1 theorem name/comments/whitespace normalized
N2 elaborated signature from LeanInteract
N3 explicit signature from Lean-side helper
N4 alpha-normalized structural form
N5 whitelisted local desugaring/unfolding
```

Every level must be derivable from the original theorem record and tagged with a normalization version.

### 13.4 Required normalization invariants

- deterministic output under a pinned environment;
- alpha-renaming does not alter `alpha_structural` hash;
- theorem-name changes do not alter headless or structural hash;
- context fingerprint remains attached;
- no proof body leaks into model inputs;
- no unrestricted `simp` or arbitrary theorem-based rewriting;
- normalization errors never replace the raw source record.

### 13.5 Explicit representation

Use a Lean-side helper invoked through LeanInteract to emit an explicit theorem type with a controlled option profile. Evaluate options such as:

```lean
set_option pp.explicit true
set_option pp.universes true
set_option pp.proofs false
set_option pp.fullNames true
```

The exact option set must be tested under the pinned Lean version. Do not assume an option exists without a smoke test. Store the option profile in the normalization manifest.

### 13.6 Structural representation

The structural JSON should encode at least:

```text
node kind
constant full name
literal value
binder information
child order
application function/arguments
universe information when relevant
source range when available
inferred type summary when affordable
```

For v0, the Lean-side helper may emit a compact recursive tree. Graph edges and richer type links can be added later.

### 13.7 Semantic atoms

Define a semantic atom as a substantive element whose accidental removal or substitution often changes the claim:

- quantifiers and binder types;
- typeclass/structure assumptions;
- predicates and named mathematical constants;
- relation operators;
- set/function constructors;
- numerical literals;
- casts and coercion targets;
- conclusion head and hypothesis heads.

Ignore or downweight:

- theorem names;
- binder names;
- source positions;
- formatting tokens;
- direct notation wrappers approved by a mapping table.

Store an ordered atom trace and a multiset signature. Transformation admission checks use these to detect semantic erasure.

### 13.8 Representation experiments

The first paper-quality ablation should compare:

1. raw only;
2. headless only;
3. elaborated signature only;
4. headless + elaborated signature;
5. explicit signature;
6. text plus structural scalar features;
7. text plus structural graph.

Do not assume the most expanded form is best.

---

## 14. Pair-label taxonomy and error ontology

### 14.1 Core labels

```text
same_claim: yes | no | ambiguous
relation: equivalent | A_stronger | B_stronger | incomparable_near_miss | unrelated | ambiguous
```

### 14.2 Evidence-only logical fields

```text
defeq: success | failure | timeout | error | not_run
proof_A_to_B: success | failure | timeout | error | not_run
proof_B_to_A: success | failure | timeout | error | not_run
counterexample_A_not_B: found | not_found | unsupported | timeout | error
counterexample_B_not_A: found | not_found | unsupported | timeout | error
```

`not_found` is not a proof of implication or equivalence.

### 14.3 Error ontology

Use multi-label tags with stable IDs:

```text
E01 missing_hypothesis
E02 extra_hypothesis
E03 vacuous_or_inconsistent_hypothesis
E04 wrong_quantifier
E05 wrong_quantifier_order
E06 wrong_domain_or_type
E07 wrong_codomain
E08 wrong_typeclass_or_structure
E09 wrong_constant_or_predicate
E10 wrong_operator
E11 wrong_inequality_strictness
E12 wrong_equality_or_iff_direction
E13 wrong_set_operation
E14 wrong_function_direction_image_preimage_map_comap
E15 wrong_cast_or_coercion
E16 wrong_index_or_bound
E17 wrong_numerical_constant
E18 wrong_answer_value
E19 special_case_only
E20 overgeneralization
E21 irrelevant_or_unbound_variable
E22 omitted_dependency_between_binders
E23 namespace_import_or_notation_mismatch
E24 malformed_or_non_elaborating_statement
E25 semantic_erasure_or_tautologization
E26 formalizes_related_but_different_claim
E27 reference_suspected_incorrect
E28 ambiguous_natural_language
E29 cosmetic_only
E30 other
```

The ontology should be versioned. Changing tag semantics requires a migration.

### 14.4 Label-quality tiers

| Tier | Definition | Training | Final evaluation |
|---|---|---:|---:|
| `gold_human` | expert double annotation plus adjudication | yes | yes |
| `gold_conservative_transform` | deterministic rule passes all positive gates | yes | diagnostic only unless human sampled |
| `gold_counterexample` | Lean-checkable separating instance or proof of non-equivalence in supported fragment | yes | yes/diagnostic |
| `benchmark` | accepted external benchmark label | optionally train only if designated | yes |
| `silver_consensus` | stringent independent judge consensus and validation | yes, weighted | no sole final claim |
| `provisional` | intended mutation/generation label | pretraining or mining only | no |
| `unknown` | insufficient evidence | no supervised target | no |

### 14.5 Label resolution precedence

Default precedence:

```text
human adjudication
  > benchmark gold policy
  > conservative transformation certificate / checked counterexample
  > high-quality multi-model consensus
  > intended generation label
```

Conflicts are not silently resolved. Create a conflict record and route it to review.

---

## 15. Deterministic transformation framework

### 15.1 Purpose

Deterministic transformations provide scale, controlled error categories, and exact provenance. They are not expected to reproduce the full real error distribution, so they must be combined with real autoformalization outputs.

### 15.2 Transformation API

```python
@dataclass(frozen=True)
class TransformCandidate:
    source_theorem_id: str
    rule_id: str
    rule_version: str
    seed: int
    candidate_code: str
    intended_relation: str
    intended_error_types: tuple[str, ...]
    trace: tuple[dict, ...]
    inverse_trace: tuple[dict, ...] | None
    expected_atom_mapping: dict[str, str]

class TransformRule(Protocol):
    rule_id: str
    family: str
    polarity: str

    def applicable(self, theorem: TheoremRecord) -> bool: ...
    def generate(self, theorem: TheoremRecord, seed: int) -> list[TransformCandidate]: ...
    def validate_trace(self, source: TheoremRecord, candidate: TheoremRecord) -> bool: ...
```

Rules operate on Lean-aware syntax or structural representations. Raw string replacement is forbidden except for formatting-only transformations.

### 15.3 Universal validation gates

Every generated candidate must pass:

1. source theorem is valid;
2. generated text parses and elaborates in the source context through LeanInteract;
3. generated declaration is proposition-valued;
4. no unresolved metavariables;
5. candidate differs from source under the relevant non-cosmetic hash;
6. exact transformation trace is present;
7. candidate is not a duplicate already generated for the source;
8. output length and complexity remain within configured bounds;
9. all LeanInteract diagnostics and versions are persisted.

### 15.4 Positive admission gates

A deterministic positive receives `gold_conservative_transform` only if all applicable gates pass:

1. the rule is on an explicit positive allowlist;
2. the rule is local and reversible, or has a checked round trip;
3. the structural diff matches the rule’s declared pattern exactly;
4. semantic atoms are preserved under the rule’s approved mapping;
5. no substantive hypothesis, binder type, quantifier, literal, or conclusion atom is removed;
6. a generic proof template or definitional equality check succeeds when required;
7. the certificate does not use source/candidate theorem constants;
8. certificate dependencies remain within a rule-specific allowlist when feasible;
9. source-to-candidate and candidate-to-source transformations both validate;
10. random human audit of the rule family meets a predeclared precision threshold.

If any gate is unavailable or fails, the example becomes `provisional_positive`, not gold.

### 15.5 Conservative positive families

#### P00 — Cosmetic formatting

- comments;
- whitespace;
- line breaks;
- theorem name changes.

Use mainly for invariance tests, not to dominate training.

#### P01 — Alpha-renaming

- binder and hypothesis renaming;
- capture-avoiding substitution;
- exact structural hash equality after alpha normalization.

#### P02 — Binder grouping and interface presentation

Examples:

```lean
(x y : α)
```

versus

```lean
(x : α) (y : α)
```

and carefully validated theorem-level currying/uncurrying.

#### P03 — Independent binder/hypothesis permutation

Only when dependency analysis proves the swap safe. Update all references by structure, not text.

#### P04 — Direct notation/desugaring variants

Examples on a strict allowlist:

```text
x ∈ {y | P y}       ↔ P x
s ⊆ t               ↔ ∀ x, x ∈ s → x ∈ t
Function.comp f g x ↔ f (g x)
```

Use exact environment constants and rule-specific certificates.

#### P05 — Local propositional interface equivalences

Examples:

```text
P ∧ Q ↔ Q ∧ P
(P ∧ Q) ∧ R ↔ P ∧ (Q ∧ R)
P ∨ Q ↔ Q ∨ P
(P ∧ Q → R) ↔ (P → Q → R)
```

Only use constructively valid forms under the current logic, unless a rule explicitly requires and records classical assumptions.

#### P06 — Definitional/reducible presentation variants

Use only when Lean’s definitional equality or a whitelisted reducible unfolding verifies the transformation and semantic atoms are preserved.

### 15.6 Transformations not automatically admitted as positives

The following are research candidates requiring stronger review:

- unrestricted `simp` normal forms;
- arithmetic normalization that removes the central operation;
- proof by a domain theorem unrelated to a local reversible rewrite;
- replacing a theorem by any other mutually provable theorem;
- broad `ring_nf`, `linarith`, `omega`, or `aesop` output as the sole justification;
- simplification to `True`, reflexivity, or another tautological form;
- semantic fusion/equisatisfiable transformations that do not preserve the same claim.

Store these under `semantic_rewrite_candidate` and use them for human policy studies, not automatic gold positives.

### 15.7 Negative mutation families

#### N01 — Relation/operator mutation

Examples:

```text
< ↔ ≤
≤ ↔ =
= ↔ ≠
∈ ↔ ∉
⊆ ↔ =
∧ ↔ ∨
```

Replacement must be type-compatible in the local context.

#### N02 — Quantifier mutation

- `∀` to `∃`;
- `∃` to `∀`;
- quantifier order changes;
- implicit dependence loss.

#### N03 — Hypothesis deletion

Remove one substantive hypothesis while repairing binder references if possible. The relation may be stronger, weaker, or malformed; do not hard-code direction without analysis.

#### N04 — Hypothesis insertion

Add:

- a plausible but unnecessary assumption;
- a stronger assumption;
- a contradictory or vacuous assumption;
- a wrong side condition.

Tag vacuity separately.

#### N05 — Domain/type/structure mutation

Examples:

```text
Nat ↔ Int
Rat ↔ Real
Set α ↔ Finset α
Group ↔ CommGroup
Continuous ↔ Measurable context
```

Only retain candidates that elaborate naturally or with a local generated cast. Track all inserted coercions.

#### N06 — Conclusion-head mutation

Change:

- requested relation;
- predicate;
- set operation;
- image/preimage direction;
- greatest/least;
- irreducible/prime-like predicates where type compatible.

#### N07 — Literal, bound, or index mutation

- `0` to `1`;
- `n` to `n + 1`;
- `< k` to `< k + 1`;
- off-by-one indices;
- swapped tuple or function arguments.

#### N08 — Cast/coercion mutation

- remove a needed cast;
- cast to the wrong target;
- move a cast across an operation when meaning changes;
- replace subtype coercion with ambient type.

#### N09 — Specialization/generalization

- fix a variable to a constant;
- replace a universal theorem by one case;
- generalize a concrete domain without preserving assumptions;
- remove a bound.

#### N10 — Nearby theorem substitution

Retrieve statements with high constant overlap from the same file/namespace/domain, then compare or mix selected components. This creates realistic negatives not tied to one token mutation.

### 15.8 Negative quality gates

A typed mutation is initially `provisional`. Promote it only through one of:

- a Lean-checkable counterexample;
- a proof of one direction plus a counterexample to the other;
- expert human adjudication;
- stringent multi-model consensus plus audited family precision.

For each mutation family, estimate accidental-equivalence rate by human sampling before large-scale training.

### 15.9 Type-aware replacement index

Build in two stages.

**Stage A — Curated table:** manually specified high-value replacements with preconditions and expected error types.

**Stage B — Environment-derived candidates:**

1. collect constants and signatures from LeanInteract declarations;
2. index constants by normalized type skeleton and arity;
3. propose replacements whose types can plausibly unify;
4. attempt replacement structurally;
5. let Lean elaboration validate actual compatibility;
6. rank by semantic proximity, namespace, name embedding, and usage context;
7. retain diverse, type-correct candidates.

Do not treat shared type as evidence of semantic mismatch or equivalence; it is only a proposal mechanism.

---

## 16. Counterexample and symbolic evidence pipeline

### 16.1 Goals

Symbolic evidence should increase precision, supply interpretable certificates, and define benchmark slices. It should not force unsupported binary labels.

### 16.2 Definitional equality

Implement a Lean-side meta command called through LeanInteract that attempts definitional equality between elaborated proposition types. Persist:

```text
success/failure/error/timeout
reduction settings
elapsed time
Lean versions
```

A success is strong evidence for representation equivalence. A failure is not evidence of mismatch.

### 16.3 Directional proof search

For each pair, generate separate goals:

```lean
example : A → B := by
  -- configured portfolio

example : B → A := by
  -- configured portfolio
```

Policy:

- compare proposition types, not imported theorem proof constants;
- explicitly prevent trivial use of globally available source/candidate theorem declarations;
- use bounded tactics and timeouts;
- store exact proof and dependency trace where available;
- run a sanity check that the target is not independently provable by the same portfolio before crediting a proof that relies on the source theorem;
- report `success` as logical evidence, not same-claim ground truth.

### 16.4 Tactic portfolio

Start with a conservative configurable portfolio inspired by BEq+ and domain solvers:

```text
exact?/apply?
simp_all or selected simp rules
tauto
omega
linarith/nlinarith
ring/noncomm_ring
convert with bounded tolerance
aesop with strict limits
```

The exact tactic set must be tested on the pinned environment. Save per-tactic outcomes and wall-clock time.

### 16.5 Counterexample search

Prioritize supported decidable fragments:

- finite types;
- bounded naturals and integers;
- booleans;
- small finite sets/lists;
- decidable propositional structures;
- arithmetic statements reducible to small exhaustive domains.

For an open theorem type, generate finite bounded instantiations only when the transformation preserves enough structure to map variables. A found separating assignment should be converted into a Lean-checkable certificate, for example with `native_decide` where appropriate.

A counterexample record includes:

```json
{
  "direction": "A_true_B_false",
  "assignments": {},
  "bounded_domain": {},
  "lean_certificate": "...",
  "checker_status": "success",
  "search_method": "..."
}
```

No counterexample found means `not_found`, never `equivalent`.

### 16.6 Certificate hygiene

For automatic positive certificates, record:

- generated proof text;
- tactic trace;
- constants used;
- forbidden-constant check;
- rule allowlist version;
- whether the proof works in a leakage-safe context;
- whether source and target round-trip structurally.

A certificate that succeeds only because both propositions are separately known theorems is not a same-claim certificate.

---

## 17. LLM data generation with a very large token budget

### 17.1 Principle

Use LLMs to generate **coverage and realistic error distributions**, not to manufacture unquestioned ground truth. Frontier models are proposers and independent weak judges. Humans and Lean evidence remain the final authority for the gold set.

### 17.2 Four LLM jobs

1. **Autoformalization sampling:** translate NL statements into many Lean candidates.
2. **Equivalent-variant proposing:** reformulate trusted Lean statements without changing the claim.
3. **Near-miss proposing:** create subtle, type-correct semantic errors with specified categories.
4. **Blinded judging:** assess Lean–Lean or NL–Lean faithfulness using structured output.

### 17.3 Model-role separation

Use different model families where possible:

| Role | Examples of suitable model class |
|---|---|
| specialized generator | ReForm-style Lean autoformalizer |
| broad generator | strong general frontier model or strong open model |
| judge A | frontier family distinct from generator |
| judge B | second independent frontier family |
| diversity generator | additional open model such as a GLM-family model |
| final adjudicator | expert human |

Exact names are configuration, not hard-coded logic. Record provider, model version, date, and parameters.

### 17.4 Real autoformalization corpus

Begin collecting real outputs as soon as ingestion works; do not wait for the deterministic transformation pipeline to be perfect.

For each trusted NL problem:

1. sample `K` candidate statements from each generator;
2. vary temperature, reasoning budget, prompt style, and availability of library context according to a controlled design;
3. require statement-only output or robustly extract the statement;
4. typecheck every candidate through LeanInteract;
5. retain invalid candidates in an analysis partition but train semantic models primarily on valid statements;
6. deduplicate candidates by structural hash;
7. store generator provenance;
8. pair valid candidates with the reference and with one another;
9. judge and human-sample them.

Pilot scale:

```text
2,000 NL problems
2–4 generator families
4–8 candidates per generator/problem
≈ 16,000–64,000 raw candidates
```

Scale only after typecheck, duplication, and label-quality reports are acceptable.

### 17.5 Targeted variant generation

Prompts should request balanced categories rather than generic “different statements.” Example categories:

```text
same claim with different binder interface
same claim using direct notation expansion
missing side condition
wrong quantifier
wrong domain
strict/non-strict relation change
wrong image/preimage direction
plausible special case
plausible overgeneralization
wrong numerical value
vacuous extra assumption
```

Every generated variant returns structured JSON with:

```text
variant_statement
intended_relation
intended_error_types
short rationale
claimed_typecheck_status
modified_span description
```

The claimed status is not trusted; LeanInteract validates it.

### 17.6 Prompt and parsing contract

- version every prompt template;
- demand JSON schema output;
- validate with Pydantic;
- retry only parse/infrastructure failures with bounded attempts;
- never silently repair a Lean statement and keep the original label without recording the repair;
- save raw output, parsed output, and all retries;
- hash the rendered prompt;
- redact secrets and provider metadata that should not enter the dataset.

### 17.7 Blinded judge protocol

Judges must not see:

- which statement is the reference;
- whether a pair came from a positive or negative generator;
- transformation rule ID;
- another judge’s answer;
- model training labels.

For Lean–Lean judging:

1. show raw proof-stripped statements;
2. optionally show elaborated signatures in a second condition;
3. ask for same-claim, directional relation, error tags, and confidence;
4. run an order-swapped copy on a subset or all high-value examples;
5. reject internally inconsistent judgments;
6. compare judge accuracy to human gold before assigning weights.

For NL–Lean judging:

1. show the NL statement and candidate;
2. optionally show a trusted reference as a separate experimental condition;
3. require a concrete mismatch description when labeling unfaithful;
4. allow `ambiguous` and `reference_suspected_incorrect`.

### 17.8 Silver-label promotion policy

A pair may become `silver_consensus` only if:

- all statements typecheck;
- at least two independent judge families agree on `same_claim`;
- confidence exceeds a calibrated threshold;
- order-swapped judgment is consistent when run;
- error tags/rationales are not mutually contradictory;
- no high-confidence symbolic evidence conflicts;
- the relevant judge slice has passed a human-audit precision threshold.

Suggested initial threshold for automatic silver promotion: estimated precision at least 95% on a representative human-audited sample. Until measured, all LLM labels remain provisional.

### 17.9 Disagreement and active-learning pool

Prioritize for human review:

- judge disagreement;
- model–judge disagreement;
- symbolic–judge disagreement;
- very small edit distance with predicted mismatch;
- high model confidence on a provisional opposite label;
- underrepresented domains/error types;
- examples from held-out generator families;
- suspected reference errors;
- semantic-erasure cases.

### 17.10 Avoiding circular evaluation

- Do not train on labels generated by the exact judge used as the main test baseline without reporting this dependency.
- Hold out at least one generator family and one judge family from training-time weak supervision.
- Never use external benchmark test labels to select models or thresholds.
- Maintain a human-only final test set that no training prompt or active-learning policy can inspect after freezing.

### 17.11 Efficient use of billions of tokens

Spend tokens in stages:

1. broad candidate generation;
2. cheap deterministic/typecheck filtering;
3. one-pass diverse judging;
4. model training;
5. uncertainty/disagreement mining;
6. expensive repeated judging only on high-value examples;
7. human labeling of the most informative residual cases.

More repeated votes on easy examples are less valuable than broader theorem domains, generators, and error categories.


---

## 18. Human annotation and adjudication

### 18.1 Human sets

Create four distinct expert-reviewed sets:

| Set | Purpose | Access policy |
|---|---|---|
| `policy_pilot` | refine same-claim definition and examples | may influence guidelines, not final metrics |
| `development_gold` | error analysis and active learning | may be used for training after version freeze |
| `calibration_gold` | probability calibration and thresholds | never used for model weight training |
| `final_human_test` | final paper claims | sealed until final evaluation |

### 18.2 Suggested scale

Pilot:

```text
200–400 Lean–Lean pairs
100–200 NL–Lean pairs
```

Development target:

```text
1,000–2,000 Lean–Lean pairs
1,000–2,000 NL–Lean pairs
```

Final test target, subject to expert availability:

```text
at least 500 examples per main task,
with enough positives and negatives for meaningful confidence intervals
```

Quality matters more than raw size. Report the exact construction process.

### 18.3 Sampling strata

Balance across:

- real autoformalizer outputs;
- deterministic hard negatives;
- LLM-proposed positives and negatives;
- source projects/domains;
- generator families;
- statement lengths and binder counts;
- proof-search success/failure;
- lexical similarity buckets;
- model confidence buckets;
- error categories;
- suspected reference problems.

The final test must not be dominated by easy alpha-renaming or obvious operator substitutions.

### 18.4 Annotation UI

Lean–Lean view:

- raw proof-stripped A and B;
- elaborated signatures;
- imports/context summary;
- typecheck status;
- optional rendered structural diff;
- no provenance or intended label.

NL–Lean view:

- full natural-language statement;
- candidate Lean statement and elaborated signature;
- optional trusted reference in a separately marked panel;
- typecheck status;
- no generator/judge provenance.

### 18.5 Required annotation fields

```text
same_claim or faithful: yes | no | ambiguous
relation: equivalent | A_stronger | B_stronger | incomparable | unrelated | ambiguous
error tags: multi-label
confidence: 1–5
short rationale: required for no/ambiguous
reference issue: none | suspected | definite
```

### 18.6 Annotation protocol

1. two independent expert annotations per item;
2. no discussion before initial labels;
3. disagreements sent to a third adjudicator;
4. retain all individual labels and rationales;
5. freeze adjudicated label with guideline version;
6. compute raw agreement, Cohen’s kappa or suitable multi-class statistic, and per-category agreement;
7. revise guidelines only between annotation rounds, never midway without versioning.

### 18.7 Human-policy audit of transformation families

Before promoting a deterministic or LLM family at scale:

1. sample at least 50–100 examples across domains and difficulties;
2. blind annotators to family identity;
3. estimate precision and confidence interval;
4. promote only families meeting the required precision;
5. downgrade or split noisy families;
6. repeat after rule changes.

### 18.8 Annotation guideline edge cases

The guideline must explicitly address:

- equivalent notation versus changed abstraction;
- simplification that removes the central claim;
- vacuous truth;
- implicit typeclass differences;
- universes and subtype coercions;
- redundant but satisfiable hypotheses;
- stronger/weaker formulations;
- answer-only formalizations;
- references that appear incorrect;
- informal statements with genuine ambiguity.

Maintain an example bank containing accepted and rejected cases for each.

---

## 19. Dataset construction, balancing, and contamination control

### 19.1 Build profiles

Use staged scale rather than immediately processing everything.

#### Profile `smoke`

```text
100 source theorems
≤1,000 pairs
single Lean project
no paid LLM calls
```

Purpose: CI and local debugging.

#### Profile `pilot`

```text
10,000 source theorems
100,000–500,000 deterministic pairs
2,000 NL problems
16,000–64,000 model-generated candidates
small human policy set
```

Purpose: validate distributions, labels, throughput, and v0 modeling.

#### Profile `research_v1`

```text
100,000+ source theorems
1–10 million high-quality pairs after filtering
10,000–50,000 NL problems
100,000–500,000 real candidate statements
full development/calibration/human test sets
```

Scale beyond this only if learning curves justify it.

### 19.2 Balance policy

Do not simply sample equal positives and negatives globally. Preserve realistic evaluation prevalence while controlling training batches.

Training sampler should balance:

- label;
- relation direction;
- quality tier;
- transformation/error family;
- source domain;
- real versus synthetic provenance;
- generator family;
- difficulty bucket.

Evaluation sets should report both their natural prevalence and balanced metrics.

### 19.3 Difficulty measures

Precompute:

```text
token edit similarity
character edit similarity
constant Jaccard
semantic-atom overlap
binder-count difference
structural tree distance
statement-length ratio
proof-search outcome
source-domain match
model-generation provenance
```

Use these for stratified sampling and error analysis, not as semantic labels.

### 19.4 Deduplication hierarchy

Deduplicate by:

1. exact source ID;
2. normalized text hash;
3. alpha-structural hash;
4. pair hash invariant to A/B order for equivalence tasks;
5. NL problem ID;
6. near-duplicate retrieval using token/atom MinHash or embedding similarity;
7. known benchmark identifiers.

Potential near duplicates across splits must be listed in a contamination report.

### 19.5 Split groups

Minimum grouping keys:

```text
source theorem family
NL problem ID
reference theorem ID
all variants of a source
all outputs for one NL problem
near-duplicate cluster
```

No group may cross split boundaries.

### 19.6 Required splits

| Split | Purpose |
|---|---|
| `train` | model fitting |
| `validation` | architecture/hyperparameter selection |
| `calibration` | temperature/isotonic/conformal threshold fitting |
| `human_test` | primary human-grounded claim |
| `benchmark_test` | external benchmark comparison |
| `real_output_test` | only real model-generated candidates |
| `heldout_transform_test` | unseen deterministic rule families |
| `heldout_project_test` | CSLib/PhysLib or another unseen project |
| `heldout_generator_test` | unseen autoformalizer family |
| `adversarial_test` | hand-designed minimal pairs and semantic erasure |

### 19.7 Benchmark isolation

ProofNetVerif and other designated external tests must be registered in a denylist before data generation. Check:

- problem IDs;
- normalized NL hashes;
- reference Lean hashes;
- theorem constants/near duplicates;
- source overlap.

No benchmark test label is used for active learning, model selection, or calibration.

### 19.8 Frozen split manifest

A frozen split manifest records every record ID, group ID, source version, and hash. Any later data correction creates a new dataset version; it never edits the frozen version in place.

---

## 20. Baselines

### 20.1 Simple baselines

- exact string equality;
- headless normalized equality;
- token and character edit distance;
- constant/semantic-atom overlap;
- generic text/code embeddings with cosine similarity;
- logistic regression or gradient-boosted model over scalar features.

### 20.2 Lean-aware symbolic baselines

- typecheck-only;
- definitional equality;
- directional proof-search portfolio;
- BEq/BEq+ adapted through LeanInteract;
- a high-precision certificate-or-abstain policy.

### 20.3 Structural baselines

- AST/operator-tree edit distance;
- GTED implementation or released code if compatible;
- TransTED/ASSESS-style score if implementation and licensing permit;
- tree/graph kernel baseline.

### 20.4 LLM judge baselines

- one strong judge;
- two-model majority/consensus;
- judge with raw Lean only;
- judge with elaborated signatures;
- reference-aware NL–Lean judge;
- repeated self-consistency on a fixed cost budget.

All LLM baselines must use frozen prompts and record cost/latency.

### 20.5 Learned baselines

- bi-encoder cosine model;
- concatenated pair cross-encoder;
- generic code model without Lean normalization;
- FormalAlign-style or related learned alignment model when reproducible;
- scalar-feature boosted tree.

### 20.6 Baseline output contract

Every baseline writes:

```json
{
  "record_id": "...",
  "method": "...",
  "method_version": "...",
  "score_same_claim": 0.0,
  "predicted_label": "yes | no | abstain",
  "relation_scores": {},
  "elapsed_ms": 0,
  "cost": {},
  "evidence": {},
  "config_hash": "..."
}
```

This allows one evaluation program to compare all methods.

---

## 21. Model design

### 21.1 Model progression

Implement in this order:

```text
M0 scalar-feature classifier
M1 concatenated Lean pair cross-encoder
M2 shared Lean encoders + bidirectional cross-attention
M3 M2 + symbolic/structural features
M4 NL–Lean model initialized from Lean encoder
M5 optional text + Expr graph model
```

Do not begin M5 until M2/M3 and the data pipeline are stable.

### 21.2 M1: concatenated cross-encoder

Input:

```text
<A_RAW>
...
</A_RAW>
<A_SIGNATURE>
...
</A_SIGNATURE>
<B_RAW>
...
</B_RAW>
<B_SIGNATURE>
...
</B_SIGNATURE>
```

A code-capable pretrained Transformer encodes the concatenated sequence. Classification heads predict:

- `p_same_claim`;
- directional implications as auxiliary targets where labels exist;
- relation class;
- error tags;
- uncertainty/abstention score.

### 21.3 M2: shared encoders plus bidirectional cross-attention

This corresponds to the user’s intended “decoder-like” cross-attention idea without requiring generative decoding.

Architecture:

1. tokenize A and B separately;
2. pass each through a shared pretrained Lean/code encoder;
3. apply `L` bidirectional cross-attention blocks:
   - A queries B;
   - B queries A;
4. pool `[CLS]`, attention-pooled, and token-difference features;
5. apply symmetric and directional heads.

Required symmetry behavior:

```text
p_same_claim(A, B) ≈ p_same_claim(B, A)
relation(A_stronger; A,B) ≈ relation(B_stronger; B,A)
```

Enforce using:

- swapped-pair augmentation;
- consistency loss;
- symmetric pooling for equivalence head;
- directional head swapping tests.

### 21.4 M3: hybrid features

Candidate scalar features:

```text
defeq success
proof A→B / B→A outcomes and timeouts
counterexample flags
GTED/tree distance
constant and semantic-atom overlap
binder/typeclass differences
length/edit statistics
context compatibility
```

Fusion options to compare:

1. concatenate with pooled neural representation;
2. separate calibrated gradient-boosted meta-model;
3. high-precision symbolic overrides followed by neural fallback.

Recommended production policy:

```text
if conservative same-claim certificate:
    return certified equivalent
elif checked separating counterexample:
    return certified mismatch
else:
    return calibrated learned prediction or abstain
```

Do not let proof-search failure override the model as a negative.

### 21.5 Prediction heads

```text
same_claim: binary with ambiguous masked or modeled separately
relation: 6-way classification
A_implies_B: binary/unknown-masked auxiliary
B_implies_A: binary/unknown-masked auxiliary
error_types: multi-label
quality/uncertainty: optional auxiliary
```

### 21.6 Loss

Initial multi-task objective:

```text
L = λeq * weighted_BCE(same_claim)
  + λrel * CE(relation)
  + λab * masked_BCE(A_implies_B)
  + λba * masked_BCE(B_implies_A)
  + λerr * weighted_BCE(error_types)
  + λsym * symmetry_consistency
  + λrank * optional pairwise_ranking
```

Tune λ values on validation only. Low-quality labels receive sample weights; unknown fields are masked.

### 21.7 Quality-aware training

Suggested starting weights, subject to validation:

```text
gold_human                  1.00
gold_counterexample          1.00
gold_conservative_transform  0.90–1.00
benchmark designated train   0.90
silver_consensus             0.40–0.70
provisional                  0.00–0.25 or pretraining only
```

Track performance with and without provisional data.

### 21.8 Curriculum

1. invariance and conservative positives;
2. curated typed minimal negatives;
3. diverse deterministic families;
4. LLM-proposed variants;
5. real autoformalization outputs;
6. gold human fine-tuning;
7. separate calibration.

Interleave real outputs early enough to prevent a synthetic-only representation.

### 21.9 Hard-negative mining

After each major model:

1. score provisional and unlabeled pools;
2. select high-confidence likely false positives;
3. include close lexical/atom neighbors;
4. seek judge/counterexample/human evidence;
5. add confirmed cases to the next dataset version;
6. keep the final test sealed.

### 21.10 Calibration and abstention

Fit calibration after model selection using the calibration split only.

Compare:

- temperature scaling;
- vector scaling;
- isotonic regression;
- beta calibration;
- optional conformal risk-control/coverage methods.

Expose three operating regions:

```text
ACCEPT: estimated high precision for faithful/equivalent
REVIEW: uncertainty or conflicting evidence
REJECT: estimated high precision for mismatch
```

Thresholds are versioned and tied to a target risk, not chosen by intuition.

### 21.11 NL–Lean model

Architecture:

```text
NL encoder
Lean encoder initialized from M2/M3
bidirectional cross-attention or a joint cross-encoder
optional trusted-reference Lean branch
multi-task faithfulness/error heads
```

Train on:

- trusted NL/reference pairs;
- conservative positive variants of references;
- verified hard negatives;
- real autoformalization candidates;
- benchmark and human labels according to split policy.

Run two modes:

1. **reference-free:** `N + C`;
2. **reference-aware:** `N + R + C`, or combine `NL–Lean(N,C)` with `Lean–Lean(R,C)`.

The downstream deployment result should emphasize reference-free reranking; reference-aware mode is useful for benchmark evaluation and dataset quality control.

### 21.12 Graph extension

Graph nodes may represent:

```text
forallE, lam, letE, app, const, fvar, bvar, sort, lit, proj
```

Edges may represent:

```text
parent/child
application function/argument
binder type/body
bound-variable reference
type-of
same constant across statements
```

Compare:

- graph-only;
- text-only;
- late fusion;
- cross-modal attention.

The graph extension earns its complexity only if it improves held-out-family, OOD, or hard-near-miss performance significantly.

---

## 22. Training and experiment discipline

### 22.1 Configuration

Every run is fully configuration driven. No hidden constants in scripts. A run configuration includes:

```text
dataset version and split manifest
model checkpoint/tokenizer
input views
maximum lengths/truncation policy
quality weights
sampler
optimizer/scheduler
seed
hardware precision
loss weights
calibration method
evaluation slices
```

### 22.2 Determinism

- seed Python, NumPy, PyTorch, dataset shuffling, and transformation RNGs;
- record nondeterministic CUDA settings;
- use deterministic evaluation;
- never regenerate a frozen split inside a training script;
- save exact record IDs used in every run.

### 22.3 Truncation

Long theorem statements cannot be naively truncated from one end. Evaluate:

- head+tail truncation;
- separate binder/hypothesis/conclusion budgets;
- structural atom-preserving truncation;
- long-context models.

Always report the percentage of examples truncated and by how much.

### 22.4 Data leakage checks before training

The training command must fail if:

- any theorem/problem group occurs in multiple protected splits;
- an exact or alpha-structural hash crosses protected splits;
- a benchmark denylisted ID appears in training;
- a final-test record is referenced by an active-learning artifact;
- dataset and model manifests do not agree on schema versions.

### 22.5 Checkpoints and selection

- choose architecture/hyperparameters on validation metrics only;
- choose calibration method on calibration metrics only;
- evaluate final test only for frozen candidate models;
- retain all model-selection decisions in a decision log.

### 22.6 Learning curves

Train at multiple data scales and source mixtures:

```text
synthetic only
synthetic + LLM variants
synthetic + real outputs
all weak data
all weak data + human gold
```

This is essential to establish what data actually contributes.

---

## 23. Evaluation plan

### 23.1 Primary Lean–Lean metrics

Report:

```text
AUROC
AUPRC
accuracy and macro-F1
precision/recall/F1 for same-claim
recall at 95%, 97%, and 99% precision
risk–coverage / accuracy–coverage curve
Brier score
ECE and reliability diagram
```

Because prevalence affects accuracy and precision, report class prevalence and balanced metrics.

### 23.2 Relation and error metrics

- macro-F1 for relation classes;
- A→B and B→A auxiliary accuracy on known labels;
- per-error-tag precision/recall/F1;
- exact-match and micro/macro metrics for multi-label errors;
- confusion between equivalent/stronger/weaker/near-miss.

### 23.3 Robustness tests

Every model must be tested for:

1. A/B order swap;
2. theorem-name randomization;
3. binder alpha-renaming;
4. whitespace/comment perturbation;
5. harmless binder regrouping;
6. unseen transformation families;
7. held-out projects;
8. held-out autoformalizer family;
9. long statements;
10. low constant overlap but same claim;
11. high constant overlap but wrong claim;
12. semantic-erasure/tautologization traps;
13. context/import differences;
14. reference errors or ambiguous NL.

### 23.4 Slice analysis

Report by:

```text
source project/domain
pair provenance
label quality
real vs synthetic
statement length
binder and typeclass count
edit similarity
semantic-atom overlap
proof-search outcome
generator family
error type
calibration confidence
```

### 23.5 Human agreement

Compare methods against adjudicated labels and report:

- method accuracy/F1;
- human–human agreement;
- method–human agreement;
- performance on human-disagreement cases;
- calibration by annotator confidence.

### 23.6 External benchmarks

Run ProofNetVerif and other compatible benchmarks exactly according to their released protocol where possible. Document any adaptation, Lean/mathlib version mismatch, or excluded example. Do not silently change benchmark imports or labels.

### 23.7 Downstream reranking

For each NL problem:

```text
generate K candidates
→ validate with LeanInteract
→ score valid candidates
→ optionally cluster near-equivalent candidates
→ choose top candidate or abstain
→ compare to human/reference label
```

Baselines:

- first candidate;
- first compiling candidate;
- random compiling candidate;
- generator self-score;
- LLM judge;
- symbolic/reference-aware metric when a reference exists;
- ensemble policies.

Metrics:

```text
faithful@1
faithful@k
MRR or nDCG over graded human labels
coverage at target precision
cost per problem
latency per candidate
percentage with no compiling candidates
percentage abstained
```

### 23.8 Repair loop

Optional but strong application:

1. choose a high-scoring but rejected/uncertain candidate;
2. provide predicted error tags and minimal mismatch explanation to a generator;
3. generate a repaired statement;
4. typecheck and rescore;
5. evaluate improvement and false-repair rate.

### 23.9 Statistical testing

Use paired bootstrap confidence intervals or suitable paired randomization tests for method differences. Correct for multiple comparisons where many variants are tested. Report effect sizes, not only p-values.

### 23.10 Success criteria

Minimum scientific success:

- learned model clearly beats simple lexical/structural baselines on human real-output test;
- calibration supports a useful high-precision operating point;
- real-output data improves over synthetic-only training;
- reranking improves faithful@1 over first-compiling-candidate.

Strong success:

- learned/hybrid model exceeds BEq+/GTED and approaches or exceeds frontier LLM-judge accuracy at substantially lower inference cost;
- gains hold on held-out generators and projects;
- error predictions improve repair success;
- graph/structural augmentation provides interpretable OOD gains.


---

## 24. Implementation roadmap with hard stage gates

The implementation is intentionally sequential. Later stages may be prototyped in notebooks, but no later-stage output may be treated as a project artifact until all earlier stage gates pass.

Each milestone must produce:

1. versioned code;
2. automated tests;
3. a machine-readable run manifest;
4. a short Markdown report under `reports/milestones/`;
5. exact commands to reproduce the report;
6. a clear pass/fail decision against the milestone gate.

### Phase 0 — Freeze the semantic and engineering contract

**Goal:** make ambiguity expensive before implementation, not after data generation.

#### Tasks

1. Convert Sections 3, 14, and 15 into versioned policy files:

   ```text
   policies/
     semantic_target_v1.md
     relation_labels_v1.yaml
     error_ontology_v1.yaml
     evidence_policy_v1.yaml
     automatic_promotion_policy_v1.yaml
   ```

2. Write at least 40 hand-constructed examples covering:
   - clearly faithful paraphrases;
   - clearly unfaithful near-misses;
   - truth-equivalent but claim-unfaithful pairs;
   - stronger/weaker pairs;
   - redundant-binder cases;
   - ambiguous cases;
   - examples where the reference statement itself is suspicious.
3. Have at least two project members independently label the examples.
4. Adjudicate disagreements and revise the policy once.
5. Freeze policy version `v1`; all future labels carry this version.
6. Choose and record the initial Lean version, mathlib revision, Python version, and `lean-interact` version.
7. Create the project license, data-license matrix, citation file, and model-card skeleton.

#### Deliverables

```text
policies/*_v1.*
examples/semantic_contract_v1.jsonl
reports/milestones/phase_0_contract.md
configs/environment.lock.yaml
LICENSE
CITATION.cff
DATA_SOURCES.md
```

#### Gate 0

Pass only when:

- all label values have operational definitions;
- at least 90% raw agreement is reached on the 40 examples after one policy review, or all persistent disagreements are assigned an explicit `UNCERTAIN` route;
- every automatic-positive rule states why it preserves the claim rather than merely truth;
- the full toolchain is pinned;
- no unresolved question can change the meaning of the primary target label.

If Gate 0 fails, do not generate training data.

### Phase 1 — LeanInteract integration spike

**Goal:** prove that the entire required Lean workflow is possible through one tested Python abstraction built on LeanInteract.

#### Tasks

1. Implement `LeanInteractBackend` behind the `LeanBackend` protocol.
2. Implement project adapters for:
   - a local LeanFaith fixture project;
   - a pinned local mathlib checkout;
   - one `GitProject` smoke test;
   - one temporary mathlib environment for benchmark snippets.
3. Implement:
   - command execution;
   - file execution;
   - declaration extraction;
   - statement validation with and without `sorry`;
   - environment-state reuse;
   - timeout handling and automatic server recovery;
   - structured conversion of messages, errors, sorries, declarations, and timing information.
4. Add a pool-backed batch path using one `AutoLeanServer` per process.
5. Add a deterministic `leanfaith doctor` command.
6. Add fixture theorems for all relevant syntax categories:
   - implicit and explicit binders;
   - typeclass binders;
   - local notation;
   - namespaces and sections;
   - attributes;
   - `where` clauses;
   - Unicode identifiers;
   - theorem, lemma, example, and def declarations;
   - intentionally invalid declarations;
   - declarations containing `sorry`.
7. Record the exact raw LeanInteract responses as scrubbed golden fixtures where stable.
8. Add a compatibility smoke test that fails loudly when the pinned LeanInteract API changes.

#### Deliverables

```text
src/leanfaith/lean/protocol.py
src/leanfaith/lean/leaninteract_backend.py
src/leanfaith/lean/project_registry.py
src/leanfaith/lean/response_normalization.py
src/leanfaith/cli/doctor.py
tests/integration/leaninteract/
reports/milestones/phase_1_leaninteract_spike.md
```

#### Gate 1

Pass only when:

- every Lean invocation in the test suite is routed through `LeanInteractBackend`;
- declaration signatures extracted by LeanInteract exactly match checked golden expectations for all fixtures;
- a killed or timed-out server is recovered without corrupting the next request;
- 1,000 mixed valid/invalid fixture requests complete with zero silently dropped records;
- a request can be reproduced from its stored context fingerprint and source text;
- no project code invokes `lake env lean`, the Lean REPL binary, or the Lean LSP directly.

### Phase 2 — Source probing and theorem extraction

**Goal:** create reliable adapters before assuming any source schema.

#### Tasks

1. Implement source probes that inspect, but do not yet ingest, each candidate source:
   - `formalmathatepfl/sft_classic` or the exact available variant;
   - mathlib;
   - CSLib;
   - PhysLib;
   - ProofNetVerif;
   - any private/local corpora.
2. For each source, produce:
   - source revision or dataset fingerprint;
   - licensing information;
   - columns/file structure;
   - 100 representative examples;
   - estimated theorem count;
   - percentage that can be reconstructed and elaborated;
   - duplicate estimate;
   - language/domain distribution;
   - proof/statement separation quality;
   - import/context requirements.
3. Implement schema-adaptive but strict adapters:
   - accepted schema variants are explicit and unit-tested;
   - unknown schemas fail with a diagnostic rather than guessing;
   - raw records are retained byte-for-byte or as source snapshots where licensing permits.
4. Extract theorem declarations from repository files with `FileCommand(..., declarations=True)`.
5. Reconstruct dataset snippets inside pinned contexts and use `Command(..., declarations=True)` where appropriate.
6. Store both successful and failed extraction attempts.
7. Generate stable theorem IDs from source identity, context fingerprint, and source span/content.
8. Deduplicate exact and near-exact theorem statements without deleting source provenance.

#### Deliverables

```text
src/leanfaith/sources/
data/source_manifests/
data/raw_index/
data/extracted/theorems/
reports/source_probes/*.md
reports/milestones/phase_2_extraction.md
```

#### Gate 2

Pass only when:

- each enabled source has a source manifest and license decision;
- at least 99.5% of successfully parsed records can be deterministically reloaded;
- every failed record has a nonempty machine-readable failure category;
- extraction from a 1,000-file mathlib sample is restartable and produces identical IDs on rerun;
- no theorem is accepted without its import/context fingerprint;
- duplicate clusters preserve all source memberships.

### Phase 3 — Statement views and semantic fingerprints

**Goal:** produce multiple useful representations without pretending that one printed string is canonical semantics.

#### Tasks

1. Implement the views specified in Section 13:
   - raw declaration;
   - headless theorem signature;
   - Lean-pretty-printed signature;
   - explicit-binder view;
   - structural expression view;
   - semantic-atom inventory;
   - constant multiset;
   - binder/dependency graph summary;
   - operator tree.
2. Implement custom Lean meta helpers only for information not exposed reliably by LeanInteract.
3. Test views under:
   - alpha-renaming;
   - binder reordering;
   - notation expansion;
   - namespace qualification;
   - universe parameters;
   - implicit arguments;
   - coercions;
   - typeclass synthesis.
4. Define which views are:
   - identity-sensitive;
   - expected invariant;
   - diagnostic only;
   - suitable model inputs;
   - forbidden as split keys because they may merge non-equivalent examples.
5. Build semantic fingerprints used for audit and deduplication, not as ground-truth labels.
6. Measure collision rates on at least 100,000 extracted statements.

#### Deliverables

```text
src/leanfaith/representations/
LeanFaith/Meta/Extract.lean
LeanFaith/Meta/Fingerprint.lean
data/representation_samples/
reports/milestones/phase_3_representations.md
```

#### Gate 3

Pass only when:

- all views round-trip through their documented serialization format;
- alpha-invariant views are empirically invariant on at least 10,000 generated renamings;
- no view silently drops a semantically material binder, constant, literal, relation, or type;
- any fingerprint collision observed in a manually audited sample is documented;
- representation extraction failures are below 0.5% on supported elaborated statements or are isolated by an explicit unsupported category.

### Phase 4 — Deterministic transformation engine

**Goal:** generate conservative positives and difficult, compiling provisional negatives with complete provenance.

#### Tasks

1. Implement a typed transformation interface:

   ```python
   class Transformation(Protocol):
       family_id: str
       version: str
       intended_relation: IntendedRelation

       def applicable(self, record: TheoremRecord) -> Applicability: ...
       def propose(self, record: TheoremRecord, rng: Random) -> list[VariantDraft]: ...
       def audit(self, source: TheoremRecord, variant: VariantRecord) -> TransformationAudit: ...
   ```

2. Implement common gates once, not separately per transformation:
   - context reconstruction;
   - elaboration;
   - declaration extraction;
   - semantic-atom comparison;
   - source/variant structural diff;
   - round-trip check where applicable;
   - certificate hygiene;
   - duplicate check;
   - triviality check;
   - evidence creation.
3. Implement positive families P00–P06 in order of risk.
4. Implement negative families N01–N10 in order of interpretability.
5. Generate no more than a configurable quota per source theorem and family.
6. Record failed applicability and failed-generation reasons; do not merely discard them.
7. Add mutation difficulty controls:
   - token edit distance band;
   - tree edit distance band;
   - semantic-atom overlap band;
   - location of changed subtree;
   - number of changed operations;
   - whether the change affects assumptions, conclusion, type, or binder structure.
8. Add counterexample attempts for decidable/small fragments.
9. Perform blinded manual audits by family.
10. Freeze transformation family versions before producing a release dataset.

#### Deliverables

```text
src/leanfaith/transforms/
configs/transforms/*.yaml
data/generated/deterministic_pilot/
reports/transformation_audits/*.md
reports/milestones/phase_4_transforms.md
```

#### Gate 4A — positive families

A positive family may be promoted to automatic gold only when:

- at least 200 randomly sampled pairs from that family have been independently audited;
- estimated precision is at least 99% with a reported confidence interval;
- no recurrent semantic-erasure pattern is found;
- every generated pair passes the family-specific invariants;
- the certificate does not import or invoke either theorem declaration as an assumption;
- the family survives a held-out source/domain audit.

Otherwise the family remains `silver`, `experimental`, or disabled.

#### Gate 4B — negative families

A negative family may be used for supervised training only when:

- variants elaborate;
- source and variant are not definitionally equal;
- automatic proof checks do not establish same-claim equivalence under the configured safe rules;
- accidental-equivalence audits are below the family threshold;
- at least one of human confirmation, counterexample evidence, or exceptionally high-confidence typed mutation evidence is present;
- the family has an explicit difficulty distribution rather than only trivial mutations.

Unverified negative intentions remain unlabeled candidate pairs.

### Phase 5 — Real autoformalization output collection

**Goal:** capture deployment-like errors before the synthetic generator dominates the project.

#### Tasks

1. Select a source NL problem pool disjoint from external evaluation benchmarks.
2. Stratify by domain, length, notation burden, and expected difficulty.
3. Run multiple generator families and checkpoints, including:
   - a formalization-specialized open model such as ReForm-32B where feasible;
   - one or more strong general open models;
   - frontier API models available to the project;
   - deliberately weaker models/checkpoints to broaden the error distribution.
4. Sample multiple candidates under several temperatures and prompting strategies.
5. Retain every raw completion and request metadata.
6. Parse candidate declarations conservatively.
7. Validate all candidates through LeanInteract.
8. Keep both compiling and noncompiling candidates, but route them to different tasks:
   - compiling candidates: faithfulness dataset;
   - noncompiling candidates: optional syntax/elaboration repair dataset, not semantic-equivalence negatives.
9. Cluster candidates by normalized signature and structural fingerprint.
10. Sample across clusters for judging and human annotation rather than overrepresenting repeated outputs.
11. Record generator identity only in provenance; hide it from judges and human annotators.

#### Deliverables

```text
data/generations/raw/
data/generations/parsed/
data/generations/validated/
configs/generation/*.yaml
reports/generation_coverage.md
reports/milestones/phase_5_real_outputs.md
```

#### Gate 5

Pass only when:

- at least three materially different generator families are represented;
- at least 10,000 compiling candidate/reference pairs exist for the pilot, subject to budget;
- no single generator contributes more than 50% of the judged pilot;
- all API calls have replayable metadata except secret values;
- benchmark/test examples have been excluded by problem ID and near-duplicate checks;
- candidate diversity is demonstrated beyond surface paraphrase statistics.

### Phase 6 — LLM-proposed transformations and multi-judge silver labels

**Goal:** use abundant model tokens to diversify examples without confusing model consensus with ground truth.

#### Tasks

1. Build separate prompts for:
   - faithful restatement;
   - stronger claim;
   - weaker claim;
   - wrong quantifier;
   - missing/added side condition;
   - wrong domain/type;
   - wrong mathematical object;
   - redundant/free-variable pathology;
   - notation/definition change;
   - subtle same-type constant substitution;
   - explanation of the minimal semantic difference.
2. Ask proposers for structured output with:
   - candidate Lean statement;
   - intended relation;
   - changed span/subexpression;
   - rationale;
   - uncertainty;
   - optional counterexample sketch.
3. Ignore proposer labels during verification except as provenance.
4. Validate all proposed Lean statements through LeanInteract.
5. Run at least two independent blinded judges from different model families.
6. Randomize pair order and include swapped-order duplicates for judge-consistency measurement.
7. Include hidden calibration items and known traps.
8. Request directional judgments rather than only binary equivalence.
9. Promote only under the policy in Section 17; route disagreement to active learning/human review.
10. Track judge drift by prompt, model version, date, and calibration performance.
11. Prevent label leakage by ensuring the same judge rationale is never included as model input in the main equivalence experiment unless explicitly studied as a separate condition.

#### Deliverables

```text
prompts/proposers/
prompts/judges/
src/leanfaith/llm/
data/generated/llm_pilot/
data/judgments/silver/
reports/judge_calibration.md
reports/milestones/phase_6_llm_data.md
```

#### Gate 6

Pass only when:

- all promoted silver labels satisfy a prespecified consensus threshold;
- swapped-order consistency is reported and acceptable;
- judge performance on hidden gold/calibration items exceeds the minimum policy threshold;
- at least 20% of LLM-generated examples are manually audited, stratified by relation and proposer/judge;
- disagreement examples are retained rather than discarded;
- training weights distinguish gold, silver, and weak labels.

### Phase 7 — Human annotation pilot and guideline freeze

**Goal:** establish a defensible gold standard for same-claim faithfulness.

#### Tasks

1. Build annotation UI and assignment tooling.
2. Train annotators on the semantic contract and trap examples.
3. Run a 100-pair pilot with at least two expert annotators per pair.
4. Conduct adjudication and collect disagreement reasons.
5. Revise guidelines once, then relabel a subset to test stability.
6. Define annotation confidence and ambiguity handling.
7. Define stopping/escalation rules:
   - third annotator;
   - project-lead adjudication;
   - mark `UNCERTAIN`;
   - flag reference defect.
8. Freeze guideline version `v1` before the main human set.

#### Deliverables

```text
annotation/guidelines_v1.md
annotation/ui/
data/human/pilot_raw/
data/human/pilot_adjudicated/
reports/human_pilot.md
reports/milestones/phase_7_human_pilot.md
```

#### Gate 7

Pass only when:

- annotators can explain F1 vs F2 distinctions;
- agreement is reported by relation and error type, not only globally;
- every recurring disagreement pattern has a guideline decision or explicit `UNCERTAIN` route;
- annotators are blinded to model/generator identity and automatic labels;
- at least 90% of pilot records have complete rationale and confidence fields.

### Phase 8 — Dataset v0 construction and freeze

**Goal:** combine heterogeneous evidence without losing its origin or contaminating splits.

#### Tasks

1. Resolve labels using a deterministic policy engine.
2. Produce separate training views:
   - gold only;
   - gold + weighted silver;
   - all evidence for weak-supervision experiments;
   - deterministic-only;
   - real-output-only;
   - synthetic-only.
3. Group-split by source problem/theorem ancestry before pair expansion.
4. Add generator-, domain-, project-, and transformation-held-out evaluation slices.
5. Run exact and fuzzy leakage checks across all splits.
6. Freeze benchmark and human-test manifests before training.
7. Publish a data card with counts, sources, licenses, label quality, and exclusions.

#### Deliverables

```text
data/releases/v0/
  train.jsonl
  validation.jsonl
  calibration.jsonl
  test_internal.jsonl
  manifests/
  DATA_CARD.md
reports/dataset_v0.md
```

#### Gate 8

Pass only when:

- all records pass schema validation;
- all referenced artifacts exist and checksums match;
- no ancestry cluster crosses splits;
- external benchmark IDs and near-duplicates are absent from training;
- label/evidence distributions are reported by split;
- every pair can be traced back to immutable source records and tool versions;
- rerunning dataset assembly yields byte-identical manifests under the same inputs.

### Phase 9 — Baseline suite

**Goal:** establish a rigorous difficulty floor before training the main model.

#### Tasks

1. Implement lexical, normalized-text, structural, symbolic, LLM-judge, and hybrid-rule baselines.
2. Adapt the LeanInteract BEq+ example into a reproducible baseline module.
3. Implement directional implication checks separately from equivalence.
4. Enforce certificate hygiene and per-example timeouts.
5. Report coverage as well as precision/recall for abstaining or partial methods.
6. Tune thresholds only on validation/calibration splits.
7. Run baselines on:
   - deterministic synthetic validation;
   - transformation-held-out validation;
   - real-output validation;
   - human pilot;
   - external benchmark where compatible.

#### Deliverables

```text
src/leanfaith/baselines/
configs/baselines/
reports/baselines_v0.md
reports/milestones/phase_9_baselines.md
```

#### Gate 9

Pass only when:

- every baseline has a versioned config and reproducible output;
- no baseline reads gold labels at inference time;
- symbolic methods distinguish timeout, search failure, proof success, and invalid input;
- at least one lexical, one structural, one symbolic, and one LLM baseline is operational;
- reported scores can be regenerated from stored prediction files.

### Phase 10 — Main Lean–Lean model

**Goal:** train a calibrated same-claim relation model with explicit bidirectional matching.

#### Tasks

1. Train M0 independent-embedding baseline.
2. Train M1 concatenated cross-encoder.
3. Train M2 shared encoder plus bidirectional cross-attention/matching blocks.
4. Add multitask heads:
   - relation class;
   - `A ⇒ B`;
   - `B ⇒ A`;
   - F1 same-claim probability;
   - error tags;
   - uncertainty/abstention score.
5. Add pair-order augmentation and symmetry/direction consistency losses.
6. Train on staged curricula and evidence-quality weights.
7. Calibrate on a dedicated split after model selection.
8. Evaluate all predeclared slices and held-out families.
9. Run synthetic-only, real-only, and mixed-data ablations.
10. Run representation ablations and remove any feature that creates source shortcuts.

#### Deliverables

```text
src/leanfaith/models/
configs/models/m0.yaml
configs/models/m1.yaml
configs/models/m2.yaml
artifacts/models/
reports/model_v0.md
reports/milestones/phase_10_model.md
```

#### Gate 10

Pass only when:

- M2 beats the strongest non-LLM structural baseline on the real-output human validation set;
- calibration error and risk–coverage curves meet the prespecified target;
- swapped-order predictions are consistent within tolerance;
- gains do not vanish on a held-out generator or project;
- training on real outputs adds measurable transfer beyond synthetic-only data;
- all model-selection decisions use validation/calibration data only.

### Phase 11 — Final human test and external evaluation

**Goal:** obtain publishable, frozen, unbiased estimates.

#### Tasks

1. Construct the final human test set after all model-design decisions are frozen.
2. Double-annotate and adjudicate all final items.
3. Lock prediction code and model checkpoints before unblinding labels.
4. Run all baselines and models once under the registered protocol.
5. Compute bootstrap confidence intervals and paired comparisons.
6. Publish full slice results and failure taxonomy.
7. Document incompatible/excluded benchmark items transparently.

#### Deliverables

```text
data/human/final_test_frozen/
reports/final_leanlean_evaluation.md
reports/external_benchmarks.md
artifacts/predictions/final/
```

#### Gate 11

Pass only when:

- final labels were unavailable during model selection;
- all methods are evaluated on identical eligible examples;
- exclusions and failures are enumerated;
- confidence intervals are reported;
- the learned model demonstrates useful discrimination and calibration on real outputs, not only synthetic data.

### Phase 12 — NL–Lean faithfulness and downstream reranking

**Goal:** show practical utility in an autoformalization pipeline.

#### Tasks

1. Add the NL encoder and fusion/matching layer.
2. Train jointly or in a controlled second stage using human/silver NL–Lean pairs.
3. Compare:
   - NL–Lean direct scoring;
   - Lean–Lean scoring against a trusted reference;
   - combined scoring when both NL and reference are available.
4. Generate K candidates per held-out NL problem.
5. Use LeanInteract to validate and extract each candidate.
6. Evaluate ranking, abstention, and calibration.
7. Measure token cost, wall-clock latency, Lean validation cost, and judge cost.
8. Test a repair loop using predicted error tags.
9. Include a held-out generator to demonstrate judge transfer.

#### Deliverables

```text
src/leanfaith/models/nllean/
configs/reranking/
reports/reranking.md
reports/repair_loop.md
artifacts/predictions/reranking/
```

#### Gate 12

Pass only when:

- reranking improves faithful@1 over first-compiling-candidate with statistical confidence;
- performance holds for at least one generator unseen in training;
- abstention improves precision at reduced coverage in a predictable way;
- improvements are not explained solely by compilation status or output length;
- repair improves faithful accuracy without an unacceptable false-repair rate.

### Phase 13 — Graph/Expr extension

**Goal:** test whether elaborated expression structure adds robust value beyond text and scalar structural features.

#### Tasks

1. Build graph extraction from elaborated expressions.
2. Define stable node and edge schemas.
3. Add graph encoders and graph-to-token fusion.
4. Compare against a parameter-matched text-only model.
5. Evaluate especially on:
   - long statements;
   - high notation variation;
   - namespace/project shift;
   - held-out mutation families;
   - binder/dependency errors.
6. Measure extraction cost and training/inference overhead.

#### Deliverables

```text
src/leanfaith/graphs/
src/leanfaith/models/graph/
reports/graph_extension.md
```

#### Gate 13

Keep the graph component in the final system only if it gives a meaningful, reproducible improvement on real or OOD slices after accounting for compute and parameter count. A null result is acceptable and should be reported; the project must not depend on forcing a graph contribution.

### Phase 14 — Release and paper package

**Goal:** make the research independently reproducible.

#### Tasks

1. Freeze code, environment, datasets, models, and evaluation manifests.
2. Produce:
   - dataset card;
   - model card;
   - benchmark protocol;
   - transformation catalog;
   - human annotation guidelines;
   - experiment registry;
   - limitations and ethics statement;
   - artifact-evaluation instructions.
3. Release only source data permitted by license; otherwise release IDs, transforms, adapters, and derived metadata as allowed.
4. Add one-command smoke reproduction and documented full reproduction.
5. Run the release from a clean machine/container.
6. Archive exact dependency locks and checksums.

#### Gate 14

Pass only when a person who did not implement the system can follow the public instructions, run the smoke pipeline, reproduce a published table subset, and trace a prediction back to its inputs and evidence.

---

## 25. Coding-agent operating contract

These instructions are mandatory for any coding agent implementing this plan.

### 25.1 Scope discipline

1. Implement only the current milestone and its explicitly listed dependencies.
2. Do not add a new framework, database, orchestration system, model family, or Lean interface without a written architectural decision record.
3. Do not silently weaken a validation rule to make a test pass.
4. Do not treat an exploratory notebook as production code.
5. Do not begin model training before the dataset and split gates pass.
6. Do not use final benchmark or human-test labels for debugging, prompt selection, threshold selection, or feature design.

### 25.2 Lean interaction discipline

1. Import and call LeanInteract only through `src/leanfaith/lean/leaninteract_backend.py` and narrowly scoped support modules.
2. Do not scatter direct LeanInteract calls throughout source adapters, transformations, or baselines.
3. Do not create an alternative Python wrapper around Lean subprocesses.
4. Custom Lean code is allowed under `LeanFaith/Meta/` when needed for metaprogramming, but Python must invoke it through LeanInteract.
5. Store the following for every Lean request:
   - project/context ID;
   - Lean and dependency revisions;
   - LeanInteract version;
   - request type and options;
   - source code or content hash;
   - timeout/memory settings;
   - start/end timestamps and duration;
   - normalized response status;
   - raw diagnostic payload or a lossless serialized form where practical.
6. Never infer semantic failure from a timeout or crash.
7. Never reuse an environment state across incompatible contexts.
8. Do not use tactic mode as the only validity oracle because it is documented as experimental; validate complete declarations with `Command` or `FileCommand` as the authoritative path.

### 25.3 Python engineering standards

1. Support the pinned Python version and declare it in `pyproject.toml`.
2. Use type hints for all public functions and strict static checking for core modules.
3. Use Pydantic or equivalent validated models at data boundaries.
4. Prefer pure functions for normalization, transformations, split assignment, and label resolution.
5. Every CLI command must:
   - accept a config file;
   - support `--seed` where randomness exists;
   - support `--dry-run` where writes are substantial;
   - write a run manifest;
   - fail nonzero on incomplete output unless explicitly configured to continue;
   - resume safely from completed shards;
   - never overwrite a frozen dataset release.
6. Use structured logging; do not parse human log text as a data interface.
7. Catch only expected exceptions and preserve their original traceback or structured details.
8. Never write `except Exception: pass`.
9. Keep secrets out of configs, logs, prompts, data records, and Git history.
10. All randomness must derive from explicit run-level and record-level seeds.
11. All IDs must be stable across reruns and independent of processing order.
12. Avoid global mutable state, especially around Lean server pools.
13. Keep functions small enough to test; avoid “pipeline god objects.”
14. Document non-obvious semantic assumptions in code, not just in the research paper.

### 25.4 Pull-request discipline

Each pull request should:

- address one issue or tightly coupled milestone slice;
- include tests and documentation;
- include a migration note for schema/config changes;
- include before/after sample output when data behavior changes;
- not mix mechanical formatting with semantic changes;
- pass all required local and CI checks;
- update the relevant milestone report or checklist.

A pull request that changes label semantics, evidence resolution, split logic, benchmark eligibility, or automatic promotion policy requires explicit human review and an updated policy version.

### 25.5 Definition of done for a feature

A feature is done only when:

1. the behavior is specified;
2. code is typed and documented;
3. unit tests cover success and failure paths;
4. an integration test covers the real dependency where relevant;
5. outputs are schema-validated;
6. provenance is preserved;
7. the CLI or API is documented;
8. the feature is represented in a smoke pipeline;
9. reproducibility is demonstrated on a clean rerun;
10. no known failure is silently converted into a semantic label.

---

## 26. Initial coding-agent backlog

The following backlog is ordered. Issue numbers are stable planning IDs, not GitHub issue numbers.

### LF-001 — Repository bootstrap

**Implement**

- `pyproject.toml` with pinned Python and dependency groups;
- `uv.lock` or equivalent lock file;
- package skeleton;
- formatter, linter, type checker, and test configuration;
- `pre-commit` hooks;
- minimal README and contributor instructions;
- CI skeleton.

**Acceptance**

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run mypy src/leanfaith
uv run pytest -q
```

all pass in a clean checkout.

### LF-002 — Lean fixture project

**Implement**

- pinned Lean toolchain;
- minimal Lake project;
- fixture files covering syntax/context cases from Phase 1;
- one custom meta helper placeholder.

**Acceptance**

- `lake build` succeeds;
- fixture declarations have documented expected signatures;
- invalid fixture files are isolated and intentionally tested.

### LF-003 — LeanInteract dependency and compatibility pin

**Implement**

- `lean-interact==0.11.4` initial pin;
- import smoke test;
- runtime version recording;
- compatibility assertion with the pinned Lean toolchain;
- upgrade checklist in `docs/leaninteract.md`.

**Acceptance**

- CI prints and stores LeanInteract/Lean versions;
- a deliberately unsupported configuration fails with a useful diagnostic;
- no unpinned LeanInteract dependency remains.

### LF-004 — Core schemas and enums

**Implement**

- IDs and hash utilities;
- `ContextRecord`, `TheoremRecord`, `VariantRecord`, `PairRecord`, `EvidenceRecord`, `ResolvedLabel`, `NLPLeanRecord`;
- relation/evidence/error enums;
- JSONL serialization and schema versioning.

**Acceptance**

- round-trip tests;
- invalid records fail early;
- schema version is mandatory;
- stable hashes do not change with JSON key order.

### LF-005 — `LeanBackend` protocol

**Implement**

- typed request/response domain models independent of LeanInteract internals;
- methods for command, file, validation, declaration extraction, and batch execution;
- status taxonomy.

**Acceptance**

- source/transformation modules can depend on the protocol without importing LeanInteract;
- statuses distinguish valid, invalid, timeout, crash, setup failure, unsupported, and internal error.

### LF-006 — `LeanInteractBackend`

**Implement**

- config creation;
- project resolution;
- `Command` and `FileCommand` execution;
- response normalization;
- `AutoLeanServer` lifecycle;
- timeout/recovery behavior;
- environment state use.

**Acceptance**

- Phase 1 fixture integration tests pass;
- full declaration signatures are extracted;
- `sorry` policy is explicit;
- server failure does not lose the input record.

### LF-007 — Lean server pool

**Implement**

- process worker initializer with preconstructed `LeanREPLConfig`;
- one `AutoLeanServer` per process;
- task sharding by context;
- bounded queue and graceful shutdown;
- memory/timeout configuration;
- retry policy limited to infrastructure failures.

**Acceptance**

- deterministic results with 1 and N workers;
- no duplicate or missing IDs in a 10,000-request test;
- retries preserve the first error and retry count;
- semantic invalidity is never retried as infrastructure failure.

### LF-008 — `leanfaith doctor`

**Implement**

Checks for:

- Python/version lock;
- Git and `elan` availability;
- Lean toolchain;
- Lake project build;
- LeanInteract import/version;
- REPL initialization;
- simple command;
- declaration extraction;
- writable cache/output paths;
- optional API credentials without exposing them.

**Acceptance**

- emits human-readable and JSON reports;
- exits nonzero on critical failure;
- suggests actionable remediation;
- never prints secrets.

### LF-009 — Source manifest and probe framework

**Implement**

- source plugin protocol;
- immutable source manifests;
- sample/probe reports;
- licensing field requirements;
- source fingerprinting.

**Acceptance**

- an unknown source schema fails before ingestion;
- each probe stores representative raw examples and summary statistics;
- source revision is mandatory.

### LF-010 — SFT Classic adapter

**Implement**

- schema discovery report;
- explicit adapters for actually observed variants;
- extraction of NL, Lean code, imports/context, theorem statement, and provenance;
- conservative code-fence/declaration parsing;
- raw-record retention.

**Acceptance**

- works on a 1,000-row sample;
- zero silently skipped records;
- every failure has a category;
- reconstructed declarations are validated through LeanInteract.

### LF-011 — Repository extractor

**Implement**

- file discovery for mathlib/CSLib/PhysLib;
- `FileCommand(..., declarations=True)` extraction;
- source-span capture;
- context/import manifest;
- restartable sharding.

**Acceptance**

- IDs are stable across worker count and reruns;
- extraction on the fixture repository is exact;
- a mathlib sample completes with complete failure accounting.

### LF-012 — Statement proof stripping and reconstruction

**Implement**

- declaration-kind-aware statement extraction from `DeclarationInfo` and source ranges;
- fresh declaration name injection;
- placeholder proof policy;
- context reconstruction.

**Acceptance**

- reconstructed statement elaborates whenever the source declaration did, for supported cases;
- theorem body/proof tokens do not appear in the extracted signature;
- unsupported syntax is surfaced, not guessed.

### LF-013 — Representation pipeline

**Implement**

- views from Section 13;
- representation IDs and hashes;
- semantic-atom extraction;
- batch CLI.

**Acceptance**

- invariance/property tests pass;
- representations link to exact theorem/context versions;
- no material atom loss in golden examples.

### LF-014 — Pair/evidence store

**Implement**

- append-only pair and evidence records;
- evidence deduplication;
- immutable evidence history;
- deterministic label-resolution engine;
- explanation trace for each resolved label.

**Acceptance**

- adding evidence never mutates or deletes prior evidence;
- label resolution is deterministic;
- policy-version changes create a new resolved label record;
- no pair label exists without a resolution trace.

### LF-015 — Transformation framework

**Implement**

- family registry;
- applicability/proposal/audit lifecycle;
- common validation gates;
- deterministic seeds;
- quotas;
- audit artifacts.

**Acceptance**

- toy transformations can be registered without modifying the engine;
- every proposal yields accepted, rejected, or errored output with a reason;
- reruns are identical.

### LF-016 — First conservative positive families

**Implement**

- alpha-renaming;
- declaration-name change;
- grouped-binder split/merge;
- safe independent binder/hypothesis reordering;
- selected notation qualification.

**Acceptance**

- all generated statements elaborate;
- family invariants and round trips pass;
- 200-pair family audit report is generated;
- no family is marked gold before Gate 4A passes.

### LF-017 — First hard negative families

**Implement**

- relation weakening/strengthening (`<`/`≤`, subset/equality where typed);
- quantifier flip;
- conjunction/disjunction mutation;
- dropped side condition;
- same-type constant substitution.

**Acceptance**

- variants elaborate;
- changes are localized and documented;
- accidental equivalence checks run;
- outputs remain provisional until evidence policy resolves them.

### LF-018 — Proof/certificate checker

**Implement**

- definitional-equality check where available;
- directional implication templates;
- safe tactic portfolios with budgets;
- certificate hygiene scanner;
- proof result status/evidence records.

**Acceptance**

- proving both directions cannot use either original theorem declaration as an axiom;
- timeout/search failure does not become a negative label;
- known positive/negative fixtures behave as expected;
- all tactic/config details are recorded.

### LF-019 — Pilot dataset report

**Implement**

Generate a 10,000–50,000 pair pilot and report:

- source/domain counts;
- relation/error counts;
- transformation counts;
- edit-distance distributions;
- evidence strength;
- duplicates;
- failure rates;
- manual-audit sample.

**Acceptance**

- report is generated from stored records, not hand-edited numbers;
- every chart/table has a source query/config;
- the project team signs off before scaling.

### LF-020 — Generation provider abstraction

**Implement**

- provider-neutral request/response schema;
- prompt versioning;
- caching;
- retries/rate limits;
- cost/token accounting;
- secret handling;
- raw response retention.

**Acceptance**

- mock provider tests pass;
- one open/local and one API provider complete a smoke run;
- cached reruns make no API call;
- model/version/date are recorded.

### LF-021 — Autoformalization candidate collector

**Implement**

- problem sampler;
- candidate generation;
- declaration parser;
- LeanInteract validation;
- clustering;
- provenance.

**Acceptance**

- compiling and noncompiling candidates are separated correctly;
- no benchmark contamination in the development pool;
- a 100-problem multi-generator pilot produces a coverage report.

### LF-022 — LLM proposer pipeline

**Implement**

- task-specific prompts;
- structured output validation;
- Lean validation;
- intended-relation provenance;
- candidate routing.

**Acceptance**

- malformed output is retained with status;
- no proposer vote directly sets a gold label;
- all compiling candidates have exact context records.

### LF-023 — LLM judge pipeline

**Implement**

- blinded pair presentation;
- order randomization and swap repeats;
- directional relation schema;
- hidden calibration items;
- consensus and disagreement computation.

**Acceptance**

- order consistency and calibration are reported;
- identity of proposer/generator is hidden;
- silver promotion follows a versioned policy only.

### LF-024 — Annotation tool and export

**Implement**

- randomized/blinded UI;
- relation/error/confidence/rationale inputs;
- assignment and adjudication workflow;
- immutable raw annotations;
- export to schemas.

**Acceptance**

- annotator identity and guideline version are recorded;
- adjudication never destroys original labels;
- pilot agreement report is reproducible.

### LF-025 — Split and leakage engine

**Implement**

- ancestry grouping;
- problem/source grouping;
- exact/fuzzy/structural near-duplicate checks;
- benchmark denylist;
- deterministic stratified split assignment.

**Acceptance**

- adversarial leakage fixtures are caught;
- split assignment is stable across processing order;
- a release cannot freeze while leakage checks fail.

### LF-026 — Baseline framework

**Implement**

- common prediction schema;
- threshold/calibration interface;
- latency/cost instrumentation;
- lexical/structural/symbolic/LLM baseline adapters.

**Acceptance**

- all baselines emit directly comparable prediction files;
- evaluation code is model-agnostic;
- missing/abstaining predictions are handled explicitly.

### LF-027 — M0/M1 training pipeline

**Implement**

- tokenizer/data collator;
- shared pair dataset;
- M0 embedding baseline;
- M1 cross-encoder;
- checkpointing and resume;
- experiment manifests;
- calibration pipeline.

**Acceptance**

- tiny overfit test succeeds;
- deterministic smoke training succeeds;
- no test labels are loaded by training code;
- prediction files include checkpoint/config/data hashes.

### LF-028 — M2 bidirectional cross-attention model

**Implement**

- shared encoder;
- A-to-B and B-to-A cross-attention blocks;
- directional pooled representations;
- relation/implication/error/uncertainty heads;
- order-consistency losses.

**Acceptance**

- tensor-shape and masking tests cover variable lengths;
- swapped inputs swap directional outputs while preserving equivalence output within tolerance;
- M2 can initialize from the chosen pretrained encoder;
- parameter count and compute are reported.

### LF-029 — Final evaluation harness

**Implement**

- metrics and slices from Section 23;
- bootstrap confidence intervals;
- risk–coverage curves;
- calibration plots;
- paired method comparisons;
- failure export for qualitative audit.

**Acceptance**

- all metrics are tested on synthetic fixtures;
- tables regenerate from frozen predictions;
- no manual spreadsheet manipulation is required.

### LF-030 — Reranking experiment

**Implement**

- candidate scorer;
- reference-aware and NL-direct modes;
- abstention policies;
- ranker evaluation;
- cost/latency reporting;
- optional repair loop.

**Acceptance**

- end-to-end run from NL problem to selected Lean statement is reproducible;
- first/first-compiling/random/LLM-judge baselines are included;
- no hidden reference information leaks into the direct NL–Lean condition.

---

## 27. Test and continuous-integration strategy

### 27.1 Test classes

#### Unit tests

Cover:

- schema validation;
- hash/ID stability;
- pure normalization functions;
- transformation applicability;
- split assignment;
- label resolution;
- metric calculations;
- model masking and symmetry behavior.

Unit tests must not require network access or a full mathlib checkout.

#### Lean integration tests

Use the pinned fixture project and LeanInteract to test:

- project setup;
- command/file elaboration;
- declaration extraction;
- invalid code;
- `sorry` handling;
- timeouts/recovery;
- environment reuse;
- parallel execution;
- custom meta helpers.

#### Golden tests

Use small, reviewed fixtures for:

- extracted theorem signatures;
- representation JSON;
- transformation diffs;
- normalized diagnostics;
- label-resolution traces;
- evaluation tables.

Golden files may be updated only with a reviewed explanation.

#### Property-based tests

Use generated examples to check:

- alpha-renaming invariance;
- stable IDs independent of processing order;
- pair swap behavior;
- transformation round trips;
- split non-overlap;
- serialization round trips;
- deterministic sampling.

#### End-to-end smoke tests

A smoke pipeline should:

```text
extract 20 fixture theorems
→ build representations
→ generate positive and negative variants
→ validate through LeanInteract
→ create pairs/evidence
→ split data
→ run two baselines
→ train a tiny model
→ produce an evaluation report
```

It must complete in CI with bounded resources.

### 27.2 CI tiers

```text
PR-fast:
  format + lint + typecheck + unit tests + tiny model tests

PR-Lean:
  fixture Lake build + LeanInteract integration tests + smoke pipeline

Nightly:
  mathlib sample extraction + 10k request stress test + deterministic rerun

Weekly/release:
  larger source probes + dataset integrity + baseline reproduction + security/license checks
```

### 27.3 Required quality checks

- line and branch coverage thresholds for core policy/data modules;
- no unresolved schema migrations;
- no dependency with an unreviewed major upgrade;
- no test using final human labels unless tagged evaluation-only;
- no network access in deterministic dataset assembly after raw sources are cached;
- no secret patterns in repository or artifacts;
- no direct Lean subprocess invocation outside explicitly approved setup/doctor diagnostics.

### 27.4 Performance regression tests

Track:

- Lean requests/second by context type;
- server startup time;
- timeout/crash rate;
- peak worker memory;
- extraction time per file;
- representation time per theorem;
- transformation acceptance rate;
- model examples/second and inference latency.

Set alerts after a stable baseline exists. Do not optimize before correctness, but do not allow silent 2× regressions.

---

## 28. Operational, cost, and reproducibility controls

### 28.1 Configuration hierarchy

Use explicit layered configuration:

```text
configs/defaults.yaml
configs/environments/{local,cluster,ci}.yaml
configs/sources/*.yaml
configs/transforms/*.yaml
configs/generation/*.yaml
configs/judges/*.yaml
configs/models/*.yaml
configs/evaluation/*.yaml
```

Resolved configuration must be written into each run directory. Environment variables may provide secrets or machine-specific paths, but must not silently change semantic settings.

### 28.2 Run directory contract

Every substantial run creates:

```text
runs/<run_id>/
  resolved_config.yaml
  manifest.json
  environment.json
  inputs.json
  logs.jsonl
  metrics.json
  failures.jsonl
  outputs/ or output_pointers.json
  COMPLETE or FAILED
```

`run_id` should include a semantic name plus an immutable content hash. A run is complete only after all expected shards and checksums are verified.

### 28.3 Data storage policy

Use append-only/raw-first storage:

```text
raw source snapshot/reference
→ parsed record
→ elaborated record
→ representations
→ variants
→ pairs
→ evidence
→ resolved labels
→ split/release manifests
```

Never overwrite raw records when parsers improve. Write a new parser/version output and retain the lineage.

Parquet is preferred for large analytical tables; JSONL is acceptable for interchange and debugging. Large code/text blobs may be content-addressed separately to avoid duplication.

### 28.4 Cache policy

Cache keys must include all semantically relevant inputs:

- source/code content hash;
- project/context fingerprint;
- Lean version;
- dependency revisions;
- LeanInteract version;
- query options;
- custom meta-helper revision;
- timeout or tactic portfolio where relevant.

Do not reuse cached validity/certificate results across incompatible contexts.

### 28.5 API token and budget policy

For LLM calls:

1. Store credentials only in a secret manager or local environment.
2. Precompute a budget forecast by model/task.
3. Start with small stratified pilots.
4. Cache all successful responses.
5. Use idempotency keys where supported.
6. Enforce per-run and cumulative spending caps.
7. Stop automatically when output validity or diversity falls below configured thresholds.
8. Track input/output tokens, retries, latency, and monetary cost per accepted example.
9. Never route private or restricted data to an external API without approval.
10. Record model aliases and resolved version IDs where the provider exposes them.

Dozens of billions of available tokens are a resource, not a reason to generate undifferentiated data. Allocation should be driven by coverage gaps and active-learning value.

### 28.6 Checkpoint and artifact policy

- Store model checkpoints with exact data/config/code hashes.
- Keep best-by-validation and last checkpoint separately.
- Never identify a “best” checkpoint using final-test performance.
- Store predictions independently of checkpoints so evaluation can be rerun.
- Track tokenizer revision and any added special tokens.
- Record hardware, precision, distributed-training settings, and random seeds.

### 28.7 Reproducibility levels

Define three levels:

```text
R1 — record reproducibility:
     reconstruct one pair, all evidence, and its label.

R2 — experiment reproducibility:
     rerun a baseline/model evaluation from frozen predictions or checkpoint.

R3 — pipeline reproducibility:
     regenerate a release slice from source manifests through evaluation.
```

The paper artifact must support R1 and R2 publicly. R3 should be supported where source licensing and compute permit.

### 28.8 Monitoring dashboards

Track at minimum:

- extraction success/failure by source and context;
- LeanInteract crash/timeout/memory rates;
- transformation applicability and acceptance by family;
- semantic-atom drift distributions;
- LLM proposer validity/diversity;
- judge disagreement/calibration;
- annotation throughput/agreement;
- label quality by evidence tier;
- split leakage alerts;
- model calibration and slice performance;
- cost per usable gold/silver example.

Dashboards are diagnostic only; their derived numbers must still be reproducible from immutable records.

---

## 29. Risk register with triggers and mandatory responses

| ID | Risk | Early-warning trigger | Mandatory response | Residual-risk reporting |
|---|---|---|---|---|
| R01 | Synthetic-to-real gap | Synthetic validation rises while real-output validation is flat or falls | Stop scaling synthetic data; increase real-output/human data; reweight curriculum; inspect shortcut features | Report synthetic vs real learning curves and transfer gaps |
| R02 | Accidental positive labels | Human audit finds semantic erasure in an automatic-positive family | Disable family promotion; invalidate affected labels; bump family/policy version; regenerate releases | Publish affected counts and corrected results |
| R03 | Accidental negative labels | Counterexample/proof/human audit finds many mutated pairs remain same-claim | Demote family to unlabeled; refine applicability; require stronger evidence | Report per-family estimated label precision |
| R04 | Truth-equivalence collapse | Model scores broad simplifications or unrelated true propositions as faithful | Add F1/F2 contrast set; remove unsafe proof-derived positives; train explicit same-claim negatives | Include semantic-erasure slice |
| R05 | Failed-search misuse | Any code maps timeout/proof failure to negative | Block release in CI; correct resolution policy; audit all affected evidence | State search coverage and failure rates |
| R06 | Context drift | Same statement changes elaboration under different revisions/imports | Treat context as part of identity; invalidate incompatible caches; pin and record revisions | Report environment versions for every benchmark |
| R07 | LeanInteract API/version drift | Compatibility smoke test or response schema changes | Freeze upgrade; run documented compatibility suite; adapt backend only; bump environment version | List tested LeanInteract/Lean ranges |
| R08 | Lean server instability/OOM | Crash/timeout rate exceeds threshold or workers leak memory | Reduce worker count; use AutoLeanServer recovery; shard by context; add memory recycling | Report throughput and failure rates |
| R09 | Source schema drift | New dataset columns/format bypass adapter assumptions | Fail closed; add a new explicit schema adapter and tests | Version source adapter and manifest |
| R10 | Benchmark contamination | Exact/near-duplicate appears in training | Quarantine affected release/model; rebuild splits; retrain material experiments | Publish contamination audit |
| R11 | Generator/judge circularity | Performance is high only when judge/generator families match | Add held-out generator/judge evaluations; diversify roles; increase human labels | Report cross-family matrix |
| R12 | LLM consensus overconfidence | Judges agree on hidden traps but are wrong | Tighten promotion threshold; recalibrate; require human review for affected strata | Report calibration-item accuracy |
| R13 | Human disagreement | Low agreement on a relation/error family | Clarify guidelines; use `UNCERTAIN`; collect third labels; do not force binary gold | Report agreement and uncertain rate |
| R14 | Reference formalization defect | Annotators repeatedly flag references as wrong/underspecified | Add reference-status field; adjudicate separately; exclude or relabel task | Report reference-defect rate |
| R15 | Shortcut learning from provenance | Performance collapses after hiding names/source formatting | Strip/randomize shortcut fields; group splits; adversarial source prediction probe | Report source/generator probing results |
| R16 | Length/compilation shortcut | Model predicts based on length, syntax, or validation status | Match distributions; include hard controls; audit feature attribution | Report controlled slices |
| R17 | Duplicate dominance | A few theorem families dominate pair counts | Cap per ancestry cluster; weight examples; report effective sample size | Publish cluster-size distribution |
| R18 | Cost explosion | Cost per accepted example exceeds budget or diversity saturates | Stop broad generation; active-learn only high-value strata; use cheaper models for screening | Report cost per usable example |
| R19 | License/release restriction | Source terms prevent redistribution | Release adapters/IDs/derived allowed metadata; seek permission; exclude restricted fields | Publish license matrix |
| R20 | Graph scope creep | Graph work delays core benchmark/model | Enforce Phase 13 dependency; keep graph on separate branch/workstream | Report text-only complete system first |
| R21 | Calibration drift | Confidence no longer matches accuracy on new generator/domain | Recalibrate on a separate current-domain set; monitor risk–coverage | Report calibration per slice/date |
| R22 | Irreproducible API model versions | Provider alias changes silently | Store date, alias, request/response IDs, behavior probes; rerun a calibration panel | State version uncertainty explicitly |
| R23 | Annotation leakage | Annotators see generator, automatic label, or model score | Fix UI; invalidate affected annotations if material; reannotate blinded | Document blinding checks |
| R24 | Certificate leakage | Equivalence proof uses source/target theorem constant or equivalent global result | Run dependency scanner; mark certificate invalid; tighten namespace/environment | Report certificate-hygiene method |
| R25 | Overclaiming undecidable semantics | Paper treats predictor as a decision procedure | Frame as calibrated empirical metric; report abstention/unknown; separate certificates from predictions | Limit claims explicitly |

### 29.1 Incident procedure

For any material data or evaluation incident:

1. freeze affected releases and runs;
2. write an incident record with discovery date, scope, root cause, and affected hashes;
3. patch code/policy with a new version;
4. regenerate affected artifacts from the earliest compromised stage;
5. rerun leakage and integrity checks;
6. update reports and paper numbers;
7. preserve the incident history rather than rewriting it away.

---

## 30. Decision gates and pivot criteria

The project should use evidence-based pivots rather than continuing every idea indefinitely.

### 30.1 Data-generation decisions

- **Automatic positive family:** keep only if audited precision reaches Gate 4A. Otherwise use as silver/experimental or remove.
- **Negative family:** keep for supervised training only if accidental-equivalence risk is controlled. Otherwise use for active learning only.
- **LLM proposer:** reduce or stop allocation when accepted-example diversity and human utility plateau.
- **LLM judge:** never promote to silver if calibration on hidden gold is below policy threshold, regardless of apparent agreement.
- **Source corpus:** deprioritize if context reconstruction is too unreliable or licensing prevents meaningful use.

### 30.2 Modeling decisions

- Continue from M0/M1 to M2 only after the data pipeline and baselines are stable.
- Keep M2 only if it gives reproducible real-output or OOD gains over M1 commensurate with complexity.
- Add graph modeling only after the text/structural model is complete.
- Keep the graph model only under Gate 13.
- Prefer the simplest calibrated model within a small performance margin for deployment/cost comparisons.

### 30.3 Project-level pivot conditions

Pivot the research emphasis toward the benchmark/data contribution if:

- model gains over strong baselines are small but the benchmark exposes important metric failures;
- human annotation reveals that target semantics need a richer taxonomy;
- cross-generator transfer remains weak despite substantial real-output data.

Pivot toward a hybrid certifier/predictor if:

- learned scores are strong primarily where symbolic methods abstain;
- symbolic positives have very high precision but low coverage;
- combining certified and predicted evidence improves risk–coverage materially.

Do not claim a successful universal faithfulness metric if gains exist only on transformations seen during training.

---

## 31. Pre-registered experiment matrix

The exact set may be refined before the final test is opened, but the categories should remain stable.

### 31.1 Core model table

Rows:

```text
Exact/raw equality
Normalized equality
Token similarity classifier
GTED/operator-tree baseline
BEq+
LLM judge A
LLM judge ensemble
M0 dual encoder
M1 cross-encoder
M2 bidirectional cross-attention
M3 hybrid symbolic + learned
M4 graph-augmented, if retained
```

Columns:

```text
macro F1
faithful-class precision/recall/F1
AUROC/AUPRC
ECE/Brier/NLL
coverage at 95% faithful precision
latency
cost
human-test accuracy
held-out-generator accuracy
held-out-project accuracy
```

### 31.2 Data-ablation table

```text
synthetic deterministic only
LLM-generated only
real autoformalization only
synthetic + real
synthetic + real + silver judged
synthetic + real + human gold
all data without quality weighting
all data with quality weighting
```

### 31.3 Representation-ablation table

```text
raw Lean only
headless signature only
pretty-printed signature only
raw + explicit view
raw + semantic atoms
raw + structural scalar features
raw + operator tree
raw + Expr graph
```

### 31.4 Generalization matrix

Train/evaluate across:

- transformation families;
- theorem source projects;
- math domains;
- generator families;
- statement-length quartiles;
- confidence/evidence tiers;
- notation-heavy vs notation-light;
- binder/side-condition/quantifier/object errors.

### 31.5 Calibration and abstention figure

Plot risk–coverage curves for:

- M1;
- M2;
- hybrid model;
- strongest LLM judge;
- symbolic baseline with abstention.

Use a single frozen calibration split and report recalibration separately for domain-shift experiments.

### 31.6 Reranking table

Rows:

```text
first generated
first compiling
random compiling
self-ranked generator
LLM judge
Lean–Lean metric with reference
NL–Lean direct metric
combined NL + reference metric
oracle
```

Columns:

```text
faithful@1
faithful@3
MRR/nDCG
coverage at precision target
Lean validation cost
model/API cost
latency
```

### 31.7 Qualitative analysis

Publish representative examples for:

- symbolic success / learned failure;
- learned success / symbolic abstention;
- all methods fooled;
- truth-equivalent but claim-unfaithful;
- redundant-variable pathology;
- wrong side condition;
- wrong domain/coercion;
- reference defect;
- human disagreement;
- successful and harmful repairs.

Do not select only favorable examples; define selection rules before inspection.

---

## 32. Minimum viable project, strong paper, and stretch result

### 32.1 Minimum viable project

A complete MVP consists of:

1. pinned LeanInteract-backed extraction/validation system;
2. at least two source corpora;
3. conservative deterministic positive and hard negative families;
4. real autoformalization candidate collection;
5. evidence-preserving schemas and leak-free splits;
6. lexical, structural, BEq+, and LLM-judge baselines;
7. M1 cross-encoder and calibrated relation output;
8. expert-labeled real-output test set;
9. one downstream reranking experiment.

The MVP is scientifically useful even without graph modeling.

### 32.2 Strong paper

A strong paper additionally includes:

- M2 bidirectional cross-attention with directional entailment heads;
- well-audited, diverse data release;
- held-out generator/project/family generalization;
- high-quality calibration and abstention analysis;
- a clear synthetic-to-real ablation;
- measurable reranking gains;
- detailed error taxonomy and human agreement;
- a hybrid symbolic/learned system with favorable cost–quality trade-off.

### 32.3 Stretch result

Stretch contributions include:

- graph augmentation with robust OOD gain;
- actionable repair generation using error tags;
- active-learning efficiency results;
- transfer across Lean projects/versions;
- a jointly trained NL–Lean and Lean–Lean metric;
- use as a reward model during autoformalization training.

---

## 33. Paper claim boundaries

The paper may claim:

- a learned, calibrated predictor of expert-judged autoformalization faithfulness;
- improvements over specified baselines on specified distributions;
- utility for reranking or triage;
- certified equivalence for the subset with valid formal certificates;
- empirical generalization to held-out transformations/generators/projects.

The paper must not claim:

- a complete decision procedure for semantic equivalence;
- that proof-search failure proves inequivalence;
- that agreement among LLM judges is ground truth;
- that all logical equivalences preserve the intended natural-language claim;
- that a reference formalization is always correct;
- universal transfer beyond tested Lean versions/projects/domains.

---

## 34. Reference implementation policy for LeanInteract

LeanInteract is the default and mandatory Python–Lean integration layer for this project because it directly supports executing Lean commands/files, extracting declarations and InfoTrees, reusing environments, and incremental/parallel elaboration. The project should follow its documented concurrency pattern: construct the shared `LeanREPLConfig` before creating processes, then use one `AutoLeanServer` per worker when multiprocessing.

The initial pin is:

```toml
[project]
requires-python = ">=3.12,<3.13"
dependencies = [
  "lean-interact==0.11.4",
]
```

This pin is an initial implementation choice, not a permanent promise. Upgrades require the compatibility protocol below.

### 34.1 Upgrade protocol

1. Open an architectural maintenance issue.
2. Read the LeanInteract release notes and interface changes.
3. Update only a dedicated branch.
4. Run:
   - full backend unit tests;
   - fixture declaration-extraction goldens;
   - timeout/recovery tests;
   - parallel stress test;
   - 1,000-record source extraction comparison;
   - certificate baseline comparison.
5. Compare normalized responses and failure rates.
6. Bump `environment_schema_version` if serialized behavior changes.
7. Invalidate caches whose keys did not already isolate the version.
8. Record the decision and any result differences.
9. Merge only after review.

### 34.2 Escape hatch policy

A direct Lean subprocess or a different interface may be used only when all of the following hold:

- LeanInteract demonstrably cannot expose a required capability;
- a minimal reproducible limitation is documented;
- a custom Lean meta command invoked through LeanInteract cannot solve it;
- the project lead approves an architectural decision record;
- the alternative is isolated behind the same `LeanBackend` protocol;
- outputs and failure semantics remain compatible;
- the paper/release discloses the exception.

This is an exception mechanism, not permission to build a second runner “for convenience.”

---

# Appendices

## Appendix A — Illustrative LeanInteract integration patterns

These snippets establish the intended structure. The implementation must verify them against the pinned LeanInteract version and wrap them in project domain models rather than exposing third-party response objects everywhere.

### A.1 Single-command declaration extraction

```python
from __future__ import annotations

from lean_interact import AutoLeanServer, Command, LeanREPLConfig
from lean_interact.interface import CommandResponse, LeanError
from lean_interact.project import LocalProject

project = LocalProject(directory="/absolute/path/to/leanfaith-fixtures")
config = LeanREPLConfig(project=project, verbose=False)
server = AutoLeanServer(config)

code = """
import Mathlib

theorem leanfaith_example (n : Nat) : n + 0 = n := by
  simp
"""

result = server.run(
    Command(
        cmd=code,
        declarations=True,
        root_goals=True,
    ),
    timeout=60,
)

if isinstance(result, LeanError):
    raise RuntimeError(f"Lean infrastructure/query failure: {result}")
if not isinstance(result, CommandResponse):
    raise TypeError(f"Unexpected LeanInteract response: {type(result)!r}")

is_valid = result.lean_code_is_valid(allow_sorry=False)
for declaration in result.declarations:
    print(declaration.full_name)
    print(declaration.signature.pp)
    print(declaration.signature.constants)
```

Implementation notes:

- `LeanError` is an infrastructure/query result, not an invalid-theorem label.
- `lean_code_is_valid(allow_sorry=False)` is the validity check for complete proof/certificate code.
- Candidate theorem statements may deliberately use a placeholder proof during statement validation; that route must call `allow_sorry=True` and mark the record `statement_valid_with_placeholder`, not `proof_complete`.
- Store `DeclarationInfo.signature.pp`, constants, ranges, binders, type/value when available, and the complete response diagnostics.

### A.2 File-level extraction

```python
from lean_interact import AutoLeanServer, FileCommand, LeanREPLConfig
from lean_interact.interface import CommandResponse, LeanError
from lean_interact.project import LocalProject

config = LeanREPLConfig(project=LocalProject(directory="/path/to/mathlib4"))
server = AutoLeanServer(config)

result = server.run(
    FileCommand(
        path="Mathlib/Algebra/Group/Basic.lean",
        declarations=True,
    ),
    timeout=600,
)

if isinstance(result, LeanError):
    # Persist the error with file/context identity.
    ...
elif isinstance(result, CommandResponse):
    for declaration in result.declarations:
        ...
else:
    raise TypeError(type(result))
```

Do not assume every declaration is a theorem. Filter using declaration kind and project policy while retaining non-theorem metadata where useful.

### A.3 InfoTree extraction

```python
from lean_interact import AutoLeanServer, Command, LeanREPLConfig
from lean_interact.interface import CommandResponse, InfoTreeOptions

server = AutoLeanServer(LeanREPLConfig())
response = server.run(
    Command(
        cmd="theorem ex (n : Nat) : n = n := by rfl",
        declarations=True,
        infotree=InfoTreeOptions.substantive,
    ),
    timeout=60,
)

if isinstance(response, CommandResponse):
    trees = response.infotree or []
    for tree in trees:
        for command_node in tree.commands():
            ...
```

InfoTrees are optional structural evidence. The core extractor must remain functional when InfoTree extraction fails or is disabled, and failure must be recorded explicitly.

### A.4 Batch execution with `LeanServerPool`

```python
from lean_interact import Command, LeanREPLConfig, LeanServerPool

# Construct once before the pool so setup/build work is not repeated by workers.
config = LeanREPLConfig(verbose=False)
commands = [
    Command(cmd=f"#eval {i} * {i}")
    for i in range(100)
]

with LeanServerPool(config, num_workers=4) as pool:
    results = pool.run_batch(
        commands,
        timeout_per_cmd=60,
        show_progress=True,
    )

for request, result in zip(commands, results, strict=True):
    # LeanServerPool may return exceptions/errors alongside successful results.
    # Normalize every item; never assume the batch succeeded globally.
    ...
```

For the theorem pipeline, batch by compatible context and use one record ID per command. Preserve order only as a convenience; correctness must use IDs.

### A.5 Backend protocol sketch

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, Sequence


class LeanStatus(StrEnum):
    VALID = "valid"
    VALID_WITH_SORRY = "valid_with_sorry"
    INVALID = "invalid"
    TIMEOUT = "timeout"
    CRASH = "crash"
    SETUP_ERROR = "setup_error"
    UNSUPPORTED = "unsupported"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True)
class LeanRequest:
    request_id: str
    context_id: str
    code: str | None = None
    file_path: Path | None = None
    declarations: bool = False
    root_goals: bool = False
    infotree: str | None = None
    allow_sorry: bool = True
    timeout_seconds: float = 60.0


@dataclass(frozen=True)
class LeanResult:
    request_id: str
    status: LeanStatus
    declarations: tuple[dict, ...]
    messages: tuple[dict, ...]
    sorries: tuple[dict, ...]
    duration_seconds: float
    raw_response: dict | None
    infrastructure_error: str | None


class LeanBackend(Protocol):
    def run(self, request: LeanRequest) -> LeanResult: ...

    def run_batch(self, requests: Sequence[LeanRequest]) -> list[LeanResult]: ...
```

The concrete backend translates these models to and from LeanInteract. Source adapters and transformations should not depend on LeanInteract classes.

### A.6 Statement-validation template

```python
def wrap_statement_for_validation(
    *,
    imports: list[str],
    namespace_prelude: str,
    theorem_name: str,
    signature: str,
) -> str:
    import_block = "\n".join(f"import {module}" for module in imports)
    return f"""{import_block}
{namespace_prelude}

theorem {theorem_name} {signature} := by
  sorry
"""
```

Requirements:

- generated theorem names must be fresh and deterministic;
- validate the extracted declaration and verify its signature, not merely the absence of a process error;
- `sorry` is allowed only because the task is statement elaboration;
- the status must remain distinct from a complete proof certificate;
- do not inject both compared theorems as axioms into equivalence checks.

### A.7 Directional certificate template

```lean
import Mathlib

namespace LeanFaith.Certificates

-- A and B are embedded as proposition expressions, not imported theorem constants.
def A : Prop := by
  exact True  -- replaced by generated proposition expression

def B : Prop := by
  exact True  -- replaced by generated proposition expression

example : A → B := by
  -- bounded, recorded tactic portfolio or generated proof
  intro h
  exact h

example : B → A := by
  intro h
  exact h

end LeanFaith.Certificates
```

The actual code generator should place proposition expressions directly into fresh definitions or examples in an isolated namespace. A certificate scanner must reject dependencies on source/target theorem constants and other disallowed shortcuts.

---

## Appendix B — Example project configuration

```yaml
schema_version: 1
project_name: leanfaith
seed: 20260710

runtime:
  python: "3.12"
  lean_interact: "0.11.4"
  max_workers: 8
  request_timeout_seconds: 120
  file_timeout_seconds: 900
  memory_hard_limit_mb: 12288
  auto_server:
    max_total_memory: 0.85
    max_process_memory: 0.85
    max_restart_attempts: 3

lean_projects:
  mathlib:
    kind: local
    path: /data/lean/mathlib4
    git_url: https://github.com/leanprover-community/mathlib4.git
    revision: REPLACE_WITH_PINNED_COMMIT
  cslib:
    kind: git
    git_url: REPLACE_WITH_VERIFIED_CSLIB_URL
    revision: REPLACE_WITH_PINNED_COMMIT
  physlib:
    kind: git
    git_url: REPLACE_WITH_VERIFIED_PHYSLIB_URL
    revision: REPLACE_WITH_PINNED_COMMIT

sources:
  sft_classic:
    enabled: true
    dataset_id: formalmathatepfl/sft_classic
    revision: REPLACE_WITH_PINNED_DATASET_REVISION
    adapter_schema: auto_probe_then_explicit
  mathlib:
    enabled: true
    project_id: mathlib
    include_globs: ["Mathlib/**/*.lean"]
    exclude_globs: ["MathlibTest/**/*.lean"]

statement_filter:
  declaration_kinds: [theorem, lemma]
  min_signature_tokens: 5
  max_signature_tokens: 2048
  keep_propositions_only: true
  require_context_reconstruction: true

representations:
  raw: true
  headless: true
  pretty_signature: true
  explicit_binders: true
  semantic_atoms: true
  operator_tree: true
  infotree: substantive

transformations:
  max_variants_per_theorem: 12
  positive_families:
    - alpha_rename_v1
    - split_merge_binders_v1
    - independent_binder_reorder_v1
  negative_families:
    - relation_boundary_v1
    - quantifier_flip_v1
    - drop_side_condition_v1
    - same_type_constant_substitution_v1
  automatic_gold_positive_families: []  # populated only after audit gates

proof_checks:
  enabled: true
  per_direction_timeout_seconds: 20
  tactics:
    - rfl
    - simp
    - aesop
    - omega
    - linarith
    - ring
  failed_search_label: unknown
  certificate_dependency_scan: true

splits:
  train: 0.80
  validation: 0.08
  calibration: 0.04
  internal_test: 0.08
  group_keys:
    - source_problem_id
    - theorem_ancestry_id
  held_out_transformation_families: []
  held_out_projects: []
  benchmark_denylist_manifest: data/benchmarks/frozen_ids.json

output:
  root: /data/leanfaith
  write_raw_responses: true
  compression: zstd
  overwrite_frozen_release: false
```

Placeholders beginning with `REPLACE_WITH_` must cause `leanfaith doctor` or config validation to fail.

---

## Appendix C — Pair and evidence examples

### C.1 Positive structural restatement

```lean
-- Source
∀ n : Nat, n + 0 = n

-- Variant
∀ x : Nat, x + 0 = x
```

Expected:

```yaml
relation: EQUIVALENT_SAME_CLAIM
faithfulness_level: F1
primary_evidence:
  - alpha_renaming_certificate
semantic_atom_change: none
```

### C.2 Truth-equivalent but claim-unfaithful simplification

```lean
-- Source
∀ n : Nat, n + 0 = n

-- Variant
True
```

Both propositions are provable, and their truth values coincide in the current theory, but they do not express the same mathematical claim.

Expected:

```yaml
relation: NOT_SAME_CLAIM
faithfulness_level: F2_ONLY_OR_TRUTH_COLLAPSE
error_tags: [E_SEMANTIC_ERASURE]
```

This example must appear in training/validation contrast sets.

### C.3 Directional weakening

```lean
-- Source A
∀ x : Real, x < 0 → x ≤ 0

-- Candidate B
∀ x : Real, x ≤ 0 → x ≤ 0
```

A and B are both provable, but B removes the meaningful strict-negativity premise and becomes tautological.

Expected:

```yaml
relation: NOT_SAME_CLAIM
error_tags: [E_ASSUMPTION_WEAKENED, E_TRIVIALIZATION]
```

### C.4 Missing side condition

```lean
-- Source
∀ x y : Real, y ≠ 0 → x / y = 1 → x = y

-- Variant
∀ x y : Real, x / y = 1 → x = y
```

The variant is a hard near-miss whose validity/equivalence depends on the semantics and totalized division behavior. It must not be labeled merely from mutation intention. Run Lean checks, seek a counterexample, and route to human review when evidence is insufficient.

### C.5 Redundant/free variable pathology

```lean
-- Reference
∀ n : Nat, n + 0 = n

-- Candidate
∀ n k : Nat, n + 0 = n
```

Whether this is accepted as faithful is a policy question. The default plan treats semantically unused extra binders as a specific faithfulness defect unless the annotation policy explicitly declares them harmless for the deployment task.

Expected default:

```yaml
relation: NOT_SAME_CLAIM_OR_MINOR_DEFECT
error_tags: [E_UNUSED_EXTRA_BINDER]
requires_policy_version: true
```

### C.6 Wrong domain

```lean
-- Reference
∀ x : Real, x^2 ≥ 0

-- Candidate
∀ x : Nat, x^2 ≥ 0
```

Expected:

```yaml
relation: CANDIDATE_WEAKER_OR_DOMAIN_RESTRICTED
error_tags: [E_WRONG_DOMAIN]
```

### C.7 Reference defect

A human annotator may determine that the released reference omits a condition present in the natural-language statement. Store:

```yaml
reference_status: SUSPECT_OR_INCORRECT
pair_label: UNCERTAIN
adjudication_required: true
```

Do not force the candidate to be wrong because it differs from a defective reference.

---

## Appendix D — LLM proposer prompt template

```text
SYSTEM
You are generating research data for evaluating Lean 4 autoformalization
faithfulness. You propose candidate theorem STATEMENTS only. Do not provide a
proof unless asked. Preserve imports and the available environment. Return
strict JSON matching the schema below.

USER
Natural-language problem:
{{natural_language}}

Reference Lean theorem statement:
```lean
{{reference_statement}}
```

Task relation to create:
{{target_relation}}

Allowed error family:
{{target_error_family}}

Requirements:
1. The output must be a single Lean theorem statement that should elaborate in
   the supplied environment after a placeholder proof is attached.
2. Make the smallest change that realizes the requested semantic relation.
3. Avoid mere formatting changes unless the requested relation is equivalent.
4. Do not copy the theorem proof.
5. Identify the exact changed mathematical content.
6. State uncertainty. Your claimed label will be treated only as a proposal.

JSON schema:
{
  "candidate_statement": "string",
  "intended_relation": "equivalent|reference_stronger|candidate_stronger|not_same_claim|uncertain",
  "error_tags": ["string"],
  "changed_content": "string",
  "reasoning_summary": "string",
  "counterexample_sketch": "string|null",
  "confidence": 0.0
}
```

The production system must reject extra prose and schema violations while retaining the raw output for diagnosis.

---

## Appendix E — Blinded LLM judge prompt template

```text
SYSTEM
Judge whether two Lean 4 theorem statements express the same mathematical
claim. Do not judge merely whether both are true or provable. Account for
quantifiers, domains, assumptions, conclusions, constants, coercions,
redundant variables, and accidental trivialization. Return strict JSON.

USER
Statement A:
```lean
{{statement_a}}
```

Statement B:
```lean
{{statement_b}}
```

Optional natural-language source:
{{natural_language_or_omitted}}

Choose one relation:
- equivalent_same_claim
- a_stronger
- b_stronger
- overlapping_but_not_equivalent
- unrelated_or_wrong_claim
- uncertain

Also predict whether A implies B and whether B implies A, but use "unknown"
when you cannot justify a direction. Do not infer non-implication from failure
to find a proof.

Return:
{
  "relation": "...",
  "a_implies_b": "yes|no|unknown",
  "b_implies_a": "yes|no|unknown",
  "same_claim_probability": 0.0,
  "error_tags": ["..."],
  "minimal_difference": "...",
  "confidence": 0.0
}
```

Judge inputs must not reveal source, generator, proposer intention, other judges, automatic transformations, or existing labels.

---

## Appendix F — Human annotation checklist

For each pair, annotators answer in order:

1. Does each statement elaborate in the recorded environment? This is supplied by tooling, not manually inferred.
2. Is either statement/reference malformed, vacuous, or obviously defective?
3. What are the quantified objects and their domains?
4. What assumptions are made?
5. What conclusion is asserted?
6. Are any constants, relations, operators, literals, or coercions materially different?
7. Has either statement been generalized or restricted?
8. Is one statement stronger or weaker?
9. Are there unused, extra, or missing binders?
10. Could both statements be true while expressing different claims?
11. Do they express the same mathematical content under the annotation policy?
12. Which error tags apply?
13. What is the annotator's confidence?
14. Is adjudication needed?

Annotators must not use “both are provable” as sufficient justification for `equivalent_same_claim`.

---

## Appendix G — Dataset release checklist

A release candidate may be frozen only when all boxes are checked:

```text
[ ] Source manifests and licenses are complete.
[ ] Lean, project, dependency, LeanInteract, and meta-helper revisions are pinned.
[ ] All records pass current schemas.
[ ] All raw-to-derived lineage pointers resolve.
[ ] Context reconstruction succeeds for all included pairs.
[ ] Pair sides elaborate under the recorded placeholder-proof policy.
[ ] Automatic-positive families passed their audit gates.
[ ] Supervised negatives meet evidence requirements.
[ ] Gold/silver/weak labels are distinguishable.
[ ] Ancestry groups do not cross splits.
[ ] External benchmark/test IDs are absent from training.
[ ] Exact, normalized, structural, and fuzzy leakage checks pass.
[ ] Duplicate and cluster distributions are reported.
[ ] Generator/source/transformation distributions are reported.
[ ] Failure and exclusion counts are reported.
[ ] Release assembly is deterministic.
[ ] Checksums and release manifest are written.
[ ] Data card documents limitations and intended use.
[ ] A clean smoke load and evaluation pass succeeds.
```

---

## Appendix H — Experiment completion checklist

```text
[ ] Research question and hypothesis are named.
[ ] Data release and split hashes are fixed.
[ ] Model/baseline config is versioned.
[ ] All random seeds are recorded.
[ ] Checkpoint and tokenizer revisions are recorded.
[ ] Thresholds were selected without test labels.
[ ] Calibration used only the calibration split.
[ ] Predictions are saved for every eligible example.
[ ] Missing/failed predictions are counted.
[ ] Overall and slice metrics are produced.
[ ] Confidence intervals are computed.
[ ] Runtime, hardware, latency, and cost are reported.
[ ] Results can be regenerated from prediction files.
[ ] Failure examples are selected by a declared procedure.
[ ] No final-test result was used to alter the method.
```

---

## Appendix I — First executable vertical slice

The coding agent should start with the smallest end-to-end path below, not with mass data generation or model code.

### Input

A fixture Lean file containing ten theorem statements.

### Pipeline

```text
1. `leanfaith doctor`
2. extract declarations with LeanInteract
3. create theorem/context records
4. build raw/headless/semantic-atom views
5. alpha-rename five theorems
6. apply one typed relation mutation to five theorems
7. validate all variants with LeanInteract
8. create pair/evidence records
9. resolve only the alpha pairs as conservative positives;
   retain mutations as provisional candidates
10. make a grouped train/validation split
11. run exact and token-similarity baselines
12. train a tiny M1 model solely as a software smoke test
13. emit a report with lineage for every prediction
```

### Vertical-slice acceptance

- one command runs the pipeline from clean fixture inputs;
- every Lean call uses LeanInteract;
- every artifact has a schema/version/hash;
- no mutation-intended negative is silently promoted;
- rerun output manifests are identical;
- deleting an intermediate shard and resuming reconstructs it correctly;
- the report links predictions to pair, variant, source theorem, context, and evidence.

Only after this slice passes should the agent implement large-scale source extraction.

---

## Appendix J — Selected references and implementation anchors

### Lean/Python infrastructure

- LeanInteract repository and documentation: <https://github.com/augustepoiroux/LeanInteract>
- LeanInteract data extraction guide: <https://github.com/augustepoiroux/LeanInteract/blob/main/docs/user-guide/data-extraction.md>
- LeanInteract performance guide: <https://github.com/augustepoiroux/LeanInteract/blob/main/docs/user-guide/performance.md>
- LeanInteract BEq+ example: <https://github.com/augustepoiroux/LeanInteract/blob/main/examples/beq_plus.py>
- LeanInteract scalable declaration extraction example: <https://github.com/augustepoiroux/LeanInteract/blob/main/examples/extract_mathlib_decls.py>

### Faithfulness/equivalence evaluation

- ProofNetVerif / BEq+ paper and released benchmark.
- GTED: graph/tree-edit-based evaluation of formalized theorem statements.
- FormalAlign: learned evaluation/alignment for autoformalization.
- ASSESS/TransTED and related structural-semantic metric work.

### Typed mutation and test generation

- Type-aware operator mutation for SMT solvers.
- Generative type-aware mutation for SMT solver testing.
- Grammar-based enumeration for SMT solver correctness/performance testing.
- Dominik Winterer's work on semantic fusion and metamorphic testing.

### Corpora/models to verify and pin during source probing

- `formalmathatepfl/sft_classic` or the exact accessible dataset/config variant.
- mathlib4.
- CSLib.
- PhysLib.
- ProofNetVerif.
- ReForm-32B or the exact available model checkpoint.

Every external source/model must be recorded by exact revision and license in the project manifests. Names in this appendix are planning anchors, not permission to rely on mutable aliases.

---

# Final definition of project completion

The project is complete when it delivers a reproducible LeanInteract-backed system that:

1. reconstructs and validates Lean theorem statements in pinned environments;
2. stores multiple representations and complete provenance;
3. creates conservative certified positives, difficult typed provisional negatives, LLM-diversified examples, real autoformalization failures, and expert gold labels;
4. prevents truth-level proof success from being confused with same-claim faithfulness;
5. trains and calibrates a bidirectional Lean–Lean relation model;
6. evaluates against strong symbolic, structural, and LLM baselines on frozen human and external sets;
7. demonstrates useful autoformalization reranking or repair behavior;
8. reports uncertainty, abstention, failure modes, compute, and cost;
9. releases the code, policies, manifests, and permitted data with enough information to reproduce the central results.
