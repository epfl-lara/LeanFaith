# LF-021 compilation-only tranche-expansion preregistration v1

**Policy:** `lf021_compilation_only_tranche_expansion_v1`  
**Artifact class:** operational research design  
**Semantic labels inspected:** no  
**Semantic labels created:** no  
**Gate credit claimed:** no

## Purpose

This policy freezes how LF-021 expands local-model collection before any
faithfulness judgment is available. It consumes immutable
operational postprocess manifest bundles implementing the version-neutral
observation contract in `tranche_expansion.py` and may inspect only:

- parse completion;
- Lean compilation/materialization;
- benchmark-screen results;
- alpha-identity deduplication;
- generator family;
- separately curated pool identity; and
- deterministic source-path proxy.

Compilation is not a faithfulness label. The policy cannot read human or LLM
judgments, resolve `same_claim`, use proof-search results, create supervision,
or close Gate 5G/Gate 5.

## Exact sequence

Every observation must be a complete prefix of this table. No failed,
low-yield, or inconvenient tranche may be skipped or replaced.

| Order | Tranche | Pool | Goedel seed | Kimina seed | StepFun seed | Raw denominator |
|---:|---|---|---:|---:|---:|---:|
| 0 | `algebra_s0` | 40-problem Algebra pool | 30 | 0 | 0 | 120 |
| 1 | `cross_domain_s0` | 20-problem cross-domain pool | 30 | 0 | 0 | 60 |
| 2 | `algebra_s1` | 40-problem Algebra pool | 31 | 1 | 1 | 120 |
| 3 | `cross_domain_s1` | 20-problem cross-domain pool | 31 | 1 | 1 | 60 |
| 4 | `algebra_s2` | 40-problem Algebra pool | 32 | 2 | 2 | 120 |
| 5 | `cross_domain_s2` | 20-problem cross-domain pool | 32 | 2 | 2 | 60 |
| 6 | `algebra_s3` | 40-problem Algebra pool | 33 | 3 | 3 | 120 |
| 7 | `cross_domain_s3` | 20-problem cross-domain pool | 33 | 3 | 3 | 60 |
| 8 | `algebra_s4` | 40-problem Algebra pool | 34 | 4 | 4 | 120 |
| 9 | `cross_domain_s4` | 20-problem cross-domain pool | 34 | 4 | 4 | 60 |
| 10 | `algebra_s5` | 40-problem Algebra pool | 35 | 5 | 5 | 120 |
| 11 | `cross_domain_s5` | 20-problem cross-domain pool | 35 | 5 | 5 | 60 |

Orders 0 and 1 are mandatory. Therefore the separately curated cross-domain
pool is collected even if the first Algebra tranche alone happens to cross the
numeric frame target.

## Candidate population

The operational population is every parsed, Lean-compiling candidate that:

1. reaches the postprocess screening stage;
2. has no active-benchmark hit; and
3. has a valid binder-normalized alpha-identity fingerprint.

Candidates with the same alpha fingerprint form one global cluster across all
observed tranches. A salted, frozen hash selects the representative, preventing
tranche order or a preferred family from winning ties systematically. All
contributing invocation, family, pool, and source-proxy identities remain
attached to the cluster.

## Mechanically checkable stop rule

After both mandatory tranches, stop at the first completed prefix satisfying
all of the following:

1. at least 240 global unique compiling, benchmark-clear clusters;
2. at least 20 clusters contributed by each of the three generator families;
3. at least 40 clusters contributed by the Algebra pool;
4. at least 30 clusters contributed by the cross-domain pool;
5. at least 5 clusters in every generator-family × pool cell; and
6. at least 1 cluster contributed by every source proxy declared in the two
   frozen pool manifests.

Otherwise, collect exactly the next row in the sequence.

If the sequence is exhausted:

- with 240 or more eligible clusters but a coverage deficit, freeze a
  deterministic 240-item `reduced_data_ablation`;
- with 200–239 eligible clusters, freeze all eligible clusters as a
  `reduced_data_ablation`;
- with fewer than 200 eligible clusters, freeze no prevalence frame and record
  `exhausted_without_frame`.

No source or family is silently substituted in any of these outcomes.

## Frame selection and propensities

The preferred frame has 240 items, within the PLAN.md range of 200–300.
When downsampling is needed:

1. strata are the representative generator family × pool × source proxy;
2. each nonempty stratum receives one item;
3. remaining slots use exact integer Hamilton apportionment over residual
   stratum capacity;
4. a frozen salted hash ranks clusters within each stratum; and
5. every selected record stores the exact inclusion propensity
   \(n_h / N_h\).

The frame remains entirely unresolved:

```text
same_claim = null
relation = null
semantic_labels_created = false
supervision_eligible = false
gate_5g_credit_claimed = false
gate_5_closed = false
```

## Implementation bindings

- Policy/config:
  `configs/generation/lf021_tranche_expansion_v1.yaml`
- Versioned schemas and evaluator:
  `src/leanfaith/generation/tranche_expansion.py`
- CLI:
  `scripts/18_plan_lf021_tranche_expansion.py`
- Decision template:
  `reports/generation/templates/lf021_tranche_expansion_decision_v1.md`
- Tests:
  `tests/unit/test_lf021_tranche_expansion.py`

The CLI performs no model calls. It writes content-addressed decision, report,
and optional frame artifacts and replays them byte-for-byte.
