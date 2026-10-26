#!/usr/bin/env python3
"""
llm_label.py - label skill updates with Claude through the Claude Code CLI.

For every row of label_sheet.csv (one update A -> B) this script:
  1. builds the input: update header, changelog, the readable .diff file
     (trimmed to a size limit) and the detector's hits with an example line each;
  2. runs `claude -p` with ALL tools disabled, no MCP servers, no saved session,
     the codebook as system prompt and a JSON schema that forces the answer shape;
  3. checks the answer (valid codes, one verdict for every detected feature)
     and retries once if it is invalid;
  4. appends one row to label_sheet_llm.csv and moves on.

The model never runs anything: it only reads text and returns JSON. That matters,
because the diffs are untrusted text written to instruct AI agents.

Your own labels in label_sheet.csv are never modified; results go to a separate
file. Re-running skips rows that already have a successful answer.

Typical use:
    python llm_label.py --rows 1-11      # check against your labels
    python llm_label.py --limit 20       # measure cost/usage
    python llm_label.py                  # everything that is left
    python llm_label.py --dry-run --rows 8   # show the prompt only
--data defaults to labelling_data/ (the single, git-tracked copy of the labels).

Screening mode (--mode screen) labels EVERY changed update instead of the label
sheet: it reads <data>/screen_sheet.csv (made by diff_analysis.py --screen-sheet),
uses Sonnet by default, and does NOT show the detector's hits, so the model judges
each update only from the diff and changelog. It answers the same questions
except the feature verdicts, and writes <data>/screen_llm.csv:
    python llm_label.py --mode screen --data data/full_v2 --limit 50   # measure cost first
    python llm_label.py --mode screen --data data/full_v2 --workers 6  # everything that is left
"""

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_DATA = HERE.parent / "labelling_data"
csv.field_size_limit(sys.maxsize)

# Columns written to label_sheet_llm.csv. The label_* names match label_tool.py,
# so the comparison script can line the two files up column by column.
OUT_COLUMNS = [
    "label_id", "sample_reason", "owner", "slug", "from_version", "to_version",
    "change_type", "detected_features",
    "label_category", "label_security_relevant", "label_correct_features",
    "label_wrong_features", "label_unsure_features", "label_notes", "label_by",
    "security_direction", "declared", "changelog_kind", "llm_reason",
    "llm_model", "llm_status", "llm_error", "llm_attempts", "llm_seconds",
    "llm_cost_usd", "llm_input_tokens", "llm_output_tokens", "diff_truncated",
]

# Columns written to screen_llm.csv: the same, minus the feature verdicts the
# screening model is not asked for, plus the two screening-only answers.
SCREEN_COLUMNS = ["screen_order"] + [c for c in OUT_COLUMNS
                                     if not c.endswith("_features") or c == "detected_features"]
SCREEN_COLUMNS[SCREEN_COLUMNS.index("changelog_kind") + 1:
               SCREEN_COLUMNS.index("changelog_kind") + 1] = ["changelog_accuracy", "patterns",
                                                              "pattern_evidence"]

# Pattern names allowed in screening mode, taken from the schema so both stay in sync.
PATTERNS = set(json.loads((HERE / "screen_schema.json").read_text(encoding="utf-8"))
               ["properties"]["patterns"]["items"]["properties"]["pattern"]["enum"])

CATEGORY_RE = re.compile(r"^[0-9]{1,2}$")
GROUP_ORDER = {"instructions": 0, "docs": 1, "code": 2}

# Messages from the CLI that mean "stop for now, try again later".
LIMIT_HINTS = ("usage limit", "rate limit", "limit reached", "quota", "credit balance",
               "too many requests", "overloaded")


# ---------------------------------------------------------------------------
# Reading inputs
# ---------------------------------------------------------------------------

