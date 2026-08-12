# Current data ledger

Date: 2026-08-12

This ledger separates prepared statements, mechanically checked pairs, proxy
targets, model-adjudicated targets, and scientific labels. Counts from
different rows must not be added unless the row explicitly says that the
records are disjoint.

| Layer | Verified count | Current meaning |
|---|---:|---|
| public mathlib statements prepared for broad transforms | 27,786 | public source universe |
| private statements with eligible representations | 18,668 | prepared private universe; broad transform coverage is not yet complete |
| first-hop deterministic proxy pairs admitted to the mixed corpus | 10,336 | 3,840 positive-intention E2 and 6,496 negative-intention D0 machine targets |
| depth-two deterministic composition signals admitted | 5,534 | noncyclic machine targets after provenance/dedup checks |
| depth-three deterministic pairs audited | 4,031 | corrected v2: 613 equivalent-intention and 3,418 near-miss-intention provisional pairs; intention-only, not training labels |
| public unary content-audited pairs | 27,327 | lower-trust provisional pairs only; strict merge is blocked |
| latest Kimi/Qwen generated candidates, gross | 1,818 | 248 Kimi plus 1,570 Qwen; cross-run dedup not established |
| latest Kimi/Qwen Lean-valid candidates, gross | 1,326 | 201 Kimi plus 1,125 Qwen |
| single-Sol proxy judgments in current corpus | 1,324 | one-family proxy supervision, not silver or gold |
| new Terra proposer variants | 64 | 53 Lean-valid with a placeholder and 11 invalid; exact replay reused all 64 checks |
| exact pairs frozen for Sol+Fable judging | 919 | 201 Kimi plus 718 Qwen, public and unresolved |
| completed Sol+Fable dual-family adjudications | 0 | first one-pair smoke is the next label milestone |
| mixed proxy corpus | 17,181 | unique machine-supervised pairs for engineering diagnostics |
| scientifically admissible F1 labels | 0 | no calibration/evaluation claim is authorized |
| genuine human-gold labels | 0 | four required gold products remain absent |

## Verified proxy corpus

The exact-replay mixed artifact is:

```text
/storage/milikic/leanfaith/experimental_mixed_supervision/
  firsthop_kimi_qwen1125_composition_f7b398af_v1
```

It contains 17,181 unique pairs and 17,194 signals across 6,599
ancestry-connected components. The signals are 10,336 deterministic first-hop,
5,534 deterministic depth-two, and 1,324 single-judge proxy signals. It is
eligible for engineering smoke training and proxy diagnostics only.

## Unary replay correction

All 16 public unary producer shards completed, and the lower-trust content
audit materialized 27,327 provisional pairs. The stricter merge did **not**
complete: its last exact-producer attempt failed because scratch Lean replay
differed from the producer scientific manifest. No strict-admitted unary count
exists, and no strict replay is currently running. The producer roots are
preserved for an explicit migration/replay rather than silently admitted.

## Shortest label-producing path

The content-preserving Sol+Fable design is frozen at:

```text
/storage/milikic/leanfaith/lf022_judge_design/sol_fable_public_v4/
  e81d93c752d232da8847eb97db611dc0e31eaee0e4304de418ee5cfe21f9eb6a
```

It binds 919 unique public pairs to GPT-5.6 Sol at `xhigh` and Claude Fable 5
at `max`, with DeepSeek held out. The execution order is:

1. one pair in AB and BA order for both families (four calls);
2. a frozen 16-pair ancestry-diverse slice (64 calls);
3. admit only per-family order-consistent, cross-family agreements as
   dual-family consensus silver;
4. route disagreements, ambiguity, and uncertainty to `REVIEW`;
5. scale through bounded resumable chunks over the 919-pair inventory.

These outputs are high-quality model-adjudicated silver, not human gold.
Scientific calibration and final evaluation still require a genuinely
human-labeled sealed panel.

## Terra proposer check

The bounded GPT-5.6 Terra proposer tranche produced 64/64 provisional variants.
Its separate LeanInteract artifact checked all 64 candidates: 53 elaborate
with a proof placeholder and 11 are invalid. A second invocation made zero
Lean executions and reused all 64 persisted results with the same outcome
counts. The check manifest is:

```text
/storage/milikic/leanfaith/lf022_codex_scale_lean_checks/
  terra_9141ca0_prefix64_v1/manifest.json
```

The manifest SHA-256 is
`2c7652c63bec26b118cd92070f12143ebd5ae64c2adcfa455f84aca1bec9a54b`;
the bound `checks.jsonl` SHA-256 is
`1487a7eb775f0c711422dc7cc38f57abb2be7a86c125a0a872b18144b64ef0a9`.
This is a generation/typechecking milestone only: it creates no semantic
label, silver record, training eligibility, evaluation eligibility, or gate
credit.

## Deterministic depth-three expansion

The corrected five-family P14--P18 v2 audit completed over the exact
5,538-record depth-two frontier. It expanded 4,368 provisional results into
4,581 exact parent-path lineages, quarantined 464 returns to the depth-one
intermediate alpha, and admitted 4,117 noncyclic lineages. Source/final-alpha
deduplication produced 4,031 unique pairs, comprising 613
equivalent-candidate and 3,418 near-miss-candidate intentions. A second full
audit invocation replayed the same set ID and bytes. See
`reports/transformation_audits/deterministic_third_hop_five_family_v1.md`.

The earlier v1 count of 4,281 is superseded: its theorem-ID cycle check missed
the 464 intermediate-alpha returns. The 4,031 corrected records remain
intention-only provisional mechanical pairs, not semantic labels. They are not
included in the existing 17,181-pair proxy corpus and must not be added to that
corpus count until the explicit merge/split adapter is built and replayed.
