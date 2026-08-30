"""Versioned ``goal_v1.0`` theorem representation.

The model view is deliberately not a Lean source language.  This module keeps
raw compilable source and its exact compilation context in a separate sidecar,
and exposes three forward routes through one Lean text renderer:

* an elaborated renderer that asks one already-loaded Lean backend to inspect a
  batch of ``ConstantInfo.type`` values; and
* a closed-Expr route that renders already-certified reference/candidate Exprs
  together in their existing Meta request; and
* a deterministic surface fallback for trusted theorem/lemma signatures.

There is intentionally no goal-to-declaration inverse.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cache
from itertools import pairwise
from pathlib import Path
from typing import Literal

from leanfaith.config.hashing import hash_canonical, sha256_hex
from leanfaith.config.paths import find_repo_root
from leanfaith.lean.protocol import LeanBackend, LeanRequest, LeanStatus

RENDERER_VERSION = "goal_v1.0"
GOAL_MARKER = "LFGOALV1JSON "
CLOSED_EXPR_MARKER = "LFGOALV1EXPRJSON "
SURFACE_PROVENANCE_TAG = "trusted_complete_parsed_signature"
SUPPORTED_DECLARATION_KINDS = frozenset({"theorem", "lemma"})
PINNED_LEAN_RENDERER_SHA256 = "4471262f812746046570c51dde5958ee33db31a450a6974071efce584ba56bc3"
PINNED_INJECTED_HELPER_SHA256 = "a6650452eebe683db295df1dfe925d3db8b03fc24e55cbc6793e838b5fe2f272"
CANONICAL_UNIVERSE_PROFILE: dict[str, object] = {
    "profile_id": "goal_v1_first_occurrence_u_i_v1",
    "route_scope": "closed_prop_expr_elaborated_named_and_supported_surface",
    "expr_input_stage": "after_instantiate_mvars_and_closed_prop_validation",
    "parameter_order": "Lean.collectLevelParams structural first occurrence",
    "expr_instantiation": "replace every used Level.param in collected order on every Expr render",
    "surface_policy": (
        "rewrite explicit simple Type/Sort level names by textual first occurrence; reject star, "
        "inferred, and compound surface level syntax"
    ),
    "canonical_name_template": "u_<zero_based_index>",
    "unused_declaration_parameters": "omitted",
}
CANONICAL_UNIVERSE_PROFILE_ID = str(CANONICAL_UNIVERSE_PROFILE["profile_id"])
CANONICAL_UNIVERSE_PROFILE_HASH = hash_canonical(CANONICAL_UNIVERSE_PROFILE)
RENDERER_SEMANTIC_PAYLOAD: dict[str, object] = {
    "hash_basis": "sha256_canonical_renderer_semantics_v1",
    "namespace": "LeanFaith.GoalV1",
    "signature": "renderClosedProp (e : Expr) : MetaM String",
    "named_delegate": ("renderConstantType (ci : ConstantInfo) := renderClosedProp ci.type"),
    "closed_expr_route": "closed_expr_in_session",
    "single_text_renderer": True,
    "state_policy": "withoutModifyingMCtx",
    "ambient_context_policy": "clear_local_context_and_local_instances_before_render",
    "transparency_policy": "Meta.TransparencyMode.default",
    "preparation_policy": "one instantiate_check_isProp_normalize pass per render or payload",
    "metadata_policy": "recursively_erase_before_check_hash_and_render",
    "exception_policy": "interrupt_and_runtime_exceptions_are_rethrown",
    "anonymous_binder_policy": (
        "open user-named outer Pis as locals; retain nondependent explicit anonymous or "
        "macro-scoped generated Pis as target arrows; reject dependent or nonexplicit truly "
        "anonymous outer Pis"
    ),
    "failure_codes": [
        "goal_v1_unresolved_expr_mvar",
        "goal_v1_unresolved_universe_mvar",
        "goal_v1_free_variable",
        "goal_v1_loose_bound_variable",
        "goal_v1_sorry_expr",
        "goal_v1_malformed_expr",
        "goal_v1_not_prop",
        "goal_v1_unsupported_anonymous_telescope_binder",
    ],
}
RENDERER_SEMANTIC_HASH = hash_canonical(RENDERER_SEMANTIC_PAYLOAD)
RENDER_CONTEXT_PAYLOAD: dict[str, object] = {
    "context_id": "goal_v1_render_context_v1",
    "renderer_semantic_hash": RENDERER_SEMANTIC_HASH,
    "universe_profile_id": CANONICAL_UNIVERSE_PROFILE_ID,
    "universe_profile_hash": CANONICAL_UNIVERSE_PROFILE_HASH,
    "telescope": "LeanFaith.GoalV1.withSupportedTelescope_nonreducing_v1",
    "anonymous_arrow_handling": "stop_named_telescope_and_preserve_target_arrow",
    "goal_printer": "Lean.Meta.ppGoal",
    "presentation_goal_kind": "syntheticOpaque_in_withoutModifyingMCtx",
    "ambient_local_context": "cleared_before_closed_expr_validation_and_rendering",
    "transparency": "Meta.TransparencyMode.default",
    "options": {
        "base": "Options.empty",
        "pp.universes": False,
        "pp.coercions": True,
        "pp.notation": True,
        "pp.mvars": False,
        "pp.inaccessibleNames": True,
        "pp.implementationDetailHyps": True,
    },
    "render_width": 1_000_000,
    "post_validator": "goal_v1_targeted_structural_v2",
}
RENDER_CONTEXT_ID = str(RENDER_CONTEXT_PAYLOAD["context_id"])
RENDER_CONTEXT_HASH = hash_canonical(RENDER_CONTEXT_PAYLOAD)
CLOSED_EXPR_HASH_ALGORITHM = "sha256_canonical_closed_expr_alpha_tree_v1"
CLOSED_EXPR_ROUTE_ID = "closed_expr_in_session"
CONSISTENCY_COVERAGE_RECEIPT: dict[str, object] = {
    "regression_id": "consistency_check_goal_field_1c6a6cca_goal_v1_v1",
    "dataset": "GuoxinChen/ConsistencyCheck",
    "revision": "1c6a6cca0f87b48d4cccb49946d3b8fc57a1eef9",
    "source_path": "consistency_check.jsonl",
    "source_file_sha256": "81cf6d9988625d84efbd8e1d6a0af4c234b2206da8350ee1d8bf547e612b1d47",
    "fixture_kind": "derived_goal_only_test_fixture",
    "upstream_field": "goal",
    "ordered_projection_fields": ["row_index", "name", "goal"],
    "fixture_encoding": "base64_of_gzip_mtime_zero_canonical_json",
    "fixture_file_sha256": "8fe6d82e11e3db07c9b6e9eee3c1983e034d50c4c0e4e3a56f90366ebe6b6149",
    "fixture_uncompressed_sha256": (
        "a0cf4ff5f74760712f7f526b87ee290781da036f97e22c3d122f8c4d9a2adf1f"
    ),
    "row_count": 859,
    "baseline_successes": 804,
    "baseline_failures": 55,
    "final_successes": 859,
    "final_failures": 0,
    "intended_layout_collapses": 9,
    "layout_collapse_names": [
        "imo_2006_p3",
        "exercise_2_13",
        "exercise_2_29",
        "exercise_4_15a",
        "exercise_13_4b1",
        "exercise_13_4b2",
        "exercise_13_6",
        "exercise_16_6",
        "exercise_28_5",
    ],
    "layout_collapse_names_hash": (
        "9fbdaba24144e28543bb08e548244bcb460bc67069bd3d8c8e4a9f2a3449b6af"
    ),
    "remaining_failure_classes": [],
    "targeted_syntax_families": [
        "absolute_and_cardinality_bars",
        "target_leading_absolute_bars",
        "factorial_postfix",
        "positive_nat_and_postfix_floor",
        "set_image_and_big_operator_primes",
        "quantified_big_operators",
        "inner_product_and_floor_delimiters",
        "generated_name_suffixes_and_proof_placeholders",
        "structure_literals",
    ],
}
CONSISTENCY_COVERAGE_RECEIPT_HASH = hash_canonical(CONSISTENCY_COVERAGE_RECEIPT)

# The YAML freeze duplicates this JSON-native payload byte-for-byte.  The
# literal SPEC_HASH is checked against it so downstream manifests have one
# stable value to pin without reading implementation details.
SPEC_PAYLOAD: dict[str, object] = {
    "representation_id": "goal_v1.0",
    "renderer_version": RENDERER_VERSION,
    "declaration_kinds": ["lemma", "theorem"],
    "grammar": {
        "local_line": "<one-or-more local names> : <Lean type>",
        "target_line": "⊢ <Lean proposition>",
        "turnstile_count": 1,
        "line_policy": "adjacent equal-type locals group; target is final",
        "term_binding_policy": (
            "complete semicolon-delimited term let/have bindings in local types or the target at "
            "any delimiter depth are serialized on one logical line; ambiguous or layout-only "
            "bindings fail closed"
        ),
        "binding_keyword_policy": (
            "surface let/have and elaborated have bindings canonicalize to let; raw source keeps "
            "its original spelling"
        ),
        "anonymous_arrow_policy": (
            "a nondependent explicit anonymous/macro-scoped generated Pi remains arrow notation in "
            "the target; surface arrow syntax is preserved, while truly anonymous "
            "implicit/instance or dependent Expr binders fail closed"
        ),
        "binding_head_policy": (
            "surface binding heads require one original-token plain or guillemet explicit "
            "non-pattern name; elaborated and final validation additionally admit Lean's printed "
            "inaccessible-name suffix; an optional type annotation is nonempty with no second "
            "top-level colon, and reserved/literal/composite or incomplete heads fail closed"
        ),
        "local_name_policy": (
            "surface binders require original-token plain or guillemet explicit names; elaborated "
            "and final local lines additionally admit Lean's printed inaccessible-name suffix; "
            "reserved/literal/composite names fail closed"
        ),
        "forall_binder_type_policy": (
            "a bare top-level forall binder is accepted only when its comma boundary is unique; "
            "otherwise the binder must be parenthesized so nested quantifier and target commas "
            "cannot be mistaken for the binder boundary"
        ),
        "fragment_completion_policy": (
            "the containing expression plus each binding type, value, and body rejects dangling "
            "commas/operators and incomplete if/fun/quantifier/show introducers at every balanced "
            "delimiter depth; by/do/calc/match fragments fail closed"
        ),
        "literal_policy": (
            "strings, raw strings, guillemets, and Char tokens in types/values/bodies are "
            "opaque to delimiter analysis and preserved byte-for-byte during layout whitespace "
            "normalization; "
            "binding heads retain original token kind so literals cannot masquerade as names"
        ),
        "delimiter_policy": (
            "parentheses, braces, brackets, and constructor angles are balanced structurally; "
            "binding := and ; cannot belong to compound delimiter/operator runs; dangling "
            "operators fail closed while supported atomic symbol terms remain values"
        ),
        "named_argument_policy": (
            "outside a claimed let/have binding, := is accepted only as a complete simple "
            "parenthesized named argument (name := value) or as one field of a complete "
            "comma-delimited structure literal; every other nested assignment fails closed"
        ),
        "targeted_token_policy": (
            "paired bars have nonempty unpadded interiors, brace-local set builders have exactly "
            "one unambiguous separator, compound bar operators are unsupported and fail closed, "
            "set image '' "
            "is whitespace-delimited with complete operands, factorial/positive-Nat/floor "
            "postfixes are "
            "narrowly recognized, floor/ceiling interiors are nonempty, and ⟪u,v⟫ has exactly "
            "two nonempty top-level operands"
        ),
        "surface_input_policy": (
            "surface rendering requires nonempty raw_statement plus caller-supplied "
            "parsed_signature; supplying it attests that the signature is complete, corresponds "
            "to compilable raw source in the stored context, and is safe for the bounded grammar; "
            "raw_statement is never parsed to guess a signature or proof boundary because loaded "
            "syntax makes top-level := ambiguous"
        ),
        "surface_provenance_tag": SURFACE_PROVENANCE_TAG,
        "surface_provenance_policy": (
            "each successful surface sidecar stores surface_provenance_tag in record.warnings as "
            "a caller-attestation marker that does not verify the attested claims; failures "
            "produce no sidecar or provenance tag"
        ),
    },
    "preserve": [
        "local_order",
        "local_names",
        "dependent_types",
        "generated_instance_names_when_elaborated",
        "coercions",
        "notation_except_term_binding_have_canonicalized_to_let",
        "universes_in_types",
    ],
    "remove": [
        "attributes",
        "declaration_keyword",
        "declaration_name",
        "command_shell",
        "imports",
        "options",
        "comments",
        "proof_delimiter",
        "proof_body",
    ],
    "sources": ["closed_prop_expr", "elaborated", "surface"],
    "surface_fail_closed_classes": [
        "anonymous_instance_binder",
        "duplicate_or_shadowed_local_name",
        "implicit_or_untyped_binder",
        "ambiguous_declaration_or_proof_boundary",
        "incomplete_or_ambiguous_term_binding",
        "assignment_syntax_outside_bounded_term_bindings",
        "missing_trusted_complete_parsed_signature",
        "raw_declaration_boundary_inference_forbidden",
        "syntax_quotation",
        "unsupported_declaration_kind",
    ],
    "compile_context_fields": [
        "schema_version",
        "project_id",
        "project_revision",
        "lean_version",
        "import_header",
        "command_preamble",
        "namespace_context",
        "open_context",
        "scoped_context",
        "options",
    ],
    "compile_context_application_order": [
        "import_header",
        "command_preamble",
        "options",
        "open_context",
        "scoped_context",
        "namespace_context",
    ],
    "elaborated_input_modes": ["inline_candidate", "loaded_constant_lookup"],
    "closed_expr_route": {
        "route_id": CLOSED_EXPR_ROUTE_ID,
        "input_mode": "closed_prop_expr",
        "expr_origin_modes": [
            "loaded_constant_type",
            "term_elaborated_proposition",
            "sft1_transformed_expr",
        ],
        "origin_source_material": {
            "loaded_constant_type": "raw_statement",
            "term_elaborated_proposition": "proposition_text",
            "sft1_transformed_expr": "constructed_expr_no_source_text",
        },
        "same_meta_request": True,
        "text_renderer": "LeanFaith.GoalV1.renderClosedProp",
        "declaration_or_proof_creation": "forbidden",
        "meta_action_command_policy": (
            "begin with the sole run_meta command and contain no declaration command; static "
            "project or helper setup belongs only in the hash-bound compile context"
        ),
        "compile_context_proof_declarations": "forbidden",
        "kernel_preparation_passes_per_endpoint": 1,
        "surface_or_text_reelaboration": "forbidden",
        "python_expr_transport": "forbidden",
    },
    "closed_expr_rejection_classes": [
        "unresolved_expr_mvar",
        "unresolved_universe_mvar",
        "free_variable",
        "loose_bound_variable",
        "sorry_expr",
        "malformed_expr",
        "non_prop",
        "unsupported_anonymous_telescope_binder",
    ],
    "closed_expr_hash": {
        "algorithm": CLOSED_EXPR_HASH_ALGORITHM,
        "preimage": "canonical JSON of the validated universe-normalized alpha Expr tree",
        "binder_names": "excluded",
        "binder_info": "retained",
        "mdata": "transparent",
        "rendered_goal_hash": "recorded separately to bind presentation names",
    },
    "canonical_universe_profile": CANONICAL_UNIVERSE_PROFILE,
    "canonical_universe_profile_hash": CANONICAL_UNIVERSE_PROFILE_HASH,
    "renderer_semantic_contract": RENDERER_SEMANTIC_PAYLOAD,
    "renderer_semantic_hash": RENDERER_SEMANTIC_HASH,
    "render_context": RENDER_CONTEXT_PAYLOAD,
    "render_context_hash": RENDER_CONTEXT_HASH,
    "closed_expr_source_material": {
        "raw_statement": "exact declaration bytes when a declaration exists",
        "proposition_text": (
            "exact caller-retained proposition text when no declaration exists; audit-only and "
            "never used for rendering or elaboration"
        ),
        "constructed_expr_no_source_text": (
            "both text fields absent with an explicit reason for structurally constructed Exprs"
        ),
        "inverse_from_goal_text": "forbidden",
    },
    "implementation_identity_fields": [
        "renderer_semantic_hash",
        "lean_renderer_sha256",
        "injected_helper_sha256",
        "python_module_sha256",
        "config_file_sha256",
        "implementation_set_hash",
    ],
    "lean_emitted_post_validation": {
        "routes": ["closed_prop_expr", "elaborated"],
        "pipeline": ["_canonicalize_elaborated_goal", "validate_goal_v1"],
        "bypass": "forbidden",
        "closed_expr_pair_failure": "atomic",
    },
    "elaborated_post_validator_coverage": {
        **CONSISTENCY_COVERAGE_RECEIPT,
        "receipt_hash": CONSISTENCY_COVERAGE_RECEIPT_HASH,
    },
    "sorry_policy": (
        "VALID_WITH_SORRY, a nonempty sorries payload, or a warning/error containing "
        '"declaration uses `sorry`" or "declaration uses \'sorry\'" fails the batch unless '
        "allow_sorry is true"
    ),
    "inverse": "forbidden",
    "elaborated_option_profile": {
        "base": "Options.empty",
        "pp.universes": False,
        "pp.coercions": True,
        "pp.notation": True,
        "pp.mvars": False,
        "pp.inaccessibleNames": True,
        "pp.implementationDetailHyps": True,
        "render_width": 1000000,
    },
}

# Filled once from hash_canonical(SPEC_PAYLOAD), then protected by tests.
SPEC_HASH = "68d893a2c566bf3f6a82c899a32a351f9a5420f5ea98168c99b887aaa01a45a8"

CompileOptionValue = str | int | float | bool
GoalV1Source = Literal["closed_prop_expr", "elaborated", "surface"]
ClosedExprOrigin = Literal[
    "loaded_constant_type",
    "term_elaborated_proposition",
    "sft1_transformed_expr",
]
ClosedExprEndpointRole = Literal["reference", "candidate"]
ClosedExprSourceMaterialKind = Literal[
    "raw_statement",
    "proposition_text",
    "constructed_expr_no_source_text",
]


class GoalV1Error(ValueError):
    """Base class for deterministic representation failures."""


class SurfaceFailureCode(StrEnum):
    UNSUPPORTED_DECLARATION_KIND = "unsupported_declaration_kind"
    DECLARATION_NOT_FOUND = "declaration_not_found"
    AMBIGUOUS_DECLARATION = "ambiguous_declaration"
    DECLARATION_KIND_MISMATCH = "declaration_kind_mismatch"
    MISSING_DECLARATION_NAME = "missing_declaration_name"
    AMBIGUOUS_PROOF_BOUNDARY = "ambiguous_proof_boundary"
    UNBALANCED_DELIMITER = "unbalanced_delimiter"
    MISSING_TARGET_SEPARATOR = "missing_target_separator"
    EMPTY_TARGET = "empty_target"
    UNTYPED_BINDER = "untyped_binder"
    ANONYMOUS_INSTANCE_BINDER = "anonymous_instance_binder"
    DUPLICATE_LOCAL_NAME = "duplicate_or_shadowed_local_name"
    ANONYMOUS_TOP_LEVEL_ARROW = "anonymous_top_level_arrow"
    SYNTAX_QUOTATION = "syntax_quotation"
    INVALID_GOAL = "invalid_goal"


class SurfaceRenderError(GoalV1Error):
    """A fail-closed surface rendering outcome with a stable code."""

    def __init__(self, code: SurfaceFailureCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class CompileContext:
    """Exact non-model context needed to compile the retained raw source."""

    project_id: str
    project_revision: str
    lean_version: str
    import_header: str
    command_preamble: str = ""
    namespace_context: tuple[str, ...] = ()
    open_context: tuple[str, ...] = ()
    scoped_context: tuple[str, ...] = ()
    options: Mapping[str, CompileOptionValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("project_id", "project_revision", "lean_version"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"compile context {field_name} must be nonempty")
        if not self.import_header.strip():
            raise ValueError("compile context import_header must be nonempty")
        for line in self.import_header.splitlines():
            stripped = line.strip()
            import_pattern = r"(?:(?:public|meta)\s+)*import\s+\S+(?:\s+\S+)*"
            if stripped and not re.fullmatch(import_pattern, stripped):
                raise ValueError(
                    "compile context import_header accepts import commands only; "
                    "put other commands in structured fields or command_preamble"
                )
        for field_name in ("namespace_context", "open_context", "scoped_context"):
            for name in getattr(self, field_name):
                if not name.strip() or any(char.isspace() for char in name):
                    raise ValueError(f"{field_name} entries must be nonempty Lean names")
        for option_name, value in self.options.items():
            if not option_name.strip() or any(char.isspace() for char in option_name):
                raise ValueError("compile option names must be nonempty and contain no whitespace")
            if not isinstance(value, (str, int, float, bool)):
                raise TypeError(f"compile option {option_name!r} has unsupported value {value!r}")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "project_id": self.project_id,
            "project_revision": self.project_revision,
            "lean_version": self.lean_version,
            "import_header": self.import_header,
            "command_preamble": self.command_preamble,
            "namespace_context": list(self.namespace_context),
            "open_context": list(self.open_context),
            "scoped_context": list(self.scoped_context),
            "options": dict(sorted(self.options.items())),
        }

    @property
    def fingerprint(self) -> str:
        return hash_canonical(self.canonical_payload())

    @property
    def compile_context_id(self) -> str:
        return f"ctx:{self.fingerprint}"


@dataclass(frozen=True, slots=True)
class RendererImplementationIdentity:
    renderer_semantic_hash: str
    lean_renderer_sha256: str
    injected_helper_sha256: str
    python_module_sha256: str
    config_file_sha256: str
    implementation_set_hash: str

    def to_dict(self) -> dict[str, str]:
        return {
            "renderer_semantic_hash": self.renderer_semantic_hash,
            "lean_renderer_sha256": self.lean_renderer_sha256,
            "injected_helper_sha256": self.injected_helper_sha256,
            "python_module_sha256": self.python_module_sha256,
            "config_file_sha256": self.config_file_sha256,
            "implementation_set_hash": self.implementation_set_hash,
        }


@dataclass(frozen=True, slots=True)
class GoalV1Record:
    representation_id: str
    goal_v1: str
    goal_v1_source: GoalV1Source
    renderer_version: str
    spec_hash: str
    raw_statement_hash: str
    declaration_kind: str
    compile_context_id: str
    implementation_identity: RendererImplementationIdentity
    typed_alpha_fingerprint: str | None = None
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "representation_id": self.representation_id,
            "goal_v1": self.goal_v1,
            "goal_v1_source": self.goal_v1_source,
            "renderer_version": self.renderer_version,
            "spec_hash": self.spec_hash,
            "raw_statement_hash": self.raw_statement_hash,
            "declaration_kind": self.declaration_kind,
            "compile_context_id": self.compile_context_id,
            "implementation_identity": self.implementation_identity.to_dict(),
            "typed_alpha_fingerprint": self.typed_alpha_fingerprint,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class GoalV1Sidecar:
    """Joinable metadata plus raw source; only ``record.goal_v1`` is model-facing."""

    record: GoalV1Record
    raw_statement: str
    compile_context: CompileContext

    def __post_init__(self) -> None:
        raw_hash = sha256_hex(self.raw_statement.encode("utf-8"))
        if raw_hash != self.record.raw_statement_hash:
            raise ValueError("raw_statement does not match raw_statement_hash")
        if self.compile_context.compile_context_id != self.record.compile_context_id:
            raise ValueError("compile_context does not match compile_context_id")

    def to_dict(self) -> dict[str, object]:
        return {
            "record": self.record.to_dict(),
            "raw_statement": self.raw_statement,
            "compile_context": self.compile_context.canonical_payload(),
        }

    def core_text(self) -> str:
        """The only field downstream pair rows copy from this sidecar."""

        return self.record.goal_v1


@dataclass(frozen=True, slots=True)
class ElaboratedInput:
    declaration_name: str
    declaration_kind: str
    raw_statement: str
    typed_alpha_fingerprint: str | None = None
    lookup_only: bool = False


@dataclass(frozen=True, slots=True)
class ElaboratedFailure:
    declaration_name: str
    detail: str


@dataclass(frozen=True, slots=True)
class ElaboratedBatchResult:
    sidecars: tuple[GoalV1Sidecar, ...]
    failures: tuple[ElaboratedFailure, ...]
    request_hash: str
    elapsed_ms: int
    raw_response_path: str | None


@dataclass(frozen=True, slots=True)
class ClosedExprSourceMaterial:
    kind: ClosedExprSourceMaterialKind
    raw_statement: str | None = None
    proposition_text: str | None = None
    absence_reason: str | None = None

    def __post_init__(self) -> None:
        values = {
            "raw_statement": self.raw_statement,
            "proposition_text": self.proposition_text,
            "absence_reason": self.absence_reason,
        }
        expected = {
            "raw_statement": "raw_statement",
            "proposition_text": "proposition_text",
            "constructed_expr_no_source_text": "absence_reason",
        }[self.kind]
        expected_value = values[expected]
        if (
            not isinstance(expected_value, str)
            or not expected_value.strip()
            or any(value is not None for name, value in values.items() if name != expected)
        ):
            raise ValueError(
                f"closed Expr source material {self.kind!r} requires only nonempty {expected}"
            )

    def to_dict(self) -> dict[str, str | None]:
        return {
            "kind": self.kind,
            "raw_statement": self.raw_statement,
            "proposition_text": self.proposition_text,
            "absence_reason": self.absence_reason,
        }

    @property
    def material_hash(self) -> str:
        return hash_canonical(self.to_dict())


@dataclass(frozen=True, slots=True)
class ClosedExprInput:
    endpoint_id: str
    endpoint_role: ClosedExprEndpointRole
    expr_origin: ClosedExprOrigin
    source_material: ClosedExprSourceMaterial
    typed_alpha_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if not self.endpoint_id.strip():
            raise ValueError("closed Expr endpoint_id must be nonempty")
        allowed_material = {
            "loaded_constant_type": {"raw_statement"},
            "term_elaborated_proposition": {"proposition_text"},
            "sft1_transformed_expr": {"constructed_expr_no_source_text"},
        }[self.expr_origin]
        if self.source_material.kind not in allowed_material:
            raise ValueError(
                f"closed Expr origin {self.expr_origin!r} cannot use source material "
                f"{self.source_material.kind!r}"
            )


@dataclass(frozen=True, slots=True)
class ClosedExprProvenance:
    expr_hash: str
    expr_hash_algorithm: str
    input_level_params: tuple[str, ...]
    canonical_level_params: tuple[str, ...]
    universe_profile_id: str
    universe_profile_hash: str
    render_scope_id: str
    render_context_id: str
    render_context_hash: str
    route_id: str
    expr_origin: ClosedExprOrigin

    def to_dict(self) -> dict[str, object]:
        return {
            "expr_hash": self.expr_hash,
            "expr_hash_algorithm": self.expr_hash_algorithm,
            "input_level_params": list(self.input_level_params),
            "canonical_level_params": list(self.canonical_level_params),
            "universe_profile_id": self.universe_profile_id,
            "universe_profile_hash": self.universe_profile_hash,
            "render_scope_id": self.render_scope_id,
            "render_context_id": self.render_context_id,
            "render_context_hash": self.render_context_hash,
            "route_id": self.route_id,
            "expr_origin": self.expr_origin,
        }


@dataclass(frozen=True, slots=True)
class ClosedExprRecord:
    representation_id: str
    goal_v1: str
    goal_v1_source: Literal["closed_prop_expr"]
    renderer_version: str
    spec_hash: str
    compile_context_id: str
    endpoint_id: str
    endpoint_role: ClosedExprEndpointRole
    source_material_hash: str
    rendered_goal_hash: str
    provenance: ClosedExprProvenance
    implementation_identity: RendererImplementationIdentity
    typed_alpha_fingerprint: str | None = None
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "representation_id": self.representation_id,
            "goal_v1": self.goal_v1,
            "goal_v1_source": self.goal_v1_source,
            "renderer_version": self.renderer_version,
            "spec_hash": self.spec_hash,
            "compile_context_id": self.compile_context_id,
            "endpoint_id": self.endpoint_id,
            "endpoint_role": self.endpoint_role,
            "source_material_hash": self.source_material_hash,
            "rendered_goal_hash": self.rendered_goal_hash,
            "provenance": self.provenance.to_dict(),
            "implementation_identity": self.implementation_identity.to_dict(),
            "typed_alpha_fingerprint": self.typed_alpha_fingerprint,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class ClosedExprSidecar:
    record: ClosedExprRecord
    source_material: ClosedExprSourceMaterial
    compile_context: CompileContext

    def __post_init__(self) -> None:
        if self.source_material.material_hash != self.record.source_material_hash:
            raise ValueError("closed Expr source material does not match its hash")
        if self.compile_context.compile_context_id != self.record.compile_context_id:
            raise ValueError("closed Expr compile context does not match its ID")

    def to_dict(self) -> dict[str, object]:
        return {
            "record": self.record.to_dict(),
            "source_material": self.source_material.to_dict(),
            "compile_context": self.compile_context.canonical_payload(),
        }

    def core_text(self) -> str:
        return self.record.goal_v1


@dataclass(frozen=True, slots=True)
class ClosedExprFailure:
    endpoint_id: str
    detail: str


@dataclass(frozen=True, slots=True)
class ClosedExprBatchResult:
    sidecars: tuple[ClosedExprSidecar, ...]
    failures: tuple[ClosedExprFailure, ...]
    request_hash: str
    elapsed_ms: int
    raw_response_path: str | None
    render_scope_id: str


@dataclass(frozen=True, slots=True)
class _Binder:
    names: tuple[str, ...]
    type_text: str


@dataclass(frozen=True, slots=True)
class _MaskedSource:
    text: str
    masked: str


@dataclass(frozen=True, slots=True)
class _BindingToken:
    keyword: str
    start: int
    end: int
    context: tuple[int, ...]


_FORALL = re.compile(r"^(?:∀|forall)\s+")
_BINDING_KEYWORD = re.compile(r"(?<![\w'.])\b(let|have)\b(?![\w'])")
_UNSUPPORTED_BINDING_RHS = re.compile(r"^(?:by|do|match|calc)\b")
_SYNTAX_QUOTATION = re.compile(r"`")
_SORRY_DIAGNOSTICS = ("declaration uses `sorry`", "declaration uses 'sorry'")
_ASCII_OPERATOR_CHARS = frozenset("!#$%&*+-./:;<=>?@\\^|~")
_ATOMIC_SYMBOL_TERMS = frozenset({"\u22a4", "\u22a5", "\u2205", "\u221e"})
_RESERVED_BINDING_NAMES = frozenset(
    {
        "abbrev",
        "axiom",
        "by",
        "calc",
        "class",
        "def",
        "deriving",
        "do",
        "else",
        "end",
        "example",
        "export",
        "extends",
        "for",
        "forall",
        "from",
        "fun",
        "have",
        "if",
        "import",
        "in",
        "include",
        "inductive",
        "infix",
        "infixl",
        "infixr",
        "instance",
        "let",
        "macro",
        "match",
        "mut",
        "mutual",
        "namespace",
        "notation",
        "opaque",
        "open",
        "partial",
        "private",
        "protected",
        "public",
        "rec",
        "return",
        "section",
        "set_option",
        "show",
        "structure",
        "syntax",
        "termination_by",
        "theorem",
        "then",
        "universe",
        "variable",
        "where",
        "with",
    }
)
_GENERATED_NAME_SUFFIX = re.compile(r"(?:✝[⁰¹²³⁴⁵⁶⁷⁸⁹]*)+")
_QUANTIFIER_TOKEN = re.compile(
    r"(?<![\w'.])(?:∃|Σ|∀|forall\b|∑'(?![\w'])|∏'(?![\w'])|∑|∏|\u22c3(?=\s)|\u22c2(?=\s))"
)
_CONDITIONAL_TOKEN = re.compile(r"(?<![\w'.])\b(if|then|else)\b(?![\w'])")
_FUN_TOKEN = re.compile(r"(?<![\w'.])\bfun\b(?![\w'])")
_SHOW_FROM_TOKEN = re.compile(r"(?<![\w'.])\b(show|from)\b(?![\w'])")
_UNSUPPORTED_LAYOUT_TOKEN = re.compile(r"(?<![\w'.])\b(by|calc|do|match)\b(?![\w'])")
_BARE_INCOMPLETE_TERMS = frozenset(
    {
        "by",
        "calc",
        "do",
        "else",
        "forall",
        "from",
        "fun",
        "have",
        "if",
        "in",
        "let",
        "match",
        "return",
        "show",
        "then",
        "where",
        "with",
    }
)

_DELIMITER_PAIRS = {
    "(": ")",
    "{": "}",
    "[": "]",
    "⟨": "⟩",
    "⟪": "⟫",
    "⌊": "⌋",
    "⌈": "⌉",
}
_CLOSING_DELIMITERS = frozenset(_DELIMITER_PAIRS.values())


def _single_quoted_literal_end(text: str, start: int) -> int | None:
    """Return the end of a conservative Lean character literal, if present."""

    if text[start] != "'":
        return None
    if start > 0 and (text[start - 1].isalnum() or text[start - 1] in "_'"):
        return None
    cursor = start + 1
    escaped = False
    while cursor < len(text) and text[cursor] != "\n":
        char = text[cursor]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "'":
            body = text[start + 1 : cursor]
            if len(body) == 1 or body.startswith("\\"):
                return cursor
            return None
        cursor += 1
    return None


def _raw_string_literal_end(text: str, start: int) -> int | None:
    """Return the end of a Lean ``r#\"...\"#`` literal, or ``None`` when absent."""

    if text[start] != "r":
        return None
    if start > 0 and (text[start - 1].isalnum() or text[start - 1] in "_'"):
        return None
    opening_quote = start + 1
    while opening_quote < len(text) and text[opening_quote] == "#":
        opening_quote += 1
    hash_count = opening_quote - start - 1
    if opening_quote >= len(text) or text[opening_quote] != '"':
        return None
    closing = '"' + "#" * hash_count
    closing_start = text.find(closing, opening_quote + 1)
    if closing_start < 0:
        raise SurfaceRenderError(
            SurfaceFailureCode.UNBALANCED_DELIMITER,
            "unterminated raw string literal",
        )
    return closing_start + len(closing) - 1


