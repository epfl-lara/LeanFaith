# Post-Gate-3 benchmark representation freeze

**Updated:** 2026-07-23
**Decision:** **PASS**
**Authorization:** LF-016 is authorized.

This report closes the additive benchmark representation-signature and overlap
freeze that remained open after Gate 3. It does not alter or reinterpret the
historical Gate-3 decision. It records the later completion of that gate's
single post-gate prerequisite.

## Frozen scope

The immutable input manifest contains 14,534 statement-side records:

- 7,030 FormalRx-Test candidate statements;
- 3,752 ProofNetVerif reference statements;
- 3,752 ProofNetVerif candidate statements.

These are the two external benchmarks locally resolved in the Revision-4.1
registry. Every unresolved registry key remains evaluation-only and fail-closed
by name until its exact source is pinned. An unavailable representation is
protected/unknown and is never evidence of non-overlap.

## Artifacts

- Input manifest SHA-256:
  `02e3d10b9e7c067ff3183dec8d199274fcca1ad8f7041d2bd8fc5dfb70543b25`.
- Immutable Phase-2 registry SHA-256:
  `f213c1106fe41b0357608101af4d34cbf01e511c4ac54430bcde500eb00e15e4`.
- Active additive registry SHA-256:
  `613441d568b51a231efe653a2622d01e1aa5d7b31eb29d6b6f9ff17bbe30feed`.
- Detailed retrieval index SHA-256:
  `591bcda1031995ef0871bb398b32a00681074d92c39525d878fd3427620757a2`.
- Archived code bundle SHA-256:
  `8b8caafd3bc70810183204b91e1f14f7e54c5dcb0b874cf438e0b46966f15900`.
- Code-tree hash:
  `e965800ceb8d05b565a729a9993a4ff14eb59bab7b4e6a71430f2e0d5722f411`.

The active hash-only registry is repository-resident at
`data/benchmarks/frozen_ids.representations_v1.json`. The 20 MiB detailed index
remains in protected artifact storage; its exact location and hash are bound by
`data/benchmarks/manifests/representation_signatures_v1.json`.

## Accounting

| Measure | Result |
|---|---:|
| Attempted/persisted statement records | 14,534/14,534 |
| Elaborated | 10,686 |
| All indexed views successful | 10,628 |
| Records with explicit failures | 3,906 |
| Explicit failure objects | 3,925 |
| FormalRx representation hashes | 16,373 |
| ProofNetVerif representation hashes | 12,445 |

Failures are persisted records, not missing denominator entries. The dominant
failure is source invalidity (3,848 records); the original Phase-2 ID, NL, and
raw Lean hashes still protect those benchmark items from contamination.

## Validation

- The resumed worker exited with status 0.
- The final artifact schema, retrieval indexes, accounting, record uniqueness,
  and failure references validate through the frozen Revision-4.1 models.
- The final record IDs equal the 14,534-item input manifest exactly.
- The additive registry preserves every Phase-2 identity/text field byte-for-
  byte and changes only representation hashes plus the append-complete flag.
- The full repository test suite passed: 521 tests, zero failures/errors/skips.
- Ruff, Ruff formatting, strict mypy, and the LeanFaith doctor passed.

The historical `reports/gates/gate_3.json` and
`reports/milestones/phase_3_representations.md` are intentionally unchanged:
they correctly record that this prerequisite was pending when Gate 3 closed.
The later machine-readable authorization is
`reports/gates/lf_016_authorization.json`.

## Decision

The benchmark representation-signature and overlap freeze is complete and
active. Gates 2 and 3 remain passed. LF-016 may begin; later generation must
load the hash-verified active registry and fail closed if its manifest or
external detailed index cannot be verified.
