"""Apply orchestration: acquire jobs, spawn Claude Code sessions, track results.

This is the main entry point for the apply pipeline. It pulls jobs from
the database, launches Chrome + Claude Code for each one, parses the
result, and updates the database. Supports parallel workers via --workers.
"""

import atexit
import json
import logging
import os
import platform
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.console import Console
from rich.live import Live

from applypilot import config
from applypilot.database import get_connection
from applypilot.apply import chrome, dashboard, prompt as prompt_mod
from applypilot.apply.chrome import (
    launch_chrome, cleanup_worker, kill_all_chrome,
    reset_worker_dir, cleanup_on_exit, _kill_process_tree,
    BASE_CDP_PORT,
)
from applypilot.apply.dashboard import (
    init_worker, update_state, add_event, get_state,
    render_full, get_totals,
)

logger = logging.getLogger(__name__)

# Blocked sites loaded from config/sites.yaml
def _load_blocked():
    from applypilot.config import load_blocked_sites
    return load_blocked_sites()

# How often to poll the DB when the queue is empty (seconds)
POLL_INTERVAL = config.DEFAULTS["poll_interval"]

# Thread-safe shutdown coordination
_stop_event = threading.Event()

# Track active Claude Code processes for skip (Ctrl+C) handling
_claude_procs: dict[int, subprocess.Popen] = {}
_claude_lock = threading.Lock()

# Register cleanup on exit
atexit.register(cleanup_on_exit)
if platform.system() != "Windows":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))


# ---------------------------------------------------------------------------
# MCP config
# ---------------------------------------------------------------------------

def _which(name: str) -> str:
    """Resolve an executable to its full path (Windows: finds .cmd/.exe shims
    that CreateProcess can't resolve from a bare name). Falls back to name."""
    import shutil
    return shutil.which(name) or name


