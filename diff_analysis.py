#!/usr/bin/env python3
"""
diff_analysis.py - What exactly changed in each skill update?

Works OFFLINE on the ZIPs already downloaded by clawhub_pilot.py.
For every update (version A -> B) in consecutive_sampled_versions.csv it opens
both ZIPs in memory, compares the files line by line and measures what B ADDED compared to A:

  1. How SKILL.md changed:
       none        - SKILL.md identical
       trivial     - only a version/date line or whitespace changed
       frontmatter - only the metadata block at the top changed (name, description ...)
       body        - the instruction text itself changed
  2. Size of the change: lines added / removed per file group (instructions, docs, code),
     files added / removed.
  3. Security-relevant features, counted on ADDED lines only (the "delta").
     WEAK features are common in normal skills; STRONG ones are rarely innocent:
       directive_lines (weak)     - sentences instructing the agent (always, must, 必须 ...)
       conceal_lines (strong)     - hiding things from the user (do not tell, silently, 不要告诉 ...)
       override_lines (strong)    - overriding other instructions (ignore previous ..., 忽略之前 ...)
       secret_lines (weak)        - any mention of keys, tokens, passwords, credentials
       secret_access_lines (strong) - well-known secret locations (~/.ssh, .aws/credentials, .netrc ...)
       new_cred_env               - credential variables (FOO_API_KEY ...) that B needs and A did not
       new_domains                - domains new in B (common harmless ones like github.com removed)
       exec_net_lines             - added code that runs commands or uses the network
       install_lines              - added install steps (pip install, curl | sh ...)
       persistence_lines (strong) - sudo, services, cron, shell start-up files
       agent_config_lines         - hooks, agent settings, permissions, AGENTS.md ...
       disclosure_lines           - security notes added by the author ("Risk: ... Mitigation: ...")
       combo_secret_and_egress    - secret access AND a way to send data out in the same update
  4. Changelog check: does the changelog call the update minor ("fix typo", "minor") while
     the update is large or adds security-relevant features?

What screening mode (llm_label.py --mode screen) uses from this script:
  [SCREEN]    reading versions, classify_skill_md (-> skill_md_change), the line diff
              (-> lines_changed and the .diff file) and write_screen_sheet.
  [BASELINE]  the phrase lists and features. Never shown to the screening model; they are
              the regex baseline compared with the LLM later, and the input of label mode.
  [LABEL]     write_label_sheet, only for label mode / manual labelling.
Sections below are tagged with these names.

IMPORTANT: the phrase lists below are a STARTING POINT. Refine them with your manual
labels (RQ1 taxonomy) and report their precision/recall in the thesis.

Outputs (in --data, default data/full):
  diffs/diff_features.csv              one row per update with all measurements
  diffs/diff_summary.json              overall numbers (base rates of every feature)
  diffs/<owner>/<slug>/<A>__<B>.diff   readable added/removed lines (for manual labelling);
                                       .diff files of updates no longer in the input are removed
  label_sheet.csv        (with --label-sheet N) stratified sample of updates to label by hand,
                         to be copied into labelling_data/ (it never overwrites labels there)
  screen_sheet.csv       (with --screen-sheet) EVERY update with changed content, in the same
                         format, for screening all of them with llm_label.py --mode screen

Usage (from the folder that contains clawhub_pilot.py):
  python diff_analysis.py --data data/full
  python diff_analysis.py --data data/full --label-sheet 400   # also create the labelling sheet
  python diff_analysis.py --data data/full --screen-sheet      # also list every update for LLM screening

Safety: ZIPs are only read in memory. Nothing is extracted to disk, nothing is executed.
"""

import argparse
import csv
import io
import json
import re
import sys
import zipfile
from collections import Counter
from pathlib import Path

# Reuse the exact same rules as the crawler (folder names, file groups, _meta.json),
# so both scripts always agree on what "instructions", "docs" and "code" mean.
try:
    from clawhub_pilot import UPDATES_FILE, file_group, safe_name, skill_folder
