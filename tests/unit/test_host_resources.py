from pathlib import Path

import pytest

from leanfaith.host_resources import (
    ReservationError,
    claim_resources,
    list_reservations,
    release_resources,
)


def _claim(
    root: Path,
    task: str,
    *,
    workers: int = 1,
    rss: float = 20.0,
    gpu: bool = False,
) -> None:
    claim_resources(
        root=root,
        task=task,
        lean_workers=workers,
        lean_rss_gib=rss,
        gpu=gpu,
        pid=1234,
        owner_session="test",
        worktree=root,
    )


def test_claims_enforce_machine_wide_lean_caps(tmp_path: Path) -> None:
    _claim(tmp_path, "CPT2")
    _claim(tmp_path, "REPR")

    with pytest.raises(ReservationError, match="worker cap exceeded"):
        _claim(tmp_path, "EVAL", rss=1.0)

    assert [item.task for item in list_reservations(tmp_path)] == ["CPT2", "REPR"]


def test_gpu_claim_is_exclusive_and_release_is_explicit(tmp_path: Path) -> None:
    _claim(tmp_path, "SFT2B", workers=0, rss=0.0, gpu=True)

    with pytest.raises(ReservationError, match="GPU is already reserved"):
        _claim(tmp_path, "TRAIN", workers=0, rss=0.0, gpu=True)

    released = release_resources(root=tmp_path, task="SFT2B")
    assert released.gpu is True
    assert list_reservations(tmp_path) == []


def test_duplicate_and_empty_claims_fail_closed(tmp_path: Path) -> None:
    _claim(tmp_path, "CPT2")
    with pytest.raises(ReservationError, match="already has a reservation"):
        _claim(tmp_path, "CPT2")
    with pytest.raises(ReservationError, match="claim at least"):
        _claim(tmp_path, "EMPTY", workers=0, rss=0.0)