def _make_mcp_config(cdp_port: int) -> dict:
    """Build MCP config dict for a specific CDP port."""
    npx = _which("npx")
    return {
        "mcpServers": {
            "playwright": {
                "command": npx,
                "args": [
                    "@playwright/mcp@latest",
                    f"--cdp-endpoint=http://localhost:{cdp_port}",
                    f"--viewport-size={config.DEFAULTS['viewport']}",
                ],
            },
            "gmail": {
                "command": npx,
                "args": ["-y", "@gongrzhe/server-gmail-autoauth-mcp"],
            },
        }
    }


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def acquire_job(target_url: str | None = None, min_score: int = 6,
                worker_id: int = 0) -> dict | None:
    """Atomically acquire the next job to apply to.

    Args:
        target_url: Apply to a specific URL instead of picking from queue.
        min_score: Minimum fit_score threshold.
        worker_id: Worker claiming this job (for tracking).

    Returns:
        Job dict or None if the queue is empty.
    """
    conn = get_connection()
    from applypilot.config import is_manual_ats
    # Loop so that skipping a manual-ATS candidate advances to the NEXT real
    # job instead of returning None (which the worker loop reads as "queue
    # empty" and quits early, stranding all remaining jobs).
    while True:
      try:
        conn.execute("BEGIN IMMEDIATE")

        if target_url:
            like = f"%{target_url.split('?')[0].rstrip('/')}%"
            row = conn.execute("""
                SELECT url, title, site, application_url, tailored_resume_path,
                       fit_score, location, full_description, cover_letter_path
                FROM jobs
                WHERE (url = ? OR application_url = ? OR application_url LIKE ? OR url LIKE ?)
                  AND tailored_resume_path IS NOT NULL
                  AND COALESCE(apply_status, 'idle') NOT IN
                      ('in_progress', 'manual', 'discarded', 'applied', 'expired')
                LIMIT 1
            """, (target_url, target_url, like, like)).fetchone()
        else:
            blocked_sites, blocked_patterns = _load_blocked()
            # Build parameterized filters to avoid SQL injection
            params: list = [min_score]
            site_clause = ""
            if blocked_sites:
                placeholders = ",".join("?" * len(blocked_sites))
                site_clause = f"AND site NOT IN ({placeholders})"
                params.extend(blocked_sites)
            url_clauses = ""
            if blocked_patterns:
                url_clauses = " ".join(f"AND url NOT LIKE ?" for _ in blocked_patterns)
                params.extend(blocked_patterns)
            row = conn.execute(f"""
                SELECT url, title, site, application_url, tailored_resume_path,
                       fit_score, location, full_description, cover_letter_path
                FROM jobs j
                WHERE tailored_resume_path IS NOT NULL
                  AND (apply_status IS NULL
                       OR (apply_status = 'failed'
                           AND (last_attempted_at IS NULL
                                OR last_attempted_at < datetime('now', '-1 day'))))
                  AND (apply_attempts IS NULL OR apply_attempts < ?)
                  -- Cross-board duplicate guard (user 2026-07-27). Dedup is
                  -- URL-keyed, so the SAME job listed on Indeed and LinkedIn is
                  -- two rows and we applied twice (Amazon Ads Science twice in
                  -- 19 minutes; 8 redundant applications total). heal-db's
                  -- title+description-hash twin check misses these because the
                  -- boards reformat the description. Same employer + same title
                  -- as something already applied to = skip.
                  -- Only when the employer is actually KNOWN: for aggregator
                  -- rows (company NULL, site 'linkedin') two different companies
                  -- can share a generic title like "Business Analyst", and
                  -- blocking those would drop legitimate jobs.
                  AND NOT EXISTS (
                        SELECT 1 FROM jobs a
                        WHERE a.applied_at IS NOT NULL
                          AND COALESCE(a.company, '') != ''
                          AND COALESCE(j.company, '') != ''
                          -- Raw-string compare alone missed the 2026-08-08
                          -- pair: "Amazon.com" on one board, "Amazon Web
                          -- Services (AWS)" on the other, same requisition
                          -- (heal-db canonicalizes names only on the NEXT
                          -- cycle). company_key collapses umbrella-brand
                          -- variants, so also match on it when both rows
                          -- carry one.
                          AND (LOWER(TRIM(a.company)) = LOWER(TRIM(j.company))
                               OR (COALESCE(a.company_key, '') != ''
                                   AND a.company_key = j.company_key))
                          AND LOWER(TRIM(a.title))   = LOWER(TRIM(j.title))
                          AND a.url != j.url)
                  -- Same posting re-listed on another board. The employer+title
                  -- guard above only fires when `company` is populated on BOTH
                  -- rows, and aggregator rows frequently have it NULL at apply
                  -- time (heal-db backfills the name only on the NEXT cycle) —
                  -- that hole let the same Nordstrom role through 3 times and
                  -- an Amazon role twice 24 minutes apart. dupe_sig is derived
                  -- from the description, which the employer writes once and
                  -- the boards only reformat, so it matches where names don't.
                  AND NOT EXISTS (
                        SELECT 1 FROM jobs a
                        WHERE a.applied_at IS NOT NULL
                          AND a.dupe_sig IS NOT NULL
                          AND j.dupe_sig IS NOT NULL
                          AND a.dupe_sig = j.dupe_sig
                          AND a.url != j.url
                          -- Small firms and agencies post identical boilerplate,
                          -- so an identical description across two DIFFERENT
                          -- known employers is two real jobs, not a re-list.
                          -- Only block when the employers agree or at least one
                          -- is unknown (the aggregator case this exists for).
                          -- Compare on company_key, not the raw name: matching
                          -- raw strings treated "Amazon"/"Amazon.com",
                          -- "Costco IT"/"Costco Wholesale" and "Jackson Lewis"/
                          -- "Jackson Lewis P.C." as different employers and let
                          -- three real duplicates through.
                          AND (COALESCE(a.company_key, '') = ''
                               OR COALESCE(j.company_key, '') = ''
                               OR a.company_key = j.company_key))
                  -- Per-company throttle:
                  --   fit_score >= 8: EXEMPT — a great match is always worth
                  --     applying to, no matter how many times we've already
                  --     applied to that employer this week (user rule,
                  --     re-confirmed 2026-07-26; before this, the 4th+ apply
                  --     was hard-blocked even at 8+, stranding ~38 Amazon 8s).
                  --   Below 8: 1st application to an employer in a rolling
                  --     week is allowed; 2nd+ is not.
                  --   Aggregator rows with unknown employer (linkedin/indeed/
                  --   google, no company) stay exempt; heal-db backfills
                  --   `company` for the big employers so they can't hide there.
                  -- Per-company weekly throttle (user 2026-09-02): at most 3
                  -- applications per company per 7 days, and score 7+ is exempt
                  -- entirely — "if multiple 7s, then all should be applied
                  -- since our scoring also changed so that 7 is a strong
                  -- enough fit".
                  --
                  -- The previous form exempted only 8+ and, below that, blocked
                  -- a company after a SINGLE application in the window (the
                  -- second NOT IN had no COUNT threshold). That was stricter
                  -- than the documented "3 per company per week" and shrank a
                  -- 41-job queue to 1 after a good cycle: every employer just
                  -- applied to locked out all its other 6s and 7s for a week.
                  AND (COALESCE(company, site) IN ('linkedin', 'indeed', 'google')
                       OR fit_score >= 7
                       OR COALESCE(company, site) NOT IN (
                               SELECT COALESCE(company, site) FROM jobs
                               WHERE apply_status = 'applied'
                                 AND applied_at > datetime('now', '-7 days')
                               GROUP BY COALESCE(company, site)
                               HAVING COUNT(*) >= 3))
                  AND fit_score >= ?
                  {site_clause}
                  {url_clauses}
                -- Freshest-first WITHIN each score band (user 2026-08-13).
                -- Measured over 14 days: a posting that turned out to be
                -- EXPIRED had a median age of 13.2 days, while a successful
                -- application had a median age of 2.3 days — 71% of expired
                -- postings were >7 days old vs 32% of successes. Ordering by
                -- `url` (effectively random) spent the session budget on the
                -- stale end of the queue first. Score still dominates, so this
                -- never demotes a strong match; it only decides which of two
                -- equally-scored jobs to try first, and the fresher one is far
                -- likelier to still be open.
                ORDER BY fit_score DESC, discovered_at DESC
                LIMIT 1
            """, [config.DEFAULTS["max_apply_attempts"]] + params).fetchone()

        if not row:
            # Genuinely empty queue — nothing left to do.
            conn.rollback()
            return None

        # Skip manual ATS sites (unsolvable CAPTCHAs). Mark this one 'manual'
        # and loop to the next candidate rather than returning None, which the
        # worker would misread as an empty queue.
        apply_url = row["application_url"] or row["url"]
        if is_manual_ats(apply_url):
            conn.execute(
                "UPDATE jobs SET apply_status = 'manual', apply_error = 'manual ATS' WHERE url = ?",
                (row["url"],),
            )
            conn.commit()
            logger.info("Skipping manual ATS: %s", row["url"][:80])
            if target_url:
                # Specific-URL mode: only that one job is relevant.
                return None
            continue

        now = datetime.now(timezone.utc).isoformat()
        conn.execute("""
            UPDATE jobs SET apply_status = 'in_progress',
                           agent_id = ?,
                           last_attempted_at = ?
            WHERE url = ?
        """, (f"worker-{worker_id}", now, row["url"]))
        conn.commit()

        return dict(row)
      except Exception:
        conn.rollback()
        raise


