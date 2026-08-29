# Consultation request: is ModernBERT the right encoder backbone for this system?

You are advising on a concrete engineering/research decision. I need a rigorous,
opinionated analysis — not a survey. Everything you need is inline below; you have no
access to my filesystem, so do not ask for files. Where a fact matters and you are not
sure of it (e.g. a model's exact context length, tokenizer, or licence), say so
explicitly and mark it as "verify before acting" rather than guessing silently. Your
training cutoff may be behind the current model landscape; call out where a newer
model might have superseded your recommendation and tell me what to check.

---

## 1. The system

**Product goal.** "LeanFaith-v1": a *lightweight* Lean 4 ↔ Lean 4 **semantic-consistency
classifier**. Input: a pair of Lean 4 theorem *statements* (reference, candidate).
Output: binary same-claim yes/no + a calibrated confidence. It must be materially
cheaper and more transparent than calling an LLM judge, and it must be usable to score
hundreds of thousands of pairs in batch (e.g. filtering/auditing autoformalization
output and synthetic-transform corpora).

The relation being learned is **semantic equivalence of mathematical claims under
syntactic and logical rewriting**, not surface similarity. Positives include
definitional-equality reprints, β/η/ζ normalisations, currying/uncurrying of hypotheses,
hypothesis permutation, contraposition, De Morgan/NNF, iff decomposition, quantifier
motion/prenexing, theorem-backed AC rewrites (`add_comm` etc.), single pinned simp
rewrites, coercion/instance normalisation, membership normal forms, extensionality
expansion. Negatives include quantifier-order swaps, direction flips (`→` reversal),
strict/non-strict boundary changes, dropped or added hypotheses, constant/operator
substitutions, and generally cases where a *countermodel or Lean-checked refutation*
certifies the two statements differ.

The discriminative signal is therefore frequently **small, local, and structural**: one
flipped quantifier order, one `≤` → `<`, one missing hypothesis, one changed implicit
argument — inside two long, highly-similar token sequences. Lexical overlap between a
positive pair and a hard negative pair is nearly identical. That is the core modelling
difficulty.

**Current architecture (the thing you are evaluating a backbone for).**

- Single encoder, **packed cross-encoder**, one forward pass per pair.
- Input view (call it `tagged_headless_pair_v1`), literally:

```
[REFERENCE]
<lean statement A, "headless" form: binders + hypotheses + conclusion, no proof>
[CANDIDATE]
<lean statement B>
```

  `[REFERENCE]` / `[CANDIDATE]` / `[HEADLESS]` are **ordinary multi-piece strings, not
  added vocab items** — the tokenizer/vocab is deliberately kept unmodified (currently
  50,368 entries, no resize).
- Pooling: **attention-masked mean** over the whole packed sequence (not `[CLS]`).
- Head: single linear logit → BCE / cross-entropy. No auxiliary heads at v1.
- Sequence budget: **1024 tokens**, frozen. Empirically, on a 17,031-pair corpus only
  **35 packed pairs (0.2%)** exceeded 1024 tokens with the ModernBERT tokenizer. A 512
  budget was considered but rejected because it would truncate binder/hypothesis sets.
- The task is **symmetric** (same-claim(A,B) = same-claim(B,A)) but the packed input is
  **directional**; the plan is to train with swapped orientations and/or average both
  directional logits, and to report swap disagreement as a metric.

**Text characteristics.** Lean 4 / Mathlib surface syntax. Heavy Unicode
(`∀ ∃ ¬ ∧ ∨ → ↔ ≤ ≥ ≠ ∈ ∉ ⊆ ⊂ λ Σ Π × ℕ ℤ ℚ ℝ ℂ`), dotted qualified constants
(`Nat.succ_le_of_lt`, `MeasureTheory.integral_add`), typeclass binders
(`[Group G] [Fintype α]`), implicit/instance-implicit braces, numeric literals, and
long identifier names that BPE fragments aggressively. Roughly half the training
statements come from Mathlib, half from competition-style formalisations
(miniF2F/ProofNet/Numina lineage).

## 2. What has already been decided, built, and measured

- Backbone in use today: **`answerdotai/ModernBERT-base`**, pinned revision
  `8949b909…`, `local_files_only`, loaded via HuggingFace `transformers`, bf16.
- A **preregistered backbone-selection pilot exists on paper but was never executed.**
  Its frozen candidate registry is exactly: ModernBERT-base (declared "smoke fallback
  only"), ModernBERT-large, `Salesforce/codet5p-220m` (encoder branch only),
  `microsoft/deberta-v3-large`. Its eligibility rule: at a 1024-token budget a model is
  eligible only if its *released* architecture supports that length **without a
  positional-architecture modification** — which, if enforced literally, eliminates the
  512-token candidates (CodeT5+ 220m, DeBERTa-v3-large) outright. ModernBERT-base/large
  are the only registry entries that natively reach 1024 (native 8192).
- The registry's declared notion of "lightweight" is **not a parameter ceiling**: it is
  (a) non-autoregressive, and (b) a measured quality/latency/memory/throughput Pareto.
  I want you to tell me whether constraint (a) is still worth keeping.
- Ablation results on a 17k-pair machine-proxy corpus (proxy labels, ancestry-disjoint
  splits, 26.4% positive — *these are not human-gold numbers*), all on ModernBERT-base:
  - **M1 packed cross-encoder** (the architecture above): test pseudo-AUPRC **0.887**,
    balanced accuracy **0.859**.
  - M0 dual encoder (bi-encoder, cosine): pseudo-AUPRC 0.603.
  - M2 bidirectional matcher: 0.684.
  So joint token interaction dominates; a bi-encoder is not competitive on this task.
- Planned training pipeline (all still ahead of me, nothing locked):
  - **S0**: MLM continued pre-training of the encoder on a Lean corpus — **469,585 rows**
    of Lean source plus ~33K compiled `theorem … := by …` statement↔proof records and
    those theorems' rendered headless signatures (so the packed-view markers are
    in-distribution). 1024 ctx, bf16, ~1–3 epochs.
  - **S1**: large-scale supervised fine-tuning of the cross-encoder on
    **300K committed / 750K stretch** deterministically-generated, certificate-carrying
    transform pairs (≈55/45 pos/neg), with family/mechanism holdout splits.
  - **S2**: refinement on 20–50K LLM-generated + verified pairs, with S1 replay.
  - **S3**: evaluation on human-labelled gold: EPLA/ASSESS (1,247 pairs), GTED (298),
    BEq human equivalence (200), ProofNetVerif (3,752 rows / 361 problems, weak). A
    small `golden_train` (~1.2–1.8K pairs) is available for an optional fine-tune;
    `final_test` is sealed and opens once.
- **Success bar.** Published reference points on these benchmarks: GTED metric ≈0.66–0.70
  accuracy; majority-vote-8 LLM judge ≈0.70 accuracy. My "done" threshold is ≥0.72
  balanced accuracy fine-tuned, ECE ≤0.08 after calibration, and ≥0.70 balanced accuracy
  in the *weak-supervision-only zero-shot* track as the headline result.

## 3. Compute and engineering constraints

- Local: one RTX 4090 (24 GB) for iteration, smokes, and probably for deployment-scale
  batch inference.
- Cluster: A100 / H100 / H200 available on demand. **Training compute is not scarce**;
  multi-seed and parallel ablations are affordable. Inference cost and simplicity matter
  much more than training cost.
- Stack: PyTorch + HuggingFace `transformers`, pinned revisions, `trust_remote_code`
  currently **false** (a candidate requiring custom modelling code is a real friction
  cost, not a blocker).
- I would like the final artefact to be releasable, so **licence** matters
  (permissive strongly preferred).
- One epoch of S0 CPT plus one S1 run over 300–750K pairs at 1024 tokens has to be
  practical; a 7B-parameter backbone at 750K training pairs is probably out of scope for
  the *iteration* loop even if the cluster could take it — say so if you disagree.

## 4. What I am asking you

**Primary question: is ModernBERT (base, and separately large) the best available
encoder backbone for this architecture and this task — and if not, what is?**

Please cover, concretely:

1. **ModernBERT's fit, mechanism by mechanism.** Does anything in its design help or
   *hurt* a packed cross-encoder that must align two ~400-token Lean statements against
   each other and detect a one-token semantic difference? In particular I want your read
   on its **alternating local/global attention** (sliding-window local layers with
   periodic global layers): in a packed pair, the aligned tokens of A and B are hundreds
   of positions apart, so if my understanding is right, most layers cannot see the
   cross-statement counterpart of a token at all and only the global layers can do the
   alignment. Is that a real limitation for this task, is it mitigated in practice
   (depth, RoPE, the pooled objective), and how would I *measure* whether it is biting?
   If it is biting, what are the fixes (interleave A and B section-wise, shorten the
   view, change the window, use a non-windowed backbone, add a light cross-attention
   layer over two separate encodings)?
2. **Tokenizer fitness.** ModernBERT's tokenizer is code-aware but not Lean-aware. How
   badly do Lean's Unicode operators, `Nat.foo_bar_baz`-style qualified names, and
   typeclass-binder syntax fragment, versus alternatives? Quantify the consequences I
   should expect (fertility → effective context, rare-token embeddings, whether the
   discriminative token — the `≤` vs `<`, the swapped `∀`/`∃` — survives as an
   identifiable piece). Given a 469K-row Lean CPT corpus, is keeping the stock vocab
   right, or should I (a) add Lean tokens and resize, (b) train a Lean tokenizer and
   re-pretrain, (c) leave it alone? Argue the trade-off against the fact that S0 CPT can
   partly compensate for a mediocre tokenizer but cannot fix a bad *segmentation* of the
   very tokens that carry the label.
3. **The alternatives, ranked.** Evaluate at least: ModernBERT-large; DeBERTa-v3-large
   (disentangled attention + RTD pretraining is historically very strong for pair
   classification — is its 512-token limit and its positional scheme actually fatal here,
   or is a 512-token view achievable and worth it?); a code-pretrained encoder
   (CodeT5+ encoder, CodeBERT/GraphCodeBERT/UniXcoder-class, StarEncoder-class); newer
   general encoders you know of (NeoBERT, EuroBERT, mmBERT, ModernBERT-variants,
   anything else); math/proof-specific encoders if any exist; and **decoder-derived
   encoders** — a small LLM converted to bidirectional/embedding use (Qwen3-Embedding
   class, LLM2Vec-style conversion, or simply a 0.5–1.5B *code/math* decoder fine-tuned
   as a sequence-pair classifier). Include for each: expected quality on *this* signal,
   context handling at 1024, tokenizer suitability for Lean, params/latency/memory,
   licence, and integration friction.
4. **Should the "non-autoregressive" constraint stay?** A Lean-pretrained decoder
   (Goedel/Kimina/DeepSeek-Prover-class, or a general code LLM) has seen far more Lean
   than any encoder ever has. What is the honest cost/benefit of using a small
   Lean-pretrained decoder as a *classifier* backbone (bidirectional-ised or not),
   given that my whole value proposition is "cheaper and more transparent than an LLM
   judge"? At what parameter count does that proposition break?
5. **Is S0 (Lean MLM CPT) the right lever at all**, or does backbone choice dominate it?
   If a 469K-row Lean CPT run on a stock ModernBERT would close most of the gap to a
   better-pretrained backbone, that changes my priority order. Tell me which of
   {backbone swap, CPT, view/architecture change, more S1 data} you expect to have the
   largest effect size on my headline metric, and why.
6. **Architecture second-guessing (bounded).** Given the ~0.887 proxy AUPRC for the
   packed cross-encoder vs 0.603 for the bi-encoder, is masked-mean pooling + single
   linear logit the right head, or should I use `[CLS]`, a difference/interaction
   feature over per-statement pooled states, or a token-alignment-aware head? Keep this
   to changes that plausibly matter for backbone selection.

## 5. Deliverable format

1. **A one-paragraph verdict**: keep ModernBERT-base, move to ModernBERT-large, or
   switch — stated as a decision, with the single strongest reason.
2. **A ranked shortlist of 3–5 backbones** with a comparison table
   (quality expectation on this signal / native context / tokenizer fit for Lean /
   params / expected batch-32 throughput class / licence / integration friction).
3. **The decisive experiment**: the cheapest protocol that would settle this
   empirically, given that I already have a 17k-pair proxy corpus, ~300K+ deterministic
   pairs coming, ~1.7K gold pairs I can spend on dev, and cluster GPUs. Specify
   candidates, controls, the *exact* metrics, the number of seeds, and a decision rule.
   Assume I will hold the view, budget, and training protocol fixed across candidates.
   Prefer a design where the answer arrives in ≤2 GPU-days.
4. **Cheap diagnostics I can run before training anything** — e.g. tokenizer fertility
   and discriminative-token-survival measurements on Lean text, attention-window
   reachability analysis for the packed view, length distributions at 512 vs 1024.
   Give me the specific quantity to compute and the threshold at which it should change
   my decision.
5. **Risks and "what would change my mind"**: the 2–3 assumptions in your recommendation
   that are most likely to be wrong, and the observation that would falsify each.

Be concrete and quantitative wherever you can. If a claim of yours rests on a benchmark
result or a model detail you are not certain of, flag it explicitly rather than
smoothing over it.
