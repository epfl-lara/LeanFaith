# LF-022 extraction-reuse attestation

LF-022 normally requires the extraction and representation `OutputManifest`
records to carry identical environment and code provenance. That remains the
default and every unreviewed mismatch is rejected.

The single exception in
`configs/sources/lf022_extraction_reuse_policy_v1.json` permits a
representation-only refresh of one exact public mathlib extraction:

- extraction manifest SHA-256:
  `b183120468eb8f88f832d4336c206c14fb5f2a4fd3b9d968165228a6185bad06`;
- theorem partition SHA-256:
  `7f1a157bfb818b49d082dcc58de221bdddb67f6e8309554395baeb29850838d7`;
- theorem count: `27,786`;
- extraction revision: `ff1dc91bcda5bf6271f6c9781e716d1931d8ec37`;
- reviewed representation revision:
  `dc29fe6d4038b842b40a4b20506803c3ee05bfec`;
- mathlib revision:
  `d568c8c09630de097a046763c17b9ea99f95f950`.

The policy binds the exact old and reviewed code-tree hashes, source frame,
context, environment, every extraction-critical path at both revisions, and
the exact patch for every changed critical path. The policy file itself has a
digest pinned in code. Editing the policy, supplying a different policy,
changing a critical path, or replacing any bound artifact therefore fails.

This exception:

- is public-mathlib-only;
- is representation-refresh-only;
- does not authorize network execution;
- creates no semantic label;
- grants no gate credit;
- is not reusable for another extraction or theorem partition.

## Operator flow

All paths must be regular files inside the repository. If scale artifacts were
created elsewhere, copy them byte-for-byte into an ignored repository-local
data directory before freezing.

After the reviewed implementation commit is checked out with a clean worktree:

```text
leanfaith freeze-lf022-extraction-reuse-attestation \
  --extraction-manifest data/scale/lf022_public_v1/extraction/manifests/mathlib.json \
  --theorems data/scale/lf022_public_v1/extraction/theorems/mathlib.jsonl \
  --contexts data/extracted/contexts/0cd06826b8767b3bc951c0eb00c802424af95785b558f9f8a61f18694a86c4ce.json \
  --mathlib-source-frame data/source_frames/mathlib/lf022_public_mathlib_frame_v1_1200.json \
  --representation-manifest <repository-local-repr-v3-manifest> \
  --representations <repository-local-repr-v3-jsonl> \
  --output data/scale/lf022_public_v3/extraction_reuse_attestation.json
```

Pass that exact artifact to the offline pool materializer:

```text
leanfaith materialize-lf022-public-pool \
  ... \
  --extraction-reuse-attestation \
    data/scale/lf022_public_v3/extraction_reuse_attestation.json
```

Omitting the flag preserves the original exact-code-provenance rule. The
attestation is replayed again while the production allocation plan is built,
so bypassing only the first materialization check is not sufficient.
