"""Job fit scoring: LLM-powered evaluation of candidate-job match quality.

Scores jobs on a 1-10 scale by comparing the user's resume against each
job description. All personal data is loaded at runtime from the user's
profile and resume file.
"""

import json
import logging
import re
import time
from datetime import datetime, timezone

from applypilot.config import RESUME_PATH, load_profile
from applypilot.database import get_connection, get_jobs_by_stage
from applypilot.llm import get_client

log = logging.getLogger(__name__)


# ── Scoring Prompt ────────────────────────────────────────────────────────

# Candidate facts + acceptable cities are loaded from profile.json and
# searches.yaml at runtime, so the scorer works for ANY user, not just the
# original author. Cached per process.
_facts_cache: tuple | None = None
_prompt_cache: str | None = None


def _candidate_facts() -> tuple[int, str, bool, str]:
    """(years_of_experience, education_level, has_grad_degree, state_code)."""
    global _facts_cache
    if _facts_cache is None:
        profile = load_profile() or {}
        exp = profile.get("experience", {})
        m = re.search(r"\d+", str(exp.get("years_of_experience_total", "2")))
        years = m and int(m.group()) or 2
        edu = (exp.get("education_level") or "Bachelor's Degree").strip()
        has_grad = any(k in edu.lower() for k in ("master", "doctor", "phd"))
        state = (profile.get("personal", {}).get("province_state") or "").strip().upper()
        _facts_cache = (years, edu, has_grad, state[:2])
    return _facts_cache


def _accept_cities() -> list[str]:
    """Onsite/hybrid-acceptable places from searches.yaml (city names only)."""
    from applypilot import config as _config
    cfg = _config.load_search_config() or {}
    pats = cfg.get("location", {}).get("accept_patterns", [])
    skip = {"remote", "anywhere", "united states", "us", "usa"}
    return [p for p in pats if p.lower() not in skip]


def _home_tok_in(tok: str, loc: str) -> bool:
    """Word-bounded for short tokens so 'wa' can't match inside 'Newark'."""
    if len(tok) <= 3:
        return bool(re.search(rf"(?<![a-z]){re.escape(tok)}(?![a-z])", loc))
    return tok in loc


