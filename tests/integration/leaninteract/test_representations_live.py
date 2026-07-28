"""LF-014 live: build repr_v3 views via #check and environment lookup."""

from __future__ import annotations

import datetime
import json
import re
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from leanfaith.cli.pipeline import _inline_alias_of
from leanfaith.config.paths import find_repo_root
from leanfaith.lean.commands import PropositionPairSource, render_alias_preflight
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.lean.protocol import LeanRequest, LeanStatus
from leanfaith.representations.pipeline import TheoremForRepresentation, build_representations
from leanfaith.representations.views import (
    normalize_pp_universe_placeholders,
    signature_near_dup_hash,
)
from leanfaith.schemas.ids import make_id

pytestmark = [
    pytest.mark.lean,
    pytest.mark.skipif(shutil.which("lake") is None, reason="Lean toolchain unavailable"),
]

_FIXTURES = find_repo_root(Path(__file__).parent) / "tests" / "lean_fixtures"
_CTX_FP = "0" * 64
_CTX = f"ctx:{_CTX_FP}"
_UTC = datetime.datetime(2026, 7, 11, tzinfo=datetime.UTC)
_SINGLE_FIXTURE_UNIVERSE = re.compile(r"\bu(?:_\d+)?\b")


def _normalize_single_fixture_universe(signature: str) -> str:
    """Align the one named fixture universe with ``#check``'s fresh spelling."""
    return _SINGLE_FIXTURE_UNIVERSE.sub("u_0", signature)


@pytest.fixture(scope="module")
def backend(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LeanInteractBackend]:
    instance = LeanInteractBackend(
        BackendSettings(
            project_dir=_FIXTURES,
            context_fingerprint=_CTX_FP,
            environment_schema_version=1,
            raw_response_dir=tmp_path_factory.mktemp("repr_raw"),
        )
    )
    yield instance
    instance.close()


def _theorem(name: str, proof_stripped: str) -> TheoremForRepresentation:
    return TheoremForRepresentation(
        theorem_id=make_id("thm", {"name": name}),
        full_name=name,
        proof_stripped=proof_stripped,
        context_id=_CTX,
    )


def test_build_repr_v3_views_from_fixture_library(backend: LeanInteractBackend) -> None:
    theorems = [
        _theorem("lf_add_comm", "theorem lf_add_comm (x y : Nat) : x + y = y + x := by sorry"),
        _theorem("lf_zero_add", "theorem lf_zero_add (n : Nat) : 0 + n = n := by sorry"),
        _theorem("lf_trivial", "theorem lf_trivial : True := by sorry"),
    ]
    records = build_representations(
        backend, theorems, imports="import LeanFaithFixtures", created_at=_UTC
    )
    assert len(records) == 3
    by_name = {r.theorem_id: r for r in records}
    for theorem in theorems:
        record = by_name[theorem.theorem_id]
        assert record.normalization_version == "repr_v3"
        assert record.view_status["raw_proof_stripped"].value == "ok"
        assert record.view_status["headless"].value == "ok"
        # signature_pp and signature_explicit come from the live elaborator.
        assert record.signature_pp, theorem.full_name
        assert record.view_status["signature_pp"].value == "ok"
        assert record.signature_explicit
        assert record.view_status["signature_explicit"].value == "ok"

    add_comm = by_name[theorems[0].theorem_id]
    assert add_comm.headless == "(x y : Nat) : x + y = y + x"
    assert "x + y = y + x" in (add_comm.signature_pp or "")
    # A near-dup signature hash is derivable and stable.
    assert len(signature_near_dup_hash(add_comm.signature_pp or "")) == 64


def test_unknown_declaration_marks_signature_failed(backend: LeanInteractBackend) -> None:
    theorems = [_theorem("does_not_exist_xyz", "theorem does_not_exist_xyz : True := by sorry")]
    records = build_representations(
        backend, theorems, imports="import LeanFaithFixtures", created_at=_UTC
    )
    record = records[0]
    # Required v0 views still succeed from the source; only the elaborated
    # views (which need the declaration to exist) fail explicitly.
    assert record.view_status["raw_proof_stripped"].value == "ok"
    assert record.view_status["headless"].value == "ok"
    assert record.view_status["signature_pp"].value == "failed"
    assert record.signature_pp is None


