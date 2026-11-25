"""Deterministic demo repository used by the timeline page and tests."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .core import BackupChain, now_ts


INITIAL_ROWS = [
    {
        "id": "A-1001",
        "name": "林一",
        "email": "lin.yi@example.com",
        "plan": "free",
        "balance": 100,
        "status": "active",
    },
    {
        "id": "A-1002",
        "name": "周二",
        "email": "zhou.er@example.com",
        "plan": "pro",
        "balance": 240,
        "status": "active",
    },
    {
        "id": "A-1003",
        "name": "陈三",
        "email": "chen.san@example.com",
        "plan": "team",
        "balance": 80,
        "status": "active",
    },
]


def seed_demo(root: str | Path, base_ts: int | None = None) -> BackupChain:
    root = Path(root)
    if root.exists():
        # Reset is explicit through reset_demo; seeding over an existing repo
        # would make demo state non-deterministic.
        if (root / ".backup-chain-demo").exists():
            reset_demo(root, base_ts=base_ts)
            return BackupChain(root)
        raise FileExistsError(f"refusing to seed demo over non-demo directory: {root}")

    t0 = (now_ts() - 12) if base_ts is None else int(base_ts)
    repo = BackupChain.initialize(root, created_at=t0 - 1)
    repo.write_demo_marker()

    repo.create_full(ts=t0, rows=INITIAL_ROWS)

    # Segment 1: LSN 1-3.
    repo.append_change(
        "upsert",
        "A-1001",
        {
            "name": "林一",
            "email": "lin.yi@example.com",
            "plan": "pro",
            "balance": 120,
            "status": "active",
        },
        ts=t0 + 1,
    )
    repo.append_change(
        "upsert",
        "A-1002",
        {
            "name": "周二",
            "email": "zhou.er@example.com",
            "plan": "pro",
            "balance": 260,
            "status": "active",
        },
        ts=t0 + 2,
    )
    repo.append_change(
        "upsert",
        "A-1004",
        {
            "name": "赵敏",
            "email": "zhao.min@example.com",
            "plan": "free",
            "balance": 0,
            "status": "active",
        },
        ts=t0 + 3,
    )

    # Segment 2: LSN 4-6.
    repo.roll_segment()
    repo.append_change("delete", "A-1003", ts=t0 + 4)
    repo.append_change(
        "upsert",
        "A-1004",
        {
            "name": "赵敏",
            "email": "zhao.min@example.com",
            "plan": "pro",
            "balance": 40,
            "status": "active",
        },
        ts=t0 + 5,
    )
    repo.append_change(
        "upsert",
        "A-1002",
        {
            "name": "周二",
            "email": "zhou.er@example.com",
            "plan": "team",
            "balance": 320,
            "status": "active",
        },
        ts=t0 + 6,
    )

    # Full 2 closes segment 2 and starts recovery dependency on segment 3.
    repo.create_full(ts=t0 + 7)

    # Segment 3: LSN 7-9.
    repo.append_change(
        "upsert",
        "A-1001",
        {
            "name": "林一",
            "email": "lin.yi@example.com",
            "plan": "enterprise",
            "balance": 180,
            "status": "active",
        },
        ts=t0 + 8,
    )
    repo.append_change(
        "upsert",
        "A-1005",
        {
            "name": "孙琦",
            "email": "sun.qi@example.com",
            "plan": "team",
            "balance": 500,
            "status": "active",
        },
        ts=t0 + 9,
    )
    repo.append_change(
        "upsert",
        "A-1005",
        {
            "name": "孙琦",
            "email": "sun.qi@example.com",
            "plan": "team",
            "balance": 480,
            "status": "active",
        },
        ts=t0 + 10,
    )
    return repo


def reset_demo(root: str | Path, base_ts: int | None = None) -> BackupChain:
    root = Path(root)
    marker = root / ".backup-chain-demo"
    if root.exists() and not marker.exists():
        raise PermissionError("refusing to reset a directory without demo marker")
    if root.exists():
        shutil.rmtree(root)
    return seed_demo(root, base_ts=base_ts)
