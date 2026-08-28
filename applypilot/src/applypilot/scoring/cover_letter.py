"""Cover letter generation: LLM-powered, profile-driven, with validation.

Generates concise, engineering-voice cover letters tailored to specific job
postings. All personal data (name, skills, achievements) comes from the user's
profile at runtime. No hardcoded personal information.
"""

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone

from applypilot import config
from applypilot.config import COVER_LETTER_DIR, RESUME_PATH, load_profile
from applypilot.database import get_connection, get_jobs_by_stage
from applypilot.llm import get_client

# Aggregator/board names that must NEVER appear as the employer in a letter.
_JOB_BOARDS = {"linkedin", "indeed", "google", "remoteok", "adzuna",
               "themuse", "hn-whoishiring", "usajobs", "glassdoor",
               "ziprecruiter"}


def _real_company(job: dict) -> str:
    """Resolve the actual employer name for prompts.

    Prefers the company column; falls back to site unless site is a job
    board, in which case the LLM is told to extract the employer from the
    description (it is almost always named there).
    """
    company = (job.get("company") or "").strip()
    if company:
        return company
    site = (job.get("site") or "").strip()
    if site.lower() in _JOB_BOARDS:
        return ("NOT PROVIDED — this posting came from a job board. Identify "
                "the real employer's name from the DESCRIPTION and use that. "
                "If the employer is genuinely unnamed, write the letter "
                "without a company name; NEVER address the job board itself.")
    return site or "Unknown"
from applypilot.scoring.validator import (
    BANNED_WORDS,
    LLM_LEAK_PHRASES,
    sanitize_text,
    validate_cover_letter,
)

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5  # max cross-run retries before giving up


# ── Prompt Builder (profile-driven) ──────────────────────────────────────