def _score_prompt() -> str:
    """Build the scoring system prompt from the user's profile + search config."""
    global _prompt_cache
    if _prompt_cache is not None:
        return _prompt_cache

    years, edu, has_grad, state = _candidate_facts()
    cities = _accept_cities()
    city_str = " / ".join(cities[:8]) if cities else "the configured home area"
    region = f"{state} state" if state else "their home state"
    cap_years = years + 1  # anything requiring more years than the candidate has is out

    grad_gate = "" if has_grad else (
        f"- REQUIRES a Master's degree, PhD, or other graduate degree (the "
        f"candidate has a {edu} only) -> SCORE: 5 maximum. \"Master's preferred\" "
        f"or \"Bachelor's or Master's\" is fine -- only a hard graduate-degree "
        f"REQUIREMENT triggers this.\n"
    )

    _prompt_cache = f"""You are a job fit evaluator. Given a candidate's resume and a job description, score how well the candidate fits the role.

HARD LOCATION GATE — apply this BEFORE any skill scoring. It overrides everything else.
The candidate works ONLY in the United States: onsite/hybrid in one of the ACCEPTED LOCATIONS ({city_str}), OR fully remote positions open to US-based workers.
- Job located outside the United States (Canada, UK, India, Mexico, Philippines, Brazil, Turkey, or ANY other country) -> SCORE: 1
- "Remote" but hiring outside the US, global-remote without explicit US eligibility, or remote restricted to another country -> SCORE: 2
- Onsite/hybrid in a US city NOT in the accepted list -> SCORE: 3 maximum. Onsite/hybrid "within the US" is NOT acceptable — only the listed locations or fully remote.
- Location missing from the LOCATION field: hunt for clues in TITLE and DESCRIPTION (city/country names, time zones, currencies like GBP/EUR/INR, non-US salary figures, visa or work-permit language, foreign-language text). Evidence points outside the US -> SCORE: 1-2. NO location evidence anywhere -> SCORE: 4 maximum (unverifiable is not acceptable).
- Only jobs VERIFIABLY in an accepted location or VERIFIABLY US-remote may score 5 or higher.

HARD SENIORITY GATE — the candidate has {years} year(s) of professional experience:
- Title or requirements indicating Senior, Sr., Staff, Principal, Distinguished, Lead, Director, VP, Chief, or Head of -> SCORE: 5 maximum, no exceptions
- ANY explicit level designation of L4/IC4/E4 or higher, SMTS/LMTS/PMTS, or roman-numeral levels III+ -> SCORE: 5 maximum, no exceptions.
- PhD required, Post-Doctoral, or Fellow positions -> SCORE: 5 maximum
- Requires {cap_years}+ years of professional experience (the candidate has {years}) -> SCORE: 5 maximum
{grad_gate}- Roles matching the candidate's experience level (0-{years} years required) -> no penalty; these are the TARGET roles

SCORING CRITERIA (applies only after the job passes the location and seniority gates):"""

    _prompt_cache += """
Scores of 6+ trigger automatic resume tailoring and a REAL application. Be harsh at 7+: a 7 must be defensible against every must-have requirement in the posting. When in doubt between two scores, ALWAYS give the lower one.

ROLE-FAMILY RULE -- this decides 6 vs 5 and overrides skill overlap:
The candidate is a DATA ANALYST. A posting only earns 6+ if the WORK ITSELF, as described in the responsibilities / "what you'll do" section, is data and analytics work: analysis, reporting, dashboards, BI, data pipelines, modeling, experimentation, data quality.
Shared TOOLS are never enough. A Security Operations Analyst, Incident Response Analyst, Pricing Analyst, Underwriting Analyst, Program Analyst, Contract/Billing Analyst, Marketing Analyst or Insurance QA Analyst that happens to list SQL, Excel, Tableau or "reporting" is a 5, NOT a 6 -- the day-to-day work is security, pricing, underwriting, program management or billing, and the candidate would be doing that work, not data analysis.
Read the RESPONSIBILITIES, not the skills list. If a posting only enumerates skills and never describes what the person actually does, you cannot confirm the role family -- score it 5.
- 9-10: Near-perfect match. The candidate meets EVERY stated requirement including nice-to-haves, the title matches their target role, and their strongest projects map directly onto the job's core work. Rare -- a few per hundred jobs.
- 7-8: Strong match. The candidate meets ALL must-have requirements (skills, tools, years, education) with real evidence in the resume, and at most one minor nice-to-have is missing. A single unmet MUST-HAVE requirement disqualifies a job from 7+.
- 6: Good match with ONE gap. The described day-to-day work IS data/analytics work (see the ROLE-FAMILY RULE above) and the candidate covers that core work, but exactly one must-have skill or tool is missing or only loosely evidenced. A worthwhile application, not a stretch.
- 5: Partial match. Multiple must-haves are missing or weakly covered, OR the role family is wrong / cannot be confirmed from the responsibilities even though the tools overlap. Most plausible-looking jobs belong at 5, not 6.
- 3-4: Weak match. Significant skill gaps, would need substantial ramp-up.
- 1-2: Poor match. Completely different field or experience level, or failed the location gate.
Sanity check before answering: if you scored 7+, name (to yourself) the resume evidence for every must-have in the posting. If any requirement lacks direct evidence, drop to 6 (one gap) or 5 (more than one).

IMPORTANT FACTORS:
- Weight technical skills heavily (programming languages, frameworks, tools) ONLY once the ROLE-FAMILY RULE is satisfied. Skill overlap never promotes an off-family role -- it decides how well he fits a role that is already data/analytics work.
- Consider transferable experience (automation, scripting, API work)
- Factor in the candidate's project experience
- Be realistic about experience level vs. job requirements (years of experience, seniority)

RESPOND IN EXACTLY THIS FORMAT (no other text):
SCORE: [1-10]
KEYWORDS: [comma-separated ATS keywords from the job description that match or could match the candidate]
REASONING: [2-3 sentences explaining the score]"""
    return _prompt_cache


