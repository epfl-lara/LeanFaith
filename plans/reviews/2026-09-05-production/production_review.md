> **Git-only reader:** start with [README.md](README.md). The absolute paths below identify the original local observations; they are provenance, not required inputs. Exported metadata is in [evidence/release_metadata.json](evidence/release_metadata.json), and the Wave 4 findings have a self-contained replay in this packet. This is the preceding analysis snapshot, not a claim that fixes are implemented.

# LeanFaith production review — 2026-09-05

Owner: Codex coordinator `/root`. Status: analysis complete; execution changes proposed below.
Scope: current project state, concrete SFT1 production blockers, and the shortest data delivery path.
This is a cross-worktree evidence snapshot, not a replacement for frozen execution contracts.
No Lean, GPU, provider generation, publication, or training was started during this review.

## Decision

Keep the research direction and prioritize SFT1 production. A focused execution correction is
needed; another broad architecture, transform-catalog, or authorization review is not needed.
The project already has released data, source inventories, reusable certificates, and authorization
for substantial SFT1 scale. Its current bottlenecks are specific implementation failures, restrictive
release selection, and stopped execution. Source count is not retained training-pair count.

Lean is the bottleneck: do safe parsing, source filtering, joins, deduplication, and selection
before Lean; reuse completed inventories and caches; repair only the failing transition; use
persistent bounded workers for the necessary audit and production. Do not compile a corpus to
increase confidence or repeat completed generation.

## Verified data ledger

| Workstream | Actual output | Readiness and limitation |
| --- | ---: | --- |
| CPT1 | 3,233,480 rows | Private release recorded; complete |
| CPT2 | 4,278,539 rows | Private release recorded; complete; 2,013,342 source-valid rows |
| SFT1 latest core, `wave2/core_v1` | 13,984 pairs / 3,496 roots; 6,992 per label | Private release recorded; useful curriculum seed with narrow negative-mechanism coverage |
| SFT1 earlier `core_v5_combined_square` | 6,412 pairs / 1,603 roots | Earlier overlapping release; do not add version counts together |
| SFT1 auxiliary N19 | 508,600 pairs / 127,150 roots | Private release recorded; deliberately easy curriculum, not broad core; existing sampling ceiling is 10% |
| SFT1 Wave 4 generation evidence | 1,183 recorded physical rows, 325 variants, 202 unique ancestry roots | Completed inputs across Mathlib, Physlib, CSLib; not yet a deduplicated release or a passed composition gate |
| SFT1 compiler-source inventory | 1,745,040 contextual signatures; 1,701,583 globally normalized text signatures | Completed zero-Lean census; not generated pairs or fully checked source contexts |
| SFT2A current shard series | 8,660 durable accepted pairs | 6,731 compacted in shards 1–2; 1,929 in 560 completed shard-3 roots; stopped on recovery bookkeeping |
| SFT2A older single-judge pool | 10,333 pairs | Separate legacy pool; not silently added to the new shard count |
| SFT2B | 100 labeled pilot rows, 97 distinct triples | Local pilot; 50,000-source generation core prepared, scale unstarted |
| EVAL | 5,111 pairs, 10 recorded baseline runs | Split frozen at 2,555 validation / 2,556 test; additional comparisons and goal-view join remain |

Publication counts above were checked against local manifests and durable publication receipts.
The SFT1 manifests for the latest core, earlier core, and N19 auxiliary match the hashes in their
publication receipts. These checks did not redownload released corpora. SFT2B alone also received
a live private-Hub metadata check in this review; its current revision contains source inputs and
the earlier three-row smoke, not the 100-row judge pilot or full generation outputs.

Key evidence and recorded Hub revisions:

- CPT1: `/storage/milikic/leanfaith/value_first/cpt1_v1/full/release/manifest.json`;
  `Lemmy00/leanfaith-cpt1-v1@ded730d3e404bef08e5a02b9f78428ff5dc6d862`.
- CPT2: `/storage/milikic/leanfaith/value_first/cpt2_v1/scale_full_v1/release/manifest.json`;
  `Lemmy00/leanfaith-cpt2-proof-validity-v1@df99c186ce1841c806d8b2a194573dc0b73fed33`.
- Latest SFT1 core: `/storage/milikic/leanfaith/value_first/sft1_deterministic_v1/wave2/combined/compacted/core_v1/manifest.json`;
  `Lemmy00/leanfaith-sft1-deterministic-v1@a3b5c921a24f5dedd57f1b4fb3155c163a0a48bd`.