except ImportError:
    sys.exit("Put diff_analysis.py in the same folder as clawhub_pilot.py and run it from there.")

csv.field_size_limit(sys.maxsize)

MAX_FILE_BYTES = 2_000_000      # skip larger files (data dumps, minified bundles)
MAX_DIFF_FILE_BYTES = 200_000   # cap each saved .diff file

# Everything this script writes, inside --data.
DIFFS_DIR = "diffs"
FEATURES_FILE = f"{DIFFS_DIR}/diff_features.csv"
DIFF_SUMMARY_FILE = f"{DIFFS_DIR}/diff_summary.json"

# ---------------------------------------------------------------------------
# [BASELINE] Phrase lists (regular expressions). Starting points - refine with your labels.
# Not shown to the screening model; TRIVIAL_KEYS_RE is the exception ([SCREEN],
# used by classify_skill_md).
# ---------------------------------------------------------------------------

URL_RE = re.compile(r"https?://([A-Za-z0-9.-]+\.[A-Za-z]{2,})", re.I)

# Frontmatter keys whose change is "trivial" (bookkeeping, not behaviour).
TRIVIAL_KEYS_RE = re.compile(r"^\s*(version|updated|updated_at|last_updated|date|modified)\s*:", re.I)

# Each feature comes in a STRONG variant (rarely innocent) and a WEAK variant
# (common in normal skills). The first full run showed that the broad lists
# fire on ~65% of benign updates, so only the strong variants are useful as
# alarms; the weak ones describe the ecosystem.
# English plus the most common Chinese phrasings (many skills are Chinese).

# WEAK: any sentence that addresses the agent with an obligation.
DIRECTIVE_RE = re.compile(
    r"(\b(you must|you should always|always|never|important:|critical:|mandatory"
    r"|before (answering|responding|replying|doing anything)|automatically)\b"
    r"|必须|务必|一定要|自动)", re.I)

# STRONG: telling the agent to hide something from the user.
CONCEAL_RE = re.compile(
    r"(\b(do not|don't|never) (tell|mention|inform|reveal|show|notify|disclose)\b"
    r"|\bwithout (telling|informing|notifying|asking|confirmation|confirming|the user'?s? knowledge)\b"
    r"|\bsilently\b|\bin the background\b|\bhide (this|it) from\b"
    r"|不要(告诉|提及|通知|告知|透露|显示)|不(告知|通知)用户|静默|悄悄|无需(确认|询问)|不需要(确认|询问))", re.I)

# STRONG: telling the agent to override other instructions.
OVERRIDE_RE = re.compile(
    r"(\b(ignore|disregard|forget|override) (all |any |the )?(previous|prior|earlier|above|other|system)"
    r"( instructions| rules| prompts?)?\b|\bregardless of (what|any|the user)"
    r"|忽略(之前|以上|先前|其他|所有)|无视(之前|以上|规则))", re.I)

# WEAK: any mention of secrets or credentials (setup text is full of these).
SECRET_RE = re.compile(
    r"(\.env\b|~/\.ssh|id_rsa|id_ed25519|authorized_keys|\.aws/|aws_(access|secret)|api[_-]?key"
    r"|secret[_-]?key|access[_-]?token|auth[_-]?token|bearer\s|password|passwd|credential"
    r"|private[_-]?key|keychain|seed phrase|mnemonic|wallet|\.npmrc|\.netrc|\.git-credentials"
    r"|cookies?\.sqlite|login data)", re.I)

# STRONG: accessing well-known secret LOCATIONS (not just talking about keys).
SECRET_ACCESS_RE = re.compile(
    r"(~/\.ssh|\.ssh/|id_rsa|id_ed25519|id_ecdsa|authorized_keys|\.aws/credentials|\.aws/config"
    r"|\.netrc|\.npmrc|\.pypirc|\.git-credentials|\.docker/config\.json|\.kube/config"
    r"|cookies\.sqlite|login data|local state|keychain|keyring|wallet\.dat|seed phrase|mnemonic"
    r"|/etc/passwd|/etc/shadow|\.bash_history|\.zsh_history"
    r"|(cat|read|open|load|source)\s*\(?\s*['\"]?[^'\"\s]*\.env\b)", re.I)

