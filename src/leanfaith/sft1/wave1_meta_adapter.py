"""Persistent same-request REPR and central-cache adapter for SFT1 Wave 1.

The renderer entrypoint is imported from the approved REPR freeze and called
once for the complete endpoint batch.  The session body itself must contain
the explicitly unrolled ``LeanFaith.GoalV1.emitClosedProp`` calls; this module
does not declare theorems, synthesize proofs, copy rendering code, or
re-elaborate model-facing text.
"""

from __future__ import annotations

import datetime
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, sha256_hex
from leanfaith.lean.cache import (
    EvidenceCache,
    EvidenceCacheEntry,
    EvidenceCacheKey,
    make_evidence_cache_entry,
)
from leanfaith.lean.protocol import LeanBackend
from leanfaith.representations.goal_v1 import (
    ClosedExprBatchResult,
    ClosedExprInput,
    ClosedExprSidecar,
    CompileContext,
    render_closed_expr_in_session,
)
from leanfaith.schemas.enums import EvidenceExecutionStatus, EvidenceKind, EvidenceTargetKind
from leanfaith.schemas.evidence import AuditValue, EvidenceRecord
from leanfaith.schemas.ids import (
    EVIDENCE_PREFIX,
    PAIR_PREFIX,
    REPRESENTATION_PREFIX,
    THEOREM_PREFIX,
    make_id,
)
from leanfaith.sft1.wave1_readiness import Wave1CacheKey, compute_wave1_cache_key_hash
from leanfaith.sft1.wave1_runtime import RuntimeEndpoint, TypedCertificateReceipt

_REPR_SPEC_HASH = "68d893a2c566bf3f6a82c899a32a351f9a5420f5ea98168c99b887aaa01a45a8"
_RENDERER_VERSION = "goal_v1.0"
_CLOSED_EXPR_ROUTE_ID = "closed_expr_in_session"
_CLOSED_EXPR_HASH_ALGORITHM = "sha256_canonical_closed_expr_alpha_tree_v1"
_RENDERER_API_HASH = "c695ad868c98f27218e82184559d90624491df25c7805bf29861dd891787261d"
_UNIVERSE_PROFILE_ID = "goal_v1_first_occurrence_u_i_v1"
_UNIVERSE_PROFILE_HASH = "d9e729134fcd6a086a58191810a9227062c66496ebe76b8da3c458a58b31cb61"
_RENDER_CONTEXT_ID = "goal_v1_render_context_v1"
_RENDER_CONTEXT_HASH = "5f44b6970f0902c968fc98a2659b26c1c9d0bcaef2960cd3ea73808f203f8f62"
_CACHE_METHOD_PREFIX = "sft1_wave1_readiness_v0_3_6"
_EXPECTED_IMPLEMENTATION_IDENTITY = {
    "renderer_semantic_hash": "0bec5429cc0e539841208be53cd52189a7b80cbdb4649ee2d45b84bd8a5ef1fd",
    "lean_renderer_sha256": "4471262f812746046570c51dde5958ee33db31a450a6974071efce584ba56bc3",
    "injected_helper_sha256": "a6650452eebe683db295df1dfe925d3db8b03fc24e55cbc6793e838b5fe2f272",
    "python_module_sha256": "496237e190c394e9bd3c3036e2bc01c635905116c5084787a42e6cb569f45517",
    "config_file_sha256": "a65d5b29760bbc5eb89405927f946f205eb99856c0538fdf5b57d3f9eceb0db7",
    "implementation_set_hash": "9a9252fff5ffc69cb65e71120fedffa83ed47271aecadbecf0ceb890feea65ff",
}
_REQUIRED_CACHE_CHECKS = (
    "typed_meta_validation",
    "typed_certificate_replay",
    "same_request_repr",
    "sidecars_persisted",
)


class Wave1MetaAdapterError(ValueError):
    """Raised when rendering, sidecar persistence, or cache binding fails."""


@dataclass(frozen=True, slots=True)
class RenderedWave1Batch:
    """Two-to-four chain endpoints rendered atomically in one Meta request."""

    sidecars: tuple[ClosedExprSidecar, ...]
    request_hash: str
    elapsed_ms: int
    raw_response_path: str | None
    render_scope_id: str
    sidecar_sha256s: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(self.request_hash) != 64 or any(
            character not in "0123456789abcdef" for character in self.request_hash
        ):
            raise Wave1MetaAdapterError("rendered batch request hash is not SHA-256")
        if not 2 <= len(self.sidecars) <= 4 or len(self.sidecars) != len(self.sidecar_sha256s):
            raise Wave1MetaAdapterError("Wave 1 rendered batch must contain two to four endpoints")
        if tuple(_sidecar_sha256(item) for item in self.sidecars) != self.sidecar_sha256s:
            raise Wave1MetaAdapterError("rendered batch complete-sidecar hash mismatch")
        contexts = {item.compile_context.compile_context_id for item in self.sidecars}
        scopes = {item.record.provenance.render_scope_id for item in self.sidecars}
        if len(contexts) != 1 or scopes != {self.render_scope_id}:
            raise Wave1MetaAdapterError("rendered batch context/scope mismatch")

    def model_facing_texts(self) -> tuple[str, ...]:
        return tuple(item.core_text() for item in self.sidecars)


