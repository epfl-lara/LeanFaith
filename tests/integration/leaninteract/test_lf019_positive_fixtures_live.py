"""Lean-backed applicability check for every LF-019 positive fixture."""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Iterator

import pytest

from leanfaith.config.paths import find_repo_root
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.lean.protocol import LeanRequest, LeanStatus
from leanfaith.representations.pipeline import TheoremForRepresentation, build_representations
from leanfaith.schemas import TheoremRecord, ValidationStatus, make_id
from leanfaith.transforms.factory import build_positive_rule_runtime
from leanfaith.transforms.positive_fixtures import (
    PositiveFixtureCase,
    load_lf019_positive_fixture_profile,
)
from leanfaith.transforms.registry import load_transformation_registry

pytestmark = [
    pytest.mark.lean,
    pytest.mark.skipif(shutil.which("lake") is None, reason="Lean toolchain unavailable"),
]

_ROOT = find_repo_root()
_CONTEXT_FINGERPRINT = "0" * 64
_CONTEXT_ID = f"ctx:{_CONTEXT_FINGERPRINT}"


@pytest.fixture(scope="module")
def backend(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LeanInteractBackend]:
    instance = LeanInteractBackend(
        BackendSettings(
            project_dir=_ROOT / "tests/lean_fixtures",
            context_fingerprint=_CONTEXT_FINGERPRINT,
            environment_schema_version=1,
            raw_response_dir=tmp_path_factory.mktemp("lf019_positive_raw"),
        )
    )
    yield instance
    instance.close()


def _source(case: PositiveFixtureCase) -> TheoremRecord:
    theorem_id = make_id("thm", {"lf019_positive_live": case.rule_id})
    ancestry_id = make_id("anc", {"lf019_positive_live": case.rule_id})
    return TheoremRecord(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        source="lf019_positive_fixture",
        source_revision="lf019_positive_fixtures_v1",
        source_record=case.case_id,
        context_id=_CONTEXT_ID,
        declaration_kind="theorem",
        declaration_name=case.source_name,
        declaration_full_name=case.source_name,
        proof_stripped_declaration=case.source_code,
        inline_elaboration_source=case.source_code,
        is_proposition=True,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        statement_content_hash=hashlib.sha256(case.source_code.encode("utf-8")).hexdigest(),
    )


@pytest.mark.parametrize(
    "case",
    load_lf019_positive_fixture_profile().config.cases,
    ids=lambda case: case.rule_id,
)
def test_lf019_positive_fixture_is_live_applicable(
    backend: LeanInteractBackend,
    case: PositiveFixtureCase,
) -> None:
    profile = load_lf019_positive_fixture_profile()
    source = _source(case)
    (representation,) = build_representations(
        backend,
        [
            TheoremForRepresentation(
                theorem_id=source.theorem_id,
                full_name=case.source_name,
                proof_stripped=case.source_code,
                context_id=_CONTEXT_ID,
                inline_declaration=True,
                inline_source=case.source_code,
            )
        ],
        imports=profile.config.imports,
        created_at=profile.config.record_timestamp,
    )
    registration = build_positive_rule_runtime(load_transformation_registry())
    execution = registration.runtime.execute(
        case.rule_id,
        source,
        representation,
        case.seed,
    )

    assert execution.attempt.applicability is not None
    assert execution.attempt.applicability.applicable is True
    assert execution.attempt.terminal_outcome == "generated"
    assert len(execution.drafts) == 1
    candidate_code = execution.drafts[0].candidate_code
    assert case.expected_candidate_fragment in candidate_code
    assert execution.drafts[0].transformation_trace[0]["operation"] == (
        case.expected_trace_operation
    )

    result = backend.run(
        LeanRequest(
            request_id=f"lf019-positive-candidate-{case.rule_id}",
            context_id=_CONTEXT_ID,
            code=f"{profile.config.imports}\n{candidate_code}",
            declarations=True,
            allow_sorry=True,
        )
    )
    assert result.status == LeanStatus.VALID_WITH_SORRY, result.messages
    assert tuple(item["full_name"] for item in result.declarations) == (case.source_name,)