# Deterministic backstop for the seniority gate: even if the scoring model
# ignores the prompt (small local models often do), these titles can never
# enter the 7+ tailor/apply queue.
_SENIOR_TITLE_RX = re.compile(
    r"\b(senior|sr\.?|staff|principal|distinguished|lead|director|"
    r"vp|vice president|chief|head of|architect|"
    r"post-?doc(toral)?|ph\.?d|fellow|"
    # Level ladders: any explicit L4+ / IC4+ / E4+ code is out (user rule:
    # leveled requisitions are not entry-level postings, period). Anchored
    # with \b so they can't match inside ordinary tokens (e.g. "HTML5").
    r"\b(?:l|ic|e)[4-9]\b|\blevel\s*[4-9]\b|"
    r"[slp]mts|"          # Salesforce SMTS/LMTS/PMTS = senior/lead/principal
    r"\w+ (iii|iv|v)\b|"  # Analyst III / Engineer IV style suffixes
    # Leadership / people-management: "2+ years" in these postings means
    # 2+ years LEADING, not doing — never entry-level.
    r"manager|supervisor|co-?founder|founding|"
    r"mid[- ]level)\b",
    re.IGNORECASE,
)

# Hard domain mismatches: fields the candidate has zero background in.
# A title match here means the role's core skill (graphics programming,
# hardware, physical engineering...) isn't on the resume at all.
_DOMAIN_MISMATCH_RX = re.compile(
    r"\b(3d|graphics|game(play)? engineer|firmware|embedded|kernel|"
    r"device driver|asic|fpga|silicon|rf engineer|antenna|"
    r"mechanical|civil|electrical engineer|aerospace|avionics|"
    r"stress engineer|structural|design and analysis engineer|"
    r"manufacturing engineer|process engineer|chemist|biologist|"
    r"clinical|nurse|pharmac)\b",
    re.IGNORECASE,
)


# The candidate's target role family. When a title is one of these, a
# domain-mismatch keyword (e.g. "clinical" in "Clinical Data Analyst") is
# just the subject area of a data role, not an out-of-field role — so the
# domain cap must NOT fire.
_TARGET_FAMILY_RX = re.compile(
    r"\b(analyst|analytics|data|scientist|statistician)\b", re.IGNORECASE)

# Pure software / infrastructure engineering roles. The candidate is a data
# analyst (user decision 2026-07-23: not pursuing SWE) — these are out of
# target even though they share some tooling. Capped to 5 so they never reach
# the apply band.
_SWE_RX = re.compile(
    r"\b(front[- ]?end|back[- ]?end|full[- ]?stack|"
    r"software engineer|software developer|software dev engineer|\bsde\b|\bswe\b|"
    r"web developer|web dev|mobile (engineer|developer)|ios|android|"
    r"devops|site reliability|\bsre\b|platform engineer|systems engineer|"
    r"security engineer|network engineer|cloud engineer|infrastructure engineer|"
    r"gameplay|game engineer|qa engineer|test engineer|automation engineer)\b",
    re.IGNORECASE,
)
# ...but keep data/ML/AI engineering, which IS target-adjacent (his projects
# are Python data pipelines + an NLP model).
_SWE_EXEMPT_RX = re.compile(
    r"\b(data|analytics|analyst|scientist|statistician|"
    r"machine learning|\bml\b|\bai\b)\b", re.IGNORECASE)


