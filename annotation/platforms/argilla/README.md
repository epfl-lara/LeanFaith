# LeanFaith Argilla deployment

This directory pins the self-hosted Argilla 2.8.0 integration used by LF-023.
It is an annotation transport, not a source of semantic labels by itself.

The deployment binds the UI/API to loopback by default. All credentials stay
in the process environment and must never enter Git, assignment bundles,
backend receipts, or annotation exports.

## Start a local integration instance

Export fresh values for every `LF_ARGILLA_*` secret shown in `env.example`,
then run:

```bash
docker compose \
  -f annotation/platforms/argilla/docker-compose.yaml \
  up -d
```

The API is then available at `http://127.0.0.1:6900`. The owner API key is
read by the optional Argilla SDK from `ARGILLA_API_KEY`; it is never accepted
as a CLI argument or persisted by LeanFaith.

The production protocol uses separate workspaces and datasets for the two
independent annotator slots. An annotator account must be registered to
exactly one slot. Peer dataset and record access must be denied by the backend,
not merely hidden in the UI. A submitted Argilla response remains mutable in
Argilla 2.8.0, so submission is only a backend snapshot. The project creates a
separate logical lock after both independent snapshots have been captured and
verified. A separate adjudication workspace is populated only after that
independent round closes.

Run the disposable public-fixture integration check with:

```bash
ARGILLA_API_URL=http://127.0.0.1:6900 \
ARGILLA_API_KEY="$LF_ARGILLA_OWNER_API_KEY" \
uv run --frozen --project annotation/platforms/argilla \
  python scripts/44_validate_argilla_integration.py
```

The dedicated `pyproject.toml` and `uv.lock` in this directory isolate the
Argilla SDK and HTTP client from LeanFaith's root lock. The root `uv.lock` is
bound into immutable LF-021 collection artifacts and must not be rewritten by
annotation-only dependencies.

The validator reads the server version from `/api/v1/version`, creates two
isolated annotator workspaces plus an adjudication workspace, requires direct
peer dataset and record requests to return HTTP 403 or 404, submits two
synthetic responses, and verifies them with the concrete Argilla 2.8 REST
transport. It hashes the exact HTTP response bytes.

Each diagnostic run is written under
`reports/annotation/argilla_local_integration_runs/<sha256>.json`. The stable
`reports/annotation/argilla_local_integration_v1.json` file is an append-only
index of those immutable run artifacts; an existing divergent run is never
silently overwritten.

## Trust boundary

An operator HMAC proves only that an exported file was not changed after the
operator attested it. Backend-origin verification is a separate direct-fetch
step bound to the registered Argilla instance, workspace, dataset, record,
response, and annotator IDs. Neither a submitted response nor a diagnostic
snapshot proves backend immutability.

No response becomes human gold merely because it came from this deployment.
Production admission remains disabled until:

1. the live server and content-addressed backend registration are recorded;
2. two real expert accounts are mapped to distinct annotator slots;
3. responses are fetched directly and pass backend-origin verification;
4. the annotation and adjudication policies accept the completed round; and
5. the training-readiness policy is explicitly revised and re-audited.

Stop the local instance without deleting its named volumes using:

```bash
docker compose \
  -f annotation/platforms/argilla/docker-compose.yaml \
  down
```

The integration validator deletes only the disposable Argilla users,
workspaces, datasets, records, and responses it creates. It does not stop the
Docker Compose deployment and its report states that explicitly.

## Production backend-origin handoff

Run production Argilla commands from this directory's pinned environment. The
provisioner accepts secrets only through environment-variable names. Create the
two authenticated human assignments first. The exact public inputs are:

```text
slot 1 manifest:
  annotation/exports/lf021_prevalence_v1/annotator_manifests/
  acffa0f85555b50776b1d1964d96671edf63f1c619ddbca913f1b5ad3a2a7168.json
  file sha256: 92e279468c7f96357c22215af3d6c2b030ec6492363054b90b4e5babc430900d

slot 2 manifest:
  annotation/exports/lf021_prevalence_v1/annotator_manifests/
  7c590a6d6de8d22cb39deb5f584f3a8ff55fc7c186721126a3331050f86e9841.json
  file sha256: a5eb47323cf1b46af0069ae717a6ed6de302a4d538cbbd2363a5b4efd9e48768

private linkage manifest:
  annotation/exports/lf021_prevalence_v1/private/manifests/
  2a76197a0f13083b3d6171e47013a49687e9798a1de599168f8678977730abda.json
  file sha256: a7e2f5e26e3241dc1aa204ab2bce228795d5b42218b0c1715caf40a876940317
```

