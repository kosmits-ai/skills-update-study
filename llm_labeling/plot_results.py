"""Publication-style bar charts of the LLM labelling results.

Reads label_sheet_llm.csv and compare_summary.json from --data and writes each
figure as PDF (vector) and PNG (300 dpi) to <data>/figures/, plus a 2x2 overview
with panel letters. Prevalence panels use only the random sample
(sample_reason == "random"), since the feature-sampled rows over-represent
detector hits. Error bars are 95% Wilson score intervals.

    python3 llm_labeling/plot_results.py --data labelling_data
"""
import argparse
import collections
import csv
import json
import math
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# Okabe-Ito colour-blind-safe palette.
BLUE, VERMILLION, GREEN, GREY = "#0072B2", "#D55E00", "#009E73", "#999999"

CATEGORIES = {
    "1": "Wording", "2": "Docs", "3": "Feature", "4": "Fix", "5": "Integration",
    "6": "Install", "7": "Behaviour", "8": "Security", "9": "Refactor", "0": "Other",
}

plt.rcParams.update({
    "font.family": "serif",
    "font.serif": ["STIXGeneral", "DejaVu Serif"],
    "mathtext.fontset": "stix",
    "font.size": 9, "axes.labelsize": 9, "xtick.labelsize": 8, "ytick.labelsize": 8,
    "legend.fontsize": 8, "legend.frameon": False,
    "axes.linewidth": 0.6, "axes.spines.top": False, "axes.spines.right": False,
    "xtick.direction": "out", "ytick.direction": "out",
    "xtick.major.width": 0.6, "ytick.major.width": 0.6,
    "xtick.major.size": 3, "ytick.major.size": 3,
    "errorbar.capsize": 2, "savefig.dpi": 300, "savefig.bbox": "tight",
    "pdf.fonttype": 42,
})
ERR = dict(ecolor="black", elinewidth=0.6, capthick=0.6)


def wilson(k, n, z=1.96):
    """95% Wilson score interval for k successes out of n, as (low, high)."""
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def pct_err(k, n):
    """Percentage and asymmetric error-bar half-widths for k/n."""
    lo, hi = wilson(k, n)
    p = k / n
    return 100 * p, (100 * max(p - lo, 0), 100 * max(hi - p, 0))


def load(data):
    with open(os.path.join(data, "label_sheet_llm.csv"), newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["llm_status"] == "ok"]
    with open(os.path.join(data, "compare_summary.json")) as f:
        summary = json.load(f)
    return rows, summary


def plot_categories(ax, rnd):
    n = len(rnd)
    cat = collections.Counter(c for r in rnd for c in set(r["label_category"]))
    keys = sorted(cat, key=lambda k: cat[k])
    vals, errs = zip(*(pct_err(cat[k], n) for k in keys))
    ax.barh(range(len(keys)), vals, color=BLUE, height=0.65,
            xerr=list(zip(*errs)), error_kw=ERR)
    ax.set_yticks(range(len(keys)), [CATEGORIES[k] for k in keys])
    ax.set_xlabel(f"Updates (%, n = {n})")
    ax.set_xlim(0, None)


def plot_direction(ax, rnd):
    groups = {"All": rnd,
              "Code": [r for r in rnd if "code" in r["change_type"]],
              "Instr./docs": [r for r in rnd if "code" not in r["change_type"]]}
    dirs = [("expands", "Expands", VERMILLION), ("neutral", "Neutral", GREY),
            ("reduces", "Reduces", GREEN)]
    w = 0.26
    for i, (d, label, color) in enumerate(dirs):
        vals, errs = zip(*(pct_err(sum(r["security_direction"] == d for r in g),
                                   len(g)) for g in groups.values()))
        x = [j + (i - 1) * w for j in range(len(groups))]
        ax.bar(x, vals, w, color=color, label=label, yerr=list(zip(*errs)),
               error_kw=ERR)
    ax.set_xticks(range(len(groups)),
                  [f"{g}\n(n = {len(v)})" for g, v in groups.items()])
    ax.set_ylabel("Updates (%) by security-sensitive access")
    ax.set_ylim(0, 100)
    ax.legend(loc="lower right", bbox_to_anchor=(1, 1.0), ncol=3, handlelength=1,
              columnspacing=1, borderaxespad=0.2)


def plot_declared(ax, rows):
    sec = [r for r in rows if r["label_security_relevant"] == "y"]
    n = len(sec)
    keys = [("yes", "Fully"), ("partial", "Partly"), ("no", "Not")]
    vals, errs = zip(*(pct_err(sum(r["declared"] == k for r in sec), n)
                       for k, _ in keys))
    ax.bar(range(len(keys)), vals, 0.6, color=VERMILLION, yerr=list(zip(*errs)),
           error_kw=ERR)
    ax.set_xticks(range(len(keys)), [l for _, l in keys])
    ax.set_xlabel(f"Declared in changelog (n = {n} security-relevant updates)")
    ax.set_ylabel("Updates (%)")
    ax.set_ylim(0, 100)


def plot_precision(ax, summary):
    prec = summary["feature_precision_llm"]
    feats = sorted(prec, key=lambda k: prec[k]["precision"])
    vals, errs, labels = [], [], []
    for k in feats:
        c, n = prec[k]["correct"], prec[k]["correct"] + prec[k]["wrong"]
        v, e = pct_err(c, n)
        vals.append(v)
        errs.append(e)
        labels.append(f"{k.replace('_', ' ')} ({n})")
    ax.barh(range(len(feats)), vals, color=BLUE, height=0.65,
            xerr=list(zip(*errs)), error_kw=ERR)
    ax.set_yticks(range(len(feats)), labels)
    ax.set_xlabel("Detector precision (%)")
    ax.set_xlim(0, 100)


def save(fig, out, name):
    for ext in ("pdf", "png"):
        path = os.path.join(out, f"{name}.{ext}")
        fig.savefig(path)
        print("wrote", path)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="labelling_data")
    args = ap.parse_args()
    out = os.path.join(args.data, "figures")
    os.makedirs(out, exist_ok=True)
    rows, summary = load(args.data)
    rnd = [r for r in rows if r["sample_reason"] == "random"]

    panels = [
        ("1_categories", (3.4, 2.8), lambda ax: plot_categories(ax, rnd)),
        ("2_security_direction", (3.4, 2.6), lambda ax: plot_direction(ax, rnd)),
        ("3_declared", (3.0, 2.4), lambda ax: plot_declared(ax, rows)),
        ("4_detector_precision", (3.4, 3.2), lambda ax: plot_precision(ax, summary)),
    ]
    for name, size, draw in panels:
        fig, ax = plt.subplots(figsize=size)
        draw(ax)
        save(fig, out, name)

    fig, axes = plt.subplots(2, 2, figsize=(7.2, 5.8),
                             gridspec_kw=dict(wspace=0.55, hspace=0.45))
    for ax, letter, (_, _, draw) in zip(axes.flat, "abcd", panels):
        draw(ax)
        ax.text(-0.02, 1.04, f"({letter})", transform=ax.transAxes,
                fontweight="bold", fontsize=10, ha="right", va="bottom")
    save(fig, out, "results_overview")


if __name__ == "__main__":
    main()
