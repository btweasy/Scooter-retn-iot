#!/usr/bin/env python3
"""Badanie platformy: opóźnienie, przepustowość i straty wiadomości dla QoS 0 i QoS 1.

Nie zmienia kodu aplikacji – uruchamia NIEZMIENIONY backend (`backend/app`) jako osobny proces,
przed nim stawia brokera Mosquitto (Docker) i pośrednika TCP psującego łącze (netem_proxy.py),
a ruch generuje load_generator.py. Każdy przebieg ma własną, świeżą bazę SQLite.

    urządzenia (load_generator) --> pośrednik TCP (opóźnienie, zerwania) --> Mosquitto --> backend --> SQLite

Wyniki (results/<data>/): summary.csv, runs.json, raw/<przebieg>.csv (każda wiadomość) i wykresy PNG.

Przykłady:
    py run_benchmark.py                      # pełny zestaw (~25 min)
    py run_benchmark.py --quick              # krótkie przebiegi (~8 min) – sprawdzenie, że wszystko działa
    py run_benchmark.py --only qos           # tylko porównanie QoS w różnych warunkach sieci
    py run_benchmark.py --only throughput    # tylko skalowanie obciążenia
    py run_benchmark.py --only batching      # tylko wpływ zapisu partiami (INGEST_FLUSH_S)
"""
import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, text

from broker_observer import BrokerObserver
from load_generator import LoadGenerator
from netem_proxy import NetemProxy
import plot

BENCH_DIR = Path(__file__).resolve().parent
BACKEND_DIR = BENCH_DIR.parent / "backend"
BROKER_CONTAINER = "scooter-bench-mosquitto"
BROKER_PORT = 18830     # porty inne niż w docker-compose, żeby nie kolidować z działającym demo
PROXY_PORT = 18831
API_PORT = 18800

# ---------------------------------------------------------------- scenariusze

# Warunki sieci / awarie porównywane dla QoS 0 i QoS 1
CONDITIONS = {
    "idealna":        {"label": "Sieć idealna"},
    "opoznienie":     {"label": "Opóźnienie 80 ± 40 ms", "delay_ms": 80, "jitter_ms": 40},
    "zerwania":       {"label": "Zerwania łącza (3 s co 10 s)", "outage_every_s": 10, "outage_len_s": 3},
    "restart_brokera": {"label": "Restart brokera", "event": "broker_restart"},
    "restart_backendu": {"label": "Awaria backendu (5 s)", "event": "backend_restart"},
}
QOS_SCOOTERS = 200          # 200 pojazdów co 1 s = 200 wiadomości/s
THROUGHPUT_LOADS = [200, 400, 600, 800, 1000, 1500]   # pojazdów, każdy co 1 s => wiadomości/s
# Zapis partiami: backend czeka do INGEST_FLUSH_S na kolejne wiadomości, zanim zrobi commit.
# Dłuższe czekanie = mniej transakcji (większa przepustowość), ale większe opóźnienie.
FLUSH_VALUES_S = [0.02, 0.05, 0.1, 0.2, 0.5]
FLUSH_SCOOTERS = 200        # wyraźnie poniżej nasycenia zapisu (~600 wiad./s) – inaczej wynik zależy od szumu kolejki
HTTP_SCOOTERS = QOS_SCOOTERS


def build_runs(only: str | None) -> list[dict]:
    runs = []
    if only in (None, "qos"):
        for condition in CONDITIONS:
            for qos in (0, 1):
                runs.append({"name": f"{condition}_qos{qos}", "group": "qos", "condition": condition,
                             "transport": "mqtt", "qos": qos, "scooters": QOS_SCOOTERS, "interval_s": 1.0})
    if only in (None, "throughput"):
        for load in THROUGHPUT_LOADS:
            runs.append({"name": f"obciazenie_{load}", "group": "throughput", "condition": "idealna",
                         "transport": "mqtt", "qos": 1, "scooters": load, "interval_s": 1.0})
    if only in (None, "batching"):
        for flush in FLUSH_VALUES_S:
            runs.append({"name": f"partie_{int(flush * 1000)}ms", "group": "batching", "condition": "idealna",
                         "transport": "mqtt", "qos": 1, "scooters": FLUSH_SCOOTERS, "interval_s": 1.0,
                         "backend_env": {"INGEST_FLUSH_S": str(flush)}, "flush_s": flush})
    if only in (None, "http"):
        runs.append({"name": "http_batch", "group": "http", "condition": "idealna",
                     "transport": "http", "qos": None, "scooters": HTTP_SCOOTERS, "interval_s": 1.0})
    return runs


# ---------------------------------------------------------------- broker (Docker)