# ── Role-family (title lane) gate — user decision 2026-08-10 ────────────
# _TARGET_FAMILY_RX above is deliberately broad ("analyst" alone matches), which
# is right for the domain gate but far too loose for the apply band: a 2026-08-10
# funnel audit found 116 of 195 unclaimed 6s were off-lane "analyst" roles
# (Security Operations, Incident Response, Pricing, Underwriting, Program,
# Contract/Billing) scoring 6 purely on SQL/Excel/dashboard overlap. With the
# apply threshold at 6 those would all become real applications, so the title
# must confirm the role family.
#
# _LANE_CORE_RX wins over _LANE_OFF_RX: "Data Analyst, Pricing Intelligence" is
# in-lane (data analyst doing pricing), while "Pricing Analyst" is not.
# Applied ONLY at exactly 6 — 7+ has its own evidence bar and 41 applications at
# 7 produced zero rejections, so that band is deliberately untouched.
# NOTE: stems that must match inflections (scien -> scientist/science) carry an
# explicit \w* — a trailing \b on a stem silently never matches. Verified by the
# lane test-case table; do not "simplify" these back into bare stems.
_LANE_CORE_RX = re.compile(
    r"\b(data analyst|data analytics|analytics analyst|data scien\w*|"
    r"business intelligence|bi (analyst|engineer|developer|specialist)|"
    r"analytics engineer|data engineer|reporting analyst|insights analyst|"
    r"business analyst|business systems analyst|statistician|"
    r"quantitative analyst|decision scientist|product analyst|"
    r"machine learning|ml engineer|data quality|data governance|"
    r"data steward|database analyst|research analyst)\b",
    re.IGNORECASE,
)

# Non-data specializations that merely share the word "analyst".
_LANE_OFF_RX = re.compile(
    r"\b(security|securities|cyber\w*|incident response|soc|threat|"
    r"fraud investigat\w*|pricing|underwrit\w*|actuar\w*|claims|insurance|"
    r"credit|loan|mortgage|collections|treasury|budget|payroll|tax|"
    r"audit\w*|compliance|program|project|contract|procurement|"
    r"billing|invoic\w*|marketing|seo|social media|brand|advertis\w*|"
    r"hr|human resources|talent|recruit\w*|people operations|"
    r"legal|paralegal|policy|regulatory|"
    r"quality assurance|qa|test analyst|"
    r"sales|account manage\w*|customer success|"
    r"help ?desk|desktop support|it support|service desk)\b",
    re.IGNORECASE,
)


def _cap_off_lane(title: str, parsed: dict) -> dict:
    """Demote a 6 to 5 when the TITLE shows the work is not data/analytics.

    Only bites at exactly 6 (the apply-band boundary since 2026-08-10). Core
    data/analytics wording always wins, so a data role in a security or pricing
    domain survives while a security or pricing role does not.
    """
    title = title or ""
    if parsed.get("score") != 6:
        return parsed
    if _LANE_CORE_RX.search(title):
        return parsed
    if _LANE_OFF_RX.search(title):
        parsed["score"] = 5
        parsed["reasoning"] = (
            parsed.get("reasoning", "")
            + " [capped at 5: off-lane role family — title is not data/analytics"
              " work, skill overlap alone does not qualify]"
        )
    return parsed


def _cap_domain(title: str, parsed: dict) -> dict:
    """Cap fit_score at 5 for titles in fields absent from the resume."""
    title = title or ""
    if _TARGET_FAMILY_RX.search(title):
        return parsed  # data/analyst family: domain keyword is just the subject
    if parsed.get("score", 0) > 5 and _DOMAIN_MISMATCH_RX.search(title):
        parsed["score"] = 5
        parsed["reasoning"] = (
            parsed.get("reasoning", "")
            + " [capped at 5: core domain not present on candidate resume]"
        )
    return parsed


# Internship / early-career pipeline titles (2026-07-30). These are DISCOVERED
# and SCORED but never auto-applied: most require current enrollment and the
# candidate's Master's admission is still pending, so a bot answering "are you
# currently enrolled?" would have to say no. fix-gates parks them for human
# review instead.
_INTERNSHIP_RX = re.compile(
    r"\b(intern|internship|co-?op|apprentice|trainee)\b", re.IGNORECASE)


def is_internship(title: str) -> bool:
    """True when a title is an internship / early-career pipeline role."""
    return bool(_INTERNSHIP_RX.search(title or ""))


_PROFILE_CACHE: dict | None = None


def _profile_for_boost() -> dict:
    """Profile, loaded once per process (scoring calls this per job)."""
    global _PROFILE_CACHE
    if _PROFILE_CACHE is None:
        try:
            from applypilot.config import load_profile
            _PROFILE_CACHE = load_profile()
        except Exception:  # never let a profile problem break scoring
            _PROFILE_CACHE = {}
    return _PROFILE_CACHE


