# Corrected mixed-proxy scalar learning curve

Date: 2026-08-12

This is an **experimental proxy diagnostic**, not a semantic-faithfulness result.
It trains on machine-generated transformation intentions and single-judge proxy
targets. It is ineligible for model selection, calibration, evaluation, gate
credit, release claims, or comparison with FormalRx.

## Frozen inputs and execution

- Dataset ID: `experimental_mixed_supervision:f3e41e400587e493904985737325ba683d51e51be88ba718ba790142a26add77`
- Dataset records: 11,501 total; 9,313 train; 1,075 validation; 1,113 test
- Producer revision: `cd8dd76b7edba0da80e60cec37131a8bb667225d`
- Experiment ID: `experimental-mixed-scalar-curve:0b569456111516e0480c6c6233f4acffacd6400da338d8b0ea5e09ca4a0ec264`
- Artifact root: `/storage/milikic/leanfaith/experimental_mixed_scalar_learning_curve/mixed_v2_cd8dd76`
- Manifest SHA-256: `98927bc2fcc51c03aae30a460806e78e9e22f1c34a73a4e14f3e786e5d47b8d7`
- Summary SHA-256: `a9a17d2107410b47648313b32c17769770ba9b5f5c242a92dc75df4031f33ba7`

The run fitted nine deterministic scalar models: exact ancestry-component-atomic
prefixes of 2,000, 5,000, and all 9,313 training records under three component
ordering seeds. Validation and test records never entered training. Features are
swap-invariant lexical summaries of the `headless` Lean statements; the model is
deliberately much weaker than the planned M0--M3 encoders.

The standalone verifier refitted every model and passed. A second complete run
returned `replayed=true` against the immutable artifact.

## Results

| Training records | Test pseudo-AUPRC range | Validation pseudo-AUPRC range | Test balanced-accuracy range |
|---:|---:|---:|---:|
| 2,000 | 0.567836--0.574852 | 0.548444--0.552833 | 0.800076--0.806878 |
| 5,000 | 0.573996--0.574837 | 0.552793--0.554861 | 0.804157--0.806878 |
| 9,313 | 0.574008 | 0.552862 | 0.804837 |

The constant-score baselines are 0.339623 test AUPRC and 0.324651 validation
AUPRC. Full-corpus Brier scores are 0.147802 on test and 0.147536 on validation.

## Interpretation

The mixed corpus contains a learnable signal: even a small symmetric lexical
model substantially exceeds the constant baseline. The learning curve is nearly
flat after 5,000 examples, however. This does **not** show that more data are
useless. It shows that this scalar feature set has saturated and motivates the
planned richer token encoders, hard composed transformations, and real-output
examples. Because the targets are machine proxies, none of these numbers estimate
true F1 same-claim accuracy.