- N19 auxiliary: `/storage/milikic/leanfaith/value_first/sft1_deterministic_v1/sprint_v1/compacted/aux_n19_square_curriculum/publication_receipt.json`;
  same repository at `c0b1bed5003af836dcfbbd0595b92913fd7c6c28`.
- EVAL: `/localhome/milikic/LeanFaith/reports/eval/value_first_v2/hub_publication_receipt_v1.json`;
  `Lemmy00/leanfaith-eval-v2@14472a6047a17c4d9fdcbd91ed191694665d004b` and
  `Lemmy00/leanfaith-eval-results-v2@158e4b5a9ff2254df20d99d159cf3416e4d21ac3`.
- SFT2B live Hub revision: `Lemmy00/leanfaith-sft2-autoformalizer-v1@a9b2d76d0f6c12e87c86434b6ad3744d13c50fee`.

## What is actually blocking SFT1

### 1. The latest core is still narrow

The latest 13,984-row core attributes 13,292 rows (95.1%) to the N25 equality/inequality-toggle
closure family. Its recorded relation-symbol-parity diagnostic reaches 0.963 balanced accuracy.
Its release gates passed, but that diagnostic was explicitly telemetry, not a blocking check.
N19 is larger and even easier: its recorded negation-XOR diagnostic is approximately 0.9973.
These datasets are useful seeds and ablations, but row counts overstate broad semantic coverage.
Evidence: the latest core's `manifest.json`, `release_report.json`, and
`pairwise_diagnostics.json`; the auxiliary's matching report files.

### 2. Wave 4 has enough input roots, but the combined release is not ready

The authoritative owner worktree is
`/localhome/milikic/LeanFaith-sft1-wave3-full-scale`, at `7498fbf` when inspected.
Its latest supplement terminal is
`/storage/milikic/leanfaith/value_first/sft1_deterministic_v1/wave4/control/supplement-bec8f12-v4/terminal.json`.
It records 202 unique roots, all three projects, five negative families, zero-call replays,
N25 share 13.8631%, and released host reservations. No SFT1 production session is active.

This review reproduced an additional release blocker without Lean. Joining the 11 completed base
runs and two supplement runs, after deduplicating identical pair IDs, makes the production
`materialize_wave4_records` function raise `duplicate pair across physical rows`. There are six
unordered rendered-pair classes with different pair IDs, affecting 12 records, with no conflicting
labels. Examples include `Nat.add_pred_div_lt`, `Nat.choose_lt_two_pow`, and
`Nat.fib_lt_fib_succ`, appearing in both the N31 and N32 runs.

The release builder deduplicates by pair ID before this check, so its existing path does not resolve
these cross-operation rendered duplicates. Repair this using deterministic selection of complete
certificate groups, or a fully specified shared-edge identity rule. Do not delete individual rows
and leave incomplete groups. Recount unique roots after deduplication and release balancing;
202 input roots do not establish an exact-200 final gate. Preserve all completed generation.

An independent read-only simulation resolved the collisions by stable group-ID ordering and
dropping six complete competing groups. It retained 319 groups, 1,161 physical rows, all 202
ancestry roots, and all five negative families, with zero closure-edge issues. This is diagnostic
evidence for a small repair; it is not a new release, persisted dataset, or completed manual audit.

### 3. The current balance rule defeats the purpose of composition

This is the most consequential planning defect. The current
`_balance_wave4_pair_delta_units` function requires a whole ancestry unit's cell-difference vector
to be zero or to have an exact negative elsewhere. For `n` variants sharing one negative base,
there are `2n` positive physical rows and `n+1` negative physical rows. Their total signed
difference is `n-1`, which is nonnegative for every unit. A multi-variant unit therefore cannot
find a unit with the negative total required to cancel it.

The independent read-only simulation found that 69 of the 202 deduplicated roots have this positive
surplus. Applying the actual balance function to the deduplicated inputs retained only **15 roots,
15 groups, and 60 rows**, all from Mathlib. It fails both the exact-200 and three-project conditions.
Generating a handful of supplemental roots cannot resolve this structural mismatch.

