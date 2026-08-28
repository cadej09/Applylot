"""Resume and cover letter validation: banned words, fabrication detection, structural checks.

All validation is profile-driven -- no hardcoded personal data. The validator receives
a profile dict (from applypilot.config.load_profile()) and validates against the user's
actual skills, companies, projects, and school.

Validation modes
----------------
strict  -- banned words = hard errors that trigger retries (original behavior)
normal  -- banned words = warnings only; fabrication/structure = errors (default)
lenient -- banned words ignored; only fabrication and required structure checked
"""

import re
import logging

log = logging.getLogger(__name__)


# ── Universal Constants (not personal data) ───────────────────────────────

_years_cache: int | None = None


def _candidate_years() -> int:
    """Years of experience from the user's profile (cached, default 2)."""
    global _years_cache
    if _years_cache is None:
        try:
            from applypilot.config import load_profile
            m = re.search(r"\d+", str((load_profile() or {}).get(
                "experience", {}).get("years_of_experience_total", "2")))
            _years_cache = int(m.group()) if m else 2
        except Exception:
            _years_cache = 2
    return _years_cache


BANNED_WORDS: list[str] = [
    "passionate", "dedicated", "committed to",
    "utilizing", "utilize", "harnessing",
    "spearheaded", "spearhead", "orchestrated", "championed", "pioneered",
    "robust", "scalable solutions", "cutting-edge", "state-of-the-art", "best-in-class",
    "proven track record", "track record of success", "demonstrated ability",
    "strong communicator", "team player", "fast learner", "self-starter", "go-getter",
    "synergy", "cross-functional collaboration", "holistic",
    "transformative", "innovative solutions", "paradigm", "ecosystem",
    "proactive", "detail-oriented", "highly motivated",
    "seamless", "full lifecycle",
    "deep understanding", "extensive experience", "comprehensive knowledge",
    "thrives in", "excels at", "adept at", "well-versed in",
    "i am confident", "i believe", "i am excited",
    "plays a critical role", "instrumental in", "integral part of",
    "strong track record", "eager to", "eager",
    # Cover-letter-specific additions
    "this demonstrates", "this reflects", "i have experience with",
    "furthermore", "additionally", "moreover",
    # Humanizer pass: documented AI tells not already covered
    "leverage", "leveraged", "leveraging", "delve", "delving",
    "testament to", "in today's", "fast-paced environment", "fast-paced",
    "results-driven", "meticulous", "meticulously",
    "foster", "fostering", "empower", "empowering", "elevate", "elevating",
    "excited to apply", "aligns with my", "resonates with",
    "dynamic environment", "ever-evolving", "evolving landscape",
    # Humanizer v2 (Wikipedia "Signs of AI writing" via blader/humanizer):
    "serves as", "boasts", "stands as", "functions as a",
    "it's not just", "not just about", "at its core",
    "in order to", "due to the fact",
    "plays a vital role", "vital role", "crucial role",
    "underscoring", "showcasing", "nestled", "pivotal",
    "rapidly evolving", "groundbreaking", "reshaping",
    "catalyst", "journey toward", "exciting times", "let's dive",
    # From the user's "Job Search" project style rules (2026-07-16):
    "streamline", "streamlined", "streamlining",
    "productionize", "productionizing", "intuitive",
    "showcase", "showcased", "crucial", "comprehensive",
    "innovative", "dynamic", "it's important to note",
    # From conorbronsdon/avoid-ai-writing + the Job Search Playbook (2026-07-19):
    "ascertain", "commence", "vibrant", "thriving", "watershed",
    "could potentially", "may eventually", "only time will tell",
    "the future looks bright", "feel free to reach out",
    "i hope this helps", "let me know if", "let's explore",
    "great question", "thrilled", "i am honored", "what surprised me",
]

