# LF-022 public provisional generation

LF-022 external generation is a public-mathlib-only collection workflow.
Generated statements are always unresolved, unvalidated, and provisional.
They are not semantic labels, training data, evaluation data, silver/gold
records, or Gate credit.

## One-pair weak-judge route smoke

LF-022 has a separate live boundary for checking that the planned weak-judge
routes work end to end. This is **not** the scale judging job. It selects the
lowest content-addressed candidate from an existing public, Lean-valid Qwen
inventory and admits exactly four serial requests:

- Kimi judges `A,B` and `B,A`;
- DeepSeek judges `A,B` and `B,A`.

The Qwen proposer, both judge families, and the held-out OpenAI/Codex evaluator
must remain distinct. The parent weak-batch spec must already bind these exact
roles and the reviewed judge-specific decoding contracts. The selector never
hand-edits or copies a smaller pseudo-inventory.

First author the previously implicit weak-batch spec and prepare its four cells
per pair. The authoring command accepts only the exact Qwen schema-v3 inventory,
copies every direct input below one ignored artifact root, verifies the
production catalog and all four family roles, and shares the judge decoding
contracts with the later live admission. Preserve the randomization-key file:
its bytes blind and order the prepared presentations, while only its hash is
written to the spec.

```bash
ROOT=/localhome/milikic/LeanFaith
cd "$ROOT"
test -z "$(git status --porcelain)"

CANDIDATE_ROOT=/storage/milikic/leanfaith/lf022_supervision_candidates/qwen3_5_snapshot1019_direct_v3
SPEC_ROOT=artifacts/generation/lf022_qwen_weak_batch_spec_v1
KEY_FILE="$SPEC_ROOT/randomization.key"
BATCH_ROOT=artifacts/generation/lf022_qwen_weak_batch_v1

# Create once, retain for exact preparation/replay, and never commit it.
if ! test -f "$KEY_FILE"; then
  install -d -m 700 "$(dirname "$KEY_FILE")"
  umask 077
  python -c 'import secrets,sys; open(sys.argv[1], "wb").write(secrets.token_bytes(32))' \
    "$KEY_FILE"
fi

SPEC_JSON=$(
  uv run leanfaith freeze-lf022-qwen-weak-batch-spec \
    --root "$ROOT" \
    --candidate-manifest "$CANDIDATE_ROOT/manifest.json" \
    --candidate-records "$CANDIDATE_ROOT/candidates.jsonl" \
    --randomization-key-file "$KEY_FILE" \
    --weak-supervision-config configs/judges/weak_supervision.yaml \
    --production-family-matrix \
      configs/generation/lf022_production_family_matrix_v2.json \
    --output-dir "$SPEC_ROOT/frozen"
)
SPEC=$(
  python -c 'import json,sys; print(json.load(sys.stdin)["spec_path"])' \
    <<<"$SPEC_JSON"
)
SPEC_SHA=$(
  python -c 'import json,sys; print(json.load(sys.stdin)["spec_sha256"])' \
    <<<"$SPEC_JSON"
)
PRODUCTION_CATALOG=$(
  python -c 'import json,sys; print(json.load(sys.stdin)["production_catalog_path"])' \
    <<<"$SPEC_JSON"
)

uv run leanfaith prepare-lf022-weak-batch \
  --root "$ROOT" \
  --spec "$SPEC" \
  --spec-sha256 "$SPEC_SHA" \
  --randomization-key-file "$KEY_FILE" \
  --output-dir "$BATCH_ROOT"
```

The spec freeze and batch preparation perform zero network calls. For the
current 718-pair snapshot, preparation must report 718 pairs and 2,872 cells.
The spec binds Qwen as proposer, Kimi as `judge_A`, DeepSeek as `judge_B`, and
OpenAI/Codex as the supervision-excluded evaluator. A wrong schema, model,
role, decoding contract, catalog, key hash, or input hash fails closed.

