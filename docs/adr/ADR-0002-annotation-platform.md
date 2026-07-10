# ADR-0002: Annotation platform for blinded Lean-pair labeling

**Status:** Proposed (finalizes at LF-023; see Decision)
**Date:** 2026-07-10
**Source of truth:** PLAN.md section 6.5 (lines 366-369) and section 18. The annotation
tooling track may begin only after Gate 3 (PLAN.md section 18.1, backlog LF-023
prerequisites LF-001 through LF-015).

## Context

PLAN.md section 6.5 mandates Argilla or Label Studio with a Lean-pair template. A thin
Streamlit fallback is permitted only after a bounded integration spike documents that
BOTH existing tools fail required blinding/schema/export behavior.

The platform must implement the section 18 protocol, which fixes the requirements any
candidate is evaluated against:

- **Blinding (18.4):** show proof-stripped statements, elaborated signatures, minimal
  context/import summary, and typecheck status; optional structural/reference panels
  explicitly marked; HIDE generator, transformation, intention, prior votes, split, and
  model scores.
- **Schema (18.5):** `same_claim` (4-way including `cannot_assess_yet`), `relation`
  (7-way), `error_types` (E01-E30 multi-label per `policies/error_ontology_v1.yaml`),
  `confidence` (1-5), `rationale` (required for not-same/ambiguous), `reference_issue`
  (3-way). `cannot_assess_yet` must route to unresolved review, not terminal ambiguity.
- **Protocol (18.6):** two independent expert labels with no discussion before first
  labels; adjudication of disagreement/low-confidence/policy triggers; raw labels and
  rationales preserved; versioned guideline attached to every label; agreement
  computation (raw agreement, Cohen's kappa, per-category) from exported data.
- **Export/import:** lossless round-trip into the canonical schemas so that stratum,
  inclusion probability, and design weight (18.3) survive the platform boundary.

## Decision

decision: **Default platform is Argilla with a Lean-pair template.** Label Studio is the
compared alternative, evaluated against the identical requirement list above. A thin
Streamlit fallback is permitted only after a bounded integration spike produces a
written report documenting, per requirement, that both Argilla and Label Studio fail
required blinding, schema, or export behavior; "inconvenient" does not qualify as
"fail."

The comparison and the final choice are executed and evidenced at **LF-023**
(annotation integration: blind templates/export/import/adjudication/agreement). This
ADR moves to Accepted, naming the chosen platform and attaching the evidence paths,
when LF-023 closes. Until then no gold or pilot round may run.

Selection criteria, in order of precedence:

1. hard blinding of the section 18.4 hidden fields (no leakage through metadata, URLs,
   or record ordering);
2. faithful representation of the section 18.5 fields, including multi-label E-codes
   and conditional required rationale;
3. lossless export/import round-trip with sampling metadata intact;
4. support for two-annotator independence and a separate adjudication queue;
5. rendering quality for Lean Unicode statements and signatures;
6. operational simplicity (self-hosted, no per-item external service dependency).

## Consequences

1. LF-023 must produce, for the chosen platform: the Lean-pair template, a blinding
   verification check, export/import round-trip tests, the adjudication workflow, and
   agreement computation on exported labels. These artifacts are the acceptance
   evidence that flips this ADR to Accepted.
2. If the Streamlit fallback is invoked, the spike report (scope, time-box, per-tool
   per-requirement failures) is appended to this ADR when it is reopened; a fallback
   without that report violates PLAN.md section 6.5.
3. Guideline versions (section 18.6) are recorded per label regardless of platform;
   platform choice may not weaken the Gate 7 thresholds (kappa >= 0.60, raw
   agreement >= 80%) or the rule that failure triggers guideline revision and a new
   blinded pilot, never threshold lowering.
4. Annotator-facing text renders the section 6.4 Unicode symbols; if the platform
   mangles them, that is a disqualifying schema/rendering failure under criterion 5.
