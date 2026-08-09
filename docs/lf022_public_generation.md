# LF-022 public provisional generation

LF-022 external generation is a public-mathlib-only collection workflow.
Generated statements are always unresolved, unvalidated, and provisional.
They are not semantic labels, training data, evaluation data, silver/gold
records, or Gate credit.

## Supported proposer routes

- `moonshotai/Kimi-K2.7-Code` has an already reviewed public provisional route.
- `Qwen/Qwen3.5-397B-A17B` may enter scientific production only through its
  exact replay-verified v2 proposer eligibility.
- `zai-org/GLM-5.2` may enter scientific production only through its exact
  replay-verified v2 proposer eligibility.

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

The route runner consumes an execution admission; a route-contract YAML is not
an admission. Build the family-specific one-source diagnostic allocation and
freeze the execution admission entirely offline before resolving credentials.

For Qwen and GLM, derive the diagnostic pool from the already immutable,
exact-replayed `repr_v3` public pool. Do **not** rematerialize that pool from the
old extraction after extraction-only code changes, weaken/refreeze its reviewed
reuse attestation, or fall back to the old pre-`repr_v3` smoke representations.
The derivation binds and replays the parent audit, all selected parent files,
the exact source/representation/context/denylist clearance, the chosen family,
and the current clean code tree.

Run from a clean repository tree:

```bash
ROOT=/localhome/milikic/LeanFaith
cd "$ROOT"
test -z "$(git status --porcelain)"

# Set one family at a time: qwen3 or glm5.
FAMILY=qwen3
QUAL_ROOT="artifacts/generation/lf022_${FAMILY}_diagnostic_from_v3_max"
PARENT_AUDIT="artifacts/generation/lf022_public_v3_max_c799f54c/audit.json"

uv run leanfaith derive-lf022-diagnostic-subpool \
  --parent-pool-audit "$PARENT_AUDIT" \
  --proposer-family "$FAMILY" \
  --out-dir "$QUAL_ROOT"

BUNDLE_JSON=$(
  uv run leanfaith freeze-code-bundle \
    --root "$ROOT" \
    --out-dir artifacts/code_bundles/lf022_proposer_qualification
)
BUNDLE=$(
  python -c 'import json,sys; print(json.load(sys.stdin)["path"])' \
    <<<"$BUNDLE_JSON"
)

ADMISSION="$QUAL_ROOT/execution_admission.json"
uv run leanfaith freeze-lf022-proposer-admission \
  --root "$ROOT" \
  --public-pool-audit "$QUAL_ROOT/audit.json" \
  --proposer-family "$FAMILY" \
  --code-bundle "$BUNDLE" \
  --output "$ADMISSION"
```

The derivation selects the first theorem in the parent audit's frozen selection
order and requires its parent representation to be `repr_v3`. It emits exactly
one byte-identical source, theorem, representation, context, and denylist
clearance, plus a two-task `G_sci`/`G_open` plan assigned to the requested
family. The admission freezer exact-replays that parent lineage, the raw and
normalized provider catalogs, route-specific contract, prior transport
evidence, prompt, code bundle, and current code-tree hash. Neither command reads
credentials or performs a network request.

New execution tasks send a self-contained proof-free named signature built
from the bound `signature_pp` representation, for example
`theorem Mathlib.name : ∀ ..., ...`. This deliberately exposes section
variables and typeclass binders that may be implicit in source text, while
excluding attributes, proof placeholders, and proof bodies. The theorem,
context, representation ID, and `repr_v3` normalization version must agree
exactly. Legacy schema-v1 tasks remain replayable against their original
source bytes; new writers always emit `source_statement_version=named_signature_v2`.

For Qwen and GLM, derive the one exact public `G_open` allocation, freeze the
batch, and preflight it without credentials:

```bash
TASK_ID=$(
  python - "$ROOT" "$ADMISSION" "$FAMILY" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
admission = json.load(open(sys.argv[2], encoding="utf-8"))
family = sys.argv[3]
assert admission["route"]["proposer_family_id"] == family
assert admission["route"]["execution_scope"] == (
    "one_item_proposer_qualification_only"
)
plan = json.load(
    open(root / admission["artifacts"]["allocation_plan"]["path"], encoding="utf-8")
)
assert plan["profile"] == "diagnostic_scaffold"
assert len(plan["tasks"]) == 2
task_ids = [
    task["task_id"]
    for task in plan["tasks"]
    if task["distribution"] == "G_open"
    and task["proposer_family_id"] == family
]
assert len(task_ids) == 1
print(task_ids[0])
PY
)

REQUEST="data/lf022_qualification/${FAMILY}/request.json"
BATCH_DIR="data/lf022_qualification/${FAMILY}/batch"
uv run leanfaith make-lf022-public-batch-request \
  --root "$ROOT" \
  --admission "$ADMISSION" \
  --allocation-task-id "$TASK_ID" \
  --output "$REQUEST" \
  --batch-directory "$BATCH_DIR"
uv run leanfaith freeze-lf022-public-batch \
  --root "$ROOT" \
  --request "$REQUEST"

MANIFEST="$BATCH_DIR/batch_manifest.json"
env -u RCP_BASE_URL -u RCP_API_KEY -u OPENAI_BASE_URL -u OPENAI_API_KEY \
  uv run leanfaith run-lf022-public-batch \
    --root "$ROOT" \
    --manifest "$MANIFEST" \
    --max-concurrency 1 \
    --minimum-request-interval-seconds 1
```

A fresh preflight must report `tasks=1`, `preflight_only=1`, `errors=0`, and
`network_calls_this_run=0`. Freezing also creates exactly one immutable global
qualification claim for Qwen or GLM. Do not proceed if a claim already exists
with different bytes.

Set runtime-only credentials, verify the exact admitted endpoint without
printing the key, and perform the explicit one-task call:

```bash
test "${RCP_BASE_URL%/}" = "https://inference.rcp.epfl.ch/v1"
test -n "${RCP_API_KEY:-}"
set +x
env -u OPENAI_BASE_URL -u OPENAI_API_KEY \
  RCP_BASE_URL="$RCP_BASE_URL" \
  RCP_API_KEY="$RCP_API_KEY" \
  uv run leanfaith run-lf022-public-batch \
    --root "$ROOT" \
    --manifest "$MANIFEST" \
    --execute-public-provisional \
    --max-concurrency 1 \
    --minimum-request-interval-seconds 1
```

The normal success path reports `successful_terminal=1`,
`failed_terminal=0`, one new terminal, and one network call. A provider or
parser terminal reports `successful_terminal=0`, `failed_terminal=1`, while
`errors` remains reserved for orchestration/executor rejection. The
frozen retry policy permits at most three attempts for explicitly listed
response-confirmed transient HTTP statuses; an unknown transport outcome is
quarantined and is never retried automatically. Never continue to certification
when `tasks != 1`, `errors != 0`, `failed_terminal != 0`, or no successful
terminal exists.

Replay the same result without credentials:

```bash
env -u RCP_BASE_URL -u RCP_API_KEY -u OPENAI_BASE_URL -u OPENAI_API_KEY \
  uv run leanfaith run-lf022-public-batch \
    --root "$ROOT" \
    --manifest "$MANIFEST" \
    --max-concurrency 1 \
    --minimum-request-interval-seconds 1
```

This must report one replayed terminal and zero network calls. Resolve the
frozen admission and task from the manifest, then certify Qwen or GLM:

```bash
read -r QUAL_ADMISSION QUAL_TASK < <(
  python - "$ROOT" "$MANIFEST" "$FAMILY" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
manifest = json.load(open(sys.argv[2], encoding="utf-8"))
family = sys.argv[3]
routes = [
    route for route in manifest["routes"]
    if route["proposer_family_id"] == family
]
assert len(routes) == 1 and len(routes[0]["tasks"]) == 1
print(
    root / routes[0]["admission"]["path"],
    root / routes[0]["tasks"][0]["task"]["path"],
)
PY
)

env -u RCP_BASE_URL -u RCP_API_KEY -u OPENAI_BASE_URL -u OPENAI_API_KEY \
  uv run leanfaith certify-lf022-proposer-route \
    --root "$ROOT" \
    --qualification-admission "$QUAL_ADMISSION" \
    --qualification-task "$QUAL_TASK"
```

Certification performs zero network calls. It exact-replays the persisted
provider request, raw response, parser result, LLM call/attempt lineage, and
variant bytes. It requires exactly one provisional variant and writes exactly
one immutable record at
`data/lf022_execution/production_eligibility/qwen3.json` or
`data/lf022_execution/production_eligibility/glm5.json`.

