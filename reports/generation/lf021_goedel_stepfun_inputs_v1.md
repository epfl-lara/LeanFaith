# LF-021 Goedel and StepFun qualification-input facts

**Status:** metadata verified; neither model executed or qualified  
**Date:** 2026-07-23  
**Scope:** patch-ready prompt, chat-template, and decoding facts for the next
two local-family fixture qualifications

## Audit boundary

This report uses only the following four files from each immutable Hugging
Face revision:

```text
README.md
config.json
tokenizer_config.json
generation_config.json
```

They were fetched with `hf download --revision <40-hex SHA> --include ...`.
The Hub API independently resolved each requested revision to the same SHA.
Both repositories were public, ungated, and declared Apache-2.0 at audit
time. The local client was `huggingface_hub==0.36.2`.

No checkpoint weights were downloaded by this audit. No model was loaded or
executed. No provider slot was enabled. No semantic record or label was
created. This report gives no Gate-5G or Gate-5 credit.

## Bound primary artifacts

### Goedel-Formalizer-V2-8B

Repository:
`Goedel-LM/Goedel-Formalizer-V2-8B@fe2d362d899601abe79d7d5e95eaa7fe9883a0cb`

| File | Bytes | SHA-256 |
|---|---:|---|
| `README.md` | 2,435 | `fb99bdc55ff4af4f2b49b14a71f64d2402ed7780202cd9e424236480e1f2e4d9` |
| `config.json` | 730 | `6d4190d4c7ae77cba38a83cce1152ead961929795a7a95d4814b3fb65ab878a3` |
| `tokenizer_config.json` | 9,789 | `5d27a191a7ba6f93bcc2a8e78bc633764bbaaee263e9f3d9225e2c8621a5dd72` |
| `generation_config.json` | 214 | `ae29473afe95319d9adb9553a2cad4a89db813ff768792f29c5278fb127ff4cc` |

Verified configuration facts:

```text
architecture = Qwen3ForCausalLM
base model declared by card = Qwen/Qwen3-8B
model max_position_embeddings = 40960
tokenizer model_max_length = 131072
model dtype = bfloat16
model use_cache = false
model eos_token_id = 151645
generation eos_token_id = [151645, 151643]
generation pad_token_id = 151643
```

The model-position limit is authoritative for qualification budgeting; the
larger tokenizer limit does not expand the model context.

### StepFun-Formalizer-7B

Repository:
`stepfun-ai/StepFun-Formalizer-7B@fb0dc612761fecd64ebbc489c2a3417e9ea01968`

| File | Bytes | SHA-256 |
|---|---:|---|
| `README.md` | 4,407 | `c863993187169d618e130adbfbd220e7f15bbbc55697b701d6f0b11fb9e33d9f` |
| `config.json` | 679 | `7ac9c14e315f21fe7b4ed23810a0c4264c093d85ffe709978824ee231eb609ed` |
| `tokenizer_config.json` | 3,062 | `aefa2a0c2214c8e3a5f1c45b080ddf70362ee3482ceb0e9b362cc42cf3ac1682` |
| `generation_config.json` | 181 | `72cbec1015da9ed03ad025483005cbf6481403abf947adfd54e504d1a66a2126` |

Verified configuration facts:

```text
architecture = Qwen2ForCausalLM
base model declared by card = deepseek-ai/DeepSeek-R1-Distill-Qwen-7B
training dataset declared by card = stepfun-ai/StepFun-Formalizer-Training
model max_position_embeddings = 131072
tokenizer model_max_length = 16384
model dtype = bfloat16
model use_cache = true
model eos_token_id = 151643
generation eos_token_id = 151643
generation bos_token_id = 151646
```

The pinned files disagree about the BOS ID: `config.json` uses `151643`,
whereas `generation_config.json` uses `151646`. Qualification must not invent
a reconciliation. It must load the pinned tokenizer/model generation config
and persist the effective IDs observed at runtime. The card's 16,384-token
output example also equals the tokenizer's declared total maximum while the
model config is larger; the actual prompt/output budget exercised must
therefore be recorded.

## Goedel prompt and chat contract

The card constructs exactly one `user` message and calls:

```python
tokenizer.apply_chat_template(
    chat,
    tokenize=True,
    add_generation_prompt=True,
    return_tensors="pt",
)
```