def test_private_declaration_recovers_expr_views_by_environment_name(
    backend: LeanInteractBackend,
) -> None:
    theorem = TheoremForRepresentation(
        theorem_id=make_id("thm", {"name": "private-fixture"}),
        full_name="_private.0.LeanFaithPrivateFixture.hidden",
        environment_lookup_name=(
            "_private.LeanFaithFixtures.Basic.0.LeanFaithPrivateFixture.hidden"
        ),
        proof_stripped=("private theorem hidden (n : Nat) : n = n := by sorry"),
        context_id=_CTX,
    )
    (record,) = build_representations(
        backend, [theorem], imports="import LeanFaithFixtures", created_at=_UTC
    )
    assert record.alpha_identity_fingerprint is not None
    assert record.signature_pp is not None
    assert record.signature_explicit is not None
    assert record.view_status["signature_pp"].value == "ok"
    assert record.view_status["signature_explicit"].value == "ok"
    assert record.view_status["semantic_atoms"].value == "ok"
    assert record.view_status["operator_tree"].value == "ok"


def test_complex_private_signature_matches_public_alias(
    backend: LeanInteractBackend,
) -> None:
    private = TheoremForRepresentation(
        theorem_id=make_id("thm", {"name": "private-complex-fixture"}),
        full_name="_private.0.LeanFaithPrivateFixture.hiddenComplex",
        environment_lookup_name=(
            "_private.LeanFaithFixtures.Basic.0.LeanFaithPrivateFixture.hiddenComplex"
        ),
        proof_stripped=(
            "private theorem hiddenComplex {α : Type u} [Inhabited α] (x : α) "
            "(h : ∀ y : α, y = y) : ((fun z => z) x = x) ∧ x = x := by sorry"
        ),
        context_id=_CTX,
    )
    public = _theorem(
        "LeanFaithPrivateFixture.publicComplex",
        (
            "theorem publicComplex {α : Type u} [Inhabited α] (x : α) "
            "(h : ∀ y : α, y = y) : ((fun z => z) x = x) ∧ x = x := by sorry"
        ),
    )

    private_record, public_record = build_representations(
        backend,
        [private, public],
        imports="import LeanFaithFixtures",
        created_at=_UTC,
    )

    for record in (private_record, public_record):
        assert record.signature_pp
        assert record.signature_explicit
        assert record.alpha_identity_fingerprint
        assert record.semantic_atoms
        assert record.operator_tree
        assert all(
            record.view_status[view].value == "ok"
            for view in (
                "signature_pp",
                "signature_explicit",
                "semantic_atoms",
                "operator_tree",
            )
        )
    assert private_record.alpha_identity_fingerprint == public_record.alpha_identity_fingerprint
    assert _normalize_single_fixture_universe(
        normalize_pp_universe_placeholders(private_record.signature_pp or "")
    ) == _normalize_single_fixture_universe(
        normalize_pp_universe_placeholders(public_record.signature_pp or "")
    )
    assert _normalize_single_fixture_universe(
        normalize_pp_universe_placeholders(private_record.signature_explicit or "")
    ) == _normalize_single_fixture_universe(
        normalize_pp_universe_placeholders(public_record.signature_explicit or "")
    )

    preflight = render_alias_preflight(
        PropositionPairSource(
            header_text="import LeanFaithFixtures",
            proposition_a=private_record.signature_explicit or "",
            proposition_b=private_record.signature_explicit or "",
            pair_id="private-complex-signature-reuse",
        )
    )
    result = backend.run(
        LeanRequest(
            request_id="private-complex-signature-alias-preflight",
            context_id=_CTX,
            code=preflight.code,
            timeout_seconds=300.0,
        )
    )
    assert result.status == LeanStatus.VALID, result.messages


def test_semantic_atoms_and_operator_tree_live(backend: LeanInteractBackend) -> None:
    from leanfaith.representations.pipeline import build_representations

    theorems = [
        _theorem("lf_add_comm", "theorem lf_add_comm (x y : Nat) : x + y = y + x := by sorry"),
    ]
    records = build_representations(
        backend, theorems, imports="import LeanFaithFixtures", created_at=_UTC
    )
    record = records[0]
    assert record.view_status["semantic_atoms"].value == "ok", record.view_status
    assert record.view_status["operator_tree"].value == "ok"
    assert record.semantic_atoms is not None
    # The commutativity statement's atoms include the quantifiers, the addition
    # head, and equality (extracted from the elaborated Expr, not text).
    assert record.semantic_atoms.count("forall") == 2
    assert "const:HAdd.hAdd" in record.semantic_atoms
    assert "const:Eq" in record.semantic_atoms
    assert record.operator_tree is not None
    assert record.operator_tree["node_count"] > 5


def test_invalid_name_cannot_erase_valid_sibling(backend: LeanInteractBackend) -> None:
    valid = _theorem("lf_trivial", "theorem lf_trivial : True := by sorry")
    missing = _theorem("missing_sibling_xyz", "theorem missing_sibling_xyz : True := by sorry")
    records = build_representations(
        backend, [valid, missing], imports="import LeanFaithFixtures", created_at=_UTC
    )
    assert records[0].view_status["signature_pp"].value == "ok"
    assert records[0].view_status["semantic_atoms"].value == "ok"
    assert records[1].view_status["signature_pp"].value == "failed"


