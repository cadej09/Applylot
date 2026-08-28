"""ApplyPilot database layer: schema, migrations, stats, and connection helpers.

Single source of truth for the jobs table schema. All columns from every
pipeline stage are created up front so any stage can run independently
without migration ordering issues.
"""

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from applypilot.config import DB_PATH, DEFAULTS

# Thread-local connection storage — each thread gets its own connection
# (required for SQLite thread safety with parallel workers)
_local = threading.local()


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Get a thread-local cached SQLite connection with WAL mode enabled.

    Each thread gets its own connection (required for SQLite thread safety).
    Connections are cached and reused within the same thread.

    Args:
        db_path: Override the default DB_PATH. Useful for testing.

    Returns:
        sqlite3.Connection configured with WAL mode and row factory.
    """
    path = str(db_path or DB_PATH)

    if not hasattr(_local, 'connections'):
        _local.connections = {}

    conn = _local.connections.get(path)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except sqlite3.ProgrammingError:
            pass

    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    _local.connections[path] = conn
    return conn


def close_connection(db_path: Path | str | None = None) -> None:
    """Close the cached connection for the current thread."""
    path = str(db_path or DB_PATH)
    if hasattr(_local, 'connections'):
        conn = _local.connections.pop(path, None)
        if conn is not None:
            conn.close()


def init_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Create the full jobs table with all columns from every pipeline stage.

    This is idempotent -- safe to call on every startup. Uses CREATE TABLE IF NOT EXISTS
    so it won't destroy existing data.

    Schema columns by stage:
      - Discovery:  url, title, salary, description, location, site, strategy, discovered_at
      - Enrichment: full_description, application_url, detail_scraped_at, detail_error
      - Scoring:    fit_score, score_reasoning, scored_at
      - Tailoring:  tailored_resume_path, tailored_at, tailor_attempts
      - Cover:      cover_letter_path, cover_letter_at, cover_attempts
      - Apply:      applied_at, apply_status, apply_error, apply_attempts,
                   agent_id, last_attempted_at, apply_duration_ms, apply_task_id,
                   verification_confidence

    Args:
        db_path: Override the default DB_PATH.

    Returns:
        sqlite3.Connection with the schema initialized.
    """
    path = db_path or DB_PATH

    # Ensure parent directory exists
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    conn = get_connection(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            -- Discovery stage (smart_extract / job_search)
            url                   TEXT PRIMARY KEY,
            title                 TEXT,
            salary                TEXT,
            description           TEXT,
            location              TEXT,
            site                  TEXT,
            company               TEXT,
            strategy              TEXT,
            discovered_at         TEXT,

            -- Enrichment stage (detail_scraper)
            full_description      TEXT,
            application_url       TEXT,
            detail_scraped_at     TEXT,
            detail_error          TEXT,

            -- Scoring stage (job_scorer)
            fit_score             INTEGER,
            score_reasoning       TEXT,
            scored_at             TEXT,

            -- Tailoring stage (resume tailor)
            tailored_resume_path  TEXT,
            tailored_at           TEXT,
            tailor_attempts       INTEGER DEFAULT 0,

            -- Cover letter stage
            cover_letter_path     TEXT,
            cover_letter_at       TEXT,
            cover_attempts        INTEGER DEFAULT 0,

            -- Application stage
            applied_at            TEXT,
            apply_status          TEXT,
            apply_error           TEXT,
            apply_attempts        INTEGER DEFAULT 0,
            agent_id              TEXT,
            last_attempted_at     TEXT,
            apply_duration_ms     INTEGER,
            apply_task_id         TEXT,
            verification_confidence TEXT
        )
    """)
    conn.commit()

    # Run migrations for any columns added after initial schema
    ensure_columns(conn)

    return conn


# Complete column registry: column_name -> SQL type with optional default.
# This is the single source of truth. Adding a column here is all that's needed
# for it to appear in both new databases and migrated ones.
_ALL_COLUMNS: dict[str, str] = {
    # Discovery
    "url": "TEXT PRIMARY KEY",
    "title": "TEXT",
    "salary": "TEXT",
    "description": "TEXT",
    "location": "TEXT",
    "site": "TEXT",
    # Real employer name. acquire_job's per-company throttle uses
    # COALESCE(company, site) and discovery INSERTs it; without this column a
    # fresh DB breaks. ensure_columns adds it to already-migrated DBs.
    "company": "TEXT",
    "strategy": "TEXT",
    "discovered_at": "TEXT",
    # Enrichment
    "full_description": "TEXT",
    "application_url": "TEXT",
    "detail_scraped_at": "TEXT",
    "detail_error": "TEXT",
    # Scoring
    "fit_score": "INTEGER",
    "score_reasoning": "TEXT",
    "scored_at": "TEXT",
    # Tailoring
    "tailored_resume_path": "TEXT",
    "tailored_at": "TEXT",
    "tailor_attempts": "INTEGER DEFAULT 0",
    # Cover letter
    "cover_letter_path": "TEXT",
    "cover_letter_at": "TEXT",
    "cover_attempts": "INTEGER DEFAULT 0",
    # Application
    "applied_at": "TEXT",
    "apply_status": "TEXT",
    "apply_error": "TEXT",
    "apply_attempts": "INTEGER DEFAULT 0",
    "agent_id": "TEXT",
    "last_attempted_at": "TEXT",
    "apply_duration_ms": "INTEGER",
    "apply_task_id": "TEXT",
    "verification_confidence": "TEXT",
    # Cross-board duplicate detection (2026-07-29). See job_dupe_sig().
    "dupe_sig": "TEXT",
    # Normalised employer key for duplicate comparison. See company_key().
    "company_key": "TEXT",
}


# Umbrella-brand aliases: boards list the same requisition under the parent
# brand on one site and a sub-brand on another — "Amazon.com" vs "Amazon Web
# Services (AWS)" double-applied the AWS Assurance Analyst req on 2026-08-08.
# First-token keying already folds "Amazon Web Services" into "amazon"; this
# maps the sub-brand keys that survive it onto the parent's key. Entries are
# POST-normalization values (lowercase first-token), aliased -> canonical.
_UMBRELLA_ALIASES = {
    "aws": "amazon",
    "alphabet": "google",
    "youtube": "google",
    "deepmind": "google",
    "facebook": "meta",
    "instagram": "meta",
    "whatsapp": "meta",
    "bytedance": "tiktok",
}


def company_key(company: str | None) -> str | None:
    """Collapse an employer name to a comparison key.

    The same employer appears under trivial variants across boards -- "Amazon"
    vs "Amazon.com", "Costco IT" vs "Costco Wholesale", "Jackson Lewis" vs
    "Jackson Lewis P.C." -- and comparing the raw strings let three real
    duplicate applications through on 2026-07-29.

    Splits on non-alphanumerics and keeps the first token, so ".com" and legal
    suffixes drop away. Distinct employers keep distinct keys ("Qureos" vs
    "Burjline Builders"). A first-token collision between two genuinely
    different companies is possible in principle, but the duplicate guard also
    requires an identical description signature, so it cannot fire on its own.

    A SINGLE-character first token absorbs the next one: "T-Mobile" splits to
    ["t", "mobile"] and would otherwise key as "t", colliding with every company
    starting with T ("T. Rowe Price" -> "trowe"). Two characters are left alone,
    so "3M" and "3M Company" both key as "3m" rather than "3m" vs "3mcompany".

    Finally the key passes through _UMBRELLA_ALIASES so sub-brands collapse
    onto their parent ("AWS" -> "amazon", "Alphabet" -> "google") — first-token
    keying can't catch those, and it let one AWS req through twice (2026-08-08).
    """
    import re

    parts = [p for p in re.split(r"[^a-z0-9]+", (company or "").lower()) if p]
    if not parts:
        return None
    key = parts[0]
    if len(key) < 2 and len(parts) > 1:
        key += parts[1]
    return _UMBRELLA_ALIASES.get(key, key)


def job_dupe_sig(title: str | None, description: str | None) -> str | None:
    """Signature identifying the SAME job posting across different boards.

    The same role listed on Indeed, LinkedIn and the employer's own ATS is three
    rows with three URLs, so URL-keyed dedup never catches it and we applied to
    the same job up to 3 times. Employer+title only helps when `company` is
    populated, which aggregator rows often lack at apply time.

    What works (measured on confirmed duplicate sets, 2026-07-29): the employer
    writes the description once, so it survives re-listing. Boards only mangle
    punctuation, casing and whitespace — so strip everything that isn't
    alphanumeric, lowercase it, and hash a 600-char window. The pre-existing
    heal check hashed 2,000 chars with only whitespace collapsed, which board
    formatting broke every time.

    Returns None when there is too little description to be a reliable key
    (short stubs and pruned rows must never collide with each other).
    """
    import hashlib
    import re

    body = re.sub(r"[^a-z0-9 ]", " ", (description or "").lower())
    body = re.sub(r"\s+", " ", body).strip()
    if len(body) < 200:
        return None
    head = re.sub(r"[^a-z0-9]", "", (title or "").lower())[:40]
    return hashlib.md5(f"{head}|{body[:600]}".encode()).hexdigest()[:16]


def ensure_columns(conn: sqlite3.Connection | None = None) -> list[str]:
    """Add any missing columns to the jobs table (forward migration).

    Reads the current table schema via PRAGMA table_info and compares against
    the full column registry. Any missing columns are added with ALTER TABLE.

    This makes it safe to upgrade the database from any previous version --
    columns are only added, never removed or renamed.

    Args:
        conn: Database connection. Uses get_connection() if None.

    Returns:
        List of column names that were added (empty if schema was already current).
    """
    if conn is None:
        conn = get_connection()

    existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    added = []

    for col, dtype in _ALL_COLUMNS.items():
        if col not in existing:
            # PRIMARY KEY columns can't be added via ALTER TABLE, but url
            # is always created with the table itself so this is safe
            if "PRIMARY KEY" in dtype:
                continue
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {dtype}")
            added.append(col)

    # acquire_job runs a correlated NOT EXISTS on dupe_sig for EVERY claim, and
    # applied_at/company gate most other queries. Without these the duplicate
    # guard full-scans ~23k rows per claim (measured: seconds per job).
    for name, ddl in (
        ("idx_jobs_dupe_sig", "CREATE INDEX IF NOT EXISTS idx_jobs_dupe_sig "
                              "ON jobs(dupe_sig) WHERE dupe_sig IS NOT NULL"),
        ("idx_jobs_applied_at", "CREATE INDEX IF NOT EXISTS idx_jobs_applied_at "
                                "ON jobs(applied_at) WHERE applied_at IS NOT NULL"),
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass  # older SQLite without partial-index support: not fatal

    if added:
        conn.commit()
    else:
        conn.commit()

    return added


def get_stats(conn: sqlite3.Connection | None = None) -> dict:
    """Return job counts by pipeline stage.

    Provides a snapshot of how many jobs are at each stage, useful for
    dashboard display and pipeline progress tracking.

    Args:
        conn: Database connection. Uses get_connection() if None.

    Returns:
        Dictionary with keys:
            total, by_site, pending_detail, with_description,
            scored, unscored, tailored, untailored_eligible,
            with_cover_letter, applied, score_distribution
    """
    if conn is None:
        conn = get_connection()

    stats: dict = {}

    # Total jobs
    stats["total"] = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    # By site breakdown
    rows = conn.execute(
        "SELECT site, COUNT(*) as cnt FROM jobs GROUP BY site ORDER BY cnt DESC"
    ).fetchall()
    stats["by_site"] = [(row[0], row[1]) for row in rows]

    # Enrichment stage
    stats["pending_detail"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE detail_scraped_at IS NULL"
    ).fetchone()[0]

    stats["with_description"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL"
    ).fetchone()[0]

    stats["detail_errors"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE detail_error IS NOT NULL"
    ).fetchone()[0]

    # Scoring stage
    stats["scored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL"
    ).fetchone()[0]

    stats["unscored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE full_description IS NOT NULL AND fit_score IS NULL"
    ).fetchone()[0]

    # Score distribution
    dist_rows = conn.execute(
        "SELECT fit_score, COUNT(*) as cnt FROM jobs "
        "WHERE fit_score IS NOT NULL "
        "GROUP BY fit_score ORDER BY fit_score DESC"
    ).fetchall()
    stats["score_distribution"] = [(row[0], row[1]) for row in dist_rows]

    # Tailoring stage
    stats["tailored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL"
    ).fetchone()[0]

    stats["untailored_eligible"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE fit_score >= 6 AND full_description IS NOT NULL "
        "AND tailored_resume_path IS NULL"
    ).fetchone()[0]

    stats["tailor_exhausted"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE COALESCE(tailor_attempts, 0) >= 5 "
        "AND tailored_resume_path IS NULL"
    ).fetchone()[0]

    # Cover letter stage
    stats["with_cover_letter"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE cover_letter_path IS NOT NULL"
    ).fetchone()[0]

    stats["cover_exhausted"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE COALESCE(cover_attempts, 0) >= 5 "
        "AND (cover_letter_path IS NULL OR cover_letter_path = '')"
    ).fetchone()[0]

    # Application stage
    stats["applied"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL"
    ).fetchone()[0]

    stats["apply_errors"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE apply_error IS NOT NULL"
    ).fetchone()[0]

    # Terminal out-of-queue routing (score-gated). 'manual' = retryable failures
    # that exhausted the attempt cap with fit_score >= 8 (worth a hand-apply);
    # 'discarded' = hard-drop reasons or exhausted low-score jobs. Neither is
    # counted in ready_to_apply below.
    stats["manual"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE apply_status = 'manual'"
    ).fetchone()[0]

    stats["discarded"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE apply_status = 'discarded'"
    ).fetchone()[0]

    # Mirror acquire_job's queue predicate exactly so this number reflects the
    # jobs the apply stage would actually pick up: tailored, fit_score >= min,
    # not applied/in_progress/manual, and under the retry cap. (The old version
    # ignored the score, the retry cap, and in_progress/manual states while
    # requiring application_url the queue doesn't — an inflated count that never
    # dropped as jobs became un-applyable.)
    stats["ready_to_apply"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE tailored_resume_path IS NOT NULL "
        "AND fit_score >= ? "
        "AND (apply_status IS NULL "
        "     OR (apply_status = 'failed' "
        "         AND (last_attempted_at IS NULL "
        "              OR last_attempted_at < datetime('now', '-1 day')))) "
        "AND COALESCE(apply_attempts, 0) < ?",
        (DEFAULTS["min_score"], DEFAULTS["max_apply_attempts"]),
    ).fetchone()[0]

    return stats


def store_jobs(conn: sqlite3.Connection, jobs: list[dict],
               site: str, strategy: str) -> tuple[int, int]:
    """Store discovered jobs, skipping duplicates by URL.

    Args:
        conn: Database connection.
        jobs: List of job dicts with keys: url, title, salary, description, location.
        site: Source site name (e.g. "RemoteOK", "Dice").
        strategy: Extraction strategy used (e.g. "json_ld", "api_response", "css_selectors").

    Returns:
        Tuple of (new_count, duplicate_count).
    """
    now = datetime.now(timezone.utc).isoformat()
    new = 0
    existing = 0

    for job in jobs:
        url = job.get("url")
        if not url:
            continue
        try:
            conn.execute(
                "INSERT INTO jobs (url, title, salary, description, location, site, strategy, discovered_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (url, job.get("title"), job.get("salary"), job.get("description"),
                 job.get("location"), site, strategy, now),
            )
            new += 1
        except sqlite3.IntegrityError:
            existing += 1

    conn.commit()
    return new, existing


def get_jobs_by_stage(conn: sqlite3.Connection | None = None,
                      stage: str = "discovered",
                      min_score: int | None = None,
                      limit: int = 100) -> list[dict]:
    """Fetch jobs filtered by pipeline stage.

    Args:
        conn: Database connection. Uses get_connection() if None.
        stage: One of "discovered", "enriched", "scored", "tailored", "applied".
        min_score: Minimum fit_score filter (only relevant for scored+ stages).
        limit: Maximum number of rows to return.

    Returns:
        List of job dicts.
    """
    if conn is None:
        conn = get_connection()

    conditions = {
        "discovered": "1=1",
        "pending_detail": "detail_scraped_at IS NULL",
        "enriched": "full_description IS NOT NULL",
        "pending_score": "full_description IS NOT NULL AND fit_score IS NULL",
        "scored": "fit_score IS NOT NULL",
        "pending_tailor": (
            "fit_score >= ? AND full_description IS NOT NULL "
            "AND tailored_resume_path IS NULL AND COALESCE(tailor_attempts, 0) < 5 "
            "AND COALESCE(apply_status, '') != 'deferred'"
        ),
        "tailored": "tailored_resume_path IS NOT NULL",
        "pending_apply": (
            "tailored_resume_path IS NOT NULL AND applied_at IS NULL "
            "AND application_url IS NOT NULL"
        ),
        "applied": "applied_at IS NOT NULL",
    }

    where = conditions.get(stage, "1=1")
    params: list = []

    if "?" in where and min_score is not None:
        params.append(min_score)
    elif "?" in where:
        params.append(6)  # default min_score

    if min_score is not None and "fit_score" not in where and stage in ("scored", "tailored", "applied"):
        where += " AND fit_score >= ?"
        params.append(min_score)

    query = f"SELECT * FROM jobs WHERE {where} ORDER BY fit_score DESC NULLS LAST, discovered_at DESC"
    if limit > 0:
        query += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()

    # Convert sqlite3.Row objects to dicts
    if rows:
        columns = rows[0].keys()
        return [dict(zip(columns, row)) for row in rows]
    return []
