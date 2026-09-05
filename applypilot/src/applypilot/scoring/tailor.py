"""Resume tailoring: LLM-powered ATS-optimized resume generation per job.

THIS IS THE HEAVIEST REFACTOR. Every piece of personal data -- name, email, phone,
skills, companies, projects, school -- is loaded at runtime from the user's profile.
Zero hardcoded personal information.

The LLM returns structured JSON, code assembles the final text. Header (name, contact)
is always code-injected, never LLM-generated. Each retry starts a fresh conversation
to avoid apologetic spirals.
"""

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from applypilot import config
from applypilot.config import RESUME_PATH, TAILORED_DIR, load_profile
from applypilot.database import get_connection, get_jobs_by_stage
from applypilot.llm import get_client
from applypilot.scoring.validator import (
    BANNED_WORDS,
    FABRICATION_WATCHLIST,
    sanitize_text,
    validate_json_fields,
    validate_tailored_resume,
)

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5  # max cross-run retries before giving up


# ── Prompt Builders (profile-driven) ──────────────────────────────────────

def _build_tailor_prompt(profile: dict) -> str:
    """Build the resume tailoring system prompt from the user's profile.

    All skills boundaries, preserved entities, and formatting rules are
    derived from the profile -- nothing is hardcoded.
    """
    boundary = profile.get("skills_boundary", {})
    resume_facts = profile.get("resume_facts", {})

    # Truthful experience figure (profile-driven, works for any user)
    _m = re.search(r"\d+", str(profile.get("experience", {}).get(
        "years_of_experience_total", "2")))
    cand_years = int(_m.group()) if _m else 2

    # Format skills boundary for the prompt
    skills_lines = []
    for category, items in boundary.items():
        if isinstance(items, list) and items:
            label = category.replace("_", " ").title()
            skills_lines.append(f"{label}: {', '.join(items)}")
    skills_block = "\n".join(skills_lines)

    # Preserved entities
    companies = resume_facts.get("preserved_companies", [])
    projects = resume_facts.get("preserved_projects", [])
    school = resume_facts.get("preserved_school", "")
    real_metrics = resume_facts.get("real_metrics", [])

    companies_str = ", ".join(companies) if companies else "N/A"
    projects_str = ", ".join(projects) if projects else "N/A"
    metrics_str = ", ".join(real_metrics) if real_metrics else "N/A"

    # User-authored truthfulness constraints (e.g. which work belongs to whom).
    honesty_notes = resume_facts.get("honesty_notes", [])
    honesty_block = ""
    if honesty_notes:
        honesty_block = "\n\n## HONESTY NOTES (facts about this candidate — never contradict these):\n" + \
            "\n".join(f"- {n}" for n in honesty_notes)

    # Include ALL banned words from the validator so the LLM knows exactly
    # what will be rejected — the validator checks for these automatically.
    banned_str = ", ".join(BANNED_WORDS)

    education = profile.get("experience", {})
    education_level = education.get("education_level", "")
    # `experience.education_level` is what he HOLDS (Bachelor's) and it also
    # feeds scorer._candidate_facts()/has_grad_degree, which decides whether
    # postings hard-requiring a completed graduate degree are reachable — so it
    # must NOT be rewritten to advertise an in-progress degree. When
    # resume_facts supplies a dedicated resume line (UPenn M.S.E. in AI,
    # starting Dec 2026), prefer it for the RESUME only, and carry the finished
    # B.S. through as a detail line so the completed degree is never dropped.
    # (user 2026-08-28)
    education_level = resume_facts.get("resume_degree_line") or education_level
    prior_education_line = resume_facts.get("prior_education_line", "")

    # Certifications are injected by code in assemble_resume_text (their own
    # education bullet), never here in the LLM output — keeps them out of the
    # skills dict (fabrication watchlist scans "certif") and off the degree
    # line, and stops the LLM dropping or mangling them (user, 2026-07-20).

    return f"""You are a senior technical recruiter rewriting a resume to get this person an interview.

Take the base resume and job description. Return a tailored resume as a JSON object.

## RECRUITER SCAN (6 seconds):
1. Title -- matches what they're hiring?
2. First 3 bullets of most recent role -- verbs and outcomes match?
3. Skills -- must-haves visible immediately?

## SKILLS BOUNDARY (real skills only):
{skills_block}

You MAY add 2-3 closely related tools (Kubernetes if Docker, Terraform if AWS, Redis if PostgreSQL). No unrelated languages/frameworks.

## TAILORING RULES:

KEYWORD MIRRORING (this is what beats the ATS): List mentally the exact skills, tools, and phrases the posting repeats — those are what the screening software weighs. Mirror the posting's EXACT words back (if they say "data visualization", write "data visualization", not "dashboarding"). Only mirror terms the candidate can honestly claim.

TITLE: Match the target role's title exactly. Drop company suffixes and team names.

TRUTH RULE (experience): the candidate has about {cand_years} year(s) of professional experience — NEVER write a larger number ("{cand_years + 1}+ years", "5+ years") or any inflated experience claim, no matter what the job asks for. "{cand_years} years of internship and associate experience" is the defensible phrasing. Overclaiming fails validation and gets caught in interviews.

SKILLS: Pick the 10-15 skills the posting actually asks for, using the posting's exact words. Organize into 3-5 category rows NAMED for this role (e.g. "Languages", "Analytics & ML", "Visualization & BI", "Tools & Libraries") — merge sparse categories so no row has fewer than 3 items, and put the job's must-haves first in each row.

EXPERIENCE: Include EVERY role from the base resume held at the preserved companies ({companies_str}), each as its OWN entry — the same company can appear twice (an internship and a later associate role are SEPARATE entries; keep both). Never drop one of these roles; trim its bullet count instead. Other base-resume experience (e.g. service jobs) only if directly relevant to the posting. Reframe EVERY bullet for this role. Same real work, different angle. Every bullet must be reworded. Never copy verbatim.

PROJECTS: Pick the 2-3 projects MOST relevant to the posting, most relevant first. The base resume's project list is a menu — choose the ones whose tools and domain mirror the job.

EDUCATION: "line" = school + degree + graduation date ONLY (from the base resume; do NOT put certificates on this line — they are added separately by the system). "details" = 2-3 short lines pulled ONLY from the base resume's education section: one combining GPA + honors (e.g. Dean's List), and one "Relevant coursework:" line listing the 5-8 base-resume courses most relevant to THIS posting. Include the spoken-languages line only when the posting values it. Do NOT add a certificates line yourself.
PRIOR DEGREE: when a prior-degree line is given below, it MUST be reproduced VERBATIM as the FIRST entry of "details". It is a completed degree and may never be dropped, reworded, merged into the "line", or presented as in progress. The graduate program on the "line" is IN PROGRESS — never describe it as completed, conferred, earned, or awarded, and never claim experience derived from it.

BULLETS (X-Y-Z formula): what was accomplished + the number that proves it + how. "Cut onboarding time 30% by redesigning the training plan" beats "responsible for onboarding". Lead with the result. Where there is no real number, show scope instead (how many people, rows, systems). Never write "as measured by". Vary verbs (Built, Designed, Implemented, Reduced, Automated, Deployed, Operated, Optimized). Order bullets strongest-match-first. Max 4 per section.

FILL THE PAGE (one page exactly — overflowing shrinks the PDF font, but a half-empty page wastes the shot):
- Experience: 4-5 bullets for the most relevant role, 2-3 each for the others. Projects: 2-3, with 2-3 bullets each. Every bullet ONE sentence, under 160 characters.
- Aim for a FULL single page: if the resume runs short, add the next most relevant project or bullet instead of leaving white space.

## VOICE:
- Write like a real engineer. Short, direct. One idea per bullet, active voice.
- GOOD: "Automated financial reporting with Python + API integrations, cut processing time from 10 hours to 2"
- BAD: "Leveraged cutting-edge AI technologies to drive transformative operational efficiencies"
- No word gets used more than twice across the document. Watch pet verbs (surface, drive, own, craft, ensure, enhance, optimize) — vary with: identify, highlight, show, build, fix, cut, run.
- Don't hedge exact numbers: "roughly/about/~" only when a number is genuinely an estimate, and never twice in a row. If the math is exact (60% vs 10% baseline = 6x), state it plainly.
- No triadic filler ("fast, reliable, and scalable"), no empty intensifiers (very, highly, deeply), no "not just X, but Y" constructions.
- Plain verbs: is, has, built, led — never "serves as", "boasts", "features". One modifier per noun, never stacked pairs ("high-quality, well-architected").
- Vary sentence length: uniform 15-25-word sentences read as machine output. Some bullets short and punchy.
- One hedge maximum per claim, never stacked ("could potentially" is two hedges).
- BANNED WORDS (using ANY of these = validation failure — do not use them even once):
  {banned_str}
- No em dashes. Use commas, periods, or hyphens.

## HARD RULES:
- Do NOT invent work, companies, degrees, or certifications
- Do NOT invent personal attributes: no language fluency ("Native Spanish speaker"), nationality, citizenship, work authorization, security clearance, or people-leadership claims ("led a team of 4") unless they are in the original resume — even if the posting asks for them
- Do NOT change real numbers ({metrics_str})
- Every metric stays under the company/role where it actually happened (the [brackets] above). NEVER move a real number to a different employer's bullets.
- Preserved companies: {companies_str} -- names stay as-is
- Preserved school: {school}
- Prior degree line (reproduce VERBATIM as the first "details" entry; omit only if blank): {prior_education_line}
- Must fit 1 page.{honesty_block}

## OUTPUT: Return ONLY valid JSON. No markdown fences. No commentary. No "here is" preamble.

{{"title":"Role Title","skills":{{"Languages":"...","Frameworks":"...","DevOps & Infra":"...","Databases":"...","Tools":"..."}},"experience":[{{"header":"Title at Company","subtitle":"Tech | Dates","bullets":["bullet 1","bullet 2","bullet 3","bullet 4"]}}],"projects":[{{"header":"Project Name - Description","subtitle":"Tech | Dates","bullets":["bullet 1","bullet 2"]}}],"education":{{"line":"{school} | {education_level} | Graduation date","details":["GPA + honors from the base resume","Relevant coursework: 5-8 courses from the base resume most relevant to this job"]}}}}"""