# New credential-like environment variables (e.g. FOO_API_KEY) = the skill now
# asks for access to one more service. Compared as sets between versions.
CRED_ENV_RE = re.compile(
    r"\b[A-Z][A-Z0-9_]*(API_KEY|APIKEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS|PRIVATE_KEY|ACCESS_KEY)\b")
PLACEHOLDER_ENV_RE = re.compile(r"^(YOUR|MY|EXAMPLE|SAMPLE|DUMMY|FAKE|TEST)_|PLACEHOLDER|XXX")

# Authors adding security disclosures ("Risk: ... Mitigation: ..."), apparently
# often prompted by the registry scanner's guidance.
DISCLOSURE_RE = re.compile(
    r"(^\s*[-*]*\s*\**\s*(risk|mitigation|security (note|notice|reminder|warning|considerations?))\b"
    r"|安全提醒|安全提示|风险提示|风险：|缓解措施)", re.I)

# Domains that are normal to add (the platform itself, code hosting front pages,
# badges, examples, XML namespaces). raw.githubusercontent.com is deliberately
# NOT here: it is a common place to host payloads.
DOMAIN_ALLOWLIST = {"clawhub.ai", "www.clawhub.ai", "github.com", "www.github.com", "img.shields.io",
                    "shields.io", "example.com", "example.org", "example.net", "www.example.com",
                    "w3.org", "www.w3.org", "schema.org", "json-schema.org", "localhost"}

# Code that executes commands or talks to the network.
EXEC_NET_RE = re.compile(
    r"(subprocess|os\.system|os\.popen|\bexec\(|\beval\(|child_process|execSync|spawn\("
    r"|\bcurl\b|\bwget\b|requests\.(get|post|put|patch|delete)|\bfetch\(|axios|urllib|http\.client"
    r"|socket\.|websocket|\bnc\s+-|invoke-webrequest|base64\s+-d|b64decode|atob\()", re.I)

# Install / setup steps.
INSTALL_RE = re.compile(
    r"(pip3? install|uv pip install|npm (i|install)\b|npx\s|yarn add|pnpm add|apt(-get)? install"
    r"|brew install|go install|cargo install|gem install|curl[^|\n]*\|\s*(ba)?sh|wget[^|\n]*\|\s*(ba)?sh"
    r"|chmod \+x)", re.I)

# STRONG: system-level persistence and privilege.
PERSIST_RE = re.compile(
    r"(\bsudo\b|systemd|systemctl|crontab|launchctl|launchagents|\.bashrc|\.zshrc|\.profile\b"
    r"|/etc/rc\.local|schtasks|registry run key)", re.I)

# Agent-configuration changes (hooks, settings, permissions, instruction files).
AGENT_CONFIG_RE = re.compile(
    r"(settings\.json|\bhooks?\b|allowed[_-]?tools|permissions?:|always:\s*true|agents\.md"
    r"|claude\.md|\.cursorrules|mcp\.json)", re.I)

# Changelogs that describe the update as small.
MINOR_CHANGELOG_RE = re.compile(
    r"\b(typo|minor|small|tiny|cleanup|clean up|bump|formatting|format|wording|readme|docs?|polish|tweak)\b", re.I)

