"""Exact, immutable recovery of one E2 infrastructure-error result."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, cast

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.models import StrictModel
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.lean.protocol import LeanRequest, LeanResult
from leanfaith.lean.session_policy import ServerMode
from leanfaith.lean.versions import in_advertised_range, parse_lean_version
from leanfaith.transforms.provisional_pair_combine import (
    ProvisionalPairCombineError,
    _load_root,
    _root_tree,
)
from leanfaith.transforms.v2_e0_scale_run import _canonical_line
from leanfaith.transforms.v2_e2_materializer import V2E2MaterializationResult
from leanfaith.transforms.v2_e2_recovery_schema import (
    RecoveryLeanAttempt,
    RecoveryPipelineAttempt,
    V2E2RecoverySpec,
    build_recovery_receipt,
    build_recovery_spec,
)
from leanfaith.transforms.v2_e2_runtime import V2E2Runtime, build_v2_e2_runtime
from leanfaith.transforms.v2_e2_scale import (
    E2_CANDIDATE_TIMEOUT_SECONDS,
    E2_INFRASTRUCTURE_MAX_ATTEMPTS,
    E2_INFRASTRUCTURE_RETRY_STATUSES,
    V2E2MaterializationInput,
    materialize_v2_e2_batch,
)
from leanfaith.transforms.v2_e2_scale_run import (
    V2E2ScaleRunManifest,
    V2E2ScaleRunManifestLegacyV1,
    V2E2ScaleRunManifestLegacyV2,
    V2E2ScaleRunSpec,
    V2E2ScaleRunSpecLegacyV1,
    V2E2ScaleRunSpecLegacyV2,
    _assemble_results,
    _iter_aligned_inputs,
    _seed,
    _write_e2_immutable,
)

_RUN_SPEC = "run_spec.json"
_MANIFEST = "manifest.json"
_RESULTS = "results.jsonl"
_RECOVERY_SPEC = "recovery_spec.json"
_RECOVERY_RECEIPT = "recovery_receipt.json"
_LEAN_VERSION = re.compile(r"Lean \(version (?P<version>\d+\.\d+\.\d+(?:-rc\d+)?)")
type E2RunSpec = V2E2ScaleRunSpec | V2E2ScaleRunSpecLegacyV2 | V2E2ScaleRunSpecLegacyV1
type E2Manifest = V2E2ScaleRunManifest | V2E2ScaleRunManifestLegacyV2 | V2E2ScaleRunManifestLegacyV1
type BackendFactory = Callable[[BackendSettings], LeanInteractBackend]
type ToolchainProbe = Callable[[Path], tuple[str, str, str]]


class V2E2RecoveryError(RuntimeError):
    """The requested repair did not satisfy the exact recovery contract."""


@dataclass(frozen=True, slots=True)
class V2E2RecoveryArtifacts:
    output_dir: Path
    recovery_spec_path: Path
    recovery_receipt_path: Path
    manifest_path: Path
    results_path: Path
    replacement_result_id: str


@dataclass(frozen=True, slots=True)
class _Target:
    line_number: int
    batch_index: int
    batch_line_number: int
    raw_line: bytes
    result: V2E2MaterializationResult


@dataclass(frozen=True, slots=True)
class _RecordedLeanCall:
    request: LeanRequest
    result: LeanResult
    stage: Literal["candidate_validation", "candidate_representation"]


class _RecordingBackend:
    """Delegate backend that records only candidate-validation retry lineage."""

    def __init__(self, backend: LeanInteractBackend) -> None:
        self.backend = backend
        self.calls: list[_RecordedLeanCall] = []

    @staticmethod
    def _stage(
        request: LeanRequest,
    ) -> Literal["candidate_validation", "candidate_representation"]:
        if request.metadata.get("artifact_kind") == "v2_e2_candidate":
            return "candidate_validation"
        if request.request_id.startswith("repr-"):
            return "candidate_representation"
        raise V2E2RecoveryError(f"unexpected Lean recovery request: {request.request_id}")

    def _freshen_representation_retry(self, requests: Sequence[LeanRequest]) -> None:
        if not requests:
            return
        stages = {self._stage(request) for request in requests}
        attempts = {str(request.metadata.get("attempt", "0")) for request in requests}
        if len(stages) != 1 or len(attempts) != 1:
            raise V2E2RecoveryError("mixed Lean recovery retry batch")
        if stages == {"candidate_representation"} and attempts != {"0"}:
            self.backend.reset_session()

    @property
    def candidate_attempts(self) -> tuple[_RecordedLeanCall, ...]:
        return tuple(call for call in self.calls if call.stage == "candidate_validation")

    @property
    def execution_binding(self) -> object:
        return self.backend.execution_binding

    def run(self, request: LeanRequest) -> LeanResult:
        self._freshen_representation_retry((request,))
        result = self.backend.run(request)
        self.calls.append(_RecordedLeanCall(request, result, self._stage(request)))
        return result

    def run_batch(self, requests: Sequence[LeanRequest]) -> list[LeanResult]:
        self._freshen_representation_retry(requests)
        results = self.backend.run_batch(requests)
        for request, result in zip(requests, results, strict=True):
            self.calls.append(_RecordedLeanCall(request, result, self._stage(request)))
        return results

    def reset_session(self) -> None:
        self.backend.reset_session()

    def close(self) -> None:
        self.backend.close()


def _parse_canonical_model[ModelT: StrictModel](path: Path, model: type[ModelT]) -> ModelT:
    if not path.is_file() or path.is_symlink():
        raise V2E2RecoveryError(f"required artifact is not a regular file: {path}")
    raw = path.read_bytes()
    try:
        payload = json.loads(raw)
        parsed = model.model_validate(payload)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise V2E2RecoveryError(f"invalid {model.__name__} at {path}: {exc}") from exc
    if raw != _canonical_line(parsed):
        raise V2E2RecoveryError(f"non-canonical {model.__name__}: {path}")
    return parsed


def _load_spec_and_manifest(parent_root: Path) -> tuple[E2RunSpec, E2Manifest]:
    spec_path = parent_root / _RUN_SPEC
    try:
        raw = json.loads(spec_path.read_bytes())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise V2E2RecoveryError(f"cannot parse E2 run spec: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("artifact_kind") != (
        "deterministic_v2_e2_scale_run_spec"
    ):
        raise V2E2RecoveryError("parent root is not a deterministic E2 scale root")
    schema_version = raw.get("schema_version")
    if schema_version == 1:
        spec: E2RunSpec = _parse_canonical_model(spec_path, V2E2ScaleRunSpecLegacyV1)
        manifest_model: type[E2Manifest] = cast(type[E2Manifest], V2E2ScaleRunManifestLegacyV1)
    elif schema_version == 2:
        spec = _parse_canonical_model(spec_path, V2E2ScaleRunSpecLegacyV2)
        manifest_model = cast(type[E2Manifest], V2E2ScaleRunManifestLegacyV2)
    elif schema_version == 3:
        spec = _parse_canonical_model(spec_path, V2E2ScaleRunSpec)
        manifest_model = cast(type[E2Manifest], V2E2ScaleRunManifest)
    else:
        raise V2E2RecoveryError(f"unsupported E2 run schema: {schema_version!r}")
    manifest = _parse_canonical_model(parent_root / _MANIFEST, manifest_model)
    if manifest.run_spec_sha256 != hash_file(spec_path):
        raise V2E2RecoveryError("parent manifest does not bind run_spec.json")
    return spec, manifest


def _scan_target(
    parent_root: Path,
    *,
    result_id: str | None,
    attempt_id: str | None,
    line_number: int,
) -> _Target:
    if result_id is None and attempt_id is None:
        raise V2E2RecoveryError("one result_id or attempt_id selector is required")
    selected: tuple[int, bytes, V2E2MaterializationResult] | None = None
    result_id_matches = 0
    attempt_id_matches = 0
    results_path = parent_root / _RESULTS
    with results_path.open("rb") as handle:
        for actual_line, raw_line in enumerate(handle, start=1):
            try:
                result = V2E2MaterializationResult.model_validate(json.loads(raw_line))
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
                raise V2E2RecoveryError(
                    f"invalid target result at {results_path}:{actual_line}: {exc}"
                ) from exc
            if raw_line != _canonical_line(result):
                raise V2E2RecoveryError(f"non-canonical result at line {actual_line}")
            if result_id is not None and result.result_id == result_id:
                result_id_matches += 1
            if attempt_id is not None and result.attempt.attempt_id == attempt_id:
                attempt_id_matches += 1
            if actual_line == line_number:
                selected = (actual_line, raw_line, result)
    if selected is None:
        raise V2E2RecoveryError("requested result line does not exist")
    actual_line, raw_line, result = selected
    if result_id is not None and (result.result_id != result_id or result_id_matches != 1):
        raise V2E2RecoveryError("target result_id is not unique at the requested line")
    if attempt_id is not None and (
        result.attempt.attempt_id != attempt_id or attempt_id_matches != 1
    ):
        raise V2E2RecoveryError("target attempt_id is not unique at the requested line")
    if result.terminal_status != "candidate_infrastructure_error":
        raise V2E2RecoveryError("target is not a candidate_infrastructure_error")
    if result.draft is None:
        raise V2E2RecoveryError("infrastructure result lacks its generated draft")

    cumulative = 0
    journal_dir = parent_root / "journal"
    for batch_index, batch_path in enumerate(sorted(journal_dir.glob("batch_*.jsonl"))):
        lines = batch_path.read_bytes().splitlines(keepends=True)
        if cumulative < actual_line <= cumulative + len(lines):
            batch_line = actual_line - cumulative
            if lines[batch_line - 1] != raw_line:
                raise V2E2RecoveryError("target result does not match its journal line")
            return _Target(actual_line, batch_index, batch_line, raw_line, result)
        cumulative += len(lines)
    raise V2E2RecoveryError("target result is absent from the journal")


def _default_toolchain_probe(project_dir: Path) -> tuple[str, str, str]:
    toolchain_path = project_dir / "lean-toolchain"
    try:
        checked_in = toolchain_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise V2E2RecoveryError(f"cannot read project toolchain: {exc}") from exc
    try:
        completed = subprocess.run(
            ["lake", "env", "lean", "--version"],
            cwd=project_dir,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise V2E2RecoveryError(f"project-pinned Lean version probe failed: {exc}") from exc
    output = (completed.stdout or completed.stderr).strip()
    match = _LEAN_VERSION.search(output)
    if match is None:
        raise V2E2RecoveryError(f"unrecognized project Lean version output: {output!r}")
    resolved = f"v{match.group('version')}"
    checked_version = parse_lean_version(checked_in)
    resolved_version = parse_lean_version(resolved)
    if checked_version != resolved_version:
        raise V2E2RecoveryError(
            f"resolved Lean {resolved_version} differs from checked-in {checked_version}"
        )
    if not in_advertised_range(resolved_version):
        raise V2E2RecoveryError(
            f"project Lean {resolved_version} is outside LeanInteract's advertised range"
        )
    return checked_in, resolved, output


def _new_backend(settings: BackendSettings) -> LeanInteractBackend:
    LeanInteractBackend.prepare_environment(settings)
    return LeanInteractBackend(replace(settings, environment_is_prepared=True))


def _copy_raw_attempts(
    calls: Sequence[_RecordedLeanCall],
    *,
    work_root: Path,
    output_root: Path,
) -> tuple[tuple[RecoveryLeanAttempt, ...], tuple[RecoveryPipelineAttempt, ...]]:
    copied_destinations: dict[Path, Path] = {}
    raw_dir = work_root / "raw_lean"
    output_raw_dir = output_root / "raw_lean"
    output_raw_dir.mkdir(parents=True, exist_ok=True)
    for source in sorted(raw_dir.rglob("*")):
        if source.is_symlink() or not source.is_file():
            if source.is_dir():
                continue
            raise V2E2RecoveryError(f"invalid raw recovery artifact: {source}")
        relative = source.relative_to(raw_dir)
        destination = output_raw_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if hash_file(destination) != hash_file(source):
                content_suffix = hash_file(source)[:16]
                destination = destination.with_name(
                    f"{destination.stem}.recovery-{content_suffix}{destination.suffix}"
                )
                if destination.exists() and hash_file(destination) != hash_file(source):
                    raise V2E2RecoveryError(
                        f"content-addressed raw response conflict at {destination}"
                    )
                if not destination.exists():
                    shutil.copy2(source, destination)
        else:
            shutil.copy2(source, destination)
        copied_destinations[source.resolve()] = destination
    pipeline_records: list[RecoveryPipelineAttempt] = []
    candidate_records: list[RecoveryLeanAttempt] = []
    candidate_index = 0
    for sequence_index, call in enumerate(calls):
        request = call.request
        result = call.result
        if result.raw_response_path is None:
            raise V2E2RecoveryError("Lean recovery attempt lacks a raw response artifact")
        source = Path(result.raw_response_path).resolve(strict=True)
        try:
            source.relative_to(raw_dir.resolve())
        except ValueError as exc:
            raise V2E2RecoveryError("Lean raw response escaped recovery work root") from exc
        try:
            destination = copied_destinations[source]
        except KeyError as exc:
            raise V2E2RecoveryError("Lean recovery raw response was not copied") from exc
        attempt_index = int(str(request.metadata.get("attempt", "0")))
        try:
            raw_payload = json.loads(destination.read_bytes())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise V2E2RecoveryError("Lean recovery raw response is not JSON") from exc
        if not isinstance(raw_payload, dict):
            raise V2E2RecoveryError("Lean recovery raw response is not an object")
        transport = raw_payload.get("transport_isolation")
        if transport is not None and not isinstance(transport, dict):
            raise V2E2RecoveryError("Lean recovery transport isolation is malformed")
        transport_attempt = None if transport is None else transport.get("attempt")
        if transport_attempt is not None and not isinstance(transport_attempt, str):
            raise V2E2RecoveryError("Lean recovery transport attempt is malformed")
        common: dict[str, object] = {
            "attempt_index": attempt_index,
            "request_id": request.request_id,
            "status": result.status.value,
            "request_hash": result.request_hash,
            "context_id": request.context_id,
            "timeout_seconds": request.timeout_seconds,
            "allow_sorry": request.allow_sorry,
            "transport_isolation_attempt": transport_attempt,
            "raw_response_relative_path": destination.relative_to(output_root).as_posix(),
            "raw_response_sha256": hash_file(destination),
        }
        pipeline_records.append(
            RecoveryPipelineAttempt.model_validate(
                {
                    "sequence_index": sequence_index,
                    "stage": call.stage,
                    **common,
                }
            )
        )
        if call.stage == "candidate_validation":
            if attempt_index != candidate_index:
                raise V2E2RecoveryError("candidate Lean attempts are not contiguous")
            candidate_records.append(RecoveryLeanAttempt.model_validate(common))
            candidate_index += 1
    return tuple(candidate_records), tuple(pipeline_records)


def _write_replacement_root(
    *,
    parent_root: Path,
    output_dir: Path,
    work_root: Path,
    spec: E2RunSpec,
    manifest: E2Manifest,
    target: _Target,
    replacement: V2E2MaterializationResult,
    recovery_spec: V2E2RecoverySpec,
    calls: Sequence[_RecordedLeanCall],
    toolchain: tuple[str, str, str],
    parent_tree_before: tuple[int, str],
) -> V2E2RecoveryArtifacts:
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}.", suffix=".partial", dir=output_dir.parent)
    )
    try:
        shutil.copytree(parent_root, staging, dirs_exist_ok=True, copy_function=shutil.copy2)
        for stale in (_RECOVERY_SPEC, _RECOVERY_RECEIPT):
            if (staging / stale).exists():
                raise V2E2RecoveryError("chained E2 recovery roots are not supported")
        raw_attempts, pipeline_attempts = _copy_raw_attempts(
            calls,
            work_root=work_root,
            output_root=staging,
        )
        _write_e2_immutable(staging / _RECOVERY_SPEC, _canonical_line(recovery_spec))

        journal_paths = tuple(sorted((staging / "journal").glob("batch_*.jsonl")))
        target_batch = journal_paths[target.batch_index]
        batch_lines = target_batch.read_bytes().splitlines(keepends=True)
        if batch_lines[target.batch_line_number - 1] != target.raw_line:
            raise V2E2RecoveryError("copied target batch changed before reconciliation")
        batch_lines[target.batch_line_number - 1] = _canonical_line(replacement)
        target_batch.unlink()
        _write_e2_immutable(target_batch, b"".join(batch_lines))

        results_path = staging / _RESULTS
        results_path.unlink()
        results_sha256 = _assemble_results(results_path, journal_paths)
        statuses = Counter(manifest.terminal_status_counts)
        family_statuses = Counter(manifest.family_status_counts)
        statuses[target.result.terminal_status] -= 1
        statuses[replacement.terminal_status] += 1
        old_family = f"{target.result.rule_id}:{target.result.terminal_status}"
        new_family = f"{replacement.rule_id}:{replacement.terminal_status}"
        family_statuses[old_family] -= 1
        family_statuses[new_family] += 1
        status_payload = {key: value for key, value in sorted(statuses.items()) if value}
        family_payload = {key: value for key, value in sorted(family_statuses.items()) if value}
        journal_entries = [(path.name, hash_file(path)) for path in journal_paths]
        manifest_payload = manifest.model_dump(mode="json")
        manifest_payload.update(
            terminal_status_counts=status_payload,
            family_status_counts=family_payload,
            journal_tree_hash=hash_canonical(journal_entries),
            results_sha256=results_sha256,
        )
        rebuilt_manifest = type(manifest).model_validate(manifest_payload)
        manifest_path = staging / _MANIFEST
        manifest_path.unlink()
        _write_e2_immutable(manifest_path, _canonical_line(rebuilt_manifest))

        if (staging / _RUN_SPEC).read_bytes() != (parent_root / _RUN_SPEC).read_bytes():
            raise V2E2RecoveryError("recovered root changed run_spec.json bytes")
        parent_tree_after = _root_tree(parent_root)
        if parent_tree_after != parent_tree_before:
            raise V2E2RecoveryError("parent root changed during recovery")

        output_tree_without_receipt = _root_tree(staging)
        checked_in, resolved, version_output = toolchain
        receipt = build_recovery_receipt(
            recovery_spec_id=recovery_spec.recovery_spec_id,
            recovery_spec_sha256=hash_file(staging / _RECOVERY_SPEC),
            replacement_result_id=replacement.result_id,
            replacement_terminal_status=replacement.terminal_status,
            replacement_result_sha256=hashlib.sha256(_canonical_line(replacement)).hexdigest(),
            lean_attempts=raw_attempts,
            pipeline_attempts=pipeline_attempts,
            checked_in_toolchain=checked_in,
            resolved_lean_version=resolved,
            resolved_lean_version_output=version_output,
            output_run_spec_sha256=hash_file(staging / _RUN_SPEC),
            output_results_sha256=hash_file(results_path),
            output_manifest_sha256=hash_file(manifest_path),
            output_journal_tree_hash=rebuilt_manifest.journal_tree_hash,
            output_root_file_count_without_receipt=output_tree_without_receipt[0],
            output_root_tree_hash_without_receipt=output_tree_without_receipt[1],
            unchanged_result_line_count=manifest.result_count - 1,
            unchanged_journal_file_count=manifest.batch_count - 1,
        )
        receipt_path = staging / _RECOVERY_RECEIPT
        _write_e2_immutable(receipt_path, _canonical_line(receipt))

        try:
            _load_root(staging)
        except ProvisionalPairCombineError as exc:
            raise V2E2RecoveryError(
                f"recovered root failed final combiner validation: {exc}"
            ) from exc

        os.rename(staging, output_dir)
        return V2E2RecoveryArtifacts(
            output_dir=output_dir,
            recovery_spec_path=output_dir / _RECOVERY_SPEC,
            recovery_receipt_path=output_dir / _RECOVERY_RECEIPT,
            manifest_path=output_dir / _MANIFEST,
            results_path=output_dir / _RESULTS,
            replacement_result_id=replacement.result_id,
        )
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def recover_v2_e2_attempt(
    *,
    parent_root: Path,
    output_dir: Path,
    repo_root: Path,
    import_header: str,
    target_result_line_number: int,
    target_result_id: str | None = None,
    target_attempt_id: str | None = None,
    profile_path: Path | None = None,
    backend_factory: BackendFactory = _new_backend,
    toolchain_probe: ToolchainProbe = _default_toolchain_probe,
) -> V2E2RecoveryArtifacts:
    """Retry one exact infrastructure result and publish a new immutable root."""

    parent_root = parent_root.resolve(strict=True)
    output_dir = output_dir.resolve()
    repo_root = repo_root.resolve(strict=True)
    if output_dir.exists():
        raise V2E2RecoveryError(f"recovery output already exists: {output_dir}")
    if output_dir.parent == parent_root or parent_root in output_dir.parents:
        raise V2E2RecoveryError("recovery output cannot be nested inside the parent root")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    if (parent_root / _RECOVERY_SPEC).exists() or (parent_root / _RECOVERY_RECEIPT).exists():
        raise V2E2RecoveryError("chained E2 recovery roots are not supported")

    spec, manifest = _load_spec_and_manifest(parent_root)
    target = _scan_target(
        parent_root,
        result_id=target_result_id,
        attempt_id=target_attempt_id,
        line_number=target_result_line_number,
    )
    try:
        _load_root(
            parent_root,
            allowed_infrastructure_result_ids=frozenset({target.result.result_id}),
        )
    except ProvisionalPairCombineError as exc:
        raise V2E2RecoveryError(f"parent root failed full validation: {exc}") from exc
    parent_tree_before = _root_tree(parent_root)

    if hashlib.sha256(import_header.encode("utf-8")).hexdigest() != spec.import_header_sha256:
        raise V2E2RecoveryError("import header does not match parent run spec")
    resolved_profile_path = (
        profile_path
        if profile_path is not None
        else repo_root
        / "configs"
        / "transformations"
        / f"{spec.profile_id.removeprefix('deterministic_')}.yaml"
    ).resolve(strict=True)
    runtime: V2E2Runtime = build_v2_e2_runtime(repo_root, path=resolved_profile_path)
    if (
        runtime.loaded.config.profile_id != spec.profile_id
        or runtime.generation_config_hash != spec.profile_config_hash
    ):
        raise V2E2RecoveryError("profile configuration differs from parent run spec")

    source_pair: tuple[Any, Any] | None = None
    for index, pair in enumerate(
        _iter_aligned_inputs(
            Path(spec.theorem_partition),
            Path(spec.representation_partition),
            max_sources=spec.max_sources,
        ),
        start=1,
    ):
        if index == target.line_number:
            source_pair = pair
            break
    if source_pair is None:
        raise V2E2RecoveryError("target source row is absent from pinned partitions")
    theorem, representation = source_pair
    (rule_id,) = runtime.rule_ids
    item = V2E2MaterializationInput(
        theorem=theorem,
        representation=representation,
        rule_id=rule_id,
        seed=_seed(spec.base_seed, theorem.theorem_id, rule_id),
    )
    execution = runtime.execute(rule_id, theorem, representation, item.seed)
    if (
        execution.attempt != target.result.attempt
        or len(execution.drafts) != 1
        or execution.drafts[0] != target.result.draft
    ):
        raise V2E2RecoveryError("exact attempt/draft does not replay from pinned sources")

    parent_file_count, parent_tree_hash = parent_tree_before
    recovery_spec = build_recovery_spec(
        parent_root_path=str(parent_root),
        parent_root_file_count=parent_file_count,
        parent_root_tree_hash=parent_tree_hash,
        parent_run_spec_sha256=hash_file(parent_root / _RUN_SPEC),
        parent_manifest_sha256=hash_file(parent_root / _MANIFEST),
        parent_results_sha256=hash_file(parent_root / _RESULTS),
        parent_journal_tree_hash=manifest.journal_tree_hash,
        target_result_id=target.result.result_id,
        target_attempt_id=target.result.attempt.attempt_id,
        target_draft_id=target.result.draft.draft_id,
        target_source_theorem_id=theorem.theorem_id,
        target_source_representation_id=representation.representation_id,
        target_result_line_number=target.line_number,
        target_batch_index=target.batch_index,
        target_batch_line_number=target.batch_line_number,
        profile_id=spec.profile_id,
        profile_config_hash=spec.profile_config_hash,
    )
    toolchain = toolchain_probe(Path(spec.project_dir).resolve(strict=True))

    work_root = Path(
        tempfile.mkdtemp(
            prefix=f".{output_dir.name}.recovery-work.",
            suffix=".partial",
            dir=output_dir.parent,
        )
    )
    backend: _RecordingBackend | None = None
    try:
        _write_e2_immutable(work_root / _RECOVERY_SPEC, _canonical_line(recovery_spec))
        settings = BackendSettings(
            project_dir=Path(spec.project_dir).resolve(strict=True),
            context_fingerprint=spec.context_id.removeprefix("ctx:"),
            environment_schema_version=1,
            raw_response_dir=work_root / "raw_lean",
            server_mode=ServerMode.STABLE,
            workers=None,
            memory_hard_limit_mb=getattr(spec, "memory_hard_limit_mb", None),
            isolate_incremental_commands=True,
            confirm_invalid_on_fresh_process=True,
        )
        backend = _RecordingBackend(backend_factory(settings))
        (replacement,) = materialize_v2_e2_batch(
            backend=cast(LeanInteractBackend, backend),
            runtime=runtime,
            inputs=(item,),
            context_id=spec.context_id,
            project_dir=Path(spec.project_dir),
            import_header=import_header,
            candidate_timeout_seconds=E2_CANDIDATE_TIMEOUT_SECONDS,
            infrastructure_max_attempts=E2_INFRASTRUCTURE_MAX_ATTEMPTS,
            fresh_session_between_infrastructure_attempts=True,
        )
        if not backend.candidate_attempts:
            raise V2E2RecoveryError("recovery did not execute the candidate Lean request")
        final_representation_results: dict[str, LeanResult] = {}
        for call in backend.calls:
            if call.stage == "candidate_representation":
                final_representation_results[call.request.request_id] = call.result
        representation_infrastructure_failure = (
            replacement.terminal_status == "candidate_representation_failed"
            and any(
                result.status in E2_INFRASTRUCTURE_RETRY_STATUSES
                for result in final_representation_results.values()
            )
        )
        if (
            replacement.terminal_status == "candidate_infrastructure_error"
            or representation_infrastructure_failure
        ):
            failure_root = output_dir.with_name(
                f".{output_dir.name}.failed-{recovery_spec.recovery_spec_id.rsplit(':', 1)[1][:16]}"
            )
            failure_payload = (
                canonical_json_bytes(
                    {
                        "artifact_kind": "deterministic_v2_e2_recovery_failure",
                        "recovery_spec_id": recovery_spec.recovery_spec_id,
                        "terminal_status": replacement.terminal_status,
                        "attempt_statuses": [call.result.status.value for call in backend.calls],
                        "failed_stage": (
                            "candidate_representation"
                            if representation_infrastructure_failure
                            else "candidate_validation"
                        ),
                        "resolved_label_count": 0,
                        "promoted_item_count": 0,
                        "training_eligible": False,
                    }
                )
                + b"\n"
            )
            _write_e2_immutable(work_root / "failure_receipt.json", failure_payload)
            if failure_root.exists():
                raise V2E2RecoveryError(f"recovery failure artifact exists: {failure_root}")
            os.rename(work_root, failure_root)
            raise V2E2RecoveryError(
                "candidate or its representation remained an infrastructure failure after "
                "fresh-session retries; "
                f"failure artifacts={failure_root}"
            )
        artifacts = _write_replacement_root(
            parent_root=parent_root,
            output_dir=output_dir,
            work_root=work_root,
            spec=spec,
            manifest=manifest,
            target=target,
            replacement=replacement,
            recovery_spec=recovery_spec,
            calls=backend.calls,
            toolchain=toolchain,
            parent_tree_before=parent_tree_before,
        )
        return artifacts
    finally:
        if backend is not None:
            backend.close()
        shutil.rmtree(work_root, ignore_errors=True)


__all__ = [
    "V2E2RecoveryArtifacts",
    "V2E2RecoveryError",
    "recover_v2_e2_attempt",
]