LLM_LEAK_PHRASES: list[str] = [
    "i am sorry", "i apologize", "i will try", "let me try",
    "i am at a loss", "i am truly sorry", "apologies for",
    "i keep fabricating", "i will have to admit", "one final attempt",
    "one last time", "if it fails again", "persistent errors",
    "i am having difficulty", "i made an error", "my mistake",
    "here is the corrected", "here is the revised", "here is the updated",
    "here is my", "below is the", "as requested",
    "note:", "disclaimer:", "important:",
    "i have rewritten", "i have removed", "i have fixed",
    "i have replaced", "i have updated", "i have corrected",
    "per your feedback", "based on your feedback", "as per the instructions",
    "the following resume", "the resume below",
    "the following cover letter", "the letter below",
]

# Known fabrication markers: completely unrelated tools/languages.
# Reasonable stretches (K8s, Terraform, Redis, Kafka etc.) are ALLOWED.
FABRICATION_WATCHLIST: set[str] = {
    # Languages with zero relation to the candidate's stack
    "c#", "c++", "golang", "rust", "ruby",
    "kotlin", "swift", "scala", "matlab",
    # Frameworks for wrong languages
    "spring", "django", "rails", "angular", "vue", "svelte",
    # Hard lies: certifications can't be stretched
    "certif", "certified", "pmp", "scrum master", "aws certified",
}

REQUIRED_SECTIONS: set[str] = {"TECHNICAL SKILLS", "EXPERIENCE", "PROJECTS", "EDUCATION"}

# Invented personal attributes: identity claims an LLM cannot verify and must
# never introduce — language fluency, citizenship/work authorization, security
# clearance, people leadership. A real one ("Native Spanish speaker", "guiding
# a team of 4 analysts") shipped through lenient mode on 2026-07-16. These are
# errors in EVERY mode, same as the years overclaim; a claim is allowed only
# when the same pattern also matches the ORIGINAL resume (real attributes stay).
_HUMAN_LANGS = (
    r"(?:spanish|french|german|italian|portuguese|mandarin|cantonese|chinese"
    r"|japanese|korean|hindi|urdu|arabic|russian|vietnamese|tagalog|polish"
    r"|dutch|turkish|hebrew|thai|swedish|norwegian|danish|greek|czech"
    r"|romanian|hungarian|ukrainian|farsi|persian|punjabi|bengali|tamil"
    r"|telugu|indonesian|malay|swahili)"
)

# Language claims capture WHICH language so a real one (Korean) never
# whitelists an invented one (Spanish).
_LANG_CLAIM_PATTERNS: list[str] = [
    rf"\b(?:native|fluent|bilingual|conversational|proficien\w*)\b[^.\n]{{0,40}}\b({_HUMAN_LANGS})\b",
    rf"\b({_HUMAN_LANGS})\b[^.\n]{{0,20}}\b(?:speaker|speaking|fluency|proficiency|native)\b",
]

# Team-size claims capture the NUMBER so a real "team of 3" never whitelists
# an inflated "team of 4 analysts".
_TEAM_SIZE_PATTERN = (
    r"\b(?:team|group)\s+of\s+(\d+)\b"
    r"|\b(?:managed|supervised|mentored|led|guided|guiding)\s+(?:a\s+team\s+of\s+)?(\d+)\s+"
    r"(?:analysts|engineers|developers|interns|employees|people|reports|members)\b"
)

# Leadership phrasing without a number — allowed only if the original resume
# makes ANY leadership-verb-over-a-team claim.
_LEADERSHIP_PATTERN = (
    r"\b(?:led|leads|leading|managed|manages|managing|supervised|supervising"
    r"|directed|directing|oversaw|overseeing|headed|heading|guided|guiding)\b"
    r"[^.\n]{0,50}\bteams?\b"
)

# Yes/no attributes: present in the tailored text but not the original = invented.
PERSONAL_ATTRIBUTE_PATTERNS: list[tuple[str, str]] = [
    (r"\b(?:u\.?s\.?|american)\s+citizen(?:ship)?\b", "citizenship"),
    (r"\bgreen\s?card\b", "work authorization"),
    (r"\b(?:authorized|eligible)\s+to\s+work\b", "work authorization"),
    (r"\b(?:security\s+clearance|ts/sci|top\s+secret|secret\s+clearance)\b",
     "security clearance"),
    (r"\bdirect\s+reports\b", "team leadership"),
]


def _claimed_languages(text: str) -> set[str]:
    langs: set[str] = set()
    for pat in _LANG_CLAIM_PATTERNS:
        langs.update(m.group(1) for m in re.finditer(pat, text))
    return langs