def _build_cover_letter_prompt(profile: dict) -> str:
    """Build the cover letter system prompt from the user's profile.

    All personal data, skills, and sign-off name come from the profile.
    """
    personal = profile.get("personal", {})
    boundary = profile.get("skills_boundary", {})
    resume_facts = profile.get("resume_facts", {})

    # Preferred name for the sign-off (falls back to full name)
    sign_off_name = personal.get("preferred_name") or personal.get("full_name", "")

    # Flatten all allowed skills
    all_skills: list[str] = []
    for items in boundary.values():
        if isinstance(items, list):
            all_skills.extend(items)
    skills_str = ", ".join(all_skills) if all_skills else "the tools listed in the resume"

    # Real metrics from resume_facts
    real_metrics = resume_facts.get("real_metrics", [])
    preserved_projects = resume_facts.get("preserved_projects", [])

    # Build achievement examples for the prompt
    projects_hint = ""
    if preserved_projects:
        projects_hint = f"\nKnown projects to reference: {', '.join(preserved_projects)}"

    metrics_hint = ""
    if real_metrics:
        metrics_hint = f"\nReal metrics to use: {', '.join(real_metrics)}"

    # Full legal name for the signature line
    full_name = personal.get("full_name", sign_off_name)

    # Truthful experience + education facts (profile-driven)
    _exp = profile.get("experience", {})
    _m = re.search(r"\d+", str(_exp.get("years_of_experience_total", "2")))
    cand_years = int(_m.group()) if _m else 2
    edu_level = _exp.get("education_level") or "a Bachelor's degree"

    # Today's date for the letter head (e.g. "July 9, 2026")
    _now = datetime.now()
    date_str = f"{_now.strftime('%B')} {_now.day}, {_now.year}"

    # One-clause introduction built from the profile's education entry
    edu = (profile.get("education") or [{}])[0]
    fos = edu.get("field_of_study")
    field = fos[0] if isinstance(fos, list) and fos else (fos or "")
    edu_line = (
        f"a {edu.get('degree') or 'B.S.'} graduate in {field} from "
        f"{edu.get('school', 'their university')}"
    ) if edu else "a recent graduate"

    # Build the full banned list from the validator so the prompt stays in sync
    # with what will actually be rejected — the validator checks all of these.
    all_banned = ", ".join(f'"{w}"' for w in BANNED_WORDS)
    leak_banned = ", ".join(f'"{p}"' for p in LLM_LEAK_PHRASES)

    return f"""Write a cover letter for {sign_off_name}. It is a one-page BUSINESS LETTER and a writing sample — hiring teams read it as evidence of communication skill. It is also a marketing tool: the goal is an interview.

METHOD (do this thinking before writing):
1. From the job description, find the 2 MOST IMPORTANT requirements — the first bullets under "what you'll do" carry the most weight.
2. For each of those 2 requirements, find the candidate's single best matching experience from the resume. Use the posting's EXACT words for skills and tools (if they say "QuickBooks", never write "accounting software").
3. Find one company-specific hook IN the posting itself: their product, mission line, a technical challenge, or team description.

FORMAT — standard business letter, in this exact order:
{date_str}

Dear Hiring Manager:
(If the posting names a specific recruiter or manager, address them instead. Always end the salutation with a colon.)

PARAGRAPH 1 — OPENING (2-3 sentences): State that the candidate is writing to apply for the EXACT job title at the company, and where the posting appeared (see POSTED ON in the job info). Introduce the candidate in one clause: {edu_line}. Then give 2-3 concrete reasons this background fits, drawn from the requirements found in METHOD step 1.

PARAGRAPH 2 — WHY THIS EMPLOYER + PROOF #1 (3-4 sentences): Open with the company-specific hook from METHOD step 3 and why it connects to the candidate's interests. Then the best matching experience for requirement #1: a real mini-story with a number, in the posting's exact words.{projects_hint}{metrics_hint}

PARAGRAPH 3 — PROOF #2 (3-4 sentences): The candidate's best evidence for requirement #2. Different story, different skill — do not repeat paragraph 2's evidence. Action verbs, at least one real metric. End by connecting these skills to what the role needs.

PARAGRAPH 4 — CLOSING (2 sentences): Thank the reader for considering the application and reiterate enthusiasm for contributing to the organization's work. End by stating the candidate looks forward to the opportunity to discuss the position further.

Sincerely,

{full_name}

HARD RULES:
- Under 300 words. Concise and factual. No flowery language.
- Do not overuse "I": never start more than two consecutive sentences with "I".
- Every claim is backed by an example, number, tool name, or outcome.
- TRUTH: the candidate has about {cand_years} year(s) of professional experience and holds: {edu_level}. NEVER claim more years of experience, additional degrees, or titles that are not in the resume. Fabricated experience fails validation and can get a real interview rescinded.

BANNED WORDS AND PHRASES (automated validator rejects ANY of these — do not use even once):
{all_banned}

ALSO BANNED (meta-commentary the validator catches):
{leak_banned}

BANNED PUNCTUATION: No em dashes (—) or en dashes (–). Use commas or periods.

VOICE:
- Write like a real engineer emailing someone they respect. Not formal, not casual. Just direct.
- NEVER narrate or explain what you're doing. BAD: "This demonstrates my commitment to X." GOOD: Just state the fact and move on.
- NEVER hedge. BAD: "might address some of your challenges." GOOD: "solves the same problem your team is facing."
- Every sentence should contain either a number, a tool name, or a specific outcome. If it doesn't, cut it.
- VARY sentence length dramatically. Uniform 15-25-word sentences read as machine output. Put at least one short, punchy sentence (under 8 words) somewhere in the letter.
- Lead with the point. No rhetorical-question openers ("What if..."), no thanks-plus-recap openers.
- Plain verbs: is, has, built, led — never "serves as", "boasts". One modifier per noun, never stacked pairs ("high-quality, well-architected").
- State facts, not their significance. BAD: "This was a pivotal achievement." GOOD: the number and what changed.
- Read it out loud. If it sounds like a robot wrote it, rewrite it.

FABRICATION = INSTANT REJECTION:
The candidate's real tools are ONLY: {skills_str}.
Do NOT mention ANY tool not in this list. If the job asks for tools not listed, talk about the work you did, not the tools.

Sign off: just "{sign_off_name}"

Output ONLY the letter text. No subject lines. No "Here is the cover letter:" preamble. No notes after the sign-off.
Start DIRECTLY with "Dear Hiring Manager," and end with the name."""