def mark_result(url: str, status: str, error: str | None = None,
                duration_ms: int | None = None,
                task_id: str | None = None,
                fit_score: int | None = None) -> None:
    """Update a job's apply status in the database.

    'applied' records success. Any other status is treated as a FAILED run and
    routed through the failure policy (see classify_failure):
      - HARD_DROP reasons          -> 'discarded' (terminal, out of queue)
      - retryable, attempts < cap  -> 'failed'    (acquire_job requeues it)
      - retryable, attempts >= cap -> 'manual' (fit_score >= 8) else 'discarded'

    The per-job failure counter (apply_attempts) is a real count: each failed
    run INCREMENTS it by 1. Nothing is stranded at the old sentinel value 99.
    """
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?
            WHERE url = ?
        """, (now, duration_ms, task_id, url))
        conn.commit()
        return

    # Failure path: read current attempts/score, increment, then classify.
    reason = _normalize_reason(error or status or "unknown")
    row = conn.execute(
        "SELECT apply_attempts, fit_score FROM jobs WHERE url = ?", (url,)
    ).fetchone()
    prev_attempts = row["apply_attempts"] if row and row["apply_attempts"] is not None else 0
    attempts = prev_attempts + 1
    if fit_score is None and row is not None:
        fit_score = row["fit_score"]
    max_attempts = config.DEFAULTS["max_apply_attempts"]
    new_status = classify_failure(reason, fit_score, attempts, max_attempts)

    conn.execute("""
        UPDATE jobs SET apply_status = ?, apply_error = ?,
                       apply_attempts = ?, agent_id = NULL,
                       apply_duration_ms = ?, apply_task_id = ?
        WHERE url = ?
    """, (new_status, reason, attempts, duration_ms, task_id, url))
    conn.commit()


def release_lock(url: str) -> None:
    """Release the in_progress lock without changing status."""
    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET apply_status = NULL, agent_id = NULL WHERE url = ? AND apply_status = 'in_progress'",
        (url,),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Utility modes (--gen, --mark-applied, --mark-failed, --reset-failed)
# ---------------------------------------------------------------------------

def gen_prompt(target_url: str, min_score: int = 6,
               model: str = "sonnet", worker_id: int = 0) -> Path | None:
    """Generate a prompt file and print the Claude CLI command for manual debugging.

    Returns:
        Path to the generated prompt file, or None if no job found.
    """
    job = acquire_job(target_url=target_url, min_score=min_score, worker_id=worker_id)
    if not job:
        return None

    # Read resume text
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    prompt = prompt_mod.build_prompt(job=job, tailored_resume=resume_text,
                                     worker_id=worker_id)

    # Release the lock so the job stays available
    release_lock(job["url"])

    # Write prompt file
    config.ensure_dirs()
    site_slug = (job.get("site") or "unknown")[:20].replace(" ", "_")
    prompt_file = config.LOG_DIR / f"prompt_{site_slug}_{job['title'][:30].replace(' ', '_')}.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    # Write MCP config for reference
    port = BASE_CDP_PORT + worker_id
    mcp_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_path.write_text(json.dumps(_make_mcp_config(port)), encoding="utf-8")

    return prompt_file


def mark_job(url: str, status: str, reason: str | None = None) -> None:
    """Manually mark a job's apply status in the database.

    Args:
        url: Job URL to mark.
        status: Either 'applied' or 'failed'.
        reason: Failure reason (only for status='failed').
    """
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL
            WHERE url = ?
        """, (now, url))
    else:
        # Manual failure mark: increment the real counter and route via the
        # same failure policy as the agent path (no more sentinel 99).
        norm = _normalize_reason(reason or "manual")
        row = conn.execute(
            "SELECT apply_attempts, fit_score FROM jobs WHERE url = ?", (url,)
        ).fetchone()
        prev_attempts = row["apply_attempts"] if row and row["apply_attempts"] is not None else 0
        attempts = prev_attempts + 1
        fit_score = row["fit_score"] if row is not None else None
        new_status = classify_failure(norm, fit_score, attempts,
                                      config.DEFAULTS["max_apply_attempts"])
        conn.execute("""
            UPDATE jobs SET apply_status = ?, apply_error = ?,
                           apply_attempts = ?, agent_id = NULL
            WHERE url = ?
        """, (new_status, norm, attempts, url))
    conn.commit()


def reset_failed() -> int:
    """Reset all failed jobs so they can be retried.

    Returns:
        Number of jobs reset.
    """
    conn = get_connection()
    cursor = conn.execute("""
        UPDATE jobs SET apply_status = NULL, apply_error = NULL,
                       apply_attempts = 0, agent_id = NULL
        WHERE apply_status = 'failed'
          OR (apply_status IS NOT NULL AND apply_status != 'applied'
              AND apply_status != 'in_progress')
    """)
    conn.commit()
    return cursor.rowcount


