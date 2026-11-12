#!/usr/bin/env python3
"""Wykresy z wyników badania (summary.csv, runs.json, raw/*.csv).

Można uruchomić osobno, żeby poprawić wygląd wykresów bez powtarzania pomiarów:
    py plot.py results/2026-11-12_1530
"""
import csv
import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")   # bez okna – tylko pliki PNG
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.patches  # noqa: E402
import matplotlib.ticker  # noqa: E402
import numpy as np  # noqa: E402

# Kolory serii (sprawdzona paleta: rozróżnialna także przy zaburzeniach widzenia barw)
COLORS = {"qos0": "#eb6834", "qos1": "#2a78d6", "http": "#1baf7a"}
LABELS = {"qos0": "MQTT QoS 0", "qos1": "MQTT QoS 1", "http": "HTTP (partie)"}
TEXT = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
GRID = "#e4e3df"
EVENT = "#c9c8c2"

CONDITION_ORDER = ["idealna", "opoznienie", "zerwania", "restart_brokera", "restart_backendu"]
# Krótkie podpisy osi X (pełna nazwa warunku jest w summary.csv)
SHORT_LABELS = {
    "idealna": "Sieć\nidealna",
    "opoznienie": "Opóźnienie\n80 ± 40 ms",
    "zerwania": "Zerwania łącza\n3 s co 10 s",
    "restart_brokera": "Restart\nbrokera",
    "restart_backendu": "Awaria\nbackendu (5 s)",
}


def style():
    plt.rcParams.update({
        "figure.dpi": 150, "savefig.dpi": 200, "savefig.bbox": "tight",
        "font.size": 10, "axes.titlesize": 11, "axes.titleweight": "bold",
        "axes.edgecolor": GRID, "axes.labelcolor": TEXT_SECONDARY, "axes.titlecolor": TEXT,
        "xtick.color": TEXT_SECONDARY, "ytick.color": TEXT_SECONDARY,
        "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8, "axes.axisbelow": True,
        "axes.spines.top": False, "axes.spines.right": False,
        "legend.frameon": False, "lines.linewidth": 2,
    })


# ---------------------------------------------------------------- wczytanie danych

def load(out_dir: Path):
    runs = json.loads((out_dir / "runs.json").read_text(encoding="utf-8"))
    return {run["name"]: run for run in runs}


def load_raw(out_dir: Path, name: str) -> dict[str, np.ndarray]:
    path = out_dir / "raw" / f"{name}.csv"
    sent, received, latency = [], [], []
    with open(path, encoding="utf-8") as file:
        for row in csv.DictReader(file):
            sent.append(float(row["sent_at"]))
            received.append(float(row["received_at"]))
            latency.append(float(row["latency_ms"]))
    return {"sent": np.array(sent), "received": np.array(received), "latency": np.array(latency)}


def series_key(run: dict) -> str:
    return "http" if run["transport"] == "http" else f"qos{run['qos']}"


def plain_log_axis(axis):
    """Skala logarytmiczna z podpisami 50, 100, 200 zamiast 5×10¹, 10², 2×10²."""
    formatter = matplotlib.ticker.FuncFormatter(lambda value, _pos: f"{value:g}")
    axis.set_major_formatter(formatter)
    axis.set_minor_formatter(matplotlib.ticker.FuncFormatter(
        lambda value, _pos: f"{value:g}" if str(f"{value:g}")[0] in "25" else ""))


def percent_label(value: float) -> str:
    return "0%" if value == 0 else f"{value:.2f}%".replace(".", ",")


def number(value) -> float:
    return float(value) if value not in ("", None) else float("nan")


# ---------------------------------------------------------------- wykresy

def grouped_bars(ax, conditions, values_by_series, value_format):
    """Słupki pogrupowane według warunków sieci, po jednym na serię (QoS 0 / QoS 1)."""
    series = list(values_by_series)
    width = 0.8 / len(series)
    x = np.arange(len(conditions))
    for index, key in enumerate(series):
        values = values_by_series[key]
        positions = x - 0.4 + width * (index + 0.5)
        bars = ax.bar(positions, values, width=width - 0.03, color=COLORS[key], label=LABELS[key])
        for bar, value in zip(bars, values):
            if not np.isnan(value):
                ax.annotate(value_format(value), (bar.get_x() + bar.get_width() / 2, bar.get_height()),
                            xytext=(0, 3), textcoords="offset points", ha="center", va="bottom",
                            fontsize=8, color=TEXT_SECONDARY)
    ax.set_xticks(x)
    return x