def _boost_target_company(job: dict, parsed: dict, profile: dict) -> dict:
    """+1 (max 9) for employers with strong pay/benefits/work-life reputation.

    User decision 2026-07-30: surface Forbes/Glassdoor-calibre employers above
    equivalent generic postings. Capped at 9 so a boosted role never leapfrogs a
    genuine 10.

    Floor raised 5 -> 6 on 2026-08-10 when the apply threshold moved 7 -> 6.
    EVERY gate (seniority, domain, SWE, spam, off-lane role family) caps at 5,
    so boosting a 5 now lands it exactly on the apply bar — employer reputation
    would silently undo every gate. The floor must stay strictly ABOVE the gate
    cap value: if the threshold ever moves again, move this with it.
    """
    targets = profile.get("target_companies") or []
    if not targets:
        return parsed
    score = parsed.get("score")
    if not isinstance(score, int) or not 6 <= score < 9:
        return parsed
    hay = f"{job.get('company') or ''} {job.get('site') or ''}".lower()
    for t in targets:
        tl = t.lower()
        if tl and tl in hay:
            parsed["score"] = score + 1
            parsed["reasoning"] = (
                parsed.get("reasoning", "")
                + f" [+1: {t} — target employer for pay/benefits/work-life]"
            )
            break
    return parsed


def _cap_swe(title: str, parsed: dict) -> dict:
    """Cap fit_score at 5 for pure software/infra engineering roles (not data)."""
    title = title or ""
    if parsed.get("score", 0) > 5 and _SWE_RX.search(title) and not _SWE_EXEMPT_RX.search(title):
        parsed["score"] = 5
        parsed["reasoning"] = (
            parsed.get("reasoning", "")
            + " [capped at 5: software-engineering role, not data/analytics]"
        )
    return parsed


def _cap_seniority(title: str, parsed: dict) -> dict:
    """Cap fit_score at 5 for titles that are clearly not entry-level.

    Cap moved 6 -> 5 on 2026-07-18 when the apply threshold moved 7 -> 6:
    gate rejects must sit below the threshold, and score 6 is now reserved
    for genuine borderline fits.
    """
    if parsed.get("score", 0) > 5 and _SENIOR_TITLE_RX.search(title or ""):
        parsed["score"] = 5
        parsed["reasoning"] = (
            parsed.get("reasoning", "")
            + " [capped at 5: senior-level title vs entry-level candidate]"
        )
    return parsed


# Deterministic years-of-experience gate. The candidate has 1-2 years;
# postings whose MINIMUM stated requirement is 4+ years are out of reach
# and get capped below the tailor/apply threshold regardless of the model.
_YEARS_RX = re.compile(
    r"(?:minimum(?: of)?|at least|requires?)?\s*"
    r"(\d{1,2})\s*(?:\+|\s*or more|\s*plus)?"
    r"(?:\s*(?:-|–|to)\s*\d{1,2})?\s*"
    r"years?(?:['’]?)\s+(?:of\s+)?[\w\s,/-]{0,40}?experience",
    re.IGNORECASE,
)


# A years figure inside a clause with any of these words is a SOFT preference,
# not a hard requirement — "3-5 years preferred" must not demote an entry role.
_SOFT_REQ_RX = re.compile(r"preferred|nice to have|a plus|ideally", re.IGNORECASE)


def _min_required_years(description: str) -> int | None:
    """Extract the smallest HARD years-of-experience requirement mentioned.

    Uses min() across matches: a JD saying '2+ years required, 5+ preferred'
    has an effective bar of 2. Clauses containing "preferred"/"nice to have"/
    "a plus"/"ideally" are ignored (soft preferences, not requirements).
    Returns None when no hard requirement is stated.
    """
    matches: list[int] = []
    for clause in re.split(r"[.\n;]", description or ""):
        if _SOFT_REQ_RX.search(clause):
            continue
        matches += [int(m) for m in _YEARS_RX.findall(clause) if 0 < int(m) <= 20]
    return min(matches) if matches else None