def reclaim_stale_jobs() -> int:
    """Reclaim jobs stranded in 'in_progress' by a killed/crashed process.

    If the apply process dies mid-application, its rows stay
    apply_status='in_progress' forever: acquire_job excludes them and
    reset_failed skips them, so they can never be retried. On startup we
    reclaim any in_progress row whose last attempt is older than 2x the
    apply timeout (i.e. no live worker could still be holding it) back to a
    fresh, pickable state.

    Returns:
        Number of rows reclaimed.
    """
    conn = get_connection()
    stale_after = config.DEFAULTS["apply_timeout"] * 2
    cursor = conn.execute(f"""
        UPDATE jobs SET apply_status = NULL, agent_id = NULL
        WHERE apply_status = 'in_progress'
          AND (last_attempted_at IS NULL
               OR last_attempted_at < datetime('now', '-{int(stale_after)} seconds'))
    """)
    conn.commit()
    return cursor.rowcount


def _is_lock_error(exc: Exception) -> bool:
    """True if `exc` is a transient SQLite contention error (locked/busy)."""
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


# ---------------------------------------------------------------------------
# Per-job execution
# ---------------------------------------------------------------------------

def run_job(job: dict, port: int, worker_id: int = 0,
            model: str = "sonnet", dry_run: bool = False) -> tuple[str, int]:
    """Spawn a Claude Code session for one job application.

    Returns:
        Tuple of (status_string, duration_ms). Status is one of:
        'applied', 'expired', 'captcha', 'login_issue',
        'failed:reason', or 'skipped'.
    """
    # Read tailored resume text
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    # Build the prompt
    agent_prompt = prompt_mod.build_prompt(
        job=job,
        tailored_resume=resume_text,
        dry_run=dry_run,
        worker_id=worker_id,
    )

    # Write per-worker MCP config
    mcp_config_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_config_path.write_text(json.dumps(_make_mcp_config(port)), encoding="utf-8")

    # Build claude command
    cmd = [
        _which("claude"),
        "--model", model,
        "-p",
        "--mcp-config", str(mcp_config_path),
        "--permission-mode", "bypassPermissions",
        "--no-session-persistence",
        "--disallowedTools", (
            "mcp__gmail__draft_email,mcp__gmail__modify_email,"
            "mcp__gmail__delete_email,mcp__gmail__download_attachment,"
            "mcp__gmail__batch_modify_emails,mcp__gmail__batch_delete_emails,"
            "mcp__gmail__create_label,mcp__gmail__update_label,"
            "mcp__gmail__delete_label,mcp__gmail__get_or_create_label,"
            "mcp__gmail__list_email_labels,mcp__gmail__create_filter,"
            "mcp__gmail__list_filters,mcp__gmail__get_filter,"
            "mcp__gmail__delete_filter"
        ),
        "--output-format", "stream-json",
        "--verbose", "-",
    ]

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    # CRITICAL: never let the agent see an Anthropic API key — the claude CLI
    # prefers ANTHROPIC_API_KEY over the Max-plan login and silently bills the
    # API instead. Apply must always run on the subscription.
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)

    worker_dir = reset_worker_dir(worker_id)

    update_state(worker_id, status="applying", job_title=job["title"],
                 company=job.get("site", ""), score=job.get("fit_score", 0),
                 start_time=time.time(), actions=0, last_action="starting")
    add_event(f"[W{worker_id}] Starting: {job['title'][:40]} @ {job.get('site', '')}")

    worker_log = config.LOG_DIR / f"worker-{worker_id}.log"
    ts_header = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_header = (
        f"\n{'=' * 60}\n"
        f"[{ts_header}] {job['title']} @ {job.get('site', '')}\n"
        f"URL: {job.get('application_url') or job['url']}\n"
        f"Score: {job.get('fit_score', 'N/A')}/10\n"
        f"{'=' * 60}\n"
    )

    start = time.time()
    stats: dict = {}
    proc = None
    watchdog: threading.Timer | None = None
    timed_out = threading.Event()
    apply_timeout = config.DEFAULTS["apply_timeout"]

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(worker_dir),
        )
        with _claude_lock:
            _claude_procs[worker_id] = proc

        # Wall-clock watchdog: a hung agent can keep the stdout stream open
        # (streaming nothing useful) far past the timeout, so reading stdout to
        # EOF is not a real bound. Kill the whole process tree after
        # apply_timeout seconds regardless of stdout state; the read loop then
        # sees EOF and we report failed:timeout below.
        def _on_timeout(pid: int) -> None:
            timed_out.set()
            _kill_process_tree(pid)

        watchdog = threading.Timer(apply_timeout, _on_timeout, args=(proc.pid,))
        watchdog.daemon = True
        watchdog.start()

        proc.stdin.write(agent_prompt)
        proc.stdin.close()

        text_parts: list[str] = []
        with open(worker_log, "a", encoding="utf-8") as lf:
            lf.write(log_header)

            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    msg_type = msg.get("type")
                    if msg_type == "assistant":
                        for block in msg.get("message", {}).get("content", []):
                            bt = block.get("type")
                            if bt == "text":
                                text_parts.append(block["text"])
                                lf.write(block["text"] + "\n")
                            elif bt == "tool_use":
                                name = (
                                    block.get("name", "")
                                    .replace("mcp__playwright__", "")
                                    .replace("mcp__gmail__", "gmail:")
                                )
                                inp = block.get("input", {})
                                if "url" in inp:
                                    desc = f"{name} {inp['url'][:60]}"
                                elif "ref" in inp:
                                    desc = f"{name} {inp.get('element', inp.get('text', ''))}"[:50]
                                elif "fields" in inp:
                                    desc = f"{name} ({len(inp['fields'])} fields)"
                                elif "paths" in inp:
                                    desc = f"{name} upload"
                                else:
                                    desc = name

                                lf.write(f"  >> {desc}\n")
                                ws = get_state(worker_id)
                                cur_actions = ws.actions if ws else 0
                                update_state(worker_id,
                                             actions=cur_actions + 1,
                                             last_action=desc[:35])
                    elif msg_type == "result":
                        stats = {
                            "input_tokens": msg.get("usage", {}).get("input_tokens", 0),
                            "output_tokens": msg.get("usage", {}).get("output_tokens", 0),
                            "cache_read": msg.get("usage", {}).get("cache_read_input_tokens", 0),
                            "cache_create": msg.get("usage", {}).get("cache_creation_input_tokens", 0),
                            "cost_usd": msg.get("total_cost_usd", 0),
                            "turns": msg.get("num_turns", 0),
                        }
                        text_parts.append(msg.get("result", ""))
                except json.JSONDecodeError:
                    text_parts.append(line)
                    lf.write(line + "\n")

        proc.wait(timeout=apply_timeout + 30)
        returncode = proc.returncode
        proc = None

        # Watchdog fired: the agent blew the wall-clock budget and was killed.
        if timed_out.is_set():
            duration_ms = int((time.time() - start) * 1000)
            elapsed = int(time.time() - start)
            add_event(f"[W{worker_id}] TIMEOUT ({elapsed}s)")
            update_state(worker_id, status="failed", last_action=f"TIMEOUT ({elapsed}s)")
            return "failed:timeout", duration_ms

        if returncode and returncode < 0:
            # Negative return code = killed by signal. If it wasn't our
            # watchdog, treat it as a user skip (Ctrl+C).
            return "skipped", int((time.time() - start) * 1000)

        output = "\n".join(text_parts)
        elapsed = int(time.time() - start)
        duration_ms = int((time.time() - start) * 1000)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        job_log = config.LOG_DIR / f"claude_{ts}_w{worker_id}_{job.get('site', 'unknown')[:20]}.txt"
        job_log.write_text(output, encoding="utf-8")

        if stats:
            cost = stats.get("cost_usd", 0)
            ws = get_state(worker_id)
            prev_cost = ws.total_cost if ws else 0.0
            update_state(worker_id, total_cost=prev_cost + cost)

        def _clean_reason(s: str) -> str:
            return re.sub(r'[*`"]+$', '', s).strip()

        low = output.lower()

        # Dead CLI login ("Failed to authenticate: OAuth session expired and
        # could not be refreshed"). Not a job failure, and unlike a rate limit
        # it will NOT self-heal — every subsequent spawn dies identically, and
        # on 2026-08-02 that ground through 23 jobs at $0 while burning their
        # attempt counters. Signal the worker loop to release the job and STOP.
        if "failed to authenticate" in low:
            add_event(f"[W{worker_id}] AUTH DEAD: claude CLI OAuth expired — stopping worker")
            update_state(worker_id, status="auth_failed",
                         last_action="claude CLI OAuth expired; run /login")
            return "auth_dead", duration_ms

        # Claude CLI session/usage limit: not a job failure. Signal the worker
        # loop to release the job and pause until the limit resets.
        if "session limit" in low or "usage limit" in low or "rate limit" in low:
            first_line = output.strip().splitlines()[0][:80] if output.strip() else "limit hit"
            add_event(f"[W{worker_id}] RATE LIMITED: {first_line}")
            update_state(worker_id, status="rate_limited", last_action="session limit hit")
            return f"rate_limited:{first_line}", duration_ms

        # APPLIED is only trusted with a verbatim confirmation quote. Agents
        # (especially small models) sometimes declare victory mid-form; the
        # quote requirement makes that mechanically impossible to count.
        if "RESULT:APPLIED" in output:
            m = re.search(r'RESULT:APPLIED\s+CONFIRMATION="([^"\n]{5,300})"', output)
            if m:
                quote = m.group(1)[:120]
                add_event(f"[W{worker_id}] APPLIED ({elapsed}s): {job['title'][:30]}")
                update_state(worker_id, status="applied",
                             last_action=f"APPLIED: {quote[:30]}")
                logger.info("Confirmation for %s: %s", job["title"][:40], quote)
                return "applied", duration_ms
            add_event(f"[W{worker_id}] REJECTED unproven APPLIED claim ({elapsed}s)")
            update_state(worker_id, status="failed",
                         last_action="applied claim w/o confirmation")
            return "failed:unconfirmed", duration_ms

        for result_status in ["EXPIRED", "CAPTCHA", "LOGIN_ISSUE"]:
            if f"RESULT:{result_status}" in output:
                add_event(f"[W{worker_id}] {result_status} ({elapsed}s): {job['title'][:30]}")
                update_state(worker_id, status=result_status.lower(),
                             last_action=f"{result_status} ({elapsed}s)")
                return result_status.lower(), duration_ms

        if "RESULT:FAILED" in output:
            for out_line in output.split("\n"):
                if "RESULT:FAILED" in out_line:
                    reason = (
                        out_line.split("RESULT:FAILED:")[-1].strip()
                        if ":" in out_line[out_line.index("FAILED") + 6:]
                        else "unknown"
                    )
                    reason = _clean_reason(reason)
                    PROMOTE_TO_STATUS = {"captcha", "expired", "login_issue"}
                    if reason in PROMOTE_TO_STATUS:
                        add_event(f"[W{worker_id}] {reason.upper()} ({elapsed}s): {job['title'][:30]}")
                        update_state(worker_id, status=reason,
                                     last_action=f"{reason.upper()} ({elapsed}s)")
                        return reason, duration_ms
                    add_event(f"[W{worker_id}] FAILED ({elapsed}s): {reason[:30]}")
                    update_state(worker_id, status="failed",
                                 last_action=f"FAILED: {reason[:25]}")
                    return f"failed:{reason}", duration_ms
            return "failed:unknown", duration_ms

        add_event(f"[W{worker_id}] NO RESULT ({elapsed}s)")
        update_state(worker_id, status="failed", last_action=f"no result ({elapsed}s)")
        return "failed:no_result_line", duration_ms

    except subprocess.TimeoutExpired:
        duration_ms = int((time.time() - start) * 1000)
        elapsed = int(time.time() - start)
        add_event(f"[W{worker_id}] TIMEOUT ({elapsed}s)")
        update_state(worker_id, status="failed", last_action=f"TIMEOUT ({elapsed}s)")
        return "failed:timeout", duration_ms
    except Exception as e:
        duration_ms = int((time.time() - start) * 1000)
        add_event(f"[W{worker_id}] ERROR: {str(e)[:40]}")
        update_state(worker_id, status="failed", last_action=f"ERROR: {str(e)[:25]}")
        return f"failed:{str(e)[:100]}", duration_ms
    finally:
        if watchdog is not None:
            watchdog.cancel()
        with _claude_lock:
            _claude_procs.pop(worker_id, None)
        if proc is not None and proc.poll() is None:
            _kill_process_tree(proc.pid)


