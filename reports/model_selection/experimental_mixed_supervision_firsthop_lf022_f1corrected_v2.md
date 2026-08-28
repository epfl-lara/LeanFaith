# Experimental mixed supervision: F1-corrected first-hop + LF-022

**Frozen:** 2026-08-12

**Code revision:** `75ed50ea8bc032928241f1bd23384e67f34d51f2`

**Dataset ID:**
`experimental_mixed_supervision:f3e41e400587e493904985737325ba683d51e51be88ba718ba790142a26add77`

This is the corrected successor to the first combined proxy corpus. The
original v1 artifact is preserved unchanged. Its adapter incorrectly used
truth-level directional implication opinions (F2) to veto claim-faithfulness
judgments (F1). Closed propositions can be mutually provable for reasons that
do not make them the same mathematical claim, so the corrected adapter checks
only the internally coherent F1 verdict/relation pair.

The corpus remains **proxy supervision** for smoke training, learning curves,
and diagnostics. It creates no semantic, silver, or human-gold labels and is
not eligible for confirmatory model selection, calibration, or evaluation.

## Frozen inventory

| Measure | Count |
|---|---:|
| Retained records | 11,501 |
| Ancestry-connected components | 6,387 |
| Train | 9,313 |
| Validation | 1,075 |
| Test | 1,113 |
| Same-claim proxy | 3,857 |
| Not-same-claim proxy | 7,644 |
| Deterministic first-hop signals | 10,336 |
| Retained Codex single-judge signals | 1,174 |
| Adapter exclusions | 2 |

The 1,174 LLM-derived signals contain 200 Kimi-proposed and 974
Qwen-proposed pairs. Their judged F1 relation distribution is 25 equivalent,
143 A-stronger, 466 B-stronger, 539 incomparable, and one unrelated. The two
excluded judgments both explicitly require expert review.

The source mix is 7,000 internal-only `sft_classic` records and 4,501 public
mathlib records. Private-source restrictions remain attached to every
applicable record. Exact-pair deduplication, proxy-target conflict quarantine,
active-benchmark screening, and connected-component split assignment ran
before freezing.

## Exact correction delta

Relative to v1:

- exactly 664 exact pairs and 664 signals were added;
- no exact pair or signal was removed;
- no retained v1 target or signal changed;
- all 664 recovered pairs are not-same-claim proxy judgments;
- recovered relations are 521 incomparable, 128 A-stronger, 14 B-stronger,
  and one unrelated;
- recovered proposer counts are 554 Qwen and 110 Kimi;
- `codex_incoherent` exclusions fell from 664 to zero;
- the two expert-review exclusions remain.

Connected components were recomputed over the enlarged graph. This
legitimately changed the component identity of 111 old exact pairs and the
split bucket of 34; it does not represent target drift or leakage.

## Immutable artifacts

Root:
`/storage/milikic/leanfaith/experimental_mixed_supervision/firsthop_kimi_qwen_75ed50e_f1corrected_v2`

| Artifact | SHA-256 |
|---|---|
| `manifest.json` | `4e4c4fd54ff1419561d904e536c75e632657979b119b9432a8bfdc6e8c361d7f` |
| `summary.json` | `2d3bbff51794be8bdecc4e2fa09c0dccb5c233d42dbe7147e816b52363cdfa8b` |
| `records.jsonl` | `02f58d4449b57ae11ed36e31ab7385ec45747d862a5b6c180175ef039ea8fa9b` |
| `excluded.jsonl` | `eb2945ad2a6ef46f682946d4dc535c3619e9b8fb7c67aec5575c651f29182acb` |
| `split_assignments.jsonl` | `cb1786655fe67c05380bef569f55aa10f94efc38a03fa07ebac12b4b0b04d625` |

The freeze ran from a clean detached worktree at the stated revision with an
explicit source-path override. Independent verification re-read all external
inputs. A second identical freeze returned `replayed=true`, and a second
external-input verification passed with the same bytes and dataset ID.

## Next admission boundary

Second-hop deterministic compositions will enter a later corpus only after all
thirteen roots finish and the receipt-bound postprocessor removes alpha cycles,
deduplicates globally, and quarantines intention conflicts. Newly completed
Qwen generations likewise require frozen reconciliation, Lean checking, and
judgment verification before admission.
