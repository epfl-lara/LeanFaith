# LF-022 fresh Sol/Fable adjudication: Qwen aef924 n1 v4

Date: 2026-08-12

## Outcome

One genuinely fresh, public, Lean-valid Qwen-proposed pair completed the full
four-cell adjudication route. GPT-5.6 Sol at `xhigh` and Claude Fable 5 at
`max` each judged the pair in AB and BA presentation order. All four calls
parsed on their first provider attempt, none requested expert review, and all
four canonicalized to:

```text
same_claim: false
relation: A_stronger
minimum self-reported confidence: 0.96
```

The reference statement asserts `η.unop2.op2 = η`; the candidate replaces the
conclusion with `True`. The judgments correctly distinguish the substantive
claim from a vacuous true proposition. Error-code opinions differed and remain
exploratory metadata only.

This result first entered the model-adjudicated candidate-silver pool. The
separate fail-closed promotion verifier has now replayed and admitted it as one
training-only model-silver record. It is not human gold, calibration data,
selection data, evaluation data, or Gate-6 human-audit credit.

## Freshness and source binding

The selector excluded the complete registered historical Sol/xhigh universe:

```text
1,510 pair IDs
1,339 theorem lineages
1,487 canonical judge-visible payloads
```

It also excluded every completed Sol/Fable pair, theorem lineage, and
judge-visible payload. The selected pair was absent from all three exclusion
sets and comes from the immutable 1,189-record Lean-valid Qwen inventory.

- Pair ID: `pair:19be97691f8f857245794809f42124146668ab3056d726cb6e1737d4a309a4d9`
- Source theorem lineage:
  `thm:27a6839712c3fcf0395d52f79b5fbb922a39245e46e811b7826e88eae1cb9bc6`
- Judge-visible payload SHA-256:
  `fe3aed0e07a792165d9ef65a591f733dfa250091d066c6408de082c1f2cf285a`
- Authoring ID:
  `lf022_sol_fable_authoring:343a78c50a6e346c9e4bac1a538aa7af12db040d2d292816d6c02349505d2352`
- Batch ID:
  `lf022_weak_batch:80a98182c05c46984ba36c2007b407e917049d329a299f921400083df89fd98d`
- Dispatch-manifest SHA-256:
  `397d749cc0ed9c2e48f46619ba3c40da84ec426f3e8d41954b2aa8fbaa7ce991`

## Execution and replay

- Sol live run:
  `lf022_sol_run:6d56fc05167444eef2075573b2288bdfb56cf2ff11419afbea6c27af96f58cc7`
- Fable live run:
  `lf022_fable_run:d92f1b32fafdeb04efc8cfc3183a713db299ac466ee1f5eb892593d9e939214b`
- Both runs: 2/2 orientations complete on the first provider attempt.
- Both runs: `private_source_content_transmitted=false`.
- Execution ID:
  `lf022_weak_execution:3aa3c5d43e36806fcb0137b5146c0e971f10a7077926da6872f71c6a540bcc38`
- Finalization ID:
  `lf022_weak_finalization:d0ecd881c2278ac90f5c0812191a34aac8131c991455ed46a897bf9c1813084b`
- Candidate ID:
  `weak_consensus:4697ac302e192ab5c583967b9cebbf76946a7efcd10a05ec00ee96d6e6b9ad49`

Final artifact bindings:

```text
finalization manifest  c3a0877beedd263e36c411de97d8409c7655ea7566e192bb50851eb01f5a2066
judgment evidence      4711518a00948be2d41048b37ce1af13bf316ac8feb2dc287891f1cfe0b05829
weak candidate         68007cfa480df6df536352f4fb79276b48a1c8dec1f49581a5a8def3dd4c749a
```

Canonical artifact root:

```text
/storage/milikic/leanfaith/lf022_weak_batches/
  sol_fable_fresh_qwen_aef924_n1_v4/batch/final/
```

## Scientific boundary

The source candidate and raw weak-consensus record remain non-trainable by
construction. The distinct promotion verifier replayed the exact four cells,
all lineage/config/prompt/parser hashes, public-source and denylist status, and
the absence of a review request or bound non-LLM evidence. Its immutable
result is:

```text
/storage/milikic/leanfaith/lf022_model_silver/
  qwen_aef924_fresh_n1_v2/

promotion manifest
  model_silver_manifest:493e6460a04f01657869f18a0238457a059cb1f511014e73f3490ef0ada28735
promotions  1
rejections  0
minimum confidence  0.96
```

Exact replay produced the same manifest and bytes. The promoted record is
eligible only for the declared weak-training arm. The schema mechanically
forbids human-gold status, calibration, model selection, sealed evaluation,
`ResolvedLabel` creation, trusted F2 evidence, and Gate-6 credit.

This is record-level eligibility only. Gate 6M has not yet been formally
closed, so this artifact does not by itself authorize a scientific
weak-training run.
