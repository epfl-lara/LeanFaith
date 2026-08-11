# Equality expansion and deterministic composition inventory

**Status:** equality scale and exact combination complete; all records remain
provisional and non-trainable

P18 and N18 were run over the frozen 5,000-statement private
`sft_classic` subset and the public 27,786-statement mathlib subset. P18 swaps
the two sides of one exact root equality. N18 changes the polarity of one exact
root equality (`=` to `≠`, or the inverse). Every attempted candidate was
re-elaborated in its source Lean context and passed through the family-specific
structural audit before it could receive the provisional terminal status.

## Equality-family results

| Run | Attempts | Provisional | Quarantined | Invalid | Representation failed | Not applicable |
|---|---:|---:|---:|---:|---:|---:|
| private P18 | 5,000 | 931 | 1 | 1 | 0 | 4,067 |
| private N18 | 5,000 | 960 | 0 | 0 | 0 | 4,040 |
| public P18 | 27,786 | 691 | 51 | 37 | 4 | 27,003 |
| public N18 | 27,786 | 711 | 8 | 68 | 0 | 26,999 |
| **total** | **65,572** | **3,293** | **60** | **106** | **4** | **62,109** |

These are 3,293 new mechanically checked variants, not semantic labels. P18
has a conservative equivalence intention and a family certificate; N18 is a
near-miss intention. Neither family is admitted to training by this audit.

## Clean P12 replay

The combined inventory excludes the historical public P12 root that preserved
two `lean_crash` outcomes. A clean single-worker replay at commit
`0e8d84c409098aa94eb8666a5d82259eeda2cb6a` produced all 27,786 terminal
records with **zero infrastructure failures**: 99 provisional, 103
audit-quarantined, 76 candidate-invalid, and 27,508 not applicable. The strict
combiner therefore remained unchanged.

## Exact combined inventory

The fail-closed combiner revalidated 30 bound materialization roots and
produced:

- **11,208 gross provisional observations**;
- **11,208 unique exact source/candidate pairs**;
- **zero exact duplicate excess**;
- 3,738 mathlib pairs and 7,470 private `sft_classic` pairs;
- zero semantic labels, promotions, training eligibility, evaluation
  eligibility, or gate credit.

The largest positive families are P14 independent binder permutation (2,013)
and P18 equality symmetry (1,622). The largest near-miss families are N12
implication converse (2,897), N11 bound-variable substitution (1,941), and N18
equality polarity (1,671). Smaller families remain in the exact inventory and
are enumerated in the machine-readable companion report.

## Reproducibility

| Artifact | SHA-256 |
|---|---|
| combined manifest | `66245742a30e9fcbf6fdd1c74a3379d130d7269242af76ec56b4d5fa16ed9cae` |
| gross observations | `ef4b075011b90fdceab499b2b9bf6c7721697c7eda1baebc420fc71928346bc8` |
| unique pairs | `974c61308072154714a57afbcb511233d65ad5d93c12cc1aca1fd7eff9a5675e` |
| public P12 clean results | `08e1bd555332148653278d80e20bc5e4815045d644e5ecbdd66d5a01618676c9` |
| private P18 results | `344f37530e1e76c22e0082c602879dcf946a8dac2406e4d7694b84e634dba4d8` |
| public P18 results | `33227f0c13b42f8d598558fd7dd4233001334c24e22dca53eb1d65049ff34c06` |
| private N18 results | `8424dc23f97a117390f1057c9b77fe08478ca356d574e36ce32e8143200b37e5` |
| public N18 results | `6038e8acf1d55534f15d7f68d2cc473685545bcf51662226edf414d4be9c7556` |

The combination hash is
`745a487927f822620d2b632ba861dce2711be493702ddff4bdb54fc309e7ae9a`.
No private theorem text is committed in this report.