Construct a later production admission with the same deployment, catalog,
prompt, decoding contract, and family matrix; change only the execution scope
to `public_provisional_g_open` and bind that exact eligibility artifact. The
admission verifier replays the qualification again. A missing, changed,
cross-family, or hand-written eligibility record fails closed.

Qualification and Kimi admissions remain byte-compatible schema-v1 records.
The first admission containing `proposer_production_eligibility` is schema v2;
new readers accept both, while v1 cannot carry production eligibility.

The repository-global qualification claim prevents a second task from silently
replacing the first qualification. A replay-verified `provider_exhausted` or
`proposer_parse_failed` terminal may authorize one fresh, versioned attempt
without deleting or changing the failed lineage:

```bash
uv run leanfaith supersede-lf022-failed-qualification \
  --root "$ROOT" \
  --qualification-admission "$QUAL_ADMISSION" \
  --qualification-task "$QUAL_TASK" \
  --next-contract qwen3_5_proposer_qualification_v2
```

Use `glm5_2_proposer_qualification_v2` for GLM. The command performs no
network call and writes a content-addressed record below
`data/lf022_execution/qualification_supersessions/<family>/`. A successful
terminal and a `transport_unknown` terminal cannot be superseded. Freeze the
fresh admission with
`--qualification-supersession <record>`; its new claim is also
content-addressed under the family directory, while the legacy claim remains
unchanged. Provider retries remain bounded and append-only.

## Scientific replay-qualified Qwen and GLM tranches

After the one-item v2 route is certified, freeze a separate scientific
admission over the existing public pool. This does not reuse the diagnostic
allocation: it selects only that family's already-frozen scientific `G_open`
allocations and binds the canonical eligibility record, v2 decoding contract,
family matrix, catalog, prompt, and current code bundle.

```bash
ROOT=/localhome/milikic/LeanFaith
cd "$ROOT"
test -z "$(git status --porcelain)"

# Run one family at a time: qwen3 (9,207 G_open tasks) or glm5 (9,206).
FAMILY=qwen3
POOL_AUDIT="artifacts/generation/lf022_public_v3_max_c799f54c/audit.json"
ELIGIBILITY="data/lf022_execution/production_eligibility/${FAMILY}.json"

BUNDLE_JSON=$(
  uv run leanfaith freeze-code-bundle \
    --root "$ROOT" \
    --out-dir "artifacts/code_bundles/lf022_${FAMILY}_scientific"
)
BUNDLE=$(
  python -c 'import json,sys; print(json.load(sys.stdin)["path"])' \
    <<<"$BUNDLE_JSON"
)

ADMISSION="artifacts/generation/lf022_${FAMILY}_scientific_v1/execution_admission.json"
uv run leanfaith freeze-lf022-scientific-qualified-admission \
  --root "$ROOT" \
  --public-pool-audit "$POOL_AUDIT" \
  --proposer-family "$FAMILY" \
  --proposer-production-eligibility "$ELIGIBILITY" \
  --code-bundle "$BUNDLE" \
  --output "$ADMISSION"

# Deterministic gate 1: one task. Later use 256, then the family's full count.
REQUEST="data/lf022_${FAMILY}_scientific/prefix_1/request.json"
BATCH_DIR="data/lf022_${FAMILY}_scientific/prefix_1/batch"
uv run leanfaith make-lf022-public-batch-request \
  --root "$ROOT" \
  --admission "$ADMISSION" \
  --allocation-offset 0 \
  --allocation-limit 1 \
  --output "$REQUEST" \
  --batch-directory "$BATCH_DIR"
uv run leanfaith freeze-lf022-public-batch \
  --root "$ROOT" \
  --request "$REQUEST"

MANIFEST="$BATCH_DIR/batch_manifest.json"
env -u RCP_BASE_URL -u RCP_API_KEY -u OPENAI_BASE_URL -u OPENAI_API_KEY \
  uv run leanfaith run-lf022-public-batch \
    --root "$ROOT" \
    --manifest "$MANIFEST" \
    --max-concurrency 1 \
    --minimum-request-interval-seconds 1
```

Live RCP response headers observed during qualification reported
`max_parallel_requests=1`. This is an operational observation, not a field
bound by the frozen provider catalog. Until a new reviewed runtime-policy
version records contrary evidence, every live Qwen/GLM qualification and
production run must retain `--max-concurrency 1`.
The command performs no network request unless the operator repeats the frozen
manifest with runtime-only credentials and `--execute-public-provisional`.
Qwen uses `qwen3_5_proposer_qualification_v2`; GLM uses
`glm5_2_proposer_qualification_v2`. A v1, missing, changed, cross-family, or
matrix- or contract-mismatched eligibility record fails before transport.

