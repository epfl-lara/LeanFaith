"""Pure, bounded checks for the frozen goal_v1.0 contract."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest
import yaml

from leanfaith.config.hashing import hash_canonical, sha256_hex
from leanfaith.config.paths import find_repo_root
from leanfaith.lean.protocol import LeanRequest, LeanResult, LeanStatus
from leanfaith.representations.goal_v1 import (
    GOAL_MARKER,
    RENDERER_VERSION,
    SPEC_HASH,
    SPEC_PAYLOAD,
    CompileContext,
    ElaboratedInput,
    SurfaceRenderError,
    render_elaborated_batch,
    render_surface,
    signature_to_goal_v1,
)

_REPO_ROOT = find_repo_root(Path(__file__).parent)
_CONFIG = _REPO_ROOT / "configs" / "representations" / "goal_v1_v1.yaml"


def _context(**overrides: object) -> CompileContext:
    values: dict[str, object] = {
        "project_id": "fixtures",
        "project_revision": "workspace",
        "lean_version": "v4.31.0-rc1",
        "import_header": "import LeanFaithFixtures",
        "command_preamble": "set_option autoImplicit false",
        "namespace_context": (),
        "open_context": ("Nat",),
        "scoped_context": (),
        "options": {"autoImplicit": False, "maxHeartbeats": 0},
    }
    values.update(overrides)
    return CompileContext(**values)  # type: ignore[arg-type]


def _load_config() -> dict[str, object]:
    loaded = yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return cast(dict[str, object], loaded)


def test_spec_hash_and_yaml_freeze_match() -> None:
    config = _load_config()

    assert hash_canonical(SPEC_PAYLOAD) == SPEC_HASH
    assert config["status"] == "frozen"
    assert config["spec"] == SPEC_PAYLOAD
    assert config["spec_hash"] == SPEC_HASH
    assert RENDERER_VERSION == "goal_v1.0"


def test_config_pins_the_exact_renderer_and_python_sources() -> None:
    config = _load_config()
    sources = cast(dict[str, dict[str, str]], config["implementation_sources"])

    for source in sources.values():
        source_path = _REPO_ROOT / source["path"]
        assert sha256_hex(source_path.read_bytes()) == source["sha256"]


def test_one_example_surface_smoke_is_joinable_without_an_inverse() -> None:
    config = _load_config()
    smoke = cast(dict[str, str], config["one_example_smoke"])
    context = _context()

    sidecar = render_surface(
        raw_statement=smoke["raw_statement"],
        declaration_kind="theorem",
        compile_context=context,
    )

    assert sidecar.core_text() == smoke["expected_goal_v1"]
    assert sidecar.record.goal_v1_source == "surface"
    assert sidecar.record.renderer_version == RENDERER_VERSION
    assert sidecar.record.spec_hash == SPEC_HASH
    assert sidecar.record.raw_statement_hash == sha256_hex(smoke["raw_statement"].encode("utf-8"))
    assert sidecar.record.compile_context_id == context.compile_context_id
    assert sidecar.raw_statement == smoke["raw_statement"]
    assert "goalV1Smoke" not in sidecar.core_text()
    assert "Nat.le_of_lt" not in sidecar.core_text()
    assert ":=" not in sidecar.core_text()
    assert sidecar.core_text().count("⊢") == 1
    assert json.dumps(sidecar.to_dict(), sort_keys=True, ensure_ascii=False)


def test_compile_context_hash_is_deterministic_but_context_sensitive() -> None:
    first = _context(options={"maxHeartbeats": 0, "autoImplicit": False})
    reordered = _context(options={"autoImplicit": False, "maxHeartbeats": 0})
    changed = _context(import_header="import Lean")

    assert first.compile_context_id == reordered.compile_context_id
    assert first.compile_context_id != changed.compile_context_id
    assert first.compile_context_id == f"ctx:{first.fingerprint}"


def test_compile_context_rejects_non_import_commands_in_import_header() -> None:
    with pytest.raises(ValueError, match="import commands only"):
        _context(import_header="import Lean\nopen Nat")


def test_dependent_universe_and_named_instance_surface_view() -> None:
    raw = """universe u
theorem complex {α : Type u} [inst : Inhabited α] (x : α)
    (h : ∀ y : α, y = y) : ((fun z => z) x = x) ∧ x = x := by
  exact ⟨rfl, h x⟩
