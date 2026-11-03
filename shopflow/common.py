"""HTTP et instrumentation Prometheus commune aux trois services."""
import json
import os
import signal
import threading
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def env_float(key, default):
    return float(os.environ.get(key, default))


class Metrics:
    def __init__(self, service):
        self.service = service
        self._lock = threading.Lock()
        self._counters = defaultdict(float)
        self._latencies = defaultdict(list)
        self.start = time.monotonic()

    def inc(self, name, amount=1, **labels):
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self._counters[key] += amount

    def observe(self, name, duration, **labels):
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self._latencies[key].append(duration)

    def render(self):
        def label_text(labels):
            all_labels = dict(labels)
            all_labels["service"] = self.service
            return "{" + ",".join(f'{k}="{v}"' for k, v in sorted(all_labels.items())) + "}"

        with self._lock:
            counters = dict(self._counters)
            latencies = {key: list(vals) for key, vals in self._latencies.items()}
        lines = ["# HELP shopflow_uptime_seconds Process uptime", "# TYPE shopflow_uptime_seconds gauge",
                 f'shopflow_uptime_seconds{{service="{self.service}"}} {time.monotonic()-self.start:.3f}']
        for (name, labels), val in sorted(counters.items()):
            lines.append(f"shopflow_{name}{label_text(labels)} {val:g}")
        bounds = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0)
        for (name, labels), vals in sorted(latencies.items()):
            for bound in bounds:
                extra = tuple(labels) + (("le", str(bound)),)
                lines.append(f"shopflow_{name}_bucket{label_text(extra)} {sum(v <= bound for v in vals)}")
            inf_labels = tuple(labels) + (("le", "+Inf"),)
            lines.append(f"shopflow_{name}_bucket{label_text(inf_labels)} {len(vals)}")
            lines.append(f"shopflow_{name}_count{label_text(labels)} {len(vals)}")
            lines.append(f"shopflow_{name}_sum{label_text(labels)} {sum(vals):.6f}")
        return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    metrics = None
    service = "service"

    def log_message(self, format, *args):
        if os.environ.get("HTTP_LOG", "0") == "1":
            super().log_message(format, *args)

    def json_response(self, status, payload, headers=None):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, str(value))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def text_response(self, status, data, content_type="text/plain; version=0.0.4"):
        raw = data.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def body_json(self):
        size = int(self.headers.get("Content-Length", "0"))
        if size > 16384:
            raise ValueError("corps trop volumineux")
        return json.loads(self.rfile.read(size).decode("utf-8") or "{}")

    def common_get(self):
        if self.path == "/metrics":
            self.text_response(200, self.metrics.render())
            return True
        if self.path == "/health/live":
            self.metrics.inc("health_checks_total", probe="live")
            self.json_response(200, {"status": "alive", "service": self.service})
            return True
        if self.path == "/health/startup":
            self.metrics.inc("health_checks_total", probe="startup")
            self.json_response(200, {"status": "started", "service": self.service})
            return True
        return False

    def record(self, path, status, start):
        family = path.split("?")[0]
        self.metrics.inc("http_requests_total", path=family, status=str(status))
        self.metrics.observe("http_request_duration_seconds", time.monotonic() - start, path=family, status=str(status))


def serve(handler, port):
    """SIGTERM -> readiness false, arrêt de l'acceptation et fin des handlers en cours."""
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    server.accepting = True

    def terminate(_sig, _frame):
        server.accepting = False
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, terminate)
    try:
        server.serve_forever(poll_interval=0.05)
    finally:
        server.server_close()
