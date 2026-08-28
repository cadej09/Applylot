"""JobSpy-based job discovery: searches Indeed, LinkedIn, Glassdoor, ZipRecruiter.

Uses python-jobspy to scrape multiple job boards, deduplicates results,
parses salary ranges, and stores everything in the ApplyPilot database.

Search queries, locations, and filtering rules are loaded from the user's
search configuration YAML (searches.yaml) rather than being hardcoded.
"""

import logging
import re
import sqlite3
import time
from datetime import datetime, timezone

from jobspy import scrape_jobs
from jobspy.model import Country as _Country

from applypilot import config
from applypilot.database import (company_key, get_connection, init_db,
                                 job_dupe_sig, store_jobs)

log = logging.getLogger(__name__)


# jobspy's LinkedIn parser feeds scraped "City, State, Country" strings into
# Country.from_string, which raises on any country missing from its enum
# (moldova, kenya, ...) and kills the ENTIRE query's results — including the
# US-remote inventory. Location.country accepts a plain str and
# display_location() passes it through, so falling back to the raw string
# keeps the foreign country visible in the location; the US-verification
# gate in store_jobspy_results then drops just that one job and counts it
# in the unverifiable/foreign tally.
_orig_country_from_string = _Country.from_string.__func__


def _lenient_country_from_string(cls, country_str: str):
    try:
        return _orig_country_from_string(cls, country_str)
    except ValueError:
        log.debug("Unknown scraped country %r; keeping raw string", country_str)
        return country_str.strip().lower()


_Country.from_string = classmethod(_lenient_country_from_string)


# -- Proxy parsing -----------------------------------------------------------

def parse_proxy(proxy_str: str) -> dict:
    """Parse host:port:user:pass into components."""
    parts = proxy_str.split(":")
    if len(parts) == 4:
        host, port, user, passwd = parts
        return {
            "host": host,
            "port": port,
            "user": user,
            "pass": passwd,
            "jobspy": f"{user}:{passwd}@{host}:{port}",
            "playwright": {
                "server": f"http://{host}:{port}",
                "username": user,
                "password": passwd,
            },
        }
    elif len(parts) == 2:
        host, port = parts
        return {
            "host": host,
            "port": port,
            "user": None,
            "pass": None,
            "jobspy": f"{host}:{port}",
            "playwright": {"server": f"http://{host}:{port}"},
        }
    else:
        raise ValueError(
            f"Proxy format not recognized: {proxy_str}. "
            f"Expected: host:port:user:pass or host:port"
        )


# -- Retry wrapper -----------------------------------------------------------

def _scrape_with_retry(kwargs: dict, max_retries: int = 2, backoff: float = 5.0):
    """Call scrape_jobs with retry on transient failures."""
    for attempt in range(max_retries + 1):
        try:
            return scrape_jobs(**kwargs)
        except Exception as e:
            err = str(e).lower()
            transient = any(k in err for k in ("timeout", "429", "proxy", "connection", "reset", "refused"))
            if transient and attempt < max_retries:
                wait = backoff * (attempt + 1)
                log.warning("Retry %d/%d in %.0fs: %s", attempt + 1, max_retries, wait, e)
                time.sleep(wait)
            else:
                raise


# -- Location filtering ------------------------------------------------------

def _load_location_config(search_cfg: dict) -> tuple[list[str], list[str]]:
    """Extract accept/reject location lists from search config.

    Falls back to sensible defaults if not defined in the YAML.
    """
    accept = search_cfg.get("location_accept", [])
    reject = search_cfg.get("location_reject_non_remote", [])
    return accept, reject


def _location_ok(location: str | None, accept: list[str], reject: list[str]) -> bool:
    """Check if a job location passes the user's location filter.

    Remote jobs are always accepted. Non-remote jobs must match an accept
    pattern and not match a reject pattern.
    """
    if not location:
        return True  # unknown location -- keep it, let scorer decide

    loc = location.lower()

    # Reject first -- catches foreign "remote" jobs (e.g. "Remote, India")
    # before the remote shortcut can wave them through.
    for r in reject:
        if r.lower() in loc:
            return False

    # Remote jobs OK (foreign ones already rejected above)
    if any(r in loc for r in ("remote", "anywhere", "work from home", "wfh", "distributed")):
        return True

    # Accept matches
    for a in accept:
        if a.lower() in loc:
            return True

    # No match -- reject unknown
    return False