def _claimed_team_sizes(text: str) -> set[str]:
    return {g for m in re.finditer(_TEAM_SIZE_PATTERN, text)
            for g in m.groups() if g}


def find_personal_attribute_claims(text: str, original_text: str = "") -> list[str]:
    """Find invented personal-attribute claims in `text`.

    A claim is reported only when the ORIGINAL resume does not already make
    it — real attributes (an actual second language, actual leadership) are
    never flagged, and specifics must match: a real "team of 3" does not
    excuse "team of 4", real Korean does not excuse "Native Spanish speaker".
    With no original text, every claim is flagged.

    Returns:
        List of "label: 'claim'" strings.
    """
    findings: list[str] = []
    text_lower = text.lower()
    original_lower = original_text.lower()

    for lang in sorted(_claimed_languages(text_lower) - _claimed_languages(original_lower)):
        findings.append(f"language fluency: '{lang}'")

    for n in sorted(_claimed_team_sizes(text_lower) - _claimed_team_sizes(original_lower)):
        findings.append(f"team leadership: 'team of {n}'")

    m = re.search(_LEADERSHIP_PATTERN, text_lower)
    if m and not re.search(_LEADERSHIP_PATTERN, original_lower or ""):
        findings.append(f"team leadership: '{m.group(0)}'")

    for pattern, label in PERSONAL_ATTRIBUTE_PATTERNS:
        m = re.search(pattern, text_lower)
        if m and not (original_lower and re.search(pattern, original_lower)):
            findings.append(f"{label}: '{m.group(0)}'")

    return findings


# ── Helpers ───────────────────────────────────────────────────────────────

def _build_skills_set(profile: dict) -> set[str]:
    """Build the set of allowed skills from the profile's skills_boundary."""
    boundary = profile.get("skills_boundary", {})
    allowed: set[str] = set()
    for category in boundary.values():
        if isinstance(category, list):
            allowed.update(s.lower().strip() for s in category)
        elif isinstance(category, set):
            allowed.update(s.lower().strip() for s in category)
    return allowed


def sanitize_text(text: str) -> str:
    """Auto-fix common LLM output issues instead of rejecting."""
    text = text.replace(" \u2014 ", ", ").replace("\u2014", ", ")   # em dash -> comma
    text = text.replace("\u2013", "-")    # en dash -> hyphen
    text = text.replace("\u2011", "-")    # non-breaking hyphen -> hyphen
    text = text.replace("\u201c", '"').replace("\u201d", '"')   # smart double quotes
    text = text.replace("\u2018", "'").replace("\u2019", "'")   # smart single quotes
    return text.strip()


# ── JSON Field Validation ─────────────────────────────────────────────────

