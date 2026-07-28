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
API key is accepted only through the environment-variable name stored in the
backend pin; there is no CLI option that accepts an API-key value.

Create the backend pin after the production workspace, dataset, and annotator
UUIDs are known:

```bash
uv run --frozen --project annotation/platforms/argilla \
  leanfaith write-argilla-backend-pin \
  --endpoint https://argilla.example.internal \
  --workspace-id <workspace-uuid> \
  --dataset-id <dataset-uuid> \
  --annotator-id <annotator-uuid> \
  --api-key-env LF_ARGILLA_ANNOTATOR_1_API_KEY \
  --output-dir annotation/argilla-backend-pins/annotator-1
```

The output filename is derived from the content-addressed pin ID. The file
contains the environment-variable name, never its value, and is written mode
0600.

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
  --pin annotation/argilla-backend-pins/annotator-1/<pin-hash>.json \
  --expected-responses annotation/responses/annotator-1-expected.json \
  --output-root annotation/responses/annotator-1-backend-origin
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
