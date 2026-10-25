#!/usr/bin/env python3
"""
label_tool.py - Label skill updates one at a time in the terminal.

Reads label_sheet.csv (made by: python diff_analysis.py --data data/full --label-sheet 400),
shows each update (what changed, the detected features with an example line each,
and the coloured diff), asks for your labels, and SAVES AFTER EVERY ROW.
You can stop at any time (q or Ctrl+C) and continue later where you left off.

For each update you are asked:
  1. Category        - one or two digits from the menu (e.g. 3 or 35)
  2. Security-relevant? y / n / u (unsure)
                       "yes" = the update expands what the skill can access or do
                       without the user's knowledge or approval
  3. Each detected feature: c (correct) / w (wrong) / u (unsure)
  4. Notes           - one free-text line (Enter to skip)

Commands you can type at any question:
  d  show the full diff (in a pager)      b  go back to the previous update
  s  skip this update for now             q  save and quit
  Enter on its own keeps the current value (useful when revising with b).

Usage:
  python label_tool.py                                  # label data/full/label_sheet.csv
  python label_tool.py --stats                          # progress + feature precision so far
  python label_tool.py --annotator B --out data/full/label_sheet_B.csv --fresh --rows 1-80
        # a second person labels rows 1-80 into their own file, without seeing your labels
"""

import argparse
import csv
import os
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

csv.field_size_limit(sys.maxsize)

CATEGORIES = {
    "1": ("wording", "rewording, typo, formatting, translation"),
    "2": ("docs", "new/changed explanations, examples, usage notes"),
    "3": ("feature", "new functionality (command, parameter, tool)"),
    "4": ("fix", "bug fix or compatibility change"),
    "5": ("integration", "new external service, API, endpoint or credential"),
    "6": ("install", "new/changed dependencies or setup steps"),
    "7": ("behaviour", "changes HOW the agent should act (rules, priorities, confirmations)"),
    "8": ("security", "security notes, hardening, permission tightening"),
    "9": ("refactor", "restructuring without new behaviour"),
    "0": ("other", "anything else - explain in the notes"),
}
NAME_TO_KEY = {name: key for key, (name, _) in CATEGORIES.items()}
LABEL_COLUMNS = ["label_category", "label_security_relevant", "label_correct_features",
                 "label_wrong_features", "label_unsure_features", "label_notes",
                 "label_by", "label_seconds"]


class Command(Exception):
    """Raised when the user types a navigation command instead of an answer."""
    def __init__(self, name):
        super().__init__(name)
        self.name = name


# ---------------------------------------------------------------------------
# Terminal helpers
# ---------------------------------------------------------------------------

class Colors:
    """ANSI colours, switched off when the output is not a terminal or --no-color."""
    def __init__(self, enabled):
        on = enabled and sys.stdout.isatty()
        self.red = "\033[31m" if on else ""
        self.green = "\033[32m" if on else ""
        self.cyan = "\033[36m" if on else ""
        self.yellow = "\033[33m" if on else ""
        self.bold = "\033[1m" if on else ""
        self.dim = "\033[2m" if on else ""
        self.reset = "\033[0m" if on else ""


def colour_diff_line(line, c):
    """Colour one line of a .diff file: removed red, added green, file headers cyan."""
    if line.startswith("+ "):
        return f"{c.green}{line}{c.reset}"
    if line.startswith("- "):
        return f"{c.red}{line}{c.reset}"
    if line.startswith("### "):
        return f"{c.cyan}{c.bold}{line}{c.reset}"
    return line


def show_in_pager(text):
    """Show long text in 'less' (colours kept), or just print it if less is missing."""
    if shutil.which("less") and sys.stdout.isatty():
        subprocess.run(["less", "-R"], input=text.encode("utf-8"))
    else:
        print(text)


def ask(prompt, current=""):
    """Read one answer. Navigation commands (d, b, s, q) raise Command.
    Enter on its own returns the current value."""
    hint = f" [{current}]" if current else ""
    try:
        answer = input(f"{prompt}{hint}: ").strip()
    except EOFError:
        raise Command("q")
    if answer.lower() in ("d", "b", "s", "q"):
        raise Command(answer.lower())
    return answer if answer else current