def _build_judge_prompt(profile: dict) -> str:
    """Build the LLM judge prompt from the user's profile."""
    boundary = profile.get("skills_boundary", {})
    resume_facts = profile.get("resume_facts", {})
    full_name = profile.get("personal", {}).get("full_name", "")

    # Flatten allowed skills for the judge
    all_skills: list[str] = []
    for items in boundary.values():
        if isinstance(items, list):
            all_skills.extend(items)
    skills_str = ", ".join(all_skills) if all_skills else "N/A"

    real_metrics = resume_facts.get("real_metrics", [])
    metrics_str = ", ".join(real_metrics) if real_metrics else "N/A"

    honesty_notes = resume_facts.get("honesty_notes", [])
    honesty_block = ""
    if honesty_notes:
        honesty_block = "\n\n## HONESTY NOTES (user-stated facts — a tailored resume contradicting any of these is a FAIL):\n" + \
            "\n".join(f"- {n}" for n in honesty_notes)

    return f"""You are a resume quality judge. A tailoring engine rewrote a resume to target a specific job. Your job is to catch LIES, not style changes.

You must answer with EXACTLY this format:
VERDICT: PASS or FAIL
ISSUES: (list any problems, or "none")

## CONTEXT -- what the tailoring engine was instructed to do (all of this is ALLOWED):
- Change the title to match the target role
- Reorder bullets and projects to put the most relevant first
- Reframe bullets to use the job's language
- Drop low-relevance bullets and replace with more relevant ones from other sections
- Reorder the skills section to put job-relevant skills first
- Change tone and wording extensively

## WHAT IS FABRICATION (FAIL for these):
1. Adding tools, languages, or frameworks to TECHNICAL SKILLS that aren't in the original. The allowed skills are ONLY: {skills_str}
2. Inventing NEW metrics or numbers not in the original. The real metrics are: {metrics_str}
3. Inventing work that has no basis in any original bullet (completely new achievements).
4. Adding companies, roles, or degrees that don't exist.
5. Changing real numbers (inflating 80% to 95%, 500 nodes to 1000 nodes).
6. Inventing PERSONAL ATTRIBUTES not in the original resume: language fluency ("Native Spanish speaker", "bilingual"), nationality or citizenship, work authorization, security clearance, or people-leadership claims ("led a team of 4 analysts", "managed 3 engineers"). These are identity claims about the candidate, not skills — a single one is an automatic FAIL, never a minor stretch.
7. Inventing an ACTIVITY the original resume never describes and hanging a real metric on it (e.g. the original credits a review-time cut to dashboards, the tailored resume claims it came from "mentoring junior analysts" or "delivering training"). Mentoring, training, or teaching colleagues counts as invented activity unless the original resume describes it — documentation-writing or presenting is NOT mentoring.
8. TRANSPLANTING a real metric or achievement to a different company or role than where it happened in the original resume (e.g. a review-time metric from one employer appearing under another employer's bullets, or work from a class project claimed as job experience). The number being real does not excuse the wrong attribution — check WHERE each number lives in the original. This is a FAIL, not a minor stretch.

## WHAT IS NOT FABRICATION (do NOT fail for these):
- Rewording any bullet, even heavily, as long as the underlying work is real
- Combining two original bullets into one
- Splitting one original bullet into two
- Describing the same work with different emphasis
- Dropping bullets entirely
- Reordering anything
- Changing the title completely
- Dropping or omitting a summary section entirely
- Expanding the education section with GPA, honors, or coursework that appear in the original resume
- The header name: it is code-injected from the user's profile ("{full_name}"). If it differs from the base resume's header that is a preferred-vs-legal name difference for the SAME person — NEVER fail for the name.

## TOLERANCE RULE:
The goal is to get interviews, not to be a perfect fact-checker. Allow up to 3 minor stretches per resume:
- Adding a closely related tool the candidate could realistically know is a MINOR STRETCH, not fabrication.
- Reframing a metric with slightly different wording is a MINOR STRETCH.
- Adding any LEARNABLE skill given their existing stack is a MINOR STRETCH.
- Only FAIL if there are MAJOR lies: completely invented projects, fake companies, fake degrees, wildly inflated numbers, or skills from a completely different domain.

Be strict about major lies. Be lenient about minor stretches and learnable skills. Do not fail for style, tone, or restructuring.{honesty_block}"""


