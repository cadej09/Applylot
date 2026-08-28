"""Extra job-board providers with free public APIs: RemoteOK + HN Who is Hiring.

Both are free JSON endpoints — no scraping, no login walls, no Cloudflare.
Jobs are filtered by the user's search queries and the hours_old window from
searches.yaml, then inserted with the same schema as the JobSpy provider.

Boards that CANNOT be added this way (login/anti-bot walled, documented so
nobody wastes a weekend rediscovering it): Wellfound, Y Combinator Jobs,
Levels.fyi, Otta/Welcome to the Jungle, Arc.dev, Dice, Built In.
"""

import html
import logging
import re
import sqlite3
import time
from datetime import datetime, timezone

import requests

from applypilot import config
from applypilot.database import get_connection

log = logging.getLogger(__name__)

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ApplyPilot/1.0"}
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return html.unescape(_TAG_RE.sub(" ", text or "")).strip()


def _query_terms(cfg: dict) -> list[str]:
    """Lowercased search query strings from searches.yaml."""
    return [q["query"].lower() for q in cfg.get("queries", []) if q.get("query")]


def _title_excluded(title: str, cfg: dict) -> bool:
    low = (title or "").lower()
    return any(x.lower() in low for x in cfg.get("exclude_titles", []))


def _location_rejected(location: str, cfg: dict) -> bool:
    low = (location or "").lower()
    rejects = cfg.get("location", {}).get("reject_patterns", [])
    return any(r.lower() in low for r in rejects)


def _insert_job(conn, url: str, title: str, location: str, site: str,
                strategy: str, description: str | None,
                salary: str = "", apply_url: str | None = None,
                company: str | None = None) -> bool:
    """Insert one job using the same schema as the JobSpy provider.

    Returns True if newly inserted, False if the URL already exists.
    """
    # US-verification gate (same rule as jobspy): remote/unknown locations
    # must show US evidence in the location or description, or they're out.
    from applypilot.discovery.jobspy import _us_verified
    accept_tokens = [p.lower() for p in
                     (config.load_search_config() or {}).get("location", {}).get("accept_patterns", [])
                     if p.lower() not in ("remote", "anywhere", "united states", "us", "usa")]
    if not _us_verified(location or "", description or "", accept_tokens):
        return False

    now = datetime.now(timezone.utc).isoformat()
    full_description = description if description and len(description) > 200 else None
    detail_scraped_at = now if full_description else None
    try:
        # dupe_sig/company_key derived at insert: a row written with a
        # full_description here is already "enriched" and never reaches
        # detail.py, so this is its only chance to get a signature. See the
        # matching note in jobspy.py.
        from applypilot.database import company_key, job_dupe_sig
        conn.execute(
            "INSERT INTO jobs (url, title, salary, description, location, site, strategy, "
            "discovered_at, full_description, application_url, detail_scraped_at, company, "
            "dupe_sig, company_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (url, title[:200], salary, (description or "")[:500], location, site,
             strategy, now, full_description, apply_url or url, detail_scraped_at,
             (company or "")[:120] or None,
             job_dupe_sig(title, full_description), company_key(company)),
        )
        return True
    except sqlite3.IntegrityError:
        return False


# ── RemoteOK ──────────────────────────────────────────────────────────────

def discover_remoteok(cfg: dict) -> dict:
    """Pull recent postings from the RemoteOK public JSON API.

    All RemoteOK jobs are remote; many are US-eligible. The scorer's location
    gate still makes the final call — this just gets candidates in the door.
    """
    hours_old = cfg.get("defaults", {}).get("hours_old", 24)
    terms = _query_terms(cfg)
    cutoff = time.time() - hours_old * 3600

    try:
        resp = requests.get("https://remoteok.com/api", headers=_UA, timeout=30)
        resp.raise_for_status()
        items = resp.json()
    except Exception as e:
        log.error("RemoteOK fetch failed: %s", e)
        return {"new": 0, "existing": 0, "error": str(e)}

    conn = get_connection()
    new = existing = 0
    for item in items:
        if not isinstance(item, dict) or "position" not in item:
            continue  # first element is a legal notice
        if float(item.get("epoch", 0) or 0) < cutoff:
            continue
        title = item.get("position", "")
        haystack = f"{title} {' '.join(item.get('tags', []) or [])}".lower()
        if not any(t in haystack for t in terms):
            continue
        if _title_excluded(title, cfg):
            continue
        location = item.get("location") or "Remote"
        if _location_rejected(location, cfg):
            continue
        url = item.get("url") or f"https://remoteok.com/remote-jobs/{item.get('slug', item.get('id', ''))}"
        company = item.get("company", "")
        desc = _strip_html(item.get("description", ""))
        salary = ""
        if item.get("salary_min") and item.get("salary_max"):
            salary = f"${int(item['salary_min']):,}-${int(item['salary_max']):,}"
        full_title = f"{title} ({company})" if company else title
        if _insert_job(conn, url, full_title, f"{location} (Remote)", "remoteok",
                       "boards", desc, salary, item.get("apply_url") or url,
                       company=company):
            new += 1
        else:
            existing += 1
    conn.commit()

    log.info("RemoteOK: %d new, %d existing (last %dh)", new, existing, hours_old)
    return {"new": new, "existing": existing}


