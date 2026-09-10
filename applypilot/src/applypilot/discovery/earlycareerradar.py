"""Discovery provider: earlycareerradar.com (internships + new-grad roles).

Why this exists
---------------
A Microsoft Data Science internship posted 2026-09-01 and closed 2026-09-09 was
never scraped. Two reasons: internships were being dropped at discovery (fixed
2026-09-10), and Microsoft is not a Workday employer, so it can only reach the
pipeline if it cross-posts to LinkedIn/Indeed. 35 of 42 target employers have
that same gap. This site aggregates early-career roles from employers' own
career sites, which is exactly the inventory the other providers miss.

Politeness / permission
-----------------------
robots.txt (checked 2026-09-10):
    User-Agent: *
    Allow: /
    Disallow: /api/
so this reads ONLY the server-rendered public pages and never the internal API.
The terms prohibit "automated access in a way that disrupts Radar", hence the
one-request-per-page, rate-limited, cached-per-run design below. If robots.txt
ever disallows these paths, this module must stop being called.

How the data is obtained
------------------------
The public pages are Next.js and stream their payload as
`self.__next_f.push([1,"...json..."])`. The job objects are already in that
HTML, so a single GET per page yields hundreds of postings with no JS execution
and no API call.

Fields worth knowing about:
  * `track` and `placeStates` are the SITE's own classification, so filtering
    uses them rather than guessing from the title -- of 3,747 summer postings,
    Data/ML & AI/Quant are 973 while SWE alone is 822.
  * `postedAt` is the TRUE posting date. Every other provider gives us only
    "when we found it", which is why a job posted nine days earlier looked
    fresh on discovery.
  * `deadlineAt` exists in the payload but is NULL on every listing row
    (verified 2026-09-10). Stored when present; do not rely on it.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from applypilot import config

log = logging.getLogger(__name__)

BASE = "https://earlycareerradar.com"
# Only paths robots.txt Allows. Never add anything under /api/.
PAGES = ("/summer-internships", "/career")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
REQUEST_GAP_SECONDS = 3.0     # deliberate: a handful of requests per run
TIMEOUT = 60


def _fetch(path: str) -> str:
    req = urllib.request.Request(BASE + path, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "ignore")


def _decode_rsc(html: str) -> str:
    """Concatenate and unescape the Next.js RSC stream chunks."""
    chunks = re.findall(r'self\.__next_f\.push\(\[1,\s*"(.*?)"\]\)', html, re.S)
    blob = "".join(chunks)
    try:
        return json.loads(f'"{blob}"')
    except Exception:
        return blob.encode("utf-8", "ignore").decode("unicode_escape", "ignore")


def _job_objects(blob: str) -> list[dict]:
    """Pull balanced JSON objects that look like postings out of the blob."""
    out: list[dict] = []
    i, n = 0, len(blob)
    while i < n:
        if blob[i] != "{":
            i += 1
            continue
        depth, j = 0, i
        while j < n and j - i < 6000:
            if blob[j] == "{":
                depth += 1
            elif blob[j] == "}":
                depth -= 1
                if depth == 0:
                    seg = blob[i:j + 1]
                    if '"title"' in seg and '"company"' in seg and '"applyUrl"' in seg:
                        try:
                            d = json.loads(seg)
                            if isinstance(d.get("title"), str):
                                out.append(d)
                        except Exception:
                            pass
                    break
            j += 1
        i = j + 1 if j > i else i + 1
    return out


def _as_text(v) -> str:
    """Fields arrive as str, dict or list depending on the row."""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        for k in ("name", "label", "title", "value"):
            if isinstance(v.get(k), str):
                return v[k]
    if isinstance(v, list) and v:
        return _as_text(v[0])
    return ""


# The site classifies every posting itself. Filtering on ITS taxonomy beats
# guessing from the title: of 3,747 summer postings, Data/ML & AI/Quant are 973,
# while SWE alone is 822 and would otherwise arrive as noise.
# Observed tracks: SWE, Other, ML & AI, Data, Other Engineering, Quant,
# Operations, Finance, Hardware, PM, Security, Consulting.
WANTED_TRACKS = {"data", "ml & ai", "quant"}

# US states worth importing: WA (home) plus the tier-2 relocation metros'
# states already encoded in the location policy. fix-gates still enforces the
# 8+ bar for tier-2, so this only decides what enters the DB.
WANTED_STATES = {"WA", "CA", "NY", "IL", "TX", "MA", "DC", "VA"}


def _wanted(d: dict) -> bool:
    """Keep a posting when the SITE's own track and location match the profile.

    Remote roles are kept regardless of state. Anything with no state at all is
    kept only if remote — a location-less onsite posting is not actionable.
    """
    track = _as_text(d.get("track")).strip().lower()
    if track and track not in WANTED_TRACKS:
        return False

    mode = _as_text(d.get("mode")).strip().lower()
    states = d.get("placeStates")
    states = [str(s).upper() for s in states] if isinstance(states, list) else []
    countries = d.get("placeCountries")
    countries = [str(c).upper() for c in countries] if isinstance(countries, list) else []

    # Non-US postings are out; the rest of the pipeline is US-only.
    if countries and not any(c in ("US", "USA", "UNITED STATES") for c in countries):
        return False
    if "remote" in mode:
        return True
    if states:
        return any(s in WANTED_STATES for s in states)
    return False


def run_discovery(conn) -> int:
    """Fetch the allowed public pages and insert matching postings.

    Returns the number of newly inserted rows.
    """
    from applypilot.database import company_key, job_dupe_sig
    from applypilot.discovery.boards import _location_rejected, _title_excluded

    cfg = config.load_search_config() or {}
    now = datetime.now(timezone.utc).isoformat()
    new = existing = skipped = 0
    seen: set[str] = set()

    for idx, path in enumerate(PAGES):
        if idx:
            time.sleep(REQUEST_GAP_SECONDS)
        try:
            html = _fetch(path)
        except urllib.error.HTTPError as e:
            log.warning("EarlyCareerRadar %s: HTTP %s", path, e.code)
            continue
        except Exception as e:  # noqa: BLE001 — a dead source must not kill discovery
            log.warning("EarlyCareerRadar %s: %s", path, type(e).__name__)
            continue

        jobs = _job_objects(_decode_rsc(html))
        log.info("EarlyCareerRadar %s: %d postings in payload", path, len(jobs))

        for d in jobs:
            title = _as_text(d.get("title")).strip()
            apply_url = _as_text(d.get("applyUrl")).strip()
            if not title or not apply_url or apply_url in seen:
                continue
            seen.add(apply_url)

            if d.get("closed") is True:
                skipped += 1
                continue
            company = _as_text(d.get("company")).strip() or None
            location = _as_text(d.get("location")).strip()

            # Same gates the other providers apply at discovery.
            if _title_excluded(title, cfg) or _location_rejected(location, cfg):
                skipped += 1
                continue
            if not _wanted(d):
                skipped += 1
                continue

            deadline = _as_text(d.get("deadlineAt")).strip() or None
            posted = _as_text(d.get("postedAt")).strip() or None
            # No description on the listing page; enrichment fills it later, so
            # leave full_description NULL and let detail.py derive the signature
            # for rows it enriches. Still derive what we can now.
            try:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO jobs (url, title, location, site, strategy, "
                    "discovered_at, application_url, company, dupe_sig, company_key) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (apply_url, title, location, "earlycareerradar", "ecr",
                     now, apply_url, company,
                     job_dupe_sig(title, None), company_key(company)),
                )
                if cur.rowcount:
                    new += 1
                    if deadline or posted:
                        # Best-effort: skip silently on an older schema.
                        try:
                            conn.execute(
                                "UPDATE jobs SET deadline_at=COALESCE(?, deadline_at), "
                                "posted_at=COALESCE(?, posted_at) WHERE url=?",
                                (deadline, posted, apply_url))
                        except Exception:
                            pass
                else:
                    existing += 1
            except Exception as e:  # noqa: BLE001
                log.debug("EarlyCareerRadar insert failed for %s: %s", title[:40], e)

    conn.commit()
    log.info("EarlyCareerRadar: %d new, %d existing, %d skipped", new, existing, skipped)
    return new
