"""Bar charts of the LLM labelling results.

Reads label_sheet_llm.csv and compare_summary.json from --data and writes PNGs to
<data>/figures/. Prevalence charts use only the random sample (sample_reason ==
"random"), since the feature-sampled rows over-represent detector hits.

    python3 llm_labeling/plot_results.py --data labelling_data
"""
import argparse
import collections
import csv
import json
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
INK, INK2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"

CATEGORIES = {
    "1": "wording", "2": "docs", "3": "feature", "4": "fix", "5": "integration",
    "6": "install", "7": "behaviour", "8": "security", "9": "refactor", "0": "other",
}

plt.rcParams.update({
    "font.size": 10, "axes.edgecolor": GRID, "axes.labelcolor": INK2,
    "xtick.color": INK2, "ytick.color": INK, "axes.titlesize": 12,
    "axes.titleweight": "bold", "axes.titlelocation": "left",
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.dpi": 200,
})


def style(ax, axis="x"):
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.grid(axis=axis, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(length=0)


def hbar(ax, labels, values, color=BLUE, fmt="{}"):
    """Horizontal bars, largest on top, value printed at the bar end."""
    y = range(len(labels))[::-1]
    ax.barh(list(y), values, color=color, height=0.7)
    ax.set_yticks(list(y), labels)
    for yi, v in zip(y, values):
        ax.text(v, yi, " " + fmt.format(v), va="center", color=INK2, fontsize=9)
    ax.set_xlim(0, max(values) * 1.15)
    style(ax)


def save(fig, out, name, note):
    fig.text(0.01, 0.01, note, color=INK2, fontsize=8)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    path = os.path.join(out, name)
    fig.savefig(path)
    plt.close(fig)
    print("wrote", path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="labelling_data")
    args = ap.parse_args()
    out = os.path.join(args.data, "figures")
    os.makedirs(out, exist_ok=True)

    with open(os.path.join(args.data, "label_sheet_llm.csv"), newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["llm_status"] == "ok"]
    with open(os.path.join(args.data, "compare_summary.json")) as f:
        summary = json.load(f)
    rnd = [r for r in rows if r["sample_reason"] == "random"]
    n = len(rnd)
    note = f"Random sample of {n} updates, labelled by LLM."

    # 1. Update categories (a two-code label counts toward both codes).
    cat = collections.Counter(c for r in rnd for c in set(r["label_category"]))
    items = sorted(cat.items(), key=lambda kv: -kv[1])
    fig, ax = plt.subplots(figsize=(7, 4))
    hbar(ax, [CATEGORIES[k] for k, _ in items], [100 * v / n for _, v in items],
         fmt="{:.0f}%")
    ax.set_xlabel("% of updates (an update can have two categories)")
    ax.set_title("What kind of change is a skill update?")
    save(fig, out, "1_categories.png", note)

    # 2. Security direction, overall and by what the update touches.
    order = [("expands", ORANGE), ("neutral", "#c3c2b7"), ("reduces", AQUA)]
    groups = collections.defaultdict(collections.Counter)
    for r in rnd:
        groups["all updates"][r["security_direction"]] += 1
        kind = "code" if "code" in r["change_type"] else "instructions/docs only"
        groups[kind][r["security_direction"]] += 1
    names = ["all updates", "code", "instructions/docs only"]
    labels = [f"{g}  (n={sum(groups[g].values())})" for g in names]
    fig, ax = plt.subplots(figsize=(7, 2.8))
    left = [0.0] * len(names)
    y = list(range(len(names)))[::-1]
    for d, color in order:
        pct = [100 * groups[g][d] / sum(groups[g].values()) for g in names]
        ax.barh(y, pct, left=left, color=color, height=0.6, label=d,
                edgecolor=SURFACE, linewidth=2)
        for yi, l, p in zip(y, left, pct):
            if p >= 6:
                ax.text(l + p / 2, yi, f"{p:.0f}%", ha="center", va="center",
                        color=INK, fontsize=9)
        left = [l + p for l, p in zip(left, pct)]
    ax.set_yticks(y, labels)
    ax.set_xlim(0, 100)
    ax.set_xlabel("% of updates")
    ax.legend(ncol=3, frameon=False, loc="lower left", bbox_to_anchor=(0, 1.0))
    ax.set_title("Does the update expand security-sensitive access?", pad=26)
    style(ax)
    save(fig, out, "2_security_direction.png", note)

    # 3. Were security-expanding updates declared in the changelog?
    sec = [r for r in rows if r["label_security_relevant"] == "y"]
    dec = collections.Counter(r["declared"] for r in sec)
    keys = [("yes", "fully declared"), ("partial", "partly declared"),
            ("no", "not declared")]
    fig, ax = plt.subplots(figsize=(7, 2.6))
    hbar(ax, [l for _, l in keys], [dec[k] for k, _ in keys], color=ORANGE)
    ax.set_xlabel("updates")
    ax.set_title("Is the security-relevant change in the changelog?")
    save(fig, out, "3_declared.png",
         f"All {len(sec)} security-relevant updates (random + feature samples), "
         "labelled by LLM.")

    # 4. Detector precision: correct vs wrong hits, sorted by precision.
    prec = summary["feature_precision_llm"]
    feats = sorted(prec, key=lambda k: prec[k]["precision"])
    fig, ax = plt.subplots(figsize=(7.5, 5))
    y = range(len(feats))
    c = [prec[k]["correct"] for k in feats]
    w = [prec[k]["wrong"] for k in feats]
    ax.barh(list(y), c, color=BLUE, height=0.7, label="correct hit",
            edgecolor=SURFACE, linewidth=2)
    ax.barh(list(y), w, left=c, color=ORANGE, height=0.7, label="false positive",
            edgecolor=SURFACE, linewidth=2)
    ax.set_yticks(list(y), feats)
    for yi, k, ci, wi in zip(y, feats, c, w):
        ax.text(ci + wi, yi, f"  {100 * prec[k]['precision']:.0f}% precise",
                va="center", color=INK2, fontsize=9)
    ax.set_xlim(0, max(ci + wi for ci, wi in zip(c, w)) * 1.3)
    ax.set_xlabel("detector hits judged by LLM")
    ax.legend(ncol=2, frameon=False, loc="lower left", bbox_to_anchor=(0, 1.0))
    ax.set_title("How often is each automated detector right?", pad=26)
    style(ax)
    save(fig, out, "4_detector_precision.png",
         "All labelled updates where the detector fired; 'unsure' verdicts omitted.")


if __name__ == "__main__":
    main()
