"""Real LeanInteract checks for LF-020 certificates and separator evidence."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from leanfaith.config.paths import find_repo_root
from leanfaith.lean.commands import PropositionPairSource
from leanfaith.lean.counterexample import run_counterexample_attempt
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.lean.proof_search import run_defeq_check, run_directional_proof_attempt
from leanfaith.lean.protocol import LeanStatus
from leanfaith.lean.typecheck import run_proposition_preflight

pytestmark = [
    pytest.mark.lean,
    pytest.mark.skipif(shutil.which("lake") is None, reason="Lean toolchain unavailable"),
]

_ROOT = find_repo_root(Path(__file__).parent)
_FIXTURES = _ROOT / "tests" / "lean_fixtures"
_FINGERPRINT = "0" * 64
_CONTEXT_ID = f"ctx:{_FINGERPRINT}"
_HEADER = "import LeanFaithFixtures.Basic"
_ALLOWED_AXIOMS = ("Classical.choice", "propext", "Quot.sound")
_FORBIDDEN_AXIOMS = ("sorryAx",)


@pytest.fixture(scope="module")
def evidence_backend(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[LeanInteractBackend]:
    backend = LeanInteractBackend(
        BackendSettings(
            project_dir=_FIXTURES,
            context_fingerprint=_FINGERPRINT,
            environment_schema_version=1,
            raw_response_dir=tmp_path_factory.mktemp("lf020_raw"),
        )
    )
    yield backend
    backend.close()


def _source(
    suffix: str,
    proposition_a: str,
    proposition_b: str,
    *,
    forbidden: tuple[str, ...] = (),
) -> PropositionPairSource:
    return PropositionPairSource(
        header_text=_HEADER,
        proposition_a=proposition_a,
        proposition_b=proposition_b,
        pair_id=f"pair:{suffix * 64}",
        forbidden_declaration_constants=forbidden,
    )


def test_live_preflight_and_defeq_separate_invalid_aliases_from_not_equal(
    evidence_backend: LeanInteractBackend,
) -> None:
    equal = _source("1", "True", "True")
    preflight = run_proposition_preflight(
        evidence_backend,
        source=equal,
        context_id=_CONTEXT_ID,
        timeout_seconds=10,
        request_id="lf020-live-preflight-equal",
    )
    assert preflight.valid
    equal_result = run_defeq_check(
        evidence_backend,
        source=equal,
        context_id=_CONTEXT_ID,
        timeout_seconds=10,
        request_id="lf020-live-defeq-equal",
    )
    assert equal_result.retry.result.status == LeanStatus.VALID
    assert equal_result.equal is True

    unequal = _source("2", "True", "False")
    unequal_preflight = run_proposition_preflight(
        evidence_backend,
        source=unequal,
        context_id=_CONTEXT_ID,
        timeout_seconds=10,
        request_id="lf020-live-preflight-unequal",
    )
    assert unequal_preflight.valid
    unequal_result = run_defeq_check(
        evidence_backend,
        source=unequal,
        context_id=_CONTEXT_ID,
        timeout_seconds=10,
        request_id="lf020-live-defeq-unequal",
    )
    assert unequal_result.retry.result.status == LeanStatus.INVALID
    assert unequal_result.equal is False


def test_live_directional_certificate_is_audited(
    evidence_backend: LeanInteractBackend,
) -> None:
    source = _source("3", "False", "True")
    attempt = run_directional_proof_attempt(
        evidence_backend,
        source=source,
        context_id=_CONTEXT_ID,
        direction="A_to_B",
        method_id="true_intro_v1",
        tactic_body="intro h\nexact True.intro",
        timeout_seconds=10,
        request_id="lf020-live-proof",
        allowed_axioms=_ALLOWED_AXIOMS,
        forbidden_axioms=_FORBIDDEN_AXIOMS,
    )
    assert attempt.retry.result.status == LeanStatus.VALID
    assert attempt.proved
    assert attempt.audit is not None
    assert attempt.audit.accepted
    assert attempt.audit.axioms == ()


def test_live_transitive_source_theorem_dependency_is_rejected(
    evidence_backend: LeanInteractBackend,
) -> None:
    source = _source("4", "False", "True", forbidden=("lf_trivial",))
    attempt = run_directional_proof_attempt(
        evidence_backend,
        source=source,
        context_id=_CONTEXT_ID,
        direction="A_to_B",
        method_id="source_leak_v1",
        tactic_body="intro h\nexact lf_trivial",
        timeout_seconds=10,
        request_id="lf020-live-proof-source-leak",
        allowed_axioms=_ALLOWED_AXIOMS,
        forbidden_axioms=_FORBIDDEN_AXIOMS,
    )
    assert attempt.retry.result.status == LeanStatus.VALID
    assert not attempt.proved
    assert attempt.policy_rejected
    assert attempt.audit is not None
    assert attempt.audit.forbidden_constant_hits == ("lf_trivial",)


def test_live_sorry_certificate_is_never_accepted(
    evidence_backend: LeanInteractBackend,
) -> None:
    source = _source("5", "True", "True")
    attempt = run_directional_proof_attempt(
        evidence_backend,
        source=source,
        context_id=_CONTEXT_ID,
        direction="A_to_B",
        method_id="admission_v1",
        tactic_body="intro h\nexact (by sorry)",
        timeout_seconds=10,
        request_id="lf020-live-proof-sorry",
        allowed_axioms=_ALLOWED_AXIOMS,
        forbidden_axioms=_FORBIDDEN_AXIOMS,
    )
    assert attempt.retry.result.status == LeanStatus.VALID_WITH_SORRY
    assert not attempt.proved
    assert attempt.policy_rejected


def test_live_kernel_decide_finds_separator_and_not_found_stays_nonnegative(
    evidence_backend: LeanInteractBackend,
) -> None:
    separated = run_counterexample_attempt(
        evidence_backend,
        source=_source("6", "True", "False"),
        context_id=_CONTEXT_ID,
        direction="A_to_B",
        timeout_seconds=10,
        request_id_prefix="lf020-live-counter-found",
        allowed_axioms=_ALLOWED_AXIOMS,
        forbidden_axioms=_FORBIDDEN_AXIOMS,
    )
    assert separated.supported
    assert separated.found
    assert separated.audit is not None
    assert separated.audit.accepted
    assert separated.command is not None
    assert "native_decide" not in separated.command.code

    same = run_counterexample_attempt(
        evidence_backend,
        source=_source("7", "True", "True"),
        context_id=_CONTEXT_ID,
        direction="A_to_B",
        timeout_seconds=10,
        request_id_prefix="lf020-live-counter-not-found",
        allowed_axioms=_ALLOWED_AXIOMS,
        forbidden_axioms=_FORBIDDEN_AXIOMS,
    )
    assert same.supported
    assert same.retry is not None
    assert same.retry.result.status == LeanStatus.INVALID
    assert not same.found
    assert not same.policy_rejected


def test_live_generated_universe_placeholders_reelaborate(
    evidence_backend: LeanInteractBackend,
) -> None:
    source = _source(
        "8",
        "(∀ {α : Type u_1} (x : α), x = x)",
        "(∀ {β : Type u_1} (y : β), y = y)",
    )
    preflight = run_proposition_preflight(
        evidence_backend,
        source=source,
        context_id=_CONTEXT_ID,
        timeout_seconds=10,
        request_id="lf020-live-universe-preflight",
    )
    assert preflight.retry.result.status == LeanStatus.VALID
    assert "universe u_1" in preflight.command.code