# ---------------------------------------------------------------------------
# Loading and saving
# ---------------------------------------------------------------------------

def read_csv(path):
    """Read a CSV into (list of dict rows, list of column names)."""
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return list(reader), list(reader.fieldnames or [])


def save_sheet(path, rows, columns):
    """Write the sheet safely: to a temporary file first, then rename it over the
    real one, so a crash can never leave a half-written sheet."""
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


def load_examples(data_dir):
    """Example lines per feature from diff_features.csv, keyed by update, so we can
    show WHY a feature fired without opening the diff."""
    path = data_dir / "diff_features.csv"
    if not path.exists():
        return {}
    rows, _ = read_csv(path)
    out = {}
    for r in rows:
        key = (r["owner"], r["slug"], r["from_version"], r["to_version"])
        out[key] = {k[len("example_"):]: v for k, v in r.items() if k.startswith("example_") and v}
    return out


def parse_rows(spec, n):
    """Turn '1-80' or '5,7,10-12' into a set of label_id numbers."""
    wanted = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            wanted.update(range(int(a), int(b) + 1))
        elif part:
            wanted.add(int(part))
    return {i for i in wanted if 1 <= i <= n}


def is_done(row):
    """A row counts as labelled once it has a category and a security answer."""
    return bool(row.get("label_category")) and bool(row.get("label_security_relevant"))


# ---------------------------------------------------------------------------
# Showing one update
# ---------------------------------------------------------------------------

def show_update(row, position, total, done, data_dir, examples, c, max_lines):
    """Print everything needed to label one update. Returns the full diff text."""
    os.system("clear" if os.name != "nt" else "cls") if sys.stdout.isatty() else None
    key = (row["owner"], row["slug"], row["from_version"], row["to_version"])
    print(f"{c.bold}Update {position}/{total}{c.reset}   {c.dim}({done} labelled, "
          f"label_id {row.get('label_id')}, picked because: {row.get('sample_reason')}){c.reset}")
    print(f"{c.bold}{row['owner']}/{row['slug']}{c.reset}   {row['from_version']} -> {row['to_version']}")
    print(f"change type: {row.get('change_type')}   SKILL.md: {row.get('skill_md_change')}   "
          f"lines changed: {row.get('lines_changed')}")
    if row.get("to_changelog"):
        print(f"changelog:   {c.yellow}{row['to_changelog'][:300]}{c.reset}")
    if row.get("new_domain_list"):
        print(f"new domains: {row['new_domain_list']}")
    if row.get("new_cred_env_list"):
        print(f"new credential variables: {row['new_cred_env_list']}")

    features = row.get("detected_features", "").split()
    if features:
        print(f"\n{c.bold}Detected features (example line that triggered it):{c.reset}")
        ex = examples.get(key, {})
        for f in features:
            print(f"  {c.cyan}{f:26s}{c.reset} {ex.get(f, '')[:150]}")
    else:
        print(f"\n{c.dim}No features detected.{c.reset}")

    diff_path = data_dir / row.get("diff_file", "")
    text = diff_path.read_text(encoding="utf-8") if diff_path.is_file() else "(no diff file found)"
    lines = text.splitlines()
    coloured = "\n".join(colour_diff_line(l, c) for l in lines)
    print(f"\n{c.bold}Diff{c.reset} {c.dim}({len(lines)} lines){c.reset}")
    for l in lines[:max_lines]:
        print(colour_diff_line(l, c))
    if len(lines) > max_lines:
        print(f"{c.yellow}... {len(lines) - max_lines} more lines - type d to see everything{c.reset}")
    return coloured


def print_menu(c):
    """Print the category menu in two columns."""
    items = [f"{k} {name:12s}" for k, (name, _) in CATEGORIES.items()]
    print(f"\n{c.bold}Categories:{c.reset}")
    for i in range(0, len(items), 5):
        print("  " + "  ".join(items[i:i + 5]))
    print(f"{c.dim}  commands: d=full diff  b=back  s=skip  q=quit   (Enter keeps current value){c.reset}")


