"""Core backup-chain storage, verification, and point-in-time recovery."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX hosts
    fcntl = None  # type: ignore[assignment]


MANIFEST_NAME = "manifest.json"
SEGMENT_RE = re.compile(r"^seg-(\d+)\.jsonl$")
FULL_RE = re.compile(r"^full-(\d+)\.json$")
DEMO_MARKER = ".backup-chain-demo"


def now_ts() -> int:
    return int(time.time())


def iso_ts(ts: int | float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def parse_ts(value: Any) -> int:
    """Parse an epoch second or ISO-8601 timestamp."""
    if value is None:
        raise ValueError("timestamp is required")
    if isinstance(value, bool):
        raise ValueError("invalid timestamp")
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if text == "":
        raise ValueError("timestamp is required")
    if re.fullmatch(r"-?\d+(\.\d+)?", text):
        return int(float(text))
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        # Date/time inputs without an offset are interpreted in the server's
        # local timezone, matching datetime-local form controls.
        dt = dt.astimezone()
    return int(dt.timestamp())


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def hash_object(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".tmp-", suffix=path.suffix or ".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


def normalize_rows(rows: Any) -> dict[str, dict[str, Any]]:
    if rows is None:
        return {}
    if isinstance(rows, dict):
        # Accept either {id: row} or a JSON object whose values are rows.
        items = list(rows.values()) if all(isinstance(v, dict) for v in rows.values()) else [rows]
    elif isinstance(rows, list):
        items = rows
    else:
        raise ValueError("rows must be a list or object")

    normalized: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("each row must be an object")
        raw_id = item.get("id", item.get("_id"))
        if raw_id is None or str(raw_id) == "":
            raise ValueError("each row requires a string id")
        row = dict(item)
        row["id"] = str(raw_id)
        normalized[row["id"]] = row
    return normalized


def segment_id(seq: int) -> str:
    return f"seg-{seq:06d}"


def full_id(seq: int) -> str:
    return f"full-{seq:06d}"


class BackupChain:
    """A directory containing full backups and ordered JSONL change segments."""

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root).expanduser().resolve()
        self.manifest_path = self.root / MANIFEST_NAME
        self._thread_lock = threading.RLock()

    # ---------- repository lifecycle ----------

    @classmethod
    def initialize(
        cls, root: str | os.PathLike[str], created_at: int | None = None
    ) -> "BackupChain":
        repo = cls(root)
        repo.root.mkdir(parents=True, exist_ok=True)
        (repo.root / "fulls").mkdir(exist_ok=True)
        (repo.root / "segments").mkdir(exist_ok=True)
        if repo.manifest_path.exists():
            return repo

        ts = created_at if created_at is not None else now_ts()
        first_seq = 1
        descriptor = {
            "seq": first_seq,
            "id": segment_id(first_seq),
            "file": f"segments/{segment_id(first_seq)}.jsonl",
            "created_at": ts,
            "sealed": False,
            "deleted": False,
        }
        (repo.root / descriptor["file"]).touch()
        manifest = {
            "version": 1,
            "created_at": ts,
            "last_ts": 0,
            "next_lsn": 1,
            "next_segment_seq": first_seq + 1,
            "next_full_seq": 1,
            "active_segment_seq": first_seq,
            "segments": [descriptor],
            "fulls": [],
            "deletions": [],
        }
        atomic_write_json(repo.manifest_path, manifest)
        return repo

    def require_initialized(self) -> None:
        if not self.manifest_path.is_file():
            raise FileNotFoundError(
                f"backup repository is not initialized: {self.manifest_path} not found"
            )

    def _read_manifest(self) -> dict[str, Any]:
        self.require_initialized()
        with open(self.manifest_path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        manifest.setdefault("segments", [])
        manifest.setdefault("fulls", [])
        manifest.setdefault("deletions", [])
        return manifest

    def _write_manifest(self, manifest: dict[str, Any]) -> None:
        atomic_write_json(self.manifest_path, manifest)

    @contextlib.contextmanager
    def edit(self) -> Iterator[dict[str, Any]]:
        with self._thread_lock:
            self.root.mkdir(parents=True, exist_ok=True)
            lock_handle = open(self.root / ".lock", "a+", encoding="utf-8")
            try:
                if fcntl is not None:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                manifest = self._read_manifest()
                yield manifest
                self._write_manifest(manifest)
            finally:
                if fcntl is not None:
                    with contextlib.suppress(Exception):
                        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                lock_handle.close()

    def manifest(self) -> dict[str, Any]:
        with self._thread_lock:
            return self._read_manifest()

    # ---------- paths and descriptors ----------

    def rel_path(self, relative: str) -> Path:
        # Descriptors are generated by us; resolve and constrain anyway.
        path = (self.root / relative).resolve()
        if self.root not in path.parents and path != self.root:
            raise ValueError(f"path escapes repository: {relative}")
        return path

    def _merged_segment_descriptors(
        self, manifest: dict[str, Any]
    ) -> list[dict[str, Any]]:
        by_seq: dict[int, dict[str, Any]] = {}
        for descriptor in manifest.get("segments", []):
            item = dict(descriptor)
            item.setdefault("deleted", False)
            item.setdefault("sealed", False)
            by_seq[int(item["seq"])] = item

        segment_dir = self.root / "segments"
        if segment_dir.is_dir():
            for path in segment_dir.iterdir():
                match = SEGMENT_RE.match(path.name)
                if not match or not path.is_file():
                    continue
                seq = int(match.group(1))
                if seq not in by_seq:
                    by_seq[seq] = {
                        "seq": seq,
                        "id": segment_id(seq),
                        "file": f"segments/{path.name}",
                        "created_at": int(path.stat().st_mtime),
                        "sealed": True,
                        "deleted": False,
                        "unregistered": True,
                    }
        return [by_seq[seq] for seq in sorted(by_seq)]

    # ---------- writing changes and full backups ----------

    def append_change(
        self,
        op: str,
        row_id: str,
        row: dict[str, Any] | None = None,
        ts: int | None = None,
    ) -> dict[str, Any]:
        if op not in {"upsert", "delete"}:
            raise ValueError("op must be upsert or delete")
        row_id = str(row_id)
        if not row_id:
            raise ValueError("row id is required")
        if op == "upsert":
            if not isinstance(row, dict):
                raise ValueError("upsert requires a row object")
            row = dict(row)
            row["id"] = row_id
        else:
            row = None

        event_ts = now_ts() if ts is None else parse_ts(ts)
        with self.edit() as manifest:
            if not manifest.get("fulls"):
                raise ChainFault(
                    "NO_FULL_BACKUP",
                    "cannot append changes before an initial full backup",
                )
            last_ts = int(manifest.get("last_ts") or 0)
            if last_ts and event_ts < last_ts:
                raise ChainFault(
                    "NON_MONOTONIC_TIMESTAMP",
                    f"change timestamp {event_ts} is earlier than repository last_ts {last_ts}",
                    event_ts=event_ts,
                    last_ts=last_ts,
                )

            active_seq = int(manifest["active_segment_seq"])
            descriptor = next(
                (d for d in manifest["segments"] if int(d["seq"]) == active_seq), None
            )
            if descriptor is None or descriptor.get("deleted"):
                raise ChainFault(
                    "ACTIVE_SEGMENT_UNAVAILABLE",
                    f"active segment {segment_id(active_seq)} is missing or deleted",
                )

            lsn = int(manifest["next_lsn"])
            body = {
                "lsn": lsn,
                "ts": event_ts,
                "op": op,
                "id": row_id,
                "row": row,
            }
            record = {**body, "sha256": hash_object(body)}
            path = self.rel_path(descriptor["file"])
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())

            manifest["next_lsn"] = lsn + 1
            manifest["last_ts"] = event_ts
            if descriptor.get("first_lsn") is None:
                descriptor["first_lsn"] = lsn
                descriptor["first_ts"] = event_ts
            descriptor["last_lsn"] = lsn
            descriptor["last_ts"] = event_ts
            return record

    def roll_segment(self, ts: int | None = None) -> dict[str, Any]:
        with self.edit() as manifest:
            active_seq = int(manifest["active_segment_seq"])
            active = next(
                d for d in manifest["segments"] if int(d["seq"]) == active_seq
            )
            active["sealed"] = True
            seq = int(manifest["next_segment_seq"])
            descriptor = {
                "seq": seq,
                "id": segment_id(seq),
                "file": f"segments/{segment_id(seq)}.jsonl",
                "created_at": now_ts() if ts is None else parse_ts(ts),
                "sealed": False,
                "deleted": False,
            }
            self.rel_path(descriptor["file"]).touch()
            manifest["segments"].append(descriptor)
            manifest["next_segment_seq"] = seq + 1
            manifest["active_segment_seq"] = seq
            return dict(descriptor)

    def _roll_if_nonempty(self, manifest: dict[str, Any]) -> int:
        active_seq = int(manifest["active_segment_seq"])
        active = next(
            d for d in manifest["segments"] if int(d["seq"]) == active_seq
        )
        path = self.rel_path(active["file"])
        nonempty = path.exists() and path.stat().st_size > 0
        if not nonempty:
            return active_seq

        active["sealed"] = True
        seq = int(manifest["next_segment_seq"])
        descriptor = {
            "seq": seq,
            "id": segment_id(seq),
            "file": f"segments/{segment_id(seq)}.jsonl",
            "created_at": now_ts(),
            "sealed": False,
            "deleted": False,
        }
        path = self.rel_path(descriptor["file"])
        path.touch()
        manifest["segments"].append(descriptor)
        manifest["next_segment_seq"] = seq + 1
        manifest["active_segment_seq"] = seq
        return seq

    def create_full(
        self,
        ts: int | str | None = None,
        rows: Any = None,
    ) -> dict[str, Any]:
        full_ts = now_ts() if ts is None else parse_ts(ts)
        with self.edit() as manifest:
            last_ts = int(manifest.get("last_ts") or 0)
            if manifest.get("fulls") and last_ts and full_ts < last_ts:
                raise ChainFault(
                    "NON_MONOTONIC_TIMESTAMP",
                    f"full timestamp {full_ts} is earlier than repository last_ts {last_ts}",
                )

            if not manifest.get("fulls"):
                # An explicitly supplied initial image is allowed only for the
                # first full. Later fulls must be derived from the trusted chain.
                rows_dict = normalize_rows({} if rows is None else rows)
            elif rows is not None:
                raise ValueError(
                    "rows can only be supplied when creating the first full backup"
                )
            else:
                restored = self.restore(at=int(manifest.get("last_ts") or full_ts))
                if not restored.get("ok"):
                    raise ChainFault(
                        "UNTRUSTED_CHAIN",
                        "refusing to create full from an untrusted recovery point",
                        recovery=restored,
                    )
                rows_dict = normalize_rows(restored["rows"])

            lsn = int(manifest["next_lsn"]) - 1
            seq = int(manifest["next_full_seq"])
            full_name = full_id(seq)
            relative = f"fulls/{full_name}.json"
            payload = {
                "kind": "full",
                "seq": seq,
                "id": full_name,
                "ts": full_ts,
                "lsn": lsn,
                "rows": rows_dict,
            }
            envelope = {**payload, "sha256": hash_object(payload)}
            atomic_write_json(self.rel_path(relative), envelope)

            start_segment_seq = self._roll_if_nonempty(manifest)
            descriptor = {
                "seq": seq,
                "id": full_name,
                "file": relative,
                "ts": full_ts,
                "lsn": lsn,
                "start_segment_seq": start_segment_seq,
                "sha256": envelope["sha256"],
            }
            manifest["fulls"].append(descriptor)
            manifest["next_full_seq"] = seq + 1
            manifest["last_ts"] = max(last_ts, full_ts)
            return dict(descriptor)

    # ---------- verification ----------

    def _read_full(
        self, descriptor: dict[str, Any], include_rows: bool = True
    ) -> dict[str, Any]:
        report: dict[str, Any] = {
            **{k: v for k, v in descriptor.items() if k != "sha256"},
            "status": "unknown",
            "exists": False,
            "ok": False,
        }
        path = self.rel_path(descriptor["file"])
        if not path.is_file():
            report.update(
                status="missing",
                error={"code": "MISSING_FULL", "message": f"full file is missing: {descriptor['file']}"},
            )
            return report

        report["exists"] = True
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            report.update(
                status="corrupt",
                error={"code": "CORRUPT_FULL", "message": f"full backup is unreadable: {exc}"},
            )
            return report

        checksum = data.pop("sha256", None)
        expected = hash_object(data)
        if not isinstance(checksum, str) or checksum != expected:
            report.update(
                status="corrupt",
                error={"code": "CHECKSUM_MISMATCH", "message": "full backup checksum does not match"},
            )
            return report

        rows = normalize_rows(data.get("rows", {}))
        report.update(
            status="ok",
            ok=True,
            ts=int(data.get("ts", descriptor.get("ts", 0))),
            lsn=int(data.get("lsn", descriptor.get("lsn", 0))),
            start_segment_seq=int(data.get("start_segment_seq", descriptor.get("start_segment_seq", 1))),
            row_count=len(rows),
            sha256=checksum,
        )
        if include_rows:
            report["rows_dict"] = rows
        return report

    def scan_segment(
        self, descriptor: dict[str, Any], include_records: bool = True
    ) -> dict[str, Any]:
        seq = int(descriptor["seq"])
        report: dict[str, Any] = {
            "seq": seq,
            "id": descriptor.get("id", segment_id(seq)),
            "file": descriptor.get("file", f"segments/{segment_id(seq)}.jsonl"),
            "sealed": bool(descriptor.get("sealed")),
            "deleted": bool(descriptor.get("deleted")),
            "unregistered": bool(descriptor.get("unregistered")),
            "exists": False,
            "status": "unknown",
            "record_count": 0,
            "first_lsn": descriptor.get("first_lsn"),
            "last_lsn": descriptor.get("last_lsn"),
            "first_ts": descriptor.get("first_ts"),
            "last_ts": descriptor.get("last_ts"),
            "records": [] if include_records else None,
            "error": None,
        }
        path = self.rel_path(report["file"])
        if descriptor.get("deleted") and not path.exists():
            report["status"] = "deleted"
            report["error"] = {
                "code": "SEGMENT_DELETED",
                "message": f"{report['id']} was intentionally deleted after dependency planning",
            }
            return report
        if not path.is_file():
            report["status"] = "missing"
            report["error"] = {
                "code": "MISSING_SEGMENT",
                "message": f"segment file is missing: {report['file']}",
            }
            return report

        report["exists"] = True
        if descriptor.get("deleted"):
            report["status"] = "deleted_but_present"

        records: list[dict[str, Any]] = []

        def corruption(code: str, message: str, line_no: int | None) -> None:
            last = records[-1] if records else None
            report["status"] = "corrupt"
            report["error"] = {
                "code": code,
                "message": message,
                "line": line_no,
                "last_trusted_lsn": last["lsn"] if last else None,
                "last_trusted_ts": last["ts"] if last else None,
            }

        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line_no, raw_line in enumerate(handle, 1):
                    if not raw_line.strip():
                        corruption("EMPTY_LINE", "segment contains an empty/corrupt line", line_no)
                        break
                    try:
                        obj = json.loads(raw_line)
                    except json.JSONDecodeError as exc:
                        corruption("INVALID_JSON", f"invalid JSON on line {line_no}: {exc.msg}", line_no)
                        break
                    if not isinstance(obj, dict):
                        corruption("INVALID_RECORD", f"record on line {line_no} is not an object", line_no)
                        break

                    checksum = obj.pop("sha256", None)
                    expected = hash_object(obj)
                    if not isinstance(checksum, str) or checksum != expected:
                        corruption(
                            "CHECKSUM_MISMATCH",
                            f"checksum verification failed at line {line_no}; segment is corrupt",
                            line_no,
                        )
                        break

                    lsn = obj.get("lsn")
                    event_ts = obj.get("ts")
                    if (
                        not isinstance(lsn, int)
                        or isinstance(lsn, bool)
                        or lsn <= 0
                        or not isinstance(event_ts, int)
                        or isinstance(event_ts, bool)
                    ):
                        corruption("INVALID_RECORD", f"record on line {line_no} has invalid lsn/ts", line_no)
                        break
                    if obj.get("op") not in {"upsert", "delete"} or not obj.get("id"):
                        corruption("INVALID_RECORD", f"record on line {line_no} has invalid op/id", line_no)
                        break
                    if obj["op"] == "upsert" and not isinstance(obj.get("row"), dict):
                        corruption("INVALID_RECORD", f"upsert on line {line_no} has no row object", line_no)
                        break

                    records.append(obj)
        except (UnicodeDecodeError, OSError) as exc:
            corruption("UNREADABLE_SEGMENT", f"segment cannot be decoded: {exc}", None)

        if report["status"] == "unknown":
            report["status"] = "ok" if not descriptor.get("deleted") else "deleted_but_present"

        if records:
            report["record_count"] = len(records)
            report["first_lsn"] = records[0]["lsn"]
            report["last_lsn"] = records[-1]["lsn"]
            report["first_ts"] = records[0]["ts"]
            report["last_ts"] = records[-1]["ts"]
        if include_records:
            report["records"] = records
        else:
            report["records"] = None
        return report

    def inspect(self, include_records: bool = False) -> dict[str, Any]:
        manifest = self._read_manifest()
        segments = [
            self.scan_segment(d, include_records=include_records)
            for d in self._merged_segment_descriptors(manifest)
        ]
        fulls = [self._read_full(d, include_rows=False) for d in manifest.get("fulls", [])]
        return {
            "root": str(self.root),
            "manifest": {
                "version": manifest.get("version"),
                "created_at": manifest.get("created_at"),
                "created_at_iso": iso_ts(manifest.get("created_at")),
                "last_ts": manifest.get("last_ts"),
                "last_ts_iso": iso_ts(manifest.get("last_ts")),
                "next_lsn": manifest.get("next_lsn"),
                "active_segment_seq": manifest.get("active_segment_seq"),
            },
            "segments": segments,
            "fulls": fulls,
            "deletions": manifest.get("deletions", []),
            "demo": (self.root / DEMO_MARKER).exists(),
        }

    # ---------- recovery ----------

    def restore(self, at: int | str | None = None) -> dict[str, Any]:
        manifest = self._read_manifest()
        if at is None or at == "latest":
            target_ts = int(manifest.get("last_ts") or now_ts())
        else:
            target_ts = parse_ts(at)

        base_result: dict[str, Any] = {
            "ok": True,
            "requested_at": target_ts,
            "requested_iso": iso_ts(target_ts),
            "base_full": None,
            "used_segments": [],
            "rows": [],
            "row_count": 0,
            "applied_records": 0,
            "last_trusted": None,
            "warnings": [],
        }

        full_reports = [
            self._read_full(descriptor, include_rows=True)
            for descriptor in manifest.get("fulls", [])
        ]
        eligible = [
            f for f in full_reports if f.get("ok") and int(f.get("ts", 0)) <= target_ts
        ]
        if not eligible:
            corrupt = [f for f in full_reports if f.get("status") == "corrupt"]
            if corrupt:
                error = {
                    "code": "CORRUPT_FULL",
                    "message": "no usable full backup at or before the requested time",
                    "full_id": corrupt[0].get("id"),
                    "detail": corrupt[0].get("error"),
                }
            elif full_reports:
                error = {
                    "code": "FULL_IS_IN_THE_FUTURE",
                    "message": "the first full backup is after the requested recovery second",
                    "first_full_ts": min(int(f["ts"]) for f in full_reports if "ts" in f),
                }
            else:
                error = {
                    "code": "NO_FULL_BACKUP",
                    "message": "repository has no full backup",
                }
            base_result.update(ok=False, error=error)
            return base_result

        base = max(eligible, key=lambda f: (int(f.get("lsn", -1)), int(f.get("seq", 0))))
        rows: dict[str, dict[str, Any]] = dict(base["rows_dict"])
        last_trusted: dict[str, Any] = {
            "kind": "full",
            "id": base["id"],
            "at": int(base["ts"]),
            "lsn": int(base["lsn"]),
            "label": f"{base['id']} 完整备份",
        }
        base_result["base_full"] = {
            "seq": base.get("seq"),
            "id": base.get("id"),
            "ts": base.get("ts"),
            "iso": iso_ts(base.get("ts")),
            "lsn": base.get("lsn"),
            "start_segment_seq": base.get("start_segment_seq", 1),
            "row_count": base.get("row_count"),
        }
        base_result["last_trusted"] = {**last_trusted, "iso": iso_ts(last_trusted["at"])}

        def finalize(ok: bool = True) -> dict[str, Any]:
            ordered = [rows[key] for key in sorted(rows)]
            base_result["rows"] = ordered
            base_result["row_count"] = len(ordered)
            if last_trusted:
                base_result["last_trusted"] = {
                    **last_trusted,
                    "iso": iso_ts(last_trusted.get("at")),
                }
            base_result["ok"] = ok
            return base_result

        def fail(
            code: str,
            message: str,
            *,
            segment: dict[str, Any] | None = None,
            extra: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            error: dict[str, Any] = {"code": code, "message": message}
            if segment:
                error["segment_id"] = segment.get("id")
                error["segment_seq"] = segment.get("seq")
                error["segment_file"] = segment.get("file")
            if extra:
                error.update(extra)
            base_result["error"] = error
            base_result["ok"] = False
            # Rows currently in `rows` are exactly the verified prefix ending at
            # base_result.last_trusted. Expose them so the UI can show the last
            # trustworthy actual rows rather than only a failure badge.
            return finalize(ok=False)

        expected_lsn = int(base["lsn"]) + 1
        expected_seq = int(base.get("start_segment_seq", 1))
        descriptors = self._merged_segment_descriptors(manifest)

        for descriptor in descriptors:
            seq = int(descriptor["seq"])
            if seq < expected_seq:
                continue
            if seq > expected_seq:
                missing = segment_id(expected_seq)
                return fail(
                    "MISSING_SEGMENT",
                    f"chain gap: expected {missing}, but next segment is {descriptor.get('id')}",
                    extra={
                        "missing_segment_seq": expected_seq,
                        "missing_segment_id": missing,
                        "next_segment_id": descriptor.get("id"),
                    },
                )

            # For an absent segment, manifest's first timestamp can prove it
            # cannot be required for an earlier recovery second. If the file is
            # present, inspect its actual records instead: a deliberately
            # overlapped file may no longer match manifest range metadata.
            first_ts = descriptor.get("first_ts")
            descriptor_path = self.rel_path(descriptor["file"])
            segment_file_exists = descriptor_path.is_file()
            if (
                not segment_file_exists
                and first_ts is not None
                and target_ts < int(first_ts)
            ):
                return finalize(ok=True)

            if descriptor.get("unregistered"):
                return fail(
                    "UNREGISTERED_SEGMENT",
                    f"segment {descriptor.get('id')} exists on disk but is not in manifest; refusing ambiguous chain",
                    segment=descriptor,
                )

            scan = self.scan_segment(descriptor, include_records=True)
            if scan["status"] == "missing":
                if descriptor.get("first_lsn") is None:
                    # A segment created by roll() but never appended to carries
                    # no LSN/time range. Its loss cannot change recovered rows.
                    base_result["warnings"].append(
                        f"{scan['id']} is missing but was empty; skipped"
                    )
                    expected_seq = seq + 1
                    continue
                return fail(
                    "MISSING_SEGMENT",
                    scan["error"]["message"],
                    segment=descriptor,
                )
            if scan["status"] == "deleted":
                if descriptor.get("empty_verified") is True:
                    base_result["warnings"].append(
                        f"{scan['id']} was deleted after verifying that it contained no records; skipped"
                    )
                    expected_seq = seq + 1
                    continue
                return fail(
                    "SEGMENT_DELETED",
                    scan["error"]["message"],
                    segment=descriptor,
                )

            segment_used = False
            for record in scan.get("records") or []:
                event_ts = int(record["ts"])
                if event_ts > target_ts:
                    # Chronological stream: this and later records are after
                    # the requested second, so the target is already complete.
                    return finalize(ok=True)

                lsn = int(record["lsn"])
                if lsn < expected_lsn:
                    return fail(
                        "OVERLAPPING_SEGMENT",
                        (
                            f"overlap/duplicate LSN in {scan['id']}: got lsn {lsn}, "
                            f"expected {expected_lsn}; recovery stopped before ambiguous writes"
                        ),
                        segment=scan,
                        extra={
                            "record_lsn": lsn,
                            "expected_lsn": expected_lsn,
                            "at_record_ts": event_ts,
                        },
                    )
                if lsn > expected_lsn:
                    return fail(
                        "MISSING_SEGMENT",
                        (
                            f"LSN gap before {scan['id']}: got lsn {lsn}, "
                            f"expected {expected_lsn}; one or more change records/segments are missing"
                        ),
                        segment=scan,
                        extra={
                            "record_lsn": lsn,
                            "expected_lsn": expected_lsn,
                            "missing_lsn_from": expected_lsn,
                            "missing_lsn_to": lsn - 1,
                        },
                    )

                op = record["op"]
                row_id = str(record["id"])
                if op == "upsert":
                    row = dict(record["row"])
                    row["id"] = row_id
                    rows[row_id] = row
                else:
                    rows.pop(row_id, None)

                if not segment_used:
                    base_result["used_segments"].append(
                        {
                            "seq": scan["seq"],
                            "id": scan["id"],
                            "file": scan["file"],
                        }
                    )
                    segment_used = True
                base_result["applied_records"] += 1
                expected_lsn = lsn + 1
                last_trusted = {
                    "kind": "record",
                    "id": f"lsn-{lsn}",
                    "at": event_ts,
                    "lsn": lsn,
                    "op": op,
                    "row_id": row_id,
                    "segment_id": scan["id"],
                    "label": f"LSN {lsn} / {scan['id']}",
                }
                base_result["last_trusted"] = {
                    **last_trusted,
                    "iso": iso_ts(event_ts),
                }

            if scan["status"] == "corrupt":
                error = scan["error"] or {}
                return fail(
                    error.get("code", "CORRUPT_SEGMENT"),
                    error.get("message", "corrupt segment"),
                    segment=scan,
                    extra={"line": error.get("line"), "scan_error": error},
                )
            if scan["status"] == "deleted_but_present":
                base_result["warnings"].append(
                    f"{scan['id']} is marked deleted but exists; verified contents were replayed"
                )

            expected_seq = seq + 1

        return finalize(ok=True)

    # ---------- demo / fault helpers ----------

    def write_demo_marker(self) -> None:
        (self.root / DEMO_MARKER).write_text(
            "Demo backup-chain repository. Safe to reset from the timeline UI.\n",
            encoding="utf-8",
        )

    def inject_corruption(
        self, segment_seq: int | None = None, mode: str = "checksum"
    ) -> dict[str, Any]:
        manifest = self._read_manifest()
        descriptors = self._merged_segment_descriptors(manifest)
        if segment_seq is None:
            segment_seq = int(manifest["active_segment_seq"])
        descriptor = next((d for d in descriptors if int(d["seq"]) == int(segment_seq)), None)
        if descriptor is None:
            raise FileNotFoundError(f"segment {segment_seq} not found")
        path = self.rel_path(descriptor["file"])
        if not path.exists():
            raise FileNotFoundError(f"segment file does not exist: {path}")

        if mode == "garbage":
            with open(path, "a", encoding="utf-8") as handle:
                handle.write("{this is not valid json\n")
        else:
            body = {
                "lsn": 9_000_000 + int(segment_seq),
                "ts": int(manifest.get("last_ts") or now_ts()),
                "op": "upsert",
                "id": "__corrupt__",
                "row": {"id": "__corrupt__", "note": "checksum intentionally invalid"},
            }
            bad = {**body, "sha256": "0" * 64}
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(bad, ensure_ascii=False) + "\n")
        return self.scan_segment(descriptor, include_records=False)

    def hide_segment(self, segment_seq: int) -> dict[str, Any]:
        manifest = self._read_manifest()
        descriptor = next(
            (d for d in self._merged_segment_descriptors(manifest) if int(d["seq"]) == int(segment_seq)),
            None,
        )
        if descriptor is None:
            raise FileNotFoundError(f"segment {segment_seq} not found")
        path = self.rel_path(descriptor["file"])
        hidden = path.with_suffix(path.suffix + ".hidden")
        if hidden.exists():
            hidden.unlink()
        if path.exists():
            path.rename(hidden)
        return {"hidden": True, "from": str(path), "to": str(hidden)}

    def unhide_segment(self, segment_seq: int) -> dict[str, Any]:
        manifest = self._read_manifest()
        descriptor = next(
            (d for d in self._merged_segment_descriptors(manifest) if int(d["seq"]) == int(segment_seq)),
            None,
        )
        if descriptor is None:
            raise FileNotFoundError(f"segment {segment_seq} not found")
        path = self.rel_path(descriptor["file"])
        hidden = path.with_suffix(path.suffix + ".hidden")
        if not hidden.exists():
            raise FileNotFoundError(f"hidden segment file not found: {hidden}")
        hidden.rename(path)
        return {"hidden": False, "path": str(path)}

    def inject_overlap(self, target_seq: int, source_seq: int) -> dict[str, Any]:
        manifest = self._read_manifest()
        descriptors = {int(d["seq"]): d for d in self._merged_segment_descriptors(manifest)}
        if target_seq not in descriptors or source_seq not in descriptors:
            raise FileNotFoundError("source or target segment not found")
        source = self.rel_path(descriptors[source_seq]["file"])
        target = self.rel_path(descriptors[target_seq]["file"])
        if not source.exists() or not target.parent.exists():
            raise FileNotFoundError("source or target file is unavailable")
        target.write_bytes(source.read_bytes())
        return {
            "overlap": True,
            "target_segment": segment_id(target_seq),
            "source_segment": segment_id(source_seq),
            "target_file": str(target),
        }


class ChainFault(Exception):
    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.details}
