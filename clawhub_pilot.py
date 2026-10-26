#!/usr/bin/env python3
"""
clawhub_pilot.py - Pilot crawler for the MSc thesis
"What Changes When Agent Tools Update?"

What it does (5 stages):
  1. LIST     Page through ClawHub's public skill catalog (GET /api/v1/skills,
              sorted by creation date) and save every skill record.
  2. CENSUS   (optional) For EVERY skill, fetch only its version list (one cheap
              request per skill) to learn how many versions each skill has.
  3. SAMPLE   Draw a two-part sample with a fixed seed (same sample every run):
                - "random": N skills at random from the whole catalog
                  -> describes the ecosystem honestly
                - "rich":   M skills at random among those with >= --min-versions
                  versions (needs the census) -> where most update records come from
              A skill can fall in both parts; it is then labelled "both".
  4. HISTORY  For every sampled skill: fetch each version's metadata, its
              security-scan verdict, and download its ZIP.
  5. ANALYSE  Hash the contents of every version, pair consecutive versions into
              "update records", and write summary tables (overall and per stratum).

Outputs (inside --out):
  raw/skill_pages/page_XXXXX.json   raw catalog pages (as returned by the API)
  skills/<owner>/<slug>/...         per-skill cache: versions, metadata, scans, ZIPs
  crawl.log                         full log of the run
  tables/
    skills_all.jsonl                one line per skill in the catalog
    skills_catalog_overview.csv     one row per skill: number of versions, dates
    version_spread_catalog.json     distribution of version counts over the catalog
    sample.json                     the sampled skills (reused on re-runs)
    sampled_skill_versions.csv      one row per (sampled skill, version)
    consecutive_sampled_versions.csv
                                    one row per consecutive version pair (A -> B)
    skills_catalog_metadata_overview.csv
                                    one row per sampled skill
    summary.json                    overall and per-stratum numbers

Usage:
  pip install requests
  export CLAWHUB_TOKEN=clh_...            # optional but gives higher rate limits

  # 1) list the full catalog and run the census, then stop and look at the numbers
  python clawhub_pilot.py --out data/full --census-only --rps 15 --workers 8 --contact you@university.gr

  # 2) two-part sample: 400 random skills + 1000 skills that were updated at least once
  python clawhub_pilot.py --out data/full --sample 400 --sample-rich 1000 --rps 15 --workers 8 --contact you@university.gr

  # quick smoke test: only 2 catalog pages, 20 skills, no census
  python clawhub_pilot.py --out data/smoke --max-pages 2 --sample 20 --contact you@university.gr

The script is resumable: anything already saved on disk is not requested again,
so you can stop it (Ctrl+C) and start it again with the same arguments.

Safety: downloaded ZIPs are stored as-is and only read in memory to compute
hashes. Nothing is extracted to disk and nothing is executed.

API reference: https://docs.openclaw.ai/clawhub/http-api
"""

import argparse
import csv
import hashlib
import io
import json
import logging
import os
import random
import re
import statistics
import sys
import threading
import time
import zipfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:  # give a clear message instead of a stack trace
    sys.exit("This script needs the 'requests' package: pip install requests")

log = logging.getLogger("clawhub_pilot")

# Every file in a skill version is put in one of three groups, each hashed
# separately, so we can tell WHAT part of a skill an update touched:
#   instructions - SKILL.md, the main text the agent reads
#   docs         - other text/Markdown files (references, examples, README...),
#                  which agents often read on demand, so they are instructions too
#   code         - everything else: scripts, configs, data files
INSTRUCTION_FILES = {"skill.md"}
DOC_EXTENSIONS = {".md", ".markdown", ".txt", ".rst", ".mdx"}

# Files added by the registry, not by the author, left out of all content
# hashes and diffs. Only matched at the top level of the skill.
#   _meta.json     - ownerId, slug, version, publishedAt (in every ZIP; differs per version)
#   skill-card.md  - ClawHub's generated Skill Card (risk/mitigation text written by
#                    the registry's LLM; in ~half of the ZIPs, regenerated per version)
REGISTRY_FILES = {"_meta.json", "skill-card.md"}

# Output tables (inside --out), shared with diff_analysis.py. They all live in
# tables/ and can be rebuilt offline from raw/ and skills/ (the cache).
TABLES = "tables"
CATALOG_FILE = f"{TABLES}/skills_all.jsonl"                            # one line per catalog skill
CENSUS_FILE = f"{TABLES}/skills_catalog_overview.csv"                  # one row per catalog skill
CENSUS_SUMMARY_FILE = f"{TABLES}/version_spread_catalog.json"          # version-count distribution
SAMPLE_FILE = f"{TABLES}/sample.json"                                  # the sampled skills + settings
VERSIONS_FILE = f"{TABLES}/sampled_skill_versions.csv"                 # one row per (sampled skill, version)
UPDATES_FILE = f"{TABLES}/consecutive_sampled_versions.csv"            # consecutive pairs A -> B
SKILLS_SUMMARY_FILE = f"{TABLES}/skills_catalog_metadata_overview.csv" # one row per sampled skill
SUMMARY_FILE = f"{TABLES}/summary.json"                                # overall and per-stratum numbers


def file_group(path):
    """Return "registry", "instructions", "docs" or "code" for a path inside a skill."""
    name = path.rsplit("/", 1)[-1].lower()
    if path.lower() in REGISTRY_FILES:
        return "registry"
    if name in INSTRUCTION_FILES:
        return "instructions"
    if os.path.splitext(name)[1] in DOC_EXTENSIONS:
        return "docs"
    return "code"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def now_iso():
    """Current UTC time as an ISO-8601 string; stored next to every saved
    response so the dataset records *when* each item was collected."""
    return datetime.now(timezone.utc).isoformat()


