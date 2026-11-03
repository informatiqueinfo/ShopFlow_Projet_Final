"""API ShopFlow : composition HTTP, protection de l'inventaire et du paiement."""
import json
import os
import threading
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from shopflow.common import Handler, Metrics, serve
from shopflow.patterns import CircuitBreaker, LoadShedder, RateLimiter, StaleCache, backoff

METRICS = Metrics("gateway")
METRICS.inc("sli_requests_total", 0, path="/catalogue")
for _mode in ("NOMINAL", "DEGRADE"):
    METRICS.inc("sli_good_total", 0, path="/catalogue", mode=_mode)
RATELIMIT = RateLimiter(capacity=10, rate=5)
SHEDDER = LoadShedder(capacity=8, reserved=2)
CB_STOCK = CircuitBreaker(failures=3, reset_after=2)
CB_PAYMENT = CircuitBreaker(failures=3, reset_after=2)
CACHE = StaleCache(fresh_for=float(os.environ.get("CACHE_FRESH_SECONDS", "3")), stale_for=90)
BULKHEAD_STOCK = threading.BoundedSemaphore(4)
BULKHEAD_PAYMENT = threading.BoundedSemaphore(4)
ENABLED = os.environ.get("RESILIENCE_ENABLED", "1") == "1"
STOCK_URL = os.environ.get("STOCK_URL", "http://127.0.0.1:8080")
PAYMENT_URL = os.environ.get("PAYMENT_URL", "http://127.0.0.1:8081")


def http_json(url, body=None, headers=None, timeout=0.25):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})})
    with urlopen(request, timeout=timeout) as resp:
        return json.load(resp)


def resilient_call(url, dependency, breaker, bulkhead, body=None, headers=None):
    """Timeout + retry borné + CB + bulkhead, avec indicateurs par pattern."""
    if ENABLED and not breaker.allowed():
        METRICS.inc("circuit_breaker_rejections_total", dependency=dependency)
        raise RuntimeError("circuit open")
    if ENABLED and not bulkhead.acquire(blocking=False):
        METRICS.inc("bulkhead_rejections_total", dependency=dependency)
        raise RuntimeError("bulkhead full")
    try:
        attempts = 2 if ENABLED else 1
        for attempt in range(attempts):
            try:
                result = http_json(url, body, headers, timeout=0.24 if ENABLED else 1.7)
                breaker.success()
                return result
            except (HTTPError, URLError, TimeoutError, OSError, ConnectionError) as err:
                METRICS.inc("dependency_failures_total", dependency=dependency)
                if isinstance(err, (TimeoutError,)) or "timed out" in str(err).lower():
                    METRICS.inc("timeouts_total", dependency=dependency)
                # Ne pas réessayer les erreurs fonctionnelles 4xx (sauf 429).
                status = getattr(err, "code", None)
                if status is not None and status < 500 and status != 429:
                    break
                if attempt + 1 < attempts:
                    # Respecter Retry-After : si le temps demandé dépasse le budget
                    # de repli du catalogue, ne PAS rappeler le service plus tôt.
                    retry_header = err.headers.get("Retry-After") if isinstance(err, HTTPError) else None
                    delay = backoff(attempt)
                    if retry_header:
                        try:
                            requested_delay = float(retry_header)
                            if requested_delay > 0.3:
                                METRICS.inc("retry_after_skipped_total", dependency=dependency)
                                break
                            delay = requested_delay
                        except ValueError:
                            pass
                    METRICS.inc("retries_total", dependency=dependency)
                    time.sleep(delay)
        breaker.failure()
        METRICS.inc("circuit_breaker_failures_total", dependency=dependency)
        raise RuntimeError(f"{dependency} unavailable")
    finally:
        if ENABLED:
            bulkhead.release()