Git-only replay refinement: 69 roots have positive class surplus and 133 have equal total labels,
but only 13 roots have an exactly zero vector of surface-feature cell differences. Exact inverse
matching adds two more roots. Shared-base class imbalance therefore explains only part of the
observed 202-to-15 reduction. A class-weighting fix alone would not establish that the stricter
feature-cell criterion is useful or achievable; the reviewer should assess both requirements.

Proposed correction: preserve complete certified groups in a versioned generation pool; define
training sampling and optional strict balancing as separate views, with physical versus logical
row counts explicit. Any weighting or sampling contract must preserve source-disjoint evaluation,
report its actual shortcut diagnostics, and avoid claiming that certificates alone establish
diverse supervision. Do not silently waive a failed shortcut score or relabel the 1,161-row
simulation as a passed release. A narrow review of this sampling/acceptance decision is justified.

### 4. Compiler-scale enumeration allocates before it caps

The completed compiler inventory contains 256 shards and the fixed 1,000-root audit sample at
`/storage/milikic/leanfaith/value_first/sft1_deterministic_v1/wave5/compiler_inventory_v1/audit/sample-01000.jsonl`.
Do not rebuild the census. Its 174 gold-screen exclusions and 26 parse failures are already recorded.

The typed audit failed on 2026-09-04 at 07:05 UTC. The recorded failing source is the valid Boolean
theorem `theorem_25769 : ∀ p q : Bool, p ∆ q = xor p q`. Descriptor enumeration constructs the full
up-to-three-hop intermediate orbit before selecting five variants. It aborted with `std::bad_alloc`;
the subsequent recovery surfaced an infrastructure crash. The preserved failure is at
`/storage/milikic/leanfaith/value_first/sft1_deterministic_v1/wave5/control/typed1000-10793f5-v1/failure.json`.

The repair is bounded enumeration before materialization: deterministic work/memory limits while
exploring sites and chains, with explicit truncation/non-applicability telemetry. Retain exact
certificates on emitted pairs. First reproduce only the failing root, then resume the existing
audit using compatible cached terminals. Merely increasing the GPU allocation does not fix Lean
enumeration. Do not relaunch the unchanged failing command.

### 5. The compiler audit conflates source coverage with accepted-row correctness

`src/leanfaith/sft1/sprint/compiler_certificate_gate.py` in the latest SFT1 worktree sets four
critical checks to `len(passed_roots) == len(sources)`, including all source proofs and all
certificates. The existing audit already records a preflight rejection for an unresolved namespace
context. Thus fixing the memory crash alone does not ensure this 1,000-source gate can pass.

Proposed correction for coordinator review: every sampled source must have an accounted terminal;
every retained source must have a reconstructed context and checked proof; every retained pair
must have its exact certificate; ineligible source contexts are excluded and reported as coverage
loss. Eligibility must be decided without cherry-picking the resulting label or shortcut score.
Set a measured coverage/yield floor and a production throughput limit, rather than requiring a
noisy external source pool to have 100% compatibility. This changes source admission, not the
evidence required for a training label. It is a proposal, not a silent waiver in this review.

## Execution order and measurable deliverables

1. **SFT1 release repair and sampling decision:** resolve the six cross-run duplicate pair classes
   while preserving whole certificate groups; replace the structurally incompatible physical-row
   balancing gate with an explicitly reviewed sampling/release contract; finish the already-required
   composition inspection and report the actual shortcut results. Build from the existing 202 roots.
   Deliver a concrete release candidate and retained pair/root/family counts. Use no new Lean for
   already-certified rows and do not generate more roots to feed the current impossible balance rule.
2. **SFT1 compiler repair in parallel:** bound descriptor enumeration; test the recorded crashing
   root; resolve the source-eligibility gate above; finish only the remaining fixed-sample audit.
   Deliver a measured eligible-root fraction, pairs per root, memory peak, and production projection.
3. **Start incremental SFT1 production:** after the applicable existing gate passes, use its standing
   authorization to run the eligible library/compiler shards in detached sessions. Publish complete
   shards as they finish. No fresh approval sentence at each checkpoint and no new optional
   transformation family on the critical path. Existing authorization covers up to 500,000 compiler
   roots and released-row checkpoints at 500K, 1M, and 2–3M; it does not guarantee their yield.
4. **Keep SFT2A a bounded parallel repair:** fix ownership generations on reopened roots, check that
   transition with the existing journal, resume shard 3 from caches, and continue configured shards.
   Its completed second shard measured 17.5 accepted rows/minute. Give SFT1 priority within the
   shared two-worker/40-GiB Lean budget. Do not rerun provider/model pilots.