def validate_json_fields(data: dict, profile: dict, mode: str = "normal",
                         original_text: str = "") -> dict:
    """Validate individual JSON fields from an LLM-generated tailored resume.

    Args:
        data:    Parsed JSON from the LLM (title, summary, skills, experience, projects, education).
        profile: User profile dict from load_profile().
        mode:    Validation strictness — "strict", "normal", or "lenient".
                 strict  → banned words are errors (trigger retries)
                 normal  → banned words are warnings (no retry)
                 lenient → banned words ignored entirely
        original_text: Base resume text; personal-attribute claims already
                 present in it are allowed.

    Returns:
        {"passed": bool, "errors": list[str], "warnings": list[str]}
    """
    errors: list[str] = []
    warnings: list[str] = []

    # Required keys — always checked regardless of mode
    for key in ("title", "skills", "experience", "projects", "education"):
        if key not in data or not data[key]:
            errors.append(f"Missing required field: {key}")
    if errors:
        return {"passed": False, "errors": errors, "warnings": warnings}

    # Collect all text for bulk checks
    all_text_parts: list[str] = [str(data.get("summary", "")), str(data.get("title", ""))]

    # Skills: check for fabrication (always enforced)
    if isinstance(data["skills"], dict):
        skills_text = " ".join(str(v) for v in data["skills"].values()).lower()
        for fake in FABRICATION_WATCHLIST:
            if len(fake) <= 2:
                continue
            if fake in skills_text:
                errors.append(f"Fabricated skill: '{fake}'")

    # Experience: preserved companies must be present (always enforced)
    resume_facts = profile.get("resume_facts", {})
    preserved_companies = resume_facts.get("preserved_companies", [])

    if isinstance(data["experience"], list):
        # Look across header AND subtitle: "Title at Company" and the equally
        # normal "Title" / "Company | Dates" split are both valid resume
        # layouts, and models legitimately choose either. Checking only the
        # header rejected correct, truthful resumes on 2026-07-28 (gemma put
        # the employer in the subtitle) and burned their retry budget.
        #
        # Match on the company's leading token rather than the full profile
        # string: "MetLife (INROADS)" is stored with the internship program in
        # parentheses, but a resume reading just "MetLife" names the employer
        # correctly. The point of this check is to catch a DROPPED or INVENTED
        # employer, not to police punctuation.
        entry_text = [
            f"{e.get('header', '')} {e.get('subtitle', '')}".lower()
            for e in data["experience"]
        ]
        for company in preserved_companies:
            core = re.split(r"\s*[(\[]", company)[0].strip().lower() or company.lower()
            if not any(core in t for t in entry_text):
                errors.append(f"Company '{company}' missing from experience")
        for entry in data["experience"]:
            for b in entry.get("bullets", []):
                all_text_parts.append(b)

    # Projects: collect bullets
    if isinstance(data["projects"], list):
        for entry in data["projects"]:
            for b in entry.get("bullets", []):
                all_text_parts.append(b)

    # Education text (string, list, or {line, details} dict) joins bulk checks
    edu_field = data.get("education", "")
    if isinstance(edu_field, dict):
        all_text_parts.append(str(edu_field.get("line", "")))
        all_text_parts.extend(str(d) for d in edu_field.get("details", []))
    elif isinstance(edu_field, list):
        all_text_parts.extend(str(d) for d in edu_field)
    else:
        all_text_parts.append(str(edu_field))

    # All base-resume roles must survive tailoring (profile-driven count —
    # the same company can legitimately appear twice, so a per-company
    # presence check alone can't catch a dropped second role).
    min_entries = resume_facts.get("min_experience_entries")
    if min_entries and isinstance(data["experience"], list) \
            and len(data["experience"]) < int(min_entries):
        errors.append(
            f"Experience must keep all {min_entries} base-resume roles "
            f"(got {len(data['experience'])})"
        )

    # Education: preserved school must be present (always enforced)
    preserved_school = resume_facts.get("preserved_school", "")
    if preserved_school:
        edu = str(data.get("education", ""))
        if preserved_school.lower() not in edu.lower():
            errors.append(f"Education '{preserved_school}' missing")

    # Bulk text checks
    all_text = " ".join(all_text_parts).lower()

    # LLM self-talk is always an error regardless of mode (indicates broken output)
    found_leaks = [p for p in LLM_LEAK_PHRASES if p in all_text]
    if found_leaks:
        errors.append(f"LLM self-talk: '{found_leaks[0]}'")

    # Experience overclaim — claiming more years than the profile states is a
    # fabrication and is an ERROR in EVERY validation mode. This must live here
    # (not only in validate_tailored_resume) because the tailor path validates
    # exclusively via validate_json_fields.
    cand_years = _candidate_years()
    overclaims = [m for m in re.findall(r"\b(\d{1,2})\s*\+?\s*years?\b", all_text)
                  if int(m) > cand_years]
    if overclaims:
        errors.append(
            f"Overclaims experience: '{max(overclaims, key=int)}+ years' "
            f"(candidate has ~{cand_years} years)"
        )

    # Invented personal attributes — error in EVERY mode, same reasoning.
    for claim in find_personal_attribute_claims(all_text, original_text):
        errors.append(f"Invented personal attribute — {claim}")

    # Banned filler words — severity depends on mode
    if mode != "lenient":
        found_banned = [w for w in BANNED_WORDS if re.search(r"\b" + re.escape(w) + r"\b", all_text)]
        if found_banned:
            msg = f"Banned words: {', '.join(found_banned[:5])}"
            if mode == "strict":
                errors.append(msg)
            else:  # normal
                warnings.append(msg)

    return {"passed": len(errors) == 0, "errors": errors, "warnings": warnings}


