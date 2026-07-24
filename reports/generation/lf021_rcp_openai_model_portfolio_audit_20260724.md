# LF-021 RCP and OpenAI Generation Portfolio Audit

Audited at: 2026-07-24  
Audit mode: read-only catalog, configuration, artifact, and documentation review  
Decision status: recommendation only; no portfolio mutation or generation authorization

## 1. Verdict

The proposed portfolio is sound with one required future correction:
generation settings must be versioned per model rather than inherited from one
blanket RCP reasoning configuration.

The recommended role ordering is:

1. `moonshotai/Kimi-K2.7-Code` as the primary RCP candidate;
2. `Qwen/Qwen3.6-35B-A3B` as the preferred distinct-family RCP candidate;
3. OpenAI Codex as a third, high-value proposal family rather than the routine
   bulk path;
4. `moonshotai/Kimi-K2.6` as the Moonshot fallback/ablation;
5. `Qwen/Qwen3.5-397B-A17B` as a Qwen capacity ablation;
6. `Qwen/Qwen3-30B-A3B-Instruct-2507` as the cheaper, non-thinking Qwen
   fallback;
7. `Qwen/Qwen3-VL-235B-A22B-Thinking` excluded from the default text-only path.

This audit does **not** establish that Kimi is semantically better than Qwen on
Lean theorem-statement autoformalization. That is a reasonable working prior,
but it requires a blinded, same-prompt public-data comparison. Model cards and
one operationally valid output cannot establish that ranking.

No bulk generation is authorized by this report. No remote output is promoted
to supervision, a semantic label, held-out evidence, or Gate credit.

## 2. Safety and audit boundary

The audit:

- read the live RCP `/models` catalog;
- read existing LeanFaith qualification artifacts;
- inspected the RCP adapter pattern in `~/LeanFlow`;
- inspected the local Codex CLI model catalog and authentication status;
- read official EPFL, Moonshot, Qwen, and OpenAI documentation;
- made no chat-completion or Codex generation request;
- transmitted no theorem, natural-language problem, Lean reference, or private
  source content;
- did not print or persist `RCP_API_KEY`;
- did not edit the frozen v1 portfolio or its bound policy.

`formalmathatepfl/sft_classic` remains prohibited from RCP, Codex, and every
other external provider. Only explicitly approved public-source records may be
used in a later qualification.

## 3. Live RCP catalog verification

The authenticated OpenAI-compatible endpoint returned HTTP 200 with 284 route
entries. Its raw-response SHA-256 was:

```text
0f98154b24af983bcdb22e6248bf98cbe5456fa9cd78932d280262777877dd66
```

This exactly matches the observation frozen in
`configs/generation/rcp_provider_portfolio_v1.yaml`; the catalog had not changed
between the two observations.

The EPFL service documents an OpenAI-compatible API and dynamic model loading.
A listed route can therefore require a cold start. Catalog presence establishes
that a route was advertised at the observation time; it does not establish:

- that a completion request will succeed;
- that every supplied decoding field is applied;
- an immutable upstream checkpoint revision;
- a model training cutoff;
- freedom from benchmark or mathlib contamination.

The catalog's generic `owned_by: "openai"` value appears on routes from multiple
organizations. It is service compatibility metadata and must not be used as
model-ownership or family evidence.

### 3.1 Exact route inventory

| User-proposed model | Exact advertised base route | Additional advertised service variants | LeanFaith family | Current evidence |
|---|---|---|---|---|
| Kimi K2.7 Code | `moonshotai/Kimi-K2.7-Code` | `moonshotai/Kimi-K2.7-Code-int4` | `moonshot_kimi_k2` | Base route has one replay-valid public, reference-hidden HTTP-200 qualification |
| Kimi K2.6 | `moonshotai/Kimi-K2.6` | `moonshotai/Kimi-K2.6-int4` | `moonshot_kimi_k2` | Catalog only; no K2.6 completion in the qualification bundle |
| Qwen3.6 35B A3B | `Qwen/Qwen3.6-35B-A3B` | `Qwen/Qwen3.6-35B-A3B-fp8` | `qwen3` | Base route has one public, reference-hidden HTTP-200 qualification |
| Qwen3.5 397B A17B | `Qwen/Qwen3.5-397B-A17B` | `Qwen/Qwen3.5-397B-A17B-fp8`; `Qwen/Qwen3.5-397B-A17B-int4` | `qwen3` | Catalog only |
| Qwen3 30B A3B Instruct 2507 | `Qwen/Qwen3-30B-A3B-Instruct-2507` | `Qwen/Qwen3-30B-A3B-Instruct-2507-bfloat16` | `qwen3` | Catalog only |
| Qwen3 VL 235B A22B Thinking | `Qwen/Qwen3-VL-235B-A22B-Thinking` | `Qwen/Qwen3-VL-235B-A22B-Thinking-fp8` | `qwen3` | Catalog only; intentionally excluded from the default text-only path |

