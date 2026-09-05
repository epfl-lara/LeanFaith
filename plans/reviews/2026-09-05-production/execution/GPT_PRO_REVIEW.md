> GPT Pro review supplied by the user on 2026-09-05. Trailing whitespace normalized.
> Original attachment SHA-256: `62d1b29f110ebe13714123f219a8afafb62d3660a124beb0cc764568647cec36`.

# Verdict

**The assessment is substantially correct. The fastest defensible route is to recover the completed composition data, separate certification from training-distribution policy, and repair the compiler path before scaling it. Do not regenerate the completed library runs or rebuild the compiler inventory.**

However, the proposed repairs need to be more comprehensive than “deduplicate, change the balancing rule, and resume.” I found additional production-path problems: **the compiler selects variants per operation but enforces a limit per root; it computes single-hop results that it never publishes; and its batch-failure handling is weaker than the audit’s.** These can cause another failure—or very poor yield—even after the reported problems are fixed.

**Verification scope:** I inspected the referenced code and evidence at commit `9bd94800886c66589110d3bceb37b26a94122abb`. I executed the count arithmetic and small counterexamples described below. Repository download failed in this environment, so I did **not** execute the complete Git-only replay, run Lean, recount the omitted corpora, or independently verify private Hub publications. Accordingly, the packet’s full replay results remain **recorded results**, not results I reproduced.

## What the evidence establishes

| Finding | Status and qualification |
|---|---|
| **13,984 core pairs, 3,496 roots; substantial shortcuts** | **Recorded.** The metadata attributes 13,292 rows to N25-derived groups—about 95%—and reports relation-parity balanced accuracy of **0.963**, despite passing the recorded candidate-only, reference-only, and family-held-out screens. Those screens therefore do not establish shortcut resistance.  |
| **508,600 auxiliary pairs** | **Recorded, explicitly auxiliary.** The historical N19 outer-negation diagnostic is approximately 0.9973. This is not a substitute for a diverse core and should not inflate the headline useful-data count.  |
| **Completed composition inputs cover 202 roots** | **Recorded:** 1,183 physical rows, 325 groups, across the three projects. The supplement’s terminal explicitly says it awaits the exact-200 build and manual inspection; it is not a passed release gate.  |
| **Whole-group deduplication retains 1,161 rows/319 groups/202 roots; balancing leaves 60 rows/15 roots** | **Recorded simulation; algorithmic explanation independently verified.** These are not automatically 1,161 *net-new* pairs relative to every older release; that requires the training-union duplicate index.  |
| **1,745,040 contextual compiler signatures already inventoried** | **Recorded completed, zero-Lean inventory.** This establishes a candidate population, not 1,745,040 compatible contexts or certified training examples.  |
| **Compiler enumeration is unbounded before selection** | **Code-verified.** The reported allocation failure is consistent with this implementation, but I did not reproduce the crash. The captured supervisor failure marker itself establishes only failure during `typed-gate-run`, not an allocator stack trace.  |

# 1. Adopt a four-part production contract

These should be independent properties, not one overloaded “passed” flag.

### Certified generated data

A generated pair is admitted only when its exact source, endpoints, transformation, and required proof/refutation evidence satisfy the certificate contract. Composition groups retain every logical role, direct and composite certificates, and exact negative-last replay. Ineligible sources and unsuccessful searches produce terminal records, **not guessed labels**.

Keep the three model fields unchanged: `{reference, candidate, label}`. Certification, provenance, group membership, and sampling information remain outside the model input. The existing evidence and frozen-rendering machinery should be preserved, not replaced.

### Unique storage

Maintain a unique model-pair index and an occurrence/provenance index separately. Several certified occurrences may justify the same model pair. Selecting one physical representation must not erase the other origins or silently detach a certificate from its group.

For the immediate repair, **deterministic whole-group selection is sufficient**. A general system that merges arbitrary certificate graphs is unnecessary. Preserve rejected duplicate occurrences in the certified archive; release only complete surviving groups.

### Training exposure

For composition data, sample **logical groups**, not uniformly from unique physical rows.

Let \(q_g\) be a group’s normalized sampling mass. Give each of its four logical roles mass \(q_g/4\). The weight of stored edge \(e\) is then

\[
w_e=\sum_{g:\,e\in g}\frac{q_g}{4}.
\]

Thus, a shared negative base receives the combined weight of the groups referencing it, while remaining stored once. Because each group contains two positive and two negative roles with matching one-sided endpoint marginals, this restores those properties in the effective composition distribution. **It does not remove joint pairwise shortcuts.** The four-role construction supports this distinction directly.