def write_json(path, data):
    """Write data as pretty JSON. We write to a temporary file first and then
    rename it, so a crash never leaves a half-written (corrupt) file behind."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def read_json(path):
    """Read a JSON file, or return None if it does not exist yet.
    Used everywhere for caching: if the file exists, we skip the request."""
    path = Path(path)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


MAX_NAME_LEN = 100  # well below the 255-character limit of macOS/Linux file systems


def safe_name(text):
    """Turn an owner handle / slug / version into a safe folder or file name
    (letters, digits, dot, dash, underscore only).

    Some catalog entries have absurdly long names (one slug is thousands of
    characters), which the operating system refuses as a folder name. Names
    longer than MAX_NAME_LEN are cut and given a short hash of the full name,
    so they stay unique. Normal names are left unchanged, so folders already
    cached by earlier runs are still found."""
    name = re.sub(r"[^A-Za-z0-9._-]", "_", str(text)) or "_"
    if len(name) > MAX_NAME_LEN:
        digest = hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:12]
        name = f"{name[:MAX_NAME_LEN - 13]}-{digest}"
    return name


def extract_items(payload):
    """Return the list of records inside an API response.
    The docs show list responses under "items"; we also accept a few other
    common key names so a small API change does not break the crawl."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("items", "versions", "results", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def semver_key(version):
    """Sort key for version strings like "1.10.2" (so 1.10 > 1.9).
    Only used as a fallback when a version has no creation timestamp."""
    parts = re.findall(r"\d+", str(version))
    return tuple(int(p) for p in parts) if parts else (0,)


# ---------------------------------------------------------------------------
# HTTP client with rate limiting and retries
# ---------------------------------------------------------------------------

class ClawHubClient:
    """A small HTTP client for the ClawHub API that is polite by design:
      - spaces requests so ALL threads together never exceed --rps per second,
      - on HTTP 429 every thread pauses as long as the server asks (Retry-After),
      - retries temporary failures (5xx, network errors) with backoff,
      - sends a User-Agent with your contact e-mail so ClawHub can reach you.

    It is safe to use from several threads at once (see --workers): each
    thread gets its own connection, while the rate limit, the 429 pause
    and the counters are shared between all threads."""

    def __init__(self, base_url, token, contact, rps, max_retries=6, timeout=60):
        """Store the settings. The token (if given) is sent as a Bearer token,
        which moves us from per-IP limits to the higher per-account limits."""
        self.base_url = base_url.rstrip("/")
        self.min_interval = 1.0 / rps if rps > 0 else 0.0
        self.max_retries = max_retries
        self.timeout = timeout
        self.headers = {"User-Agent": f"msc-thesis-clawhub-pilot/0.2 (+mailto:{contact})"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        self._local = threading.local()      # one requests.Session per thread
        self._lock = threading.Lock()        # protects the shared state below
        self._next_slot = 0.0                # earliest time the next request may start
        self._paused_until = 0.0             # set when the server answers 429
        # Counters reported at the end of the run.
        self.stats = Counter()

    def _session(self):
        """Return this thread's own HTTP session (created on first use).
        Sessions keep connections open, which makes repeated requests faster."""
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(self.headers)
            self._local.session = session
        return session

    def _count(self, key):
        """Increase a shared counter safely from any thread."""
        with self._lock:
            self.stats[key] += 1

    def _throttle(self):
        """Wait for this request's turn. Each request reserves the next free
        time slot, min_interval seconds after the previous one, so the total
        rate over all threads stays at or below --rps. If the server recently
        answered 429, nobody starts before that pause is over."""
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next_slot, self._paused_until)
            self._next_slot = start + self.min_interval
        wait = start - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def _pause_all(self, seconds):
        """After a 429, make every thread wait (not just the one that got it)."""
        with self._lock:
            self._paused_until = max(self._paused_until, time.monotonic() + seconds)

    @staticmethod
    def _retry_delay(resp, attempt):
        """Decide how many seconds to wait before retrying.
        Preference order (as ClawHub's docs recommend):
          1. Retry-After header (seconds),
          2. RateLimit-Reset header (seconds until reset),
          3. X-RateLimit-Reset header (absolute Unix time),
          4. otherwise exponential backoff: 1, 2, 4, 8 ... seconds (max 5 min).
        A little random "jitter" is added so retries are not synchronised."""
        jitter = random.uniform(0, 1)
        if resp is not None:
            for header in ("Retry-After", "RateLimit-Reset"):
                value = resp.headers.get(header)
                if value:
                    try:
                        return max(1.0, float(value)) + jitter
                    except ValueError:
                        pass
            value = resp.headers.get("X-RateLimit-Reset")
            if value:
                try:
                    return max(1.0, float(value) - time.time()) + jitter
                except ValueError:
                    pass
        return min(300.0, 2.0 ** attempt) + jitter

    def get(self, path, params=None):
        """GET a path such as "/api/v1/skills" and return the final Response.
        Only 429 (rate limited), 5xx (server error) and network errors are
        retried; any other status (200, 403, 404, 410 ...) is returned to the
        caller, which decides what it means."""
        url = f"{self.base_url}{path}"
        resp = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            self._count("requests")
            try:
                resp = self._session().get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt == self.max_retries:
                    raise
                delay = self._retry_delay(None, attempt)
                self._count("retries")
                log.warning("Network error on %s (%s); retrying in %.1fs", path, exc, delay)
                time.sleep(delay)
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == self.max_retries:
                    return resp
                delay = self._retry_delay(resp, attempt)
                if resp.status_code == 429:
                    self._count("rate_limited")
                    self._pause_all(delay)   # slow everyone down, not just this thread
                self._count("retries")
                log.warning("HTTP %s on %s; waiting %.1fs before retry",
                            resp.status_code, path, delay)
                time.sleep(delay)
                continue
            return resp
        return resp


# ---------------------------------------------------------------------------
# Stage 1: list the whole catalog
# ---------------------------------------------------------------------------

