"""Atomic cross-worktree reservations for scarce Lean/GPU host resources."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import socket
import sys
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

DEFAULT_RESERVATION_ROOT = Path("/storage/milikic/leanfaith/value_first/host_reservations")
MAX_LEAN_WORKERS = 2
MAX_LEAN_RSS_GIB = 40.0
MAX_GPU_JOBS = 1

_TASK_RE = re.compile(r"[A-Z][A-Z0-9-]*")


class ReservationError(RuntimeError):
    """Raised when a resource claim is invalid or exceeds the host budget."""


@dataclass(frozen=True)
class Reservation:
    """One task's explicit claim on shared host resources."""

    task: str
    lean_workers: int
    lean_rss_gib: float
    gpu: bool
    pid: int
    owner_session: str
    hostname: str
    worktree: str
    created_at: str

    @classmethod
    def from_path(cls, path: Path) -> Reservation:
        raw: object = json.loads(path.read_text())
        if not isinstance(raw, dict):
            raise ReservationError(f"invalid reservation object: {path}")
        payload = cast(dict[str, object], raw)
        try:
            task = payload["task"]
            lean_workers = payload["lean_workers"]
            lean_rss_gib = payload["lean_rss_gib"]
            gpu = payload["gpu"]
            pid = payload["pid"]
            owner_session = payload["owner_session"]
            hostname = payload["hostname"]
            worktree = payload["worktree"]
            created_at = payload["created_at"]
        except KeyError as exc:
            raise ReservationError(f"missing {exc.args[0]!r} in reservation: {path}") from exc
        if not isinstance(task, str):
            raise ReservationError(f"invalid task in reservation: {path}")
        if not isinstance(lean_workers, int) or isinstance(lean_workers, bool):
            raise ReservationError(f"invalid lean_workers in reservation: {path}")
        if not isinstance(lean_rss_gib, int | float) or isinstance(lean_rss_gib, bool):
            raise ReservationError(f"invalid lean_rss_gib in reservation: {path}")
        if not isinstance(gpu, bool):
            raise ReservationError(f"invalid gpu in reservation: {path}")
        if not isinstance(pid, int) or isinstance(pid, bool):
            raise ReservationError(f"invalid pid in reservation: {path}")
        for key, value in (
            ("owner_session", owner_session),
            ("hostname", hostname),
            ("worktree", worktree),
            ("created_at", created_at),
        ):
            if not isinstance(value, str):
                raise ReservationError(f"invalid {key} in reservation: {path}")
        return cls(
            task=task,
            lean_workers=lean_workers,
            lean_rss_gib=float(lean_rss_gib),
            gpu=gpu,
            pid=pid,
            owner_session=cast(str, owner_session),
            hostname=cast(str, hostname),
            worktree=cast(str, worktree),
            created_at=cast(str, created_at),
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":")) + "\n"


def _validate_task(task: str) -> None:
    if _TASK_RE.fullmatch(task) is None:
        raise ReservationError("task must match [A-Z][A-Z0-9-]*")


def _reservation_path(root: Path, task: str) -> Path:
    _validate_task(task)
    return root / f"{task.lower()}.json"


