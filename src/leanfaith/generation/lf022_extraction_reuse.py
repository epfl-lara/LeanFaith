"""Reviewed LF-022 reuse of one exact extraction across a representation refresh.

The ordinary LF-022 admission path requires extraction and representation
manifests to have identical code provenance.  This module implements the only
approved exception: reuse of the exact public mathlib extraction produced at
``ff1dc91`` while refreshing representations with the reviewed ``dc29fe6``
tree.  The exception is content-addressed, binds every relevant artifact, and
checks an exact reviewed snapshot of extraction-critical paths.

This is not a general "trust this mismatch" switch.  A caller-provided policy,
an arbitrary extraction, a different theorem partition, a different
representation tree, or drift in any extraction-critical path fails closed.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol, Self

from pydantic import Field, model_validator

from leanfaith.config.hashing import (
    canonical_json_bytes,
    hash_canonical,
    hash_file,
    sha256_hex,
)
from leanfaith.config.models import StrictModel
from leanfaith.schemas.enums import ArtifactClass, DataStage
from leanfaith.schemas.ids import HEX64_PATTERN, id_pattern, make_id
from leanfaith.schemas.manifest import OutputManifest, collect_code_state

LF022_EXTRACTION_REUSE_POLICY_PATH = Path("configs/sources/lf022_extraction_reuse_policy_v1.json")
# Filled with the reviewed canonical policy digest.  Loading any other policy
# fails even if it is internally self-consistent.
LF022_EXTRACTION_REUSE_POLICY_SHA256 = (
    "86598dc59d3936bbc3608bbdcab46e7722433a48e14b80e4989b86380dd7a613"
)


class LF022ExtractionReuseError(ValueError):
    """The reviewed extraction-reuse exception failed closed."""


class LF022ExtractionReuseArtifactBinding(StrictModel):
    """Exact repository-relative regular-file binding."""

    path: str = Field(min_length=1)
    sha256: str = Field(pattern=HEX64_PATTERN)

    @model_validator(mode="after")
    def _safe_path(self) -> Self:
        value = PurePosixPath(self.path)
        if (
            value.is_absolute()
            or "." in value.parts
            or ".." in value.parts
            or "\\" in self.path
            or value.as_posix() != self.path
        ):
            raise ValueError("artifact path must be normalized and repository-relative")
        return self


class LF022ExtractionReuseBindingLike(Protocol):
    """Minimum binding surface accepted by the reuse verifier."""

    path: str
    sha256: str


def narrow_lf022_extraction_reuse_binding(
    binding: LF022ExtractionReuseBindingLike,
) -> LF022ExtractionReuseArtifactBinding:
    """Drop caller-specific metadata while preserving the exact file binding.

    LF-022 JSONL bindings additionally carry ``record_count``.  Passing their
    full Pydantic dump into this strict two-field model fails on that legitimate
    extra field.  Narrowing by attribute keeps the fail-closed path/hash checks
    while allowing both ordinary and JSONL bindings to replay an attestation.
    """

    return LF022ExtractionReuseArtifactBinding(
        path=binding.path,
        sha256=binding.sha256,
    )


class LF022ExtractionCriticalPath(StrictModel):
    """One reviewed extraction-critical path at old and refreshed revisions."""

    path: str = Field(min_length=1)
    old_sha256: str = Field(pattern=HEX64_PATTERN)
    reviewed_sha256: str = Field(pattern=HEX64_PATTERN)
    change_status: Literal["unchanged", "modified"]
    patch_sha256: str = Field(pattern=HEX64_PATTERN)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        value = PurePosixPath(self.path)
        if (
            value.is_absolute()
            or "." in value.parts
            or ".." in value.parts
            or "\\" in self.path
            or value.as_posix() != self.path
        ):
            raise ValueError("critical path must be normalized and repository-relative")
        expected_status = "unchanged" if self.old_sha256 == self.reviewed_sha256 else "modified"
        if self.change_status != expected_status:
            raise ValueError("critical-path change status differs from exact byte hashes")
        return self


class LF022ExtractionReusePolicyV1(StrictModel):
    """Checked-in, content-addressed review decision for one exact reuse."""

    schema_version: Literal[1] = 1
    policy_id: str = Field(pattern=id_pattern("lf022_extraction_reuse_policy_v1"))
    decision: Literal["approved_exact_extraction_reuse_for_representation_refresh"]
    purpose: Literal["lf022_public_mathlib_repr_v3_refresh_only"]
    old_extraction_manifest: LF022ExtractionReuseArtifactBinding
    old_theorem_records: LF022ExtractionReuseArtifactBinding
    old_theorem_record_count: Literal[27786]
    context_records: LF022ExtractionReuseArtifactBinding
    mathlib_source_frame: LF022ExtractionReuseArtifactBinding
    source: Literal["mathlib"]
    mathlib_revision: Literal["d568c8c09630de097a046763c17b9ea99f95f950"]
    context_hash: Literal["d08d78779f9a3ea7c9c1779c982cc8359a309c94de2ea0b4c2c5a3df85ff51fc"]
    environment_hash: Literal["e447ac3a773b0d29ec75b51bcfa5318158399e9fe7459a650f9d0bfef9986298"]
    old_git_revision: Literal["ff1dc91bcda5bf6271f6c9781e716d1931d8ec37"]
    old_code_tree_hash: Literal["6908169b2af3c89be96724f6124e435cbb37294dd9574f056a344c07651493d6"]
    reviewed_representation_git_revision: Literal["dc29fe6d4038b842b40a4b20506803c3ee05bfec"]
    reviewed_representation_code_tree_hash: Literal[
        "9c19681288a441c8c0f949860b386ef83d19f1280ba0fcfd33c48035ee53179d"
    ]
    extraction_critical_paths: tuple[LF022ExtractionCriticalPath, ...] = Field(min_length=1)
    audited_changed_paths: tuple[str, ...] = Field(min_length=1)
    audited_diff_sha256: str = Field(pattern=HEX64_PATTERN)
    public_source_only: Literal[True] = True
    representation_refresh_only: Literal[True] = True
    network_execution_authorized: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    gate_credit_authorized: Literal[False] = False

    @model_validator(mode="after")
    def _canonical(self) -> Self:
        paths = tuple(item.path for item in self.extraction_critical_paths)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("extraction_critical_paths must be sorted and unique")
        changed = tuple(
            item.path for item in self.extraction_critical_paths if item.change_status == "modified"
        )
        if self.audited_changed_paths != changed:
            raise ValueError("audited_changed_paths differs from exact critical-path changes")
        expected_diff = hash_canonical(
            [
                item.model_dump(mode="json")
                for item in self.extraction_critical_paths
                if item.change_status == "modified"
            ]
        )
        if self.audited_diff_sha256 != expected_diff:
            raise ValueError("audited_diff_sha256 differs from reviewed path records")
        expected_id = make_id(
            "lf022_extraction_reuse_policy_v1",
            self.model_dump(mode="json", exclude={"policy_id"}),
        )
        if self.policy_id != expected_id:
            raise ValueError("policy_id differs from canonical reviewed policy content")
        return self


class LF022ExtractionReuseAttestationV1(StrictModel):
    """Frozen authorization for one exact old-extraction/new-representation pair."""

    schema_version: Literal[1] = 1
    attestation_id: str = Field(pattern=id_pattern("lf022_extraction_reuse_attestation_v1"))
    policy: LF022ExtractionReuseArtifactBinding
    decision: Literal["approved_exact_extraction_reuse_for_representation_refresh"]
    old_extraction_manifest: LF022ExtractionReuseArtifactBinding
    old_theorem_records: LF022ExtractionReuseArtifactBinding
    context_records: LF022ExtractionReuseArtifactBinding
    mathlib_source_frame: LF022ExtractionReuseArtifactBinding
    new_representation_manifest: LF022ExtractionReuseArtifactBinding
    new_representation_records: LF022ExtractionReuseArtifactBinding
    representation_input_theorem_locator_hash: str = Field(pattern=HEX64_PATTERN)
    old_git_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    old_code_tree_hash: str = Field(pattern=HEX64_PATTERN)
    new_git_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    new_code_tree_hash: str = Field(pattern=HEX64_PATTERN)
    attesting_git_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    attesting_code_tree_hash: str = Field(pattern=HEX64_PATTERN)
    audited_diff_sha256: str = Field(pattern=HEX64_PATTERN)
    source: Literal["mathlib"]
    mathlib_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    context_hash: str = Field(pattern=HEX64_PATTERN)
    environment_hash: str = Field(pattern=HEX64_PATTERN)
    public_source_only: Literal[True] = True
    representation_refresh_only: Literal[True] = True
    network_execution_authorized: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    gate_credit_authorized: Literal[False] = False

    @model_validator(mode="after")
    def _canonical(self) -> Self:
        expected = make_id(
            "lf022_extraction_reuse_attestation_v1",
            self.model_dump(mode="json", exclude={"attestation_id"}),
        )
        if self.attestation_id != expected:
            raise ValueError("attestation_id differs from canonical content")
        return self


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _repo_regular_path(repo_root: Path, relative: str, *, owner: str) -> Path:
    root = repo_root.resolve(strict=True)
    candidate = root / PurePosixPath(relative)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise LF022ExtractionReuseError(f"{owner} escapes repository") from exc
    current = root
    for part in PurePosixPath(relative).parts:
        current /= part
        try:
            mode = current.lstat().st_mode
        except OSError as exc:
            raise LF022ExtractionReuseError(f"{owner} is unavailable") from exc
        if stat.S_ISLNK(mode):
            raise LF022ExtractionReuseError(f"{owner} contains a symlink")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise LF022ExtractionReuseError(f"{owner} is not a repository-local regular file")
    return resolved


def _binding(repo_root: Path, path: Path) -> LF022ExtractionReuseArtifactBinding:
    root = repo_root.resolve(strict=True)
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise LF022ExtractionReuseError("artifact must be inside the repository")
    return LF022ExtractionReuseArtifactBinding(
        path=PurePosixPath(resolved.relative_to(root).as_posix()).as_posix(),
        sha256=hash_file(resolved),
    )


def _load_json_model[ModelT: StrictModel](
    repo_root: Path,
    binding: LF022ExtractionReuseArtifactBinding,
    model: type[ModelT],
    *,
    owner: str,
) -> ModelT:
    path = _repo_regular_path(repo_root, binding.path, owner=owner)
    if hash_file(path) != binding.sha256:
        raise LF022ExtractionReuseError(f"{owner} hash mismatch")
    try:
        document = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant {value}")
            ),
        )
        restored = model.model_validate(document)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise LF022ExtractionReuseError(f"invalid {owner}: {exc}") from exc
    if path.read_bytes() != canonical_json_bytes(restored.model_dump(mode="json")) + b"\n":
        raise LF022ExtractionReuseError(f"{owner} is not canonical JSON plus newline")
    return restored


def load_reviewed_lf022_extraction_reuse_policy(
    repo_root: Path,
) -> tuple[LF022ExtractionReusePolicyV1, LF022ExtractionReuseArtifactBinding]:
    """Load only the checked-in, digest-pinned review policy."""

    path = _repo_regular_path(
        repo_root,
        LF022_EXTRACTION_REUSE_POLICY_PATH.as_posix(),
        owner="reviewed extraction-reuse policy",
    )
    observed = hash_file(path)
    if observed != LF022_EXTRACTION_REUSE_POLICY_SHA256:
        raise LF022ExtractionReuseError(
            "reviewed extraction-reuse policy bytes differ from the code-pinned digest"
        )
    binding = _binding(repo_root, path)
    policy = _load_json_model(
        repo_root,
        binding,
        LF022ExtractionReusePolicyV1,
        owner="reviewed extraction-reuse policy",
    )
    _verify_policy_git_snapshot(repo_root, policy)
    return policy, binding


def _git_output(repo_root: Path, argv: list[str]) -> str:
    try:
        return subprocess.run(
            ["git", *argv],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise LF022ExtractionReuseError(f"git provenance check failed: {' '.join(argv)}") from exc


def _git_bytes(repo_root: Path, argv: list[str]) -> bytes:
    try:
        return subprocess.run(
            ["git", *argv],
            cwd=repo_root,
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise LF022ExtractionReuseError(f"git provenance check failed: {' '.join(argv)}") from exc


def _verify_policy_git_snapshot(
    repo_root: Path,
    policy: LF022ExtractionReusePolicyV1,
) -> None:
    """Prove that policy path hashes and patches describe the pinned commits."""

    for item in policy.extraction_critical_paths:
        old_bytes = _git_bytes(
            repo_root,
            ["show", f"{policy.old_git_revision}:{item.path}"],
        )
        reviewed_bytes = _git_bytes(
            repo_root,
            [
                "show",
                f"{policy.reviewed_representation_git_revision}:{item.path}",
            ],
        )
        patch = _git_bytes(
            repo_root,
            [
                "diff",
                "--binary",
                policy.old_git_revision,
                policy.reviewed_representation_git_revision,
                "--",
                item.path,
            ],
        )
        if (
            sha256_hex(old_bytes) != item.old_sha256
            or sha256_hex(reviewed_bytes) != item.reviewed_sha256
            or sha256_hex(patch) != item.patch_sha256
        ):
            raise LF022ExtractionReuseError(
                f"review policy does not describe exact git bytes for {item.path}"
            )


def _verify_current_reviewed_paths(
    repo_root: Path,
    policy: LF022ExtractionReusePolicyV1,
    *,
    require_clean: bool,
) -> tuple[str, str]:
    state = collect_code_state(repo_root)
    if state.code_tree_hash is None:
        raise LF022ExtractionReuseError("current code tree hash is unavailable")
    if require_clean and state.git_dirty:
        raise LF022ExtractionReuseError("attestation freeze requires a clean worktree")
    if (
        _git_output(
            repo_root,
            [
                "merge-base",
                "--is-ancestor",
                policy.reviewed_representation_git_revision,
                "HEAD",
            ],
        )
        != ""
    ):
        raise LF022ExtractionReuseError("reviewed representation revision is not an ancestor")
    for item in policy.extraction_critical_paths:
        path = _repo_regular_path(repo_root, item.path, owner=f"critical path {item.path}")
        if hash_file(path) != item.reviewed_sha256:
            raise LF022ExtractionReuseError(
                f"extraction-critical path drifted from reviewed bytes: {item.path}"
            )
    return state.git_revision, state.code_tree_hash


def _manifest_theorem_input_path(
    manifest: OutputManifest,
    theorem_sha256: str,
) -> str:
    return _unique_manifest_checksum_path(
        manifest.input_partition_checksums,
        theorem_sha256,
        owner="representation theorem input",
    )


def _unique_manifest_checksum_path(
    checksums: dict[str, str],
    sha256: str,
    *,
    owner: str,
) -> str:
    matches = sorted(path for path, digest in checksums.items() if digest == sha256)
    if len(matches) != 1:
        raise LF022ExtractionReuseError(f"{owner} must have exactly one checksum binding")
    return matches[0]


def _verify_bound_file(
    repo_root: Path,
    binding: LF022ExtractionReuseArtifactBinding,
    *,
    owner: str,
) -> Path:
    path = _repo_regular_path(repo_root, binding.path, owner=owner)
    if hash_file(path) != binding.sha256:
        raise LF022ExtractionReuseError(f"{owner} hash mismatch")
    return path


def _jsonl_record_count(repo_root: Path, binding: LF022ExtractionReuseArtifactBinding) -> int:
    path = _verify_bound_file(repo_root, binding, owner="bound JSONL partition")
    count = 0
    with path.open("rb") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if not raw.strip():
                raise LF022ExtractionReuseError(
                    f"bound JSONL contains a blank record at line {line_number}"
                )
            try:
                json.loads(
                    raw,
                    object_pairs_hook=_reject_duplicate_keys,
                    parse_constant=lambda value: (_ for _ in ()).throw(
                        ValueError(f"non-finite JSON constant {value}")
                    ),
                )
            except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
                raise LF022ExtractionReuseError(
                    f"bound JSONL has invalid record at line {line_number}: {exc}"
                ) from exc
            count += 1
    return count


def freeze_lf022_extraction_reuse_attestation(
    *,
    repo_root: Path,
    extraction_manifest_path: Path,
    theorem_records_path: Path,
    context_records_path: Path,
    mathlib_source_frame_path: Path,
    representation_manifest_path: Path,
    representation_records_path: Path,
    output_path: Path,
) -> LF022ExtractionReuseAttestationV1:
    """Freeze the exact reviewed exception; never infer or broaden approval."""

    policy, policy_binding = load_reviewed_lf022_extraction_reuse_policy(repo_root)
    supplied = {
        "old_extraction_manifest": _binding(repo_root, extraction_manifest_path),
        "old_theorem_records": _binding(repo_root, theorem_records_path),
        "context_records": _binding(repo_root, context_records_path),
        "mathlib_source_frame": _binding(repo_root, mathlib_source_frame_path),
        "new_representation_manifest": _binding(repo_root, representation_manifest_path),
        "new_representation_records": _binding(repo_root, representation_records_path),
    }
    for field in (
        "old_extraction_manifest",
        "old_theorem_records",
        "context_records",
        "mathlib_source_frame",
    ):
        if supplied[field] != getattr(policy, field):
            raise LF022ExtractionReuseError(f"{field} differs from reviewed policy")

    extraction_manifest = _load_json_model(
        repo_root,
        supplied["old_extraction_manifest"],
        OutputManifest,
        owner="old extraction manifest",
    )
    representation_manifest = _load_json_model(
        repo_root,
        supplied["new_representation_manifest"],
        OutputManifest,
        owner="new representation manifest",
    )
    theorem_count = _jsonl_record_count(repo_root, supplied["old_theorem_records"])
    representation_count = _jsonl_record_count(
        repo_root,
        supplied["new_representation_records"],
    )
    if (
        extraction_manifest.stage is not DataStage.ELABORATED
        or extraction_manifest.artifact_class is not ArtifactClass.PRODUCTION
        or extraction_manifest.source != policy.source
        or extraction_manifest.source_revision != policy.mathlib_revision
        or extraction_manifest.row_count != policy.old_theorem_record_count
        or theorem_count != policy.old_theorem_record_count
        or extraction_manifest.context_hash != policy.context_hash
        or extraction_manifest.environment_hash != policy.environment_hash
        or extraction_manifest.code.git_revision != policy.old_git_revision
        or extraction_manifest.code_tree_hash != policy.old_code_tree_hash
        or extraction_manifest.code.code_tree_hash != policy.old_code_tree_hash
    ):
        raise LF022ExtractionReuseError("old extraction manifest differs from reviewed provenance")
    if (
        extraction_manifest.output_partition_checksums.get(policy.old_theorem_records.path)
        != policy.old_theorem_records.sha256
        or extraction_manifest.file_checksums.get(policy.old_theorem_records.path)
        != policy.old_theorem_records.sha256
        or extraction_manifest.input_partition_checksums.get(policy.mathlib_source_frame.path)
        != policy.mathlib_source_frame.sha256
    ):
        raise LF022ExtractionReuseError("old extraction manifest artifact bindings differ")
    if (
        representation_manifest.stage is not DataStage.REPRESENTED
        or representation_manifest.artifact_class is not ArtifactClass.PRODUCTION
        or representation_manifest.source != policy.source
        or representation_manifest.source_revision != "from_theorem_partition"
        or representation_manifest.attempted_row_count != policy.old_theorem_record_count
        or representation_manifest.row_count != representation_count
        or representation_manifest.environment_hash != policy.environment_hash
        or representation_manifest.code.git_revision != policy.reviewed_representation_git_revision
        or representation_manifest.code_tree_hash != policy.reviewed_representation_code_tree_hash
        or representation_manifest.code.code_tree_hash
        != policy.reviewed_representation_code_tree_hash
    ):
        raise LF022ExtractionReuseError(
            "new representation manifest differs from reviewed provenance"
        )
    representation_sha = supplied["new_representation_records"].sha256
    output_partition_path = _unique_manifest_checksum_path(
        representation_manifest.output_partition_checksums,
        representation_sha,
        owner="representation output partition",
    )
    file_checksum_path = _unique_manifest_checksum_path(
        representation_manifest.file_checksums,
        representation_sha,
        owner="representation output file",
    )
    if output_partition_path != file_checksum_path:
        raise LF022ExtractionReuseError("representation output checksum maps bind different paths")
    theorem_input_path = _manifest_theorem_input_path(
        representation_manifest,
        policy.old_theorem_records.sha256,
    )
    current_revision, current_tree = _verify_current_reviewed_paths(
        repo_root,
        policy,
        require_clean=True,
    )
    payload: dict[str, object] = {
        "schema_version": 1,
        "policy": policy_binding.model_dump(mode="json"),
        "decision": policy.decision,
        **{key: value.model_dump(mode="json") for key, value in supplied.items()},
        "representation_input_theorem_locator_hash": hash_canonical(
            {
                "schema": "lf022_representation_input_locator_v1",
                "path": theorem_input_path,
            }
        ),
        "old_git_revision": policy.old_git_revision,
        "old_code_tree_hash": policy.old_code_tree_hash,
        "new_git_revision": policy.reviewed_representation_git_revision,
        "new_code_tree_hash": policy.reviewed_representation_code_tree_hash,
        "attesting_git_revision": current_revision,
        "attesting_code_tree_hash": current_tree,
        "audited_diff_sha256": policy.audited_diff_sha256,
        "source": policy.source,
        "mathlib_revision": policy.mathlib_revision,
        "context_hash": policy.context_hash,
        "environment_hash": policy.environment_hash,
        "public_source_only": True,
        "representation_refresh_only": True,
        "network_execution_authorized": False,
        "semantic_labels_created": False,
        "gate_credit_authorized": False,
    }
    attestation = LF022ExtractionReuseAttestationV1.model_validate(
        {
            **payload,
            "attestation_id": make_id(
                "lf022_extraction_reuse_attestation_v1",
                payload,
            ),
        }
    )
    root = repo_root.resolve(strict=True)
    destination = output_path if output_path.is_absolute() else root / output_path
    if not destination.resolve(strict=False).is_relative_to(root):
        raise LF022ExtractionReuseError("attestation output must stay inside repository")
    data = canonical_json_bytes(attestation.model_dump(mode="json")) + b"\n"
    if destination.exists():
        if destination.is_symlink() or destination.read_bytes() != data:
            raise LF022ExtractionReuseError("attestation output exists with different bytes")
        return attestation
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return attestation


def verify_lf022_extraction_reuse_attestation(
    *,
    repo_root: Path,
    attestation: LF022ExtractionReuseAttestationV1,
    attestation_binding: LF022ExtractionReuseArtifactBinding,
    extraction_manifest: OutputManifest,
    extraction_manifest_binding: LF022ExtractionReuseArtifactBinding,
    theorem_records_binding: LF022ExtractionReuseArtifactBinding,
    representation_manifest: OutputManifest,
    representation_manifest_binding: LF022ExtractionReuseArtifactBinding,
    representation_records_binding: LF022ExtractionReuseArtifactBinding,
    require_current_attesting_code_state: bool = True,
) -> None:
    """Replay an attestation and require the exact supplied provenance.

    ``require_current_attesting_code_state=False`` is reserved for consumers
    of the already-frozen artifact authorization.  In that mode the verifier
    still checks the digest-pinned review policy, its historical Git snapshots,
    the canonical attestation, every artifact hash/count, and both producer
    manifests.  It merely avoids requiring an unrelated future repository
    checkout to equal the full tree that originally froze the attestation.
    """

    restored = _load_json_model(
        repo_root,
        attestation_binding,
        LF022ExtractionReuseAttestationV1,
        owner="extraction-reuse attestation",
    )
    if restored != attestation:
        raise LF022ExtractionReuseError("supplied attestation differs from bound bytes")
    policy, policy_binding = load_reviewed_lf022_extraction_reuse_policy(repo_root)
    if attestation.policy != policy_binding:
        raise LF022ExtractionReuseError("attestation binds a non-reviewed policy")
    if (
        attestation.decision != policy.decision
        or attestation.old_extraction_manifest != policy.old_extraction_manifest
        or attestation.old_theorem_records != policy.old_theorem_records
        or attestation.context_records != policy.context_records
        or attestation.mathlib_source_frame != policy.mathlib_source_frame
        or attestation.old_git_revision != policy.old_git_revision
        or attestation.old_code_tree_hash != policy.old_code_tree_hash
        or attestation.new_git_revision != policy.reviewed_representation_git_revision
        or attestation.new_code_tree_hash != policy.reviewed_representation_code_tree_hash
        or attestation.audited_diff_sha256 != policy.audited_diff_sha256
        or attestation.source != policy.source
        or attestation.mathlib_revision != policy.mathlib_revision
        or attestation.context_hash != policy.context_hash
        or attestation.environment_hash != policy.environment_hash
    ):
        raise LF022ExtractionReuseError("attestation differs from reviewed policy")
    if (
        attestation.old_extraction_manifest != extraction_manifest_binding
        or attestation.old_theorem_records != theorem_records_binding
        or attestation.new_representation_manifest != representation_manifest_binding
        or attestation.new_representation_records != representation_records_binding
    ):
        raise LF022ExtractionReuseError("attestation artifact bindings differ from inputs")
    restored_extraction_manifest = _load_json_model(
        repo_root,
        extraction_manifest_binding,
        OutputManifest,
        owner="old extraction manifest",
    )
    restored_representation_manifest = _load_json_model(
        repo_root,
        representation_manifest_binding,
        OutputManifest,
        owner="new representation manifest",
    )
    if (
        restored_extraction_manifest != extraction_manifest
        or restored_representation_manifest != representation_manifest
    ):
        raise LF022ExtractionReuseError("supplied manifest differs from bound bytes")
    for owner, binding in (
        ("old theorem records", attestation.old_theorem_records),
        ("context records", attestation.context_records),
        ("mathlib source frame", attestation.mathlib_source_frame),
        ("new representation records", attestation.new_representation_records),
    ):
        _verify_bound_file(repo_root, binding, owner=owner)
    if (
        extraction_manifest.stage is not DataStage.ELABORATED
        or extraction_manifest.artifact_class is not ArtifactClass.PRODUCTION
        or extraction_manifest.source != policy.source
        or extraction_manifest.row_count != policy.old_theorem_record_count
        or extraction_manifest.code_tree_hash != attestation.old_code_tree_hash
        or extraction_manifest.code.git_revision != attestation.old_git_revision
        or extraction_manifest.code.code_tree_hash != attestation.old_code_tree_hash
        or extraction_manifest.environment_hash != attestation.environment_hash
        or extraction_manifest.context_hash != attestation.context_hash
        or extraction_manifest.source_revision != attestation.mathlib_revision
        or representation_manifest.code_tree_hash != attestation.new_code_tree_hash
        or representation_manifest.code.git_revision != attestation.new_git_revision
        or representation_manifest.code.code_tree_hash != attestation.new_code_tree_hash
        or representation_manifest.environment_hash != attestation.environment_hash
        or representation_manifest.stage is not DataStage.REPRESENTED
        or representation_manifest.artifact_class is not ArtifactClass.PRODUCTION
        or representation_manifest.source != policy.source
        or representation_manifest.source_revision != "from_theorem_partition"
        or representation_manifest.attempted_row_count != policy.old_theorem_record_count
    ):
        raise LF022ExtractionReuseError("attested manifest provenance differs from inputs")
    if (
        extraction_manifest.output_partition_checksums.get(policy.old_theorem_records.path)
        != policy.old_theorem_records.sha256
        or extraction_manifest.file_checksums.get(policy.old_theorem_records.path)
        != policy.old_theorem_records.sha256
        or extraction_manifest.input_partition_checksums.get(policy.mathlib_source_frame.path)
        != policy.mathlib_source_frame.sha256
    ):
        raise LF022ExtractionReuseError("attested extraction artifact bindings differ")
    if _jsonl_record_count(repo_root, theorem_records_binding) != policy.old_theorem_record_count:
        raise LF022ExtractionReuseError("attested theorem-record count differs")
    representation_count = _jsonl_record_count(repo_root, representation_records_binding)
    if representation_manifest.row_count != representation_count:
        raise LF022ExtractionReuseError("attested representation-record count differs")
    representation_output_path = _unique_manifest_checksum_path(
        representation_manifest.output_partition_checksums,
        representation_records_binding.sha256,
        owner="attested representation output partition",
    )
    representation_file_path = _unique_manifest_checksum_path(
        representation_manifest.file_checksums,
        representation_records_binding.sha256,
        owner="attested representation output file",
    )
    if representation_output_path != representation_file_path:
        raise LF022ExtractionReuseError(
            "attested representation checksum maps bind different paths"
        )
    representation_input_path = _manifest_theorem_input_path(
        representation_manifest,
        theorem_records_binding.sha256,
    )
    if (
        hash_canonical(
            {
                "schema": "lf022_representation_input_locator_v1",
                "path": representation_input_path,
            }
        )
        != attestation.representation_input_theorem_locator_hash
    ):
        raise LF022ExtractionReuseError(
            "representation theorem-input locator differs from attestation"
        )
    if require_current_attesting_code_state:
        current_revision, current_tree = _verify_current_reviewed_paths(
            repo_root,
            policy,
            require_clean=False,
        )
        if (
            current_revision != attestation.attesting_git_revision
            or current_tree != attestation.attesting_code_tree_hash
        ):
            raise LF022ExtractionReuseError("current attesting code tree differs from attestation")


__all__ = [
    "LF022_EXTRACTION_REUSE_POLICY_PATH",
    "LF022_EXTRACTION_REUSE_POLICY_SHA256",
    "LF022ExtractionCriticalPath",
    "LF022ExtractionReuseArtifactBinding",
    "LF022ExtractionReuseAttestationV1",
    "LF022ExtractionReuseError",
    "LF022ExtractionReusePolicyV1",
    "freeze_lf022_extraction_reuse_attestation",
    "load_reviewed_lf022_extraction_reuse_policy",
    "narrow_lf022_extraction_reuse_binding",
    "verify_lf022_extraction_reuse_attestation",
]
