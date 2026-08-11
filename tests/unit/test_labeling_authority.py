from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

import leanfaith.labeling.authority as authority_module
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, sha256_hex
from leanfaith.labeling.authority import (
    AUTHORITY_VERIFICATION_PREFIX,
    AuthorityArtifactError,
    AuthorityArtifactReferenceV1,
    AuthorityArtifactSerialization,
    AuthorityRouteDisabledError,
    AuthoritySemanticProjectionV1,
    AuthorityVerificationReceiptV1,
    authority_route_enabled,
    build_disabled_authority_verification_receipt,
    load_local_authority_artifact,
    require_authority_route_enabled,
)
from leanfaith.labeling.quality import (
    AuthorityArtifactKind,
    CandidateCommitment,
    ResolutionSource,
    load_active_label_resolution_policy,
    make_authority_artifact_binding,
)
from leanfaith.schemas.enums import (
    QualityTier,
    RelationLabel,
    ResolutionOutcome,
)
from leanfaith.schemas.ids import make_id
from leanfaith.schemas.pair import PairRecord

REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_json(path: Path, *, artifact_id: str = "fixture:authority") -> bytes:
    payload = (
        canonical_json_bytes(
            {
                "artifact_id": artifact_id,
                "nested": {"status": "verified_transport_only"},
            }
        )
        + b"\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return payload


def _json_reference(
    path: Path,
    payload: bytes,
    *,
    kind: AuthorityArtifactKind = AuthorityArtifactKind.HUMAN_ADJUDICATION,
    artifact_id: str = "fixture:authority",
) -> AuthorityArtifactReferenceV1:
    return AuthorityArtifactReferenceV1(
        artifact_kind=kind,
        artifact=str(path),
        sha256=sha256_hex(payload),
        serialization=AuthorityArtifactSerialization.CANONICAL_JSON,
        expected_artifact_id=artifact_id,
        artifact_id_field="artifact_id",
    )


def _pair() -> PairRecord:
    return PairRecord(
        pair_id=make_id("pair", {"fixture": "authority"}),
        theorem_a_id=make_id("thm", {"fixture": "authority-a"}),
        theorem_b_id=make_id("thm", {"fixture": "authority-b"}),
        pair_source="authority_fixture",
        split_group_ids=("ancestry:authority",),
    )


def _positive_projection() -> AuthoritySemanticProjectionV1:
    return AuthoritySemanticProjectionV1(
        same_claim=True,
        resolution_outcome=ResolutionOutcome.SAME_CLAIM,
        relation=RelationLabel.EQUIVALENT,
    )


def test_reference_requires_json_id_binding_and_jsonl_forbids_one() -> None:
    with pytest.raises(ValidationError, match="require expected_artifact_id"):
        AuthorityArtifactReferenceV1(
            artifact_kind=AuthorityArtifactKind.HUMAN_ADJUDICATION,
            artifact="artifact.json",
            sha256="a" * 64,
            serialization=AuthorityArtifactSerialization.CANONICAL_JSON,
        )
    with pytest.raises(ValidationError, match="identified only by exact file SHA-256"):
        AuthorityArtifactReferenceV1(
            artifact_kind=AuthorityArtifactKind.HUMAN_ADJUDICATION,
            artifact="artifact.json",
            sha256="a" * 64,
            serialization=AuthorityArtifactSerialization.CANONICAL_JSONL,
            expected_artifact_id="fixture:id",
            artifact_id_field="artifact_id",
        )


def test_loads_canonical_json_and_constructs_verified_binding(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    artifact = root / "authority.json"
    payload = _write_json(artifact)
    reference = _json_reference(Path("authority.json"), payload)

    loaded = load_local_authority_artifact(reference, repo_root=root)

    assert loaded.resolved_path == artifact.resolve()
    assert loaded.containment_root == root.resolve()
    assert isinstance(loaded.payload, dict)
    assert loaded.payload["artifact_id"] == "fixture:authority"
    assert loaded.binding.artifact_id == "fixture:authority"
    assert loaded.binding.artifact_sha256 == sha256_hex(payload)


def test_loads_strict_canonical_jsonl_by_file_hash(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    artifact = root / "authority.jsonl"
    payload = b"".join(
        canonical_json_bytes({"row": index, "status": "transport_only"}) + b"\n"
        for index in range(2)
    )
    artifact.write_bytes(payload)
    digest = sha256_hex(payload)
    reference = AuthorityArtifactReferenceV1(
        artifact_kind=AuthorityArtifactKind.SUPPORTING_AUDIT,
        artifact="authority.jsonl",
        sha256=digest,
        serialization=AuthorityArtifactSerialization.CANONICAL_JSONL,
    )

    loaded = load_local_authority_artifact(reference, repo_root=root)

    assert len(loaded.payload) == 2
    assert loaded.binding.artifact_id == f"sha256:{digest}"


@pytest.mark.parametrize(
    "payload",
    [
        b'{"artifact_id":"fixture:authority","nested":{}}',
        b'{"artifact_id":"fixture:authority","nested":{}}\n\n',
        b'{"artifact_id":"fixture:authority", "nested":{}}\n',
        b'{"artifact_id":"fixture:authority","value":NaN}\n',
    ],
)
def test_noncanonical_or_nonfinite_json_fails_closed(
    tmp_path: Path,
    payload: bytes,
) -> None:
    root = tmp_path / "repo"
    artifact = root / "authority.json"
    artifact.parent.mkdir()
    artifact.write_bytes(payload)

    with pytest.raises(AuthorityArtifactError):
        load_local_authority_artifact(
            _json_reference(Path("authority.json"), payload),
            repo_root=root,
        )


def test_nested_duplicate_json_key_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    artifact = root / "authority.json"
    artifact.parent.mkdir()
    payload = b'{"artifact_id":"fixture:authority","nested":{"x":1,"x":2}}\n'
    artifact.write_bytes(payload)

    with pytest.raises(AuthorityArtifactError, match="duplicate JSON key"):
        load_local_authority_artifact(
            _json_reference(Path("authority.json"), payload),
            repo_root=root,
        )


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        canonical_json_bytes({"row": 1}),
        canonical_json_bytes({"row": 1}) + b"\n\n",
        b'{"row":1, "status":"noncanonical"}\n',
    ],
)
def test_malformed_jsonl_fails_closed(tmp_path: Path, payload: bytes) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    artifact = root / "authority.jsonl"
    artifact.write_bytes(payload)
    reference = AuthorityArtifactReferenceV1(
        artifact_kind=AuthorityArtifactKind.SUPPORTING_AUDIT,
        artifact="authority.jsonl",
        sha256=sha256_hex(payload),
        serialization=AuthorityArtifactSerialization.CANONICAL_JSONL,
    )

    with pytest.raises(AuthorityArtifactError):
        load_local_authority_artifact(reference, repo_root=root)


def test_relative_parent_and_symlink_escapes_are_rejected(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside.json"
    payload = _write_json(outside)
    (root / "escape.json").symlink_to(outside)

    with pytest.raises(AuthorityArtifactError, match="escapes repository root"):
        load_local_authority_artifact(
            _json_reference(Path("../outside.json"), payload),
            repo_root=root,
        )
    with pytest.raises(AuthorityArtifactError, match="cannot contain symlinks"):
        load_local_authority_artifact(
            _json_reference(Path("escape.json"), payload),
            repo_root=root,
        )


def test_leaf_and_intermediate_symlinks_are_rejected_even_inside_root(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    real_directory = root / "real"
    artifact = real_directory / "authority.json"
    payload = _write_json(artifact)
    (root / "leaf.json").symlink_to(artifact)
    (root / "linked_directory").symlink_to(real_directory, target_is_directory=True)

    for referenced_path in (Path("leaf.json"), Path("linked_directory/authority.json")):
        with pytest.raises(AuthorityArtifactError, match="cannot contain symlinks"):
            load_local_authority_artifact(
                _json_reference(referenced_path, payload),
                repo_root=root,
            )


def test_absolute_artifact_requires_repo_or_registered_private_root(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    private = tmp_path / "private"
    outside = tmp_path / "outside"
    root.mkdir()
    private.mkdir()
    outside.mkdir()
    private_artifact = private / "authority.json"
    outside_artifact = outside / "authority.json"
    private_payload = _write_json(private_artifact)
    outside_payload = _write_json(outside_artifact)

    with pytest.raises(AuthorityArtifactError, match="outside registered roots"):
        load_local_authority_artifact(
            _json_reference(outside_artifact, outside_payload),
            repo_root=root,
            private_roots=(private,),
        )

    loaded = load_local_authority_artifact(
        _json_reference(private_artifact, private_payload),
        repo_root=root,
        private_roots=(private,),
    )
    assert loaded.containment_root == private.resolve()


def test_private_root_symlink_cannot_escape_registered_root(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    private = tmp_path / "private"
    outside = tmp_path / "outside"
    root.mkdir()
    private.mkdir()
    outside.mkdir()
    artifact = outside / "authority.json"
    payload = _write_json(artifact)
    link = private / "escape.json"
    link.symlink_to(artifact)

    with pytest.raises(AuthorityArtifactError, match="cannot contain symlinks"):
        load_local_authority_artifact(
            _json_reference(link, payload),
            repo_root=root,
            private_roots=(private,),
        )


def test_hash_and_embedded_artifact_id_mismatch_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    artifact = root / "authority.json"
    payload = _write_json(artifact)
    reference = _json_reference(Path("authority.json"), payload)

    with pytest.raises(AuthorityArtifactError, match="SHA-256 mismatch"):
        load_local_authority_artifact(
            reference.model_copy(update={"sha256": "b" * 64}),
            repo_root=root,
        )
    with pytest.raises(AuthorityArtifactError, match="artifact ID mismatch"):
        load_local_authority_artifact(
            reference.model_copy(update={"expected_artifact_id": "fixture:other"}),
            repo_root=root,
        )


def test_pre_post_hash_drift_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "repo"
    artifact = root / "authority.json"
    payload = _write_json(artifact)
    observed = iter((payload, payload + b" "))
    monkeypatch.setattr(authority_module, "_read_all_fd", lambda _fd: next(observed))

    with pytest.raises(AuthorityArtifactError, match="hash changed during read"):
        load_local_authority_artifact(
            _json_reference(Path("authority.json"), payload),
            repo_root=root,
        )


def test_every_authority_route_is_disabled_by_default() -> None:
    for kind in AuthorityArtifactKind:
        assert authority_route_enabled(kind) is False
        with pytest.raises(AuthorityRouteDisabledError, match="disabled"):
            require_authority_route_enabled(kind)


def test_disabled_receipt_binds_target_policy_semantics_and_evidence(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    artifact = root / "authority.json"
    payload = _write_json(artifact)
    loaded = load_local_authority_artifact(
        _json_reference(Path("authority.json"), payload),
        repo_root=root,
    )
    policy = load_active_label_resolution_policy(REPO_ROOT)
    target = _pair()
    evidence_id = make_id("ev", {"fixture": "authority-human"})

    receipt = build_disabled_authority_verification_receipt(
        primary=loaded,
        supporting=(),
        target=target,
        policy=policy,
        source=ResolutionSource.HUMAN_ADJUDICATION,
        quality_tier=QualityTier.GOLD_HUMAN,
        resolution_method="expert_adjudication",
        commitment=CandidateCommitment.TERMINAL,
        semantic_projection=_positive_projection(),
        accepted_evidence_ids=(evidence_id,),
        provenance=("fixture:authority",),
    )

    assert receipt.target_id == target.pair_id
    assert receipt.target_sha256 == hash_canonical(target.model_dump(mode="json"))
    assert receipt.policy_file_sha256 == policy.policy_file_sha256
    assert receipt.gate_file_sha256 == policy.gate_file_sha256
    assert receipt.route_enabled is False
    assert receipt.production_eligible is False
    assert receipt.candidate_emitted is False
    assert receipt.label_emitted is False
    assert "caller_projection_hash_bound_unverified" in receipt.verification_check_codes
    assert all("projection_verified" not in check for check in receipt.verification_check_codes)
    assert receipt.receipt_id == make_id(
        AUTHORITY_VERIFICATION_PREFIX,
        receipt.model_dump(mode="json", exclude={"receipt_id"}),
    )
    assert AuthorityVerificationReceiptV1.model_validate_json(receipt.model_dump_json()) == receipt


def test_disabled_receipt_rejects_unregistered_method_or_wrong_artifact_kind(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    artifact = root / "authority.json"
    payload = _write_json(artifact)
    policy = load_active_label_resolution_policy(REPO_ROOT)
    human = load_local_authority_artifact(
        _json_reference(Path("authority.json"), payload),
        repo_root=root,
    )

    with pytest.raises(AuthorityArtifactError, match="method/tier"):
        build_disabled_authority_verification_receipt(
            primary=human,
            supporting=(),
            target=_pair(),
            policy=policy,
            source=ResolutionSource.HUMAN_ADJUDICATION,
            quality_tier=QualityTier.GOLD_HUMAN,
            resolution_method="benchmark_import",
            commitment=CandidateCommitment.TERMINAL,
            semantic_projection=_positive_projection(),
            accepted_evidence_ids=(make_id("ev", {"fixture": "wrong-method"}),),
            provenance=("fixture:wrong-method",),
        )

    wrong_binding = make_authority_artifact_binding(
        artifact_kind=AuthorityArtifactKind.FROZEN_BENCHMARK_LABEL,
        artifact_id=human.binding.artifact_id,
        artifact_sha256=human.binding.artifact_sha256,
    )
    receipt_payload = {
        **build_disabled_authority_verification_receipt(
            primary=human,
            supporting=(),
            target=_pair(),
            policy=policy,
            source=ResolutionSource.HUMAN_ADJUDICATION,
            quality_tier=QualityTier.GOLD_HUMAN,
            resolution_method="expert_adjudication",
            commitment=CandidateCommitment.TERMINAL,
            semantic_projection=_positive_projection(),
            accepted_evidence_ids=(make_id("ev", {"fixture": "wrong-kind"}),),
            provenance=("fixture:wrong-kind",),
        ).model_dump(mode="python"),
        "primary_artifact": wrong_binding,
    }
    with pytest.raises(ValidationError, match="primary artifact kind"):
        AuthorityVerificationReceiptV1.model_validate(receipt_payload)


def test_separator_receipt_is_partial_and_still_nonproduction(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    artifact = root / "separator.json"
    payload = _write_json(artifact, artifact_id="fixture:separator")
    loaded = load_local_authority_artifact(
        _json_reference(
            Path("separator.json"),
            payload,
            kind=AuthorityArtifactKind.CERTIFICATE_OR_SEPARATOR,
            artifact_id="fixture:separator",
        ),
        repo_root=root,
    )
    policy = load_active_label_resolution_policy(REPO_ROOT)
    evidence_id = make_id("ev", {"fixture": "separator"})
    projection = AuthoritySemanticProjectionV1(
        same_claim=False,
        resolution_outcome=ResolutionOutcome.NOT_SAME_CLAIM,
        relation=None,
    )

    receipt = build_disabled_authority_verification_receipt(
        primary=loaded,
        supporting=(),
        target=_pair(),
        policy=policy,
        source=ResolutionSource.PROMOTED_CERTIFICATE_OR_SEPARATOR,
        quality_tier=QualityTier.GOLD_COUNTEREXAMPLE,
        resolution_method="separator_certificate",
        commitment=CandidateCommitment.PARTIAL_NEGATIVE,
        semantic_projection=projection,
        accepted_evidence_ids=(evidence_id,),
        provenance=("fixture:separator",),
    )
    assert receipt.commitment is CandidateCommitment.PARTIAL_NEGATIVE
    assert receipt.production_eligible is False

    with pytest.raises(ValidationError, match="terminal negative receipt"):
        AuthorityVerificationReceiptV1.model_validate(
            {
                **receipt.model_dump(mode="python"),
                "commitment": CandidateCommitment.TERMINAL,
            }
        )


def test_infrastructure_exports_no_candidate_or_label_builder() -> None:
    assert not hasattr(authority_module, "build_resolution_candidate")
    assert not hasattr(authority_module, "resolve_label")
