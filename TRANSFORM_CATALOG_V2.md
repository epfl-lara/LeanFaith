# Deterministic Transform Catalog v2 — expansion design

> **Active-plan status (2026-08-30): provisional input, not bulk-generation approval.** The SFT1
> owner must audit this catalog, propose changes, review the preserving/breaking set with the user,
> and record explicit approval in `plans/30_sft1_deterministic.md` before generating the new large
> dataset. Historical family IDs and evidence remain useful; the active task contract supersedes
> this document's former “design of record” wording.
>
> Provenance: produced 2026-08-28 by codex exec (gpt-5.6-sol, high reasoning) as a consultation
> for the refocus plan's Track D, at the owner's request ("be more ambitious with deterministic
> data; expand the set of transforms; look at the literature"). Companion to the archived refocus
> plan at `docs/archive/PLAN-2026-08-30-refocus-v3.md`. New implementation tickets may reference
> family IDs from here but must follow the active SFT1 brief.

## Executive recommendation

Expand from 21 implemented families to roughly **40–45 useful families**, asymmetrically:

- Invest heavily in **universally sound positives**: definitional equality, certified logical
  equivalences, and theorem-backed local rewrites.
- Admit a negative as trusted only if its certificate proves either (1) a separating valuation
  for the exact logical skeleton, (2) a concrete counterexample/refutation, or (3) an explicitly
  defined structural claim-scope change under a documented weaker label contract.
- Move transformation execution into **Lean Meta over typed `Expr`**, using Python for
  orchestration and independent audit. The current surface parser is the main reason several
  ostensibly broad families yield zero.

With 100K–150K represented statements, caps, deduplication, and depth-three composition:
**Committed: 300K retained deterministic pairs. Stretch: 750K** from ~1.0–1.5M mechanically valid
raw candidates. Do not claim "unlimited" useful volume: neutral wrappers and synthetic arithmetic
make raw volume unlimited, but effective independent training signal is not.

## 1. Critical soundness point

A certified syntactic delta is not automatically a certificate of semantic non-equivalence:
`A → B` and `B → A` coincide when `A = B` or in degenerate contexts; dropping a redundant
conjunct preserves meaning; swapping `=`/`≠` under an impossible hypothesis changes nothing;
type-compatible operator substitution guarantees typing, not a changed claim. FaithformBench
treats perturbation invalidity as an assumption audited empirically; FormalAlign's mutation
strategies are benchmark-construction devices, not metatheoretic non-equivalence proofs.

### Evidence classes (label contract)

| Class | Meaning | Can directly label? |
|---|---|---|
| `P-DEF` | Source and candidate whole types are definitionally equal | Preserve |
| `P-SCHEMA` | Candidate is one exact instance of a universally proved logical equivalence | Preserve |
| `P-LEMMA` | One pinned equality/iff theorem applied at one certified occurrence | Preserve |
| `N-SEP` | A structural Boolean/first-order countermodel separates the two abstract schemas | Break (under a declared schema-inequivalence contract) |
| `N-PROOF` | Lean checks a concrete witness/refutation showing the claims differ | Break |
| `F2-DIR` | Only implication/strengthening/weakening/specialization is certified | Directional auxiliary; NOT binary F1 gold |

A failed proof search must never count as `N-PROOF`.

## 2. Required machinery: typed Lean Meta rewrite engine

The existing Expr export (`LeanFaith/Meta/ExprJson.lean`) retains binders/applications/constants/
de Bruijn/projections/lets/literals, but the Python `operator_tree` is untyped structure +
statistics — no per-subterm inferred types, no local-context reconstruction at paths, no
instance/coercion identities, no theorem-rewrite matching. Enough for fixed root patterns, not a
high-coverage catalog.

Build a Lean-side transformation command that: loads the declaration type as `Expr`; walks it
under a reconstructed local context; calls `inferType`, `isProp`, typeclass synthesis, and defeq
in Lean; selects a typed path; constructs the candidate `Expr`; pretty-prints the full candidate
type; and emits a certificate (source/candidate Expr hashes, transformed path, binder-depth map,
instantiated theorem/universe args, synthesized instance hashes, expected relation class). A
separate audit path reconstructs the expected candidate from the re-elaborated source and
compares — the generator is never its own sole witness.

## 3. New positive families (P19–P38)

All compose at any depth provided the final composite certificate is recomputed and intermediate
Expr cycles are excluded. "High" ≈ applicable to 10–20%+ of mathlib-style statements once
implemented over typed Expr.

| ID + mechanics | Class / soundness precondition | Applicability | Difficulty |
|---|---|---|---|
| **P19 Definitional normal-form variation** — reprint after controlled β/δ/ι/ζ/η choice | `P-DEF`: whole type isDefEq, different bytes, no metavars | High | Medium |
| **P20 Local fold/unfold** — unfold one transparent definition or fold exact body back | `P-DEF`: exact constant/universes/implicits/reducibility; whole type defeq | High | Med–hard |
| **P21 β/ζ abstraction** — subterm ↔ `let`; introduce/eliminate one redex | `P-DEF`: capture-free substitution, exact residual hash | High | Medium |
| **P22 Typed η expansion/reduction** — `f` ↔ `fun x => f x` | `P-DEF`: subterm infers to Pi type, fresh binder | Med–high | Medium |
| **P23 Proof-binder currying/uncurrying** — `A → B → C` ↔ `A ∧ B → C` | `P-SCHEMA` (constructive): both domains Prop, exact adjacency, no dependent proof domains | High | Medium |
| **P24 Independent hypothesis permutation** — reorder adjacent proof binders | `P-SCHEMA`: no cross-reference; exact de Bruijn lifting | High | Easy–med |
| **P25 Logical neutral embedding** — `A` ↔ `True ∧ A` etc. | `P-SCHEMA`: exact Prop subtree, approved wrapper only; template cap ≤1% each | High (bridge family, low standalone value) | Easy |
| **P26 Material implication** — `A → B` ↔ `¬A ∨ B` | `P-SCHEMA` (classical; record axiom) | High | Medium |
| **P27 Contrapositive** — `A → B` ↔ `¬B → ¬A` | `P-SCHEMA` (classical) | High/med | Medium |
| **P28 Iff decomposition** — `A ↔ B` ↔ `(A → B) ∧ (B → A)` | `P-SCHEMA` (constructive) | Medium | Easy–med |
| **P29 Negation normal forms** — ¬¬ insertion/removal, De Morgan | `P-SCHEMA` (constructive/classical split recorded) | High/med | Medium |
| **P30 Quantifier/connective distribution** — `∀` over `∧`, `∃` over `∨`, factoring | `P-SCHEMA`: free-var check; `Nonempty` witness where needed | Medium | Hard |
| **P31 Prenex/quantifier motion** — `A → ∀x, B x` ↔ `∀x, A → B x` etc. | `P-SCHEMA`: no free occurrence on prohibited side | Medium | Hard |
| **P32 Theorem-backed AC rewriting** — commute/reassociate `+ * ∧ ∨ ∪ ∩ max…` | `P-LEMMA`: bind exact theorem (e.g. `add_comm`), match all implicit/instance args, one occurrence | High | Med–hard |
| **P33 Equality-hypothesis substitution** — under `h : a = b`, rewrite one later site | `P-LEMMA`: local equality binder, exact occurrence + transport path | Med–high | Hard |
| **P34 Whitelisted local simp rewrite** — one pinned simp theorem at one occurrence | `P-LEMMA`: `simp only [lemma]`, no context/recursion/side-condition discharge; curated 100–500 lemma bank | High | Hard |
| **P35 Membership normal forms** — `x ∈ s ∩ t` etc. expand/fold | `P-LEMMA`: exact Set/Finset constants + pinned iff theorem | Med–high | Med–hard |
| **P36 Extensional equality expansion** — funext/set-ext/structure-ext | `P-LEMMA`: exact extensionality theorem, complete field coverage | Medium (very hard positives) | Hard |
| **P37 Coercion/instance normalization** — expose/fold coercion chains | `P-DEF`/`P-LEMMA` only: same synthesized instance hash or Subsingleton proof | Med–high | Hard |
| **P38 Existential/subtype packaging** — `∃ x, P x` ↔ `Nonempty {x // P x}` | `P-SCHEMA` (constructive): exact universe/dependent alignment | Low–med | Hard |

Notes: P25 is a composition bridge, not corpus bulk. P34 is NOT "run full simp" — single
theorem-backed rewrite with exact trace only.

## 4. New negative families (N19–N28)

| ID + mechanics | Class / soundness precondition | Applicability | Difficulty |
|---|---|---|---|
| **N19 Whole-claim negation** — closed theorem `A` → `¬A` | `N-PROOF`: source proof of `A` + exact `Not A` = contradictory in pinned env | High (every admitted theorem) | Easy–med |
| **N20 Certified false side condition** — `A` → `A ∧ F`, `F` pinned refutable | `N-PROOF`: kernel proof of `¬F`; cap heavily (learnable pattern) | High by construction | Easy |
| **N21 Boolean-skeleton polarity mutation** — negate one influencing atom | `N-SEP`: truth-table/SAT separating valuation; atom must influence root | High | Medium |
| **N22 Connective replacement** — `∧↔∨`, `→↔↔`, edge insert/remove | `N-SEP`: exact skeleton, distinct atoms, concrete separator | Med–high | Medium |
| **N23 Quantifier scope specialization/generalization** | `F2-DIR` default (NOT binary gold) | High/med | Med–hard |
| **N24 Hypothesis strengthening/weakening** | `F2-DIR` default; `N-SEP` only with separator + non-vacuity | High | Medium |
| **N25 Law-certified incompatible relation** — `<`→reverse-`≤`, Eq→Ne with law | `N-PROOF` + anti-vacuity: asymmetry/irreflexivity theorem + inhabitants for all outer binders | Low–med | Hard |
| **N26 Witnessed numeric/bound mutation** — perturb literal/exponent/bound + witness | `N-PROOF`: `norm_num`/`omega`/`decide`/explicit witness refutes candidate | Medium | Med–hard |
| **N27 Type/domain drift with witness** — `Nat↔Int`, Set↔Finset + separating element | `N-PROOF`: explicit embeddings + concrete separator | Low–med | Hard |
| **N28 Proof-producing finite templates** — true theorem + refuted near-miss | `N-PROOF`: stored proof + refutation, no sorry, axiom audit; template caps mandatory | Synthetic-high | Medium |

**Not trusted negatives by design alone** (silver/F2 pool unless a separator is produced):
arbitrary nearby-theorem splice, same-typed constant substitution, theorem specialization,
hypothesis deletion, conjunct omission, typeclass-instance substitution, failed proof search,
"different operator trees". ACE-Dataset: replay its claimed certificates under the pinned
toolchain before admission.

## 5. Top 10 by training value

1. P34 whitelisted local rewrite · 2. P20 fold/unfold · 3. P33 equality-hypothesis substitution ·
4. P23 currying/uncurrying · 5. P36 extensionality · 6. P27 contrapositive · 7. P30/P31
quantifier motion · 8. P32 AC rewrites · 9. N21 polarity mutation · 10. N26 witnessed numeric
mutation. (Runners-up: P26, P35, N24-with-separator, N27. P19/P25 are high-yield infrastructure
but shortcut-prone — diversify + cap.)

## 6. Volume plan (over 100–150K source statements)

Raw plausible inventory ~0.8–1.5M candidates; **post-cap retained: 300K committed / 750K
stretch**. Committed 300K mix: definitional+binder positives 75K; classical/logical positives
55K; mathlib theorem-backed positives 35K; structural-separator negatives 65K; proof-certified
negatives 35K; proof-producing synthetic 30K; cleaned legacy 5K → **≈55% positive / 45%
negative**. Depth mix 45/35/20 (d1/d2/d3), max one negative hop, final certificate recomputed
after composition, cycle check against EVERY prior path state (the v1 cycle bug missed 464
returns).

Caps: family ≤8%; mechanism superclass ≤15%; exact template ≤2% (neutral wrappers ≤1%); exact
rewrite lemma ≤0.25–0.5%; source ancestry ≤4 direct + 4 composed; synthetic template × numeric
range ≤0.5%; synthetic sources overall ≤15% committed / ≤20% stretch; P01 with `lf_alpha_*` = 0%
until regenerated markerless. Swapped orientation is a training-time augmentation, not a new
pair. Enforce bins for token overlap, length ratio, GTED distance, family, depth, domain. Stop
stretch expansion if lexical-only balanced accuracy rises or ancestry-level validation gains
flatten.

## 7. Build order

0. **Typed Lean Meta transformation/audit bridge (prerequisite for everything below).**
1. P23 currying/uncurrying → 2. P24 hypothesis permutation → 3. P20 fold/unfold → 4. P21 β/ζ →
5. P32 AC rewrites → 6. P26+P27 → 7. P34 whitelisted rewrite → 8. N21/N22 separator negatives.
Only after a 5K–10K source pilot: P30/P31, N25/N27.

## 8. Current 21 families: keep/rework/deprecate

- **Quarantine/regenerate:** P01 (lf_alpha leak).
- **Keep:** P02, P04 (cap; grow table via elaborator), P11 (generalize), P14 (split data- vs
  proof-binders vs P24), P15/P16/P18 (generalize root-only → certified nested occurrence).
- **Supersede:** P12 & P17 → P23; P13 → P22; P05–P10 surface parsers → Meta implementations.
- **Demote current negatives** until upgraded: N01/N02 (need separator), N03 (F2-DIR default),
  N07 → N26, N10 → hard-candidate pool only, N11 (require influence/separator), N12 (N-SEP with
  distinct atoms), N13–N17 (replace with Meta + separator/witness), N18 (explicit evidence tier).

## 9. Failure risks → cheap guards

Structural delta ≠ semantic break → evidence-class enum, D0 never labels directly. Irrelevant
atom mutation → Boolean influence test + separator. Binder capture → Lean-side transform is the
authority, never Python index arithmetic. Empty domains → require `Nonempty` witness. Classical
in constructive stratum → record axioms, split mechanism IDs. Instance drift → identical instance
hash or Subsingleton proof. simp erasing claims → `simp only [one_lemma]` only. Lemma drift →
pin env revision + theorem type hash. Composition cancelling a mutation → fingerprint check vs
all prior states + recomputed certificate. Negative-then-positive losing its separator →
transport/regenerate separator on the final pair. Truth via sorry → kernel-checked proofs +
axiom audit. Marker shortcuts → ordinary binder names + lexical canary. Wrapper flooding →
caps + near-dup clustering. Imported corpora overclaiming → replay certificates. Cross-split
leakage through descendants → union-find all root ancestries before splitting.

## Bottom line

The route to "as much deterministic data as we want" is: (1) a typed Lean Meta rewrite engine;
(2) 15–20 high-coverage positive metamorphic relations; (3) theorem-backed local mathlib
rewrites; (4) a formally explicit boundary between separator-certified negatives, proof-certified
negatives, and directional/silver mutations; (5) strict diversity caps and ancestry-aware
composition. That supports **300K committed / 750K stretch**. Going beyond is mechanically easy
but should be justified by unique structure and held-out-family gains, not raw candidate count.
