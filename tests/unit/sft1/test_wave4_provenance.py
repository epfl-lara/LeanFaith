from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.sft1.sprint import engine, provenance, square
from leanfaith.sft1.sprint.integrity import validate_view
from leanfaith.sft1.sprint.publish import local_files
from leanfaith.sft1.sprint.runner import project_pins
from leanfaith.sft1.sprint.store import SemanticCache, read_json_object

ROOT = Path(__file__).resolve().parents[3]
IMPLEMENTATION_COMMIT = subprocess.run(
    ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()
ENGINE_SOURCE_SHA256 = sha256_hex(
    subprocess.run(
        ["git", "-C", str(ROOT), "show", "HEAD:LeanFaith/Meta/SFT1/Sprint.lean"],
        check=True,
        capture_output=True,
    ).stdout
)


def _checked(tag: int) -> dict[str, object]:
    return {
        "meta_checked": True,
        "kernel_checked": True,
        "kernel_level_instantiation": "none",
        "proof_expr_hash_u64": str(tag),
    }


def _site() -> dict[str, object]:
    return {
        "kind": "binder_pair",
        "index": 0,
        "detail": "test",
        "guard_variable_index": 0,
        "bound_variable_index": None,
        "literal": 0,
        "path": [3, 0],
    }


def _payload(root: str = "Test.root") -> dict[str, Any]:
    negative = "N31_DROP_REQUIRED_GUARD_PROOF_V1"
    evidence = {
        "negative_operation": negative,
        "direction": "guard",
        "hops": [
            {
                "p_operation": "P14_SWAP_INDEPENDENT_DATA_BINDERS_V1",
                "c_operation": "P14_SWAP_INDEPENDENT_DATA_BINDERS_V1",
                "mechanism": "P14",
                "superclass": "binder_permutation",
                "inverse_token": "adjacent_data_binder_swap",
                "p_site": _site(),
                "c_site": _site(),
                "p_input_alpha_hash": "1",
                "c_input_alpha_hash": "2",
                "p_output_alpha_hash": "3",
                "c_output_alpha_hash": "4",
                "p_direct_iff": _checked(10),
                "c_direct_iff": _checked(11),
                "site_transport": "disjoint_root_coordinates",
            }
        ],
        "p_composite_iff": _checked(12),
        "c_composite_iff": _checked(13),
        "source_proof": {
            "kind": "loaded_environment_constant",
            "constant": root,
            "value_expr_hash_u64": "14",
        },
        "source_proof_check": _checked(15),
        "base_candidate_refutation": {
            "kind": "boundary_counterexample:test",
            "check": _checked(16),
            "grounding": {
                "assignment": [],
                "binder_count": 0,
                "tactic_calls": 0,
                "universe_instantiation": "none",
            },
            "boundary": 0,
            "separator": {"kind": "source_guard_false:test", "check": _checked(17)},
            "witnesses": [],
            "witness_checks": [],
            "enumeration": None,
        },
        "p_prime_transported_proof": _checked(18),
        "c_prime_refutation": _checked(19),
        "not_iff_c_p": _checked(20),
        "not_iff_p_prime_c_prime": _checked(21),
        "negative_last_replay": {
            "operation_id": negative,
            "reference_alpha_hash": "3",
            "candidate_alpha_hash": "4",
            "reference_expr_equal": True,
            "candidate_expr_equal": True,
            "reference_replay_exact": True,
            "candidate_replay_exact": True,
            "site": _site(),
            "refutation": _checked(22),
        },
        "closure": {
            "exact_typed": True,
            "site_policy": "disjoint_only_no_transport_inference",
            "depth": 1,
        },
    }
    evidence["negative_last_replay"]["certificate"] = evidence["base_candidate_refutation"]
    return {
        "schema_version": 1,
        "kind": "wave4_root",
        "status": "retained",
        "reason": "",
        "operation_id": "ORBIT_WAVE4_N31_V1",
        "negative_operation": negative,
        "engine_semantic_version": "placeholder",
        "root": root,
        "module": "Test",
        "level_params": [],
        "enumerated_variant_count": 1,
        "variants": [
            {
                "index": 0,
                "depth": 1,
                "p_alpha_hash": "1",
                "c_alpha_hash": "2",
                "p_prime_alpha_hash": "3",
                "c_prime_alpha_hash": "4",
                "negative_site": _site(),
                "goals": {
                    "p": f"⊢ True -- {root}:p",
                    "c": f"⊢ False -- {root}:c",
                    "p_prime": f"⊢ True -- {root}:p_prime",
                    "c_prime": f"⊢ False -- {root}:c_prime",
                },
                "evidence": evidence,
            }
        ],
    }


def _material(endpoint: str, statement: str) -> dict[str, object]:
    if endpoint == "p":
        return {
            "kind": "raw_statement",
            "raw_statement": statement,
            "proposition_text": None,
            "absence_reason": None,
        }
    return {
        "kind": "constructed_expr_no_source_text",
        "raw_statement": None,
        "proposition_text": None,
        "absence_reason": f"constructed {endpoint}",
    }


def _render_record(endpoint: str, goal: str, statement: str, context_id: str) -> dict[str, object]:
    material = _material(endpoint, statement)
    return {
        "record": {
            "endpoint_id": f"0.{endpoint}",
            "goal_v1": goal,
            "rendered_goal_hash": sha256_hex(goal.encode("utf-8")),
            "compile_context_id": context_id,
            "source_material_hash": hash_canonical(material),
            "spec_hash": "a" * 64,
            "implementation_identity": {"renderer_semantic_hash": "renderer"},
            "provenance": {"expr_hash": hash_canonical([endpoint, goal])},
        },
        "source_material": material,
    }


def _fixture(tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any], Path, Any]:
    loaded = square.load_wave4_config(ROOT)
    pins = project_pins(loaded.runtime.config)
    context = engine.build_compile_context(ROOT, pins)
    identity = engine.engine_identity(ROOT, pins, context).to_dict()
    project = pins.to_dict()
    root = "Test.root"
    root_id = "root:" + hash_canonical([project["project_id"], project["project_revision"], root])
    statement = "theorem Test.root : True := by trivial"
    payload = _payload()
    payload["engine_semantic_version"] = identity["semantic_version"]
    descriptors = square.preselect_wave4_variant_descriptors(
        payload,
        operation_id="ORBIT_WAVE4_N31_V1",
        policy=loaded.policy,
        maximum_depth=3,
        expected_root=root,
        selection_root_id=root_id,
    )
    validated = square.validate_wave4_root_payload(
        payload,
        operation_id="ORBIT_WAVE4_N31_V1",
        policy=loaded.policy,
        maximum_depth=3,
        expected_root=root,
        selected_descriptors=descriptors,
        selection_root_id=root_id,
    )
    payload["enumeration_hash"] = validated.enumeration_hash
    variant = square.select_wave4_variants(validated, loaded.policy)[0]
    goals = variant.raw["goals"]
    selected: dict[str, Any] = {
        "index": variant.index,
        "selection_hash": variant.selection_hash,
        "content_hash": variant.content_hash,
        "reference_chain_hash": variant.reference_chain_hash,
        "candidate_chain_hash": variant.candidate_chain_hash,
        "reference_site_hash": variant.reference_site_hash,
        "candidate_site_hash": variant.candidate_site_hash,
        "variant": variant.raw,
        "render": {
            endpoint: _render_record(
                endpoint, goals[endpoint], statement, context.compile_context_id
            )
            for endpoint in ("p", "c", "p_prime", "c_prime")
        },
    }
    process_hash = "b" * 64
    render_hash = "c" * 64
    commit = "d" * 40
    cache_record = {
        "schema_version": 1,
        "kind": provenance.WAVE4_CACHE_KIND,
        "cache_schema": provenance.WAVE4_CACHE_SCHEMA,
        "operation_id": "ORBIT_WAVE4_N31_V1",
        "operation_revision": 1,
        "root": root,
        "status": "retained",
        "reason": "",
        "policy_hash": loaded.policy.policy_hash,
        "maximum_depth": 3,
        "payload": payload,
        "enumeration_hash": validated.enumeration_hash,
        "selected": [selected],
        "engine": identity,
        "implementation_commit": commit,
        "process_request_hash": process_hash,
        "render_request_hash": render_hash,
    }
    key = square.wave4_cache_key(
        operation_id="ORBIT_WAVE4_N31_V1",
        name=root,
        policy_hash=loaded.policy.policy_hash,
        maximum_depth=3,
        engine_source_sha256=str(identity["source_sha256"]),
        compile_context_id=str(identity["compile_context_id"]),
        engine_semantic_version=str(identity["semantic_version"]),
        project_revision=str(project["project_revision"]),
        lean_version=str(project["lean_version"]),
        import_options_fingerprint=str(identity["import_options_fingerprint"]),
        revision=1,
    )
    evidence = variant.raw["evidence"]
    row_evidence, row_check = square.Wave4Runner._row_evidence(
        "negative_last", evidence, variant.selection_hash
    )
    sidecar = {
        "pair_id": "pair:test",
        "root_id": root_id,
        "root_name": root,
        "module": "Test",
        "statement": statement,
        "operation_id": "ORBIT_WAVE4_N31_V1",
        "negative_operation": "N31_DROP_REQUIRED_GUARD_PROOF_V1",
        "row_kind": "negative_last",
        "label": False,
        "wave4": {
            "selection_hash": variant.selection_hash,
            "content_hash": variant.content_hash,
            "enumeration_hash": validated.enumeration_hash,
            "variant_index": 0,
            "depth": 1,
            "reference_chain_hash": variant.reference_chain_hash,
            "candidate_chain_hash": variant.candidate_chain_hash,
            "reference_site_hash": variant.reference_site_hash,
            "candidate_site_hash": variant.candidate_site_hash,
            "logical_role": "negative_last",
        },
        "evidence": row_evidence,
        "evidence_hash": hash_canonical(row_evidence),
        "site": {
            "kind": "wave4_chain",
            "detail": hash_canonical([variant.reference_site_hash, variant.candidate_site_hash]),
        },
        "repr": {
            "reference": selected["render"]["p_prime"]["record"],
            "candidate": selected["render"]["c_prime"]["record"],
            "reference_source_material": selected["render"]["p_prime"]["source_material"],
            "candidate_source_material": selected["render"]["c_prime"]["source_material"],
        },
        "project": project,
        "engine": identity,
        "lean_request_hashes": {"process": process_hash, "render": render_hash},
        "level_params": [],
        "implementation_commit": commit,
        "runner_source_sha256": hash_file(ROOT / "src/leanfaith/sft1/sprint/square.py"),
        "row_check": row_check,
    }
    content_sha = hash_canonical(cache_record)
    snapshot_file = "cache_records/shard-0001.jsonl"
    sidecar["cache"] = {
        "kind": provenance.WAVE4_CACHE_KIND,
        "schema": provenance.WAVE4_CACHE_SCHEMA,
        "revision": 1,
        "key": key,
        "path": f"roots/{key[:2]}/{key}.json",
        "content_sha256": content_sha,
        "snapshot": {"file": snapshot_file, "line": 0, "content_sha256": content_sha},
    }
    release = tmp_path / "release"
    (release / "cache_records").mkdir(parents=True)
    (release / snapshot_file).write_text(json.dumps(cache_record) + "\n", encoding="utf-8")
    return sidecar, cache_record, release, loaded.policy


