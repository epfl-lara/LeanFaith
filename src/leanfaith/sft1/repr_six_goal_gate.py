"""Bounded SFT1 six-real-goal integration gate for the frozen REPR API.

The module is inert on import.  It validates the exact gate specification and
builds one direct-Expr request per pair through
``render_closed_expr_in_session``.  Callers supply already-initialized project
backends, so this layer never spawns a Lean process per case.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from leanfaith.config.hashing import hash_canonical, hash_file, sha256_hex
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import find_repo_root
from leanfaith.lean.protocol import LeanBackend, LeanRequest, LeanResult
from leanfaith.representations.goal_v1 import (
    CLOSED_EXPR_MARKER,
    ClosedExprFailure,
    ClosedExprInput,
    ClosedExprSidecar,
    ClosedExprSourceMaterial,
    CompileContext,
    render_closed_expr_in_session,
)

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)]
GitCommit = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$", strict=True)]
NonEmptyStr = Annotated[str, Field(min_length=1, strict=True)]
PositiveInt = Annotated[int, Field(gt=0, strict=True)]

DEFAULT_CONFIG_PATH = Path(
    "configs/transformations/sft1_value_first_v1/repr_six_goal_gate_v0_3_1.yaml"
)
EXPECTED_EXECUTION_CONFIG_PATH = Path(
    "configs/transformations/sft1_value_first_v1/repr_six_goal_gate_execution_v0_3_1.yaml"
)
EXPECTED_CASE_IDS: tuple[str, ...] = (
    "mathlib_add_pow",
    "physlib_kinetic_energy_conserved",
    "cslib_ret_merge",
    "lean_compiler_int_lt",
    "canonical_gold_aime_1983_p1",
    "consistency_check_amc12a_2019_p21",
)
EXPECTED_HELPER_FILE_SHA256 = "c87b9c5065a41f51e7cbdcdcc98f14fedc6a015054c40a0cfe4367ad63330129"
EXPECTED_HELPER_PREAMBLE_SHA256 = "bd0e3ef6b5e5c50bf07b31771e2a2ca0da131323d10d8571994bdd24a922981a"
EXPECTED_EXECUTION_CONFIG_FILE_SHA256 = (
    "82f22c08082e26424e1a55627b707d341e7fa84f72348cfed0b007b0526505ff"
)
EXPECTED_EXECUTION_CONFIG_HASH = "dfc7037ee8d5a340b82b237fa14ef1f3d9c2752bf64e91d34846d9570fac5747"
EXPECTED_GATE_CONFIG_FILE_SHA256 = (
    "5126eb8fb314218017fc930a79ab82cb810ff929e1794ce4617551f6c70ced91"
)
EXPECTED_GATE_CONFIG_HASH = "7404e31935ab35b9c3270bf46654936121944a7e8f55fb91da4f1e047f59c0ad"
EXPECTED_RECEIPT_PATH = Path(
    "configs/transformations/sft1_value_first_v1/repr_six_goal_gate_receipt_v0_3_1.json"
)
EXPECTED_RECEIPT_FILE_SHA256 = "ebd400b4a7b05daa933b1abaaacc378d1a7b9ae68f9159ac03453cd6081406a8"
EXPECTED_RECEIPT_HASH = "f62b68ebc946469952bdd34674c127e2bd1146b0a8febbe5d199fea54a081e78"
EXPECTED_EVIDENCE_DIRECTORY = Path(
    "/storage/milikic/leanfaith/value_first/sft1_deterministic_v1/"
    "repr_six_goal/attempt_009/evidence"
)
EXPECTED_EVIDENCE_BUNDLE_DIRECTORY = Path(
    "configs/transformations/sft1_value_first_v1/repr_six_goal_evidence_v0_3_1"
)
EXPECTED_EVIDENCE_BUNDLE_MANIFEST_FILE_SHA256 = (
    "aeb44673d45ce3bb31923fec7ab402c40aefdefa108302deed0b9f0fe0246d46"
)

EXACT_FAILURE_CLASSES: tuple[str, ...] = (
    "reference_render_failure",
    "candidate_render_failure",
    "expr_mvar",
    "universe_mvar",
    "free_variable",
    "loose_bound_variable",
    "anonymous_binder_name",
    "forbidden_rendered_placeholder",
    "ill_typed",
    "non_prop",
    "wrong_turnstile_count",
    "required_distinct_render_collapsed",
    "universe_profile_mismatch",
    "renderer_context_mismatch",
    "repr_real_goal_coverage_not_passed",
)

_LEAN_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*$")
_ENDPOINT_ID = re.compile(r"^[a-z0-9_]+\.(?:reference|candidate)$")


class ReferenceMode(StrEnum):
    IMPORTED_CONSTANT = "imported_constant"
    EXTRACTED_SIGNATURE = "extracted_signature"


class GateReference(StrictModel):
    mode: ReferenceMode
    constant_name: str | None
    proposition_text: str | None
    raw_statement: str | None

    @model_validator(mode="after")
    def _validate_mode(self) -> GateReference:
        if self.mode == ReferenceMode.IMPORTED_CONSTANT:
            if (
                not isinstance(self.constant_name, str)
                or _LEAN_NAME.fullmatch(self.constant_name) is None
                or not isinstance(self.raw_statement, str)
                or not self.raw_statement.strip()
                or self.proposition_text is not None
            ):
                raise ValueError("imported_constant requires a Lean name and raw statement only")
        elif (
            self.constant_name is not None
            or not isinstance(self.proposition_text, str)
            or not self.proposition_text.strip()
            or self.raw_statement is not None
        ):
            raise ValueError(
                "extracted_signature requires proposition_text and no declaration fields"
            )
        return self


class GateCase(StrictModel):
    case_id: NonEmptyStr
    ordinal: PositiveInt
    family_id: Literal["P01"]
    operation_id: Literal["P01_ALPHA_RENAME_SINGLE_V1"]
    polarity: Literal["positive"]
    production_admission: Literal[False]
    source_family: NonEmptyStr
    source_revision: NonEmptyStr
    source_path: NonEmptyStr
    source_record: str | None = None
    source_file_sha256: Sha256 | None = None
    source_formal_statement_sha256: Sha256 | None = None
    source_syntax_normalization_id: str | None = None
    source_syntax_normalization_rule: str | None = None
    normalized_proposition_sha256: Sha256 | None = None
    backend_id: Literal[
        "mathlib_bigoperators",
        "mathlib_default",
        "lean_compiler_default",
        "physlib_default",
        "cslib_default",
    ]
    compile_project_id: Literal["mathlib", "physlib", "cslib"]
    compile_project_revision: NonEmptyStr
    lean_version: NonEmptyStr
    import_header: NonEmptyStr
    namespace_context: tuple[str, ...]
    open_context: tuple[str, ...]
    scoped_context: tuple[str, ...]
    options: dict[str, bool]
    reference: GateReference

    @model_validator(mode="after")
    def _validate_context(self) -> GateCase:
        if self.options != {"Elab.async": False, "autoImplicit": False}:
            raise ValueError("six-goal compile options must disable async and autoImplicit")
        if self.namespace_context or self.open_context:
            raise ValueError("six-goal cases use no ambient namespace or open namespace")
        if any(
            not item.strip() or any(char.isspace() for char in item) for item in self.scoped_context
        ):
            raise ValueError("scoped context entries must be nonempty Lean names")
        if self.case_id == "cslib_ret_merge" and (
            self.scoped_context or any("TimeM" in item for item in self.open_context)
        ):
            raise ValueError("cslib_ret_merge must not open the TimeM notation scope")
        if self.reference.mode == ReferenceMode.EXTRACTED_SIGNATURE and (
            self.source_record is None
            or self.source_file_sha256 is None
            or self.source_formal_statement_sha256 is None
        ):
            raise ValueError("extracted signatures require exact source and statement hashes")
        normalization = (
            self.source_syntax_normalization_id,
            self.source_syntax_normalization_rule,
            self.normalized_proposition_sha256,
        )
        if self.case_id == "consistency_check_amc12a_2019_p21":
            expected_prefix = (
                "consistencycheck_typed_finset_sum_syntax_v1",
                "replace_legacy_typed_finset_sum_in_binder_with_membership_binder_and_typed_lower_bound_v1",
            )
            if normalization[:2] != expected_prefix or self.reference.proposition_text is None:
                raise ValueError(
                    "ConsistencyCheck requires its exact recorded syntax normalization"
                )
            expected_hash = sha256_hex(self.reference.proposition_text.encode("utf-8"))
            if self.normalized_proposition_sha256 != expected_hash:
                raise ValueError("ConsistencyCheck normalized proposition hash does not replay")
        elif any(item is not None for item in normalization):
            raise ValueError("only the ConsistencyCheck fixture has a syntax normalization")
        return self


class GateAuthorization(StrictModel):
    repr_dependency_integration: Literal[True]
    six_real_goal_gate: Literal[True]
    one_example_gate: Literal[False]
    hundred_root_gate: Literal[False]
    ten_k_pilot: Literal[False]
    row_generation: Literal[False]
    bulk_scale: Literal[False]
    publication: Literal[False]


class ReprBinding(StrictModel):
    implementation_commit: Literal["93cd9cf9d4848827f2bacad57a35c3d7f01500f7"]
    freeze_commit: Literal["176a783842c5a73b84413dfa8347670608b615d9"]
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
    python_entrypoint: Literal["render_closed_expr_in_session"]
    endpoint_emitter: Literal["LeanFaith.GoalV1.emitClosedProp"]
    model_facing_projection: Literal["sidecar.core_text()"]


class FixedPreamble(StrictModel):
    source_path: Literal["LeanFaith/Meta/SFT1/RepresentationGate.lean"]
    file_sha256: Sha256
    injected_preamble_sha256: Sha256
    import_strip_policy: Literal["remove_lines_whose_first_token_is_import_v1"]
    review_status: Literal["reviewed_for_bounded_six_goal_gate"]
    review_checks: tuple[str, ...]


class ExecutionContract(StrictModel):
    one_persistent_backend_per_backend_id: Literal[True]
    one_run_meta_per_pair: Literal[True]
    reference_and_candidate_alive_in_same_request: Literal[True]
    explicitly_unrolled_endpoints: Literal[True]
    emitter_calls_per_endpoint: Literal[1]
    endpoint_count_per_pair: Literal[2]
    endpoint_declarations_allowed: Literal[False]
    candidate_proofs_allowed: Literal[False]
    surface_candidate_rendering_allowed: Literal[False]
    candidate_text_reelaboration_allowed: Literal[False]
    goal_v1_compilation_allowed: Literal[False]
    complete_sidecars_persisted: Literal[True]
    exact_turnstile_count: Literal[1]
    reference_candidate_render_must_differ: Literal[True]
    forbidden_render_substrings: tuple[Literal["[anonymous]", "⋯"], ...]
    stop_on_first_failure: Literal[True]
    cslib_time_m_scope_must_remain_closed: Literal[True]


class ReceiptBinding(StrictModel):
    receipt_path: str | None
    receipt_file_sha256: Sha256 | None
    regression_id: str | None
    receipt_hash: Sha256 | None
    execution_config_file_sha256: Sha256 | None
    execution_config_hash: Sha256 | None
    passed: bool = Field(strict=True)
    repr_consistency_check_receipt_is_substitutable: Literal[False]


class SixGoalGateConfig(StrictModel):
    schema_version: Literal[1]
    gate_id: Literal["sft1_repr_six_real_goal_direct_expr_v0_3_1"]
    status: Literal["pending_execution", "passed"]
    purpose: NonEmptyStr
    authorization: GateAuthorization
    repr_binding: ReprBinding
    fixed_preamble: FixedPreamble
    execution_contract: ExecutionContract
    receipt_binding: ReceiptBinding
    cases: tuple[GateCase, ...]

    @model_validator(mode="after")
    def _validate_exact_cases(self) -> SixGoalGateConfig:
        receipt_values = (
            self.receipt_binding.receipt_path,
            self.receipt_binding.receipt_file_sha256,
            self.receipt_binding.regression_id,
            self.receipt_binding.receipt_hash,
            self.receipt_binding.execution_config_file_sha256,
            self.receipt_binding.execution_config_hash,
        )
        if self.status == "pending_execution":
            if any(value is not None for value in receipt_values) or self.receipt_binding.passed:
                raise ValueError("pending six-goal config cannot contain a frozen receipt")
        elif any(value is None for value in receipt_values) or not self.receipt_binding.passed:
            raise ValueError("passed six-goal config requires a complete frozen receipt")
        if tuple(case.case_id for case in self.cases) != EXPECTED_CASE_IDS:
            raise ValueError("six-goal case order or identity differs from the approved gate")
        if tuple(case.ordinal for case in self.cases) != tuple(range(1, 7)):
            raise ValueError("six-goal ordinals must be exactly 1 through 6")
        expected_modes = (
            ReferenceMode.IMPORTED_CONSTANT,
            ReferenceMode.IMPORTED_CONSTANT,
            ReferenceMode.IMPORTED_CONSTANT,
            ReferenceMode.IMPORTED_CONSTANT,
            ReferenceMode.EXTRACTED_SIGNATURE,
            ReferenceMode.EXTRACTED_SIGNATURE,
        )
        if tuple(case.reference.mode for case in self.cases) != expected_modes:
            raise ValueError("six-goal reference routes differ from the approved split")
        if self.cases[2].import_header != "import Cslib.Algorithms.Lean.MergeSort.MergeSort":
            raise ValueError("cslib_ret_merge import drifted")
        if self.cases[2].scoped_context:
            raise ValueError("cslib_ret_merge may not activate a scoped notation")
        expected_contexts = (
            ("mathlib_bigoperators", "import Mathlib", ("BigOperators",)),
            ("physlib_default", "import Physlib.ClassicalMechanics.FreeParticle.Basic", ()),
            ("cslib_default", "import Cslib.Algorithms.Lean.MergeSort.MergeSort", ()),
            ("lean_compiler_default", "import Init.Sym.Lemmas", ()),
            ("mathlib_default", "import Mathlib", ()),
            ("mathlib_bigoperators", "import Mathlib", ("BigOperators",)),
        )
        observed_contexts = tuple(
            (case.backend_id, case.import_header, case.scoped_context) for case in self.cases
        )
        if observed_contexts != expected_contexts:
            raise ValueError("six-goal imports or notation scopes are not the minimal freeze")
        if (
            self.cases[4].source_file_sha256
            != "a0c4d102a0ea4d2923cca85129c6cda054a11b1854462eed3d7e71e555b703ea"
            or self.cases[4].source_formal_statement_sha256
            != "8b0061199a23b47539e6f30df775109d5c6776ea1c2206f452d5a9d48240aa7e"
        ):
            raise ValueError("canonical-gold source binding differs from the reviewed record")
        return self


@dataclass(frozen=True, slots=True)
class LoadedSixGoalGate:
    config: SixGoalGateConfig
    path: Path
    config_hash: str
    config_file_sha256: str
    helper_path: Path
    helper_file_sha256: str
    helper_preamble: str
    helper_preamble_sha256: str


@dataclass(frozen=True, slots=True)
class GateCaseOutcome:
    case_id: str
    source: str
    family: str
    operation: str
    polarity: str
    passed: bool
    exact_failure_class: str | None
    request_hash: str
    elapsed_ms: int
    evidence_path: str | None
    evidence_sha256: str | None
    failure_details: tuple[str, ...]
    diagnostic_rendered_goals: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class SixGoalGateResult:
    gate_id: str
    config_hash: str
    config_file_sha256: str
    helper_file_sha256: str
    helper_preamble_sha256: str
    outcomes: tuple[GateCaseOutcome, ...]
    passed: bool
    stopped_case_id: str | None
    evidence_directory: str


class GateValidationError(ValueError):
    def __init__(self, exact_failure_class: str, detail: str) -> None:
        if exact_failure_class not in EXACT_FAILURE_CLASSES:
            raise ValueError(f"unknown six-goal exact failure class {exact_failure_class!r}")
        super().__init__(detail)
        self.exact_failure_class = exact_failure_class


def _helper_preamble(source: str) -> str:
    lines = [line for line in source.splitlines() if not line.lstrip().startswith("import ")]
    return "\n".join(lines).strip()


def _strict_json_object(path: Path, *, label: str) -> dict[str, Any]:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{label} contains duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {label}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload


def _exact_object(value: object, keys: set[str], *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{label} has an unexpected schema")
    return value


def _require_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


def _require_positive_int(value: object, *, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _expected_source(case: GateCase) -> dict[str, str | None]:
    return {
        "family": case.source_family,
        "revision": case.source_revision,
        "path": case.source_path,
        "record": case.source_record,
        "file_sha256": case.source_file_sha256,
        "formal_statement_sha256": case.source_formal_statement_sha256,
        "syntax_normalization_id": case.source_syntax_normalization_id,
        "syntax_normalization_rule": case.source_syntax_normalization_rule,
        "normalized_proposition_sha256": case.normalized_proposition_sha256,
    }


def _expected_evidence_bindings(config: SixGoalGateConfig) -> dict[str, str]:
    repr_binding = config.repr_binding
    return {
        "gate_config_hash": EXPECTED_EXECUTION_CONFIG_HASH,
        "gate_config_file_sha256": EXPECTED_EXECUTION_CONFIG_FILE_SHA256,
        "helper_file_sha256": EXPECTED_HELPER_FILE_SHA256,
        "helper_preamble_sha256": EXPECTED_HELPER_PREAMBLE_SHA256,
        "repr_freeze_commit": repr_binding.freeze_commit,
        "repr_spec_hash": repr_binding.spec_hash,
        "repr_implementation_set_hash": repr_binding.implementation_set_hash,
        "renderer_semantic_hash": repr_binding.renderer_semantic_hash,
        "renderer_api_hash": repr_binding.renderer_api_hash,
        "universe_profile_id": repr_binding.universe_profile_id,
        "universe_profile_hash": repr_binding.universe_profile_hash,
        "render_context_id": repr_binding.render_context_id,
        "render_context_hash": repr_binding.render_context_hash,
    }


def _expected_implementation_identity(config: SixGoalGateConfig) -> dict[str, str]:
    repr_binding = config.repr_binding
    return {
        "renderer_semantic_hash": repr_binding.renderer_semantic_hash,
        "lean_renderer_sha256": repr_binding.lean_renderer_sha256,
        "injected_helper_sha256": repr_binding.injected_helper_sha256,
        "python_module_sha256": repr_binding.python_sha256,
        "config_file_sha256": repr_binding.config_sha256,
        "implementation_set_hash": repr_binding.implementation_set_hash,
    }


def _verified_evidence_bundle(
    repo_root: Path,
    *,
    receipt_cases: Sequence[Mapping[str, Any]],
    config: SixGoalGateConfig,
) -> tuple[dict[str, tuple[Path, str]], dict[str, int]]:
    """Load the Git-local attempt-009 evidence bundle and replay all hash bindings."""

    if EXPECTED_EVIDENCE_BUNDLE_DIRECTORY.is_absolute():
        raise ValueError("SFT1 six-goal evidence bundle directory must be repository-relative")
    bundle_directory = (repo_root / EXPECTED_EVIDENCE_BUNDLE_DIRECTORY).resolve()
    if not bundle_directory.is_relative_to(repo_root.resolve()):
        raise ValueError("SFT1 six-goal evidence bundle escapes the repository")
    manifest_path = bundle_directory / "manifest.json"
    try:
        manifest_file_sha256 = hash_file(manifest_path)
    except OSError as exc:
        raise ValueError(f"SFT1 six-goal Git evidence bundle is unavailable: {exc}") from exc
    if manifest_file_sha256 != EXPECTED_EVIDENCE_BUNDLE_MANIFEST_FILE_SHA256:
        raise ValueError("SFT1 six-goal evidence bundle manifest hash mismatch")
    manifest = _strict_json_object(
        manifest_path,
        label="SFT1 six-goal evidence bundle manifest",
    )
    expected_manifest_fields = {
        "bundle_id",
        "cases",
        "execution_config_file_sha256",
        "execution_config_hash",
        "forbidden_rendered_substring_failure_classes",
        "gate_id",
        "helper_file_sha256",
        "helper_preamble_sha256",
        "live_adversarial_rejection_probes_per_forbidden_string",
        "receipt_file_sha256",
        "receipt_hash",
        "receipt_path",
        "schema_version",
        "source_attempt_id",
        "source_evidence_directory",
        "successful_case_evidence_claim",
        "totals",
    }
    if set(manifest) != expected_manifest_fields:
        raise ValueError("SFT1 six-goal evidence bundle manifest has an unexpected schema")
    exact_manifest = {
        "schema_version": 1,
        "bundle_id": "sft1_repr_six_real_goal_direct_expr_attempt_009_v0_3_1",
        "gate_id": config.gate_id,
        "source_attempt_id": "attempt_009",
        "source_evidence_directory": str(EXPECTED_EVIDENCE_DIRECTORY),
        "receipt_path": str(EXPECTED_RECEIPT_PATH),
        "receipt_file_sha256": EXPECTED_RECEIPT_FILE_SHA256,
        "receipt_hash": EXPECTED_RECEIPT_HASH,
        "execution_config_file_sha256": EXPECTED_EXECUTION_CONFIG_FILE_SHA256,
        "execution_config_hash": EXPECTED_EXECUTION_CONFIG_HASH,
        "helper_file_sha256": EXPECTED_HELPER_FILE_SHA256,
        "helper_preamble_sha256": EXPECTED_HELPER_PREAMBLE_SHA256,
        "successful_case_evidence_claim": "no_forbidden_rendered_residue_survived",
        "live_adversarial_rejection_probes_per_forbidden_string": False,
        "forbidden_rendered_substring_failure_classes": {
            "[anonymous]": "anonymous_binder_name",
            "⋯": "forbidden_rendered_placeholder",
        },
    }
    if any(manifest.get(field) != expected for field, expected in exact_manifest.items()):
        raise ValueError("SFT1 six-goal evidence bundle binding mismatch")

    manifest_cases = manifest["cases"]
    if not isinstance(manifest_cases, list) or len(manifest_cases) != len(receipt_cases):
        raise ValueError("SFT1 six-goal evidence bundle case count mismatch")
    case_paths: dict[str, tuple[Path, str]] = {}
    expected_case_fields = {
        "bundle_file",
        "bundle_file_sha256",
        "canonical_evidence_sha256",
        "case_id",
        "elapsed_ms",
        "ordinal",
        "request_hash",
    }
    for ordinal, (case_id, manifest_case, receipt_case) in enumerate(
        zip(EXPECTED_CASE_IDS, manifest_cases, receipt_cases, strict=True),
        start=1,
    ):
        manifest_case = _exact_object(
            manifest_case,
            expected_case_fields,
            label=f"{case_id} evidence bundle manifest case",
        )
        expected_filename = f"{ordinal:02d}_{case_id}.json"
        exact_case = {
            "ordinal": ordinal,
            "case_id": case_id,
            "bundle_file": expected_filename,
            "canonical_evidence_sha256": receipt_case.get("evidence_sha256"),
            "request_hash": receipt_case.get("request_hash"),
            "elapsed_ms": receipt_case.get("elapsed_ms"),
        }
        if any(manifest_case.get(field) != expected for field, expected in exact_case.items()):
            raise ValueError(f"{case_id} evidence bundle case binding mismatch")
        bundle_file_sha256 = _require_sha256(
            manifest_case["bundle_file_sha256"],
            label=f"{case_id} evidence bundle file hash",
        )
        bundle_path = (bundle_directory / expected_filename).resolve()
        if bundle_path.parent != bundle_directory:
            raise ValueError(f"{case_id} evidence bundle path escapes its directory")
        try:
            observed_file_sha256 = hash_file(bundle_path)
        except OSError as exc:
            raise ValueError(f"{case_id} Git evidence bundle file is unavailable: {exc}") from exc
        if observed_file_sha256 != bundle_file_sha256:
            raise ValueError(f"{case_id} Git evidence bundle file hash mismatch")
        case_paths[case_id] = (bundle_path, bundle_file_sha256)

    totals = _exact_object(
        manifest["totals"],
        {"case_count", "complete_sidecar_bytes", "elapsed_ms", "endpoint_count"},
        label="SFT1 six-goal evidence bundle totals",
    )
    expected_totals = {
        "case_count": 6,
        "endpoint_count": 12,
        "elapsed_ms": 21_546,
        "complete_sidecar_bytes": 119_895,
    }
    if totals != expected_totals:
        raise ValueError("SFT1 six-goal evidence bundle totals mismatch")
    return case_paths, expected_totals


def _verify_complete_sidecar(
    value: object,
    *,
    case: GateCase,
    config: SixGoalGateConfig,
    helper_preamble: str,
    endpoint_input: ClosedExprInput,
    expected_role: Literal["reference", "candidate"],
    expected_render_scope_id: str,
) -> tuple[str, str, str]:
    sidecar = _exact_object(
        value,
        {"compile_context", "record", "source_material"},
        label=f"{case.case_id} {expected_role} complete sidecar",
    )
    expected_context = build_compile_context(
        case,
        helper_preamble=helper_preamble,
    )
    if sidecar["compile_context"] != expected_context.canonical_payload():
        raise ValueError(f"{case.case_id} {expected_role} compile context mismatch")
    if sidecar["source_material"] != endpoint_input.source_material.to_dict():
        raise ValueError(f"{case.case_id} {expected_role} source material mismatch")

    record = _exact_object(
        sidecar["record"],
        {
            "compile_context_id",
            "endpoint_id",
            "endpoint_role",
            "goal_v1",
            "goal_v1_source",
            "implementation_identity",
            "provenance",
            "rendered_goal_hash",
            "renderer_version",
            "representation_id",
            "source_material_hash",
            "spec_hash",
            "typed_alpha_fingerprint",
            "warnings",
        },
        label=f"{case.case_id} {expected_role} closed-Expr record",
    )
    expected_endpoint_id = f"{case.case_id}.{expected_role}"
    exact_record: dict[str, object] = {
        "compile_context_id": expected_context.compile_context_id,
        "endpoint_id": expected_endpoint_id,
        "endpoint_role": expected_role,
        "goal_v1_source": "closed_prop_expr",
        "renderer_version": "goal_v1.0",
        "source_material_hash": endpoint_input.source_material.material_hash,
        "spec_hash": config.repr_binding.spec_hash,
        "typed_alpha_fingerprint": None,
        "warnings": [],
    }
    if any(record.get(field) != expected for field, expected in exact_record.items()):
        raise ValueError(f"{case.case_id} {expected_role} closed-Expr record binding mismatch")

    goal = record["goal_v1"]
    if not isinstance(goal, str) or not goal.strip() or goal.count("⊢") != 1:
        raise ValueError(f"{case.case_id} {expected_role} goal has a noncanonical turnstile")
    for forbidden in config.execution_contract.forbidden_render_substrings:
        if forbidden in goal:
            raise ValueError(
                f"{case.case_id} {expected_role} goal contains forbidden {forbidden!r}"
            )
    rendered_goal_hash = _require_sha256(
        record["rendered_goal_hash"],
        label=f"{case.case_id} {expected_role} rendered goal hash",
    )
    if rendered_goal_hash != sha256_hex(goal.encode("utf-8")):
        raise ValueError(f"{case.case_id} {expected_role} rendered goal hash does not replay")

    implementation_identity = _exact_object(
        record["implementation_identity"],
        set(_expected_implementation_identity(config)),
        label=f"{case.case_id} {expected_role} implementation identity",
    )
    if implementation_identity != _expected_implementation_identity(config):
        raise ValueError(f"{case.case_id} {expected_role} REPR implementation mismatch")

    provenance = _exact_object(
        record["provenance"],
        {
            "canonical_level_params",
            "expr_hash",
            "expr_hash_algorithm",
            "expr_origin",
            "input_level_params",
            "render_context_hash",
            "render_context_id",
            "render_scope_id",
            "route_id",
            "universe_profile_hash",
            "universe_profile_id",
        },
        label=f"{case.case_id} {expected_role} provenance",
    )
    expr_hash = _require_sha256(
        provenance["expr_hash"],
        label=f"{case.case_id} {expected_role} closed Expr hash",
    )
    exact_provenance = {
        "expr_hash_algorithm": "sha256_canonical_closed_expr_alpha_tree_v1",
        "expr_origin": endpoint_input.expr_origin,
        "render_context_hash": config.repr_binding.render_context_hash,
        "render_context_id": config.repr_binding.render_context_id,
        "render_scope_id": expected_render_scope_id,
        "route_id": config.repr_binding.route_id,
        "universe_profile_hash": config.repr_binding.universe_profile_hash,
        "universe_profile_id": config.repr_binding.universe_profile_id,
    }
    if any(provenance.get(field) != expected for field, expected in exact_provenance.items()):
        raise ValueError(f"{case.case_id} {expected_role} closed-Expr provenance mismatch")
    input_levels = provenance["input_level_params"]
    canonical_levels = provenance["canonical_level_params"]
    if (
        not isinstance(input_levels, list)
        or not all(isinstance(level, str) and level for level in input_levels)
        or len(input_levels) != len(set(input_levels))
        or not isinstance(canonical_levels, list)
        or canonical_levels != [f"u_{index}" for index in range(len(input_levels))]
    ):
        raise ValueError(f"{case.case_id} {expected_role} universe parameters are noncanonical")

    identity_payload = {
        "renderer_version": record["renderer_version"],
        "spec_hash": record["spec_hash"],
        "goal_v1_source": record["goal_v1_source"],
        "goal_v1": goal,
        "rendered_goal_hash": rendered_goal_hash,
        "endpoint_id": record["endpoint_id"],
        "endpoint_role": record["endpoint_role"],
        "source_material_hash": record["source_material_hash"],
        "compile_context_id": record["compile_context_id"],
        "provenance": provenance,
        "implementation_identity": implementation_identity,
    }
    if record["representation_id"] != "repr:" + hash_canonical(identity_payload):
        raise ValueError(f"{case.case_id} {expected_role} representation ID does not replay")
    return goal, expr_hash, rendered_goal_hash


def _verify_frozen_evidence(
    receipt_case: Mapping[str, Any],
    *,
    case: GateCase,
    config: SixGoalGateConfig,
    helper_preamble: str,
    evidence_path_override: Path | None = None,
    evidence_file_sha256: str | None = None,
) -> tuple[int, int]:
    receipt_case = _exact_object(
        dict(receipt_case),
        {"case_id", "elapsed_ms", "evidence_path", "evidence_sha256", "request_hash"},
        label=f"{case.case_id} receipt case",
    )
    if receipt_case["case_id"] != case.case_id:
        raise ValueError(f"{case.case_id} receipt case identity mismatch")
    _require_sha256(
        receipt_case["request_hash"],
        label=f"{case.case_id} receipt request hash",
    )
    _require_positive_int(
        receipt_case["elapsed_ms"],
        label=f"{case.case_id} receipt elapsed_ms",
    )
    expected_path = EXPECTED_EVIDENCE_DIRECTORY / f"{case.ordinal:02d}_{case.case_id}.json"
    raw_path = receipt_case.get("evidence_path")
    if not isinstance(raw_path, str) or Path(raw_path) != expected_path:
        raise ValueError(f"{case.case_id} evidence path differs from the frozen attempt")
    source_evidence_path = Path(raw_path)
    if source_evidence_path.parent != EXPECTED_EVIDENCE_DIRECTORY:
        raise ValueError(f"{case.case_id} evidence is outside the frozen evidence directory")
    expected_sha256 = _require_sha256(
        receipt_case.get("evidence_sha256"),
        label=f"{case.case_id} receipt evidence hash",
    )
    evidence_path = evidence_path_override or source_evidence_path
    if evidence_path.name != expected_path.name:
        raise ValueError(f"{case.case_id} replay evidence filename differs from the frozen case")
    try:
        observed_file_sha256 = hash_file(evidence_path)
    except OSError as exc:
        raise ValueError(f"{case.case_id} frozen evidence is unavailable: {exc}") from exc
    if evidence_file_sha256 is not None and observed_file_sha256 != evidence_file_sha256:
        raise ValueError(f"{case.case_id} frozen evidence file hash mismatch")

    payload = _strict_json_object(evidence_path, label=f"{case.case_id} frozen evidence")
    canonical_evidence = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if sha256_hex(canonical_evidence) != expected_sha256:
        raise ValueError(f"{case.case_id} canonical frozen evidence hash mismatch")
    if set(payload) != {
        "bindings",
        "case_id",
        "complete_sidecars",
        "endpoint_bindings",
        "gate_id",
        "measurements",
        "model_facing_projection",
        "reporting_dimensions",
        "representation_only_candidate",
        "request",
        "schema_version",
        "source",
    }:
        raise ValueError(f"{case.case_id} frozen evidence has an unexpected schema")
    if (
        payload["schema_version"] != 1
        or type(payload["schema_version"]) is not int
        or payload["gate_id"] != config.gate_id
        or payload["case_id"] != case.case_id
    ):
        raise ValueError(f"{case.case_id} frozen evidence identity mismatch")

    bindings = _exact_object(
        payload["bindings"],
        set(_expected_evidence_bindings(config)),
        label=f"{case.case_id} evidence bindings",
    )
    if bindings != _expected_evidence_bindings(config):
        raise ValueError(f"{case.case_id} evidence binding mismatch")
    if payload["source"] != _expected_source(case):
        raise ValueError(f"{case.case_id} evidence source binding mismatch")
    expected_reporting = {
        "source": case.source_family,
        "family": case.family_id,
        "operation": case.operation_id,
        "polarity": case.polarity,
        "exact_failure_class": None,
    }
    if payload["reporting_dimensions"] != expected_reporting:
        raise ValueError(f"{case.case_id} evidence reporting dimensions mismatch")
    expected_candidate = {
        "family": case.family_id,
        "operation": case.operation_id,
        "polarity": case.polarity,
        "production_admission": case.production_admission,
    }
    if payload["representation_only_candidate"] != expected_candidate:
        raise ValueError(f"{case.case_id} representation-only candidate binding mismatch")

    request = _exact_object(
        payload["request"],
        {"elapsed_ms", "raw_response_path", "render_scope_id", "request_hash"},
        label=f"{case.case_id} evidence request",
    )
    request_hash = _require_sha256(
        request["request_hash"], label=f"{case.case_id} evidence request hash"
    )
    elapsed_ms = _require_positive_int(
        request["elapsed_ms"], label=f"{case.case_id} evidence elapsed_ms"
    )
    if (
        request_hash != receipt_case.get("request_hash")
        or elapsed_ms != receipt_case.get("elapsed_ms")
        or request["render_scope_id"] != f"sft1-repr-six:{case.case_id}"
        or not isinstance(request["raw_response_path"], str)
        or not request["raw_response_path"]
    ):
        raise ValueError(f"{case.case_id} request binding differs from the receipt")

    complete_sidecars = payload["complete_sidecars"]
    if not isinstance(complete_sidecars, list) or len(complete_sidecars) != 2:
        raise ValueError(f"{case.case_id} must persist exactly two complete sidecars")
    endpoint_inputs = _inputs(case)
    reference = _verify_complete_sidecar(
        complete_sidecars[0],
        case=case,
        config=config,
        helper_preamble=helper_preamble,
        endpoint_input=endpoint_inputs[0],
        expected_role="reference",
        expected_render_scope_id=request["render_scope_id"],
    )
    candidate = _verify_complete_sidecar(
        complete_sidecars[1],
        case=case,
        config=config,
        helper_preamble=helper_preamble,
        endpoint_input=endpoint_inputs[1],
        expected_role="candidate",
        expected_render_scope_id=request["render_scope_id"],
    )
    reference_goal, reference_expr_hash, reference_render_hash = reference
    candidate_goal, candidate_expr_hash, candidate_render_hash = candidate
    if reference_goal == candidate_goal:
        raise ValueError(f"{case.case_id} reference and candidate render identically")

    expected_endpoint_bindings = {
        "reference_closed_expr_hash": reference_expr_hash,
        "candidate_closed_expr_hash": candidate_expr_hash,
        "reference_render_hash": reference_render_hash,
        "candidate_render_hash": candidate_render_hash,
    }
    if payload["endpoint_bindings"] != expected_endpoint_bindings:
        raise ValueError(f"{case.case_id} endpoint hash bindings do not replay")
    if payload["model_facing_projection"] != {
        "reference": reference_goal,
        "candidate": candidate_goal,
    }:
        raise ValueError(f"{case.case_id} model-facing projection is not sidecar.core_text()")

    measurements = _exact_object(
        payload["measurements"],
        {"complete_sidecar_bytes_per_pair", "lean_seconds"},
        label=f"{case.case_id} evidence measurements",
    )
    expected_sidecar_bytes = len(
        json.dumps(
            complete_sidecars,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    if (
        type(measurements["complete_sidecar_bytes_per_pair"]) is not int
        or measurements["complete_sidecar_bytes_per_pair"] != expected_sidecar_bytes
        or type(measurements["lean_seconds"]) not in {int, float}
        or measurements["lean_seconds"] != elapsed_ms / 1000.0
    ):
        raise ValueError(f"{case.case_id} evidence measurements do not replay")
    return elapsed_ms, measurements["complete_sidecar_bytes_per_pair"]


def _load_frozen_execution_config(repo_root: Path) -> LoadedConfig[SixGoalGateConfig]:
    execution_path = repo_root / EXPECTED_EXECUTION_CONFIG_PATH
    if hash_file(execution_path) != EXPECTED_EXECUTION_CONFIG_FILE_SHA256:
        raise ValueError("SFT1 six-goal execution config file hash mismatch")
    loaded: LoadedConfig[SixGoalGateConfig] = load_config(execution_path, SixGoalGateConfig)
    if loaded.config_hash != EXPECTED_EXECUTION_CONFIG_HASH:
        raise ValueError("SFT1 six-goal execution config semantic hash mismatch")
    if loaded.config.status != "pending_execution":
        raise ValueError("SFT1 six-goal execution artifact must remain pending_execution")
    return loaded


def _verify_frozen_receipt(
    repo_root: Path,
    config: SixGoalGateConfig,
    *,
    helper_preamble: str,
) -> None:
    execution = _load_frozen_execution_config(repo_root)
    for field_name in (
        "gate_id",
        "purpose",
        "authorization",
        "repr_binding",
        "fixed_preamble",
        "execution_contract",
        "cases",
    ):
        if getattr(execution.config, field_name) != getattr(config, field_name):
            raise ValueError(f"passed six-goal config drifts from execution field {field_name}")
    binding = config.receipt_binding
    assert binding.receipt_path is not None
    assert binding.receipt_file_sha256 is not None
    assert binding.regression_id is not None
    assert binding.receipt_hash is not None
    receipt_path = (repo_root / binding.receipt_path).resolve()
    if not receipt_path.is_relative_to(repo_root.resolve()):
        raise ValueError("SFT1 six-goal receipt path escapes the repository")
    if Path(binding.receipt_path) != EXPECTED_RECEIPT_PATH:
        raise ValueError("SFT1 six-goal receipt path differs from the typed freeze")
    observed_file_sha256 = hash_file(receipt_path)
    if (
        observed_file_sha256 != binding.receipt_file_sha256
        or observed_file_sha256 != EXPECTED_RECEIPT_FILE_SHA256
    ):
        raise ValueError("SFT1 six-goal receipt file hash mismatch")
    payload = _strict_json_object(receipt_path, label="SFT1 six-goal receipt")
    if set(payload) != {
        "cases",
        "gate_config_file_sha256",
        "gate_config_hash",
        "helper_file_sha256",
        "helper_preamble_sha256",
        "passed",
        "receipt_hash",
        "regression_id",
        "schema_version",
    }:
        raise ValueError("SFT1 six-goal receipt has an unexpected schema")
    receipt_core = dict(payload)
    observed_receipt_hash = receipt_core.pop("receipt_hash")
    if (
        observed_receipt_hash != hash_canonical(receipt_core)
        or observed_receipt_hash != binding.receipt_hash
        or observed_receipt_hash != EXPECTED_RECEIPT_HASH
    ):
        raise ValueError("SFT1 six-goal receipt semantic hash mismatch")
    cases = receipt_core.get("cases")
    if (
        not isinstance(cases, list)
        or any(not isinstance(case, dict) for case in cases)
        or tuple(case.get("case_id") for case in cases) != EXPECTED_CASE_IDS
    ):
        raise ValueError("SFT1 six-goal receipt case order differs from the gate")
    expected_case_fields = {
        "case_id",
        "elapsed_ms",
        "evidence_path",
        "evidence_sha256",
        "request_hash",
    }
    if any(
        not isinstance(case, dict)
        or set(case) != expected_case_fields
        or not isinstance(case["elapsed_ms"], int)
        or case["elapsed_ms"] <= 0
        or not isinstance(case["evidence_path"], str)
        or not case["evidence_path"]
        or not isinstance(case["evidence_sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", case["evidence_sha256"]) is None
        or not isinstance(case["request_hash"], str)
        or re.fullmatch(r"[0-9a-f]{64}", case["request_hash"]) is None
        for case in cases
    ):
        raise ValueError("SFT1 six-goal receipt case evidence is malformed")
    exact = {
        "schema_version": 1,
        "regression_id": binding.regression_id,
        "passed": True,
        "gate_config_file_sha256": EXPECTED_EXECUTION_CONFIG_FILE_SHA256,
        "gate_config_hash": EXPECTED_EXECUTION_CONFIG_HASH,
        "helper_file_sha256": EXPECTED_HELPER_FILE_SHA256,
        "helper_preamble_sha256": EXPECTED_HELPER_PREAMBLE_SHA256,
    }
    if any(receipt_core.get(field) != value for field, value in exact.items()):
        raise ValueError("SFT1 six-goal receipt bindings differ from the executed gate")
    if "094550" in receipt_path.read_text(encoding="utf-8"):
        raise ValueError("REPR ConsistencyCheck receipt cannot substitute for the SFT1 receipt")
    bundle_paths, bundle_totals = _verified_evidence_bundle(
        repo_root,
        receipt_cases=cases,
        config=config,
    )
    replayed_elapsed_ms = 0
    replayed_sidecar_bytes = 0
    for case, receipt_case in zip(config.cases, cases, strict=True):
        bundle_path, bundle_file_sha256 = bundle_paths[case.case_id]
        elapsed_ms, sidecar_bytes = _verify_frozen_evidence(
            receipt_case,
            case=case,
            config=config,
            helper_preamble=helper_preamble,
            evidence_path_override=bundle_path,
            evidence_file_sha256=bundle_file_sha256,
        )
        replayed_elapsed_ms += elapsed_ms
        replayed_sidecar_bytes += sidecar_bytes
    if (
        replayed_elapsed_ms != bundle_totals["elapsed_ms"]
        or replayed_sidecar_bytes != bundle_totals["complete_sidecar_bytes"]
    ):
        raise ValueError("SFT1 six-goal evidence bundle aggregate replay mismatch")


def load_six_goal_gate(path: Path | None = None) -> LoadedSixGoalGate:
    repo_root = find_repo_root(Path(__file__))
    config_path = repo_root / (path or DEFAULT_CONFIG_PATH)
    loaded: LoadedConfig[SixGoalGateConfig] = load_config(config_path, SixGoalGateConfig)
    config_file_sha256 = hash_file(config_path)
    helper_path = repo_root / loaded.config.fixed_preamble.source_path
    helper_source = helper_path.read_text(encoding="utf-8")
    helper_file_sha256 = hash_file(helper_path)
    helper_preamble = _helper_preamble(helper_source)
    helper_preamble_sha256 = sha256_hex(helper_preamble.encode("utf-8"))
    expected = loaded.config.fixed_preamble
    if helper_file_sha256 != expected.file_sha256:
        raise ValueError("SFT1 six-goal helper file hash mismatch")
    if helper_preamble_sha256 != expected.injected_preamble_sha256:
        raise ValueError("SFT1 six-goal injected preamble hash mismatch")
    if helper_file_sha256 != EXPECTED_HELPER_FILE_SHA256:
        raise ValueError("SFT1 six-goal helper differs from the typed loader freeze")
    if helper_preamble_sha256 != EXPECTED_HELPER_PREAMBLE_SHA256:
        raise ValueError("SFT1 six-goal preamble differs from the typed loader freeze")
    if loaded.config.status == "pending_execution":
        if config_path.resolve() != (repo_root / EXPECTED_EXECUTION_CONFIG_PATH).resolve():
            raise ValueError("SFT1 six-goal execution config path differs from the typed freeze")
        if config_file_sha256 != EXPECTED_EXECUTION_CONFIG_FILE_SHA256:
            raise ValueError("SFT1 six-goal execution config file hash mismatch")
        if loaded.config_hash != EXPECTED_EXECUTION_CONFIG_HASH:
            raise ValueError("SFT1 six-goal execution config semantic hash mismatch")
    else:
        if config_file_sha256 != EXPECTED_GATE_CONFIG_FILE_SHA256:
            raise ValueError("SFT1 six-goal config file differs from the typed loader freeze")
        if loaded.config_hash != EXPECTED_GATE_CONFIG_HASH:
            raise ValueError("SFT1 six-goal effective config differs from the typed loader freeze")
        _verify_frozen_receipt(
            repo_root,
            loaded.config,
            helper_preamble=helper_preamble,
        )
    return LoadedSixGoalGate(
        config=loaded.config,
        path=loaded.path,
        config_hash=loaded.config_hash,
        config_file_sha256=config_file_sha256,
        helper_path=helper_path,
        helper_file_sha256=helper_file_sha256,
        helper_preamble=helper_preamble,
        helper_preamble_sha256=helper_preamble_sha256,
    )


def _endpoint_ids(case_id: str) -> tuple[str, str]:
    reference = f"{case_id}.reference"
    candidate = f"{case_id}.candidate"
    if _ENDPOINT_ID.fullmatch(reference) is None or _ENDPOINT_ID.fullmatch(candidate) is None:
        raise ValueError(f"unsafe endpoint ID derived from {case_id!r}")
    return reference, candidate


def build_session_body(case: GateCase, *, render_scope_id: str) -> str:
    reference_id, candidate_id = _endpoint_ids(case.case_id)
    if case.reference.mode == ReferenceMode.IMPORTED_CONSTANT:
        assert case.reference.constant_name is not None
        source_line = (
            "  let sourceExpr ← "
            "LeanFaith.SFT1.RepresentationGate.importedTheoremType "
            f"``{case.reference.constant_name}"
        )
        source_origin = "loaded_constant_type"
    else:
        assert case.reference.proposition_text is not None
        source_line = (
            "  let sourceExpr ← "
            "LeanFaith.SFT1.RepresentationGate.elaborateReferenceProp\n"
            f"    (← `(term| {case.reference.proposition_text}))"
        )
        source_origin = "term_elaborated_proposition"
    body = f"""run_meta do
{source_line}
  LeanFaith.SFT1.RepresentationGate.assertP23BinderAllocatorHygiene sourceExpr
  let candidateExpr ←
    LeanFaith.SFT1.RepresentationGate.alphaRenameGateCandidate sourceExpr
  LeanFaith.GoalV1.emitClosedProp
    "{reference_id}" "{render_scope_id}" "{source_origin}" sourceExpr
  LeanFaith.GoalV1.emitClosedProp
    "{candidate_id}" "{render_scope_id}" "sft1_transformed_expr" candidateExpr"""
    if body.count("LeanFaith.GoalV1.emitClosedProp") != 2:
        raise AssertionError("six-goal body must explicitly unroll exactly two emitters")
    return body


def _inputs(case: GateCase) -> tuple[ClosedExprInput, ClosedExprInput]:
    reference_id, candidate_id = _endpoint_ids(case.case_id)
    if case.reference.mode == ReferenceMode.IMPORTED_CONSTANT:
        assert case.reference.raw_statement is not None
        origin: Literal["loaded_constant_type", "term_elaborated_proposition"] = (
            "loaded_constant_type"
        )
        source_material = ClosedExprSourceMaterial(
            kind="raw_statement",
            raw_statement=case.reference.raw_statement,
        )
    else:
        assert case.reference.proposition_text is not None
        origin = "term_elaborated_proposition"
        source_material = ClosedExprSourceMaterial(
            kind="proposition_text",
            proposition_text=case.reference.proposition_text,
        )
    return (
        ClosedExprInput(
            endpoint_id=reference_id,
            endpoint_role="reference",
            expr_origin=origin,
            source_material=source_material,
        ),
        ClosedExprInput(
            endpoint_id=candidate_id,
            endpoint_role="candidate",
            expr_origin="sft1_transformed_expr",
            source_material=ClosedExprSourceMaterial(
                kind="constructed_expr_no_source_text",
                absence_reason=(
                    "single explicit binder name changed structurally by the hash-bound "
                    "SFT1 gate helper"
                ),
            ),
        ),
    )


def build_compile_context(case: GateCase, *, helper_preamble: str) -> CompileContext:
    return CompileContext(
        project_id=case.compile_project_id,
        project_revision=case.compile_project_revision,
        lean_version=case.lean_version,
        import_header=case.import_header,
        command_preamble=helper_preamble,
        namespace_context=case.namespace_context,
        open_context=case.open_context,
        scoped_context=case.scoped_context,
        options=case.options,
    )


class _CapturingBackend:
    def __init__(self, delegate: LeanBackend) -> None:
        self.delegate = delegate
        self.last_result: LeanResult | None = None

    def run(self, request: LeanRequest) -> LeanResult:
        self.last_result = self.delegate.run(request)
        return self.last_result

    def run_batch(self, requests: Sequence[LeanRequest]) -> list[LeanResult]:
        raise AssertionError(
            f"six-goal direct Expr gate uses one pair request, not batch of {len(requests)}"
        )

    def close(self) -> None:
        return None


def _diagnostic_goals(result: LeanResult | None) -> dict[str, str]:
    goals: dict[str, str] = {}
    if result is None:
        return goals
    for message in result.messages:
        for line in str(message.get("data", "")).splitlines():
            marker = line.find(CLOSED_EXPR_MARKER)
            if marker < 0:
                continue
            try:
                payload = json.loads(line[marker + len(CLOSED_EXPR_MARKER) :])
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            endpoint_id = payload.get("endpoint_id")
            goal = payload.get("goal_v1")
            if isinstance(endpoint_id, str) and isinstance(goal, str):
                goals[endpoint_id] = goal
    return goals


def _classify_closed_expr_failures(failures: Sequence[ClosedExprFailure]) -> str:
    informative = [
        failure for failure in failures if "failed atomically" not in failure.detail.lower()
    ]
    selected = informative[0] if informative else failures[0]
    detail = selected.detail.lower()
    if "universe" in detail and ("mvar" in detail or "metavariable" in detail):
        return "universe_mvar"
    if "expr_mvar" in detail or "expression metavariable" in detail:
        return "expr_mvar"
    if "free_variable" in detail or "free variable" in detail:
        return "free_variable"
    if "loose_bound" in detail or "loose bound" in detail:
        return "loose_bound_variable"
    if "anonymous" in detail:
        return "anonymous_binder_name"
    if "⋯" in selected.detail or "rendered placeholder" in detail:
        return "forbidden_rendered_placeholder"
    if "not_prop" in detail or "not prop" in detail or "expected a proposition" in detail:
        return "non_prop"
    if any(
        marker in detail
        for marker in ("malformed", "ill-typed", "type mismatch", "failed to synthesize")
    ):
        return "ill_typed"
    if selected.endpoint_id.endswith(".candidate"):
        return "candidate_render_failure"
    return "reference_render_failure"


def _validate_sidecars(
    case: GateCase,
    sidecars: tuple[ClosedExprSidecar, ...],
    *,
    forbidden_substrings: tuple[str, ...],
) -> tuple[ClosedExprSidecar, ClosedExprSidecar]:
    reference_id, candidate_id = _endpoint_ids(case.case_id)
    by_id = {sidecar.record.endpoint_id: sidecar for sidecar in sidecars}
    if set(by_id) != {reference_id, candidate_id}:
        raise GateValidationError(
            "reference_render_failure",
            f"{case.case_id}: closed-Expr sidecar endpoints are incomplete",
        )
    reference = by_id[reference_id]
    candidate = by_id[candidate_id]
    if (
        reference.record.endpoint_role != "reference"
        or candidate.record.endpoint_role != "candidate"
    ):
        raise GateValidationError(
            "renderer_context_mismatch", f"{case.case_id}: endpoint roles are reversed"
        )
    reference_text = reference.core_text()
    candidate_text = candidate.core_text()
    for role, text in (("reference", reference_text), ("candidate", candidate_text)):
        if text.count("⊢") != 1:
            raise GateValidationError(
                "wrong_turnstile_count",
                f"{case.case_id}: {role} has a noncanonical turnstile count",
            )
        for forbidden in forbidden_substrings:
            if forbidden in text:
                exact_class = {
                    "[anonymous]": "anonymous_binder_name",
                    "⋯": "forbidden_rendered_placeholder",
                }[forbidden]
                raise GateValidationError(
                    exact_class,
                    f"{case.case_id}: {role} contains forbidden rendered substring {forbidden!r}",
                )
    if reference_text == candidate_text:
        raise GateValidationError(
            "required_distinct_render_collapsed",
            f"{case.case_id}: alpha candidate did not change model-facing text",
        )
    reference_provenance = reference.record.provenance
    candidate_provenance = candidate.record.provenance
    if (
        reference_provenance.universe_profile_id != candidate_provenance.universe_profile_id
        or reference_provenance.universe_profile_hash != candidate_provenance.universe_profile_hash
    ):
        raise GateValidationError(
            "universe_profile_mismatch",
            f"{case.case_id}: reference/candidate universe profile diverged",
        )
    if (
        reference_provenance.render_context_id != candidate_provenance.render_context_id
        or reference_provenance.render_context_hash != candidate_provenance.render_context_hash
        or reference.record.compile_context_id != candidate.record.compile_context_id
    ):
        raise GateValidationError(
            "renderer_context_mismatch",
            f"{case.case_id}: reference/candidate representation context diverged",
        )
    return reference, candidate


def _write_json_atomic(path: Path, payload: object) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    with temporary.open("wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return sha256_hex(encoded)


def _append_journal(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(encoded + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def run_six_goal_gate(
    backends: Mapping[str, LeanBackend],
    *,
    evidence_directory: Path,
    config_path: Path | None = None,
    timeout_seconds: float = 300.0,
) -> SixGoalGateResult:
    """Run only the approved six-pair gate against persistent caller backends."""

    loaded = load_six_goal_gate(config_path)
    if loaded.config.status == "passed":
        raise ValueError("SFT1 six-goal receipt is frozen; execution is closed")
    required_backends = {case.backend_id for case in loaded.config.cases}
    missing = required_backends - set(backends)
    if missing:
        raise ValueError(f"missing persistent gate backends: {sorted(missing)!r}")
    if timeout_seconds <= 0:
        raise ValueError("six-goal timeout must be positive")

    outcomes: list[GateCaseOutcome] = []
    journal_path = evidence_directory / "six_goal_gate.journal.jsonl"
    stopped_case_id: str | None = None
    forbidden = tuple(loaded.config.execution_contract.forbidden_render_substrings)
    for case in loaded.config.cases:
        render_scope_id = f"sft1-repr-six:{case.case_id}"
        context = build_compile_context(case, helper_preamble=loaded.helper_preamble)
        session_body = build_session_body(case, render_scope_id=render_scope_id)
        request_id = "sft1-repr-six:" + hash_canonical(
            {
                "gate_config_hash": loaded.config_hash,
                "helper_preamble_sha256": loaded.helper_preamble_sha256,
                "case_id": case.case_id,
                "compile_context_id": context.compile_context_id,
                "repr_implementation_set_hash": loaded.config.repr_binding.implementation_set_hash,
            }
        )
        capturing = _CapturingBackend(backends[case.backend_id])
        batch = render_closed_expr_in_session(
            capturing,
            inputs=_inputs(case),
            compile_context=context,
            render_scope_id=render_scope_id,
            session_body=session_body,
            request_id=request_id,
            timeout_seconds=timeout_seconds,
        )
        diagnostic_goals = _diagnostic_goals(capturing.last_result)
        failure_details = tuple(failure.detail for failure in batch.failures)
        exact_failure_class: str | None = (
            _classify_closed_expr_failures(batch.failures) if batch.failures else None
        )
        evidence_path: Path | None = None
        evidence_sha256: str | None = None
        if not failure_details:
            try:
                reference, candidate = _validate_sidecars(
                    case,
                    batch.sidecars,
                    forbidden_substrings=forbidden,
                )
            except GateValidationError as exc:
                failure_details = (str(exc),)
                exact_failure_class = exc.exact_failure_class
            else:
                evidence_path = evidence_directory / f"{case.ordinal:02d}_{case.case_id}.json"
                complete_sidecars = [reference.to_dict(), candidate.to_dict()]
                complete_sidecar_bytes = len(
                    json.dumps(
                        complete_sidecars,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
                evidence_payload = {
                    "schema_version": 1,
                    "gate_id": loaded.config.gate_id,
                    "case_id": case.case_id,
                    "reporting_dimensions": {
                        "source": case.source_family,
                        "family": case.family_id,
                        "operation": case.operation_id,
                        "polarity": case.polarity,
                        "exact_failure_class": None,
                    },
                    "representation_only_candidate": {
                        "family": case.family_id,
                        "operation": case.operation_id,
                        "polarity": case.polarity,
                        "production_admission": case.production_admission,
                    },
                    "source": {
                        "family": case.source_family,
                        "revision": case.source_revision,
                        "path": case.source_path,
                        "record": case.source_record,
                        "file_sha256": case.source_file_sha256,
                        "formal_statement_sha256": case.source_formal_statement_sha256,
                        "syntax_normalization_id": case.source_syntax_normalization_id,
                        "syntax_normalization_rule": case.source_syntax_normalization_rule,
                        "normalized_proposition_sha256": case.normalized_proposition_sha256,
                    },
                    "request": {
                        "request_hash": batch.request_hash,
                        "elapsed_ms": batch.elapsed_ms,
                        "raw_response_path": batch.raw_response_path,
                        "render_scope_id": batch.render_scope_id,
                    },
                    "bindings": {
                        "gate_config_hash": loaded.config_hash,
                        "gate_config_file_sha256": loaded.config_file_sha256,
                        "helper_file_sha256": loaded.helper_file_sha256,
                        "helper_preamble_sha256": loaded.helper_preamble_sha256,
                        "repr_freeze_commit": loaded.config.repr_binding.freeze_commit,
                        "repr_spec_hash": loaded.config.repr_binding.spec_hash,
                        "repr_implementation_set_hash": (
                            loaded.config.repr_binding.implementation_set_hash
                        ),
                        "renderer_semantic_hash": (
                            loaded.config.repr_binding.renderer_semantic_hash
                        ),
                        "renderer_api_hash": loaded.config.repr_binding.renderer_api_hash,
                        "universe_profile_id": loaded.config.repr_binding.universe_profile_id,
                        "universe_profile_hash": loaded.config.repr_binding.universe_profile_hash,
                        "render_context_id": loaded.config.repr_binding.render_context_id,
                        "render_context_hash": loaded.config.repr_binding.render_context_hash,
                    },
                    "endpoint_bindings": {
                        "reference_closed_expr_hash": reference.record.provenance.expr_hash,
                        "candidate_closed_expr_hash": candidate.record.provenance.expr_hash,
                        "reference_render_hash": reference.record.rendered_goal_hash,
                        "candidate_render_hash": candidate.record.rendered_goal_hash,
                    },
                    "complete_sidecars": complete_sidecars,
                    "model_facing_projection": {
                        "reference": reference.core_text(),
                        "candidate": candidate.core_text(),
                    },
                    "measurements": {
                        "lean_seconds": batch.elapsed_ms / 1000.0,
                        "complete_sidecar_bytes_per_pair": complete_sidecar_bytes,
                    },
                }
                evidence_sha256 = _write_json_atomic(evidence_path, evidence_payload)
        passed = not failure_details
        outcome = GateCaseOutcome(
            case_id=case.case_id,
            source=case.source_family,
            family=case.family_id,
            operation=case.operation_id,
            polarity=case.polarity,
            passed=passed,
            exact_failure_class=exact_failure_class,
            request_hash=batch.request_hash,
            elapsed_ms=batch.elapsed_ms,
            evidence_path=str(evidence_path) if evidence_path is not None else None,
            evidence_sha256=evidence_sha256,
            failure_details=failure_details,
            diagnostic_rendered_goals=diagnostic_goals,
        )
        outcomes.append(outcome)
        _append_journal(
            journal_path,
            {
                "schema_version": 1,
                "gate_id": loaded.config.gate_id,
                "case_id": case.case_id,
                "source": case.source_family,
                "family": case.family_id,
                "operation": case.operation_id,
                "polarity": case.polarity,
                "passed": passed,
                "exact_failure_class": exact_failure_class,
                "request_hash": batch.request_hash,
                "elapsed_ms": batch.elapsed_ms,
                "evidence_path": outcome.evidence_path,
                "evidence_sha256": outcome.evidence_sha256,
                "failure_details": list(failure_details),
                "diagnostic_rendered_goals": dict(sorted(diagnostic_goals.items())),
            },
        )
        if not passed:
            stopped_case_id = case.case_id
            break

    passed = len(outcomes) == len(loaded.config.cases) and all(
        outcome.passed for outcome in outcomes
    )
    return SixGoalGateResult(
        gate_id=loaded.config.gate_id,
        config_hash=loaded.config_hash,
        config_file_sha256=loaded.config_file_sha256,
        helper_file_sha256=loaded.helper_file_sha256,
        helper_preamble_sha256=loaded.helper_preamble_sha256,
        outcomes=tuple(outcomes),
        passed=passed,
        stopped_case_id=stopped_case_id,
        evidence_directory=str(evidence_directory),
    )


def freeze_passed_gate_receipt(result: SixGoalGateResult, path: Path) -> tuple[str, str]:
    """Freeze a receipt only after all six outcomes have durable passing evidence."""

    repo_root = find_repo_root(Path(__file__))
    execution = _load_frozen_execution_config(repo_root)
    if (
        not result.passed
        or tuple(outcome.case_id for outcome in result.outcomes) != EXPECTED_CASE_IDS
    ):
        raise ValueError("cannot freeze an incomplete or failed six-goal gate")
    if any(
        not outcome.passed
        or outcome.exact_failure_class is not None
        or outcome.evidence_path is None
        or outcome.evidence_sha256 is None
        or outcome.failure_details
        for outcome in result.outcomes
    ):
        raise ValueError("six-goal receipt requires six complete passing evidence records")
    exact_result_bindings = {
        "gate_id": execution.config.gate_id,
        "config_hash": EXPECTED_EXECUTION_CONFIG_HASH,
        "config_file_sha256": EXPECTED_EXECUTION_CONFIG_FILE_SHA256,
        "helper_file_sha256": EXPECTED_HELPER_FILE_SHA256,
        "helper_preamble_sha256": EXPECTED_HELPER_PREAMBLE_SHA256,
        "stopped_case_id": None,
        "evidence_directory": str(EXPECTED_EVIDENCE_DIRECTORY),
    }
    if any(getattr(result, field) != expected for field, expected in exact_result_bindings.items()):
        raise ValueError("six-goal result bindings differ from the frozen execution")
    helper_path = repo_root / execution.config.fixed_preamble.source_path
    if hash_file(helper_path) != EXPECTED_HELPER_FILE_SHA256:
        raise ValueError("six-goal helper differs from the frozen execution")
    helper_preamble = _helper_preamble(helper_path.read_text(encoding="utf-8"))
    if sha256_hex(helper_preamble.encode("utf-8")) != EXPECTED_HELPER_PREAMBLE_SHA256:
        raise ValueError("six-goal helper preamble differs from the frozen execution")
    result_receipt_cases = [
        {
            "case_id": outcome.case_id,
            "request_hash": outcome.request_hash,
            "elapsed_ms": outcome.elapsed_ms,
            "evidence_path": outcome.evidence_path,
            "evidence_sha256": outcome.evidence_sha256,
        }
        for outcome in result.outcomes
    ]
    bundle_paths, _ = _verified_evidence_bundle(
        repo_root,
        receipt_cases=result_receipt_cases,
        config=execution.config,
    )
    for case, outcome in zip(execution.config.cases, result.outcomes, strict=True):
        if (
            outcome.source != case.source_family
            or outcome.family != case.family_id
            or outcome.operation != case.operation_id
            or outcome.polarity != case.polarity
            or type(outcome.elapsed_ms) is not int
            or outcome.elapsed_ms <= 0
            or re.fullmatch(r"[0-9a-f]{64}", outcome.request_hash) is None
        ):
            raise ValueError(f"{case.case_id} outcome bindings differ from the execution config")
        receipt_case = result_receipt_cases[case.ordinal - 1]
        bundle_path, bundle_file_sha256 = bundle_paths[case.case_id]
        _verify_frozen_evidence(
            receipt_case,
            case=case,
            config=execution.config,
            helper_preamble=helper_preamble,
            evidence_path_override=bundle_path,
            evidence_file_sha256=bundle_file_sha256,
        )
    regression_id = result.gate_id
    core = {
        "schema_version": 1,
        "regression_id": regression_id,
        "gate_config_hash": result.config_hash,
        "gate_config_file_sha256": result.config_file_sha256,
        "helper_file_sha256": result.helper_file_sha256,
        "helper_preamble_sha256": result.helper_preamble_sha256,
        "cases": [
            {
                "case_id": outcome.case_id,
                "request_hash": outcome.request_hash,
                "elapsed_ms": outcome.elapsed_ms,
                "evidence_path": outcome.evidence_path,
                "evidence_sha256": outcome.evidence_sha256,
            }
            for outcome in result.outcomes
        ],
        "passed": True,
    }
    receipt_hash = hash_canonical(core)
    payload = {**core, "receipt_hash": receipt_hash}
    _write_json_atomic(path, payload)
    return regression_id, receipt_hash


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "EXPECTED_CASE_IDS",
    "EXPECTED_EVIDENCE_BUNDLE_DIRECTORY",
    "EXPECTED_EVIDENCE_BUNDLE_MANIFEST_FILE_SHA256",
    "EXPECTED_EVIDENCE_DIRECTORY",
    "EXPECTED_EXECUTION_CONFIG_FILE_SHA256",
    "EXPECTED_EXECUTION_CONFIG_HASH",
    "EXPECTED_EXECUTION_CONFIG_PATH",
    "EXPECTED_GATE_CONFIG_FILE_SHA256",
    "EXPECTED_GATE_CONFIG_HASH",
    "EXPECTED_HELPER_FILE_SHA256",
    "EXPECTED_HELPER_PREAMBLE_SHA256",
    "EXPECTED_RECEIPT_FILE_SHA256",
    "EXPECTED_RECEIPT_HASH",
    "EXPECTED_RECEIPT_PATH",
    "GateCase",
    "GateCaseOutcome",
    "LoadedSixGoalGate",
    "ReferenceMode",
    "SixGoalGateConfig",
    "SixGoalGateResult",
    "build_compile_context",
    "build_session_body",
    "freeze_passed_gate_receipt",
    "load_six_goal_gate",
    "run_six_goal_gate",
]
