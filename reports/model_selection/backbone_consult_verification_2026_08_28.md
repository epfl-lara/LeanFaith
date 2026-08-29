# Backbone consultation verification — 2026-08-28

Two external consultations on "is ModernBERT the right encoder backbone" (GPT Pro and Claude,
both prompted with `prompts/encoder_backbone_review_gpt_pro_v1.md`) were cross-checked against
the pinned local snapshots, the installed `transformers` modeling code, live HF model cards, and
fresh local tokenizer measurements. This file records what was **confirmed**, what was
**refuted**, and what remains **unverified**, and feeds PLAN.md Track T-B.

## Consultation verdicts (as received)

- **GPT Pro**: switch default checkpoint to `jhu-clsp/ettin-encoder-150m`, keep ModernBERT-base
  as incumbent control; EuroBERT-210m as the dense-attention challenger; do not jump to
  ModernBERT-large.
- **Claude**: keep the ModernBERT line (base = iteration, large = default ship candidate);
  decide via a ≤2-GPU-day pilot that must include a widened-local-window arm and ettin-encoder-1b.
- **Convergent core (adopted)**: keep ModernBERT-base as iteration vehicle; small multi-arm pilot
  with an *interventional* attention probe; stock tokenizer for selection; DeBERTa only as a
  512 science control; keep the single-forward-pass constraint but admit one decoder probe;
  S1 data quality > S0 CPT > backbone as expected effect sizes; never block S0/S1 on the pilot.

## Verified — ModernBERT geometry (pinned snapshot + installed modeling code)

- `global_attn_every_n_layers: 3`, `local_attention: 128`, `max_position_embeddings: 8192`,
  vocab 50,368 — both base (22 layers) and large (28 layers) configs.
- `modeling_modernbert.py:464`: `layer_id % 3 != 0 → local`, window
  `(local_attention//2, local_attention//2)` = **±64 tokens per side**. Global layers are
  0,3,…: **base 8/22, large 10/28, final layer global in both** (GPT Pro's schedule claim exact;
  Claude's ±64-vs-±128 question resolved: ±64).
- Local layers build their RoPE cache to `config.local_attention` with θ=10k
  (`modeling_modernbert.py:467`) — widening the window at fine-tune is a mechanically clean
  config change (cache auto-extends), as Claude assumed.
- Packed-view consequence (measured): mean packed pair = 158 tokens ⇒ A↔B counterpart offsets
  ~70–450 positions — outside every local window; cross-statement token alignment happens
  **only** in the global layers. Both reports' core geometry reading is correct.
- ModernBERT tokenizer: 84 `[unused]`-named reserved entries in `tokenizer.json`
  (config vocab 50,368 vs tokenizer base 50,280) → the **no-resize special-token path is real**.

## Verified — tokenizer/operator audit (run locally, pinned snapshots, 41-operator set)

| Tokenizer | UNK ops | Ops >3 pieces | Collisions in `a OP b` | Fertility (tok/char, gate3 headless n=2000) | Packed pairs >512 / >1024 (17k corpus n=4000) |
|---|---:|---:|---:|---:|---|
| ModernBERT (base=large) | **0** | **0** | **0** | 0.545 (mean 65.3, p95 151) | 1.4% / **0.30%** |
| DeBERTa-v3-large (SPM, slow) | **0** | 0 | 0 | **0.495** (best) | 1.3% / 0.20% |
| CodeT5+-220m | 0 | 0 | 0 | 0.628 (worst) | — |

- **Claude's DeBERTa disqualification fear is REFUTED**: the 128K SPM covers every tested Lean
  operator — no UNK collapse, no `∀`/`∃` or `≤`/`<` collisions — and it has the best fertility.
  **98.7% of packed pairs fit 512** ⇒ DeBERTa passes GPT Pro's ≥98% re-entry bar as a *science
  control* (edit-span retention ≥99.5% still to check once D emits edit-site spans). It remains
  ship-ineligible: 512-native, no FA2/unpadding path, frozen 1024 view.
- Markers are multi-piece as designed: `[REFERENCE]`=5, `[CANDIDATE]`=6, `[HEADLESS]`=5 pieces.
- Qualified names fragment similarly everywhere (`Nat.succ_le_of_lt`: 9/10/10 pieces) — not a
  differentiator between candidates.
- The ADR's 512-rejection was for the richer Gate-3 `[SIGNATURE_EXPLICIT]` bundle; under the
  current headless packed view 512 loses only ~1.4% — worth remembering, not worth unfreezing.

## Verified — candidate landscape (HF cards, 2026-08-28)

| Model | Confirmed | Notes |
|---|---|---|
| jhu-clsp/ettin-encoder-{17m…1b} | ✓ exists, MIT, ModernBERT arch+tokenizer, 2T tokens incl. code/scientific, no `trust_remote_code`; 150m: 22L/768h, `max_position_embeddings` **7999** (GPT Pro's odd number confirmed); 1b: 28L/1792h | GLUE 88.9 vs 88.4, MTEB-retrieval 45.7 vs 43.9 vs ModernBERT-base — real but modest |
| EuroBERT-610m | ✓ Apache-2.0, 8192 ctx, math+code, beats XLM-R-XL on code/math; **`trust_remote_code` required** per card | Dense-attention-every-layer from the paper — not restated on card (low risk) |
| NeoBERT | ✓ MIT, 250M, 4096 — but **google/bert WordPiece tokenizer + RefinedWeb (no code) + `trust_remote_code`** | **Disqualified** for Lean, as Claude predicted |
| Qwen/Qwen3-Reranker-0.6B | ✓ Apache-2.0, 32K, single-pass yes/no-token logits | seq-cls conversion claim **unverified** (not on card) — check `tomaarsen/…-seq-cls` before use |
| AI-MO/Kimina-Prover-Preview-Distill-1.5B | ✓ Apache-2.0, Qwen2.5-Math-1.5B base, Lean 4 RL-distill | viable Lean-pretrained ceiling probe |
| Post-Jan-2026 successors | Search: none found | landscape = Ettin/EuroBERT/mmBERT wave + language-specific ModernBERT clones (mmBERT is multilingual-targeted, not code/math) |

## Unverified / to check at pilot freeze

- Ettin exact per-size params and per-revision hashes; EuroBERT native-transformers status
  (card still says remote code); Qwen3-Reranker seq-cls conversion; newer Kimina distills;
  Ettin cross-objective paper numbers (encoders > equal/larger decoders on classification) —
  cited from the suite's card, not independently reproduced.
- Both consultations' quality deltas (±pp tables) are **priors, not measurements** — the pilot
  exists precisely to replace them.

## Local evidence the consultations did not have

- S0 already ran (ledger): chunks-CPT beats mixed/harder-CPT on golden dev (AUPRC 0.849) —
  consistent with both reports' "S1 data quality dominates" ranking, and a caution against
  Claude's "oversample statement-like tokens" suggestion; treat CPT-mixture changes as measured
  ablations only.

## Resulting plan changes (PLAN.md)

1. New **Track T-B backbone pilot** (arms, protocol, throughput-conditional decision rule,
   registry-v2 freeze, ADR-0004 literal conclusion recorded, "lightweight" re-pinned as one
   deterministic forward pass ≤2B params).
2. S0: next CPT pass packs marker-tagged statement pairs; ModernBERT-large "cheap upgrade"
   sentence superseded by T-B.
3. S1: calibrate after directional-logit averaging.
4. Track D: certificates record packed-view edit-site token spans.
5. Ledger + verification section updated.