Next freeze a clean-tree code bundle and the exact one-pair route evidence
without provider credentials. Continue using the ignored `artifacts/` tree so
the operation does not dirty the admitted source tree:

```bash
RAW_RCP_CATALOG=<pinned-raw-rcp-models-response.json>
FREEZE_ROOT=artifacts/generation/lf022_weak_judge_live_smoke_v1

BUNDLE_JSON=$(
  uv run leanfaith freeze-code-bundle \
    --root "$ROOT" \
    --out-dir "$FREEZE_ROOT/code_bundle"
)
BUNDLE=$(
  python -c 'import json,sys; print(json.load(sys.stdin)["path"])' \
    <<<"$BUNDLE_JSON"
)

FREEZE_JSON=$(
  uv run leanfaith freeze-lf022-weak-live-smoke \
    --root "$ROOT" \
    --batch-root "$BATCH_ROOT" \
    --production-catalog "$PRODUCTION_CATALOG" \
    --raw-rcp-catalog "$RAW_RCP_CATALOG" \
    --code-bundle "$BUNDLE" \
    --output-dir "$FREEZE_ROOT/route_inputs"
)
CONFIG=$(
  python -c 'import json,sys; print(json.load(sys.stdin)["config_path"])' \
    <<<"$FREEZE_JSON"
)
CONFIG_SHA=$(
  python -c 'import json,sys; print(json.load(sys.stdin)["config_sha256"])' \
    <<<"$FREEZE_JSON"
)
```

Freezing performs zero network calls. It binds both the normalized production
catalog and the raw RCP `/models` response separately, validates the code
bundle, and records `scale_judge_qualified=false`.

Prepare the deterministic one-pair selector and immutable admission, still
without credentials or network access:

```bash
PREPARE_JSON=$(
  env -u RCP_BASE_URL -u RCP_API_KEY \
    -u OPENAI_BASE_URL -u OPENAI_API_KEY \
    uv run leanfaith prepare-lf022-weak-live-smoke \
      --root "$ROOT" \
      --batch-root "$BATCH_ROOT" \
      --config "$CONFIG" \
      --config-sha256 "$CONFIG_SHA"
)
ADMISSION=$(
  python -c 'import json,sys; print(json.load(sys.stdin)["admission_path"])' \
    <<<"$PREPARE_JSON"
)
ADMISSION_SHA=$(
  python -c 'import json,sys; print(json.load(sys.stdin)["admission_sha256"])' \
    <<<"$PREPARE_JSON"
)
```

The preparation result must report one selected pair, four admitted cells,
zero network calls, and `training_eligible=false`. Execute only by supplying
the exact runtime-only RCP environment and the explicit live flag:

```bash
test "${RCP_BASE_URL%/}" = "https://inference.rcp.epfl.ch/v1"
test -n "${RCP_API_KEY:-}"
set +x
env -u OPENAI_BASE_URL -u OPENAI_API_KEY \
  RCP_BASE_URL="$RCP_BASE_URL" \
  RCP_API_KEY="$RCP_API_KEY" \
  uv run leanfaith execute-lf022-weak-live-smoke \
    --root "$ROOT" \
    --batch-root "$BATCH_ROOT" \
    --admission "$ADMISSION" \
    --admission-sha256 "$ADMISSION_SHA" \
    --execute-public-provisional
```

The executor is serial, allows one attempt per cell and no more than four
network calls, persists the wire response before parsing, and resumes completed
cells instead of sending them again. New freezes bind a 3,600-second maximum
request timeout, matching the production LF-022 policy so provider queueing or
high-reasoning inference is not mistaken for a failure after one minute. This
is a maximum, not a delay: responses that finish sooner return immediately.
An unknown transport outcome at that bound is terminal and is never retried
automatically. Runtime credentials are held only in memory and are not
serialized or printed. Earlier content-addressed 60-second smoke artifacts are
preserved as historical evidence and must not be mutated or resent; use a new
freeze and admission for another live qualification attempt.

