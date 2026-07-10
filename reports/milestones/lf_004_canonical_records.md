# LF-004 — Canonical record schemas

**Date:** 2026-07-10
**Scope (PLAN.md §26):** implement §11 modules, semantic/evidence target-kind
integrity, reverse label links, and cross-record invariants.

## Delivered

Canonical schema homes exactly per §7.1 (definitions never duplicated;
`schemas/__init__.py` re-exports only):

- `enums.py`: all §11.1 enums verbatim (ResolutionOutcome, RelationLabel,
  IntendedRelation, QualityTier, ValidationStatus, TransformationFamilyStatus,
  EvidenceExecutionStatus, SemanticLabelTargetKind, EvidenceTargetKind) plus
  supporting operational enums (NLTrust §9.4, ViewStatus, GeneratorKind,
  Polarity, EvidenceKind §16.2, LLMRole §17.2, ParseStatus, AnnotationAnswer
  §18.5, ReferenceIssue, Decision §20.6, SourceKind/AccessStatus §9.5,
  ArtifactClass, DataStage).
- `ids.py` (extended): canonical ID prefixes fixed once (`ctx`, `thm`, `anc`,
  `repr`, `var`, `pair`, `ev`, `lbl`, `nllean`, `draft`, `audit`, `call`,
  `ann`) and `id_pattern()` for field validation.
- `theorem.py`: `ContextRecord` (§11.2; context_id == "ctx:"+fingerprint
  enforced), `TheoremRecord` (§11.3; ancestry rules of §12.6 — sorted unique
  roots, single root without parents, union with parents), and
  `RepresentationRecord` (§11.4; §13.2 view names verbatim, full view-status
  cover, null-view⇔non-ok-status consistency, UTC timestamps).
- `variant.py`: `VariantRecord` (§11.5; quality stays provisional until
  resolution — supervised quality only via ResolvedLabel), plus the §15.2
  support records `Applicability`, `VariantDraft`, `TransformationAudit`
  (§11.10/§7.1 place their single definition here; LF-016 imports them).
  E01–E30 spelling guard (`ECODE_PATTERN`).
- `pair.py`: `PairRecord` (§11.6; sorted-unique `split_group_ids`) and
  cross-record `check_pair_groups` (§19.5 union rule: both sides' root
  ancestries + NL problem group).
- `evidence.py`: `EvidenceRecord` (§11.7) with typed per-kind values
  (Typecheck/Defeq/Proof/ClaimAlignment/Counterexample/Judgment/Audit),
  target-kind→ID-prefix integrity, success-requires-value, kind↔value-type
  consistency, pair-only kinds barred from family targets. `not_proved` /
  `not_found` are values, never labels.
- `label.py`: `ResolvedLabel` (§11.8) enforcing: F1==same_claim; outcome↔
  same_claim (null never means false); §3.5 review route vs terminal
  ambiguity; §3.3 relation consistency; mechanical F2 derivation from
  directional truths (failed search stays null); §14.5 eligibility rules
  (unknown→no training target, provisional→never eval); §14.4 equivalent→
  E29-only. Cross-record `check_label_target_link` verifies the one-to-one
  reverse link.
- `nl_lean.py`: `NLPLeanRecord` (§11.9) with `ReferencePairLink`
  (per-reference PairRecords, declared-reference check, problem group in
  split groups).
- `llm.py`: `LLMCallRecord` (§11.11) — parse-status consistency, §9.2
  private-source approval requirement, primary-eval-judge supervision
  exclusion (§17.2).
- `annotation.py`: `AnnotationRecord` (§18.5) — rationale required for
  not-same/ambiguous, confidence 1–5, target integrity.
- `prediction.py`: `PredictionRecord` (§20.6) — pair/nllean record IDs only,
  canonical relation spellings only, E-code guard, score bounds.
- `source.py`: `SourceManifest` (§9.5) with §9.2 approval consistency.

## Acceptance evidence

```text
uv run ruff check .          → All checks passed!
uv run ruff format --check . → all files formatted
uv run mypy                  → Success: no issues found in 23 source files
uv run pytest                → 143 passed
```

Golden cases: Appendix C.1 (alpha restatement), C.2 (claim erasure with
F2=true, F1=false), C.7 (review route), C.8 (terminal ambiguity) all
construct; every §11.8 invariant has a dedicated violation test.

## Notes / deviations

- §15.2 shows the support records as frozen dataclasses; they are implemented
  as frozen Pydantic strict models so `extra="forbid"` and JSON round-trips
  hold uniformly. Field names/semantics match §15.2 exactly. The §7.1 canonical
  home (schemas/variant.py) is used, matching the "definitions never
  duplicated" rule; LF-016 imports these rather than redefining them.
- Ambiguous-relation rule: `relation=ambiguous` is required exactly for
  terminal-ambiguity labels; unresolved review routes serialize
  `relation=unknown` (matches Appendix C.7/C.8).

**Next:** LF-005 — backend protocol (exact Appendix A.5 contract, §8.4 parity
test).