def qos_runs(runs: dict) -> tuple[list[str], list[str]]:
    conditions = [c for c in CONDITION_ORDER if f"{c}_qos0" in runs or f"{c}_qos1" in runs]
    return conditions, [SHORT_LABELS[c] for c in conditions]


def plot_loss(runs: dict, out_dir: Path):
    """Straty rozdzielone na sieć (przed brokerem) i backend (broker dostał, w bazie brak).
    Kolor = poziom QoS, wzór = miejsce utraty."""
    conditions, labels = qos_runs(runs)
    if not conditions:
        return
    fig, ax = plt.subplots(figsize=(9, 4.4))
    x = np.arange(len(conditions))
    width = 0.4
    for index, qos in enumerate((0, 1)):
        key = f"qos{qos}"
        positions = x - 0.2 + width * index
        for position, condition in zip(positions, conditions):
            run = runs.get(f"{condition}_{key}")
            if run is None:
                continue
            generated = run["generated"] or 1
            network = 100 * number(run.get("lost_network", run["lost"])) / generated
            backend = 100 * number(run.get("lost_backend", 0)) / generated
            ax.bar(position, network, width=width - 0.03, color=COLORS[key])
            ax.bar(position, backend, width=width - 0.03, bottom=network, color="white",
                   edgecolor=COLORS[key], hatch="////", linewidth=1)
            ax.annotate(percent_label(number(run["loss_pct"])), (position, network + backend),
                        xytext=(0, 3), textcoords="offset points", ha="center", va="bottom",
                        fontsize=8, color=TEXT_SECONDARY)
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_ylabel("Utracone wiadomości [%]")
    ax.set_title("Straty wiadomości: QoS 0 vs QoS 1")
    legend = [
        matplotlib.patches.Patch(color=COLORS["qos0"], label=LABELS["qos0"]),
        matplotlib.patches.Patch(color=COLORS["qos1"], label=LABELS["qos1"]),
        matplotlib.patches.Patch(facecolor="#9a9994", label="zginęły w sieci (nie dotarły do brokera)"),
        matplotlib.patches.Patch(facecolor="white", edgecolor="#52514e", hatch="////",
                                 label="zginęły w backendzie (awaria / odrzucone jako spóźnione)"),
    ]
    ax.legend(handles=legend, loc="upper left", fontsize=8)
    ax.set_ylim(0, max(ax.get_ylim()[1], 1) * 1.5)   # miejsce na legendę nad słupkami
    fig.savefig(out_dir / "1_straty_qos.png")
    plt.close(fig)


def plot_latency_bars(runs: dict, out_dir: Path):
    conditions, labels = qos_runs(runs)
    if not conditions:
        return
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2), sharey=True)
    for ax, (column, title) in zip(axes, [("latency_p50_ms", "Mediana (p50)"), ("latency_p95_ms", "p95")]):
        values = {f"qos{q}": [number(runs.get(f"{c}_qos{q}", {}).get(column, "")) for c in conditions]
                  for q in (0, 1)}
        grouped_bars(ax, conditions, values, lambda v: f"{v:.0f}")
        ax.set_xticklabels(labels, fontsize=8)
        ax.set_yscale("log")
        plain_log_axis(ax.yaxis)
        ax.set_title(f"Opóźnienie end-to-end – {title}")
    axes[0].set_ylabel("Opóźnienie [ms] (skala log.)")
    axes[0].legend(loc="upper left")
    fig.savefig(out_dir / "2_opoznienia_qos.png")
    plt.close(fig)


def plot_cdf(ax, latencies_ms: np.ndarray, color: str, label: str, linestyle: str = "-"):
    values = np.sort(latencies_ms)
    if len(values) == 0:
        return
    fraction = np.arange(1, len(values) + 1) / len(values)
    ax.plot(values, fraction * 100, color=color, label=label, linestyle=linestyle)