def list_all_skills(client, out_dir, page_limit, max_pages=None):
    """Page through GET /api/v1/skills?sort=createdAt and save every page.

    sort=createdAt: the docs say it is stable for full crawls, while
    sort=updated reshuffles whenever a skill is republished.
    Why follow nextCursor until it is null: the docs warn that a short page
    does NOT mean the end of the results (it may have fewer items than the limit).

    Progress is saved after each page (list_state.json), so an interrupted
    crawl continues where it stopped. Returns the de-duplicated skill list."""
    pages_dir = out_dir / "raw" / "skill_pages"
    state_file = out_dir / "raw" / "list_state.json"
    state = read_json(state_file) or {"next_page": 0, "cursor": None, "done": False}

    while not state["done"]:
        if max_pages is not None and state["next_page"] >= max_pages:
            log.info("Stopping catalog listing early (--max-pages %d)", max_pages)
            break
        params = {"sort": "createdAt", "limit": page_limit}
        if state["cursor"]:
            params["cursor"] = state["cursor"]
        resp = client.get("/api/v1/skills", params=params)
        if resp.status_code != 200:
            raise RuntimeError(f"Catalog listing failed: HTTP {resp.status_code}: {resp.text[:300]}")
        payload = resp.json()
        write_json(pages_dir / f"page_{state['next_page']:05d}.json",
                   {"fetched_at": now_iso(), "params": params, "payload": payload})
        cursor = payload.get("nextCursor") if isinstance(payload, dict) else None
        log.info("Catalog page %d: %d skills", state["next_page"], len(extract_items(payload)))
        state = {"next_page": state["next_page"] + 1, "cursor": cursor, "done": cursor is None}
        write_json(state_file, state)

    # Re-read every saved page. Pages are live reads, so the same skill can
    # appear twice; we keep the first occurrence of each (owner, slug) pair.
    skills, seen = [], set()
    for page_file in sorted(pages_dir.glob("page_*.json")):
        for item in extract_items(read_json(page_file)["payload"]):
            key = (item.get("ownerHandle"), item.get("slug"))
            if key in seen or not item.get("slug"):
                continue
            seen.add(key)
            skills.append(item)

    (out_dir / CATALOG_FILE).parent.mkdir(parents=True, exist_ok=True)
    with open(out_dir / CATALOG_FILE, "w", encoding="utf-8") as fh:
        for item in skills:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
    log.info("Catalog: %d unique skills saved to %s", len(skills), CATALOG_FILE)
    return skills


# ---------------------------------------------------------------------------
# Eligibility 
# ---------------------------------------------------------------------------

def eligible_skills(skills):
    """Return (eligible_skills, number_of_ambiguous_slugs).

    Slugs are not globally unique on ClawHub (two publishers can use the same
    slug) but the version endpoints are addressed by slug. We skip slugs owned by more than one publisher and
    report how many were skipped."""
    owners_per_slug = defaultdict(set)
    for s in skills:
        owners_per_slug[s["slug"]].add(s.get("ownerHandle"))
    ambiguous = {slug for slug, owners in owners_per_slug.items() if len(owners) > 1}
    return [s for s in skills if s["slug"] not in ambiguous], len(ambiguous)


def skill_key(skill):
    """The (owner, slug) pair that identifies a skill everywhere in this script."""
    return (skill.get("ownerHandle") or "_unknown", skill["slug"])


def skill_folder(out_dir, owner, slug):
    """Folder where everything about one skill is cached."""
    return out_dir / "skills" / safe_name(owner) / safe_name(slug)


# ---------------------------------------------------------------------------
# Stage 2: census - how many versions does every skill have?
# ---------------------------------------------------------------------------

def run_census(client, skills, out_dir, min_versions, workers=1):
    """Fetch the version list (not the contents) of every eligible skill.

    This costs one request per skill (more only for skills with >100 versions)
    and tells us, for the whole catalog, how many versions each skill has.
    We need it to (a) describe the ecosystem and (b) draw the "rich" sample
    of skills with enough versions to study updates.

    With workers > 1 several skills are fetched at the same time. This helps
    because most of the time is spent waiting for the server to answer. The
    shared rate limit (--rps) still applies to all threads together.

    The version lists are cached in the same place the history stage uses
    (skills/<owner>/<slug>/versions.json), so this work is never repeated.
    Writes CENSUS_FILE (one row per skill) and CENSUS_SUMMARY_FILE, and
    returns a dict {(owner, slug): number_of_versions}."""
    eligible, n_ambiguous = eligible_skills(skills)

    def census_one(skill):
        """Fetch (or read from cache) one skill's version list and turn it
        into one census row. Runs inside a worker thread."""
        owner, slug = skill_key(skill)
        try:
            versions, status = list_versions(client, slug, owner, skill_folder(out_dir, owner, slug))
        except Exception as exc:  # one broken entry must not stop the whole census
            log.warning("Census: skipping %s/%s after error: %s", owner, slug[:80], exc)
            versions, status = [], "error"
        times = sorted(v.get("createdAt") for v in versions if v.get("createdAt") is not None)
        return {
            "owner": owner, "slug": slug, "status": status,
            "n_versions": len(versions),
            "first_version_at": times[0] if times else None,
            "last_version_at": times[-1] if times else None,
        }

    def is_cached(skill):
        """True if this skill's version list is already saved on disk."""
        try:
            return (skill_folder(out_dir, *skill_key(skill)) / "versions.json").exists()
        except OSError:
            return False

    already = sum(is_cached(s) for s in eligible)
    log.info("Census: %d skills, %d already cached, %d to fetch with %d worker(s)",
             len(eligible), already, len(eligible) - already, workers)

    rows, started = [], time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(census_one, s) for s in eligible]
        try:
            for i, future in enumerate(as_completed(futures), 1):
                rows.append(future.result())
                if i % 250 == 0:
                    rate = i / max(1e-9, time.monotonic() - started)
                    eta_min = (len(eligible) - i) / rate / 60 if rate else float("inf")
                    log.info("Census: %d / %d skills done (%.1f skills/s, ~%.0f min left)",
                             i, len(eligible), rate, eta_min)
        except KeyboardInterrupt:
            # Stop handing out new work; what is already saved stays cached.
            for f in futures:
                f.cancel()
            log.warning("Census interrupted. Everything fetched so far is saved; "
                        "run the same command again to continue.")
            raise SystemExit(1)

    rows.sort(key=lambda r: (r["owner"], r["slug"]))  # stable order for CENSUS_FILE
    counts = {(r["owner"], r["slug"]): r["n_versions"] for r in rows}

    write_csv(out_dir / CENSUS_FILE, rows)
    values = [r["n_versions"] for r in rows]
    census = {
        "generated_at": now_iso(),
        "catalog_size": len(skills),
        "eligible_skills": len(eligible),
        "ambiguous_slugs_skipped": n_ambiguous,
        "failed_version_lists": sum(r["status"] != 200 for r in rows),
        "versions_per_skill": describe(values),
        f"skills_with_>={min_versions}_versions": sum(n >= min_versions for n in values),
        "potential_update_records": sum(max(0, n - 1) for n in values),
    }
    write_json(out_dir / CENSUS_SUMMARY_FILE, census)
    log.info("Census done: %d skills, %d with >= %d versions",
             len(rows), census[f"skills_with_>={min_versions}_versions"], min_versions)
    return counts


# ---------------------------------------------------------------------------
# Stage 3: draw a reproducible two-part sample
# ---------------------------------------------------------------------------