Base, FP8, INT4, and BF16 route names are distinct serving routes. A future
runner must never silently substitute one for another. A quantized or alternate
precision route needs its own catalog observation and one-example qualification,
but it remains in the same model family for diversity accounting.

### 3.2 OpenAI-named RCP routes

The catalog also exposes:

```text
openai/gpt-oss-120b
openai/gpt-oss-120b-bfloat16
openai/gpt-oss-20b
openai/gpt-oss-20b-bfloat16
```

These are not proprietary ChatGPT/Codex access and must not be conflated with
the local Codex CLI models. They are optional open-weight RCP candidates and
would require the same qualification process as any other RCP route.

## 4. Model-specific reasoning and decoding contracts

The frozen v1 portfolio contains the blanket settings:

```yaml
chat_template_kwargs:
  enable_thinking: true
reasoning_effort: high
```

That is suitable as an initial transport hypothesis, but it is not a valid
cross-model decoding contract.

### 4.1 Kimi K2.7 Code

The official Kimi K2.7 Code model card states that thinking is forced and
recommends `temperature=1.0` and `top_p=0.95` for thinking-mode inference.

The existing Kimi one-problem qualification instead used:

```text
temperature = 0.0
reasoning_effort = high
enable_thinking = true
```

Its HTTP-200 result and Lean elaboration demonstrate only that the route and
adapter worked operationally. They are not representative quality evidence
under the upstream recommended generation configuration. A future v2 scientific
configuration should use the pinned K2.7-specific settings and qualify them on
one public example before any larger run.

### 4.2 Kimi K2.6

Kimi K2.6 supports thinking and instant modes, but its documented vLLM/SGLang
switch uses model-specific `chat_template_kwargs` semantics, including a
`thinking` key for disabling thinking. A generic `enable_thinking` field must
not be assumed to control this route without a capability qualification.

K2.6 remains a fallback and within-Moonshot ablation. It does not add a second
independent generator family.

### 4.3 Qwen3.6 35B A3B

The official Qwen3.6 card recommends the following precise coding/thinking
profile:

```text
temperature = 0.6
top_p = 0.95
top_k = 20
min_p = 0.0
presence_penalty = 0.0
repetition_penalty = 1.0
enable_thinking = true
```

The existing Qwen3.6 one-problem qualification matches this profile. The route
accepted the combined payload without field-removal retries and returned HTTP
200. This proves payload acceptance, not the independent application of each
field and not semantic faithfulness.

### 4.4 Qwen3.5 397B A17B

The official Qwen3.5 card recommends the same thinking-mode sampling profile as
Qwen3.6. This model should remain an upper-capacity ablation. Its much larger
total and active parameter counts do not themselves predict better
autoformalization faithfulness.

### 4.5 Qwen3 30B A3B Instruct 2507

The official card states that this checkpoint is non-thinking and does not
produce `<think>` blocks. Its documented default recommendation is approximately:

```text
temperature = 0.7
top_p = 0.8
top_k = 20
min_p = 0.0
```

The future per-model contract must omit thinking fields for this route. Sending
the portfolio-wide `enable_thinking=true` would be semantically misleading even
if the server accepts or ignores it.

### 4.6 Qwen3 VL 235B A22B Thinking

This is a multimodal model. LeanFaith's present generation problem is
text-to-text, so the VL checkpoint adds complexity without a justified
information source. Keep it excluded unless a later, separately scoped
diagram/image-to-Lean experiment is approved.

## 5. LeanFlow RCP adapter finding

`~/LeanFlow/agent/providers/api_caller.py` applies a generic RCP policy:

