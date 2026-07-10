# ADR-0003: Data versioning (content-hash manifests first, DVC as pointers)

**Status:** Accepted
**Date:** 2026-07-10
**Source of truth:** PLAN.md section 6.1 (line 332) and section 28.5; repository layout
section 7 (`dvc.yaml` / `dvc.lock` marked "research_v1 onward").

## Context

The project needs artifact identity that is (a) verifiable offline from the repository
alone, (b) stable across storage backends, and (c) available from the first pilot run,
before any release-track infrastructure exists. PLAN.md fixes the mechanism:
content-hash manifests through pilot, DVC added at `research_v1` without replacing
manifests (section 6.1); "Content-hash manifests remain authoritative. DVC starts at
research_v1 for storage pointers" (section 28.5).

Large artifacts (dataset caches, raw LLM responses, model checkpoints, extraction
snapshots) do not belong in git, and the repository must stay clonable without bulk
data access.

## Decision

decision: **Content-hash manifests are the single authority for artifact identity from
Phase 0 onward, permanently.** Concretely:

1. Every immutable data stage, source snapshot, and released artifact is identified by
   a manifest recording content hashes (per-file and aggregate), row counts where
   applicable, and the producing config/policy versions. Two artifacts are the same
   artifact if and only if their manifest hashes match.
2. Through pilot, manifests are the only versioning layer; no DVC files exist in the
   repository before `research_v1` (matching the section 7 layout annotations).
3. At `research_v1`, DVC is added strictly as a storage-pointer layer (`dvc.yaml`,
   `dvc.lock`): it answers "where do the bytes live," never "what is this artifact."
   DVC metadata never replaces, overrides, or shortcuts a manifest; any
   manifest-vs-DVC disagreement is resolved by the manifest and treated as a storage
   defect.
4. decision: **Bulk bytes live under `/storage/milikic`** (approved local bulk
   storage): large binary artifacts, dataset caches, source checkouts, raw response
   stores, and checkpoints. The git repository keeps manifests and hashes only; no
   large binary is committed. The DVC remote/cache added at `research_v1` points into
   `/storage/milikic`.
5. Secrets (including `HF_TOKEN`) are referenced by environment name only and never
   appear in manifests, DVC files, or storage paths (PLAN.md section 6.1). Manifests
   for private or gated sources record IDs, revisions, licenses, and hashes, never the
   restricted content itself (section 28.6).

## Consequences

1. Reproducibility claims in reports and the paper cite manifest hashes, not DVC
   revisions or storage paths.
2. Loss of `/storage/milikic` contents loses bytes but not identity: manifests in git
   still define exactly which artifacts existed, and re-derived artifacts must
   hash-match to count as the same stage.
3. Adding DVC at `research_v1` is a pure addition: no manifest format change, no
   re-hashing, no migration of pilot artifacts is permitted as part of the DVC
   introduction.
4. Code that consumes an artifact validates it against its manifest hash before use;
   a hash mismatch is a hard failure, not a warning.
5. W&B remains the default experiment tracker with offline/export mode
   (section 28.5); it references manifest hashes and is not an artifact-identity
   authority either.