# ── Helpers ──────────────────────────────────────────────────────────────

def _strip_preamble(text: str) -> str:
    """Remove LLM preamble before 'Dear Hiring Manager,' if present.

    Gemini and other models sometimes output "Here is the cover letter:" or
    similar meta-commentary before the actual letter text. Strip everything
    before the first occurrence of "Dear" so the validator's start-check passes.
    """
    dear_idx = text.lower().find("dear")
    if dear_idx > 0:
        return text[dear_idx:]
    return text


# ── Core Generation ──────────────────────────────────────────────────────

def generate_cover_letter(
    resume_text: str, job: dict, profile: dict,
    max_retries: int = 3, validation_mode: str = "normal",
) -> str:
    """Generate a cover letter with fresh context on each retry + auto-sanitize.

    Same design as tailor_resume: fresh conversation per attempt, issues noted
    in the prompt, no conversation history stacking.

    Args:
        resume_text:      The candidate's resume text (base or tailored).
        job:              Job dict with title, site, location, full_description.
        profile:          User profile dict.
        max_retries:      Maximum retry attempts.
        validation_mode:  "strict", "normal", or "lenient".

    Returns:
        The cover letter text (best attempt even if validation failed).
    """
    company = _real_company(job)
    _site = (job.get("site") or "").lower()
    posted_on = {"linkedin": "LinkedIn", "indeed": "Indeed", "google": "Google Jobs",
                 "remoteok": "RemoteOK", "themuse": "The Muse", "adzuna": "Adzuna",
                 "usajobs": "USAJOBS", "hn-whoishiring": "Hacker News"}.get(
                     _site, "the company's careers site")
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {company}\n"
        f"POSTED ON: {posted_on}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    avoid_notes: list[str] = []
    letter = ""
    client = get_client()
    cl_prompt_base = _build_cover_letter_prompt(profile)

    for attempt in range(max_retries + 1):
        # Fresh conversation every attempt
        prompt = cl_prompt_base
        if avoid_notes:
            prompt += "\n\n## AVOID THESE ISSUES:\n" + "\n".join(
                f"- {n}" for n in avoid_notes[-5:]
            )

        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": (
                f"RESUME:\n{resume_text}\n\n---\n\n"
                f"TARGET JOB:\n{job_text}\n\n"
                "Write the cover letter:"
            )},
        ]

        letter = client.chat(messages, max_tokens=4096, temperature=0.7)
        letter = sanitize_text(letter)  # auto-fix em dashes, smart quotes
        letter = _strip_preamble(letter)  # remove any "Here is the letter:" prefix

        validation = validate_cover_letter(letter, mode=validation_mode,
                                           original_text=resume_text)
        if validation["passed"]:
            return letter

        avoid_notes.extend(validation["errors"])
        # Warnings never block — only hard errors trigger a retry
        log.debug(
            "Cover letter attempt %d/%d failed: %s",
            attempt + 1, max_retries + 1, validation["errors"],
        )

    return letter  # last attempt even if failed


# ── Batch Entry Point ────────────────────────────────────────────────────

