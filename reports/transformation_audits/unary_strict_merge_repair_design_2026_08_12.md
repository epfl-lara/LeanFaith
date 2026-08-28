# Public unary strict-merge diagnosis and repair design

Date: 2026-08-12

## Decision

The preserved public unary producer output is internally complete and
content-audited, but it is **not** an independently replayed strict artifact.
No existing record may be upgraded by assertion, relocation, or by writing the
old `full_lean_replay_audit.json` format. The current strict-admitted count
remains **zero**.

The scientifically sound recovery is a new, explicitly versioned
candidate-level migration. It must independently replay every accepted
candidate and admit only the exact successes. It must not claim that the full
producer generation—including rejected and non-applicable outcomes—replayed.

## Bound facts

Producer root:

```text
/storage/milikic/leanfaith/deterministic_scale/
  run_76de447_public_schema4_v1/unary
```

The 16 producer run specifications share:

| Binding | Value |
|---|---|
| producer Git revision | `76de4470a548c997e043d1bd0d4915b810664144` |
| producer code-tree hash | `8872d87d8176e47a4cc7037f54099a0e28391251700d48bbc5f20d82b1f1fcf4` |
| shard-set specification hash | `c735030657401eb73636d48857e9c1b89d56f19aacbb39f8b0ecd75c5dfb8d1d` |
| source universe | 27,786 mathlib theorem records |
| theorem partition SHA-256 | `7f1a157bfb818b49d082dcc58de221bdddb67f6e8309554395baeb29850838d7` |
| representation partition SHA-256 | `c799f54c60d3eb3f45a0fa473231ba991e871b7de440c65b037436721037e505` |
| source-inventory manifest SHA-256 | `f2d9e84f36a63b362684b5ec0a3d1ec86c6a7ddce60856f7e3aeb47dbca625c9` |
| benchmark manifest SHA-256 | `4ffbd31dd0e10efb9dfe7e57fb815690f3e1d750e4640aeff959ca4cbdc911df` |
| mathlib revision | `d568c8c09630de097a046763c17b9ea99f95f950` |
| mathlib tree hash | `c0c130fcbbadd6aa9a081e39d4980f4bdc7cd9dc` |
| context ID | `ctx:0cd06826b8767b3bc951c0eb00c802424af95785b558f9f8a61f18694a86c4ce` |

The producer manifests and journals reconcile to these terminal counts:

| Terminal outcome | Count |
|---|---:|
| accepted | 27,327 |
| audit quarantined | 1,741 |
| candidate invalid | 2,067 |
| candidate representation failed | 402 |
| not applicable | 162,482 |
| protected benchmark overlap | 7 |

For every accepted item there is exactly one draft, candidate theorem,
`repr_v3` representation, audit, variant, and pair. An exploratory audit
computed this digest over seven per-shard partition-hash lists and their
counts:

```text
f4d316fc815bad7c996839c16d982901f5f7adccddfcf379570fd97ab97d4246
```

The digest is **not an admissible trust anchor** because no preserved artifact
defines its canonical preimage, ordering, schema, or hash algorithm. The
migration must recompute and publish a versioned canonical accepted inventory
rather than trusting this bare value. The existing content-audit artifact
remains:

```text
/storage/milikic/leanfaith/deterministic_scale/
  run_76de447_public_schema4_v1/unary/provisional_merged/
  provisional_merged_manifest.
  dda088624e25ee271a7ac8d013e8f63414188596a35c3d5c240ef8b72dfc268d.json
```

Its logical manifest hash is
`dda088624e25ee271a7ac8d013e8f63414188596a35c3d5c240ef8b72dfc268d`,
its file SHA-256 is
`699e34ecd90547750520d7a680de7f39ffe981e0705c832c4071f1f0d82b95d2`,
and its merged pair partition SHA-256 is
`f649b1c12f934c1c7bfb992fef92c0c38a4fb6cebec5274cae35bcd18bdfd1ba`.
It explicitly records `merge_replayed_with_lean=false`,
`training_eligible=false`, `evaluation_eligible=false`, and
`gate_credit=false`.

## Confirmed failure and information gap

The final strict attempt is recorded at:

```text
/storage/milikic/leanfaith/deterministic_scale/
  run_76de447_public_schema4_v1/unary/orchestration/merge_20260810.log
```

The log SHA-256 is
`149d2a898f8c55ecb4219248fbb4a9a640e44815b335a8a218e6e99ab29b3e6c`.
The exact-producer-code attempt reached scratch materialization and failed with
`scratch Lean replay differs from the producer scientific manifest`.

