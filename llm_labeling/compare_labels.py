#!/usr/bin/env python3
"""
compare_labels.py - compare the LLM's labels with yours, and summarise them.

Reads label_sheet.csv (your labels) and label_sheet_llm.csv (the LLM's), matched
by label_id, and reports:
  - agreement on the rows both have labelled: category (exact, and sharing at
    least one code), security relevance (with Cohen's kappa), and per-feature
    verdicts;
  - from the LLM labels: precision of each detector feature (share of hits judged
    correct), the category mix and the security-relevant share on the RANDOM half
    only (the feature half over-samples unusual updates).

Writes compare_summary.json and compare_disagreements.csv next to the LLM file.

    python compare_labels.py --data data/full
"""

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

csv.field_size_limit(sys.maxsize)


def read(path):
    """Read a CSV into {label_id: row}."""
    if not path.exists():
        sys.exit(f"Missing {path}")
    with open(path, newline="", encoding="utf-8") as fh:
        return {r["label_id"]: r for r in csv.DictReader(fh)}


CATEGORY_NAMES = {"1": "wording", "2": "docs", "3": "feature", "4": "fix", "5": "integration",
                  "6": "install", "7": "behaviour", "8": "security", "9": "refactor", "0": "other"}


def categories(value):
    """Category as a set of names, whether stored as codes ('97') or as names
    ('refactor;behaviour', as label_tool.py saves them)."""
    value = (value or "").strip().lower()
    if not value:
        return set()
    if value.isdigit():
        return {CATEGORY_NAMES[d] for d in value}
    return {p.strip() for p in value.replace(",", ";").split(";") if p.strip()}


def security(value):
    """'y' / 'n' / 'u' from either 'y' or 'yes' style values."""
    value = (value or "").strip().lower()
    return value[:1] if value[:1] in ("y", "n", "u") else value


def verdicts(row):
    """{feature: 'c'|'w'|'u'} from the label_*_features columns of one row."""
    out = {}
    for col, v in (("label_correct_features", "c"), ("label_wrong_features", "w"),
                   ("label_unsure_features", "u")):
        for f in (row.get(col) or "").split():
            out[f] = v
    return out


