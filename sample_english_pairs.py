#!/usr/bin/env python3
"""
sample_english_pairs.py - Draw a random sample of English skill updates for LLM screening.

Reads screen_sheet.csv (made by: python diff_analysis.py --data <data> --screen-sheet),
which lists every changed update in a fixed random order (screen_order). For each
update it detects the language of SKILL.md in BOTH versions (A and B) with lingua,
offline. An update counts as English when lingua's top language is English with
confidence >= --min-confidence in both versions, so updates that translate a skill
are left out too.

The sample is the first --n English updates in screen_order. Because that order is
random, this is a simple random sample of English updates, and a larger --n later
only adds updates (the first 800 stay the same).

Before detection, code blocks, inline code, URLs and HTML tags are removed: they look
English in almost every skill and would hide, for example, a Chinese text.

Outputs (in --data):
  screen_sheet_languages.csv   every update in the screen sheet with both languages
                               (for describing the language mix of the sample)
  screen_sheet_en<N>.csv       the sample, in screen_sheet.csv format plus language columns;
                               pass it to: llm_label.py --mode screen --sheet <file>

Usage:
  python sample_english_pairs.py --data data/full_v2 --n 800

Safety: ZIPs are read in memory only (via diff_analysis.load_version); nothing is run.
"""

import argparse
import csv
import re
import sys
from collections import Counter
from pathlib import Path

try:
    from lingua import Language, LanguageDetectorBuilder
except ImportError:
    sys.exit("This script needs lingua: pip install lingua-language-detector")

from clawhub_pilot import file_group, skill_folder
from diff_analysis import load_version

csv.field_size_limit(sys.maxsize)

MAX_CHARS = 6000   # enough text to decide; keeps detection fast on very long SKILL.md files
MIN_LETTERS = 40   # below this the text is too short to judge reliably

# Text that looks English in every skill, whatever language the skill is written in.
NOISE_RE = re.compile(r"```.*?```|`[^`\n]*`|https?://\S+|<[^>]+>", re.S)


def skill_md_text(data, owner, slug, version):
    """SKILL.md of one version with the noise removed, or None if it is missing.
    Prefers the top-level SKILL.md; falls back to any instructions file."""
    files = load_version(skill_folder(data, owner, slug), version)
    if not files:
        return None
    text = files.get("SKILL.md")
    if text is None:
        text = next((t for name, t in sorted(files.items())
                     if file_group(name) == "instructions" and t), None)
    if text is None:
        return None
    return NOISE_RE.sub(" ", text)[:MAX_CHARS]


def detect(detector, text):
    """(language name, English confidence) for one text; ("too_short", 0) if too short
    and ("missing", 0) if there is no SKILL.md."""
    if text is None:
        return "missing", 0.0
    if sum(c.isalpha() for c in text) < MIN_LETTERS:
        return "too_short", 0.0
    top = detector.detect_language_of(text)
    name = top.name.lower() if top else "unknown"
    return name, round(detector.compute_language_confidence(text, Language.ENGLISH), 3)


def main():
    p = argparse.ArgumentParser(description="Random sample of English skill updates for screening")
    p.add_argument("--data", type=Path, default=Path("data/full_v2"), help="pipeline folder")
    p.add_argument("--n", type=int, default=800, help="number of English updates to sample (default: 800)")
    p.add_argument("--min-confidence", type=float, default=0.5,
                   help="minimum English confidence for each version (default: 0.5)")
    args = p.parse_args()

    sheet = args.data / "screen_sheet.csv"
    if not sheet.exists():
        sys.exit(f"{sheet} not found - run: python diff_analysis.py --data {args.data} --screen-sheet")
    with open(sheet, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        columns = list(reader.fieldnames)
        rows = sorted(reader, key=lambda r: int(r["screen_order"]))

    detector = LanguageDetectorBuilder.from_all_languages().build()
    cache = {}  # most versions appear in two updates (as B, then as A)

    def language(owner, slug, version):
        key = (owner, slug, version)
        if key not in cache:
            cache[key] = detect(detector, skill_md_text(args.data, owner, slug, version))
        return cache[key]

    lang_cols = ["lang_from", "en_conf_from", "lang_to", "en_conf_to", "is_english"]
    english = []
    for i, r in enumerate(rows, 1):
        lang_a, conf_a = language(r["owner"], r["slug"], r["from_version"])
        lang_b, conf_b = language(r["owner"], r["slug"], r["to_version"])
        ok = (lang_a == lang_b == "english"
              and conf_a >= args.min_confidence and conf_b >= args.min_confidence)
        r.update(lang_from=lang_a, en_conf_from=conf_a, lang_to=lang_b, en_conf_to=conf_b,
                 is_english=int(ok))
        if ok:
            english.append(r)
        if i % 500 == 0:
            print(f"{i} / {len(rows)} updates checked, {len(english)} English so far")

    with open(args.data / "screen_sheet_languages.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["screen_order", "label_id"] + lang_cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    sample = english[:args.n]
    out = args.data / f"screen_sheet_en{args.n}.csv"
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns + lang_cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(sample)

    # Report: why updates were left out, and what the sample looks like.
    def reason(r):
        if r["is_english"]:
            return "english"
        langs = {r["lang_from"], r["lang_to"]}
        if langs & {"missing", "too_short"}:
            return "no usable SKILL.md (missing or too short)"
        if "english" in langs:
            return "language changed between A and B"
        return "not English"

    print(f"\nUpdates in screen sheet: {len(rows)}  (skills: {len({(r['owner'], r['slug']) for r in rows})})")
    for k, v in Counter(reason(r) for r in rows).most_common():
        print(f"  {k:42s} {v:5d}  {v / len(rows):6.1%}")
    langs = Counter(r["lang_to"] for r in rows if not r["is_english"])
    print("Languages of B among the excluded:", dict(langs.most_common(8)))
    print(f"English updates: {len(english)} from {len({(r['owner'], r['slug']) for r in english})} skills")
    if len(sample) < args.n:
        print(f"WARNING: only {len(sample)} English updates exist, fewer than --n {args.n}.")
    last = sample[-1]["screen_order"] if sample else "-"
    print(f"Sample: {len(sample)} updates from {len({(r['owner'], r['slug']) for r in sample})} skills "
          f"(screen_order 1-{last}) -> {out}")
    print(f"Types: {dict(Counter(r['change_type'] for r in sample).most_common())}")


if __name__ == "__main__":
    main()
