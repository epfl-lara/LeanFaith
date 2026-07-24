"""End-to-end LF-020 symbolic-evidence collection.

This module creates evidence and enriched pair partitions only.  It never
constructs a ``ResolvedLabel`` and never interprets ``not_proved`` or
``not_found`` as a semantic negative.
"""

from __future__ import annotations

import datetime
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.evidence.certificates import ClaimAlignmentSpec
from leanfaith.evidence.config import LoadedEvidenceConfigs, ProofMethodConfig
from leanfaith.lean.axiom_audit import CertificateAudit
from leanfaith.lean.cache import (
    EvidenceCache,
    EvidenceCacheEntry,
    EvidenceCacheKey,
    compute_evidence_cache_key_hash,
)
from leanfaith.lean.commands import Direction, PropositionPairSource
from leanfaith.lean.counterexample import CounterexampleAttempt, run_counterexample_attempt
from leanfaith.lean.proof_search import (
    ProofAttemptResult,
    run_defeq_check,
    run_directional_proof_attempt,
)
from leanfaith.lean.protocol import LeanBackend, LeanResult, LeanStatus
from leanfaith.lean.typecheck import PropositionPreflight, run_proposition_preflight
from leanfaith.representations.views import normalize_pp_universe_placeholders
from leanfaith.schemas.enums import (
    EvidenceExecutionStatus,
    EvidenceKind,
    EvidenceTargetKind,
)
from leanfaith.schemas.evidence import (
    AuditValue,
    ClaimAlignmentValue,
    CounterexampleValue,
    DefeqValue,
    EvidenceRecord,
    ProofValue,
)
from leanfaith.schemas.ids import EVIDENCE_PREFIX, make_id
from leanfaith.schemas.pair import PairRecord
from leanfaith.schemas.theorem import ContextRecord, RepresentationRecord, TheoremRecord

EvidenceDirection = Literal["none", "A_to_B", "B_to_A", "equivalence_only"]


class EvidencePipelineError(RuntimeError):
    """Input lineage, cache, or artifact invariants failed closed."""


@dataclass(frozen=True, slots=True)
class EvidenceCollectorSettings:
    root: Path
    artifact_dir: Path
    environment_hash: str
    semantic_policy_version: str
    semantic_policy_hash: str
    created_at: datetime.datetime

    def __post_init__(self) -> None:
        if self.created_at.tzinfo is None or self.created_at.utcoffset() != datetime.timedelta(0):
            raise ValueError("evidence collection timestamp must be timezone-aware UTC")


@dataclass(frozen=True, slots=True)
class PairEvidenceResult:
    pair: PairRecord
    evidence: tuple[EvidenceRecord, ...]
    cache_hits: int
    cache_misses: int


def _forbidden_constants(theorem_a: TheoremRecord, theorem_b: TheoremRecord) -> tuple[str, ...]:
    names = {
        name
        for theorem in (theorem_a, theorem_b)
        for name in (theorem.declaration_name, theorem.declaration_full_name)
        if name
    }
    return tuple(sorted(names))


def _pair_source(
    *,
    pair: PairRecord,
    theorem_a: TheoremRecord,
    theorem_b: TheoremRecord,
    representation_a: RepresentationRecord,
    representation_b: RepresentationRecord,
    context: ContextRecord,
) -> PropositionPairSource:
    violations = _pair_lineage_violations(
        pair=pair,
        theorem_a=theorem_a,
        theorem_b=theorem_b,
        representation_a=representation_a,
        representation_b=representation_b,
        context_a=context,
        context_b=context,
    )
    if representation_a.normalization_version != representation_b.normalization_version:
        violations.append("normalization_version_mismatch")
    if violations:
        raise EvidencePipelineError(
            f"pair {pair.pair_id} lineage invalid: {','.join(sorted(violations))}"
        )

    signature_a = representation_a.signature_explicit
    signature_b = representation_b.signature_explicit
    if signature_a is None or signature_b is None:
        raise EvidencePipelineError("signature_explicit_missing")
    if "⋯" in signature_a or "⋯" in signature_b:
        raise EvidencePipelineError("signature_explicit_contains_proof_elision")
    if "?m" in signature_a or "?m" in signature_b:
        raise EvidencePipelineError("signature_explicit_contains_metavariable")
    # Normalize each side independently.  The original pretty-printer suffixes
    # are local to separate Lean requests and do not encode semantic identity.
    signature_a = normalize_pp_universe_placeholders(signature_a)
    signature_b = normalize_pp_universe_placeholders(signature_b)
    return PropositionPairSource(
        header_text=context.header_text,
        proposition_a=signature_a,
        proposition_b=signature_b,
        pair_id=pair.pair_id,
        forbidden_declaration_constants=_forbidden_constants(theorem_a, theorem_b),
    )


def _pair_lineage_violations(
    *,
    pair: PairRecord,
    theorem_a: TheoremRecord,
    theorem_b: TheoremRecord,
    representation_a: RepresentationRecord,
    representation_b: RepresentationRecord,
    context_a: ContextRecord,
    context_b: ContextRecord,
) -> list[str]:
    """Return cross-record identity violations before any Lean execution."""

    violations: list[str] = []
    if pair.theorem_a_id != theorem_a.theorem_id:
        violations.append("pair_theorem_a_mismatch")
    if pair.theorem_b_id != theorem_b.theorem_id:
        violations.append("pair_theorem_b_mismatch")
    if representation_a.theorem_id != theorem_a.theorem_id:
        violations.append("representation_a_theorem_mismatch")
    if representation_b.theorem_id != theorem_b.theorem_id:
        violations.append("representation_b_theorem_mismatch")
    if theorem_a.context_id != context_a.context_id:
        violations.append("theorem_a_context_mismatch")
    if representation_a.context_id != context_a.context_id:
        violations.append("representation_a_context_mismatch")
    if theorem_b.context_id != context_b.context_id:
        violations.append("theorem_b_context_mismatch")
    if representation_b.context_id != context_b.context_id:
        violations.append("representation_b_context_mismatch")
    return violations


