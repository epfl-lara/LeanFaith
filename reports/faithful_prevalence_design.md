# LF-021 faithful-prevalence design v3

**Status:** frozen before semantic-label inspection  
**Policy:** `policies/lf021_prevalence_design_v3.yaml`  
**Estimator:** `lf021_prevalence_estimator_v2`  
**Gate effect:** Gate 5G is mechanically closed; this design does not close Gate 5

The verified post-exhaustion population contains 250 unique problem-aware
eligible units. Under the v3 policy, the production CSPRNG froze a 240-item,
31-stratum extended-v1 frame:

```text
lf021_extended_prevalence_frame_v1:c4d248631456c45a05a6c4d929cda6a69c6b91e05e5e65ed4a8ca8eff0367312
sha256 = a07b352030a2c51fa51ebcabc00a3c1d1ecf2041318feabfe57a4c70fc365069
```

Its exact 16-tranche production lineage is:

```text
lf021_gate5g_lineage:ddac5e106c92b263ed96c9974eeadcef25f4980f1e777b06bca75de133b0aa1d
sha256 = a2bb9dba960a7906057647162a6ba00e17f26d0aa89180940e9e6112138ca761
```

## Population and primary estimand

The primary finite-population unit is one unique
`(problem_group, alpha_identity_fingerprint)` claim in the verified
post-exhaustion extended population. The population contains benchmark-clear
compiling claims eligible for the extended-v1 frame under the v3 policy.
Alpha-identical outputs may share one human label only when they belong to the
same problem group. The design never merges the same Lean proposition across
distinct natural-language problems.

The primary categorical estimand is the finite-population vector:

```text
faithful, unfaithful, terminally ambiguous
```

The headline scalar is faithful prevalence among terminal non-ambiguous
claims. A required sensitivity treats terminal ambiguity as not faithful.
Three-way proportions and the ambiguity rate are always reported; ambiguity
is never silently coerced in the main estimate.

Within sampling stratum `h`, every selected claim has
`pi_h = n_h / N_h` and design weight `N_h / n_h`. Category totals use the
stratified Horvitz-Thompson estimator with the known population total.

## Primary confidence intervals

The primary 95% interval is a deterministic, design-based finite-population
interval. For each sampling stratum, the estimator inverts equal-tailed
hypergeometric tests for the unknown category total. A Bonferroni correction
simultaneously covers the lower and upper missingness assignments for all
three semantic categories in every stratum.

This exact method supports a sampled non-certainty stratum with `n_h = 1`;
such a stratum is not pooled after labels are seen. Certainty strata
(`n_h = N_h`) contribute their observed totals exactly.

## Secondary estimands

The frozen extended-v1 frame retains every cluster member and exact
multiplicities.
Two secondary estimands reuse the problem-aware claim label:

1. retained compiling invocation-weighted prevalence; and
2. retained compiling invocation-weighted prevalence separately for each
   scalable generator family.

Their point estimator is the stratified Hájek ratio:

```text
sum_i (label_i * retained_multiplicity_i / pi_i)
-------------------------------------------------
sum_i (retained_multiplicity_i / pi_i)
```

Per-family estimates replace total multiplicity with that family's retained
member count. Duplicate invocations therefore contribute to the invocation
estimand but never become independent human labels.

Secondary 95% intervals use stratified Taylor linearization and are
descriptive, pointwise intervals. If any non-certainty sampling stratum has
only one sampled claim, its within-stratum variance is not identified. The
secondary point estimate remains valid, but its interval is reported as
`unsupported_singleton_noncertainty_stratum`; the implementation does not
invent a variance, pool strata after seeing labels, or silently drop the
stratum.

## Ambiguity and nonresponse

Terminal expert ambiguity is a semantic outcome. Missing adjudication and
`resolution_outcome=unresolved` are workflow nonresponse, not ambiguity.
Respondent-only point estimates are explicitly descriptive whenever
nonresponse is nonzero.

For both the primary and multiplicity-weighted results, report worst-case
nonresponse bounds:

- lower: every nonresponse is unfaithful;
- upper: every nonresponse is faithful.

The primary exact interval additionally expands category bounds by applying
the two extreme nonresponse assignments before hypergeometric inversion.
Every frozen frame item must be attempted; an absent or unresolved terminal
record remains visible in counts and weights.

## Source-proxy interpretation

`source_proxy` means an **operational source-path proxy** used for coverage
accounting. It is not an adjudicated mathematical domain. Reports may say
"source-proxy coverage" or "source-proxy invocation count"; they must not
claim semantic-domain prevalence from this field.

## Three-family limitation

This frame contains three scalable local families:

```text
goedel_formalizer_v2_8b
kimina_autoformalizer_7b
stepfun_formalizer_7b
```

It is a `three_family_collection_only` design. It cannot support a
confirmatory D4/D5 mixture claim or a clean `heldout_generator_test` claim.
One-problem supplemental Kimi/Qwen/Codex qualifications do not become a
fourth scalable family and receive no Gate credit.

## Reproducibility and boundaries

The report binds the exact extended-v1 frame, 16-tranche Gate-5G lineage,
adjudication projection, and v3 → v2 → v1 policy lineage. Frame item IDs and
unique `(problem_group, alpha)` keys must be one-to-one; stratum row counts
must equal their frozen `n_h`; family multiplicities must reconcile to member
totals. Divergent report overwrite is rejected.

The estimator creates no label, changes no supervision eligibility, and
closes no gate. The frame is frozen; actual prevalence reporting now waits
only for genuine terminal human adjudication artifacts.
