# Experimental mathlib 2,000-pair machine-supervision corpus

## Outcome

LeanFaith now has its first frozen, loader-ready corpus for an opt-in learning
curve. The build published exactly 2,000 public-mathlib Lean--Lean pairs:

- 1,000 `same_claim` pseudo-targets from E2 deterministic transformations with
  exact bound transformation receipts;
- 1,000 `not_same_claim` pseudo-targets from D0 typed semantic-delta
  transformations;
- 1,569 ancestry-connected components with at most four selected variants per
  component; and
- 1,618 train, 193 validation, and 189 test records.

The corpus is
`experimental-machine-supervision:a5851226b1c8b132880548fd2c084fed2a9118721009e3d17b4a600245be1033`.
Its manifest is
`/storage/milikic/leanfaith/experimental_machine_supervision/mathlib_2k_v1/manifest.json`
with SHA-256
`c35079c7c7cc6421844a0c5eb1ee30684ca6faf23d71e1f136b18238477fda1d`.
The readable 20-pair sample is
`/storage/milikic/leanfaith/experimental_machine_supervision/mathlib_2k_v1/public_sample.md`.

## Scientific boundary

These values are transformation intentions, not resolved F1 semantic labels.
Every record remains `quality_tier=provisional`, has no semantic-label ID, and
is ineligible for scientific training, model selection, calibration,
evaluation, or release claims. Loading requires the explicit
`--allow-experimental-machine-supervision` policy and is admitted only for
`smoke_training` or `learning_curve` purposes.

This boundary is deliberate: it permits the first useful model/data sanity
checks without misrepresenting deterministic intentions as expert gold.

## Selection and leakage controls

The builder replayed the exact 11,208-pair deterministic audit, required E2
positive-seed receipts, joined and rehashed the bound Lean results, verified
theorem/representation/context/ancestry lineage, and screened both sides
against the active benchmark registry. It excluded three benchmark overlaps,
four records missing required views, five candidate-code duplicates, and eight
alpha-identity duplicates before quota selection.

Connected components were computed by union-find across overlapping ancestry
groups. No component crosses a split. Model-visible input contains only
`headless` and `signature_explicit`; proof bodies are absent.

## Family counts

| Family | Count |
|---|---:|
| P14 independent binder permutation | 400 |
| P15 root `Iff` reversal | 169 |
| P16 conjunction reassociation | 1 |
| P18 root equality symmetry | 430 |
| N11 bound-variable substitution | 232 |
| N12 implication converse | 383 |
| N15 conjunct omission | 1 |
| N16 domain-guard removal | 1 |
| N18 root equality polarity | 383 |

The one-item P16/N15/N16 strata are retained for coverage visibility, not as
evidence that those mechanisms are adequately represented. Learning-curve
results must report the family imbalance and cannot make per-family claims for
those strata.

## Verification

- The producer was the clean, pushed commit
  `df4f1112f67386ccc6739fb03b3424d9088677c5`.
- The corpus verifier passed against all external lineage inputs.
- A second freeze under the identical code/config/input state returned
  `replayed=true` and reproduced the same dataset and bytes.
- Focused builder/denylist/lineage tests passed.
- The full Python test suite, Ruff, formatting, strict mypy, doctor, and Lean
  fixture checks passed for the producer worktree.

The machine-readable milestone record is
`reports/model_selection/experimental_machine_supervision_mathlib_2k_v1.json`.

## Next use

The immediate next model milestone is a small representation/label sanity
baseline and learning curve over this corpus. It must remain clearly named as
experimental machine supervision. Human-gold products and full D0--D5
confirmatory training remain separate future steps.