def _leading_forall_count(representation: RepresentationRecord) -> int:
    """Count the elaborated proposition's leading ``forallE`` nodes.

    Gate-3 operator trees are derived from the same elaborated expression as
    the alpha fingerprint.  The identity-only alignment template uses this
    count to fail closed on partial maps; it does not attempt general binder
    alignment or classify arbitrary reordered premises.
    """

    tree = representation.operator_tree
    if tree is None or not isinstance(tree.get("root"), dict):
        raise EvidencePipelineError("alpha_identity_assumption_v1 requires a Gate-3 operator_tree")
    node: object = tree["root"]
    count = 0
    while isinstance(node, dict) and node.get("k") == "forall":
        count += 1
        node = node.get("body")
    return count


def _validate_identity_alignment(
    alignment: ClaimAlignmentSpec | None,
    pair_id: str,
    representation_a: RepresentationRecord,
    representation_b: RepresentationRecord,
) -> None:
    """Validate the deliberately narrow v1 alignment certificate template."""

    if alignment is None:
        return
    if alignment.pair_id != pair_id:
        raise EvidencePipelineError("alignment specification targets the wrong pair")
    if alignment.direction != "both":
        raise EvidencePipelineError("alpha_identity_assumption_v1 requires direction=both")
    for label, mapping, prefix in (
        ("binder_map", alignment.binder_map, "binder:"),
        ("premise_map", alignment.premise_map, "premise:"),
    ):
        expected_keys = {f"{prefix}{index}" for index in range(len(mapping))}
        if set(mapping) != expected_keys or set(mapping.values()) != expected_keys:
            raise EvidencePipelineError(
                f"alpha_identity_assumption_v1 requires a total identity {label}"
            )
        if any(source != target for source, target in mapping.items()):
            raise EvidencePipelineError(
                f"alpha_identity_assumption_v1 requires an identity {label}"
            )
    count_a = _leading_forall_count(representation_a)
    count_b = _leading_forall_count(representation_b)
    if count_a != count_b:
        raise EvidencePipelineError(
            "alpha_identity_assumption_v1 requires equal leading forall counts"
        )
    mapped_count = len(alignment.binder_map) + len(alignment.premise_map)
    if mapped_count != count_a:
        raise EvidencePipelineError(
            "alpha_identity_assumption_v1 maps are not total over leading foralls"
        )


def _cache_key(
    *,
    pair: PairRecord,
    theorem_a: TheoremRecord,
    theorem_b: TheoremRecord,
    representation_a: RepresentationRecord,
    representation_b: RepresentationRecord,
    context: ContextRecord,
    settings: EvidenceCollectorSettings,
    kind: EvidenceKind,
    direction: EvidenceDirection,
    method_version: str,
    timeout_seconds: float,
    config_hash: str,
) -> EvidenceCacheKey:
    return EvidenceCacheKey(
        pair_id=pair.pair_id,
        theorem_a_id=theorem_a.theorem_id,
        theorem_b_id=theorem_b.theorem_id,
        theorem_a_statement_hash=theorem_a.statement_content_hash,
        theorem_b_statement_hash=theorem_b.statement_content_hash,
        representation_a_id=representation_a.representation_id,
        representation_b_id=representation_b.representation_id,
        representation_a_content_hash=representation_a.content_hash,
        representation_b_content_hash=representation_b.content_hash,
        representation_version=representation_a.normalization_version,
        context_id=context.context_id,
        context_fingerprint=context.context_fingerprint,
        environment_schema_version=context.environment_schema_version,
        environment_hash=settings.environment_hash,
        evidence_kind=kind,
        evidence_direction=direction,
        method_version=method_version,
        timeout_seconds=timeout_seconds,
        config_hash=config_hash,
        semantic_policy_version=settings.semantic_policy_version,
        semantic_policy_hash=settings.semantic_policy_hash,
        lean_version=context.lean_version,
        lean_interact_version=context.lean_interact_version,
        repl_revision=context.repl_revision,
        project_revision=context.project_revision,
    )


def _result_artifact(
    result: LeanResult,
    *,
    root: Path,
) -> tuple[dict[str, object], dict[str, str]]:
    raw_path: str | None = None
    artifacts: dict[str, str] = {}
    if result.raw_response_path is not None:
        path = Path(result.raw_response_path)
        if path.is_file():
            try:
                raw_path = path.relative_to(root).as_posix()
            except ValueError:
                raw_path = str(path)
            artifacts[raw_path] = hash_file(path)
    return (
        {
            "request_hash": result.request_hash,
            "status": result.status.value,
            "messages": list(result.messages),
            "sorries": list(result.sorries),
            "raw_response_path": raw_path,
            "infrastructure_error": result.infrastructure_error,
        },
        artifacts,
    )


def _write_immutable_artifact(
    *,
    settings: EvidenceCollectorSettings,
    cache_key_hash: str,
    payload: dict[str, object],
) -> tuple[str, str]:
    path = settings.artifact_dir / cache_key_hash[:2] / f"{cache_key_hash}.json"
    data = canonical_json_bytes(payload) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise EvidencePipelineError(f"immutable evidence artifact conflict: {path}")
    else:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{cache_key_hash}.",
            suffix=".partial",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o444)
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != data:
                    raise EvidencePipelineError(
                        f"immutable evidence artifact conflict: {path}"
                    ) from None
        finally:
            temporary.unlink(missing_ok=True)
    try:
        relative = path.relative_to(settings.root).as_posix()
    except ValueError:
        relative = str(path)
    return relative, hash_file(path)


def _evidence_id(
    *,
    key_hash: str,
    status: EvidenceExecutionStatus,
    value: object,
    auxiliary_ids: tuple[str, ...] = (),
) -> str:
    payload = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    return make_id(
        EVIDENCE_PREFIX,
        {
            "schema": "symbolic_evidence_v1",
            "cache_key_hash": key_hash,
            "status": status.value,
            "value": payload,
            "auxiliary_ids": tuple(sorted(auxiliary_ids)),
        },
    )