Each family uses the same deterministic three-stage gate:

1. Freeze and execute `OFFSET=0, LIMIT=1`. Require one successful terminal,
   zero failed terminals, zero orchestration errors, and an exact offline
   replay before continuing.
2. Freeze and execute `OFFSET=0, LIMIT=256`. Require all 256 tasks to have
   terminal records, `error_count=0`, no `transport_unknown`, and at least 243
   `provisional_variants_created` terminals. Inspect a deterministic 32-item
   sample for prompt leakage, proof placeholders/bodies, malformed declaration
   boundaries, and duplicate output. This is operational generation QA, not
   semantic labeling.
3. Only after stage 2 passes, freeze `OFFSET=0` with `LIMIT=9207` for Qwen or
   `LIMIT=9206` for GLM. Existing deterministic task IDs replay from the global
   executor store, so widening the prefix must not repeat completed calls.

After the live prefix finishes, first run the batch command again without
credentials or `--execute-public-provisional` to create its exact offline
replay report. Then run the fixed operational audit. The audit may itself run
from newer QA code: it materializes the admission-bound code bundle in a
private checkout and replays all executor terminals in an isolated subprocess
whose `PYTHONPATH` contains only that historical source. Provider credentials
are stripped, every loaded `leanfaith.*` module is origin-checked, and the
report binds both the admitted historical code tree and the current QA
implementation code tree. Changing executor behavior still requires a new
reviewed admission; changing only QA code does not silently change replay
semantics:

```bash
uv run leanfaith qa-lf022-prefix256 \
  --root "$ROOT" \
  --manifest "$MANIFEST" \
  --offline-replay-report "$OFFLINE_REPLAY_REPORT" \
  --output-dir "$BATCH_DIR/operational_qa_v1"
```

The command has no sampling or threshold options. It requires exactly 256
validated terminals, an exact 256-task offline replay with zero network calls
and orchestration errors, no `transport_unknown`, and at least 243 successful
terminals. It independently reruns the admission-bound historical executor's
exact network-free terminal reconstruction for every task, requires every
historical terminal ID/path/SHA binding to match the current artifact set,
freezes the historical module bindings, rejects unbound extra task artifacts,
revalidates canonical provisional records with the production candidate
parser, rejects duplicate normalized output globally, and freezes a
hash-ranked 32-task reviewer JSONL. The report derives its complete failure-code
set from its persisted counts and bindings, so a hand-constructed incoherent
pass is invalid. The report and bundle are
operational QA only: they create no semantic label, promotion,
training/evaluation eligibility, or Gate credit. A failed threshold still
produces an immutable no-go report and reviewer bundle; when fewer than 32 tasks
succeeded, that failure-only bundle contains every successful task instead of
silently rejecting before the no-go report is written. The command exits
nonzero.

Every stage uses its own immutable request and batch manifest. A failed gate
requires a new reviewed contract/admission version; do not change the offset,
drop failures from the denominator, or substitute more tasks. All live stages
retain `--max-concurrency 1`.

Every output remains public-only, provisional, unresolved, unlabeled, and
ineligible for training, evaluation, promotion, or Gate credit.

## Scientific Kimi production tranches

Kimi already has a reviewed `public_provisional_g_open` route, so it does not
use the Qwen/GLM one-item proposer-qualification gate. The scientific admission
must instead bind the exact reviewed public pool, current code bundle, raw and
normalized provider catalogs, Kimi v3 route contract, prompt, and successful
route evidence. Freeze it offline from a clean committed tree:

```bash
ROOT=/localhome/milikic/LeanFaith
cd "$ROOT"
git diff --quiet
git diff --cached --quiet

POOL_AUDIT="artifacts/generation/lf022_public_v3_max_c799f54c/audit.json"
BUNDLE_JSON=$(
  uv run leanfaith freeze-code-bundle \
    --root "$ROOT" \
    --out-dir artifacts/code_bundles/lf022_kimi_scientific
)
BUNDLE=$(
  python -c 'import json,sys; print(json.load(sys.stdin)["path"])' \
    <<<"$BUNDLE_JSON"
)

ADMISSION="artifacts/generation/lf022_kimi_scientific_v3/execution_admission.json"
uv run leanfaith freeze-lf022-scientific-kimi-admission \
  --root "$ROOT" \
  --public-pool-audit "$POOL_AUDIT" \
  --code-bundle "$BUNDLE" \
  --output "$ADMISSION"
```

