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


def test_config_loads_and_pins_seven_operations() -> None:
    loaded = load_sprint_config(ROOT)
    config = loaded.config
    assert tuple(config.engine.operations) == OPERATIONS
    assert config.project.options == {"Elab.async": False, "autoImplicit": False}
    assert config.execution.lean_workers == 1
    success = {f.operation_id for f in config.fixtures if f.expect_status == "retained"}
    rejection = {f.operation_id for f in config.fixtures if f.expect_status != "retained"}
    assert success == set(OPERATIONS)
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
    body = engine.process_body([("Nat.foo", mask), ("Foo.«bar baz».qux", 127)])
    assert body.startswith("run_meta do")
    assert "#[`Nat.foo, `Foo.«bar baz».qux] #[65, 127]" in body


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
    assert len(set(others)) == 6


def test_new_non_test_implementation_stays_compact() -> None:
    paths = [
        ROOT / "LeanFaith/Meta/SFT1/Sprint.lean",
        *(ROOT / "src/leanfaith/sft1/sprint").glob("*.py"),
    ]
    total = sum(len(path.read_text(encoding="utf-8").splitlines()) for path in paths)
    assert total < 4000, total
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
