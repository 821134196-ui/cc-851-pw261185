"""Zero-dependency HTTP API and static timeline page."""
from __future__ import annotations

import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .core import BackupChain, ChainFault, parse_ts
from .demo import reset_demo
from .planner import (
    DeletionBlocked,
    apply_prune,
    build_timeline,
    deletion_plan,
    prune_plan,
    delete_segment,
)

WEB_DIR = Path(__file__).parent / "web"


class BackupServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], repo_root: str | Path):
        self.repo_root = Path(repo_root)
        self.repo = BackupChain(self.repo_root)
        super().__init__(address, BackupRequestHandler)


class BackupRequestHandler(BaseHTTPRequestHandler):
    server_version = "BackupChain/0.1"

    @property
    def backup_server(self) -> BackupServer:
        return self.server  # type: ignore[return-value]

    @property
    def repo(self) -> BackupChain:
        return self.backup_server.repo

    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep test/server output readable.
        print(f"[http] {self.address_string()} - {fmt % args}")

    # ---------- response helpers ----------

    def send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status: int, code: str, message: str, **extra: Any) -> None:
        self.send_json({"ok": False, "error": {"code": code, "message": message, **extra}}, status)

    def read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 2_000_000:
            raise ValueError("request body is too large")
        raw = self.rfile.read(length)
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        return value

    def query(self) -> dict[str, str]:
        parsed = urlparse(self.path)
        values = parse_qs(parsed.query, keep_blank_values=True)
        return {key: items[-1] for key, items in values.items()}

    # ---------- GET ----------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/health":
                self.send_json({"ok": True, "service": "backup-chain", "repo": str(self.repo.root)})
                return
            if path == "/api/timeline":
                self.send_json({"ok": True, "timeline": build_timeline(self.repo)})
                return
            if path == "/api/restore":
                query = self.query()
                at = query.get("at")
                if at is None:
                    self.send_error_json(400, "MISSING_AT", "query parameter at is required")
                    return
                result = self.repo.restore(at)
                self.send_json({"ok": bool(result.get("ok")), "result": result})
                return
            if path == "/api/deletion-plan":
                query = self.query()
                if "segment" not in query:
                    self.send_error_json(400, "MISSING_SEGMENT", "query parameter segment is required")
                    return
                plan = deletion_plan(self.repo, query["segment"], query.get("retain_from"))
                self.send_json({"ok": True, "plan": plan})
                return
            if path == "/api/prune-plan":
                query = self.query()
                if "retain_from" not in query:
                    self.send_error_json(400, "MISSING_RETAIN_FROM", "retain_from is required")
                    return
                self.send_json({"ok": True, "plan": prune_plan(self.repo, query["retain_from"])})
                return
            if path in {"/", "/index.html"}:
                self.serve_static("index.html", "text/html; charset=utf-8")
                return
            if path == "/app.js":
                self.serve_static("app.js", "text/javascript; charset=utf-8")
                return
            if path == "/styles.css":
                self.serve_static("styles.css", "text/css; charset=utf-8")
                return
            self.send_error_json(404, "NOT_FOUND", f"unknown path: {path}")
        except ValueError as exc:
            self.send_error_json(400, "BAD_REQUEST", str(exc))
        except FileNotFoundError as exc:
            self.send_error_json(404, "NOT_FOUND", str(exc))
        except ChainFault as exc:
            self.send_json({"ok": False, "error": exc.to_dict()}, 409)
        except Exception as exc:  # pragma: no cover - HTTP boundary
            self.send_error_json(500, "INTERNAL", f"{type(exc).__name__}: {exc}")

    def serve_static(self, name: str, content_type: str | None = None) -> None:
        path = (WEB_DIR / name).resolve()
        if WEB_DIR not in path.parents or not path.is_file():
            self.send_error_json(404, "ASSET_NOT_FOUND", name)
            return
        body = path.read_bytes()
        if content_type is None:
            content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---------- POST ----------

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            body = self.read_json_body()
            if path == "/api/changes":
                record = self.repo.append_change(
                    op=str(body.get("op") or "upsert"),
                    row_id=str(body.get("id") or ""),
                    row=body.get("row"),
                    ts=body.get("ts"),
                )
                self.send_json({"ok": True, "record": record, "timeline": build_timeline(self.repo)})
                return
            if path == "/api/fulls":
                descriptor = self.repo.create_full(ts=body.get("ts"), rows=body.get("rows"))
                self.send_json({"ok": True, "full": descriptor, "timeline": build_timeline(self.repo)})
                return
            if path == "/api/roll":
                descriptor = self.repo.roll_segment(ts=body.get("ts"))
                self.send_json({"ok": True, "segment": descriptor, "timeline": build_timeline(self.repo)})
                return
            if path == "/api/delete-segment":
                try:
                    result = delete_segment(
                        self.repo,
                        body.get("segment"),
                        retain_from=body.get("retain_from"),
                        force=bool(body.get("force")),
                        acknowledge_dependents=bool(body.get("acknowledge_dependents")),
                    )
                except DeletionBlocked as blocked:
                    self.send_json({"ok": False, "blocked": True, "plan": blocked.plan}, 409)
                    return
                self.send_json({"ok": True, "deletion": result, "timeline": build_timeline(self.repo)})
                return
            if path == "/api/prune":
                retain_from = body.get("retain_from")
                if retain_from is None:
                    self.send_error_json(400, "MISSING_RETAIN_FROM", "retain_from is required")
                    return
                if body.get("apply"):
                    result = apply_prune(self.repo, retain_from)
                else:
                    result = {"prune_plan": prune_plan(self.repo, retain_from)}
                self.send_json({"ok": True, **result, "timeline": build_timeline(self.repo)})
                return
            if path == "/api/faults/corrupt":
                report = self.repo.inject_corruption(
                    segment_seq=body.get("segment"),
                    mode=str(body.get("mode") or "checksum"),
                )
                self.send_json({"ok": True, "segment": report, "timeline": build_timeline(self.repo)})
                return
            if path == "/api/faults/hide":
                result = self.repo.hide_segment(int(body["segment"]))
                self.send_json({"ok": True, "result": result, "timeline": build_timeline(self.repo)})
                return
            if path == "/api/faults/unhide":
                result = self.repo.unhide_segment(int(body["segment"]))
                self.send_json({"ok": True, "result": result, "timeline": build_timeline(self.repo)})
                return
            if path == "/api/faults/overlap":
                result = self.repo.inject_overlap(
                    target_seq=int(body["target"]), source_seq=int(body["source"])
                )
                self.send_json({"ok": True, "result": result, "timeline": build_timeline(self.repo)})
                return
            if path == "/api/demo/reset":
                if not (self.repo.root / ".backup-chain-demo").exists():
                    self.send_error_json(403, "NOT_DEMO", "reset is only allowed for marked demo repositories")
                    return
                reset_demo(self.repo.root)
                self.send_json({"ok": True, "timeline": build_timeline(self.repo)})
                return
            self.send_error_json(404, "NOT_FOUND", f"unknown path: {path}")
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            self.send_error_json(400, "BAD_REQUEST", str(exc))
        except FileNotFoundError as exc:
            self.send_error_json(404, "NOT_FOUND", str(exc))
        except ChainFault as exc:
            self.send_json({"ok": False, "error": exc.to_dict()}, 409)
        except DeletionBlocked as exc:
            self.send_json({"ok": False, "blocked": True, "plan": exc.plan}, 409)
        except Exception as exc:  # pragma: no cover - HTTP boundary
            self.send_error_json(500, "INTERNAL", f"{type(exc).__name__}: {exc}")

    def log_error(self, format: str, *args: Any) -> None:  # noqa: A003
        # Avoid noisy default traceback-style access logs on expected 4xx.
        try:
            self.log_message(format, *args)
        except Exception:
            pass


def serve(root: str | Path, host: str = "127.0.0.1", port: int = 8000) -> None:
    server = BackupServer((host, port), root)
    print(f"Backup Chain timeline: http://{host}:{port}/  (repo: {Path(root).resolve()})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        server.server_close()