It supplies no system message. With the pinned chat template, the rendered
shape is:

```text
<|im_start|>user
{USER_CONTENT}<|im_end|>
<|im_start|>assistant
```

No `enable_thinking=false` option is passed. The model is therefore allowed to
produce its normal Qwen3 reasoning before the final Lean fence.

The card-exact user-template value, represented as one JSON string, is:

```json
"Please autoformalize the following natural language problem statement in Lean 4. Use the following theorem name: {THEOREM_NAME}\nThe natural language statement is: \n{NL_STATEMENT}Think before you provide the lean statement."
```

Its decoded UTF-8 SHA-256 is:

```text
fb7ec1dd8ebe99706862038e7b4c2aaef0fa04ca339886fe33a329f2d77ff7fa
```

The absence of a newline between `{NL_STATEMENT}` and `Think` is present in
the primary card and is not a transcription error.

For LeanFaith, append the already frozen common output contract after two
newlines. The patch-ready user-template value is:

```json
"Please autoformalize the following natural language problem statement in Lean 4. Use the following theorem name: {THEOREM_NAME}\nThe natural language statement is: \n{NL_STATEMENT}Think before you provide the lean statement.\n\n{COMMON_SUFFIX}"
```

Its decoded UTF-8 SHA-256 is:

```text
3bf3984277543a516e3e543cbaa25924f0a8b568ecb5fd93427d5f0b4ca15449
```

The pinned tokenizer chat-template string has SHA-256:

```text
a55ee1b1660128b7098723e0abcd92caa0788061051c62d51cbe87d9cf1974d8
```

### Goedel decoding facts

The card and `generation_config.json` agree on:

```yaml
do_sample: true
temperature: 0.9
top_k: 20
top_p: 0.95
```

The card additionally supplies:

```yaml
max_new_tokens: 16384
seed: 30
num_return_sequences: 1  # implicit model.generate default
num_beams: 1             # implicit model.generate default
repetition_penalty: 1.0  # implicit model.generate default
dtype: bfloat16
```

Patch-ready qualification values should preserve those settings and make
every implicit default explicit. `seed: 30` is a primary-card fact, not a
LeanFaith-selected seed.

The card loads the model with `trust_remote_code=True`, but the pinned config
contains the standard `Qwen3ForCausalLM` architecture and no `auto_map`
declaration. Under LeanFaith's fail-closed policy, the card's flag is an
observed example, not authorization: `allow_remote_code` remains false unless
a separate authorization is recorded.

The card decodes the entire prompt plus completion and extracts the last
lowercase `lean4` fence. LeanFaith must instead slice generated token IDs
after the input length, persist and parse completion-only text, and apply the
stricter single-final-fence adapter.

### Goedel implementation prerequisite

`generation_config.json` defines two EOS IDs:

```json
[151645, 151643]
```

The current `LocalHFDecodingConfig.eos_token_id` accepts only one integer.
Before Goedel qualification, the local runtime must either:

1. support an ordered nonempty tuple/list of EOS IDs and pass both IDs to
   `generate`; or
2. omit an explicit request-level EOS override, use the pinned model
   generation config unchanged, and persist the effective two-ID value.

Silently keeping only one EOS ID is not acceptable.

## StepFun prompt and chat contract

The card constructs two messages:

```json
[
  {"role": "system", "content": "You are an expert in mathematics and Lean 4."},
  {"role": "user", "content": "{USER_CONTENT}"}
]
```

It then performs:

```python
tokenizer.apply_chat_template(
    dialog,
    tokenize=False,
    add_generation_prompt=True,
) + "<think>"
```

The literal `<think>` is appended **after** the fully rendered chat template.
It is not part of the user message.

The system-prompt UTF-8 SHA-256 is:

```text
9836ff56bfca9ec0062ac9363b26e7fe0a6d663281bc5436b86282306b7ce68a
```

The card-exact user template fixes the example theorem name. Replacing only
the problem and registered header with placeholders gives this audit value:

```json
"Please autoformalize the following problem in Lean 4 with a header. Use the following theorem names: my_favorite_theorem.\n\n{NL_STATEMENT}\n\nYour code should start with:\n```Lean4\n{REGISTERED_HEADER}\n```\n"
```

