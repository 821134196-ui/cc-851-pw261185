"""Timeline construction and deletion dependency planning."""
from __future__ import annotations

from typing import Any

from .core import BackupChain, iso_ts, parse_ts, segment_id


def parse_segment_seq(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    if text.startswith("seg-"):
        return int(text.split("-", 1)[1])
    raise ValueError(f"invalid segment id: {value!r}")


def _summarize_restore(result: dict[str, Any]) -> dict[str, Any]:
    error = result.get("error") or {}
    return {
        "ok": result.get("ok", False),
        "base_full_id": (result.get("base_full") or {}).get("id"),
        "base_full_seq": (result.get("base_full") or {}).get("seq"),
        "segment_seqs": [s["seq"] for s in result.get("used_segments", [])],
        "segment_ids": [s["id"] for s in result.get("used_segments", [])],
        "last_trusted": result.get("last_trusted"),
        "error": result.get("error"),
        "error_code": error.get("code"),
        "error_segment_seq": error.get("segment_seq"),
        "error_segment_id": error.get("segment_id"),
    }


def _interval_signature(summary: dict[str, Any]) -> tuple[Any, ...]:
    return (
        summary.get("ok"),
        summary.get("base_full_id"),
        tuple(summary.get("segment_ids") or []),
        summary.get("error_code"),
        summary.get("error_segment_id"),
    )


def _interval_dict(start: int, end: int, summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "from": start,
        "to": end,
        "second_count": end - start + 1,
        "from_iso": iso_ts(start),
        "to_iso": iso_ts(end),
        **summary,
    }


def _verified_intervals(
    repo: BackupChain,
    start: int,
    end: int,
    start_result: dict[str, Any] | None = None,
    end_result: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Split a boundary interval if recovery behavior changes inside it.

    Timestamp boundaries normally make each interval stable, but absent or
    deleted segments may not contribute records/timestamps. Checking both ends
    and binary-splitting on a signature mismatch prevents a single good left
    edge from standing in for seconds that actually fail.
    """
    start_summary = _summarize_restore(
        start_result if start_result is not None else repo.restore(start)
    )
    if start >= end:
        return [_interval_dict(start, start, start_summary)]

    end_summary = _summarize_restore(
        end_result if end_result is not None else repo.restore(end)
    )
    if _interval_signature(start_summary) == _interval_signature(end_summary):
        return [_interval_dict(start, end, start_summary)]

    middle = (start + end) // 2
    middle_result = repo.restore(middle)
    left = _verified_intervals(repo, start, middle, start_result, middle_result)
    right = _verified_intervals(repo, middle + 1, end, None, end_result)
    return [*left, *right]


def build_timeline(repo: BackupChain) -> dict[str, Any]:
    """Build second-resolution spans and their recovery dependencies."""
    manifest = repo.manifest()
    inspection = repo.inspect(include_records=True)
    fulls = inspection["fulls"]
    segments = inspection["segments"]
    healthy_fulls = [f for f in fulls if f.get("ok")]

    manifest_summary = {
        "version": manifest.get("version"),
        "active_segment_seq": manifest.get("active_segment_seq"),
        "next_segment_seq": manifest.get("next_segment_seq"),
        "next_lsn": manifest.get("next_lsn"),
        "last_ts": manifest.get("last_ts"),
    }

    if not healthy_fulls:
        return {
            "initialized": True,
            "manifest": manifest_summary,
            "oldest_at": None,
            "newest_at": None,
            "oldest_iso": None,
            "newest_iso": None,
            "seconds": 0,
            "boundaries": [],
            "intervals": [],
            "fulls": fulls,
            "segments": [{k: v for k, v in s.items() if k != "records"} for s in segments],
            "deletions": inspection.get("deletions", []),
            "demo": inspection.get("demo", False),
        }

    oldest_at = min(int(f["ts"]) for f in healthy_fulls)
    timestamps: set[int] = set()
    for full in healthy_fulls:
        ts = int(full["ts"])
        if ts >= oldest_at:
            timestamps.add(ts)

    for segment in segments:
        for record in segment.get("records") or []:
            ts = int(record["ts"])
            if ts >= oldest_at:
                timestamps.add(ts)

    # Missing/deleted/unreadable segments expose no records above. Their
    # manifest range metadata is still needed to place fault-transition
    # boundaries; otherwise one good left-edge sample can paint a false span.
    for descriptor in manifest.get("segments", []):
        for key in ("first_ts", "last_ts"):
            value = descriptor.get(key)
            if value is not None:
                ts = int(value)
                if ts >= oldest_at:
                    timestamps.add(ts)
    for segment in segments:
        for key in ("first_ts", "last_ts"):
            value = segment.get(key)
            if value is not None:
                ts = int(value)
                if ts >= oldest_at:
                    timestamps.add(ts)

    newest_at = max(
        [int(manifest.get("last_ts") or oldest_at), oldest_at, *timestamps]
    )
    timestamps.add(newest_at)
    boundaries = sorted(timestamps)

    intervals: list[dict[str, Any]] = []
    for index, start in enumerate(boundaries):
        end = boundaries[index + 1] - 1 if index + 1 < len(boundaries) else newest_at
        if end < start:
            end = start
        intervals.extend(_verified_intervals(repo, start, end))

    # Merge adjacent intervals with the same health/dependency signature for
    # compact visualization while retaining exact second spans.
    spans: list[dict[str, Any]] = []
    for interval in intervals:
        signature = {
            "ok": interval["ok"],
            "base_full_id": interval["base_full_id"],
            "segment_ids": tuple(interval["segment_ids"]),
            "error_code": (interval.get("error") or {}).get("code"),
            "error_segment_id": interval.get("error_segment_id"),
        }
        if spans:
            previous = spans[-1]
            previous_signature = {
                "ok": previous["ok"],
                "base_full_id": previous["base_full_id"],
                "segment_ids": tuple(previous["segment_ids"]),
                "error_code": (previous.get("error") or {}).get("code"),
                "error_segment_id": previous.get("error_segment_id"),
            }
            if (
                interval["from"] == previous["to"] + 1
                and signature == previous_signature
            ):
                previous["to"] = interval["to"]
                previous["to_iso"] = iso_ts(previous["to"])
                previous["second_count"] = previous["to"] - previous["from"] + 1
                continue
        spans.append({k: v for k, v in interval.items()})

    return {
        "initialized": True,
        "manifest": manifest_summary,
        "oldest_at": oldest_at,
        "newest_at": newest_at,
        "oldest_iso": iso_ts(oldest_at),
        "newest_iso": iso_ts(newest_at),
        "seconds": newest_at - oldest_at + 1,
        "boundaries": [{"at": ts, "iso": iso_ts(ts)} for ts in boundaries],
        "intervals": intervals,
        "spans": spans,
        "fulls": fulls,
        "segments": [{k: v for k, v in s.items() if k != "records"} for s in segments],
        "deletions": inspection.get("deletions", []),
        "demo": inspection.get("demo", False),
    }


def _merge_ranges(ranges: list[dict[str, int]]) -> list[dict[str, Any]]:
    if not ranges:
        return []
    ordered = sorted(ranges, key=lambda r: r["from"])
    merged: list[dict[str, Any]] = []
    for item in ordered:
        start, end = int(item["from"]), int(item["to"])
        if end < start:
            continue
        if not merged or start > merged[-1]["to"] + 1:
            merged.append({"from": start, "to": end, "second_count": end - start + 1})
        else:
            merged[-1]["to"] = max(merged[-1]["to"], end)
            merged[-1]["second_count"] = merged[-1]["to"] - merged[-1]["from"] + 1
    for item in merged:
        item["from_iso"] = iso_ts(item["from"])
        item["to_iso"] = iso_ts(item["to"])
    return merged


def _intersect_range(
    ranges: list[dict[str, Any]], lower: int | None = None, upper: int | None = None
) -> list[dict[str, Any]]:
    result: list[dict[str, int]] = []
    for item in ranges:
        start, end = int(item["from"]), int(item["to"])
        if lower is not None:
            start = max(start, lower)
        if upper is not None:
            end = min(end, upper)
        if start <= end:
            result.append({"from": start, "to": end})
    return _merge_ranges(result)


def structural_dependency_ranges(
    manifest: dict[str, Any], timeline: dict[str, Any], seq: int
) -> list[dict[str, Any]]:
    """Compute dependency seconds from chain topology, not current replay success.

    A failed replay stops at the first fault and therefore cannot be used to
    infer which later segments are structurally required.  Healthy fulls divide
    the timeline into base-image eras; within an era, every segment at or after
    the full's ``start_segment_seq`` is part of the LSN chain once its first
    timestamp has entered the requested recovery second.
    """
    healthy_fulls = [
        full
        for full in timeline.get("fulls", [])
        if full.get("ok") and full.get("ts") is not None
    ]
    if not healthy_fulls or timeline.get("newest_at") is None:
        return []

    # A pathological chain could contain two fulls at the same second; restore
    # chooses the higher LSN/seq, so planning must use that same winner.
    fulls_by_ts: dict[int, dict[str, Any]] = {}
    for full in healthy_fulls:
        ts = int(full["ts"])
        current = fulls_by_ts.get(ts)
        rank = lambda item: (int(item.get("lsn", -1)), int(item.get("seq", 0)))
        if current is None or rank(full) > rank(current):
            fulls_by_ts[ts] = full
    selected_fulls = sorted(fulls_by_ts.items(), key=lambda item: item[0])

    descriptor = next(
        (d for d in manifest.get("segments", []) if int(d["seq"]) == seq),
        None,
    )
    inspection_segment = next(
        (s for s in timeline.get("segments", []) if int(s["seq"]) == seq),
        None,
    )
    range_source = descriptor or inspection_segment or {}
    first_ts = range_source.get("first_ts")
    if first_ts is None and inspection_segment is not None:
        first_ts = inspection_segment.get("first_ts")
    if first_ts is None:
        # A segment with no timestamped records cannot be required by a
        # second-level recovery point.  This is also the provably-empty case.
        return []
    first_ts = int(first_ts)

    raw_ranges: list[dict[str, int]] = []
    newest_at = int(timeline["newest_at"])
    for index, (base_ts, full) in enumerate(selected_fulls):
        next_full_ts: int | None = None
        for later_ts, _later_full in selected_fulls[index + 1 :]:
            if later_ts > base_ts:
                next_full_ts = later_ts
                break
        upper = next_full_ts - 1 if next_full_ts is not None else newest_at
        if upper < base_ts:
            continue

        start_segment_seq = int(full.get("start_segment_seq") or 1)
        if seq < start_segment_seq:
            continue

        last_lsn = range_source.get("last_lsn")
        full_lsn = full.get("lsn")
        if (
            last_lsn is not None
            and full_lsn is not None
            and int(last_lsn) <= int(full_lsn)
        ):
            # The full image already contains every record in this segment.
            continue

        lower = max(base_ts, first_ts)
        if lower <= upper:
            raw_ranges.append({"from": lower, "to": upper})

    return _merge_ranges(raw_ranges)


def deletion_plan(
    repo: BackupChain,
    segment: Any,
    retain_from: Any = None,
    *,
    point_limit: int = 5000,
) -> dict[str, Any]:
    seq = parse_segment_seq(segment)
    manifest = repo.manifest()
    timeline = build_timeline(repo)
    descriptor = next(
        (d for d in manifest.get("segments", []) if int(d["seq"]) == seq),
        None,
    )
    inspection_segment = next(
        (s for s in timeline["segments"] if int(s["seq"]) == seq),
        None,
    )
    if descriptor is None and inspection_segment is None:
        raise FileNotFoundError(f"segment {segment_id(seq)} does not exist")

    ranges = structural_dependency_ranges(manifest, timeline, seq)
    total_points = sum(int(r["second_count"]) for r in ranges)

    points: list[dict[str, Any]] | None = None
    points_truncated = total_points > point_limit
    if not points_truncated:
        points = []
        for item in ranges:
            for at in range(int(item["from"]), int(item["to"]) + 1):
                points.append({"at": at, "iso": iso_ts(at)})

    retain_ts = parse_ts(retain_from) if retain_from not in (None, "") else None
    expired_ranges: list[dict[str, Any]] = []
    retained_ranges: list[dict[str, Any]] = []
    retained_points = total_points
    if retain_ts is not None:
        expired_ranges = _intersect_range(ranges, upper=retain_ts - 1)
        retained_ranges = _intersect_range(ranges, lower=retain_ts)
        retained_points = sum(int(r["second_count"]) for r in retained_ranges)

    active_seq = int(manifest.get("active_segment_seq", -1))
    already_deleted = bool((descriptor or {}).get("deleted"))
    is_active = seq == active_seq
    safe_without_retention = total_points == 0 and not is_active and not already_deleted
    allowed_under_retention = (
        retain_ts is not None
        and retained_points == 0
        and not is_active
        and not already_deleted
    )

    reason_code = "ok"
    if already_deleted:
        reason_code = "already_deleted"
        recommendation = "already deleted"
    elif is_active:
        reason_code = "active_segment"
        recommendation = "blocked: roll the active segment before deleting it"
    elif retain_ts is None and total_points:
        reason_code = "dependents_require_retain_or_force"
        recommendation = "blocked: recovery points depend on this segment; choose retain_from or explicitly force"
    elif retain_ts is not None and retained_points:
        reason_code = "retained_dependents"
        recommendation = "blocked: retained recovery points still depend on this segment"
    elif retain_ts is not None:
        reason_code = "retention_safe"
        recommendation = "deletable under retention: only expired recovery points depend on it"
    elif total_points == 0:
        reason_code = "safe"
        recommendation = "safe to delete: no recovery point depends on it"
    else:
        reason_code = "explicit_acknowledgement_required"
        recommendation = "deletion requires explicit acknowledgement"

    return {
        "segment_seq": seq,
        "segment_id": segment_id(seq),
        "descriptor": descriptor,
        "status": inspection_segment,
        "active": is_active,
        "already_deleted": already_deleted,
        "retain_from": retain_ts,
        "retain_from_iso": iso_ts(retain_ts),
        "dependent_range_count": len(ranges),
        "dependent_point_count": total_points,
        "dependent_ranges": ranges,
        "recovery_points": points,
        "points_truncated": points_truncated,
        "expired_ranges": expired_ranges,
        "retained_dependent_ranges": retained_ranges,
        "retained_dependent_point_count": retained_points if retain_ts is not None else total_points,
        "safe_without_retention": safe_without_retention,
        "delete_allowed_under_retention": allowed_under_retention,
        "reason_code": reason_code,
        "dependency_basis": "structural-full-and-lsn-ranges",
        "recommendation": recommendation,
        "timeline_oldest_at": timeline.get("oldest_at"),
        "timeline_newest_at": timeline.get("newest_at"),
    }


class DeletionBlocked(RuntimeError):
    def __init__(self, plan: dict[str, Any]):
        self.plan = plan
        super().__init__(plan["recommendation"])


def delete_segment(
    repo: BackupChain,
    segment: Any,
    *,
    retain_from: Any = None,
    force: bool = False,
    acknowledge_dependents: bool = False,
    deleted_at: int | None = None,
) -> dict[str, Any]:
    plan = deletion_plan(repo, segment, retain_from)
    if plan["already_deleted"]:
        return {"deleted": False, "already_deleted": True, "plan": plan}
    if plan["active"] and not force:
        raise DeletionBlocked(plan)
    if plan["active"] and force:
        # Even forced web/demo deletion should not remove the append target.
        plan["recommendation"] = "refusing to delete the active append segment; roll it first"
        raise DeletionBlocked(plan)

    if retain_from is None:
        if plan["dependent_point_count"] > 0 and not (force and acknowledge_dependents):
            raise DeletionBlocked(plan)
    elif plan["retained_dependent_point_count"] > 0 and not (
        force and acknowledge_dependents
    ):
        raise DeletionBlocked(plan)

    seq = plan["segment_seq"]
    from .core import now_ts

    ts = now_ts() if deleted_at is None else deleted_at

    with repo.edit() as manifest:
        descriptor = next(
            (d for d in manifest["segments"] if int(d["seq"]) == seq),
            None,
        )
        if descriptor is None:
            # An orphan file discovered by the merged directory scan is not in
            # the manifest. Remove the file, but audit it in deletions rather
            # than pretending there is a descriptor to update.
            orphan = next(
                (
                    d
                    for d in repo._merged_segment_descriptors(manifest)
                    if int(d["seq"]) == seq
                ),
                None,
            )
            if orphan is None:
                raise FileNotFoundError(f"segment {segment_id(seq)} does not exist")
            relative = orphan["file"]
            path = repo.rel_path(relative)
            if path.exists():
                path.unlink()
            hidden_path = path.with_name(path.name + ".hidden")
            if hidden_path.exists():
                hidden_path.unlink()
            record = {
                "segment_seq": seq,
                "segment_id": plan["segment_id"],
                "file": relative,
                "deleted_at": ts,
                "deleted_at_iso": iso_ts(ts),
                "retain_from": plan["retain_from"],
                "dependent_point_count": plan["dependent_point_count"],
                "dependent_ranges": plan["dependent_ranges"],
                "forced": bool(force),
                "unregistered": True,
            }
            manifest.setdefault("deletions", []).append(record)
            return {"deleted": True, "unregistered": True, "record": record, "plan": plan}

        relative = descriptor["file"]
        path = repo.rel_path(relative)
        scan = repo.scan_segment(descriptor, include_records=True)
        empty_verified = scan.get("record_count", 0) == 0 and (
            scan.get("status") == "ok"
            or (
                scan.get("status") == "missing"
                and descriptor.get("first_lsn") is None
                and descriptor.get("first_ts") is None
            )
        )
        if path.exists():
            path.unlink()
        hidden_path = path.with_name(path.name + ".hidden")
        if hidden_path.exists():
            # A fault-injection "hide" may have moved the bytes aside. An
            # explicit segment deletion must remove that copy as well.
            hidden_path.unlink()
        descriptor["deleted"] = True
        descriptor["deleted_at"] = ts
        descriptor["empty_verified"] = empty_verified
        descriptor["deletion_summary"] = {
            "retain_from": plan["retain_from"],
            "dependent_point_count": plan["dependent_point_count"],
            "dependent_ranges": plan["dependent_ranges"],
            "forced": bool(force),
            "empty_verified": empty_verified,
        }
        record = {
            "segment_seq": seq,
            "segment_id": plan["segment_id"],
            "file": relative,
            "deleted_at": ts,
            "deleted_at_iso": iso_ts(ts),
            "retain_from": plan["retain_from"],
            "dependent_point_count": plan["dependent_point_count"],
            "dependent_ranges": plan["dependent_ranges"],
            "forced": bool(force),
            "empty_verified": empty_verified,
        }
        manifest.setdefault("deletions", []).append(record)
        return {"deleted": True, "record": record, "plan": plan}


def prune_plan(repo: BackupChain, retain_from: Any) -> dict[str, Any]:
    retain_ts = parse_ts(retain_from)
    manifest = repo.manifest()
    active_seq = int(manifest.get("active_segment_seq", -1))
    segment_plans: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []

    for descriptor in manifest.get("segments", []):
        seq = int(descriptor["seq"])
        plan = deletion_plan(repo, seq, retain_ts)
        segment_plans.append(plan)
        if (
            seq != active_seq
            and not descriptor.get("deleted")
            and plan["retained_dependent_point_count"] == 0
        ):
            candidates.append(
                {
                    "segment_seq": seq,
                    "segment_id": plan["segment_id"],
                    "dependent_point_count": plan["dependent_point_count"],
                    "dependent_ranges": plan["dependent_ranges"],
                    "expired_ranges": plan["expired_ranges"],
                }
            )

    return {
        "retain_from": retain_ts,
        "retain_from_iso": iso_ts(retain_ts),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "segments": segment_plans,
    }


def apply_prune(repo: BackupChain, retain_from: Any) -> dict[str, Any]:
    planned = prune_plan(repo, retain_from)
    results = []
    for candidate in planned["candidates"]:
        results.append(
            delete_segment(
                repo,
                candidate["segment_seq"],
                retain_from=planned["retain_from"],
            )
        )
    return {
        "prune_plan": planned,
        "deleted": [r["record"] for r in results if r.get("deleted")],
        "results": results,
    }