# ---------------------------------------------------------------------------
# Asking the questions for one update
# ---------------------------------------------------------------------------

def ask_category(current):
    """Ask for one or two categories, as digits (e.g. 3 or 35) or names."""
    current_keys = "".join(NAME_TO_KEY.get(n, "") for n in current.split(";") if n)
    while True:
        answer = ask("1) Category (1-2 digits)", current_keys)
        keys = list(answer) if answer.isdigit() else [NAME_TO_KEY.get(answer.lower(), "?")]
        if keys and len(keys) <= 2 and all(k in CATEGORIES for k in keys):
            return ";".join(dict.fromkeys(CATEGORIES[k][0] for k in keys))
        print("   Please type one or two digits from the menu, e.g. 3 or 35.")


def ask_choice(prompt, options, current=""):
    """Ask until the answer is one of the allowed single letters."""
    while True:
        answer = ask(prompt, current).lower()
        if answer in options:
            return options[answer]
        if answer in options.values():
            return answer
        print(f"   Please type one of: {', '.join(options)}.")


def ask_features(row, c):
    """For each detected feature ask: correct / wrong / unsure."""
    features = row.get("detected_features", "").split()
    if not features:
        return "", "", ""
    old = {}
    for col, verdict in (("label_correct_features", "c"), ("label_wrong_features", "w"),
                         ("label_unsure_features", "u")):
        for f in (row.get(col) or "").split():
            old[f] = verdict
    print(f"3) For each detected feature: {c.bold}c{c.reset}=correct  "
          f"{c.bold}w{c.reset}=wrong  {c.bold}u{c.reset}=unsure")
    result = {"c": [], "w": [], "u": []}
    for f in features:
        verdict = ask_choice(f"   {f}", {"c": "c", "w": "w", "u": "u"}, old.get(f, ""))
        result[verdict].append(f)
    return " ".join(result["c"]), " ".join(result["w"]), " ".join(result["u"])


