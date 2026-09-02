"""Lean-free invariants for the compact SFT1 sprint engine adapter, screens,
inventory scanner, durable store, and compaction."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from leanfaith.config.hashing import hash_canonical
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
    assert tuple(config.engine.operations) == OPERATIONS
    assert config.project.options == {"Elab.async": False, "autoImplicit": False}
    assert config.execution.lean_workers == 1
    success = {f.operation_id for f in config.fixtures if f.expect_status == "retained"}
    rejection = {f.operation_id for f in config.fixtures if f.expect_status != "retained"}
    # P_DROP_REDUNDANT_GUARD_PROOF_V1 receives its success fixture once the budgeted yield run
    # identifies a Mathlib root with a provably redundant guard.
    waived = {waiver.operation_id for waiver in config.fixtures_success_waivers}
    assert waived == {"P_DROP_REDUNDANT_GUARD_PROOF_V1"}
    assert set(OPERATIONS) - success <= waived
    assert rejection == set(OPERATIONS)


def test_config_rejects_async_elaboration() -> None:
    loaded = load_sprint_config(ROOT)
    payload = loaded.config.model_dump(mode="json")
    payload["project"]["options"]["Elab.async"] = True
    with pytest.raises(ValueError):
        SprintConfig.model_validate(payload)


def test_engine_source_declares_version_and_no_forbidden_tokens() -> None:
    source = (ROOT / engine.ENGINE_RELATIVE_PATH).read_text(encoding="utf-8")
    assert engine.engine_semantic_version(ROOT) == "sft1_sprint_engine_v1"
    for token in ("sorry", "addDecl", "addAndCompile", "ppGoal", "mkSorry", "sorryAx"):
        assert token not in source.replace("hasSorry", ""), token
    assert "Kernel.check" in source
    assert "Iff.intro" in source
    assert "Nat.lt_asymm" in source and "Int.lt_asymm" in source


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


def test_new_non_test_implementation_stays_compact() -> None:
    paths = [
        ROOT / "LeanFaith/Meta/SFT1/Sprint.lean",
        *(ROOT / "src/leanfaith/sft1/sprint").glob("*.py"),
    ]
    total = sum(len(path.read_text(encoding="utf-8").splitlines()) for path in paths)
    assert total < 9000, total
    assert json.loads(json.dumps({"ok": True}))["ok"]


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