The operator must provide an existing Argilla owner identity and two distinct
existing Argilla users with role `annotator`. Export their keys without
printing them:

```bash
export LF_ARGILLA_OWNER_API_KEY=...
export LF_ARGILLA_SLOT_1_API_KEY=...
export LF_ARGILLA_SLOT_2_API_KEY=...
```

Provision the full round into a **new**, operator-chosen private path. The
command refuses an existing output root:

```bash
uv run --frozen --project annotation/platforms/argilla \
  leanfaith provision-argilla-prevalence \
  --authentication-key /secure/leanfaith/lf021_prevalence_v1/annotation.key \
  --endpoint https://argilla.example.internal \
  --owner-api-key-env LF_ARGILLA_OWNER_API_KEY \
  --slot-1-assignment \
    /secure/leanfaith/lf021_prevalence_v1/assignments/independent_annotator_1.json \
  --slot-2-assignment \
    /secure/leanfaith/lf021_prevalence_v1/assignments/independent_annotator_2.json \
  --slot-1-bundle-manifest \
    annotation/exports/lf021_prevalence_v1/annotator_manifests/acffa0f85555b50776b1d1964d96671edf63f1c619ddbca913f1b5ad3a2a7168.json \
  --slot-2-bundle-manifest \
    annotation/exports/lf021_prevalence_v1/annotator_manifests/7c590a6d6de8d22cb39deb5f584f3a8ff55fc7c186721126a3331050f86e9841.json \
  --slot-1-workspace lf021-prevalence-slot-1 \
  --slot-2-workspace lf021-prevalence-slot-2 \
  --slot-1-dataset lf021-prevalence-items-1 \
  --slot-2-dataset lf021-prevalence-items-2 \
  --slot-1-annotator-id <slot-1-argilla-user-uuid> \
  --slot-2-annotator-id <slot-2-argilla-user-uuid> \
  --slot-1-api-key-env LF_ARGILLA_SLOT_1_API_KEY \
  --slot-2-api-key-env LF_ARGILLA_SLOT_2_API_KEY \
  --adjudication-workspace lf021-prevalence-adjudication \
  --provisioned-at 2026-07-28T13:00:00Z \
  --output-root /secure/leanfaith/lf021_prevalence_v1/argilla/provisioning_v1
```

The transaction creates two isolated workspaces, two 240-record datasets, and
one owner-only adjudication workspace. Each record renders the natural-language
statement and `headless`, `signature_pp`, and `signature_explicit` for both
Lean A and Lean B. The `reference_issue` choices are exactly `none`,
`suspected`, and `definite`.

Before publication, the command authenticates both assignments, verifies the
exact manifest and bundle bytes, checks that both annotators are distinct,
requires every new record to have zero responses, and tests peer workspace,
dataset, and all 240 peer-record requests through each annotator identity.
Failure rolls back the newly created remote resources.

On success, the entire private root is published atomically with mode-0700
directories and mode-0600 files:

```text
/secure/leanfaith/lf021_prevalence_v1/argilla/provisioning_v1/
  backend_pins/<slot>/<pin-hash>.json
  record_mappings/<slot>/<mapping-sha256>.json
  projection_bindings/<slot>/bindings/<binding-hash>.json
  runtime_bindings/<runtime-hash>.json
```

Each record mapping contains exactly 240 token-sorted, duplicate-free
token-to-record UUID bindings. The runtime manifest binds the remote
workspace/dataset/user identities, exact public manifests, assignments, pins,
mappings, projection bindings, Argilla SDK/server versions, and peer-isolation
result. It stores environment-variable names but no secret values. Every
artifact remains pre-response, non-semantic, non-gold, and
training-ineligible.

### Crash recovery before publication

The provisioner writes a private recovery journal beside the intended output
root before the first remote side effect. For the example above, its path is:

```text
/secure/leanfaith/lf021_prevalence_v1/argilla/
  .provisioning_v1.argilla-recovery-v1.json
```

If provisioning crashes or reports that manual cleanup is required before the
runtime binding is published, use the same owner-key environment-variable name
recorded in that journal:

```bash
uv run --frozen --project annotation/platforms/argilla \
  leanfaith cleanup-argilla-provisioning \
  --recovery-journal \
    /secure/leanfaith/lf021_prevalence_v1/argilla/.provisioning_v1.argilla-recovery-v1.json \
  --owner-api-key-env LF_ARGILLA_OWNER_API_KEY
```

Recovery verifies the pinned endpoint, Argilla 2.8.0 owner identity, and the
exact journaled workspace and dataset names and UUIDs. It deletes only those
resources, verifies HTTP 404 for each deletion, updates the private journal,
and prints only the terminal state plus dataset/workspace deletion counts. It
is safe to retry after partial cleanup. It refuses `published` journals:
published rounds must remain intact for response capture and must never be
removed through crash recovery. It also refuses cleanup when the intended
private output root exists, covering a crash after atomic local publication
but before the journal's final state update.

The direct-fetch command consumes a strict, label-free expected-response
manifest bound to that exact `backend_pin_id`. Its `expected_responses` array
contains only Argilla record, response, and submission UUIDs; the manifest
also fixes `semantic_labels_included`, `human_gold_eligible`, and
`training_eligible` to `false`.

After exporting the named key into the process environment, capture the
submitted backend snapshots with:

```bash
uv run --frozen --project annotation/platforms/argilla \
  leanfaith capture-argilla-responses \
  --pin \
    /secure/leanfaith/lf021_prevalence_v1/argilla/provisioning_v1/backend_pins/independent_annotator_1/<pin-hash>.json \
  --expected-responses \
    /secure/leanfaith/lf021_prevalence_v1/responses/independent_annotator_1-expected.json \
  --output-root \
    /secure/leanfaith/lf021_prevalence_v1/responses/independent_annotator_1-backend-origin
```

This command always uses the concrete production REST transport. It retains
exact raw dataset and record response bytes plus backend-origin receipts. Its
private content-addressed capture manifest binds exact copies of the backend
pin and expected-response manifest to every ordered receipt and raw payload.
The command rejects existing output directories that are accessible by group
or other users instead of changing their permissions. Its status explicitly
remains non-gold and non-training: a submitted Argilla
snapshot is mutable, contains no project logical lock, and still requires
separate operator integrity evidence and adjudication.

Project the exact capture into canonical locked raw-vote records:

```bash
uv run --frozen --project annotation/platforms/argilla \
  leanfaith project-argilla-capture \
  --human-assignment \
    /secure/leanfaith/lf021_prevalence_v1/assignments/independent_annotator_1.json \
  --pin \
    /secure/leanfaith/lf021_prevalence_v1/argilla/provisioning_v1/backend_pins/independent_annotator_1/<pin-hash>.json \
  --projection-binding \
    /secure/leanfaith/lf021_prevalence_v1/argilla/provisioning_v1/projection_bindings/independent_annotator_1/bindings/<binding-hash>.json \
  --capture-root \
    /secure/leanfaith/lf021_prevalence_v1/responses/independent_annotator_1-backend-origin \
  --capture-manifest \
    /secure/leanfaith/lf021_prevalence_v1/responses/independent_annotator_1-backend-origin/manifests/<capture-hash>.json \
  --output-root \
    /secure/leanfaith/lf021_prevalence_v1/responses/independent_annotator_1-projected
```

The command revalidates the assignment's exact public-bundle token membership,
the capture manifest, copied pin, expected-response membership, every receipt,
and every retained raw dataset and record payload.
Its only outputs are:

```text
/secure/leanfaith/lf021_prevalence_v1/responses/independent_annotator_1-projected/
  locked_responses/<locked-response-sha256>.jsonl
  manifests/<projection-manifest-sha256>.json
```

Both files are private, canonical, content-addressed, and idempotent. They are
raw independent votes, not verified human identity, HMAC evidence,
adjudication, gold labels, Gate 5 closure, or training data.
