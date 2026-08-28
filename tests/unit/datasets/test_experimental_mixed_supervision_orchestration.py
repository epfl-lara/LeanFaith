from __future__ import annotations

import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.config.hashing import hash_file, sha256_hex
from leanfaith.datasets import experimental_mixed_supervision_orchestration as orchestration
from leanfaith.datasets.experimental_mixed_supervision import (
    ExperimentalMixedAdapterResult,
    ExperimentalMixedSupervisionError,
)
from leanfaith.schemas.enums import ValidationStatus, ViewStatus
from leanfaith.schemas.theorem import CANONICAL_VIEW_NAMES, RepresentationRecord, TheoremRecord


def _theorem(index: int = 1) -> TheoremRecord:
    statement = "theorem source (n : Nat) : n = n := by sorry"
    return TheoremRecord(
        theorem_id="thm:" + f"{index:064x}",
        ancestry_id="anc:" + f"{index + 1:064x}",
        root_ancestry_ids=("anc:" + f"{index + 1:064x}",),
        source="mathlib",
        source_revision="fixture",
        context_id="ctx:" + "3" * 64,
        declaration_kind="theorem",
        declaration_name="source",
        declaration_full_name="Fixture.source",
        proof_stripped_declaration=statement,
        is_proposition=True,
        elaboration_status=ValidationStatus.ELABORATES,
        statement_content_hash=sha256_hex(statement.encode()),
    )


def _representation(theorem: TheoremRecord) -> RepresentationRecord:
    view_status = dict.fromkeys(CANONICAL_VIEW_NAMES, ViewStatus.NOT_ATTEMPTED)
    for name in ("raw_proof_stripped", "headless", "signature_pp"):
        view_status[name] = ViewStatus.OK
    return RepresentationRecord(
        representation_id="repr:" + "4" * 64,
        theorem_id=theorem.theorem_id,
        normalization_version="fixture_v1",
        context_id=theorem.context_id,
        raw_proof_stripped=theorem.proof_stripped_declaration,
        headless="(n : Nat) : n = n",
        signature_pp="n = n",
        alpha_identity_fingerprint="5" * 64,
        view_status=view_status,
        content_hash="6" * 64,
        created_at=datetime.datetime(2026, 8, 12, tzinfo=datetime.UTC),
    )