- `chat_template_kwargs.enable_thinking` mirrors whether reasoning is enabled;
- RCP `reasoning_effort` is sent when reasoning is enabled;
- `xhigh` is mapped to RCP `high`.

The associated tests confirm that RCP defaults to high reasoning and that
`xhigh` is clamped to `high`.

This makes the user's proposed `high` setting correct at the generic RCP
transport layer. It does **not** remove the need for model-specific request
bodies:

- Kimi K2.7 forces thinking;
- Kimi K2.6 documents different thinking-toggle semantics;
- Qwen3.6 and Qwen3.5 accept `enable_thinking`;
- Qwen3 30B Instruct is explicitly non-thinking.

Recommendation: retain LeanFlow's transport behavior for its own workflows, but
make LeanFaith's future v2 qualification config select a frozen capability and
decoding contract by exact route ID. Fail closed if an exact route has no
contract.

## 6. OpenAI Codex access

The local environment was safely verified as:

```text
codex-cli 0.144.1
authentication: ChatGPT login active
```

The read-only local Codex catalog exposed these user-selectable models:

```text
gpt-5.6-sol
gpt-5.6-terra
gpt-5.6-luna
gpt-5.5
gpt-5.4
gpt-5.4-mini
gpt-5.3-codex-spark
```

The existing isolated `gpt-5.6-terra` qualification used `xhigh` reasoning,
prompt-via-stdin, an empty working directory, read-only sandboxing, disabled web
search, no inherited environment, and a strict output schema. It completed
successfully and was recovered offline without a second provider call.

The recovered execution used 9,742 input tokens for 187 output tokens, including
127 reasoning tokens. An earlier transport probe used 16,573 input tokens. This
large fixed agent-context overhead makes direct `codex exec` a poor default for
routine high-volume candidate generation.

Recommended Codex role:

- high-value open-ended adversarial proposals;
- difficult statement variants where agent reasoning may be valuable;
- a third generator family for diversity;
- never its own validator;
- not a clean held-out OpenAI judge if OpenAI Codex output enters training
  supervision;
- no private-source prompt content.

Recommended transport split:

- use the direct RCP OpenAI-compatible client for repeatable, high-volume RCP
  generation;
- use `codex exec` selectively for high-value public-source proposals;
- do not route RCP Chat Completions through Codex merely to unify interfaces,
  because that adds agent context and obscures the simpler provider request.

## 7. Family and judge accounting

The following family accounting is mandatory:

| Routes/checkpoints | One family ID | Independent-family credit |
|---|---|---:|
| Kimi K2.7 and K2.6, including precision variants | `moonshot_kimi_k2` | 1 total |
| Every listed Qwen checkpoint and precision variant | `qwen3` | 1 total |
| Proprietary models accessed through Codex CLI | `openai_codex` | 1 total |

Model size, checkpoint version, and quantization do not create independent
families.

If Moonshot, Qwen, and OpenAI Codex are all used as proposers or weak
supervisors, none of those same families may serve as the primary clean held-out
judge. The clean judge must come from a family excluded from all training-time
supervision.

## 8. Current qualification evidence

The repository already contains one-problem operational evidence for:

| Model | Result | What it proves | What it does not prove |
|---|---|---|---|
| `moonshotai/Kimi-K2.7-Code` | HTTP 200; output parsed; Lean status `valid_with_sorry` | Exact base route and adapter completed one public reference-hidden request | Faithfulness, superiority, supervision eligibility, contamination safety |
| `Qwen/Qwen3.6-35B-A3B` | HTTP 200; output parsed; Lean status `valid_with_sorry` | Exact base route accepted the complete requested payload and completed one public reference-hidden request | Independent application of every payload field, faithfulness, superiority |
| `gpt-5.6-terra` via Codex | Process exit 0; structured output recovered; Lean status `valid_with_sorry` | Isolated Codex adapter works for one public reference-hidden request | Faithfulness, inexpensive bulk suitability, immutable checkpoint identity |

Lean compilation with `sorry` is an operational check only. It must never be
reported as semantic success.

The combined read-only audit records one non-blocking metadata defect: the Qwen
config's `frozen_at` timestamp is later than its persisted execution time. The
bound SHA-256 values still match. Preserve the existing evidence unchanged and
correct timestamps only in a new version.