Start with root-balanced group sampling, rather than allowing roots with more variants to dominate. Freeze ancestry/duplicate-component splits **before** deriving weights.

Preserve the existing N25 released-row cap and its family-attribution meaning; do not silently replace it with a sampling-only cap. Also cap N25 training exposure. Use **zero N19** in the first comparison.

### Shortcut diagnostics

Run diagnostics on both the physical corpus and the **actual weighted training view**. Include explicit pair-comparison features, not only side-tagged token features.

A failed diagnostic means “shortcut-prone training distribution,” not “invalid certificate.” Conversely, proof certification does not justify calling a distribution broad or shortcut-resistant.

**Proposed policy change:** allow additive publication of explicitly identified certified curriculum/data shards without exact feature-cell equality. Keep shortcut thresholds as requirements for claiming a distribution has passed the corresponding quality screen. Do not relabel failed screens as passed, and do not let them prevent generating the counterexamples needed to improve the distribution.

# 2. Critical repairs, acceptance rules, and minimal regressions

Paths abbreviated as `sprint/` below are under `src/leanfaith/sft1/sprint/`.

## A. Resolve duplicates before the strict materializer

**Code location:** `sprint/square.py::build_wave4_release`, particularly lines **4800–5070**, merges source runs and then calls `materialize_wave4_records`. The latter rejects different pair IDs sharing an unordered model-pair key. Consequently, merging exact IDs alone does not resolve the recorded cross-run duplicates.

**Change:**

Introduce a shared complete-group selector before strict materialization:

1. Recompute model-pair keys; check row/evidence bindings.
2. Separate same-label duplicates from conflicting labels or corrupted identities.
3. Process groups in a fixed, content-based order.
4. Reserve all four roles atomically. On a conflicting same-label duplicate representation, discard the **whole losing group**, not one edge.
5. Rebuild surviving memberships with `_rematerialize_wave4_selection`.
6. Retain every source occurrence, original certificate reference, and selection reason in provenance.

Do not classify different origin paths or alternative valid certificate occurrences as semantic corruption merely because complete sidecar bytes differ. Equally, do not merge differing endpoint identities or labels under an existing stable ID.

For incremental production, a published prefix must remain immutable. Use fixed batch/commit ordering and published-winner precedence—not worker completion order or a later “better” winner that would require rewriting previous shards.

**Acceptance:** the packet fixture should recover its documented **319 groups, 1,161 rows, and all 202 roots before subsequent selection**, with zero conflicts, dangling memberships, or partial groups. Preserve certificate and pair IDs; derived membership hashes may change transparently.

**Smallest regression:** two complete groups with one same-label duplicate edge, a legitimate shared base, and a conflicting-label variant. Permuting inputs must preserve selected content and provenance. Run the packet replay once as the larger Lean-free regression.

For the existing composition gate, select a deterministic **200-root gate view** from the recovered pool while preserving required project/family coverage. Keep the other roots in the pool; do not regenerate them.

## B. Remove physical feature-cell balancing from certified-storage admission

**Code location:** `sprint/square.py::_balance_wave4_pair_delta_units` and `select_wave4_release_groups`, lines **3500–4210**. Enforcement also appears in `build_wave4_release` and the Wave 5 shard-validation block around **1560–1610**. Single-hop selection has a related destructive rule in `sprint/views.py::_balanced_wave3_selection`, lines **800–980**.

There are **two distinct problems**.

For \(G_r\) groups and \(B_r\) distinct shared negative bases under a root, the current storage contract gives

\[
P_r=2G_r,\qquad N_r=G_r+B_r,\qquad P_r-N_r=G_r-B_r\geq0.
\]

Therefore, no combination of whole-root units can cancel a positive total surplus with a negative-total unit: such units do not exist under this construction.

I independently recomputed the packet’s post-dedup cell totals:

- **638 positive, 523 negative** physical rows;
- **319 groups, 204 distinct bases** implied by those counts;
- a **115-row positive surplus**.

But correcting that surplus is not enough. The balancer requires zero vectors or **exact inverse feature-cell vectors**, not merely equal class totals. The packet reports 133 roots with equal totals, yet only 13 zero-vector roots plus the two inverse-matched roots survive. Its cells also contain **210 positive rows with no negative examples in the corresponding cells**. Weighting cannot manufacture missing support.

I also executed the elementary counterexample

\[
(1,-1,0),\quad(0,1,-1),\quad(-1,0,1).
\]