def _canonical_line(model: TheoremRecord | RepresentationRecord, wrapper: str) -> bytes:
    import json

    return (
        json.dumps(
            {wrapper: model.model_dump(mode="json")},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )


def test_target_loader_streams_only_requested_records(tmp_path: Path) -> None:
    theorem = _theorem()
    path = tmp_path / "theorems.jsonl"
    path.write_bytes(
        b'{"theorem_id":"thm:'
        + b"f" * 64
        + b'",not-valid-json}\n'
        + _canonical_line(theorem, "theorem")
    )

    records = orchestration._load_target_records(  # pyright: ignore[reportPrivateUsage]
        (path,),
        target_theorem_ids=frozenset((theorem.theorem_id,)),
        model=TheoremRecord,
        wrapper_key="theorem",
    )

    assert records == {theorem.theorem_id: theorem}


def test_target_loader_rejects_duplicate_canonical_source(tmp_path: Path) -> None:
    theorem = _theorem()
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    first.write_bytes(_canonical_line(theorem, "theorem"))
    second.write_bytes(_canonical_line(theorem, "theorem"))

    with pytest.raises(ExperimentalMixedSupervisionError, match="more than once"):
        orchestration._load_target_records(  # pyright: ignore[reportPrivateUsage]
            (first, second),
            target_theorem_ids=frozenset((theorem.theorem_id,)),
            model=TheoremRecord,
            wrapper_key="theorem",
        )


def test_verify_audits_requires_complete_clean_and_canonical_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: list[tuple[str, bool]] = []

    def fake_verify(**kwargs: Any) -> Any:
        observed.append((Path(kwargs["audit_root"]).name, kwargs["require_complete_clean"]))
        assert kwargs["parent_audit_roots"] == ()
        return SimpleNamespace(judgments=(), checks=(), parent_audit_bindings=())

    monkeypatch.setattr(orchestration, "verify_completed_lf022_codex_audit", fake_verify)
    sources = (
        orchestration.ExperimentalLF022AuditSource(
            name="qwen",
            repo_root=tmp_path,
            checks_path=tmp_path / "q",
            audit_root=tmp_path / "qwen",
        ),
        orchestration.ExperimentalLF022AuditSource(
            name="kimi",
            repo_root=tmp_path,
            checks_path=tmp_path / "k",
            audit_root=tmp_path / "kimi",
        ),
    )

    orchestration._verify_audits(sources)  # pyright: ignore[reportPrivateUsage]

    assert observed == [("kimi", True), ("qwen", True)]


def test_lf022_binding_covers_audit_variant_task_and_raw_lean(
    tmp_path: Path,
) -> None:
    audit_root = tmp_path / "audit"
    audit_root.mkdir()
    (audit_root / "manifest.json").write_text("{}\n", encoding="utf-8")
    checks_path = tmp_path / "checks.jsonl"
    checks_path.write_text("{}\n", encoding="utf-8")
    variant_dir = tmp_path / "variant"
    variant_dir.mkdir()
    variant_path = variant_dir / "variant.jsonl"
    variant_path.write_text("{}\n", encoding="utf-8")
    task_path = variant_dir / "task.json"
    task_path.write_text("{}\n", encoding="utf-8")
    raw_path = tmp_path / "lean-response.json"
    raw_path.write_text("{}\n", encoding="utf-8")
    check = SimpleNamespace(
        source_variant_artifact=str(variant_path),
        source_variant_artifact_sha256=hash_file(variant_path),
        attempts=(
            SimpleNamespace(
                raw_response_path=str(raw_path),
                raw_response_sha256=hash_file(raw_path),
            ),
        ),
    )
    source = orchestration.ExperimentalLF022AuditSource(
        name="kimi",
        repo_root=tmp_path,
        checks_path=checks_path,
        audit_root=audit_root,
    )
    bindings = orchestration._InputBindings()  # pyright: ignore[reportPrivateUsage]

    orchestration._bind_lf022_source_artifacts(  # pyright: ignore[reportPrivateUsage]
        bindings,
        audit_source=source,
        verified=cast(Any, SimpleNamespace(checks=(check,), parent_audit_bindings=())),
    )
    frozen = bindings.finish()

    assert set(frozen) == {
        "lf022/kimi/audit/manifest.json",
        "lf022/kimi/checks",
        f"lf022/kimi/variant/{hash_file(variant_path)}",
        f"lf022/kimi/task/{hash_file(task_path)}",
        f"lf022/kimi/lean_raw/{hash_file(raw_path)}",
    }
    assert {binding.partition for binding in frozen.values()} == {"lf022_codex"}


def test_lf022_binding_includes_declared_verified_parent_tree(tmp_path: Path) -> None:
    audit_root = tmp_path / "audit"
    audit_root.mkdir()
    (audit_root / "manifest.json").write_text("{}\n", encoding="utf-8")
    parent_root = tmp_path / "parent"
    (parent_root / "items" / "one").mkdir(parents=True)
    parent_manifest = parent_root / "manifest.json"
    parent_manifest.write_text('{"parent":true}\n', encoding="utf-8")
    (parent_root / "items" / "one" / "completed.json").write_text(
        '{"complete":true}\n', encoding="utf-8"
    )
    checks_path = tmp_path / "checks.jsonl"
    checks_path.write_text("{}\n", encoding="utf-8")
    source = orchestration.ExperimentalLF022AuditSource(
        name="qwen",
        repo_root=tmp_path,
        checks_path=checks_path,
        audit_root=audit_root,
        parent_audit_roots=(parent_root,),
    )
    verified = SimpleNamespace(
        checks=(),
        parent_audit_bindings=(
            SimpleNamespace(
                audit_root=str(parent_root.resolve()),
                manifest_sha256=hash_file(parent_manifest),
            ),
        ),
    )
    bindings = orchestration._InputBindings()  # pyright: ignore[reportPrivateUsage]

    orchestration._bind_lf022_source_artifacts(  # pyright: ignore[reportPrivateUsage]
        bindings,
        audit_source=source,
        verified=cast(Any, verified),
    )

    assert "lf022/qwen/parent00/manifest.json" in bindings.finish()
    assert "lf022/qwen/parent00/items/one/completed.json" in bindings.finish()


def test_lf022_binding_rejects_unverified_parent(tmp_path: Path) -> None:
    audit_root = tmp_path / "audit"
    audit_root.mkdir()
    (audit_root / "manifest.json").write_text("{}\n", encoding="utf-8")
    parent_root = tmp_path / "parent"
    parent_root.mkdir()
    (parent_root / "manifest.json").write_text("{}\n", encoding="utf-8")
    checks_path = tmp_path / "checks.jsonl"
    checks_path.write_text("{}\n", encoding="utf-8")
    source = orchestration.ExperimentalLF022AuditSource(
        name="qwen",
        repo_root=tmp_path,
        checks_path=checks_path,
        audit_root=audit_root,
        parent_audit_roots=(parent_root,),
    )

    with pytest.raises(ExperimentalMixedSupervisionError, match="differ"):
        orchestration._bind_lf022_source_artifacts(  # pyright: ignore[reportPrivateUsage]
            orchestration._InputBindings(),  # pyright: ignore[reportPrivateUsage]
            audit_source=source,
            verified=cast(Any, SimpleNamespace(checks=(), parent_audit_bindings=())),
        )


def test_orchestration_combines_sources_and_marks_composition_omitted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            (
                "schema_version: 1",
                "profile_id: fixture_mixed",
                "selection_seed: fixture-seed",
                "model_input_profile: headless_only_v1",
                "retain_all_clean_pairs: true",
                "first_hop_partition: included",
                "lf022_codex_partition: included",
                "composition_partition: omitted_pending_receipt",
                "train_percent: 80",
                "validation_percent: 10",
                "test_percent: 10",
                "",
            )
        ),
        encoding="utf-8",
    )
    first_hop_root = tmp_path / "first-hop"
    first_hop_root.mkdir()
    (first_hop_root / "manifest.json").write_text("{}\n", encoding="utf-8")
    audit_root = tmp_path / "audit"
    audit_root.mkdir()
    (audit_root / "manifest.json").write_text("{}\n", encoding="utf-8")
    checks_path = tmp_path / "checks.jsonl"
    checks_path.write_text("{}\n", encoding="utf-8")
    theorem_path = tmp_path / "theorems.jsonl"
    theorem_path.write_text("{}\n", encoding="utf-8")
    representation_path = tmp_path / "representations.jsonl"
    representation_path.write_text("{}\n", encoding="utf-8")
    benchmark_paths: dict[str, Path] = {}
    for name in ("manifest", "base", "active", "detail", "input", "bundle", "auth"):
        path = tmp_path / f"benchmark-{name}.json"
        path.write_text("{}\n", encoding="utf-8")
        benchmark_paths[name] = path
    registry = SimpleNamespace(
        manifest_path=benchmark_paths["manifest"],
        base_registry_path=benchmark_paths["base"],
        active_registry_path=benchmark_paths["active"],
        detailed_index_path=benchmark_paths["detail"],
        input_manifest_path=benchmark_paths["input"],
        code_bundle_path=benchmark_paths["bundle"],
    )
    theorem = _theorem()
    representation = _representation(theorem)
    judgment = SimpleNamespace(source_record_ids=(theorem.theorem_id, "var:" + "7" * 64))
    verified = SimpleNamespace(judgments=(judgment,), checks=(), parent_audit_bindings=())
    first_candidate = object()
    lf022_candidate = object()
    exclusion = object()
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        orchestration,
        "_load_registry",
        lambda **_kwargs: (registry, benchmark_paths["auth"]),
    )
    monkeypatch.setattr(
        orchestration,
        "verify_experimental_first_hop_projection",
        lambda *_args, **_kwargs: SimpleNamespace(
            selectable_count=1,
            inputs={},
            config=SimpleNamespace(
                benchmark_active_registry_sha256=hash_file(benchmark_paths["active"])
            ),
        ),
    )
    monkeypatch.setattr(
        orchestration,
        "load_selectable_experimental_first_hop_projection",
        lambda *_args, **_kwargs: (object(),),
    )
    monkeypatch.setattr(orchestration, "_verify_audits", lambda _sources: (verified,))

    def fake_load_targets(_paths: Any, *, model: type[Any], **_kwargs: Any) -> dict[str, Any]:
        return {theorem.theorem_id: theorem if model is TheoremRecord else representation}

    monkeypatch.setattr(orchestration, "_load_target_records", fake_load_targets)
    monkeypatch.setattr(
        orchestration,
        "adapt_selectable_first_hop_projection",
        lambda *_args, **_kwargs: ExperimentalMixedAdapterResult(
            candidates=cast(Any, (first_candidate,))
        ),
    )
    monkeypatch.setattr(
        orchestration,
        "adapt_verified_lf022_codex_audit",
        lambda *_args, **_kwargs: ExperimentalMixedAdapterResult(
            candidates=cast(Any, (lf022_candidate,)),
            exclusions=cast(Any, (exclusion,)),
        ),
    )

    def fake_freeze(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(replayed=False)

    monkeypatch.setattr(orchestration, "freeze_experimental_mixed_supervision", fake_freeze)
    result = orchestration.freeze_experimental_mixed_supervision_from_artifacts(
        repo_root=tmp_path,
        output_dir=tmp_path / "output",
        config_path=config_path,
        first_hop_projection_dir=first_hop_root,
        lf022_audits=(
            orchestration.ExperimentalLF022AuditSource(
                name="kimi",
                repo_root=tmp_path,
                checks_path=checks_path,
                audit_root=audit_root,
            ),
        ),
        source_theorem_paths=(theorem_path,),
        source_representation_paths=(representation_path,),
    )

    assert captured["candidates"] == (first_candidate, lf022_candidate)
    assert captured["adapter_exclusions"] == (exclusion,)
    assert captured["config"].composition_partition == "omitted_pending_receipt"
    assert not any(binding.partition == "composition" for binding in captured["inputs"].values())
    assert result.first_hop_candidate_count == 1
    assert result.lf022_candidate_count == 1
    assert result.adapter_exclusion_count == 1
    assert result.input_binding_count == len(captured["inputs"])


def test_composition_source_requires_three_distinct_roots(tmp_path: Path) -> None:
    with pytest.raises(ExperimentalMixedSupervisionError, match="must be distinct"):
        orchestration.ExperimentalCompositionSource(
            full_run_root=tmp_path,
            seed_dir=tmp_path,
            postprocess_root=tmp_path / "postprocess",
            source_theorem_paths=(tmp_path / "source-theorems.jsonl",),
            source_representation_paths=(tmp_path / "source-representations.jsonl",),
        )


def test_composition_source_binding_rejects_seed_inventory_and_accepts_original_partitions(
    tmp_path: Path,
) -> None:
    paths = {
        name: tmp_path / f"{name}.jsonl"
        for name in (
            "seed_theorems",
            "seed_representations",
            "private_theorems",
            "public_theorems",
            "private_representations",
            "public_representations",
        )
    }
    for name, path in paths.items():
        path.write_text(f'{{"artifact":"{name}"}}\n', encoding="utf-8")
    export_manifest = SimpleNamespace(
        source_theorem_partition_sha256s=tuple(
            sorted(
                (
                    hash_file(paths["private_theorems"]),
                    hash_file(paths["public_theorems"]),
                )
            )
        ),
        source_representation_partition_sha256s=tuple(
            sorted(
                (
                    hash_file(paths["private_representations"]),
                    hash_file(paths["public_representations"]),
                )
            )
        ),
    )

    seed_only = orchestration.ExperimentalCompositionSource(
        full_run_root=tmp_path / "full",
        seed_dir=tmp_path / "seed",
        postprocess_root=tmp_path / "postprocess",
        source_theorem_paths=(paths["seed_theorems"],),
        source_representation_paths=(paths["seed_representations"],),
    )
    with pytest.raises(ExperimentalMixedSupervisionError, match="differ from receipt export"):
        orchestration._bind_composition_source_partitions(
            seed_only,
            export_manifest=cast(Any, export_manifest),
            bindings=orchestration._InputBindings(),
        )

    original = orchestration.ExperimentalCompositionSource(
        full_run_root=tmp_path / "full",
        seed_dir=tmp_path / "seed",
        postprocess_root=tmp_path / "postprocess",
        source_theorem_paths=(paths["private_theorems"], paths["public_theorems"]),
        source_representation_paths=(
            paths["private_representations"],
            paths["public_representations"],
        ),
    )
    theorem_bindings, representation_bindings = orchestration._bind_composition_source_partitions(
        original,
        export_manifest=cast(Any, export_manifest),
        bindings=orchestration._InputBindings(),
    )

    assert tuple(item.sha256 for item in theorem_bindings) == (
        export_manifest.source_theorem_partition_sha256s
    )
    assert tuple(item.sha256 for item in representation_bindings) == (
        export_manifest.source_representation_partition_sha256s
    )


def test_included_composition_config_is_pinned_and_valid() -> None:
    loaded = orchestration.load_config(
        Path("configs/data/experimental_mixed_supervision_firsthop_lf022_composition_v1.yaml"),
        orchestration.ExperimentalMixedSupervisionConfig,
    )

    assert loaded.config.first_hop_partition == "included"
    assert loaded.config.lf022_codex_partition == "included"
    assert loaded.config.composition_partition == "included"


def test_orchestration_rejects_first_hop_screened_by_different_registry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            (
                "schema_version: 1",
                "profile_id: fixture_mixed",
                "selection_seed: fixture-seed",
                "model_input_profile: headless_only_v1",
                "retain_all_clean_pairs: true",
                "first_hop_partition: included",
                "lf022_codex_partition: included",
                "composition_partition: omitted_pending_receipt",
                "train_percent: 80",
                "validation_percent: 10",
                "test_percent: 10",
                "",
            )
        ),
        encoding="utf-8",
    )
    active = tmp_path / "active.json"
    active.write_text("{}\n", encoding="utf-8")
    registry = SimpleNamespace(active_registry_path=active)
    monkeypatch.setattr(
        orchestration,
        "_load_registry",
        lambda **_kwargs: (registry, None),
    )
    monkeypatch.setattr(
        orchestration,
        "verify_experimental_first_hop_projection",
        lambda *_args, **_kwargs: SimpleNamespace(
            selectable_count=1,
            inputs={},
            config=SimpleNamespace(benchmark_active_registry_sha256="f" * 64),
        ),
    )

    with pytest.raises(ExperimentalMixedSupervisionError, match="different active benchmark"):
        orchestration.freeze_experimental_mixed_supervision_from_artifacts(
            repo_root=tmp_path,
            output_dir=tmp_path.parent / "external-output",
            config_path=config_path,
            first_hop_projection_dir=tmp_path / "first-hop",
            lf022_audits=(
                orchestration.ExperimentalLF022AuditSource(
                    name="kimi",
                    repo_root=tmp_path,
                    checks_path=tmp_path / "checks",
                    audit_root=tmp_path / "audit",
                ),
            ),
            source_theorem_paths=(tmp_path / "theorems",),
            source_representation_paths=(tmp_path / "representations",),
        )