def _rewrite_snapshot(sidecar: dict[str, Any], record: dict[str, Any], release: Path) -> None:
    digest = hash_canonical(record)
    sidecar["cache"]["content_sha256"] = digest
    sidecar["cache"]["snapshot"]["content_sha256"] = digest
    (release / "cache_records/shard-0001.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )


def _verify(
    sidecar: dict[str, Any], tmp_path: Path, release: Path, policy: Any
) -> tuple[int | None, list[str], bool | None]:
    return provenance.verify_square_cache(
        sidecar,
        tmp_path / "cache",
        snapshots=provenance.SnapshotStore(release),
        repo_root=ROOT,
        wave4_policy=policy,
    )


def test_wave4_snapshot_binds_full_identity_and_is_derived_as_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecar, _record, release, policy = _fixture(tmp_path)
    assert _verify(sidecar, tmp_path, release, policy) == (
        provenance.WAVE4_CACHE_SCHEMA,
        [],
        None,
    )
    monkeypatch.setattr(
        provenance,
        "engine_commit_map",
        lambda _root: {str(sidecar["engine"]["source_sha256"]): ["d" * 40]},
    )
    derived = provenance.derive_provenance(
        [{"row": {"reference": "x", "candidate": "y", "label": False}, "sidecar": sidecar}],
        repo_root=ROOT,
        cache_root=tmp_path / "cache",
        release_dir=release,
    )
    assert derived["consistent"], derived["issues"]
    assert derived["wave4_cache_records_verified"] == 1
    assert derived["square_cache_records_verified"] == 0


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("options", "import/options fingerprint"),
        ("import", "import/options fingerprint"),
        ("project", "ancestry root identity"),
        ("root", "ancestry root identity"),
        ("source", "root statement"),
        ("chain", "reference_chain_hash"),
        ("site", "selected site identity"),
        ("label", "row label"),
        ("engine", "cache record engine identity"),
        ("checker", "checker/engine semantic version"),
    ],
)
def test_wave4_snapshot_rejects_identity_mismatch(
    tmp_path: Path, mutation: str, message: str
) -> None:
    sidecar, record, release, policy = _fixture(tmp_path)
    if mutation == "options":
        sidecar["project"]["options"]["autoImplicit"] = True
    elif mutation == "import":
        sidecar["project"]["import_header"] = "import Std"
    elif mutation == "project":
        sidecar["project"]["project_id"] = "other"
    elif mutation == "root":
        sidecar["root_name"] = "Test.other"
    elif mutation == "source":
        sidecar["statement"] = "theorem Test.root : False := by contradiction"
    elif mutation == "chain":
        sidecar["wave4"]["reference_chain_hash"] = "0" * 64
    elif mutation == "site":
        sidecar["site"]["detail"] = "0" * 64
    elif mutation == "label":
        sidecar["label"] = True
    elif mutation == "engine":
        sidecar["engine"]["source_sha256"] = "0" * 64
    elif mutation == "checker":
        record["payload"]["engine_semantic_version"] = "other-checker"
        _rewrite_snapshot(sidecar, record, release)
    else:  # pragma: no cover - the parametrization is exhaustive
        raise AssertionError(mutation)
    assert any(message in issue for issue in _verify(sidecar, tmp_path, release, policy)[1])


