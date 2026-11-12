#!/usr/bin/env python3
"""Porównanie dwóch serii pomiarów, np. przed i po zmianie w backendzie.

    py compare.py results/przed_poprawkami results/po_poprawkach

Zapisuje do katalogu drugiej serii: porownanie.csv oraz wykresy porownanie_*.png.
"""
import csv
import json
import sys
from pathlib import Path

import numpy as np

import plot
from plot import CONDITION_ORDER, SHORT_LABELS, TEXT_SECONDARY, number, percent_label, plain_log_axis, plt

BEFORE_COLOR = "#9a9994"   # neutralny szary: stan wyjściowy
AFTER_COLOR = plot.COLORS["qos1"]


def load(out_dir: Path) -> dict:
    runs = json.loads((out_dir / "runs.json").read_text(encoding="utf-8"))
    return {run["name"]: run for run in runs}


def bar_pair(ax, x, before, after, value_format, labels):
    width = 0.38
    for offset, values, color, label in ((-width / 2, before, BEFORE_COLOR, labels[0]),
                                         (width / 2, after, AFTER_COLOR, labels[1])):
        bars = ax.bar(x + offset, values, width=width - 0.03, color=color, label=label)
        for bar, value in zip(bars, values):
            if not np.isnan(value):
                ax.annotate(value_format(value), (bar.get_x() + bar.get_width() / 2, bar.get_height()),
                            xytext=(0, 3), textcoords="offset points", ha="center", va="bottom",
                            fontsize=8, color=TEXT_SECONDARY)


def compare_loss(before: dict, after: dict, out_dir: Path, labels):
    conditions = [c for c in CONDITION_ORDER if f"{c}_qos1" in before and f"{c}_qos1" in after]
    if not conditions:
        return
    x = np.arange(len(conditions))
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.4))
    for ax, column, title, fmt in (
            (axes[0], "loss_pct", "Straty wiadomości – MQTT QoS 1", percent_label),
            (axes[1], "latency_p95_ms", "Opóźnienie p95 – MQTT QoS 1", lambda v: f"{v:.0f}")):
        values_before = [number(before[f"{c}_qos1"][column]) for c in conditions]
        values_after = [number(after[f"{c}_qos1"][column]) for c in conditions]
        bar_pair(ax, x, values_before, values_after, fmt, labels)
        ax.set_xticks(x)
        ax.set_xticklabels([SHORT_LABELS[c] for c in conditions], fontsize=8)
        ax.set_title(title)
    axes[0].set_ylabel("Utracone wiadomości [%]")
    axes[1].set_ylabel("Opóźnienie [ms] (skala log.)")
    axes[1].set_yscale("log")
    plain_log_axis(axes[1].yaxis)
    axes[0].legend(loc="upper right")   # po prawej słupki są niskie – legenda nie zasłania podpisów
    fig.savefig(out_dir / "porownanie_qos1.png")
    plt.close(fig)


def compare_throughput(before: dict, after: dict, out_dir: Path, labels):
    def series(runs):
        load_runs = sorted((r for r in runs.values() if r["group"] == "throughput"), key=lambda r: r["offered_per_s"])
        return ([r["offered_per_s"] for r in load_runs], [number(r["achieved_per_s"]) for r in load_runs],
                [number(r["latency_p50_ms"]) for r in load_runs])

    offered_b, achieved_b, p50_b = series(before)
    offered_a, achieved_a, p50_a = series(after)
    if not offered_b or not offered_a:
        return
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))
    top = max(offered_b + offered_a)
    axes[0].plot([0, top], [0, top], color=TEXT_SECONDARY, linestyle=":", linewidth=1, label="idealnie")
    for offered, achieved, p50, color, label in ((offered_b, achieved_b, p50_b, BEFORE_COLOR, labels[0]),
                                                 (offered_a, achieved_a, p50_a, AFTER_COLOR, labels[1])):
        axes[0].plot(offered, achieved, color=color, marker="o", markersize=6, label=label)
        axes[1].plot(offered, p50, color=color, marker="o", markersize=6, label=label)
    axes[0].set_xlim(0, top * 1.05)
    axes[0].set_ylim(0, top * 1.05)
    axes[0].set_xlabel("Wysyłane [wiad./s]")
    axes[0].set_ylabel("Zapisane [wiad./s]")
    axes[0].set_title("Przepustowość (MQTT QoS 1)")
    axes[0].legend(loc="upper left", fontsize=8)
    axes[1].set_yscale("log")
    plain_log_axis(axes[1].yaxis)
    axes[1].set_xlabel("Wysyłane [wiad./s]")
    axes[1].set_ylabel("Mediana opóźnienia [ms] (skala log.)")
    axes[1].set_title("Opóźnienie przy rosnącym obciążeniu")
    axes[1].legend(loc="upper left", fontsize=8)
    fig.savefig(out_dir / "porownanie_przepustowosc.png")
    plt.close(fig)


def write_table(before: dict, after: dict, out_dir: Path):
    columns = ["loss_pct", "lost_network", "lost_backend", "latency_p50_ms", "latency_p95_ms", "achieved_per_s"]
    with open(out_dir / "porownanie.csv", "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["przebieg"] + [f"{c}_przed" for c in columns] + [f"{c}_po" for c in columns])
        for name in after:
            if name in before:
                writer.writerow([name] + [before[name].get(c, "") for c in columns]
                                + [after[name].get(c, "") for c in columns])


def main():
    if len(sys.argv) != 3:
        sys.exit("Użycie: py compare.py <katalog_przed> <katalog_po>")
    before_dir, after_dir = Path(sys.argv[1]), Path(sys.argv[2])
    plot.style()
    before, after = load(before_dir), load(after_dir)
    labels = ("przed poprawkami", "po poprawkach")
    compare_loss(before, after, after_dir, labels)
    compare_throughput(before, after, after_dir, labels)
    write_table(before, after, after_dir)
    print(f"Porównanie zapisane w {after_dir}")


if __name__ == "__main__":
    main()