## 9. Recommended next portfolio version

Do not mutate the frozen v1 portfolio. A future v2 should:

1. preserve the current role ordering and family grouping;
2. replace blanket decoding with exact-route capability contracts;
3. distinguish `catalog_listed`, `transport_qualified`,
   `operationally_elaborated`, `human_quality_audited`, and
   `scientifically_admitted`;
4. record base and alternate-precision routes separately;
5. use upstream-recommended sampling settings unless a preregistered ablation
   says otherwise;
6. keep training cutoff and contamination status `unknown` when RCP exposes no
   immutable revision;
7. require the same frozen public prompt and hidden-reference policy for a
   blinded one-example comparison;
8. require human semantic review before declaring one model better;
9. require a new authorization record before more than one qualification
   request per route or any bulk collection;
10. preserve the prohibition on private `sft_classic` transmission.

Suggested model-specific contracts for that future v2:

| Exact route | Role | Contract |
|---|---|---|
| `moonshotai/Kimi-K2.7-Code` | primary | forced thinking; `temperature=1.0`, `top_p=0.95`; RCP `reasoning_effort=high` only if separately observed |
| `Qwen/Qwen3.6-35B-A3B` | distinct-family candidate | current precise-code settings; thinking enabled |
| `gpt-5.6-terra` via Codex | selective third family | isolated `codex exec`; `xhigh`; strict schema; high-value public prompts only |
| `moonshotai/Kimi-K2.6` | fallback/ablation | model-specific thinking contract established by one-example capability test |
| `Qwen/Qwen3.5-397B-A17B` | capacity ablation | Qwen thinking profile |
| `Qwen/Qwen3-30B-A3B-Instruct-2507` | cheap fallback | non-thinking; omit thinking fields; `temperature=0.7`, `top_p=0.8`, `top_k=20`, `min_p=0.0` |
| `Qwen/Qwen3-VL-235B-A22B-Thinking` | excluded | no default text-only execution |

## 10. Evidence references

Local frozen evidence:

- `configs/generation/rcp_provider_portfolio_v1.yaml`  
  SHA-256 `6f6a79159ff68a3cb4bec59fd52a21e84a7f84b721622521046c40d60557c298`
- `policies/rcp_remote_generation_v1.yaml`  
  SHA-256 `c924efacdf2bd28373eed255c9b04b8e45cb56d430b2f4906b253ab63d661470`
- `configs/generation/rcp_qwen_qualification_v1.yaml`  
  SHA-256 `9a6bc2ee1a983f9457c2b327df75f38e3937aa68bf4c1e6d0d756dcc1be7b2b1`
- `configs/generation/codex_exec_public_qualification_v1.json`  
  SHA-256 `b5d5a546b35e6a9e184f11931a1d38d9bd0d8d4649a37c6d59ad734c1e804017`
- `reports/generation/lf021_remote_one_problem_qualifications_combined_audit_v1.json`

Primary public documentation:

- [EPFL RCP AI Inference as a Service](https://www.epfl.ch/research/facilities/rcp/ai-inference-as-a-service/)
- [Kimi K2.7 Code model card](https://huggingface.co/moonshotai/Kimi-K2.7-Code)
- [Kimi K2.6 model card](https://huggingface.co/moonshotai/Kimi-K2.6)
- [Qwen3.6 35B A3B model card](https://huggingface.co/Qwen/Qwen3.6-35B-A3B)
- [Qwen3.5 397B A17B model card](https://huggingface.co/Qwen/Qwen3.5-397B-A17B)
- [Qwen3 30B A3B Instruct 2507 model card](https://huggingface.co/Qwen/Qwen3-30B-A3B-Instruct-2507)
- [Qwen3 VL 235B A22B Thinking model card](https://huggingface.co/Qwen/Qwen3-VL-235B-A22B-Thinking)
- [OpenAI Codex non-interactive mode](https://developers.openai.com/codex/noninteractive)
- [OpenAI Codex models](https://developers.openai.com/codex/models)

## 11. Authorization outcome

```text
portfolio_v1_mutated: false
bulk_generation_authorized: false
catalog_requests_performed: 1
new_generation_requests_performed: 0
private_source_transmission_performed: false
semantic_labels_created: false
supervision_eligible: false
gate_credit_claimed: false
```
