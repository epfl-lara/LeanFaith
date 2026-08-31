"""One persistent-request, two-row SFT1 smoke.

The live path is deliberately small: one Mathlib backend, one Meta request,
four closed Expr endpoints, two immutable central-cache entries, and no
production or scale surface.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import psutil  # type: ignore[import-untyped]
from pydantic import Field, model_validator

from leanfaith.config.hashing import (
    canonical_json_bytes,
    hash_canonical,
    hash_file,
    sha256_hex,
)
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import find_repo_root
from leanfaith.host_resources import (
    Reservation,
    claim_resources,
    list_reservations,
    release_resources,
)
from leanfaith.lean.cache import EvidenceCache, EvidenceCacheKey
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.lean.protocol import LeanBackend, LeanRequest, LeanResult
from leanfaith.lean.session_policy import ServerMode
from leanfaith.representations.goal_v1 import (
    ClosedExprInput,
    ClosedExprSidecar,
    ClosedExprSourceMaterial,
    CompileContext,
    render_closed_expr_in_session,
)
from leanfaith.schemas.enums import (
    EvidenceExecutionStatus,
    EvidenceKind,
    EvidenceTargetKind,
)
from leanfaith.schemas.evidence import AuditValue, EvidenceRecord
from leanfaith.schemas.ids import (
    EVIDENCE_PREFIX,
    PAIR_PREFIX,
    THEOREM_PREFIX,
    make_id,
)

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)]
GitCommit = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$", strict=True)]
NonEmpty = Annotated[str, Field(min_length=1, strict=True)]

DEFAULT_CONFIG = Path("configs/transformations/sft1_value_first_v1/thin_smoke_v1.yaml")
SMOKE_MARKER = "LFSFT1SMOKEJSON "
RESOURCE_TASK = "SFT1-THIN-SMOKE"
METHOD_VERSION = "sft1_thin_smoke_v1"
POLICY_VERSION = "sft1_two_local_rows_2026_08_31"


class Authorization(StrictModel):
    exact_local_row_count: Literal[2]
    mathlib_only: Literal[True]
    p01_allowed: Literal[False]
    general_n31_bank_allowed: Literal[False]
    census_allowed: Literal[False]
    hundred_roots_allowed: Literal[False]
    ten_k_allowed: Literal[False]
    production_allowed: Literal[False]
    scale_allowed: Literal[False]
    training_allowed: Literal[False]
    publication_allowed: Literal[False]


class Implementation(StrictModel):
    base_commit: Literal["fc8cdc2c6d9d93e99e20933a17dbcfa2afc2be48"]
    base_tree: Literal["130bf13d240798e8830b106ddaa17a8e3feeb08f"]
    wave1_path: Literal["LeanFaith/Meta/SFT1/Wave1.lean"]
    wave1_sha256: Sha256
    helper_path: Literal["LeanFaith/Meta/SFT1/ThinSmoke.lean"]
    helper_sha256: Sha256
    runner_path: Literal["src/leanfaith/sft1/thin_smoke.py"]
    runner_sha256: Sha256
    import_strip_policy: Literal["remove_lines_whose_first_token_is_import_v1"]


class Project(StrictModel):
    project_id: Literal["mathlib"]
    project_dir: NonEmpty
    project_revision: Literal["d568c8c09630de097a046763c17b9ea99f95f950"]
    lean_version: Literal["v4.31.0-rc1"]
    lean_interact_version: Literal["0.11.4"]
    repl_revision: Literal["augustepoiroux/repl@lean-interact-0.11.4"]
    import_header: Literal["import Mathlib"]
    options: dict[str, bool]
    persistent_worker_count: Literal[1]
    lean_rss_claim_gib: Literal[24]

    @model_validator(mode="after")
    def _options(self) -> Project:
        if self.options != {"Elab.async": False, "autoImplicit": False}:
            raise ValueError("thin smoke must disable Elab.async and autoImplicit")
        return self


class PairSpec(StrictModel):
    root_kind: Literal["imported_mathlib_theorem", "hand_written_closed_canary"]
    root_name: NonEmpty
    root_id_basis: tuple[NonEmpty, ...]
    operation_id: Literal["P18_SYMMETRIZE_EQUALITY_V1", "N31_DROP_REQUIRED_GUARD_RUBRIC_V1"]
    label: bool
    source_path: str | None = None
    source_file_sha256: Sha256 | None = None
    reference_proposition: str | None = None
    proof_evidence_operation_id: str | None = None
    candidate_truth: str | None = None
    counterexample_witness: str | None = None
    smoke_only: bool | None = None


class ReprBinding(StrictModel):
    freeze_commit: Literal["176a783842c5a73b84413dfa8347670608b615d9"]
    implementation_commit: Literal["93cd9cf9d4848827f2bacad57a35c3d7f01500f7"]
    spec_hash: Literal["68d893a2c566bf3f6a82c899a32a351f9a5420f5ea98168c99b887aaa01a45a8"]
    config_sha256: Literal["a65d5b29760bbc5eb89405927f946f205eb99856c0538fdf5b57d3f9eceb0db7"]
    lean_renderer_sha256: Literal[
        "4471262f812746046570c51dde5958ee33db31a450a6974071efce584ba56bc3"
    ]
    injected_helper_sha256: Literal[
        "a6650452eebe683db295df1dfe925d3db8b03fc24e55cbc6793e838b5fe2f272"
    ]
    python_sha256: Literal["496237e190c394e9bd3c3036e2bc01c635905116c5084787a42e6cb569f45517"]
    implementation_set_hash: Literal[
        "9a9252fff5ffc69cb65e71120fedffa83ed47271aecadbecf0ceb890feea65ff"
    ]
    renderer_semantic_hash: Literal[
        "0bec5429cc0e539841208be53cd52189a7b80cbdb4649ee2d45b84bd8a5ef1fd"
    ]
    renderer_api_hash: Literal["c695ad868c98f27218e82184559d90624491df25c7805bf29861dd891787261d"]
    universe_profile_id: Literal["goal_v1_first_occurrence_u_i_v1"]
    universe_profile_hash: Literal[
        "d9e729134fcd6a086a58191810a9227062c66496ebe76b8da3c458a58b31cb61"
    ]
    render_context_id: Literal["goal_v1_render_context_v1"]
    render_context_hash: Literal["5f44b6970f0902c968fc98a2659b26c1c9d0bcaef2960cd3ea73808f203f8f62"]
    route_id: Literal["closed_expr_in_session"]


class Output(StrictModel):
    evidence_dir: NonEmpty
    staging_root: NonEmpty
    core_rows_file: Literal["rows.jsonl"]
    sidecars_file: Literal["sidecars.jsonl"]
    manifest_file: Literal["manifest.json"]


class ThinSmokeConfig(StrictModel):
    schema_version: Literal[1]
    smoke_id: Literal["sft1_thin_two_row_smoke_v1"]
    status: Literal["implementation"]
    authorization: Authorization
    implementation: Implementation
    project: Project
    positive: PairSpec
    negative: PairSpec
    repr: ReprBinding
    output: Output

    @model_validator(mode="after")
    def _exact_pairs(self) -> ThinSmokeConfig:
        if (
            self.positive.root_kind != "imported_mathlib_theorem"
            or self.positive.root_name != "Nat.lor_comm"
            or self.positive.operation_id != "P18_SYMMETRIZE_EQUALITY_V1"
            or self.positive.label is not True
            or self.positive.source_path != "Mathlib/Data/Nat/Bitwise.lean"
            or self.positive.source_file_sha256 is None
            or any(
                value is not None
                for value in (
                    self.positive.reference_proposition,
                    self.positive.proof_evidence_operation_id,
                    self.positive.candidate_truth,
                    self.positive.counterexample_witness,
                    self.positive.smoke_only,
                )
            )
        ):
            raise ValueError("positive smoke fixture drift")
        if (
            self.negative.root_kind != "hand_written_closed_canary"
            or self.negative.root_name != "n31_nat_eq_zero_add_one_v1"
            or self.negative.operation_id != "N31_DROP_REQUIRED_GUARD_RUBRIC_V1"
            or self.negative.label is not False
            or self.negative.reference_proposition != "∀ (n : Nat) (hn : n = 0), n + 1 = 1"
            or self.negative.proof_evidence_operation_id != "N31_DROP_REQUIRED_GUARD_PROOF_V1"
            or self.negative.candidate_truth != "refuted"
            or self.negative.counterexample_witness != "(1 : Nat)"
            or self.negative.smoke_only is not True
            or self.negative.source_path is not None
            or self.negative.source_file_sha256 is not None
        ):
            raise ValueError("negative smoke fixture drift")
        return self


class ThinSmokeError(RuntimeError):
    """Fail-closed smoke error."""


class CountingBackend:
    def __init__(self, delegate: LeanBackend) -> None:
        self.delegate = delegate
        self.run_calls = 0
        self.last_result: LeanResult | None = None

    def run(self, request: LeanRequest) -> LeanResult:
        self.run_calls += 1
        self.last_result = self.delegate.run(request)
        return self.last_result

    def run_batch(self, requests: Sequence[LeanRequest]) -> list[LeanResult]:
        return [self.run(request) for request in requests]

    def close(self) -> None:
        self.delegate.close()


class PeakRssSampler:
    def __init__(self) -> None:
        self.peak_bytes = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample, daemon=True)

    def _sample(self) -> None:
        process = psutil.Process()
        while not self._stop.wait(0.025):
            processes = [process, *process.children(recursive=True)]
            total = 0
            for item in {child.pid: child for child in processes}.values():
                try:
                    total += item.memory_info().rss
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            self.peak_bytes = max(self.peak_bytes, total)

    def __enter__(self) -> PeakRssSampler:
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2)


def load_thin_smoke_config(
    repo_root: Path, config_path: Path | None = None
) -> LoadedConfig[ThinSmokeConfig]:
    path = config_path or repo_root / DEFAULT_CONFIG
    loaded = load_config(path, ThinSmokeConfig)
    config = loaded.config
    for relative, expected in (
        (config.implementation.wave1_path, config.implementation.wave1_sha256),
        (config.implementation.helper_path, config.implementation.helper_sha256),
        (config.implementation.runner_path, config.implementation.runner_sha256),
    ):
        if hash_file(repo_root / relative) != expected:
            raise ThinSmokeError(f"implementation hash mismatch: {relative}")
    project = Path(config.project.project_dir)
    source = project / cast(str, config.positive.source_path)
    if hash_file(source) != config.positive.source_file_sha256:
        raise ThinSmokeError("Mathlib source hash mismatch")
    git_head = _run_readonly_git(project, "rev-parse", "HEAD")
    if git_head != config.project.project_revision:
        raise ThinSmokeError("Mathlib revision mismatch")
    return loaded


def _run_readonly_git(directory: Path, *args: str) -> str:
    import subprocess

    result = subprocess.run(
        ["git", "-C", str(directory), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _require_clean_implementation(repo_root: Path) -> tuple[str, str]:
    status = _run_readonly_git(repo_root, "status", "--porcelain", "--untracked-files=all")
    if status:
        raise ThinSmokeError("live smoke requires a clean committed thin implementation")
    commit = _run_readonly_git(repo_root, "rev-parse", "HEAD")
    tree = _run_readonly_git(repo_root, "rev-parse", "HEAD^{tree}")
    if _run_readonly_git(repo_root, "write-tree") != tree:
        raise ThinSmokeError("Git index tree differs from the committed implementation tree")
    return commit, tree


def _strip_imports(path: Path) -> str:
    return (
        "\n".join(
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("import ")
        ).rstrip()
        + "\n"
    )


def build_compile_context(repo_root: Path, config: ThinSmokeConfig) -> CompileContext:
    preamble = _strip_imports(repo_root / config.implementation.wave1_path)
    preamble += _strip_imports(repo_root / config.implementation.helper_path)
    return CompileContext(
        project_id=config.project.project_id,
        project_revision=config.project.project_revision,
        lean_version=config.project.lean_version,
        import_header=config.project.import_header,
        command_preamble=preamble,
        options=config.project.options,
    )


def build_inputs(config: ThinSmokeConfig) -> tuple[ClosedExprInput, ...]:
    return (
        ClosedExprInput(
            endpoint_id="positive.reference",
            endpoint_role="reference",
            expr_origin="loaded_constant_type",
            source_material=ClosedExprSourceMaterial(
                kind="raw_statement",
                raw_statement=(
                    "theorem Nat.lor_comm (n m : ℕ) : n ||| m = m ||| n := "  # noqa: RUF001
                    "Nat.bitwise_comm Bool.or_comm n m"
                ),
            ),
        ),
        ClosedExprInput(
            endpoint_id="positive.candidate",
            endpoint_role="candidate",
            expr_origin="sft1_transformed_expr",
            source_material=ClosedExprSourceMaterial(
                kind="constructed_expr_no_source_text",
                absence_reason="P18 candidate constructed by frozen Wave1 from Nat.lor_comm",
            ),
        ),
        ClosedExprInput(
            endpoint_id="negative.reference",
            endpoint_role="reference",
            expr_origin="term_elaborated_proposition",
            source_material=ClosedExprSourceMaterial(
                kind="proposition_text",
                proposition_text=cast(str, config.negative.reference_proposition),
            ),
        ),
        ClosedExprInput(
            endpoint_id="negative.candidate",
            endpoint_role="candidate",
            expr_origin="sft1_transformed_expr",
            source_material=ClosedExprSourceMaterial(
                kind="constructed_expr_no_source_text",
                absence_reason="exact smoke-only N31 guard deletion from the live reference Expr",
            ),
        ),
    )


def build_session_body(render_scope_id: str) -> str:
    return f'''run_meta do
  let positive ← LeanFaith.SFT1.ThinSmoke.buildPositive
  let negative ← LeanFaith.SFT1.ThinSmoke.buildNegative
  LeanFaith.SFT1.ThinSmoke.emitEvidence positive negative
  LeanFaith.GoalV1.emitClosedProp
    "positive.reference" "{render_scope_id}" "loaded_constant_type" positive.reference
  LeanFaith.GoalV1.emitClosedProp
    "positive.candidate" "{render_scope_id}" "sft1_transformed_expr" positive.candidate
  LeanFaith.GoalV1.emitClosedProp
    "negative.reference" "{render_scope_id}" "term_elaborated_proposition" negative.reference
  LeanFaith.GoalV1.emitClosedProp
    "negative.candidate" "{render_scope_id}" "sft1_transformed_expr" negative.candidate'''


def _extract_smoke_evidence(result: LeanResult | None) -> dict[str, Any]:
    if result is None:
        raise ThinSmokeError("missing captured Lean result")
    payloads: list[dict[str, Any]] = []
    for message in result.messages:
        for line in str(message.get("data", "")).splitlines():
            marker = line.find(SMOKE_MARKER)
            if marker >= 0:
                value = json.loads(line[marker + len(SMOKE_MARKER) :])
                if not isinstance(value, dict):
                    raise ThinSmokeError("non-object smoke evidence payload")
                payloads.append(value)
    if len(payloads) != 1:
        raise ThinSmokeError(f"expected one smoke evidence payload, found {len(payloads)}")
    payload = payloads[0]
    if payload.get("schema_version") != 1:
        raise ThinSmokeError("smoke evidence schema mismatch")
    return payload


def _validate_typed_evidence(name: str, spec: PairSpec, value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ThinSmokeError(f"missing {name} typed evidence")
    if (
        value.get("operation_id") != spec.operation_id
        or value.get("certificate_replay") is not True
        or value.get("candidate_elaboration") != "valid_closed_prop"
        or not str(value.get("reference_proof_expr_hash_u64", "")).isdigit()
    ):
        raise ThinSmokeError(f"invalid {name} typed evidence")
    certificate = value.get("certificate")
    if name == "positive":
        if (
            value.get("selected_site") != "outer_target"
            or value.get("source_theorem") != "Nat.lor_comm"
            or value.get("reference_proof") != "loaded_mathlib_theorem"
            or certificate != {"kind": "p18", "target_site": "outer_target"}
        ):
            raise ThinSmokeError("invalid P18 proof/certificate evidence")
    elif (
        value.get("proof_evidence_operation_id") != "N31_DROP_REQUIRED_GUARD_PROOF_V1"
        or value.get("selected_site") != "/bindingBody"
        or value.get("reference_proof") != "kernel_checked"
        or value.get("candidate_truth") != "refuted"
        or value.get("counterexample_witness") != "(1 : Nat)"
        or not str(value.get("candidate_refutation_expr_hash_u64", "")).isdigit()
        or not str(value.get("witness_refutation_expr_hash_u64", "")).isdigit()
        or value.get("smoke_only") is not True
        or value.get("general_n31_bank_activated") is not False
        or certificate
        != {
            "kind": "n31_canary_guard_drop_v1",
            "canary_id": "n31_nat_eq_zero_add_one_v1",
            "guard_ordinal": 1,
            "binder_name": "hn",
            "binder_info": "default",
            "construction": "lowerLooseBVars_1_1",
        }
    ):
        raise ThinSmokeError("invalid N31 proof/refutation/certificate evidence")
    return value


def _sidecar_map(sidecars: Sequence[ClosedExprSidecar]) -> dict[str, ClosedExprSidecar]:
    result = {sidecar.record.endpoint_id: sidecar for sidecar in sidecars}
    if set(result) != {
        "positive.reference",
        "positive.candidate",
        "negative.reference",
        "negative.candidate",
    }:
        raise ThinSmokeError("closed Expr endpoint inventory mismatch")
    for sidecar in result.values():
        text = sidecar.core_text()
        if text.count("⊢") != 1 or "[anonymous]" in text or "⋯" in text:
            raise ThinSmokeError(f"invalid rendered endpoint {sidecar.record.endpoint_id}")
    for prefix in ("positive", "negative"):
        if result[f"{prefix}.reference"].core_text() == result[f"{prefix}.candidate"].core_text():
            raise ThinSmokeError(f"{prefix} endpoints rendered identically")
    return result


def _validate_repr_binding(
    endpoints: Mapping[str, ClosedExprSidecar],
    *,
    config: ThinSmokeConfig,
    context: CompileContext,
) -> None:
    expected_implementation = {
        "renderer_semantic_hash": config.repr.renderer_semantic_hash,
        "lean_renderer_sha256": config.repr.lean_renderer_sha256,
        "injected_helper_sha256": config.repr.injected_helper_sha256,
        "python_module_sha256": config.repr.python_sha256,
        "config_file_sha256": config.repr.config_sha256,
        "implementation_set_hash": config.repr.implementation_set_hash,
    }
    for endpoint_id, sidecar in endpoints.items():
        record = sidecar.record
        provenance = record.provenance
        if (
            record.spec_hash != config.repr.spec_hash
            or record.compile_context_id != context.compile_context_id
            or record.implementation_identity.to_dict() != expected_implementation
            or provenance.universe_profile_id != config.repr.universe_profile_id
            or provenance.universe_profile_hash != config.repr.universe_profile_hash
            or provenance.render_context_id != config.repr.render_context_id
            or provenance.render_context_hash != config.repr.render_context_hash
            or provenance.route_id != config.repr.route_id
        ):
            raise ThinSmokeError(f"frozen REPR identity mismatch at {endpoint_id}")


def _root_id(spec: PairSpec) -> str:
    return "root:" + hash_canonical(list(spec.root_id_basis))


def _pair_id(spec: PairSpec, reference: ClosedExprSidecar, candidate: ClosedExprSidecar) -> str:
    return make_id(
        PAIR_PREFIX,
        {
            "root_id": _root_id(spec),
            "operation_id": spec.operation_id,
            "reference_expr_hash": reference.record.provenance.expr_hash,
            "candidate_expr_hash": candidate.record.provenance.expr_hash,
        },
    )


def _core_row(
    spec: PairSpec, reference: ClosedExprSidecar, candidate: ClosedExprSidecar
) -> dict[str, object]:
    row = {
        "pair_id": _pair_id(spec, reference, candidate),
        "root_id": _root_id(spec),
        "reference": reference.core_text(),
        "candidate": candidate.core_text(),
        "label": spec.label,
        "operation_id": spec.operation_id,
    }
    if set(row) != {"pair_id", "root_id", "reference", "candidate", "label", "operation_id"}:
        raise ThinSmokeError("core row schema drift")
    return row


def _cache_key(
    *,
    row: Mapping[str, object],
    reference: ClosedExprSidecar,
    candidate: ClosedExprSidecar,
    context: CompileContext,
    config: ThinSmokeConfig,
    config_hash: str,
) -> EvidenceCacheKey:
    pair_id = cast(str, row["pair_id"])
    operation_id = cast(str, row["operation_id"])
    environment_hash = hash_canonical(
        {
            "project_revision": config.project.project_revision,
            "compile_context": context.canonical_payload(),
            "wave1_sha256": config.implementation.wave1_sha256,
            "helper_sha256": config.implementation.helper_sha256,
            "runner_sha256": config.implementation.runner_sha256,
        }
    )
    policy_hash = hash_canonical(
        {
            "authorization": config.authorization.model_dump(mode="json"),
            "operation_id": operation_id,
            "smoke_only": True,
        }
    )
    return EvidenceCacheKey(
        pair_id=pair_id,
        theorem_a_id=make_id(THEOREM_PREFIX, {"pair_id": pair_id, "role": "reference"}),
        theorem_b_id=make_id(THEOREM_PREFIX, {"pair_id": pair_id, "role": "candidate"}),
        theorem_a_statement_hash=reference.record.provenance.expr_hash,
        theorem_b_statement_hash=candidate.record.provenance.expr_hash,
        representation_a_id=reference.record.representation_id,
        representation_b_id=candidate.record.representation_id,
        representation_a_content_hash=reference.record.rendered_goal_hash,
        representation_b_content_hash=candidate.record.rendered_goal_hash,
        representation_version="goal_v1.0",
        context_id=context.compile_context_id,
        context_fingerprint=context.fingerprint,
        environment_schema_version=1,
        environment_hash=environment_hash,
        evidence_kind=EvidenceKind.TRANSFORMATION_AUDIT,
        evidence_direction="none",
        method_version=METHOD_VERSION,
        timeout_seconds=300.0,
        config_hash=config_hash,
        semantic_policy_version=POLICY_VERSION,
        semantic_policy_hash=policy_hash,
        lean_version=config.project.lean_version,
        lean_interact_version=config.project.lean_interact_version,
        repl_revision=config.project.repl_revision,
        project_revision=config.project.project_revision,
    )


def _repr_summary(
    reference: ClosedExprSidecar,
    candidate: ClosedExprSidecar,
    config: ThinSmokeConfig,
) -> dict[str, object]:
    return {
        "reference": {
            "closed_expr_hash": reference.record.provenance.expr_hash,
            "render_hash": reference.record.rendered_goal_hash,
            "representation_id": reference.record.representation_id,
        },
        "candidate": {
            "closed_expr_hash": candidate.record.provenance.expr_hash,
            "render_hash": candidate.record.rendered_goal_hash,
            "representation_id": candidate.record.representation_id,
        },
        "spec_hash": reference.record.spec_hash,
        "implementation_identity": reference.record.implementation_identity.to_dict(),
        "universe_profile_id": reference.record.provenance.universe_profile_id,
        "universe_profile_hash": reference.record.provenance.universe_profile_hash,
        "render_context_id": reference.record.provenance.render_context_id,
        "render_context_hash": reference.record.provenance.render_context_hash,
        "route_id": reference.record.provenance.route_id,
        "frozen_binding": config.repr.model_dump(mode="json"),
    }


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.partial")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _install_immutable(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not path.is_file() or path.read_bytes() != data:
            raise ThinSmokeError(f"immutable smoke artifact conflict: {path}")
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.partial")
    with temporary.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.link(temporary, path)
    except FileExistsError:
        if not path.is_file() or path.read_bytes() != data:
            raise ThinSmokeError(f"immutable smoke artifact race: {path}") from None
    finally:
        temporary.unlink(missing_ok=True)


def _jsonl(values: Sequence[Mapping[str, object]]) -> bytes:
    return b"".join(canonical_json_bytes(value) + b"\n" for value in values)


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ThinSmokeError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_nonfinite_json(value: str) -> float:
    raise ThinSmokeError(f"non-finite JSON value {value!r}")


def _read_canonical_json_object(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise ThinSmokeError(f"expected regular smoke artifact: {path}")
    raw = path.read_bytes()
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_nonfinite_json,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ThinSmokeError(f"invalid smoke JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ThinSmokeError(f"expected JSON object: {path}")
    if raw != canonical_json_bytes(value) + b"\n":
        raise ThinSmokeError(f"noncanonical smoke JSON: {path}")
    return value


def _read_canonical_jsonl(path: Path) -> list[dict[str, object]]:
    if path.is_symlink() or not path.is_file():
        raise ThinSmokeError(f"expected regular smoke artifact: {path}")
    raw = path.read_bytes()
    if not raw or not raw.endswith(b"\n"):
        raise ThinSmokeError(f"invalid JSONL framing: {path}")
    values: list[dict[str, object]] = []
    for line in raw.splitlines():
        try:
            value = json.loads(
                line.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_json_keys,
                parse_constant=_reject_nonfinite_json,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ThinSmokeError(f"invalid smoke JSONL {path}: {exc}") from exc
        if not isinstance(value, dict) or line != canonical_json_bytes(value):
            raise ThinSmokeError(f"noncanonical smoke JSONL record: {path}")
        values.append(value)
    return values


def _raw_artifact(result: LeanResult) -> tuple[str, str]:
    if result.raw_response_path is None:
        raise ThinSmokeError("backend did not persist its raw response")
    path = Path(result.raw_response_path)
    if path.is_symlink() or not path.is_file():
        raise ThinSmokeError("backend raw response is missing")
    return str(path.resolve()), hash_file(path)


def run_thin_smoke(
    repo_root: Path,
    *,
    config_path: Path | None = None,
) -> Path:
    implementation_commit, implementation_tree = _require_clean_implementation(repo_root)
    loaded = load_thin_smoke_config(repo_root, config_path)
    config = loaded.config
    evidence_dir = repo_root / config.output.evidence_dir
    if evidence_dir.exists():
        raise ThinSmokeError("thin-smoke evidence already exists; use replay instead of rerun")
    context = build_compile_context(repo_root, config)
    render_scope_id = "sft1-thin-smoke:v1"
    session_body = build_session_body(render_scope_id)
    request_id = "sft1-thin:" + hash_canonical(
        {
            "config_hash": loaded.config_hash,
            "compile_context_id": context.compile_context_id,
            "session_body": session_body,
        }
    )
    staging = Path(config.output.staging_root)
    run_id = hash_canonical(
        {
            "implementation_commit": implementation_commit,
            "implementation_tree": implementation_tree,
            "config_hash": loaded.config_hash,
            "request_id": request_id,
        }
    )
    run_root = staging / "runs" / run_id
    raw_dir = staging / "raw"
    backend = CountingBackend(
        LeanInteractBackend(
            BackendSettings(
                project_dir=Path(config.project.project_dir),
                context_fingerprint=context.fingerprint,
                environment_schema_version=1,
                raw_response_dir=raw_dir,
                server_mode=ServerMode.STABLE,
                workers=None,
                memory_hard_limit_mb=24576,
                enable_parallel_elaboration=False,
                isolate_incremental_commands=True,
            )
        )
    )
    started = time.perf_counter()
    with PeakRssSampler() as sampler:
        try:
            batch = render_closed_expr_in_session(
                backend,
                inputs=build_inputs(config),
                compile_context=context,
                render_scope_id=render_scope_id,
                session_body=session_body,
                request_id=request_id,
                timeout_seconds=300.0,
            )
        finally:
            backend.close()
    elapsed_seconds = time.perf_counter() - started
    if backend.run_calls != 1:
        raise ThinSmokeError(f"expected one persistent Lean request, observed {backend.run_calls}")
    if batch.failures or len(batch.sidecars) != 4:
        details = "; ".join(f"{item.endpoint_id}: {item.detail}" for item in batch.failures)
        raise ThinSmokeError(f"closed Expr smoke failed: {details}")
    evidence = _extract_smoke_evidence(backend.last_result)
    endpoints = _sidecar_map(batch.sidecars)
    _validate_repr_binding(endpoints, config=config, context=context)
    pairs = (
        ("positive", config.positive),
        ("negative", config.negative),
    )
    manifest_relative = (Path(config.output.evidence_dir) / config.output.manifest_file).as_posix()
    rows: list[dict[str, object]] = []
    sidecars: list[dict[str, object]] = []
    keys: list[EvidenceCacheKey] = []
    for name, spec in pairs:
        reference = endpoints[f"{name}.reference"]
        candidate = endpoints[f"{name}.candidate"]
        row = _core_row(spec, reference, candidate)
        key = _cache_key(
            row=row,
            reference=reference,
            candidate=candidate,
            context=context,
            config=config,
            config_hash=loaded.config_hash,
        )
        pair_evidence = _validate_typed_evidence(name, spec, evidence.get(name))
        rows.append(row)
        sidecars.append(
            {
                "pair_id": row["pair_id"],
                "root_id": row["root_id"],
                "operation_id": spec.operation_id,
                "selected_site": pair_evidence["selected_site"],
                "transform_certificate": pair_evidence["certificate"],
                "certificate_replay": pair_evidence["certificate_replay"],
                "project": {
                    "project_id": config.project.project_id,
                    "project_revision": config.project.project_revision,
                    "lean_version": config.project.lean_version,
                    "lean_interact_version": config.project.lean_interact_version,
                    "compile_context_id": context.compile_context_id,
                },
                "repr": _repr_summary(reference, candidate, config),
                "candidate_elaboration": pair_evidence["candidate_elaboration"],
                "proof_and_counterexample_evidence": pair_evidence,
                "cache_identity": key.model_dump(mode="json"),
                "manifest_link": manifest_relative,
            }
        )
        keys.append(key)

    rows_bytes = _jsonl(rows)
    sidecars_bytes = _jsonl(sidecars)
    rows_stage = run_root / config.output.core_rows_file
    sidecars_stage = run_root / config.output.sidecars_file
    raw_source, _raw_source_hash = _raw_artifact(cast(LeanResult, backend.last_result))
    raw_bytes = Path(raw_source).read_bytes()
    raw_stage = run_root / "raw_response.json"
    replay_bundle_stage = run_root / "replay_bundle.json"
    replay_bundle = {
        "schema_version": 1,
        "run_id": run_id,
        "implementation_commit": implementation_commit,
        "implementation_tree": implementation_tree,
        "config_file_sha256": hash_file(loaded.path),
        "config_semantic_hash": loaded.config_hash,
        "lean_request_hash": batch.request_hash,
        "rows": rows,
        "sidecars": sidecars,
    }
    replay_bundle_bytes = canonical_json_bytes(replay_bundle) + b"\n"
    for path, data in (
        (rows_stage, rows_bytes),
        (sidecars_stage, sidecars_bytes),
        (raw_stage, raw_bytes),
        (replay_bundle_stage, replay_bundle_bytes),
    ):
        _install_immutable(path, data)
    artifact_hashes = {
        str(rows_stage.resolve()): sha256_hex(rows_bytes),
        str(sidecars_stage.resolve()): sha256_hex(sidecars_bytes),
        str(raw_stage.resolve()): sha256_hex(raw_bytes),
        str(replay_bundle_stage.resolve()): sha256_hex(replay_bundle_bytes),
    }
    cache = EvidenceCache(staging / "cache")
    cache_entries = []
    for row, sidecar, key in zip(rows, sidecars, keys, strict=True):
        evidence_record = EvidenceRecord(
            evidence_id=make_id(
                EVIDENCE_PREFIX,
                {"pair_id": row["pair_id"], "method_version": METHOD_VERSION},
            ),
            target_kind=EvidenceTargetKind.LEAN_PAIR,
            target_id=cast(str, row["pair_id"]),
            kind=EvidenceKind.TRANSFORMATION_AUDIT,
            status=EvidenceExecutionStatus.SUCCESS,
            value=AuditValue(
                checks={
                    "closed_expr_render": True,
                    "candidate_elaboration": True,
                    "certificate_replay": True,
                    "proof_or_refutation": True,
                },
                detail_artifact=str(replay_bundle_stage.resolve()),
            ),
            method_version=METHOD_VERSION,
            config_hash=loaded.config_hash,
            raw_artifact=str(raw_stage.resolve()),
            created_at=datetime.datetime.now(datetime.UTC),
            metadata={
                "operation_id": cast(str, row["operation_id"]),
                "smoke_only": True,
                "label": cast(bool, row["label"]),
            },
        )
        certificate_hash = hash_canonical(sidecar["transform_certificate"])
        cache_entries.append(
            cache.put(
                key,
                evidence_record,
                generated_code_hash=sha256_hex(session_body.encode("utf-8")),
                lean_request_hashes=(batch.request_hash,),
                certificate_dependency_hash=certificate_hash,
                artifact_hashes=artifact_hashes,
            )
        )
    calls_before_replay = backend.run_calls
    replayed = [cache.get(key) for key in keys]
    if any(entry is None for entry in replayed) or backend.run_calls != calls_before_replay:
        raise ThinSmokeError("central cache replay missed or issued another Lean request")
    for entry in replayed:
        if (
            entry is None
            or entry.lean_request_hashes != (batch.request_hash,)
            or entry.artifact_hashes != artifact_hashes
        ):
            raise ThinSmokeError("central cache replay lineage mismatch")
    rows_relative = (Path(config.output.evidence_dir) / config.output.core_rows_file).as_posix()
    sidecars_relative = (Path(config.output.evidence_dir) / config.output.sidecars_file).as_posix()
    manifest = {
        "schema_version": 1,
        "smoke_id": config.smoke_id,
        "status": "passed",
        "run_id": run_id,
        "implementation_commit": implementation_commit,
        "implementation_tree": implementation_tree,
        "config_path": loaded.path.relative_to(repo_root).as_posix(),
        "config_file_sha256": hash_file(loaded.path),
        "config_semantic_hash": loaded.config_hash,
        "wave1_sha256": config.implementation.wave1_sha256,
        "helper_sha256": config.implementation.helper_sha256,
        "runner_sha256": config.implementation.runner_sha256,
        "row_count": 2,
        "labels": {"positive": 1, "negative": 1},
        "ordered_pair_ids": [row["pair_id"] for row in rows],
        "core_rows_path": rows_relative,
        "core_rows_sha256": sha256_hex(rows_bytes),
        "sidecars_path": sidecars_relative,
        "sidecars_sha256": sha256_hex(sidecars_bytes),
        "replay_bundle_path": str(replay_bundle_stage.resolve()),
        "replay_bundle_sha256": sha256_hex(replay_bundle_bytes),
        "lean_request_hash": batch.request_hash,
        "lean_request_count": backend.run_calls,
        "cache_replay_lean_request_count": backend.run_calls - calls_before_replay,
        "cache_entry_hashes": [entry.cache_key_hash for entry in cache_entries],
        "cache_replay_hits": len([entry for entry in replayed if entry is not None]),
        "cache_root": str((staging / "cache").resolve()),
        "raw_response_path": str(raw_stage.resolve()),
        "raw_response_sha256": sha256_hex(raw_bytes),
        "elapsed_seconds": round(elapsed_seconds, 6),
        "lean_elapsed_ms": batch.elapsed_ms,
        "peak_process_tree_rss_bytes": sampler.peak_bytes,
        "resource_task": RESOURCE_TASK,
        "resource_claim": {
            "lean_workers": 1,
            "lean_rss_gib": config.project.lean_rss_claim_gib,
            "gpu": False,
        },
        "resource_released": False,
        "general_n31_bank_activated": False,
        "production_or_scale_authorized": False,
    }
    partial_dir = evidence_dir.parent / (
        f".{evidence_dir.name}.{run_id[:12]}.{os.getpid()}.partial"
    )
    partial_dir.mkdir(parents=True, exist_ok=False)
    _write_atomic(partial_dir / config.output.core_rows_file, rows_bytes)
    _write_atomic(partial_dir / config.output.sidecars_file, sidecars_bytes)
    _write_atomic(
        partial_dir / config.output.manifest_file,
        canonical_json_bytes(manifest) + b"\n",
    )
    if evidence_dir.exists():
        raise ThinSmokeError("thin-smoke evidence appeared during atomic installation")
    os.rename(partial_dir, evidence_dir)
    return evidence_dir / config.output.manifest_file


def mark_resource_released(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("resource_task") != RESOURCE_TASK:
        raise ThinSmokeError("cannot mark invalid manifest resource release")
    payload["resource_released"] = True
    _write_atomic(path, canonical_json_bytes(payload) + b"\n")


def _release_owned_reservation(claimed: Reservation) -> None:
    current = [item for item in list_reservations() if item.task == claimed.task]
    if current != [claimed]:
        raise ThinSmokeError("refusing to release a resource claim not owned by this smoke")
    released = release_resources(task=claimed.task)
    if released != claimed:
        raise ThinSmokeError("released resource claim did not match the owned smoke claim")


def replay_thin_smoke(repo_root: Path, *, config_path: Path | None = None) -> int:
    loaded = load_thin_smoke_config(repo_root, config_path)
    config = loaded.config
    evidence_dir = repo_root / config.output.evidence_dir
    manifest_path = evidence_dir / config.output.manifest_file
    rows_path = evidence_dir / config.output.core_rows_file
    sidecars_path = evidence_dir / config.output.sidecars_file
    manifest = _read_canonical_json_object(manifest_path)
    rows = _read_canonical_jsonl(rows_path)
    sidecars = _read_canonical_jsonl(sidecars_path)
    if (
        manifest.get("status") != "passed"
        or manifest.get("resource_released") is not True
        or manifest.get("row_count") != 2
        or manifest.get("labels") != {"positive": 1, "negative": 1}
        or manifest.get("lean_request_count") != 1
        or manifest.get("cache_replay_lean_request_count") != 0
        or manifest.get("cache_replay_hits") != 2
        or manifest.get("general_n31_bank_activated") is not False
        or manifest.get("production_or_scale_authorized") is not False
        or manifest.get("resource_claim") != {"lean_workers": 1, "lean_rss_gib": 24, "gpu": False}
    ):
        raise ThinSmokeError("manifest does not describe a completed bounded two-row smoke")
    if (
        manifest.get("config_file_sha256") != hash_file(loaded.path)
        or manifest.get("config_semantic_hash") != loaded.config_hash
        or manifest.get("wave1_sha256") != config.implementation.wave1_sha256
        or manifest.get("helper_sha256") != config.implementation.helper_sha256
        or manifest.get("runner_sha256") != config.implementation.runner_sha256
        or manifest.get("core_rows_path") != rows_path.relative_to(repo_root).as_posix()
        or manifest.get("sidecars_path") != sidecars_path.relative_to(repo_root).as_posix()
        or manifest.get("core_rows_sha256") != hash_file(rows_path)
        or manifest.get("sidecars_sha256") != hash_file(sidecars_path)
    ):
        raise ThinSmokeError("manifest implementation or serialized artifact binding mismatch")
    if len(rows) != 2 or len(sidecars) != 2:
        raise ThinSmokeError("replay requires exactly two rows and two sidecars")
    if any(
        not isinstance(row.get(field), str)
        for row in rows
        for field in ("pair_id", "root_id", "reference", "candidate", "operation_id")
    ):
        raise ThinSmokeError("core row string field is malformed")
    ordered_pair_ids = [cast(str, row["pair_id"]) for row in rows]
    if (
        len(set(ordered_pair_ids)) != 2
        or len({row.get("root_id") for row in rows}) != 2
        or manifest.get("ordered_pair_ids") != ordered_pair_ids
        or {row.get("label") for row in rows} != {False, True}
    ):
        raise ThinSmokeError("two-row identity, root, label, or order invariant failed")

    run_id = manifest.get("run_id")
    request_hash = manifest.get("lean_request_hash")
    if (
        not isinstance(run_id, str)
        or re.fullmatch(r"[0-9a-f]{64}", run_id) is None
        or not isinstance(request_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", request_hash) is None
    ):
        raise ThinSmokeError("manifest run ID is invalid")
    run_root = Path(config.output.staging_root) / "runs" / run_id
    replay_bundle_path = run_root / "replay_bundle.json"
    raw_path = run_root / "raw_response.json"
    rows_stage = run_root / config.output.core_rows_file
    sidecars_stage = run_root / config.output.sidecars_file
    cache_root = Path(config.output.staging_root) / "cache"
    if (
        manifest.get("replay_bundle_path") != str(replay_bundle_path.resolve())
        or manifest.get("raw_response_path") != str(raw_path.resolve())
        or manifest.get("cache_root") != str(cache_root.resolve())
        or manifest.get("replay_bundle_sha256") != hash_file(replay_bundle_path)
        or manifest.get("raw_response_sha256") != hash_file(raw_path)
        or rows_stage.is_symlink()
        or sidecars_stage.is_symlink()
        or raw_path.is_symlink()
        or replay_bundle_path.is_symlink()
        or rows_stage.read_bytes() != rows_path.read_bytes()
        or sidecars_stage.read_bytes() != sidecars_path.read_bytes()
    ):
        raise ThinSmokeError("immutable replay artifact binding mismatch")
    replay_bundle = _read_canonical_json_object(replay_bundle_path)
    if replay_bundle != {
        "schema_version": 1,
        "run_id": run_id,
        "implementation_commit": manifest.get("implementation_commit"),
        "implementation_tree": manifest.get("implementation_tree"),
        "config_file_sha256": manifest.get("config_file_sha256"),
        "config_semantic_hash": manifest.get("config_semantic_hash"),
        "lean_request_hash": manifest.get("lean_request_hash"),
        "rows": rows,
        "sidecars": sidecars,
    }:
        raise ThinSmokeError("replay bundle content mismatch")

    expected_sidecar_keys = {
        "pair_id",
        "root_id",
        "operation_id",
        "selected_site",
        "transform_certificate",
        "certificate_replay",
        "project",
        "repr",
        "candidate_elaboration",
        "proof_and_counterexample_evidence",
        "cache_identity",
        "manifest_link",
    }
    expected_manifest_link = (
        Path(config.output.evidence_dir) / config.output.manifest_file
    ).as_posix()
    sidecars_by_pair = {value.get("pair_id"): value for value in sidecars}
    if len(sidecars_by_pair) != 2 or set(sidecars_by_pair) != set(ordered_pair_ids):
        raise ThinSmokeError("row and sidecar pair inventory mismatch")
    artifact_hashes = {
        str(rows_stage.resolve()): hash_file(rows_stage),
        str(sidecars_stage.resolve()): hash_file(sidecars_stage),
        str(raw_path.resolve()): hash_file(raw_path),
        str(replay_bundle_path.resolve()): hash_file(replay_bundle_path),
    }
    cache = EvidenceCache(cache_root)
    expected_cache_hashes = manifest.get("cache_entry_hashes")
    if not isinstance(expected_cache_hashes, list) or len(expected_cache_hashes) != 2:
        raise ThinSmokeError("manifest cache inventory mismatch")
    hits = 0
    for index, row in enumerate(rows):
        if set(row) != {
            "pair_id",
            "root_id",
            "reference",
            "candidate",
            "label",
            "operation_id",
        }:
            raise ThinSmokeError("core row schema drift during replay")
        pair_id = row["pair_id"]
        sidecar = sidecars_by_pair[pair_id]
        if (
            set(sidecar) != expected_sidecar_keys
            or sidecar.get("root_id") != row["root_id"]
            or sidecar.get("operation_id") != row["operation_id"]
            or sidecar.get("manifest_link") != expected_manifest_link
            or sidecar.get("certificate_replay") is not True
            or sidecar.get("candidate_elaboration") != "valid_closed_prop"
        ):
            raise ThinSmokeError("row and sidecar semantic binding mismatch")
        name = "positive" if row["label"] is True else "negative"
        spec = config.positive if name == "positive" else config.negative
        proof_value = _validate_typed_evidence(
            name, spec, sidecar.get("proof_and_counterexample_evidence")
        )
        if (
            sidecar.get("selected_site") != proof_value["selected_site"]
            or sidecar.get("transform_certificate") != proof_value["certificate"]
            or row["operation_id"] != spec.operation_id
        ):
            raise ThinSmokeError("typed evidence and sidecar certificate mismatch")
        repr_value = sidecar.get("repr")
        if not isinstance(repr_value, dict):
            raise ThinSmokeError("sidecar REPR evidence missing")
        reference_repr = repr_value.get("reference")
        candidate_repr = repr_value.get("candidate")
        cache_identity = sidecar.get("cache_identity")
        if (
            not isinstance(reference_repr, dict)
            or not isinstance(candidate_repr, dict)
            or not isinstance(cache_identity, dict)
            or repr_value.get("frozen_binding") != config.repr.model_dump(mode="json")
        ):
            raise ThinSmokeError("sidecar REPR or cache identity malformed")
        key = EvidenceCacheKey.model_validate(cache_identity)
        if (
            key.pair_id != pair_id
            or key.config_hash != loaded.config_hash
            or key.theorem_a_statement_hash != reference_repr.get("closed_expr_hash")
            or key.theorem_b_statement_hash != candidate_repr.get("closed_expr_hash")
            or key.representation_a_content_hash
            != sha256_hex(cast(str, row["reference"]).encode("utf-8"))
            or key.representation_b_content_hash
            != sha256_hex(cast(str, row["candidate"]).encode("utf-8"))
        ):
            raise ThinSmokeError("core row, closed Expr, and cache key binding mismatch")
        entry = cache.get(key)
        if entry is None:
            raise ThinSmokeError("cache replay miss")
        value = entry.evidence.value
        if (
            entry.cache_key_hash != expected_cache_hashes[index]
            or entry.artifact_hashes != artifact_hashes
            or entry.generated_code_hash
            != sha256_hex(build_session_body("sft1-thin-smoke:v1").encode("utf-8"))
            or entry.lean_request_hashes != (request_hash,)
            or entry.certificate_dependency_hash != hash_canonical(sidecar["transform_certificate"])
            or entry.evidence.status != EvidenceExecutionStatus.SUCCESS
            or entry.evidence.target_id != pair_id
            or entry.evidence.metadata
            != {"operation_id": row["operation_id"], "smoke_only": True, "label": row["label"]}
            or not isinstance(value, AuditValue)
            or value.checks
            != {
                "closed_expr_render": True,
                "candidate_elaboration": True,
                "certificate_replay": True,
                "proof_or_refutation": True,
            }
            or value.detail_artifact != str(replay_bundle_path.resolve())
            or entry.evidence.raw_artifact != str(raw_path.resolve())
        ):
            raise ThinSmokeError("cache evidence lineage mismatch")
        hits += 1
    return hits


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "replay", "validate"))
    parser.add_argument("--repo-root", type=Path, default=find_repo_root(Path.cwd()))
    parser.add_argument("--config", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    config_path = args.config.resolve() if args.config else None
    if args.command == "validate":
        loaded = load_thin_smoke_config(repo_root, config_path)
        print(loaded.config_hash)
        return 0
    if args.command == "replay":
        print(json.dumps({"cache_hits": replay_thin_smoke(repo_root, config_path=config_path)}))
        return 0
    loaded = load_thin_smoke_config(repo_root, config_path)
    reservation = claim_resources(
        task=RESOURCE_TASK,
        lean_workers=1,
        lean_rss_gib=loaded.config.project.lean_rss_claim_gib,
        gpu=False,
        pid=os.getpid(),
        owner_session="Codex-/root-sft1-thin-smoke",
        worktree=repo_root,
    )
    manifest_path: Path | None = None
    try:
        manifest_path = run_thin_smoke(repo_root, config_path=config_path)
    finally:
        _release_owned_reservation(reservation)
    if manifest_path is None:
        raise ThinSmokeError("smoke ended without a manifest")
    mark_resource_released(manifest_path)
    print(manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
