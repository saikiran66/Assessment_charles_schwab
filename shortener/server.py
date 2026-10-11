"""Small JSON HTTP API implemented with the Python standard library."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
from urllib.parse import urlsplit

from .config import Settings
from .database import Database
from .orchestrator import WorkflowEngine, WorkflowError
from .service import NotFoundError, ShortenerService, ValidationError


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "AgenticShortener/0.1"
    max_body_bytes = 16_384

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/healthz":
            self._json(200, {"status": "ok"})
            return
        if path == "/api/metrics":
            with self.app.database.connect() as connection:
                links = connection.execute("SELECT count(*) FROM links").fetchone()[0]
                clicks = connection.execute("SELECT count(*) FROM clicks").fetchone()[0]
                workflows = connection.execute("SELECT count(*) FROM workflow_runs").fetchone()[0]
                completed = connection.execute("SELECT count(*) FROM workflow_runs WHERE status='completed'").fetchone()[0]
            self._json(200, {"links": links, "clicks": clicks, "workflow_runs": workflows,
                             "workflow_success_rate": round(completed / workflows, 3) if workflows else None})
            return
        match = re.fullmatch(r"/api/links/([A-Za-z0-9]{4,12})/analytics", path)
        if match:
            try:
                self._json(200, self.app.shortener.analytics(match.group(1)))
            except NotFoundError as exc:
                self._error(404, str(exc))
            return
        match = re.fullmatch(r"/api/workflows/([0-9a-f-]{36})", path)
        if match:
            try:
                self._json(200, self.app.workflow.get_run(match.group(1)))
            except WorkflowError as exc:
                self._error(404, str(exc))
            return
        match = re.fullmatch(r"/([A-Za-z0-9]{4,12})", path)
        if match:
            try:
                destination = self.app.shortener.resolve(
                    match.group(1), self.headers.get("Referer"), self.headers.get("User-Agent")
                )
            except NotFoundError as exc:
                self._error(404, str(exc))
                return
            self.send_response(302)
            self.send_header("Location", destination)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._error(404, "route not found")

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        payload = self._read_json()
        if payload is None:
            return
        try:
            if path == "/api/links":
                created = self.app.shortener.create_link(payload.get("url"), payload.get("custom_code"), payload.get("expires_at"))
                self._json(201, created)
                return
            if path == "/api/workflows":
                injected = payload.get("inject_failure")
                if injected is not None and not isinstance(injected, str):
                    raise ValueError("inject_failure must be a task name")
                result = self.app.workflow.start(payload.get("scenario", ""), approve_release=False,
                                                 fail_once=injected)
                self._json(202 if result["status"] == "awaiting_approval" else 201, result)
                return
            match = re.fullmatch(r"/api/workflows/([0-9a-f-]{36})/approve", path)
            if match:
                result = self.app.workflow.resume(match.group(1), approve_release=True)
                self._json(200, result)
                return
            match = re.fullmatch(r"/api/workflows/([0-9a-f-]{36})/rollback", path)
            if match:
                result = self.app.workflow.rollback(match.group(1), payload.get("task_id", ""))
                self._json(200, result)
                return
            match = re.fullmatch(r"/api/workflows/([0-9a-f-]{36})/replan", path)
            if match:
                result = self.app.workflow.replan(match.group(1), payload.get("task_id", ""), payload.get("output", {}))
                self._json(200, result)
                return
            self._error(404, "route not found")
        except ValidationError as exc:
            self._error(400, str(exc))
        except ValueError as exc:
            self._error(400, str(exc))
        except WorkflowError as exc:
            self._error(409, str(exc))
        except Exception:
            self.log_error("request failed unexpectedly")
            self._error(500, "internal server error")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_json(self) -> dict | None:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._error(400, "invalid Content-Length")
            return None
        if size <= 0 or size > self.max_body_bytes:
            self._error(413 if size > self.max_body_bytes else 400, "request body must be between 1 and 16384 bytes")
            return None
        if "application/json" not in self.headers.get("Content-Type", "").lower():
            self._error(415, "Content-Type must be application/json")
            return None
        try:
            payload = json.loads(self.rfile.read(size))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._error(400, "request body must contain valid JSON")
            return None
        if not isinstance(payload, dict):
            self._error(400, "JSON body must be an object")
            return None
        return payload

    def _json(self, status: int, body: dict) -> None:
        encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(encoded)

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def log_message(self, format: str, *args) -> None:
        # Keep request logs useful without echoing full query strings or request bodies.
        super().log_message(format, *args)


class Application:
    def __init__(self, settings: Settings):
        self.database = Database(settings.database_path)
        self.database.initialize()
        self.shortener = ShortenerService(self.database, settings.base_url, settings.max_url_length)
        self.workflow = WorkflowEngine(self.database, settings.max_retries)


def serve(settings: Settings) -> None:
    app = Application(settings)
    handler = type("BoundApiHandler", (ApiHandler,), {"app": app})
    server = ThreadingHTTPServer((settings.host, settings.port), handler)
    server.daemon_threads = True
    print(f"URL shortener listening on http://{settings.host}:{settings.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
    finally:
        server.server_close()