def draw_sample(skills, out_dir, n_random, n_rich, min_versions, seed, census_counts):
    """Draw the two-part sample with a fixed seed, so the same command always
    produces the same sample (important for reproducibility):
      - "random": n_random skills from all eligible skills
      - "rich":   n_rich skills from eligible skills with >= min_versions
                  versions (uses the census counts)
    Each returned skill carries a "_stratum" label: random, rich or both.

    If SAMPLE_FILE already exists it is reused, but only if it was drawn with
    the same settings; otherwise we stop with a clear message rather than
    silently mixing two different samples."""
    settings = {"n_random": n_random, "n_rich": n_rich,
                "min_versions": min_versions, "seed": seed}
    sample_file = out_dir / SAMPLE_FILE
    existing = read_json(sample_file)
    if existing:
        if existing.get("settings") != settings:
            sys.exit(f"{sample_file} was drawn with different settings "
                     f"({existing.get('settings')}). Use a new --out folder, or delete "
                     f"it if you really want a new sample.")
        log.info("Reusing existing %s (%d skills)", SAMPLE_FILE, len(existing["skills"]))
        return existing["skills"]

    eligible, n_ambiguous = eligible_skills(skills)
    # Two independent random generators so that changing one part's size
    # does not change which skills the other part picks.
    rng_random = random.Random(f"{seed}-random")
    rng_rich = random.Random(f"{seed}-rich")

    random_part = rng_random.sample(eligible, min(n_random, len(eligible)))
    rich_pool = [s for s in eligible if census_counts.get(skill_key(s), 0) >= min_versions]
    rich_part = rng_rich.sample(rich_pool, min(n_rich, len(rich_pool))) if n_rich else []

    # Merge the two parts, labelling skills that were picked by both.
    chosen = {}
    for s in random_part:
        chosen[skill_key(s)] = {**s, "_stratum": "random"}
    for s in rich_part:
        key = skill_key(s)
        chosen[key] = {**s, "_stratum": "both" if key in chosen else "rich"}

    write_json(sample_file, {
        "created_at": now_iso(),
        "settings": settings,
        "catalog_size": len(skills),
        "eligible_skills": len(eligible),
        "ambiguous_slugs_skipped": n_ambiguous,
        "rich_pool_size": len(rich_pool) if n_rich else None,
        "skills": list(chosen.values()),
    })
    log.info("Sample: %d random + %d rich (from a pool of %d) = %d unique skills",
             len(random_part), len(rich_part), len(rich_pool), len(chosen))
    if n_rich and len(rich_pool) < n_rich:
        log.warning("Only %d skills have >= %d versions; the rich part is smaller than requested.",
                    len(rich_pool), min_versions)
    return list(chosen.values())


# ---------------------------------------------------------------------------
# Stage 3: collect the version history of one skill
# ---------------------------------------------------------------------------

def list_versions(client, slug, owner, skill_dir):
    """List every published version of a skill:
    GET /api/v1/skills/{slug}/versions, following nextCursor.
    ownerHandle is passed as a hint (the API ignores unknown parameters).
    The result is cached in versions.json."""
    cache = skill_dir / "versions.json"
    cached = read_json(cache)
    if cached is not None:
        return cached["versions"], cached["status"]

    versions, cursor, status = [], None, 200
    while True:
        params = {"limit": 100, "ownerHandle": owner}
        if cursor:
            params["cursor"] = cursor
        resp = client.get(f"/api/v1/skills/{slug}/versions", params=params)
        status = resp.status_code
        if status != 200:
            log.warning("%s/%s: version list returned HTTP %s", owner, slug, status)
            break
        payload = resp.json()
        versions.extend(extract_items(payload))
        cursor = payload.get("nextCursor") if isinstance(payload, dict) else None
        if not cursor:
            break

    write_json(cache, {"fetched_at": now_iso(), "status": status, "versions": versions})
    return versions, status


def fetch_version_detail(client, slug, owner, version, skill_dir):
    """Fetch one version's metadata, file list and security block:
    GET /api/v1/skills/{slug}/versions/{version}. Cached per version."""
    cache = skill_dir / "meta" / f"{safe_name(version)}.json"
    cached = read_json(cache)
    if cached is not None:
        return cached
    resp = client.get(f"/api/v1/skills/{slug}/versions/{version}",
                      params={"ownerHandle": owner})
    record = {"fetched_at": now_iso(), "status": resp.status_code,
              "payload": resp.json() if _is_json(resp) else resp.text[:2000]}
    write_json(cache, record)
    return record


def fetch_scan(client, slug, owner, version, skill_dir):
    """Fetch ClawHub's security-scan verdict for one version:
    GET /api/v1/skills/{slug}/scan?version=...
    These verdicts (clean / suspicious / malicious) become free labels for
    the thesis. Cached per version."""
    cache = skill_dir / "scan" / f"{safe_name(version)}.json"
    cached = read_json(cache)
    if cached is not None:
        return cached
    resp = client.get(f"/api/v1/skills/{slug}/scan",
                      params={"version": version, "ownerHandle": owner})
    record = {"fetched_at": now_iso(), "status": resp.status_code,
              "payload": resp.json() if _is_json(resp) else resp.text[:2000]}
    write_json(cache, record)
    return record


def download_version(client, slug, owner, version, skill_dir):
    """Download one version: GET /api/v1/download?slug=...&version=...
    Possible outcomes, all recorded in download/<version>.json:
      - "zip":            hosted skill; the ZIP is saved to zips/<version>.zip
      - "github_handoff": GitHub-backed skill; the API returns JSON with repo,
                          commit and path instead of a ZIP (follow it with git later)
      - "gone":           HTTP 410, version was soft-deleted
      - "blocked":        HTTP 403, blocked (e.g. flagged malicious)
      - "error":          anything else
    Cached, so finished downloads are never repeated."""
    outcome_file = skill_dir / "download" / f"{safe_name(version)}.json"
    cached = read_json(outcome_file)
    if cached is not None:
        return cached

    resp = client.get("/api/v1/download",
                      params={"slug": slug, "version": version, "ownerHandle": owner})
    outcome = {"fetched_at": now_iso(), "status": resp.status_code}

    if resp.status_code == 200 and _is_json(resp):
        outcome["kind"] = "github_handoff"
        outcome["handoff"] = resp.json()
    elif resp.status_code == 200:
        zip_path = skill_dir / "zips" / f"{safe_name(version)}.zip"
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        zip_path.write_bytes(resp.content)
        outcome["kind"] = "zip"
        outcome["zip_file"] = str(zip_path.relative_to(skill_dir))
        outcome["zip_sha256"] = hashlib.sha256(resp.content).hexdigest()
    elif resp.status_code == 410:
        outcome["kind"] = "gone"
        outcome["message"] = resp.text[:500]
    elif resp.status_code == 403:
        outcome["kind"] = "blocked"
        outcome["message"] = resp.text[:500]
    else:
        outcome["kind"] = "error"
        outcome["message"] = resp.text[:500]

    write_json(outcome_file, outcome)
    return outcome