def _is_operator_symbol(char: str) -> bool:
    return char in _ASCII_OPERATOR_CHARS or unicodedata.category(char).startswith("S")


def _is_standalone_delimiter(text: str, start: int, token: str) -> bool:
    before = text[start - 1] if start else ""
    after_index = start + len(token)
    after = text[after_index] if after_index < len(text) else ""
    before_is_compound = bool(before and _is_operator_symbol(before))
    if token == ";" and before and before not in _ASCII_OPERATOR_CHARS:
        # A single nullary symbolic atom may be a complete value right
        # against the binding separator. Fragment validation below still
        # rejects a dangling Unicode infix such as ``x ∧;``.
        before_is_compound = False
    return not before_is_compound and not (after and _is_operator_symbol(after))


def _collapse_layout_whitespace(text: str) -> str:
    """Collapse layout while preserving strings, guillemets, and Char tokens byte-for-byte."""

    output: list[str] = []
    pending_space = False
    in_string = False
    in_guillemet = False
    escaped = False
    opaque_literal_finish = -1
    source = text.strip()
    for index, char in enumerate(source):
        if index <= opaque_literal_finish:
            output.append(char)
            continue
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if in_guillemet:
            output.append(char)
            if char == "»":
                in_guillemet = False
            continue
        if char.isspace():
            pending_space = True
            continue
        if pending_space and output:
            output.append(" ")
        pending_space = False
        output.append(char)
        if char == "r" and (finish := _raw_string_literal_end(source, index)) is not None:
            opaque_literal_finish = finish
        elif char == '"':
            in_string = True
        elif char == "«":
            in_guillemet = True
        elif char == "'" and (finish := _single_quoted_literal_end(source, index)) is not None:
            opaque_literal_finish = finish
    return "".join(output)


