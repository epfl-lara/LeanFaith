# LF-033 public all-family provisional inventory

**Audit date:** 2026-08-11

**Disposition:** complete exploratory materialization; provisional candidates only

The clean public inventory now contains **2,237 exact source-candidate groups**
from the frozen 27,786-record mathlib source. All 2,237 observations have a
distinct exact pair key. This is exact code-level grouping, not a claim that
all candidates are semantically distinct.

The fresh N11 replay completed all 27,786 attempts and added 233 provisional
bound-variable-substitution candidates. It reported 961 audit-quarantined, 117
Lean-invalid, and 26,475 non-applicable attempts. No infrastructure failure,
resolved semantic label, promotion, or training eligibility was present.

## Duplicate-root correction

The first relocation-stable combination failed closed because the input list
contained byte-identical reruns for N15 and N16. The two duplicate directory
trees had identical run specifications, manifests, results, journals, and
relocation-stable root-binding identities. The corrected inventory retains one
canonical root for each family.

This correction changed the pre-N11 gross count from 2,006 to 2,004 while
preserving all 2,004 exact pair keys. After adding N11, the final inventory has
2,237 gross observations and 2,237 exact pair groups.

| Artifact | Verified value |
|---|---|
| final combination hash | `fbe939a86a275aa413fbe602aec6fd93e7f324613631d0f0851a2668c7bcc4f0` |
| final manifest SHA-256 | `cd2e5322976ed2c7518cec1c95f1fb4bd24038db281b4c4ef2ebf8c0d3b071a8` |
| final gross-output SHA-256 | `31660fea716039ec0831a8d50256feb7817776ee4330ef32f9a6970c2981f2f7` |
| final unique-output SHA-256 | `8cd557a53cc035ff9a81f47618dcf6c09170be705b3db2b1ef321250e2d76829` |
| N11 manifest SHA-256 | `107f4592e8bf3177dc7fc17cb2e200a386f66202301ec67696b2506d713932b8` |
| N11 results SHA-256 | `836fa8285cc13e28fe7ddd6d97d849766c89ed74c97d2606e7a0aaeb4d316d68` |
| resolved labels / promoted items / training eligibility | `0 / 0 / false` |

## Family counts

| Family | Provisional observations |
|---|---:|
| P14 independent binder permutation | 1,143 |
| N12 implication converse | 419 |
| P11 bounded quantifiers | 264 |
| N11 bound-variable substitution | 233 |
| P15 root iff reversal | 173 |
| P09 projections | 2 |
| N15 conjunct omission | 1 |
| N16 domain-guard removal | 1 |
| P16 conjunction reassociation | 1 |

The machine-readable companion report records all source, output, launcher,
overlap, and correction hashes. It contains no theorem text or raw source
content: `lf033_public_all_clean_inventory_v1.json`.
