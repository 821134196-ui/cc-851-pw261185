"""Command-line interface for the backup-chain tool."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .core import BackupChain, ChainFault, iso_ts
from .demo import seed_demo
from .planner import (
    DeletionBlocked,
    apply_prune,
    build_timeline,
    delete_segment,
    deletion_plan,
    prune_plan,
)
from .server import serve


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def load_row_json(args: argparse.Namespace) -> dict[str, Any] | None:
    if args.json is not None:
        value = json.loads(args.json)
    elif args.file is not None:
        value = json.loads(Path(args.file).read_text(encoding="utf-8"))
    else:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("row JSON must be an object")
    return value


def print_restore(result: dict[str, Any]) -> None:
    requested = result["requested_iso"]
    print(f"Requested recovery point: {requested} ({result['requested_at']})")
    base = result.get("base_full")
    if base:
        print(f"Base full backup: {base['id']} at {base.get('iso')} (lsn={base['lsn']})")
    trusted = result.get("last_trusted")
    if trusted:
        print(
            "Last trustworthy point: "
            f"{trusted.get('iso')} / {trusted.get('label')} (at={trusted.get('at')}, lsn={trusted.get('lsn')})"
        )
    if result.get("used_segments"):
        print("Replayed segments: " + ", ".join(s["id"] for s in result["used_segments"]))
    if result.get("ok"):
        print(f"Status: OK — recovered {result['row_count']} row(s), applied {result['applied_records']} change(s)")
    else:
        err = result.get("error") or {}
        print(f"Status: STOPPED — [{err.get('code')}] {err.get('message')}")
        print("Rows below are the verified prefix at the last trustworthy point.")

    rows = result.get("rows") or []
    if not rows:
        print("(no rows)")
        return
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    print("\n" + "\t".join(columns))
    for row in rows:
        print("\t".join(json.dumps(row.get(col, ""), ensure_ascii=False) for col in columns))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="backup-chain", description="Minimal full + appended-segment backup chain")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="initialize a repository")
    p_init.add_argument("path")
    p_init.add_argument("--demo", action="store_true", help="seed a small demo timeline")
    p_init.add_argument("--base-ts", type=int, default=None)

    p_full = sub.add_parser("full", help="create a full backup")
    p_full.add_argument("path")
    p_full.add_argument("--at", default=None)
    p_full.add_argument("--rows-file", default=None, help="initial full image JSON (first full only)")

    p_put = sub.add_parser("put", help="append an upsert change")
    p_put.add_argument("path")
    p_put.add_argument("id")
    p_put.add_argument("--json", default=None)
    p_put.add_argument("--file", default=None)
    p_put.add_argument("--at", default=None)

    p_delete_row = sub.add_parser("delete-row", help="append a row deletion")
    p_delete_row.add_argument("path")
    p_delete_row.add_argument("id")
    p_delete_row.add_argument("--at", default=None)

    p_roll = sub.add_parser("roll", help="seal current segment and create next one")
    p_roll.add_argument("path")

    p_timeline = sub.add_parser("timeline", help="show second-level recovery timeline")
    p_timeline.add_argument("path")
    p_timeline.add_argument("--json", action="store_true")

    p_restore = sub.add_parser("restore", help="restore to a second-level point")
    p_restore.add_argument("path")
    p_restore.add_argument("--at", required=True, help="epoch seconds or ISO timestamp")
    p_restore.add_argument("--json", action="store_true")

    p_plan = sub.add_parser("plan-deletion", help="calculate recovery points depending on a segment")
    p_plan.add_argument("path")
    p_plan.add_argument("segment")
    p_plan.add_argument("--retain-from", default=None)
    p_plan.add_argument("--json", action="store_true")

    p_delete = sub.add_parser("delete-segment", help="plan then optionally delete a segment")
    p_delete.add_argument("path")
    p_delete.add_argument("segment")
    p_delete.add_argument("--retain-from", default=None)
    p_delete.add_argument("--force", action="store_true")
    p_delete.add_argument("--acknowledge-dependent-points", action="store_true")
    p_delete.add_argument("--apply", action="store_true")
    p_delete.add_argument("--json", action="store_true")

    p_prune = sub.add_parser("prune", help="delete segments not needed by retained recovery points")
    p_prune.add_argument("path")
    p_prune.add_argument("--retain-from", required=True)
    p_prune.add_argument("--apply", action="store_true")
    p_prune.add_argument("--json", action="store_true")

    p_serve = sub.add_parser("serve", help="run timeline web page and JSON API")
    p_serve.add_argument("path")
    p_serve.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    p_serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    p_serve.add_argument("--demo", action="store_true", help="seed demo data if path is uninitialized")

    p_corrupt = sub.add_parser("corrupt", help="demo/test fault: append a bad record")
    p_corrupt.add_argument("path")
    p_corrupt.add_argument("--segment", type=int, default=None)
    p_corrupt.add_argument("--mode", choices=["checksum", "garbage"], default="checksum")

    p_hide = sub.add_parser("hide-segment", help="demo/test fault: move segment file aside")
    p_hide.add_argument("path")
    p_hide.add_argument("segment", type=int)

    p_unhide = sub.add_parser("unhide-segment", help="restore a hidden segment file")
    p_unhide.add_argument("path")
    p_unhide.add_argument("segment", type=int)

    p_overlap = sub.add_parser("make-overlap", help="demo/test fault: overwrite target with source bytes")
    p_overlap.add_argument("path")
    p_overlap.add_argument("--target", type=int, required=True)
    p_overlap.add_argument("--source", type=int, required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            if args.demo:
                repo = seed_demo(args.path, base_ts=args.base_ts)
            else:
                repo = BackupChain.initialize(args.path, created_at=args.base_ts)
            print_json({"ok": True, "repo": str(repo.root), "demo": args.demo})
            return 0

        repo = BackupChain(args.path)
        if args.command == "serve" and getattr(args, "demo", False) and not repo.manifest_path.exists():
            seed_demo(args.path)
        repo.require_initialized()

        if args.command == "full":
            rows = None
            if args.rows_file:
                rows = json.loads(Path(args.rows_file).read_text(encoding="utf-8"))
            descriptor = repo.create_full(ts=args.at, rows=rows)
            print_json({"ok": True, "full": descriptor})
        elif args.command == "put":
            row = load_row_json(args)
            record = repo.append_change("upsert", args.id, row, ts=args.at)
            print_json({"ok": True, "record": record})
        elif args.command == "delete-row":
            record = repo.append_change("delete", args.id, ts=args.at)
            print_json({"ok": True, "record": record})
        elif args.command == "roll":
            print_json({"ok": True, "segment": repo.roll_segment()})
        elif args.command == "timeline":
            timeline = build_timeline(repo)
            if args.json:
                print_json({"ok": True, "timeline": timeline})
            else:
                print(f"Timeline: {timeline['oldest_iso']} -> {timeline['newest_iso']} ({timeline['seconds']} seconds)")
                for span in timeline["spans"]:
                    state = "OK" if span["ok"] else "BLOCKED"
                    segs = ",".join(span["segment_ids"]) or "-"
                    print(f"{span['from_iso']} .. {span['to_iso']} [{state}] base={span['base_full_id']} segments={segs}")
        elif args.command == "restore":
            result = repo.restore(args.at)
            if args.json:
                print_json({"ok": result["ok"], "result": result})
            else:
                print_restore(result)
            return 0 if result["ok"] else 4
        elif args.command == "plan-deletion":
            plan = deletion_plan(repo, args.segment, args.retain_from)
            if args.json:
                print_json({"ok": True, "plan": plan})
            else:
                print(f"{plan['segment_id']}: {plan['dependent_point_count']} dependent recovery point(s) in {plan['dependent_range_count']} range(s)")
                for item in plan["dependent_ranges"]:
                    print(f"  {item['from_iso']} .. {item['to_iso']} ({item['second_count']}s)")
                print(plan["recommendation"])
        elif args.command == "delete-segment":
            if not args.apply:
                plan = deletion_plan(repo, args.segment, args.retain_from)
                print_json({"dry_run": True, "apply_with": "rerun with --apply", "plan": plan})
                return 0
            try:
                result = delete_segment(
                    repo,
                    args.segment,
                    retain_from=args.retain_from,
                    force=args.force,
                    acknowledge_dependents=args.acknowledge_dependent_points,
                )
            except DeletionBlocked as exc:
                print_json({"ok": False, "blocked": True, "plan": exc.plan})
                return 3
            print_json({"ok": True, "deletion": result})
        elif args.command == "prune":
            if args.apply:
                result = apply_prune(repo, args.retain_from)
                print_json({"ok": True, **result})
            else:
                print_json({"ok": True, "dry_run": True, "plan": prune_plan(repo, args.retain_from)})
        elif args.command == "serve":
            serve(args.path, host=args.host, port=args.port)
        elif args.command == "corrupt":
            print_json({"ok": True, "segment": repo.inject_corruption(args.segment, args.mode)})
        elif args.command == "hide-segment":
            print_json({"ok": True, "result": repo.hide_segment(args.segment)})
        elif args.command == "unhide-segment":
            print_json({"ok": True, "result": repo.unhide_segment(args.segment)})
        elif args.command == "make-overlap":
            print_json({"ok": True, "result": repo.inject_overlap(args.target, args.source)})
        return 0
    except DeletionBlocked as exc:
        print_json({"ok": False, "blocked": True, "plan": exc.plan})
        return 3
    except ChainFault as exc:
        print_json({"ok": False, "error": exc.to_dict()})
        return 2
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