def plot_latency_cdf(runs: dict, out_dir: Path):
    names = [n for n in ("idealna_qos0", "idealna_qos1", "opoznienie_qos0", "opoznienie_qos1") if n in runs]
    if not names:
        return
    fig, ax = plt.subplots(figsize=(8, 4.2))
    for name in names:
        run = runs[name]
        key = series_key(run)
        linestyle = "-" if run["condition"] == "idealna" else "--"
        plot_cdf(ax, load_raw(out_dir, name)["latency"], COLORS[key],
                 f"{LABELS[key]} – {run['condition_label']}", linestyle)
    ax.set_xscale("log")
    plain_log_axis(ax.xaxis)
    ax.set_xlabel("Opóźnienie end-to-end [ms] (skala log.)")
    ax.set_ylabel("Wiadomości o opóźnieniu ≤ x [%]")
    ax.set_title("Rozkład opóźnień (dystrybuanta)")
    ax.legend(loc="lower right", fontsize=8)
    fig.savefig(out_dir / "3_rozklad_opoznien.png")
    plt.close(fig)


def plot_timeline(runs: dict, out_dir: Path, condition: str, number_in_name: int):
    names = [f"{condition}_qos{q}" for q in (0, 1) if f"{condition}_qos{q}" in runs]
    if not names:
        return
    fig, axes = plt.subplots(len(names), 1, figsize=(9, 2.4 * len(names) + 0.6), sharex=True, sharey=True)
    axes = np.atleast_1d(axes)
    for ax, name in zip(axes, names):
        run = runs[name]
        key = series_key(run)
        start = run["started_at"]
        raw = load_raw(out_dir, name)
        seconds = raw["received"] - start
        edges = np.arange(0, max(seconds.max() if len(seconds) else 1, run["duration_s"]) + 2, 1)
        counts, _ = np.histogram(seconds, bins=edges)
        for event_start, event_end, _label in run["events"]:
            ax.axvspan(event_start - start, max(event_end - start, event_start - start + 0.3),
                       color=EVENT, alpha=0.6, linewidth=0)
        ax.axhline(run["offered_per_s"], color=TEXT_SECONDARY, linestyle=":", linewidth=1)
        ax.step(edges[:-1], counts, where="post", color=COLORS[key])
        ax.set_title(f"{LABELS[key]} – straty {percent_label(run['loss_pct'])}", loc="left", fontsize=10)
        ax.set_ylabel("Zapisane [wiad./s]")
    axes[-1].set_xlabel("Czas od początku pomiaru [s]   (szare pasy: awaria, kropki: tempo wysyłania)")
    fig.suptitle(f"Przebieg w czasie: {runs[names[0]]['condition_label']}", fontweight="bold", color=TEXT)
    fig.savefig(out_dir / f"{number_in_name}_przebieg_{condition}.png")
    plt.close(fig)


def plot_throughput(runs: dict, out_dir: Path):
    load_runs = sorted((r for r in runs.values() if r["group"] == "throughput"), key=lambda r: r["offered_per_s"])
    if not load_runs:
        return
    offered = np.array([r["offered_per_s"] for r in load_runs])
    achieved = np.array([number(r["achieved_per_s"]) for r in load_runs])
    p50 = np.array([number(r["latency_p50_ms"]) for r in load_runs])
    p95 = np.array([number(r["latency_p95_ms"]) for r in load_runs])

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))
    ax = axes[0]
    ax.plot(offered, offered, color=TEXT_SECONDARY, linestyle=":", linewidth=1, label="idealnie (zapis = wysyłanie)")
    ax.plot(offered, achieved, color=COLORS["qos1"], marker="o", markersize=6, label="zapisane do bazy")
    for x, y in zip(offered, achieved):
        ax.annotate(f"{y:.0f}", (x, y), xytext=(0, -14), textcoords="offset points", ha="center",
                    fontsize=8, color=TEXT_SECONDARY)
    ax.set_xlabel("Wysyłane [wiad./s]")
    ax.set_ylabel("Zapisane [wiad./s]")
    ax.set_title("Przepustowość (MQTT QoS 1)")
    ax.legend(loc="upper left", fontsize=8)

    ax = axes[1]
    ax.plot(offered, p50, color=COLORS["qos1"], marker="o", markersize=6, label="mediana (p50)")
    ax.plot(offered, p95, color=COLORS["qos1"], marker="s", markersize=6, linestyle="--", label="p95")
    ax.set_yscale("log")
    plain_log_axis(ax.yaxis)
    ax.set_xlabel("Wysyłane [wiad./s]")
    ax.set_ylabel("Opóźnienie [ms] (skala log.)")
    ax.set_title("Opóźnienie przy rosnącym obciążeniu")
    ax.legend(loc="upper left", fontsize=8)
    fig.savefig(out_dir / "7_przepustowosc.png")
    plt.close(fig)