# Every line-level feature: (column name, regex, file groups it applies to).
# None = all groups.
LINE_FEATURES = (
    ("directive_lines", DIRECTIVE_RE, ("instructions", "docs")),      # weak
    ("conceal_lines", CONCEAL_RE, ("instructions", "docs")),          # strong
    ("override_lines", OVERRIDE_RE, ("instructions", "docs")),        # strong
    ("secret_lines", SECRET_RE, None),                                # weak
    ("secret_access_lines", SECRET_ACCESS_RE, None),                  # strong
    ("exec_net_lines", EXEC_NET_RE, ("code",)),
    ("install_lines", INSTALL_RE, None),
    ("persistence_lines", PERSIST_RE, None),                          # strong
    ("agent_config_lines", AGENT_CONFIG_RE, None),
    ("disclosure_lines", DISCLOSURE_RE, ("instructions", "docs")),    # descriptive
)
FEATURES = tuple(name for name, _, _ in LINE_FEATURES)
STRONG = ("conceal_lines", "override_lines", "secret_access_lines", "persistence_lines")
# Everything listed in a sheet's detected_features column.
DETECTED_COLS = FEATURES + ("new_domains", "new_cred_env", "combo_secret_and_egress",
                            "changelog_mismatch")
# Columns shared by label_sheet.csv and screen_sheet.csv (read by label_tool.py and llm_label.py).
SHEET_COLUMNS = ["label_id", "sample_reason", "owner", "slug", "from_version", "to_version",
                 "change_type", "skill_md_change", "lines_changed", "detected_features",
                 "new_domain_list", "new_cred_env_list", "to_changelog", "diff_file"]


# ---------------------------------------------------------------------------
# [SCREEN] Reading a version
# ---------------------------------------------------------------------------

def load_version(skill_dir, version):
    """Read one version's ZIP from disk and return {path: text} for its author files.

    Same normalisation as the crawler's hashing: the common top-level folder is
    removed and the registry's _meta.json is dropped. Binary or very large files
    are kept with text None, so they still count as added/removed files.
    Returns None when this version has no downloaded ZIP."""
    outcome_file = skill_dir / "download" / f"{safe_name(version)}.json"
    if not outcome_file.exists():
        return None
    outcome = json.loads(outcome_file.read_text(encoding="utf-8"))
    if outcome.get("kind") != "zip":
        return None
    try:
        with zipfile.ZipFile(skill_dir / outcome["zip_file"]) as zf:
            raw = [(i.filename, zf.read(i) if i.file_size <= MAX_FILE_BYTES else None)
                   for i in zf.infolist() if not i.is_dir()]
    except (zipfile.BadZipFile, FileNotFoundError, KeyError):
        return None

    names = [n for n, _ in raw]
    first = {n.split("/", 1)[0] for n in names}
    if len(first) == 1 and all("/" in n for n in names):
        cut = len(next(iter(first))) + 1
        raw = [(n[cut:], d) for n, d in raw]

    files = {}
    for name, data in raw:
        if file_group(name) == "registry":
            continue
        text = None
        if data is not None:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = None  # binary file
        files[name] = text
    return files


def split_frontmatter(text):
    """Split a SKILL.md into (frontmatter, body).
    Frontmatter is the metadata block between the first two '---' lines."""
    if text and text.lstrip().startswith("---"):
        lines = text.lstrip().splitlines()
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return "\n".join(lines[1:i]), "\n".join(lines[i + 1:])
    return "", text or ""


def added_removed(old, new):
    """Lines present in `new` but not `old` (added) and vice versa (removed).
    Counted as multisets, so moving lines around does not count as a change.
    Blank lines are ignored."""
    a = Counter(l.rstrip() for l in (old or "").splitlines() if l.strip())
    b = Counter(l.rstrip() for l in (new or "").splitlines() if l.strip())
    return list((b - a).elements()), list((a - b).elements())


# ---------------------------------------------------------------------------
# [SCREEN] Comparing two versions. compare() also counts the [BASELINE] features
# in the same pass over the added lines.
# ---------------------------------------------------------------------------

