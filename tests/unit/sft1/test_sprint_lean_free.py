"""Lean-free invariants for the compact SFT1 sprint engine adapter, screens,
inventory scanner, durable store, and compaction."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical
from leanfaith.representations.goal_v1 import _closed_expr_command
from leanfaith.sft1.sprint import engine, inventory, screens, store
from leanfaith.sft1.sprint.runner import (
    OPERATIONS,
    SprintConfig,
    inspection_sample,
    load_sprint_config,
)

ROOT = Path(__file__).resolve().parents[3]


def test_config_loads_and_pins_engine_operations() -> None:
    loaded = load_sprint_config(ROOT)
    config = loaded.config
    configured = set(config.engine.operations)
    assert tuple(config.engine.operations) == OPERATIONS[:9]
    assert config.project.options == {"Elab.async": False, "autoImplicit": False}
    assert config.execution.lean_workers == 1
    success = {f.operation_id for f in config.fixtures if f.expect_status == "retained"}
    rejection = {f.operation_id for f in config.fixtures if f.expect_status != "retained"}
    # P_DROP_REDUNDANT_GUARD_PROOF_V1 receives its success fixture once the budgeted yield run
    # identifies a Mathlib root with a provably redundant guard.
    waived = {waiver.operation_id for waiver in config.fixtures_success_waivers}
    assert waived == {"P_DROP_REDUNDANT_GUARD_PROOF_V1"}
    assert configured - success <= waived
    assert rejection == configured


def test_config_rejects_async_elaboration() -> None:
    loaded = load_sprint_config(ROOT)
    payload = loaded.config.model_dump(mode="json")
    payload["project"]["options"]["Elab.async"] = True
    with pytest.raises(ValueError):
        SprintConfig.model_validate(payload)


def test_engine_source_declares_version_and_no_forbidden_tokens() -> None:
    source = (ROOT / engine.ENGINE_RELATIVE_PATH).read_text(encoding="utf-8")
    assert engine.engine_semantic_version(ROOT) == "sft1_wave5_compiler_engine_v1"
    for token in ("sorry", "addDecl", "addAndCompile", "ppGoal", "mkSorry", "sorryAx"):
        assert token not in source.replace("hasSorry", ""), token
    assert "Kernel.check" in source
    assert "Iff.intro" in source
    # N32 is no longer hard-coded to Nat/Int.  Its strict-order path builds the
    # exact generic asymmetry proof from the source and candidate applications;
    # unsupported typeclasses fall back to a checked concrete counterexample.
    assert "mkAppM ``lt_asymm #[sourceApp]" in source
    assert "return mkApp asymm candApp" in source
    assert "if Expr.eqv args[2]! args[3]! then none" in source
    assert 'groundedCandidateRefute root cand "role_order_boundary_counterexample"' in source
    assert 'throwRej "n32_certificate_search_heartbeat_limit"' in source
    assert "groundTelescope cand0" in source
    assert 'checkedProof "refutation"' in source


def test_process_body_and_mask_round_trip() -> None:
    mask = engine.operation_mask(["P15_SWAP_IFF_SIDES_V1", "N31_DROP_REQUIRED_GUARD_PROOF_V1"])
    assert engine.operations_in_mask(mask) == (
        "P15_SWAP_IFF_SIDES_V1",
        "N31_DROP_REQUIRED_GUARD_PROOF_V1",
    )
    body = engine.process_body([("Nat.foo", mask), ("Foo.«bar baz».qux'", 127)])
    assert body.startswith("run_meta do")
    assert '#["Nat.foo", "Foo.«bar baz».qux\'"] #[65, 127]' in body
    assert "`" not in body


def test_render_body_has_exactly_two_emitter_calls_per_pair_and_no_runtime_tokens() -> None:
    body = engine.render_body(
        [("Nat.foo", "P15_SWAP_IFF_SIDES_V1"), ("Nat.bar", "N25_TOGGLE_EQ_NE_PROOF_V1")], "scope:1"
    )
    assert body.count("LeanFaith.GoalV1.emitClosedProp") == 4
    for token in ("IO.println", "Term.elabTerm", "addDecl", "trace"):
        assert token not in body
    inputs = engine.render_inputs(
        [("Nat.foo", "P15_SWAP_IFF_SIDES_V1")], {"Nat.foo": "theorem foo : 1 = 1"}
    )
    assert [item.endpoint_id for item in inputs] == ["0.reference", "0.candidate"]
    context = engine.build_compile_context(
        ROOT,
        engine.ProjectPins(
            project_id="mathlib",
            project_dir=Path("/nonexistent"),
            project_revision="rev",
            lean_version="v",
            lean_interact_version="0.11.4",
            repl_revision="r",
            import_header="import Mathlib",
            options={"Elab.async": False},
        ),
    )
    command = _closed_expr_command(context, body)
    assert command.startswith("import Lean\nimport Mathlib")
    assert "namespace LeanFaith.SFT1.Sprint" in command


def test_evidence_line_parsing() -> None:
    messages = [
        {"severity": "info", "data": 'LFSFT1SPRINTJSON {"kind":"root","root":"Nat.foo"}\nother'},
        {"severity": "warning", "data": "unrelated"},
    ]
    parsed = engine.parse_evidence_lines(messages)
    assert parsed == [{"kind": "root", "root": "Nat.foo"}]


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        ("n : ℕ\n⊢ n = n", None),
        ("inst✝ : Group G\nG : Type u_0\n⊢ True", None),
        ("n✝ : ℕ\n⊢ n✝ = 0", "dagger_on_ordinary_local"),
        ("n : ℕ\n⊢ ∀ x✝, x✝ = n", "dagger_in_target"),
        ("[anonymous] : True\n⊢ False", "anonymous_binder_name"),
        ("n : ℕ\n⊢ n = ⋯", "forbidden_rendered_placeholder"),
        ("n : ℕ\n⊢ n = n\n⊢ True", "wrong_turnstile_count"),
        ("⊢ True", None),
    ),
)
def test_residue_policy(text: str, expected: str | None) -> None:
    assert screens.residue_violation(text) == expected


def test_instance_dagger_count_and_local_names() -> None:
    text = "α : Type u_0\ninst✝¹ : Preorder α\ninst✝ : Nonempty α\na b : α\n⊢ a ≤ b"
    assert screens.local_names(text) == ["α", "inst✝¹", "inst✝", "a", "b"]
    assert screens.instance_dagger_count(text) == 2
    assert screens.residue_violation(text) is None


def test_gold_blocklist_loads_pinned_file() -> None:
    loaded = load_sprint_config(ROOT)
    gold = screens.GoldBlocklist.load(
        ROOT / loaded.config.screens.gold_blocklist_path,
        expected_sha256=loaded.config.screens.gold_blocklist_sha256,
    )
    assert len(gold.near_dup_hashes) > 5000
    assert gold.hit("n : ℕ\n⊢ n = n") is False
    with pytest.raises(screens.ScreenError):
        screens.GoldBlocklist.load(
            ROOT / loaded.config.screens.gold_blocklist_path, expected_sha256="0" * 64
        )


def test_deduplicate_keeps_min_hash_and_rejects_conflicts() -> None:
    key_a = screens.unordered_pair_key("h1", "h2")
    assert key_a == screens.unordered_pair_key("h2", "h1")
    records = [
        {"unordered_pair_key": key_a, "row_hash": "b", "label": True},
        {"unordered_pair_key": key_a, "row_hash": "a", "label": True},
        {"unordered_pair_key": "k2", "row_hash": "c", "label": True},
        {"unordered_pair_key": "k2", "row_hash": "d", "label": False},
        {"unordered_pair_key": "k3", "row_hash": "e", "label": False},
    ]
    outcome = screens.deduplicate(records)
    assert [item["row_hash"] for item in outcome.kept] == ["a", "e"]
    assert outcome.duplicate_count == 1
    assert outcome.conflict_count == 1 and outcome.conflict_keys == ("k2",)


def test_inventory_scanner_tracks_namespaces_and_statements() -> None:
    source = """
/-! doc -/
namespace Nat
variable {n : ℕ}

/-- doc with theorem inside -/
theorem foo (h : 0 < n) : n ≠ 0 := by
  omega

@[simp] lemma bar : n + 0 = n := rfl

private theorem hidden : True := trivial

section
theorem _root_.Int.baz : (1 : ℤ) = 1 := rfl
end

theorem multi {m : ℕ}
    (h : m ≤ n) :
    m ≤ n + 1 :=
  h.trans (Nat.le_succ _)

theorem pattern : ∀ n, 0 < n + 1
  | 0 => by simp
  | n + 1 => by simp