@pytest.mark.parametrize("mutation", ["base", "negative_last"])
def test_wave4_snapshot_rejects_tampered_certificate_closure(tmp_path: Path, mutation: str) -> None:
    sidecar, record, release, policy = _fixture(tmp_path)
    evidence = record["selected"][0]["variant"]["evidence"]
    if mutation == "base":
        del evidence["base_candidate_refutation"]["separator"]
    else:
        evidence["negative_last_replay"]["candidate_replay_exact"] = False
    record["payload"]["variants"][0] = record["selected"][0]["variant"]
    _rewrite_snapshot(sidecar, record, release)
    issues = _verify(sidecar, tmp_path, release, policy)[1]
    assert any("Wave 4 cache record invalid" in issue for issue in issues)


def test_wave4_cache_rejects_legacy_live_only_ambiguity(tmp_path: Path) -> None:
    sidecar, record, release, policy = _fixture(tmp_path)
    live_path = tmp_path / "cache" / sidecar["cache"]["path"]
    live_path.parent.mkdir(parents=True)
    live_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    sidecar["cache"]["snapshot"] = None
    assert _verify(sidecar, tmp_path, release, policy)[1] == [
        "Wave 4 release requires an immutable cache snapshot"
    ]


def test_wave4_snapshot_rejects_content_tampering(tmp_path: Path) -> None:
    sidecar, record, release, policy = _fixture(tmp_path)
    record["render_request_hash"] = "0" * 64
    (release / "cache_records/shard-0001.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )
    assert _verify(sidecar, tmp_path, release, policy)[1] == [
        "Wave 4 cache snapshot content hash differs from the sidecar"
    ]


def test_snapshot_store_rejects_release_path_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside.jsonl"
    outside.write_text("{}\n", encoding="utf-8")
    store = provenance.SnapshotStore(tmp_path / "release")
    assert store.load({"file": "../outside.jsonl", "line": 0}) is None


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def _release_identity(project_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    loaded = square.load_wave4_config(ROOT)
    project = project_pins(loaded.runtime.config).to_dict()
    project["project_id"] = project_id
    project["project_dir"] = f"/synthetic/{project_id}"
    project["project_revision"] = hash_canonical([project_id, "revision"])[:40]
    import_fingerprint = hash_canonical(
        {
            "import_header": project["import_header"],
            "options": dict(sorted(project["options"].items())),
            "lean_version": project["lean_version"],
            "project_revision": project["project_revision"],
        }
    )
    identity = {
        "source_sha256": ENGINE_SOURCE_SHA256,
        "compile_context_id": "ctx:" + hash_canonical([project_id, project["project_revision"]]),
        "semantic_version": "sft1-wave4-test-engine-v1",
        "import_options_fingerprint": import_fingerprint,
    }
    return project, identity


def _certified_root(
    *,
    loaded: Any,
    project: dict[str, Any],
    identity: dict[str, Any],
    root: str,
    cache_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    operation = "ORBIT_WAVE4_N31_V1"
    statement = f"theorem {root} : True := by trivial"
    root_id = "root:" + hash_canonical([project["project_id"], project["project_revision"], root])
    payload = _payload(root)
    payload["engine_semantic_version"] = identity["semantic_version"]
    descriptors = square.preselect_wave4_variant_descriptors(
        payload,
        operation_id=operation,
        policy=loaded.policy,
        maximum_depth=3,
        expected_root=root,
        selection_root_id=root_id,
    )
    validated = square.validate_wave4_root_payload(
        payload,
        operation_id=operation,
        policy=loaded.policy,
        maximum_depth=3,
        expected_root=root,
        selected_descriptors=descriptors,
        selection_root_id=root_id,
    )
    payload["enumeration_hash"] = validated.enumeration_hash
    variant = square.select_wave4_variants(validated, loaded.policy)[0]
    goals = variant.raw["goals"]
    selected = {
        "index": variant.index,
        "selection_hash": variant.selection_hash,
        "content_hash": variant.content_hash,
        "reference_chain_hash": variant.reference_chain_hash,
        "candidate_chain_hash": variant.candidate_chain_hash,
        "reference_site_hash": variant.reference_site_hash,
        "candidate_site_hash": variant.candidate_site_hash,
        "variant": variant.raw,
        "render": {
            endpoint: _render_record(
                endpoint, goals[endpoint], statement, identity["compile_context_id"]
            )
            for endpoint in ("p", "c", "p_prime", "c_prime")
        },
    }
    process_hash = hash_canonical([root, "process"])
    render_request_hash = hash_canonical([root, "render"])
    cache_record = {
        "schema_version": 1,
        "kind": provenance.WAVE4_CACHE_KIND,
        "cache_schema": provenance.WAVE4_CACHE_SCHEMA,
        "operation_id": operation,
        "operation_revision": 1,
        "root": root,
        "status": "retained",
        "reason": "",
        "policy_hash": loaded.policy.policy_hash,
        "maximum_depth": 3,
        "payload": payload,
        "enumeration_hash": validated.enumeration_hash,
        "selected": [selected],
        "engine": identity,
        "implementation_commit": IMPLEMENTATION_COMMIT,
        "process_request_hash": process_hash,
        "render_request_hash": render_request_hash,
    }
    key = square.wave4_cache_key(
        operation_id=operation,
        name=root,
        policy_hash=loaded.policy.policy_hash,
        maximum_depth=3,
        engine_source_sha256=identity["source_sha256"],
        compile_context_id=identity["compile_context_id"],
        engine_semantic_version=identity["semantic_version"],
        project_revision=project["project_revision"],
        lean_version=project["lean_version"],
        import_options_fingerprint=identity["import_options_fingerprint"],
        revision=1,
    )
    SemanticCache(cache_root).put_root(key, cache_record)
    runner = object.__new__(square.Wave4Runner)
    runner.operation_id = operation
    runner.base = SimpleNamespace(
        root_id=lambda _name: root_id,
        pins=SimpleNamespace(to_dict=lambda: dict(project)),
        identity=SimpleNamespace(to_dict=lambda: dict(identity)),
    )
    runner.square_root_key = lambda _name: key
    runner.statements = {root: statement}
    return runner.build_wave4_rows(root, cache_record, {"name": root})


def _wave4_run(
    root: Path,
    *,
    project_id: str,
    root_count: int,
    recovered: bool = False,
    suffix: str = "",
) -> Path:
    loaded = square.load_wave4_config(ROOT)
    project, identity = _release_identity(project_id)
    run_id = f"wave4-{project_id}{suffix}"
    run_dir = root / run_id
    cache_root = root / "cache"
    rows: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    terminals: list[dict[str, Any]] = []
    for index in range(root_count):
        name = f"Test.{project_id}{suffix.replace('-', '_')}_{index}"
        root_rows, root_groups = _certified_root(
            loaded=loaded,
            project=project,
            identity=identity,
            root=name,
            cache_root=cache_root,
        )
        rows.extend(root_rows)
        groups.extend(root_groups)
        terminals.append(
            {
                "kind": "square_terminal",
                "root": name,
                "status": "retained",
                "reason": "",
                "source": "recovered" if recovered and index == 0 else "cache",
                "batch": 1,
                "pair_ids": [item["sidecar"]["pair_id"] for item in root_rows],
                "logical_groups": root_groups,
            }
        )
    manifest = {
        "schema_version": 1,
        "sprint_id": "wave4-test",
        "run_id": run_id,
        "runner_kind": provenance.WAVE4_CACHE_KIND,
        "operation_id": "ORBIT_WAVE4_N31_V1",
        "wave4_cache_schema": provenance.WAVE4_CACHE_SCHEMA,
        "wave4_policy_hash": loaded.policy.policy_hash,
        "wave4_maximum_depth": 3,
        "project": project,
        "engine": identity,
        "implementation_commit": IMPLEMENTATION_COMMIT,
        "implementation_dirty": False,
        "max_roots": root_count,
        "cache_root": str(cache_root),
        "replay_requested": False,
        "argv": ["synthetic"],
    }
    status = {
        "run_id": run_id,
        "runner_kind": provenance.WAVE4_CACHE_KIND,
        "operation_id": "ORBIT_WAVE4_N31_V1",
        "policy_hash": loaded.policy.policy_hash,
        "maximum_depth": 3,
        "roots_considered": root_count,
        "roots_lean": 0,
        "roots_cache": root_count,
        "retained_roots": root_count,
        "retained_variants": len(groups),
        "logical_rows": len(groups) * 4,
        "physical_rows": len(rows),
        "terminals_by_status": {"retained": root_count},
        "lean_requests": 0,
        "lean_elapsed_ms": 0,
        "wall_seconds": 0.1,
        "peak_process_tree_rss_bytes": 1024,
        "final": True,
        "replay_mode": False,
    }
    replay = {
        "run_id": run_id,
        "lean_requests": 0,
        "duplicate_rows": 0,
        "retained_before": len(rows),
        "retained_after": len(rows),
        "roots_considered": root_count,
    }
    _write_json(run_dir / "run.json", manifest)
    _write_json(run_dir / "status.json", status)
    _write_json(run_dir / "replay_report.json", replay)
    (run_dir / "journal.jsonl").write_bytes(
        b"".join(canonical_json_bytes(item) + b"\n" for item in terminals)
    )
    (run_dir / "retained.jsonl").write_bytes(
        b"".join(canonical_json_bytes(item) + b"\n" for item in rows)
    )
    return run_dir


def _gate_report(path: Path, policy_hash: str) -> Path:
    document = {
        "schema_version": 1,
        "kind": "sft1_wave4_composition_gate_v1",
        "gate_id": "wave4_gate:" + hash_canonical(["test", policy_hash]),
        "release_id": "wave4_release:test-gate",
        "policy_hash": policy_hash,
        "unique_ancestry_roots": 200,
        "physical_rows": 800,
        "logical_groups": 200,
        "logical_rows": 800,
        "source_receipts_sha256": hash_canonical(["test-runs"]),
        "source_runs": [{"checks": {"zero_call_replay": True}}],
        "manual_inspection": {"passed": True},
        "manifest_sha256": "1" * 64,
        "integrity_report_sha256": "2" * 64,
        "shortcut_screens_sha256": "3" * 64,
        "checks": {
            "exactly_200_ancestry_roots": True,
            "all_source_runs_replayed_without_calls": True,
            "forced_resume_observed": True,
            "exact_certificate_closure": True,
            "all_four_logical_roles": True,
            "manual_inspection": True,
            "shortcut_screens": True,
            "integrity": True,
            "zero_n19": True,
            "n25_cap": True,
        },
        "passed": True,
    }
    document["content_binding_sha256"] = hash_canonical(document)
    _write_json(path, document)
    return path


def _passing_release_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        square,
        "_wave4_release_builder_identity",
        lambda _root: {
            "commit": IMPLEMENTATION_COMMIT,
            "dirty": False,
            "square_source_sha256": hash_file(ROOT / "src/leanfaith/sft1/sprint/square.py"),
        },
    )
    from leanfaith.sft1.sprint import shortcut

    monkeypatch.setattr(
        shortcut,
        "run_screens_v3",
        lambda _records: {
            "screens": [
                {"name": "candidate_only", "passed": True},
                {"name": "reference_only", "passed": True},
                {"name": "family_held_out", "passed": True},
            ],
            "passed": True,
        },
    )
    monkeypatch.setattr(shortcut, "pairwise_shortcut_diagnostics", lambda _records: {})
    monkeypatch.setattr(shortcut, "outer_negation_xor_baseline", lambda _records: {})
    monkeypatch.setattr(shortcut, "permutation_control", lambda _records: {})


def _release_runs(tmp_path: Path) -> list[Path]:
    runs_root = tmp_path / "runs"
    return [
        _wave4_run(
            runs_root,
            project_id="mathlib",
            root_count=1,
            recovered=True,
            suffix="-a",
        ),
        _wave4_run(runs_root, project_id="mathlib", root_count=1, suffix="-b"),
        _wave4_run(runs_root, project_id="physlib", root_count=1),
        _wave4_run(runs_root, project_id="cslib", root_count=1),
    ]


def _build_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _passing_release_environment(monkeypatch)
    loaded = square.load_wave4_config(ROOT)
    output = tmp_path / "release"
    square.build_wave4_release(
        ROOT,
        loaded,
        run_dirs=_release_runs(tmp_path),
        output_dir=output,
        composition_gate_report=_gate_report(tmp_path / "gate.json", loaded.policy.policy_hash),
    )
    return output


def _rewrite_shard_manifest(output: Path, *, groups: bool = False) -> None:
    manifest = read_json_object(output / "manifest.json")
    shard = output / "shard-0001"
    shard_manifest = read_json_object(shard / "manifest.json")
    field = "closure_groups_sha256" if groups else "rows_sha256"
    filename = "closure_groups.jsonl" if groups else "rows.jsonl"
    shard_manifest[field] = hash_file(shard / filename)
    shard_manifest["content_sha256"] = hash_canonical(
        {key: value for key, value in shard_manifest.items() if key != "content_sha256"}
    )
    _write_json(shard / "manifest.json", shard_manifest)
    manifest["shards"][0] = shard_manifest
    _write_json(output / "manifest.json", manifest)


def test_wave4_explicit_release_accepts_multiple_runs_per_project_and_is_publishable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _build_release(tmp_path, monkeypatch)
    manifest = read_json_object(output / "manifest.json")
    report = read_json_object(output / "release_report.json")
    integrity = read_json_object(output / "integrity_report.json")
    assert report["passed"] is True
    assert integrity["passed"] is True
    assert set(manifest["projects"]) == {"mathlib", "physlib", "cslib"}
    assert len(manifest["source_runs"]) == 4
    assert sum(item["project_id"] == "mathlib" for item in manifest["source_runs"]) == 2
    assert all(
        set(json.loads(line)) == {"reference", "candidate", "label"}
        for path in output.glob("shard-*/rows.jsonl")
        for line in path.read_text("utf-8").splitlines()
    )
    upload_set = {path.relative_to(output).as_posix() for path in local_files(output)}
    assert "composition_gate_report.json" in upload_set
    assert "cache_records/shard-0001.jsonl" in upload_set
    assert "shard-0001/closure_groups.jsonl" in upload_set


@pytest.mark.parametrize("artifact", ["row", "group", "snapshot", "receipt"])
def test_wave4_integrity_detects_release_or_source_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, artifact: str
) -> None:
    output = _build_release(tmp_path, monkeypatch)
    if artifact == "row":
        path = output / "shard-0001/rows.jsonl"
        rows = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
        rows[0]["candidate"] += " changed"
        path.write_bytes(b"".join(canonical_json_bytes(item) + b"\n" for item in rows))
        _rewrite_shard_manifest(output)
    elif artifact == "group":
        path = output / "shard-0001/closure_groups.jsonl"
        groups = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
        groups[0]["closure_certificate_hash"] = "0" * 64
        path.write_bytes(b"".join(canonical_json_bytes(item) + b"\n" for item in groups))
        _rewrite_shard_manifest(output, groups=True)
    elif artifact == "snapshot":
        path = output / "cache_records/shard-0001.jsonl"
        values = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
        values[0]["reason"] = "tampered"
        path.write_bytes(b"".join(canonical_json_bytes(item) + b"\n" for item in values))
    else:
        manifest = read_json_object(output / "manifest.json")
        status = Path(manifest["source_runs"][0]["run_dir"]) / "status.json"
        document = read_json_object(status)
        document["final"] = False
        _write_json(status, document)
    manifest = read_json_object(output / "manifest.json")
    report = validate_view(
        repo_root=ROOT,
        staging_root=output.parent,
        run_id=manifest["release_id"],
        compacted_dir=output,
    )
    assert report["passed"] is False
    assert report["issues"]


def test_wave4_release_rejects_dirty_generator_or_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _passing_release_environment(monkeypatch)
    loaded = square.load_wave4_config(ROOT)
    runs = _release_runs(tmp_path)
    manifest_path = runs[0] / "run.json"
    manifest = read_json_object(manifest_path)
    manifest["implementation_dirty"] = True
    _write_json(manifest_path, manifest)
    with pytest.raises(square.SquareError, match="not release-authorized"):
        square.build_wave4_release(
            ROOT,
            loaded,
            run_dirs=runs,
            output_dir=tmp_path / "dirty-generator",
            composition_gate_report=_gate_report(tmp_path / "gate.json", loaded.policy.policy_hash),
        )
    monkeypatch.setattr(
        square,
        "_wave4_release_builder_identity",
        lambda _root: {"commit": IMPLEMENTATION_COMMIT, "dirty": True},
    )
    with pytest.raises(square.SquareError, match="clean release-builder"):
        square.build_wave4_release(
            ROOT,
            loaded,
            run_dirs=runs[1:],
            output_dir=tmp_path / "dirty-builder",
            composition_gate_report=tmp_path / "gate.json",
        )


def test_wave4_release_rejects_unreachable_generator_or_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _passing_release_environment(monkeypatch)
    loaded = square.load_wave4_config(ROOT)
    runs = _release_runs(tmp_path)
    run_manifest_path = runs[0] / "run.json"
    run_manifest = read_json_object(run_manifest_path)
    run_manifest["implementation_commit"] = "0" * 40
    _write_json(run_manifest_path, run_manifest)
    gate = _gate_report(tmp_path / "gate.json", loaded.policy.policy_hash)
    with pytest.raises(square.SquareError, match="not release-authorized"):
        square.build_wave4_release(
            ROOT,
            loaded,
            run_dirs=runs,
            output_dir=tmp_path / "unreachable-generator",
            composition_gate_report=gate,
        )
    monkeypatch.setattr(
        square,
        "_wave4_release_builder_identity",
        lambda _root: {"commit": "0" * 40, "dirty": False},
    )
    with pytest.raises(square.SquareError, match="not an ancestor"):
        square.build_wave4_release(
            ROOT,
            loaded,
            run_dirs=runs[1:],
            output_dir=tmp_path / "unreachable-builder",
            composition_gate_report=gate,
        )


def test_wave4_200_root_gate_binds_manual_inspection_and_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _passing_release_environment(monkeypatch)
    runs_root = tmp_path / "gate-runs"
    runs = [
        _wave4_run(
            runs_root,
            project_id="mathlib",
            root_count=67,
            recovered=True,
        ),
        _wave4_run(runs_root, project_id="physlib", root_count=67),
        _wave4_run(runs_root, project_id="cslib", root_count=66),
    ]
    sample = tmp_path / "inspection.jsonl"
    sampled = [
        json.loads(line)
        for run in runs
        for line in run.joinpath("retained.jsonl").read_text("utf-8").splitlines()
    ]
    sample.write_bytes(b"".join(canonical_json_bytes(item) + b"\n" for item in sampled))
    verdict = tmp_path / "verdict.json"
    _write_json(
        verdict,
        {
            "schema_version": 1,
            "rows_read_by_hand": len(sampled),
            "wrong_labels_found": 0,
            "sample_path": str(sample),
            "sample_sha256": hash_file(sample),
        },
    )
    output = tmp_path / "gate-release"
    report = square.build_wave4_release(
        ROOT,
        square.load_wave4_config(ROOT),
        run_dirs=runs,
        output_dir=output,
        gate_200=True,
        inspection_verdict_paths=[verdict],
    )
    gate = read_json_object(output / "composition_gate_report.json")
    assert report["passed"] is True
    assert gate["passed"] is True
    assert gate["unique_ancestry_roots"] == 200
    assert gate["checks"]["forced_resume_observed"] is True
    assert gate["manual_inspection"]["passed"] is True
    assert gate["manual_inspection"]["exact_release_coverage"] is True

    released_pair_ids = frozenset(
        str(item["pair_id"])
        for path in output.glob("shard-*/sidecars.jsonl")
        for item in (json.loads(line) for line in path.read_text("utf-8").splitlines())
    )
    sample.write_bytes(b"".join(canonical_json_bytes(item) + b"\n" for item in sampled[:-1]))
    _write_json(
        verdict,
        {
            "schema_version": 1,
            "rows_read_by_hand": len(sampled) - 1,
            "wrong_labels_found": 0,
            "sample_path": str(sample),
            "sample_sha256": hash_file(sample),
        },
    )
    missing_one = square._wave4_inspection_receipts([verdict], released_pair_ids=released_pair_ids)
    assert missing_one["passed"] is False
    assert missing_one["missing_pair_count"] == 1