def plot_batching(runs: dict, out_dir: Path):
    """Kompromis zapisu partiami: dłuższe zbieranie partii = mniej transakcji, ale większe opóźnienie."""
    batch_runs = sorted((r for r in runs.values() if r["group"] == "batching"), key=lambda r: r["ingest_flush_s"])
    if not batch_runs:
        return
    flush_ms = [r["ingest_flush_s"] * 1000 for r in batch_runs]
    p50 = [number(r["latency_p50_ms"]) for r in batch_runs]
    p95 = [number(r["latency_p95_ms"]) for r in batch_runs]
    achieved = [number(r["achieved_per_s"]) for r in batch_runs]

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))
    ax = axes[0]
    ax.plot(flush_ms, p50, color=COLORS["qos1"], marker="o", markersize=6, label="mediana (p50)")
    ax.plot(flush_ms, p95, color=COLORS["qos1"], marker="s", markersize=6, linestyle="--", label="p95")
    ax.set_xscale("log")
    ax.set_yscale("log")
    plain_log_axis(ax.xaxis)
    plain_log_axis(ax.yaxis)
    ax.set_xticks(flush_ms)
    ax.set_xlabel("INGEST_FLUSH_S – maks. czas zbierania partii [ms]")
    ax.set_ylabel("Opóźnienie [ms] (skala log.)")
    ax.set_title("Opóźnienie a czas zbierania partii")
    ax.legend(loc="upper left", fontsize=8)

    ax = axes[1]
    offered = batch_runs[0]["offered_per_s"]
    ax.axhline(offered, color=TEXT_SECONDARY, linestyle=":", linewidth=1, label="wysyłane")
    ax.plot(flush_ms, achieved, color=COLORS["qos1"], marker="o", markersize=6, label="zapisane do bazy")
    ax.set_xscale("log")
    plain_log_axis(ax.xaxis)
    ax.set_xticks(flush_ms)
    ax.set_ylim(0, offered * 1.15)
    ax.set_xlabel("INGEST_FLUSH_S – maks. czas zbierania partii [ms]")
    ax.set_ylabel("Zapisane [wiad./s]")
    ax.set_title(f"Przepustowość przy {offered:.0f} wiad./s")
    ax.legend(loc="lower right", fontsize=8)
    fig.savefig(out_dir / "8_zapis_partiami.png")
    plt.close(fig)


def plot_mqtt_vs_http(runs: dict, out_dir: Path):
    names = [n for n in ("idealna_qos0", "idealna_qos1", "http_batch") if n in runs]
    if "http_batch" not in names:
        return
    fig, ax = plt.subplots(figsize=(8, 4.2))
    for name in names:
        key = series_key(runs[name])
        plot_cdf(ax, load_raw(out_dir, name)["latency"], COLORS[key], LABELS[key])
    ax.set_xscale("log")
    plain_log_axis(ax.xaxis)
    ax.set_xlabel("Opóźnienie end-to-end [ms] (skala log.)")
    ax.set_ylabel("Wiadomości o opóźnieniu ≤ x [%]")
    ax.set_title(f"MQTT vs HTTP – {runs['http_batch']['offered_per_s']:.0f} wiad./s, sieć idealna")
    ax.legend(loc="lower right")
    fig.savefig(out_dir / "9_mqtt_vs_http.png")
    plt.close(fig)


def make_all(out_dir: Path):
    style()
    runs = load(out_dir)
    plot_loss(runs, out_dir)
    plot_latency_bars(runs, out_dir)
    plot_latency_cdf(runs, out_dir)
    plot_timeline(runs, out_dir, "zerwania", 4)
    plot_timeline(runs, out_dir, "restart_brokera", 5)
    plot_timeline(runs, out_dir, "restart_backendu", 6)
    plot_throughput(runs, out_dir)
    plot_batching(runs, out_dir)
    plot_mqtt_vs_http(runs, out_dir)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Użycie: py plot.py results/<katalog_przebiegu>")
    make_all(Path(sys.argv[1]))
