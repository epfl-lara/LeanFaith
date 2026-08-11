# Deterministic-scale execution and recovery

This runbook is authoritative for scientific LF-017/LF-018 deterministic
materialization. It does not promote variants or create semantic labels.

## Two scientifically distinct passes

N10 is pair-aware: its donor pool is the complete immutable source universe,
and an accepted result consumes ancestry capacity for both the primary and
donor. It must therefore never inherit a shard-local donor pool.

Run the unary families in deterministic root-component shards with:

```text
configs/transformations/deterministic_scale_unary_sharded_v1.yaml
```

Run N10 separately over the same immutable inventory with `--shard-count 1`
and:

```text
configs/transformations/deterministic_scale_n10_global_v1.yaml
```

The materializer fails closed if an active N10 rule is combined with
`--shard-count` greater than one. The unary shard merger also recomputes this
policy. The global N10 output is a separate provisional partition set.

Each worker still parses and validates every theorem and every `repr_v3` row
against the immutable inventory, including IDs, content hashes, context, exact
coverage, counts, and upstream manifests. To avoid retaining the complete
large representation partition in every process, a unary worker keeps only the
full representation records assigned to its shard. The dedicated global N10
pass keeps the complete selected source universe because any eligible theorem
may be a donor. Both input partitions are hashed again after the streaming scan
and before any run output is created. This retention optimization does not
change source ordering, root-component shard assignments, donor scheduling, or
exact replay semantics.

Manual concatenation of the two partition sets is scientifically invalid. After
both passes have independently completed and merged, authorize them together
with:

```text
leanfaith combine-deterministic-scale-passes \
  --unary-merged-output <unary-merged-dir> \
  --n10-merged-output <n10-merged-dir> \
  --output-dir <combined-manifest-dir>
```

This command re-invokes each ordinary merge (and therefore its exact Lean
replay), verifies identical source inventory, code, project, toolchain context,
and benchmark provenance, enforces disjoint family ownership/record IDs/
candidate payloads, and recomputes the admission caps over both passes. Only
the resulting content-addressed `deterministic_scale_two_pass_manifest`
authorizes downstream code to treat the two outputs together. It does not make
the provisional records training-eligible.

## Resume and merge verification

Every source theorem runs in a fresh LeanInteract backend. Within that source,
incremental optimization remains enabled so repeated commands reuse only the
import-header cache. Every semantic command receives the versioned deterministic
nonce prefix, and Lean elaboration is explicitly synchronous
(``Elab.async=false``). These settings and the Lean method version are bound by
the schema-v4 run spec and therefore by both run-spec hashes. Producer and
replay raw-response trees retain the transport-isolation evidence emitted by
the backend.

`--resume` always performs exact deterministic replay, including Lean
elaboration and candidate representation, for each persisted source shard.
Receipt-chain hashes detect accidental file corruption but are not evidence
that persisted Lean results remain valid.

`--fast-resume` is retired and rejected. It must not appear in scientific
commands or manifests.

When every source shard already exists, an exact `--resume` execution:

1. validates the receipt chain;
2. replays every deterministic rule;
3. re-elaborates every accepted candidate through LeanInteract;
4. rebuilds every accepted representation;
5. compares every rebuilt source shard exactly with its persisted journal;
6. writes `full_lean_replay_audit.json`.

The audit file is self-hashed accounting metadata, not proof that Lean was run;
an attacker who can rewrite a shard can also rewrite that file. The merge
command therefore performs the exact `--resume` Lean replay itself against the
pinned clean project revision/tree and context before it reads any producer
partition as scientific output. It then rebuilds attempt, draft, quarantine,
candidate, audit, variant, and pair lineage from the immutable theorem/repr_v3
inventory. In particular it applies `check_pair_groups` and rejects missing
donor ancestry even when all producer JSON, hashes, receipts, partitions,
manifests, and replay-audit hashes were consistently rewritten.

Merged deterministic variants remain `provisional`; merged pairs remain
unresolved; the merged manifest records `training_eligible=false`.

## Provisional content-audit merge while strict replay runs

The scientific merge above remains authoritative. A second, explicitly
lower-trust command exists only so completed producer shards can support
exploratory mining and smoke modeling without waiting for the sequential exact
replay of the complete source universe:

```text
leanfaith generate-deterministic \
  --merge-scale-shards-provisional \
  --output-dir <new-provisional-output-dir> \
  --shard-output-dir <shard-0> \
  --shard-output-dir <shard-1> ...
```

It requires the complete bound shard set and recomputes source assignment,
input hashes, journal and receipt-chain integrity, producer partition equality,
raw-response bindings, deterministic record identities, ancestry, candidate
deduplication, and cross-record semantic lineage. It retains the fact that
producer candidates were checked through LeanInteract, but it deliberately
does not rerun all producer work through Lean during merge.

The artifact kind is
`deterministic_scale_provisional_merged_manifest`. Its fail-closed literal
fields are:

```text
verification_mode=producer_content_audit_without_full_replay
merge_replayed_with_lean=false
exploratory_modeling_eligible=true
training_eligible=false
evaluation_eligible=false
gate_credit=false
```

This artifact cannot close a gate, enter evaluation, be promoted, or be
described as scientifically replayed. It must use a separate output directory;
the strict merge continues independently and supersedes it when complete.

## Legacy journal recovery and migration

There is no in-place scientific migration for legacy schema-v1 through
schema-v3 run specs/journal directories. This includes schema-v2 shard sets
produced with shard-local N10 scheduling and schema-v3 scale runs that disabled
incrementality instead of combining fresh per-source backends with deterministic
command isolation.

For a legacy directory:

1. stop the process and preserve the directory read-only for diagnostics;
2. record its run-spec, journal, partition, and manifest checksums;
3. do not pass it to the current merger;
4. freeze the original theorem, repr_v3, upstream-manifest, config, code, and
   benchmark inputs;
5. start a new output directory using the current unary-sharded or N10-global
   profile;
6. complete the new run;
7. invoke merge, which performs the mandatory full exact Lean replay itself.

Individual `ScaleSourceShard` records still carry their record-level
`schema_version: 1`; they are scientifically identified by, and cannot be
detached from, the current hash-bound run spec. Copying those files into a new
run is forbidden.

If a legacy run cannot reproduce because an input or code bundle is missing,
retain it only as an operational/debug artifact. It is not eligible for a
scientific release or training dataset.
