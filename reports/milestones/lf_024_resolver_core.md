# LF-024 — Diagnostic resolver core

**Status:** diagnostic core implemented and focused tests pass; **LF-024 is not
complete**

**Date:** 2026-08-11

**Scope:** deterministic resolution mechanics and diagnostic batch plumbing
only

## Milestone boundary

This milestone establishes the fail-closed core that can replay diagnostic
label-resolution semantics. The public resolver accepts candidates only through
an opaque process-local `VerifiedCandidateSet` capability. No nonempty
production capability can currently be minted, so the batch operation requires
an explicitly empty raw-candidate partition and emits unresolved `REVIEW`.
Raw-candidate behavior remains testable only through a clearly private
diagnostic core. This milestone does not establish the production adapters that
decide whether an admission, authority artifact, or candidate set is genuine.
Therefore it authorizes no production semantic label, training label,
evaluation label, promotion, or evidence admission.

| Production output created by this milestone | Count |
|---|---:|
| Production semantic labels | 0 |
| Production evidence admissions | 0 |
| Production candidate promotions | 0 |

Any labels constructed by the focused tests are in-memory fixtures. Any batch
output produced through the current operation is diagnostic-only.

## Implemented diagnostic core

The implementation is split into the following modules:

- `src/leanfaith/labeling/aggregation.py` implements an admission-gated F0/F2
  evidence projection. It keeps F1 outside the mechanical evidence layer,
  treats failed proof search and a missing counterexample as unknown, accepts a
  positive definitional-equality result without treating `not_equal` as F0
  refutation, and records accepted and ignored evidence explicitly.
- `src/leanfaith/labeling/quality.py` loads the exact Gate-0-bound resolution
  policy and defines content-addressed candidate and authority-binding shapes.
  These shapes are necessary plumbing, but their referenced artifact content
  is not yet independently verified by typed loaders.
- `src/leanfaith/labeling/conflicts.py` defines append-only,
  content-addressed conflict and precedence-override records.
- `src/leanfaith/labeling/resolution.py` keeps raw-candidate precedence and
  conflict behavior in `_resolve_target_diagnostic` for focused tests. Its
  public `resolve_target` and replay verifier can obtain candidates only by
  opening an immutable, non-copyable, non-serializable
  `VerifiedCandidateSet`. The sole currently minted capability is the empty
  set; there is no public constructor or nonempty production factory.
- `src/leanfaith/cli/resolve_labels.py` provides a staged, rollback-guarded
  diagnostic batch operation over explicit target, evidence, empty-candidate,
  and optional prior-label partitions. It rejects a nonempty raw-candidate
  partition before resolution or output publication, hashes every input and
  output partition, rechecks inputs and policy
  bytes immediately before publication, records canonical invocation paths,
  writes a run manifest stating that no candidate was inferred or promoted.
  Every diagnostic label is projected to
  `train_eligibility=false, eval_eligibility=false`; the dependent label ID,
  linked-target hash/link, and resolution-audit ID are recomputed and checked.
  The external run manifest is the commit marker. A strict descriptor inside
  the output binds its run ID and expected manifest hash, so a recognized
  output left by the hard-crash publication gap can be atomically quarantined
  before reuse. Ordinary Python exceptions use best-effort rollback; this is
  not a claim of a filesystem-wide atomic transaction.

The core currently verifies these invariants:

1. F0 and F2 evidence never create an F1/same-claim label.
2. Failed proof search, unsupported counterexample search, and
   counterexample-not-found remain unknown rather than negative.
3. The target's linked evidence is an exact closed set; duplicate, missing,
   cross-target, stale-policy, or multiply admitted inputs fail closed.
4. Strong semantic or certificate disagreement produces an append-only
   conflict plus unresolved REVIEW output rather than destructive overwrite.
5. Deterministic input ordering, content-addressed records, target reverse
   links, and replay verification are exercised by focused tests.
6. Public diagnostic resolution cannot consume a raw semantic candidate;
   nonempty candidate files fail closed and the structural candidate-set receipt
   cannot mint or coerce the opaque capability.
7. Diagnostic outputs are never eligible for model training or evaluation,
   including human- and benchmark-derived semantic fixtures.
8. The exported resolver core itself cannot enable training or evaluation
   eligibility; that capability remains absent until the deferred production
   adapters are independently verified.

## Production guard

Production resolution is deliberately disabled. Calling the batch operation
with `artifact_class=production` fails before input loading or output creation
with the guard:

```text
only diagnostic label resolution is enabled until typed authority and
evidence-admission adapters independently verify bound artifact content
```

The run manifest for an allowed diagnostic execution records
`linked_evidence_graph_closed=true`, `candidate_partition_explicit=true`,
`raw_candidate_partition_required_empty=true`,
`verified_candidate_capability=false`, `candidate_set_closed=false`,
`production_admission=false`,
`candidate_inference=false`, and `candidate_promotion=false`.

## Focused verification

**Verified snapshot (2026-08-11): 118 focused unit tests passed.**

| Test module | Passed |
|---|---:|
| `tests/unit/test_labeling_aggregation.py` | 13 |
| `tests/unit/test_labeling_conflicts.py` | 15 |
| `tests/unit/test_labeling_quality.py` | 36 |
| `tests/unit/test_labeling_resolution.py` | 21 |
| `tests/unit/test_resolve_label_batch.py` | 13 |
| `tests/unit/test_resolve_labels_cli.py` | 8 |
| `tests/unit/test_resolve_labels_transaction.py` | 12 |
| **Total** | **118** |

The focused command was:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run pytest -q -p no:cacheprovider \
  tests/unit/test_labeling_aggregation.py \
  tests/unit/test_labeling_conflicts.py \
  tests/unit/test_labeling_quality.py \
  tests/unit/test_labeling_resolution.py \
  tests/unit/test_resolve_label_batch.py \
  tests/unit/test_resolve_labels_cli.py \
  tests/unit/test_resolve_labels_transaction.py
```

This count documents the diagnostic core only. It is not an LF-024 completion
claim: the deferred production-verification boundary below remains mandatory.

The same commit candidate passed the complete repository test suite,
repository-wide Ruff and formatting checks, strict mypy over all 278 source
modules, `leanfaith doctor`, and the LeanInteract fixture checks exercised by
the full suite.

## Deferred work required for LF-024 completion

The following adapters and closure mechanisms remain mandatory before the
production guard may be removed:

1. **Typed evidence audit and admission verifier.** Load the referenced
   manifest, raw certificate, audit, and replay receipt; recompute their hashes;
   verify target, subject-evidence, context, method, configuration, policy, and
   required certificate checks; and reject self-asserted replay booleans or
   generic all-true audit maps.
2. **Method-specific candidate-evidence validation.** Enforce the exact
   evidence kinds and payloads for expert adjudication, benchmark import,
   conservative positive certificates, checked separators, directional
   proof-plus-reverse-separator with claim alignment, and independent
   multi-family/multi-orientation consensus. Evidence must support the
   candidate's actual same-claim and relation fields, not merely satisfy a
   count.
3. **Typed authority-artifact loaders.** Independently load and verify human
   adjudication, frozen benchmark label, family-promotion,
   certificate/separator, and consensus-promotion artifacts before constructing
   an authority-bound candidate. A caller-supplied artifact ID and hash are not
   sufficient authority.
4. **Typed candidate-set replay and capability minting.** Replay every
   source-specific authority inventory, bind every admissible candidate for one
   target, and only then mint a target/policy-bound nonempty
   `VerifiedCandidateSet`. The existing structural manifest verifier is
   diagnostic and is intentionally unable to mint or coerce this capability.
5. **Safe re-resolution and conflict closure.** Bind the prior label, prior
   audit, and prior closed candidate set; preserve the incumbent authority;
   prevent authority downgrade; record the superseded label; and require a
   typed human-adjudication or benchmark-incident closure artifact that names
   each conflict it closes after required certificate replay.
6. **Typed representation certificate and F0 derivation.** Add verified
   representation-equivalence and structural-separation evidence. Promoted P01
   alpha identity must derive F0=true through accepted typed evidence;
   policy-accepted structural separation may derive F0=false; a generic
   counterexample or `defeq not_equal` must not silently serve as that
   certificate. Human/benchmark F0 remains accepted only when its verified
   authority artifact explicitly carries the judgment.

Each item requires negative tests showing that missing, stale, mismatched,
cross-target, or tampered artifacts fail closed. Only after these items and the
repository-wide LF-024 checks pass may the production CLI guard be reconsidered
and an LF-024 completion milestone be written.

## Background data work

Deterministic transformation jobs and LLM generation/judgment jobs are
unaffected by this diagnostic resolver milestone and may continue under their
existing policies. Their outputs remain provisional: they do not become
semantic labels, gold/silver promotions, training records, calibration data, or
evaluation data through the current resolver core.