def kappa(pairs):
    """Cohen's kappa for a list of (a, b) labels."""
    n = len(pairs)
    if not n:
        return None
    po = sum(a == b for a, b in pairs) / n
    ca, cb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    return None if pe == 1 else round((po - pe) / (1 - pe), 3)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", type=Path, default=Path("data/full"))
    p.add_argument("--human", type=Path, default=None, help="default: <data>/label_sheet.csv")
    p.add_argument("--llm", type=Path, default=None, help="default: <data>/label_sheet_llm.csv")
    args = p.parse_args()
    human = read(args.human or args.data / "label_sheet.csv")
    llm_path = args.llm or args.data / "label_sheet_llm.csv"
    llm = {k: r for k, r in read(llm_path).items() if r.get("llm_status") == "ok"}

    # ---- agreement on rows both labelled ----
    both = [k for k in llm if human.get(k, {}).get("label_category")]
    cat_exact = cat_overlap = 0
    sec_pairs, feat_pairs, disagreements = [], [], []
    for k in both:
        h, m = human[k], llm[k]
        hc, mc = categories(h["label_category"]), categories(m["label_category"])
        cat_exact += hc == mc
        cat_overlap += bool(hc & mc)
        hc, mc = "+".join(sorted(hc)), "+".join(sorted(mc))
        hs, ms = security(h["label_security_relevant"]), security(m["label_security_relevant"])
        sec_pairs.append((hs, ms))
        hv, mv = verdicts(h), verdicts(m)
        feat_diff = []
        for f in sorted(set(hv) | set(mv)):
            feat_pairs.append((hv.get(f, "-"), mv.get(f, "-")))
            if hv.get(f) != mv.get(f):
                feat_diff.append(f"{f}: you={hv.get(f, '-')} llm={mv.get(f, '-')}")
        if hc != mc or hs != ms or feat_diff:
            disagreements.append({
                "label_id": k, "skill": f"{m['owner']}/{m['slug']}",
                "update": f"{m['from_version']}->{m['to_version']}",
                "category_you": hc, "category_llm": mc,
                "security_you": hs, "security_llm": ms,
                "feature_differences": "; ".join(feat_diff),
                "llm_reason": m.get("llm_reason", ""), "your_notes": h.get("label_notes", "")})

    # ---- descriptive results from the LLM labels ----
    precision = defaultdict(Counter)
    for r in llm.values():
        for f, v in verdicts(r).items():
            precision[f][v] += 1
    random_rows = [r for r in llm.values() if r.get("sample_reason") == "random"]
    cat_mix = Counter(c for r in random_rows for c in categories(r["label_category"]))
    sec_random = Counter(security(r["label_security_relevant"]) for r in random_rows)
    direction = Counter(r.get("security_direction", "") for r in random_rows)
    changelog = Counter(r.get("changelog_kind", "") for r in random_rows)
    sec_by_type = defaultdict(Counter)
    for r in random_rows:
        sec_by_type[r.get("change_type", "")][security(r["label_security_relevant"])] += 1

    summary = {
        "llm_rows_ok": len(llm),
        "agreement": {
            "rows_compared": len(both),
            "category_exact": f"{cat_exact}/{len(both)}",
            "category_share_a_code": f"{cat_overlap}/{len(both)}",
            "security_same": f"{sum(a == b for a, b in sec_pairs)}/{len(sec_pairs)}",
            "security_kappa": kappa(sec_pairs),
            "feature_verdicts_same": f"{sum(a == b for a, b in feat_pairs)}/{len(feat_pairs)}",
            "feature_verdict_kappa": kappa(feat_pairs),
        },
        "feature_precision_llm": {
            f: {"correct": c["c"], "wrong": c["w"], "unsure": c["u"],
                "precision": round(c["c"] / (c["c"] + c["w"]), 3) if c["c"] + c["w"] else None}
            for f, c in sorted(precision.items())},
        "random_half": {
            "rows": len(random_rows),
            "category_codes": dict(cat_mix.most_common()),
            "security_relevant": dict(sec_random),
            "security_direction": dict(direction),
            "changelog_kind": dict(changelog),
            "security_by_change_type": {t: dict(c) for t, c in sorted(sec_by_type.items())},
        },
    }

    out_dir = llm_path.parent
    (out_dir / "compare_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    with open(out_dir / "compare_disagreements.csv", "w", newline="", encoding="utf-8") as fh:
        cols = ["label_id", "skill", "update", "category_you", "category_llm", "security_you",
                "security_llm", "feature_differences", "llm_reason", "your_notes"]
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(disagreements)

    a = summary["agreement"]
    print(f"LLM rows labelled: {len(llm)}")
    print(f"Compared with your labels on {a['rows_compared']} rows:")
    print(f"  category exact      {a['category_exact']}   (share a code: {a['category_share_a_code']})")
    print(f"  security same       {a['security_same']}   kappa {a['security_kappa']}")
    print(f"  feature verdicts    {a['feature_verdicts_same']}   kappa {a['feature_verdict_kappa']}")
    print("Detector precision according to the LLM (correct / (correct + wrong)):")
    for f, s in summary["feature_precision_llm"].items():
        print(f"  {f:26s} {s['precision'] if s['precision'] is not None else '-':>6}   "
              f"(c {s['correct']}, w {s['wrong']}, u {s['unsure']})")
    print(f"Random half ({len(random_rows)} rows): security {dict(sec_random)}")
    print(f"Saved {out_dir / 'compare_summary.json'} and {out_dir / 'compare_disagreements.csv'}")


if __name__ == "__main__":
    main()
