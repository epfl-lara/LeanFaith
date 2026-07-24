# ADR-0006 — LF-021 provisional local generator families

- **Status:** accepted; three collection families activated and mechanical collection completed; reserved ReForm family remains inactive
- **Date:** 2026-07-23
- **Scope:** LF-021 local real-output collection on public, trusted fixtures

## Context

LF-021 needs at least three materially distinct successful generator families
for its collection checkpoint and a fourth family reserved from supervision
for a later held-out-generator study. ADR-0005 proved only the deterministic
fixture/replay path; it did not authorize any model.

This ADR verifies the four candidates in
`reports/generation/lf021_local_model_probe_v1.json` against the repositories'
own pinned Hugging Face metadata, model cards, `config.json`,
`tokenizer_config.json`, and `generation_config.json`. No paper, third-party
summary, model output, or provider claim is used to fill missing repository
metadata. The verification resolved every requested revision to the same
40-character commit SHA. At probe time, each SHA was also the repository's
current `main`, but the immutable SHA—not `main`—is authoritative.

The verified pins do **not** activate the models. Training-data lineage and
benchmark overlap are not fully disclosed by the pinned cards. Under the
fail-closed rule, every candidate remains disabled until its overlap record,
prompt/output adapter, local runtime, and one-fixture end-to-end qualification
all pass.

## Verified immutable pins