def classify_skill_md(old_files, new_files):
    """Say how SKILL.md changed: none / trivial / frontmatter / body / added / removed."""
    def find(files):
        for name, text in files.items():
            if file_group(name) == "instructions":
                return text
        return None

    old, new = find(old_files), find(new_files)
    if old is None and new is None:
        return "absent"
    if old is None:
        return "added"
    if new is None:
        return "removed"
    if old == new:
        return "none"
    old_fm, old_body = split_frontmatter(old)
    new_fm, new_body = split_frontmatter(new)
    if old_body.split() != new_body.split():  # ignore pure whitespace changes
        return "body"
    if old_fm.split() == new_fm.split():
        return "trivial"  # only whitespace differed
    fm_added, fm_removed = added_removed(old_fm, new_fm)
    if all(TRIVIAL_KEYS_RE.match(l) for l in fm_added + fm_removed):
        return "trivial"
    return "frontmatter"


def compare(old_files, new_files):
    """Measure everything that changed from version A (old) to version B (new).
    Returns (feature dict, list of diff text blocks for the .diff file)."""
    out = Counter()
    diff_blocks = []
    old_domains, old_env = set(), set()
    for text in old_files.values():
        if text:
            old_domains.update(d.lower() for d in URL_RE.findall(text))
            old_env.update(m.group(0) for m in CRED_ENV_RE.finditer(text))
    new_domains, new_env = set(), set()
    for text in new_files.values():
        if text:
            new_env.update(m.group(0) for m in CRED_ENV_RE.finditer(text))

    for name in sorted(set(old_files) | set(new_files)):
        group = file_group(name)
        old, new = old_files.get(name), new_files.get(name)
        if name not in old_files:
            out[f"{group}_files_added"] += 1
        elif name not in new_files:
            out[f"{group}_files_removed"] += 1
        if old == new:
            continue
        added, removed = added_removed(old, new)
        out[f"{group}_lines_added"] += len(added)
        out[f"{group}_lines_removed"] += len(removed)

        for line in added:
            new_domains.update(d.lower() for d in URL_RE.findall(line))
            for feature, regex, groups in LINE_FEATURES:
                if (groups is None or group in groups) and regex.search(line):
                    out[feature] += 1
                    # Keep one example line per feature, to help manual labelling.
                    out.setdefault(f"example_{feature}", line.strip()[:200])

        if added or removed:
            block = [f"### {name}  [{group}]"]
            block += [f"- {l}" for l in removed[:300]] + [f"+ {l}" for l in added[:300]]
            diff_blocks.append("\n".join(block))

    def bare(domain):
        return domain[4:] if domain.startswith("www.") else domain

    fresh_all = sorted(new_domains - old_domains)
    fresh = [d for d in fresh_all if d not in DOMAIN_ALLOWLIST and bare(d) not in DOMAIN_ALLOWLIST]
    out["new_domains_all"] = len(fresh_all)
    out["new_domains"] = len(fresh)  # after removing the allowlist
    # Ignore documentation placeholders such as YOUR_API_KEY or EXAMPLE_TOKEN.
    env_added = sorted(e for e in new_env - old_env if not PLACEHOLDER_ENV_RE.search(e))
    out["new_cred_env"] = len(env_added)
    out["new_cred_env_list"] = " ".join(env_added[:10])
    return out, fresh, diff_blocks




# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def analyse_update(u, get, data, save_diffs):
    """Compare one update (a row of UPDATES_FILE) and return its feature row."""
    owner, slug = u["owner"], u["slug"]
    row = {k: u.get(k) for k in ("owner", "slug", "stratum", "from_version", "to_version",
                                 "from_created_at", "to_created_at", "change_type", "to_changelog")}
    if u["change_type"] in ("identical", "unknown"):
        row["skill_md_change"] = "none" if u["change_type"] == "identical" else "unknown"
        return row
    old, new = get(owner, slug, u["from_version"]), get(owner, slug, u["to_version"])
    if old is None or new is None:
        row["skill_md_change"] = "unknown"
        return row

    feats, fresh_domains, blocks = compare(old, new)
    row["skill_md_change"] = classify_skill_md(old, new)
    for group in ("instructions", "docs", "code"):
        for kind in ("lines_added", "lines_removed", "files_added", "files_removed"):
            row[f"{group}_{kind}"] = feats.get(f"{group}_{kind}", 0)
    for f in FEATURES:
        row[f] = feats.get(f, 0)
    row["new_domains"] = feats.get("new_domains", 0)
    row["new_domains_all"] = feats.get("new_domains_all", 0)
    row["new_domain_list"] = " ".join(fresh_domains[:10])
    row["new_cred_env"] = feats.get("new_cred_env", 0)
    row["new_cred_env_list"] = feats.get("new_cred_env_list", "")

    # Combinations that matter more than any single feature.
    # Exfiltration shape: the update adds access to secrets AND a way to send data out.
    row["combo_secret_and_egress"] = int(
        (row["secret_access_lines"] > 0 or row["new_cred_env"] > 0)
        and (row["exec_net_lines"] > 0 or row["new_domains"] > 0))
    row["any_strong_feature"] = int(any(row[f] for f in STRONG) or row["combo_secret_and_egress"])
    row["any_feature"] = int(any(row[f] for f in FEATURES if f != "disclosure_lines")
                             or row["new_domains"] > 0 or row["new_cred_env"] > 0)

    size = sum(row[f"{g}_lines_added"] + row[f"{g}_lines_removed"]
               for g in ("instructions", "docs", "code"))
    row["lines_changed"] = size
    minor = bool(MINOR_CHANGELOG_RE.search(u.get("to_changelog") or ""))
    row["changelog_says_minor"] = int(minor)
    # "Minor" changelog on an update that is big or adds strong security features.
    row["changelog_mismatch"] = int(minor and (size > 50 or row["any_strong_feature"]))
    for f in FEATURES:
        row[f"example_{f}"] = feats.get(f"example_{f}", "")

    if save_diffs and blocks:
        path = diff_path(data, owner, slug, u["from_version"], u["to_version"])
        path.parent.mkdir(parents=True, exist_ok=True)
        header = (f"{owner}/{slug}  {u['from_version']} -> {u['to_version']}\n"
                  f"change_type: {u['change_type']}   changelog: {u.get('to_changelog', '')}\n\n")
        path.write_text((header + "\n\n".join(blocks))[:MAX_DIFF_FILE_BYTES], encoding="utf-8")
    return row


def diff_path(data, owner, slug, a, b):
    """Where the readable .diff file of one update is stored."""
    return (data / DIFFS_DIR / safe_name(owner) / safe_name(slug)
            / f"{safe_name(a)}__{safe_name(b)}.diff")


def remove_stale_diffs(data, updates):
    """Delete .diff files that this run did not produce, e.g. left over from an
    earlier run with different rules, so diffs/ always matches the input."""
    current = {diff_path(data, u["owner"], u["slug"], u["from_version"], u["to_version"])
               for u in updates}
    stale = [p for p in (data / DIFFS_DIR).glob("*/*/*.diff") if p not in current]
    for p in stale:
        p.unlink()
    for folder in sorted((data / DIFFS_DIR).glob("*/*"), reverse=True) + \
            sorted((data / DIFFS_DIR).glob("*")):
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    if stale:
        print(f"Removed {len(stale)} stale .diff files from {data / DIFFS_DIR}")


