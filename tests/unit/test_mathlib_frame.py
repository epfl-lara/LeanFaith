"""Deterministic public mathlib file-frame tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from leanfaith.config.hashing import hash_file
from leanfaith.sources.mathlib import RepoFileEntry, RepoInventory
from leanfaith.sources.mathlib_frame import (
    MathlibFileFrame,
    MathlibFrameError,
    build_mathlib_file_frame,
    load_and_verify_mathlib_file_frame,
    make_mathlib_inventory_id,
    mathlib_domain,
    mathlib_frame_additions,
    verify_mathlib_file_frame,
    write_mathlib_file_frame,
)

_REVISION = "d568c8c09630de097a046763c17b9ea99f95f950"


def _digest(index: int) -> str:
    return f"{index:064x}"


def _inventory(
    *,
    source: str = "mathlib",
    revision: str = _REVISION,
    root_module: str = "Mathlib",
    files: tuple[RepoFileEntry, ...] | None = None,
) -> RepoInventory:
    if files is None:
        files = (
            *(
                RepoFileEntry(
                    relative_path=f"Mathlib/Algebra/Fixture{index}.lean",
                    sha256=_digest(index),
                )
                for index in range(1, 7)
            ),
            *(
                RepoFileEntry(
                    relative_path=f"Mathlib/Topology/Fixture{index}.lean",
                    sha256=_digest(100 + index),
                )
                for index in range(1, 4)
            ),
            RepoFileEntry(relative_path="Mathlib/Init.lean", sha256=_digest(200)),
        )
    return RepoInventory(
        source=source,
        revision=revision,
        root_module=root_module,
        globs=("Mathlib/**/*.lean",),
        file_count=len(files),
        files=files,
    )


def _frame(
    inventory: RepoInventory | None = None,
    *,
    seed: str = "public-frame-v1",
    target: int = 5,
    excluded_domains: tuple[str, ...] = (),
):
    return build_mathlib_file_frame(
        inventory or _inventory(),
        expected_revision=_REVISION,
        target_file_count=target,
        selection_seed=seed,
        excluded_domains=excluded_domains,
    )


def test_frame_is_deterministic_content_addressed_and_proportional() -> None:
    first = _frame()
    second = _frame()

    assert first == second
    assert first.frame_id.startswith("mathlib_file_frame_v1:")
    assert first.source == "mathlib"
    assert first.private_source is False
    assert first.release_eligible is True
    assert first.inventory_file_count == first.eligible_file_count == 10
    assert first.excluded_file_count == 0
    assert first.excluded_domains == ()
    assert first.target_file_count == first.selected_file_count == 5
    assert [allocation.model_dump() for allocation in first.domain_allocations] == [
        {"domain": "Algebra", "inventory_file_count": 6, "selected_file_count": 3},
        {"domain": "Init", "inventory_file_count": 1, "selected_file_count": 1},
        {"domain": "Topology", "inventory_file_count": 3, "selected_file_count": 1},
    ]
    paths = [member.relative_path for member in first.members]
    assert paths == sorted(paths)
    assert len(paths) == len(set(paths)) == 5


def test_inventory_order_and_glob_order_do_not_change_frame() -> None:
    inventory = _inventory()
    permuted = inventory.model_copy(
        update={
            "files": tuple(reversed(inventory.files)),
            "globs": ("Mathlib/NumberTheory/**/*.lean", "Mathlib/**/*.lean"),
        }
    )
    matching = inventory.model_copy(
        update={
            "globs": ("Mathlib/**/*.lean", "Mathlib/NumberTheory/**/*.lean"),
        }
    )

    assert make_mathlib_inventory_id(permuted) == make_mathlib_inventory_id(matching)
    assert _frame(permuted) == _frame(matching)


def test_selection_seed_changes_content_id_and_rank() -> None:
    first = _frame(seed="seed-a")
    second = _frame(seed="seed-b")

    assert first.frame_id != second.frame_id
    assert first.selection_seed_sha256 != second.selection_seed_sha256
    assert {item.selection_rank_sha256 for item in first.members} != {
        item.selection_rank_sha256 for item in second.members
    }
    assert first.domain_allocations == second.domain_allocations


def test_progressive_frames_are_nested_and_return_exact_additions() -> None:
    previous = _frame(target=5)
    expanded = _frame(target=8)

    additions = mathlib_frame_additions(previous, expanded)
    assert len(additions) == 3
    assert {item.relative_path for item in previous.members}.isdisjoint(
        item.relative_path for item in additions
    )
    assert {item.relative_path for item in expanded.members} == {
        item.relative_path for item in previous.members
    } | {item.relative_path for item in additions}

    with pytest.raises(MathlibFrameError, match="target must be larger"):
        mathlib_frame_additions(expanded, previous)
    with pytest.raises(MathlibFrameError, match="same inventory and selection"):
        mathlib_frame_additions(previous, _frame(seed="other-seed", target=8))


def test_explicit_domain_exclusions_are_hash_bound_and_replayed() -> None:
    frame = _frame(target=4, excluded_domains=("Init", "Topology"))

    assert frame.eligible_file_count == 6
    assert frame.excluded_file_count == 4
    assert frame.excluded_domains == ("Init", "Topology")
    assert {member.domain for member in frame.members} == {"Algebra"}
    assert [item.domain for item in frame.domain_allocations] == ["Algebra"]

    without_exclusions = _frame(target=4)
    assert frame.frame_id != without_exclusions.frame_id
    with pytest.raises(MathlibFrameError, match="absent from inventory"):
        _frame(target=4, excluded_domains=("Missing",))
    with pytest.raises(MathlibFrameError, match="unique and sorted"):
        _frame(target=4, excluded_domains=("Topology", "Init"))


@pytest.mark.parametrize(
    ("relative_path", "domain"),
    [
        ("Mathlib/Algebra/Group/Basic.lean", "Algebra"),
        ("Mathlib/NumberTheory/Prime.lean", "NumberTheory"),
        ("Mathlib/Tactic.lean", "Tactic"),
    ],
)
def test_mathlib_domain(relative_path: str, domain: str) -> None:
    assert mathlib_domain(relative_path) == domain


@pytest.mark.parametrize(
    "relative_path",
    [
        "Private/A.lean",
        "../Mathlib/A.lean",
        "/Mathlib/A.lean",
        "Mathlib/../Private/A.lean",
        "Mathlib\\Algebra\\A.lean",
        "Mathlib//Algebra/A.lean",
        "Mathlib/Algebra/A.txt",
        " Mathlib/Algebra/A.lean",
    ],
)
def test_non_mathlib_or_noncanonical_paths_are_rejected(relative_path: str) -> None:
    entry = RepoFileEntry(relative_path=relative_path, sha256=_digest(1))
    with pytest.raises(MathlibFrameError, match=r"path|Mathlib"):
        _frame(_inventory(files=(entry,)))


def test_private_source_and_wrong_root_are_rejected() -> None:
    with pytest.raises(MathlibFrameError, match=r"inventory\.source"):
        _frame(_inventory(source="sft_classic"))
    with pytest.raises(MathlibFrameError, match="root_module"):
        _frame(_inventory(root_module="Private"))


def test_revision_target_hash_and_duplicate_validation_fail_closed() -> None:
    with pytest.raises(MathlibFrameError, match="differs from expected"):
        _frame(_inventory(revision="0" * 40))
    with pytest.raises(MathlibFrameError, match="exceeds eligible inventory"):
        build_mathlib_file_frame(
            _inventory(),
            expected_revision=_REVISION,
            target_file_count=11,
            selection_seed="seed",
        )
    with pytest.raises(MathlibFrameError, match="positive integer"):
        build_mathlib_file_frame(
            _inventory(),
            expected_revision=_REVISION,
            target_file_count=True,
            selection_seed="seed",
        )

    bad_hash = RepoFileEntry(relative_path="Mathlib/A.lean", sha256="A" * 64)
    with pytest.raises(MathlibFrameError, match="invalid inventory hash"):
        _frame(_inventory(files=(bad_hash,)))

    duplicate = RepoFileEntry(relative_path="Mathlib/A.lean", sha256=_digest(1))
    with pytest.raises(MathlibFrameError, match="duplicate inventory path"):
        _frame(_inventory(files=(duplicate, duplicate)))


def test_inventory_accounting_and_seed_validation_fail_closed() -> None:
    inventory = _inventory().model_copy(update={"file_count": 9})
    with pytest.raises(MathlibFrameError, match="file_count"):
        _frame(inventory)
    with pytest.raises(MathlibFrameError, match="selection_seed"):
        _frame(seed=" seed")


def test_frame_model_rejects_content_id_and_allocation_tampering() -> None:
    frame = _frame()
    payload = frame.model_dump(mode="json")
    payload["target_file_count"] = 4
    with pytest.raises(ValidationError, match="target_file_count"):
        MathlibFileFrame.model_validate(payload)

    payload = frame.model_dump(mode="json")
    payload["selection_seed_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="frame_id differs"):
        MathlibFileFrame.model_validate(payload)


def test_immutable_write_load_and_replay(tmp_path: Path) -> None:
    inventory = _inventory()
    frame = _frame(inventory)
    output = tmp_path / "frames" / "mathlib.json"

    first_hash = write_mathlib_file_frame(frame, output)
    second_hash = write_mathlib_file_frame(frame, output)
    assert first_hash == second_hash == hash_file(output)
    assert (
        load_and_verify_mathlib_file_frame(
            output,
            inventory=inventory,
            expected_revision=_REVISION,
            selection_seed="public-frame-v1",
        )
        == frame
    )

    with pytest.raises(MathlibFrameError, match="deterministic replay"):
        load_and_verify_mathlib_file_frame(
            output,
            inventory=inventory,
            expected_revision=_REVISION,
            selection_seed="wrong-seed",
        )


def test_replay_rejects_inventory_hash_drift(tmp_path: Path) -> None:
    inventory = _inventory()
    frame = _frame(inventory)
    output = tmp_path / "frame.json"
    write_mathlib_file_frame(frame, output)

    changed_first = inventory.files[0].model_copy(update={"sha256": _digest(999)})
    changed = inventory.model_copy(update={"files": (changed_first, *inventory.files[1:])})
    with pytest.raises(MathlibFrameError, match="deterministic replay"):
        load_and_verify_mathlib_file_frame(
            output,
            inventory=changed,
            expected_revision=_REVISION,
            selection_seed="public-frame-v1",
        )


def test_loader_rejects_noncanonical_duplicate_and_tampered_json(tmp_path: Path) -> None:
    inventory = _inventory()
    frame = _frame(inventory)
    output = tmp_path / "frame.json"

    output.write_text(json.dumps(frame.model_dump(mode="json"), indent=2) + "\n")
    with pytest.raises(MathlibFrameError, match="not canonical JSON"):
        load_and_verify_mathlib_file_frame(
            output,
            inventory=inventory,
            expected_revision=_REVISION,
            selection_seed="public-frame-v1",
        )

    canonical = json.dumps(frame.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    output.write_text('{"schema_version":1,"schema_version":1}\n')
    with pytest.raises(MathlibFrameError, match="duplicate JSON key"):
        load_and_verify_mathlib_file_frame(
            output,
            inventory=inventory,
            expected_revision=_REVISION,
            selection_seed="public-frame-v1",
        )

    payload = json.loads(canonical)
    payload["members"][0]["sha256"] = "f" * 64
    output.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    with pytest.raises(MathlibFrameError, match="frame_id differs"):
        load_and_verify_mathlib_file_frame(
            output,
            inventory=inventory,
            expected_revision=_REVISION,
            selection_seed="public-frame-v1",
        )


def test_write_rejects_conflicting_existing_artifact_and_symlink(tmp_path: Path) -> None:
    frame = _frame()
    output = tmp_path / "frame.json"
    output.write_text("{}\n")
    with pytest.raises(MathlibFrameError, match="conflicts"):
        write_mathlib_file_frame(frame, output)

    target = tmp_path / "target.json"
    target.write_text("{}\n")
    symlink = tmp_path / "link.json"
    symlink.symlink_to(target)
    with pytest.raises(MathlibFrameError, match="conflicts"):
        write_mathlib_file_frame(frame, symlink)


def test_write_rejects_symlinked_ancestor_without_writing_target(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)

    with pytest.raises(MathlibFrameError, match="symlink"):
        write_mathlib_file_frame(_frame(), linked / "frames" / "mathlib.json")

    assert not (outside / "frames").exists()


def test_load_rejects_symlinked_ancestor(tmp_path: Path) -> None:
    inventory = _inventory()
    outside = tmp_path / "outside"
    target = outside / "frame.json"
    write_mathlib_file_frame(_frame(inventory), target)
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)

    with pytest.raises(MathlibFrameError, match="symlink"):
        load_and_verify_mathlib_file_frame(
            linked / "frame.json",
            inventory=inventory,
            expected_revision=_REVISION,
            selection_seed="public-frame-v1",
        )


def test_load_rejects_symlinked_leaf(tmp_path: Path) -> None:
    inventory = _inventory()
    target = tmp_path / "target.json"
    write_mathlib_file_frame(_frame(inventory), target)
    linked = tmp_path / "linked.json"
    linked.symlink_to(target)

    with pytest.raises(MathlibFrameError, match="symlink"):
        load_and_verify_mathlib_file_frame(
            linked,
            inventory=inventory,
            expected_revision=_REVISION,
            selection_seed="public-frame-v1",
        )


def test_write_and_load_reject_non_regular_leaf(tmp_path: Path) -> None:
    inventory = _inventory()
    directory = tmp_path / "frame.json"
    directory.mkdir()

    with pytest.raises(MathlibFrameError, match="conflicts"):
        write_mathlib_file_frame(_frame(inventory), directory)
    with pytest.raises(MathlibFrameError, match="regular file"):
        load_and_verify_mathlib_file_frame(
            directory,
            inventory=inventory,
            expected_revision=_REVISION,
            selection_seed="public-frame-v1",
        )


def test_direct_verifier_rejects_tampered_unvalidated_model() -> None:
    inventory = _inventory()
    frame = _frame(inventory)
    tampered = frame.model_copy(update={"inventory_id": "mathlib_repo_inventory_v1:" + "0" * 64})
    with pytest.raises(MathlibFrameError, match="deterministic replay"):
        verify_mathlib_file_frame(
            tampered,
            inventory,
            expected_revision=_REVISION,
            selection_seed="public-frame-v1",
        )
