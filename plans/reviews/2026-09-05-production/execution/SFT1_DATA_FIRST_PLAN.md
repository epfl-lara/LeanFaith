# SFT1 data first: execution handoff

Date: 2026-09-05. Coordinator: Codex `/root`.
Status: planning and handoff complete; implementation and publication are the SFT1 owner's next work.
Code reviewed: `9bd94800886c66589110d3bceb37b26a94122abb`, based on SFT1 production
`7498fbf4e78421012c1c729d05576c345c5f16f1`.

## Decision

**Deliver a usable SFT1 dataset first, then grow it with the existing library mechanisms.**
The first checkpoint is a new private Hub release with an executable sampling view, not another
review, local pool, or test report. Recover completed Wave 4 evidence without Lean, publish it,
then generate missing square closures from already certified negatives before spending a full
sweep's runtime on new roots. Extend that production stream through the remaining Mathlib inventory
when measured net yield justifies it.

This follows the user's instruction to prioritize SFT1 before other new work. It supersedes the
older sequence that put full Wave 4 composition and the compiler audit on the immediate path.
No new SFT2, CPT, evaluation-baseline, model, or training work belongs in this assignment. Do not
interrupt unrelated live jobs or discard another owner's changes. SFT1 remains the priority after
the first publication; the small recovered release is the first delivery, not the end of SFT1.

Lean is the bottleneck: do safe parsing, eligibility, provenance joins, deduplication, and selection
before Lean; reuse persistent project workers and content-addressed evidence. Do not compile a
completed corpus again for confidence. Existing catalog and incremental private publication
authorization is recorded in the [SFT1 brief](../../../30_sft1_deterministic.md). This handoff adds
no mechanism and does not weaken any proof requirement.

## What the two reviews establish

