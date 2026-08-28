# LF-022 Qwen snapshot-1019 direct supervision candidates (schema v3)

Date: 2026-08-11

This is the first scale materialization of the source-neutral LF-022
supervision-candidate inventory from the frozen Qwen post-generation snapshot.
It projects directly from exact Lean checks and does not bind or require a
Codex audit.

## Frozen inputs

- Spec:
  `configs/generation/lf022_supervision_candidates_qwen_snapshot1019_v3.json`
- Spec SHA-256:
  `0b623bb81e68e83c217555b731b82758281f19e2c4dbdfd8214ceadae891e06a`
- Post-generation selector ID:
  `lf022_postgen_terminal_selector:6472682bd2de081e9b007d82eb374556b2fb95ec29f0ea2c39ab2f42ca1582ee`
- Selected terminal count: 1,046
- Lean checks SHA-256:
  `b53736c22fb7cc134081e33a9a4f7e5e1724d0e3f170b6930b05658ef0a2d6c4`
- Lean-check manifest SHA-256:
  `f106d1a299c89eca5b5c82df562bb6522b42ca1de79b9d2caa91dd1a042d65e9`
- Checked provisional variants: 1,019
- Output root:
  `/storage/milikic/leanfaith/lf022_supervision_candidates/qwen3_5_snapshot1019_direct_v3`

The selected historical-terminal verifier hash-checks and canonical-parses the
complete 9,207-task batch envelope, its freeze request, every route admission,
all task-binding identities, and the selector journal. It opens only the 1,046
selected task bodies. For every selected terminal it still reconstructs the
exact prompt/preflight and replays the persisted attempt, provider request,
wire request and response, provider-raw response, generic LLM lineage, parsed
output, provisional variants, and terminal record.

This boundary verifies an already-trusted frozen selector with current replay
code. It deliberately does not repeat the exhaustive public-pool,
authorization, denylist, and source-eligibility audit for every unselected
batch task. The original exhaustive verifier remains available.

## Result

- Inventory ID:
  `lf022_supervision_inventory:09cd971c0158447ab7c1dd2ef77f56d75576568e2caddf36f7c67c4fbb48ec0a`
- Declaration-verified Lean-valid candidate records: 718
- Unique judge-visible payloads: 718
- Dispatch-eligible unresolved pairs: 718
- Future two-family swapped-order calls: 2,872
- Bound Codex diagnostics: 0
- Weak-supervision votes supplied by Codex: 0
- Semantic labels, silver records, training records, evaluation records, and
  gate credit: 0

Every record has schema v3, state
`unresolved_awaiting_two_family_judging`, and false
training/evaluation/gate capabilities. Running the official builder twice
reproduced the same inventory ID and immutable outputs.

Together with the earlier Kimi prefix-256 inventory, LeanFaith now has 919
inspectable, Lean-valid, unresolved judge-ready pairs requiring 3,676 future
judge calls. They are candidate data, not resolved F1 supervision.

## Scale measurement

The first build completed in 16.37 seconds with a maximum resident set size of
159,672 KiB and no swap activity. The previous exhaustive implementation had
materialized all batch source/task objects and exceeded 10 GiB on this
snapshot; the selected-only replay removes that scaling blocker without
weakening selected-terminal lineage verification.

## Output hashes

- `candidates.jsonl`:
  `2f9b1cd518bcd8769976eec8c4717ef21e16a3165966f30ef54df376faef2978`
- `public_sample.jsonl`:
  `a713dce68f2f4cf512c8950b7d02288f9bbb88da1fb1d13064410e979bdc1b03`
- `summary.md`:
  `01c91038ccbe5b2154c95e5354a42133edd8ad7c22fd642fec0e2c4bb36c7b7f`
- `manifest.json`:
  `75fd6c5046a4f63abf8603337bea4c2c6e277c7f827a57f9ed6f04463531399f`

## Reproduction command

```bash
.venv/bin/leanfaith build-lf022-supervision-candidates \
  --root /localhome/milikic/LeanFaith-rcp-5e672b9 \
  --spec configs/generation/lf022_supervision_candidates_qwen_snapshot1019_v3.json \
  --spec-sha256 0b623bb81e68e83c217555b731b82758281f19e2c4dbdfd8214ceadae891e06a \
  --output-dir /storage/milikic/leanfaith/lf022_supervision_candidates/qwen3_5_snapshot1019_direct_v3
```

This artifact is a judging queue, not a labeled dataset.
