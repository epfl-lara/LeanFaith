# Deterministic five-family third-hop audit (v2 correction)

Date: 2026-08-12

## Supersession notice

The original v1 interpretation is superseded. Its theorem-ID cycle check did
not detect returns to the depth-one intermediate alpha identity when the final
theorem had a new provenance-derived ID. Reproduction against the exact seed
inventory and depth-two chain receipt found 464 such path-level cycles among
the records v1 had admitted: P14 219, P15 29, P16 8, P17 0, and P18 208.

The corrected v2 audit binds those exact seed and chain inputs, expands every
provisional third-hop result into one record per exact parent chain, compares
the final alpha identity with the complete prior path history (original,
depth-one, and depth-two), and quarantines cycles before source/final-alpha
deduplication. The immutable v1 bytes remain preserved for audit history, but
its 4,281-pair count must not be used as the current result.

## Corrected result

All five registered E2 positive transformation families completed over the
exact 5,538-record depth-two frontier. The materializers produced 4,368
provisional third-hop results:

| Third-hop family | Provisional results | Other terminal outcomes |
|---|---:|---|
| P14 independent binder permutation | 2,212 | 1,728 not applicable; 1,589 audit quarantined; 9 invalid |
| P15 root `Iff` reversal | 120 | 5,418 not applicable |
| P16 conjunction reassociation | 62 | 5,476 not applicable |
| P17 hypothesis packing | 1 | 5,537 not applicable |
| P18 root equality symmetry | 1,973 | 3,560 not applicable; 5 invalid |

The corrected audit expanded the 4,368 provisional results across their exact
parent paths into 4,581 depth-three lineage records. It produced:

| Third-hop family | Expanded lineages | Cycle quarantines | Admitted lineages |
|---|---:|---:|---:|
| P14 independent binder permutation | 2,306 | 219 | 2,087 |
| P15 root `Iff` reversal | 139 | 29 | 110 |
| P16 conjunction reassociation | 64 | 8 | 56 |
| P17 hypothesis packing | 1 | 0 | 1 |
| P18 root equality symmetry | 2,071 | 208 | 1,863 |

| Audited output | Count |
|---|---:|
| provisional third-hop results | 4,368 |
| gross expanded path lineages | 4,581 |
| intermediate-alpha cycle quarantines | 464 |
| admitted noncyclic path lineages | 4,117 |
| unique source/final-alpha pairs | 4,031 |
| duplicate excess admitted lineages | 86 |
| equivalent-candidate pairs | 613 |
| near-miss-candidate pairs | 3,418 |

The audit output is:

```text
/storage/milikic/leanfaith/deterministic_v2/
  composition_third_hop_audits/frontier_084859ee_five_families_v2
```

Its set ID is:

```text
detcomp_depth3_set:e65fe7f1c9a1b14fd3d1dfdd211e3a891b3f568cafb8533b57e9585251b5b75d
```

The manifest file SHA-256 is
`7a0a07f1abdbf28c74a5fd07aa7fb392e18e8ffcc47d40f530398f659ec51fc7`.
The expanded-chain partition SHA-256 is
`c65f5b84fb3adc446128261607f5d8234ae15d819ef2171aee70a8711d0a0dd6`.
The unique-pair partition SHA-256 is
`1156d72c2077f210099e61e00dd18803a99922398a5ca49577be2db3856fd37c`.
The quarantine partition SHA-256 is
`8c7edbf2b8378cc774c7332fd19fa33a4e49b0cba3a7ebdbaa79fcd7dc8b59a1`.

For historical verification, the preserved v1 manifest and unique-pair
partition still hash to
`1b8dfff97372649d5ab8c58897e86b9c8a1c6007ed4d607e0566f0e2e3e525af`
and
`d6adeca9a732547643faea9f21ff703116d1a199466acb109581a421b26c4dac`,
respectively.

## Replay

An independent second invocation re-read every bound input and reproduced the
same v2 set ID, 4,581 expanded-lineage count, 464-cycle quarantine, 4,117
admitted-lineage count, 4,031 unique-pair count, and output bytes. It reported
`status=replayed` with exit status zero.

## Scope

These are mechanically grounded, provenance-audited, intention-only
provisional pairs. The third hop is certificate-backed as a local E2
transformation, and the audit
preserves whether the depth-two parent was an equivalent candidate or a
near-miss candidate. The artifact does not promote either intention to an F1
semantic label. It records zero resolved labels, zero promotions, and:

```text
training_eligible = false
evaluation_eligible = false
gate_credit = false
```