Use [GPT Pro's review](GPT_PRO_REVIEW.md), Fable 5.1's review supplied by the user, and the
[original packet](../README.md) as evidence. The execution decisions below resolve their differences.

| Issue | Decision and qualification |
| --- | --- |
| Completed Wave 4 cannot pass the present selector | Confirmed by the production-code replay. Whole-group collision removal recovers 319 groups / 1,161 physical rows / 202 roots. Exact inverse feature-cell balancing then leaves only 60 rows / 15 roots. Shared-base class surplus and missing/opposed cell support are separate problems. |
| Fastest next production route | Adopt Fable's priority for library single-hop generation and established squares. First recover existing data, then exploit already certified non-N25 roots. Do not put compiler repair or composition optimization ahead of publication. |
| Sampling shared physical edges | Adopt GPT Pro's logical-group exposure formula. Generic physical-row class balancing does not restore each endpoint's label marginals. Correct weighting still does not eliminate pairwise shortcuts. |
| Compiler memory crash | Cause remains unproven. Code confirms repeated engine elaboration under command isolation, and also traversal without an explicit work bound. Neither establishes the failing allocation phase. The preserved retry files do not establish two equivalent fresh-server allocation failures. |
| Claimed large output and speed | Forecasts, not commitments. Fable's historic throughput used a different operation mask and selected root population. Census matches are not certified-negative yield. Measure net unique cap-compliant rows per Lean-second. |
| N25 and first training comparison | Preserve the current 25% physical released-row cap and also cap exposure at 25%. Do not adopt Fable's proposed 40%, or turn the physical cap into a weights-only rule. Training is deferred by the user's current priority. |
| Quality screens | Certification, unique storage, sampling, and shortcut results are different properties. Publish explicitly identified certified curriculum shards with honest diagnostic outcomes. A failed feature screen cannot become a fabricated pass or a certificate rejection. |

Independent read-only inspection of local row files gives the following starting inventory. These
counts are observations from storage on 2026-09-05, not a newly validated or published release.

| Input | Unique unordered rendered pairs | Novelty |
| --- | ---: | --- |
| Frozen Wave 2 `core_v1` | 13,984 | Historical release |
| Frozen `core_v5_combined_square` | 6,412 | 4,490 additional to Wave 2 |
| Frozen union | **18,474** | Releases overlap on 1,922 pairs; no label conflict found |
| 13 non-fixture Wave 3 full-scale Mathlib runs | **372** | 362 additional to frozen union |
| Recovered Wave 4 | **1,161** | 947 additional to frozen union; 835 additional after Wave 3 |
| Naive union of these inputs | **19,671** | Before cross-release complete-group selection and final integrity/gold checks |

The frozen union has 17,782 N25-family and only 692 non-N25 rows, approximately 96.3% N25.
It remains a named historical curriculum; its full union is not a new cap-compliant core.
The recovered Wave 4 pool has 638 positive and 523 negative physical rows; family attribution is
N31 665, N32 316, N25 164, N26 8, N30 8. N25 is 14.1%, so a useful immediate publication does
not require relaxing the cap. Fable's 383 Wave 3 rows were not reproduced from this particular
finalized run selection. Recount explicit lineage-backed inputs rather than using that number.

## Delivery A: publish recovered SFT1

Own this first. Extend existing `src/leanfaith/sft1/sprint/` selection, integrity, view, and publishing
code; do not build a parallel runner or generic data platform. Add a versioned release/view policy
under `configs/transformations/sft1_value_first_v1/`. Keep old configs and hash-bound evidence intact.

1. Load the completed Wave 4 base and supplement through their original generation policies and
   receipts. Check endpoint/evidence bindings and construct a global unordered-pair index before
   the strict materializer. Preserve every occurrence and certificate reference in provenance.
2. Select complete groups deterministically using a fixed content/group-ID order compatible with
   the packet replay. A competing same-label representation loses its whole group, with a reason;
   legitimate shared bases remain shared. A conflicting label or corrupted identity quarantines
   all affected groups and triggers an integrity investigation. Do not silently downgrade it to
   ordinary low yield. Rematerialize surviving memberships; never drop a single required edge.
3. Recover the packet's 319 complete groups / 1,161 rows / 202 roots before later selection, or
   explain an evidence-backed count difference. Select a deterministic exact-200 inspection view
   with the required project/family coverage, preserving the other roots in the pool. Finish the
   already required rendered-pair inspection once; do not regenerate roots to make a round number.
4. Remove physical feature-cell equality from admission for the additive certified curriculum
   policy. Retain the legacy policy and its 60-row result as historical behavior. Update selector,
   release validation, integrity, and consumed view schemas together so a downstream loader cannot
   silently reapply the old rule. Deferred compiler readers must reject the new policy explicitly
   if they do not support it; no compiler repair is required for this release.
5. Publish physical rows, group index, keyed evidence/provenance, sampling sidecar, diagnostics,
   manifest, and a short load/sample example to an additive `data_first_v1/` prefix in private
   `Lemmy00/leanfaith-sft1-deterministic-v1`. Reuse existing publisher and schema conventions;
   record exact configuration names in the task brief. Mark this a certified curriculum, including
   any failed shortcut screens. Keep the model row exactly `{reference, candidate, label}`.
6. Recover eligible Wave 3 singles as a separate named view with explicit lineage and sampling.
   Do this after the first Wave 4 upload if single-hop view plumbing would delay that upload.
   Do not concatenate overlapping views as though every row were new.

Composition exposure is fixed as follows. Assign normalized mass to ancestry roots, distribute a
root's mass across its complete groups, and give each group's four logical roles mass `q_g / 4`.
For a physical edge shared by groups, sum those role contributions:

`w_e = sum(q_g / 4 for each group-role occurrence stored as edge e)`.

This yields equal positive/negative mass and matching one-sided endpoint marginals. Enforce both
physical N25 <=25% and N25 exposure <=25%, attributing exposure through group provenance. If caps
need selection or reweighting, do it at group/root level and verify the resulting properties.
Freeze ancestry/duplicate components before any partition or weight derivation. Support reproducible
orientation swapping in the sampling recipe, outside the stored core rows. Single-hop rows need
their own explicit mass rule and diagnostics; they cannot inherit four-role guarantees.

Evaluate physical and actual weighted views, including candidate-only, reference-only, family-held-out,
and explicit pairwise-rule diagnostics. Record sample sizes and failed thresholds honestly. Do not
start a new balancing-optimizer or learned-model experiment to make these diagnostics look better.

For incremental union views, use immutable published-winner precedence and a fixed shard order.
Keep cross-release duplicate/ancestry links even when historical datasets remain separate. A view
may reference an already stored shared edge with compatible evidence; it must not lose a closure
role because a preceding dataset owns that physical row. Avoid arbitrary graph merging: where the
existing membership model cannot express this safely, drop the competing group deterministically.

Minimum verification is bounded and directly tied to changed behavior: complete-group duplicate
selection under input permutation, conflicting labels, legitimate shared bases, group weights with
one/two/five variants and multiple bases, deterministic resume, cap checks, and an actual
publish/readback manifest match. Run scoped static checks and the packet replay once after the
patch. Reuse existing generation/certificate and resume evidence; no new Lean is needed for A.

**A is done when a new private Hub revision agrees with local manifest/schema/checksums, a consumer
can load and sample the default view, and exact row/root/group counts, novelty, family mix, exposure,
and limitations are reported.** A local archive or a passing test suite is not this checkpoint.

## Delivery B: grow the dataset with proven library operations

After A, keep the worker producing useful SFT1 data and publish additional completed shards.

**First fill missing squares over certified negatives.** Census finalized prior runs with the
existing `square.py census --source-run-ids ... --source-staging-root ...` route. Include the old
`tenk` certified N31/N32 negatives where provenance and environment identity match. Join against
already completed square requests and the union index before scheduling Lean. Prioritize novel
non-N25 results with existing `SQUARE_WAVE2_N31_V1`, `SQUARE_WAVE2_N32_V1`, and
`SQUARE_WAVE2_N26_V1`. Missing closure certificates require Lean; prior source evidence does not
by itself certify the square. Reuse exact compatible cache hits. Do not replay completed Wave 2
N25/N32 full-pool coverage and call those attempts progress.

**Then extend single-hop coverage.** Derive a new config from the frozen `wave3_v1.yaml`, with new
run/output identifiers and the existing pinned inventory/project. Start with these complete IDs:

```text
N31_DROP_REQUIRED_GUARD_PROOF_V1
N32_SWAP_ROLE_ORDER_PROOF_V1
N26_INCREMENT_BOUND_PROOF_V1
N30_ADD_UNJUSTIFIED_UNIQUENESS_PROOF_V1
N29_SWAP_WITNESS_DEPENDENCY_PROOF_V1
N25_TOGGLE_EQ_NE_PROOF_V1
P14_SWAP_INDEPENDENT_DATA_BINDERS_V1
P15_SWAP_IFF_SIDES_V1
P18_SYMMETRIZE_EQUALITY_V1
P23_CURRY_PROP_PAIR_V1
```

Resolve exact executable arguments from the current CLI; Fable's abbreviated operation IDs are
not valid runner aliases. Cheap source screens and prior terminal/cache joins run first. Exclude
N19, new mechanisms, and multi-hop/orbit enumeration. N25 generation is useful only for documented
balance headroom or missing coverage; do not spend the queue on excess N25. Inventory regex
matches are candidates, not evidence that an operation will succeed.

Reuse the unchanged engine's one-example/100-root evidence. Check one end-to-end serialized result
through the new release route and its resume using existing evidence. For an unmeasured operation
mask, process a fixed 1,000-root prefix of the intended remaining queue, then continue the same
resumable queue through roughly 10K and the remaining inventory when production is healthy and
the measured yield/runtime is useful. Keep every pilot output; no parallel throwaway pilot and no
new approval ceremony for the already authorized catalog. If sampling is enriched, report that
stratum and extrapolate only within it.

At the first prefix, report attempted/new/cache-hit roots, per-family newly certified negatives,
net unique released pairs after selection, Lean time, wall time, RSS, failure classes, and projected
remaining time. Measure missing-square work similarly. Choose the next available library lane by
net useful yield per Lean-second; deterministic not-applicable roots are terminal, not retries.
Low yield in a family does not block successful siblings. If the projection becomes multi-day on
this host, stop at a durable shard boundary and report the needed compute or smaller useful scope.
Do not promise 35–40K, 100K, or millions before measuring cap-compliant novelty.

Use a fresh explicit controller deadline and stop conditions. The existing `--sprint-end-utc`
option feeds a gate report; it is not a generation-loop wall-clock limit. Do not assume copying
Fable's command or changing that option makes an unattended run bounded.

## Operational constraints and completion evidence

Use an isolated `milikic/sft1-data-first-delivery` worktree based on this review branch or a verified
descendant containing the latest SFT1 implementation. Claim SFT1 and update its brief's status,
owner, timestamp, next gate, and append-only log. Respect its writable paths. Shared backend,
dependencies, and root policies stay coordinator-owned. Keep release policy separate from
generation identity: a Python/view-only change may import old evidence through an explicit
compatibility check; do not remove semantic hash bindings or force a new release hash into old
cache lookups. A changed source, proof engine, renderer, or environment needs its proper identity.

Use one persistent Lean worker by default. Inspect and claim shared resources before any Lean work;
the machine-wide cap remains two workers / 40 GiB / one GPU. A one-worker 24-GiB reservation cannot
be combined with another 24-GiB claim. No GPU is needed here. Keep `Elab.async=false`, existing
per-worker journals, and context caches. Do not lower the documented hard address-space limit to
fit a reservation; coordinate the actual host allocation.

Any run outliving the turn uses a named detached `tmux` session. Before launch record committed
code, config and input hashes, exact command, output/cache roots, resource claim, journal/log,
deadline and stop rules, and resume command. Verify the real process tree and advancing journal
before handoff. Report terminal accounting and manifest/checksum verification before calling a
run complete. Stop for real certificate/label corruption, unsafe resource use, or broken durable
state; do not turn ordinary source incompatibility or optional low yield into a project-wide gate.

Order of visible results: first recovered release; first newly generated non-N25 square shard;
then the resumable library sweep and incremental SFT1 revisions. Report these actual deliveries
before test counts. Do not mark all of SFT1 complete when A is published, and do not start training
implicitly when a row-count checkpoint is reached.

## Deferred decisions

Compiler work resumes only after a useful SFT1 publication and evidence that it deserves priority
over the productive library queue. First phase-bisect `theorem_25769`; put the immutable engine
in reusable context while isolating per-source code; isolate failing roots, account infrastructure
terminals, and fix gate plus downstream admission consistently. Preserve fail-closed certificates.
Review source axiom trust before scaling compiler-derived labels. Then address discarded single-hop
outputs and root-wide variant caps. Benchmark that repaired route before a new large audit.

Traversal bounds and direct stored-hop reconstruction are necessary before renewed composition
scale, but are not required to publish existing library evidence. New negative mechanisms, real
number grounding, or a changed N25 mix are later explicit scope decisions if measured library
yield is insufficient. Another independent review, a model choice, and a training comparison are
not prerequisites for deliveries A and B.

## Evidence locations

All storage paths below are under
`/storage/milikic/leanfaith/value_first/sft1_deterministic_v1/`:

- Wave 4 original run lists: `wave4/control/gate-inputs-bec8f12-v2/run_summary.json` and
  `wave4/control/supplement-bec8f12-v4/terminal.json`.
- Wave 3 selected source runs: finalized non-fixture runs under
  `wave3/full_scale_v1/mathlib/runs/`; bind explicit receipts, not an unrestricted directory glob.
- Frozen rows: `wave2/combined/compacted/core_v1/` and
  `sprint_v1/compacted/core_v5_combined_square/`.
- Wave 5 failure evidence: `wave5/compiler_audit_v1/typed_certificate_gate/raw_responses/` and
  `wave5/control/typed1000-10793f5-v1/failure.json`.

The Git packet's projected rows support selector replay only. Actual publication must load original
bound production evidence from storage; the projection is not a replacement for certificates.
