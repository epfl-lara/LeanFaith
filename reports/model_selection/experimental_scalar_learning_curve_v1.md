# Experimental scalar pseudo-target learning curve (v1)

**Status:** `PASS_EXPERIMENTAL_DIAGNOSTIC`  
**Producer commit:** `5348ffb46b5af049655e8e52e1b60590cdeae3d0`  
**Experiment:** `experimental-scalar-curve:02386e50c4255cdf5d2b8b2984937830b69bc774cc1af2a2620776906017b0de`

## Result

The first frozen LeanFaith learning curve completed, passed standalone
verification, and reproduced byte-for-byte on an exact rerun. It fitted 18
deterministic scalar models over six ancestry-component budgets and three fixed
component-order seeds, producing 6,876 predictions and 36 metric records.

At the full 1,260-component / 1,618-record training budget:

| Split | Pseudo-AUPRC | Constant-score AUPRC | Balanced accuracy | Brier |
|---|---:|---:|---:|---:|
| Validation | 0.815420 | 0.497409 | 0.798915 | 0.135810 |
| Test | 0.806439 | 0.497354 | 0.810358 | 0.131947 |

The descriptive curve rises sharply between 64 and 256 components and then
largely plateaus:

| Components | Validation mean AUPRC | Test mean AUPRC |
|---:|---:|---:|
| 64 | 0.698994 | 0.713814 |
| 128 | 0.786467 | 0.792871 |
| 256 | 0.804331 | 0.808115 |
| 512 | 0.811863 | 0.812649 |
| 1,024 | 0.809309 | 0.807634 |
| 1,260 | 0.815420 | 0.806439 |

The three full-budget entries are the same training set, not independent
replicates. Smaller-budget entries vary only by which complete ancestry
components enter the deterministic prefix.

## Interpretation

This establishes one useful and one cautionary fact:

1. The frozen pair format, ancestry-safe splits, feature extraction, training,
   scoring, immutable publication, and exact verifier work end to end.
2. Very small surface/structural features can predict the deterministic
   transformation intentions. This is **not** evidence that they recognize
   mathematical faithfulness; it demonstrates why later evaluation must hold
   out transformation families and include structurally controlled real errors.

The result is therefore a shortcut/learnability diagnostic. It is not M0, does
not select a backbone or checkpoint, does not fit calibration thresholds, and
cannot enter scientific evaluation or release claims.

## Bound artifacts

Artifact root:
`/storage/milikic/leanfaith/model_diagnostics/experimental_scalar_learning_curve_5348ffb_v1`

| Artifact | SHA-256 |
|---|---|
| `manifest.json` | `bc82628147d6360e1e5488fa0a5a64f670a816ad2a513ece686f9b8b2e43f429` |
| `models.jsonl` | `40534ba3b7683f691a19a55b6921cd049405200fa94ccfcd77edae00bd4281fd` |
| `predictions.jsonl` | `82424cd1769991a47b3df50764266c1cb2c46bac512fe02938a4225990c98b54` |
| `metrics.jsonl` | `6985de2cc8cf2243794edc946859895c749b7046bab996cc1ba6f6a9604140f5` |
| `summary.json` | `4c8ed9afe1b9c3728bc5f33efaf9f19982d505e2b86386a1f30ad4d6f024ca83` |
| `summary.md` | `32bece3dd667ee5f2994824a602e6429a1f3592b4da26c8706da0552a6b4f440` |

The dataset manifest is bound at SHA-256
`c35079c7c7cc6421844a0c5eb1ee30684ca6faf23d71e1f136b18238477fda1d`.
The producer worktree remains pinned at the producer commit so the standalone
verifier continues to reproduce the exact deterministic fit after later report
commits.

## Verification performed

- full repository test suite;
- Ruff lint and formatting;
- strict mypy over the complete source tree;
- LeanFaith environment doctor;
- Lean fixture `lake build`;
- first immutable artifact publication;
- standalone verification using only the manifest-bound dataset path;
- exact second run with `replayed=true`;
- adversarial tests for self-consistent model tampering, symlinks, unrelated
  repository provenance, output overlap, and concurrent publication.

## Next use

Keep this scalar model as a fixed shortcut baseline. Expand the deterministic
corpus, reconcile the Lean-valid LLM variants, and require future learned models
to improve on held-out transformation, anti-shortcut, and real-output slices
rather than merely improve this pseudo-target score.