class GatewayHandler(Handler):
    metrics, service = METRICS, "gateway"

    def do_GET(self):
        if self.common_get():
            return
        if self.path == "/health/ready":
            # Ne pas rendre l'API non prête quand le stock est en panne : fallback disponible.
            ready = self.server.accepting
            METRICS.inc("health_checks_total", probe="ready", result=str(ready).lower())
            self.json_response(200 if ready else 503, {"ready": ready, "dependency_stock": "optional for catalogue"})
            return
        path = urlparse(self.path).path
        if path not in ("/catalogue", "/"):
            self.json_response(404, {"error": "not found"})
            return
        start = time.monotonic()
        if not self.admit(start, path, priority=False):
            return
        try:
            cached = CACHE.read("A1")
            if ENABLED and cached:
                result, _ = cached
                self.reply(200, {"mode": "NOMINAL", "product": result, "source": "cache"}, path, start)
                METRICS.inc("cache_hits_total", mode="fresh")
                return
            try:
                result = resilient_call(f"{STOCK_URL}/stock/A1", "stock", CB_STOCK, BULKHEAD_STOCK)
                CACHE.write("A1", result)
                self.reply(200, {"mode": "NOMINAL", "product": result, "source": "stock"}, path, start)
                return
            except RuntimeError:
                stale = CACHE.read("A1", allow_stale=True) if ENABLED else None
                if stale:
                    METRICS.inc("cache_hits_total", mode="stale")
                    METRICS.inc("fallback_total", path=path)
                    self.reply(200, {"mode": "DEGRADE", "product": stale[0], "stock_certified": False}, path, start)
                else:
                    self.reply(503, {"error": "stock unavailable; no cached value"}, path, start)
        finally:
            self.release(False)

    def do_POST(self):
        start = time.monotonic()
        path = urlparse(self.path).path
        if path != "/checkout":
            self.json_response(404, {"error": "not found"})
            return
        if not self.admit(start, path, priority=True):
            return
        try:
            try:
                body = self.body_json()
                if body.get("sku", "A1") not in ("A1", "B2"):
                    self.reply(400, {"error": "unknown sku"}, path, start)
                    return
                # Commandes : jamais utiliser un stock périmé pour débiter le client.
                sku = body.get("sku", "A1")
                stock = resilient_call(f"{STOCK_URL}/stock/{sku}", "stock", CB_STOCK, BULKHEAD_STOCK)
                if stock["quantity"] < 1:
                    self.reply(409, {"error": "out of stock"}, path, start)
                    return
                key = self.headers.get("Idempotency-Key") or str(uuid.uuid4())
                result = resilient_call(f"{PAYMENT_URL}/pay", "payment", CB_PAYMENT, BULKHEAD_PAYMENT,
                                        {"amount": stock["price"]}, {"Idempotency-Key": key})
                self.reply(200, {"mode": "NOMINAL", "order": "accepted", "payment": result, "idempotency_key": key}, path, start)
            except (ValueError, KeyError, TypeError):
                self.reply(400, {"error": "invalid checkout data"}, path, start)
            except RuntimeError:
                # Fallback contrôlé : aucune écriture financière si incertitude de stock/paiement.
                METRICS.inc("fallback_total", path="/checkout")
                self.reply(503, {"mode": "INDISPONIBLE", "error": "checkout temporarily unavailable"}, path, start)
        finally:
            self.release(True)

    def admit(self, start, path, priority):
        if not ENABLED:
            return True
        client = self.headers.get("X-Client-Id") or self.client_address[0]
        allowed, retry_after = RATELIMIT.allow(client)
        if not allowed:
            METRICS.inc("rate_limit_rejections_total")
            self.reply(429, {"error": "client quota exceeded"}, path, start, {"Retry-After": str(retry_after)})
            return False
        if not SHEDDER.acquire(priority):
            METRICS.inc("load_shed_rejections_total", priority=str(priority).lower())
            self.reply(503, {"error": "server overloaded"}, path, start, {"Retry-After": "1"})
            return False
        return True

    def release(self, priority):
        if ENABLED:
            SHEDDER.release(priority)

    def reply(self, status, body, path, start, headers=None):
        self.json_response(status, body, headers)
        self.record(path, status, start)
        if status == 200:
            mode = body.get("mode", "NOMINAL")
            if time.monotonic() - start <= 1:
                METRICS.inc("sli_good_total", path=path, mode=mode)
        # Les 429 correspondent au refus d'un client hors quota, pas à une panne interne.
        if status != 429:
            METRICS.inc("sli_requests_total", path=path)


def main():
    port = int(os.environ.get("PORT", "8000"))
    print(f"gateway started on {port} resilience={ENABLED}", flush=True)
    serve(GatewayHandler, port)


if __name__ == "__main__":
    main()
