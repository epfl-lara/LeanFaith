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
| latest frozen Kimi/Qwen generated candidates, gross | 1,916 | 248 Kimi plus the corrected 1,668-item Qwen aef924 snapshot; excludes the active resumable generation jobs; cross-run dedup not established |
| latest frozen Kimi/Qwen Lean-valid candidates, gross | 1,390 | 201 Kimi plus 1,189 Qwen; every Qwen item is bound to the corrected immutable LeanInteract check |
| single-Sol proxy judgments in current corpus | 1,324 | one-family proxy supervision, not silver or gold |
| exhaustive historical Sol/xhigh exclusion universe | 1,510 | unique pair IDs across five registered corpora and 1,339 source-theorem lineages; one registered Qwen corpus is a redundant preserved subset |
| new Terra proposer variants | 64 | 53 Lean-valid with a placeholder and 11 invalid; exact replay reused all 64 checks |
| exact pairs in the legacy Sol+Fable route queue | 919 | 201 Kimi plus 718 Qwen; useful for route replay, but not a fresh scientific judge pool |
| pairs in the frozen queue never historically judged by Sol | 0 | corrected audit: a separately stored 718-pair Qwen Sol corpus covers the apparent 409-pair remainder |
| completed Sol+Fable route smokes with historical overlap | 1 | valid four-cell route smoke, but its pair was historically seen by Sol |
| completed genuinely fresh Sol+Fable adjudications | 17 | one initial pair plus a disjoint 16-pair scale batch; all 68 required cells parsed on first attempt with zero provider/parse/infra failures |
| promoted model-adjudicated training-silver records | 9 | exact immutable replay: initial 1/1 plus scale 8/16; barred from selection, calibration, evaluation, human-gold, trusted F2, and Gate-6 credit |
| retained scale-batch promotion rejections | 8 | six semantic inconsistencies; five below the 0.90 confidence floor, with overlapping reasons; every scheduled pair has one promote/reject row |
| active Kimi/Qwen proposer expansion | in progress | restartable long-running jobs; no volatile live count is promoted into this ledger before immutable snapshotting and Lean checking |
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

## Shortest candidate-silver path

The content-preserving Sol+Fable design is frozen at:

```text
/storage/milikic/leanfaith/lf022_judge_design/sol_fable_public_v4/
  e81d93c752d232da8847eb97db611dc0e31eaee0e4304de418ee5cfe21f9eb6a
```

It binds 919 unique public pairs to GPT-5.6 Sol at `xhigh` and Claude Fable 5
at `max`, with DeepSeek held out. This original queue is now route-replay
evidence rather than a fresh judging pool: all 919 pair IDs are covered by the
exhaustive historical registry.

The canonical exclusion registry is:

```text
configs/generation/lf022_historical_sol_xhigh_registry_v1.json
```

It registers five completed Sol/xhigh corpora and expands to 1,510 unique pair
IDs across 1,339 source-theorem lineages. The preserved 718-pair Qwen corpus is
a redundant subset of the cumulative 975-pair Qwen corpus, but remains
registered so every historical artifact is explicit. Future selection must
exclude the union of both pair IDs and source-theorem lineages. It must also
scan finalized Sol/Fable batches and exclude any pair or lineage already
completed there.

The corrected execution order is:

1. one pair in AB and BA order for both families (four calls) -- **completed**;
2. collect new Kimi/Qwen/GLM proposer outputs created after the complete
   Sol-history freeze -- the resumable Qwen and Kimi jobs are currently in
   progress, but their live counts are deliberately not treated as frozen
   data;
3. Lean-check and freeze genuinely fresh candidates after excluding historical
   pair IDs, historical theorem lineages, and completed Sol/Fable batches;
4. run one genuinely fresh four-cell route check, then freeze a 16-pair
   theorem-lineage-diverse slice (64 calls) -- the fresh one-pair check has
   completed and the 16-pair batch has also completed;
5. retain only per-family order-consistent, cross-family agreements as
   model-adjudicated candidate silver;
6. route disagreements, ambiguity, and uncertainty to `REVIEW`;
7. apply the separately registered promotion policy before any candidate may
   become training-eligible silver;
8. scale through bounded resumable chunks over the new clean inventory.

The historical-overlap smoke produced a non-trainable `candidate_consensus`: it is a
route-level example of high-quality model-adjudicated candidate silver, not an
automatic promotion. After the registered promotion audit, accepted fresh
outputs may become training-eligible silver. Sol/Fable agreement never makes a
record human gold. Scientific calibration and final evaluation still require
a genuinely human-labeled sealed panel.

The completed smoke artifact is:

```text
/storage/milikic/leanfaith/lf022_weak_batches/
  sol_fable_live_smoke_qwen_n1_v3/batch/final/finalization_manifest.json
```

It contains four parsed judgment-evidence records and one order-consistent,
cross-family `candidate_consensus`. The finalization ID is
`lf022_weak_finalization:4aa122f248672be580f4a3cebeaa4b4cff7d2ca4490dcf6fdea7d816a8608fdb`.
It is an engineering route smoke only: the exhaustive five-corpus audit found
the pair in the historical Qwen Sol/xhigh evidence that the v3 authoring
exclusion union had omitted.

The corrected, genuinely fresh path is now active. The 1,668-item Qwen aef924
snapshot was checked through LeanInteract: 1,189 candidates elaborate with a
placeholder and 479 are invalid, with no infrastructure failures. The exact
1,189-item judge inventory excludes all five registered historical Sol corpora
by pair ID, theorem lineage, and canonical judge-visible payload. Its immutable
artifact is:

```text
/storage/milikic/leanfaith/lf022_supervision_candidates/
  qwen3_5_aef924_direct_v3
```

The first fresh selected pair completed four first-attempt judgments. Sol at
`xhigh` and Fable at `max`, each in AB and BA order, independently agreed on
`not_same_claim` and canonical `A_stronger`; the minimum self-reported
confidence was 0.96 and no source judgment requested review. Its finalization
ID is
`lf022_weak_finalization:d0ecd881c2278ac90f5c0812191a34aac8131c991455ed46a897bf9c1813084b`.
The registered promotion verifier replayed it twice and emitted one
training-only record with zero rejections under manifest
`model_silver_manifest:493e6460a04f01657869f18a0238457a059cb1f511014e73f3490ef0ada28735`.
See `reports/generation/lf022_sol_fable_fresh_qwen_aef924_n1_v4.md`.

A disjoint 16-pair scale batch completed under batch ID
`lf022_weak_batch:12d662a1fe4d61c1171553c271f9ca98610f1ea742531ebf38f7a846d20e728b`.
It contains 16 unique source theorem lineages and 64 required cells. Sol/xhigh
and Fable/max each completed 32/32 parsed cells with exactly 32 attempts, zero
retries, and no provider, parser, infrastructure, or private-source failure.
Offline replay produced 10 candidate consensuses, five swapped-order
inconsistencies, and one cross-family disagreement. The stricter promotion
policy admitted eight records (six `B_stronger`, one `A_stronger`, one
`incomparable`) and retained eight explicit rejections; the two remaining raw
consensuses failed the 0.90 confidence floor. The immutable promotion manifest
is
`model_silver_manifest:9a7202bdc59cfcdcbab0fe0d21fde624c3d9ffb8cc540c9561b6e4beb547903c`.

These nine records satisfy the record-level promotion policy, but Gate 6M has
not yet been formally closed. They therefore demonstrate the bounded
training-only route; a scientific weak-training run remains unauthorized
until its frozen Gate-6M denominator and closure report exist.

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