These sum to zero, but none has an exact inverse among the others. The heuristic would reject an aggregate-balanced combination.

**Change:** replace this admission rule with the group-weighted training view above and explicit support diagnostics. Do **not** spend the next day building a more elaborate balancing optimizer. Whole-root quarantine is also stricter than certificate completeness requires: the existing rematerializer can remove complete groups while retaining shared bases correctly.

Version the selector, release checks, shard reader/writer, and relevant integrity handling together. A selector-only edit will still fail downstream validation.

**Acceptance:** storage eligibility no longer depends on equal physical feature-cell counts; the effective composition view has exact positive/negative mass balance; failed shortcut screens remain visible.

**Smallest regression:** roots with one, two, and five variants, plus a root with multiple negative bases. Check unchanged unique-row counts, complete groups, correct shared-edge weights, and weighted one-sided marginals. Keep the old 60-row result only as a legacy-policy regression.

## C. Bound enumeration and selected reconstruction

**Code locations:** `LeanFaith/Meta/SFT1/Sprint.lean::{wave4Applications,wave4DescribeExpand,buildWave4Descriptors}`, lines **2940–3250**; and both `rebuildSelectedWave4Orbits` and `rebuildSelectedCompilerWave4Orbits`.

The implementation materializes applications on both endpoints, considers Cartesian pairings, recursively concatenates descendants, and only later applies Python’s selection limit. The selected-certificate request then **re-enumerates the entire descriptor population again**.

**Change:**

Use deterministic bounded traversal with explicit limits on attempted site/pair work, live frontier, accumulated descriptors, and expression size. Join compatible operation/site classes before pairing where possible. A top-five heap after exhaustive traversal, or a larger memory allocation, is not a repair.

Retain depth at most three and enforce **five selected variants per ancestry root across operations**, before certification. Interleave operations/depths so an early enumeration branch cannot consume the entire budget.

Persist selected operation/site recipes with content identities and endpoint bindings. Reconstruct those recipes directly using the existing replay checks. An interim bounded re-enumeration is acceptable only when both phases use identical frozen bounds and descriptor identities; old traversal indices must not be interpreted under a changed enumerator.

Record “budget exhausted/truncated” separately from “exhaustively not applicable.”

**Acceptance:** instrumented counters never exceed configured bounds; repeat runs select identical descriptors; selected reconstruction does not perform unrestricted search; every admitted selected variant retains exact replay and all existing proof checks.

**Smallest regression:** one wide branching fixture, one ordinary successful fixture, and one tampered selected recipe. Exercise **both** descriptor and certificate requests. Revisit the recorded offending source once on the production host, rather than restarting the 1,000-root job to discover whether the repair worked.

The exhaustive-enumeration setting in `wave4_v1.yaml` makes this an explicit policy/version change, not a silent implementation optimization.

## D. Separate source eligibility from certificate correctness throughout the gate chain

**Code locations:** `sprint/compiler_certificate_gate.py`, summary checks at **1750–1900** and `CompilerTypedCertificateGateRunner::_record_from_outcome`; `sprint/compiler_scale.py::verify_audit_gate`, starting at **line 426**; and `sprint/compiler_replay.py::_preflight_reason`.

The typed audit equates several correctness checks with “every sampled root passed.” The scale loader independently requires `compatible == expected_rows` and `incompatible == 0`. Meanwhile, the sample deliberately includes namespace/context-complexity strata, and the captured journal already contains an unresolved-namespace rejection. Changing only the typed summary cannot unlock scale.

**Change:** introduce explicit terminal categories:

- source compatible or source ineligible, with reason and scope;
- operation not applicable or bounded search exhausted;
- retained with complete validated evidence;
- infrastructure unknown/pending;
- certificate or integrity defect.

Known source ineligibility may reduce coverage. It must not fail the entire audit. **A malformed purportedly retained certificate must still fail the gate**, not be reclassified as harmless ineligibility.

Unify cheap eligibility checks between selection and replay. Cache context failures only when the failure is genuinely context-wide; one bad theorem must not blacklist a good shared context.

Produce the context-compatibility receipt from the same checked source evidence used by the typed audit where compatible. Do not execute the same 1,000 sources twice merely to satisfy two receipt formats.

**Acceptance:** all original 1,000 sample members remain accounted for; no success-only replacement sample; every admitted pair passes all proof, rendering, contamination, and group checks; infrastructure unknowns do not masquerade as semantic terminals; retain the existing nonzero-output and useful-family requirements.