def _validate_kind(kind: str) -> None:
    if kind not in SUPPORTED_DECLARATION_KINDS:
        raise SurfaceRenderError(
            SurfaceFailureCode.UNSUPPORTED_DECLARATION_KIND,
            f"goal_v1.0 accepts theorem/lemma, got {kind!r}",
        )


def _mask_comments(source: str) -> _MaskedSource:
    """Mask nested Lean comments while preserving strings, guillemets, and offsets."""

    out = list(source)
    block_depth = 0
    in_line_comment = False
    in_string = False
    in_guillemet = False
    escaped = False
    index = 0
    while index < len(source):
        char = source[index]
        next_two = source[index : index + 2]
        if in_line_comment:
            if char == "\n":
                in_line_comment = False
            else:
                out[index] = " "
            index += 1
            continue
        if block_depth:
            if next_two == "/-":
                out[index] = out[index + 1] = " "
                block_depth += 1
                index += 2
            elif next_two == "-/":
                out[index] = out[index + 1] = " "
                block_depth -= 1
                index += 2
            else:
                if char != "\n":
                    out[index] = " "
                index += 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if in_guillemet:
            if char == "»":
                in_guillemet = False
            index += 1
            continue
        if char == "r" and (finish := _raw_string_literal_end(source, index)) is not None:
            index = finish + 1
            continue
        if char == "'" and (finish := _single_quoted_literal_end(source, index)) is not None:
            index = finish + 1
            continue
        if next_two == "--":
            out[index] = out[index + 1] = " "
            in_line_comment = True
            index += 2
            continue
        if next_two == "/-":
            out[index] = out[index + 1] = " "
            block_depth = 1
            index += 2
            continue
        if char == '"':
            in_string = True
        elif char == "«":
            in_guillemet = True
        index += 1
    if block_depth:
        raise SurfaceRenderError(
            SurfaceFailureCode.UNBALANCED_DELIMITER,
            "unterminated block comment",
        )
    if in_string:
        raise SurfaceRenderError(
            SurfaceFailureCode.UNBALANCED_DELIMITER,
            "unterminated string literal",
        )
    return _MaskedSource(source, "".join(out))


def _mask_literals_for_target(masked: str) -> str:
    """Hide quoted syntax tokens but retain one non-space value sentinel."""

    out = list(masked)
    index = 0
    while index < len(masked):
        char = masked[index]
        if char == "r" and (raw_finish := _raw_string_literal_end(masked, index)) is not None:
            finish = raw_finish
        elif char == '"':
            finish = index + 1
            escaped = False
            while finish < len(masked):
                current = masked[finish]
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    break
                finish += 1
            if finish >= len(masked):
                raise GoalV1Error("unterminated string literal in target")
        elif char == "«":
            finish = masked.find("»", index + 1)
            if finish < 0:
                raise GoalV1Error("unterminated guillemet identifier in target")
        elif (
            char == "'"
            and masked.startswith("''", index)
            and index > 0
            and masked[index - 1].isspace()
            and index + 2 < len(masked)
            and masked[index + 2].isspace()
        ):
            index += 2
            continue
        elif char == "'":
            literal_end = _single_quoted_literal_end(masked, index)
            if literal_end is None:
                if index == 0 or not (masked[index - 1].isalnum() or masked[index - 1] in "_'∑∏"):
                    raise GoalV1Error("unsupported or unterminated single-quoted target syntax")
                index += 1
                continue
            finish = literal_end
        else:
            index += 1
            continue
        out[index] = "x"
        for literal_index in range(index + 1, finish + 1):
            if out[literal_index] != "\n":
                out[literal_index] = " "
        index = finish + 1
    return "".join(out)


def _skip_space(text: str, index: int) -> int:
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def _matching_delimiter(masked: str, start: int) -> int:
    opening = masked[start]
    expected = _DELIMITER_PAIRS[opening]
    stack = [expected]
    in_string = False
    in_guillemet = False
    escaped = False
    opaque_literal_finish = -1
    for index in range(start + 1, len(masked)):
        char = masked[index]
        if index <= opaque_literal_finish:
            continue
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if in_guillemet:
            if char == "»":
                in_guillemet = False
            continue
        if char == "r" and (finish := _raw_string_literal_end(masked, index)) is not None:
            opaque_literal_finish = finish
            continue
        if char == '"':
            in_string = True
            continue
        if char == "«":
            in_guillemet = True
            continue
        if char == "'" and (finish := _single_quoted_literal_end(masked, index)) is not None:
            opaque_literal_finish = finish
            continue
        if char in _DELIMITER_PAIRS:
            stack.append(_DELIMITER_PAIRS[char])
        elif char in _CLOSING_DELIMITERS:
            if not stack or char != stack[-1]:
                raise SurfaceRenderError(
                    SurfaceFailureCode.UNBALANCED_DELIMITER,
                    f"unexpected {char!r} at offset {index}",
                )
            stack.pop()
            if not stack:
                return index
    raise SurfaceRenderError(
        SurfaceFailureCode.UNBALANCED_DELIMITER,
        f"missing closing {expected!r}",
    )


def _top_level_positions(text: str, token: str) -> list[int]:
    positions: list[int] = []
    stack: list[str] = []
    in_string = False
    in_guillemet = False
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if in_guillemet:
            if char == "»":
                in_guillemet = False
            index += 1
            continue
        if char == "r" and (finish := _raw_string_literal_end(text, index)) is not None:
            index = finish + 1
            continue
        if char == "'" and (finish := _single_quoted_literal_end(text, index)) is not None:
            index = finish + 1
            continue
        if char == '"':
            in_string = True
        elif char == "«":
            in_guillemet = True
        elif char in _DELIMITER_PAIRS:
            stack.append(_DELIMITER_PAIRS[char])
        elif char in _CLOSING_DELIMITERS:
            if not stack or stack.pop() != char:
                raise SurfaceRenderError(
                    SurfaceFailureCode.UNBALANCED_DELIMITER,
                    f"unexpected {char!r} at offset {index}",
                )
        elif not stack and text.startswith(token, index):
            if token in {":=", ";"} and not _is_standalone_delimiter(text, index, token):
                index += 1
                continue
            positions.append(index)
            index += len(token)
            continue
        index += 1
    if stack:
        raise SurfaceRenderError(
            SurfaceFailureCode.UNBALANCED_DELIMITER,
            f"missing closing {stack[-1]!r}",
        )
    return positions


def _split_top_level_once(text: str, token: str) -> tuple[str, str] | None:
    positions = _top_level_positions(text, token)
    if not positions:
        return None
    index = positions[0]
    return text[:index], text[index + len(token) :]


def _is_guillemet_name(name: str) -> bool:
    return re.fullmatch(r"«[^«»\n]+»", name) is not None


def _plain_name_prefix_end(name: str) -> int:
    if not name or not (name[0].isalpha() or name[0] == "_"):
        return 0
    index = 1
    while index < len(name) and (name[index].isalnum() or name[index] in "_'"):
        index += 1
    return index


def _is_supported_local_name(name: str, *, allow_generated: bool) -> bool:
    """Recognize one original-token name, plus Lean's printed inaccessible suffix."""

    if _is_guillemet_name(name):
        return True
    prefix_end = _plain_name_prefix_end(name)
    if not prefix_end:
        return False
    plain = name[:prefix_end]
    if plain == "_" or plain in _RESERVED_BINDING_NAMES:
        return False
    suffix = name[prefix_end:]
    return not suffix or (allow_generated and _GENERATED_NAME_SUFFIX.fullmatch(suffix) is not None)


def _name_identity(name: str) -> str:
    """Compare escaped and unescaped spellings as the same Lean name."""

    return name[1:-1] if _is_guillemet_name(name) else name


def _parse_names(text: str, *, allow_generated: bool = False) -> tuple[str, ...]:
    names_list: list[str] = []
    index = 0
    while index < len(text):
        index = _skip_space(text, index)
        if index >= len(text):
            break
        if text[index] == "«":
            finish = text.find("»", index + 1)
            if finish < 0:
                raise SurfaceRenderError(
                    SurfaceFailureCode.UNTYPED_BINDER,
                    f"unterminated guillemet binder name: {text!r}",
                )
            names_list.append(text[index : finish + 1])
            index = finish + 1
            if index < len(text) and not text[index].isspace():
                raise SurfaceRenderError(
                    SurfaceFailureCode.UNTYPED_BINDER,
                    f"guillemet binder name is not token-delimited: {text!r}",
                )
            continue
        finish = index
        while finish < len(text) and not text[finish].isspace():
            finish += 1
        names_list.append(text[index:finish])
        index = finish
    names = tuple(names_list)
    if not names or any(name in {"_", "·"} for name in names):
        raise SurfaceRenderError(
            SurfaceFailureCode.UNTYPED_BINDER,
            f"binder has no stable explicit name: {text!r}",
        )
    if any(not _is_supported_local_name(name, allow_generated=allow_generated) for name in names):
        raise SurfaceRenderError(
            SurfaceFailureCode.UNTYPED_BINDER,
            f"unsupported binder name syntax: {text!r}",
        )
    return names