After all four cells are terminal, verify every content-addressed artifact with
credentials removed. Offline replay refuses to make a request if any terminal
is missing:

```bash
env -u RCP_BASE_URL -u RCP_API_KEY \
  -u OPENAI_BASE_URL -u OPENAI_API_KEY \
  uv run leanfaith replay-lf022-weak-live-smoke \
    --root "$ROOT" \
    --batch-root "$BATCH_ROOT" \
    --admission "$ADMISSION" \
    --admission-sha256 "$ADMISSION_SHA"
```

Even a fully successful smoke is only one-pair route-qualification evidence.
Its parsed judgments and weak-consensus diagnostic remain ineligible for
supervision, training, evaluation, promotion, and Gate credit. A separate
reviewed scale admission is required before judging the 919-pair queue.

## Supported proposer routes

- `moonshotai/Kimi-K2.7-Code` v3 is archived and offline-replay-only after its
  failed prefix-256 audit. Kimi-v4 may enter scientific production only through
  its 16/16 hard-case qualification and the exact replay-certified route
  eligibility described below.
- `Qwen/Qwen3.5-397B-A17B` may enter scientific production only through its
  exact replay-verified v2 proposer eligibility.
- `zai-org/GLM-5.2` may enter scientific production only through its exact
  replay-verified v2 proposer eligibility.
- `deepseek-ai/DeepSeek-V4-Pro` passed its one-item, strict public `G_open`
  proposer qualification. The successful terminal replays with zero network
  calls and binds the canonical v2 family matrix. Scientific execution still
  requires an exact family-only reallocation of the immutable public pool and
  a separately frozen production admission.

The matching `*_production_route_v1.yaml` files are non-executable policy
templates. Only a content-addressed execution admission that binds a verified
eligibility record can activate a production route.

Qwen, GLM, and DeepSeek were previously observed as judges. That is transport
evidence, not proposer qualification. Their qualification contracts bind the exact
model, provider catalog, prompt, decoding fields, code bundle, allocation, and
public source. `reasoning_effort=high` and
`chat_template_kwargs.enable_thinking=true` are sent only because those exact
fields are explicitly allowed by each bound capability policy. Unknown
reasoning fields are rejected rather than renamed or silently dropped.

## Qualification and activation

The route runner consumes an execution admission; a route-contract YAML is not
an admission. Build the family-specific one-source diagnostic allocation and
freeze the execution admission entirely offline before resolving credentials.

For Qwen, GLM, and DeepSeek, derive the diagnostic pool from the already immutable,
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

# Set one family at a time: qwen3, glm5, or deepseek_v4.
FAMILY=qwen3
QUAL_ROOT="artifacts/generation/lf022_${FAMILY}_diagnostic_from_v3_max"
PARENT_AUDIT="artifacts/generation/lf022_public_v3_max_c799f54c/audit.json"

uv run leanfaith derive-lf022-diagnostic-subpool \
  --parent-pool-audit "$PARENT_AUDIT" \
  --proposer-family "$FAMILY" \
  --out-dir "$QUAL_ROOT"

# DeepSeek alone additionally binds the versioned replacement matrix:
#   --family-matrix configs/generation/lf022_production_family_matrix_v2.json

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
`data/lf022_execution/production_eligibility/qwen3.json`,
`data/lf022_execution/production_eligibility/glm5.json`, or
`data/lf022_execution/production_eligibility/deepseek_v4.json`.

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

## Scientific replay-qualified proposer tranches

After the one-item v2 route is certified, freeze a separate scientific
admission over the existing public pool. This does not reuse the diagnostic
allocation: it selects only that family's already-frozen scientific `G_open`
allocations and binds the canonical eligibility record, v2 decoding contract,
family matrix, catalog, prompt, and current code bundle. DeepSeek eligibility
binds family-matrix v2, while the immutable parent public pool binds v1. Derive
a matrix-only scientific pool without reopening extraction or changing any
source, theorem, representation, context, authorization, or denylist artifact:

