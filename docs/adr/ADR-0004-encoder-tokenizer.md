# ADR-0004: Encoder and tokenizer selection

**Status:** authored_pending_gate_0_review (Gate 0 approval is recorded in
`reports/milestones/phase_0_contract.md`, not here; finalizes at the LF-028
tokenizer audit; see Decision)
**Date:** 2026-07-10
**Source of truth:** PLAN.md section 6.4 (lines 362-364), section 13.7,
sections 21.1-21.3.

## Context

The model family of PLAN.md sections 21.1-21.3 (M0 dual encoder through M4, plus the
M5 graph extension) needs a pretrained encoder whose tokenizer handles Lean statement
text well. Lean text is dense in Unicode mathematical symbols, dotted namespaces,
subscripted identifiers, and long elaborated signatures; a tokenizer that fragments
these inflates sequence length, forces truncation, and degrades exactly the
near-miss-sensitive comparisons the project targets.

PLAN.md fixes the candidate set and the audit obligation: default candidate
ModernBERT-large, compared against the CodeT5+ encoder and DeBERTa-v3-large on a fixed
pilot (section 6.4); the comparison runs on fixed extracted strata with
sequence/truncation statistics and Unicode fragmentation reported, special-token
changes tested only on pilot (section 13.7); the ADR freezes tokenizer, special tokens,
max lengths, and truncation before any non-smoke training (section 21.2).

## Decision

decision: **Default candidate is ModernBERT-large.** It is the presumptive encoder for
the whole model family (M0 dual encoder through M4, and the M5 graph extension,
PLAN.md sections 21.1-21.3) unless the fixed-pilot comparison overturns it; the
section 13.7 decision recorded here governs all non-smoke training.

decision: The comparison protocol is fixed as follows and executed at **LF-028**:

1. **Candidates:** ModernBERT-large (default), CodeT5+ encoder, DeBERTa-v3-large. No
   other encoder enters the comparison without superseding this ADR.
2. **Data:** fixed extracted strata from the pinned environment (ADR-0001); the same
   strata for all three candidates; strata identified by manifest hash (ADR-0003).
3. **Tokenizer audit (blocking):** before any non-smoke training, audit Unicode/token
   fragmentation for the section 6.4 symbol list
   `∀ ∃ → ↔ ≤ ≥ ⊆ ∈ ∉ ⟨ ⟩`, plus namespaces (dotted identifiers such as
   `Nat.succ_le_iff`), subscripts, common constants, and explicit elaborated
   signatures. Report per-candidate: tokens-per-symbol for each listed symbol,
   sequence-length distributions per representation, and truncation rates at candidate
   max lengths.
4. **Special tokens:** additions are permitted only with measured benefit on the pilot
   (same strata, before/after comparison) and are recorded in this ADR when it is
   finalized. No speculative vocabulary additions.
5. **Freeze:** when LF-028 closes, this ADR moves to Accepted and freezes the chosen
   encoder, exact tokenizer revision, special-token set (possibly empty), max lengths
   per representation, and truncation policy. After the freeze, any change reopens
   this ADR and invalidates non-smoke runs made under the old freeze.

## Consequences

1. No non-smoke training may start before the audit in item 3 exists and this ADR is
   Accepted (PLAN.md sections 13.7 and 21.2). Smoke runs may use the default candidate
   with stock tokenizer settings.
2. The audit consumes only extracted pilot strata; it does not require external
   provider access or the sealed test sets.
3. Representation ablations (section 13.8) inherit the frozen tokenizer settings;
   representation choice and encoder choice are decided separately, but truncation
   statistics from item 3 inform representation max lengths.
4. If ModernBERT-large loses the comparison, the replacement is one of the two named
   alternatives; choosing outside the candidate set requires superseding this ADR with
   a new comparison on the same fixed strata.
5. Encoder weights and tokenizer files are cached under the approved bulk storage
   location with their identities (model ID, revision, file hashes) recorded in
   manifests (ADR-0003).