def _audit_record(
    *,
    pair_id: str,
    audit: CertificateAudit,
    method_version: str,
    config_hash: str,
    detail_artifact: str,
    created_at: datetime.datetime,
    cache_key_hash: str,
) -> EvidenceRecord:
    value = AuditValue(
        checks=dict(sorted(audit.checks.items())),
        violation_codes=audit.violation_codes,
        detail_artifact=detail_artifact,
    )
    evidence_id = make_id(
        EVIDENCE_PREFIX,
        {
            "schema": "certificate_audit_v1",
            "pair_id": pair_id,
            "certificate_name": audit.certificate_name,
            "direct_constants": audit.direct_constants,
            "transitive_constants": audit.transitive_constants,
            "axioms": audit.axioms,
            "checks": audit.checks,
            "violations": audit.violation_codes,
            "method_version": method_version,
        },
    )
    return EvidenceRecord(
        evidence_id=evidence_id,
        target_kind=EvidenceTargetKind.LEAN_PAIR,
        target_id=pair_id,
        kind=EvidenceKind.AXIOM_AUDIT,
        status=EvidenceExecutionStatus.SUCCESS,
        value=value,
        method_version=f"{method_version}/axiom_dependency_audit_v1",
        config_hash=config_hash,
        raw_artifact=detail_artifact,
        created_at=created_at,
        metadata={
            "cache_key": cache_key_hash,
            "certificate_name": audit.certificate_name,
            "dependency_count": len(audit.transitive_constants),
            "axiom_count": len(audit.axioms),
        },
    )


def _operational_status(statuses: list[LeanStatus]) -> EvidenceExecutionStatus:
    if LeanStatus.TIMEOUT in statuses:
        return EvidenceExecutionStatus.TIMEOUT
    if any(
        status
        in {
            LeanStatus.CRASH,
            LeanStatus.SETUP_ERROR,
            LeanStatus.INTERNAL_ERROR,
        }
        for status in statuses
    ):
        return EvidenceExecutionStatus.ERROR
    if LeanStatus.UNSUPPORTED in statuses:
        return EvidenceExecutionStatus.UNSUPPORTED
    return EvidenceExecutionStatus.ERROR


def _unsupported_value(kind: EvidenceKind) -> ClaimAlignmentValue | CounterexampleValue | None:
    if kind == EvidenceKind.CLAIM_ALIGNMENT:
        return ClaimAlignmentValue(
            alignment_version="unsupported_v1",
            binder_map={},
            premise_map={},
            conclusion_role_map={},
            direction="both",
            outcome="unsupported",
        )
    if kind == EvidenceKind.COUNTEREXAMPLE:
        return CounterexampleValue(
            outcome="unsupported",
            direction="equivalence_only",
            domain=None,
            encoding=None,
        )
    return None