def _parse_binder_content(content: str, opening: str) -> _Binder:
    split = _split_top_level_once(content.strip(), ":")
    if split is None:
        if opening == "[":
            raise SurfaceRenderError(
                SurfaceFailureCode.ANONYMOUS_INSTANCE_BINDER,
                f"surface mode cannot recover Lean's generated instance name for [{content}]",
            )
        raise SurfaceRenderError(
            SurfaceFailureCode.UNTYPED_BINDER,
            f"binder lacks an explicit type: {content!r}",
        )
    raw_names, raw_type = split
    names = _parse_names(raw_names.strip())
    type_text = _collapse_layout_whitespace(raw_type)
    if not type_text:
        raise SurfaceRenderError(
            SurfaceFailureCode.UNTYPED_BINDER,
            f"binder has an empty type: {content!r}",
        )
    try:
        type_text = _canonicalize_binding_expression(type_text)
    except GoalV1Error as exc:
        raise SurfaceRenderError(
            SurfaceFailureCode.INVALID_GOAL,
            f"binder type is outside the bounded binding grammar: {exc}",
        ) from exc
    return _Binder(names, type_text)


def _parse_leading_binders(text: str) -> tuple[list[_Binder], str]:
    binders: list[_Binder] = []
    index = _skip_space(text, 0)
    while index < len(text) and text[index] in "({[":
        finish = _matching_delimiter(text, index)
        binders.append(_parse_binder_content(text[index + 1 : finish], text[index]))
        index = _skip_space(text, finish + 1)
    return binders, text[index:]


def _peel_forall_binders(target: str) -> tuple[list[_Binder], str]:
    binders: list[_Binder] = []
    remaining = target.strip()
    while (match := _FORALL.match(remaining)) is not None:
        body = remaining[match.end() :]
        comma_positions = _top_level_positions(body, ",")
        if not comma_positions:
            raise SurfaceRenderError(
                SurfaceFailureCode.UNTYPED_BINDER,
                "top-level forall has no comma-delimited body",
            )
        clause = body[: comma_positions[0]].strip()
        clause_binders, clause_remainder = _parse_leading_binders(clause)
        if clause_binders:
            if clause_remainder.strip():
                raise SurfaceRenderError(
                    SurfaceFailureCode.UNTYPED_BINDER,
                    f"unsupported forall binder clause: {clause!r}",
                )
            binders.extend(clause_binders)
        else:
            if len(comma_positions) != 1:
                raise SurfaceRenderError(
                    SurfaceFailureCode.UNTYPED_BINDER,
                    "unparenthesized top-level forall has multiple possible comma boundaries; "
                    "parenthesize the binder",
                )
            binders.append(_parse_binder_content(clause, "("))
        remaining = body[comma_positions[0] + 1 :].strip()
    return binders, remaining


def _strip_balanced_outer_parentheses(text: str) -> str:
    stripped = text.strip()
    while stripped.startswith("("):
        finish = _matching_delimiter(stripped, 0)
        if finish != len(stripped) - 1:
            break
        stripped = stripped[1:-1].strip()
    return stripped


def _group_binders(binders: Sequence[_Binder]) -> list[str]:
    grouped: list[_Binder] = []
    for binder in binders:
        if grouped and grouped[-1].type_text == binder.type_text:
            previous = grouped[-1]
            grouped[-1] = _Binder(previous.names + binder.names, previous.type_text)
        else:
            grouped.append(binder)
    return [f"{' '.join(binder.names)} : {binder.type_text}" for binder in grouped]


def _canonicalize_surface_universe_names(signature: str) -> str:
    """Apply the shared first-occurrence ``u_i`` profile to bounded surface levels."""

    masked = _mask_literals_for_target(signature)
    keyword = r"(?<![\w'.])(?:Type|Sort)(?![\w'])"
    if re.search(rf"{keyword}\s*(?:\*|\(|[0-9])", masked) or re.search(r"\.\{", masked):
        raise SurfaceRenderError(
            SurfaceFailureCode.INVALID_GOAL,
            "surface universe syntax must use an explicit simple level name",
        )
    mapping: dict[str, str] = {}
    pattern = re.compile(r"(?<![\w'.])(Type|Sort)\s+([A-Za-z_][A-Za-z0-9_']*)(?![\w'.])")
    matches = list(pattern.finditer(masked))
    matched_keywords = {match.start() for match in matches}
    for keyword_match in re.finditer(keyword, masked):
        if keyword_match.start() in matched_keywords:
            continue
        suffix = masked[keyword_match.end() :]
        if not suffix:
            continue
        immediate = suffix[0]
        following = suffix.lstrip()
        if immediate.isspace() and following:
            first = following[0]
            if first in {"*", "?", "(", "_"} or first.isdigit() or first.isidentifier():
                raise SurfaceRenderError(
                    SurfaceFailureCode.INVALID_GOAL,
                    "surface universe syntax must use one explicit simple level name",
                )
        elif immediate in {"*", "?", "("} or immediate.isdigit():
            raise SurfaceRenderError(
                SurfaceFailureCode.INVALID_GOAL,
                "surface universe syntax must use one explicit simple level name",
            )
    pieces: list[str] = []
    cursor = 0
    for match in matches:
        sort, name = match.groups()
        if signature[match.start(2) : match.end(2)] != name:
            raise SurfaceRenderError(
                SurfaceFailureCode.INVALID_GOAL,
                "surface universe syntax must use one explicit simple level name",
            )
        if name in {"_", "max", "imax"}:
            raise SurfaceRenderError(
                SurfaceFailureCode.INVALID_GOAL,
                f"unsupported compound or inferred surface universe level {name!r}",
            )
        suffix = masked[match.end() :]
        next_nonspace = suffix.lstrip()[:1]
        if next_nonspace and (next_nonspace.isalnum() or next_nonspace in {"_", ".", "+"}):
            raise SurfaceRenderError(
                SurfaceFailureCode.INVALID_GOAL,
                "surface universe syntax must use one simple level name",
            )
        canonical = mapping.setdefault(name, f"u_{len(mapping)}")
        pieces.append(signature[cursor : match.start()])
        pieces.append(f"{sort} {canonical}")
        cursor = match.end()
    pieces.append(signature[cursor:])
    return "".join(pieces)


def _lexical_contexts(text: str) -> tuple[str, tuple[tuple[int, ...], ...], dict[int, int]]:
    """Return literal-masked text, delimiter context at each offset, and scope ends."""

    masked = _mask_literals_for_target(_mask_comments(text).masked)
    contexts: list[tuple[int, ...]] = []
    stack: list[tuple[str, int]] = []
    scope_ends: dict[int, int] = {}
    for index, char in enumerate(masked):
        contexts.append(tuple(opening_index for _, opening_index in stack))
        if char in _DELIMITER_PAIRS:
            stack.append((_DELIMITER_PAIRS[char], index))
            continue
        if char not in _CLOSING_DELIMITERS:
            continue
        if not stack or stack[-1][0] != char:
            raise GoalV1Error(f"unbalanced target delimiter {char!r} at offset {index}")
        _, opening_index = stack.pop()
        scope_ends[opening_index] = index
    if stack:
        closing, _ = stack[-1]
        raise GoalV1Error(f"unbalanced target delimiter; missing {closing!r}")
    contexts.append(())
    return masked, tuple(contexts), scope_ends


def _positions_at_all_depths(
    masked: str,
    contexts: Sequence[tuple[int, ...]],
    token: str,
) -> list[tuple[int, tuple[int, ...]]]:
    positions: list[tuple[int, tuple[int, ...]]] = []
    index = 0
    while index < len(masked):
        if masked.startswith(token, index):
            if token in {":=", ";"} and not _is_standalone_delimiter(masked, index, token):
                raise GoalV1Error(
                    f"ambiguous compound operator containing {token!r} at offset {index}"
                )
            positions.append((index, contexts[index]))
            index += len(token)
            continue
        index += 1
    return positions


def _scope_end_for_context(
    context: tuple[int, ...],
    *,
    text_length: int,
    scope_ends: Mapping[int, int],
) -> int:
    return scope_ends[context[-1]] if context else text_length


def _single_bar_positions(masked: str) -> tuple[int, ...]:
    """Return bars that are not part of a known multi-character operator."""

    positions: list[int] = []
    for index, char in enumerate(masked):
        if char != "|":
            continue
        before = masked[index - 1] if index else ""
        after = masked[index + 1] if index + 1 < len(masked) else ""
        if (before and before in "|<") or (after and after in "|>"):
            continue
        positions.append(index)
    return tuple(positions)


def _validate_balanced_bars(
    masked: str,
    contexts: Sequence[tuple[int, ...]],
    scope_ends: Mapping[int, int],
    *,
    label: str,
) -> frozenset[int]:
    """Validate absolute-value bars and one brace-local set-builder separator."""

    if any(token in masked for token in ("||", "<|", "|>")):
        raise GoalV1Error(f"{label} uses an unsupported compound bar operator")

    grouped: dict[tuple[int, ...], list[int]] = {}
    for position in _single_bar_positions(masked):
        grouped.setdefault(contexts[position], []).append(position)

    closing_bars: set[int] = set()
    for context, positions in grouped.items():
        remaining = list(positions)
        brace_opening = context[-1] if context and masked[context[-1]] == "{" else None
        if brace_opening is not None:
            brace_closing = scope_ends[brace_opening]
            separator_candidates = [
                position
                for position in positions
                if masked[brace_opening + 1 : position].strip()
                and masked[position + 1 : brace_closing].strip()
                and position > brace_opening + 1
                and position + 1 < brace_closing
                and masked[position - 1].isspace()
                and masked[position + 1].isspace()
            ]
            if len(positions) % 2:
                if len(separator_candidates) != 1:
                    raise GoalV1Error(f"{label} has an ambiguous set-builder or unmatched bar")
                remaining.remove(separator_candidates[0])
            elif separator_candidates:
                raise GoalV1Error(f"{label} has an ambiguous even-count set-builder bar")
        if len(remaining) % 2:
            raise GoalV1Error(f"{label} has unmatched absolute-value/cardinality bar")
        for opening, closing in zip(remaining[::2], remaining[1::2], strict=True):
            content = masked[opening + 1 : closing]
            if not content.strip() or content[:1].isspace() or content[-1:].isspace():
                raise GoalV1Error(f"{label} has an empty or whitespace-padded paired bar")
            closing_bars.add(closing)
    return frozenset(closing_bars)


def _slice_ends_at_validated_bar(
    masked: str,
    start: int,
    end: int,
    closing_bars: frozenset[int],
) -> bool:
    final = end - 1
    while final >= start and masked[final].isspace():
        final -= 1
    return final in closing_bars


def _has_supported_postfix_edge(text: str, *, allow_trailing_bar: bool) -> bool:
    """Recognize only the frozen postfix/atomic notations accepted at an edge."""

    if re.search(r"(?<![\w'])\u2115\+$", text):
        return True
    if re.search(r"[⌋⌉]₊$", text):
        return True
    if allow_trailing_bar and text.endswith("|"):
        return True
    if not text.endswith("!"):
        return False
    prefix = text[:-1].rstrip()
    if not prefix:
        return False
    tail = prefix[-1]
    return tail.isalnum() or tail in _CLOSING_DELIMITERS or tail in "_'✝₊"


def _validate_set_image_operators(
    masked: str,
    contexts: Sequence[tuple[int, ...]],
    scope_ends: Mapping[int, int],
    closing_bars: frozenset[int],
    *,
    label: str,
) -> None:
    """Validate only the whitespace-delimited set-image token ``''``."""

    positions = [match.start() for match in re.finditer(r"(?<=\s)''(?=\s)", masked)]
    for position in positions:
        context = contexts[position]
        scope_start = context[-1] + 1 if context else 0
        scope_end = _scope_end_for_context(
            context,
            text_length=len(masked),
            scope_ends=scope_ends,
        )
        lhs = masked[scope_start:position]
        _validate_fragment_edge(
            lhs.strip(),
            label=f"{label} set-image left operand",
            allow_empty=False,
            allow_trailing_bar=_slice_ends_at_validated_bar(
                masked, scope_start, position, closing_bars
            ),
        )
        rhs_start = _skip_space(masked, position + 2)
        if rhs_start >= scope_end or masked.startswith("''", rhs_start):
            raise GoalV1Error(f"{label} has a missing or repeated set-image right operand")
        first = masked[rhs_start]
        allowed_unary_prefixes = frozenset({"↑", "⇑", "√", "¬"})
        if (
            first in _CLOSING_DELIMITERS
            or first in ",;:"
            or first in _ASCII_OPERATOR_CHARS
            or (
                unicodedata.category(first).startswith("S")
                and first not in allowed_unary_prefixes
                and first not in _DELIMITER_PAIRS
            )
        ):
            raise GoalV1Error(f"{label} has an incomplete set-image right operand")


def _validate_special_delimiter_content(
    masked: str,
    opening: int,
    closing: int,
    closing_bars: frozenset[int],
    *,
    label: str,
) -> None:
    opening_char = masked[opening]
    content_start = opening + 1
    content = masked[content_start:closing]
    requires_content = opening_char in {"⌊", "⌈", "⟪"}
    _validate_fragment_edge(
        content.strip(),
        label=f"{label} delimiter content",
        allow_empty=not requires_content,
        allow_trailing_bar=_slice_ends_at_validated_bar(
            masked, content_start, closing, closing_bars
        ),
    )
    if opening_char != "⟪":
        return
    commas = _top_level_positions(content, ",")
    if len(commas) != 1:
        raise GoalV1Error(f"{label} inner-product delimiter requires exactly one comma")
    comma = commas[0]
    _validate_fragment_edge(
        content[:comma].strip(),
        label=f"{label} inner-product left operand",
        allow_empty=False,
    )
    _validate_fragment_edge(
        content[comma + 1 :].strip(),
        label=f"{label} inner-product right operand",
        allow_empty=False,
        allow_trailing_bar=_slice_ends_at_validated_bar(
            masked, content_start + comma + 1, closing, closing_bars
        ),
    )


