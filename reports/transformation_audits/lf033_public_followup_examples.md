# LF-033 public deterministic follow-up: first-sight examples

This report shows representative `provisional_variant` rows from the completed
public deterministic-v2 follow-up. The source and candidate statements below are
copied exactly from the frozen JSONL artifacts; no statement was reconstructed or
edited for presentation.

> **Status caveat:** these are provisional transformation outputs, not semantic
> labels and not training-eligible data. All seven manifests record
> `promoted_item_count = 0`, `resolved_label_count = 0`, and
> `training_eligible = false`. A positive or negative family name records the
> generator's intention only. In particular, successful Lean elaboration does not
> prove that a proposed negative changed the mathematical claim.

## Frozen artifact provenance

Shared source-theorem partition:

`/localhome/milikic/LeanFaith-verify-76de447-v3/data/scale/lf022_public_v1/extraction/theorems/mathlib.jsonl`

- SHA-256: `7f1a157bfb818b49d082dcc58de221bdddb67f6e8309554395baeb29850838d7`
- Source revision: mathlib `d568c8c09630de097a046763c17b9ea99f95f950`

Candidate-result root:

`/storage/milikic/leanfaith/deterministic_v2/run_e2f071a_followup_public_exploratory_v1`

| Family | Intended operation | Provisional rows | Results artifact SHA-256 |
|---|---|---:|---|
| P14 | Permute independent binders | 1,143 | `32680b4ca6b1618f049279018e933646feab66f86227460ff058181d3438b13b` |
| P15 | Reverse the two sides of a root `↔` | 173 | `01b013dde550a5d145d1812d2457af6ba9dfe20fa1c63e26690b77f69cf6face` |
| N11 | Substitute one same-typed bound variable for another | 233 | `51d81985418ae4304bf197cb78409ac8cfc126b648d2c9ed32afff84b874318e` |
| N12 | Reverse an implication | 419 | `ff3856b27e1118f7aeaaed8949ac2cc3d0454e6f870725a3410f3acf02eff6be` |
| P16 | Reassociate a three-way conjunction | 1 | `09baafcb83bafe5a42edf452a6617b8de2bdb5d7c0685127d6efd1fac52e28f7` |
| N15 | Omit one conjunct | 1 | `0bf25f7ae9ab5522616b4d11cecaacc6a57ea1a297f69b38afa860fc6c39390e` |
| N16 | Remove a bounded-quantifier domain guard | 1 | `7de8d1918a76c859bb4b0ce08013b2bcc616226940bb7a0540fdc113a817fa96` |

Each family artifact is `<candidate-result root>/<lowercase family>/results.jsonl`;
the adjacent `manifest.json` binds the displayed checksum and counts.

## P14 — independent binder permutation

Plain language: reorder two universally quantified inputs when neither input's
type depends on the other. The body keeps referring to the same names. This is
intended to preserve the claim.

### P14 example 1

- Candidate artifact: `p14/results.jsonl:21869`
- Result ID: `v2e2_result:703145eceff198af40fbcb22d7f99e123f233e4a6df7cf209d5c6416d4e9ff1c`
- Variant ID: `var:688fbe4bd8f904ec686b8ce966179a122c7cf8babfea62f1958cd6a46d36a41e`
- Source artifact: shared source partition, line 21869
- Source theorem ID: `thm:4c80e15bcf0e6806e28b8c4fb4abc6eaa3ac1523a7f027a32136fa27936b2825`

Source:

```lean
lemma uIoc_comm (a b : α) : Ι a b = Ι b a := by sorry
```

Candidate:

```lean
lemma uIoc_comm (b a : α) : Ι a b = Ι b a := by sorry
```

### P14 example 2

- Candidate artifact: `p14/results.jsonl:5305`
- Result ID: `v2e2_result:d0527d5eb205155c7be8cd368cd535b15bf5634e45a6fd0ea302d260db034ae3`
- Variant ID: `var:9e18ef5dde457c9192f083d59c27434b3d2828db3b079e085ae6521d1119315c`
- Source artifact: shared source partition, line 5305
- Source theorem ID: `thm:6556974a694e42d723b0ff79364d41855162ae7add8e55a68bccba1eac5d901e`

Source:

```lean
theorem w_nonneg (D : ℝ) (x : E) : 0 ≤ w D x := by sorry
```

Candidate:

```lean
theorem w_nonneg (x : E) (D : ℝ) : 0 ≤ w D x := by sorry
```

### P14 example 3

- Candidate artifact: `p14/results.jsonl:5313`
- Result ID: `v2e2_result:2872914ec205b9d237139325f39a0351accd5e71a126daf107f8530b2270de4b`
- Variant ID: `var:e4efbe03a5082c27953eae0ef63556d80975f51240797eeaf89b97980bac1398`
- Source artifact: shared source partition, line 5313
- Source theorem ID: `thm:aa6a12f084efe11dff59c0cb204be987d2392990147280cf53829cf083569267`

Source:

```lean
theorem y_nonneg (D : ℝ) (x : E) : 0 ≤ y D x := by sorry
```

Candidate:

```lean
theorem y_nonneg (x : E) (D : ℝ) : 0 ≤ y D x := by sorry
```

## P15 — root equivalence reversal

Plain language: exchange the left and right sides of the theorem's outermost
`↔`. Since logical equivalence is symmetric, this is intended to preserve the
claim.

### P15 example 1

- Candidate artifact: `p15/results.jsonl:16927`
- Result ID: `v2e2_result:834b0996e1d55bba9c8b8523883e769350c96b4f3b933dee5165a6b6a9bcf268`
- Variant ID: `var:d97805ffe5c9cc05f159d5cf995f6badc2e6c450242485d8ea14d228e1412847`
- Source artifact: shared source partition, line 16927
- Source theorem ID: `thm:683b658ad43e033a4706b281fa37be941c535f0f45cfd7410c06dffb891438f4`

Source:

```lean
theorem fact_iff {p : Prop} : Fact p ↔ p := by sorry
```

Candidate:

```lean
theorem fact_iff {p : Prop} : p ↔ Fact p := by sorry
```

### P15 example 2

- Candidate artifact: `p15/results.jsonl:13409`
- Result ID: `v2e2_result:cb877f4cdbad5e8343054d4afed33958ac7347c41210d5540b8ba371ffcd1689`
- Variant ID: `var:a23ae6330ee38c0cfc7418ac7503d551641072d7dfdab8b407640abdad71ea57`
- Source artifact: shared source partition, line 13409
- Source theorem ID: `thm:c2114c4460fc9d21eb93c637890bbee7bc60dba11b8f7c01423db5c00577534f`

Source:

```lean
theorem prime_iff {p : ℕ} : p.Prime ↔ _root_.Prime p := by sorry
```

Candidate:

```lean
theorem prime_iff {p : ℕ} : _root_.Prime p ↔ p.Prime := by sorry
```

### P15 example 3

- Candidate artifact: `p15/results.jsonl:16967`
- Result ID: `v2e2_result:cb3359bc62ab7a6284fc4d600b6a09ed19ba71cbf4d95489bcc99093a62e4c43`
- Variant ID: `var:25b22393581173cf2c3370e8d85115f00840b1768f4b2c602782b8a55875aae5`
- Source artifact: shared source partition, line 16967
- Source theorem ID: `thm:9f8dce660104691175a306b2cca507ed9d33a1170f60e89f867d057f42a8eae6`

Source:

```lean
theorem imp_iff_or_not {b a : Prop} : b → a ↔ a ∨ ¬b := by sorry
```

Candidate:

```lean
theorem imp_iff_or_not {b a : Prop} : a ∨ ¬b ↔ b → a := by sorry
```

## N11 — bound-variable substitution

Plain language: replace one use of a bound variable with another variable of the
same Lean type. The result remains well typed but is intended to change the
claim, often by collapsing an asymmetric expression.

### N11 example 1

- Candidate artifact: `n11/results.jsonl:6299`
- Result ID: `v2d0_result:79dd760b7a7d9b13ff0cf0e5bac754d86424b61f8ce9da3910d5d1ca35509aa0`
- Variant ID: `var:08817f63708ac5c5f2f2ca39cfadfccd91731bed0c01e64a4f7f8b6ed4e9da24`
- Source artifact: shared source partition, line 6299
- Source theorem ID: `thm:86852bafd0007f33fafcba586b2c79da1d03f3446d05164d9047af96eaaca326`

Source:

```lean
theorem dist_eq (z w : ℂ) : dist z w = ‖z - w‖ := by sorry
```

Candidate:

```lean
theorem dist_eq (z w : ℂ) : dist z w = ‖w - w‖ := by sorry
```

