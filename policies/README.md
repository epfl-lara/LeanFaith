# Policy authority for the value-first program

The active plan is `../PLAN.md`, with operational data/label contracts in
`../plans/00_shared_contracts.md`.

Active additive policies:

- `source_use_v2.yaml` — owner-authorized research and external-model use of
  `formalmathatepfl/*`, with private-first outputs.
- `evaluation_use_v2.yaml` — evaluation-only external-judge use of the canonical 5,111 pairs.

Files ending in `_v1` and policies/configs referenced by archived plans remain immutable authority
for reproducing their historical runs. Their sealed-test, promotion-gate, private-source, split,
preregistration, and model-selection rules do not silently govern the new value-first tasks. New
tasks add a v2 config/policy rather than weakening a v1 file. Semantic decisions in
`semantic_policy_v1.md` remain useful precedent, but active SFT2/evaluation prompts pin the rubric
in `plans/00_shared_contracts.md` until a versioned successor is approved.