5. **Keep SFT2B off the SFT1 critical path:** activate the already-prepared four 12,500-source shards
   when the eight-A100/H100-80GB target is accessible. Implement the persistent Lean/three-judge
   consumer in parallel: the launcher currently writes downstream queue entries but no production
   worker consumes that queue. Raw generations are not labeled training rows.
6. **Prepare the first training comparison from a useful frozen checkpoint:** reconcile the frozen
   goal representation with EVAL's existing pair IDs, then compare the agreed Ettin model and
   ModernBERT control when training is separately scheduled. Do not require every optional CPT/SFT
   stage or every additional baseline before the first comparison. A 50K–100K diverse core is a
   useful initial planning target, not a new minimum gate or a claim that current yield supports it.

Treat the first production shard's post-filter yield and elapsed time as the forecast. The existing
Wave 4 inputs cost about 1.47 Lean-seconds per recorded physical row, including gate work; naive
linear extrapolation to 500K would be roughly 8.5 worker-days before further selection. This is a
warning against an unsupported completion promise, not a production benchmark. Improve bounded
selection/cache reuse and measure the actual shard; request a suitable larger host if the measured
production projection remains unreasonable. More GPU memory alone does not accelerate Lean.

Report progress as new unique training pairs published, unique ancestry roots, source and negative
family coverage, remaining eligible sources, and measured throughput. Do not count test passes,
artifact bytes, raw generation attempts, repeated releases, or auxiliary rows as broad core growth.
Failures should name the failed transition and the smallest repair. Completed siblings and cached
terminals remain reusable.

## Focused independent-review packet

GPT Pro can review the following bounded decision while the implementation repairs proceed.
There is no need to repeat a full project or 46-operation catalog review.

> LeanFaith needs to scale SFT1 while preserving exact certificates on every released label.
> Current evidence: 13,984 published core pairs, 95.1% from equality/inequality-toggle closure;
> relation-symbol parity scores 96.3%. Another 508,600 pairs are easy N19 auxiliary curriculum.
> Wave 4 has completed 202 ancestry roots but the combined builder rejects six cross-operation
> duplicate rendered-pair classes. Complete-group deduplication preserves all 202 roots and 1,161
> physical rows, but exact whole-root pair-delta balancing leaves only 15 roots/60 rows, all Mathlib.
> With `n` variants sharing a negative base, physical class counts are `2n` positive versus `n+1`
> negative; all multi-variant units have nonnegative surplus and cannot find the inverse vector
> required by that rule. The compiler census contains 1,745,040 contextual signatures;
> its 1,000-root audit crashed because three-hop descriptor enumeration expands before the
> five-variant cap. At least one sampled source has an unresolved namespace context, while the
> checker requires every sampled source to pass.
>
> Review only: (1) change source-audit acceptance to terminal accounting plus explicit compatibility
> coverage, with 100% exact evidence on retained sources/pairs; (2) deterministic work-bounded
> enumeration and exclusion of truncated/ineligible cases; (3) complete-group duplicate resolution
> and a sampling/release contract compatible with shared-base composition. Consider a unique
> certified generation pool plus separate sampling/diagnostic views; keep actual shortcut scores
> visible instead of turning failed diagnostics into a claimed pass. Identify any counterexample that changes a
> label or leaks evaluation data. Recommend concrete acceptance rules and one bounded regression
> for each real defect. Do not add per-operation authorization layers, rerun passed pilots, require
> zero rejection from heterogeneous sources, or make optional mechanism expansion block production.

## Scope and remaining uncertainty

The initial checkout is `milikic/sft2b-source-correction-v3` and contains old SFT1/SFT2A briefs.
Current production evidence is in independent SFT1 Wave 3–5, SFT2A 72-hour-sprint, and SFT2B
scale-sprint worktrees. This explains contradictory status prose; it must not cause repeated gates.
Existing EVAL changes in the initial checkout were preserved. This review modifies only its root
plan's cross-worktree pointer and this document.

No new production job was launched. Final Wave 4 release yield/shortcut results, post-repair
compiler-context coverage and throughput, and a working SFT2B queue consumer remain unverified.
No production-readiness claim should be inferred from a passing source-input count alone.