### N11 example 2

- Candidate artifact: `n11/results.jsonl:16976`
- Result ID: `v2d0_result:0a602485bcc64c2598a8f00b1e303f49f6f46b50bf2789d8513651c096e0684d`
- Variant ID: `var:abb4022fe166e4ab4c5995dda8af0b0e70c900124d7de403479425ee798928ab`
- Source artifact: shared source partition, line 16976
- Source theorem ID: `thm:4d8f30fbd183bc57a4332c8856a75bbe8ce24a9254217eb29f43becbaebb44b3`

Source:

```lean
theorem peirce (a b : Prop) : ((a → b) → a) → a := by sorry
```

Candidate:

```lean
theorem peirce (a b : Prop) : ((b → b) → a) → a := by sorry
```

### N11 example 3

- Candidate artifact: `n11/results.jsonl:16953`
- Result ID: `v2d0_result:0e9a9a5cf0b2076e74810c3a383aca31ce205578b4a4133a7b326558c9f045e1`
- Variant ID: `var:1e181a44fd6f095ed3045d60d9cbe74df66f6376372d81b570df7846edeb59a1`
- Source artifact: shared source partition, line 16953
- Source theorem ID: `thm:ffd18592057c9c08eacb8b24b69884e30e03e60c44708100032e5366ac1a6314`

Source:

```lean
theorem xor_comm (a b : Prop) : Xor a b = Xor b a := by sorry
```

Candidate:

```lean
theorem xor_comm (a b : Prop) : Xor a b = Xor a a := by sorry
```

## N12 — implication converse

Plain language: exchange a theorem's hypothesis and conclusion, changing
`P → Q` into `Q → P`. The converse is type correct but is not generally the same
claim.

### N12 example 1

- Candidate artifact: `n12/results.jsonl:16926`
- Result ID: `v2d0_result:63aaf3cd4b681ee57951aa93c7a201f8f9b5f8457801409866b56709c6ddeeff`
- Variant ID: `var:ea581db223c2542c9927080f42fa163e5eb2b3660481e267b081a8180afcdc01`
- Source artifact: shared source partition, line 16926
- Source theorem ID: `thm:e7fca417d558f06e68d8a882389efeb3ccad4cea6937ffa72921cbb46977c505`

Source:

```lean
theorem Fact.elim {p : Prop} (h : Fact p) : p := by sorry
```

Candidate:

```lean
theorem Fact.elim {p : Prop} (h : p) : Fact p := by sorry
```

### N12 example 2

- Candidate artifact: `n12/results.jsonl:13360`
- Result ID: `v2d0_result:7ebd84124dbc8ee688a70a175c15236452bae48c4882f7d50e7d5b3a31fd5f4a`
- Variant ID: `var:004230eef134ccd36d67ff27530378f9e7510cbafb50db0f5500f13fa985f98d`
- Source artifact: shared source partition, line 13360
- Source theorem ID: `thm:308bb069643810089d695a10a03b2bc76b3cca3e62a177069eb07b05cb63dbbe`

Source:

```lean
theorem Prime.pos {p : ℕ} (pp : Prime p) : 0 < p := by sorry
```

Candidate:

```lean
theorem Prime.pos {p : ℕ} (pp : 0 < p) : Prime p := by sorry
```

### N12 example 3

- Candidate artifact: `n12/results.jsonl:13363`
- Result ID: `v2d0_result:490ff73d73effe5253f19450fb05b879bcb981bfa4b920b874d3f4453f89078f`
- Variant ID: `var:67a80d332c595f8a33646fa1be16ff5dd34af039dfcb85895e83eaf73e9ea4eb`
- Source artifact: shared source partition, line 13363
- Source theorem ID: `thm:2676d4072d5c2b282f1ea589e710f34be4cc55dd2e7647d8e4a2db3fbea451c6`

Source:

```lean
lemma Prime.one_le {p : ℕ} (hp : p.Prime) : 1 ≤ p := by sorry
```

Candidate:

```lean
lemma Prime.one_le {p : ℕ} (hp : 1 ≤ p) : p.Prime := by sorry
```

## P16 — conjunction reassociation

Plain language: change `A ∧ (B ∧ C)` into `(A ∧ B) ∧ C` while keeping the three
conjuncts unchanged. This is intended to preserve the claim. Only one
provisional P16 output was produced in this run.