```bash
ROOT=/localhome/milikic/LeanFaith
cd "$ROOT"
test -z "$(git status --porcelain)"

PARENT_AUDIT="artifacts/generation/lf022_public_v3_max_c799f54c/audit.json"
MATRIX="configs/generation/lf022_production_family_matrix_v2.json"
REALLOCATED_POOL="artifacts/generation/lf022_public_v3_max_matrix_v2"

uv run leanfaith reallocate-lf022-public-pool \
  --root "$ROOT" \
  --parent-pool-audit "$PARENT_AUDIT" \
  --family-matrix "$MATRIX" \
  --out-dir "$REALLOCATED_POOL"
```

The command exact-replays the immutable parent and rewrites only the admission
and family-role allocation. It is offline, provisional, and creates no label,
training/evaluation eligibility, promotion, or Gate credit. A schema-v2
scientific audit is executable only when the admission freezer can exact-replay
this derivation against the current clean code tree.

```bash
ROOT=/localhome/milikic/LeanFaith
cd "$ROOT"
test -z "$(git status --porcelain)"

# Run one family at a time. Use the audit whose exact matrix matches the
# family's eligibility. Matrix v2 rotates Kimi, Qwen, and DeepSeek.
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
version records contrary evidence, every live Qwen/GLM/DeepSeek qualification and
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

## Kimi-v4 requalification after the failed v3 prefix

The immutable Kimi-v3 prefix-256 remains the historical record: 227 tasks
succeeded and 29 failed. Reinspection of the persisted wire bodies attributes
22 failures to the 16,384-token completion ceiling, five to declaration-boundary
parsing (including one valid structure literal containing `:=`), and two to
avoidable max-parallel HTTP 429 responses. The v4 work does not rewrite or
silently reinterpret those terminal artifacts.

`configs/generation/lf022_kimi_k2_7_proposer_v4.yaml` is the reviewed
requalification contract, not by itself a production admission. It binds the v2 prompt,
32,768-token high-reasoning request, transient-HTTP-only retry policy, one
maximum in-flight request, and a capability-first 16-case decision rule. The
first selected case is the only permitted capability call. The other 15 calls
must not begin unless it returns HTTP 200, the expected model, `finish_reason`
`stop`, and one strict parsed variant. A `length` response is recorded as
`output_budget_exhausted` and is never retried with identical payload bytes.

Freeze the exact challenge offline from the admitted-code historical replay:

```bash
ROOT=/localhome/milikic/LeanFaith
cd "$ROOT"
test -z "$(git status --porcelain)"

BUNDLE_JSON=$(
  uv run leanfaith freeze-code-bundle \
    --root "$ROOT" \
    --out-dir artifacts/code_bundles/lf022_kimi_v4
)
BUNDLE=$(
  python -c 'import json,sys; print(json.load(sys.stdin)["path"])' \
    <<<"$BUNDLE_JSON"
)

uv run leanfaith freeze-lf022-kimi-v4-challenge \
  --root "$ROOT" \
  --current-code-bundle "$BUNDLE"
```

The command performs no network request and creates no execution admission. It
requires a clean Git tree, validates and binds the complete current code bundle,
and records exact hashes for the selector, historical loader, response parsers,
contract, and prompt before reparsing any historical body. It replays all 256
historical terminals and deterministically selects six prior budget-exhausted
cases, two still-proof-bearing cases, and eight prior-success controls from 16
unique sources. Its output is content-addressed beneath
`artifacts/generation/lf022_kimi_v4_challenge_selection_v2/`. Replay it later
only from that same code tree with:

```bash
uv run leanfaith verify-lf022-kimi-v4-challenge \
  --root "$ROOT" \
  --selection artifacts/generation/lf022_kimi_v4_challenge_selection_v2/<sha256>.json
