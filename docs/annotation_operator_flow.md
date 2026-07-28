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
  --bundle-manifest \
    annotation/exports/lf021_prevalence_v1/annotator_manifests/acffa0f85555b50776b1d1964d96671edf63f1c619ddbca913f1b5ad3a2a7168.json \
  --private-linkage-manifest \
    annotation/exports/lf021_prevalence_v1/private/manifests/2a76197a0f13083b3d6171e47013a49687e9798a1de599168f8678977730abda.json \
  --authentication-key /secure/leanfaith/lf021_prevalence_v1/annotation.key \
  --round-id prevalence_round_1 \
  --annotator-slot independent_annotator_1 \
  --annotator-id expert_1 \
  --annotator-principal-hash <64-lowercase-hex-characters> \
  --backend-id argilla \
  --assigned-at 2026-07-28T12:00:00Z \
  --output \
    /secure/leanfaith/lf021_prevalence_v1/assignments/independent_annotator_1.json
```

Registered production backend IDs are `argilla`, `label_studio`, and
`streamlit_documented_fallback`. The assignment is authenticated and immutable.
Create it before granting the annotator access to the response form.

For slot 2, use this exact manifest:

```text
annotation/exports/lf021_prevalence_v1/annotator_manifests/
7c590a6d6de8d22cb39deb5f584f3a8ff55fc7c186721126a3331050f86e9841.json
```

Its file SHA-256 is
`a5eb47323cf1b46af0069ae717a6ed6de302a4d538cbbd2363a5b4efd9e48768`.
The slot-1 manifest file SHA-256 is
`92e279468c7f96357c22215af3d6c2b030ec6492363054b90b4e5babc430900d`.
The private linkage manifest file SHA-256 is
`a7e2f5e26e3241dc1aa204ab2bce228795d5b42218b0c1715caf40a876940317`.

## 3. Provision isolated datasets and freeze pre-response bindings

Set the owner and the two distinct annotator keys in the environment, then run
the production provisioner once:

```bash
export LF_ARGILLA_OWNER_API_KEY=...
export LF_ARGILLA_SLOT_1_API_KEY=...
export LF_ARGILLA_SLOT_2_API_KEY=...

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

The output root is new, mode 0700, and contains mode-0600 backend pins,
token-sorted 240-token-to-record mappings, pre-response projection bindings,
and one content-addressed runtime binding. The command renders all three Lean
views on each side, uses the canonical `reference_issue` values
`none`/`suspected`/`definite`, verifies every new record has zero responses,
and proves that each annotator cannot see or fetch the peer or adjudication
resources. It creates no response, semantic label, gold, or training
eligibility.

Provisioning writes a private crash-recovery journal beside the intended
output root before making any remote change. If the process exits without
publishing the runtime binding, export the same owner-key environment variable
and clean up the exact journaled resources:

```bash
uv run --frozen --project annotation/platforms/argilla \
  leanfaith cleanup-argilla-provisioning \
  --recovery-journal \
    /secure/leanfaith/lf021_prevalence_v1/argilla/.provisioning_v1.argilla-recovery-v1.json \
  --owner-api-key-env LF_ARGILLA_OWNER_API_KEY
```

The cleanup command verifies the pinned endpoint, owner role, and exact
workspace and dataset identities before deletion, then verifies their absence.
Its stdout contains only the terminal state and deletion counts; it never
prints an API key. It refuses a journal whose provisioning transaction was
already published or whose intended private output root already exists. A
published round must be preserved and handled through the normal
response-capture workflow, not crash recovery.

After submission, use `capture-argilla-responses` and
`project-argilla-capture` exactly as documented in
`annotation/platforms/argilla/README.md`. The project-owned output paths are
content-addressed:

```text
/secure/leanfaith/lf021_prevalence_v1/responses/independent_annotator_1-projected/
  locked_responses/<locked-response-sha256>.jsonl
  manifests/<projection-manifest-sha256>.json
```

The JSONL is mode-0600, nonempty, newline terminated, immutable after capture,
and uses canonical JSON on each line. Argilla's submitted response object is
not assumed to be immutable. Every row remains a raw vote. Projection
revalidates the exact public-bundle membership and backend-origin capture but
does not verify assignment HMAC, human identity, independence, adjudication,
gold, or training eligibility.

The backend adapter preserves its submission ID in `backend_submission_id`.
It does not infer or add semantic outcomes.

## 4. Attest the exact export

The trusted verifier checks that the assigned person produced the responses
and that the exact local export snapshot has been frozen. Both confirmations
are deliberately required flags. The legacy flag spelling
`--confirm-backend-export-locked` refers to that project-owned snapshot; it
does not assert backend-row immutability:

```bash
uv run leanfaith attest-human-submission \
  --human-assignment annotation/assignments/expert_1.json \
  --responses \
    annotation/responses/annotator-1-projected/locked_responses/<locked-response-sha256>.jsonl \
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
