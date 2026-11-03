"""Cinq expériences locales reproductibles, sans Docker, avec résultats JSON réels.

Les trois services tournent sur localhost et ne modifient aucune donnée externe.
Chaque phase (avant/après) démarre avec de nouveaux processus et un état propre.
"""
import argparse
import concurrent.futures
import json
import os
import pathlib
import statistics
import subprocess
import sys
import time
import uuid
from collections import Counter
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:18000"
ADMIN = {"X-Admin-Token": "local-demo-only"}


def request(url, body=None, headers=None, timeout=3):
    raw = json.dumps(body).encode() if body is not None else None
    req = Request(url, data=raw, headers={"Content-Type": "application/json", **(headers or {})})
    start = time.monotonic()
    try:
        with urlopen(req, timeout=timeout) as res:
            return res.status, json.load(res), time.monotonic() - start
    except HTTPError as err:
        return err.code, json.load(err), time.monotonic() - start
    except (URLError, ConnectionError, TimeoutError, OSError):
        return 0, {"error": "network"}, time.monotonic() - start


def wait_ready(url, seconds=5):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        status, _, _ = request(url, timeout=0.3)
        if status == 200:
            return
        time.sleep(0.05)
    raise RuntimeError(f"service not ready: {url}")


class LocalServices:
    def __init__(self, enabled):
        self.enabled = enabled
        self.procs = []

    def __enter__(self):
        env = {**os.environ, "PYTHONPATH": str(ROOT), "HTTP_LOG": "0", "ADMIN_TOKEN": "local-demo-only"}
        for service, port in (("stock", 18080), ("payment", 18081), ("gateway", 18000)):
            custom = {**env, "PORT": str(port), "STOCK_URL": "http://127.0.0.1:18080",
                      "PAYMENT_URL": "http://127.0.0.1:18081", "CACHE_FRESH_SECONDS": "0.05",
                      "RESILIENCE_ENABLED": "1" if self.enabled else "0"}
            proc = subprocess.Popen([sys.executable, "-m", f"shopflow.{service}"], cwd=ROOT, env=custom,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            self.procs.append(proc)
            wait_ready(f"http://127.0.0.1:{port}/health/live")
        return self

    def __exit__(self, *_):
        for process in reversed(self.procs):
            if process.poll() is None:
                process.terminate()
        for process in reversed(self.procs):
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            if process.stderr:
                process.stderr.close()


SCENARIOS = {
    "A": {"title": "30pct_erreurs_stock", "fault": {"error_rate": 0.3}},
    "B": {"title": "stock_indisponible", "fault": {"down": True}},
    "C": {"title": "stock_lent_1300ms", "fault": {"latency": 1.3}},
    "D": {"title": "reponse_paiement_perdue", "fault": {"lost_reply_once": True}},
    "E": {"title": "client_abusif_50rps", "fault": {}},
}


def measure(result, response, start):
    code, payload, duration = response
    result.append({"status": code, "mode": payload.get("mode", "ERREUR"),
                   "duration_ms": round(duration * 1000, 1),
                   "good": code == 200 and duration < 1 and payload.get("mode", "NOMINAL") in ("NOMINAL", "DEGRADE")})


def summarize(calls, safety):
    success = sum(r["good"] for r in calls)
    duration = sorted(r["duration_ms"] for r in calls if r["status"] == 200)
    p95 = duration[max(0, int(0.95 * len(duration) + 0.99999) - 1)] if duration else None
    return {"requests": len(calls), "sli_pct": round(100 * success / len(calls), 2) if calls else 0,
            "statuses": dict(Counter(str(r["status"]) for r in calls)),
            "degraded": sum(r["mode"] == "DEGRADE" for r in calls),
            "p95_200_ms": p95, "safety_stop": safety}


def phase(scenario, enabled, samples, duration):
    with LocalServices(enabled):
        # Amorçage cache, également effectué sur la phase sans patterns (mais ignoré).
        request(f"{BASE}/catalogue", headers={"X-Client-Id": "warmup"})
        time.sleep(0.08)
        fault = SCENARIOS[scenario]["fault"]
        route = "http://127.0.0.1:18081" if scenario == "D" else "http://127.0.0.1:18080"
        if fault:
            request(f"{route}/admin/fault", fault, ADMIN)
        if scenario == "E":
            return overload_phase(duration, enabled)
        results = []
        stopped = False
        charge_count = None
        for i in range(samples):
            identity = f"chaos-{i}"
            if scenario == "D":
                response = request(f"{BASE}/checkout", {"sku": "A1"},
                                   {"X-Client-Id": identity, "Idempotency-Key": f"chaos-pay-{i}"})
            else:
                response = request(f"{BASE}/catalogue", headers={"X-Client-Id": identity})
            measure(results, response, time.monotonic())
            # Arrêt de sécurité si le SLI reste sous 20 % après 8 appels consécutifs.
            if len(results) >= 8 and sum(r["good"] for r in results[-8:]) < 2:
                stopped = True
                break
        if scenario == "D":
            _, charges, _ = request(f"{route}/admin/charges", headers=ADMIN)
            charge_count = charges.get("count")
        summary = summarize(results, stopped)
        if charge_count is not None:
            summary["distinct_charges"] = charge_count
        return summary


def overload_phase(duration, enabled):
    """Comparer l'impact d'un même client en flood sur un témoin 2 r/s."""
    calls = []
    witness = []
    start = time.monotonic()

    def flood():
        while time.monotonic() - start < duration:
            result = request(f"{BASE}/catalogue", headers={"X-Client-Id": "abusive"}, timeout=2)
            measure(calls, result, start)
            time.sleep(0.02)  # cadence cible ~50 requêtes par seconde

    def observer():
        while time.monotonic() - start < duration:
            result = request(f"{BASE}/catalogue", headers={"X-Client-Id": "reasonable"}, timeout=2)
            measure(witness, result, start)
            time.sleep(0.5)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        work = [pool.submit(flood)]
        work.append(pool.submit(observer))
        for future in work:
            future.result()
    values = summarize(calls, False)
    values["witness"] = summarize(witness, False)
    values["flood_mode"] = "sans_quota" if not enabled else "quota_10_jetons_5_par_s"
    return values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenarios", default="ABCDE", help="Lettres A à E")
    parser.add_argument("--samples", type=int, default=16)
    parser.add_argument("--duration", type=float, default=3, help="durée du scénario E en secondes")
    parser.add_argument("--out", default="results/experiences_locales.json")
    opts = parser.parse_args()
    report = {"methodology": "Deux exécutions isolées par scénario : sans puis avec patterns.",
              "criteria": "SLI = HTTP 200 nominal ou dégradé en moins de 1 s; arrêt si moins de 2 succès sur les 8 derniers appels.",
              "scenarios": {}}
    for scenario in opts.scenarios:
        if scenario not in SCENARIOS:
            raise SystemExit(f"unknown scenario: {scenario}")
        before = phase(scenario, False, opts.samples, opts.duration)
        after = phase(scenario, True, opts.samples, opts.duration)
        report["scenarios"][scenario] = {"title": SCENARIOS[scenario]["title"],
                                         "hypothesis": {
                                             "A": "Retries, CB et cache réduisent les échecs intermittents.",
                                             "B": "La consultation continue en dégradé via le cache pendant la panne totale.",
                                             "C": "Le timeout borne le délai et le cache protège le SLI.",
                                             "D": "Le retry avec la même clé retrouve le paiement sans double débit.",
                                             "E": "Le quota isole le client abusif et protège le témoin.",
                                         }[scenario], "before": before, "after": after,
                                         "abort_criterion": "<2 succès sur les 8 derniers appels; phase arrêtée au plus tard après le nombre d'échantillons prévu."}
        print(f"{scenario} {SCENARIOS[scenario]['title']}: SLI {before['sli_pct']}% -> {after['sli_pct']}%", flush=True)
    path = ROOT / opts.out
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Rapport réel écrit : {path}")


if __name__ == "__main__":
    main()