```

The requalification decision was fixed before live access: at least 14/16 had
to strictly parse, no selected response could exhaust its output budget or be
an HTTP-200 empty response, and neither selected historical declaration-
boundary failure could repeat. The live challenge completed on 2026-08-10 with
16/16 strict parsed variants, zero output-budget exhaustion, zero HTTP-200
empty responses, and no repeated proof-bearing parser failure. Two first
attempts received HTTP 429 and then succeeded under the frozen transient-only
retry policy. The qualification is immutable at:

```text
data/lf022_kimi_v4_requalification/v1/643258f3a8bd89e88a00d9a37e778431d3268ed28e68f831d943e29220f82c37/qualification.json
```

Certify the route without credentials. The certifier validates the archived
selection code bundle and every semantics-bound implementation file, then
replays all sixteen persisted terminals through the selected requalification
semantics:

```bash
ROOT=/localhome/milikic/LeanFaith
SELECTION="artifacts/generation/lf022_kimi_v4_challenge_selection_v2/643258f3a8bd89e88a00d9a37e778431d3268ed28e68f831d943e29220f82c37.json"
QUALIFICATION="data/lf022_kimi_v4_requalification/v1/643258f3a8bd89e88a00d9a37e778431d3268ed28e68f831d943e29220f82c37/qualification.json"
MATRIX="artifacts/generation/lf022_public_v3_max_c799f54c/family_matrix.json"

env -u RCP_BASE_URL -u RCP_API_KEY -u OPENAI_BASE_URL -u OPENAI_API_KEY \
  uv run leanfaith certify-lf022-kimi-v4-route \
    --root "$ROOT" \
    --selection "$SELECTION" \
    --qualification "$QUALIFICATION" \
    --family-matrix "$MATRIX"
```

Certification produced the content-addressed eligibility ID
`lf022_kimi_v4_route_eligibility:15c29ee78b7915bf04c1e40b6b95346f452ebd49cfdf0eb79ef2ad20418fe553`.
It replays all sixteen terminals with zero network calls and writes one
canonical route-only eligibility record under
`data/lf022_execution/production_eligibility/`. New Kimi scientific admissions
must bind that record, the v4 contract, v2 prompt, exact public family matrix,
and a current clean code bundle. Missing, changed, or cross-route eligibility
fails before transport. Qualification and eligibility create no semantic
label, silver/gold promotion, training/evaluation eligibility, or Gate credit.

## Archived Kimi-v3 scientific route

The failed prefix-256 audit permanently closed the Kimi-v3 scientific launch
path. Both legacy Kimi-v3 admission commands now reject every request, and
explicit live execution rejects every existing manifest bound to
`kimi_k2_7_public_smoke_v3`. Existing admissions, requests,
terminals, and raw responses remain immutable evidence and may still be replayed
with the ordinary commands in offline mode. They cannot be extended into a new
prefix or full run.

The capability-first runner is restricted to the frozen challenge. Ordinary
production uses the standard public-batch executor only after replay-verified
eligibility and a separately frozen Kimi-v4 scientific admission exist. The
historical v3 admission remains archived and cannot be extended.

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

### Batch-scoped Lean validation

After a frozen batch finishes and replays exactly, validate only its successful
provisional variants by binding the checker to that batch manifest:

```bash
uv run leanfaith check-lf022-provisional-lean \
  --project mathlib=/path/to/mathlib4 \
  --input-root data/lf022_execution \
  --batch-manifest data/<batch>/batch_manifest.json \
  --output-root /separate/content-addressed/check-root
```

The batch selector is canonical and content-addressed. Every selected execution
task must have a terminal artifact at its canonical location; unrelated tasks
under the shared executor root are ignored. The output manifest records the
batch ID, manifest path/hash, and selected task count. This stage establishes
Lean elaboration only and creates no semantic label or training eligibility.