Report coverage and yield by source/context, features, and length strata. Do not interpret an unweighted success rate from this deliberately stratified sample as population coverage.

**Smallest regression:** a valid source plus an unresolved-context source should permit an eligibility-qualified result; replacing the latter with a corrupted retained certificate must fail. Test the **downstream scale loader** against both outcomes.

## E. Reuse compatible results without weakening cache identity

**Code locations:** `sprint/compiler_scale.py::{_checker_dependency_hashes,_root_cache_identity}`, around **680–740**; `compiler_certificate_gate.py::_typed_gate_run_identity` and `CompilerTypedCertificateGateRunner::_validate_cached`; `square.py::Wave4Runner.try_cache`.

Whole implementation/configuration hashes and policy identities currently couple proof caches to orchestration and release policy. “Change the policy and resume” can therefore turn into another generation run.

**Change:** separate source/proof compatibility, descriptor-search policy, and release/sampling policy. Preserve original immutable cache objects. Add an explicit compatibility importer that validates old evidence under an applicable schema and records the original hashes, identities, and Lean-call accounting.

Do not simply remove checker hashes or declare all old results compatible. Changed source, project, proof-checking semantics, or renderer semantics require appropriate invalidation. A sampling-policy change does not.

Likewise, load completed Wave 4 runs against their original generation policy, then apply the new release policy. Passing a new policy hash into the existing old-run loader is not a migration strategy.

**Acceptance:** release-only changes reuse compatible certificates with **zero Lean calls**; changed proof/context identities cannot hit those caches; restart preserves IDs, receipts, and accounting.

**Smallest regression:** make backend construction raise if invoked, import compatible completed results after a release-policy change, and verify success. A changed source/project identity must reject that reuse. Add one interruption between cache write and journal terminal.

## F. Repair the compiler executor’s actual output path

**Code location:** `sprint/compiler_scale.py::_LeanTypedExecutor.execute_batch`, lines **1020–1330**.

Three issues deserve immediate correction.

**Per-operation versus per-root limit.** Selection runs separately for each orbit operation, potentially yielding up to five from each. The final materializer enforces five groups across the root. A root with three surviving variants from two operations can therefore fail after paying for six certificates. Select globally before certification; do not raise the guard to thirty.

**Computed single-hop results are discarded.** `processCompilerRoot` runs the configured single-hop operations, but production materializes only Wave 4 closures and rejects a root with `no_certified_wave4_closure`. Useful single-hop output is consequently not a production yield path.

Publish retained single-hop results through a separate certified-pair view, using `rebuildCompilerPairs`, frozen rendering, and the existing certificate checks. Do not force them into the four-role closure schema or route them through unchanged destructive cell matching. This is reuse of enabled mechanisms, not a new transformation programme.

**Batch poisoning.** Production rejects every source in an invalid descriptor batch. The audit already has source-bisection machinery to isolate bad members. Reuse it, while preserving independently validated successes and treating evidence corruption differently from source reconstruction failure.

**Acceptance and smallest regressions:** one root with two operations producing three variants each must certify at most five; one single-hop-success/no-orbit root must produce a properly rendered certified pair; one bad source beside one good source must not erase the good result.

## G. Close one compiler-source trust-boundary gap

**Static risk, not an observed wrong label:** source contexts may contain preceding declarations, including axioms. The inventory classifies these rather than rejecting them, and typed reconstruction preserves the non-import context. `checkedProof` checks the term and its kernel typing but does not itself establish an approved transitive axiom dependency set. Kernel validity is relative to the environment’s assumptions.

Before broad compiler admission, require source and generated proof dependencies to stay within the pinned trust policy, excluding unapproved source-local axioms and indirect `sorryAx` dependencies. Reuse dependency results by compatible context/proof identity.

**Smallest regression:** a compiler source proved through a newly introduced `axiom bad : False`, an indirect-sorry helper, and a normal legitimate source. The first two must not enter the certified pool. This is a small label-quality safeguard, not a new corpus-wide validation exercise.

# 3. Next 24–48 hours

These are execution windows and priorities, not a promise that a particular row count is attainable.