@dataclass(frozen=True, slots=True)
class RenderedWave1Pair:
    reference: ClosedExprSidecar
    candidate: ClosedExprSidecar
    request_hash: str
    elapsed_ms: int
    raw_response_path: str | None
    render_scope_id: str
    reference_sidecar_sha256: str
    candidate_sidecar_sha256: str

    def __post_init__(self) -> None:
        if self.reference_sidecar_sha256 != _sidecar_sha256(self.reference):
            raise Wave1MetaAdapterError("reference complete-sidecar hash mismatch")
        if self.candidate_sidecar_sha256 != _sidecar_sha256(self.candidate):
            raise Wave1MetaAdapterError("candidate complete-sidecar hash mismatch")
        if self.reference.compile_context != self.candidate.compile_context:
            raise Wave1MetaAdapterError("reference/candidate compile contexts differ")
        if self.reference.record.provenance.render_scope_id != self.render_scope_id or (
            self.candidate.record.provenance.render_scope_id != self.render_scope_id
        ):
            raise Wave1MetaAdapterError("pair render scope differs from complete sidecars")

    def model_facing_texts(self) -> tuple[str, str]:
        """The sole model-facing projection permitted by the contract."""

        return self.reference.core_text(), self.candidate.core_text()


@dataclass(frozen=True, slots=True)
class PersistedWave1Sidecars:
    reference_path: Path
    candidate_path: Path
    reference_sha256: str
    candidate_sha256: str


def _sidecar_bytes(sidecar: ClosedExprSidecar) -> bytes:
    return canonical_json_bytes(sidecar.to_dict()) + b"\n"


def _sidecar_sha256(sidecar: ClosedExprSidecar) -> str:
    return sha256_hex(_sidecar_bytes(sidecar))


def _expected_representation_id(sidecar: ClosedExprSidecar) -> str:
    record = sidecar.record
    payload = {
        "renderer_version": record.renderer_version,
        "spec_hash": record.spec_hash,
        "goal_v1_source": record.goal_v1_source,
        "goal_v1": record.goal_v1,
        "rendered_goal_hash": record.rendered_goal_hash,
        "endpoint_id": record.endpoint_id,
        "endpoint_role": record.endpoint_role,
        "source_material_hash": record.source_material_hash,
        "compile_context_id": record.compile_context_id,
        "provenance": record.provenance.to_dict(),
        "implementation_identity": record.implementation_identity.to_dict(),
    }
    return "repr:" + hash_canonical(payload)


def _validate_rendered_endpoint(sidecar: ClosedExprSidecar, expected: ClosedExprInput) -> None:
    record = sidecar.record
    if record.endpoint_id != expected.endpoint_id or record.endpoint_role != expected.endpoint_role:
        raise Wave1MetaAdapterError("REPR sidecar endpoint identity/order mismatch")
    if record.provenance.expr_origin != expected.expr_origin:
        raise Wave1MetaAdapterError("REPR sidecar Expr-origin mismatch")
    if record.goal_v1_source != "closed_prop_expr":
        raise Wave1MetaAdapterError("Wave 1 endpoint did not use the closed-Expr route")
    if record.renderer_version != _RENDERER_VERSION or record.spec_hash != _REPR_SPEC_HASH:
        raise Wave1MetaAdapterError("REPR spec hash drift")
    provenance = record.provenance
    if (
        provenance.route_id != _CLOSED_EXPR_ROUTE_ID
        or provenance.expr_hash_algorithm != _CLOSED_EXPR_HASH_ALGORITHM
        or provenance.universe_profile_id != _UNIVERSE_PROFILE_ID
        or provenance.universe_profile_hash != _UNIVERSE_PROFILE_HASH
        or provenance.render_context_id != _RENDER_CONTEXT_ID
        or provenance.render_context_hash != _RENDER_CONTEXT_HASH
    ):
        raise Wave1MetaAdapterError("REPR universe or render-context binding drift")
    if record.implementation_identity.to_dict() != _EXPECTED_IMPLEMENTATION_IDENTITY:
        raise Wave1MetaAdapterError("REPR implementation identity drift")
    text = sidecar.core_text()
    if record.rendered_goal_hash != sha256_hex(text.encode("utf-8")):
        raise Wave1MetaAdapterError("REPR rendered-goal content hash mismatch")
    if record.representation_id != _expected_representation_id(sidecar):
        raise Wave1MetaAdapterError("REPR representation identity mismatch")
    if text.count("⊢") != 1:
        raise Wave1MetaAdapterError("rendered endpoint must contain exactly one turnstile")
    if "[anonymous]" in text:
        raise Wave1MetaAdapterError("anonymous_binder_name")
    if "⋯" in text:
        raise Wave1MetaAdapterError("forbidden_rendered_placeholder")