def test_inline_and_named_paths_have_same_identity(backend: LeanInteractBackend) -> None:
    named = _theorem("lf_add_comm", "theorem lf_add_comm (x y : Nat) : x + y = y + x := by sorry")
    inline = TheoremForRepresentation(
        theorem_id=make_id("thm", {"name": "lf_inline_add_comm"}),
        full_name="lf_inline_add_comm",
        proof_stripped=(
            "import LeanFaithFixtures\n"
            "theorem lf_inline_add_comm (x y : Nat) : x + y = y + x := by sorry"
        ),
        context_id=_CTX,
        inline_declaration=True,
    )
    records = build_representations(
        backend, [named, inline], imports="import LeanFaithFixtures", created_at=_UTC
    )
    assert records[0].alpha_identity_fingerprint == records[1].alpha_identity_fingerprint
    assert records[0].signature_explicit == records[1].signature_explicit


def test_inline_ambient_pp_options_cannot_change_signature_views(
    backend: LeanInteractBackend,
) -> None:
    clean = TheoremForRepresentation(
        theorem_id=make_id("thm", {"name": "lf_pp_clean"}),
        full_name="lf_pp_clean",
        proof_stripped=(
            'theorem lf_pp_clean (α : Type) (x : α) : x = x ∧ "a  b" = "a  b" := by sorry'
        ),
        context_id=_CTX,
        inline_declaration=True,
    )
    hostile_source = "\n".join(
        (
            "set_option pp.all true",
            "set_option pp.explicit true",
            "set_option pp.universes true",
            "set_option pp.notation false",
            "set_option pp.parens true",
            "set_option pp.mvars true",
            "set_option pp.raw true",
            "set_option pp.oneline true",
            ('theorem lf_pp_hostile (α : Type) (x : α) : x = x ∧ "a  b" = "a  b" := by sorry'),
            # Source-authored diagnostics deliberately inherit the hostile
            # options. They must not become representation signatures.
            "#check @lf_pp_hostile",
            "#check @lf_pp_hostile",
        )
    )
    hostile = TheoremForRepresentation(
        theorem_id=make_id("thm", {"name": "lf_pp_hostile"}),
        full_name="lf_pp_hostile",
        proof_stripped=(
            'theorem lf_pp_hostile (α : Type) (x : α) : x = x ∧ "a  b" = "a  b" := by sorry'
        ),
        inline_source=hostile_source,
        context_id=_CTX,
        inline_declaration=True,
    )

    clean_record, hostile_record = build_representations(
        backend,
        [clean, hostile],
        imports="import LeanFaithFixtures",
        created_at=_UTC,
    )

    assert hostile_record.signature_pp == clean_record.signature_pp
    assert hostile_record.signature_explicit == clean_record.signature_explicit
    assert '"a  b"' in (hostile_record.signature_pp or "")
    assert hostile_record.alpha_identity_fingerprint == clean_record.alpha_identity_fingerprint


def test_independent_retries_use_appended_helpers_after_inline_marker_spoofs(
    backend: LeanInteractBackend,
) -> None:
    from leanfaith.representations.pipeline import (
        _retry_environment_signature_views,
        _retry_tree_views,
    )

    name = "lf_retry_marker_spoof"
    signature_pp_spoof = f'LFSIGPPJSON {{"name":"{name}","signature_pp":"source spoof"}}'
    signature_explicit_spoof = (
        f'LFSIGEXPLICITJSON {{"name":"{name}","signature_explicit":"source spoof"}}'
    )
    tree_spoof = f'LFTREEJSON {{"name":"{name}","tree":{{"k":"const","n":"False","us":"[]"}}}}'
    assert json.loads(signature_pp_spoof.split(" ", 1)[1])["signature_pp"] == "source spoof"
    assert (
        json.loads(signature_explicit_spoof.split(" ", 1)[1])["signature_explicit"]
        == "source spoof"
    )
    assert json.loads(tree_spoof.split(" ", 1)[1])["tree"]["n"] == "False"
    inline_source = "\n".join(
        (
            f"theorem {name} : True := by trivial",
            f"#eval IO.println {json.dumps(signature_pp_spoof)}",
            f"#eval IO.println {json.dumps(signature_explicit_spoof)}",
            f"#eval IO.println {json.dumps(tree_spoof)}",
        )
    )
    theorem = TheoremForRepresentation(
        theorem_id=make_id("thm", {"name": name}),
        full_name=name,
        proof_stripped=f"theorem {name} : True := by sorry",
        inline_source=inline_source,
        context_id=_CTX,
        inline_declaration=True,
    )

    recovered_pp = _retry_environment_signature_views(
        backend,
        [theorem],
        "import LeanFaithFixtures",
        explicit=False,
    )
    recovered_explicit = _retry_environment_signature_views(
        backend,
        [theorem],
        "import LeanFaithFixtures",
        explicit=True,
    )
    recovered_tree = _retry_tree_views(
        backend,
        [theorem],
        "import LeanFaithFixtures",
    )

    assert recovered_pp[theorem.theorem_id] == "True"
    assert recovered_explicit[theorem.theorem_id] == "True"
    assert recovered_tree[theorem.theorem_id] == {
        "k": "const",
        "n": "True",
        "us": "[]",
    }