def test_freeze_clis_reject_relative_output_before_reading_inputs() -> None:
    runner = CliRunner()
    first_hop = runner.invoke(
        app,
        [
            "freeze-experimental-first-hop-projection",
            "--audit-dir",
            "missing-audit",
            "--positive-seed-dir",
            "missing-seeds",
            "--output-dir",
            "relative-output",
        ],
    )
    assert first_hop.exit_code == 1
    assert "must be an absolute path outside the repository" in first_hop.output

    mixed = runner.invoke(
        app,
        [
            "freeze-experimental-mixed-supervision",
            "--output-dir",
            "relative-output",
            "--first-hop-projection-dir",
            "missing-first-hop",
            "--audit-spec",
            "missing-audits.json",
            "--source-theorems",
            "missing-theorems.jsonl",
            "--source-representations",
            "missing-representations.jsonl",
        ],
    )
    assert mixed.exit_code == 1
    assert "must be an absolute path outside the repository" in mixed.output


def test_mixed_freeze_cli_exposes_receipt_bound_composition_roots() -> None:
    result = CliRunner().invoke(
        app,
        ["freeze-experimental-mixed-supervision", "--help"],
        env={"COLUMNS": "200"},
    )

    assert result.exit_code == 0
    assert "--composition-full-run-root" in result.output
    assert "--composition-seed-dir" in result.output
    assert "--composition-postprocess-root" in result.output
    assert "--composition-source-theorems" in result.output
    assert "--composition-source-representations" in result.output


def test_replay_entrypoint_requires_existing_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result = SimpleNamespace(artifacts=SimpleNamespace(replayed=False))
    monkeypatch.setattr(orchestration, "_assemble_and_freeze", lambda **_kwargs: result)

    with pytest.raises(ExperimentalMixedSupervisionError, match="created a new corpus"):
        orchestration.replay_verify_experimental_mixed_supervision_from_artifacts(
            repo_root=tmp_path,
            output_dir=tmp_path / "output",
            config_path=tmp_path / "config",
            first_hop_projection_dir=tmp_path / "first-hop",
            lf022_audits=(),
            source_theorem_paths=(),
            source_representation_paths=(),
        )
