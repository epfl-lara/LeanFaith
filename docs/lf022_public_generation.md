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
git diff --quiet
git diff --cached --quiet

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

The normal success path reports one new terminal and one network call. The
frozen retry policy permits at most three attempts for explicitly listed
response-confirmed transient HTTP statuses; an unknown transport outcome is
quarantined and is never retried automatically. Never continue to certification
when `tasks != 1`, `errors != 0`, or no successful terminal exists.

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
