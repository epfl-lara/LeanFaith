# LF-023 authenticated annotation operator flow

This is the production handoff for the already frozen, blinded 240-item
prevalence frame. It creates raw independent annotation records and
administrative reports only. It does **not** adjudicate, promote, resolve, or
make any record training-eligible.

All paths below are examples. Keep the authentication key, assignments,
attestations, response exports, import artifacts, agreement report, and routing
queue in ignored mode-0600 storage. Never transmit private `sft_classic`
content or the private linkage to an external service.

## 1. Create the authentication key

Create one local random key of at least 32 bytes and restrict it before use:

```bash
umask 077
openssl rand 32 > annotation/keys/lf023.key
chmod 600 annotation/keys/lf023.key
```

The code never generates or prints this key.

The HMAC authenticates artifacts and assertions made by the trusted local
operator. It does not independently prove an annotator's human identity,
independence, or the backend origin of a response. Consequently these
operator-attested records remain raw, non-gold, and training-ineligible until
the separate backend-origin trust root and adjudication policy are implemented
and verified.

## 2. Assign each blinded bundle before responses exist

Run once per independent annotator slot. The principal hash is a stable
pseudonymous SHA-256 key declared by the trusted operator; the two hashes must
represent different people. The hashes support a consistency check of that
declaration, not cryptographic proof of identity or independence.

```bash
uv run leanfaith create-human-assignment \
  --bundle-manifest annotation/exports/lf021_prevalence_v1/independent_annotator_1/manifest.json \
  --private-linkage-manifest annotation/exports/lf021_prevalence_v1/private/manifest.json \
  --authentication-key annotation/keys/lf023.key \
  --round-id prevalence_round_1 \
  --annotator-slot independent_annotator_1 \
  --annotator-id expert_1 \
  --annotator-principal-hash <64-lowercase-hex-characters> \
  --backend-id argilla \
  --assigned-at 2026-07-28T12:00:00Z \
  --output annotation/assignments/expert_1.json
```

Registered production backend IDs are `argilla`, `label_studio`, and
`streamlit_documented_fallback`. The assignment is authenticated and immutable.
Create it before granting the annotator access to the response form.

## 3. Export and freeze submitted-response snapshots

Export each submitted backend response once and project it to canonical
`LockedAnnotationResponseEnvelopeV1` JSONL. The JSONL is the project-owned
logical lock: it must be mode-0600, nonempty, newline terminated, immutable
after capture, and use canonical JSON on each line. Argilla's submitted
response object is not assumed to be immutable. Every row remains a raw vote.

The backend adapter must preserve its immutable submission ID in
`backend_submission_id`. It must not infer or add semantic outcomes.

## 4. Attest the exact export

The trusted verifier checks that the assigned person produced the responses
and that the exact local export snapshot has been frozen. Both confirmations
are deliberately required flags. The legacy flag spelling
`--confirm-backend-export-locked` refers to that project-owned snapshot; it
does not assert backend-row immutability:

```bash
uv run leanfaith attest-human-submission \
  --human-assignment annotation/assignments/expert_1.json \
  --responses annotation/responses/expert_1.jsonl \
  --authentication-key annotation/keys/lf023.key \
  --backend-export-id <immutable-backend-export-id> \
  --verifier-id operator_1 \
  --attested-at 2026-07-29T12:00:00Z \
  --confirm-operator-assertion \
  --confirm-backend-export-locked \
  --output annotation/attestations/expert_1.json
```

Attestation fails if response identities, slot, annotator, round, bundle,
guideline, timestamps, or tokens conflict with the assignment.

## 5. Import each slot

```bash
uv run leanfaith import-annotation \
  --bundle-manifest <slot-public-manifest> \
  --private-linkage-manifest <private-linkage-manifest> \
  --human-assignment annotation/assignments/expert_1.json \
  --human-submission-attestation annotation/attestations/expert_1.json \
  --authentication-key annotation/keys/lf023.key \
  --responses annotation/responses/expert_1.jsonl
```

Import reauthenticates all bindings, writes immutable locked-response and raw
`AnnotationRecord` collections, and installs a logical per-item response lock.
A divergent retry for the same campaign, round, slot, and item is rejected.

## 6. Write agreement and adjudication routing

Only after both slot manifests report `complete=true`:

```bash
uv run leanfaith write-annotation-agreement \
  --first-import-manifest <slot-1-import-manifest> \
  --second-import-manifest <slot-2-import-manifest> \
  --authentication-key annotation/keys/lf023.key \
  --output annotation/attestations/prevalence_round_1_agreement.json

uv run leanfaith write-adjudication-queue \
  --first-import-manifest <slot-1-import-manifest> \
  --second-import-manifest <slot-2-import-manifest> \
  --authentication-key annotation/keys/lf023.key \
  --output annotation/attestations/prevalence_round_1_adjudication_queue.json
```

Both commands reload and reverify the full authenticated import lineage. The
agreement artifact is descriptive. The queue contains routing triggers and
`semantic_resolution=null`; it requires a later, genuine human adjudication.
Neither artifact creates a semantic label, gold label, or training example.

An optional `--policy-trigger-set` accepts a mode-0600 canonical
`AdjudicationPolicyTriggerSetV1` artifact. Free-form CLI target lists are not
accepted.

## Failure and replay rules

- Never edit an assignment, attestation, response export, import, agreement, or
  queue in place. Identical reruns are idempotent; divergent content is rejected.
- Never use the fixture backend in production. Test-fixture assignments and
  imports are rejected by all production commands.
- Keep the two annotators and stable principal hashes distinct.
- Do not treat agreement as adjudication.
- Preserve all raw records and all disagreements.
- Do not make Gate 5, gold, promotion, or training-readiness claims until the
  later resolver/adjudication policies and gates have independently passed.

The numbered compatibility entry points are `scripts/40_*.py` through
`scripts/43_*.py`; they forward to the same Typer commands and validation.
