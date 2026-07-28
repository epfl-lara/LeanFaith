# Weak-judge calibration report

**Status:** not run; calibration protocol foundation only

**Date:** 2026-07-28

**Applies to:** Phase 6 LF-022 weak-supervision judges

## Result

There is no judge-calibration result yet.

| Quantity | Current value |
|---|---:|
| Assigned weak-judge families | 0 of 2 |
| Assigned held-out primary evaluation families | 0 of 1 |
| Live calibration calls | 0 |
| Live A/B and B/A judgment pairs | 0 |
| Human-labeled calibration items scored | 0 |
| Trap items scored | 0 |
| Cross-family agreements observed | 0 |
| Swap-consistent agreements observed | 0 |
| Semantic labels created | 0 |
| Silver records promoted | 0 |

Therefore all empirical quantities—including swap agreement, cross-family
agreement, classwise accuracy, abstention rate, calibration error, and
promotion precision—are **undefined**, not zero-valued performance estimates.
Gate 6G and Gate 6 remain open.

## Operational smoke is not calibration

A separate public-source smoke used Qwen3.5 and GLM 5.2 as the two weak
families, with Kimi as proposer and the primary evaluation family held out.
Both A/B orientations parsed for both judges, the four evidence records
replayed, and the candidate-only aggregate remained smoke-quarantined.

That single item is transport, schema, remapping, persistence, and replay
evidence only. It is not drawn from a human-labeled calibration sample and
therefore does not change any quantity in the table above.

## Implemented protocol safeguards

The current foundation enforces:

- a strict blinded judge-response schema;
- no proposer family, generation intention, gold/silver label, other-judge
  vote, or symbolic evidence in the default visible prompt;
- distinct A/B and B/A copies;
- deterministic remapping of stronger/weaker relations and directional
  implications to canonical order;
- randomized dispatch order bound to a persisted key hash;
- distinct proposer, judge A, judge B, and held-out primary evaluation
  families for confirmatory use;
- retention of uncertainty, ambiguity, disagreement, all-abstain, and swap
  inconsistency;
- per-call judgment evidence rather than a direct resolved label; and
- a candidate-only aggregate that is schema-barred from training,
  evaluation, and silver promotion.

The canonical judge configuration is
`configs/judges/weak_supervision.yaml`. It deliberately leaves
`judge_A_family`, `judge_B_family`, and `primary_eval_judge_family` unset and
sets `live_calls_authorized: false`.

## Calibration set boundary

Judge calibration must use a frozen, human-labeled pilot/calibration set whose
ancestry and NL-problem groups do not cross into training or final evaluation.
LLM agreement cannot create that set. Gate 5's 240-item prevalence frame is
not currently usable because it still has zero human terminal labels.

The calibration design must bind, before live judging:

- item IDs and human-label version;
- label-source and ambiguity policy;
- proposer and judge family identities;
- the primary evaluation family held out from all supervision;
- prompt and parser hashes;
- exact model revisions and decoding settings;
- trap-item strata;
- A/B and B/A task identities;
- aggregation and abstention rules; and
- all reported metric implementations.

No benchmark or final-human-test item may be used for prompt selection,
family selection, threshold setting, or promotion calibration.

## Metrics to compute after admission

After genuine human labels and admitted live judgments exist, report at least:

- parse and terminal-outcome rates;
- per-family and joint coverage;
- A/B versus B/A agreement after directional remapping;
- cross-family semantic agreement;
- raw and classwise agreement with humans;
- ambiguity and uncertainty routing;
- confidence calibration descriptively;
- disagreement and trap-item performance; and
- counts by proposer, judge family, source, relation, and generation stratum.

The plan's Gate 6 swapped-order threshold is at least 90%. Passing that
threshold alone does not authorize silver promotion. Promotion also requires
the human pilot, capped stratified audit, and registered promotion policy.

## Next admissible action

The one-item operational path is complete. The next admissible action is not
bulk judging: it is to obtain the required genuine human pilot labels, audit
the guidelines/agreement outcomes, and only then freeze a separately
versioned production collection and judge-calibration admission. Until then,
no calibration, silver, or supervision claim is made.