# ---------------------------------------------------------------------------
# Failure classification / requeue policy
# ---------------------------------------------------------------------------

# HARD_DROP: retrying or hand-applying is pointless. On ANY of these the job is
# discarded immediately (terminal, out of queue) regardless of score/attempts.
HARD_DROP_REASONS: set[str] = {
    "already_applied",
    "not_a_job_application",
    "not_eligible_location",
    "not_eligible_work_auth",
    "unsafe_verification",
    "expired",
}

# Everything else (unconfirmed, timeout, login_issue, sso_required, captcha,
# page_error, stuck, no_result_line, manual_ats, network, unknown/generic, ...)
# is RETRYABLE: increment apply_attempts and requeue as 'failed' until the cap.


def _normalize_reason(result_or_reason: str) -> str:
    """Extract the bare failure reason from a 'failed:reason' string, a
    'status:detail' string, or a bare reason. Strips whitespace."""
    s = result_or_reason or "unknown"
    reason = s.split(":", 1)[-1] if ":" in s else s
    return reason.strip() or "unknown"


def classify_failure(reason: str, fit_score, attempts: int,
                     max_attempts: int) -> str:
    """Decide the terminal apply_status for a failed run.

    Args:
        reason: Normalized failure reason (see _normalize_reason).
        fit_score: Job's fit score (used only for score-gated routing).
        attempts: apply_attempts value AFTER incrementing for this failed run.
        max_attempts: Retry cap (config.DEFAULTS['max_apply_attempts']).

    Returns:
        'discarded' -> HARD_DROP, or retryable-but-exhausted with score < 8.
        'manual'    -> retryable-but-exhausted with fit_score >= 8.
        'failed'    -> retryable and still under the cap (acquire_job requeues).
    """
    if reason in HARD_DROP_REASONS:
        return "discarded"
    # Retryable. "Failed in multiple runs" == attempts >= max_attempts.
    if attempts >= max_attempts:
        try:
            score = int(fit_score) if fit_score is not None else 0
        except (ValueError, TypeError):
            score = 0
        return "manual" if score >= 8 else "discarded"
    return "failed"