# ── JSON Extraction ───────────────────────────────────────────────────────

def extract_json(raw: str) -> dict:
    """Robustly extract JSON from LLM response (handles fences, preamble).

    Args:
        raw: Raw LLM response text.

    Returns:
        Parsed JSON dict.

    Raises:
        ValueError: If no valid JSON found.
    """
    raw = raw.strip()

    # Direct parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Markdown fences
    if "```" in raw:
        for part in raw.split("```")[1::2]:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            try:
                return json.loads(part)
            except json.JSONDecodeError:
                continue

    # Find outermost { ... }
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            pass

    raise ValueError("No valid JSON found in LLM response")


# ── Resume Assembly (profile-driven header) ──────────────────────────────

def assemble_resume_text(data: dict, profile: dict) -> str:
    """Convert JSON resume data to formatted plain text.

    Header (name, location, contact) is ALWAYS code-injected from the profile,
    never LLM-generated. All text fields are sanitized.

    Args:
        data: Parsed JSON resume from the LLM.
        profile: User profile dict from load_profile().

    Returns:
        Formatted resume text.
    """
    personal = profile.get("personal", {})
    lines: list[str] = []

    # Header -- always code-injected from profile
    lines.append(personal.get("full_name", ""))
    lines.append(sanitize_text(data.get("title", "Software Engineer")))

    # Location from search config or profile -- leave blank if not available
    # The location line is optional; the original used a hardcoded city.
    # We omit it here; the LLM prompt can include it if the user sets it.

    # Contact line
    contact_parts: list[str] = []
    if personal.get("email"):
        contact_parts.append(personal["email"])
    if personal.get("phone"):
        contact_parts.append(personal["phone"])
    if personal.get("github_url"):
        contact_parts.append(personal["github_url"])
    if personal.get("linkedin_url"):
        contact_parts.append(personal["linkedin_url"])
    if contact_parts:
        lines.append(" | ".join(contact_parts))
    lines.append("")

    # Section order: Education, Skills, Experience, Projects (no Summary —
    # user decision 2026-07-20).
    lines.append("EDUCATION")
    edu = data.get("education", "")
    edu_details: list[str] = []
    if isinstance(edu, dict):
        lines.append(sanitize_text(str(edu.get("line", ""))))
        edu_details = [str(d) for d in edu.get("details", [])]
    elif isinstance(edu, list):
        items = [str(x) for x in edu] or [""]
        lines.append(sanitize_text(items[0]))
        edu_details = items[1:]
    else:
        lines.append(sanitize_text(str(edu)))
    # Drop any certs line the LLM slipped in; we add the authoritative one.
    edu_details = [d for d in edu_details if not d.lower().lstrip("- ").startswith("certificat")]
    for d in edu_details:
        lines.append(f"- {sanitize_text(d)}")
    # Certificates as their own bullet, injected from the profile (never on the
    # degree line, never in the skills dict) — user decision 2026-07-20.
    certs = profile.get("resume_facts", {}).get("certifications", [])
    if certs:
        lines.append(f"- Certificates: {sanitize_text(', '.join(certs))}")
    lines.append("")

    # Technical Skills
    lines.append("TECHNICAL SKILLS")
    if isinstance(data["skills"], dict):
        for cat, val in data["skills"].items():
            lines.append(f"{cat}: {sanitize_text(str(val))}")
    lines.append("")

    # Experience
    lines.append("EXPERIENCE")
    for entry in data.get("experience", []):
        lines.append(sanitize_text(entry.get("header", "")))
        if entry.get("subtitle"):
            lines.append(sanitize_text(entry["subtitle"]))
        for b in entry.get("bullets", []):
            lines.append(f"- {sanitize_text(b)}")
        lines.append("")

    # Projects
    lines.append("PROJECTS")
    for entry in data.get("projects", []):
        lines.append(sanitize_text(entry.get("header", "")))
        if entry.get("subtitle"):
            lines.append(sanitize_text(entry["subtitle"]))
        for b in entry.get("bullets", []):
            lines.append(f"- {sanitize_text(b)}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ── LLM Judge ────────────────────────────────────────────────────────────

def judge_tailored_resume(
    original_text: str, tailored_text: str, job_title: str, profile: dict
) -> dict:
    """LLM judge layer: catches subtle fabrication that programmatic checks miss.

    Args:
        original_text: Base resume text.
        tailored_text: Tailored resume text.
        job_title: Target job title.
        profile: User profile for building the judge prompt.

    Returns:
        {"passed": bool, "verdict": str, "issues": str, "raw": str}
    """
    judge_prompt = _build_judge_prompt(profile)

    messages = [
        {"role": "system", "content": judge_prompt},
        {"role": "user", "content": (
            f"JOB TITLE: {job_title}\n\n"
            f"ORIGINAL RESUME:\n{original_text}\n\n---\n\n"
            f"TAILORED RESUME:\n{tailored_text}\n\n"
            "Judge this tailored resume:"
        )},
    ]

    client = get_client()
    response = client.chat(messages, max_tokens=4096, temperature=0.1)

    # Reasoning models decorate the verdict ("**VERDICT:** PASS") — strip
    # markdown and match loosely; an unparseable verdict still fails safe.
    clean = response.replace("*", "").upper()
    m = re.search(r"VERDICT\s*:?\s*(PASS|FAIL)", clean)
    passed = bool(m and m.group(1) == "PASS")
    issues = "none"
    if "ISSUES:" in clean:
        issues_idx = clean.index("ISSUES:")
        issues = response.replace("*", "")[issues_idx + 7:].strip()

    return {
        "passed": passed,
        "verdict": "PASS" if passed else "FAIL",
        "issues": issues,
        "raw": response,
    }


# ── Core Tailoring ───────────────────────────────────────────────────────

def tailor_resume(
    resume_text: str, job: dict, profile: dict,
    max_retries: int = 3, validation_mode: str = "normal",
) -> tuple[str, dict]:
    """Generate a tailored resume via JSON output + fresh context on each retry.

    Key design choices:
    - LLM returns structured JSON, code assembles the text (no header leaks)
    - Each retry starts a FRESH conversation (no apologetic spiral)
    - Issues from previous attempts are noted in the system prompt
    - Em dashes and smart quotes are auto-fixed, not rejected

    Args:
        resume_text:      Base resume text.
        job:              Job dict with title, site, location, full_description.
        profile:          User profile dict.
        max_retries:      Maximum retry attempts.
        validation_mode:  "strict", "normal", or "lenient".
                          strict  -- banned words trigger retries
                          normal  -- banned words = warnings only
                          lenient -- banned words ignored
                          The LLM judge and all fabrication checks (years,
                          personal attributes) run in EVERY mode; a final
                          judge FAIL never ships.

    Returns:
        (tailored_text, report) where report contains validation details.
    """
    from applypilot.scoring.cover_letter import _real_company
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {_real_company(job)}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    report: dict = {
        "attempts": 0, "validator": None, "judge": None,
        "status": "pending", "validation_mode": validation_mode,
    }
    avoid_notes: list[str] = []
    tailored = ""
    client = get_client()
    tailor_prompt_base = _build_tailor_prompt(profile)

    for attempt in range(max_retries + 1):
        report["attempts"] = attempt + 1

        # Fresh conversation every attempt
        prompt = tailor_prompt_base
        if avoid_notes:
            prompt += "\n\n## AVOID THESE ISSUES (from previous attempt):\n" + "\n".join(
                f"- {n}" for n in avoid_notes[-5:]
            )

        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"ORIGINAL RESUME:\n{resume_text}\n\n---\n\nTARGET JOB:\n{job_text}\n\nReturn the JSON:"},
        ]

        raw = client.chat(messages, max_tokens=8192, temperature=0.4)

        # Parse JSON from response
        try:
            data = extract_json(raw)
        except ValueError:
            avoid_notes.append("Output was not valid JSON. Return ONLY a JSON object, nothing else.")
            continue

        # Layer 1: Validate JSON fields
        validation = validate_json_fields(data, profile, mode=validation_mode,
                                          original_text=resume_text)
        report["validator"] = validation

        if not validation["passed"]:
            # Only retry if there are hard errors (warnings never block)
            avoid_notes.extend(validation["errors"])
            if attempt < max_retries:
                continue
            # Last attempt — assemble whatever we got
            tailored = assemble_resume_text(data, profile)
            report["status"] = "failed_validation"
            return tailored, report

        # Assemble text (header injected by code, em dashes auto-fixed)
        tailored = assemble_resume_text(data, profile)

        # Layer 2: LLM judge (catches subtle fabrication) — runs in EVERY
        # mode. Fabrication is a truth problem, not a style preference, so no
        # mode may skip it (the lenient skip here once shipped a "Native
        # Spanish speaker" resume with zero errors). A judge FAIL that
        # survives all retries must NOT ship — "approved_with_judge_warning"
        # used to count as a success downstream, which meant the judge could
        # never actually block anything.
        judge = judge_tailored_resume(resume_text, tailored, job.get("title", ""), profile)
        report["judge"] = judge

        if not judge["passed"]:
            avoid_notes.append(f"Judge rejected: {judge['issues']}")
            if attempt < max_retries:
                continue
            report["status"] = "failed_judge"
            return tailored, report

        # Both passed
        report["status"] = "approved"
        return tailored, report

    report["status"] = "exhausted_retries"
    return tailored, report