def _validate_structured_introducers(fragment: str, *, label: str) -> None:
    """Reject incomplete structured terms at every balanced delimiter depth."""

    masked, contexts, scope_ends = _lexical_contexts(fragment)
    closing_bars = _validate_balanced_bars(masked, contexts, scope_ends, label=label)
    layout_match = _UNSUPPORTED_LAYOUT_TOKEN.search(masked)
    if layout_match is not None:
        raise GoalV1Error(
            f"{label} uses unsupported layout/macro introducer {layout_match.group(1)!r}"
        )

    quantifier_events: dict[tuple[int, ...], list[tuple[int, int, str]]] = {}
    for match in _QUANTIFIER_TOKEN.finditer(masked):
        quantifier_events.setdefault(contexts[match.start()], []).append(
            (match.start(), match.end(), "quantifier")
        )
    for position, char in enumerate(masked):
        if char == ",":
            quantifier_events.setdefault(contexts[position], []).append(
                (position, position + 1, "comma")
            )
    for context, events in quantifier_events.items():
        if not any(kind == "quantifier" for _, _, kind in events):
            continue
        quantifier_stack: list[tuple[int, int]] = []
        context_end = _scope_end_for_context(
            context,
            text_length=len(masked),
            scope_ends=scope_ends,
        )
        for start, end, kind in sorted(events):
            if kind == "quantifier":
                quantifier_stack.append((start, end))
                continue
            if not quantifier_stack:
                raise GoalV1Error(f"{label} has an ambiguous comma in quantified syntax")
            _quantifier_start, quantifier_end = quantifier_stack.pop()
            _validate_fragment_edge(
                masked[quantifier_end:start].strip(),
                label=f"{label} quantifier binder",
                allow_empty=False,
                allow_trailing_bar=_slice_ends_at_validated_bar(
                    masked, quantifier_end, start, closing_bars
                ),
            )
            _validate_fragment_edge(
                masked[end:context_end].strip(),
                label=f"{label} quantifier body",
                allow_empty=False,
                allow_trailing_bar=_slice_ends_at_validated_bar(
                    masked, end, context_end, closing_bars
                ),
            )
        if quantifier_stack:
            raise GoalV1Error(f"{label} has an incomplete comma-binding quantifier")

    conditional_events: dict[tuple[int, ...], list[tuple[int, int, str]]] = {}
    for match in _CONDITIONAL_TOKEN.finditer(masked):
        conditional_events.setdefault(contexts[match.start()], []).append(
            (match.start(), match.end(), match.group(1))
        )
    for context, events in conditional_events.items():
        conditional_stack: list[tuple[str, int, int]] = []
        context_end = _scope_end_for_context(
            context,
            text_length=len(masked),
            scope_ends=scope_ends,
        )
        for start, end, keyword in events:
            if keyword == "if":
                conditional_stack.append(("if", start, end))
            elif keyword == "then":
                if not conditional_stack or conditional_stack[-1][0] != "if":
                    raise GoalV1Error(f"{label} has an unmatched 'then'")
                _, if_start, if_end = conditional_stack[-1]
                _validate_fragment_edge(
                    masked[if_end:start].strip(),
                    label=f"{label} if condition",
                    allow_empty=False,
                    allow_trailing_bar=_slice_ends_at_validated_bar(
                        masked, if_end, start, closing_bars
                    ),
                )
                conditional_stack[-1] = ("then", if_start, end)
            else:
                if not conditional_stack or conditional_stack[-1][0] != "then":
                    raise GoalV1Error(f"{label} has an unmatched 'else'")
                _, _if_start, then_end = conditional_stack.pop()
                _validate_fragment_edge(
                    masked[then_end:start].strip(),
                    label=f"{label} then branch",
                    allow_empty=False,
                    allow_trailing_bar=_slice_ends_at_validated_bar(
                        masked, then_end, start, closing_bars
                    ),
                )
                _validate_fragment_edge(
                    masked[end:context_end].strip(),
                    label=f"{label} else branch",
                    allow_empty=False,
                    allow_trailing_bar=_slice_ends_at_validated_bar(
                        masked, end, context_end, closing_bars
                    ),
                )
        if conditional_stack:
            raise GoalV1Error(f"{label} has an incomplete if/then/else term")

    fun_events: dict[tuple[int, ...], list[tuple[int, int, str]]] = {}
    for match in _FUN_TOKEN.finditer(masked):
        fun_events.setdefault(contexts[match.start()], []).append(
            (match.start(), match.end(), "fun")
        )
    index = 0
    while index < len(masked):
        token = "=>" if masked.startswith("=>", index) else "↦" if masked[index] == "↦" else ""
        if token:
            fun_events.setdefault(contexts[index], []).append((index, index + len(token), "arrow"))
            index += len(token)
        else:
            index += 1
    for context, events in fun_events.items():
        fun_stack: list[tuple[int, int]] = []
        context_end = _scope_end_for_context(
            context,
            text_length=len(masked),
            scope_ends=scope_ends,
        )
        for start, end, kind in sorted(events):
            if kind == "fun":
                fun_stack.append((start, end))
                continue
            if not fun_stack:
                raise GoalV1Error(f"{label} has an unmatched function arrow")
            _fun_start, fun_end = fun_stack.pop()
            _validate_fragment_edge(
                masked[fun_end:start].strip(),
                label=f"{label} fun binder",
                allow_empty=False,
                allow_trailing_bar=_slice_ends_at_validated_bar(
                    masked, fun_end, start, closing_bars
                ),
            )
            _validate_fragment_edge(
                masked[end:context_end].strip(),
                label=f"{label} fun body",
                allow_empty=False,
                allow_trailing_bar=_slice_ends_at_validated_bar(
                    masked, end, context_end, closing_bars
                ),
            )
        if fun_stack:
            raise GoalV1Error(f"{label} has an incomplete fun term")

    show_events: dict[tuple[int, ...], list[tuple[int, int, str]]] = {}
    for match in _SHOW_FROM_TOKEN.finditer(masked):
        show_events.setdefault(contexts[match.start()], []).append(
            (match.start(), match.end(), match.group(1))
        )
    for context, events in show_events.items():
        show_stack: list[tuple[int, int]] = []
        context_end = _scope_end_for_context(
            context,
            text_length=len(masked),
            scope_ends=scope_ends,
        )
        for start, end, keyword in events:
            if keyword == "show":
                show_stack.append((start, end))
                continue
            if not show_stack:
                raise GoalV1Error(f"{label} has an unmatched 'from'")
            _show_start, show_end = show_stack.pop()
            _validate_fragment_edge(
                masked[show_end:start].strip(),
                label=f"{label} show type",
                allow_empty=False,
                allow_trailing_bar=_slice_ends_at_validated_bar(
                    masked, show_end, start, closing_bars
                ),
            )
            _validate_fragment_edge(
                masked[end:context_end].strip(),
                label=f"{label} show body",
                allow_empty=False,
                allow_trailing_bar=_slice_ends_at_validated_bar(
                    masked, end, context_end, closing_bars
                ),
            )
        if show_stack:
            raise GoalV1Error(f"{label} has an incomplete show/from term")


def _validate_complete_fragment(fragment: str, *, label: str) -> None:
    """Reject obvious incomplete syntax in the bounded term grammar."""

    masked, contexts, scope_ends = _lexical_contexts(fragment)
    closing_bars = _validate_balanced_bars(masked, contexts, scope_ends, label=label)
    _validate_set_image_operators(
        masked,
        contexts,
        scope_ends,
        closing_bars,
        label=label,
    )
    stripped = masked.strip()
    _validate_fragment_edge(
        stripped,
        label=label,
        allow_empty=False,
        allow_trailing_bar=_slice_ends_at_validated_bar(masked, 0, len(masked), closing_bars),
    )
    for opening, closing in scope_ends.items():
        _validate_special_delimiter_content(
            masked,
            opening,
            closing,
            closing_bars,
            label=label,
        )
    unwrapped = _strip_balanced_outer_parentheses(stripped)
    if unwrapped in _BARE_INCOMPLETE_TERMS or re.search(
        r"(?:^|[^\w'])(?:by|calc|do|else|forall|from|fun|have|if|in|let|match|return|show|then|where|with)\s*$",
        stripped,
    ):
        raise GoalV1Error(f"{label} is a bare incomplete term introducer")
    if re.search(
        r"\(\s*(?:by|calc|do|else|forall|from|fun|have|if|in|let|match|return|show|then|where|with)\s*\)\s*$",
        stripped,
    ):
        raise GoalV1Error(f"{label} ends in a parenthesized incomplete term introducer")


def _validate_fragment_edge(
    text: str,
    *,
    label: str,
    allow_empty: bool,
    allow_trailing_bar: bool = False,
) -> None:
    """Reject incomplete syntax at one expression or balanced-delimiter edge."""

    if not text:
        if allow_empty:
            return
        raise GoalV1Error(f"{label} is empty")
    if text.startswith(",") or text.endswith(","):
        raise GoalV1Error(f"{label} has a dangling comma")
    if text.startswith(("||", "<|", "|>")) or text.endswith(("||", "<|", "|>")):
        raise GoalV1Error(f"{label} has a dangling compound bar operator")
    trailing_unicode_operator = (
        unicodedata.category(text[-1]).startswith("S") and text[-1] not in _ATOMIC_SYMBOL_TERMS
    )
    incomplete_edge = (
        text.endswith((":", ";")) or text[-1] in _ASCII_OPERATOR_CHARS or trailing_unicode_operator
    )
    if incomplete_edge and not _has_supported_postfix_edge(
        text,
        allow_trailing_bar=allow_trailing_bar,
    ):
        raise GoalV1Error(f"{label} ends with an incomplete operator or delimiter")


def _validate_term_introducer(fragment: str, *, label: str) -> None:
    """Validate a few complete term introducers without pretending to parse Lean."""

    stripped = _strip_balanced_outer_parentheses(fragment)
    if re.search(r"\bif\s+then\b|\bthen\s+else\b|\bshow\s+from\b|(?:∀|\bforall)\s*,", stripped):
        raise GoalV1Error(f"{label} has an empty structured-term segment")
    if re.match(r"^(?:by|do|calc|match)\b", stripped):
        raise GoalV1Error(f"{label} uses an unsupported layout/macro introducer")
    if re.match(r"^if\b", stripped):
        then_matches = list(re.finditer(r"\bthen\b", stripped))
        else_matches = list(re.finditer(r"\belse\b", stripped))
        if not then_matches or not else_matches:
            raise GoalV1Error(f"{label} has an incomplete if/then/else term")
        then_match = then_matches[0]
        else_match = next(
            (match for match in else_matches if match.start() >= then_match.end()),
            None,
        )
        if (
            else_match is None
            or not stripped[2 : then_match.start()].strip()
            or not stripped[then_match.end() : else_match.start()].strip()
            or not stripped[else_match.end() :].strip()
        ):
            raise GoalV1Error(f"{label} has an incomplete if/then/else term")
    if re.match(r"^fun\b", stripped):
        arrows = _top_level_positions(stripped, "=>") + _top_level_positions(stripped, "↦")
        if not arrows:
            raise GoalV1Error(f"{label} has an incomplete fun term")
        arrow = min(arrows)
        token_length = 2 if stripped.startswith("=>", arrow) else 1
        if not stripped[3:arrow].strip() or not stripped[arrow + token_length :].strip():
            raise GoalV1Error(f"{label} has an incomplete fun term")
    if re.match(r"^(?:∀\s*|forall\b)", stripped):
        commas = _top_level_positions(stripped, ",")
        keyword_end = 1 if stripped.startswith("∀") else len("forall")
        if (
            not commas
            or not stripped[keyword_end : commas[0]].strip()
            or not stripped[commas[0] + 1 :].strip()
        ):
            raise GoalV1Error(f"{label} has an incomplete forall term")
    if re.match(r"^show\b", stripped):
        from_match = re.search(r"\bfrom\b", stripped)
        if (
            from_match is None
            or not stripped[len("show") : from_match.start()].strip()
            or not stripped[from_match.end() :].strip()
        ):
            raise GoalV1Error(f"{label} has an incomplete show/from term")
    _validate_structured_introducers(fragment, label=label)


def _validate_binding_head(
    head: str,
    *,
    keyword: str,
    offset: int,
    allow_generated_names: bool,
) -> None:
    """Validate the deliberately small named binding-head grammar."""

    comment_masked_head = _mask_comments(head).masked.strip()
    split = _split_top_level_once(comment_masked_head, ":")
    raw_name, annotation = split if split is not None else (comment_masked_head, None)
    name = raw_name.strip()
    if not _is_supported_local_name(name, allow_generated=allow_generated_names):
        raise GoalV1Error(f"unsupported {keyword} binding head at offset {offset}")
    if annotation is not None:
        try:
            masked_annotation = _mask_literals_for_target(_mask_comments(annotation).masked)
            if _top_level_positions(masked_annotation, ":"):
                raise GoalV1Error("binding type has a second top-level ':'")
            _validate_complete_fragment(masked_annotation, label=f"{keyword} binding type")
            _validate_term_introducer(masked_annotation, label=f"{keyword} binding type")
        except GoalV1Error as exc:
            raise GoalV1Error(
                f"unsupported {keyword} binding head at offset {offset}: {exc}"
            ) from exc


def _is_supported_named_argument_assignment(
    masked: str,
    *,
    expression: str,
    position: int,
    context: tuple[int, ...],
    scope_ends: Mapping[int, int],
) -> bool:
    """Allow only the explicit parenthesized ``(name := value)`` exception."""

    if not context:
        return False
    opening = context[-1]
    if masked[opening] != "(" or opening not in scope_ends:
        return False
    closing = scope_ends[opening]
    name = _mask_comments(expression[opening + 1 : position]).masked.strip()
    value = masked[position + 2 : closing].strip()
    if _top_level_positions(masked[opening + 1 : closing], ","):
        return False
    if not _is_supported_local_name(name, allow_generated=False) or not value:
        return False
    try:
        _validate_complete_fragment(value, label="named argument value")
        _validate_term_introducer(value, label="named argument value")
    except GoalV1Error:
        return False
    return True


def _supported_structure_literal_assignments(
    masked: str,
    *,
    expression: str,
    assignments: Sequence[tuple[int, tuple[int, ...]]],
    contexts: Sequence[tuple[int, ...]],
    scope_ends: Mapping[int, int],
) -> set[int]:
    """Accept complete simple ``{ field := value, ... }`` literals as a unit."""

    by_context: dict[tuple[int, ...], list[int]] = {}
    for position, context in assignments:
        if context and masked[context[-1]] == "{":
            by_context.setdefault(context, []).append(position)

    supported: set[int] = set()
    comma_positions = _positions_at_all_depths(masked, contexts, ",")
    for context, context_assignments in by_context.items():
        opening = context[-1]
        closing = scope_ends[opening]
        commas = [
            position
            for position, comma_context in comma_positions
            if comma_context == context and opening < position < closing
        ]
        boundaries = [opening, *commas, closing]
        valid = True
        field_names: set[str] = set()
        for left, right in pairwise(boundaries):
            segment_assignments = [
                position for position in context_assignments if left < position < right
            ]
            if len(segment_assignments) != 1:
                valid = False
                break
            assignment = segment_assignments[0]
            field_name = _mask_comments(expression[left + 1 : assignment]).masked.strip()
            value = masked[assignment + 2 : right].strip()
            if (
                not _is_supported_local_name(field_name, allow_generated=False)
                or field_name in field_names
                or not value
            ):
                valid = False
                break
            field_names.add(field_name)
            try:
                _validate_complete_fragment(value, label="structure field value")
                _validate_term_introducer(value, label="structure field value")
            except GoalV1Error:
                valid = False
                break
        if valid:
            supported.update(context_assignments)
    return supported