"""

    sidecar = render_surface(
        raw_statement=raw,
        declaration_kind="theorem",
        compile_context=_context(command_preamble="universe u"),
    )

    assert sidecar.core_text() == (
        "α : Type u\ninst : Inhabited α\nx : α\nh : ∀ y : α, y = y\n⊢ ((fun z => z) x = x) ∧ x = x"
    )


def test_top_level_named_forall_moves_into_local_context() -> None:
    assert signature_to_goal_v1(": ∀ (x y : Nat), x = x") == "x y : Nat\n⊢ x = x"


def test_comment_tokens_inside_strings_do_not_change_the_signature() -> None:
    raw = """@[simp] theorem stringComment (s : String)
      (h : s = "-- not a comment /- either -/") : s = s := by
      have proofOnlySentinel : String := "must not leak"
      rfl
    """

    sidecar = render_surface(
        raw_statement=raw,
        declaration_kind="theorem",
        compile_context=_context(),
    )

    assert sidecar.core_text() == ('s : String\nh : s = "-- not a comment /- either -/"\n⊢ s = s')
    assert "proofOnlySentinel" not in sidecar.core_text()
    assert "must not leak" not in sidecar.core_text()


def test_declaration_keywords_inside_strings_and_guillemet_names_are_not_candidates() -> None:
    raw = """theorem «theorem» (s : String) (h : s = "lemma def theorem") : s = s := rfl"""

    sidecar = render_surface(
        raw_statement=raw,
        declaration_kind="theorem",
        compile_context=_context(),
    )

    assert sidecar.core_text() == 's : String\nh : s = "lemma def theorem"\n⊢ s = s'


def test_equation_style_proof_without_colon_equals_fails_closed() -> None:
    raw = """theorem equationStyle : Nat → Nat
      | 0 => 0
      | n => n
    """

    with pytest.raises(SurfaceRenderError) as raised:
        render_surface(
            raw_statement=raw,
            declaration_kind="theorem",
            compile_context=_context(),
        )

    assert raised.value.code.value == "ambiguous_proof_boundary"


@pytest.mark.parametrize(
    ("raw", "kind", "expected_code"),
    [
        ("def rejected (x : Nat) : Nat := x", "def", "unsupported_declaration_kind"),
        (
            "theorem anonInst {α : Type} [Inhabited α] (x : α) : True := by trivial",
            "theorem",
            "anonymous_instance_binder",
        ),
        (
            "theorem arrow : True → True := fun h => h",
            "theorem",
            "anonymous_top_level_arrow",
        ),
        (
            "theorem shadow (x : Nat) (x : Nat) : True := by trivial",
            "theorem",
            "duplicate_or_shadowed_local_name",
        ),
    ],
)
def test_surface_ambiguities_fail_closed(raw: str, kind: str, expected_code: str) -> None:
    with pytest.raises(SurfaceRenderError) as raised:
        render_surface(
            raw_statement=raw,
            declaration_kind=kind,
            compile_context=_context(),
        )

    assert raised.value.code.value == expected_code


def test_bounded_multi_source_surface_pilot_matches_declared_outcomes() -> None:
    config = _load_config()
    pilot = cast(dict[str, object], config["pilot"])
    cases = cast(list[dict[str, str]], pilot["cases"])
    consistency_case = next(
        case for case in cases if case["case_id"] == "consistency_check_amc12a_2019_p21"
    )
    assert consistency_case["fixture_kind"] == "derived_from_upstream_formal_statement"
    assert consistency_case["upstream_field"] == "formal_statement"
    assert consistency_case["upstream_formal_statement_sha256"] == (
        "bf963db06cfe1d75498daba3defa86a95bf720d225b21f57f496b82c027c1ddc"
    )
    assert consistency_case["upstream_goal_sha256"] == (
        "1b04a53200851b24a7d89474957231af6ad25e51d6a895f84d54ab1bcfee292b"
    )
    successes = 0
    expected_failures = 0

    for case in cases:
        if "expected_failure" in case:
            with pytest.raises(SurfaceRenderError) as raised:
                render_surface(
                    raw_statement=case["raw_statement"],
                    declaration_kind="theorem",
                    compile_context=_context(
                        project_id=case["source_family"],
                        project_revision=case["source_revision"],
                    ),
                    parsed_signature=case["parsed_signature"],
                )
            assert raised.value.code.value == case["expected_failure"]
            expected_failures += 1
            continue
        sidecar = render_surface(
            raw_statement=case["raw_statement"],
            declaration_kind="theorem",
            compile_context=_context(
                project_id=case["source_family"],
                project_revision=case["source_revision"],
            ),
            parsed_signature=case["parsed_signature"],
        )
        assert sidecar.core_text() == case["expected_goal_v1"]
        assert sidecar.record.goal_v1_source == "surface"
        assert sidecar.raw_statement == case["raw_statement"]
        successes += 1

    assert len(cases) == 6
    assert successes == 4
    assert expected_failures == 2


class _OneRequestBackend:
    def __init__(self, context: CompileContext) -> None:
        self.context = context
        self.requests: list[LeanRequest] = []

    def run(self, request: LeanRequest) -> LeanResult:
        self.requests.append(request)
        assert request.code is not None
        assert "theorem first" in request.code
        assert "lemma second" in request.code
        messages: tuple[dict[str, object], ...] = (
            {
                "severity": "info",
                "data": GOAL_MARKER
                + json.dumps(
                    {
                        "name": "first",
                        "constant_kind": "theorem",
                        "goal_v1": "x : Nat\n⊢ x = x",
                    }
                ),
            },
            {
                "severity": "info",
                "data": GOAL_MARKER
                + json.dumps(
                    {
                        "name": "second",
                        "constant_kind": "theorem",
                        "goal_v1": "⊢ True",
                    }
                ),
            },
        )
        return LeanResult(
            request_id=request.request_id,
            request_hash="a" * 64,
            context_id=request.context_id,
            context_fingerprint=self.context.fingerprint,
            status=LeanStatus.VALID,
            messages=messages,
            elapsed_ms=7,
            raw_response_path="raw.json",
        )

    def run_batch(self, requests: Sequence[LeanRequest]) -> list[LeanResult]:
        raise AssertionError(f"goal_v1 uses one homogeneous request, got {len(requests)}")

    def close(self) -> None:
        return None


class _StaticBackend(_OneRequestBackend):
    def __init__(
        self,
        context: CompileContext,
        *,
        status: LeanStatus,
        messages: tuple[dict[str, object], ...],
    ) -> None:
        super().__init__(context)
        self.status = status
        self.messages = messages

    def run(self, request: LeanRequest) -> LeanResult:
        self.requests.append(request)
        return LeanResult(
            request_id=request.request_id,
            request_hash="b" * 64,
            context_id=request.context_id,
            context_fingerprint=self.context.fingerprint,
            status=self.status,
            messages=self.messages,
            elapsed_ms=5,
        )


def test_elaborated_batch_uses_one_protocol_request_and_preserves_raw_source() -> None:
    context = _context()
    backend = _OneRequestBackend(context)
    declarations = (
        ElaboratedInput("first", "theorem", "theorem first (x : Nat) : x = x := rfl"),
        ElaboratedInput("second", "lemma", "lemma second : True := True.intro"),
    )

    result = render_elaborated_batch(
        backend,
        declarations=declarations,
        compile_context=context,
        request_id="goal-v1-batch",
    )

    assert len(backend.requests) == 1
    assert not result.failures
    assert [sidecar.record.goal_v1_source for sidecar in result.sidecars] == [
        "elaborated",
        "elaborated",
    ]
    assert [sidecar.raw_statement for sidecar in result.sidecars] == [
        item.raw_statement for item in declarations
    ]
    assert result.request_hash == "a" * 64
    assert result.elapsed_ms == 7
    assert result.raw_response_path == "raw.json"


def test_elaborated_command_applies_context_and_skips_loaded_constant_source() -> None:
    context = _context(
        command_preamble='namespace GoalV1Scope\nscoped notation "GOALV1NAT" => Nat\nend GoalV1Scope',
        namespace_context=("Outer", "Inner"),
        open_context=("Nat",),
        scoped_context=("GoalV1Scope",),
        options={"Elab.async": False, "maxHeartbeats": 0},
    )
    inline_raw = "theorem inline : True := True.intro"
    loaded_raw = "theorem lf_add_comm (x y : Nat) : x + y = y + x := Nat.add_comm x y"
    messages: tuple[dict[str, object], ...] = (
        {
            "data": GOAL_MARKER
            + json.dumps(
                {
                    "name": "Outer.Inner.inline",
                    "constant_kind": "theorem",
                    "goal_v1": "⊢ True",
                }
            )
        },
        {
            "data": GOAL_MARKER
            + json.dumps(
                {
                    "name": "lf_add_comm",
                    "constant_kind": "theorem",
                    "goal_v1": "x y : Nat\n⊢ x + y = y + x",
                }
            )
        },
    )
    backend = _StaticBackend(context, status=LeanStatus.VALID, messages=messages)
    declarations = (
        ElaboratedInput("Outer.Inner.inline", "theorem", inline_raw),
        ElaboratedInput("lf_add_comm", "theorem", loaded_raw, lookup_only=True),
    )

    result = render_elaborated_batch(
        backend,
        declarations=declarations,
        compile_context=context,
        request_id="goal-v1-context",
    )

    assert not result.failures
    code = backend.requests[0].code
    assert code is not None
    assert 'scoped notation "GOALV1NAT" => Nat' in code
    assert "set_option Elab.async false" in code
    assert "set_option maxHeartbeats 0" in code
    assert "open Nat" in code
    assert "open scoped GoalV1Scope" in code
    assert "namespace Outer\nnamespace Inner" in code
    assert "end Inner\nend Outer" in code
    assert inline_raw in code
    assert loaded_raw not in code
    assert result.sidecars[1].raw_statement == loaded_raw
    assert result.sidecars[1].record.warnings == ("already_loaded_constant_lookup",)


def test_elaborated_kind_is_checked_against_environment() -> None:
    context = _context()
    messages: tuple[dict[str, object], ...] = (
        {
            "data": GOAL_MARKER
            + json.dumps(
                {
                    "name": "notATheorem",
                    "constant_kind": "definition",
                    "goal_v1": "⊢ True",
                }
            )
        },
    )
    result = render_elaborated_batch(
        _StaticBackend(context, status=LeanStatus.VALID, messages=messages),
        declarations=(
            ElaboratedInput(
                "notATheorem",
                "theorem",
                "theorem notATheorem : True := True.intro",
            ),
        ),
        compile_context=context,
        request_id="goal-v1-kind",
    )

    assert not result.sidecars
    assert (
        result.failures[0].detail == "environment constant kind is 'definition', expected theorem"
    )


def test_elaborated_sorry_policy_is_enforced() -> None:
    context = _context()
    messages: tuple[dict[str, object], ...] = (
        {
            "data": GOAL_MARKER
            + json.dumps(
                {
                    "name": "withSorry",
                    "constant_kind": "theorem",
                    "goal_v1": "⊢ True",
                }
            )
        },
    )
    declaration = ElaboratedInput("withSorry", "theorem", "theorem withSorry : True := by sorry")

    rejected = render_elaborated_batch(
        _StaticBackend(context, status=LeanStatus.VALID_WITH_SORRY, messages=messages),
        declarations=(declaration,),
        compile_context=context,
        request_id="goal-v1-sorry-reject",
    )
    accepted = render_elaborated_batch(
        _StaticBackend(context, status=LeanStatus.VALID_WITH_SORRY, messages=messages),
        declarations=(declaration,),
        compile_context=context,
        request_id="goal-v1-sorry-accept",
        allow_sorry=True,
    )

    assert not rejected.sidecars
    assert len(rejected.failures) == 1
    assert not accepted.failures
    assert accepted.sidecars[0].record.warnings[-1] == "compiled_with_sorry"


def test_invalid_batch_preserves_payloads_that_were_rendered() -> None:
    context = _context()
    messages: tuple[dict[str, object], ...] = (
        {
            "data": GOAL_MARKER
            + json.dumps(
                {
                    "name": "first",
                    "constant_kind": "theorem",
                    "goal_v1": "⊢ True",
                }
            )
        },
        {"severity": "error", "data": "second declaration failed"},
    )
    result = render_elaborated_batch(
        _StaticBackend(context, status=LeanStatus.INVALID, messages=messages),
        declarations=(
            ElaboratedInput("first", "theorem", "theorem first : True := True.intro"),
            ElaboratedInput("second", "theorem", "theorem second : Missing := by exact missing"),
        ),
        compile_context=context,
        request_id="goal-v1-partial",
    )

    assert [sidecar.record.goal_v1 for sidecar in result.sidecars] == ["⊢ True"]
    assert result.sidecars[0].record.warnings[-1] == "batch_had_lean_errors"
    assert [failure.declaration_name for failure in result.failures] == ["second"]


def test_elaborated_parser_takes_last_matching_payload_fail_closed() -> None:
    class _SpoofBackend(_OneRequestBackend):
        def run(self, request: LeanRequest) -> LeanResult:
            base = super().run(request)
            return LeanResult(
                request_id=base.request_id,
                request_hash=base.request_hash,
                context_id=base.context_id,
                context_fingerprint=base.context_fingerprint,
                status=base.status,
                messages=({"severity": "info", "data": f'{GOAL_MARKER}{{"name":"first"}}'},),
            )

    context = _context()
    result = render_elaborated_batch(
        _SpoofBackend(context),
        declarations=(
            ElaboratedInput("first", "theorem", "theorem first (x : Nat) : x = x := rfl"),
            ElaboratedInput("second", "lemma", "lemma second : True := True.intro"),
        ),
        compile_context=context,
        request_id="goal-v1-spoof",
    )

    assert not result.sidecars
    assert {failure.declaration_name for failure in result.failures} == {"first", "second"}


def test_goal_v1_module_does_not_import_leaninteract_backend() -> None:
    source = (_REPO_ROOT / "src" / "leanfaith" / "representations" / "goal_v1.py").read_text(
        encoding="utf-8"
    )
    assert "leanfaith.lean.leaninteract_backend" not in source