The old replay implementation compared the complete regenerated shard
manifest before scratch cleanup, excluding the operational
`raw_response_file_count` and `raw_response_tree_hash` fields, and then
discarded the scratch directory. It did not persist the first record-level
difference. Therefore the evidence establishes that full generation did not
replay, but it does **not** identify whether the first difference was an
accepted candidate, an invalid diagnostic, a representation failure, or
another terminal outcome. Treating the 27,327 accepted records as
independently replayed would consequently be unsound.

## Required migration artifact

Implement a new artifact kind, for example
`deterministic_unary_candidate_replay_migration_v1`. It must never overwrite a
producer shard or emit `full_lean_replay_audit.json`.

The migration specification must bind:

1. all 16 producer `run_spec.json` and `manifest.json` file hashes;
2. the existing provisional content-audit manifest's logical ID, exact file
   hash, and merged-pair partition hash listed above;
3. a new canonical accepted-inventory artifact with an explicit schema,
   SHA-256 algorithm, byte encoding, lexicographic pair-ID order,
   per-partition hashes and counts, and whole-file hash; the exploratory
   `f4d316fc...` digest above is informational only;
4. the immutable theorem, representation, inventory, benchmark, context,
   mathlib, producer-code, and replay-code identities;
5. the LeanInteract and REPL revisions plus the replay method version;
6. the frozen accepted pair-ID list in lexicographic UTF-8 byte order, its
   canonical JSONL encoding, record count, and file SHA-256; and
7. a content-addressed output root distinct from every producer root.

Before any candidate receipt is admitted, the migration must independently
rerun the existing global content/lineage validator over the bound provisional
artifact. Only that successful validation may set
`producer_content_audit=true`.

For each accepted pair, an independent replay worker must:

1. load the source, `TransformationAttempt`, draft, candidate theorem,
   representation, audit, variant, and pair through their exact producer
   bindings;
2. re-execute the pinned deterministic rule and rebuild the complete canonical
   `TransformationAttempt`, `VariantDraft`, candidate `TheoremRecord`,
   `VariantRecord`, and `PairRecord`; require byte-exact equality to every
   persisted provenance-bearing field, including inline-context binding,
   candidate-code hash, forward/inverse trace, intended relation, source
   lineage, split groups, and parent identifiers;
3. reconstruct the candidate in the pinned source context;
4. elaborate it through a fresh, isolated LeanInteract request with
   `allow_sorry=true`, retaining the raw response before parsing;
5. require the replayed validation class to equal the persisted class;
6. rebuild `repr_v3` and require exact theorem ID, statement hash,
   representation ID, representation content hash, alpha fingerprint, required
   view statuses, and required view payloads;
7. rerun the family audit and require an exact clean audit identity; and
8. write a self-hashed receipt that binds every input and raw/rebuilt output.

Any operational field excluded from exact record comparison must appear in a
closed, versioned allowlist with a documented reason; the default allowlist is
empty. Diagnostics text and raw-response filenames may be recorded outside
identity only because the semantic status and raw content hashes remain bound.

The migration manifest must bind the complete expected pair-ID inventory,
canonical successful-receipt and quarantine partitions, their counts and file
hashes, uniqueness checks, and a deterministic join-root hash. A receipt's
self-hash alone is never admission authority. Any semantic payload or status
mismatch is a quarantine, never an automatic rewrite.

## Fail-closed admission contract

The migrated merged artifact may include a pair only when exactly one valid
receipt exists and every check above passes. It must satisfy all of these
conditions:

```text
producer_content_audit = true
accepted_candidate_elaboration_with_placeholder_replay = true
accepted_candidate_representation_replay = true
accepted_transformation_trace_replay = true
full_generation_replay = false
resolved_semantic_labels = 0
promoted_items = 0
output_quality_tier = provisional
training_eligible = false
evaluation_eligible = false
gate_credit = false
```

Missing, duplicate, malformed, noncanonical, mismatched, timed-out, crashed,
or infrastructure-failed receipts go to an explicit quarantine partition.
The admitted count is the number of exact successful receipts—not 27,327 by
assumption. The old strict manifest type must reject this migration artifact;
downstream readers must opt in to the new type explicitly.

This migration certifies mechanical generation, statement elaborability with a
proof placeholder, representation, and provenance only. It does not certify
theorem validity. It does not turn a negative transformation intention into an
F1 semantic label and does not prove that a positive family is gold without
its separately registered certificate/promotion policy.

## Execution order

1. Add schema, receipt verifier, replay worker, deterministic join, and
   adversarial unit tests.
2. Run one accepted item from each observed family end to end.
3. Replay a frozen ancestry-diverse bounded tranche and perform an independent
   receipt-verifier replay.
4. Only then run all 27,327 accepted candidates with restartable, bounded
   workers.
5. Materialize admitted and quarantined partitions, publish hashes and counts,
   and independently replay the final join.
