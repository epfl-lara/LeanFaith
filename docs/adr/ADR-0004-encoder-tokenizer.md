# ADR-0004: Encoder and tokenizer pilot

**Status:** protocol_frozen_pending_execution
**Date:** 2026-07-14
**Source of truth:** PLAN.md Revision 4.1 §6.4, §13.7, and §21.2

## Context

LeanFaith needs a non-autoregressive encoder whose tokenizer preserves Lean
binders, hypotheses, conclusions, Unicode operators, and qualified constants.
No backbone is selected in advance and no hard parameter ceiling defines
"lightweight". The scientific decision is a preregistered quality/efficiency
pilot; ModernBERT-base is only a smoke fallback if the pilot cannot run.

## Frozen candidate registry

Pin exact model and tokenizer revisions before any pilot tokenization:

1. `answerdotai/ModernBERT-base`;
2. `answerdotai/ModernBERT-large`;
3. `Salesforce/codet5p-220m`, encoder branch only;
4. `microsoft/deberta-v3-large`.

The later execution amendment to this ADR records immutable revisions and the
winner. It may not change the selection rule after candidate predictions are
observed.

## Context-length eligibility

Audit the frozen Gate-3 10,000-theorem manifest with the exact pilot
representation bundle:

```text
[HEADLESS]
...
[SIGNATURE_EXPLICIT]
...
```

Use a deterministic section budget. Use 512 tokens only if every conclusion
and at least 99% of statements' complete binder, typeclass-binder, and
hypothesis sets are retained. Otherwise use 1,024. At 1,024, a model is
eligible only when its released architecture supports that length without a
positional-architecture modification. Preserve all excluded examples under a
`long_input` evaluation slice.

## Training protocol

- same 50,000 ancestry-disjoint pairs, or all if fewer;
- 50/50 positive-negative batches;
- equal semantic input content, effective batch size, and example exposure;
- AdamW;
- learning rates `{5e-6, 1e-5, 2e-5}`;
- weight decay `{0.01, 0.1}`;
- one tuning seed and three independent confirmation seeds per candidate.

`selection_gold` must contain at least 100 faithful and 100 unfaithful
ancestry/NL groups and at least 50 groups per relation class included in the
confirmatory relation metric.

## Deterministic winner rule

Use a hierarchical paired bootstrap over seeds and ancestry/NL groups with
simultaneous one-sided 95% confidence bounds over every candidate comparison.

1. Retain candidates whose AUPRC deficit from the empirical best has
   simultaneous upper bound ≤0.01.
2. From those, retain candidates whose relation-macro-F1 deficit from the
   empirical relation best has simultaneous upper bound ≤0.02.
3. Select the survivor with highest median cached-reference batch-32 pairs per
   second.
4. Throughput differences below 5% break ties by lower peak memory, fewer
   loaded parameters, then lexicographically smaller model ID.

Runtime uses one frozen environment and supported numeric precision, with 20
warmup and 100 timed batches. Report tokenization separately and end-to-end,
and report bootstrap intervals. If the selection sample lacks its required
group counts, the decision remains provisional rather than weakening the rule.

## Consequences

- No non-smoke model training begins before this protocol's inputs and exact
  candidate revisions are frozen.
- No test or calibration label enters the pilot.
- Special-token changes are separate preregistered ablations, not post-result
  fixes.
- The selected backbone, tokenizer, length, representation version, and pilot
  artifact hashes are appended here when LF-028 closes.