end Nat
theorem top : True := trivial
"""
    decls = list(
        inventory.scan_source(
            source, module="Mathlib.Data.Nat.Test", path="Mathlib/Data/Nat/Test.lean"
        )
    )
    names = [d.name for d in decls]
    assert names == ["Nat.foo", "Nat.bar", "Int.baz", "Nat.multi", "Nat.pattern", "top"]
    by_name = {d.name: d for d in decls}
    assert by_name["Nat.foo"].statement == "theorem foo (h : 0 < n) : n ≠ 0"
    assert by_name["Nat.multi"].statement == "theorem multi {m : ℕ} (h : m ≤ n) : m ≤ n + 1"
    assert by_name["Nat.pattern"].statement == "theorem pattern : ∀ n, 0 < n + 1"
    assert by_name["Nat.bar"].statement == "@[simp] lemma bar : n + 0 = n"
    assert by_name["Int.baz"].line == 15


def test_lean_name_literal_escapes_unusual_components() -> None:
    assert inventory.lean_name_literal("Nat.foo") == "`Nat.foo"
    assert inventory.lean_name_literal("Nat.«foo bar».baz") == "`Nat.«foo bar».baz"
    assert inventory.lean_name_literal("Nat.foo+") == "`Nat.«foo+»"


def test_ordered_roots_is_deterministic_and_weighted() -> None:
    rows = [{"name": f"Nat.t{i}", "module": "Mathlib.Data.Nat.X"} for i in range(6)] + [
        {"name": f"G.t{i}", "module": "Mathlib.Order.Y"} for i in range(6)
    ]
    pools = [
        inventory.Pool("nat_int", ("Mathlib.Data.Nat",), 3),
        inventory.Pool("general", (), 1),
    ]
    first = inventory.ordered_roots(rows, pools, order_salt="s")
    second = inventory.ordered_roots(rows, pools, order_salt="s")
    assert first == second
    assert [pool for _, pool in first[:4]] == ["nat_int", "nat_int", "nat_int", "general"]
    assert len(first) == 12 and len({name for name, _ in first}) == 12
    assert inventory.ordered_roots(rows, pools, order_salt="other") != first


def test_journal_and_cache_round_trip(tmp_path: Path) -> None:
    journal = store.Journal(tmp_path / "journal.jsonl")
    journal.append({"kind": "root", "root": "a"})
    journal.append_many([{"kind": "terminal", "root": "a", "operation_id": "P15"}])
    with (tmp_path / "journal.jsonl").open("ab") as handle:
        handle.write(b'{"kind": "torn"')  # interrupted append
    assert [r["kind"] for r in journal.read()] == ["root", "terminal"]
    cache = store.SemanticCache(tmp_path / "cache")
    key = store.SemanticCache.op_key(
        reference_alpha_hash="1",
        operation_id="P15",
        engine_semantic_version="v",
        lean_version="l",
        project_revision="p",
        import_options_fingerprint="f",
        name="Nat.foo",
    )
    assert cache.get_op(key) is None
    cache.put_op(key, {"status": "retained"})
    assert cache.get_op(key) == {"status": "retained"}
    cache.put_op(key, {"status": "retained"})
    with pytest.raises(store.StoreError):
        cache.put_op(key, {"status": "rejected"})
    assert key == hash_canonical(
        {
            "kind": "sprint_operation",
            "cache_schema": 2,
            "name": "Nat.foo",
            "reference_alpha_hash": "1",
            "operation_id": "P15",
            "engine_semantic_version": "v",
            "lean_version": "l",
            "project_revision": "p",
            "import_options_fingerprint": "f",
        }
    )


def test_inspection_sample_includes_every_n31_row() -> None:
    records = []
    for op in OPERATIONS:
        for index in range(8):
            records.append({"operation_id": op, "row_hash": f"{op}-{index}"})
    sample = inspection_sample(records, count=30)
    assert len(sample) == 30
    assert (
        sum(1 for item in sample if item["operation_id"] == "N31_DROP_REQUIRED_GUARD_PROOF_V1") == 8
    )
    others = [
        item["operation_id"]
        for item in sample
        if item["operation_id"] != "N31_DROP_REQUIRED_GUARD_PROOF_V1"
    ]
    assert len(set(others)) == len(OPERATIONS) - 1


def test_sprint_implementation_stays_bounded_and_uses_shared_surfaces() -> None:
    package = ROOT / "src/leanfaith/sft1/sprint"
    bounded_files = {
        ROOT / "LeanFaith/Meta/SFT1/Sprint.lean": 4_500,
        package / "runner.py": 4_000,
        package / "square.py": 7_000,
        package / "compiler_certificate_gate.py": 3_000,
        package / "compiler_inventory.py": 2_800,
        package / "compiler_replay.py": 3_000,
        package / "compiler_scale.py": 4_800,
        package / "orbit.py": 1_500,
    }
    counts = {
        path.relative_to(ROOT).as_posix(): len(path.read_text(encoding="utf-8").splitlines())
        for path in bounded_files
    }
    over_limit = {
        path.relative_to(ROOT).as_posix(): counts[path.relative_to(ROOT).as_posix()] - limit
        for path, limit in bounded_files.items()
        if counts[path.relative_to(ROOT).as_posix()] > limit
    }
    assert over_limit == {}, over_limit

    # Keep a package-wide backstop, but make the architectural boundaries above
    # and the reachability checks below the primary guard against parallel engines.
    implementation_paths = [
        ROOT / "LeanFaith/Meta/SFT1/Sprint.lean",
        *sorted(package.glob("*.py")),
    ]
    total = sum(len(path.read_text(encoding="utf-8").splitlines()) for path in implementation_paths)
    assert total <= 38_000, total

    duplicate_wave_modules = [
        path.relative_to(ROOT).as_posix()
        for path in package.rglob("*.py")
        if path.name in {"wave3.py", "wave4.py", "wave5.py"}
    ]
    assert duplicate_wave_modules == []

    square_source = (package / "square.py").read_text(encoding="utf-8")
    assert "from leanfaith.sft1.sprint.orbit import (" in square_source
    for shared_orbit_symbol in ("policy_from_config(", "cap_negative_operation_share("):
        assert shared_orbit_symbol in square_source
    assert "Wave4Runner(" in square_source and "build_wave4_view(" in square_source

    compiler_inventory_source = (package / "compiler_inventory.py").read_text(encoding="utf-8")
    compiler_replay_source = (package / "compiler_replay.py").read_text(encoding="utf-8")
    assert (
        "from leanfaith.sft1.sprint import compiler_inventory as inventory_module"
        in compiler_replay_source
    )
    assert "from leanfaith.sft1.sprint.compiler_inventory import (" in compiler_replay_source
    for shared_inventory_symbol in (
        "load_inventory_config(",
        "load_pinned_input_shards(",
        "reconstruct_source(",
    ):
        assert shared_inventory_symbol in compiler_replay_source
    for cli_source in (compiler_inventory_source, compiler_replay_source):
        assert "def main(" in cli_source
        assert 'if __name__ == "__main__":' in cli_source
        assert "argparse.ArgumentParser(" in cli_source

    direct_lake_lean = re.compile(
        r"(?:lake\s+env\s+lean|[\"']lake[\"']\s*,\s*[\"']env[\"']\s*,\s*[\"']lean[\"'])"
    )
    forbidden_launches = [
        path.relative_to(ROOT).as_posix()
        for path in sorted(package.glob("*.py"))
        if direct_lake_lean.search(path.read_text(encoding="utf-8"))
    ]
    assert forbidden_launches == []


def _synthetic_records(*, leak: bool, count: int = 240) -> list[dict[str, object]]:
    import random

    rng = random.Random(7)
    records: list[dict[str, object]] = []
    mechanisms = ["P15", "P18", "N25", "N31"]
    for index in range(count):
        mechanism = mechanisms[index % 4]
        label = mechanism.startswith("P")
        root = f"Root.t{index // 3}"
        words = [rng.choice(["a", "b", "c", "d", "e", "f"]) for _ in range(6)]
        reference = f"{words[0]} {words[1]} : ℕ\n⊢ {words[2]} + {words[3]} = {words[4]}"
        candidate = f"{words[0]} {words[1]} : ℕ\n⊢ {words[4]} = {words[2]} + {words[3]}"
        if leak and not label:
            candidate += " ∧ True"
        records.append(
            {
                "row": {
                    "reference": reference,
                    "candidate": candidate,
                    "label": label,
                    "operation_id": f"{mechanism}_X",
                },
                "sidecar": {"root_name": root, "mechanism": mechanism},
            }
        )
    return records


def test_shortcut_screen_detects_surface_leak_and_accepts_noise() -> None:
    from leanfaith.sft1.sprint import shortcut

    leaked = shortcut.run_screens(_synthetic_records(leak=True))
    assert leaked["passed"] is False
    candidate_only = next(s for s in leaked["screens"] if s["name"] == "candidate_only")
    assert candidate_only["balanced_accuracy"] > 0.9
    clean = shortcut.run_screens(_synthetic_records(leak=False))
    assert all(s["balanced_accuracy"] == s["balanced_accuracy"] for s in clean["screens"])
    assert clean["rows"] == 240 and clean["positives"] == 120


def test_ancestry_shards_never_split_a_root() -> None:
    from leanfaith.sft1.sprint.runner import ancestry_shards, group_by_ancestry

    records = []
    for root in range(7):
        for pair in range(3):
            records.append(
                {
                    "row": {"root_id": f"root:{root}"},
                    "row_hash": f"{root}-{pair}",
                    "label": True,
                    "root_name": f"r{root}",
                }
            )
    grouped = group_by_ancestry(records)
    shards = ancestry_shards(grouped, 5)
    assert sum(len(shard) for shard in shards) == 21
    for shard in shards[:-1]:
        assert len(shard) >= 5
    seen: dict[str, int] = {}
    for index, shard in enumerate(shards):
        for item in shard:
            seen.setdefault(item["row"]["root_id"], index)
            assert seen[item["row"]["root_id"]] == index


def test_canonical_surface_matches_frozen_route_and_rejects_unsupported_text() -> None:
    from leanfaith.sft1.sprint.runner import canonical_surface

    canonical, violation = canonical_surface("n : ℕ\n⊢ n + 0 = n")
    assert violation is None and canonical == "n : ℕ\n⊢ n + 0 = n"
    rejected, reason = canonical_surface("a b : ℕ\n⊢ a ||| b = b ||| a")
    assert rejected is None and reason is not None and reason.startswith("repr_surface:")


def test_balanced_view_equalizes_labels_per_root() -> None:
    from leanfaith.sft1.sprint.runner import balanced_view

    records = []
    for root, (positives, negatives) in {"a": (3, 1), "b": (2, 2), "c": (4, 0)}.items():
        for index in range(positives):
            records.append({"root_name": root, "label": True, "row_hash": f"{root}p{index}"})
        for index in range(negatives):
            records.append({"root_name": root, "label": False, "row_hash": f"{root}n{index}"})
    kept = balanced_view(records)
    assert len(kept) == 6
    for root in ("a", "b"):
        subset = [item for item in kept if item["root_name"] == root]
        assert sum(1 for item in subset if item["label"]) == sum(
            1 for item in subset if not item["label"]
        )
    assert not any(item["root_name"] == "c" for item in kept)


def _segment_record(
    *,
    root: str,
    operation: str,
    engine_sha: str,
    context: str,
    cache_schema: int,
    cache_root: Path,
    semantic_version: str = "sft1_sprint_engine_v1",
) -> dict[str, object]:
    from leanfaith.sft1.sprint.provenance import legacy_op_key, legacy_root_key

    fingerprint = "f" * 64
    project = {
        "project_id": "mathlib",
        "project_dir": "/x",
        "project_revision": "r" * 40,
        "lean_version": "v4.31.0-rc1",
        "lean_interact_version": "0.11.4",
        "repl_revision": "repl",
        "import_header": "import Mathlib",
        "options": {"Elab.async": False},
    }
    alpha = f"alpha-{root}"
    common = {
        "project_revision": project["project_revision"],
        "lean_version": project["lean_version"],
        "import_options_fingerprint": fingerprint,
        "engine_semantic_version": semantic_version,
        "name": root,
    }
    cache = store.SemanticCache(cache_root)
    root_key = (
        store.SemanticCache.root_key(**common) if cache_schema == 2 else legacy_root_key(**common)
    )
    cache.put_root(root_key, {"name": root, "reference_alpha_hash": alpha})
    op_common = {
        "reference_alpha_hash": alpha,
        "operation_id": operation,
        "engine_semantic_version": semantic_version,
        "lean_version": project["lean_version"],
        "project_revision": project["project_revision"],
        "import_options_fingerprint": fingerprint,
    }
    cache_key = (
        store.SemanticCache.op_key(**op_common, name=root)
        if cache_schema == 2
        else legacy_op_key(**op_common)
    )
    identity = {"renderer_semantic_hash": "s", "implementation_set_hash": "i"}
    return {
        "row": {"pair_id": f"pair:{root}", "root_id": f"root:{root}", "label": True},
        "sidecar": {
            "root_name": root,
            "operation_id": operation,
            "engine": {
                "source_sha256": engine_sha,
                "compile_context_id": context,
                "semantic_version": semantic_version,
                "import_options_fingerprint": fingerprint,
            },
            "project": project,
            "cache_key": cache_key,
            "repr": {
                "reference": {"implementation_identity": identity, "spec_hash": "spec"},
                "candidate": {"implementation_identity": identity, "spec_hash": "spec"},
            },
        },
        "row_hash": f"h-{root}",
        "label": True,
    }


def test_provenance_records_multiple_engine_segments_and_cache_schemas(tmp_path: Path) -> None:
    from leanfaith.sft1.sprint.provenance import derive_provenance, engine_commit_map

    commit_map = engine_commit_map(ROOT)
    assert len(commit_map) >= 2
    known = list(commit_map)
    older, current = known[0], known[-1]
    cache_root = tmp_path / "cache"
    records = [
        _segment_record(
            root="Nat.a",
            operation="P18_SYMMETRIZE_EQUALITY_V1",
            engine_sha=older,
            context="ctx:one",
            cache_schema=1,
            cache_root=cache_root,
        ),
        _segment_record(
            root="Nat.b",
            operation="P18_SYMMETRIZE_EQUALITY_V1",
            engine_sha=current,
            context="ctx:two",
            cache_schema=1,
            cache_root=cache_root,
        ),
        _segment_record(
            root="Nat.c",
            operation="N31_DROP_REQUIRED_GUARD_PROOF_V1",
            engine_sha=current,
            context="ctx:two",
            cache_schema=2,
            cache_root=cache_root,
        ),
    ]
    provenance = derive_provenance(records, repo_root=ROOT, cache_root=cache_root)
    assert provenance["consistent"], provenance["issues"]
    assert provenance["engine_source_sha256_set"] == sorted({older, current})
    assert provenance["cache_schemas"] == [1, 2]
    segments = {
        (s["engine_source_sha256"], s["cache_schema"]): s["rows"] for s in provenance["segments"]
    }
    assert segments == {(older, 1): 1, (current, 1): 1, (current, 2): 1}
    assert all(segment["engine_commits"] for segment in provenance["segments"])


def test_provenance_flags_mixed_semantic_versions(tmp_path: Path) -> None:
    from leanfaith.sft1.sprint.provenance import derive_provenance, engine_commit_map

    cache_root = tmp_path / "cache"
    current = list(engine_commit_map(ROOT))[-1]
    records = [
        _segment_record(
            root="Nat.a",
            operation="P18_SYMMETRIZE_EQUALITY_V1",
            engine_sha=current,
            context="ctx:two",
            cache_schema=2,
            cache_root=cache_root,
        ),
        _segment_record(
            root="Nat.b",
            operation="P18_SYMMETRIZE_EQUALITY_V1",
            engine_sha=current,
            context="ctx:two",
            cache_schema=2,
            cache_root=cache_root,
            semantic_version="sft1_sprint_engine_v9",
        ),
    ]
    provenance = derive_provenance(records, repo_root=ROOT, cache_root=cache_root)
    assert provenance["consistent"] is False
    assert any("semantic versions" in issue for issue in provenance["issues"])


def _view_record(
    root: str, operation: str, *, detail: str = "", index: int = 0
) -> dict[str, object]:
    label = operation.startswith("P")
    return {
        "row": {
            "pair_id": f"pair:{root}:{operation}:{index}",
            "root_id": f"root:{root}",
            "reference": f"⊢ ref {root}",
            "candidate": f"⊢ cand {root} {operation} {index}",
            "label": label,
            "operation_id": operation,
        },
        "sidecar": {"site": {"detail": detail}, "evidence": {}},
        "row_hash": hash_canonical([root, operation, index]),
        "unordered_pair_key": hash_canonical([root, operation, index, "k"]),
        "label": label,
        "operation_id": operation,
        "root_name": root,
        "mechanism": operation.split("_", 1)[0],
    }


def test_core_v2_matches_relation_cells_caps_n31_and_stores_orientation() -> None:
    from leanfaith.sft1.sprint.views import build_core, cell_of

    records: list[dict[str, object]] = []
    for index in range(6):  # six equality roots with both cells
        records.append(_view_record(f"eq{index}", "P18_SYMMETRIZE_EQUALITY_V1"))
        records.append(_view_record(f"eq{index}", "N25_TOGGLE_EQ_NE_PROOF_V1", detail="eq_to_ne"))
    for index in range(3):  # three disequality roots with both cells
        records.append(_view_record(f"ne{index}", "P_NE_SYMMETRIZE_V1"))
        records.append(_view_record(f"ne{index}", "N25_TOGGLE_EQ_NE_PROOF_V1", detail="ne_to_eq"))
    records.append(_view_record("lt0", "N32_SWAP_ROLE_ORDER_PROOF_V1"))
    records.append(_view_record("lt0", "P14_SWAP_INDEPENDENT_DATA_BINDERS_V1"))
    for index in range(5):  # guard roots: only a fraction may enter the core
        records.append(_view_record(f"g{index}", "N31_DROP_REQUIRED_GUARD_PROOF_V1"))
        records.append(_view_record(f"g{index}", "P_DROP_REDUNDANT_GUARD_PROOF_V1"))
    assert cell_of(records[0]) == "eq_pos" and cell_of(records[1]) == "eq_neg"
    core, report = build_core(records, n31_cap_fraction=0.2)
    assert report["matched_roots_per_relation"] == 3
    assert report["order_pairs"] == 1
    assert report["n31_cap_rows"] == int(0.2 * 14)
    assert report["guard_pairs"] == report["n31_cap_rows"]
    positives = sum(1 for item in core if item["label"])
    assert positives == len(core) - positives
    families = {item["sidecar"]["core_family"] for item in core}
    assert families == {"eq_relation", "ne_relation", "order", "guard"}
    swapped = [item for item in core if item["sidecar"]["orientation"] == "swapped"]
    assert swapped, "orientation randomization must swap some stored rows"
    for item in swapped:
        assert item["row"]["reference"].startswith("⊢ cand")
    unswapped = [item for item in core if item["sidecar"]["orientation"] == "original"]
    assert all(item["row"]["reference"].startswith("⊢ ref") for item in unswapped)


def test_run_screens_v2_reports_polarity_paired_families() -> None:
    from leanfaith.sft1.sprint import shortcut

    records = []
    import random

    rng = random.Random(3)
    for index in range(160):
        family = ["eq_relation", "ne_relation"][index % 2]
        label = index % 4 < 2
        words = " ".join(rng.choice("abcdef") for _ in range(5))
        records.append(
            {
                "row": {
                    "reference": f"x : ℕ\n⊢ {words} = 0",
                    "candidate": f"x : ℕ\n⊢ 0 = {words}",
                    "label": label,
                    "root_id": f"root:{index // 2}",
                },
                "sidecar": {"core_family": family, "orientation": "original"},
            }
        )
    result = shortcut.run_screens_v2(records)
    assert result["feature_mode"].startswith("side_tagged")
    assert set(result["families"]) == {"eq_relation", "ne_relation"}
    assert [s["name"] for s in result["screens"]] == [
        "candidate_only",
        "reference_only",
        "family_held_out",
    ]


def test_cache_accepts_volatile_proof_fingerprint_differences(tmp_path: Path) -> None:
    cache = store.SemanticCache(tmp_path / "cache")
    base = {
        "status": "retained",
        "evidence": {"refutation": {"check": {"proof_expr_hash_u64": "1", "kernel_checked": True}}},
        "engine": {"source_sha256": "a"},
        "render": None,
    }
    cache.put_op("k" * 64, base)
    changed = json.loads(json.dumps(base))
    changed["evidence"]["refutation"]["check"]["proof_expr_hash_u64"] = "2"
    cache.put_op("k" * 64, changed)  # fingerprint drift is tolerated
    changed["evidence"]["refutation"]["check"]["kernel_checked"] = False
    with pytest.raises(store.StoreError):
        cache.put_op("k" * 64, changed)


def _seed_core_records(count: int = 40) -> list[dict[str, object]]:
    """Paired core records: one positive and one negative per root."""

    import random

    rng = random.Random(11)
    records: list[dict[str, object]] = []
    families = ["eq_relation", "ne_relation", "order", "guard"]
    for index in range(count):
        root = f"root:{index}"
        family = families[index % 4]
        words = " ".join(rng.choice("abcdefgh") for _ in range(6))
        for label, operation in (
            (True, "P18_SYMMETRIZE_EQUALITY_V1"),
            (False, "N25_TOGGLE_EQ_NE_PROOF_V1" if index % 2 else "N32_SWAP_ROLE_ORDER_PROOF_V1"),
        ):
            pair_id = f"pair:{index}:{int(label)}"
            check = {"meta_checked": True, "kernel_checked": True}
            evidence = (
                {
                    "equivalence_proof": {"check": check},
                    "candidate_truth": "proved_equivalent_to_reference",
                }
                if label
                else {
                    "refutation": {"check": check},
                    "source_proof_check": check,
                    "candidate_truth": "refuted",
                }
            )
            records.append(
                {
                    "row": {
                        "pair_id": pair_id,
                        "root_id": root,
                        "reference": f"x : ℕ\n⊢ {words} = 0",
                        "candidate": f"x : ℕ\n⊢ 0 {'=' if label else '≠'} {words}",
                        "label": label,
                        "operation_id": operation,
                    },
                    "sidecar": {
                        "pair_id": pair_id,
                        "root_id": root,
                        "root_name": f"Root.t{index}",
                        "operation_id": operation,
                        "evidence": evidence,
                        "core_family": family,
                        "core_cell": "cell",
                        "orientation": "original",
                        "engine": {
                            "source_sha256": "e" * 64,
                            "compile_context_id": "ctx:test",
                            "semantic_version": "sft1_sprint_engine_v1",
                            "import_options_fingerprint": "f" * 64,
                        },
                        "project": {
                            "project_id": "mathlib",
                            "project_revision": "r" * 40,
                            "lean_version": "v4.31.0-rc1",
                        },
                        "repr": {
                            "reference": {
                                "implementation_identity": {"renderer_semantic_hash": "s"},
                                "spec_hash": "spec",
                                "goal_v1": f"x : ℕ\n⊢ {words} = 0",
                                "rendered_goal_hash": "",
                                "provenance": {"expr_hash": "a"},
                            },
                            "candidate": {
                                "implementation_identity": {"renderer_semantic_hash": "s"},
                                "spec_hash": "spec",
                                "goal_v1": f"x : ℕ\n⊢ 0 {'=' if label else '≠'} {words}",
                                "rendered_goal_hash": "",
                                "provenance": {"expr_hash": "b"},
                            },
                        },
                        "cache_key": "",
                    },
                    "row_hash": hash_canonical([pair_id]),
                    "unordered_pair_key": hash_canonical([pair_id, "u"]),
                    "label": label,
                    "operation_id": operation,
                    "root_name": f"Root.t{index}",
                }
            )
    return records


def test_mechanism_map_is_exact() -> None:
    from leanfaith.sft1.sprint.engine import mechanism_of

    assert mechanism_of("P_NE_SYMMETRIZE_V1") == "PNE"
    assert mechanism_of("P_DROP_REDUNDANT_GUARD_PROOF_V1") == "PDRG"
    assert mechanism_of("N25_TOGGLE_EQ_NE_PROOF_V1") == "N25"
    with pytest.raises(engine.SprintEngineError):
        mechanism_of("P_UNKNOWN")


def test_seed_records_store_exactly_one_swap_per_root_and_three_field_rows() -> None:
    from leanfaith.sft1.sprint.seed import seed_records

    seeds = seed_records(_seed_core_records())
    assert len(seeds) == 80
    assert all(set(item["row"]) == {"reference", "candidate", "label"} for item in seeds)
    swapped = [item for item in seeds if item["sidecar"]["orientation"] == "swapped"]
    assert len(swapped) == 40
    per_root: dict[str, int] = {}
    for item in seeds:
        root = item["sidecar"]["root_id"]
        per_root[root] = per_root.get(root, 0) + (
            1 if item["sidecar"]["orientation"] == "swapped" else 0
        )
    assert set(per_root.values()) == {1}
    for item in swapped:
        assert item["row"]["reference"].startswith("x : ℕ\n⊢ 0")
        assert item["sidecar"]["stored_reference_is"] == "candidate"
    assert all(item["sidecar"]["mechanism"] in {"P18", "N25", "N32"} for item in seeds)
    assert all("pair_id" in item["sidecar"] and "root_id" in item["sidecar"] for item in seeds)


def test_screens_v3_are_order_invariant_under_shuffles() -> None:
    import random

    from leanfaith.sft1.sprint import shortcut
    from leanfaith.sft1.sprint.seed import seed_records

    seeds = seed_records(_seed_core_records())
    baseline = shortcut.run_screens_v3(seeds)
    for seed in (1, 7, 42):
        shuffled = list(seeds)
        random.Random(seed).shuffle(shuffled)
        assert shortcut.run_screens_v3(shuffled) == baseline
    assert baseline["method"]["order_invariant"] is True
    assert set(baseline["per_family"]) == {"candidate_only", "reference_only", "family_held_out"}
    assert set(baseline["per_family"]["candidate_only"]) == {
        "eq_relation",
        "ne_relation",
        "order",
        "guard",
    }


def test_screens_v3_serialize_undefined_single_label_family_metrics_as_null() -> None:
    from leanfaith.sft1.sprint import shortcut
    from leanfaith.sft1.sprint.seed import seed_records

    records = json.loads(json.dumps(seed_records(_seed_core_records())))
    family = str(records[0]["sidecar"]["core_family"])
    for record in records:
        if record["sidecar"]["core_family"] == family:
            record["row"]["label"] = True
            record["sidecar"]["label"] = True

    report = shortcut.run_screens_v3(records)

    assert report["per_family"]["candidate_only"][family] is None
    assert report["per_family"]["reference_only"][family] is None
    assert report["per_family"]["family_held_out"][family] is None
    assert b'"candidate_only"' in canonical_json_bytes(report)


def test_diversity_floor_is_proportional() -> None:
    from leanfaith.sft1.sprint.seed import diversity_floor

    assert diversity_floor(994) == 50
    assert diversity_floor(10000) == 100
    assert diversity_floor(30) == 2


def test_validator_compares_full_provenance_and_model_facing_rows(tmp_path: Path) -> None:
    from leanfaith.sft1.sprint import integrity

    # A shard with three-field rows and metadata sidecars, plus a manifest whose
    # provenance object differs from the sidecar-derived one in a non-segment field.
    seeds = _seed_core_records(4)
    compacted = tmp_path / "view"
    shard = compacted / "shard-0001"
    shard.mkdir(parents=True)
    rows = []
    sidecars = []
    for item in seeds:
        row = dict(item["row"])
        sidecar = dict(item["sidecar"])
        sidecar["mechanism"] = "WRONG"
        rows.append(
            {"reference": row["reference"], "candidate": row["candidate"], "label": row["label"]}
        )
        sidecars.append(sidecar)
    from leanfaith.config.hashing import canonical_json_bytes, sha256_hex

    rows_bytes = b"".join(canonical_json_bytes(r) + b"\n" for r in rows)
    sidecar_bytes = b"".join(canonical_json_bytes(s) + b"\n" for s in sidecars)
    (shard / "rows.jsonl").write_bytes(rows_bytes)
    (shard / "sidecars.jsonl").write_bytes(sidecar_bytes)
    (shard / "manifest.json").write_bytes(
        canonical_json_bytes(
            {
                "row_count": len(rows),
                "rows_sha256": sha256_hex(rows_bytes),
                "sidecars_sha256": sha256_hex(sidecar_bytes),
                "complete": False,
            }
        )
    )
    manifest = {
        "retained_rows": len(rows),
        "input_records": len(rows),
        "provenance": {"schema_version": 1, "segments": [], "row_count": 0},
        "finalized": True,
    }
    (compacted / "manifest.json").write_bytes(canonical_json_bytes(manifest))
    retained = tmp_path / "retained.jsonl"
    retained.write_bytes(b"".join(canonical_json_bytes(item) + b"\n" for item in seeds))
    report = integrity.validate_view(
        repo_root=ROOT,
        staging_root=tmp_path,
        run_id="x",
        compacted_dir=compacted,
        retained_path=retained,
    )
    counts = report["issue_counts"]
    assert counts.get("mechanism_metadata") == len(rows)
    assert counts.get("manifest_provenance") == 1
    assert counts.get("finalized_shard_incomplete") == 1
    assert "row_schema" not in counts


def _square_payload(name: str, direction: str = "eq_to_ne") -> dict[str, object]:
    check = {"meta_checked": True, "kernel_checked": True, "proof_expr_hash_u64": "1"}
    words = name.replace(".", " ")
    return {
        "status": "retained",
        "reason": "",
        "direction": direction,
        "module": "Mathlib.Test",
        "level_params": [],
        "alpha": {"p": "1", "c": "2", "p_prime": "3", "c_prime": "4"},
        "goals": {
            "p": f"x : ℕ\n⊢ {words} = 0",
            "c": f"x : ℕ\n⊢ {words} ≠ 0",
            "p_prime": f"x : ℕ\n⊢ 0 = {words}",
            "c_prime": f"x : ℕ\n⊢ 0 ≠ {words}",
        },
        "evidence": {
            "direction": direction,
            "t_p": "P18_SYMMETRIZE_EQUALITY_V1",
            "t_c": "P_NE_SYMMETRIZE_V1",
            "diamond": {"expr_equal": True, "direction_equal": True},
            "p_iff_p_prime": check,
            "p_prime_iff_p": check,
            "c_iff_c_prime": check,
            "source_proof": {"kind": "loaded_environment_constant", "constant": name},
            "source_proof_check": check,
            "c_refutation": {"kind": "source_proof_contradiction", "check": check},
            "p_prime_transported_proof": check,
            "c_prime_refutation": check,
            "not_iff_c_p": check,
            "not_iff_p_prime_c_prime": check,
            "universe_instantiation": "none",
        },
        "elapsed_ms": 1,
    }


def _square_render(payload: dict[str, object]) -> dict[str, object]:
    from leanfaith.config.hashing import sha256_hex

    goals = payload["goals"]
    assert isinstance(goals, dict)
    render: dict[str, object] = {}
    for endpoint, text in goals.items():
        render[endpoint] = {
            "record": {
                "goal_v1": text,
                "rendered_goal_hash": sha256_hex(str(text).encode("utf-8")),
                "provenance": {"expr_hash": hash_canonical([endpoint, text])},
                "spec_hash": "spec",
                "implementation_identity": {"renderer_semantic_hash": "s"},
            },
            "source_material": {"kind": "constructed_expr_no_source_text"},
        }
    render["request_hash"] = "r" * 64
    return render


def test_square_rows_have_identical_marginals_and_four_kinds() -> None:
    from leanfaith.sft1.sprint import square

    runner = square.SquareRunner.__new__(square.SquareRunner)
    runner.operation_id = square.SQUARE_OPERATION
    runner.cache_schema = 3
    runner.recovered_roots = []
    runner.base = type("Base", (), {})()  # minimal stand-in for identity fields
    runner.base.root_id = lambda name: f"root:{name}"
    runner.base.pins = type(
        "Pins",
        (),
        {
            "to_dict": lambda self: {"project_id": "mathlib"},
            "lean_version": "v4.31.0-rc1",
            "project_revision": "r" * 40,
        },
    )()
    runner.base.identity = type(
        "Identity",
        (),
        {
            "to_dict": lambda self: {"source_sha256": "e"},
            "source_sha256": "e",
            "semantic_version": "sft1_sprint_engine_v1",
            "import_options_fingerprint": "f" * 64,
        },
    )()
    runner.base.implementation_commit = "c" * 40
    runner.base.context = type("Context", (), {"compile_context_id": "ctx:test"})()
    runner.statements = {}
    positives_ref: list[str] = []
    negatives_ref: list[str] = []
    positives_cand: list[str] = []
    negatives_cand: list[str] = []
    all_rows = []
    for name in ("Nat.a", "Nat.b", "Int.c"):
        payload = _square_payload(name)
        record = {
            "render": _square_render(payload),
            "evidence": payload["evidence"],
            "direction": payload["direction"],
            "alpha": payload["alpha"],
            "module": "Mathlib.Test",
            "level_params": [],
            "process_request_hash": "p" * 64,
        }
        rows = runner.build_rows(name, record, {"source_run": "tenk", "source_pair_id": "pair:x"})
        assert [item["sidecar"]["row_kind"] for item in rows] == [k for k, *_ in square.ROW_KINDS]
        assert [item["label"] for item in rows] == [True, True, False, False]
        assert all(set(item["row"]) == {"reference", "candidate", "label"} for item in rows)
        assert len({item["sidecar"]["pair_id"] for item in rows}) == 4
        assert all(item["sidecar"]["group_id"] == f"root:{name}" for item in rows)
        for item in rows:
            (positives_ref if item["label"] else negatives_ref).append(item["row"]["reference"])
            (positives_cand if item["label"] else negatives_cand).append(item["row"]["candidate"])
        all_rows.extend(rows)
    assert sorted(positives_ref) == sorted(negatives_ref)
    assert sorted(positives_cand) == sorted(negatives_cand)
    assert len({item["unordered_pair_key"] for item in all_rows}) == len(all_rows)
    negatives = [item for item in all_rows if not item["label"]]
    assert all(
        item["sidecar"]["evidence"]["refutation"]["goal"] == "Not (Iff reference candidate)"
        for item in negatives
    )


def test_square_process_and_render_bodies() -> None:
    from leanfaith.sft1.sprint import square

    body = square.process_body(["Nat.a", "Nat.b'"])
    assert body.startswith("run_meta do") and '"Nat.b\'"' in body
    render = square.render_body(["Nat.a", "Nat.b"], "scope:square")
    assert render.count("LeanFaith.GoalV1.emitClosedProp") == 8
    assert "(squares[1]!).cPrime" in render
    inputs = square.render_inputs(["Nat.a"], {"Nat.a": "theorem a : 1 = 1"})
    assert [item.endpoint_id for item in inputs] == ["0.p", "0.c", "0.p_prime", "0.c_prime"]
    assert {item.endpoint_role for item in inputs} == {"reference", "candidate"}


def test_permutation_control_is_reproducible() -> None:
    from leanfaith.sft1.sprint import shortcut
    from leanfaith.sft1.sprint.seed import seed_records

    seeds = seed_records(_seed_core_records())
    first = shortcut.permutation_control(seeds, seeds=(1,))
    second = shortcut.permutation_control(list(reversed(seeds)), seeds=(1,))
    assert first == second
    assert first["actual"]["candidate_only"][0] == second["actual"]["candidate_only"][0]
    assert "seed_1" in first["per_seed"]


def test_cacheable_status_is_an_explicit_whitelist() -> None:
    """Only whitelisted deterministic terminals may enter or leave the semantic cache."""
    for status in ("ok", "retained", "rejected", "not_applicable"):
        assert engine.cacheable_status(status)
    for status in ("error", "", "failed", "unknown", None, 3):
        assert not engine.cacheable_status(status)


def test_square_inspection_lists_every_row_grouped_by_root() -> None:
    from leanfaith.sft1.sprint import square

    def rec(root: str, kind: str, label: bool, ref: str, cand: str) -> dict[str, object]:
        return {
            "row": {"reference": ref, "candidate": cand, "label": label},
            "sidecar": {
                "root_name": root,
                "module": "M",
                "statement": f"theorem {root}",
                "row_kind": kind,
                "square": {"direction": "eq_to_ne", "t_p": "P18", "t_c": "P_NE"},
                "evidence": {
                    "square": {
                        "p_iff_p_prime": {"metaChecked": True, "kernelChecked": True},
                        "c_refutation": {
                            "kind": "source_proof_contradiction",
                            "check": {"metaChecked": True, "kernelChecked": False},
                        },
                        "direction": "eq_to_ne",
                    }
                },
            },
        }

    records = [
        rec("b", "not_iff_c_p", False, "c", "p"),
        rec("a", "p_prime_iff_p", True, "p'", "p"),
        rec("b", "p_prime_iff_p", True, "p'", "p"),
        rec("a", "not_iff_p_prime_c_prime", False, "p'", "c'"),
    ]
    lines = square.square_inspection_lines(records)
    text = "\n".join(lines)
    assert text.index("## a") < text.index("## b")
    assert lines.count("### p_prime_iff_p (label True)") == 2
    assert "p_iff_p_prime:MK" in text and "c_refutation:FAIL(source_proof_contradiction)" in text
    a_block = text[text.index("## a") : text.index("## b")]
    assert a_block.index("p_prime_iff_p") < a_block.index("not_iff_p_prime_c_prime")


def test_square_rows_attribute_generating_engine() -> None:
    """Cache-served roots keep the engine identity and commit that generated their certificates."""
    from leanfaith.sft1.sprint import square

    runner = square.SquareRunner.__new__(square.SquareRunner)
    runner.operation_id = square.SQUARE_OPERATION
    runner.cache_schema = 3
    runner.recovered_roots = []
    runner.base = type("Base", (), {})()
    runner.base.root_id = lambda name: f"root:{name}"
    runner.base.pins = type(
        "Pins",
        (),
        {
            "to_dict": lambda self: {"project_id": "mathlib"},
            "lean_version": "v4.31.0-rc1",
            "project_revision": "r" * 40,
        },
    )()
    runner.base.identity = type(
        "Identity",
        (),
        {
            "to_dict": lambda self: {"source_sha256": "current"},
            "source_sha256": "current",
            "semantic_version": "sft1_sprint_engine_v1",
            "import_options_fingerprint": "f" * 64,
        },
    )()
    runner.base.implementation_commit = "c" * 40
    runner.base.context = type("Context", (), {"compile_context_id": "ctx:test"})()
    runner.statements = {}
    payload = _square_payload("Nat.a")
    base_record = {
        "render": _square_render(payload),
        "evidence": payload["evidence"],
        "direction": payload["direction"],
        "alpha": payload["alpha"],
        "module": "Mathlib.Test",
        "level_params": [],
        "process_request_hash": "p" * 64,
    }
    cached = {**base_record, "engine": {"source_sha256": "old"}, "implementation_commit": "o" * 40}
    rows = runner.build_rows("Nat.a", cached, {"source_run": "tenk", "source_pair_id": "pair:x"})
    assert {row["sidecar"]["engine"]["source_sha256"] for row in rows} == {"old"}
    assert {row["sidecar"]["implementation_commit"] for row in rows} == {"o" * 40}
    fresh = runner.build_rows("Nat.a", base_record, {"source_run": "tenk"})
    assert {row["sidecar"]["engine"]["source_sha256"] for row in fresh} == {"current"}
    # a record without a commit never gets one fabricated from the current checkout
    assert {row["sidecar"]["implementation_commit"] for row in fresh} == {None}
    assert {row["sidecar"]["implementation_commit_source"] for row in fresh} == {"cache_record"}
    resolved = {
        **base_record,
        "implementation_commit": "g" * 40,
        "implementation_commit_source": "generating_run_manifest:square_20",
    }
    rows = runner.build_rows("Nat.a", resolved, {"source_run": "tenk"})
    assert {row["sidecar"]["implementation_commit"] for row in rows} == {"g" * 40}
    assert {row["sidecar"]["implementation_commit_source"] for row in rows} == {
        "generating_run_manifest:square_20"
    }


def test_select_squares_drops_duplicate_squares_whole_and_conserves_rows() -> None:
    import random

    from leanfaith.sft1.sprint import square

    kinds = [kind for kind, *_rest in square.ROW_KINDS]

    def rows(root: str, p: str, c: str, p2: str, c2: str) -> list[dict[str, object]]:
        pairs = {
            "p_prime_iff_p": (p2, p),
            "c_iff_c_prime": (c, c2),
            "not_iff_c_p": (c, p),
            "not_iff_p_prime_c_prime": (p2, c2),
        }
        return [
            {
                "sidecar": {"root_id": root, "row_kind": kind, "root_name": root},
                "unordered_pair_key": "|".join(sorted(pairs[kind])),
                "label": kind in {"p_prime_iff_p", "c_iff_c_prime"},
                "row_hash": f"{root}:{kind}",
            }
            for kind in kinds
        ]

    a = rows("root:a", "x=y", "x≠y", "y=x", "y≠x")
    b = rows("root:b", "y=x", "y≠x", "x=y", "x≠y")  # the same square seen from the other corner
    c = rows("root:c", "u=v", "u≠v", "v=u", "v≠u")
    op = square.SQUARE_OPERATION
    selection = square.select_squares(a + b + c, ())
    assert len(selection.kept) == 8 and selection.degenerate_roots == []
    assert len(selection.duplicate_squares) == 1
    dropped = selection.duplicate_squares[0]
    assert {dropped["root_id"], dropped["duplicate_of_root_id"]} == {"root:a", "root:b"}
    assert dropped["operation_id"] == op and dropped["square"] == f"{dropped['root_id']}|{op}"
    assert set(selection.accepted_roots) == {f"root:c|{op}", dropped["duplicate_of"]}
    assert [r["sidecar"]["row_kind"] for r in selection.kept[:4]] == kinds
    shuffled = a + b + c
    random.Random(3).shuffle(shuffled)
    again = square.select_squares(shuffled, ())
    assert [r["row_hash"] for r in again.kept] == [r["row_hash"] for r in selection.kept]
    conflicted = square.select_squares(a + c, (a[0]["unordered_pair_key"],))
    assert conflicted.degenerate_roots == [f"root:a|{op}"] and conflicted.conflict_rows == 1
    assert [r["sidecar"]["root_id"] for r in conflicted.kept] == ["root:c"] * 4
    partial = square.select_squares(a[:3] + c, ())
    assert partial.degenerate_roots == [f"root:a|{op}"] and len(partial.kept) == 4
    # the same root under two square operations forms two distinct squares
    other = [{**r, "sidecar": {**r["sidecar"], "operation_id": "SQUARE_N25_BINDER_V1"}} for r in c]
    for r in other:
        r["unordered_pair_key"] = r["unordered_pair_key"] + "'"
    both = square.select_squares(c + other, ())
    assert len(both.kept) == 8 and both.duplicate_squares == []


def test_validator_pair_id_reads_row_then_sidecar() -> None:
    from leanfaith.sft1.sprint import integrity

    five_field = {"row": {"pair_id": "pair:row"}, "sidecar": {"pair_id": "pair:side"}}
    three_field = {
        "row": {"reference": "a", "candidate": "b", "label": True},
        "sidecar": {"pair_id": "pair:side"},
    }
    assert integrity._pair_id(five_field) == "pair:row"
    assert integrity._pair_id(three_field) == "pair:side"


def _square_runner_stub(source_sha256: str = "e") -> object:
    from leanfaith.sft1.sprint import square

    runner = square.SquareRunner.__new__(square.SquareRunner)
    runner.operation_id = square.SQUARE_OPERATION
    runner.cache_schema = 3
    runner.recovered_roots = []
    runner.base = type("Base", (), {})()
    runner.base.root_id = lambda name: f"root:{name}"
    runner.base.pins = type(
        "Pins",
        (),
        {
            "to_dict": lambda self: {"project_id": "mathlib"},
            "lean_version": "v4.31.0-rc1",
            "project_revision": "r" * 40,
        },
    )()
    runner.base.identity = type(
        "Identity",
        (),
        {
            "to_dict": lambda self: {
                "source_sha256": self.source_sha256,
                "semantic_version": self.semantic_version,
                "import_options_fingerprint": self.import_options_fingerprint,
            },
            "source_sha256": source_sha256,
            "semantic_version": "sft1_sprint_engine_v1",
            "import_options_fingerprint": "f" * 64,
        },
    )()
    runner.base.implementation_commit = "c" * 40
    runner.base.context = type("Context", (), {"compile_context_id": "ctx:test"})()
    runner.cache_schema = 3
    runner.statements = {}
    runner.recovered_roots = []
    return runner


def test_square_truths_and_cache_identity_for_every_row_kind() -> None:
    from leanfaith.sft1.sprint import integrity

    runner = _square_runner_stub()
    payload = _square_payload("Nat.a")
    record = {
        "render": _square_render(payload),
        "evidence": payload["evidence"],
        "direction": payload["direction"],
        "alpha": payload["alpha"],
        "module": "Mathlib.Test",
        "level_params": [],
        "process_request_hash": "p" * 64,
    }
    rows = runner.build_rows(
        "Nat.a", record, {"source_run": "tenk"}, reconciliation={"matches": True}
    )
    assert len(rows) == 4
    for item in rows:
        sidecar = item["sidecar"]
        kind = sidecar["row_kind"]
        expected = integrity.SQUARE_ROW_TRUTHS[kind]
        assert (sidecar["reference_truth"], sidecar["candidate_truth"]) == expected
        evidence = sidecar["evidence"]
        assert (evidence["reference_truth"], evidence["candidate_truth"]) == expected
        if item["label"]:
            assert evidence["reference_truth"] == evidence["candidate_truth"]
        else:
            assert evidence["reference_truth"] != evidence["candidate_truth"]
            assert evidence["refutation"]["goal"] == "Not (Iff reference candidate)"
        assert integrity._check_evidence(sidecar, "SQUARE_N25_SYMMETRY_V1") is None
        swapped = dict(sidecar)
        swapped["reference_truth"], swapped["candidate_truth"] = expected[1], expected[0]
        if expected[0] != expected[1]:
            assert integrity._check_evidence(swapped, "SQUARE_N25_SYMMETRY_V1") is not None
        cache = sidecar["cache"]
        key = runner.square_root_key("Nat.a")
        assert cache["kind"] == "square_root" and cache["schema"] == 3 and cache["revision"] == 0
        assert cache["key"] == key and cache["path"] == f"roots/{key[:2]}/{key}.json"
        assert cache["content_sha256"] == hash_canonical(record) and cache["snapshot"] is None
        assert "cache_key" not in sidecar
        assert sidecar["square"]["alpha"] == payload["alpha"]
        assert sidecar["square"]["alpha_reconciliation"] == {"matches": True}
    kinds = {item["sidecar"]["row_kind"]: item for item in rows}
    assert kinds["not_iff_c_p"]["sidecar"]["reference_truth"] == "refuted"
    assert kinds["not_iff_c_p"]["sidecar"]["candidate_truth"] == "proved"
    assert kinds["not_iff_p_prime_c_prime"]["sidecar"]["reference_truth"] == "proved"
    assert kinds["not_iff_p_prime_c_prime"]["sidecar"]["candidate_truth"] == "refuted"


def test_reconcile_square_alpha_against_stored_render_response(tmp_path: Path) -> None:
    import json

    from leanfaith.sft1.sprint import square

    request_hash = "a" * 64
    entries = [
        {"index": 0, "p": "1", "c": "2", "p_prime": "3", "c_prime": "4"},
        {"index": 1, "p": "5", "c": "6", "p_prime": "7", "c_prime": "8"},
    ]
    raw = {
        "request_hash": request_hash,
        "request": {
            "code": 'run_meta do\n  let squares ← LeanFaith.SFT1.Sprint.rebuildSquares #["Nat.a", "Nat.b"]\n  emit'
        },
        "response": {
            "messages": [
                {
                    "data": "LFSFT1SPRINTJSON "
                    + json.dumps({"kind": "square_rebuild", "squares": entries})
                    + "\n"
                }
            ]
        },
    }
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / f"{request_hash}.abcd1234.json").write_text(json.dumps(raw), encoding="utf-8")
    record = {
        "root": "Nat.b",
        "alpha": {"p": "5", "c": "6", "p_prime": "7", "c_prime": "8"},
        "render": {"request_hash": request_hash, "p": {"record": {"endpoint_id": "1.p"}}},
    }
    result = square.reconcile_square_alpha(record, raw_dir)
    assert result["matches"] and result["chunk_index"] == 1 and result["raw_files"] == 1
    assert result["rebuild"] == record["alpha"]
    wrong = {**record, "alpha": {**record["alpha"], "c": "x"}}
    result = square.reconcile_square_alpha(wrong, raw_dir)
    assert not result["matches"] and result["reason"] == "alpha_mismatch:c"
    misnamed = {**record, "root": "Nat.c"}
    assert square.reconcile_square_alpha(misnamed, raw_dir)["reason"] == "chunk_name_mismatch"
    missing = {**record, "render": {**record["render"], "request_hash": "b" * 64}}
    assert (
        square.reconcile_square_alpha(missing, raw_dir)["reason"] == "raw_render_response_missing"
    )
    # a second stored response for the same request must agree
    disagreeing = json.loads(json.dumps(raw))
    disagreeing["response"]["messages"][0]["data"] = "LFSFT1SPRINTJSON " + json.dumps(
        {"kind": "square_rebuild", "squares": [entries[0], {**entries[1], "p": "9"}]}
    )
    (raw_dir / f"{request_hash}.abcd1234.response-ffff.json").write_text(
        json.dumps(disagreeing), encoding="utf-8"
    )
    assert square.reconcile_square_alpha(record, raw_dir)["reason"] == "raw_responses_disagree"


def test_load_square_retained_ignores_orphans_and_duplicates(tmp_path: Path) -> None:
    import json

    from leanfaith.sft1.sprint import square
    from leanfaith.sft1.sprint.runner import RunPaths

    paths = RunPaths(tmp_path, "run")
    paths.run_dir.mkdir(parents=True)
    journal = [
        {"kind": "square_begin", "root": "a", "pair_ids": ["a1", "a2"]},
        {"kind": "square_terminal", "root": "a", "status": "retained", "pair_ids": ["a1", "a2"]},
        {"kind": "square_begin", "root": "b", "pair_ids": ["b1", "b2"]},
    ]
    paths.journal.write_text("".join(json.dumps(r) + "\n" for r in journal), encoding="utf-8")

    def rec(pair_id: str, root: str) -> dict[str, object]:
        return {
            "row": {"reference": "x", "candidate": "y", "label": True},
            "sidecar": {"pair_id": pair_id, "root_name": root},
        }

    rows = [rec("a1", "a"), rec("a2", "a"), rec("b1", "b"), rec("a1", "a")]
    paths.retained.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    kept = square.load_square_retained(paths)
    assert [r["sidecar"]["pair_id"] for r in kept] == ["a1", "a2"]
    assert square.terminal_pair_ids(paths.journal) == {"a": ["a1", "a2"]}


def test_verify_square_cache_record(tmp_path: Path) -> None:
    import json

    from leanfaith.config.hashing import hash_canonical
    from leanfaith.sft1.sprint import provenance

    engine_block = {
        "source_sha256": "e" * 64,
        "compile_context_id": "ctx:1",
        "semantic_version": "sft1_sprint_engine_v1",
        "import_options_fingerprint": "f" * 64,
    }
    project = {"project_revision": "r" * 40, "lean_version": "v4.31.0-rc1"}
    key = hash_canonical(
        {
            "kind": "square_root",
            "cache_schema": 2,
            "operation_id": "SQUARE_N25_SYMMETRY_V1",
            "name": "Nat.a",
            "engine_semantic_version": engine_block["semantic_version"],
            "project_revision": project["project_revision"],
            "lean_version": project["lean_version"],
            "import_options_fingerprint": engine_block["import_options_fingerprint"],
        }
    )
    alpha = {"p": "1", "c": "2", "p_prime": "3", "c_prime": "4"}
    sidecar = {
        "root_name": "Nat.a",
        "operation_id": "SQUARE_N25_SYMMETRY_V1",
        "engine": engine_block,
        "project": project,
        "lean_request_hashes": {"process": "p" * 64, "render": "q" * 64},
        "square": {"alpha": alpha},
        "implementation_commit": "c" * 40,
        "cache": {
            "kind": "square_root",
            "schema": 2,
            "key": key,
            "path": f"roots/{key[:2]}/{key}.json",
        },
    }
    record = {
        "root": "Nat.a",
        "status": "retained",
        "operation_id": "SQUARE_N25_SYMMETRY_V1",
        "engine": engine_block,
        "process_request_hash": "p" * 64,
        "render": {"request_hash": "q" * 64},
        "alpha": alpha,
        "implementation_commit": "c" * 40,
    }
    cache_root = tmp_path / "cache"
    path = cache_root / f"roots/{key[:2]}/{key}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    assert provenance.verify_square_cache(sidecar, cache_root) == (2, [], None)
    tampered = dict(record, engine={**engine_block, "source_sha256": "0" * 64})
    path.write_text(json.dumps(tampered), encoding="utf-8")
    _schema, issues, _live = provenance.verify_square_cache(sidecar, cache_root)
    assert issues == ["cache record engine source_sha256 differs"]
    path.unlink()
    assert provenance.verify_square_cache(sidecar, cache_root)[1] == ["cache record absent"]
    bad_key = dict(
        sidecar,
        cache={**sidecar["cache"], "key": "0" * 64, "path": "roots/00/" + "0" * 64 + ".json"},
    )
    assert (
        "cache key does not match the square-root identity"
        in provenance.verify_square_cache(bad_key, cache_root)[1]
    )
    # a record that predates the commit field verifies through its generating run
    path.write_text(json.dumps({**record, "implementation_commit": None}), encoding="utf-8")
    runs_root = tmp_path / "runs"
    (runs_root / "square_20").mkdir(parents=True)
    (runs_root / "square_20" / "run.json").write_text(
        json.dumps({"operation_id": "SQUARE_N25_SYMMETRY_V1", "implementation_commit": "c" * 40}),
        encoding="utf-8",
    )
    (runs_root / "square_20" / "journal.jsonl").write_text(
        json.dumps(
            {"kind": "square_terminal", "root": "Nat.a", "status": "retained", "source": "lean"}
        )
        + "\n",
        encoding="utf-8",
    )
    resolved = dict(sidecar, implementation_commit_source="generating_run_manifest:square_20")
    assert provenance.verify_square_cache(resolved, cache_root, runs_root=runs_root) == (
        2,
        [],
        None,
    )
    assert provenance.verify_square_cache(sidecar, cache_root, runs_root=runs_root)[1] == [
        "cache record lacks an implementation commit"
    ]
    other_run = dict(resolved, implementation_commit="d" * 40)
    assert provenance.verify_square_cache(other_run, cache_root, runs_root=runs_root)[1] == [
        "generating run square_20 recorded a different implementation commit"
    ]


def test_generating_run_commits_maps_lean_terminals(tmp_path: Path) -> None:
    import json

    from leanfaith.sft1.sprint import square

    runs_root = tmp_path / "runs"
    for name, commit, rows in (
        ("square_20", "a" * 40, [("Nat.x", "lean"), ("Nat.y", "cache")]),
        ("square_100", "b" * 40, [("Nat.x", "cache"), ("Nat.y", "lean")]),
        ("other_op", "c" * 40, [("Nat.z", "lean")]),
    ):
        (runs_root / name).mkdir(parents=True)
        op = "SQUARE_N25_SYMMETRY_V1" if name != "other_op" else "N25"
        (runs_root / name / "run.json").write_text(
            json.dumps({"operation_id": op, "implementation_commit": commit}), encoding="utf-8"
        )
        (runs_root / name / "journal.jsonl").write_text(
            "".join(
                json.dumps(
                    {"kind": "square_terminal", "root": r, "status": "retained", "source": s}
                )
                + "\n"
                for r, s in rows
            ),
            encoding="utf-8",
        )
    assert square.generating_run_commits(runs_root) == {
        "Nat.x": ("square_20", "a" * 40),
        "Nat.y": ("square_100", "b" * 40),
    }


def test_square_resume_validates_run_manifest(tmp_path: Path) -> None:
    from leanfaith.sft1.sprint import square
    from leanfaith.sft1.sprint.runner import RunPaths
    from leanfaith.sft1.sprint.store import SemanticCache

    runner = _square_runner_stub()
    runner.paths = RunPaths(tmp_path, "run")
    runner.paths.run_dir.mkdir(parents=True)
    runner.cache = SemanticCache(tmp_path / "cache")
    runner.loaded = type("Loaded", (), {"config_hash": "h" * 64})()
    runner.config = type("Config", (), {"sprint_id": "sft1"})()
    runner.repo_root = Path(__file__).resolve().parents[3]
    runner.run_id = "run"
    runner.roots = [{"name": "Nat.a"}, {"name": "Nat.b"}]
    runner.max_roots = None
    runner.write_run_manifest()
    runner.write_run_manifest()  # identical identity resumes silently
    runner.roots = [{"name": "Nat.a"}]
    try:
        runner.write_run_manifest()
    except square.SquareError as exc:
        assert "roots_sha256" in str(exc) and "root_count" in str(exc)
    else:
        raise AssertionError("resume with a different root list must fail")
    runner.roots = [{"name": "Nat.a"}, {"name": "Nat.b"}]
    runner.base.identity.source_sha256 = "other"
    try:
        runner.write_run_manifest()
    except square.SquareError as exc:
        assert "engine_source_sha256" in str(exc)
    else:
        raise AssertionError("resume with a different engine must fail")
    runner.write_run_manifest(replay=True)  # zero-Lean replay ignores the engine text
    runner.max_roots = 100  # a forced resume may widen max_roots
    runner.base.identity.source_sha256 = "e"
    runner.write_run_manifest()


def test_square_card_states_direct_not_iff_evidence_and_supersession() -> None:
    from leanfaith.sft1.sprint.publish import dataset_card

    manifest = {
        "retained_rows": 8,
        "labels": {"positive": 4, "negative": 4},
        "roots": 2,
        "orientation_rule": "square_fixed_marginals",
        "grouping": "four_rows_per_root_same_shard",
        "row_kinds": {"p_prime_iff_p": 2},
        "duplicate_squares_dropped": 0,
        "degenerate_squares_dropped": 0,
        "conservation": {
            "screened_rows": 8,
            "kept_rows": 8,
            "duplicate_square_rows_dropped": 0,
            "degenerate_square_rows_dropped": 0,
            "holds": True,
        },
        "operations": {"SQUARE_N25_SYMMETRY_V1": 8},
        "provenance": {"engine_semantic_versions": ["v"], "segments": []},
        "supersedes": "core_v3_square",
        "artifact_status": "x",
    }
    card = dataset_card("core_v3_square_v2", manifest, None)
    assert "`Not (Iff reference candidate)`" in card
    assert "complete ground assignment" not in card
    assert "supersedes `sprint_v1/core_v3_square`" in card
    plain = dataset_card("tenk", {**manifest, "orientation_rule": None, "supersedes": None}, None)
    assert "complete ground assignment" in plain and "supersedes" not in plain

    wave2 = dataset_card("core_v1", manifest, None, remote_prefix="wave2/core_v1")
    assert 'path: "wave2/core_v1/shard-*/rows.jsonl"' in wave2
    assert "limited to local certified transforms" in wave2


def test_existing_prefix_verification_uses_git_and_lfs_digests(tmp_path: Path) -> None:
    import hashlib
    from types import SimpleNamespace

    from leanfaith.sft1.sprint.publish import PublishError, _verify_existing_prefix

    regular = tmp_path / "README.md"
    regular.write_bytes(b"card\n")
    large = tmp_path / "shard-0001" / "sidecars.jsonl"
    large.parent.mkdir()
    large.write_bytes(b"sidecar\n")

    def git_blob_sha1(path: Path) -> str:
        data = path.read_bytes()
        return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()

    prefix = "sprint_v1/run"
    remote = [
        SimpleNamespace(
            path=f"{prefix}/README.md",
            size=regular.stat().st_size,
            blob_id=git_blob_sha1(regular),
            lfs=None,
        ),
        SimpleNamespace(
            path=f"{prefix}/shard-0001/sidecars.jsonl",
            size=large.stat().st_size,
            blob_id="pointer-blob",
            lfs=SimpleNamespace(sha256=hashlib.sha256(large.read_bytes()).hexdigest()),
        ),
    ]

    class FakeApi:
        def repo_info(self, **_: object) -> object:
            return SimpleNamespace(sha="a" * 40, private=True)

        def list_repo_tree(self, **_: object) -> list[object]:
            return remote

    verification, hashes = _verify_existing_prefix(
        FakeApi(),
        repo_id="owner/repo",
        revision="a" * 40,
        remote_prefix=prefix,
        local_root=tmp_path,
        files=[regular, large],
    )
    assert verification == {
        "method": "immutable_hub_tree_git_blob_sha1_and_xet_lfs_sha256",
        "full_fresh_download": False,
        "immutable_revision": "a" * 40,
        "remote_prefix": prefix,
        "path_count": 2,
        "byte_count": regular.stat().st_size + large.stat().st_size,
        "regular_git_blobs": 1,
        "xet_lfs_files": 1,
        "path_set_match": True,
        "size_match": True,
        "digest_match": True,
    }
    assert hashes[f"{prefix}/README.md"] == hashlib.sha256(regular.read_bytes()).hexdigest()

    remote[1].lfs.sha256 = "0" * 64
    with pytest.raises(PublishError, match=r"sidecars\.jsonl"):
        _verify_existing_prefix(
            FakeApi(),
            repo_id="owner/repo",
            revision="a" * 40,
            remote_prefix=prefix,
            local_root=tmp_path,
            files=[regular, large],
        )


def test_operation_cache_revision_only_changes_revised_keys() -> None:
    from leanfaith.sft1.sprint import square

    common = {
        "name": "Nat.a",
        "engine_semantic_version": "sft1_sprint_engine_v1",
        "project_revision": "r" * 40,
        "lean_version": "v4.31.0-rc1",
        "import_options_fingerprint": "f" * 64,
    }
    assert square.operation_cache_revision("SQUARE_N25_SYMMETRY_V1") == 0
    assert square.operation_cache_revision("SQUARE_N19_CURRICULUM_V1") == 1
    legacy = square.square_cache_key(
        operation_id="SQUARE_N25_SYMMETRY_V1", revision=0, schema=2, **common
    )
    from leanfaith.config.hashing import hash_canonical

    assert legacy == hash_canonical(
        {
            "kind": "square_root",
            "cache_schema": 2,
            "operation_id": "SQUARE_N25_SYMMETRY_V1",
            **common,
        }
    )
    r0 = square.square_cache_key(
        operation_id="SQUARE_N19_CURRICULUM_V1", revision=0, schema=2, **common
    )
    r1 = square.square_cache_key(
        operation_id="SQUARE_N19_CURRICULUM_V1", revision=1, schema=2, **common
    )
    assert r0 != r1


def test_schema3_cache_keys_bind_engine_context_and_revision() -> None:
    from leanfaith.sft1.sprint import square

    common = {
        "operation_id": "SQUARE_N25_SYMMETRY_V1",
        "name": "Nat.a",
        "engine_semantic_version": "sft1_sprint_engine_v1",
        "project_revision": "r" * 40,
        "lean_version": "v4.31.0-rc1",
        "import_options_fingerprint": "f" * 64,
        "revision": 0,
    }
    base = square.square_cache_key(
        **common, schema=3, engine_source_sha256="e" * 64, compile_context_id="ctx:1"
    )
    assert base != square.square_cache_key(
        **common, schema=3, engine_source_sha256="0" * 64, compile_context_id="ctx:1"
    )
    assert base != square.square_cache_key(
        **common, schema=3, engine_source_sha256="e" * 64, compile_context_id="ctx:2"
    )
    assert base != square.square_cache_key(
        **{**common, "revision": 1},
        schema=3,
        engine_source_sha256="e" * 64,
        compile_context_id="ctx:1",
    )
    legacy = square.square_cache_key(**common, schema=2)
    assert legacy != base
    with pytest.raises(square.SquareError):
        square.square_cache_key(**common, schema=3)


def test_cache_put_never_overwrites_an_existing_record(tmp_path: Path) -> None:
    from leanfaith.sft1.sprint.runner import RunPaths
    from leanfaith.sft1.sprint.store import Journal, SemanticCache

    runner = _square_runner_stub()
    runner.cache = SemanticCache(tmp_path / "cache")
    paths = RunPaths(tmp_path, "run")
    paths.run_dir.mkdir(parents=True)
    runner.journal = Journal(paths.journal)
    first = {"process_request_hash": "a" * 64, "render": {"request_hash": "b" * 64}, "v": 1}
    assert runner.cache_put("Nat.a", first) == "written"
    assert runner.cache_put("Nat.a", dict(first)) == "identical"
    other = {"process_request_hash": "c" * 64, "render": {"request_hash": "d" * 64}, "v": 2}
    assert runner.cache_put("Nat.a", other) == "kept_existing"
    assert runner.cache.get_root(runner.square_root_key("Nat.a"))["v"] == 1
    skipped = [r for r in runner.journal.read() if r.get("kind") == "cache_write_skipped"]
    assert len(skipped) == 1 and skipped[0]["new_process_request_hash"] == "c" * 64


def _snapshot_fixture(tmp_path: Path) -> tuple[dict[str, object], dict[str, object], Path]:
    engine_block = {
        "source_sha256": "e" * 64,
        "compile_context_id": "ctx:1",
        "semantic_version": "sft1_sprint_engine_v1",
        "import_options_fingerprint": "f" * 64,
    }
    project = {"project_revision": "r" * 40, "lean_version": "v4.31.0-rc1"}
    alpha = {"p": "1", "c": "2", "p_prime": "3", "c_prime": "4"}
    record = {
        "root": "Nat.a",
        "status": "retained",
        "operation_id": "SQUARE_N25_SYMMETRY_V1",
        "engine": engine_block,
        "process_request_hash": "p" * 64,
        "render": {"request_hash": "q" * 64},
        "alpha": alpha,
        "implementation_commit": "c" * 40,
    }
    key = hash_canonical(
        {
            "kind": "square_root",
            "cache_schema": 3,
            "operation_id": "SQUARE_N25_SYMMETRY_V1",
            "operation_revision": 0,
            "name": "Nat.a",
            "engine_source_sha256": engine_block["source_sha256"],
            "compile_context_id": "ctx:1",
            "engine_semantic_version": engine_block["semantic_version"],
            "project_revision": project["project_revision"],
            "lean_version": project["lean_version"],
            "import_options_fingerprint": engine_block["import_options_fingerprint"],
        }
    )
    sidecar = {
        "root_name": "Nat.a",
        "operation_id": "SQUARE_N25_SYMMETRY_V1",
        "engine": engine_block,
        "project": project,
        "lean_request_hashes": {"process": "p" * 64, "render": "q" * 64},
        "square": {"alpha": alpha},
        "implementation_commit": "c" * 40,
        "cache": {
            "kind": "square_root",
            "schema": 3,
            "revision": 0,
            "key": key,
            "path": f"roots/{key[:2]}/{key}.json",
            "content_sha256": hash_canonical(record),
            "snapshot": {"file": "cache_records/shard-0001.jsonl", "line": 0},
        },
    }
    release = tmp_path / "release"
    (release / "cache_records").mkdir(parents=True)
    import json

    (release / "cache_records" / "shard-0001.jsonl").write_text(
        json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    return sidecar, record, release


def test_release_snapshot_survives_a_later_cache_overwrite(tmp_path: Path) -> None:
    import json

    from leanfaith.config.hashing import canonical_json_bytes
    from leanfaith.sft1.sprint import provenance

    sidecar, record, release = _snapshot_fixture(tmp_path)
    cache_root = tmp_path / "cache"
    live = cache_root / str(sidecar["cache"]["path"])
    live.parent.mkdir(parents=True)
    live.write_bytes(canonical_json_bytes(record) + b"\n")
    store = provenance.SnapshotStore(release)
    schema, issues, live_agrees = provenance.verify_square_cache(
        sidecar, cache_root, snapshots=store
    )
    assert (schema, issues, live_agrees) == (3, [], True)
    # a later fixture overwrites the live record: the release stays verified
    live.write_bytes(canonical_json_bytes({**record, "process_request_hash": "z" * 64}) + b"\n")
    schema, issues, live_agrees = provenance.verify_square_cache(
        sidecar, cache_root, snapshots=provenance.SnapshotStore(release)
    )
    assert issues == [] and live_agrees is False
    # tampering with the snapshot is caught
    packed = release / "cache_records" / "shard-0001.jsonl"
    packed.write_text(json.dumps({**record, "alpha": {}}) + "\n", encoding="utf-8")
    _, issues, _ = provenance.verify_square_cache(
        sidecar, cache_root, snapshots=provenance.SnapshotStore(release)
    )
    assert issues == ["cache snapshot content hash differs from the sidecar"]


def test_recover_square_record_from_run_evidence(tmp_path: Path) -> None:
    import json

    from leanfaith.sft1.sprint import square
    from leanfaith.sft1.sprint.runner import RunPaths

    paths = RunPaths(tmp_path, "square_full")
    paths.run_dir.mkdir(parents=True)
    payload = _square_payload("Nat.a")
    process_hash = "a" * 64
    render_hash = "b" * 64
    engine_block = {"source_sha256": "e" * 64, "compile_context_id": "ctx:1"}
    rows = []
    endpoints = {"p": "P", "c": "C", "p_prime": "P'", "c_prime": "C'"}
    for kind, label, ref_ep, cand_ep, _key in square.ROW_KINDS:

        def rec(ep: str) -> dict[str, object]:
            return {
                "endpoint_id": f"0.{ep}",
                "goal_v1": endpoints[ep],
                "provenance": {"expr_hash": ep},
            }

        rows.append(
            {
                "row": {
                    "reference": endpoints[ref_ep],
                    "candidate": endpoints[cand_ep],
                    "label": label,
                },
                "sidecar": {
                    "root_name": "Nat.a",
                    "row_kind": kind,
                    "lean_request_hashes": {"process": process_hash, "render": render_hash},
                    "engine": engine_block,
                    "evidence": {"square": payload["evidence"]},
                    "repr": {
                        "reference": rec(ref_ep),
                        "candidate": rec(cand_ep),
                        "reference_source_material": {"kind": "raw_statement"},
                        "candidate_source_material": {"kind": "derived"},
                    },
                },
            }
        )
    paths.retained.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    line = "LFSFT1SPRINTJSON " + json.dumps(
        {**payload, "kind": "square", "root": "Nat.a", "status": "retained"}
    )
    raw = {
        "request_hash": process_hash,
        "request": {"code": ""},
        "response": {"messages": [{"severity": "info", "data": line}]},
    }
    (raw_dir / f"{process_hash}.abcd.json").write_text(json.dumps(raw), encoding="utf-8")
    (raw_dir / f"{process_hash}.abcd.response-ffff.json").write_text(
        json.dumps(raw), encoding="utf-8"
    )
    record = square.recover_square_record(
        paths, "Nat.a", raw_dir=raw_dir, operation_id="SQUARE_N25_SYMMETRY_V1"
    )
    assert record is not None
    assert record["alpha"] == payload["alpha"] and record["evidence"] == payload["evidence"]
    assert record["process_request_hash"] == process_hash
    assert record["render"]["request_hash"] == render_hash
    assert {"p", "c", "p_prime", "c_prime"} <= set(record["render"])
    assert record["render"]["p_prime"]["record"]["endpoint_id"] == "0.p_prime"
    assert record["implementation_commit"] is None
    assert record["implementation_commit_source"] == "recovered_from_run_evidence:square_full"
    assert record["recovered_from"]["process_raw_copies"] == 2
    # disagreeing stored copies are refused
    bad = dict(raw)
    bad["response"] = {
        "messages": [
            {
                "severity": "info",
                "data": "LFSFT1SPRINTJSON "
                + json.dumps(
                    {
                        **payload,
                        "kind": "square",
                        "root": "Nat.a",
                        "status": "retained",
                        "alpha": {**payload["alpha"], "p": "x"},
                    }
                ),
            }
        ]
    }
    (raw_dir / f"{process_hash}.abcd.response-eeee.json").write_text(
        json.dumps(bad), encoding="utf-8"
    )
    assert (
        square.recover_square_record(
            paths, "Nat.a", raw_dir=raw_dir, operation_id="SQUARE_N25_SYMMETRY_V1"
        )
        is None
    )


def test_select_squares_prefers_the_complete_v4_square() -> None:
    from leanfaith.sft1.sprint import square

    kinds = [kind for kind, *_rest in square.ROW_KINDS]

    def rows(root: str, op: str, p: str, c: str, p2: str, c2: str) -> list[dict[str, object]]:
        pairs = {
            "p_prime_iff_p": (p2, p),
            "c_iff_c_prime": (c, c2),
            "not_iff_c_p": (c, p),
            "not_iff_p_prime_c_prime": (p2, c2),
        }
        return [
            {
                "sidecar": {
                    "root_id": root,
                    "operation_id": op,
                    "row_kind": kind,
                    "root_name": root,
                },
                "unordered_pair_key": "|".join(sorted(pairs[kind])),
                "label": kind in {"p_prime_iff_p", "c_iff_c_prime"},
                "row_hash": f"{root}:{op}:{kind}",
            }
            for kind in kinds
        ]

    v3 = rows("root:a", "SQUARE_N25_SYMMETRY_V1", "x=y", "x≠y", "y=x", "y≠x")
    v4 = rows("root:a", "SQUARE_N25_BINDER_V1", "x=y", "x≠y", "∀b a, x=y", "∀b a, x≠y")
    only_v3 = rows("root:b", "SQUARE_N25_SYMMETRY_V1", "u=v", "u≠v", "v=u", "v≠u")
    selection = square.select_squares(
        v3 + v4 + only_v3, (), preferred_operations=("SQUARE_N25_BINDER_V1",)
    )
    assert len(selection.kept) == 8
    assert {k.split("|")[1] for k in selection.accepted_roots} == {
        "SQUARE_N25_BINDER_V1",
        "SQUARE_N25_SYMMETRY_V1",
    }
    assert [s["square"] for s in selection.superseded_squares] == ["root:a|SQUARE_N25_SYMMETRY_V1"]
    assert selection.superseded_squares[0]["superseded_by"] == "root:a|SQUARE_N25_BINDER_V1"
    assert selection.duplicate_squares == [] and selection.degenerate_roots == []
    # without a preference both squares of root a would collide on the (C, P) pair
    plain = square.select_squares(v3 + v4 + only_v3, ())
    assert len(plain.duplicate_squares) == 1 and len(plain.kept) == 8


def _mixed_sidecars() -> list[dict[str, object]]:
    sidecars: list[dict[str, object]] = []
    specs = [
        (
            "root:a",
            "SQUARE_N25_BINDER_V1",
            "SQ25B",
            "N25_TOGGLE_EQ_NE_PROOF_V1",
            "P14_SWAP_INDEPENDENT_DATA_BINDERS_V1",
            "square_n25_p14",
        ),
        (
            "root:b",
            "SQUARE_N32_BINDER_V1",
            "SQ32B",
            "N32_SWAP_ROLE_ORDER_PROOF_V1",
            "P23_CURRY_PROP_PAIR_V1",
            "square_n32_p23",
        ),
        (
            "root:c",
            "SQUARE_N19_CURRICULUM_V1",
            "SQ19",
            "N19_WHOLE_CLAIM_NEGATION_V1",
            "P18_SYMMETRIZE_EQUALITY_V1",
            "square_n19_eq",
        ),
    ]
    for root, op, mech, neg, t_p, family in specs:
        for kind, label, *_rest in [
            (k[0], k[1])
            for k in [
                (x[0], x[1])
                for x in __import__(
                    "leanfaith.sft1.sprint.square", fromlist=["ROW_KINDS"]
                ).ROW_KINDS
            ]
        ]:
            sidecars.append(
                {
                    "root_id": root,
                    "operation_id": op,
                    "mechanism": mech,
                    "row_kind": kind,
                    "label": label,
                    "core_family": family,
                    "square": {"negative_operation": neg, "t_p": t_p},
                }
            )
    return sidecars


def test_manifest_aggregates_derive_from_sidecars_and_validator_compares_them() -> None:
    from leanfaith.sft1.sprint import integrity, square

    sidecars = _mixed_sidecars()
    aggregates = square.sidecar_aggregates(sidecars)
    assert aggregates["mechanisms"] == {"SQ19": 4, "SQ25B": 4, "SQ32B": 4}
    assert aggregates["negative_mechanisms"] == {
        "N19_WHOLE_CLAIM_NEGATION_V1": 4,
        "N25_TOGGLE_EQ_NE_PROOF_V1": 4,
        "N32_SWAP_ROLE_ORDER_PROOF_V1": 4,
    }
    assert aggregates["operations"] == {
        "SQUARE_N19_CURRICULUM_V1": 4,
        "SQUARE_N25_BINDER_V1": 4,
        "SQUARE_N32_BINDER_V1": 4,
    }
    assert aggregates["squares"] == 3 and aggregates["curriculum_only"] is True
    derived = integrity.sidecar_aggregate_counts(sidecars)
    manifest = {
        **{
            k: derived[k]
            for k in (
                "operations",
                "mechanisms",
                "negative_mechanisms",
                "transforms",
                "families",
                "row_kinds",
                "roots",
                "squares",
                "retained_rows",
                "labels",
                "curriculum_only",
            )
        },
        "artifact_status": "curriculum_auxiliary_certified_easy_pattern",
    }
    assert integrity.manifest_aggregate_issues(manifest, sidecars) == []
    wrong = {
        **manifest,
        "mechanisms": {"SQ25": 12},
        "artifact_status": "square_release_high_confidence",
    }
    issues = integrity.manifest_aggregate_issues(wrong, sidecars)
    assert any(text.startswith("mechanisms:") for text in issues)
    assert "curriculum-only view labelled as a core release" in issues
    missing = {k: v for k, v in manifest.items() if k != "negative_mechanisms"}
    assert "negative_mechanisms missing from the manifest" in integrity.manifest_aggregate_issues(
        missing, sidecars
    )
    core_only = [s for s in sidecars if s["operation_id"] != "SQUARE_N19_CURRICULUM_V1"]
    core_manifest = {
        **integrity.sidecar_aggregate_counts(core_only),
        "artifact_status": "square_release_high_confidence_curriculum_seed",
    }
    assert integrity.manifest_aggregate_issues(core_manifest, core_only) == []


def test_pairwise_shortcut_diagnostics_are_telemetry() -> None:
    from leanfaith.sft1.sprint import shortcut

    records = [
        {"row": {"reference": "n : ℕ\n⊢ a = b", "candidate": "n : ℕ\n⊢ b = a", "label": True}},
        {"row": {"reference": "n : ℕ\n⊢ a ≠ b", "candidate": "n : ℕ\n⊢ a = b", "label": False}},
        {
            "row": {
                "reference": "⊢ ¬∀ (n : ℕ), a = b",
                "candidate": "n : ℕ\n⊢ a = b",
                "label": False,
            }
        },
        {
            "row": {
                "reference": "⊢ ¬∀ (n : ℕ), a = b",
                "candidate": "⊢ ¬∀ (n : ℕ), b = a",
                "label": True,
            }
        },
    ]
    result = shortcut.pairwise_shortcut_diagnostics(records)
    assert result["kind"] == "telemetry_not_a_gate" and result["rows"] == 4
    assert set(result["rules"]) == {
        "relation_parity",
        "target_equality",
        "binder_delta",
        "negation_xor",
    }
    assert result["rules"]["negation_xor"]["rows_with_negation_on_exactly_one_side"] == 1
    assert result["rules"]["negation_xor"]["balanced_accuracy"] == 0.75
    assert result["rules"]["target_equality"]["agreeing_rows"] == 0
    assert 0.0 <= result["max_balanced_accuracy"] <= 1.0
    assert shortcut.pairwise_shortcut_diagnostics([]) == {"rows": 0, "rules": {}}


def test_curriculum_sampling_configs_and_card() -> None:
    from leanfaith.sft1.sprint import square
    from leanfaith.sft1.sprint.publish import dataset_card

    configs = square.curriculum_sampling_configs()
    weights = {c["name"]: c["weight"] for c in configs["configs"]}
    assert weights == {"n19_0pct": 0.0, "n19_2pct": 0.02, "n19_5pct": 0.05, "n19_10pct": 0.10}
    assert configs["initial_default"] == "n19_2pct" and configs["hard_ceiling"] == "n19_10pct"
    manifest = {
        "retained_rows": 8,
        "labels": {"positive": 4, "negative": 4},
        "roots": 2,
        "orientation_rule": "square_fixed_marginals",
        "grouping": "four_rows_per_root_same_shard",
        "row_kinds": {"p_prime_iff_p": 2},
        "duplicate_squares_dropped": 0,
        "degenerate_squares_dropped": 0,
        "conservation": {"holds": True},
        "operations": {"SQUARE_N19_CURRICULUM_V1": 8},
        "negative_mechanisms": {"N19_WHOLE_CLAIM_NEGATION_V1": 8},
        "curriculum_only": True,
        "sampling_configs": configs,
        "provenance": {"engine_semantic_versions": ["v"], "segments": []},
        "artifact_status": "curriculum_auxiliary_certified_easy_pattern",
    }
    release = {
        "outer_negation_xor_baseline": {"balanced_accuracy": 0.98},
        "pairwise_diagnostics": {
            "rules": {"negation_xor": {"rule": "r", "balanced_accuracy": 0.98}}
        },
        "checks": {"a": True},
    }
    card = dataset_card("aux_n19_square_curriculum", manifest, release)
    assert "Never concatenate this view into the headline core" in card
    assert "**0.98**" in card and "`n19_2pct` | 0.02 | initial_default" in card
    assert "not broad theorem-equivalence coverage" in card and "telemetry, not a gate" in card
    assert "N19_WHOLE_CLAIM_NEGATION_V1" in card


def test_screen_sample_keeps_whole_roots_and_is_deterministic(tmp_path: Path) -> None:
    import json

    from leanfaith.sft1.sprint import shortcut

    view = tmp_path / "view"
    roots = [f"root:{i}" for i in range(30)]
    for shard, chunk in enumerate((roots[:15], roots[15:]), start=1):
        shard_dir = view / f"shard-{shard:04d}"
        shard_dir.mkdir(parents=True)
        rows, sidecars = [], []
        for root in chunk:
            for kind in range(4):
                rows.append(
                    {
                        "reference": f"{root} r{kind}",
                        "candidate": f"{root} c{kind}",
                        "label": kind < 2,
                    }
                )
                sidecars.append({"root_id": root, "row_kind": str(kind)})
        (shard_dir / "rows.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
        )
        (shard_dir / "sidecars.jsonl").write_text(
            "".join(json.dumps(s) + "\n" for s in sidecars), encoding="utf-8"
        )
    full, info = shortcut.screen_sample(view, max_rows=1000)
    assert info["method"] == "full_view" and len(full) == 120
    sample, info = shortcut.screen_sample(view, max_rows=40)
    assert (
        info["rows_screened"] == 40 and info["roots_screened"] == 10 and info["rows_total"] == 120
    )
    counts: dict[str, int] = {}
    for record in sample:
        counts[record["sidecar"]["root_id"]] = counts.get(record["sidecar"]["root_id"], 0) + 1
    assert set(counts.values()) == {4}  # whole roots only
    again, _ = shortcut.screen_sample(view, max_rows=40)
    assert [r["row"] for r in again] == [r["row"] for r in sample]
    assert next(iter(shortcut.iter_serialized_view(view)))["row"]["reference"] == "root:0 r0"
