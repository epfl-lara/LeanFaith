# Deterministic V2 schema-3 13-family smoke receipt

Captured at `2026-08-11T16:50:16Z` from the completed orchestration receipt at:

`/storage/milikic/leanfaith/deterministic_v2/composition_second_hops/chain_fd196dd_schema3_v1/smoke_w1/orchestration/receipt.json`

This is a point-in-time, audit-only report. It creates no semantic labels,
promotions, training eligibility, evaluation eligibility, or gate credit.

## Outcome

The smoke receipt is complete and internally consistent. All 13 registered
composition families are accepted schema-3 roots. Every family has exactly 64
terminal results, for 832 results total. The aggregate contains:

| Terminal status | Count |
|---|---:|
| `provisional_variant` | 145 |
| `audit_quarantined` | 17 |
| `candidate_invalid` | 1 |
| `not_applicable` | 669 |
| `candidate_infrastructure_error` | 0 |

All roots have zero resolved labels, zero promoted items, and
`training_eligible=false`.

The launcher is bound to commit
`f21fe4523ddf6ca872c96a6273aa43d6fd5b7f7f`. All 13 roots were explicitly
reused and carry producer-commit attestation
`fd196dd3ef9cfce490151632b7121c69e62567b4`. The distinction is intentional:
the producer commit created the roots, while the later launcher commit
validated and receipted them.

## Orchestration binding

| Artifact | SHA-256 |
|---|---|
| `launch_spec.json` | `8d26046aab658e29c4b16ddeb0334eed1387832375c31bef0cf2b3bac77a51aa` |
| `status.json` | `d4973637a48b81656bbe42c172818851a74b5a3838d3d2530bc3ef9244a9eedf` |
| `receipt.json` | `ec475deddc7a4205b5065e480b9b90954f976639a01826b94fe4930a39af7f4d` |

Launch ID:
`detcomp_smoke_launch:1dd440e4299507a92e7c48792bddf06cb996f2f2e520462d4992a0f603381e10`

Receipt ID:
`detcomp_smoke_receipt:35106ee991d53fc22a63a388ac433887c5134fe9ba527ebaa36f745d52da7699`

The launch specification, final status, and receipt all parse as canonical
launcher artifacts. The receipt's launch-spec and final-status hashes match
the live files, and its self-derived identity validates. All status entries are
`reused`, have exit code zero, and have no recorded error.

## Per-family results

| Family | Provisional | Other terminal results | Root tree hash |
|---|---:|---|---|
| P14 | 32 | 11 audit-quarantined; 21 not-applicable | `46d72515181906ae3fdd4f120e6416a8dab207b059c1382c460d85ee538486c0` |
| P18 | 31 | 33 not-applicable | `f9e6cc53752180d446c9517d4534925f6b3cb7bdf736f50a3af4414368029c7a` |
| N18 | 31 | 33 not-applicable | `286a48b51c0bc5586486cbb357935053144d49e88e9169afdc1cffc27cf1284b` |
| N11 | 24 | 6 audit-quarantined; 1 invalid; 33 not-applicable | `1004f8c12acb4250fdddfc9d6d76de5f5ad12f8cdb4a38a02f11e6768b128215` |
| N12 | 23 | 41 not-applicable | `6c3cae404ef48750b74d28555503328fff9e9b86e527442aa384d034cd5478c4` |
| P15 | 4 | 60 not-applicable | `f41d5ec05954652e9b7df2832f14f24160c862964c68b755eb14acf133299fce` |
| P16 | 0 | 64 not-applicable | `78f713fdc04136592a42de011d22e5caaec156adbdf674918b6f76e0f147353e` |
| P17 | 0 | 64 not-applicable | `7533d9f770b6278682b77e3c1de4a374114a42b9f9c3e173c811e6b1ac2efcbe` |
| N13 | 0 | 64 not-applicable | `a15386fa222c8053ea60194b77e071ae74aa44fd7c02d8f29280b9f9dd052f79` |
| N14 | 0 | 64 not-applicable | `76d039b4689ed698fd9426e700d4a75e086128d65215767f1fb4094034a39d7c` |
| N15 | 0 | 64 not-applicable | `30b326b4388e428d29a08f2283543ce745154084610879173de8e1e319854f4d` |
| N16 | 0 | 64 not-applicable | `d2c9e801990ba9ea733eacec5f019dcbc4fe0419376baf9538fba28cb7665fa1` |
| N17 | 0 | 64 not-applicable | `bdc279bcd28a1a9f7766e45197dd09b36a5eb4b6241cfebaa2b41336468e77dd` |

The machine-readable companion report binds every root's path, profile, run
kind, root-binding ID, run-spec hash, manifest hash, results hash, launcher-log
hash, root-tree hash, and terminal-status counts.

## Validation performed

The live report check:

- parsed the launch specification, status, and receipt through their strict
  schema models and verified canonical byte serialization;
- recomputed and matched the launch-spec, status, and receipt file hashes;
- re-ran the normal root loader and run-model loader on every family;
- required schema version 3, exactly 64 results, and zero infrastructure
  errors for every root;
- recomputed every root receipt, including run-spec, manifest, results, log,
  root-binding, and root-tree hashes, and compared it with the signed receipt;
- independently summed all family result and status counts.

## Excluded diagnostics

These paths are intentionally not part of this accepted smoke receipt:

- bounded-memory worker 1:
  `/storage/milikic/leanfaith/deterministic_v2/composition_second_hops/chain_fd196dd_schema3_v1/smoke_parity/p14_w1`;
- bounded-memory worker 4:
  `/storage/milikic/leanfaith/deterministic_v2/composition_second_hops/chain_fd196dd_schema3_v1/smoke_parity/p14_w4`;
- incomplete unlimited worker 4:
  `/storage/milikic/leanfaith/deterministic_v2/composition_second_hops/chain_fd196dd_schema3_v1/smoke_parity_nomem/p14_w4`.

The first two contain bounded-memory infrastructure failures, and the third did
not publish a complete root. They are separately bound and explained by
`reports/generation/deterministic_v2_p14_recovery_resource_parity_point_in_time_v1.{json,md}`.
