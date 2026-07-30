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
policy. The global N10 output is a separate provisional partition set; a later
dataset-freeze stage may combine it with merged unary output while enforcing
global ancestry caps and connected split components.

## Resume and merge verification

`--resume` always performs exact deterministic replay, including Lean
elaboration and candidate representation, for each persisted source shard.
Receipt-chain hashes detect accidental file corruption but are not evidence
that persisted Lean results remain valid.

`--fast-resume` is retired and rejected. It must not appear in scientific
commands or manifests.

A completed producer shard is not merge-eligible immediately after its first
run. Re-run the identical command with `--resume` after completion. When every
source shard already exists, the materializer:

1. validates the receipt chain;
2. replays every deterministic rule;
3. re-elaborates every accepted candidate through LeanInteract;
4. rebuilds every accepted representation;
5. compares every rebuilt source shard exactly with its persisted journal;
6. writes `full_lean_replay_verification.json`.

The merge command requires and recomputes that receipt for every producer
shard. It then rebuilds attempt, draft, candidate, audit, variant, and pair
lineage from the immutable theorem/repr_v3 inventory. In particular it applies
`check_pair_groups` and rejects missing donor ancestry even when all producer
JSON, hashes, receipts, partitions, and manifests were consistently rewritten.

Merged deterministic variants remain `provisional`; merged pairs remain
unresolved; the merged manifest records `training_eligible=false`.

## Legacy journal recovery and migration

There is no in-place scientific migration for legacy schema-v1 run
specs/journal directories or for schema-v2 shard sets produced with
shard-local N10 scheduling.

For a legacy directory:

1. stop the process and preserve the directory read-only for diagnostics;
2. record its run-spec, journal, partition, and manifest checksums;
3. do not pass it to the current merger;
4. freeze the original theorem, repr_v3, upstream-manifest, config, code, and
   benchmark inputs;
5. start a new output directory using the current unary-sharded or N10-global
   profile;
6. complete the new run and perform the mandatory full exact `--resume`;
7. merge only after the full Lean-replay receipt exists.

Individual `ScaleSourceShard` records still carry their record-level
`schema_version: 1`; they are scientifically identified by, and cannot be
detached from, the current hash-bound run spec. Copying those files into a new
run is forbidden.

If a legacy run cannot reproduce because an input or code bundle is missing,
retain it only as an operational/debug artifact. It is not eligible for a
scientific release or training dataset.
