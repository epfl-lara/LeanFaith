# LF-022 Kimi prefix-256 direct supervision candidates (schema v3)

Date: 2026-08-11

This is the first real, non-mocked materialization of the source-neutral
LF-022 supervision-candidate inventory. It projects directly from the exact
Kimi prefix-256 Lean checks and does not bind or require a Codex audit.

## Frozen inputs

- Spec: `configs/generation/lf022_supervision_candidates_kimi_prefix256_v3.json`
- Spec SHA-256: `e8ec0cb0f728b4355075562341300abe74137827943c8e64ea37420336eae9ab`
- Lean checks SHA-256: `46972e934b26e9ee6df112a6e135223f83267b58e93ccde2be79e40d6ed54810`
- Lean-check manifest SHA-256: `8805cec56a157ce955354b63d54e9714948856e437ead076f59b3295656b253d`
- Output root: `/storage/milikic/leanfaith/lf022_supervision_candidates/kimi_prefix256_direct_v3`

The selector replay hash-checks and canonical-parses the public batch manifest,
its frozen request, every route admission, and every bound frozen task before
replaying the selected terminals and provisional-variant lines. It does not
require the historical upstream eligibility-selection implementation to remain
byte-identical. Each accepted variant is also rebound to its adjacent canonical
execution task, proposer, source theorem, representation, context, imports, and
exact Lean-check record; any frozen-task or adjacent-task drift fails closed.

## Result

- Inventory ID: `lf022_supervision_inventory:00eea42e3faca301c707cab236ed7cf75592148c8f78ab2ea03b62272ffba811`
- Checked variants in the frozen input: 248
- Declaration-verified Lean-valid candidate records: 201
- Unique judge-visible payloads: 201
- Dispatch-eligible unresolved pairs: 201
- Future two-family swapped-order calls: 804
- Bound Codex diagnostics: 0
- Weak-supervision votes supplied by Codex: 0
- Semantic labels, silver records, training records, evaluation records, and gate credit: 0

Every record has schema v3, state
`unresolved_awaiting_two_family_judging`, and false training/evaluation/gate
capabilities. Running the official builder twice reproduced the same immutable
artifacts.

## Output hashes

- `candidates.jsonl`: `358a78e25583158d5d1e87e01c6e03e17b20bdf814bb16b63aa3484df86d1372`
- `public_sample.jsonl`: `d1647cc968b8acc3b9ee7b137d69e4c6197071d58df7b98176249102a0cc23fb`
- `summary.md`: `2085ba285aae96fe7ac5b173da3fc88c552de95e09108a0cfad4eb81773f15c9`
- `manifest.json`: `647648a7e010e817ee954e4897511fa5bcd68c03dea3f12b62d978538db578dc`

## Reproduction command

```bash
.venv/bin/leanfaith build-lf022-supervision-candidates \
  --root /localhome/milikic/LeanFaith \
  --spec configs/generation/lf022_supervision_candidates_kimi_prefix256_v3.json \
  --spec-sha256 e8ec0cb0f728b4355075562341300abe74137827943c8e64ea37420336eae9ab \
  --output-dir /storage/milikic/leanfaith/lf022_supervision_candidates/kimi_prefix256_direct_v3
```

This artifact is a judging queue, not a labeled dataset.