# -- US-verification at insert time -------------------------------------------
# User rule: if a job's location cannot be verified as US (home-area city,
# US state code, or explicit US language in the location OR description),
# it never enters the DB. Kills foreign-remote and location-less postings
# at the door instead of wasting scoring/tailoring on them.

_US_EVIDENCE_RX = re.compile(
    r"united states|u\.s\.|\busa\b|\bus[- ]based\b|within the us\b|"
    r"\bus only\b|remote \(us\)|remote - us\b|us remote|"
    r"authorized to work in the (us|united states)|us work authorization|"
    # Proper "City, ST" shape, matched CASE-SENSITIVELY via a scoped (?-i:)
    # so the two-letter state codes can't collide with English words like
    # "or"/"in"/"me"/"hi"/"ok" the way the old lowercase alternation did.
    r"(?-i:[A-Z][A-Za-z.]+,\s*(?:WA|OR|CA|TX|NY|IL|CO|GA|NC|VA|AZ|MA|PA|FL|OH|MI|MN|UT|TN|MO|MD|NJ|WI|IN|SC|AL|KY|OK|CT|IA|NV|AR|KS|MS|NM|NE|ID|HI|NH|ME|MT|RI|DE|SD|ND|AK|VT|WV|WY)\b)",
    re.IGNORECASE,
)
_FOREIGN_LOC_RX = re.compile(
    r"canada|united kingdom|\buk\b|ireland|india|philippines|pakistan|"
    r"mexico|brazil|argentina|colombia|europe|\bemea\b|\bapac\b|\blatam\b|"
    r"australia|new zealand|germany|france|spain|poland|portugal|romania|"
    r"netherlands|singapore|japan|korea|vietnam|nigeria|egypt|turkey|"
    r"türkiye|ukraine|worldwide|global[- ]remote",
    re.IGNORECASE,
)


def _tok_in(tok: str, loc: str) -> bool:
    """Substring match, but word-bounded for short tokens ('wa' must not
    match inside 'Newark' or 'Delaware')."""
    if len(tok) <= 3:
        return bool(re.search(rf"(?<![a-z]){re.escape(tok)}(?![a-z])", loc))
    return tok in loc


def _us_verified(location: str, description: str, accept_tokens: list[str]) -> bool:
    """User rule: onsite/hybrid must be in the home area; remote must be
    verifiably US; no location at all requires US evidence in the text."""
    loc = (location or "").lower()
    if _FOREIGN_LOC_RX.search(loc):
        return False
    remoteish = ("remote" in loc or "anywhere" in loc
                 or loc.strip() in ("united states", "usa", "us"))
    if remoteish:
        blob = f"{location or ''} {(description or '')[:5000]}"
        return bool(_US_EVIDENCE_RX.search(blob))
    if loc:
        # Onsite/hybrid: home-area cities/state ONLY. "Austin, TX" or
        # "Chicago, Illinois" or "NYC Metro Area" all fail here.
        dc = ("washington, dc" in loc or "washington dc" in loc
              or ", dc" in loc or "d.c" in loc)
        return not dc and any(_tok_in(t, loc) for t in accept_tokens)
    # No location given: require US evidence in the description
    return bool(_US_EVIDENCE_RX.search((description or "")[:5000]))


# -- DB storage (JobSpy DataFrame -> SQLite) ---------------------------------

