# LF-033/LF-034 deterministic-v2 opportunity audit

**Audit date:** 2026-08-11

**Disposition:** read-only exact-family prefilter completed; all scheduled
public follow-up families materialized with LeanInteract, while private
materialization remains queued. No output has label, promotion, or training
credit.

This audit called each implemented family's own site enumerator over the
frozen Gate-3 corpus. It did not generate variants or infer semantic labels.
The Gate-3 corpus contains exactly 5,000 mathlib and 5,000 private
`sft_classic` theorem statements.

Frozen inputs:

| Artifact | SHA-256 |
|---|---|
| theorem partition | `8eb75ffa0b9233c5a91492fa181f604e3c098a6f3970799bcb0406f8b517f09e` |
| representation partition | `cd0a706448a1df2effab245ca175aa918c34f28f3f840ed666361f84397602ae` |

Theorem hits count source statements with at least one prefilter site. Site
counts include all sites before the deterministic seed selects at most one
draft for a source/family execution. Neither count predicts final acceptance:
every selected draft must still elaborate and pass the exact structural,
inverse, fingerprint, atom, and representation audits.

| Family | Mathlib theorem hits | `sft_classic` theorem hits | Total hits | Total sites |
|---|---:|---:|---:|---:|
| P05 resolved names | 2 | 1 | 3 | 3 |
| P08 type ascriptions | 766 | 37 | 803 | 2,064 |
| P06 implicit arguments | 0 | 0 | 0 | 0 |
| P10 constructors | 2 | 0 | 2 | 2 |
| P07 coercion surface | 3 | 0 | 3 | 3 |
| P09 projections | 1 | 0 | 1 | 1 |
| P11 bounded quantifiers | 60 | 20 | 80 | 91 |
| P12 proof-arrow binder | 0 | 0 | 0 | 0 |
| P13 restricted eta | 0 | 0 | 0 | 0 |
| P14 independent binder permutation | 1,995 | 3,030 | 5,025 | 13,644 |
| P15 root-Iff reversal | 8 | 94 | 102 | 102 |
| P16 conjunction reassociation | 0 | 37 | 37 | 37 |
| P17 hypothesis packing | 0 | 1 | 1 | 1 |
| N11 bound-variable substitution | 338 | 1,716 | 2,054 | 28,251 |
| N12 implication converse | 20 | 2,486 | 2,506 | 2,506 |
| N13 witness dependency | 0 | 3 | 3 | 3 |
| N14 negation scope | 0 | 16 | 16 | 16 |
| N15 conjunct omission | 0 | 247 | 247 | 494 |
| N16 domain-guard removal | 0 | 10 | 10 | 10 |
| N17 role-sensitive arguments | 0 | 6 | 6 | 6 |

The private half supplies most of the useful opportunities for P14–P17 and
N11–N17. This is direct evidence for using the already-elaborated private
corpus in deterministic data expansion rather than extrapolating yield from
public mathlib alone.

The surface-only E0/P13/N11 parsers rejected the same 484 source statements
before returning sites because those declarations contain surface constructs
outside the conservative parser contract. These are explicit exclusions, not
accepted or silently dropped pairs. Tree-backed families completed the scan
without enumerator exceptions.

## Completed public follow-up materialization

The public follow-up queue completed all seven scheduled family passes over
the full 27,786-statement public corpus:

```text
/storage/milikic/leanfaith/deterministic_v2/
  run_e2f071a_followup_public_exploratory_v1
```

| Family | Provisional | Quarantined | Invalid | Representation failed | Not applicable |
|---|---:|---:|---:|---:|---:|
| P14 independent binder permutation | 1,143 | 264 | 15 | 19 | 26,345 |
| P15 root-Iff reversal | 173 | 0 | 8 | 0 | 27,605 |
| N11 bound-variable substitution | 233 | 960 | 118 | 0 | 26,475 |
| N12 implication converse | 419 | 12 | 29 | 0 | 27,326 |
| P16 conjunction reassociation | 1 | 0 | 0 | 0 | 27,785 |
| N15 conjunct omission | 1 | 1 | 0 | 0 | 27,784 |
| N16 domain-guard removal | 1 | 0 | 0 | 0 | 27,785 |
| **Total** | **1,971** | **1,237** | **170** | **19** | **191,105** |

The 194,502 terminal rows equal `27,786 x 7`. All 1,971 provisional rows have
unique result, variant, and candidate-theorem IDs. Candidate code hashes have
1,966 unique values. Four candidate-code hash values recur, accounting for
five occurrences beyond their first; all arise from different source
statements and root ancestries. There are zero repeated `(source ancestry,
candidate code hash)` pairs.

The machine-readable binding and exact manifest/result hashes are stored in:

```text
reports/transformation_audits/
  lf033_public_followup_materialization_v1.json
```

Together with LF-032's 270 clean public deterministic-v2 variants, the current
public deterministic-v2 exploratory pool contains **2,241 provisional pairs**.
This is a data-volume result, not a family-promotion or F1-label claim.

## Queued private materialization

After the private v1 deterministic merge, the private portfolio queue runs all
nonzero profiles above, including the E0 surface profile and P17's single
site. P13 remains a mechanically verified zero-yield control. All outputs stay
provisional and must preserve:

```text
resolved_label_count = 0
promoted_item_count = 0
training_eligible = false
```
