
import os
import random
import threading
import time
from urllib.parse import urlparse

from shopflow.common import Handler, Metrics, serve

METRICS = Metrics("stock")
FAULT = {"down": False, "error_rate": 0.0, "latency": 0.0}
LOCK = threading.Lock()
ITEMS = {"A1": {"sku": "A1", "name": "Écouteurs", "quantity": 20, "price": 29.9},
         "B2": {"sku": "B2", "name": "Clavier", "quantity": 15, "price": 49.9}}


class StockHandler(Handler):
    metrics, service = METRICS, "stock"

    def do_GET(self):
        start = time.monotonic()
        if self.common_get():
            return
        path = urlparse(self.path).path
        if path == "/health/ready":
            with LOCK:
                down = FAULT["down"]
            ready = not down and self.server.accepting
            METRICS.inc("health_checks_total", probe="ready", result=str(ready).lower())
            self.json_response(200 if ready else 503, {"ready": ready})
            return
        if not path.startswith("/stock/"):
            self.json_response(404, {"error": "not found"})
            return
        with LOCK:
            fault = dict(FAULT)
        if fault["latency"]:
            time.sleep(fault["latency"])
        if fault["down"] or random.random() < fault["error_rate"]:
            self.json_response(503, {"error": "inventory unavailable"}, {"Retry-After": "1"})
            self.record("/stock", 503, start)
            METRICS.inc("dependency_failures_total", target="inventory")
            return
        item = ITEMS.get(path.removeprefix("/stock/"))
        self.json_response(200 if item else 404, item or {"error": "unknown sku"})
        self.record("/stock", 200 if item else 404, start)

    def do_POST(self):
        if self.path != "/admin/fault" or self.headers.get("X-Admin-Token") != os.environ.get("ADMIN_TOKEN", "local-demo-only"):
            self.json_response(403, {"error": "forbidden"})
            return
        try:
            values = self.body_json()
            down = values.get("down", False)
            rate = float(values.get("error_rate", 0.0))
            latency = float(values.get("latency", 0.0))
            if not isinstance(down, bool) or not 0 <= rate <= 1 or not 0 <= latency <= 5:
                raise ValueError("invalid fault parameters")
            with LOCK:
                FAULT.update(down=down, error_rate=rate, latency=latency)
            self.json_response(200, {"fault": dict(FAULT)})
        except (ValueError, TypeError) as error:
            self.json_response(400, {"error": str(error)})


def main():
    port = int(os.environ.get("PORT", "8080"))
    print(f"stock started on {port}", flush=True)
    serve(StockHandler, port)


if __name__ == "__main__":
    main()