def write_label_sheet(rows, data, size, seed):
    """[LABEL] Create label_sheet.csv: a stratified sample of updates for manual labelling.

    Half of the sheet is a plain random sample of content-changing updates (gives
    unbiased shares for the RQ1 taxonomy). The other half is spread over the
    detected features, so even rare features get enough examples to measure
    their precision. The column `sample_reason` records why each update was
    picked, so the two halves can be analysed (and weighted) separately."""
    import random
    rng = random.Random(seed)
    pool = [r for r in rows if r.get("change_type") not in ("identical", "unknown")
            and r.get("skill_md_change") != "unknown"]
    chosen, seen = [], set()

    def take(candidates, n, reason):
        candidates = [r for r in candidates if id(r) not in seen]
        for r in rng.sample(candidates, min(n, len(candidates))):
            seen.add(id(r))
            chosen.append({**r, "sample_reason": reason})

    take(pool, size // 2, "random")
    per_feature = max(1, (size - len(chosen)) // len(DETECTED_COLS))
    for f in DETECTED_COLS:
        take([r for r in pool if int(r.get(f) or 0) > 0], per_feature, f"feature:{f}")
    rng.shuffle(chosen)  # so you do not label all of one kind in a row

    columns = SHEET_COLUMNS + [
               # --- your labels (fill these in) ---
               "label_category", "label_security_relevant", "label_correct_features",
               "label_wrong_features", "label_notes"]
    with open(data / "label_sheet.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for i, r in enumerate(chosen, 1):
            w.writerow({**r, "label_id": i, **sheet_fields(r)})
    return len(chosen)


def sheet_fields(r):
    """The detected_features and diff_file columns of one sheet row."""
    detected = [f for f in DETECTED_COLS if int(r.get(f) or 0) > 0]
    return {"detected_features": " ".join(detected),
            "diff_file": str(diff_path(Path("."), r["owner"], r["slug"],
                                       r["from_version"], r["to_version"]))}


def write_screen_sheet(rows, data, seed):
    """[SCREEN] Create screen_sheet.csv: every update whose content changed, for llm_label.py
    --mode screen. label_id is "owner/slug/A__B" rather than a number, so it stays
    the same when this file is rebuilt and an interrupted screening run can resume.
    Updates without a .diff file (only whitespace or line order changed) are left out.

    The rows are shuffled with a fixed seed and numbered in screen_order. llm_label.py
    works down the sheet in this order, so whatever it has labelled when the budget
    runs out (the first N rows) is a simple random sample of all changed updates, and
    a later run just extends that sample. Same seed = same order on every rebuild.
    Returns (rows written, rows left out)."""
    import random
    chosen, skipped = [], 0
    for r in rows:
        if r.get("change_type") in ("identical", "unknown") or r.get("skill_md_change") == "unknown":
            continue
        fields = sheet_fields(r)
        if not (data / fields["diff_file"]).is_file():
            skipped += 1
            continue
        label_id = f"{r['owner']}/{r['slug']}/{r['from_version']}__{r['to_version']}"
        chosen.append({**r, **fields, "label_id": label_id, "sample_reason": "all"})
    random.Random(seed).shuffle(chosen)
    with open(data / "screen_sheet.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["screen_order"] + SHEET_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for i, r in enumerate(chosen, 1):
            w.writerow({**r, "screen_order": i})
    return len(chosen), skipped


def main():
    """Go through UPDATES_FILE, compare every pair of versions and write the outputs."""
    p = argparse.ArgumentParser(description="Offline diff analysis of skill updates")
    p.add_argument("--data", default="data/full", help="folder written by clawhub_pilot.py")
    p.add_argument("--no-diffs", action="store_true", help="do not write the readable .diff files")
    p.add_argument("--label-sheet", type=int, default=0,
                   help="also create label_sheet.csv with this many updates to label (e.g. 400)")
    p.add_argument("--screen-sheet", action="store_true",
                   help="also create screen_sheet.csv with every changed update (for LLM screening)")
    p.add_argument("--seed", type=int, default=7,
                   help="random seed for the label sheet and the screen sheet order (default: 7)")
    args = p.parse_args()

    data = Path(args.data)
    updates_file = data / UPDATES_FILE
    if not updates_file.exists():
        sys.exit(f"{updates_file} not found - run clawhub_pilot.py first.")
    updates = list(csv.DictReader(open(updates_file, encoding="utf-8")))
    cache = {}

    def get(owner, slug, version):
        """Load a version once and keep it, since most versions appear in two updates."""
        key = (owner, slug, version)
        if key not in cache:
            if len(cache) > 400:  # keep memory small: updates of one skill are consecutive
                cache.clear()
            cache[key] = load_version(skill_folder(data, owner, slug), version)
        return cache[key]

    rows = []
    for i, u in enumerate(updates, 1):
        rows.append(analyse_update(u, get, data, not args.no_diffs))
        if i % 1000 == 0:
            print(f"{i} / {len(updates)} updates analysed")

    if not args.no_diffs:
        remove_stale_diffs(data, updates)

    # Write the per-update table (union of all columns, in a stable order).
    columns = []
    for r in rows:
        for k in r:
            if k not in columns:
                columns.append(k)
    (data / FEATURES_FILE).parent.mkdir(parents=True, exist_ok=True)
    with open(data / FEATURES_FILE, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns)
        w.writeheader()
        w.writerows(rows)

    # Summary: base rates over updates whose content really changed.
    changed = [r for r in rows if r.get("skill_md_change") != "unknown"
               and r["change_type"] not in ("identical", "unknown")]
    n = len(changed) or 1
    md = Counter(r["skill_md_change"] for r in changed)

    def rate(col):
        return round(sum(1 for r in changed if int(r.get(col) or 0) > 0) / n, 4)

    weak = ["directive_lines", "secret_lines", "agent_config_lines", "install_lines",
            "exec_net_lines", "new_domains_all", "new_domains", "new_cred_env", "any_feature"]
    strong = list(STRONG) + ["combo_secret_and_egress", "any_strong_feature"]
    summary = {
        "updates_total": len(rows),
        "updates_with_content_change": len(changed),
        "skill_md_change": dict(md),
        "skill_md_change_share": {k: round(v / n, 3) for k, v in md.items()},
        "base_rates_weak": {c: rate(c) for c in weak},
        "base_rates_strong": {c: rate(c) for c in strong},
        "security_disclosure_added": rate("disclosure_lines"),
        "changelog_says_minor_share": rate("changelog_says_minor"),
        "changelog_mismatch_share": rate("changelog_mismatch"),
        "top_new_domains": Counter(d for r in changed for d in (r.get("new_domain_list") or "").split()
                                   ).most_common(25),
        "top_new_cred_env": Counter(e for r in changed for e in (r.get("new_cred_env_list") or "").split()
                                    ).most_common(25),
    }
    (data / DIFF_SUMMARY_FILE).write_text(json.dumps(summary, indent=2, ensure_ascii=False),
                                             encoding="utf-8")

    print("\n=== Diff analysis ===")
    print(f"Updates analysed:            {summary['updates_total']}")
    print(f"Updates with content change: {summary['updates_with_content_change']}")
    print("How SKILL.md changed (share of content-changing updates):")
    for k, v in sorted(summary["skill_md_change_share"].items(), key=lambda x: -x[1]):
        print(f"  {k:12s} {v:6.1%}  ({md[k]})")
    print("Share of content-changing updates that ADD at least one ...")
    print("  WEAK features (common in normal skills):")
    for k, v in summary["base_rates_weak"].items():
        print(f"    {k:26s} {v:6.1%}")
    print("  STRONG features (rarely innocent):")
    for k, v in summary["base_rates_strong"].items():
        print(f"    {k:26s} {v:6.1%}")
    print(f"  security disclosure text added: {summary['security_disclosure_added']:.1%}")
    print(f"Changelog says 'minor':      {summary['changelog_says_minor_share']:.1%}")
    print(f"  ... but update is big or has a strong feature: {summary['changelog_mismatch_share']:.1%}")
    print("Most frequent newly added domains (allowlist removed):")
    for d, c in summary["top_new_domains"][:10]:
        print(f"  {c:5d}  {d}")
    print("Most frequent newly required credential variables:")
    for e, c in summary["top_new_cred_env"][:10]:
        print(f"  {c:5d}  {e}")

    if args.label_sheet:
        k = write_label_sheet(rows, data, args.label_sheet, args.seed)
        print(f"\nlabel_sheet.csv written with {k} updates to label (see the label_* columns).")
    if args.screen_sheet:
        k, skipped = write_screen_sheet(rows, data, args.seed)
        print(f"\nscreen_sheet.csv written with {k} updates to screen "
              f"({skipped} changed updates have no .diff file and were left out).")


if __name__ == "__main__":
    main()