# ── Batch Entry Point ────────────────────────────────────────────────────

def run_tailoring(min_score: int = 6, limit: int = 20,
                  validation_mode: str = "normal") -> dict:
    """Generate tailored resumes for high-scoring jobs.

    Args:
        min_score:       Minimum fit_score to tailor for.
        limit:           Maximum jobs to process.
        validation_mode: "strict", "normal", or "lenient".

    Returns:
        {"approved": int, "failed": int, "errors": int, "elapsed": float}
    """
    profile = load_profile()
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    conn = get_connection()

    jobs = get_jobs_by_stage(conn=conn, stage="pending_tailor", min_score=min_score, limit=limit)

    if not jobs:
        log.info("No untailored jobs with score >= %d.", min_score)
        return {"approved": 0, "failed": 0, "errors": 0, "elapsed": 0.0}

    TAILORED_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Tailoring resumes for %d jobs (score >= %d)...", len(jobs), min_score)
    t0 = time.time()
    completed = 0
    results: list[dict] = []
    stats: dict[str, int] = {"approved": 0, "failed_validation": 0, "failed_judge": 0, "error": 0}

    for job in jobs:
        completed += 1
        try:
            tailored, report = tailor_resume(resume_text, job, profile,
                                             validation_mode=validation_mode)

            # Build safe filename prefix (URL hash keeps same-title jobs from
            # overwriting each other's files)
            safe_title = re.sub(r"[^\w\s-]", "", job["title"])[:50].strip().replace(" ", "_")
            safe_site = re.sub(r"[^\w\s-]", "", job["site"])[:20].strip().replace(" ", "_")
            url_tag = hashlib.md5(job["url"].encode()).hexdigest()[:8]
            prefix = f"{safe_site}_{safe_title}_{url_tag}"

            # Save tailored resume text
            txt_path = TAILORED_DIR / f"{prefix}.txt"
            txt_path.write_text(tailored, encoding="utf-8")

            # Save job description for traceability
            job_path = TAILORED_DIR / f"{prefix}_JOB.txt"
            job_desc = (
                f"Title: {job['title']}\n"
                f"Company: {job['site']}\n"
                f"Location: {job.get('location', 'N/A')}\n"
                f"Score: {job.get('fit_score', 'N/A')}\n"
                f"URL: {job['url']}\n\n"
                f"{job.get('full_description', '')}"
            )
            job_path.write_text(job_desc, encoding="utf-8")

            # Save validation report
            report_path = TAILORED_DIR / f"{prefix}_REPORT.json"
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

            # Generate PDF for approved resumes (best-effort)
            pdf_path = None
            if report["status"] == "approved":
                try:
                    from applypilot.scoring.pdf import convert_to_pdf
                    pdf_path = str(convert_to_pdf(txt_path))
                except Exception:
                    log.debug("PDF generation failed for %s", txt_path, exc_info=True)

            result = {
                "url": job["url"],
                "path": config.to_data_relative(txt_path),  # portable: relative to APP_DIR
                "pdf_path": pdf_path,
                "title": job["title"],
                "site": job["site"],
                "status": report["status"],
                "attempts": report["attempts"],
            }
        except Exception as e:
            result = {
                "url": job["url"], "title": job["title"], "site": job["site"],
                "status": "error", "attempts": 0, "path": None, "pdf_path": None,
            }
            log.error("%d/%d [ERROR] %s -- %s", completed, len(jobs), job["title"][:40], e)

        results.append(result)
        stats[result.get("status", "error")] = stats.get(result.get("status", "error"), 0) + 1

        elapsed = time.time() - t0
        rate = completed / elapsed if elapsed > 0 else 0
        log.info(
            "%d/%d [%s] attempts=%s | %.1f jobs/min | %s",
            completed, len(jobs),
            result["status"].upper(),
            result.get("attempts", "?"),
            rate * 60,
            result["title"][:40],
        )

    # Persist to DB: increment attempt counter for ALL, save path only for approved
    now = datetime.now(timezone.utc).isoformat()
    for r in results:
        if r["status"] == "approved":
            conn.execute(
                "UPDATE jobs SET tailored_resume_path=?, tailored_at=?, "
                "tailor_attempts=COALESCE(tailor_attempts,0)+1 WHERE url=?",
                (r["path"], now, r["url"]),
            )
        elif r["status"] == "error":
            # Provider outage / infra failure — the LLM never produced a
            # draft, so this was not a real attempt. Counting these burned
            # whole queues to the attempt cap during the 2026-07-16 outages.
            pass
        else:
            conn.execute(
                "UPDATE jobs SET tailor_attempts=COALESCE(tailor_attempts,0)+1 WHERE url=?",
                (r["url"],),
            )
    conn.commit()

    elapsed = time.time() - t0
    log.info(
        "Tailoring done in %.1fs: %d approved, %d failed_validation, %d failed_judge, %d errors",
        elapsed,
        stats.get("approved", 0),
        stats.get("failed_validation", 0),
        stats.get("failed_judge", 0),
        stats.get("error", 0),
    )

    return {
        "approved": stats.get("approved", 0),
        "failed": stats.get("failed_validation", 0) + stats.get("failed_judge", 0),
        "errors": stats.get("error", 0),
        "elapsed": elapsed,
    }