# Deterministic location gate: onsite/hybrid is only acceptable in the
# user's configured home area (searches.yaml accept_patterns + profile
# state). Remote-US is fine. Anything verifiably placing the job in another
# state without "remote" in the location is out.
_STATE_CODE_RX = re.compile(r",\s*([A-Za-z]{2})(?:\b|$)")

# Positive evidence that a remote role is open to US-based workers.
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
# Clear foreign-remote markers.
_FOREIGN_RX = re.compile(
    r"canada|united kingdom|\buk\b|ireland|india|philippines|pakistan|"
    r"mexico|brazil|argentina|colombia|europe|\bemea\b|\bapac\b|\blatam\b|"
    r"australia|new zealand|germany|france|spain|poland|portugal|romania|"
    r"netherlands|singapore|japan|korea|vietnam|nigeria|egypt|turkey|"
    r"türkiye|ukraine|worldwide|global[- ]remote",
    re.IGNORECASE,
)


def _cap_location(location: str, description: str, parsed: dict) -> dict:
    """Cap the score for out-of-area onsite jobs AND unverifiable-US remote jobs."""
    if parsed.get("score", 0) <= 6:
        return parsed
    loc = (location or "").lower()
    if not loc:
        # No location at all: require US evidence in the description,
        # otherwise the job is unverifiable and out (user rule).
        if _US_EVIDENCE_RX.search((description or "")[:5000]):
            return parsed
        parsed["score"] = 4
        parsed["reasoning"] = (parsed.get("reasoning", "")
            + " [capped at 4: no location given and no US evidence in description]")
        return parsed
    if "remote" in loc or "anywhere" in loc:
        # Remote must be VERIFIABLY US-based. Foreign marker in the location
        # itself -> out. Otherwise require positive US evidence in the
        # location or description; "Remote" with no country is not enough.
        if _FOREIGN_RX.search(loc):
            parsed["score"] = 2
            parsed["reasoning"] = (parsed.get("reasoning", "")
                + f" [capped at 2: remote but foreign region in '{location}']")
            return parsed
        blob = f"{location} {(description or '')[:5000]}"
        if not _US_EVIDENCE_RX.search(blob):
            parsed["score"] = 4
            parsed["reasoning"] = (parsed.get("reasoning", "")
                + " [capped at 4: remote with no verifiable US eligibility]")
        return parsed
    home_state = _candidate_facts()[3]
    tokens = tuple(p.lower() for p in _accept_cities())
    # Washington-state users: don't let "Washington, DC" pass as WA.
    dc = home_state == "WA" and (
        "washington, dc" in loc or "washington dc" in loc
        or ", dc" in loc or "d.c" in loc)
    # WHITELIST rule: onsite/hybrid must name a home-area place. Anything
    # else ("Austin, TX", "Chicago, Illinois", "NYC Metro Area") is capped —
    # no state-code parsing, no benefit of the doubt.
    if not dc and tokens and any(_home_tok_in(t, loc) for t in tokens):
        return parsed
    parsed["score"] = 3
    parsed["reasoning"] = (
        parsed.get("reasoning", "")
        + f" [capped at 3: onsite/hybrid in '{location}' — outside the "
          f"configured home area and not remote]"
    )
    return parsed


# Deterministic education gate: the candidate holds a Bachelor's degree.
# Postings that HARD-REQUIRE a graduate degree are out of reach. Sentences
# mentioning "preferred", "bachelor", or "or equivalent" never trigger it
# ("Bachelor's or Master's", "Master's preferred" are fine).
_GRAD_RX = re.compile(
    r"(master'?s?\s+degree|master'?s\b|ph\.?\s?d|m\.s\.|msc\b|doctorate|"
    r"doctoral|graduate degree)"
    r"[^.\n]{0,60}?(is required|required|must have|must hold)",
    re.IGNORECASE,
)


def _requires_grad_degree(description: str) -> bool:
    for sentence in re.split(r"[.\n]", description or ""):
        low = sentence.lower()
        if "preferred" in low or "bachelor" in low or "or equivalent" in low:
            continue
        if _GRAD_RX.search(sentence):
            return True
    return False