def store_jobspy_results(conn: sqlite3.Connection, df, source_label: str) -> tuple[int, int]:
    """Store JobSpy DataFrame results into the DB. Returns (new, existing)."""
    now = datetime.now(timezone.utc).isoformat()
    new = 0
    existing = 0
    dropped_loc = 0

    # Discovery-time seniority filter: wrong-level titles never enter the
    # pipeline, so they cost zero enrichment/scoring time downstream.
    _search_cfg = config.load_search_config() or {}
    _excl = [x.lower() for x in _search_cfg.get("exclude_titles", [])]
    _accept_tokens = [p.lower() for p in _search_cfg.get("location", {}).get("accept_patterns", [])
                      if p.lower() not in ("remote", "anywhere", "united states", "us", "usa")]

    for _, row in df.iterrows():
        url = str(row.get("job_url", ""))
        if not url or url == "nan":
            continue

        title = str(row.get("title", "")) if str(row.get("title", "")) != "nan" else None
        if title and _excl and any(x in title.lower() for x in _excl):
            continue  # senior/lead/intern/clearance titles: skip at the door
        company = str(row.get("company", "")) if str(row.get("company", "")) != "nan" else None
        location_str = str(row.get("location", "")) if str(row.get("location", "")) != "nan" else None

        # Build salary string from min/max
        salary = None
        min_amt = row.get("min_amount")
        max_amt = row.get("max_amount")
        interval = str(row.get("interval", "")) if str(row.get("interval", "")) != "nan" else ""
        currency = str(row.get("currency", "")) if str(row.get("currency", "")) != "nan" else ""
        if min_amt and str(min_amt) != "nan":
            if max_amt and str(max_amt) != "nan":
                salary = f"{currency}{int(float(min_amt)):,}-{currency}{int(float(max_amt)):,}"
            else:
                salary = f"{currency}{int(float(min_amt)):,}"
            if interval:
                salary += f"/{interval}"

        description = str(row.get("description", "")) if str(row.get("description", "")) != "nan" else None
        site_name = str(row.get("site", source_label))
        is_remote = row.get("is_remote", False)

        site_label = f"{site_name}"
        if is_remote:
            location_str = f"{location_str} (Remote)" if location_str else "Remote"

        strategy = "jobspy"

        # If JobSpy gave us a full description, promote it directly
        full_description = None
        detail_scraped_at = None
        if description and len(description) > 200:
            full_description = description
            detail_scraped_at = now

        # Extract apply URL if JobSpy provided it
        apply_url = str(row.get("job_url_direct", "")) if str(row.get("job_url_direct", "")) != "nan" else None

        # US-verification gate: unverifiable/foreign locations never enter
        if not _us_verified(location_str or "", description or "", _accept_tokens):
            dropped_loc += 1
            continue

        try:
            # dupe_sig/company_key MUST be derived here, not only in the
            # enrichment stage: when JobSpy supplies a description inline the
            # row is written already-enriched and never reaches detail.py, so
            # it would carry a NULL signature forever. The apply-stage guard
            # requires both sides non-NULL, so a NULL sig silently disables it
            # — that is how one AWS req was applied to twice on 08-08 and again
            # on 08-11 (0 of 1,166 rows discovered since 08-09 had a sig).
            conn.execute(
                "INSERT INTO jobs (url, title, salary, description, location, site, strategy, discovered_at, "
                "full_description, application_url, detail_scraped_at, company, dupe_sig, company_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (url, title, salary, description, location_str, site_label, strategy, now,
                 full_description, apply_url, detail_scraped_at, company,
                 job_dupe_sig(title, full_description), company_key(company)),
            )
            new += 1
        except sqlite3.IntegrityError:
            existing += 1

    if dropped_loc:
        log.info("US-verification gate: dropped %d unverifiable/foreign jobs", dropped_loc)
    conn.commit()
    return new, existing


# -- Single search execution -------------------------------------------------

