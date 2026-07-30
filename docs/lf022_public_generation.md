# LF-022 public provisional generation

LF-022 external generation is a public-mathlib-only collection workflow.
Generated statements are always unresolved, unvalidated, and provisional.
They are not semantic labels, training data, evaluation data, silver/gold
records, or Gate credit.

## Supported proposer routes

- `moonshotai/Kimi-K2.7-Code` has an already reviewed public provisional route.
- `Qwen/Qwen3.5-397B-A17B` is blocked until its one exact proposer
  qualification succeeds and is certified.
- `zai-org/GLM-5.2` is blocked until its one exact proposer qualification
  succeeds and is certified.

The matching `*_production_route_v1.yaml` files are non-executable policy
templates. Only a content-addressed execution admission that binds a verified
eligibility record can activate a production route.

Qwen and GLM were previously observed as judges. That is transport evidence,
not proposer qualification. Their qualification contracts bind the exact
model, provider catalog, prompt, decoding fields, code bundle, allocation, and
public source. `reasoning_effort=high` and
`chat_template_kwargs.enable_thinking=true` are sent only because those exact
fields are explicitly allowed by each bound capability policy. Unknown
reasoning fields are rejected rather than renamed or silently dropped.

## Qualification and activation

For each of Qwen and GLM:

1. Starting from a canonical reviewed admission with
   `execution_scope=one_item_proposer_qualification_only`, create the exact
   one-task request offline:

   ```bash
   uv run leanfaith make-lf022-public-batch-request \
     --admission PATH_TO_REVIEWED_QUALIFICATION_ADMISSION \
     --allocation-task-id lf022_production_task:... \
     --output data/lf022_qwen_or_glm_qualification_request.json
   ```

   The command verifies that the selected task is a public `G_open` allocation
   for that admitted family. Qwen/GLM qualification requests reject any second
   task.
2. Freeze the request:

   ```bash
   uv run leanfaith freeze-lf022-public-batch \
     --request data/lf022_qwen_or_glm_qualification_request.json
   ```

3. Replay the frozen batch offline. This performs no provider call.
4. Set `RCP_BASE_URL` and `RCP_API_KEY` only in the runtime environment, then
   make the single explicit live call:

   ```bash
   uv run leanfaith run-lf022-public-batch \
     --manifest PATH_TO_FROZEN_QUALIFICATION_BATCH \
     --execute-public-provisional \
     --max-concurrency 1 \
     --minimum-request-interval-seconds 1
   ```

5. Replay the same batch offline. A successful terminal must contain exactly
   one unvalidated provisional variant and the complete request/response
   lineage.
6. Certify the exact qualification:

   ```bash
   uv run leanfaith certify-lf022-proposer-route \
     --qualification-admission PATH_TO_FROZEN_QUALIFICATION_ADMISSION \
     --qualification-task PATH_TO_FROZEN_QUALIFICATION_TASK
   ```

   Certification performs zero network calls. It exact-replays the persisted
   provider request, raw response, parser result, LLM call/attempt lineage, and
   variant bytes. It writes exactly one immutable family record under
   `data/lf022_execution/production_eligibility/`.

7. Construct the production route with the same provider deployment, catalog
   snapshot, prompt, decoding contract, and family matrix; change only
   `execution_scope` to `public_provisional_g_open` and bind the exact
   `proposer_production_eligibility` artifact. The admission verifier replays
   the qualification again. A missing, changed, cross-family, or hand-written
   eligibility record fails closed.

Qualification and Kimi admissions remain byte-compatible schema v1 records.
The first admission containing `proposer_production_eligibility` is schema v2;
new readers accept both, while v1 cannot carry production eligibility.

The repository-global qualification claim prevents a second task from silently
replacing the first qualification. Provider retries remain bounded and
append-only; an unknown transport outcome is quarantined and never retried
automatically.

## Separation and privacy invariants

- `formalmathatepfl/sft_classic` and every private-source marker are rejected
  before prompt rendering and again before transport.
- Optional natural-language content is forbidden on the public `G_open` route.
- The allocation plan binds two judge families distinct from the proposer.
- Any later SCI validator must come from a different family.
- The held-out OpenAI/Codex evaluation family cannot supervise generation,
  validation, judging, or training.
- Tests and ordinary commands are offline. Live calls require the explicit
  `--execute-public-provisional` flag and runtime-only credentials.
- Request bytes, raw response bytes, retries, parse failures, and terminal
  results are content-addressed and resumable.