def _canonicalize_binding_expression(
    expression: str,
    *,
    allow_generated_names: bool = False,
) -> str:
    """Validate the bounded term-binding grammar and canonicalize ``have`` to ``let``.

    This is deliberately a structural, fail-closed mini-parser, not a Lean
    parser. Every visible term-level binding must be a complete, simple,
    semicolon-delimited binding in one balanced delimiter context. Opaque
    values may contain balanced subterms; layout/macro values and ambiguous
    same-context nested bindings are outside v1.0.
    """

    if not expression.strip():
        raise GoalV1Error("expression is empty")
    masked, contexts, scope_ends = _lexical_contexts(expression)
    if _SYNTAX_QUOTATION.search(masked):
        raise GoalV1Error("surface/elaborated syntax quotations are unsupported in goal_v1.0")
    _validate_complete_fragment(masked, label="expression")
    _validate_term_introducer(masked, label="expression")
    bindings = tuple(
        _BindingToken(match.group(1), match.start(), match.end(), contexts[match.start()])
        for match in _BINDING_KEYWORD.finditer(masked)
        if not masked[: match.start()].rstrip().endswith(".")
    )
    assignments = _positions_at_all_depths(masked, contexts, ":=")
    separators = _positions_at_all_depths(masked, contexts, ";")
    claimed_assignments: set[int] = set()
    claimed_separators: set[int] = set()

    for binding in bindings:
        context_end = scope_ends[binding.context[-1]] if binding.context else len(masked)
        same_context_assignments = [
            position
            for position, context in assignments
            if context == binding.context and binding.end <= position < context_end
        ]
        same_context_separators_before_assignment = [
            position
            for position, context in separators
            if context == binding.context and binding.end <= position < context_end
        ]
        if not same_context_assignments or (
            same_context_separators_before_assignment
            and same_context_separators_before_assignment[0] < same_context_assignments[0]
        ):
            raise GoalV1Error(
                f"incomplete {binding.keyword} binding at offset {binding.start}: missing ':='"
            )
        assignment = same_context_assignments[0]
        if any(
            other.context == binding.context and binding.end <= other.start < assignment
            for other in bindings
            if other is not binding
        ):
            raise GoalV1Error(
                f"incomplete {binding.keyword} binding at offset {binding.start}: ambiguous head"
            )
        head = expression[binding.end : assignment].strip()
        _validate_binding_head(
            head,
            keyword=binding.keyword,
            offset=binding.start,
            allow_generated_names=allow_generated_names,
        )

        same_context_separators = [
            position
            for position, context in separators
            if context == binding.context and assignment + 2 <= position < context_end
        ]
        if not same_context_separators:
            raise GoalV1Error(
                f"incomplete {binding.keyword} binding at offset {binding.start}: "
                "missing ';' body separator"
            )
        separator = same_context_separators[0]
        value = masked[assignment + 2 : separator].strip()
        if not value or value.startswith(","):
            raise GoalV1Error(
                f"incomplete {binding.keyword} binding at offset {binding.start}: "
                "empty or comma-led value"
            )
        _validate_complete_fragment(value, label=f"{binding.keyword} binding value")
        _validate_term_introducer(value, label=f"{binding.keyword} binding value")
        if _UNSUPPORTED_BINDING_RHS.match(value):
            raise GoalV1Error(
                f"unsupported layout/macro value in {binding.keyword} binding "
                f"at offset {binding.start}"
            )
        if any(
            other.context == binding.context and assignment + 2 <= other.start < separator
            for other in bindings
            if other is not binding
        ):
            raise GoalV1Error(f"ambiguous same-context binding value at offset {binding.start}")
        body = masked[separator + 1 : context_end].strip()
        if not body or body.startswith(","):
            raise GoalV1Error(
                f"incomplete {binding.keyword} binding at offset {binding.start}: "
                "empty or comma-led body"
            )
        _validate_complete_fragment(body, label=f"{binding.keyword} binding body")
        _validate_term_introducer(body, label=f"{binding.keyword} binding body")
        if assignment in claimed_assignments or separator in claimed_separators:
            raise GoalV1Error(
                f"overlapping term bindings at offset {binding.start} are unsupported"
            )
        claimed_assignments.add(assignment)
        claimed_separators.add(separator)

    structure_assignments = _supported_structure_literal_assignments(
        masked,
        expression=expression,
        assignments=assignments,
        contexts=contexts,
        scope_ends=scope_ends,
    )
    unclaimed_assignments = [
        position
        for position, context in assignments
        if position not in claimed_assignments
        and position not in structure_assignments
        and not _is_supported_named_argument_assignment(
            masked,
            expression=expression,
            position=position,
            context=context,
            scope_ends=scope_ends,
        )
    ]
    if unclaimed_assignments:
        raise GoalV1Error(f"target contains an unclaimed ':=' at offset {unclaimed_assignments[0]}")
    unclaimed_separators = [
        position for position, _context in separators if position not in claimed_separators
    ]
    if unclaimed_separators:
        raise GoalV1Error(f"target contains an unclaimed ';' at offset {unclaimed_separators[0]}")

    pieces: list[str] = []
    cursor = 0
    for binding in bindings:
        pieces.append(expression[cursor : binding.start])
        pieces.append("let" if binding.keyword == "have" else binding.keyword)
        cursor = binding.end
    pieces.append(expression[cursor:])
    return "".join(pieces)


def _canonicalize_surface_target(target: str) -> str:
    collapsed = _collapse_layout_whitespace(target)
    if not collapsed:
        raise SurfaceRenderError(SurfaceFailureCode.EMPTY_TARGET, "target is empty")
    try:
        return _canonicalize_binding_expression(collapsed)
    except GoalV1Error as exc:
        raise SurfaceRenderError(
            SurfaceFailureCode.AMBIGUOUS_PROOF_BOUNDARY,
            f"surface target is outside the bounded binding grammar: {exc}",
        ) from exc


def _is_elaborated_local_header(line: str) -> bool:
    """Recognize Lean local headers, including a colon-only multiline header."""

    if not line or line[0].isspace():
        return False
    for position in _top_level_positions(line, ":"):
        before, after = line[:position], line[position + 1 :]
        if before.strip() and (not after or after[0].isspace()):
            return True
    return False


def _canonicalize_elaborated_locals(lines: Sequence[str]) -> list[str]:
    logical_lines: list[str] = []
    current: list[str] = []
    for line in lines:
        starts_local = _is_elaborated_local_header(line)
        if starts_local:
            if current:
                logical_lines.append(" ".join(current))
            current = [line]
            continue
        if not current or not line.strip():
            raise GoalV1Error("unsupported multiline local-context layout")
        current.append(line.strip())
    if current:
        logical_lines.append(" ".join(current))

    canonical_lines: list[str] = []
    for line in logical_lines:
        local_split = _split_top_level_once(line, " : ")
        if local_split is None or not local_split[0].strip():
            raise GoalV1Error("elaborated local line has no structural name/type separator")
        names, type_text = local_split
        canonical_names = " ".join(_parse_names(names, allow_generated=True))
        collapsed_type = _collapse_layout_whitespace(type_text)
        canonical_type = _canonicalize_binding_expression(
            collapsed_type,
            allow_generated_names=True,
        )
        canonical_lines.append(f"{canonical_names} : {canonical_type}")
    return canonical_lines


def _canonicalize_elaborated_goal(goal: str) -> str:
    lines = [line.rstrip() for line in goal.splitlines()]
    target_indices = [index for index, line in enumerate(lines) if line.startswith("⊢ ")]
    if len(target_indices) != 1:
        return goal
    target_index = target_indices[0]
    canonical_locals = _canonicalize_elaborated_locals(lines[:target_index])
    segments = [lines[target_index][2:].strip()]
    segments.extend(line.strip() for line in lines[target_index + 1 :])
    if any(not segment for segment in segments) or any(
        segment.startswith("|") for segment in segments[1:]
    ):
        raise GoalV1Error("unsupported multiline target layout")
    collapsed_target = _collapse_layout_whitespace(" ".join(segments))
    canonical_target = _canonicalize_binding_expression(
        collapsed_target,
        allow_generated_names=True,
    )
    return "\n".join([*canonical_locals, f"⊢ {canonical_target}"])


def validate_goal_v1(goal: str) -> None:
    lines = goal.splitlines()
    if not lines or any(not line.strip() for line in lines):
        raise GoalV1Error("goal_v1 must contain nonempty physical lines")
    masked_goal = _mask_literals_for_target(_mask_comments(goal).masked)
    masked_lines = masked_goal.splitlines()
    if masked_goal.count("⊢") != 1 or not masked_lines[-1].startswith("⊢ "):
        raise GoalV1Error("goal_v1 must contain exactly one final turnstile target")
    if any("⊢" in line for line in masked_lines[:-1]):
        raise GoalV1Error("goal_v1 local lines must not contain a turnstile")
    local_identities: list[str] = []
    for line in lines[:-1]:
        local_split = _split_top_level_once(line, " : ")
        if local_split is None or not local_split[0].strip():
            raise GoalV1Error("every goal_v1 local line must contain a structural ' : '")
        names, type_text = local_split
        local_identities.extend(
            _name_identity(name) for name in _parse_names(names, allow_generated=True)
        )
        canonical_type = _canonicalize_binding_expression(type_text, allow_generated_names=True)
        if canonical_type != type_text:
            raise GoalV1Error("goal_v1 local type uses noncanonical 'have'; use 'let'")
    if len(local_identities) != len(set(local_identities)):
        raise GoalV1Error("goal_v1 local names must be unique after Lean-name normalization")
    target = lines[-1][2:]
    canonical_target = _canonicalize_binding_expression(target, allow_generated_names=True)
    if canonical_target != target:
        raise GoalV1Error("goal_v1 target uses noncanonical 'have'; use 'let'")


def signature_to_goal_v1(signature: str) -> str:
    """Render a trusted name-free theorem signature without invoking Lean."""

    masked_signature = _canonicalize_surface_universe_names(
        _mask_comments(signature).masked.strip()
    )
    if _SYNTAX_QUOTATION.search(_mask_literals_for_target(masked_signature)):
        raise SurfaceRenderError(
            SurfaceFailureCode.SYNTAX_QUOTATION,
            "Lean name/syntax quotations are outside the bounded surface grammar",
        )
    binders, after_binders = _parse_leading_binders(masked_signature)
    target_split = _split_top_level_once(after_binders, ":")
    if target_split is None:
        raise SurfaceRenderError(
            SurfaceFailureCode.MISSING_TARGET_SEPARATOR,
            "signature has no top-level ':' before its target",
        )
    before_target, target = target_split
    if before_target.strip():
        raise SurfaceRenderError(
            SurfaceFailureCode.MISSING_TARGET_SEPARATOR,
            f"unsupported text before target separator: {before_target!r}",
        )
    forall_binders, target = _peel_forall_binders(target)
    binders.extend(forall_binders)
    target = _canonicalize_surface_target(target)
    names = [_name_identity(name) for binder in binders for name in binder.names]
    if len(names) != len(set(names)):
        raise SurfaceRenderError(
            SurfaceFailureCode.DUPLICATE_LOCAL_NAME,
            "surface mode cannot reproduce Lean's sanitized shadowed local names",
        )
    goal = "\n".join([*_group_binders(binders), f"⊢ {target}"])
    try:
        validate_goal_v1(goal)
    except GoalV1Error as exc:
        raise SurfaceRenderError(SurfaceFailureCode.INVALID_GOAL, str(exc)) from exc
    return goal


def _strip_helper_imports(source: str) -> str:
    return "\n".join(line for line in source.splitlines() if not line.startswith("import "))


@cache
def _helper_body() -> str:
    helper_path = find_repo_root(Path(__file__).parent) / "LeanFaith" / "Meta" / "GoalV1.lean"
    helper_bytes = helper_path.read_bytes()
    helper_hash = sha256_hex(helper_bytes)
    if helper_hash != PINNED_LEAN_RENDERER_SHA256:
        raise RuntimeError(
            "refusing to inject unpinned GoalV1.lean: "
            f"expected {PINNED_LEAN_RENDERER_SHA256}, got {helper_hash}"
        )
    try:
        helper_source = helper_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("GoalV1.lean is not valid UTF-8") from exc
    body = _strip_helper_imports(helper_source)
    body_hash = sha256_hex(body.encode("utf-8"))
    if body_hash != PINNED_INJECTED_HELPER_SHA256:
        raise RuntimeError(
            "refusing to inject a helper body that does not match its pin: "
            f"expected {PINNED_INJECTED_HELPER_SHA256}, got {body_hash}"
        )
    return body


@cache
def _implementation_identity() -> RendererImplementationIdentity:
    repo_root = find_repo_root(Path(__file__).parent)
    _helper_body()
    payload = {
        "renderer_semantic_hash": RENDERER_SEMANTIC_HASH,
        "lean_renderer_sha256": PINNED_LEAN_RENDERER_SHA256,
        "injected_helper_sha256": PINNED_INJECTED_HELPER_SHA256,
        "python_module_sha256": sha256_hex(Path(__file__).read_bytes()),
        "config_file_sha256": sha256_hex(
            (repo_root / "configs" / "representations" / "goal_v1_v1.yaml").read_bytes()
        ),
    }
    return RendererImplementationIdentity(
        renderer_semantic_hash=RENDERER_SEMANTIC_HASH,
        lean_renderer_sha256=PINNED_LEAN_RENDERER_SHA256,
        injected_helper_sha256=PINNED_INJECTED_HELPER_SHA256,
        python_module_sha256=str(payload["python_module_sha256"]),
        config_file_sha256=str(payload["config_file_sha256"]),
        implementation_set_hash=hash_canonical(payload),
    )


def _build_sidecar(
    *,
    goal_v1: str,
    source: GoalV1Source,
    raw_statement: str,
    declaration_kind: str,
    compile_context: CompileContext,
    typed_alpha_fingerprint: str | None = None,
    warnings: tuple[str, ...] = (),
) -> GoalV1Sidecar:
    validate_goal_v1(goal_v1)
    implementation_identity = _implementation_identity()
    raw_hash = sha256_hex(raw_statement.encode("utf-8"))
    representation_id = "repr:" + hash_canonical(
        {
            "renderer_version": RENDERER_VERSION,
            "spec_hash": SPEC_HASH,
            "goal_v1_source": source,
            "goal_v1": goal_v1,
            "raw_statement_hash": raw_hash,
            "declaration_kind": declaration_kind,
            "compile_context_id": compile_context.compile_context_id,
            "implementation_identity": implementation_identity.to_dict(),
        }
    )
    return GoalV1Sidecar(
        record=GoalV1Record(
            representation_id=representation_id,
            goal_v1=goal_v1,
            goal_v1_source=source,
            renderer_version=RENDERER_VERSION,
            spec_hash=SPEC_HASH,
            raw_statement_hash=raw_hash,
            declaration_kind=declaration_kind,
            compile_context_id=compile_context.compile_context_id,
            implementation_identity=implementation_identity,
            typed_alpha_fingerprint=typed_alpha_fingerprint,
            warnings=warnings,
        ),
        raw_statement=raw_statement,
        compile_context=compile_context,
    )