def _cap_education(description: str, parsed: dict) -> dict:
    """Cap fit_score at 6 when the JD hard-requires a graduate degree."""
    _, edu, has_grad, _ = _candidate_facts()
    if has_grad:
        return parsed  # candidate holds a graduate degree; gate not needed
    if parsed.get("score", 0) > 6 and _requires_grad_degree(description):
        parsed["score"] = 6
        parsed["reasoning"] = (
            parsed.get("reasoning", "")
            + f" [capped at 6: JD requires a graduate degree, candidate has a {edu}]"
        )
    return parsed


def _cap_years(description: str, parsed: dict) -> dict:
    """Cap fit_score at 6 when the JD requires well beyond the candidate's years."""
    if parsed.get("score", 0) > 6:
        cand_years = _candidate_facts()[0]
        years = _min_required_years(description)
        if years is not None and years > cand_years:
            parsed["score"] = 6
            parsed["reasoning"] = (
                parsed.get("reasoning", "")
                + f" [capped at 6: JD requires {years}+ years, candidate has {cand_years}]"
            )
    return parsed


def _parse_score_response(response: str) -> dict:
    """Parse the LLM's score response into structured data.

    Args:
        response: Raw LLM response text.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    # None = no parseable SCORE line. The caller must leave the job unscored:
    # writing 0 for unparseable output buried hundreds of jobs on 2026-07-20
    # when a fallback model answered scoring prompts with resume-tailoring
    # text (same derailment that got the local 8B banned).
    score = None
    keywords = ""
    reasoning = response

    for line in response.split("\n"):
        # Reasoning models decorate labels ("**SCORE:** 7", "## SCORE: 7")
        line = line.strip().lstrip("#").replace("**", "").strip()
        if line.startswith("SCORE:"):
            try:
                score = int(re.search(r"\d+", line).group())
                score = max(1, min(10, score))
            except (AttributeError, ValueError):
                score = None
        elif line.startswith("KEYWORDS:"):
            keywords = line.replace("KEYWORDS:", "").strip()
        elif line.startswith("REASONING:"):
            reasoning = line.replace("REASONING:", "").strip()

    return {"score": score, "keywords": keywords, "reasoning": reasoning}


def score_job(resume_text: str, job: dict) -> dict:
    """Score a single job against the resume.

    Args:
        resume_text: The candidate's full resume text.
        job: Job dict with keys: title, site, location, full_description.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job['site']}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    messages = [
        {"role": "system", "content": _score_prompt()},
        {"role": "user", "content": f"RESUME:\n{resume_text}\n\n---\n\nJOB POSTING:\n{job_text}"},
    ]

    try:
        client = get_client()
        # A provider can answer 200 with garbage (OpenRouter's nemotron replies
        # to scoring prompts with resume-tailoring prose — it derailed 434 jobs
        # on 2026-07-20 and stalled a whole run on 07-25). Exception-based
        # failover can't see that, so on unparseable output demote the provider
        # and retry the SAME job down the chain before giving up on it.
        for _ in range(3):
            response = client.chat(messages, max_tokens=4096, temperature=0.2)
            parsed = _parse_score_response(response)
            if parsed["score"] is not None:
                break
            log.warning("Unparseable score response for '%s' from %s — demoting.",
                        job.get("title", "?"), getattr(client, "base_url", "?"))
            if not (hasattr(client, "demote_current")
                    and client.demote_current("unparseable scoring output")):
                break
        if parsed["score"] is None:
            log.warning("Unparseable score response for '%s' — leaving unscored.",
                        job.get("title", "?"))
            return parsed
        parsed = _cap_seniority(job.get("title", ""), parsed)
        parsed = _cap_domain(job.get("title", ""), parsed)
        parsed = _cap_swe(job.get("title", ""), parsed)
        parsed = _cap_off_lane(job.get("title", ""), parsed)
        parsed = _cap_years(job.get("full_description") or "", parsed)
        parsed = _cap_education(job.get("full_description") or "", parsed)
        parsed = _cap_location(job.get("location") or "",
                               job.get("full_description") or "", parsed)
        # Boost LAST so it applies to the post-cap score — a role capped for
        # seniority or domain must not be lifted back over the bar by employer
        # reputation.
        return _boost_target_company(job, parsed, _profile_for_boost())
    except Exception as e:
        log.error("LLM error scoring job '%s': %s", job.get("title", "?"), e)
        # Provider outage: score=None means "leave unscored, retry next pass".
        # Writing 0 here permanently buried ~650 jobs on 2026-07-20 when the
        # whole free chain hit daily quotas mid-run (score=0 rows never
        # re-enter pending_score).
        return {"score": None, "keywords": "", "reasoning": f"LLM error: {e}"}