def _run_one_search(
    search: dict,
    sites: list[str],
    results_per_site: int,
    hours_old: int,
    proxy_config: dict | None,
    defaults: dict,
    max_retries: int,
    accept_locs: list[str],
    reject_locs: list[str],
    glassdoor_map: dict,
) -> dict:
    """Run a single search query and store results in DB."""
    s = search
    label = f"\"{s['query']}\" in {s['location']} {'(remote)' if s.get('remote') else ''}"
    if "tier" in s:
        label += f" [tier {s['tier']}]"

    # Split sites: Glassdoor needs simplified location, others use original
    gd_location = glassdoor_map.get(s["location"], s["location"].split(",")[0])
    has_glassdoor = "glassdoor" in sites
    other_sites = [si for si in sites if si != "glassdoor"]

    all_dfs = []

    # Run non-Glassdoor sites with original location
    if other_sites:
        kwargs = {
            "site_name": other_sites,
            "search_term": s["query"],
            "location": s["location"],
            "results_wanted": results_per_site,
            "hours_old": hours_old,
            "description_format": "markdown",
            "country_indeed": defaults.get("country_indeed", "usa"),
            "verbose": 0,
        }
        if s.get("remote"):
            kwargs["is_remote"] = True
        if proxy_config:
            kwargs["proxies"] = [proxy_config["jobspy"]]
        if "linkedin" in other_sites:
            kwargs["linkedin_fetch_description"] = True
        try:
            df = _scrape_with_retry(kwargs, max_retries=max_retries)
            all_dfs.append(df)
        except Exception as e:
            log.error("[%s] (non-gd): %.200s", label, e)
            # jobspy parse bugs kill the whole combined call. (The
            # "Invalid country string" one is patched lenient at module
            # import above.) Rescue by scraping each site individually so
            # one bad parser only loses its own site's results instead of
            # all of them.
            if len(other_sites) > 1:
                for si in other_sites:
                    solo = {**kwargs, "site_name": [si]}
                    try:
                        all_dfs.append(_scrape_with_retry(solo, max_retries=1))
                        log.info("[%s] per-site rescue: %s ok", label, si)
                    except Exception as se:
                        log.warning("[%s] per-site rescue: %s failed (%.100s)",
                                    label, si, se)

    # Run Glassdoor separately with simplified location
    if has_glassdoor:
        gd_kwargs = {
            "site_name": ["glassdoor"],
            "search_term": s["query"],
            "location": gd_location,
            "results_wanted": results_per_site,
            "hours_old": hours_old,
            "description_format": "markdown",
            "verbose": 0,
        }
        if s.get("remote"):
            gd_kwargs["is_remote"] = True
        if proxy_config:
            gd_kwargs["proxies"] = [proxy_config["jobspy"]]
        try:
            gd_df = _scrape_with_retry(gd_kwargs, max_retries=max_retries)
            all_dfs.append(gd_df)
        except Exception as e:
            log.error("[%s] (glassdoor): %s", label, e)

    if not all_dfs:
        log.error("[%s]: all sites failed", label)
        return {"new": 0, "existing": 0, "errors": 1, "filtered": 0, "total": 0, "label": label}

    import pandas as pd
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        df = pd.concat(all_dfs, ignore_index=True) if len(all_dfs) > 1 else all_dfs[0]

    if len(df) == 0:
        log.info("[%s] 0 results", label)
        return {"new": 0, "existing": 0, "errors": 0, "filtered": 0, "total": 0, "label": label}

    # Filter by location before storing
    before = len(df)
    df = df[df.apply(lambda row: _location_ok(
        str(row.get("location", "")) if str(row.get("location", "")) != "nan" else None,
        accept_locs, reject_locs,
    ), axis=1)]
    filtered = before - len(df)

    conn = get_connection()
    new, existing = store_jobspy_results(conn, df, s["query"])

    msg = f"[{label}] {before} results -> {new} new, {existing} dupes"
    if filtered:
        msg += f", {filtered} filtered (location)"
    log.info(msg)

    return {"new": new, "existing": existing, "errors": 0, "filtered": filtered, "total": before, "label": label}


# -- Single query search -----------------------------------------------------

def search_jobs(
    query: str,
    location: str,
    sites: list[str] | None = None,
    remote_only: bool = False,
    results_per_site: int = 50,
    hours_old: int = 72,
    proxy: str | None = None,
    country_indeed: str = "usa",
) -> dict:
    """Run a single job search via JobSpy and store results in DB."""
    if sites is None:
        sites = ["indeed", "linkedin", "zip_recruiter"]

    proxy_config = parse_proxy(proxy) if proxy else None

    log.info("Search: \"%s\" in %s | sites=%s | remote=%s", query, location, sites, remote_only)

    kwargs = {
        "site_name": sites,
        "search_term": query,
        "location": location,
        "results_wanted": results_per_site,
        "hours_old": hours_old,
        "description_format": "markdown",
        "country_indeed": country_indeed,
        "verbose": 2,
    }

    if remote_only:
        kwargs["is_remote"] = True

    if proxy_config:
        kwargs["proxies"] = [proxy_config["jobspy"]]

    if "linkedin" in sites:
        kwargs["linkedin_fetch_description"] = True

    try:
        df = scrape_jobs(**kwargs)
    except Exception as e:
        log.error("JobSpy search failed: %s", e)
        return {"error": str(e), "total": 0, "new": 0, "existing": 0}

    total = len(df)
    log.info("JobSpy returned %d results", total)

    if total == 0:
        return {"total": 0, "new": 0, "existing": 0}

    if "site" in df.columns:
        site_counts = df["site"].value_counts()
        for site, count in site_counts.items():
            log.info("  %s: %d", site, count)

    conn = init_db()
    new, existing = store_jobspy_results(conn, df, query)
    log.info("Stored: %d new, %d already in DB", new, existing)

    db_total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    pending = conn.execute("SELECT COUNT(*) FROM jobs WHERE detail_scraped_at IS NULL").fetchone()[0]
    log.info("DB total: %d jobs, %d pending detail scrape", db_total, pending)

    return {"total": total, "new": new, "existing": existing}