def _seconds_until_limit_reset(message: str, default: int = 900,
                               max_wait: int = 6 * 3600) -> int:
    """Parse 'resets 5:40pm' from a Claude CLI limit message.

    Returns seconds to wait (+2 min buffer). Falls back to `default` if the
    message has no parseable time; never waits longer than `max_wait`.
    """
    m = re.search(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)", message, re.IGNORECASE)
    if not m:
        return default
    hour = int(m.group(1)) % 12
    if m.group(3).lower() == "pm":
        hour += 12
    minute = int(m.group(2) or 0)
    now = datetime.now()
    reset = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if reset <= now:
        reset += timedelta(days=1)
    return min(max_wait, int((reset - now).total_seconds()) + 120)


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

def worker_loop(worker_id: int = 0, limit: int = 1,
                target_url: str | None = None,
                min_score: int = 6, headless: bool = False,
                model: str = "sonnet", dry_run: bool = False) -> tuple[int, int]:
    """Run jobs sequentially until limit is reached or queue is empty.

    Args:
        worker_id: Numeric worker identifier.
        limit: Max jobs to process (0 = continuous).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome headless.
        model: Claude model name.
        dry_run: Don't click Submit.

    Returns:
        Tuple of (applied_count, failed_count).
    """
    applied = 0
    failed = 0
    attempts = 0            # total attempts — for logging only, NOT the limit
    continuous = limit == 0
    empty_polls = 0
    port = BASE_CDP_PORT + worker_id
    # Chrome is launched ONCE per worker and reused across jobs. Cold-launching
    # for every job wastes minutes (fixed sleeps + netstat sweeps) per run.
    chrome_proc = None

    try:
        while not _stop_event.is_set():
            # The --limit budget counts SUCCESSFUL applies only. Counting every
            # attempt made "13 applied + 83 failed" stop at limit=96 with real
            # work left; failures must not consume the budget. The run still
            # terminates when the queue drains (acquire_job returns None).
            if not continuous and applied >= limit:
                break

            update_state(worker_id, status="idle", job_title="", company="",
                         last_action="waiting for job", actions=0)

            # Acquire the next job. acquire_job runs OUTSIDE the per-job
            # try/except, so a transient "database is locked" under multi-worker
            # contention would otherwise kill the worker permanently. Retry with
            # backoff and keep the worker alive on genuine transient errors.
            job = None
            acquired = False
            backoff = 0.5
            for _ in range(5):
                try:
                    job = acquire_job(target_url=target_url, min_score=min_score,
                                      worker_id=worker_id)
                    acquired = True
                    break
                except sqlite3.OperationalError as e:
                    if not _is_lock_error(e):
                        raise
                    logger.warning("W%d acquire_job DB busy: %s", worker_id, e)
                    if _stop_event.wait(timeout=backoff):
                        break
                    backoff = min(backoff * 2, 8.0)
            if _stop_event.is_set():
                break
            if not acquired:
                # Contention outlasted our retries — don't die, just loop and
                # try to acquire again on the next pass.
                add_event(f"[W{worker_id}] DB busy, retrying acquire...")
                continue

            if not job:
                if not continuous:
                    add_event(f"[W{worker_id}] Queue empty")
                    update_state(worker_id, status="done", last_action="queue empty")
                    break
                empty_polls += 1
                update_state(worker_id, status="idle",
                             last_action=f"polling ({empty_polls})")
                if empty_polls == 1:
                    add_event(f"[W{worker_id}] Queue empty, polling every {POLL_INTERVAL}s...")
                # Use Event.wait for interruptible sleep
                if _stop_event.wait(timeout=POLL_INTERVAL):
                    break  # Stop was requested during wait
                continue

            empty_polls = 0

            try:
                # Reuse the worker's Chrome across jobs. Only (re)launch on the
                # first job or when the existing instance crashed/disconnected.
                # Fallback: if the health check or reuse path fails, fall back to
                # a cold relaunch so apply never hard-breaks.
                if chrome_proc is None or not chrome.is_chrome_healthy(chrome_proc, port):
                    if chrome_proc is not None:
                        add_event(f"[W{worker_id}] Chrome unhealthy — relaunching...")
                        cleanup_worker(worker_id, chrome_proc, port=port)
                        chrome_proc = None
                    add_event(f"[W{worker_id}] Launching Chrome...")
                    chrome_proc = launch_chrome(worker_id, port=port, headless=headless)
                else:
                    # Reuse: clear stale tabs to about:blank between jobs
                    # (best-effort; the agent navigates to the job URL anyway).
                    try:
                        chrome.reset_chrome_tabs(port)
                    except Exception:
                        logger.debug("reset_chrome_tabs failed", exc_info=True)

                result, duration_ms = run_job(job, port=port, worker_id=worker_id,
                                                model=model, dry_run=dry_run)

                if result == "skipped":
                    release_lock(job["url"])
                    add_event(f"[W{worker_id}] Skipped: {job['title'][:30]}")
                    continue
                elif result == "applied":
                    if dry_run:
                        # Dry run: nothing was actually submitted. Do NOT record
                        # applied_at, or the job gets locked out of a real apply.
                        release_lock(job["url"])
                        add_event(f"[W{worker_id}] DRY RUN ok — reviewed, NOT submitted: {job['title'][:30]}")
                    else:
                        mark_result(job["url"], "applied", duration_ms=duration_ms)
                    applied += 1
                    update_state(worker_id, jobs_applied=applied,
                                 jobs_done=applied + failed)
                elif result == "auth_dead":
                    # CLI login is gone; nothing this worker does can fix it.
                    # Release the job untouched (no attempt burned) and stop.
                    release_lock(job["url"])
                    add_event(f"[W{worker_id}] Stopping: claude CLI needs an "
                              f"interactive /login before applies can run")
                    break
                elif result.startswith("rate_limited"):
                    # Not the job's fault: release it and pause until the limit
                    # resets instead of burning through the whole queue.
                    release_lock(job["url"])
                    wait_s = _seconds_until_limit_reset(result)
                    add_event(
                        f"[W{worker_id}] Session limit — pausing {wait_s // 60} min until reset"
                    )
                    update_state(worker_id, status="rate_limited",
                                 last_action=f"paused {wait_s // 60}m (limit reset)")
                    if _stop_event.wait(timeout=wait_s):
                        break
                    continue
                else:
                    reason = result.split(":", 1)[-1] if ":" in result else result
                    mark_result(job["url"], "failed", reason,
                                duration_ms=duration_ms,
                                fit_score=job.get("fit_score"))
                    failed += 1
                    update_state(worker_id, jobs_failed=failed,
                                 jobs_done=applied + failed)

            except KeyboardInterrupt:
                release_lock(job["url"])
                if _stop_event.is_set():
                    break
                add_event(f"[W{worker_id}] Job skipped (Ctrl+C)")
                continue
            except Exception as e:
                logger.exception("Worker %d launcher error", worker_id)
                add_event(f"[W{worker_id}] Launcher error: {str(e)[:40]}")
                release_lock(job["url"])
                failed += 1
                update_state(worker_id, jobs_failed=failed)
                # A launcher-level error may have left Chrome wedged; drop it so
                # the next job triggers a clean relaunch.
                if chrome_proc is not None:
                    cleanup_worker(worker_id, chrome_proc, port=port)
                    chrome_proc = None

            attempts += 1
            if target_url:
                break
    finally:
        # Tear Chrome down once, when the worker is finished.
        if chrome_proc is not None:
            cleanup_worker(worker_id, chrome_proc, port=port)

    update_state(worker_id, status="done",
                 last_action=f"finished ({attempts} attempts)")
    return applied, failed