# ── Full Resume Text Validation ───────────────────────────────────────────

def validate_tailored_resume(text: str, profile: dict, original_text: str = "") -> dict:
    """Programmatic validation of a tailored resume against the user's profile.

    Args:
        text: The tailored resume text to validate.
        profile: User profile dict from load_profile().
        original_text: The original base resume text (for fabrication comparison).

    Returns:
        {"passed": bool, "errors": list[str], "warnings": list[str]}
    """
    errors: list[str] = []
    warnings: list[str] = []
    text_lower = text.lower()

    personal = profile.get("personal", {})
    resume_facts = profile.get("resume_facts", {})

    # 1. Check required sections exist (flexible matching)
    section_variants: dict[str, list[str]] = {
        "TECHNICAL SKILLS": ["technical skills", "skills", "tech stack", "core skills", "technologies"],
        "EXPERIENCE": ["experience", "work experience", "professional experience"],
        "PROJECTS": ["projects", "personal projects", "key projects", "selected projects"],
        "EDUCATION": ["education", "academic background"],
    }
    for section, variants in section_variants.items():
        if not any(v in text_lower for v in variants):
            errors.append(f"Missing required section: {section} (or variant)")

    # 2. Check name preserved (warn, don't error -- we can inject it)
    full_name = personal.get("full_name", "")
    if full_name and full_name.lower() not in text_lower:
        warnings.append(f"Name '{full_name}' missing -- will be injected")

    # 3. Check companies preserved
    for company in resume_facts.get("preserved_companies", []):
        if company.lower() not in text_lower:
            errors.append(f"Company '{company}' missing -- cannot remove real experience")

    # 4. Check projects preserved
    for project in resume_facts.get("preserved_projects", []):
        if project.lower() not in text_lower:
            warnings.append(f"Project '{project}' not found -- may have been renamed")

    # 5. Check school preserved
    preserved_school = resume_facts.get("preserved_school", "")
    if preserved_school and preserved_school.lower() not in text_lower:
        errors.append(f"Education '{preserved_school}' missing")

    # 6. Check contact info preserved (warn, don't error -- we can inject)
    email = personal.get("email", "")
    phone = personal.get("phone", "")
    if email and email.lower() not in text_lower:
        warnings.append("Email missing -- will be injected")
    if phone and phone not in text:
        warnings.append("Phone missing -- will be injected")

    # 7. Scan TECHNICAL SKILLS section for fabricated tools
    skills_start = text_lower.find("technical skills")
    skills_end = text_lower.find("experience", skills_start) if skills_start != -1 else -1
    if skills_start != -1 and skills_end != -1:
        skills_block = text_lower[skills_start:skills_end]
        for fake in FABRICATION_WATCHLIST:
            if len(fake) <= 2:
                continue
            if fake in skills_block:
                errors.append(f"FABRICATED SKILL in Technical Skills: '{fake}'")

    # 8. Scan full document for fabrication watchlist items not in original
    if original_text:
        original_lower = original_text.lower()
        for fake in FABRICATION_WATCHLIST:
            if len(fake) <= 2:
                continue
            if fake in text_lower and fake not in original_lower:
                warnings.append(f"New tool/skill appeared: '{fake}' (not in original)")

    # 9. Em dashes (should be auto-fixed by sanitize_text, but safety net)
    if "\u2014" in text or "\u2013" in text:
        errors.append("Contains em dash or en dash.")

    # 10. Banned words (word-boundary matching)
    found_banned = [w for w in BANNED_WORDS if re.search(r"\b" + re.escape(w) + r"\b", text_lower)]
    if found_banned:
        errors.append(f"Banned words: {', '.join(found_banned[:5])}")

    # 11. LLM self-talk leak detection
    found_leaks = [p for p in LLM_LEAK_PHRASES if p in text_lower]
    if found_leaks:
        errors.append(f"LLM self-talk: '{found_leaks[0]}'")

    # 12. Duplicate section detection
    for section_name in ["summary", "experience", "education", "projects"]:
        count = text_lower.count(f"\n{section_name}\n") + text_lower.count(f"\n{section_name} \n")
        if text_lower.startswith(f"{section_name}\n"):
            count += 1
        if count > 1:
            errors.append(f"Section '{section_name}' appears {count} times.")

    # 13. Experience overclaim — claiming more years than the profile states
    # is a fabrication and is an error in EVERY validation mode.
    cand_years = _candidate_years()
    overclaims = [m for m in re.findall(r"\b(\d{1,2})\s*\+?\s*years?\b", text_lower)
                  if int(m) > cand_years]
    if overclaims:
        errors.append(
            f"Overclaims experience: '{max(overclaims, key=int)}+ years' "
            f"(candidate has ~{cand_years} years)"
        )

    # 14. Invented personal attributes — error in every mode.
    for claim in find_personal_attribute_claims(text, original_text):
        errors.append(f"Invented personal attribute — {claim}")

    return {
        "passed": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }


# ── Cover Letter Validation ──────────────────────────────────────────────

def validate_cover_letter(text: str, mode: str = "normal",
                          original_text: str = "") -> dict:
    """Programmatic validation of a cover letter.

    Args:
        text: The cover letter text to validate.
        mode: Validation strictness — "strict", "normal", or "lenient".
              strict  → banned words are errors (trigger retries); word limit enforced
              normal  → banned words are warnings; word limit is soft (+25 words)
              lenient → banned words ignored; word count not checked
        original_text: Resume the letter was written from; personal-attribute
              claims already present in it are allowed.

    Returns:
        {"passed": bool, "errors": list[str], "warnings": list[str]}
    """
    errors: list[str] = []
    warnings: list[str] = []
    text_lower = text.lower()

    # 1. Em dashes — always an error (sanitize_text should have caught these)
    if "\u2014" in text or "\u2013" in text:
        errors.append("Contains em dash or en dash.")

    # 2. Banned words — severity depends on mode
    if mode != "lenient":
        found = [w for w in BANNED_WORDS if re.search(r"\b" + re.escape(w) + r"\b", text_lower)]
        if found:
            msg = f"Banned words: {', '.join(found[:5])}"
            if mode == "strict":
                errors.append(msg)
            else:  # normal
                warnings.append(msg)

    # 3. Word count (business-letter format targets ~300 words).
    # A 500-word letter is a quality failure no recruiter reads, so the
    # hard ceiling applies in EVERY mode (like fabrication checks) —
    # lenient relaxes the target, not the ceiling.
    words = len(text.split())
    if words > 375:
        errors.append(
            f"Far too long ({words} words). Rewrite to UNDER 290 words. "
            "Cut whole sentences, not just phrases.")
    elif mode == "strict" and words > 300:
        errors.append(f"Too long ({words} words). Max 300.")
    elif mode == "normal" and words > 325:
        warnings.append(f"Long ({words} words). Target 300.")

    # 4. LLM self-talk — always an error regardless of mode
    found_leaks = [p for p in LLM_LEAK_PHRASES if p in text_lower]
    if found_leaks:
        errors.append(f"LLM self-talk: '{found_leaks[0]}'")

    # 5. Business-letter opening: date line then "Dear ...:" — the salutation
    # must appear within the first few lines.
    if "dear" not in text.strip()[:160].lower():
        errors.append("Must open with the date and a 'Dear ...:' salutation.")

    # 6. Experience overclaim — always an error in every mode. A letter is
    # about the candidate; any years claim above the profile's figure is
    # treated as fabrication.
    cand_years = _candidate_years()
    overclaims = [m for m in re.findall(r"\b(\d{1,2})\s*\+?\s*years?\b", text_lower)
                  if int(m) > cand_years]
    if overclaims:
        errors.append(
            f"Overclaims experience: '{max(overclaims, key=int)}+ years' "
            f"(candidate has ~{cand_years} years)"
        )

    # 7. Invented personal attributes — always an error in every mode.
    for claim in find_personal_attribute_claims(text, original_text):
        errors.append(f"Invented personal attribute — {claim}")

    return {"passed": len(errors) == 0, "errors": errors, "warnings": warnings}