def render_wave1_pair(
    backend: LeanBackend,
    *,
    reference: ClosedExprInput,
    candidate: ClosedExprInput,
    compile_context: CompileContext,
    render_scope_id: str,
    session_body: str,
    request_id: str,
    timeout_seconds: float,
    require_distinct: bool = True,
) -> RenderedWave1Pair:
    """Render both live Expr endpoints in the same exact REPR request."""

    if reference.endpoint_role != "reference" or candidate.endpoint_role != "candidate":
        raise Wave1MetaAdapterError("Wave 1 pair endpoint roles must be reference then candidate")
    batch = render_wave1_batch(
        backend,
        inputs=(reference, candidate),
        compile_context=compile_context,
        render_scope_id=render_scope_id,
        session_body=session_body,
        request_id=request_id,
        timeout_seconds=timeout_seconds,
        require_distinct=require_distinct,
    )
    return RenderedWave1Pair(
        reference=batch.sidecars[0],
        candidate=batch.sidecars[1],
        request_hash=batch.request_hash,
        elapsed_ms=batch.elapsed_ms,
        raw_response_path=batch.raw_response_path,
        render_scope_id=batch.render_scope_id,
        reference_sidecar_sha256=batch.sidecar_sha256s[0],
        candidate_sidecar_sha256=batch.sidecar_sha256s[1],
    )


def render_wave1_batch(
    backend: LeanBackend,
    *,
    inputs: tuple[ClosedExprInput, ...],
    compile_context: CompileContext,
    render_scope_id: str,
    session_body: str,
    request_id: str,
    timeout_seconds: float,
    require_distinct: bool = True,
) -> RenderedWave1Batch:
    """Render every endpoint of a bounded composition in one exact request."""

    if not 2 <= len(inputs) <= 4:
        raise Wave1MetaAdapterError("Wave 1 endpoint batch must contain two to four Exprs")
    if inputs[0].endpoint_role != "reference" or any(
        item.endpoint_role != "candidate" for item in inputs[1:]
    ):
        raise Wave1MetaAdapterError("Wave 1 batch roles must be one reference then candidates")
    result: ClosedExprBatchResult = render_closed_expr_in_session(
        backend,
        inputs=inputs,
        compile_context=compile_context,
        render_scope_id=render_scope_id,
        session_body=session_body,
        request_id=request_id,
        timeout_seconds=timeout_seconds,
    )
    if result.failures or len(result.sidecars) != len(inputs):
        detail = "; ".join(f"{item.endpoint_id}: {item.detail}" for item in result.failures)
        raise Wave1MetaAdapterError(f"atomic Wave 1 closed-Expr rendering failed: {detail}")
    for sidecar, expected in zip(result.sidecars, inputs, strict=True):
        _validate_rendered_endpoint(sidecar, expected)
        if sidecar.compile_context != compile_context:
            raise Wave1MetaAdapterError("REPR sidecar does not bind the requested compile context")
        if sidecar.record.provenance.render_scope_id != render_scope_id:
            raise Wave1MetaAdapterError("endpoint render-scope binding mismatch")
    render_hashes = tuple(item.record.rendered_goal_hash for item in result.sidecars)
    texts = tuple(item.core_text() for item in result.sidecars)
    if require_distinct and (
        len(render_hashes) != len(set(render_hashes)) or len(texts) != len(set(texts))
    ):
        raise Wave1MetaAdapterError("required distinct endpoint rendering collapsed")
    return RenderedWave1Batch(
        sidecars=result.sidecars,
        request_hash=result.request_hash,
        elapsed_ms=result.elapsed_ms,
        raw_response_path=result.raw_response_path,
        render_scope_id=result.render_scope_id,
        sidecar_sha256s=tuple(_sidecar_sha256(item) for item in result.sidecars),
    )


