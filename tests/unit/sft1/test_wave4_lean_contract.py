from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SOURCE = (ROOT / "LeanFaith/Meta/SFT1/Sprint.lean").read_text(encoding="utf-8")


def _between(start: str, end: str) -> str:
    start_at = SOURCE.index(start)
    return SOURCE[start_at : SOURCE.index(end, start_at)]


def test_descriptor_phase_contains_no_certificate_or_render_work() -> None:
    descriptor_phase = _between(
        "structure Wave4DescriptorHop where",
        "private def wave4ExtendOne",
    )
    for certificate_call in (
        "checkedProof",
        "preservingIffProof",
        "refuteSquareNegative",
        "negativeEvidenceJson",
        "prerender",
    ):
        assert certificate_call not in descriptor_phase
    assert "enumerateWave4Op" in descriptor_phase
    assert "wave4DescriptorAllowedNext" in descriptor_phase
    assert "wave4_expression_cycle" in descriptor_phase


def test_selected_phase_replays_then_certifies_at_most_five_descriptors() -> None:
    certification = _between(
        "private def certifyWave4Descriptor",
        "/-- Compatibility path for exhaustive audits.",
    )
    assert "applyOp descriptor.root negativeOp" in certification
    assert "replayWave4Op rootP described.pOp described.pSite" in certification
    assert "replayWave4Op rootC described.cOp described.cSite" in certification
    assert "wave4ExtendOne" in certification
    assert "indices.isEmpty || indices.size > 5" in certification
    assert "refuteSquareNegative first.root first.op first.c first.direction" in certification

    selected_api = _between(
        "def rebuildSelectedWave4Orbits",
        "private def wave4DescriptorHopJson",
    )
    assert "buildWave4Descriptors" in selected_api
    assert "certifyWave4Descriptors descriptors indices" in selected_api


def test_wave4_keeps_full_family_payload_and_direct_negative_last_replay() -> None:
    serializer = _between(
        "private def negativeEvidenceFields",
        "/-- Exact direct preserving witness",
    )
    for field in (
        '"kind"',
        '"check"',
        '"grounding"',
        '"boundary"',
        '"separator"',
        '"witnesses"',
        '"witness_checks"',
        '"enumeration"',
    ):
        assert field in serializer

    evidence = _between("def wave4Evidence", "private def wave4VariantJson")
    assert "let replayRoot : Root := { orbit.root with reference := orbit.pPrime }" in evidence
    assert "let negativeLastApplied ← applyOp replayRoot negativeOp" in evidence
    assert "Expr.equal negativeLastCandidate orbit.cPrime" in evidence
    assert "refuteWave4ReappliedNegative replayRoot orbit.op" in evidence
    assert "negativeEvidenceJson orbit.root orbit.baseNegative" in evidence
    assert "negativeEvidenceJson replayRoot negativeLastEvidence" in evidence
    assert '"negative_last_replay"' in evidence
    assert '"candidate_replay_exact"' in evidence


def test_descriptor_json_cannot_be_mistaken_for_a_retained_certificate() -> None:
    descriptor_api = _between(
        "def processWave4DescriptorRoot",
        "def processWave4DescriptorRoots",
    )
    assert '"kind", Json.str "wave4_descriptor_root"' in descriptor_api
    assert '"described"' in descriptor_api
    assert '"certificate_phase", Json.str "selected_only"' in descriptor_api
    assert '"status", Json.str "retained"' not in descriptor_api