def render_surface(
    *,
    raw_statement: str,
    declaration_kind: str,
    compile_context: CompileContext,
    parsed_signature: str | None = None,
) -> GoalV1Sidecar:
    """Render a caller-supplied trusted signature while retaining raw source."""

    _validate_kind(declaration_kind)
    if not raw_statement.strip():
        raise ValueError("surface raw_statement must be nonempty")
    if parsed_signature is None:
        raise SurfaceRenderError(
            SurfaceFailureCode.AMBIGUOUS_PROOF_BOUNDARY,
            "goal_v1.0 never infers a signature/proof boundary from raw source; "
            "provide a trusted complete parsed_signature",
        )
    goal = signature_to_goal_v1(parsed_signature)
    return _build_sidecar(
        goal_v1=goal,
        source="surface",
        raw_statement=raw_statement,
        declaration_kind=declaration_kind,
        compile_context=compile_context,
        warnings=(SURFACE_PROVENANCE_TAG,),
    )


def _lean_option_value(value: CompileOptionValue) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _qualified_declaration_name(
    compile_context: CompileContext,
    declaration_name: str,
    *,
    lookup_only: bool,
) -> str:
    if not compile_context.namespace_context or lookup_only:
        return declaration_name
    prefix = ".".join(compile_context.namespace_context) + "."
    if not declaration_name.startswith(prefix):
        raise ValueError(
            f"declaration {declaration_name!r} must be fully qualified with {prefix!r} "
            "when namespace_context is nonempty"
        )
    return declaration_name


def _elaborated_command(
    compile_context: CompileContext,
    declarations: Sequence[ElaboratedInput],
) -> str:
    import_lines = [
        line.strip() for line in compile_context.import_header.splitlines() if line.strip()
    ]
    imports = "\n".join(["import Lean", *(line for line in import_lines if line != "import Lean")])
    lines = [imports, _helper_body()]
    if compile_context.command_preamble.strip():
        lines.append(compile_context.command_preamble.rstrip())
    lines.extend(
        f"set_option {option_name} {_lean_option_value(value)}"
        for option_name, value in sorted(compile_context.options.items())
    )
    if compile_context.open_context:
        lines.append("open " + " ".join(compile_context.open_context))
    if compile_context.scoped_context:
        lines.append("open scoped " + " ".join(compile_context.scoped_context))
    lines.extend(f"namespace {name}" for name in compile_context.namespace_context)
    lines.extend(item.raw_statement.rstrip() for item in declarations if not item.lookup_only)
    lines.extend(f"end {name}" for name in reversed(compile_context.namespace_context))
    lines.extend(
        "lfGoalV1 "
        + json.dumps(
            _qualified_declaration_name(
                compile_context,
                item.declaration_name,
                lookup_only=item.lookup_only,
            ),
            ensure_ascii=False,
        )
        for item in declarations
    )
    return "\n".join(line for line in lines if line.strip())


def _parse_goal_payloads(
    messages: Sequence[dict[str, object]],
    expected_names: set[str],
) -> dict[str, tuple[str | None, str | None]]:
    selected: dict[str, tuple[str | None, str | None]] = {}
    for message in messages:
        for line in str(message.get("data", "")).splitlines():
            marker = line.find(GOAL_MARKER)
            if marker < 0:
                continue
            try:
                payload = json.loads(line[marker + len(GOAL_MARKER) :])
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            name = payload.get("name")
            if not isinstance(name, str) or name not in expected_names:
                continue
            value = payload.get("goal_v1")
            constant_kind = payload.get("constant_kind")
            # Last matching helper payload is authoritative. A not-found or
            # malformed later payload clears a source-authored spoof.
            selected[name] = (
                value if isinstance(value, str) else None,
                constant_kind if isinstance(constant_kind, str) else None,
            )
    return selected


def _messages_report_sorry(messages: Sequence[dict[str, object]]) -> bool:
    return any(
        str(message.get("severity", "")).lower() in {"warning", "error"}
        and any(marker in str(message.get("data", "")) for marker in _SORRY_DIAGNOSTICS)
        for message in messages
    )


_FORBIDDEN_CLOSED_EXPR_SESSION = re.compile(
    r"(?m)^\s*(?:(?:private|protected|public|noncomputable|unsafe)\s+)*"
    r"(?:theorem|lemma|axiom|opaque|example)\b|"
    r":=\s*by\b|\bsorry\b|sorryAx|mkSorry|addDecl|addAndCompile|ppGoal"
)
_FORBIDDEN_CLOSED_EXPR_ACTION_DECLARATION = re.compile(
    r"(?m)^\s*(?:(?:private|protected|public|noncomputable|unsafe)\s+)*"
    r"(?:abbrev|axiom|class|def|elab|example|inductive|instance|lemma|macro|opaque|"
    r"structure|syntax|theorem)\b"
)
_FORBIDDEN_CLOSED_EXPR_RUNTIME = re.compile(
    r"Term\.elabTerm|Parser\.runParserCategory|lfTextElaboratesAs|"
    r"lfCandidateEmission\?|lfTransformPp|\bppExpr\b|addDecl|addAndCompile|mkSorry|sorryAx|"
    r"IO\.(?:print|println|eprint|eprintln|getStdout|getStderr)|\bputStr(?:Ln)?\b|"
    r"log(?:Info|Warning|Error)(?:At)?|logMessage|modifyMessageLog|\btrace\b"
)
_CLOSED_EXPR_EMITTER_CALL = re.compile(r"(?<![\w'.])LeanFaith\.GoalV1\.emitClosedProp(?![\w'])")
_RUN_META_COMMAND = re.compile(r"(?m)^\s*run_meta\s+do\b")


def _closed_expr_command(compile_context: CompileContext, session_body: str) -> str:
    import_lines = [
        line.strip() for line in compile_context.import_header.splitlines() if line.strip()
    ]
    imports = "\n".join(["import Lean", *(line for line in import_lines if line != "import Lean")])
    lines = [imports, _helper_body()]
    if compile_context.command_preamble.strip():
        lines.append(compile_context.command_preamble.rstrip())
    lines.extend(
        f"set_option {option_name} {_lean_option_value(value)}"
        for option_name, value in sorted(compile_context.options.items())
    )
    if compile_context.open_context:
        lines.append("open " + " ".join(compile_context.open_context))
    if compile_context.scoped_context:
        lines.append("open scoped " + " ".join(compile_context.scoped_context))
    lines.extend(f"namespace {name}" for name in compile_context.namespace_context)
    lines.append(session_body.rstrip())
    lines.extend(f"end {name}" for name in reversed(compile_context.namespace_context))
    return "\n".join(line for line in lines if line.strip())


def _parse_closed_expr_payloads(
    messages: Sequence[dict[str, object]],
    expected_endpoint_ids: set[str],
) -> tuple[dict[str, dict[str, object]], tuple[str, ...]]:
    selected: dict[str, dict[str, object]] = {}
    issues: list[str] = []
    for message in messages:
        for line in str(message.get("data", "")).splitlines():
            marker = line.find(CLOSED_EXPR_MARKER)
            if marker < 0:
                continue
            try:
                payload = json.loads(line[marker + len(CLOSED_EXPR_MARKER) :])
            except json.JSONDecodeError:
                issues.append("malformed LFGOALV1EXPRJSON payload")
                continue
            if not isinstance(payload, dict):
                issues.append("non-object LFGOALV1EXPRJSON payload")
                continue
            endpoint_id = payload.get("endpoint_id")
            if not isinstance(endpoint_id, str) or endpoint_id not in expected_endpoint_ids:
                issues.append("unexpected LFGOALV1EXPRJSON endpoint")
                continue
            if endpoint_id in selected:
                issues.append(f"duplicate LFGOALV1EXPRJSON endpoint {endpoint_id!r}")
                continue
            selected[endpoint_id] = payload
    return selected, tuple(issues)