# ---------------------------------------------------------------------------
# Main entry point (called from cli.py)
# ---------------------------------------------------------------------------

def main(limit: int = 1, target_url: str | None = None,
         min_score: int = 6, headless: bool = False, model: str = "sonnet",
         dry_run: bool = False, continuous: bool = False,
         poll_interval: int = 60, workers: int = 1) -> None:
    """Launch the apply pipeline.

    Args:
        limit: Max jobs to apply to (0 or with continuous=True means run forever).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome in headless mode.
        model: Claude model name.
        dry_run: Don't click Submit.
        continuous: Run forever, polling for new jobs.
        poll_interval: Seconds between DB polls when queue is empty.
        workers: Number of parallel workers (default 1).
    """
    global POLL_INTERVAL
    POLL_INTERVAL = poll_interval
    _stop_event.clear()

    config.ensure_dirs()
    console = Console()

    # Reclaim jobs stranded 'in_progress' by a previous crash/kill so they can
    # be retried instead of being locked out of the queue forever.
    try:
        reclaimed = reclaim_stale_jobs()
        if reclaimed:
            console.print(f"[dim]Reclaimed {reclaimed} stranded in_progress job(s)[/dim]")
    except Exception:
        logger.exception("reclaim_stale_jobs failed")

    if continuous:
        effective_limit = 0
        mode_label = "continuous"
    else:
        effective_limit = limit
        mode_label = f"{limit} jobs"

    # Initialize dashboard for all workers
    for i in range(workers):
        init_worker(i)

    worker_label = f"{workers} worker{'s' if workers > 1 else ''}"
    console.print(f"Launching apply pipeline ({mode_label}, {worker_label}, poll every {POLL_INTERVAL}s)...")
    console.print("[dim]Ctrl+C = skip current job(s) | Ctrl+C x2 = stop[/dim]")

    # Double Ctrl+C handler
    _ctrl_c_count = 0

    def _sigint_handler(sig, frame):
        nonlocal _ctrl_c_count
        _ctrl_c_count += 1
        if _ctrl_c_count == 1:
            console.print("\n[yellow]Skipping current job(s)... (Ctrl+C again to STOP)[/yellow]")
            # Kill all active Claude processes to skip current jobs
            with _claude_lock:
                for wid, cproc in list(_claude_procs.items()):
                    if cproc.poll() is None:
                        _kill_process_tree(cproc.pid)
        else:
            console.print("\n[red bold]STOPPING[/red bold]")
            _stop_event.set()
            with _claude_lock:
                for wid, cproc in list(_claude_procs.items()):
                    if cproc.poll() is None:
                        _kill_process_tree(cproc.pid)
            kill_all_chrome()
            raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _sigint_handler)

    try:
        with Live(render_full(), console=console, refresh_per_second=2) as live:
            # Daemon thread for display refresh only (no business logic)
            _dashboard_running = True

            def _refresh():
                while _dashboard_running:
                    live.update(render_full())
                    time.sleep(0.5)

            refresh_thread = threading.Thread(target=_refresh, daemon=True)
            refresh_thread.start()

            if workers == 1:
                # Single worker — run directly in main thread
                total_applied, total_failed = worker_loop(
                    worker_id=0,
                    limit=effective_limit,
                    target_url=target_url,
                    min_score=min_score,
                    headless=headless,
                    model=model,
                    dry_run=dry_run,
                )
            else:
                # Multi-worker — distribute limit across workers
                if effective_limit:
                    base = effective_limit // workers
                    extra = effective_limit % workers
                    limits = [base + (1 if i < extra else 0)
                              for i in range(workers)]
                else:
                    limits = [0] * workers  # continuous mode

                with ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix="apply-worker") as executor:
                    futures = {
                        executor.submit(
                            worker_loop,
                            worker_id=i,
                            limit=limits[i],
                            target_url=target_url,
                            min_score=min_score,
                            headless=headless,
                            model=model,
                            dry_run=dry_run,
                        ): i
                        for i in range(workers)
                    }

                    results: list[tuple[int, int]] = []
                    for future in as_completed(futures):
                        wid = futures[future]
                        try:
                            results.append(future.result())
                        except Exception:
                            logger.exception("Worker %d crashed", wid)
                            results.append((0, 0))

                total_applied = sum(r[0] for r in results)
                total_failed = sum(r[1] for r in results)

            _dashboard_running = False
            refresh_thread.join(timeout=2)
            live.update(render_full())

        totals = get_totals()
        console.print(
            f"\n[bold]Done: {total_applied} applied, {total_failed} failed "
            f"(${totals['cost']:.3f})[/bold]"
        )
        console.print(f"Logs: {config.LOG_DIR}")

    except KeyboardInterrupt:
        pass
    finally:
        _stop_event.set()
        kill_all_chrome()
