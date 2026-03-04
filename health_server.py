import json
import logging
import threading
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


def start_health_server(host: str = "0.0.0.0", port: int = 8000) -> tuple[ThreadingHTTPServer, threading.Thread]:
    """Start a tiny health server in a background thread."""
    server = ThreadingHTTPServer((host, port), _HealthRequestHandler)
    thread = threading.Thread(target=server.serve_forever, name="health-server", daemon=True)
    thread.start()
    logger.info("Health server started on http://%s:%d/health", host, port)
    return server, thread


def stop_health_server(server: ThreadingHTTPServer) -> None:
    try:
        server.shutdown()
        server.server_close()
        logger.info("Health server stopped")
    except Exception as e:
        logger.warning("Health server stop failed: %s", e)