This command reads no credentials and performs no network request. The bound
scientific plan currently contains exactly 9,207 Kimi `G_open` tasks. Select
deterministic tranches by their position in that exact plan rather than passing
thousands of task IDs. The selected IDs are then stored sorted and unique in
the immutable request:

```bash
# Start with LIMIT=1. After end-to-end review, use LIMIT=256 and then LIMIT=9207.
OFFSET=0
LIMIT=1
TAG="prefix_${LIMIT}"
REQUEST="data/lf022_kimi_scientific/${TAG}/request.json"
BATCH_DIR="data/lf022_kimi_scientific/${TAG}/batch"

uv run leanfaith make-lf022-public-batch-request \
  --root "$ROOT" \
  --admission "$ADMISSION" \
  --allocation-offset "$OFFSET" \
  --allocation-limit "$LIMIT" \
  --output "$REQUEST" \
  --batch-directory "$BATCH_DIR"
uv run leanfaith freeze-lf022-public-batch \
  --root "$ROOT" \
  --request "$REQUEST"
```

The freezer fails closed if the requested window extends past the admitted
Kimi `G_open` plan. Thus `OFFSET=0 LIMIT=1`, `OFFSET=0 LIMIT=256`, and
`OFFSET=0 LIMIT=9207` freeze the reviewed one-item, prefix-256, and complete
scientific batches without a large command line. An operator may instead use
non-overlapping offsets. The immutable request and manifest store the exact
sorted task IDs; record the human-readable offset and limit in the associated
run notes rather than claiming that those two convenience arguments are schema
fields.

Preflight any frozen tranche without credentials:

```bash
MANIFEST="$BATCH_DIR/batch_manifest.json"
env -u RCP_BASE_URL -u RCP_API_KEY -u OPENAI_BASE_URL -u OPENAI_API_KEY \
  uv run leanfaith run-lf022-public-batch \
    --root "$ROOT" \
    --manifest "$MANIFEST" \
    --max-concurrency 1 \
    --minimum-request-interval-seconds 1
```

Only after the one-item result has been inspected end to end should the same
manifest be executed with runtime-only credentials and the explicit live flag:

```bash
test "${RCP_BASE_URL%/}" = "https://inference.rcp.epfl.ch/v1"
test -n "${RCP_API_KEY:-}"
set +x
env -u OPENAI_BASE_URL -u OPENAI_API_KEY \
  RCP_BASE_URL="$RCP_BASE_URL" \
  RCP_API_KEY="$RCP_API_KEY" \
  uv run leanfaith run-lf022-public-batch \
    --root "$ROOT" \
    --manifest "$MANIFEST" \
    --execute-public-provisional \
    --max-concurrency 1 \
    --minimum-request-interval-seconds 1
```

Use one concurrent request for Kimi as well. The prefix-256 diagnostic run
observed the same key-level `max_parallel_requests=1` response header and
confirmed that higher client concurrency creates avoidable HTTP 429 retries.

All tranches use the same global executor output root and deterministic task
identities. Re-freezing a larger prefix therefore replays already terminal
tasks without another provider call and continues only with unfinished tasks.
The prefix-256 tranche is the mechanical go/no-go audit for the full run. Do
not start the 9,207-task tranche unless all 256 tasks have terminal records,
`error_count=0`, no `transport_unknown` terminal exists, and at least 243 of
256 tasks (95%) end as `provisional_variants_created`. Inspect a deterministic
32-item sample of the successful raw and parsed artifacts for prompt leakage,
proof bodies/placeholders, malformed declaration boundaries, and duplicated
outputs. This is operational generation QA, not semantic labeling. A failed
threshold requires a route or prompt review and a new versioned admission; it
must not be bypassed by silently dropping failed tasks.

Use `qa-lf022-prefix256` above for this audit; do not select the 32 examples by
hand or substitute a different offline replay report.

Every generated variant remains provisional, unresolved, unlabeled, and
ineligible for training, evaluation, promotion, or Gate credit until later
independent validation and label resolution.

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
