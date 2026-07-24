# LF-021 compilation-only tranche decision

- Policy ID: `{{ policy_id }}`
- Policy SHA-256: `{{ policy_sha256 }}`
- Decision ID: `{{ decision_id }}`
- Observed immutable postprocess manifests: `{{ observed_manifest_count }}`
- Action: `{{ action }}`
- Next preregistered tranche: `{{ next_tranche_or_none }}`
- Parser successes: `{{ parser_success_count }}`
- Lean-compiling candidates: `{{ compile_success_count }}`
- Globally alpha-deduplicated, benchmark-clear candidates: `{{ unique_compiling_count }}`
- Human prevalence frame: `{{ frame_id_or_none }}`
- Human prevalence frame size: `{{ frame_size_or_zero }}`
- Reduced-data ablation: `{{ reduced_data_ablation }}`
- Semantic labels inspected: `false`
- Semantic labels created: `false`
- Gate 5G credit claimed: `false`
- Gate 5 closed: `false`

## Coverage deficits

{{ coverage_deficits_or_none }}

## Reduced-data flags

{{ reduced_data_flags_or_none }}

## Interpretation

This report may use only parser, Lean-compilation, benchmark-screen,
alpha-deduplication, generator-family, problem-pool, and deterministic
source-path proxy fields. Compilation is not a faithfulness label. The report
does not resolve any `same_claim` or relation value and cannot close a Gate.