def docker(*args, check=True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def start_broker():
    docker("rm", "-f", BROKER_CONTAINER, check=False)
    conf = (BENCH_DIR / "mosquitto.conf").as_posix()
    docker("run", "-d", "--name", BROKER_CONTAINER, "-p", f"{BROKER_PORT}:1883",
           "-v", f"{conf}:/mosquitto/config/mosquitto.conf:ro", "eclipse-mosquitto:2")
    wait_for_port(BROKER_PORT, 30, "broker MQTT")


def restart_broker():
    # docker restart wysyła SIGTERM – Mosquitto zapisuje trwałe sesje na dysk przed wyjściem
    docker("restart", "-t", "10", BROKER_CONTAINER)


def stop_broker():
    docker("rm", "-f", BROKER_CONTAINER, check=False)


def wait_for_port(port: int, timeout_s: float, what: str):
    import socket
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"{what} nie odpowiada na porcie {port}")


# ---------------------------------------------------------------- backend (osobny proces)

class Backend:
    """Niezmieniony backend uruchomiony przez uvicorn ze zmiennymi środowiskowymi przebiegu."""

    def __init__(self, database_url: str, mqtt: bool, client_id: str, log_path: Path, extra_env: dict):
        self.env = {**os.environ,
                    "DATABASE_URL": database_url,
                    "MQTT_ENABLED": "1" if mqtt else "0",
                    "MQTT_HOST": "127.0.0.1",
                    "MQTT_PORT": str(BROKER_PORT),
                    "MQTT_CLIENT_ID": client_id,
                    "AUTO_MIGRATE": "0",            # schemat tworzy create_schema() – bez zależności od alembic
                    "TELEMETRY_RETENTION_H": "0",   # nic nie usuwamy w trakcie pomiaru
                    "PYTHONIOENCODING": "utf-8",
                    **extra_env}
        self.env.pop("API_TOKEN", None)
        self.mqtt = mqtt
        self.log_path = log_path
        self.process: subprocess.Popen | None = None

    def create_schema(self):
        code = ("from app.db import Base, engine\nfrom app import models\n"
                "Base.metadata.drop_all(engine)\nBase.metadata.create_all(engine)")
        subprocess.run([sys.executable, "-c", code], cwd=BACKEND_DIR, env=self.env, check=True)

    def start(self):
        log = open(self.log_path, "a", encoding="utf-8")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(API_PORT),
             "--log-level", "warning"],
            cwd=BACKEND_DIR, env=self.env, stdout=log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            health = self.metrics(timeout=1)
            if health is not None and (not self.mqtt or health.get("mqtt_connected")):
                return
            time.sleep(0.2)
        raise RuntimeError(f"Backend nie wystartował – zobacz {self.log_path}")

    def kill(self):
        """Twarde zakończenie procesu (jak awaria zasilania) – bez porządnego zamknięcia."""
        if self.process is not None:
            self.process.kill()
            self.process.wait()
            self.process = None

    def stop(self):
        """Na Windows nie ma łagodnego zatrzymania procesu (terminate = kill), dlatego stop() wołamy
        dopiero wtedy, gdy kolejka zapisu backendu jest pusta (wait_until_drained)."""
        self.kill()

    @staticmethod
    def metrics(timeout: float = 3) -> dict | None:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/api/metrics", timeout=timeout) as response:
                return json.loads(response.read())
        except OSError:
            return None


# ---------------------------------------------------------------- jeden przebieg

