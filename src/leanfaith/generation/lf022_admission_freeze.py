"""Offline construction of one reviewed LF-022 proposer execution admission.

The public-pool materializer remains non-executable.  This module bridges one
exact, one-source diagnostic scaffold to the existing execution verifier.  It
does not resolve credentials, contact a provider, create labels, or authorize
anything other than provisional public ``G_open`` collection.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from leanfaith.config.code_bundle import validate_code_bundle
from leanfaith.config.hashing import canonical_json_bytes, hash_file
from leanfaith.config.models import StrictModel
from leanfaith.generation.lf022_execution import (
    LF022ExecutionArtifacts,
    LF022ExecutionError,
    LF022GOpenExecutionAdmission,
    LF022RCPDecodingContract,
    LF022RCPRetryPolicy,
    LF022RCPRouteBinding,
    load_lf022_execution_task_inputs,
    make_lf022_g_open_execution_admission,
    verify_lf022_execution_admission,
)
from leanfaith.generation.lf022_production import (
    LF022ArtifactBinding,
    LF022ProductionPlanManifest,
    LF022ProviderCatalogSnapshot,
)
from leanfaith.generation.lf022_public_pool import LF022PublicPoolAudit
from leanfaith.schemas.manifest import collect_code_state

LF022SupportedProposerFamily = Literal["moonshot_kimi_k2", "qwen3", "glm5"]

_RAW_CATALOG_PATH = (
    "reports/generation/lf022_rcp_public_smoke_v3/catalog/"
    "0f98154b24af983bcdb22e6248bf98cbe5456fa9cd78932d280262777877dd66.json"
)
_NORMALIZED_CATALOG_PATH = "configs/generation/lf022_rcp_catalog_snapshot_v1.json"
_PORTFOLIO_PATH = "configs/generation/rcp_provider_portfolio_v2.yaml"
_EVIDENCE_PATH = "reports/generation/lf022_rcp_public_smoke_qualification_v1.json"
_PROMPT_PATH = "prompts/proposers/lean_variant_v1.txt"


class LF022AdmissionFreezeError(ValueError):
    """A diagnostic scaffold cannot become a reviewed execution admission."""


@dataclass(frozen=True, slots=True)
class FrozenLF022ExecutionAdmission:
    """One immutable, replay-verified execution admission."""

    admission: LF022GOpenExecutionAdmission
    admission_path: Path


@dataclass(frozen=True, slots=True)
class _RouteSpec:
    model_id: str
    canonical_family: str
    contract_id: str
    contract_path: str
    execution_scope: Literal[
        "public_provisional_g_open",
        "one_item_proposer_qualification_only",
    ]
    decoding: dict[str, object]


_ROUTES: dict[LF022SupportedProposerFamily, _RouteSpec] = {
    "moonshot_kimi_k2": _RouteSpec(
        model_id="moonshotai/Kimi-K2.7-Code",
        canonical_family="moonshotai/kimi-k2",
        contract_id="kimi_k2_7_public_smoke_v3",
        contract_path="configs/generation/lf022_rcp_public_smoke_v3.yaml",
        execution_scope="public_provisional_g_open",
        decoding={
            "temperature": 1.0,
            "top_p": 0.95,
            "top_k": None,
            "min_p": None,
            "presence_penalty": None,
            "repetition_penalty": None,
            "max_tokens": 16_384,
            "seed": 42,
            "stream": False,
            "thinking_mode": "forced_thinking",
            "reasoning_effort": "high",
            "chat_template_enable_thinking": True,
            "chat_template_thinking": None,
            "thinking_fields_forbidden": False,
        },
    ),
    "qwen3": _RouteSpec(
        model_id="Qwen/Qwen3.5-397B-A17B",
        canonical_family="qwen/qwen3",
        contract_id="qwen3_5_proposer_qualification_v1",
        contract_path="configs/generation/lf022_qwen3_5_proposer_qualification_v1.yaml",
        execution_scope="one_item_proposer_qualification_only",
        decoding={
            "temperature": 0.6,
            "top_p": 0.95,
            "top_k": 20,
            "min_p": 0.0,
            "presence_penalty": 0.0,
            "repetition_penalty": 1.0,
            "max_tokens": 4_096,
            "seed": 42,
            "stream": False,
            "thinking_mode": "enabled",
            "reasoning_effort": "high",
            "chat_template_enable_thinking": True,
            "chat_template_thinking": None,
            "thinking_fields_forbidden": False,
        },
    ),
    "glm5": _RouteSpec(
        model_id="zai-org/GLM-5.2",
        canonical_family="zai-org/glm-5.2",
        contract_id="glm5_2_proposer_qualification_v1",
        contract_path="configs/generation/lf022_glm5_2_proposer_qualification_v1.yaml",
        execution_scope="one_item_proposer_qualification_only",
        decoding={
            "temperature": 0.0,
            "top_p": 1.0,
            "top_k": None,
            "min_p": None,
            "presence_penalty": None,
            "repetition_penalty": None,
            "max_tokens": 8_192,
            "seed": 42,
            "stream": False,
            "thinking_mode": "enabled",
            "reasoning_effort": "high",
            "chat_template_enable_thinking": True,
            "chat_template_thinking": None,
            "thinking_fields_forbidden": False,
        },
    ),
}

_RECOVERY_ROUTES: dict[Literal["qwen3", "glm5"], _RouteSpec] = {
    "qwen3": _RouteSpec(
        model_id="Qwen/Qwen3.5-397B-A17B",
        canonical_family="qwen/qwen3",
        contract_id="qwen3_5_proposer_qualification_v2",
        contract_path="configs/generation/lf022_qwen3_5_proposer_qualification_v2.yaml",
        execution_scope="one_item_proposer_qualification_only",
        decoding={
            "temperature": 0.6,
            "top_p": 0.95,
            "top_k": 20,
            "min_p": 0.0,
            "presence_penalty": 0.0,
            "repetition_penalty": 1.0,
            "max_tokens": 16_384,
            "seed": 42,
            "stream": False,
            "thinking_mode": "enabled",
            "reasoning_effort": "high",
            "chat_template_enable_thinking": True,
            "chat_template_thinking": None,
            "thinking_fields_forbidden": False,
        },
    ),
    "glm5": _RouteSpec(
        model_id="zai-org/GLM-5.2",
        canonical_family="zai-org/glm-5.2",
        contract_id="glm5_2_proposer_qualification_v2",
        contract_path="configs/generation/lf022_glm5_2_proposer_qualification_v2.yaml",
        execution_scope="one_item_proposer_qualification_only",
        decoding={
            "temperature": 0.0,
            "top_p": 1.0,
            "top_k": None,
            "min_p": None,
            "presence_penalty": None,
            "repetition_penalty": None,
            "max_tokens": 8_192,
            "seed": 42,
            "stream": False,
            "thinking_mode": "enabled",
            "reasoning_effort": "high",
            "chat_template_enable_thinking": True,
            "chat_template_thinking": None,
            "thinking_fields_forbidden": False,
        },
    ),
}

_QUALIFIED_PRODUCTION_ROUTES: dict[Literal["qwen3", "glm5"], _RouteSpec] = {
    family: _RouteSpec(
        model_id=spec.model_id,
        canonical_family=spec.canonical_family,
        contract_id=spec.contract_id,
        contract_path=spec.contract_path,
        execution_scope="public_provisional_g_open",
        decoding=spec.decoding,
    )
    for family, spec in _RECOVERY_ROUTES.items()
}


def _repo_file_binding(
    repo_root: Path,
    path: Path | str,
    *,
    label: str,
) -> LF022ArtifactBinding:
    root = repo_root.resolve(strict=True)
    candidate = Path(path)
    candidate = candidate if candidate.is_absolute() else root / candidate
    try:
        resolved = candidate.resolve(strict=True)
        relative = resolved.relative_to(root)
    except (LF022ExecutionError, OSError, ValueError) as exc:
        raise LF022AdmissionFreezeError(f"{label} must be a repository-local regular file") from exc
    if candidate.is_symlink() or not resolved.is_file():
        raise LF022AdmissionFreezeError(f"{label} must be a repository-local regular file")
    return LF022ArtifactBinding(
        path=PurePosixPath(relative.as_posix()).as_posix(),
        sha256=hash_file(resolved),
    )


def _load_canonical[RecordT: StrictModel](
    *,
    repo_root: Path,
    binding: LF022ArtifactBinding,
    model: type[RecordT],
    label: str,
) -> RecordT:
    path = repo_root / binding.path
    raw = path.read_bytes()
    try:
        record = model.model_validate_json(raw)
    except ValueError as exc:
        raise LF022AdmissionFreezeError(f"invalid {label}: {exc}") from exc
    expected = canonical_json_bytes(record.model_dump(mode="json"))
    if raw not in {expected, expected + b"\n"}:
        raise LF022AdmissionFreezeError(f"{label} is not canonical JSON")
    return record


def _output_path(repo_root: Path, path: Path) -> Path:
    root = repo_root.resolve(strict=True)
    candidate = path if path.is_absolute() else root / path
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise LF022AdmissionFreezeError(
            "execution admission output must remain inside the repository"
        ) from exc
    if not relative.parts or "." in relative.parts or ".." in relative.parts:
        raise LF022AdmissionFreezeError(
            "execution admission output must use a normalized repository path"
        )
    current = root
    for part in relative.parts[:-1]:
        current /= part
        if current.is_symlink():
            raise LF022AdmissionFreezeError("execution admission output cannot traverse symlinks")
        if current.exists() and not current.is_dir():
            raise LF022AdmissionFreezeError("execution admission output parent is not a directory")
        current.mkdir(exist_ok=True)
    return candidate


def _write_immutable(path: Path, payload: bytes) -> None:
    if path.is_symlink():
        raise LF022AdmissionFreezeError("execution admission output cannot be a symlink")
    if path.exists():
        if not path.is_file() or path.read_bytes() != payload:
            raise LF022AdmissionFreezeError("existing execution admission output differs")
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".partial",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                raise LF022AdmissionFreezeError(
                    "concurrent execution admission output differs"
                ) from None
    finally:
        temporary.unlink(missing_ok=True)


def _freeze_lf022_execution_admission(
    *,
    repo_root: Path,
    public_pool_audit_path: Path,
    proposer_family_id: LF022SupportedProposerFamily,
    code_bundle_path: Path,
    output_path: Path,
    provider_catalog_raw_path: Path | None = None,
    qualification_supersession_path: Path | None = None,
    proposer_production_eligibility_path: Path | None = None,
    expected_profile: Literal[
        "diagnostic_scaffold",
        "scientific_production_scaffold",
    ],
) -> FrozenLF022ExecutionAdmission:
    """Create and exact-replay one profile-specific execution admission offline."""

    root = repo_root.resolve(strict=True)
    if (
        expected_profile == "scientific_production_scaffold"
        and qualification_supersession_path is not None
    ):
        raise LF022AdmissionFreezeError(
            "scientific admission cannot use a qualification supersession"
        )
    qualified_scientific = (
        expected_profile == "scientific_production_scaffold"
        and proposer_family_id in {"qwen3", "glm5"}
    )
    if qualified_scientific != (proposer_production_eligibility_path is not None):
        raise LF022AdmissionFreezeError(
            "scientific Qwen/GLM admission requires exactly one production eligibility; "
            "diagnostic and Kimi admissions forbid it"
        )
    if qualified_scientific:
        spec = _QUALIFIED_PRODUCTION_ROUTES[cast(Literal["qwen3", "glm5"], proposer_family_id)]
    elif qualification_supersession_path is not None:
        if proposer_family_id == "moonshot_kimi_k2":
            raise LF022AdmissionFreezeError("Kimi cannot use a qualification supersession")
        spec = _RECOVERY_ROUTES[proposer_family_id]
    else:
        spec = _ROUTES[proposer_family_id]
    audit_binding = _repo_file_binding(
        root,
        public_pool_audit_path,
        label="public-pool audit",
    )
    audit = _load_canonical(
        repo_root=root,
        binding=audit_binding,
        model=LF022PublicPoolAudit,
        label="public-pool audit",
    )
    if (
        audit.profile != expected_profile
        or not audit.public_sources_only
        or not audit.private_sft_classic_forbidden
        or audit.network_execution_authorized
        or audit.semantic_labels_created
    ):
        raise LF022AdmissionFreezeError(
            "execution admission requires the exact requested public-only pool profile"
        )
    if expected_profile == "diagnostic_scaffold" and (
        audit.requested_count != 1
        or audit.selected_count != 1
        or audit.selected_unique_ancestry_count != 1
    ):
        raise LF022AdmissionFreezeError(
            "diagnostic execution admission requires one exact public source"
        )
    if expected_profile == "scientific_production_scaffold" and (
        audit.requested_count != audit.selected_count
        or audit.selected_unique_ancestry_count != audit.selected_count
    ):
        raise LF022AdmissionFreezeError(
            "scientific execution admission requires every selected source ancestry to be unique"
        )
    plan_binding = audit.outputs.production_plan
    plan = _load_canonical(
        repo_root=root,
        binding=plan_binding,
        model=LF022ProductionPlanManifest,
        label="diagnostic allocation plan",
    )
    if (
        plan.profile != expected_profile
        or plan.unique_source_count != audit.selected_count
        or len(plan.tasks) != 2 * plan.unique_source_count
        or plan.network_execution_authorized
        or plan.semantic_labels_created
        or plan.execution_bindings_present
    ):
        raise LF022AdmissionFreezeError(
            "allocation plan profile, source count, or non-executable state differs "
            "from the public-pool audit"
        )
    if expected_profile == "diagnostic_scaffold" and (
        {task.distribution for task in plan.tasks} != {"G_sci", "G_open"}
        or {task.proposer_family_id for task in plan.tasks} != {proposer_family_id}
    ):
        raise LF022AdmissionFreezeError(
            "diagnostic plan must contain exactly one source and two tasks "
            "assigned to the selected proposer"
        )
    if expected_profile == "scientific_production_scaffold" and not any(
        task.distribution == "G_open" and task.proposer_family_id == proposer_family_id
        for task in plan.tasks
    ):
        raise LF022AdmissionFreezeError(
            "scientific plan contains no G_open task for the reviewed proposer route"
        )

    code_tree_hash = collect_code_state(root).code_tree_hash
    if code_tree_hash is None:
        raise LF022AdmissionFreezeError("current repository code-tree hash is unavailable")
    if audit.schema_version == 2:
        if expected_profile != "diagnostic_scaffold":
            raise LF022AdmissionFreezeError(
                "derived public-pool audits are restricted to diagnostic admission"
            )
        if proposer_family_id == "moonshot_kimi_k2":
            raise LF022AdmissionFreezeError(
                "derived diagnostic subpools support only qwen3 or glm5"
            )
        from leanfaith.generation.lf022_diagnostic_subpool import (
            LF022DiagnosticSubpoolError,
            verify_lf022_diagnostic_subpool,
        )

        try:
            verify_lf022_diagnostic_subpool(
                repo_root=root,
                audit=audit,
                expected_proposer_family_id=proposer_family_id,
                expected_code_tree_hash=code_tree_hash,
            )
        except LF022DiagnosticSubpoolError as exc:
            raise LF022AdmissionFreezeError(
                f"derived diagnostic subpool exact replay rejected: {exc}"
            ) from exc
    code_bundle_binding = _repo_file_binding(
        root,
        code_bundle_path,
        label="code bundle",
    )
    try:
        observed_bundle_sha = validate_code_bundle(
            root / code_bundle_binding.path,
            code_tree_hash,
        )
    except (OSError, ValueError) as exc:
        raise LF022AdmissionFreezeError(f"invalid code bundle: {exc}") from exc
    if observed_bundle_sha != code_bundle_binding.sha256:
        raise LF022AdmissionFreezeError("code-bundle SHA-256 changed during validation")

    raw_catalog_binding = _repo_file_binding(
        root,
        provider_catalog_raw_path or Path(_RAW_CATALOG_PATH),
        label="raw provider catalog",
    )
    normalized_catalog_binding = _repo_file_binding(
        root,
        _NORMALIZED_CATALOG_PATH,
        label="normalized provider catalog",
    )
    normalized_catalog = _load_canonical(
        repo_root=root,
        binding=normalized_catalog_binding,
        model=LF022ProviderCatalogSnapshot,
        label="normalized provider catalog",
    )
    deployments = tuple(
        deployment
        for deployment in normalized_catalog.deployments
        if deployment.model_id == spec.model_id
    )
    if len(deployments) != 1 or deployments[0].deployment_id != spec.model_id:
        raise LF022AdmissionFreezeError(
            "selected proposer lacks one exact normalized provider deployment"
        )

    decoding = LF022RCPDecodingContract.model_validate(
        {
            "schema_version": 1,
            "contract_id": spec.contract_id,
            **spec.decoding,
        }
    )
    route = LF022RCPRouteBinding(
        provider_id="epfl_rcp",
        model_id=spec.model_id,
        deployment_id=deployments[0].deployment_id,
        proposer_family_id=proposer_family_id,
        canonical_family=spec.canonical_family,
        catalog_snapshot_id=normalized_catalog.snapshot_id,
        route_snapshot_revision=f"rcp-catalog-sha256:{raw_catalog_binding.sha256}",
        underlying_checkpoint_revision_status="provider_not_disclosed",
        execution_scope=spec.execution_scope,
        decoding=decoding,
    )
    retry_policy = LF022RCPRetryPolicy(
        max_attempts=3,
        request_timeout_seconds=3_600,
        base_delay_seconds=1.0,
        maximum_delay_seconds=60.0,
        retryable_http_statuses=(408, 409, 425, 429, 500, 502, 503, 504),
    )
    reviewed_route_contract_binding = _repo_file_binding(
        root,
        spec.contract_path,
        label="reviewed route contract",
    )
    supersession_binding = (
        _repo_file_binding(
            root,
            qualification_supersession_path,
            label="qualification supersession",
        )
        if qualification_supersession_path is not None
        else None
    )
    eligibility_binding = (
        _repo_file_binding(
            root,
            proposer_production_eligibility_path,
            label="proposer production eligibility",
        )
        if proposer_production_eligibility_path is not None
        else None
    )
    if supersession_binding is not None:
        from leanfaith.generation.lf022_route_qualification import (
            LF022RouteQualificationError,
            verify_lf022_qualification_supersession,
        )

        try:
            supersession = verify_lf022_qualification_supersession(
                repo_root=root,
                supersession_binding=supersession_binding,
            )
        except LF022RouteQualificationError as exc:
            raise LF022AdmissionFreezeError(
                f"qualification supersession exact replay rejected: {exc}"
            ) from exc
        if (
            supersession.proposer_family_id != proposer_family_id
            or supersession.model_id != spec.model_id
            or supersession.next_decoding_contract_id != spec.contract_id
        ):
            raise LF022AdmissionFreezeError(
                "qualification supersession belongs to a different recovery route"
            )
    if eligibility_binding is not None:
        from leanfaith.generation.lf022_route_qualification import (
            LF022RouteQualificationError,
            verify_lf022_proposer_production_eligibility,
        )

        try:
            eligibility = verify_lf022_proposer_production_eligibility(
                repo_root=root,
                eligibility_binding=eligibility_binding,
            )
        except LF022RouteQualificationError as exc:
            raise LF022AdmissionFreezeError(
                f"proposer production eligibility exact replay rejected: {exc}"
            ) from exc
        if (
            eligibility.proposer_family_id != proposer_family_id
            or eligibility.model_id != spec.model_id
            or eligibility.decoding_contract_id != spec.contract_id
            or eligibility.qualification_contract != reviewed_route_contract_binding
            or eligibility.family_matrix != plan.artifacts.family_matrix
            or eligibility.family_matrix_id != plan.family_matrix_id
        ):
            raise LF022AdmissionFreezeError(
                "proposer production eligibility belongs to a different v2 route or matrix"
            )
    artifacts = LF022ExecutionArtifacts(
        public_pool_audit=audit_binding,
        allocation_plan=plan_binding,
        provider_catalog_raw=raw_catalog_binding,
        provider_catalog_normalized=normalized_catalog_binding,
        reviewed_route_portfolio=_repo_file_binding(
            root,
            _PORTFOLIO_PATH,
            label="reviewed route portfolio",
        ),
        reviewed_route_contract=reviewed_route_contract_binding,
        reviewed_route_evidence=_repo_file_binding(
            root,
            _EVIDENCE_PATH,
            label="reviewed route evidence",
        ),
        prompt_template=_repo_file_binding(
            root,
            _PROMPT_PATH,
            label="reviewed proposer prompt",
        ),
        code_bundle=code_bundle_binding,
        proposer_production_eligibility=eligibility_binding,
        qualification_supersession=supersession_binding,
    )
    admission = make_lf022_g_open_execution_admission(
        public_pool_audit_id=audit.audit_id,
        allocation_plan_id=plan.manifest_id,
        artifacts=artifacts,
        route=route,
        retry_policy=retry_policy,
        code_tree_hash=code_tree_hash,
    )
    try:
        verified = verify_lf022_execution_admission(
            repo_root=root,
            admission=admission,
        )
    except (OSError, ValueError) as exc:
        raise LF022AdmissionFreezeError(
            f"execution admission exact replay rejected: {exc}"
        ) from exc
    if (
        verified.admission_id != admission.admission_id
        or verified.audit.audit_id != audit.audit_id
        or verified.plan.manifest_id != plan.manifest_id
    ):
        raise LF022AdmissionFreezeError(
            "execution admission replay returned different bound artifacts"
        )
    if expected_profile == "scientific_production_scaffold":
        from leanfaith.generation.lf022_batch import (
            LF022BatchError,
            audit_lf022_g_open_source_eligibility,
        )

        try:
            eligible_count = audit_lf022_g_open_source_eligibility(
                repo_root=root,
                admission=admission,
                verified=verified,
                inputs=load_lf022_execution_task_inputs(
                    repo_root=root,
                    verified=verified,
                ),
            )
        except (LF022BatchError, LF022ExecutionError) as exc:
            raise LF022AdmissionFreezeError(
                f"scientific proposer source eligibility audit rejected: {exc}"
            ) from exc
        expected_proposer_g_open = sum(
            task.distribution == "G_open" and task.proposer_family_id == proposer_family_id
            for task in plan.tasks
        )
        if eligible_count != expected_proposer_g_open:
            raise LF022AdmissionFreezeError(
                "scientific proposer source eligibility count differs from its allocation plan"
            )

    destination = _output_path(root, output_path)
    payload = canonical_json_bytes(admission.model_dump(mode="json")) + b"\n"
    _write_immutable(destination, payload)
    persisted_binding = _repo_file_binding(
        root,
        destination,
        label="persisted execution admission",
    )
    persisted = _load_canonical(
        repo_root=root,
        binding=persisted_binding,
        model=LF022GOpenExecutionAdmission,
        label="persisted execution admission",
    )
    if persisted != admission:
        raise LF022AdmissionFreezeError(
            "persisted execution admission differs from constructed admission"
        )
    try:
        verify_lf022_execution_admission(repo_root=root, admission=persisted)
    except (LF022ExecutionError, OSError, ValueError) as exc:
        raise LF022AdmissionFreezeError(
            f"persisted execution admission replay rejected: {exc}"
        ) from exc
    return FrozenLF022ExecutionAdmission(
        admission=persisted,
        admission_path=destination,
    )


def freeze_lf022_diagnostic_execution_admission(
    *,
    repo_root: Path,
    public_pool_audit_path: Path,
    proposer_family_id: LF022SupportedProposerFamily,
    code_bundle_path: Path,
    output_path: Path,
    provider_catalog_raw_path: Path | None = None,
    qualification_supersession_path: Path | None = None,
) -> FrozenLF022ExecutionAdmission:
    """Create and exact-replay one family-specific diagnostic admission offline."""

    return _freeze_lf022_execution_admission(
        repo_root=repo_root,
        public_pool_audit_path=public_pool_audit_path,
        proposer_family_id=proposer_family_id,
        code_bundle_path=code_bundle_path,
        output_path=output_path,
        provider_catalog_raw_path=provider_catalog_raw_path,
        qualification_supersession_path=qualification_supersession_path,
        proposer_production_eligibility_path=None,
        expected_profile="diagnostic_scaffold",
    )


def freeze_lf022_scientific_kimi_execution_admission(
    *,
    repo_root: Path,
    public_pool_audit_path: Path,
    code_bundle_path: Path,
    output_path: Path,
    provider_catalog_raw_path: Path | None = None,
) -> FrozenLF022ExecutionAdmission:
    """Admit the reviewed Kimi route over one exact scientific public pool."""

    return _freeze_lf022_execution_admission(
        repo_root=repo_root,
        public_pool_audit_path=public_pool_audit_path,
        proposer_family_id="moonshot_kimi_k2",
        code_bundle_path=code_bundle_path,
        output_path=output_path,
        provider_catalog_raw_path=provider_catalog_raw_path,
        qualification_supersession_path=None,
        proposer_production_eligibility_path=None,
        expected_profile="scientific_production_scaffold",
    )


def freeze_lf022_scientific_qualified_execution_admission(
    *,
    repo_root: Path,
    public_pool_audit_path: Path,
    proposer_family_id: Literal["qwen3", "glm5"],
    proposer_production_eligibility_path: Path,
    code_bundle_path: Path,
    output_path: Path,
    provider_catalog_raw_path: Path | None = None,
) -> FrozenLF022ExecutionAdmission:
    """Admit one replay-qualified Qwen/GLM route over the scientific public pool."""

    return _freeze_lf022_execution_admission(
        repo_root=repo_root,
        public_pool_audit_path=public_pool_audit_path,
        proposer_family_id=proposer_family_id,
        code_bundle_path=code_bundle_path,
        output_path=output_path,
        provider_catalog_raw_path=provider_catalog_raw_path,
        qualification_supersession_path=None,
        proposer_production_eligibility_path=proposer_production_eligibility_path,
        expected_profile="scientific_production_scaffold",
    )


__all__ = [
    "FrozenLF022ExecutionAdmission",
    "LF022AdmissionFreezeError",
    "LF022SupportedProposerFamily",
    "freeze_lf022_diagnostic_execution_admission",
    "freeze_lf022_scientific_kimi_execution_admission",
    "freeze_lf022_scientific_qualified_execution_admission",
]