def run_scoring(limit: int = 0, rescore: bool = False) -> dict:
    """Score unscored jobs that have full descriptions.

    Args:
        limit: Maximum number of jobs to score in this run.
        rescore: If True, re-score all jobs (not just unscored ones).

    Returns:
        {"scored": int, "errors": int, "elapsed": float, "distribution": list}
    """
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    conn = get_connection()

    if rescore:
        query = "SELECT * FROM jobs WHERE full_description IS NOT NULL"
        if limit > 0:
            query += f" LIMIT {limit}"
        jobs = conn.execute(query).fetchall()
    else:
        jobs = get_jobs_by_stage(conn=conn, stage="pending_score", limit=limit)

    if not jobs:
        log.info("No unscored jobs with descriptions found.")
        return {"scored": 0, "errors": 0, "elapsed": 0.0, "distribution": []}

    # Convert sqlite3.Row to dicts if needed
    if jobs and not isinstance(jobs[0], dict):
        columns = jobs[0].keys()
        jobs = [dict(zip(columns, row)) for row in jobs]

    log.info("Scoring %d jobs sequentially...", len(jobs))
    t0 = time.time()
    completed = 0
    errors = 0
    results: list[dict] = []

    consecutive_outages = 0
    for job in jobs:
        result = score_job(resume_text, job)
        result["url"] = job["url"]
        completed += 1

        if result["score"] is None or result["score"] == 0:
            errors += 1

        results.append(result)

        log.info(
            "[%d/%d] score=%s  %s",
            completed, len(jobs), result["score"], job.get("title", "?")[:60],
        )

        # Abort only when the chain is genuinely DEAD (every provider raised),
        # not when a provider merely returned unparseable text — that case now
        # demotes the provider inside score_job and keeps going. Conflating the
        # two aborted a healthy run at 103/2071 on 2026-07-25 while three
        # freshly-funded providers sat unused further down the chain.
        chain_dead = (result["score"] is None
                      and str(result.get("reasoning", "")).startswith("LLM error"))
        if chain_dead:
            consecutive_outages += 1
            if consecutive_outages >= 10:
                log.error(
                    "Aborting scoring run: %d consecutive provider-chain "
                    "failures (%d/%d jobs done). Remaining jobs stay pending.",
                    consecutive_outages, completed, len(jobs),
                )
                break
        else:
            consecutive_outages = 0

    # Write scores to DB (outage rows keep fit_score NULL for the next pass)
    now = datetime.now(timezone.utc).isoformat()
    for r in results:
        if r["score"] is None:
            continue
        conn.execute(
            "UPDATE jobs SET fit_score = ?, score_reasoning = ?, scored_at = ? WHERE url = ?",
            (r["score"], f"{r['keywords']}\n{r['reasoning']}", now, r["url"]),
        )
    conn.commit()

    elapsed = time.time() - t0
    log.info("Done: %d scored in %.1fs (%.1f jobs/sec)", len(results), elapsed, len(results) / elapsed if elapsed > 0 else 0)

    # Score distribution
    dist = conn.execute("""
        SELECT fit_score, COUNT(*) FROM jobs
        WHERE fit_score IS NOT NULL
        GROUP BY fit_score ORDER BY fit_score DESC
    """).fetchall()
    distribution = [(row[0], row[1]) for row in dist]

    return {
        "scored": len(results),
        "errors": errors,
        "elapsed": elapsed,
        "distribution": distribution,
    }
