# Experimental mixed proxy corpus with deterministic composition

Status: complete, clean-code frozen, externally verified, and exact-replay verified.

This artifact extends the F1-corrected first-hop plus Kimi/Qwen proxy corpus
with the complete receipt-bound depth-two deterministic composition export.
It remains proxy supervision only: it contains no human-gold labels and is not
eligible for confirmatory model selection, calibration, evaluation, gate
credit, or release claims.

## Frozen artifact

- Producer revision: `974c476b317a834e07e1357d600087332872556a`
- Dataset ID: `experimental_mixed_supervision:886da05a36e8b2125ec63c2ff8b0888b3cea48a3f498bc5b6721d9f358f81f6d`
- Root: `/storage/milikic/leanfaith/experimental_mixed_supervision/firsthop_kimi_qwen_composition_974c476_v1`
- Manifest SHA-256: `616c1646e89ccb0357eaf5eba552c94a714ba767128b5a23f8abc1e2c6b5f8f3`
- Records SHA-256: `947acffee34254584f267387182fbddbf80df553201233f5e090dd3aa1a6bc6e`
- Split assignments SHA-256: `d4249410af593823bd84b36216bfcdb30a4fae60982d88780dab765740448f14`

## Counts

- 17,031 deduplicated pairs and 17,044 retained proxy signals
- 13,633 train, 1,604 validation, and 1,794 test records
- 4,538 same-claim and 12,493 not-same-claim proxy targets
- 10,336 deterministic first-hop signals
- 5,534 deterministic composition signals: 681 positive-to-positive and
  4,853 positive-to-negative
- 1,174 completed single-Codex-judge signals
- 6,469 ancestry-connected split components
- 3,129 exclusions: 3,123 reversible composition cycles, three benchmark
  overlaps, one missing headless normalization, and two expert-review cases

The composition adapter verified all 8,661 exported unique pairs and admitted
5,534. Four exact composition pairs merged with records already present in the
other partitions, so the final basis contains 5,530 composition-only records
and three records with agreeing mixed proxy evidence.

## Verification

The clean producer run completed from a detached worktree at the recorded
revision. A second complete run re-read and re-hashed every bound input,
recomputed admission, deduplication, ancestry components, and splits, and
returned `replayed=true` with the same dataset ID and all output bytes
unchanged. Focused dataset, orchestration, scalar portability, import, and
configuration tests passed, together with Ruff, formatting, and strict mypy.

This corpus is large enough for the first token-encoder proxy training run. It
does not replace the later human-gold partitions required for scientific
calibration and final evaluation.