def _is_json(resp):
    """True when the server answered with JSON (checked via Content-Type)."""
    return "json" in resp.headers.get("Content-Type", "").lower()


# ---------------------------------------------------------------------------
# Content hashing
# ---------------------------------------------------------------------------

def hash_zip_contents(zip_bytes):
    """Fingerprint a skill version by its *contents*, ignoring ZIP details
    (timestamps, compression, file order) that change without the skill changing.

    Returns four hashes (see file_group() for the groups):
      content_hash      - all author files together
      instruction_hash  - only SKILL.md (the main text the agent reads)
      docs_hash         - other text/Markdown files
      code_hash         - scripts, configs and everything else
    plus file counts and the total size (of author files only).

    Registry-added files (_meta.json, skill-card.md) are left out of every hash,
    because they change between versions even when the author changed nothing.

    If every file sits inside one common top-level folder (e.g. "gifgrep-1.2.3/"),
    that folder name is removed first, because it usually contains the version
    number and would make every version look different.

    The ZIP is read in memory only: nothing is extracted, nothing is executed."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        files = [(info.filename, zf.read(info)) for info in zf.infolist() if not info.is_dir()]

    names = [name for name, _ in files]
    first_parts = {name.split("/", 1)[0] for name in names}
    if len(first_parts) == 1 and all("/" in name for name in names):
        prefix = next(iter(first_parts)) + "/"
        files = [(name[len(prefix):], data) for name, data in files]

    has_registry_file = any(file_group(name) == "registry" for name, _ in files)
    has_skill_card = any(name.lower() == "skill-card.md" for name, _ in files)
    files = [(name, data) for name, data in files if file_group(name) != "registry"]
    per_file = sorted((name, hashlib.sha256(data).hexdigest()) for name, data in files)

    def combine(entries):
        """One fingerprint for a list of (path, file-hash) pairs; None if empty."""
        if not entries:
            return None
        h = hashlib.sha256()
        for name, digest in entries:
            h.update(f"{name}\0{digest}\n".encode("utf-8"))
        return h.hexdigest()

    groups = {g: [e for e in per_file if file_group(e[0]) == g]
              for g in ("instructions", "docs", "code")}
    return {
        # An empty skill still gets a content hash so it can be compared.
        "content_hash": combine(per_file) or hashlib.sha256(b"").hexdigest(),
        "instruction_hash": combine(groups["instructions"]),
        "docs_hash": combine(groups["docs"]),
        "code_hash": combine(groups["code"]),
        "n_files": len(per_file),
        "n_doc_files": len(groups["docs"]),
        "n_code_files": len(groups["code"]),
        "total_bytes": sum(len(data) for _, data in files),
        "has_skill_md": bool(groups["instructions"]),
        "has_registry_file": has_registry_file,
        "has_skill_card": has_skill_card,
    }


def scan_verdict(scan_record):
    """Pull a single verdict word (e.g. clean / suspicious / malicious) out of
    a saved scan response. The exact JSON layout is not fully documented, so
    we look in the likely places and fall back to None."""
    payload = scan_record.get("payload") if scan_record else None
    if not isinstance(payload, dict):
        return None
    for container in (payload.get("security"), payload.get("version", {}).get("security")
                      if isinstance(payload.get("version"), dict) else None, payload):
        if isinstance(container, dict):
            for key in ("status", "verdict"):
                if isinstance(container.get(key), str):
                    return container[key]
    return None


# The five dimensions ClawHub's LLM scanner rates for every version
# (seen in GET /api/v1/skills/{slug}/scan responses). Each gets a rating such
# as "note" or "concern"; we keep the raw words and do not guess an order.
SCAN_DIMENSIONS = ("purpose_capability", "instruction_scope", "install_mechanism",
                   "environment_proportionality", "persistence_privilege")


def scan_details(scan_record):
    """Extract the useful parts of a saved scan response, as flat columns:
      scan_http_status       - HTTP status of the scan request (explains missing verdicts)
      scan_has_result        - did a scanner produce a definitive verdict?
      scan_checked_at        - when ClawHub scanned this version (Unix ms)
      scan_llm_model         - which model judged it (verdicts can change with the model)
      scan_llm_verdict       - the LLM scanner's own verdict
      scan_llm_confidence    - low / medium / high
      scan_vt_status         - VirusTotal result, if any
      scan_skillspector_status - SkillSpector result, if any
      dim_<name>             - the LLM's rating of each dimension (e.g. note / concern)
      mod_malware_blocked    - skill-level moderation flag (note: it describes the
                               skill's CURRENT state, not necessarily this version)
    The "security" block is per version; the "moderation" block is per skill."""
    out = {"scan_http_status": scan_record.get("status") if scan_record else None}
    payload = scan_record.get("payload") if scan_record else None
    security = payload.get("security") if isinstance(payload, dict) else None
    moderation = payload.get("moderation") if isinstance(payload, dict) else None
    security = security if isinstance(security, dict) else {}
    scanners = security.get("scanners") if isinstance(security.get("scanners"), dict) else {}
    llm = scanners.get("llm") if isinstance(scanners.get("llm"), dict) else {}

    def status_of(scanner):
        """Short status of one scanner block, whatever its exact shape."""
        if isinstance(scanner, dict):
            return scanner.get("normalizedStatus") or scanner.get("status") or scanner.get("verdict")
        return None

    out.update({
        "scan_has_result": security.get("hasScanResult"),
        "scan_checked_at": security.get("checkedAt"),
        "scan_llm_model": llm.get("model") or security.get("model"),
        "scan_llm_verdict": status_of(llm),
        "scan_llm_confidence": llm.get("confidence"),
        "scan_vt_status": status_of(scanners.get("vt")),
        "scan_skillspector_status": status_of(scanners.get("skillspector")),
        "mod_malware_blocked": moderation.get("isMalwareBlocked") if isinstance(moderation, dict) else None,
    })
    ratings = {d.get("name"): d.get("rating") for d in (llm.get("dimensions") or [])
               if isinstance(d, dict)}
    for dim in SCAN_DIMENSIONS:
        out[f"dim_{dim}"] = ratings.get(dim)
    return out


# ---------------------------------------------------------------------------
# Stage 3 driver: process one skill end to end
# ---------------------------------------------------------------------------

def process_skill(client, skill, out_dir, with_scan):
    """Collect everything for one sampled skill and return one row per version.
    Steps: list versions -> for each version fetch metadata, scan verdict and
    ZIP -> hash the ZIP contents. Every step is cached on disk."""
    owner, slug = skill_key(skill)
    skill_dir = skill_folder(out_dir, owner, slug)
    versions, list_status = list_versions(client, slug, owner, skill_dir)

    rows = []
    for v in versions:
        version = v.get("version")
        if not version:
            continue
        detail = fetch_version_detail(client, slug, owner, version, skill_dir)
        scan = fetch_scan(client, slug, owner, version, skill_dir) if with_scan else None
        dl = download_version(client, slug, owner, version, skill_dir)

        hashes = {}
        if dl.get("kind") == "zip":
            try:
                hashes = hash_zip_contents((skill_dir / dl["zip_file"]).read_bytes())
            except zipfile.BadZipFile:
                log.warning("%s/%s %s: downloaded file is not a valid ZIP", owner, slug, version)
                dl["kind"] = "bad_zip"

        rows.append({
            "owner": owner,
            "slug": slug,
            "stratum": skill.get("_stratum", "random"),
            "version": version,
            "created_at": v.get("createdAt"),
            "changelog": (v.get("changelog") or "").strip().replace("\n", " ")[:500],
            "meta_status": detail.get("status"),
            "download_kind": dl.get("kind"),
            "download_status": dl.get("status"),
            "scan_verdict": scan_verdict(scan) if scan else None,
            **scan_details(scan),
            **{k: hashes.get(k) for k in ("content_hash", "instruction_hash", "docs_hash",
                                          "code_hash", "n_files", "n_doc_files", "n_code_files",
                                          "total_bytes", "has_skill_md", "has_registry_file",
                                          "has_skill_card")},
        })

    # Oldest first: by creation time when available, otherwise by version number.
    rows.sort(key=lambda r: (r["created_at"] is None, r["created_at"] or 0,
                             semver_key(r["version"])))
    return rows, list_status


# ---------------------------------------------------------------------------
# Stage 4: build update records and summaries
# ---------------------------------------------------------------------------

def build_updates(version_rows):
    """Pair each version with the one before it (A -> B) for the same skill.
    Each pair is one "update record". Three yes/no columns say which group of
    files changed (instructions_changed, docs_changed, code_changed), and
    change_type summarises them:
      identical                  - no author file changed (e.g. a metadata-only
                                   republish; the registry's _meta.json is ignored)
      instructions               - only SKILL.md changed
      docs / code                - only that group changed
      instructions+code, ...     - several groups changed, joined with "+"
      unknown                    - at least one side could not be downloaded/hashed"""
    by_skill = defaultdict(list)
    for row in version_rows:
        by_skill[(row["owner"], row["slug"])].append(row)

    updates = []
    for (owner, slug), rows in by_skill.items():
        for a, b in zip(rows, rows[1:]):
            changed = {}
            if not (a["content_hash"] and b["content_hash"]):
                change = "unknown"
            elif a["content_hash"] == b["content_hash"]:
                change = "identical"
                changed = {"instructions": False, "docs": False, "code": False}
            else:
                changed = {
                    "instructions": a["instruction_hash"] != b["instruction_hash"],
                    "docs": a["docs_hash"] != b["docs_hash"],
                    "code": a["code_hash"] != b["code_hash"],
                }
                change = "+".join(g for g, c in changed.items() if c) or "identical"
            updates.append({
                "owner": owner, "slug": slug, "stratum": b["stratum"],
                "from_version": a["version"], "to_version": b["version"],
                "from_created_at": a["created_at"], "to_created_at": b["created_at"],
                "change_type": change,
                "instructions_changed": changed.get("instructions"),
                "docs_changed": changed.get("docs"),
                "code_changed": changed.get("code"),
                "files_delta": (b["n_files"] - a["n_files"])
                               if a["n_files"] is not None and b["n_files"] is not None else None,
                "bytes_delta": (b["total_bytes"] - a["total_bytes"])
                               if a["total_bytes"] is not None and b["total_bytes"] is not None else None,
                "from_scan": a["scan_verdict"], "to_scan": b["scan_verdict"],
                "scan_changed": (a["scan_verdict"] != b["scan_verdict"])
                                if a["scan_verdict"] and b["scan_verdict"] else None,
                # Which of the LLM scanner's dimension ratings changed, e.g.
                # "instruction_scope:note->concern". Empty = no rating changed.
                "scan_dim_changes": ";".join(
                    f"{dim}:{a.get('dim_' + dim)}->{b.get('dim_' + dim)}"
                    for dim in SCAN_DIMENSIONS
                    if a.get("dim_" + dim) and b.get("dim_" + dim)
                    and a.get("dim_" + dim) != b.get("dim_" + dim)),
                "same_scan_model": (a.get("scan_llm_model") == b.get("scan_llm_model"))
                                   if a.get("scan_llm_model") and b.get("scan_llm_model") else None,
                "to_changelog": b["changelog"],
            })
    return updates


def write_csv(path, rows, fieldnames=None):
    """Write a list of dicts to CSV (UTF-8). Column order follows the first
    row unless fieldnames is given."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    rows = list(rows)
    fieldnames = fieldnames or (list(rows[0].keys()) if rows else [])
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)




def histogram(values):
    """Bucket version counts into 0, 1, 2, 3-4, 5-9, 10-19, 20+ so the
    distribution can be read at a glance."""
    buckets = Counter()
    for n in values:
        label = ("0" if n == 0 else "1" if n == 1 else "2" if n == 2 else
                 "3-4" if n <= 4 else "5-9" if n <= 9 else "10-19" if n <= 19 else "20+")
        buckets[label] += 1
    return {k: buckets.get(k, 0) for k in ("0", "1", "2", "3-4", "5-9", "10-19", "20+")}


def describe(values):
    """Min / median / mean / max plus the histogram of a list of counts."""
    if not values:
        return {"min": 0, "median": 0, "mean": 0, "max": 0, "histogram": histogram([])}
    return {
        "min": min(values),
        "median": statistics.median(values),
        "mean": round(statistics.mean(values), 2),
        "max": max(values),
        "histogram": histogram(values),
    }


def group_stats(skill_rows, version_rows, updates, min_versions):
    """Headline numbers for one group of skills (the whole sample, or one
    stratum): versions per skill, update records and their change types,
    download outcomes and scan verdicts."""
    counts = [r["n_versions"] for r in skill_rows]
    unique_counts = [r["n_unique_contents"] for r in skill_rows]
    change_types = Counter(u["change_type"] for u in updates)
    known = len(updates) - change_types.get("unknown", 0)
    return {
        "skills": len(skill_rows),
        "versions_total": len(version_rows),
        "versions_per_skill": describe(counts),
        "unique_contents_per_skill_histogram": histogram(unique_counts),
        f"skills_with_>={min_versions}_versions": sum(n >= min_versions for n in counts),
        f"skills_with_>={min_versions}_unique_contents": sum(n >= min_versions for n in unique_counts),
        "update_records_total": len(updates),
        "update_change_types": dict(change_types),
        # Share of identical updates among those we could compare (unknown excluded).
        "share_identical_updates": round(change_types["identical"] / known, 3) if known else None,
        "download_outcomes": dict(Counter(r["download_kind"] for r in version_rows)),
        "scan_verdicts": dict(Counter(r["scan_verdict"] for r in version_rows)),
        # How ClawHub's LLM scanner rated each dimension, over all versions.
        "scan_dimension_ratings": {
            dim: dict(Counter(r.get("dim_" + dim) for r in version_rows))
            for dim in SCAN_DIMENSIONS},
        # Updates where the scanner's verdict or any dimension rating changed.
        "updates_scan_verdict_changed": sum(bool(u.get("scan_changed")) for u in updates),
        "updates_dimension_rating_changed": sum(bool(u.get("scan_dim_changes")) for u in updates),
    }


def summarise(catalog_size, sample, version_rows, updates, list_statuses,
              client_stats, min_versions, out_dir):
    """Compute the pilot's headline numbers and save them to SUMMARY_FILE:
    once for the whole sample, once for the random part and once for the rich part.
    Also writes SKILLS_SUMMARY_FILE (one row per sampled skill)."""
    per_skill = defaultdict(list)
    for row in version_rows:
        per_skill[(row["owner"], row["slug"])].append(row)

    skill_rows = []
    for s in sample:
        key = skill_key(s)
        rows = per_skill.get(key, [])
        unique = {r["content_hash"] for r in rows if r["content_hash"]}
        skill_rows.append({
            "owner": key[0], "slug": key[1],
            "stratum": s.get("_stratum", "random"),
            "version_list_status": list_statuses.get(key),
            "n_versions": len(rows),
            "n_downloaded": sum(r["download_kind"] == "zip" for r in rows),
            "n_unique_contents": len(unique),
            "n_gone": sum(r["download_kind"] == "gone" for r in rows),
            "n_blocked": sum(r["download_kind"] == "blocked" for r in rows),
            "github_backed": any(r["download_kind"] == "github_handoff" for r in rows),
            "latest_scan": rows[-1]["scan_verdict"] if rows else None,
        })
    write_csv(out_dir / SKILLS_SUMMARY_FILE, skill_rows)

    summary = {
        "generated_at": now_iso(),
        "catalog_size": catalog_size,
        "overall": group_stats(skill_rows, version_rows, updates, min_versions),
        "by_stratum": {},
        "http_client": dict(client_stats),
    }
    # Each part is reported in full: a skill picked by both parts ("both")
    # counts in the random part AND in the rich part, so the random part
    # stays an unbiased random sample of the catalog.
    parts = {"random part": {"random", "both"}, "rich part": {"rich", "both"}}
    for part, labels in parts.items():
        s_rows = [r for r in skill_rows if r["stratum"] in labels]
        if s_rows:
            summary["by_stratum"][part] = group_stats(
                s_rows,
                [r for r in version_rows if r["stratum"] in labels],
                [u for u in updates if u["stratum"] in labels],
                min_versions)
    write_json(out_dir / SUMMARY_FILE, summary)
    return summary


def print_group(title, g, min_versions):
    """Print the numbers of one group (overall or one stratum)."""
    vps = g["versions_per_skill"]
    print(f"\n--- {title} ---")
    print(f"Skills:                  {g['skills']}")
    print(f"Versions collected:      {g['versions_total']}")
    print(f"Versions per skill:      min {vps['min']}, median {vps['median']}, "
          f"mean {vps['mean']}, max {vps['max']}")
    print(f"  histogram:             {vps['histogram']}")
    print(f"Skills with >= {min_versions} versions:        "
          f"{g[f'skills_with_>={min_versions}_versions']}")
    print(f"Skills with >= {min_versions} unique contents: "
          f"{g[f'skills_with_>={min_versions}_unique_contents']}")
    print(f"Update records:          {g['update_records_total']}")
    print(f"  change types:          {g['update_change_types']}")
    print(f"  identical share:       {g['share_identical_updates']}")
    print(f"Download outcomes:       {g['download_outcomes']}")
    print(f"Scan verdicts:           {g['scan_verdicts']}")
    print(f"Updates where scan verdict changed:   {g['updates_scan_verdict_changed']}")
    print(f"Updates where a dimension rating changed: {g['updates_dimension_rating_changed']}")
    print("Scanner dimension ratings (all versions):")
    for dim, counts in g["scan_dimension_ratings"].items():
        print(f"  {dim:28s} {counts}")


def print_report(summary, min_versions):
    """Print a short human-readable report of the run to the console."""
    print("\n=== ClawHub pilot summary ===")
    print(f"Catalog size:            {summary['catalog_size']}")
    print_group("Overall", summary["overall"], min_versions)
    for part, g in summary["by_stratum"].items():
        print_group(part.capitalize(), g, min_versions)
    print(f"\nHTTP:                    {summary['http_client']}")


def print_census(census_file, min_versions):
    """Print the census results (version counts over the whole catalog)."""
    c = read_json(census_file)
    vps = c["versions_per_skill"]
    print("\n=== ClawHub census ===")
    print(f"Catalog size:            {c['catalog_size']}")
    print(f"Eligible skills:         {c['eligible_skills']} "
          f"({c['ambiguous_slugs_skipped']} ambiguous slugs skipped)")
    print(f"Failed version lists:    {c['failed_version_lists']}")
    print(f"Versions per skill:      min {vps['min']}, median {vps['median']}, "
          f"mean {vps['mean']}, max {vps['max']}")
    print(f"  histogram:             {vps['histogram']}")
    print(f"Skills with >= {min_versions} versions: {c[f'skills_with_>={min_versions}_versions']}")
    print(f"Potential update records (all skills): {c['potential_update_records']}")


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    """Read the command-line options (run with --help to see them all)."""
    p = argparse.ArgumentParser(description="ClawHub skill-history pilot crawler (MSc thesis)")
    p.add_argument("--out", default="data/pilot", help="output folder (default: data/pilot)")
    p.add_argument("--sample", type=int, default=200,
                   help="size of the RANDOM part of the sample (default: 200)")
    p.add_argument("--sample-rich", type=int, default=0,
                   help="size of the RICH part: skills with >= --min-versions versions. "
                        "Needs the census, which is run automatically (default: 0 = off)")
    p.add_argument("--min-versions", type=int, default=2,
                   help="version threshold for the rich part and the summaries (default: 2, "
                        "i.e. skills with at least one update)")
    p.add_argument("--seed", type=int, default=42, help="random seed for the sample (default: 42)")
    p.add_argument("--census", action="store_true",
                   help="run the census even when --sample-rich is 0")
    p.add_argument("--census-only", action="store_true",
                   help="list the catalog, run the census, print it and stop")
    p.add_argument("--rps", type=float, default=3.0,
                   help="max requests per second we send, over all workers together "
                        "(default: 3, far below ClawHub's limits)")
    p.add_argument("--workers", type=int, default=8,
                   help="requests in flight at the same time (default: 8). More workers help "
                        "when the server is slow to answer; --rps still caps the total rate")
    p.add_argument("--page-limit", type=int, default=200, help="skills per catalog page, 1-200 (default: 200)")
    p.add_argument("--max-pages", type=int, default=None,
                   help="stop the catalog listing after N pages (for quick tests only)")
    p.add_argument("--no-scan", action="store_true", help="skip the per-version security scan requests")
    p.add_argument("--list-only", action="store_true", help="only run stage 1 (catalog listing)")
    p.add_argument("--contact", required=True,
                   help="your e-mail, sent in the User-Agent so ClawHub can contact you")
    p.add_argument("--base-url", default="https://clawhub.ai", help="API base URL")
    p.add_argument("--token", default=None,
                   help="API token (prefer the CLAWHUB_TOKEN environment variable instead)")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING"])
    return p.parse_args(argv)


def setup_logging(out_dir, level):
    """Log to the console and to <out>/crawl.log at the same time."""
    out_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    log.setLevel(level)
    for handler in (logging.StreamHandler(), logging.FileHandler(out_dir / "crawl.log", encoding="utf-8")):
        handler.setFormatter(fmt)
        log.addHandler(handler)


def main(argv=None):
    """Run the stages in order: list skills -> census -> sample -> history -> analyse."""
    args = parse_args(argv)
    out_dir = Path(args.out)
    setup_logging(out_dir, args.log_level)

    token = os.environ.get("CLAWHUB_TOKEN") or args.token
    if args.token:
        log.warning("Token given on the command line; prefer the CLAWHUB_TOKEN environment "
                    "variable so it does not end up in your shell history.")
    if not token:
        log.info("No token: using anonymous (per-IP) rate limits.")

    client = ClawHubClient(args.base_url, token, args.contact, args.rps)

    # Stage 1: catalog
    skills = list_all_skills(client, out_dir, args.page_limit, args.max_pages)
    if args.list_only:
        print(f"Catalog listing done: {len(skills)} skills.")
        return
    if args.max_pages is not None:
        log.warning("--max-pages is set: the catalog is incomplete and sorted newest-first, "
                    "so the sample is NOT representative. Use it for tests only.")

    # Stage 2: census (only when needed or asked for)
    census_counts = {}
    if args.census or args.census_only or args.sample_rich > 0:
        census_counts = run_census(client, skills, out_dir, args.min_versions, args.workers)
        print_census(out_dir / CENSUS_SUMMARY_FILE, args.min_versions)
        if args.census_only:
            print(f"\nHTTP: {dict(client.stats)}")
            return

    # Stage 3: sample
    sample = draw_sample(skills, out_dir, args.sample, args.sample_rich,
                         args.min_versions, args.seed, census_counts)

    # Stage 4: history - several skills are processed at the same time
    # (--workers). Each skill's versions are handled by one worker, and the
    # shared --rps limit applies to all of them together.
    version_rows, list_statuses = [], {}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(process_skill, client, s, out_dir, not args.no_scan): s
                   for s in sample}
        try:
            for i, future in enumerate(as_completed(futures), 1):
                skill = futures[future]
                owner, slug = skill_key(skill)
                try:
                    rows, status = future.result()
                except Exception as exc:  # one broken skill must not stop the run
                    log.warning("Skipping %s/%s after error: %s", owner, slug[:80], exc)
                    rows, status = [], "error"
                version_rows.extend(rows)
                list_statuses[(owner, slug)] = status
                log.info("[%d/%d] %s/%s (%s): %d versions", i, len(sample), owner, slug,
                         skill.get("_stratum"), len(rows))
        except KeyboardInterrupt:
            for f in futures:
                f.cancel()
            log.warning("Interrupted - writing summaries for what was collected so far. "
                        "Run the same command again to continue.")

    # Keep a stable row order (by skill, then oldest version first) no matter
    # which worker finished first, so the CSV files are reproducible.
    order = {skill_key(s): i for i, s in enumerate(sample)}
    version_rows.sort(key=lambda r: order.get((r["owner"], r["slug"]), 0))

    # Stage 5: analyse
    updates = build_updates(version_rows)
    write_csv(out_dir / VERSIONS_FILE, version_rows)
    write_csv(out_dir / UPDATES_FILE, updates)
    summary = summarise(len(skills), sample, version_rows, updates, list_statuses,
                        client.stats, args.min_versions, out_dir)
    print_report(summary, args.min_versions)


if __name__ == "__main__":
    main()