def count_rows(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(text("SELECT COUNT(*) FROM telemetry")).scalar_one()


def wait_until_drained(engine, generator: LoadGenerator, timeout_s: float):
    """Po zakończeniu wysyłania czekamy, aż zaległe wiadomości (QoS 1, kolejka zapisu) trafią do bazy:
    licznik w bazie stoi przez 3 s, generator nie ma niepotwierdzonych wiadomości, kolejka backendu pusta."""
    deadline = time.monotonic() + timeout_s
    last_count, stable_since = -1, time.monotonic()
    while time.monotonic() < deadline:
        count = count_rows(engine)
        metrics = Backend.metrics() or {}
        if count != last_count:
            last_count, stable_since = count, time.monotonic()
        quiet = time.monotonic() - stable_since >= 3
        if quiet and generator.pending_qos1() == 0 and metrics.get("ingest_queue", 0) == 0:
            return
        time.sleep(0.5)


def run_one(run: dict, out_dir: Path, duration_s: float) -> dict:
    name = run["name"]
    condition = CONDITIONS[run["condition"]]
    db_path = out_dir / "db" / f"{name}.db"
    database_url = "sqlite:///" + db_path.as_posix()
    mqtt = run["transport"] == "mqtt"
    run_tag = f"{name}-{int(time.time())}"
    # świeży broker na każdy przebieg: trwałe sesje poprzednich przebiegów nie zbierają wiadomości w tle
    start_broker()

    backend = Backend(database_url, mqtt, client_id=f"bench-backend-{run_tag}",
                      log_path=out_dir / "logs" / f"{name}.log", extra_env=run.get("backend_env", {}))
    backend.create_schema()
    backend.start()

    observer = None
    if mqtt:
        observer = BrokerObserver("127.0.0.1", BROKER_PORT, client_id=f"bench-observer-{run_tag}")
        observer.start()

    proxy = None
    if mqtt:
        proxy = NetemProxy(PROXY_PORT, "127.0.0.1", BROKER_PORT,
                           delay_ms=condition.get("delay_ms", 0), jitter_ms=condition.get("jitter_ms", 0),
                           outage_every_s=condition.get("outage_every_s", 0),
                           outage_len_s=condition.get("outage_len_s", 0))
        proxy.start()

    generator = LoadGenerator(run["transport"], run["scooters"], run["interval_s"], qos=run["qos"] or 0,
                              mqtt_port=PROXY_PORT, api_port=API_PORT, run_id=run_tag)
    generator.connect()

    events = []   # (początek, koniec, opis) – czas Unix, do zaznaczenia na wykresie
    started = time.time()
    generator.start(duration_s)

    event = condition.get("event")
    if event:
        time.sleep(duration_s * 0.4)
        event_start = time.time()
        if event == "broker_restart":
            restart_broker()
            wait_for_port(BROKER_PORT, 30, "broker MQTT")
        elif event == "backend_restart":
            backend.kill()
            time.sleep(5)
            backend.start()
        events.append((event_start, time.time(), condition["label"]))

    generator.join()
    if proxy is not None:
        proxy.stop_outages()

    engine = create_engine(database_url)
    # przy przeciążeniu backend nadrabia zaległości jeszcze długo po końcu wysyłania (~500 wiad./s)
    wait_until_drained(engine, generator, timeout_s=60 + generator.stats.generated / 300)
    if proxy is not None:
        # odczyt dopiero teraz: zerwanie trwające w chwili końca wysyłania jest już zakończone i zapisane
        events.extend((start, end, "zerwanie łącza") for start, end in proxy.outages)
    backend_metrics = Backend.metrics() or {}

    generator.disconnect()
    if proxy is not None:
        proxy.stop()
    if observer is not None:
        observer.stop()
    backend.stop()

    result = analyse(engine, run, generator, observer, started, duration_s, events, backend_metrics, out_dir)
    engine.dispose()
    return result


# ---------------------------------------------------------------- analiza wyników

def to_epoch(value) -> float:
    """Czas z bazy (naiwny UTC) -> sekundy Unix. SQLite przy zapytaniu tekstowym zwraca napis."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=timezone.utc).timestamp()


def percentile(sorted_values: list[float], p: float) -> float | None:
    if not sorted_values:
        return None
    index = min(len(sorted_values) - 1, max(0, round(p / 100 * (len(sorted_values) - 1))))
    return sorted_values[index]


def analyse(engine, run, generator, observer, started, duration_s, events, backend_metrics, out_dir) -> dict:
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT scooter_id, seq, sent_at, ts FROM telemetry WHERE scooter_id LIKE 'BM-%'")).all()

    # zapis każdej wiadomości – z tego pliku plot.py rysuje rozkłady i przebiegi w czasie
    raw_path = out_dir / "raw" / f"{run['name']}.csv"
    delivered = set()
    latencies_ms = []
    with open(raw_path, "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["scooter_id", "seq", "sent_at", "received_at", "latency_ms"])
        for scooter_id, seq, sent_at, ts in rows:
            sent_epoch, received_epoch = to_epoch(sent_at), to_epoch(ts)
            latency = (received_epoch - sent_epoch) * 1000
            delivered.add((scooter_id, seq))
            latencies_ms.append(latency)
            writer.writerow([scooter_id, seq, f"{sent_epoch:.6f}", f"{received_epoch:.6f}", f"{latency:.3f}"])

    generated = generator.stats.generated
    lost = generated - len(delivered)

    # gdzie zginęły wiadomości: przed brokerem (sieć) czy za nim (backend)
    sent = {(scooter_id, seq) for scooter_id, last in generator.stats.per_scooter_seq.items()
            for seq in range(1, last + 1)}
    if observer is not None:
        reached_broker = sent & observer.seen
        lost_network = len(sent - reached_broker)
        lost_backend = len(reached_broker - delivered)
    else:   # HTTP: brak brokera, wszystko liczymy jako stratę w drodze do bazy
        lost_network, lost_backend = lost, 0
    latencies_ms.sort()
    received_times = sorted(to_epoch(ts) for *_, ts in rows)
    span = (received_times[-1] - received_times[0]) if len(received_times) > 1 else 0

    return {
        "name": run["name"],
        "group": run["group"],
        "condition": run["condition"],
        "condition_label": CONDITIONS[run["condition"]]["label"],
        "transport": run["transport"],
        "qos": "" if run["qos"] is None else run["qos"],
        "scooters": run["scooters"],
        "offered_per_s": round(run["scooters"] / run["interval_s"], 1),
        "ingest_flush_s": run.get("flush_s", 0.2),
        "duration_s": duration_s,
        "generated": generated,
        "delivered": len(delivered),
        "lost": lost,
        "loss_pct": round(100 * lost / generated, 3) if generated else 0,
        "lost_network": lost_network,
        "lost_backend": lost_backend,
        "rejected_by_client": generator.stats.rejected_by_client,
        "out_of_order_at_broker": observer.out_of_order if observer is not None else "",
        "duplicates_at_broker": observer.duplicates if observer is not None else "",
        "duplicates_dropped": backend_metrics.get("duplicates", ""),
        "achieved_per_s": round(len(delivered) / span, 1) if span else 0,
        "latency_mean_ms": round(sum(latencies_ms) / len(latencies_ms), 1) if latencies_ms else "",
        "latency_p50_ms": round(percentile(latencies_ms, 50), 1) if latencies_ms else "",
        "latency_p95_ms": round(percentile(latencies_ms, 95), 1) if latencies_ms else "",
        "latency_p99_ms": round(percentile(latencies_ms, 99), 1) if latencies_ms else "",
        "latency_max_ms": round(latencies_ms[-1], 1) if latencies_ms else "",
        "started_at": round(started, 3),
        "events": events,
    }


# ---------------------------------------------------------------- start

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--duration", type=float, default=30, help="czas wysyłania w jednym przebiegu [s]")
    parser.add_argument("--quick", action="store_true", help="krótkie przebiegi (12 s) i mniejsze obciążenia")
    parser.add_argument("--only", choices=["qos", "throughput", "batching", "http"], help="tylko jedna grupa przebiegów")
    parser.add_argument("--out", help="katalog wyników (domyślnie results/<data_godzina>)")
    args = parser.parse_args()

    duration = 12 if args.quick else args.duration
    if args.quick:
        THROUGHPUT_LOADS[:] = [200, 600, 1000]
        FLUSH_VALUES_S[:] = [0.02, 0.2, 0.5]
    if shutil.which("docker") is None:
        sys.exit("Brak polecenia docker – broker Mosquitto uruchamiany jest w kontenerze (uruchom Docker Desktop).")

    out_dir = Path(args.out).resolve() if args.out else BENCH_DIR / "results" / datetime.now().strftime("%Y-%m-%d_%H%M")
    for sub in ("db", "raw", "logs"):
        (out_dir / sub).mkdir(parents=True, exist_ok=True)

    runs = build_runs(args.only)
    print(f"Przebiegów: {len(runs)}, po {duration:.0f} s wysyłania. Wyniki: {out_dir}")
    results = load_previous_results(out_dir, {run["name"] for run in runs})
    try:
        for number, run in enumerate(runs, 1):
            print(f"[{number}/{len(runs)}] {run['name']} ...", end=" ", flush=True)
            result = run_one(run, out_dir, duration)
            results.append(result)
            print(f"wysłano {result['generated']}, straty {result['loss_pct']}%, "
                  f"p95 {result['latency_p95_ms']} ms, {result['achieved_per_s']} wiad./s")
            save_results(results, out_dir)
    finally:
        stop_broker()

    plot.make_all(out_dir)
    print(f"Gotowe. Wykresy i summary.csv w {out_dir}")


def load_previous_results(out_dir: Path, rerun_names: set[str]) -> list[dict]:
    """Przy --out istniejącego katalogu zachowujemy wyniki innych grup, a nadpisujemy tylko powtarzane."""
    path = out_dir / "runs.json"
    if not path.exists():
        return []
    previous = json.loads(path.read_text(encoding="utf-8"))
    return [result for result in previous if result["name"] not in rerun_names]


def save_results(results: list[dict], out_dir: Path):
    (out_dir / "runs.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    columns = []
    for result in results:   # starsze wyniki mogą nie mieć nowszych kolumn
        columns += [key for key in result if key != "events" and key not in columns]
    with open(out_dir / "summary.csv", "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)


if __name__ == "__main__":
    main()