| Provisional role | Repository and immutable revision | License | Declared base family | Config architecture | Config positions | Parameters |
|---|---|---|---|---|---:|---:|
| supervision family 1 | [`AI-MO/Kimina-Autoformalizer-7B@ddd47cb477d93b3ca990468e1c0d5ad6b60973dd`](https://huggingface.co/AI-MO/Kimina-Autoformalizer-7B/tree/ddd47cb477d93b3ca990468e1c0d5ad6b60973dd) | Apache-2.0 | `Qwen/Qwen2.5-Coder-7B-Instruct` | `Qwen2ForCausalLM` | 32,768 | 7,615,616,512 |
| supervision family 2 | [`Goedel-LM/Goedel-Formalizer-V2-8B@fe2d362d899601abe79d7d5e95eaa7fe9883a0cb`](https://huggingface.co/Goedel-LM/Goedel-Formalizer-V2-8B/tree/fe2d362d899601abe79d7d5e95eaa7fe9883a0cb) | Apache-2.0 | `Qwen/Qwen3-8B` | `Qwen3ForCausalLM` | 40,960 | 8,190,735,360 |
| supervision family 3 | [`stepfun-ai/StepFun-Formalizer-7B@fb0dc612761fecd64ebbc489c2a3417e9ea01968`](https://huggingface.co/stepfun-ai/StepFun-Formalizer-7B/tree/fb0dc612761fecd64ebbc489c2a3417e9ea01968) | Apache-2.0 | `deepseek-ai/DeepSeek-R1-Distill-Qwen-7B` | `Qwen2ForCausalLM` | 131,072 | 7,615,616,512 |
| supervision-excluded held-out family | [`GuoxinChen/ReForm-8B@1589c832cfad679a280b222e694b987a33befd26`](https://huggingface.co/GuoxinChen/ReForm-8B/tree/1589c832cfad679a280b222e694b987a33befd26) | Apache-2.0 | `Qwen/Qwen3-8B` | `Qwen3ForCausalLM` | 40,960 | 8,190,735,360 |

All four repositories are public and ungated. The report's `15.2G` and
`16.4G` values are correct rounded decimal sums of the checkpoint
`safetensors` files: 15,231,271,864 bytes for Kimina,
16,381,516,824 bytes for Goedel, 15,231,271,960 bytes for StepFun, and
16,381,516,712 bytes for ReForm.

The following pinned-file hashes bind this review:

| Repository | `README.md` SHA-256 | `config.json` SHA-256 | `tokenizer_config.json` SHA-256 | `generation_config.json` SHA-256 |
|---|---|---|---|---|
| Kimina | `3dacbeef57ce2e15a83a3dee53b908e2020acf3617dce6c502325d553c1c1a75` | `1bc02cfa2fbdb052184f7af9d348ccfe1ed374db9e123c202f3bd855c22669be` | `adf48fe03efc6ccb759a592b536527423b99c1db65fcf5520e9286abbcc3397f` | `722a06091ec871c6cbc3dda5412f294267bcd5d9310f4ffbef1d358000dd3bfc` |
| Goedel | `fb99bdc55ff4af4f2b49b14a71f64d2402ed7780202cd9e424236480e1f2e4d9` | `6d4190d4c7ae77cba38a83cce1152ead961929795a7a95d4814b3fb65ab878a3` | `5d27a191a7ba6f93bcc2a8e78bc633764bbaaee263e9f3d9225e2c8621a5dd72` | `ae29473afe95319d9adb9553a2cad4a89db813ff768792f29c5278fb127ff4cc` |
| StepFun | `c863993187169d618e130adbfbd220e7f15bbbc55697b701d6f0b11fb9e33d9f` | `7ac9c14e315f21fe7b4ed23810a0c4264c093d85ffe709978824ee231eb609ed` | `aefa2a0c2214c8e3a5f1c45b080ddf70362ee3482ceb0e9b362cc42cf3ac1682` | `72cbec1015da9ed03ad025483005cbf6481403abf947adfd54e504d1a66a2126` |
| ReForm | `dab91cf6676f157058f3b68aae5e015573b5573f4b2d0e834875c0b902f55ede` | `1f07053448f4e78ae0e21211c91ff8445e591e6da218a5432aec59e6ef6d8338` | `c9836df2e942ba9887ef1c721c100d9f4746aaf86e79e7ed99ceb0af78a3b48e` | `85eb7aa4abab67dfbc3898a53616cd1c039f63d97c456becc2deb65286c037e8` |

The model `config.json` values—not tokenizer declarations—bind supported
position counts in the probe. This matters because StepFun declares 131,072
model positions but a 16,384 tokenizer maximum, while ReForm declares 40,960
model positions and a 131,072 tokenizer maximum. No LF-021 runtime may infer a
safe prompt/generation budget from either number alone; qualification records
the budget actually exercised.

## Checked-in configuration boundary

This ADR does not silently repurpose the existing provider slots.
`configs/generation/providers.yaml` still describes the Phase-0 external and
abstract provider registry, while `configs/generation/real_outputs_v1.yaml`
still has generation disabled, an empty `allowed_provider_slots`, and no
frozen prompt or parser. In particular, the single `open_diversity` slot is not
three local families, and the `specialized_generator` ReForm slot is not
supervision-eligible under this ADR.

Before any candidate can become active, a versioned local-generator registry
must bind each of the four exact revisions to its separate role, runtime,
prompt, parser, source matrix, and overlap record; the ready real-output config
must reference that registry and list only the enabled supervision families.
ReForm must remain explicitly supervision-excluded. Until that config change
and its validation tests exist, the checked-in configuration authorizes zero
local model calls.

## Prompt and output adapters

Each model receives the same public problem, registered Lean header, theorem
name, and strict LeanFaith output contract. The model-specific wrapper follows
the pinned card. Exact template bytes will live in versioned prompt files and
must hash to the run manifest before activation.

The wrapper operates on the prompt, while the output parser operates on
**completion-only text** after input tokens have been removed. This distinction
is binding: StepFun's input prompt itself contains a Lean header fence, and
decoding or parsing the concatenated prompt plus completion would create a
false multiple-fence failure and could leak prompt text into the raw-response
artifact.

The common suffix is:

```text
Return the final answer as one Lean 4 theorem or lemma declaration in one
final Markdown fence labelled `lean4`. Use the registered theorem name.
Do not invent a different import context. Explanatory reasoning may precede
the final fence, but return no second Lean fence or alternative declaration.
```

### `kimina_card_chat_v1`

- Apply the pinned tokenizer chat template with
  `add_generation_prompt=true`.
- System message:
  `You are an expert in mathematics and Lean 4.`
- User message:
  `Please autoformalize the following problem in Lean 4 with a header. Use the following theorem names: {THEOREM_NAME}.\n\n{NL_STATEMENT}\n\n{COMMON_SUFFIX}`
- Qualification decoding is frozen as `do_sample=true`,
  `temperature=0.6`, `top_p=0.95`, `top_k=20`,
  `repetition_penalty=1.1`, `max_new_tokens=2048`, `n=1`, plus the
  recorded seed. The temperature/top-p/token limit follow the card example;
  top-k and repetition penalty are made explicit from the pinned generation
  config rather than left to runtime defaults.

The pinned card says this model normally emits a header and code ending in
`by sorry`. The output adapter therefore extracts the statement structurally;
it never treats the generated proof placeholder as evidence.

### `goedel_v2_card_chat_v1`

- Apply the pinned tokenizer chat template to a single user message with
  `add_generation_prompt=true`.
- User message:
  `Please autoformalize the following natural language problem statement in Lean 4. Use the following theorem name: {THEOREM_NAME}\nThe natural language statement is: \n{NL_STATEMENT}Think before you provide the lean statement.\n\n{COMMON_SUFFIX}`
- Qualification decoding is frozen as `do_sample=true`,
  `temperature=0.9`, `top_k=20`, `top_p=0.95`,
  `max_new_tokens=16384`, `n=1`, plus the recorded seed.

The pinned card extracts the last `lean4` fence. LeanFaith uses the stricter
single-final-fence rule below and retains all preceding reasoning only in the
immutable raw response.

### `stepfun_card_think_v1`

- Apply the pinned tokenizer chat template with
  `add_generation_prompt=true`, then append the literal `<think>`.
- System message:
  `You are an expert in mathematics and Lean 4.`
- User message:
  `Please autoformalize the following problem in Lean 4 with a header. Use the following theorem names: {THEOREM_NAME}.\n\n{NL_STATEMENT}\n\nYour code should start with:\n```Lean4\n{REGISTERED_HEADER}\n```\n\n{COMMON_SUFFIX}`
- Qualification decoding is frozen as `do_sample=true`,
  `temperature=0.6`, `top_p=0.95`, `max_new_tokens=16384`, `n=1`, plus
  the recorded seed.

### `reform_card_raw_v1`

- Tokenize the raw prompt directly; do not apply a chat template.
- Prompt:
  `Think step by step to translate the mathematical problem in natural language to Lean 4, and verify the consistency.\n{NL_STATEMENT}\n\nUse the following theorem name: {THEOREM_NAME}.\n{COMMON_SUFFIX}`
- Qualification decoding is frozen as `do_sample=true`,
  `temperature=0.6`, `top_k=20`, `top_p=0.95`,
  `max_new_tokens=32768`, `n=1`, plus the recorded seed. The token limit
  follows the card example; the sampling values are made explicit from the
  pinned generation config.

### `lean_final_fence_signature_v1`

This is the accepted card-oriented adapter design, but it is not implemented
or authorized by the current config. The existing
`direct_autoformalization_v1` parser is deliberately stricter: it accepts one
proof-free fence with no surrounding text, header, import, or proof. An
offline fixture that passes that existing parser proves only the offline
lineage path; it does not qualify any model under this ADR.

Activating `lean_final_fence_signature_v1` requires a checked-in parser,
model-specific prompt templates, typed-config support for the new parser ID,
and unit/integration tests for every rule below. No run may describe one parser
ID while executing the other.

The planned card-oriented adapter:

1. persist the complete raw output before parsing;
2. accept arbitrary reasoning text only before one final `lean4`/`Lean4`
   fence and reject a second Lean fence or trailing alternative;
3. allow no generated import/header except the absent header or the exact
   registered header;
4. parse exactly one named `theorem` or `lemma`;
5. use LeanInteract in the registered context to extract its proposition
   signature, excluding the declaration value/proof structurally;
6. require the declaration to elaborate as `Prop`;
7. pass only the proof-stripped statement into the existing LF-021
   materialization path; and
8. persist every parse/elaboration failure as an operational failure, never a
   semantic negative.

The planned parser may accept a generated `by sorry` value because the value
is discarded before the statement is inspected. It may not accept an axiom,
definition, multiple declarations, an unregistered context, or a statement
whose extracted name differs from the registered theorem name. The raw
completion, including any discarded proof, remains quarantined and is never a
model-visible statement representation.

## Exposure and overlap findings

“Not disclosed” is not evidence of no overlap.

| Family | What the pinned Hugging Face card establishes | Remaining overlap status |
|---|---|---|
| Kimina | Project Numina developed it for competition-style NL-to-Lean autoformalization. No training dataset is declared. | Training lineage and overlap with the public problem pool are unverified. Potential Numina/competition-source overlap must be screened. **Disabled.** |
| Goedel V2 | The card says it is the Goedel-Prover-V2 formalizer and describes an internal 300-item Omni-MATH evaluation. No training dataset is declared. | Training lineage and overlap with Goedel-derived/public pool records are unverified. **Disabled.** |
| StepFun | Metadata declares `stepfun-ai/StepFun-Formalizer-Training`. The card names FormalMATH-Lite, ProverBench, and CombiBench as evaluation benchmarks. | The model card does not pin the training dataset revision or enumerate its source lineages. Those evaluation benchmarks are exposure tags, not proven training data. **Disabled.** |
| ReForm | The card describes reflective generation and names miniF2F, ProofNet, Putnam, and AIME 2025 as evaluation benchmarks. It declares no training dataset. | Project policy already flags ReForm×Lean-Workbook as overlap, but the pinned card does not independently establish its exact training lineage. Lean-Workbook and all named benchmark lineages are excluded from clean held-out evidence until the overlap registry closes. **Disabled.** |

The three supervision candidates and the held-out candidate are distinct
fine-tuned checkpoint families. Goedel and ReForm share the Qwen3-8B base, so
the later ReForm study is a held-out **generator-checkpoint/fine-tuning
family** study, not a held-out base-model-family study. Publications and
reports must use that narrower description.

## Decision

Provisionally reserve:

```text
supervision-eligible:
  - AI-MO/Kimina-Autoformalizer-7B
  - Goedel-LM/Goedel-Formalizer-V2-8B
  - stepfun-ai/StepFun-Formalizer-7B

held-out from every training/supervision path:
  - GuoxinChen/ReForm-8B
```

“Provisionally reserve” assigns intended roles and immutable repository pins;
it does not enable inference or qualify a family for Gate 5G. ReForm output,
votes, scores, rankings, repairs, and derived labels are forbidden from every
training and weak-supervision artifact. A role change requires a superseding
ADR and regeneration of every affected split/contamination manifest.

## Per-model activation gate

A model changes from disabled to locally active only when **all** of the
following are true for that exact revision:

1. **Metadata binding:** the repository revision, license, base model,
   architecture, card/config/tokenizer/generation hashes, and non-gated
   availability equal this ADR.
2. **Overlap closure:** a machine-readable overlap record binds the model
   revision, declared datasets/exposures, exact and near-duplicate denylist
   results, allowed problem-source matrix, and unresolved-lineage disposition.
   An unresolved training lineage may be accepted only by excluding every
   plausible overlapping source from that model's collection slice; otherwise
   the model stays disabled.
3. **Local-only execution:** weights execute under project control. No
   inference API or remote endpoint is called. No private `sft_classic` text,
   identifier, derived prompt, or model output is transmitted off-machine.
   The activation fixture is public, trusted, hand-authored, denylist-cleared,
   and marked `artifact_class=smoke`.
4. **Runtime freeze:** record the environment lock, Python/runtime/driver
   versions, runtime adapter source hash, model and tokenizer revisions,
   numeric precision, device mapping, generation configuration, seed, and
   runtime-config canonical hash. A runtime change invalidates qualification.
   Deterministic greedy execution may be used for implementation smoke, but it
   cannot qualify a model whose frozen qualification contract specifies
   seeded sampling. Smoke and qualification requests carry distinct
   `execution_purpose` values and the smoke result is Gate-5G-ineligible.
5. **Prompt freeze:** check in the exact model-specific template, common
   suffix, tokenizer/chat-template binding, registered Lean header, rendered
   prompt, and their hashes. No prompt is reconstructed from this prose at run
   time.
6. **Output/parser freeze:** store the immutable raw response before parsing
   and bind its SHA-256, normalized candidate bytes/hash, parser ID/source
   hash, LeanInteract context fingerprint, completion-only extraction rule,
   and terminal parse/elaboration outcome.
7. **One public fixture end to end:** the model must produce a candidate on
   the same frozen public fixture; the candidate must pass the output adapter,
   elaborate through LeanInteract as exactly one proposition, materialize the
   expected call/attempt/variant/theorem/representation lineage, pass the
   frozen candidate-screening record, and only then admit the pair/NL-Lean
   lineage. It creates no semantic label. A malformed, noncompiling, unscreened,
   or non-admitted result leaves that model disabled; it is not a negative
   label.
8. **Isolation tests:** confirm that no generated proof/value enters
   model-visible statement views, no held-out ReForm artifact enters
   supervision, and no two model families are collapsed merely because they
   share a Qwen base.

The fixture establishes implementation correctness only. It is ineligible
for training, calibration, prevalence, evaluation, release, and Gate 5G.
“Replay to identical semantic IDs and hashes” means replaying the persisted
request and raw-response artifacts. It does not assert that stochastic model
generation is bitwise reproducible across runtime or hardware changes.

GPU memory, latency, throughput, token counts, and runtime failures are
recorded for reproducibility and feasibility diagnosis only. There is no
prescribed hardware, speed, memory, or cost threshold and no family wins or
loses a scientific comparison from these qualification measurements.

## Gate 5G boundary

Neither this ADR nor successful smoke fixtures close Gate 5G.

A family counts toward Gate 5G only after an authorized research collection
run produces actual, non-smoke outputs that:

- originate from distinct public problem records under the frozen source
  matrix;
- are not raw, normalized, alpha-fingerprint, or ancestry duplicates of
  another output counted for that family;
- elaborate as propositions under their recorded Lean contexts;
- have complete provider/call/prompt/parser/runtime lineage; and
- survive the frozen denylist and overlap rules.

The Gate 5G report must state unique attempted, parsed, compiling, and failed
counts per family. At least three supervision-eligible families must each have
nonzero unique compiling outputs, and every other Gate 5G criterion in
`PLAN.md` still applies. ReForm is reported separately as reserved held-out
coverage and contributes no supervision data.

## Non-decisions

This ADR does not:

- enable any model before qualification;
- authorize an external provider or inference endpoint;
- authorize private-source transmission;
- label any model output as faithful or unfaithful;
- treat compilation as semantic correctness;
- close Gate 5G or Gate 5;
- select judges, validators, or LLM weak-label providers;
- authorize ReForm for supervision;
- claim held-out base-model-family generalization;
- add localization or repair generation; or
- prescribe hardware, latency, throughput, memory, token, or cost targets.

## Consequences

The three designated local collection families passed their activation
boundaries and completed the exact 16-tranche Gate-5G collection. ReForm
remains inactive and supervision-excluded. The resulting
`three_family_collection_only` scope supports a reduced-data ablation but not
a clean held-out-generator claim. Gate 5G is mechanically closed; genuine
human semantic annotation, prevalence reporting, and Gate 5 remain pending.
