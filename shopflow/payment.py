"""Paiement idempotent même si la réponse HTTP se perd après enregistrement."""
import os
import threading
import time

from shopflow.common import Handler, Metrics, serve

METRICS = Metrics("payment")
LOCK = threading.Lock()
CHARGES = {}
FAULT = {"lost_reply_once": False, "down": False}
LOST_KEYS = set()


class PaymentHandler(Handler):
    metrics, service = METRICS, "payment"

    def do_GET(self):
        if self.common_get():
            return
        if self.path == "/health/ready":
            with LOCK:
                down = FAULT["down"]
            ready = not down and self.server.accepting
            METRICS.inc("health_checks_total", probe="ready", result=str(ready).lower())
            self.json_response(200 if ready else 503, {"ready": ready})
        elif self.path == "/admin/charges" and self.headers.get("X-Admin-Token") == os.environ.get("ADMIN_TOKEN", "local-demo-only"):
            with LOCK:
                values = list(CHARGES.values())
            self.json_response(200, {"charges": values, "count": len(values)})
        else:
            self.json_response(404, {"error": "not found"})

    def do_POST(self):
        start = time.monotonic()
        if self.path == "/admin/fault":
            if self.headers.get("X-Admin-Token") != os.environ.get("ADMIN_TOKEN", "local-demo-only"):
                self.json_response(403, {"error": "forbidden"})
                return
            try:
                values = self.body_json()
                with LOCK:
                    FAULT.update(lost_reply_once=bool(values.get("lost_reply_once", False)), down=bool(values.get("down", False)))
                self.json_response(200, {"fault": dict(FAULT)})
            except (ValueError, TypeError):
                self.json_response(400, {"error": "invalid request"})
            return
        if self.path != "/pay":
            self.json_response(404, {"error": "not found"})
            return
        key = self.headers.get("Idempotency-Key", "")
        if not key or len(key) > 128:
            self.json_response(400, {"error": "Idempotency-Key required"})
            return
        try:
            body = self.body_json()
            amount = float(body["amount"])
            if amount <= 0:
                raise ValueError("amount must be positive")
        except (ValueError, KeyError, TypeError):
            self.json_response(400, {"error": "invalid amount"})
            return
        with LOCK:
            if FAULT["down"]:
                self.json_response(503, {"error": "payment unavailable"}, {"Retry-After": "1"})
                self.record("/pay", 503, start)
                return
            if key in CHARGES:
                charge = CHARGES[key]
                if charge["amount"] != amount:
                    self.json_response(409, {"error": "key reused with different amount"})
                    return
                METRICS.inc("idempotency_replays_total")
            else:
                charge = {"payment_id": f"PAY-{len(CHARGES)+1:05d}", "amount": amount, "status": "paid"}
                CHARGES[key] = charge
                METRICS.inc("payment_charges_total")
            # Panne après COMMIT: l'appelant perd la réponse, mais le débit est déjà effectué.
            lose = FAULT["lost_reply_once"] and key not in LOST_KEYS
            if lose:
                LOST_KEYS.add(key)
        if lose:
            METRICS.inc("payment_lost_responses_total")
            self.close_connection = True
            try:
                self.connection.shutdown(2)
            except OSError:
                pass
            return
        self.json_response(200, charge)
        self.record("/pay", 200, start)


def main():
    port = int(os.environ.get("PORT", "8081"))
    print(f"payment started on {port}", flush=True)
    serve(PaymentHandler, port)


if __name__ == "__main__":
    main()
