"""Offline, exact replay verification for the LF-021 RCP v2 qualification.

This module performs no network or provider operation.  It validates every
persisted record with its owning schema, reconstructs the deterministic
request/invocation/terminal lineage, checks all cross-file hashes, scans the
qualification artifacts for runtime credential material, and binds the
separate LeanInteract elaboration diagnostic.

The resulting report is intentionally operational only.  A successful parse
or elaboration does not assess mathematical faithfulness and creates no label
or Gate credit.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.models import StrictModel
from leanfaith.generation import rcp_qualification_v1 as engine
from leanfaith.generation.prompts import parse_direct_autoformalization_output
from leanfaith.generation.providers import ProviderIdentity, ProviderRawResponse, ProviderRequest
from leanfaith.generation.rcp_qualification_v2 import (
    RCPQualificationManifestV2,
    RCPQualificationPreflightV2,
    load_rcp_qualification_v2,
)

_HEX64 = r"^[0-9a-f]{64}$"


class RCPQualificationVerifyV2Error(RuntimeError):
    """An offline qualification artifact failed exact replay verification."""


class RCPSecretScanV2(StrictModel):
    credential_environment_present: Literal[True] = True
    exact_credential_occurrences: Literal[0] = 0
    bearer_header_occurrences: Literal[0] = 0
    authorization_field_occurrences: Literal[0] = 0
    rcp_api_key_name_occurrences: Literal[0] = 0
    files_scanned: int = Field(ge=1)


class RCPLeanOperationalValidationV2(StrictModel):
    artifact: str
    artifact_sha256: str = Field(pattern=_HEX64)
    request_hash: str = Field(pattern=_HEX64)
    method_version: str
    declaration_name: Literal["leanfaith_rcp_qualification_v1"]
    parsed_statement_sha256: str = Field(pattern=_HEX64)
    operational_status: Literal["valid_with_sorry"]
    declaration_count: Literal[1] = 1
    sorry_count: Literal[1] = 1
    error_absent: Literal[True] = True
    semantic_faithfulness_assessed: Literal[False] = False


class RCPQualificationVerificationV2(StrictModel):
    schema_version: Literal[1] = 1
    verification_id: str = Field(pattern=r"^rcp_qualification_verification_v2:[0-9a-f]{64}$")
    artifact_kind: Literal["lf021_rcp_kimi_qualification_verification_v2"]
    verification_mode: Literal["exact_offline_verify_only"]
    provider_calls_performed: Literal[0] = 0
    network_requests_performed: Literal[0] = 0
    config_hash: str = Field(pattern=_HEX64)
    config_file_sha256: str = Field(pattern=_HEX64)
    bound_artifact_hashes: dict[str, str]
    invocation_id: str = Field(pattern=r"^rcp_qualification_invocation:[0-9a-f]{64}$")
    manifest_id: str = Field(pattern=r"^rcp_qualification_manifest_v2:[0-9a-f]{64}$")
    terminal_id: str = Field(pattern=r"^rcp_qualification_terminal:[0-9a-f]{64}$")
    terminal_status: Literal["raw_collected"]
    model_id: Literal["moonshotai/Kimi-K2.7-Code"]
    parsed_statement: str
    parsed_statement_sha256: str = Field(pattern=_HEX64)
    artifact_inventory: dict[str, str]
    artifact_inventory_sha256: str = Field(pattern=_HEX64)
    secret_scan: RCPSecretScanV2
    lean_operational_validation: RCPLeanOperationalValidationV2
    reference_transmission_performed: Literal[False] = False
    private_source_transmission_performed: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    semantic_faithfulness_assessed: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False
    gate_closed: Literal[False] = False

    def id_payload(self) -> dict[str, object]:
        return {
            key: value
            for key, value in self.model_dump(mode="json").items()
            if key != "verification_id"
        }

    @model_validator(mode="after")
    def _identity(self) -> RCPQualificationVerificationV2:
        if self.artifact_inventory_sha256 != hash_canonical(self.artifact_inventory):
            raise ValueError("verification inventory hash differs")
        expected = "rcp_qualification_verification_v2:" + hash_canonical(
            {"schema": "lf021_rcp_qualification_verification_v2", **self.id_payload()}
        )
        if self.verification_id != expected:
            raise ValueError("qualification verification ID differs")
        return self


@dataclass(frozen=True, slots=True)
class RCPQualificationVerificationRunV2:
    report: RCPQualificationVerificationV2
    report_path: Path
    report_sha256: str


def _repo_relative(path: Path, repo_root: Path) -> str:
    root = repo_root.resolve()
    if path.is_symlink() or not path.is_file():
        raise RCPQualificationVerifyV2Error(f"unsafe or missing artifact: {path}")
    try:
        return str(path.resolve().relative_to(root))
    except ValueError as exc:
        raise RCPQualificationVerifyV2Error(f"artifact escapes repository: {path}") from exc


def _load_json_model(path: Path, model: type[StrictModel]) -> StrictModel:
    try:
        return model.model_validate_json(path.read_bytes())
    except ValueError as exc:
        raise RCPQualificationVerifyV2Error(f"{path} fails {model.__name__}: {exc}") from exc


def _require(condition: bool, detail: str) -> None:
    if not condition:
        raise RCPQualificationVerifyV2Error(detail)


def _persist_immutable(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise RCPQualificationVerifyV2Error(f"immutable verification report conflict: {path}")
        return hash_file(path)
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
                raise RCPQualificationVerifyV2Error(
                    f"concurrent verification report conflict: {path}"
                ) from None
        return hash_file(path)
    finally:
        temporary.unlink(missing_ok=True)


def _scan_secrets(paths: tuple[Path, ...], *, credential: str) -> RCPSecretScanV2:
    _require(bool(credential), "RCP_API_KEY is required for exact credential scanning")
    exact = credential.encode("utf-8")
    exact_count = 0
    bearer_count = 0
    authorization_count = 0
    env_name_count = 0
    for path in paths:
        data = path.read_bytes()
        lowered = data.lower()
        exact_count += data.count(exact)
        bearer_count += lowered.count(b"bearer ")
        authorization_count += lowered.count(b'"authorization"')
        env_name_count += data.count(b"RCP_API_KEY")
    _require(exact_count == 0, "runtime RCP credential occurs in persisted artifacts")
    _require(bearer_count == 0, "Bearer header material occurs in persisted artifacts")
    _require(
        authorization_count == 0,
        "authorization field occurs in persisted artifacts",
    )
    _require(env_name_count == 0, "RCP_API_KEY environment name occurs in run artifacts")
    return RCPSecretScanV2(
        files_scanned=len(paths),
        exact_credential_occurrences=0,
        bearer_header_occurrences=0,
        authorization_field_occurrences=0,
        rcp_api_key_name_occurrences=0,
    )


def _validate_lean_raw(
    path: Path,
    *,
    repo_root: Path,
    parsed_statement: str,
    parsed_statement_sha256: str,
) -> RCPLeanOperationalValidationV2:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RCPQualificationVerifyV2Error("Lean diagnostic is not valid JSON") from exc
    _require(isinstance(document, dict), "Lean diagnostic root is not an object")
    request = document.get("request")
    response = document.get("response")
    _require(isinstance(request, dict), "Lean diagnostic lacks request")
    _require(isinstance(response, dict), "Lean diagnostic lacks response")
    request_hash = document.get("request_hash")
    _require(isinstance(request_hash, str), "Lean diagnostic lacks request hash")
    _require(document.get("error") is None, "Lean diagnostic carries a backend error")
    _require(request.get("allow_sorry") is True, "Lean diagnostic did not explicitly allow sorry")
    expected_code = f"import Mathlib\n{parsed_statement} := by sorry"
    _require(
        request.get("code") == expected_code,
        "Lean diagnostic code differs from parsed output",
    )
    declarations = response.get("declarations")
    sorries = response.get("sorries")
    _require(isinstance(declarations, list), "Lean diagnostic declarations are absent")
    _require(len(declarations) == 1, "Lean diagnostic declaration count differs")
    declaration = declarations[0]
    _require(isinstance(declaration, dict), "Lean diagnostic declaration is malformed")
    _require(
        declaration.get("full_name") == "leanfaith_rcp_qualification_v1",
        "Lean diagnostic declaration name differs",
    )
    _require(isinstance(sorries, list) and len(sorries) == 1, "Lean sorry count differs")
    return RCPLeanOperationalValidationV2(
        artifact=_repo_relative(path, repo_root),
        artifact_sha256=hash_file(path),
        request_hash=request_hash,
        method_version=str(document.get("method_version")),
        declaration_name="leanfaith_rcp_qualification_v1",
        parsed_statement_sha256=parsed_statement_sha256,
        operational_status="valid_with_sorry",
        declaration_count=1,
        sorry_count=1,
        error_absent=True,
        semantic_faithfulness_assessed=False,
    )


def verify_rcp_qualification_v2(
    *,
    repo_root: Path,
    config_path: Path,
    output_directory: Path,
    lean_raw_path: Path,
    report_path: Path,
    credential: str,
) -> RCPQualificationVerificationRunV2:
    """Verify one persisted v2 run without network or provider execution."""

    root = repo_root.resolve()
    loaded = load_rcp_qualification_v2(config_path, repo_root=root)
    output = output_directory.resolve()
    try:
        output.relative_to(root)
    except ValueError as exc:
        raise RCPQualificationVerifyV2Error("qualification output escapes repository") from exc
    _require(output.is_dir() and not output.is_symlink(), "qualification output is unsafe")

    manifest_path = output / "qualification_manifest_v2.json"
    catalog_path = output / "catalog_observation.json"
    audit_path = output / "reference_blind_audit.json"
    invocation_path = output / "invocation.json"
    terminal_path = output / "terminal.json"

    manifest = _load_json_model(manifest_path, RCPQualificationManifestV2)
    catalog = _load_json_model(catalog_path, engine.RCPModelCatalogObservation)
    audit = _load_json_model(audit_path, engine.RCPReferenceBlindAudit)
    invocation = _load_json_model(invocation_path, engine.RCPQualificationInvocation)
    terminal = _load_json_model(terminal_path, engine.RCPQualificationTerminal)
    assert isinstance(manifest, RCPQualificationManifestV2)
    assert isinstance(catalog, engine.RCPModelCatalogObservation)
    assert isinstance(audit, engine.RCPReferenceBlindAudit)
    assert isinstance(invocation, engine.RCPQualificationInvocation)
    assert isinstance(terminal, engine.RCPQualificationTerminal)

    _require(
        manifest.config_hash == loaded.loaded_config.config_hash,
        "manifest config hash differs",
    )
    _require(
        manifest.bound_artifact_hashes == loaded.bound_artifact_hashes,
        "manifest bound artifact inventory differs",
    )
    _require(
        manifest.output_directory == str(output.relative_to(root)),
        "manifest output directory differs",
    )
    _require(manifest.invocation_sha256 == hash_file(invocation_path), "invocation hash differs")
    _require(
        manifest.reference_blind_audit_sha256 == hash_file(audit_path),
        "reference-blind audit hash differs",
    )
    _require(manifest.terminal_sha256 == hash_file(terminal_path), "terminal hash differs")
    _require(manifest.catalog_observation_id == catalog.observation_id, "catalog ID differs")
    _require(
        manifest.catalog_raw_response_sha256 == catalog.raw_response_sha256,
        "catalog raw-response hash differs",
    )
    _require(
        invocation.catalog_observation_id == catalog.observation_id,
        "invocation catalog differs",
    )
    _require(terminal.invocation_id == invocation.invocation_id, "terminal invocation differs")
    _require(manifest.terminal_status.value == terminal.status.value, "terminal status differs")
    _require(manifest.model_id == invocation.model_id == terminal.model_id, "model IDs differ")
    _require(
        audit.model_dump(mode="json")
        == loaded.engine_loaded.reference_blind_audit.model_dump(mode="json"),
        "persisted reference-blind audit does not replay",
    )

    selected_model = loaded.engine_loaded.loaded_config.config.models.primary
    replay_invocation = engine.RCPQualificationInvocation.create(
        config_hash=loaded.loaded_config.config_hash,
        catalog=catalog,
        model=selected_model,
        model_selection="primary",
        problem=loaded.engine_loaded.problem,
        prompt_template_sha256=loaded.engine_loaded.prompt_template_sha256,
        rendered_prompt_sha256=loaded.engine_loaded.rendered_prompt_sha256,
        decoding=loaded.engine_loaded.loaded_config.config.decoding.provider_decoding(),
    )
    _require(
        replay_invocation.model_dump(mode="json") == invocation.model_dump(mode="json"),
        "invocation does not replay byte-semantically",
    )

    _require(len(terminal.attempt_record_ids) == 1, "qualification attempt count differs")
    attempt_path = output / "attempts/0000/attempt_record.json"
    attempt = _load_json_model(attempt_path, engine.RCPAttemptRecord)
    assert isinstance(attempt, engine.RCPAttemptRecord)
    _require(
        (attempt.attempt_record_id,) == terminal.attempt_record_ids,
        "terminal attempt lineage differs",
    )
    request_path = root / attempt.request_artifact
    wire_path = root / attempt.wire_response_artifact
    provider_raw_path = root / attempt.provider_response_artifact
    request = _load_json_model(request_path, ProviderRequest)
    provider_raw = _load_json_model(provider_raw_path, ProviderRawResponse)
    assert isinstance(request, ProviderRequest)
    assert isinstance(provider_raw, ProviderRawResponse)
    _require(
        hash_file(request_path) == attempt.request_artifact_sha256,
        "request file hash differs",
    )
    _require(hash_file(wire_path) == attempt.wire_response_sha256, "wire file hash differs")
    _require(
        hash_file(provider_raw_path) == attempt.provider_response_sha256,
        "provider raw-response file hash differs",
    )
    _require(request.request_hash == attempt.request_hash, "request lineage differs")
    _require(provider_raw.request_hash == request.request_hash, "response request lineage differs")
    _require(
        provider_raw.attempt_id == attempt.provider_attempt_id,
        "response attempt lineage differs",
    )
    _require(provider_raw.status == "success", "provider response is not successful")

    replay_request = ProviderRequest.create(
        identity=ProviderIdentity(
            provider=request.provider,
            model=request.model,
            revision=request.revision,
            transport="fixture",
        ),
        prompt_template_hash=loaded.engine_loaded.prompt_template_sha256,
        rendered_prompt=loaded.engine_loaded.rendered_prompt,
        decoding=loaded.engine_loaded.loaded_config.config.decoding.provider_decoding(),
        input_ids=(loaded.engine_loaded.problem.problem_record_id,),
        private_source_content=False,
        attempt_index=0,
    )
    _require(
        replay_request.model_dump(mode="json") == request.model_dump(mode="json"),
        "provider request does not replay byte-semantically",
    )

    if provider_raw.output_text is None:
        raise RCPQualificationVerifyV2Error("provider output text is absent")
    parsed = parse_direct_autoformalization_output(provider_raw.output_text)
    _require(
        parsed.statement_sha256 == terminal.parsed_statement_sha256,
        "parsed statement hash differs from terminal",
    )
    replay_terminal = engine._terminal(
        invocation=invocation,
        attempt_records=(attempt,),
        status=engine.RCPTerminalStatus.RAW_COLLECTED,
        output_sha256=provider_raw.output_hash,
        parsed_statement_sha256=parsed.statement_sha256,
        parse_error_code=None,
    )
    _require(
        replay_terminal.model_dump(mode="json") == terminal.model_dump(mode="json"),
        "terminal does not replay byte-semantically",
    )

    preflight_suffix = catalog.observation_id.rsplit(":", 1)[-1]
    preflight_path = (
        root / loaded.loaded_config.config.outputs.preflight_root / f"{preflight_suffix}.json"
    )
    preflight = _load_json_model(preflight_path, RCPQualificationPreflightV2)
    assert isinstance(preflight, RCPQualificationPreflightV2)
    _require(
        preflight.catalog.model_dump(mode="json") == catalog.model_dump(mode="json"),
        "preflight catalog differs from run catalog",
    )
    _require(
        preflight.bound_artifact_hashes == loaded.bound_artifact_hashes,
        "preflight artifact bindings differ",
    )

    lean_validation = _validate_lean_raw(
        lean_raw_path,
        repo_root=root,
        parsed_statement=parsed.statement,
        parsed_statement_sha256=parsed.statement_sha256,
    )

    run_paths = tuple(
        sorted(
            (path for path in output.rglob("*") if path.is_file() and not path.is_symlink()),
            key=lambda path: str(path.relative_to(root)),
        )
    )
    scan_paths = (*run_paths, preflight_path, lean_raw_path)
    secret_scan = _scan_secrets(scan_paths, credential=credential)

    verifier_module = Path(__file__).resolve()
    inventory_paths = (*scan_paths, config_path.resolve(), verifier_module)
    artifact_inventory = {
        _repo_relative(path, root): hash_file(path)
        for path in sorted(set(inventory_paths), key=lambda item: str(item))
    }
    payload = {
        "schema_version": 1,
        "artifact_kind": "lf021_rcp_kimi_qualification_verification_v2",
        "verification_mode": "exact_offline_verify_only",
        "provider_calls_performed": 0,
        "network_requests_performed": 0,
        "config_hash": loaded.loaded_config.config_hash,
        "config_file_sha256": hash_file(config_path),
        "bound_artifact_hashes": loaded.bound_artifact_hashes,
        "invocation_id": invocation.invocation_id,
        "manifest_id": manifest.manifest_id,
        "terminal_id": terminal.terminal_id,
        "terminal_status": terminal.status.value,
        "model_id": invocation.model_id,
        "parsed_statement": parsed.statement,
        "parsed_statement_sha256": parsed.statement_sha256,
        "artifact_inventory": artifact_inventory,
        "artifact_inventory_sha256": hash_canonical(artifact_inventory),
        "secret_scan": secret_scan.model_dump(mode="json"),
        "lean_operational_validation": lean_validation.model_dump(mode="json"),
        "reference_transmission_performed": False,
        "private_source_transmission_performed": False,
        "semantic_labels_created": False,
        "semantic_faithfulness_assessed": False,
        "supervision_eligible": False,
        "gate_credit_claimed": False,
        "gate_closed": False,
    }
    verification_id = "rcp_qualification_verification_v2:" + hash_canonical(
        {"schema": "lf021_rcp_qualification_verification_v2", **payload}
    )
    report = RCPQualificationVerificationV2.model_validate(
        {"verification_id": verification_id, **payload}
    )
    report_bytes = canonical_json_bytes(report.model_dump(mode="json")) + b"\n"
    report_sha256 = _persist_immutable(report_path, report_bytes)
    return RCPQualificationVerificationRunV2(
        report=report,
        report_path=report_path,
        report_sha256=report_sha256,
    )