# ── Hacker News "Who is Hiring" ──────────────────────────────────────────

def discover_hn_hiring(cfg: dict) -> dict:
    """Pull fresh job comments from this month's HN 'Ask HN: Who is hiring?'.

    Uses the free Algolia HN API. Only comments posted within hours_old are
    taken (the thread is monthly; a daily cycle only wants the new ones).
    Comments are kept if they mention a search query term AND look
    remote/US/Seattle-friendly; the scorer's location gate does the rest.
    """
    hours_old = cfg.get("defaults", {}).get("hours_old", 24)
    terms = _query_terms(cfg)
    cutoff = int(time.time()) - hours_old * 3600

    try:
        story = requests.get(
            "https://hn.algolia.com/api/v1/search_by_date",
            params={"query": "Ask HN: Who is hiring?",
                    "tags": "story,author_whoishiring", "hitsPerPage": 1},
            headers=_UA, timeout=30,
        ).json()["hits"]
        if not story:
            return {"new": 0, "existing": 0, "error": "no hiring thread found"}
        story_id = story[0]["objectID"]

        comments = requests.get(
            "https://hn.algolia.com/api/v1/search_by_date",
            params={"tags": f"comment,story_{story_id}", "hitsPerPage": 1000,
                    "numericFilters": f"created_at_i>{cutoff}"},
            headers=_UA, timeout=30,
        ).json().get("hits", [])
    except Exception as e:
        log.error("HN hiring fetch failed: %s", e)
        return {"new": 0, "existing": 0, "error": str(e)}

    conn = get_connection()
    new = existing = 0
    for c in comments:
        text = _strip_html(c.get("comment_text", ""))
        if len(text) < 100:
            continue
        low = text.lower()
        if not any(t in low for t in terms):
            continue
        # Loose geo prefilter: remote or Seattle-area mention; scorer verifies.
        if not any(k in low for k in ("remote", "seattle", "bellevue", "redmond", ", wa")):
            continue
        # Header convention: "Company | Role | Location | ..."
        first_line = text.split("\n")[0][:150]
        title = " | ".join(p.strip() for p in first_line.split("|")[:3]) or first_line
        if _title_excluded(title, cfg):
            continue
        url = f"https://news.ycombinator.com/item?id={c['objectID']}"
        if _insert_job(conn, url, title, "See posting (HN)", "hn-whoishiring",
                       "boards", text):
            new += 1
        else:
            existing += 1
    conn.commit()

    log.info("HN Who is Hiring: %d new, %d existing (last %dh)", new, existing, hours_old)
    return {"new": new, "existing": existing}


# ── The Muse (free public API, no key needed, has Entry Level filter) ────

_MUSE_CATEGORIES = ["Data and Analytics", "Data Science"]


def discover_themuse(cfg: dict) -> dict:
    """Pull entry-level data jobs from The Muse public API.

    No API key required at low volume. Entry Level + location filters do a
    lot of the screening before the scorer even sees the jobs.
    """
    import os
    from datetime import timedelta

    hours_old = cfg.get("defaults", {}).get("hours_old", 24)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_old)
    conn = get_connection()
    new = existing = 0

    for category in _MUSE_CATEGORIES:
        for page in range(3):  # 20/page; 3 pages per category is plenty daily
            try:
                params = {
                    "category": category,
                    "level": "Entry Level",
                    "location": ["Seattle, WA", "Flexible / Remote"],
                    "page": page,
                }
                api_key = os.environ.get("THEMUSE_API_KEY", "")
                if api_key:
                    params["api_key"] = api_key
                resp = requests.get("https://www.themuse.com/api/public/jobs",
                                    params=params, headers=_UA, timeout=30)
                resp.raise_for_status()
                results = resp.json().get("results", [])
            except Exception as e:
                log.warning("The Muse fetch failed (%s p%d): %s", category, page, e)
                break
            if not results:
                break
            for job in results:
                pub = job.get("publication_date", "")
                try:
                    if datetime.fromisoformat(pub.replace("Z", "+00:00")) < cutoff:
                        continue
                except ValueError:
                    pass
                title = job.get("name", "")
                if _title_excluded(title, cfg):
                    continue
                locations = ", ".join(l.get("name", "") for l in job.get("locations", []))
                if _location_rejected(locations, cfg):
                    continue
                company = (job.get("company") or {}).get("name", "")
                url = (job.get("refs") or {}).get("landing_page", "")
                if not url:
                    continue
                desc = _strip_html(job.get("contents", ""))
                full_title = f"{title} ({company})" if company else title
                if _insert_job(conn, url, full_title, locations or "See posting",
                               "themuse", "boards", desc, company=company):
                    new += 1
                else:
                    existing += 1
    conn.commit()
    log.info("The Muse: %d new, %d existing (last %dh)", new, existing, hours_old)
    return {"new": new, "existing": existing}


