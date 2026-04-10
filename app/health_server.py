import json
import logging
import threading
import errno
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger(__name__)


class _HealthRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        logger.info("[health] " + format, *args)

    def _write_json(self, status_code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            self._write_json(200, {"status": "ok"})
            return
        self._write_json(404, {"status": "not_found"})

    def do_HEAD(self):  # noqa: N802
        if self.path == "/health":
            self._write_json(200, {"status": "ok"})
            return
        self._write_json(404, {"status": "not_found"})


def start_health_server(host: str = "0.0.0.0", port: int = 8888) -> tuple[ThreadingHTTPServer | None, threading.Thread | None]:
    """Start a tiny health server in a background thread.

    If the port is already in use, we log a warning and return (None, None)
    instead of crashing the whole server.
    """
    try:
        server = ThreadingHTTPServer((host, port), _HealthRequestHandler)
    except OSError as e:
        if getattr(e, "errno", None) in (errno.EADDRINUSE, 48):
            logger.warning("Health server port in use; skipping health server (host=%s port=%d)", host, port)
            return (None, None)
        raise
    thread = threading.Thread(target=server.serve_forever, name="health-server", daemon=True)
    thread.start()
    logger.info("Health server started on http://%s:%d/health", host, port)
    return server, thread