def _install_immutable(path: Path, payload: bytes) -> None:
    if path.exists():
        if not path.is_file() or path.is_symlink() or path.read_bytes() != payload:
            raise Wave1MetaAdapterError(f"immutable sidecar conflict at {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".partial", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o444)
        try:
            os.link(temporary, path)
        except FileExistsError:
            if not path.is_file() or path.is_symlink() or path.read_bytes() != payload:
                raise Wave1MetaAdapterError(f"immutable sidecar conflict at {path}") from None
    finally:
        temporary.unlink(missing_ok=True)


def persist_wave1_sidecars(pair: RenderedWave1Pair, root: Path) -> PersistedWave1Sidecars:
    """Persist both complete sidecars content-addressed and without overwrite."""

    reference_payload = _sidecar_bytes(pair.reference)
    candidate_payload = _sidecar_bytes(pair.candidate)
    reference_path = (
        root / "v1" / pair.reference_sidecar_sha256[:2] / (pair.reference_sidecar_sha256 + ".json")
    )
    candidate_path = (
        root / "v1" / pair.candidate_sidecar_sha256[:2] / (pair.candidate_sidecar_sha256 + ".json")
    )
    _install_immutable(reference_path, reference_payload)
    _install_immutable(candidate_path, candidate_payload)
    return PersistedWave1Sidecars(
        reference_path=reference_path,
        candidate_path=candidate_path,
        reference_sha256=pair.reference_sidecar_sha256,
        candidate_sha256=pair.candidate_sidecar_sha256,
    )


def persist_wave1_batch_sidecars(
    batch: RenderedWave1Batch, root: Path
) -> tuple[tuple[Path, str], ...]:
    """Persist a complete composed-chain endpoint batch without overwrite."""

    persisted: list[tuple[Path, str]] = []
    for sidecar, digest in zip(batch.sidecars, batch.sidecar_sha256s, strict=True):
        payload = _sidecar_bytes(sidecar)
        path = root / "v1" / digest[:2] / f"{digest}.json"
        _install_immutable(path, payload)
        persisted.append((path, digest))
    return tuple(persisted)


def runtime_endpoints_from_pair(pair: RenderedWave1Pair) -> tuple[RuntimeEndpoint, RuntimeEndpoint]:
    """Project complete REPR sidecars into the composition-runtime boundary."""

    def project(sidecar: ClosedExprSidecar, sidecar_sha256: str) -> RuntimeEndpoint:
        record = sidecar.record
        provenance = record.provenance
        return RuntimeEndpoint(
            closed_expr_hash=provenance.expr_hash,
            render_hash=record.rendered_goal_hash,
            core_text_sha256=sha256_hex(sidecar.core_text().encode("utf-8")),
            complete_sidecar_sha256=sidecar_sha256,
            render_request_hash=pair.request_hash,
            render_scope_id=pair.render_scope_id,
            repr_spec_hash=cast(
                "Literal['68d893a2c566bf3f6a82c899a32a351f9a5420f5ea98168c99b887aaa01a45a8']",
                record.spec_hash,
            ),
            renderer_api_hash=cast(
                "Literal['c695ad868c98f27218e82184559d90624491df25c7805bf29861dd891787261d']",
                _RENDERER_API_HASH,
            ),
            universe_profile_id=cast(
                "Literal['goal_v1_first_occurrence_u_i_v1']", provenance.universe_profile_id
            ),
            universe_profile_hash=cast(
                "Literal['d9e729134fcd6a086a58191810a9227062c66496ebe76b8da3c458a58b31cb61']",
                provenance.universe_profile_hash,
            ),
            render_context_id=cast(
                "Literal['goal_v1_render_context_v1']", provenance.render_context_id
            ),
            render_context_hash=cast(
                "Literal['5f44b6970f0902c968fc98a2659b26c1c9d0bcaef2960cd3ea73808f203f8f62']",
                provenance.render_context_hash,
            ),
        )

    return (
        project(pair.reference, pair.reference_sidecar_sha256),
        project(pair.candidate, pair.candidate_sidecar_sha256),
    )


def runtime_endpoints_from_batch(batch: RenderedWave1Batch) -> tuple[RuntimeEndpoint, ...]:
    """Project an atomically rendered two-to-four endpoint batch."""

    def project(sidecar: ClosedExprSidecar, sidecar_sha256: str) -> RuntimeEndpoint:
        record = sidecar.record
        provenance = record.provenance
        return RuntimeEndpoint(
            closed_expr_hash=provenance.expr_hash,
            render_hash=record.rendered_goal_hash,
            core_text_sha256=sha256_hex(sidecar.core_text().encode("utf-8")),
            complete_sidecar_sha256=sidecar_sha256,
            render_request_hash=batch.request_hash,
            render_scope_id=batch.render_scope_id,
            repr_spec_hash=cast(
                "Literal['68d893a2c566bf3f6a82c899a32a351f9a5420f5ea98168c99b887aaa01a45a8']",
                record.spec_hash,
            ),
            renderer_api_hash=cast(
                "Literal['c695ad868c98f27218e82184559d90624491df25c7805bf29861dd891787261d']",
                _RENDERER_API_HASH,
            ),
            universe_profile_id=cast(
                "Literal['goal_v1_first_occurrence_u_i_v1']", provenance.universe_profile_id
            ),
            universe_profile_hash=cast(
                "Literal['d9e729134fcd6a086a58191810a9227062c66496ebe76b8da3c458a58b31cb61']",
                provenance.universe_profile_hash,
            ),
            render_context_id=cast(
                "Literal['goal_v1_render_context_v1']", provenance.render_context_id
            ),
            render_context_hash=cast(
                "Literal['5f44b6970f0902c968fc98a2659b26c1c9d0bcaef2960cd3ea73808f203f8f62']",
                provenance.render_context_hash,
            ),
        )

    return tuple(
        project(sidecar, digest)
        for sidecar, digest in zip(batch.sidecars, batch.sidecar_sha256s, strict=True)
    )


@dataclass(frozen=True, slots=True)
class Wave1CentralCacheBinding:
    wave1_key: Wave1CacheKey
    wave1_key_hash: str
    central_key: EvidenceCacheKey
    render_request_hash: str
    raw_response_path: str | None
    reference_sidecar_sha256: str
    candidate_sidecar_sha256: str


def bind_wave1_central_cache_key(
    wave1_key: Wave1CacheKey,
    *,
    pair: RenderedWave1Pair,
    environment_schema_version: int,
    lean_interact_version: str,
    repl_revision: str,
    timeout_seconds: float,
) -> Wave1CentralCacheBinding:
    """Bind the complete 30-field Wave 1 key into the shared evidence cache."""

    reference_representation_id = pair.reference.record.representation_id
    candidate_representation_id = pair.candidate.record.representation_id
    reference_representation_content_hash = pair.reference_sidecar_sha256
    candidate_representation_content_hash = pair.candidate_sidecar_sha256
    compile_context = pair.reference.compile_context
    compile_context_fingerprint = compile_context.fingerprint
    if (
        wave1_key.source_closed_expr_hash != pair.reference.record.provenance.expr_hash
        or wave1_key.candidate_closed_expr_hash != pair.candidate.record.provenance.expr_hash
    ):
        raise Wave1MetaAdapterError("Wave 1 cache key closed-Expr hashes differ from REPR sidecars")
    if (
        wave1_key.project_id != compile_context.project_id
        or wave1_key.project_revision != compile_context.project_revision
        or wave1_key.lean_version != compile_context.lean_version
    ):
        raise Wave1MetaAdapterError("Wave 1 cache key project context differs from REPR sidecars")
    wave1_key_hash = compute_wave1_cache_key_hash(wave1_key)
    pair_id = make_id(
        PAIR_PREFIX,
        {
            "wave1_cache_key_hash": wave1_key_hash,
            "reference_representation_id": reference_representation_id,
            "candidate_representation_id": candidate_representation_id,
        },
    )
    theorem_a_id = make_id(
        THEOREM_PREFIX,
        {"role": "reference", "closed_expr_hash": wave1_key.source_closed_expr_hash},
    )
    theorem_b_id = make_id(
        THEOREM_PREFIX,
        {"role": "candidate", "closed_expr_hash": wave1_key.candidate_closed_expr_hash},
    )
    expected_prefix = f"{REPRESENTATION_PREFIX}:"
    if not reference_representation_id.startswith(expected_prefix) or not (
        candidate_representation_id.startswith(expected_prefix)
    ):
        raise Wave1MetaAdapterError("central cache requires canonical REPR representation IDs")
    central_key = EvidenceCacheKey(
        pair_id=pair_id,
        theorem_a_id=theorem_a_id,
        theorem_b_id=theorem_b_id,
        theorem_a_statement_hash=wave1_key.source_closed_expr_hash,
        theorem_b_statement_hash=wave1_key.candidate_closed_expr_hash,
        representation_a_id=reference_representation_id,
        representation_b_id=candidate_representation_id,
        representation_a_content_hash=reference_representation_content_hash,
        representation_b_content_hash=candidate_representation_content_hash,
        representation_version=f"goal_v1:{_REPR_SPEC_HASH}",
        context_id=f"ctx:{compile_context_fingerprint}",
        context_fingerprint=compile_context_fingerprint,
        environment_schema_version=environment_schema_version,
        environment_hash=wave1_key.environment_fingerprint_hash,
        evidence_kind=EvidenceKind.TRANSFORMATION_AUDIT,
        evidence_direction="none",
        method_version=f"{_CACHE_METHOD_PREFIX}:{wave1_key_hash}",
        timeout_seconds=timeout_seconds,
        config_hash=wave1_key.policy_config_hash,
        semantic_policy_version="sft1_revision_0_3_6",
        semantic_policy_hash=wave1_key.operation_registry_entry_hash,
        lean_version=wave1_key.lean_version,
        lean_interact_version=lean_interact_version,
        repl_revision=repl_revision,
        project_revision=wave1_key.project_revision,
    )
    return Wave1CentralCacheBinding(
        wave1_key=wave1_key,
        wave1_key_hash=wave1_key_hash,
        central_key=central_key,
        render_request_hash=pair.request_hash,
        raw_response_path=pair.raw_response_path,
        reference_sidecar_sha256=pair.reference_sidecar_sha256,
        candidate_sidecar_sha256=pair.candidate_sidecar_sha256,
    )


def make_wave1_audit_evidence(
    binding: Wave1CentralCacheBinding,
    *,
    checks: Mapping[str, bool | None],
    violation_codes: tuple[str, ...],
    typed_replay_artifact_path: str,
    typed_replay_artifact_sha256: str,
    raw_response_artifact_path: str,
    raw_response_artifact_sha256: str,
    created_at: datetime.datetime,
) -> EvidenceRecord:
    """Construct evidence only; it never resolves or emits an SFT1 label."""

    if not binding.raw_response_path:
        raise Wave1MetaAdapterError("live Wave 1 evidence requires the renderer raw response path")
    if raw_response_artifact_path != binding.raw_response_path:
        raise Wave1MetaAdapterError(
            "raw response evidence path differs from the rendered Meta request"
        )
    artifact_bindings = (
        ("typed replay", typed_replay_artifact_path, typed_replay_artifact_sha256),
        ("raw response", raw_response_artifact_path, raw_response_artifact_sha256),
    )
    for role, path, digest in artifact_bindings:
        if not path:
            raise Wave1MetaAdapterError(f"{role} artifact path is empty")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise Wave1MetaAdapterError(f"{role} artifact hash is not SHA-256")
    raw_response = Path(raw_response_artifact_path)
    if raw_response.is_symlink() or not raw_response.is_file():
        raise Wave1MetaAdapterError("renderer raw response is not a regular persisted file")
    try:
        observed_raw_response_sha256 = sha256_hex(raw_response.read_bytes())
    except OSError as exc:
        raise Wave1MetaAdapterError("renderer raw response could not be read") from exc
    if observed_raw_response_sha256 != raw_response_artifact_sha256:
        raise Wave1MetaAdapterError("renderer raw response artifact hash mismatch")
    payload = {
        "pair_id": binding.central_key.pair_id,
        "wave1_cache_key_hash": binding.wave1_key_hash,
        "checks": dict(sorted(checks.items())),
        "violation_codes": list(violation_codes),
    }
    return EvidenceRecord(
        evidence_id=make_id(EVIDENCE_PREFIX, payload),
        target_kind=EvidenceTargetKind.LEAN_PAIR,
        target_id=binding.central_key.pair_id,
        kind=EvidenceKind.TRANSFORMATION_AUDIT,
        status=EvidenceExecutionStatus.SUCCESS,
        value=AuditValue(
            checks=dict(sorted(checks.items())),
            violation_codes=violation_codes,
            detail_artifact=typed_replay_artifact_path,
        ),
        method_version=binding.central_key.method_version,
        config_hash=binding.central_key.config_hash,
        raw_artifact=raw_response_artifact_path,
        created_at=created_at,
        metadata={
            "wave1_cache_key_hash": binding.wave1_key_hash,
            "typed_replay_artifact_sha256": typed_replay_artifact_sha256,
            "raw_artifact_sha256": raw_response_artifact_sha256,
        },
    )


class Wave1CentralCacheAdapter:
    """Thin fail-closed adapter over the repository's immutable central cache."""

    def __init__(self, cache: EvidenceCache) -> None:
        self._cache = cache

    @staticmethod
    def _validate_replay_receipt(
        binding: Wave1CentralCacheBinding,
        replay_receipt: TypedCertificateReceipt,
        replay_artifact_sha256: str,
    ) -> tuple[TypedCertificateReceipt, bytes]:
        try:
            replay_receipt = TypedCertificateReceipt.model_validate(
                replay_receipt.model_dump(mode="json")
            )
        except ValueError as exc:
            raise Wave1MetaAdapterError("cache entry has an invalid typed replay receipt") from exc
        payload = canonical_json_bytes(replay_receipt.model_dump(mode="json")) + b"\n"
        expected_replay_artifact_sha256 = sha256_hex(payload)
        if (
            len(replay_artifact_sha256) != 64
            or any(character not in "0123456789abcdef" for character in replay_artifact_sha256)
            or replay_artifact_sha256 != expected_replay_artifact_sha256
            or replay_receipt.operation_id != binding.wave1_key.operation_id
            or replay_receipt.source_closed_expr_hash != binding.wave1_key.source_closed_expr_hash
            or replay_receipt.candidate_closed_expr_hash
            != binding.wave1_key.candidate_closed_expr_hash
            or hash_canonical(replay_receipt.model_dump(mode="json"))
            != binding.wave1_key.evidence_certificate_payload_hash
            or replay_receipt.render_request_hash != binding.render_request_hash
            or replay_receipt.source_sidecar_sha256 != binding.reference_sidecar_sha256
            or replay_receipt.candidate_sidecar_sha256 != binding.candidate_sidecar_sha256
        ):
            raise Wave1MetaAdapterError("typed replay artifact does not bind the cache key")
        return replay_receipt, payload

    def _read_bound_artifact(
        self,
        *,
        entry: EvidenceCacheEntry,
        artifact_path: str,
        expected_sha256: str,
        role: str,
    ) -> bytes:
        if (
            len(expected_sha256) != 64
            or any(character not in "0123456789abcdef" for character in expected_sha256)
            or entry.artifact_hashes.get(artifact_path) != expected_sha256
        ):
            raise Wave1MetaAdapterError(f"central cache {role} artifact binding mismatch")
        path = Path(artifact_path)
        if not path.is_absolute():
            if self._cache.artifact_root is None:
                raise Wave1MetaAdapterError(
                    f"central cache {role} artifact is relative without an artifact root"
                )
            path = self._cache.artifact_root / path
        if path.is_symlink() or not path.is_file():
            raise Wave1MetaAdapterError(
                f"central cache {role} artifact is not a regular persisted file"
            )
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise Wave1MetaAdapterError(f"central cache {role} artifact could not be read") from exc
        if sha256_hex(payload) != expected_sha256:
            raise Wave1MetaAdapterError(f"central cache {role} artifact content hash mismatch")
        return payload

    def _read_unique_artifact_with_hash(
        self,
        *,
        entry: EvidenceCacheEntry,
        expected_sha256: str,
        role: str,
    ) -> bytes:
        """Reopen one exact content-addressed artifact during cache replay."""

        matching_paths = tuple(
            path for path, digest in entry.artifact_hashes.items() if digest == expected_sha256
        )
        if len(matching_paths) != 1:
            raise Wave1MetaAdapterError(
                f"central cache {role} must have exactly one bound artifact path"
            )
        return self._read_bound_artifact(
            entry=entry,
            artifact_path=matching_paths[0],
            expected_sha256=expected_sha256,
            role=role,
        )

    @staticmethod
    def _validate_binding(binding: Wave1CentralCacheBinding) -> None:
        wave1_key_hash = compute_wave1_cache_key_hash(binding.wave1_key)
        central_key = binding.central_key
        expected_pair_id = make_id(
            PAIR_PREFIX,
            {
                "wave1_cache_key_hash": wave1_key_hash,
                "reference_representation_id": central_key.representation_a_id,
                "candidate_representation_id": central_key.representation_b_id,
            },
        )
        expected_theorem_a_id = make_id(
            THEOREM_PREFIX,
            {
                "role": "reference",
                "closed_expr_hash": binding.wave1_key.source_closed_expr_hash,
            },
        )
        expected_theorem_b_id = make_id(
            THEOREM_PREFIX,
            {
                "role": "candidate",
                "closed_expr_hash": binding.wave1_key.candidate_closed_expr_hash,
            },
        )
        if (
            binding.wave1_key_hash != wave1_key_hash
            or central_key.pair_id != expected_pair_id
            or central_key.theorem_a_id != expected_theorem_a_id
            or central_key.theorem_b_id != expected_theorem_b_id
            or central_key.theorem_a_statement_hash != binding.wave1_key.source_closed_expr_hash
            or central_key.theorem_b_statement_hash != binding.wave1_key.candidate_closed_expr_hash
            or central_key.representation_a_content_hash != binding.reference_sidecar_sha256
            or central_key.representation_b_content_hash != binding.candidate_sidecar_sha256
            or central_key.method_version != f"{_CACHE_METHOD_PREFIX}:{wave1_key_hash}"
            or central_key.config_hash != binding.wave1_key.policy_config_hash
            or central_key.semantic_policy_hash != binding.wave1_key.operation_registry_entry_hash
            or central_key.environment_hash != binding.wave1_key.environment_fingerprint_hash
            or central_key.lean_version != binding.wave1_key.lean_version
            or central_key.project_revision != binding.wave1_key.project_revision
            or central_key.evidence_kind != EvidenceKind.TRANSFORMATION_AUDIT
        ):
            raise Wave1MetaAdapterError("central cache binding is internally inconsistent")

    def _validate_entry(
        self,
        binding: Wave1CentralCacheBinding,
        entry: EvidenceCacheEntry,
        *,
        expected_replay_receipt: TypedCertificateReceipt | None = None,
        expected_replay_artifact_sha256: str | None = None,
    ) -> TypedCertificateReceipt:
        """Replay every Wave 1 cache invariant for both reads and writes."""

        self._validate_binding(binding)
        if entry.cache_key != binding.central_key:
            raise Wave1MetaAdapterError("central cache entry does not contain the bound key")
        evidence = entry.evidence
        if evidence.metadata.get("wave1_cache_key_hash") != binding.wave1_key_hash:
            raise Wave1MetaAdapterError("central cache entry lost its Wave 1 key binding")
        if not isinstance(evidence.value, AuditValue) or any(
            evidence.value.checks.get(check) is not True for check in _REQUIRED_CACHE_CHECKS
        ):
            raise Wave1MetaAdapterError("central cache entry lacks mandatory replay checks")
        if entry.certificate_dependency_hash != binding.wave1_key.evidence_certificate_payload_hash:
            raise Wave1MetaAdapterError("central cache certificate dependency hash mismatch")
        if binding.render_request_hash not in entry.lean_request_hashes:
            raise Wave1MetaAdapterError("central cache entry lost its REPR/Meta request")
        required_sidecars = {
            binding.reference_sidecar_sha256,
            binding.candidate_sidecar_sha256,
        }
        if not required_sidecars.issubset(set(entry.artifact_hashes.values())):
            raise Wave1MetaAdapterError("central cache entry lost complete sidecars")
        self._read_unique_artifact_with_hash(
            entry=entry,
            expected_sha256=binding.reference_sidecar_sha256,
            role="reference complete sidecar",
        )
        self._read_unique_artifact_with_hash(
            entry=entry,
            expected_sha256=binding.candidate_sidecar_sha256,
            role="candidate complete sidecar",
        )

        typed_replay_path = evidence.value.detail_artifact
        typed_replay_sha256 = evidence.metadata.get("typed_replay_artifact_sha256")
        if typed_replay_path is None or not isinstance(typed_replay_sha256, str):
            raise Wave1MetaAdapterError("central cache entry lacks a typed replay artifact")
        typed_replay_payload = self._read_bound_artifact(
            entry=entry,
            artifact_path=typed_replay_path,
            expected_sha256=typed_replay_sha256,
            role="typed replay",
        )
        try:
            stored_replay_receipt = TypedCertificateReceipt.model_validate_json(
                typed_replay_payload
            )
        except ValueError as exc:
            raise Wave1MetaAdapterError(
                "central cache typed replay artifact is not a valid receipt"
            ) from exc
        stored_replay_receipt, canonical_replay_payload = self._validate_replay_receipt(
            binding,
            stored_replay_receipt,
            typed_replay_sha256,
        )
        if typed_replay_payload != canonical_replay_payload:
            raise Wave1MetaAdapterError("central cache typed replay artifact is not canonical JSON")
        if expected_replay_receipt is not None and (
            stored_replay_receipt != expected_replay_receipt
            or typed_replay_sha256 != expected_replay_artifact_sha256
        ):
            raise Wave1MetaAdapterError(
                "central cache typed replay artifact differs from the replayed receipt"
            )

        raw_response_path = evidence.raw_artifact
        raw_response_sha256 = evidence.metadata.get("raw_artifact_sha256")
        if (
            not binding.raw_response_path
            or raw_response_path != binding.raw_response_path
            or not isinstance(raw_response_sha256, str)
        ):
            raise Wave1MetaAdapterError("central cache entry lost its rendered raw response")
        self._read_bound_artifact(
            entry=entry,
            artifact_path=raw_response_path,
            expected_sha256=raw_response_sha256,
            role="raw response",
        )
        return stored_replay_receipt

    def get_after_replay(
        self,
        binding: Wave1CentralCacheBinding,
        *,
        replay_receipt: TypedCertificateReceipt,
        replay_artifact_sha256: str,
    ) -> EvidenceCacheEntry | None:
        replay_receipt, _payload = self._validate_replay_receipt(
            binding, replay_receipt, replay_artifact_sha256
        )
        entry = self._cache.get(binding.central_key)
        if entry is not None:
            self._validate_entry(
                binding,
                entry,
                expected_replay_receipt=replay_receipt,
                expected_replay_artifact_sha256=replay_artifact_sha256,
            )
        return entry

    def put(
        self,
        binding: Wave1CentralCacheBinding,
        evidence: EvidenceRecord,
        *,
        lean_request_hashes: tuple[str, ...],
        certificate_dependency_hash: str,
        artifact_hashes: dict[str, str],
    ) -> EvidenceCacheEntry:
        try:
            proposed = make_evidence_cache_entry(
                binding.central_key,
                evidence,
                lean_request_hashes=lean_request_hashes,
                certificate_dependency_hash=certificate_dependency_hash,
                artifact_hashes=artifact_hashes,
            )
        except ValueError as exc:
            raise Wave1MetaAdapterError("proposed central cache entry is invalid") from exc
        self._validate_entry(binding, proposed)
        installed = self._cache.put(
            binding.central_key,
            evidence,
            lean_request_hashes=lean_request_hashes,
            certificate_dependency_hash=certificate_dependency_hash,
            artifact_hashes=artifact_hashes,
        )
        self._validate_entry(binding, installed)
        replayed = self._cache.get(binding.central_key)
        if replayed != installed:
            raise Wave1MetaAdapterError("central cache immutable readback mismatch")
        self._validate_entry(binding, replayed)
        return installed


__all__ = [
    "PersistedWave1Sidecars",
    "RenderedWave1Batch",
    "RenderedWave1Pair",
    "Wave1CentralCacheAdapter",
    "Wave1CentralCacheBinding",
    "Wave1MetaAdapterError",
    "bind_wave1_central_cache_key",
    "make_wave1_audit_evidence",
    "persist_wave1_batch_sidecars",
    "persist_wave1_sidecars",
    "render_wave1_batch",
    "render_wave1_pair",
    "runtime_endpoints_from_batch",
    "runtime_endpoints_from_pair",
]