def _string_tuple(value: object, *, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise GoalV1Error(f"closed Expr payload {field_name} must be a string array")
    return tuple(value)


def _require_exact_json_keys(
    value: Mapping[str, object],
    expected: set[str],
    *,
    label: str,
) -> None:
    actual = set(value)
    if actual != expected:
        raise GoalV1Error(
            f"closed Expr {label} keys mismatch: "
            f"missing={sorted(expected - actual)!r}, extra={sorted(actual - expected)!r}"
        )


def _validate_closed_level_tree(
    value: object,
    *,
    level_params: list[str],
) -> None:
    if not isinstance(value, dict):
        raise GoalV1Error("closed Expr level node must be a JSON object")
    kind = value.get("k")
    if kind == "zero":
        _require_exact_json_keys(value, {"k"}, label="zero-level node")
    elif kind == "succ":
        _require_exact_json_keys(value, {"k", "level"}, label="succ-level node")
        _validate_closed_level_tree(value["level"], level_params=level_params)
    elif kind in {"max", "imax"}:
        _require_exact_json_keys(value, {"k", "left", "right"}, label=f"{kind}-level node")
        _validate_closed_level_tree(value["left"], level_params=level_params)
        _validate_closed_level_tree(value["right"], level_params=level_params)
    elif kind == "param":
        _require_exact_json_keys(value, {"k", "name"}, label="level-param node")
        name = value["name"]
        if not isinstance(name, str) or not name:
            raise GoalV1Error("closed Expr level parameter name must be nonempty")
        if name not in level_params:
            level_params.append(name)
    elif kind == "mvar":
        raise GoalV1Error("closed Expr tree contains an unresolved universe metavariable")
    else:
        raise GoalV1Error(f"closed Expr level node has unsupported kind {kind!r}")


def _validate_closed_expr_tree_node(
    value: object,
    *,
    binder_depth: int,
    level_params: list[str],
) -> None:
    if not isinstance(value, dict):
        raise GoalV1Error("closed Expr node must be a JSON object")
    kind = value.get("k")
    if kind in {"forall", "lambda"}:
        _require_exact_json_keys(
            value,
            {"k", "binder_info", "domain", "body"},
            label=f"{kind} node",
        )
        if value["binder_info"] not in {
            "default",
            "implicit",
            "strictImplicit",
            "instImplicit",
        }:
            raise GoalV1Error(f"closed Expr {kind} node has unsupported binder_info")
        _validate_closed_expr_tree_node(
            value["domain"],
            binder_depth=binder_depth,
            level_params=level_params,
        )
        _validate_closed_expr_tree_node(
            value["body"],
            binder_depth=binder_depth + 1,
            level_params=level_params,
        )
    elif kind == "app":
        _require_exact_json_keys(value, {"k", "fn", "arg"}, label="app node")
        _validate_closed_expr_tree_node(
            value["fn"], binder_depth=binder_depth, level_params=level_params
        )
        _validate_closed_expr_tree_node(
            value["arg"], binder_depth=binder_depth, level_params=level_params
        )
    elif kind == "const":
        _require_exact_json_keys(value, {"k", "name", "levels"}, label="const node")
        name = value["name"]
        levels = value["levels"]
        if not isinstance(name, str) or not name:
            raise GoalV1Error("closed Expr constant name must be nonempty")
        if name in {"sorryAx", "Lean.sorryAx"}:
            raise GoalV1Error("closed Expr tree contains sorryAx")
        if not isinstance(levels, list):
            raise GoalV1Error("closed Expr constant levels must be a JSON array")
        for level in levels:
            _validate_closed_level_tree(level, level_params=level_params)
    elif kind == "bvar":
        _require_exact_json_keys(value, {"k", "index"}, label="bvar node")
        index = value["index"]
        if type(index) is not int or not 0 <= index < binder_depth:
            raise GoalV1Error("closed Expr tree contains a loose or malformed bound variable")
    elif kind == "sort":
        _require_exact_json_keys(value, {"k", "level"}, label="sort node")
        _validate_closed_level_tree(value["level"], level_params=level_params)
    elif kind == "literal":
        if set(value) == {"k", "nat"}:
            nat = value["nat"]
            if not isinstance(nat, str) or re.fullmatch(r"0|[1-9][0-9]*", nat) is None:
                raise GoalV1Error("closed Expr natural literal is not canonical decimal text")
        elif set(value) == {"k", "string"}:
            if not isinstance(value["string"], str):
                raise GoalV1Error("closed Expr string literal must be text")
        else:
            raise GoalV1Error("closed Expr literal node has unknown or extra fields")
    elif kind == "projection":
        _require_exact_json_keys(
            value,
            {"k", "type_name", "index", "base"},
            label="projection node",
        )
        type_name = value["type_name"]
        index = value["index"]
        if not isinstance(type_name, str) or not type_name:
            raise GoalV1Error("closed Expr projection type name must be nonempty")
        if type(index) is not int or index < 0:
            raise GoalV1Error("closed Expr projection index must be a nonnegative integer")
        _validate_closed_expr_tree_node(
            value["base"], binder_depth=binder_depth, level_params=level_params
        )
    elif kind == "let":
        _require_exact_json_keys(
            value,
            {"k", "type", "value", "body", "nondependent"},
            label="let node",
        )
        if type(value["nondependent"]) is not bool:
            raise GoalV1Error("closed Expr let nondependent flag must be boolean")
        _validate_closed_expr_tree_node(
            value["type"], binder_depth=binder_depth, level_params=level_params
        )
        _validate_closed_expr_tree_node(
            value["value"], binder_depth=binder_depth, level_params=level_params
        )
        _validate_closed_expr_tree_node(
            value["body"],
            binder_depth=binder_depth + 1,
            level_params=level_params,
        )
    elif kind in {"fvar", "mvar"}:
        raise GoalV1Error(f"closed Expr tree contains forbidden {kind} node")
    else:
        raise GoalV1Error(f"closed Expr node has unsupported kind {kind!r}")


def _validate_closed_expr_tree(
    value: object,
    *,
    canonical_level_params: tuple[str, ...],
) -> dict[str, object]:
    level_params: list[str] = []
    _validate_closed_expr_tree_node(value, binder_depth=0, level_params=level_params)
    if tuple(level_params) != canonical_level_params:
        raise GoalV1Error(
            "closed Expr tree level parameters do not match the frozen first-occurrence profile"
        )
    assert isinstance(value, dict)
    return value


def _closed_expr_sidecar_from_payload(
    *,
    payload: Mapping[str, object],
    item: ClosedExprInput,
    compile_context: CompileContext,
    render_scope_id: str,
    implementation_identity: RendererImplementationIdentity,
) -> ClosedExprSidecar:
    required_fields = {
        "schema_version",
        "endpoint_id",
        "goal_v1",
        "goal_v1_source",
        "route_id",
        "expr_origin",
        "expr_hash_algorithm",
        "expr_tree",
        "input_level_params",
        "canonical_level_params",
        "render_scope_id",
        "universe_profile_id",
        "universe_profile_hash",
        "renderer_semantic_hash",
        "render_context_id",
        "render_context_hash",
    }
    _require_exact_json_keys(payload, required_fields, label="payload")
    exact_fields = {
        "schema_version": 1,
        "endpoint_id": item.endpoint_id,
        "goal_v1_source": "closed_prop_expr",
        "route_id": CLOSED_EXPR_ROUTE_ID,
        "expr_origin": item.expr_origin,
        "expr_hash_algorithm": CLOSED_EXPR_HASH_ALGORITHM,
        "render_scope_id": render_scope_id,
        "universe_profile_id": CANONICAL_UNIVERSE_PROFILE_ID,
        "universe_profile_hash": CANONICAL_UNIVERSE_PROFILE_HASH,
        "renderer_semantic_hash": RENDERER_SEMANTIC_HASH,
        "render_context_id": RENDER_CONTEXT_ID,
        "render_context_hash": RENDER_CONTEXT_HASH,
    }
    if type(payload.get("schema_version")) is not int:
        raise GoalV1Error("closed Expr payload schema_version must be the integer 1")
    for field_name, expected in exact_fields.items():
        if payload.get(field_name) != expected:
            raise GoalV1Error(
                f"closed Expr payload {field_name} mismatch: "
                f"expected {expected!r}, got {payload.get(field_name)!r}"
            )
    goal = payload.get("goal_v1")
    if not isinstance(goal, str):
        raise GoalV1Error("closed Expr payload goal_v1 must be a string")
    goal = _canonicalize_elaborated_goal(goal)
    validate_goal_v1(goal)
    input_level_params = _string_tuple(
        payload.get("input_level_params"), field_name="input_level_params"
    )
    canonical_level_params = _string_tuple(
        payload.get("canonical_level_params"), field_name="canonical_level_params"
    )
    if len(input_level_params) != len(set(input_level_params)):
        raise GoalV1Error("closed Expr input level parameters must be unique")
    expected_canonical = tuple(f"u_{index}" for index in range(len(input_level_params)))
    if canonical_level_params != expected_canonical:
        raise GoalV1Error(
            "closed Expr canonical level parameters do not follow the frozen u_i profile"
        )
    expr_tree = _validate_closed_expr_tree(
        payload.get("expr_tree"),
        canonical_level_params=canonical_level_params,
    )
    expr_hash = hash_canonical(expr_tree)
    rendered_goal_hash = sha256_hex(goal.encode("utf-8"))
    provenance = ClosedExprProvenance(
        expr_hash=expr_hash,
        expr_hash_algorithm=CLOSED_EXPR_HASH_ALGORITHM,
        input_level_params=input_level_params,
        canonical_level_params=canonical_level_params,
        universe_profile_id=CANONICAL_UNIVERSE_PROFILE_ID,
        universe_profile_hash=CANONICAL_UNIVERSE_PROFILE_HASH,
        render_scope_id=render_scope_id,
        render_context_id=RENDER_CONTEXT_ID,
        render_context_hash=RENDER_CONTEXT_HASH,
        route_id=CLOSED_EXPR_ROUTE_ID,
        expr_origin=item.expr_origin,
    )
    identity_payload = {
        "renderer_version": RENDERER_VERSION,
        "spec_hash": SPEC_HASH,
        "goal_v1_source": "closed_prop_expr",
        "goal_v1": goal,
        "rendered_goal_hash": rendered_goal_hash,
        "endpoint_id": item.endpoint_id,
        "endpoint_role": item.endpoint_role,
        "source_material_hash": item.source_material.material_hash,
        "compile_context_id": compile_context.compile_context_id,
        "provenance": provenance.to_dict(),
        "implementation_identity": implementation_identity.to_dict(),
    }
    return ClosedExprSidecar(
        record=ClosedExprRecord(
            representation_id="repr:" + hash_canonical(identity_payload),
            goal_v1=goal,
            goal_v1_source="closed_prop_expr",
            renderer_version=RENDERER_VERSION,
            spec_hash=SPEC_HASH,
            compile_context_id=compile_context.compile_context_id,
            endpoint_id=item.endpoint_id,
            endpoint_role=item.endpoint_role,
            source_material_hash=item.source_material.material_hash,
            rendered_goal_hash=rendered_goal_hash,
            provenance=provenance,
            implementation_identity=implementation_identity,
            typed_alpha_fingerprint=item.typed_alpha_fingerprint,
        ),
        source_material=item.source_material,
        compile_context=compile_context,
    )


def render_closed_expr_in_session(
    backend: LeanBackend,
    *,
    inputs: Sequence[ClosedExprInput],
    compile_context: CompileContext,
    render_scope_id: str,
    session_body: str,
    request_id: str,
    timeout_seconds: float = 300.0,
) -> ClosedExprBatchResult:
    """Render live reference/candidate Exprs in one existing-style Meta request.

    ``session_body`` constructs or retrieves the certified Exprs and calls
    ``LeanFaith.GoalV1.emitClosedProp`` while they are still in memory. Python
    never transports an Expr, inserts retained source text, or re-elaborates a
    printed candidate.
    """

    if len(inputs) < 2:
        raise ValueError("closed Expr session requires at least reference and candidate inputs")
    if not render_scope_id.strip():
        raise ValueError("closed Expr render_scope_id must be nonempty")
    if not session_body.strip():
        raise ValueError("closed Expr session_body must be nonempty")
    endpoint_ids = [item.endpoint_id for item in inputs]
    if len(endpoint_ids) != len(set(endpoint_ids)):
        raise ValueError("closed Expr endpoint IDs must be unique within a session")
    roles = {item.endpoint_role for item in inputs}
    if roles != {"reference", "candidate"}:
        raise ValueError("closed Expr session requires reference and candidate endpoint roles")
    executable_session = _mask_literals_for_target(_mask_comments(session_body).masked)
    executable_preamble = _mask_literals_for_target(
        _mask_comments(compile_context.command_preamble).masked
    )
    forbidden = _FORBIDDEN_CLOSED_EXPR_SESSION.search(
        executable_preamble + "\n" + executable_session
    )
    if forbidden is not None:
        raise ValueError(
            "closed Expr session contains forbidden declaration/proof/copied-renderer token "
            f"{forbidden.group(0)!r}"
        )
    run_meta_matches = tuple(_RUN_META_COMMAND.finditer(executable_session))
    if len(run_meta_matches) != 1:
        raise ValueError("closed Expr session must contain exactly one executable run_meta command")
    if executable_session[: run_meta_matches[0].start()].strip():
        raise ValueError(
            "closed Expr session must begin with its sole run_meta command; static project/helper "
            "setup belongs in the hash-bound compile context"
        )
    forbidden_action_declaration = _FORBIDDEN_CLOSED_EXPR_ACTION_DECLARATION.search(
        executable_session
    )
    if forbidden_action_declaration is not None:
        raise ValueError(
            "closed Expr Meta action may not contain a declaration command: "
            f"{forbidden_action_declaration.group(0)!r}"
        )
    runtime_suffix = executable_session[run_meta_matches[0].start() :]
    forbidden_runtime = _FORBIDDEN_CLOSED_EXPR_RUNTIME.search(runtime_suffix)
    if forbidden_runtime is not None:
        raise ValueError(
            "closed Expr Meta action contains a forbidden text round-trip or declaration API "
            f"{forbidden_runtime.group(0)!r}"
        )
    emitter_count = len(_CLOSED_EXPR_EMITTER_CALL.findall(runtime_suffix))
    if emitter_count != len(inputs):
        raise ValueError(
            "closed Expr Meta action must call the shared emitter exactly once per endpoint: "
            f"expected {len(inputs)}, found {emitter_count}"
        )
    if CLOSED_EXPR_MARKER.strip() in session_body:
        raise ValueError("closed Expr session may not print the payload marker directly")

    request = LeanRequest(
        request_id=request_id,
        context_id=compile_context.compile_context_id,
        code=_closed_expr_command(compile_context, session_body),
        allow_sorry=False,
        timeout_seconds=timeout_seconds,
        metadata={"goal_v1_route": CLOSED_EXPR_ROUTE_ID, "render_scope_id": render_scope_id},
    )
    result = backend.run(request)
    result_detail = result.infrastructure_error or "; ".join(
        str(message.get("data", "")) for message in result.messages
    )
    if (
        result.status != LeanStatus.VALID
        or result.sorries
        or _messages_report_sorry(result.messages)
    ):
        detail = result_detail or f"closed Expr session failed with status {result.status.value}"
        return ClosedExprBatchResult(
            sidecars=(),
            failures=tuple(ClosedExprFailure(endpoint_id, detail) for endpoint_id in endpoint_ids),
            request_hash=result.request_hash,
            elapsed_ms=result.elapsed_ms,
            raw_response_path=result.raw_response_path,
            render_scope_id=render_scope_id,
        )

    parsed, parse_issues = _parse_closed_expr_payloads(result.messages, set(endpoint_ids))
    if parse_issues:
        detail = "; ".join(parse_issues)
        return ClosedExprBatchResult(
            sidecars=(),
            failures=tuple(ClosedExprFailure(endpoint_id, detail) for endpoint_id in endpoint_ids),
            request_hash=result.request_hash,
            elapsed_ms=result.elapsed_ms,
            raw_response_path=result.raw_response_path,
            render_scope_id=render_scope_id,
        )
    implementation_identity = _implementation_identity()
    sidecars: list[ClosedExprSidecar] = []
    failures: list[ClosedExprFailure] = []
    for item in inputs:
        payload = parsed.get(item.endpoint_id)
        if payload is None:
            failures.append(
                ClosedExprFailure(item.endpoint_id, "missing or malformed LFGOALV1EXPRJSON payload")
            )
            continue
        try:
            sidecars.append(
                _closed_expr_sidecar_from_payload(
                    payload=payload,
                    item=item,
                    compile_context=compile_context,
                    render_scope_id=render_scope_id,
                    implementation_identity=implementation_identity,
                )
            )
        except (GoalV1Error, ValueError) as exc:
            failures.append(ClosedExprFailure(item.endpoint_id, str(exc)))
    if failures:
        failed_ids = {failure.endpoint_id for failure in failures}
        failures.extend(
            ClosedExprFailure(item.endpoint_id, "closed Expr session failed atomically")
            for item in inputs
            if item.endpoint_id not in failed_ids
        )
        sidecars = []
    return ClosedExprBatchResult(
        sidecars=tuple(sidecars),
        failures=tuple(failures),
        request_hash=result.request_hash,
        elapsed_ms=result.elapsed_ms,
        raw_response_path=result.raw_response_path,
        render_scope_id=render_scope_id,
    )


def render_elaborated_batch(
    backend: LeanBackend,
    *,
    declarations: Sequence[ElaboratedInput],
    compile_context: CompileContext,
    request_id: str,
    allow_sorry: bool = False,
    timeout_seconds: float = 300.0,
) -> ElaboratedBatchResult:
    """Render several constants in one request to an already-loaded backend."""

    if not declarations:
        raise ValueError("elaborated batch must contain at least one declaration")
    names = [item.declaration_name for item in declarations]
    if len(names) != len(set(names)):
        raise ValueError("elaborated declaration names must be unique within a batch")
    for item in declarations:
        _validate_kind(item.declaration_kind)
        if not item.declaration_name.strip():
            raise ValueError("elaborated declaration name must be nonempty")
        if not item.raw_statement.strip():
            raise ValueError(f"raw statement for {item.declaration_name!r} must be nonempty")
        _qualified_declaration_name(
            compile_context,
            item.declaration_name,
            lookup_only=item.lookup_only,
        )

    request = LeanRequest(
        request_id=request_id,
        context_id=compile_context.compile_context_id,
        code=_elaborated_command(compile_context, declarations),
        allow_sorry=allow_sorry,
        timeout_seconds=timeout_seconds,
    )
    result = backend.run(request)
    result_detail = result.infrastructure_error or "; ".join(
        str(message.get("data", "")) for message in result.messages
    )
    reported_sorry = (
        result.status == LeanStatus.VALID_WITH_SORRY
        or bool(result.sorries)
        or _messages_report_sorry(result.messages)
    )
    if reported_sorry and not allow_sorry:
        detail = result_detail or "Lean reported sorry but allow_sorry is false"
        failures = tuple(ElaboratedFailure(name, detail) for name in names)
        return ElaboratedBatchResult(
            sidecars=(),
            failures=failures,
            request_hash=result.request_hash,
            elapsed_ms=result.elapsed_ms,
            raw_response_path=result.raw_response_path,
        )
    processable_statuses = {
        LeanStatus.VALID,
        LeanStatus.VALID_WITH_SORRY,
        LeanStatus.INVALID,
    }
    if result.status not in processable_statuses:
        detail = result_detail or f"Lean renderer failed with status {result.status.value}"
        failures = tuple(ElaboratedFailure(name, detail) for name in names)
        return ElaboratedBatchResult(
            sidecars=(),
            failures=failures,
            request_hash=result.request_hash,
            elapsed_ms=result.elapsed_ms,
            raw_response_path=result.raw_response_path,
        )

    parsed = _parse_goal_payloads(result.messages, set(names))
    sidecars: list[GoalV1Sidecar] = []
    failures_list: list[ElaboratedFailure] = []
    for item in declarations:
        payload = parsed.get(item.declaration_name)
        if payload is None:
            failures_list.append(
                ElaboratedFailure(
                    item.declaration_name,
                    "missing or malformed LFGOALV1JSON payload"
                    + (f": {result_detail}" if result_detail else ""),
                )
            )
            continue
        goal, constant_kind = payload
        if goal is None:
            failures_list.append(
                ElaboratedFailure(
                    item.declaration_name,
                    "missing or malformed LFGOALV1JSON payload"
                    + (f": {result_detail}" if result_detail else ""),
                )
            )
            continue
        try:
            goal = _canonicalize_elaborated_goal(goal)
        except GoalV1Error as exc:
            failures_list.append(ElaboratedFailure(item.declaration_name, str(exc)))
            continue
        if constant_kind != "theorem":
            failures_list.append(
                ElaboratedFailure(
                    item.declaration_name,
                    f"environment constant kind is {constant_kind!r}, expected theorem",
                )
            )
            continue
        warnings = [
            "already_loaded_constant_lookup"
            if item.lookup_only
            else "inline_candidate_compiled_in_batch"
        ]
        if result.status == LeanStatus.INVALID:
            warnings.append("batch_had_lean_errors")
        if reported_sorry:
            warnings.append("compiled_with_sorry")
        try:
            sidecar = _build_sidecar(
                goal_v1=goal,
                source="elaborated",
                raw_statement=item.raw_statement,
                declaration_kind=item.declaration_kind,
                compile_context=compile_context,
                typed_alpha_fingerprint=item.typed_alpha_fingerprint,
                warnings=tuple(warnings),
            )
        except GoalV1Error as exc:
            failures_list.append(ElaboratedFailure(item.declaration_name, str(exc)))
            continue
        sidecars.append(sidecar)
    return ElaboratedBatchResult(
        sidecars=tuple(sidecars),
        failures=tuple(failures_list),
        request_hash=result.request_hash,
        elapsed_ms=result.elapsed_ms,
        raw_response_path=result.raw_response_path,
    )