def read_csv(path):
    """Read a CSV into a list of dicts (empty list if the file does not exist)."""
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def parse_rows(spec):
    """Turn '1-11' or '1,5,9-12' into a set of label_ids (strings)."""
    wanted = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            wanted.update(str(i) for i in range(int(a), int(b) + 1))
        elif part:
            wanted.add(str(int(part)))
    return wanted


def load_examples(features_csv):
    """Map (owner, slug, from, to) -> {feature: example line} from diff_features.csv.
    Optional: without it the model just gets the feature names."""
    examples = {}
    if not features_csv or not features_csv.exists():
        return examples
    with open(features_csv, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            key = (r["owner"], r["slug"], r["from_version"], r["to_version"])
            examples[key] = {k[len("example_"):]: v for k, v in r.items()
                             if k.startswith("example_") and v}
    return examples


def trim_diff(text, max_chars, max_line=400):
    """Shorten a .diff file to roughly max_chars, keeping the most useful parts.

    The file is a header followed by one block per changed file
    ('### path  [group]', then '- ' and '+ ' lines). Blocks are ordered
    instructions -> docs -> code, very long lines are cut, and each block
    gets a share of the budget. Returns (text, was_truncated)."""
    head, _, rest = text.partition("\n\n### ")
    blocks = [("### " + b) for b in rest.split("\n\n### ")] if rest else []

    def group_of(block):
        m = re.match(r"### .*\[(\w+)\]", block)
        return GROUP_ORDER.get(m.group(1) if m else "", 3)

    blocks.sort(key=group_of)
    out, used, truncated = [head.strip()], len(head), False
    for i, block in enumerate(blocks):
        lines = [l if len(l) <= max_line else l[:max_line] + " …[line cut]"
                 for l in block.splitlines()]
        truncated |= any(l.endswith("…[line cut]") for l in lines)
        remaining_blocks = len(blocks) - i
        budget = max(600, (max_chars - used) // remaining_blocks)
        kept, size = [], 0
        for l in lines:
            if size + len(l) + 1 > budget:
                break
            kept.append(l)
            size += len(l) + 1
        if len(kept) < len(lines):
            truncated = True
            kept.append(f"…[{len(lines) - len(kept)} more lines of this file not shown]")
        out.append("\n".join(kept))
        used += size
        if used >= max_chars and i + 1 < len(blocks):
            truncated = True
            out.append(f"…[{len(blocks) - i - 1} more changed files not shown]")
            break
    return "\n\n".join(out), truncated


def build_prompt(row, diff_text, examples, show_detector=True):
    """The user message for one update. Everything from the skill author sits
    inside <untrusted_update>; the detector hits come after it (label mode only)."""
    detected = row.get("detected_features", "").split()
    hits = []
    for f in detected:
        ex = examples.get(f, "")
        if f == "new_domains":
            ex = row.get("new_domain_list", "")
        elif f == "new_cred_env":
            ex = row.get("new_cred_env_list", "")
        hits.append(f"- {f}" + (f": {ex}" if ex else ""))
    features_part = "\n".join(hits) if hits else "(none detected; return an empty features object)"
    if show_detector:
        intro = "Label this update according to the codebook.\n\n"
        detector_part = (
            "Detector hits (judge each one AFTER deciding category and security; give a "
            "verdict for every name listed, using exactly these names as keys):\n"
            f"{features_part}\n")
    else:
        intro = ("Label this update according to the codebook, in screening mode "
                 "(no detector hits; judge it only from the diff and changelog).\n\n")
        detector_part = ""
    return (
        intro +
        "<untrusted_update>\n"
        f"skill: {row['owner']}/{row['slug']}\n"
        f"versions: {row['from_version']} -> {row['to_version']}\n"
        f"change_type: {row.get('change_type', '')}\n"
        f"skill_md_change: {row.get('skill_md_change', '')}\n"
        f"lines_changed: {row.get('lines_changed', '')}\n"
        f"changelog of {row['to_version']}: {row.get('to_changelog', '') or '(empty)'}\n\n"
        f"{diff_text}\n"
        "</untrusted_update>\n\n"
        f"{detector_part}"
    )


# ---------------------------------------------------------------------------
# Calling the model
# ---------------------------------------------------------------------------

def call_claude(prompt, system_prompt, schema, model, timeout, workdir):
    """Run one `claude -p` call with every tool switched off. Returns the parsed
    JSON result printed by the CLI (raises RuntimeError on failure)."""
    cmd = ["claude", "-p",
           "--model", model,
           "--tools", "",                 # no tools at all: the model can only answer
           "--strict-mcp-config",         # and no MCP servers are loaded
           "--no-session-persistence",    # every update judged on its own
           "--output-format", "json",
           "--json-schema", schema,
           "--system-prompt", system_prompt]
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                          timeout=timeout, cwd=workdir)
    out = proc.stdout.strip()
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        raise RuntimeError(f"no JSON from CLI (exit {proc.returncode}): "
                           f"{(proc.stderr or out)[:300]}")
    if data.get("is_error"):
        raise RuntimeError(f"CLI error: {str(data.get('result') or data.get('api_error_status'))[:300]}")
    return data


def validate(answer, detected):
    """Check the model's answer. Returns (clean_answer, problem or None)."""
    if not isinstance(answer, dict):
        return None, "answer is not an object"
    cat = str(answer.get("category", ""))
    if not CATEGORY_RE.match(cat):
        return None, f"bad category {cat!r}"
    feats = answer.get("features") or {}
    missing = [f for f in detected if f not in feats]
    if missing:
        return None, f"no verdict for {missing}"
    bad = {f: v for f, v in feats.items() if f in detected and v not in ("c", "w", "u")}
    if bad:
        return None, f"bad verdicts {bad}"
    answer["features"] = {f: feats[f] for f in detected}  # drop any extra keys
    patterns = answer.get("patterns")
    if patterns is not None:  # screening mode
        if not isinstance(patterns, list) or any(
                not isinstance(p, dict) or p.get("pattern") not in PATTERNS for p in patterns):
            return None, f"bad patterns {str(patterns)[:200]}"
    return answer, None


def screen_fields(answer):
    """The screening-only answers as CSV columns. patterns is a space-separated list
    of names; pattern_evidence keeps each pattern with its diff line, as JSON."""
    found = answer.get("patterns") or []
    return {
        "changelog_accuracy": answer.get("changelog_accuracy", ""),
        "patterns": " ".join(sorted({p["pattern"] for p in found})),
        "pattern_evidence": json.dumps([{"pattern": p["pattern"],
                                         "evidence": str(p.get("evidence", ""))[:200]}
                                        for p in found], ensure_ascii=False),
    }


def label_one(row, args, system_prompt, schema, examples, workdir):
    """Label one update end to end and return the output row."""
    started = time.monotonic()
    out = {c: row.get(c, "") for c in ("label_id", "screen_order", "sample_reason", "owner", "slug",
                                       "from_version", "to_version", "change_type",
                                       "detected_features")}
    screen = args.mode == "screen"
    out.update(llm_model=args.model, label_by=f"llm-{args.model}" + ("-screen" if screen else ""))
    # In screening mode the model sees no detector hits, so it owes no feature verdicts.
    detected = [] if screen else row.get("detected_features", "").split()

    diff_path = args.data / row.get("diff_file", "")
    if not row.get("diff_file") or not diff_path.exists():
        out.update(llm_status="no_diff", llm_error=f"missing {row.get('diff_file')}")
        return out, 0.0
    diff_text, truncated = trim_diff(diff_path.read_text(encoding="utf-8", errors="replace"),
                                     args.max_diff_chars)
    out["diff_truncated"] = int(truncated)
    key = (row["owner"], row["slug"], row["from_version"], row["to_version"])
    prompt = build_prompt(row, diff_text, examples.get(key, {}), show_detector=not screen)

    cost, in_tok, out_tok, problem = 0.0, 0, 0, None
    for attempt in range(1, args.retries + 2):
        try:
            data = call_claude(prompt, system_prompt, schema, args.model, args.timeout, workdir)
        except subprocess.TimeoutExpired:
            problem = "timeout"
            continue
        except RuntimeError as exc:
            problem = str(exc)
            if any(h in problem.lower() for h in LIMIT_HINTS):
                out.update(llm_status="limit", llm_error=problem, llm_attempts=attempt)
                return out, cost
            continue
        cost += float(data.get("total_cost_usd") or 0)
        usage = data.get("usage") or {}
        in_tok += sum(int(usage.get(k) or 0) for k in
                      ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        out_tok += int(usage.get("output_tokens") or 0)
        answer, problem = validate(data.get("structured_output"), detected)
        if answer:
            feats = answer["features"]
            out.update(
                label_category=answer["category"],
                label_security_relevant=answer["security_relevant"],
                label_correct_features=" ".join(f for f, v in feats.items() if v == "c"),
                label_wrong_features=" ".join(f for f, v in feats.items() if v == "w"),
                label_unsure_features=" ".join(f for f, v in feats.items() if v == "u"),
                label_notes=answer["notes"],
                security_direction=answer["security_direction"],
                declared=answer["declared"],
                changelog_kind=answer["changelog_kind"],
                llm_reason=answer["reason"],
                **(screen_fields(answer) if screen else {}),
                llm_status="ok", llm_error="", llm_attempts=attempt)
            break
    else:
        out.update(llm_status="failed", llm_error=problem, llm_attempts=args.retries + 1)
    out.update(llm_seconds=round(time.monotonic() - started, 1), llm_cost_usd=round(cost, 5),
               llm_input_tokens=in_tok, llm_output_tokens=out_tok)
    return out, cost


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", choices=("label", "screen"), default="label",
                   help="label: the label sheet, with detector hits (default). "
                        "screen: every changed update, without detector hits")
    p.add_argument("--data", type=Path, default=DEFAULT_DATA,
                   help="folder with the sheet and diffs/ (default: labelling_data/)")
    p.add_argument("--sheet", type=Path, default=None,
                   help="default: <data>/label_sheet.csv, or <data>/screen_sheet.csv in screen mode")
    p.add_argument("--features", type=Path, default=None,
                   help="diff_features.csv for example lines (default: <data>/diff_features.csv, optional)")
    p.add_argument("--out", type=Path, default=None,
                   help="default: <data>/label_sheet_llm.csv, or <data>/screen_llm.csv in screen mode")
    p.add_argument("--model", default=None,
                   help="model alias or ID passed to claude (default: opus; sonnet in screen mode)")
    p.add_argument("--rows", default=None, help="only these label_ids, e.g. 1-11 or 1,5,9-12")
    p.add_argument("--limit", type=int, default=None, help="label at most this many new rows")
    p.add_argument("--workers", type=int, default=3, help="parallel calls (default: 3)")
    p.add_argument("--budget-usd", type=float, default=None,
                   help="stop starting new rows once this much has been spent in this run")
    p.add_argument("--max-diff-chars", type=int, default=24000,
                   help="trim each diff to about this many characters (default: 24000)")
    p.add_argument("--retries", type=int, default=1, help="extra attempts after an invalid answer")
    p.add_argument("--timeout", type=int, default=300, help="seconds per call (default: 300)")
    p.add_argument("--redo-failed", action="store_true", help="also retry rows that failed before")
    p.add_argument("--dry-run", action="store_true", help="print the prompt for the first row and stop")
    p.add_argument("--allow-api-key", action="store_true",
                   help="run even if ANTHROPIC_API_KEY is set (that bills the API, not your plan)")
    args = p.parse_args()

    screen = args.mode == "screen"
    args.model = args.model or ("sonnet" if screen else "opus")
    sheet = args.sheet or args.data / ("screen_sheet.csv" if screen else "label_sheet.csv")
    out_path = args.out or args.data / ("screen_llm.csv" if screen else "label_sheet_llm.csv")
    features_csv = args.features or args.data / "diff_features.csv"

    if os.environ.get("ANTHROPIC_API_KEY") and not args.allow_api_key and not args.dry_run:
        sys.exit("ANTHROPIC_API_KEY is set, so these calls would be billed to the API. "
                 "Unset it (or pass --allow-api-key if that is what you want).")

    rows = read_csv(sheet)
    if not rows:
        sys.exit(f"No rows in {sheet}")
    system_prompt = (HERE / "codebook.md").read_text(encoding="utf-8")
    schema_file = "screen_schema.json" if screen else "label_schema.json"
    schema = json.dumps(json.loads((HERE / schema_file).read_text(encoding="utf-8")))
    examples = {} if screen else load_examples(features_csv)  # only label mode shows hits

    done = {r["label_id"] for r in read_csv(out_path)
            if r.get("llm_status") == "ok" or (r.get("llm_status") == "failed" and not args.redo_failed)}
    todo = [r for r in rows if r["label_id"] not in done]
    if args.rows:
        wanted = parse_rows(args.rows)
        todo = [r for r in todo if r["label_id"] in wanted]
    if args.limit is not None:
        todo = todo[:args.limit]

    if args.dry_run:
        r = todo[0] if todo else rows[0]
        diff_text, _ = trim_diff((args.data / r["diff_file"]).read_text(encoding="utf-8", errors="replace"),
                                 args.max_diff_chars)
        key = (r["owner"], r["slug"], r["from_version"], r["to_version"])
        print(build_prompt(r, diff_text, examples.get(key, {}), show_detector=not screen))
        return

    print(f"{len(rows)} rows in sheet, {len(done)} already done, {len(todo)} to label "
          f"with model '{args.model}' ({args.workers} at a time). Output: {out_path}")
    if not todo:
        return

    new_file = not out_path.exists()
    lock, spent, n_ok, n_bad = threading.Lock(), 0.0, 0, 0
    stop = threading.Event()
    workdir = tempfile.mkdtemp(prefix="llm_label_")  # empty folder: no project files or CLAUDE.md

    with open(out_path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=SCREEN_COLUMNS if screen else OUT_COLUMNS,
                                extrasaction="ignore")
        if new_file:
            writer.writeheader()

        def task(row):
            if stop.is_set():
                return None, 0.0
            return label_one(row, args, system_prompt, schema, examples, workdir)

        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = [pool.submit(task, r) for r in todo]
            try:
                for i, fut in enumerate(as_completed(futures), 1):
                    out, cost = fut.result()
                    if out is None:
                        continue
                    with lock:
                        spent += cost
                        if out.get("llm_status") == "limit":
                            stop.set()
                            print(f"Usage limit reached: {out['llm_error'][:200]}\n"
                                  "Stopping. Run the same command later to continue.")
                            continue  # not written, so it is retried next time
                        writer.writerow(out)
                        fh.flush()
                        n_ok += out.get("llm_status") == "ok"
                        n_bad += out.get("llm_status") != "ok"
                        print(f"[{i}/{len(todo)}] #{out['label_id']} {out['owner']}/{out['slug'][:30]} "
                              f"{out['from_version']}->{out['to_version']}: {out.get('llm_status')} "
                              f"cat={out.get('label_category', '')} sec={out.get('label_security_relevant', '')} "
                              f"${float(out.get('llm_cost_usd') or 0):.3f} (run total ${spent:.2f})")
                        if args.budget_usd is not None and spent >= args.budget_usd and not stop.is_set():
                            stop.set()
                            print(f"Budget of ${args.budget_usd:.2f} reached; finishing calls in progress.")
            except KeyboardInterrupt:
                stop.set()
                print("\nInterrupted; rows already saved are kept.")
                for f in futures:
                    f.cancel()

    print(f"Done: {n_ok} labelled, {n_bad} failed or missing, ${spent:.2f} spent in this run.")


if __name__ == "__main__":
    main()