| Window | Parallel work and deliverable |
|---|---|
| **Hours 0–4** | **Release/data owner:** implement whole-group deduplication, provenance preservation, logical weights, and versioned release checks against existing artifacts—no Lean. **Lean owner:** bound enumeration/reconstruction, enforce the global variant limit, and run the small targeted live fixtures. **Training/evaluation owner:** prepare the frozen goal-view join and existing matcher/collator smokes; measure tokenizer lengths and local throughput. No full training yet. |
| **Hours 4–12** | Recover the completed Wave 4 pool; build the exact-200 gate view and finish its **existing required inspection**, without expanding it into another review programme. Publish the recovered data additively after the applicable corrected checks pass. Import compatible compiler audit terminals and finish only missing/incompatible work on the original sample. Generate eligibility and typed-certificate receipts from shared evidence. |
| **Hours 12–24** | Continue the authorized library producer and compiler producer through independently complete shards. Use the existing 256-root compiler shard boundary initially. Measure **net unique useful pairs per Lean-second**, not raw terminal counts. At the first completed shard, inspect yield, memory, rejection taxonomy, and projected runtime; do not wait until 10,000 roots to notice a disastrous projection. Publish completed shards through the existing queue. |
| **Hours 24–48** | Continue the productive source/family queues, cheaply prioritizing under-supported non-N25 cases. Freeze a training snapshot from completed verified shards and launch the first authorized matched-budget comparison. Save an early checkpoint while generation continues into later immutable snapshots. Do not wait for 500K, 1M, or 2M milestones to publish or begin the first comparison. |

The existing manifest-last shard writer and publication/recovery queue are useful infrastructure. Reuse them; do not build another framework. Validate new rows/groups and their receipts incrementally, then reference completed shard receipts at later checkpoints instead of revalidating the entire corpus repeatedly.

Use the shared resource ledger. The ceiling is **two Lean workers and 40 GiB combined**, not two independent 24-GiB allocations. Start with one worker where necessary; add the second only under a compatible measured allocation. Python release work and training preparation can proceed independently of that Lean bottleneck.

Do not promise 100K or 500K rows in this window. The recorded composition work consumed about 1.74 million Lean milliseconds for 1,183 physical rows, including its gate workload; that is not a clean production benchmark, but it is enough to reject optimistic extrapolation without new measurements.

# 4. First meaningful training checkpoint

**Compare data, not several architecture and pretraining changes simultaneously.**

Use the planned Ettin-150M symmetric matcher for two short runs: **legacy seed only** versus **legacy seed plus the new certified snapshot**. Apply the same declared view-construction rules, optimizer recipe, initialization seed, and matched non-padding-token budget. Exclude N19 from both. Defer CPT stages, SFT2, the ModernBERT full-run control, and hyperparameter searches until this comparison exists. The training brief already provides the architecture and implementation references; training must never invoke Lean.

Aim for a substantially enlarged, diverse snapshot—50K–100K would be useful—but **do not turn that target into another launch gate**. At the planned freeze, use the completed snapshot and report its actual unique pairs, ancestry components, family coverage, and exposure distribution. A small salvage-only experiment must be described as such, not as evidence that broad SFT1 production succeeded.

Set the numerical token budget from the local throughput measurement and freeze it before either run. **Save and evaluate the first comparison checkpoint at 25% of that common budget**, then at its completion. This gives an early answer without committing to a full training campaign.

Before launching, verify complete-input handling: independently padded sides, finite weights, swap invariance, and no silent truncation of label-critical hypotheses or targets. Over-length groups can be excluded from that training view while remaining in certified storage.

Use the frozen EVAL validation assignment and the **1,227-row primary semantic population**, with group-level uncertainty reporting and explicit representation coverage. Keep validity-only rows separate. Select configurations and thresholds on validation only; reserve test evaluation for the selected result. The recorded EVAL note does not establish completion of the `goal_v1.0` join, so check that artifact rather than restarting existing baselines.

# 5. Decisions genuinely needed

**One policy amendment:** adopt the separation of certified storage, sampling, and shortcut diagnostics; bounded rather than exhaustive enumeration; and eligibility-qualified rather than universal-source-success auditing. These are proposed changes to current policies—not permissions already implied by the old gates. Record them together in a versioned contract while preserving proof requirements, contamination screening, group completeness, stable identities, existing family caps, and historical artifacts.

**One bounded training/compute authorization:** authorize the two-run comparison after its data/EVAL pins and measured budget are frozen. The current brief authorizes preparation and throughput work but defers full training. Prefer hardware demonstrated sufficient by that measurement; do not request larger placement merely by habit.

The active SFT1 authorization already covers the relevant generation path, conditional scaling, and incremental private publication. It does **not** require another approval at each shard or row milestone, and it does not require restarting the inventory, adding SFT2, or completing expensive optional baselines first.

**The practical objective is an expanding, immutable stream of certified, uniquely indexed data with an explicit training distribution—not another all-or-nothing “perfect corpus” gate.**