# -- Full crawl (all queries x all locations) --------------------------------

def _full_crawl(
    search_cfg: dict,
    tiers: list[int] | None = None,
    locations: list[str] | None = None,
    sites: list[str] | None = None,
    results_per_site: int = 100,
    hours_old: int = 72,
    proxy: str | None = None,
    max_retries: int = 2,
) -> dict:
    """Run all search queries from search config across all locations."""
    if sites is None:
        sites = ["indeed", "linkedin", "zip_recruiter"]

    # Build search combinations from config
    queries = search_cfg.get("queries", [])
    locs = search_cfg.get("locations", [])
    defaults = search_cfg.get("defaults", {})
    glassdoor_map = search_cfg.get("glassdoor_location_map", {})
    accept_locs, reject_locs = _load_location_config(search_cfg)

    if tiers:
        queries = [q for q in queries if q.get("tier") in tiers]
    if locations:
        locs = [loc for loc in locs if loc.get("label") in locations]

    searches = []
    for q in queries:
        for loc in locs:
            searches.append({
                "query": q["query"],
                "location": loc["location"],
                "remote": loc.get("remote", False),
                "tier": q.get("tier", 0),
            })

    proxy_config = parse_proxy(proxy) if proxy else None

    log.info("Full crawl: %d search combinations", len(searches))
    log.info("Sites: %s | Results/site: %d | Hours old: %d",
             ", ".join(sites), results_per_site, hours_old)

    # Ensure DB schema is ready
    init_db()

    total_new = 0
    total_existing = 0
    total_errors = 0
    completed = 0

    for s in searches:
        result = _run_one_search(
            s, sites, results_per_site, hours_old,
            proxy_config, defaults, max_retries,
            accept_locs, reject_locs, glassdoor_map,
        )
        completed += 1
        total_new += result["new"]
        total_existing += result["existing"]
        total_errors += result["errors"]

        if completed % 5 == 0 or completed == len(searches):
            log.info("Progress: %d/%d queries done (%d new, %d dupes, %d errors)",
                     completed, len(searches), total_new, total_existing, total_errors)

    # Final stats
    conn = get_connection()
    db_total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    log.info("Full crawl complete: %d new | %d dupes | %d errors | %d total in DB",
             total_new, total_existing, total_errors, db_total)

    return {
        "new": total_new,
        "existing": total_existing,
        "errors": total_errors,
        "db_total": db_total,
        "queries": len(searches),
    }


# -- Public entry point ------------------------------------------------------

def run_discovery(cfg: dict | None = None) -> dict:
    """Main entry point for JobSpy-based job discovery.

    Loads search queries and locations from the user's search config YAML,
    then runs a full crawl across all configured job boards.

    Args:
        cfg: Override the search configuration dict. If None, loads from
             the user's searches.yaml file.

    Returns:
        Dict with stats: new, existing, errors, db_total, queries.
    """
    if cfg is None:
        cfg = config.load_search_config()

    if not cfg:
        log.warning("No search configuration found. Run `applypilot init` to create one.")
        return {"new": 0, "existing": 0, "errors": 0, "db_total": 0, "queries": 0}

    proxy = cfg.get("proxy")
    sites = cfg.get("sites")
    results_per_site = cfg.get("defaults", {}).get("results_per_site", 100)
    hours_old = cfg.get("defaults", {}).get("hours_old", 72)
    tiers = cfg.get("tiers")
    locations = cfg.get("location_labels")

    return _full_crawl(
        search_cfg=cfg,
        tiers=tiers,
        locations=locations,
        sites=sites,
        results_per_site=results_per_site,
        hours_old=hours_old,
        proxy=proxy,
    )