class SymbolicEvidenceCollector:
    """Collect the five terminal LF-020 evidence jobs for one Lean pair."""

    def __init__(
        self,
        *,
        backend: LeanBackend,
        configs: LoadedEvidenceConfigs,
        cache: EvidenceCache,
        settings: EvidenceCollectorSettings,
    ) -> None:
        self.backend = backend
        self.configs = configs
        self.cache = cache
        self.settings = settings
        if not self._enabled_methods("closed_truth"):
            raise EvidencePipelineError(
                "portfolio has no enabled closed_truth method; not_proved would be invalid"
            )
        if not any(
            method.method_id == "exact_assumption_v1"
            for method in self._enabled_methods("binder_aligned_claim")
        ):
            raise EvidencePipelineError(
                "portfolio lacks enabled exact_assumption_v1 binder-aligned replay"
            )

    def _cached(self, key: EvidenceCacheKey) -> EvidenceCacheEntry | None:
        return self.cache.get(key)

    def _persist(
        self,
        *,
        key: EvidenceCacheKey,
        evidence: EvidenceRecord,
        auxiliary: tuple[EvidenceRecord, ...],
        generated_code_hash: str | None,
        request_hashes: tuple[str, ...],
        dependency_hash: str | None,
        artifact_hashes: dict[str, str],
    ) -> EvidenceCacheEntry:
        return self.cache.put(
            key,
            evidence,
            auxiliary_evidence=auxiliary,
            generated_code_hash=generated_code_hash,
            lean_request_hashes=request_hashes,
            certificate_dependency_hash=dependency_hash,
            artifact_hashes=artifact_hashes,
        )

    def _terminal_without_lean(
        self,
        *,
        pair: PairRecord,
        kind: EvidenceKind,
        method_version: str,
        config_hash: str,
        status: EvidenceExecutionStatus,
        reason: str,
        raw_artifact: str | None = None,
        metadata: dict[str, str | int | float | bool | None] | None = None,
    ) -> EvidenceRecord:
        value = _unsupported_value(kind) if status == EvidenceExecutionStatus.UNSUPPORTED else None
        evidence_id = make_id(
            EVIDENCE_PREFIX,
            {
                "schema": "symbolic_evidence_terminal_v1",
                "pair_id": pair.pair_id,
                "kind": kind.value,
                "method_version": method_version,
                "status": status.value,
                "reason": reason,
            },
        )
        return EvidenceRecord(
            evidence_id=evidence_id,
            target_kind=EvidenceTargetKind.LEAN_PAIR,
            target_id=pair.pair_id,
            kind=kind,
            status=status,
            value=value,
            method_version=method_version,
            config_hash=config_hash,
            raw_artifact=raw_artifact,
            created_at=self.settings.created_at,
            metadata={"terminal_reason": reason, **(metadata or {})},
        )

    def collect_pair(
        self,
        *,
        pair: PairRecord,
        theorem_a: TheoremRecord,
        theorem_b: TheoremRecord,
        representation_a: RepresentationRecord,
        representation_b: RepresentationRecord,
        context_a: ContextRecord,
        context_b: ContextRecord,
        alignment: ClaimAlignmentSpec | None = None,
    ) -> PairEvidenceResult:
        lineage_violations = _pair_lineage_violations(
            pair=pair,
            theorem_a=theorem_a,
            theorem_b=theorem_b,
            representation_a=representation_a,
            representation_b=representation_b,
            context_a=context_a,
            context_b=context_b,
        )
        if lineage_violations:
            raise EvidencePipelineError(
                f"pair {pair.pair_id} lineage invalid: {','.join(sorted(lineage_violations))}"
            )
        if context_a.context_id != context_b.context_id:
            records = tuple(
                self._terminal_without_lean(
                    pair=pair,
                    kind=kind,
                    method_version="cross_context_unsupported_v1",
                    config_hash=(
                        self.configs.counterexample.config_hash
                        if kind == EvidenceKind.COUNTEREXAMPLE
                        else self.configs.portfolio.config_hash
                    ),
                    status=EvidenceExecutionStatus.UNSUPPORTED,
                    reason="cross_context_pair_requires_explicit_bridge_policy",
                )
                for kind in (
                    EvidenceKind.DEFEQ,
                    EvidenceKind.PROOF_A_IMPLIES_B,
                    EvidenceKind.PROOF_B_IMPLIES_A,
                    EvidenceKind.CLAIM_ALIGNMENT,
                    EvidenceKind.COUNTEREXAMPLE,
                )
            )
            return self._finish(pair, records, cache_hits=0, cache_misses=0)

        try:
            source = _pair_source(
                pair=pair,
                theorem_a=theorem_a,
                theorem_b=theorem_b,
                representation_a=representation_a,
                representation_b=representation_b,
                context=context_a,
            )
        except EvidencePipelineError as exc:
            reason = str(exc)
            status = (
                EvidenceExecutionStatus.UNSUPPORTED
                if "missing" in reason or "elision" in reason or "metavariable" in reason
                else EvidenceExecutionStatus.ERROR
            )
            records = tuple(
                self._terminal_without_lean(
                    pair=pair,
                    kind=kind,
                    method_version="proposition_source_v1",
                    config_hash=(
                        self.configs.counterexample.config_hash
                        if kind == EvidenceKind.COUNTEREXAMPLE
                        else self.configs.portfolio.config_hash
                    ),
                    status=status,
                    reason=reason,
                )
                for kind in (
                    EvidenceKind.DEFEQ,
                    EvidenceKind.PROOF_A_IMPLIES_B,
                    EvidenceKind.PROOF_B_IMPLIES_A,
                    EvidenceKind.CLAIM_ALIGNMENT,
                    EvidenceKind.COUNTEREXAMPLE,
                )
            )
            return self._finish(pair, records, cache_hits=0, cache_misses=0)

        _validate_identity_alignment(
            alignment,
            pair.pair_id,
            representation_a,
            representation_b,
        )
        keys = self._keys(
            pair=pair,
            theorem_a=theorem_a,
            theorem_b=theorem_b,
            representation_a=representation_a,
            representation_b=representation_b,
            context=context_a,
            alignment=alignment,
        )
        cached = {name: self._cached(key) for name, key in keys.items()}
        if all(entry is not None for entry in cached.values()):
            records = tuple(
                record
                for name in ("defeq", "A_to_B", "B_to_A", "alignment", "counterexample")
                for record in (
                    cached[name].evidence,  # type: ignore[union-attr]
                    *cached[name].auxiliary_evidence,  # type: ignore[union-attr]
                )
            )
            return self._finish(pair, records, cache_hits=len(keys), cache_misses=0)

        preflight = run_proposition_preflight(
            self.backend,
            source=source,
            context_id=context_a.context_id,
            timeout_seconds=self.configs.portfolio.config.default_timeout_seconds,
            request_id=f"lf020-preflight-{pair.pair_id.split(':', 1)[1][:16]}",
        )
        if not preflight.valid:
            status = _operational_status([preflight.retry.result.status])
            if preflight.retry.result.status == LeanStatus.INVALID:
                status = EvidenceExecutionStatus.ERROR
            preflight_entries: dict[str, EvidenceCacheEntry] = {}
            for name, key in keys.items():
                existing = cached[name]
                if existing is not None:
                    preflight_entries[name] = existing
                    continue
                artifact, artifacts, request_hashes, dependency_hash = self._artifact_from_results(
                    key=key,
                    job=f"{key.evidence_kind.value}_preflight_failure",
                    code_hashes=(),
                    results=(),
                    preflight=preflight,
                )
                evidence = self._terminal_without_lean(
                    pair=pair,
                    kind=key.evidence_kind,
                    method_version=key.method_version,
                    config_hash=key.config_hash,
                    status=status,
                    reason=f"proposition_preflight_{preflight.retry.result.status.value}",
                    raw_artifact=artifact,
                    metadata={
                        "cache_key": compute_evidence_cache_key_hash(key),
                        "raw_artifact_sha256": artifacts[artifact],
                    },
                )
                preflight_entries[name] = self._persist(
                    key=key,
                    evidence=evidence,
                    auxiliary=(),
                    generated_code_hash=preflight.command.code_sha256,
                    request_hashes=request_hashes,
                    dependency_hash=dependency_hash,
                    artifact_hashes=artifacts,
                )
            records = tuple(
                record
                for name in (
                    "defeq",
                    "A_to_B",
                    "B_to_A",
                    "alignment",
                    "counterexample",
                )
                for record in (
                    preflight_entries[name].evidence,
                    *preflight_entries[name].auxiliary_evidence,
                )
            )
            hits = sum(entry is not None for entry in cached.values())
            return self._finish(
                pair,
                records,
                cache_hits=hits,
                cache_misses=len(keys) - hits,
            )

        produced: dict[str, EvidenceCacheEntry] = {}
        for name in ("defeq", "A_to_B", "B_to_A", "alignment", "counterexample"):
            cached_entry = cached[name]
            if cached_entry is not None:
                produced[name] = cached_entry
                continue
            key = keys[name]
            if name == "defeq":
                produced[name] = self._collect_defeq(key, source, preflight)
            elif name in {"A_to_B", "B_to_A"}:
                direction: Direction = "A_to_B" if name == "A_to_B" else "B_to_A"
                produced[name] = self._collect_direction(
                    key,
                    source,
                    direction=direction,
                    preflight=preflight,
                )
            elif name == "alignment":
                produced[name] = self._collect_alignment(
                    key,
                    source,
                    representation_a,
                    representation_b,
                    alignment,
                    preflight,
                )
            else:
                produced[name] = self._collect_counterexample(key, source, preflight)

        records = tuple(
            record
            for name in ("defeq", "A_to_B", "B_to_A", "alignment", "counterexample")
            for record in (produced[name].evidence, *produced[name].auxiliary_evidence)
        )
        hits = sum(entry is not None for entry in cached.values())
        return self._finish(pair, records, cache_hits=hits, cache_misses=len(keys) - hits)

    def _keys(
        self,
        *,
        pair: PairRecord,
        theorem_a: TheoremRecord,
        theorem_b: TheoremRecord,
        representation_a: RepresentationRecord,
        representation_b: RepresentationRecord,
        context: ContextRecord,
        alignment: ClaimAlignmentSpec | None,
    ) -> dict[str, EvidenceCacheKey]:
        portfolio = self.configs.portfolio
        counterexample = self.configs.counterexample

        def new_key(
            *,
            kind: EvidenceKind,
            direction: EvidenceDirection,
            method_version: str,
            timeout_seconds: float,
            config_hash: str,
        ) -> EvidenceCacheKey:
            return _cache_key(
                pair=pair,
                theorem_a=theorem_a,
                theorem_b=theorem_b,
                representation_a=representation_a,
                representation_b=representation_b,
                context=context,
                settings=self.settings,
                kind=kind,
                direction=direction,
                method_version=method_version,
                timeout_seconds=timeout_seconds,
                config_hash=config_hash,
            )

        alignment_direction: EvidenceDirection
        if alignment is None:
            alignment_direction = "none"
        elif alignment.direction == "both":
            alignment_direction = "equivalence_only"
        else:
            alignment_direction = alignment.direction
        return {
            "defeq": new_key(
                kind=EvidenceKind.DEFEQ,
                direction="none",
                method_version="defeq_rfl_v1",
                timeout_seconds=portfolio.config.default_timeout_seconds,
                config_hash=portfolio.config_hash,
            ),
            "A_to_B": new_key(
                kind=EvidenceKind.PROOF_A_IMPLIES_B,
                direction="A_to_B",
                method_version=f"{portfolio.config.method_version}/A_to_B",
                timeout_seconds=sum(
                    method.timeout_seconds for method in portfolio.config.methods if method.enabled
                ),
                config_hash=portfolio.config_hash,
            ),
            "B_to_A": new_key(
                kind=EvidenceKind.PROOF_B_IMPLIES_A,
                direction="B_to_A",
                method_version=f"{portfolio.config.method_version}/B_to_A",
                timeout_seconds=sum(
                    method.timeout_seconds for method in portfolio.config.methods if method.enabled
                ),
                config_hash=portfolio.config_hash,
            ),
            "alignment": new_key(
                kind=EvidenceKind.CLAIM_ALIGNMENT,
                direction=alignment_direction,
                method_version=(
                    "claim_alignment_absent_v1"
                    if alignment is None
                    else f"{alignment.alignment_version}/{alignment.template_id}"
                ),
                timeout_seconds=portfolio.config.default_timeout_seconds,
                config_hash=hash_canonical(
                    {
                        "portfolio": portfolio.config_hash,
                        "alignment": (
                            None if alignment is None else alignment.model_dump(mode="json")
                        ),
                    }
                ),
            ),
            "counterexample": new_key(
                kind=EvidenceKind.COUNTEREXAMPLE,
                direction="equivalence_only",
                method_version=counterexample.config.method_version,
                timeout_seconds=counterexample.config.default_timeout_seconds * 2,
                config_hash=counterexample.config_hash,
            ),
        }

    def _artifact_from_results(
        self,
        *,
        key: EvidenceCacheKey,
        job: str,
        code_hashes: tuple[str, ...],
        results: tuple[LeanResult, ...],
        audits: tuple[CertificateAudit, ...] = (),
        preflight: PropositionPreflight | None = None,
    ) -> tuple[str, dict[str, str], tuple[str, ...], str | None]:
        summaries: list[dict[str, object]] = []
        artifacts: dict[str, str] = {}
        all_code_hashes = code_hashes
        all_request_hashes: tuple[str, ...] = ()
        if preflight is not None:
            summary, raw = _result_artifact(preflight.retry.result, root=self.settings.root)
            summaries.append({"stage": "pair_preflight", **summary})
            artifacts.update(raw)
            all_code_hashes = (preflight.command.code_sha256, *all_code_hashes)
            all_request_hashes = (preflight.retry.result.request_hash,)
        for result in results:
            summary, raw = _result_artifact(result, root=self.settings.root)
            summaries.append({"stage": job, **summary})
            artifacts.update(raw)
        all_request_hashes += tuple(result.request_hash for result in results)
        audit_payload = [
            {
                "certificate_name": audit.certificate_name,
                "direct_constants": audit.direct_constants,
                "transitive_constants": audit.transitive_constants,
                "axioms": audit.axioms,
                "checks": audit.checks,
                "violation_codes": audit.violation_codes,
            }
            for audit in audits
        ]
        key_hash = compute_evidence_cache_key_hash(key)
        relative, digest = _write_immutable_artifact(
            settings=self.settings,
            cache_key_hash=key_hash,
            payload={
                "schema_version": 1,
                "job": job,
                "cache_key_hash": key_hash,
                "generated_code_hashes": all_code_hashes,
                "results": summaries,
                "certificate_audits": audit_payload,
            },
        )
        artifacts[relative] = digest
        dependency_hash = hash_canonical(audit_payload) if audit_payload else None
        return (
            relative,
            artifacts,
            all_request_hashes,
            dependency_hash,
        )

    def _collect_defeq(
        self,
        key: EvidenceCacheKey,
        source: PropositionPairSource,
        preflight: PropositionPreflight,
    ) -> EvidenceCacheEntry:
        result = run_defeq_check(
            self.backend,
            source=source,
            context_id=key.context_id,
            timeout_seconds=key.timeout_seconds,
            request_id=f"lf020-defeq-{compute_evidence_cache_key_hash(key)[:16]}",
        )
        lean = result.retry.result
        if result.equal is None:
            status = _operational_status([lean.status])
            value = None
        else:
            status = EvidenceExecutionStatus.SUCCESS
            value = DefeqValue(outcome="equal" if result.equal else "not_equal")
        artifact, artifacts, request_hashes, dependency_hash = self._artifact_from_results(
            key=key,
            job="defeq",
            code_hashes=(result.command.code_sha256,),
            results=(lean,),
            preflight=preflight,
        )
        key_hash = compute_evidence_cache_key_hash(key)
        evidence = EvidenceRecord(
            evidence_id=_evidence_id(key_hash=key_hash, status=status, value=value),
            target_kind=EvidenceTargetKind.LEAN_PAIR,
            target_id=key.pair_id,
            kind=EvidenceKind.DEFEQ,
            status=status,
            value=value,
            method_version=key.method_version,
            config_hash=key.config_hash,
            raw_artifact=artifact,
            created_at=self.settings.created_at,
            metadata={
                "cache_key": key_hash,
                "generated_code_sha256": result.command.code_sha256,
                "raw_artifact_sha256": artifacts[artifact],
            },
        )
        return self._persist(
            key=key,
            evidence=evidence,
            auxiliary=(),
            generated_code_hash=result.command.code_sha256,
            request_hashes=request_hashes,
            dependency_hash=dependency_hash,
            artifact_hashes=artifacts,
        )

    def _enabled_methods(self, mode: str) -> tuple[ProofMethodConfig, ...]:
        return tuple(
            method
            for method in self.configs.portfolio.config.methods
            if method.enabled and mode in method.comparison_modes
        )

    def _collect_direction(
        self,
        key: EvidenceCacheKey,
        source: PropositionPairSource,
        *,
        direction: Literal["A_to_B", "B_to_A"],
        preflight: PropositionPreflight,
    ) -> EvidenceCacheEntry:
        policy = self.configs.portfolio.config.certificate_policy
        attempts: list[ProofAttemptResult] = []
        accepted: ProofAttemptResult | None = None
        for method in self._enabled_methods("closed_truth"):
            attempt = run_directional_proof_attempt(
                self.backend,
                source=source,
                context_id=key.context_id,
                direction=direction,
                method_id=method.method_id,
                tactic_body=method.tactic_body,
                timeout_seconds=method.timeout_seconds,
                request_id=(
                    f"lf020-proof-{direction}-{method.method_id}-"
                    f"{compute_evidence_cache_key_hash(key)[:12]}"
                ),
                allowed_axioms=policy.allowed_standard_axioms,
                forbidden_axioms=policy.forbidden_axioms,
            )
            attempts.append(attempt)
            if attempt.proved:
                accepted = attempt
                break

        audits = tuple(attempt.audit for attempt in attempts if attempt.audit is not None)
        code_hashes = tuple(attempt.command.code_sha256 for attempt in attempts)
        lean_results = tuple(attempt.retry.result for attempt in attempts)
        artifact, artifacts, request_hashes, dependency_hash = self._artifact_from_results(
            key=key,
            job=f"proof_{direction}",
            code_hashes=code_hashes,
            results=lean_results,
            audits=audits,
            preflight=preflight,
        )
        key_hash = compute_evidence_cache_key_hash(key)
        audit_records = tuple(
            _audit_record(
                pair_id=key.pair_id,
                audit=audit,
                method_version=f"{key.method_version}/{attempt.method_id}",
                config_hash=key.config_hash,
                detail_artifact=artifact,
                created_at=self.settings.created_at,
                cache_key_hash=key_hash,
            )
            for attempt in attempts
            if (audit := attempt.audit) is not None
        )
        audit_record_by_certificate = {
            audit.certificate_name: record
            for audit, record in zip(audits, audit_records, strict=True)
        }
        if accepted is not None:
            assert accepted.audit is not None
            status = EvidenceExecutionStatus.SUCCESS
            value: ProofValue | None = ProofValue(
                outcome="proved",
                tactic=accepted.method_id,
                axioms=accepted.audit.axioms,
            )
        elif any(attempt.policy_rejected for attempt in attempts):
            status = EvidenceExecutionStatus.ABSTAIN
            value = None
        elif any(
            attempt.retry.result.status
            in {
                LeanStatus.TIMEOUT,
                LeanStatus.CRASH,
                LeanStatus.SETUP_ERROR,
                LeanStatus.INTERNAL_ERROR,
            }
            for attempt in attempts
        ):
            status = _operational_status([attempt.retry.result.status for attempt in attempts])
            value = None
        else:
            # A complete, admission-free portfolio was tried.  This is only a
            # search outcome and never evidence of nonimplication.
            status = EvidenceExecutionStatus.SUCCESS
            value = ProofValue(outcome="not_proved", tactic=None, axioms=())
        evidence = EvidenceRecord(
            evidence_id=_evidence_id(
                key_hash=key_hash,
                status=status,
                value=value,
                auxiliary_ids=tuple(record.evidence_id for record in audit_records),
            ),
            target_kind=EvidenceTargetKind.LEAN_PAIR,
            target_id=key.pair_id,
            kind=key.evidence_kind,
            status=status,
            value=value,
            method_version=key.method_version,
            config_hash=key.config_hash,
            raw_artifact=artifact,
            created_at=self.settings.created_at,
            metadata={
                "cache_key": key_hash,
                "direction": direction,
                "attempt_count": len(attempts),
                "axiom_audit_evidence_id": (
                    audit_record_by_certificate[accepted.audit.certificate_name].evidence_id
                    if accepted is not None and accepted.audit is not None
                    else ""
                ),
                "raw_artifact_sha256": artifacts[artifact],
            },
        )
        return self._persist(
            key=key,
            evidence=evidence,
            auxiliary=audit_records,
            generated_code_hash=hash_canonical(code_hashes),
            request_hashes=request_hashes,
            dependency_hash=dependency_hash,
            artifact_hashes=artifacts,
        )

    def _collect_alignment(
        self,
        key: EvidenceCacheKey,
        source: PropositionPairSource,
        representation_a: RepresentationRecord,
        representation_b: RepresentationRecord,
        alignment: ClaimAlignmentSpec | None,
        preflight: PropositionPreflight,
    ) -> EvidenceCacheEntry:
        key_hash = compute_evidence_cache_key_hash(key)
        if alignment is None:
            absent_value = _unsupported_value(EvidenceKind.CLAIM_ALIGNMENT)
            assert isinstance(absent_value, ClaimAlignmentValue)
            evidence = EvidenceRecord(
                evidence_id=_evidence_id(
                    key_hash=key_hash,
                    status=EvidenceExecutionStatus.UNSUPPORTED,
                    value=absent_value,
                ),
                target_kind=EvidenceTargetKind.LEAN_PAIR,
                target_id=key.pair_id,
                kind=EvidenceKind.CLAIM_ALIGNMENT,
                status=EvidenceExecutionStatus.UNSUPPORTED,
                value=absent_value,
                method_version=key.method_version,
                config_hash=key.config_hash,
                created_at=self.settings.created_at,
                metadata={"cache_key": key_hash, "terminal_reason": "alignment_spec_absent"},
            )
            return self._persist(
                key=key,
                evidence=evidence,
                auxiliary=(),
                generated_code_hash=None,
                request_hashes=(),
                dependency_hash=None,
                artifact_hashes={},
            )
        fingerprint_present = bool(
            representation_a.alpha_identity_fingerprint
            and representation_b.alpha_identity_fingerprint
        )
        fingerprint_equal = bool(
            fingerprint_present
            and representation_a.alpha_identity_fingerprint
            == representation_b.alpha_identity_fingerprint
        )
        attempts: list[ProofAttemptResult] = []
        if fingerprint_equal:
            directions: tuple[Direction, ...]
            if alignment.direction == "both":
                directions = ("A_to_B", "B_to_A")
            else:
                directions = (alignment.direction,)
            method = next(
                method
                for method in self._enabled_methods("binder_aligned_claim")
                if method.method_id == "exact_assumption_v1"
            )
            policy = self.configs.portfolio.config.certificate_policy
            for direction in directions:
                attempts.append(
                    run_directional_proof_attempt(
                        self.backend,
                        source=source,
                        context_id=key.context_id,
                        direction=direction,
                        method_id=method.method_id,
                        tactic_body=method.tactic_body,
                        timeout_seconds=method.timeout_seconds,
                        request_id=f"lf020-alignment-{direction}-{key_hash[:12]}",
                        allowed_axioms=policy.allowed_standard_axioms,
                        forbidden_axioms=policy.forbidden_axioms,
                    )
                )
        audits = tuple(attempt.audit for attempt in attempts if attempt.audit is not None)
        code_hashes = tuple(attempt.command.code_sha256 for attempt in attempts)
        lean_results = tuple(attempt.retry.result for attempt in attempts)
        artifact = ""
        artifacts: dict[str, str] = {}
        request_hashes: tuple[str, ...] = ()
        dependency_hash: str | None = None
        if attempts:
            artifact, artifacts, request_hashes, dependency_hash = self._artifact_from_results(
                key=key,
                job="claim_alignment",
                code_hashes=code_hashes,
                results=lean_results,
                audits=audits,
                preflight=preflight,
            )
        audit_records = tuple(
            _audit_record(
                pair_id=key.pair_id,
                audit=audit,
                method_version=f"{key.method_version}/{attempt.method_id}",
                config_hash=key.config_hash,
                detail_artifact=artifact,
                created_at=self.settings.created_at,
                cache_key_hash=key_hash,
            )
            for attempt in attempts
            if (audit := attempt.audit) is not None
        )
        if not fingerprint_present:
            status = EvidenceExecutionStatus.UNSUPPORTED
            outcome: Literal["certified", "rejected", "unsupported"] = "unsupported"
        elif not fingerprint_equal:
            status = EvidenceExecutionStatus.SUCCESS
            outcome = "rejected"
        elif attempts and all(attempt.proved for attempt in attempts):
            status = EvidenceExecutionStatus.SUCCESS
            outcome = "certified"
        elif any(attempt.policy_rejected for attempt in attempts):
            status = EvidenceExecutionStatus.ABSTAIN
            outcome = "rejected"
        elif any(
            attempt.retry.result.status
            in {
                LeanStatus.TIMEOUT,
                LeanStatus.CRASH,
                LeanStatus.SETUP_ERROR,
                LeanStatus.INTERNAL_ERROR,
            }
            for attempt in attempts
        ):
            status = _operational_status([attempt.retry.result.status for attempt in attempts])
            outcome = "rejected"
        else:
            status = EvidenceExecutionStatus.SUCCESS
            outcome = "rejected"
        alignment_value: ClaimAlignmentValue | None = None
        if status in {
            EvidenceExecutionStatus.SUCCESS,
            EvidenceExecutionStatus.UNSUPPORTED,
        }:
            alignment_value = ClaimAlignmentValue(
                alignment_version=alignment.alignment_version,
                binder_map=alignment.binder_map,
                premise_map=alignment.premise_map,
                conclusion_role_map=alignment.conclusion_role_map,
                direction=alignment.direction,
                outcome=outcome,
            )
        evidence = EvidenceRecord(
            evidence_id=_evidence_id(
                key_hash=key_hash,
                status=status,
                value=alignment_value,
                auxiliary_ids=tuple(record.evidence_id for record in audit_records),
            ),
            target_kind=EvidenceTargetKind.LEAN_PAIR,
            target_id=key.pair_id,
            kind=EvidenceKind.CLAIM_ALIGNMENT,
            status=status,
            value=alignment_value,
            method_version=key.method_version,
            config_hash=key.config_hash,
            raw_artifact=artifact or None,
            created_at=self.settings.created_at,
            metadata={
                "cache_key": key_hash,
                "template_id": alignment.template_id,
                "alpha_identity_present": fingerprint_present,
                "alpha_identity_equal": fingerprint_equal,
                "raw_artifact_sha256": artifacts.get(artifact, ""),
            },
        )
        return self._persist(
            key=key,
            evidence=evidence,
            auxiliary=audit_records,
            generated_code_hash=hash_canonical(code_hashes) if code_hashes else None,
            request_hashes=request_hashes,
            dependency_hash=dependency_hash,
            artifact_hashes=artifacts,
        )

    def _collect_counterexample(
        self,
        key: EvidenceCacheKey,
        source: PropositionPairSource,
        preflight: PropositionPreflight,
    ) -> EvidenceCacheEntry:
        key_hash = compute_evidence_cache_key_hash(key)
        portfolio_policy = self.configs.portfolio.config.certificate_policy
        attempts: list[CounterexampleAttempt] = []
        accepted: CounterexampleAttempt | None = None
        for direction in ("A_to_B", "B_to_A"):
            attempt = run_counterexample_attempt(
                self.backend,
                source=source,
                context_id=key.context_id,
                direction=direction,
                timeout_seconds=self.configs.counterexample.config.default_timeout_seconds,
                request_id_prefix=f"lf020-counter-{direction}-{key_hash[:12]}",
                allowed_axioms=portfolio_policy.allowed_standard_axioms,
                forbidden_axioms=portfolio_policy.forbidden_axioms,
            )
            attempts.append(attempt)
            if attempt.found:
                accepted = attempt
                break
        audits = tuple(attempt.audit for attempt in attempts if attempt.audit is not None)
        code_hashes = tuple(
            command.code_sha256
            for attempt in attempts
            for command in (attempt.preflight_command, attempt.command)
            if command is not None
        )
        lean_results = tuple(
            result
            for attempt in attempts
            for result in (
                attempt.preflight_retry.result,
                None if attempt.retry is None else attempt.retry.result,
            )
            if result is not None
        )
        artifact, artifacts, request_hashes, dependency_hash = self._artifact_from_results(
            key=key,
            job="counterexample",
            code_hashes=code_hashes,
            results=lean_results,
            audits=audits,
            preflight=preflight,
        )
        audit_records = tuple(
            _audit_record(
                pair_id=key.pair_id,
                audit=audit,
                method_version=f"{key.method_version}/{attempt.direction}",
                config_hash=key.config_hash,
                detail_artifact=artifact,
                created_at=self.settings.created_at,
                cache_key_hash=key_hash,
            )
            for attempt in attempts
            if (audit := attempt.audit) is not None
        )
        if accepted is not None:
            assert accepted.audit is not None
            status = EvidenceExecutionStatus.SUCCESS
            value: CounterexampleValue | None = CounterexampleValue(
                outcome="found",
                direction=accepted.direction,
                domain="closed_decidable",
                encoding="kernel_decide_v1",
                witness_artifact=artifact,
                axioms=accepted.audit.axioms,
            )
        elif any(attempt.policy_rejected for attempt in attempts):
            status = EvidenceExecutionStatus.ABSTAIN
            value = None
        elif any(
            result.status
            in {
                LeanStatus.TIMEOUT,
                LeanStatus.CRASH,
                LeanStatus.SETUP_ERROR,
                LeanStatus.INTERNAL_ERROR,
            }
            for result in lean_results
        ):
            status = _operational_status([result.status for result in lean_results])
            value = None
        elif len(attempts) == 2 and all(attempt.supported for attempt in attempts):
            status = EvidenceExecutionStatus.SUCCESS
            value = CounterexampleValue(
                outcome="not_found",
                direction="equivalence_only",
                domain="closed_decidable",
                encoding="kernel_decide_v1",
            )
        else:
            status = EvidenceExecutionStatus.UNSUPPORTED
            value = CounterexampleValue(
                outcome="unsupported",
                direction="equivalence_only",
                domain=None,
                encoding="kernel_decide_v1",
            )
        evidence = EvidenceRecord(
            evidence_id=_evidence_id(
                key_hash=key_hash,
                status=status,
                value=value,
                auxiliary_ids=tuple(record.evidence_id for record in audit_records),
            ),
            target_kind=EvidenceTargetKind.LEAN_PAIR,
            target_id=key.pair_id,
            kind=EvidenceKind.COUNTEREXAMPLE,
            status=status,
            value=value,
            method_version=key.method_version,
            config_hash=key.config_hash,
            raw_artifact=artifact,
            created_at=self.settings.created_at,
            metadata={
                "cache_key": key_hash,
                "engine": "kernel_decide_v1",
                "attempt_count": len(attempts),
                "raw_artifact_sha256": artifacts[artifact],
            },
        )
        return self._persist(
            key=key,
            evidence=evidence,
            auxiliary=audit_records,
            generated_code_hash=hash_canonical(code_hashes),
            request_hashes=request_hashes,
            dependency_hash=dependency_hash,
            artifact_hashes=artifacts,
        )

    @staticmethod
    def _finish(
        pair: PairRecord,
        records: tuple[EvidenceRecord, ...],
        *,
        cache_hits: int,
        cache_misses: int,
    ) -> PairEvidenceResult:
        by_id = {record.evidence_id: record for record in records}
        if len(by_id) != len(records):
            raise EvidencePipelineError("duplicate evidence IDs in pair result")
        linked = PairRecord.model_validate(
            {
                **pair.model_dump(mode="python"),
                "evidence_ids": tuple(sorted(set(pair.evidence_ids) | set(by_id))),
            }
        )
        return PairEvidenceResult(
            pair=linked,
            evidence=tuple(by_id[key] for key in sorted(by_id)),
            cache_hits=cache_hits,
            cache_misses=cache_misses,
        )
