# Phase 0 — Contract, sources, providers, environment (Revision 4.1)

**Updated:** 2026-07-18
**Decision:** **PASS — internal-research scope only**

This gate does not grant redistribution, external-provider transmission, or
release permission for the private `sft_classic` source.

## Locked decisions and evidence

1. LeanInteract 0.11.4 remains the only production Python–Lean boundary.
   The environment lock pins Lean/mathlib `v4.31.0-rc1` inside LeanInteract's
   advertised range, and gate-facing commands verify the actual checkout
   toolchain and Git revision rather than trusting registry text.
2. `formalmathatepfl/sft_classic` access is verified at revision
   `0bf9f424309f668c2c2dd214aef6ec5d1d5c042f` with 3,036,270 rows and the
   archived 100-row input hash
   `9913ae837d021d6e9857659346fe47088762c3ab19dc378551e77a5bc0be38cd`.
3. Private-source authorization is fail closed:

   ```text
   access_basis = authenticated private project access
   institutional_policy_status = internal research only; release permission pending
   license_status = undeclared
   redistribution = false
   external_transmission = false
   release_eligibility = false
   ```

4. `sft_classic` content may not be sent to external providers. All unresolved
   external provider slots are explicitly disabled until the Phase-5 ADR.
5. `configs/sources/public_replication.yaml` defines a public-source replication
   profile so the scientific pipeline is not release-dependent on private data.
6. The verified private probe is canonical; the stale top-level unresolved
   probe state has been removed.
7. F0/F1/F2, terminal relations, unresolved-review behavior, evidence tiers,
   benchmark isolation, split grouping, calibration, and preregistered
   hypotheses are versioned policies. Revision 4.1 migration readers preserve
   legacy records while all new writers use schema version 2.
8. FormalRx paper, dataset input revision, and available 1.7B/4B/8B checkpoint
   revisions are pinned. FormalRx labels are currently withheld and its model
   card is incomplete; artifact availability does not block the core project.
9. Backbone selection is a preregistered four-candidate pilot with no hard
   parameter ceiling. ModernBERT-base is only an implementation smoke fallback.
10. No staffing, schedule, compensation, budget, or prescribed hardware
    policy is introduced.

## 2026-07-18 input revalidation

The internal-only authorization was revalidated after the Gate-2/3 source and
benchmark work. In particular, the gate now binds both sides of the benchmark
registry contract and the canonical private-source config:

| Input | SHA-256 |
|---|---|
| `configs/benchmarks/registry.yaml` | `ddc26730a647d75c5cf39052aa45d99cabc840dd84d774cb9e32cf3d116e929b` |
| `policies/benchmark_denylist_v1.yaml` | `2b40201771d2d09b0a34bf193c63bc51a64af0c6a7bdb0921710a6cf531804d4` |
| `data/benchmarks/frozen_ids.json` | `f213c1106fe41b0357608101af4d34cbf01e511c4ac54430bcde500eb00e15e4` |
| `configs/sources/sft_classic.yaml` | `a98d0f9fe422a0766d77d95e0355f066976ab52604693bd8f9f44484d558ba63` |

The source config records `probe_status: verified_private`, the exact pinned
revision, undeclared license, and fail-closed release/transmission flags. The
provider registry still disables every unresolved external slot. FormalRx
artifact availability remains non-blocking for the primary Lean--Lean work.

## Provider boundary

The provider registry is resolved for Gate 0 by explicit disablement, not by
guessing model IDs. Phase 5 may later enable approved public-source or locally
served generation slots through its ADR. No later provider ADR may transmit
private `sft_classic` content unless a separate source-authorization record
supersedes the present prohibition.

## Consequences

- Internal Gate-2/3 processing may proceed.
- Public release artifacts must be reconstructable without private rows.
- External generation remains blocked until its separate Phase-5 decision.
- Gate 1 was rerun on 2026-07-18 after the current Revision 4.1 and Gate-2/3
  implementation changes; its independent report records the result.
- LF-016 remains blocked until Gates 2 and 3 close.

This report is finalized before its hash is written into
`reports/gates/gate_0.json`; any change requires regenerating that gate report.