@contextmanager
def _locked(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".lock"
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _load_unlocked(root: Path) -> list[Reservation]:
    return [Reservation.from_path(path) for path in sorted(root.glob("*.json"))]


def list_reservations(root: Path = DEFAULT_RESERVATION_ROOT) -> list[Reservation]:
    """Read a consistent snapshot of all current claims."""

    with _locked(root):
        return _load_unlocked(root)


def claim_resources(
    *,
    root: Path = DEFAULT_RESERVATION_ROOT,
    task: str,
    lean_workers: int,
    lean_rss_gib: float,
    gpu: bool,
    pid: int,
    owner_session: str,
    worktree: Path,
) -> Reservation:
    """Atomically add one claim if the machine-wide totals remain within budget."""

    _validate_task(task)
    if lean_workers < 0 or lean_rss_gib < 0:
        raise ReservationError("workers and RSS must be non-negative")
    if lean_workers == 0 and lean_rss_gib != 0:
        raise ReservationError("Lean RSS must be zero when no Lean worker is claimed")
    if lean_workers > 0 and lean_rss_gib <= 0:
        raise ReservationError("a Lean worker claim requires a positive measured RSS budget")
    if lean_workers == 0 and not gpu:
        raise ReservationError("claim at least one Lean worker or the GPU")
    if pid <= 0:
        raise ReservationError("pid must be positive")

    with _locked(root):
        target = _reservation_path(root, task)
        if target.exists():
            raise ReservationError(f"task {task} already has a reservation; release it first")
        existing = _load_unlocked(root)
        total_workers = sum(item.lean_workers for item in existing) + lean_workers
        total_rss = sum(item.lean_rss_gib for item in existing) + lean_rss_gib
        total_gpu = sum(item.gpu for item in existing) + int(gpu)
        if total_workers > MAX_LEAN_WORKERS:
            raise ReservationError(
                f"Lean worker cap exceeded: requested total {total_workers}, cap {MAX_LEAN_WORKERS}"
            )
        if total_rss > MAX_LEAN_RSS_GIB:
            raise ReservationError(
                f"Lean RSS cap exceeded: requested total {total_rss:g} GiB, "
                f"cap {MAX_LEAN_RSS_GIB:g} GiB"
            )
        if total_gpu > MAX_GPU_JOBS:
            raise ReservationError("the local GPU is already reserved")

        reservation = Reservation(
            task=task,
            lean_workers=lean_workers,
            lean_rss_gib=lean_rss_gib,
            gpu=gpu,
            pid=pid,
            owner_session=owner_session,
            hostname=socket.gethostname(),
            worktree=str(worktree.resolve()),
            created_at=datetime.now(UTC).isoformat(),
        )
        descriptor, temporary_name = tempfile.mkstemp(
            dir=root,
            prefix=f".{task.lower()}-",
            suffix=".tmp",
            text=True,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w") as temporary_file:
                temporary_file.write(reservation.to_json())
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, target)
        finally:
            temporary_path.unlink(missing_ok=True)
        return reservation


def release_resources(*, root: Path = DEFAULT_RESERVATION_ROOT, task: str) -> Reservation:
    """Atomically remove and return an explicit task claim."""

    with _locked(root):
        target = _reservation_path(root, task)
        if not target.is_file():
            raise ReservationError(f"task {task} has no reservation")
        reservation = Reservation.from_path(target)
        target.unlink()
        return reservation


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_RESERVATION_ROOT,
        help=f"shared claim directory (default: {DEFAULT_RESERVATION_ROOT})",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    claim_parser = subparsers.add_parser("claim", help="atomically claim Lean/GPU capacity")
    claim_parser.add_argument("task")
    claim_parser.add_argument("--workers", type=int, default=0)
    claim_parser.add_argument("--lean-rss-gib", type=float, default=0.0)
    claim_parser.add_argument("--gpu", action="store_true")
    claim_parser.add_argument("--pid", type=int, default=os.getpid())
    claim_parser.add_argument("--owner-session", default="unrecorded")

    release_parser = subparsers.add_parser("release", help="release one task claim")
    release_parser.add_argument("task")

    list_parser = subparsers.add_parser("list", help="show all current claims")
    list_parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point used by all worktrees on the shared host."""

    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "claim":
            reservation = claim_resources(
                root=args.root,
                task=args.task,
                lean_workers=args.workers,
                lean_rss_gib=args.lean_rss_gib,
                gpu=args.gpu,
                pid=args.pid,
                owner_session=args.owner_session,
                worktree=Path.cwd(),
            )
            print(reservation.to_json(), end="")
        elif args.command == "release":
            reservation = release_resources(root=args.root, task=args.task)
            print(f"Released {reservation.task}.")
        else:
            reservations = list_reservations(args.root)
            if args.json:
                print(json.dumps([asdict(item) for item in reservations], sort_keys=True))
            elif not reservations:
                print("No active host reservations.")
            else:
                for item in reservations:
                    gpu = "yes" if item.gpu else "no"
                    print(
                        f"{item.task}: workers={item.lean_workers} "
                        f"rss={item.lean_rss_gib:g}GiB gpu={gpu} pid={item.pid} "
                        f"session={item.owner_session}"
                    )
    except ReservationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
