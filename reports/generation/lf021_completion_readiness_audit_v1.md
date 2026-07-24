# LF-021 completion and Gate-5 readiness audit v1

**Date:** 2026-07-24  
**Scope:** label-blind review of `PLAN.md`, the LF-021 milestone, the frozen
tranche-expansion policy/implementation, and the implemented command surface  
**Semantic labels inspected:** no  
**Active raw outputs inspected:** no  
**Frozen artifacts modified:** no

## Verdict

LF-021 is **not yet ready to freeze its scientific prevalence frame, close
Gate 5G, close Gate 5, or hand off to LF-022**.

The local collection and postprocessing machinery can finish the declared
label-blind tranche sequence, and the current expansion CLI can write a
content-addressed unresolved frame. However, one correctness defect in the
frame population must be repaired before that output can be used, and the
Gate-5G finalizer, annotation handoff, adjudication import, prevalence
estimator, and Gate-5 finalizer do not yet exist.

## Confirmed pre-frame blocker

`src/leanfaith/generation/tranche_expansion.py::_cluster_candidates` groups
candidates by `alpha_identity_fingerprint` alone and creates a global cluster
ID from that fingerprint alone.

That is invalid for an NL-to-Lean prevalence frame. The same Lean proposition
can be generated for two different NL problems and be faithful for one but not
the other. The current code can therefore:

- collapse semantically distinct `N + candidate Lean` items;
- retain only one representative `problem_record_id`;
- distort the unique-candidate stopping count and coverage cells; and
- make one human label appear to cover outputs from unrelated NL problems.

It also conflicts with PLAN §17.6, which requires deduplication to retain
problem identity.

**Required correction before a frame is frozen:** issue a versioned,
pre-label amendment. At minimum, the cluster identity must contain
`problem_record_id + alpha_identity_fingerprint`, every member must be checked
to have the same problem, and the frame record must preserve the complete
member/family multiplicities needed for prevalence weighting. A stronger
scientific design should distinguish:

1. problem-plus-alpha unique-claim prevalence; and
2. invocation-weighted generator-output prevalence.

If per-generator faithful-output prevalence is a target, exact duplicate
members from different generator families cannot simply disappear. A single
human label may be reused for alpha-identical candidates for the **same**
problem, but the frame/report must retain per-family member counts and use
them in the estimand.

Do not close Gate 5G from a v1 frame. Preserve v1 decisions as historical
label-blind evidence; create a new policy/schema/implementation version and a
deviation record rather than rewriting them.

## Other completion gaps

### Prevalence estimand and interval are not decision-complete

The current frame records exact stratum inclusion probabilities, but neither
the plan nor code freezes:

- whether the primary estimand is unique problem-plus-alpha claims or raw
  compiling invocations;
- how exact duplicate multiplicity contributes to overall and per-family
  prevalence;
- the design-weighted estimator;
- the confidence-interval method for unequal-probability stratified sampling;
- the treatment of strata with one sampled non-certainty unit;
- terminal ambiguity in the primary prevalence number; or
- unresolved human nonresponse after adjudication.

These choices must be frozen before labels are inspected. The report should
include the three-way faithful/unfaithful/ambiguous proportions, the planned
binary sensitivity with ambiguous treated as not faithful, and explicit
nonresponse bounds.

The current source-path `source_proxy` is operational coverage metadata, not
an adjudicated semantic domain. Gate reports must call it a source proxy and
must not claim semantic-domain prevalence from it.

### Three-family scope is not represented by the current frame flag

The frozen local policy uses three successful generator families. PLAN §17.3
and Gate 5 require this to be a `reduced_data_ablation` for confirmatory D4/D5
and `heldout_generator_test` claims. The expansion model currently sets
`reduced_data_ablation=false` whenever a preferred frame is reached, because
that flag only represents frame-size/coverage exhaustion.

The Gate-5G report must therefore carry a separate, mandatory scope flag such
as `three_family_collection_only`; it must state that Gate 5G may pass while
confirmatory D4/D5 and held-out-generator claims remain unavailable. The
supplemental Kimi/Qwen/Codex one-problem qualifications do not satisfy this
fourth-family requirement.

### Required Gate-5 deliverables and commands are absent

The following PLAN Phase-5 deliverables do not currently exist:

```text
data/real_outputs/validated/manifest_v1.json
reports/generation_coverage.md
reports/faithful_prevalence_design.md
reports/milestones/phase_5_real_outputs.md
reports/gates/gate_5g.json
reports/gates/gate_5.json
```

`reports/milestones/lf_021_real_outputs.md` is useful progress evidence but is
not the declared Phase-5 completion artifact.

The installed Typer command surface also has no commands for:

```text
close-gate5g
export-annotation
import-annotation
adjudicate-annotation (or a validated adjudication import)
report-prevalence
close-gate5
generate-llm-variants
```

Only the `AnnotationRecord` schema exists. There is no blind export/import
pipeline, double-label/adjudication ledger, or design-weighted prevalence
report implementation. PLAN §7.2 names `export-annotation`,
`import-annotation`, and `generate-llm-variants`, but their scripts/commands
are not implemented; the numbered script names in that table now conflict
with the scripts already occupying those numbers.

## Work that can be prepared now

None of the following requires semantic labels or active raw-output access:

1. Create the versioned problem-aware expansion amendment and synthetic tests.
   Re-evaluate completed immutable postprocess manifests only after the new
   policy, schema, implementation hashes, and deviation record are frozen.
2. Implement a fail-closed Gate-5G finalizer against synthetic fixtures. It
   should validate the final decision/frame, every bound artifact/hash,
   coverage, family identities, replay evidence, benchmark screens,
   propensities, unresolved status, and the three-family claim limitation.
3. Freeze `reports/faithful_prevalence_design.md` (or a versioned policy file)
   with the estimand, weighting, CI, ambiguity, nonresponse, and source-proxy
   rules.
4. Implement LF-023's annotation tooling on fixtures under the §24.0 parallel
   annotation carve-out:
   - blind export from a frame item to its `NLPLeanRecord` and linked
     candidate/reference `PairRecord`;
   - generator/seed/intention/score hiding;
   - two independent raw `AnnotationRecord`s;
   - immutable adjudication import;
   - no label written back into frozen frame/terminal artifacts.
5. Implement the prevalence estimator/report and Gate-5 finalizer with
   synthetic labels.
6. Prepare the Phase-5 provider ADR and the frozen judge×supervision matrix
   required before LF-022. `configs/generation/providers.yaml` still has all
   relevant external/judge slots disabled and unresolved.
7. Add canonical Phase-5 generation-coverage, validated-manifest, milestone,
   and gate-report schemas/paths rather than relying on one-off prose.

## Work that must wait

1. The final corrected frame cannot be materialized until the declared local
   expansion has a complete, replay-verified postprocess prefix.
2. Gate 5G cannot close until the corrected frame is immutable and all final
   collection/postprocess/replay/overlap evidence is bound.
3. The actual annotation export cannot be frozen until that frame exists.
4. Gate 5 cannot close until the frame has 200–300 terminal adjudicated human
   labels and a frozen prevalence report. Every frame item should be attempted;
   `cannot_assess_yet` remains nonterminal and must be escalated or reported as
   nonresponse.
5. Real-output supervision, real-output test composition, and headroom claims
   must wait for Gate 5.
6. Under the current strict backlog and the LF-021 milestone's own definition
   of incomplete, LF-022 should not start until Gate 5 closes. If the project
   intends to allow quarantined LF-022 generation after Gate 5G but before
   Gate 5, that exception must first be made explicit in a versioned PLAN/ADR;
   the current LF-021/LF-022 acceptance order does not state it.

## Patch-ready Gate 5G closure criteria

Gate 5G may pass only when all of the following hold:

1. the corrected expansion decision is a frame action, not
   `collect_next_tranche` or `exhausted_without_frame`;
2. preferred-frame closure has no coverage deficits; a reduced/exhausted
   frame remains an explicit non-passing deviation unless PLAN is amended;
3. the frame has 200–300 unique **problem-aware** items and validates against
   its versioned schema;
4. every frame pointer and all observation/policy/implementation bytes match
   their recorded SHA-256 values;
5. every selected item is benchmark-clear, compiling, unresolved,
   `supervision_eligible=false`, and free of semantic labels;
6. all required family/pool/source-proxy cells and inclusion propensities
   reconcile mechanically;
7. all collection and postprocess denominators and replay audits pass;
8. ReForm×Lean-Workbook is explicitly `not_applicable` unless ReForm is used,
   rather than silently omitted;
9. the report binds exact family/revision/overlap records and counts only the
   three scalable local families for Gate credit;
10. the report records the three-family limitation for D4/D5 and held-out
    generator claims; and
11. `reports/gates/gate_5g.json`, the coverage report, validated manifest, and
    Phase-5 milestone are finalized before their hashes enter any later gate.

## Patch-ready Gate 5 closure criteria

Gate 5 may pass only when:

1. Gate 5G is passed and hash-bound;
2. the immutable frame and annotation-export manifest agree exactly;
3. every item has two independent raw annotations or an explicit terminal
   workflow failure;
4. disagreements, low confidence, `cannot_assess_yet`, and reference issues
   follow the frozen adjudication policy;
5. 200–300 items have terminal adjudicated labels;
6. the prevalence report reproduces from immutable raw annotations,
   adjudications, frame propensities, and the frozen estimator policy;
7. faithful, unfaithful, ambiguous, and nonresponse counts/weights are all
   reported, with confidence/sensitivity intervals;
8. no labels are copied into raw/postprocess/frame artifacts and no
   unresolved item becomes supervision eligible;
9. downstream real-output-test sizing, power/headroom assumptions, and
   reduced-data limitations cite the exact prevalence report; and
10. `reports/gates/gate_5.json` binds Gate 5G, the annotation/adjudication
    manifests, prevalence design/report, code/config/environment hashes, and
    the finalized Phase-5 milestone.

## Current safe command boundary

The present label-blind decision command is:

```bash
uv run python scripts/18_plan_lf021_tranche_expansion.py \
  --root /localhome/milikic/LeanFaith \
  --policy configs/generation/lf021_tranche_expansion_v1.yaml \
  --postprocess-manifest <manifest-0> \
  --postprocess-manifest <manifest-1> \
  ... \
  --output reports/generation/lf021_tranche_expansion_v1
```

It remains valid for reproducing historical v1 decisions, but because of the
problem-identity defect it must not be used to freeze the scientific
prevalence frame or close Gate 5G. The corrected version must use a new policy,
implementation, output namespace, and tests.