def run_cover_letters(min_score: int = 6, limit: int = 20,
                      validation_mode: str = "normal") -> dict:
    """Generate cover letters for high-scoring jobs that have tailored resumes.

    Args:
        min_score:       Minimum fit_score threshold.
        limit:           Maximum jobs to process.
        validation_mode: "strict", "normal", or "lenient".

    Returns:
        {"generated": int, "errors": int, "elapsed": float}
    """
    profile = load_profile()
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    conn = get_connection()

    # Fetch jobs that have tailored resumes but no cover letter yet
    jobs = conn.execute(
        "SELECT * FROM jobs "
        "WHERE fit_score >= ? AND tailored_resume_path IS NOT NULL "
        "AND full_description IS NOT NULL "
        "AND (cover_letter_path IS NULL OR cover_letter_path = '') "
        "AND COALESCE(cover_attempts, 0) < ? "
        "AND COALESCE(apply_status, '') != 'deferred' "
        "ORDER BY fit_score DESC LIMIT ?",
        (min_score, MAX_ATTEMPTS, limit),
    ).fetchall()

    if not jobs:
        log.info("No jobs needing cover letters (score >= %d).", min_score)
        return {"generated": 0, "errors": 0, "elapsed": 0.0}

    # Convert rows to dicts
    if jobs and not isinstance(jobs[0], dict):
        columns = jobs[0].keys()
        jobs = [dict(zip(columns, row)) for row in jobs]

    COVER_LETTER_DIR.mkdir(parents=True, exist_ok=True)
    log.info(
        "Generating cover letters for %d jobs (score >= %d)...",
        len(jobs), min_score,
    )
    t0 = time.time()
    completed = 0
    results: list[dict] = []
    error_count = 0

    for job in jobs:
        completed += 1
        try:
            letter = generate_cover_letter(resume_text, job, profile,
                                          validation_mode=validation_mode)

            # Deterministic date line: models routinely skip the {date_str}
            # the prompt asks for, so enforce it in code instead.
            _n = datetime.now()
            _date = f"{_n.strftime('%B')} {_n.day}, {_n.year}"
            if not re.search(
                r"(january|february|march|april|may|june|july|august|"
                r"september|october|november|december)\s+\d{1,2},\s*\d{4}",
                letter[:300], re.IGNORECASE,
            ):
                letter = f"{_date}\n\n{letter.lstrip()}"

            # Build safe filename prefix (URL hash keeps same-title jobs from
            # overwriting each other's files)
            safe_title = re.sub(r"[^\w\s-]", "", job["title"])[:50].strip().replace(" ", "_")
            safe_site = re.sub(r"[^\w\s-]", "", job["site"])[:20].strip().replace(" ", "_")
            url_tag = hashlib.md5(job["url"].encode()).hexdigest()[:8]
            prefix = f"{safe_site}_{safe_title}_{url_tag}"

            cl_path = COVER_LETTER_DIR / f"{prefix}_CL.txt"
            cl_path.write_text(letter, encoding="utf-8")

            # Generate PDF (best-effort)
            pdf_path = None
            try:
                from applypilot.scoring.pdf import convert_to_pdf
                pdf_path = str(convert_to_pdf(cl_path))
            except Exception:
                log.debug("PDF generation failed for %s", cl_path, exc_info=True)

            result = {
                "url": job["url"],
                "path": config.to_data_relative(cl_path),  # portable: relative to APP_DIR
                "pdf_path": pdf_path,
                "title": job["title"],
                "site": job["site"],
            }
            results.append(result)

            elapsed = time.time() - t0
            rate = completed / elapsed if elapsed > 0 else 0
            log.info(
                "%d/%d [OK] | %.1f jobs/min | %s",
                completed, len(jobs), rate * 60, result["title"][:40],
            )
        except Exception as e:
            result = {
                "url": job["url"], "title": job["title"], "site": job["site"],
                "path": None, "pdf_path": None, "error": str(e),
            }
            error_count += 1
            results.append(result)
            log.error("%d/%d [ERROR] %s -- %s", completed, len(jobs), job["title"][:40], e)

    # Persist to DB: increment attempt counter for ALL, save path only for successes
    now = datetime.now(timezone.utc).isoformat()
    saved = 0
    for r in results:
        if r.get("path"):
            conn.execute(
                "UPDATE jobs SET cover_letter_path=?, cover_letter_at=?, "
                "cover_attempts=COALESCE(cover_attempts,0)+1 WHERE url=?",
                (r["path"], now, r["url"]),
            )
            saved += 1
        else:
            conn.execute(
                "UPDATE jobs SET cover_attempts=COALESCE(cover_attempts,0)+1 WHERE url=?",
                (r["url"],),
            )
    conn.commit()

    elapsed = time.time() - t0
    log.info("Cover letters done in %.1fs: %d generated, %d errors", elapsed, saved, error_count)

    return {
        "generated": saved,
        "errors": error_count,
        "elapsed": elapsed,
    }