@pytest.mark.parametrize("original_name", ["lf_add_comm", "Eq.refl"])
def test_environment_inferred_alias_cross_path_reelaborates(
    backend: LeanInteractBackend,
    original_name: str,
) -> None:
    named = _theorem(original_name, f"theorem {original_name} : True := by sorry")
    (named_record,) = build_representations(
        backend, [named], imports="import LeanFaithFixtures", created_at=_UTC
    )
    assert named_record.signature_explicit is not None
    assert named_record.headless is not None
    inline_name = "lf_cross_path_fixture_" + original_name.replace(".", "_")
    alias_source = _inline_alias_of(inline_name, original_name)
    inline = TheoremForRepresentation(
        theorem_id=make_id("thm", {"name": inline_name}),
        full_name=inline_name,
        proof_stripped=alias_source,
        context_id=_CTX,
        source_signature=named_record.headless,
        inline_declaration=True,
        inline_source=alias_source,
    )
    (inline_record,) = build_representations(
        backend, [inline], imports="import LeanFaithFixtures", created_at=_UTC
    )
    assert inline_record.alpha_identity_fingerprint == named_record.alpha_identity_fingerprint
    assert normalize_pp_universe_placeholders(
        inline_record.signature_explicit or ""
    ) == normalize_pp_universe_placeholders(named_record.signature_explicit)


def test_inline_elaboration_context_is_not_a_model_view(backend: LeanInteractBackend) -> None:
    proof_stripped = "theorem lf_inline_context_only : True := by sorry"
    theorem = TheoremForRepresentation(
        theorem_id=make_id("thm", {"name": "lf_inline_context_only"}),
        full_name="lf_inline_context_only",
        proof_stripped=proof_stripped,
        inline_source=(
            'def lf_context_helper : True := by\n  have secret := "INLINE_CONTEXT_PROOF_SENTINEL"\n'
            "  exact True.intro\n"
            f"{proof_stripped}"
        ),
        context_id=_CTX,
        inline_declaration=True,
    )
    (record,) = build_representations(
        backend, [theorem], imports="import LeanFaithFixtures", created_at=_UTC
    )
    assert record.signature_pp == "True"
    assert record.raw_proof_stripped == proof_stripped
    assert "INLINE_CONTEXT_PROOF_SENTINEL" not in record.model_dump_json()


def test_alpha_renaming_preserves_identity_fingerprint(backend: LeanInteractBackend) -> None:
    first = TheoremForRepresentation(
        theorem_id=make_id("thm", {"name": "lf_alpha_a"}),
        full_name="lf_alpha_a",
        proof_stripped="theorem lf_alpha_a (x : Nat) : x = x := by sorry",
        context_id=_CTX,
        inline_declaration=True,
    )
    second = TheoremForRepresentation(
        theorem_id=make_id("thm", {"name": "lf_alpha_b"}),
        full_name="lf_alpha_b",
        proof_stripped="theorem lf_alpha_b (renamed : Nat) : renamed = renamed := by sorry",
        context_id=_CTX,
        inline_declaration=True,
    )
    records = build_representations(
        backend, [first, second], imports="import LeanFaithFixtures", created_at=_UTC
    )
    assert records[0].alpha_identity_fingerprint == records[1].alpha_identity_fingerprint


def test_proof_bodies_do_not_enter_model_visible_semantic_views(
    backend: LeanInteractBackend,
) -> None:
    a = _theorem("lf_proof_body_a", "theorem lf_proof_body_a : True := by sorry")
    b = _theorem("lf_proof_body_b", "theorem lf_proof_body_b : True := by sorry")
    first, second = build_representations(
        backend, [a, b], imports="import LeanFaithFixtures", created_at=_UTC
    )
    assert first.signature_pp == second.signature_pp == "True"
    assert first.signature_explicit == second.signature_explicit
    assert first.semantic_atoms == second.semantic_atoms
    assert first.operator_tree == second.operator_tree
    assert first.alpha_identity_fingerprint == second.alpha_identity_fingerprint
    for record in (first, second):
        assert "LEANFAITH_PROOF_SENTINEL" not in record.model_dump_json()