- Candidate artifact: `p16/results.jsonl:19154`
- Result ID: `v2e2_result:72f7472db2fc4bea110be0826954c81925ddc2c4b0404a983f4b0de87b65149d`
- Variant ID: `var:9f5ced779a1c98ec969b8ae725f7bb93c1b38d42f092eec55de8af8e49a69012`
- Source artifact: shared source partition, line 19154
- Source theorem ID: `thm:e9ba5776fa7139cd04df945ee9829d084fea5d63b9d9cfab24aea9691135d754`

Source:

```lean
private lemma one_lt_re_one_add {x : ℝ} (hx : 0 < x) (y : ℝ) :
    1 < (1 + x : ℂ).re ∧ 1 < (1 + x + I * y).re ∧ 1 < (1 + x + 2 * I * y).re := by sorry
```

Candidate:

```lean
private lemma one_lt_re_one_add {x : ℝ} (hx : 0 < x) (y : ℝ) :
    ((1 < (1 + x : ℂ).re) ∧ (1 < (1 + x + I * y).re)) ∧ (1 < (1 + x + 2 * I * y).re) := by sorry
```

## N15 — conjunct omission

Plain language: remove one side of an `A ∧ B` conclusion. This usually weakens
the statement by dropping a required result. Only one provisional N15 output was
produced in this run.

- Candidate artifact: `n15/results.jsonl:19050`
- Result ID: `v2d0_result:53cee249b74e6a1c1f07b1c52c48f0faff6443685825649708c4df865bf36472`
- Variant ID: `var:52bfac5cb1b000317c4159e1689a482124ef44e9e322f135759cd5f5cf0ab5f1`
- Source artifact: shared source partition, line 19050
- Source theorem ID: `thm:1c5da8cfe537409774e301e2e555c8fd60b679428f328371c45f3856e01529cb`

Source:

```lean
private lemma LSeries.LSeriesSummable_logMul_and_hasDerivAt {f : ℕ → ℂ} {s : ℂ}
    (h : abscissaOfAbsConv f < s.re) :
    LSeriesSummable (logMul f) s ∧ HasDerivAt (LSeries f) (-LSeries (logMul f) s) s := by sorry
```

Candidate:

```lean
private lemma LSeries.LSeriesSummable_logMul_and_hasDerivAt {f : ℕ → ℂ} {s : ℂ}
    (h : abscissaOfAbsConv f < s.re) :
    LSeriesSummable (logMul f) s := by sorry
```

## N16 — domain-guard removal

Plain language: change a bounded quantifier `∀ x ∈ S, P x` into the unbounded
`∀ x, P x`. This normally strengthens the statement by asking for the property
outside `S`. Only one provisional N16 output was produced here, and its guard is
`univ`; that guard is plausibly vacuous. This example is therefore useful
evidence for keeping the generated row provisional rather than automatically
assigning a negative label.

- Candidate artifact: `n16/results.jsonl:7370`
- Result ID: `v2d0_result:81bc0c3b62ec0adf50c6302f7b1af7bf7b8108388841de6a7e98c6321ecb441e`
- Variant ID: `var:ad23b47e912f3d30182bd82ff7e51338f2514cdbc2fbbd4125a5992b804e2185`
- Source artifact: shared source partition, line 7370
- Source theorem ID: `thm:621d7c9b9143a1756c8bee8e8f666043dbc15f2b289dc5caf63f283efe444879`

Source:

```lean
private theorem rexp_neg_deriv_aux :
    ∀ x ∈ univ, HasDerivWithinAt (rexp ∘ Neg.neg) (-rexp (-x)) univ x := by sorry
```

Candidate:

```lean
private theorem rexp_neg_deriv_aux :
    ∀ x, HasDerivWithinAt (rexp ∘ Neg.neg) (-rexp (-x)) univ x := by sorry
```

## Immediate reading

- P14, P15, and P16 provide clear syntactic variations intended to preserve a
  theorem's claim.
- N11 and N12 already provide hundreds of compact, type-correct near misses that
  visibly alter variable use or logical direction.
- N15 demonstrates a useful omission error, but the current surface matcher has
  very low coverage on this public source pool.
- N16 demonstrates why deterministic intent is not itself a label: its sole
  provisional match removed membership in `univ`, which may leave the claim
  unchanged.

These observations describe this frozen exploratory run only; promotion and
semantic resolution remain separate downstream steps.