def label_one(row, c, annotator):
    """Ask all questions for one update and store the answers in the row."""
    start = time.time()
    category = ask_category(row.get("label_category", ""))
    security = ask_choice("2) Security-relevant? y/n/u", {"y": "yes", "n": "no", "u": "unsure"},
                          row.get("label_security_relevant", ""))
    correct, wrong, unsure = ask_features(row, c)
    notes = ask("4) Notes (Enter to skip)", row.get("label_notes", ""))
    previous_seconds = float(row.get("label_seconds") or 0)
    row.update({
        "label_category": category,
        "label_security_relevant": security,
        "label_correct_features": correct,
        "label_wrong_features": wrong,
        "label_unsure_features": unsure,
        "label_notes": notes,
        "label_by": annotator,
        "label_seconds": round(previous_seconds + time.time() - start),
    })


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def print_stats(rows):
    """Progress, category shares (random half) and feature precision (all rows)."""
    done = [r for r in rows if is_done(r)]
    print(f"Labelled: {len(done)} / {len(rows)}")
    if not done:
        return
    seconds = [float(r.get("label_seconds") or 0) for r in done]
    print(f"Average time per update: {sum(seconds) / len(seconds):.0f} s")

    random_rows = [r for r in done if r.get("sample_reason") == "random"]
    cats = Counter(cat for r in random_rows for cat in r["label_category"].split(";"))
    print(f"\nCategories in the RANDOM half ({len(random_rows)} updates; an update can have two):")
    for cat, n in cats.most_common():
        print(f"  {cat:12s} {n:4d}  {n / max(1, len(random_rows)):6.1%}")
    sec = Counter(r["label_security_relevant"] for r in random_rows)
    print(f"Security-relevant in the random half: {dict(sec)}")

    counts = defaultdict(Counter)
    for r in done:
        for col, verdict in (("label_correct_features", "correct"),
                             ("label_wrong_features", "wrong"),
                             ("label_unsure_features", "unsure")):
            for f in (r.get(col) or "").split():
                counts[f][verdict] += 1
    if counts:
        print("\nFeature precision so far (correct / (correct + wrong)):")
        for f, k in sorted(counts.items()):
            judged = k["correct"] + k["wrong"]
            prec = f"{k['correct'] / judged:6.1%}" if judged else "   n/a"
            print(f"  {f:26s} {prec}   correct {k['correct']:3d}  wrong {k['wrong']:3d}  "
                  f"unsure {k['unsure']:3d}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    """Load the sheet, then show and label updates until done or the user quits."""
    p = argparse.ArgumentParser(description="Label skill updates in the terminal")
    p.add_argument("--data", default="data/full", help="data folder (default: data/full)")
    p.add_argument("--sheet", default=None, help="sheet to read (default: <data>/label_sheet.csv)")
    p.add_argument("--out", default=None, help="file to save labels in (default: same as --sheet)")
    p.add_argument("--annotator", default="A", help="your initials, stored with each label")
    p.add_argument("--fresh", action="store_true",
                   help="start without existing labels (for a second annotator; needs --out)")
    p.add_argument("--rows", default=None, help="only these label_ids, e.g. 1-80 or 1,5,9-12")
    p.add_argument("--lines", type=int, default=60, help="diff lines shown before 'd' (default 60)")
    p.add_argument("--no-color", action="store_true", help="plain text output")
    p.add_argument("--stats", action="store_true", help="only print progress and precision")
    args = p.parse_args()

    data_dir = Path(args.data)
    sheet = Path(args.sheet) if args.sheet else data_dir / "label_sheet.csv"
    out = Path(args.out) if args.out else sheet
    if not sheet.exists():
        sys.exit(f"{sheet} not found. Create it with: python diff_analysis.py --data {data_dir} "
                 f"--label-sheet 400")
    if args.fresh and out == sheet:
        sys.exit("--fresh would erase labels in the original sheet; use it together with --out.")

    # Continue from the output file if it already exists (resuming).
    source = out if out.exists() else sheet
    rows, columns = read_csv(source)
    for col in LABEL_COLUMNS:
        if col not in columns:
            columns.append(col)
    if args.fresh and source == sheet:
        for r in rows:
            for col in LABEL_COLUMNS:
                r[col] = ""

    if args.stats:
        print_stats(rows)
        return

    # Keep one backup of the sheet as it was before this session.
    if out.exists():
        shutil.copyfile(out, Path(str(out) + ".bak"))

    selected = list(range(len(rows)))
    if args.rows:
        wanted = parse_rows(args.rows, len(rows))
        selected = [i for i in selected if int(rows[i].get("label_id") or i + 1) in wanted]

    c = Colors(not args.no_color)
    examples = load_examples(data_dir)
    history = []          # positions visited, for "b" (back)
    skipped = set()
    pos = 0

    def next_open(start):
        """Index (in `selected`) of the next unlabelled, not-skipped update from start."""
        for j in range(start, len(selected)):
            if not is_done(rows[selected[j]]) and j not in skipped:
                return j
        return None

    pos = next_open(0)
    try:
        while pos is not None:
            row = rows[selected[pos]]
            done = sum(is_done(rows[i]) for i in selected)
            full_diff = show_update(row, pos + 1, len(selected), done, data_dir, examples, c, args.lines)
            print_menu(c)
            try:
                label_one(row, c, args.annotator)
                save_sheet(out, rows, columns)
                history.append(pos)
                pos = next_open(pos + 1)
            except Command as cmd:
                if cmd.name == "q":
                    break
                if cmd.name == "d":
                    show_in_pager(full_diff)
                elif cmd.name == "s":
                    skipped.add(pos)
                    pos = next_open(pos + 1)
                elif cmd.name == "b":
                    if history:
                        pos = history.pop()
                    else:
                        print("   (nothing to go back to)")
                        time.sleep(1)
    except KeyboardInterrupt:
        print()
    save_sheet(out, rows, columns)

    done = sum(is_done(rows[i]) for i in selected)
    print(f"\nSaved to {out}. Labelled {done} / {len(selected)}.")
    if pos is None and skipped:
        print(f"{len(skipped)} skipped update(s) are still open; run the tool again to label them.")
    elif pos is None:
        print("All selected updates are labelled. See: python label_tool.py --stats")


if __name__ == "__main__":
    main()