# ── Adzuna (free key: https://developer.adzuna.com — activates when set) ─

def discover_adzuna(cfg: dict) -> dict:
    """US job aggregate search via Adzuna. Needs ADZUNA_APP_ID/ADZUNA_APP_KEY."""
    import os
    app_id = os.environ.get("ADZUNA_APP_ID", "")
    app_key = os.environ.get("ADZUNA_APP_KEY", "")
    if not app_id or not app_key:
        return {"skipped": "no ADZUNA_APP_ID/ADZUNA_APP_KEY in .env"}

    hours_old = cfg.get("defaults", {}).get("hours_old", 24)
    conn = get_connection()
    new = existing = 0
    tier1 = [q["query"] for q in cfg.get("queries", []) if q.get("tier") == 1]

    for query in tier1 or ["data analyst"]:
        try:
            resp = requests.get(
                "https://api.adzuna.com/v1/api/jobs/us/search/1",
                params={"app_id": app_id, "app_key": app_key, "what": query,
                        "where": "Washington", "max_days_old": max(1, hours_old // 24),
                        "results_per_page": 50, "content-type": "application/json"},
                headers=_UA, timeout=30)
            resp.raise_for_status()
            results = resp.json().get("results", [])
        except Exception as e:
            log.warning("Adzuna fetch failed (%s): %s", query, e)
            continue
        for job in results:
            title = job.get("title", "")
            if _title_excluded(title, cfg):
                continue
            location = (job.get("location") or {}).get("display_name", "")
            if _location_rejected(location, cfg):
                continue
            url = job.get("redirect_url", "")
            company = (job.get("company") or {}).get("display_name", "")
            salary = ""
            if job.get("salary_min"):
                salary = f"${int(job['salary_min']):,}-${int(job.get('salary_max', job['salary_min'])):,}"
            full_title = f"{title} ({company})" if company else title
            if url and _insert_job(conn, url, full_title, location, "adzuna",
                                   "boards", job.get("description", ""), salary,
                                   company=company):
                new += 1
            else:
                existing += 1
    conn.commit()
    log.info("Adzuna: %d new, %d existing", new, existing)
    return {"new": new, "existing": existing}


# ── USAJOBS (free key: https://developer.usajobs.gov — activates when set) ─

def discover_usajobs(cfg: dict) -> dict:
    """Federal jobs via USAJOBS. Needs USAJOBS_API_KEY + USAJOBS_EMAIL."""
    import os
    api_key = os.environ.get("USAJOBS_API_KEY", "")
    email = os.environ.get("USAJOBS_EMAIL", "")
    if not api_key or not email:
        return {"skipped": "no USAJOBS_API_KEY/USAJOBS_EMAIL in .env"}

    conn = get_connection()
    new = existing = 0
    tier1 = [q["query"] for q in cfg.get("queries", []) if q.get("tier") == 1]
    headers = {**_UA, "Authorization-Key": api_key, "User-Agent": email}

    for query in tier1 or ["data analyst"]:
        try:
            resp = requests.get(
                "https://data.usajobs.gov/api/search",
                params={"Keyword": query, "LocationName": "Washington",
                        "DatePosted": 1, "ResultsPerPage": 50},
                headers=headers, timeout=30)
            resp.raise_for_status()
            items = resp.json().get("SearchResult", {}).get("SearchResultItems", [])
        except Exception as e:
            log.warning("USAJOBS fetch failed (%s): %s", query, e)
            continue
        for item in items:
            d = item.get("MatchedObjectDescriptor", {})
            title = d.get("PositionTitle", "")
            if _title_excluded(title, cfg):
                continue
            url = d.get("PositionURI", "")
            location = "; ".join(
                loc.get("LocationName", "") for loc in d.get("PositionLocation", [])
            )[:150]
            org = d.get("OrganizationName", "")
            desc = (d.get("UserArea", {}).get("Details", {}) or {}).get("JobSummary", "")
            full_title = f"{title} ({org})" if org else title
            if url and _insert_job(conn, url, full_title, location, "usajobs",
                                   "boards", desc, company=org):
                new += 1
            else:
                existing += 1
    conn.commit()
    log.info("USAJOBS: %d new, %d existing", new, existing)
    return {"new": new, "existing": existing}


# ── Entry point ──────────────────────────────────────────────────────────

def run_boards_discovery(cfg: dict | None = None) -> dict:
    """Run all extra-board providers. Called from the discover pipeline stage."""
    if cfg is None:
        cfg = config.load_search_config()
    if not cfg:
        return {"remoteok": "no config", "hn": "no config"}
    return {
        "remoteok": discover_remoteok(cfg),
        "hn_whoishiring": discover_hn_hiring(cfg),
        "themuse": discover_themuse(cfg),
        "adzuna": discover_adzuna(cfg),
        "usajobs": discover_usajobs(cfg),
    }