Its decoded UTF-8 SHA-256 is:

```text
8fb48b991251784b5f4d5b1aac48d609d1bd6e61f602a21890eff2a87fd92db0
```

The patch-ready LeanFaith template replaces the fixed theorem name and appends
the frozen common output contract:

```json
"Please autoformalize the following problem in Lean 4 with a header. Use the following theorem names: {THEOREM_NAME}.\n\n{NL_STATEMENT}\n\nYour code should start with:\n```Lean4\n{REGISTERED_HEADER}\n```\n\n{COMMON_SUFFIX}"
```

Its decoded UTF-8 SHA-256 is:

```text
8cb6ac391ee0fba7896bfddd8da4b8bf63f77e44431bc407282bde8da3f9d502
```

The pinned tokenizer chat-template string has SHA-256:

```text
b6835114b7303ddd78919a82e4d9f7d8c26ed0d7dfc36beeb12d524f6144eab1
```

The appended assistant prefix is exactly seven UTF-8 bytes:

```text
<think>
```

Its SHA-256 is:

```text
7d329bb7d9d43bf17bcafd4cb8203e1b94423923e87980bd1d2d9fc525d50b99
```

The header fence is part of the **input prompt**. Parsing the concatenated
prompt and completion would therefore create a false multiple-fence result.
Only completion token IDs may be decoded into the raw output passed to the
final-fence parser.

### StepFun decoding facts

The card and `generation_config.json` agree on:

```yaml
do_sample: true
temperature: 0.6
top_p: 0.95
```

The card additionally supplies:

```yaml
max_new_tokens: 16384
num_return_sequences: 1
```

Neither primary artifact specifies `top_k`, `repetition_penalty`, or a random
seed. Patch-ready qualification must therefore use:

```yaml
top_k: null
repetition_penalty: 1.0
seed: 0  # LeanFaith fixture choice; not attributed to the StepFun card
num_beams: 1
dtype: bfloat16
```

The card uses vLLM with `tensor_parallel_size=4` for its example. That is a
runtime example, not a scientific requirement or hardware mandate. A
LeanFaith qualification may use its authorized local runtime, but it must
record the actually exercised runtime and precision.

### StepFun implementation prerequisite

The current generic `ChatTemplatePromptFormatter` can add a system message and
request `add_generation_prompt=true`, but it cannot append text after the
rendered template. Adding `<think>` to the unrendered user prompt would be
incorrect.

Before StepFun qualification, implement and test either:

1. a model-specific formatter that appends literal `<think>` after
   `apply_chat_template`; or
2. a strictly typed `assistant_prefix_after_template` option whose value and
   hash are bound in the request and result.

The formatter test must prove that `<think>` occurs after the assistant marker
and outside the user message.

## Shared LeanFaith output contract

Both recommended wrappers reference:

```text
prompts/autoformalizers/common_final_fence_v1.txt
```

The current file is 299 bytes with SHA-256:

```text
0bdd45e0e8ed86ce3eba34399d74297e180aa97eacd87c1604d0e14dbf15f222
```

This suffix is a LeanFaith constraint, not a claim about either primary model
card. The eventual prompt files must contain literal expanded suffix text at
render time, and the run must bind the template artifact, common-suffix
artifact, rendered prompt, formatter source, parser source, and their hashes.

## Patch-ready summary

| Field | Goedel | StepFun |
|---|---|---|
| formatter ID | `goedel_v2_card_chat_v1` | `stepfun_card_think_v1` |
| messages | user only | system + user |
| add generation prompt | true | true |
| post-template assistant prefix | none | literal `<think>` |
| do sample | true | true |
| temperature | 0.9 | 0.6 |
| top-p | 0.95 | 0.95 |
| top-k | 20 | null |
| max new tokens | 16,384 | 16,384 |
| seed | 30 from card | 0 LeanFaith choice |
| EOS | `[151645, 151643]` | use pinned effective config; observed `151643` |
| remote code | card says true; LeanFaith remains false absent authorization | false |
| decode for parser | completion only | completion only |
| blocking schema change | multi-EOS preservation | post-template `<think>` suffix |

These facts are sufficient to patch the next two qualification inputs without
guessing model-specific behavior. They do not activate either checkpoint and
do not alter ADR-0006's pending-activation decision.
