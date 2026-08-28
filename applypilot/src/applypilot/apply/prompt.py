"""Prompt builder for the autonomous job application agent.

Constructs the full instruction prompt that tells Claude Code / the AI agent
how to fill out a job application form using Playwright MCP tools. All
personal data is loaded from the user's profile -- nothing is hardcoded.
"""

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

from applypilot import config

logger = logging.getLogger(__name__)


def _build_profile_summary(profile: dict) -> str:
    """Format the applicant profile section of the prompt.

    Reads all relevant fields from the profile dict and returns a
    human-readable multi-line summary for the agent.
    """
    p = profile
    personal = p["personal"]
    work_auth = p["work_authorization"]
    comp = p["compensation"]
    exp = p.get("experience", {})
    avail = p.get("availability", {})
    eeo = p.get("eeo_voluntary", {})

    lines = [
        f"Name: {personal['full_name']}",
        f"Email: {personal['email']}",
        f"Phone: {personal['phone']}",
    ]

    # Address -- handle optional fields gracefully
    addr_parts = [
        personal.get("address", ""),
        personal.get("city", ""),
        personal.get("province_state", ""),
        personal.get("country", ""),
        personal.get("postal_code", ""),
    ]
    lines.append(f"Address: {', '.join(p for p in addr_parts if p)}")

    if personal.get("linkedin_url"):
        lines.append(f"LinkedIn: {personal['linkedin_url']}")
    if personal.get("github_url"):
        lines.append(f"GitHub: {personal['github_url']}")
    if personal.get("portfolio_url"):
        lines.append(f"Portfolio: {personal['portfolio_url']}")
    if personal.get("website_url"):
        lines.append(f"Website: {personal['website_url']}")

    # Work authorization
    lines.append(f"Work Auth: {work_auth.get('legally_authorized_to_work', 'See profile')}")
    lines.append(f"Sponsorship Needed: {work_auth.get('require_sponsorship', 'See profile')}")
    if work_auth.get("work_permit_type"):
        lines.append(f"Work Permit: {work_auth['work_permit_type']}")

    # Compensation
    currency = comp.get("salary_currency", "USD")
    lines.append(f"Salary Expectation: ${comp['salary_expectation']} {currency}")

    # Experience
    if exp.get("years_of_experience_total"):
        lines.append(f"Years Experience: {exp['years_of_experience_total']}")
    if exp.get("education_level"):
        lines.append(f"Education: {exp['education_level']}")

    # Availability
    lines.append(f"Available: {avail.get('earliest_start_date', 'Immediately')}")

    # Standard responses
    lines.extend([
        "Age 18+: Yes",
        "Background Check: Yes",
        "Felony: No",
        "Previously Worked Here: No",
        "How Heard: Online Job Board",
    ])

    # EEO
    lines.append(f"Gender: {eeo.get('gender', 'Decline to self-identify')}")
    lines.append(f"Race: {eeo.get('race_ethnicity', 'Decline to self-identify')}")
    lines.append(f"Veteran: {eeo.get('veteran_status', 'I am not a protected veteran')}")
    lines.append(f"Disability: {eeo.get('disability_status', 'I do not wish to answer')}")

    return "\n".join(lines)


def _build_location_check(profile: dict, search_config: dict) -> str:
    """Build the location eligibility check section of the prompt.

    Uses the accept_patterns from search config to determine which cities
    are acceptable for hybrid/onsite roles.
    """
    personal = profile["personal"]
    location_cfg = search_config.get("location", {})
    accept_patterns = location_cfg.get("accept_patterns", [])
    primary_city = personal.get("city", location_cfg.get("primary", "your city"))

    # Build the list of acceptable cities for hybrid/onsite. Country-level
    # and remote tokens ("United States", "US", "Remote") exist in the accept
    # list only so remote jobs pass discovery — they must NEVER make the
    # agent believe onsite anywhere in the US is acceptable.
    _non_city = {"remote", "anywhere", "united states", "us", "usa"}
    cities = [p for p in accept_patterns if p.lower() not in _non_city]
    city_list = ", ".join(cities) if cities else primary_city

    return f"""== LOCATION CHECK (do this FIRST before any form) ==
Read the job page. Determine the work arrangement. Then decide:
- "Remote" or "work from anywhere" -> ELIGIBLE. Apply.
- "Hybrid" or "onsite" in {city_list} -> ELIGIBLE. Apply.
- "Hybrid" or "onsite" in another city BUT the posting also says "remote OK" or "remote option available" -> ELIGIBLE. Apply.
- "Onsite only" or "hybrid only" in any city outside the list above with NO remote option -> NOT ELIGIBLE. Stop immediately. Output RESULT:FAILED:not_eligible_location
- The acceptable onsite/hybrid cities are ONLY the Washington-state list above. Onsite/hybrid in ANY other US state (Texas, California, New York, DC, etc.) is NOT ELIGIBLE unless the posting explicitly offers a remote option. "Within the US" is only acceptable for fully REMOTE roles.
- City is overseas (India, Philippines, Europe, etc.) with no remote option -> NOT ELIGIBLE. Output RESULT:FAILED:not_eligible_location
- Cannot determine location -> Continue applying. If a screening question reveals it's non-local onsite, answer honestly and let the system reject if needed.
Do NOT fill out forms for jobs that are clearly onsite in a non-acceptable location. Check EARLY, save time."""


def _build_salary_section(profile: dict) -> str:
    """Build the salary negotiation instructions.

    Adapts floor, range, and currency from the profile's compensation section.
    """
    comp = profile["compensation"]
    currency = comp.get("salary_currency", "USD")
    floor = comp["salary_expectation"]
    range_min = comp.get("salary_range_min", floor)
    range_max = comp.get("salary_range_max", str(int(floor) + 20000) if floor.isdigit() else floor)
    conversion_note = comp.get("currency_conversion_note", "")

    # Compute example hourly rates at 3 salary levels
    try:
        floor_int = int(floor)
        examples = [
            (f"${floor_int // 1000}K", floor_int // 2080),
            (f"${(floor_int + 25000) // 1000}K", (floor_int + 25000) // 2080),
            (f"${(floor_int + 55000) // 1000}K", (floor_int + 55000) // 2080),
        ]
        hourly_line = ", ".join(f"{sal} = ${hr}/hr" for sal, hr in examples)
    except (ValueError, TypeError):
        hourly_line = "Divide annual salary by 2080"

    # Currency conversion guidance
    if conversion_note:
        convert_line = f"Posting is in a different currency? -> {conversion_note}"
    else:
        convert_line = "Posting is in a different currency? -> Target midpoint of their range. Convert if needed."

    return f"""== SALARY (think, don't just copy) ==
${floor} {currency} is the FLOOR. Never go below it. But don't always use it either.

Decision tree:
1. Job posting shows a range (e.g. "$120K-$160K")? -> Answer with the MIDPOINT ($140K).
2. Title says Senior, Staff, Lead, Principal, Architect, or level II/III/IV? -> Minimum $110K {currency}. Use midpoint of posted range if higher.
3. {convert_line}
4. No salary info anywhere? -> Use ${floor} {currency}.
5. Asked for a range? -> Give posted midpoint minus 10% to midpoint plus 10%. No posted range? -> "${range_min}-${range_max} {currency}".
6. Hourly rate? -> Divide your annual answer by 2080. ({hourly_line})"""


def _build_screening_section(profile: dict) -> str:
    """Build the screening questions guidance section."""
    personal = profile["personal"]
    exp = profile.get("experience", {})
    city = personal.get("city", "their city")
    years = exp.get("years_of_experience_total", "multiple")
    target_role = exp.get("target_role", personal.get("current_job_title", "software engineer"))
    work_auth = profile["work_authorization"]

    reloc = profile.get("relocation", {}) or {}
    if reloc.get("willing"):
        if (reloc.get("assistance") or "").startswith("required"):
            assist = ("Relocation assistance: REQUIRED — any question about "
                      "needing/requiring relocation assistance -> YES.")
        else:
            assist = ("Relocation assistance: preferred but not required — "
                      "\"do you REQUIRE relocation assistance?\" -> No; "
                      "\"would you LIKE relocation assistance?\" -> Yes.")
        reloc_line = (
            f"lives in {city}. WILLING to relocate to: {reloc.get('metros')}. "
            f"If asked \"willing to relocate?\" for one of those metros (or generally) -> YES. "
            + assist)
    else:
        reloc_line = f"lives in {city}, cannot relocate"

    return f"""== SCREENING QUESTIONS (be strategic) ==
Hard facts -> answer truthfully from the profile. No guessing. This includes:
  - Location/relocation: {reloc_line}
  - Work authorization: {work_auth.get('legally_authorized_to_work', 'see profile')}
  - Citizenship, clearance, licenses, certifications: answer from profile only
  - Criminal/background: answer from profile only

Skills and tools -> be confident. This candidate is a {target_role} with {years} years experience. If the question asks "Do you have experience with [tool]?" and it's in the same domain (DevOps, backend, ML, cloud, automation), answer YES. Software engineers learn tools fast. Don't sell short.

Open-ended questions ("Why do you want this role?", "Tell us about yourself", "What interests you?") -> Write 2-3 sentences. Be specific to THIS job. Reference something from the job description. Connect it to a real achievement from the resume. No generic fluff. No "I am passionate about..." -- sound like a real person.

EEO/demographics -> use the APPLICANT PROFILE's exact answers (Gender, Race, Veteran, Disability). If the form doesn't offer the profile's wording, pick the closest option.
  - DISABILITY (CC-305 and similar): the answer is "No, I do not have a disability and have not had one in the past". NEVER select "Yes". After selecting, verify in the snapshot that the "No" option is the one actually checked before moving on."""


def _build_education_section(profile: dict) -> str:
    """Build explicit education-fill instructions.

    Reads an optional profile['education'] list; falls back to sensible
    defaults + normalization rules so the agent never guesses school, degree,
    or major on ATS education forms.
    """
    edu_entries = profile.get("education") or []

    # Defaults / normalization (override via profile['education'][0]).
    school = "University of Washington"
    campus = "Seattle"
    degree = "B.S."
    field_order = ["Informatics", "Information Science", "Computer Science"]
    grad = gpa = start = ""

    if edu_entries:
        e0 = edu_entries[0]
        school = e0.get("school", school)
        campus = e0.get("campus", campus)
        degree = e0.get("degree", degree)
        fos = e0.get("field_of_study")
        if isinstance(fos, list) and fos:
            field_order = fos
        elif isinstance(fos, str) and fos:
            field_order = [fos] + [f for f in field_order if f != fos]
        grad = e0.get("graduation_date", "")
        gpa = e0.get("gpa", "")
        start = e0.get("start_date", "")

    field_line = " -> ".join(f'"{f}"' for f in field_order)
    lines = [
        "== EDUCATION (fill exactly like this — do NOT guess) ==",
        f"School / University: {school}",
        f"  - If asked to pick a specific campus/location, choose: {campus}",
        f"Degree: {degree}  (if the form's options use full words, pick \"Bachelor of Science\" or \"Bachelor's Degree\")",
        f"Field of Study / Major: choose the FIRST option the form offers, in this order: {field_line}",
    ]
    if start:
        lines.append(f"Start date / attended from: {start}")
    if grad:
        lines.append(f"Graduation / end date: {grad}")
    if gpa:
        lines.append(f"GPA (only if asked): {gpa}")
    for e in edu_entries[1:]:
        lines.append(
            f"Also list if multiple entries allowed: {e.get('degree','')} "
            f"{e.get('field_of_study','')}, {e.get('school','')} {e.get('graduation_date','')}".strip()
        )
    return "\n".join(lines)


def _build_hard_rules(profile: dict) -> str:
    """Build the hard rules section with work auth and name from profile."""
    personal = profile["personal"]
    work_auth = profile["work_authorization"]

    full_name = personal["full_name"]
    preferred_name = personal.get("preferred_name", full_name.split()[0])
    preferred_last = full_name.split()[-1] if " " in full_name else ""
    display_name = f"{preferred_name} {preferred_last}".strip() if preferred_last else preferred_name

    # Build work auth rule dynamically
    auth_info = work_auth.get("legally_authorized_to_work", "")
    sponsorship = work_auth.get("require_sponsorship", "")
    permit_type = work_auth.get("work_permit_type", "")

    work_auth_rule = "Work auth: Answer truthfully from profile."
    if permit_type:
        work_auth_rule = f"Work auth: {permit_type}. Sponsorship needed: {sponsorship}."

    name_rule = f'Name: Legal name = {full_name}.'
    if preferred_name and preferred_name != full_name.split()[0]:
        name_rule += f' Preferred name = {preferred_name}. Use "{display_name}" unless a field specifically says "legal name".'

    return f"""== HARD RULES (never break these) ==
1. Never lie about: citizenship, work authorization, criminal history, education credentials, security clearance, licenses.
2. {work_auth_rule}
3. {name_rule}"""


def _build_captcha_section() -> str:
    """Build the CAPTCHA detection and solving instructions.

    Reads the CapSolver API key from environment. The CAPTCHA section
    contains no personal data -- it's the same for every user.
    """
    config.load_env()
    capsolver_key = os.environ.get("CAPSOLVER_API_KEY", "")

    return f"""== CAPTCHA ==
You solve CAPTCHAs via the CapSolver REST API. No browser extension. You control the entire flow.
API key: {capsolver_key or 'NOT CONFIGURED — skip to MANUAL FALLBACK for all CAPTCHAs'}
API base: https://api.capsolver.com

CRITICAL RULE: When ANY CAPTCHA appears (hCaptcha, reCAPTCHA, Turnstile -- regardless of what it looks like visually), you MUST:
1. Run CAPTCHA DETECT to get the type and sitekey
2. Run CAPTCHA SOLVE (createTask -> poll -> inject) with the CapSolver API
3. ONLY go to MANUAL FALLBACK if CapSolver returns errorId > 0
Do NOT skip the API call based on what the CAPTCHA looks like. CapSolver solves CAPTCHAs server-side -- it does NOT need to see or interact with images, puzzles, or games. Even "drag the pipe" or "click all traffic lights" hCaptchas are solved via API token, not visually. ALWAYS try the API first.

--- CAPTCHA DETECT ---
Run this browser_evaluate after every navigation, Apply/Submit/Login click, or when a page feels stuck.
IMPORTANT: Detection order matters. hCaptcha elements also have data-sitekey, so check hCaptcha BEFORE reCAPTCHA.

browser_evaluate function: () => {{{{
  const r = {{}};
  const url = window.location.href;
  // 1. hCaptcha (check FIRST -- hCaptcha uses data-sitekey too)
  const hc = document.querySelector('.h-captcha, [data-hcaptcha-sitekey]');
  if (hc) {{{{
    r.type = 'hcaptcha'; r.sitekey = hc.dataset.sitekey || hc.dataset.hcaptchaSitekey;
  }}}}
  if (!r.type && document.querySelector('script[src*="hcaptcha.com"], iframe[src*="hcaptcha.com"]')) {{{{
    const el = document.querySelector('[data-sitekey]');
    if (el) {{{{ r.type = 'hcaptcha'; r.sitekey = el.dataset.sitekey; }}}}
  }}}}
  // hCaptcha in cross-origin iframe: sitekey is in the iframe URL (query or #fragment)
  if (!r.type) {{{{
    for (const fr of document.querySelectorAll('iframe[src*="hcaptcha.com"]')) {{{{
      const m = (fr.src || '').match(/sitekey=([0-9a-fA-F-]{{{{8,}}}})/);
      if (m) {{{{ r.type = 'hcaptcha'; r.sitekey = m[1]; break; }}}}
    }}}}
  }}}}
  // 2. Cloudflare Turnstile
  if (!r.type) {{{{
    const cf = document.querySelector('.cf-turnstile, [data-turnstile-sitekey]');
    if (cf) {{{{
      r.type = 'turnstile'; r.sitekey = cf.dataset.sitekey || cf.dataset.turnstileSitekey;
      if (cf.dataset.action) r.action = cf.dataset.action;
      if (cf.dataset.cdata) r.cdata = cf.dataset.cdata;
    }}}}
  }}}}
  if (!r.type && document.querySelector('script[src*="challenges.cloudflare.com"]')) {{{{
    r.type = 'turnstile_script_only'; r.note = 'Wait 3s and re-detect.';
  }}}}
  // 3. reCAPTCHA v3 (invisible, loaded via render= param)
  if (!r.type) {{{{
    const s = document.querySelector('script[src*="recaptcha"][src*="render="]');
    if (s) {{{{
      const m = s.src.match(/render=([^&]+)/);
      if (m && m[1] !== 'explicit') {{{{ r.type = 'recaptchav3'; r.sitekey = m[1]; }}}}
    }}}}
  }}}}
  // 4. reCAPTCHA v2 (checkbox or invisible)
  if (!r.type) {{{{
    const rc = document.querySelector('.g-recaptcha');
    if (rc) {{{{ r.type = 'recaptchav2'; r.sitekey = rc.dataset.sitekey; }}}}
  }}}}
  if (!r.type && document.querySelector('script[src*="recaptcha"]')) {{{{
    const el = document.querySelector('[data-sitekey]');
    if (el) {{{{ r.type = 'recaptchav2'; r.sitekey = el.dataset.sitekey; }}}}
  }}}}
  // 5. FunCaptcha (Arkose Labs)
  if (!r.type) {{{{
    const fc = document.querySelector('#FunCaptcha, [data-pkey], .funcaptcha');
    if (fc) {{{{ r.type = 'funcaptcha'; r.sitekey = fc.dataset.pkey; }}}}
  }}}}
  if (!r.type && document.querySelector('script[src*="arkoselabs"], script[src*="funcaptcha"]')) {{{{
    const el = document.querySelector('[data-pkey]');
    if (el) {{{{ r.type = 'funcaptcha'; r.sitekey = el.dataset.pkey; }}}}
  }}}}
  if (r.type) {{{{ r.url = url; return r; }}}}
  return null;
}}}}

Result actions:
- null -> no CAPTCHA. Continue normally.
- "turnstile_script_only" -> browser_wait_for time: 3, re-run detect.
- Any other type -> proceed to CAPTCHA SOLVE below.

--- CAPTCHA SOLVE ---
Three steps: createTask -> poll -> inject. Do each as a separate browser_evaluate call.

STEP 1 -- CREATE TASK (copy this exactly, fill in the 3 placeholders):
browser_evaluate function: async () => {{{{
  const r = await fetch('https://api.capsolver.com/createTask', {{{{
    method: 'POST',
    headers: {{{{'Content-Type': 'application/json'}}}},
    body: JSON.stringify({{{{
      clientKey: '{capsolver_key}',
      task: {{{{
        type: 'TASK_TYPE',
        websiteURL: 'PAGE_URL',
        websiteKey: 'SITE_KEY'
      }}}}
    }}}})
  }}}});
  return await r.json();
}}}}

TASK_TYPE values (use EXACTLY these strings):
  hcaptcha     -> HCaptchaTaskProxyLess
  recaptchav2  -> ReCaptchaV2TaskProxyLess
  recaptchav3  -> ReCaptchaV3TaskProxyLess
  turnstile    -> AntiTurnstileTaskProxyLess
  funcaptcha   -> FunCaptchaTaskProxyLess

PAGE_URL = the url from detect result. SITE_KEY = the sitekey from detect result.
For recaptchav3: add "pageAction": "submit" to the task object (or the actual action found in page scripts).
For turnstile: add "metadata": {{"action": "...", "cdata": "..."}} if those were in detect result.

Response: {{"errorId": 0, "taskId": "abc123"}} on success.
If errorId > 0 -> CAPTCHA SOLVE failed. Go to MANUAL FALLBACK.

STEP 2 -- POLL (replace TASK_ID with the taskId from step 1):
Loop: browser_wait_for time: 3, then run:
browser_evaluate function: async () => {{{{
  const r = await fetch('https://api.capsolver.com/getTaskResult', {{{{
    method: 'POST',
    headers: {{{{'Content-Type': 'application/json'}}}},
    body: JSON.stringify({{{{
      clientKey: '{capsolver_key}',
      taskId: 'TASK_ID'
    }}}})
  }}}});
  return await r.json();
}}}}

- status "processing" -> wait 3s, poll again. Max 10 polls (30s).
- status "ready" -> extract token:
    reCAPTCHA: solution.gRecaptchaResponse
    hCaptcha:  solution.gRecaptchaResponse
    Turnstile: solution.token
- errorId > 0 or 30s timeout -> MANUAL FALLBACK.

STEP 3 -- INJECT TOKEN (replace THE_TOKEN with actual token string):

For reCAPTCHA v2/v3:
browser_evaluate function: () => {{{{
  const token = 'THE_TOKEN';
  document.querySelectorAll('[name="g-recaptcha-response"]').forEach(el => {{{{ el.value = token; el.style.display = 'block'; }}}});
  if (window.___grecaptcha_cfg) {{{{
    const clients = window.___grecaptcha_cfg.clients;
    for (const key in clients) {{{{
      const walk = (obj, d) => {{{{
        if (d > 4 || !obj) return;
        for (const k in obj) {{{{
          if (typeof obj[k] === 'function' && k.length < 3) try {{{{ obj[k](token); }}}} catch(e) {{{{}}}}
          else if (typeof obj[k] === 'object') walk(obj[k], d+1);
        }}}}
      }}}};
      walk(clients[key], 0);
    }}}}
  }}}}
  return 'injected';
}}}}

For hCaptcha:
browser_evaluate function: () => {{{{
  const token = 'THE_TOKEN';
  const ta = document.querySelector('[name="h-captcha-response"], textarea[name*="hcaptcha"]');
  if (ta) ta.value = token;
  document.querySelectorAll('iframe[data-hcaptcha-response]').forEach(f => f.setAttribute('data-hcaptcha-response', token));
  const cb = document.querySelector('[data-hcaptcha-widget-id]');
  if (cb && window.hcaptcha) try {{{{ window.hcaptcha.getResponse(cb.dataset.hcaptchaWidgetId); }}}} catch(e) {{{{}}}}
  return 'injected';
}}}}

For Turnstile:
browser_evaluate function: () => {{{{
  const token = 'THE_TOKEN';
  const inp = document.querySelector('[name="cf-turnstile-response"], input[name*="turnstile"]');
  if (inp) inp.value = token;
  if (window.turnstile) try {{{{ const w = document.querySelector('.cf-turnstile'); if (w) window.turnstile.getResponse(w); }}}} catch(e) {{{{}}}}
  return 'injected';
}}}}

For FunCaptcha:
browser_evaluate function: () => {{{{
  const token = 'THE_TOKEN';
  const inp = document.querySelector('#FunCaptcha-Token, input[name="fc-token"]');
  if (inp) inp.value = token;
  if (window.ArkoseEnforcement) try {{{{ window.ArkoseEnforcement.setConfig({{{{data: {{{{blob: token}}}}}}}}) }}}} catch(e) {{{{}}}}
  return 'injected';
}}}}

After injecting: browser_wait_for time: 2, then snapshot.
- Widget gone or green check -> success. Click Submit if needed.
- No change -> click Submit/Verify/Continue button (some sites need it).
- Still stuck -> token may have expired (~2 min lifetime). Re-run from STEP 1.

--- MANUAL FALLBACK ---
You should ONLY be here if CapSolver createTask returned errorId > 0. If you haven't tried CapSolver yet, GO BACK and try it first.
If CapSolver genuinely failed (errorId > 0):
1. Audio challenge: Look for "audio" or "accessibility" button -> click it for an easier challenge.
2. Text/logic puzzles: Solve them yourself. Think step by step. Common tricks: "All but 9 die" = 9 left. "3 sisters and 4 brothers, how many siblings?" = 7.
3. Simple text captchas ("What is 3+7?", "Type the word") -> solve them.
4. All else fails -> Output RESULT:CAPTCHA."""


def build_prompt(job: dict, tailored_resume: str,
                 cover_letter: str | None = None,
                 dry_run: bool = False, worker_id: int = 0) -> str:
    """Build the full instruction prompt for the apply agent.

    Loads the user profile and search config internally. All personal data
    comes from the profile -- nothing is hardcoded.

    Args:
        job: Job dict from the database (must have url, title, site,
             application_url, fit_score, tailored_resume_path).
        tailored_resume: Plain-text content of the tailored resume.
        cover_letter: Optional plain-text cover letter content.
        dry_run: If True, tell the agent not to click Submit.
        worker_id: Numeric worker identifier. The résumé/cover-letter upload
            files are copied to a PER-WORKER directory so concurrent workers
            never overwrite each other's upload before it is submitted.

    Returns:
        Complete prompt string for the AI agent.
    """
    profile = config.load_profile()
    search_config = config.load_search_config()
    personal = profile["personal"]

    # --- Resolve resume PDF path (portable: handles relative + cross-machine paths) ---
    resume_path = job.get("tailored_resume_path")
    if not resume_path:
        raise ValueError(f"No tailored resume for job: {job.get('title', 'unknown')}")

    resume_local = config.resolve_data_path(resume_path)
    src_pdf = resume_local.with_suffix(".pdf").resolve()
    if not src_pdf.exists():
        raise ValueError(f"Resume PDF not found: {src_pdf}")

    # Copy to a clean filename for upload (recruiters see the filename).
    # PER-WORKER dir: with multiple concurrent workers a single shared "current"
    # path races — one worker would overwrite another's résumé before it's
    # uploaded, submitting the WRONG résumé. Each worker gets its own dir.
    full_name = personal["full_name"]
    name_slug = full_name.replace(" ", "_")
    dest_dir = config.APPLY_WORKER_DIR / f"current-{worker_id}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    upload_pdf = dest_dir / f"{name_slug}_Resume.pdf"
    shutil.copy(str(src_pdf), str(upload_pdf))
    pdf_path = str(upload_pdf)

    # --- Cover letter handling ---
    cover_letter_text = cover_letter or ""
    cl_upload_path = ""
    cl_path = job.get("cover_letter_path")
    cl_local = config.resolve_data_path(cl_path)
    if cl_local and cl_local.exists():
        cl_src = cl_local
        # Read text from .txt sibling (PDF is binary)
        cl_txt = cl_src.with_suffix(".txt")
        if cl_txt.exists():
            cover_letter_text = cl_txt.read_text(encoding="utf-8")
        elif cl_src.suffix == ".txt":
            cover_letter_text = cl_src.read_text(encoding="utf-8")
        # Upload must be PDF
        cl_pdf_src = cl_src.with_suffix(".pdf")
        if cl_pdf_src.exists():
            cl_upload = dest_dir / f"{name_slug}_Cover_Letter.pdf"
            shutil.copy(str(cl_pdf_src), str(cl_upload))
            cl_upload_path = str(cl_upload)

    # --- Build all prompt sections ---
    profile_summary = _build_profile_summary(profile)
    location_check = _build_location_check(profile, search_config)
    salary_section = _build_salary_section(profile)
    screening_section = _build_screening_section(profile)
    education_section = _build_education_section(profile)
    hard_rules = _build_hard_rules(profile)
    captcha_section = _build_captcha_section()

    # Additional uploadable documents from the profile (transcripts etc.)
    docs = profile.get("documents", {}) or {}
    doc_lines = []
    if docs.get("transcript_unofficial"):
        doc_lines.append(f"- Unofficial transcript (default for transcript uploads): {docs['transcript_unofficial']}")
    if docs.get("transcript_official"):
        doc_lines.append(f"- Official transcript (only if the form explicitly demands OFFICIAL): {docs['transcript_official']}")
    documents_section = ""
    if doc_lines:
        documents_section = "\n7b. Transcript or education-document upload requested? Use these files (browser_file_upload):\n" + "\n".join("   " + d for d in doc_lines) + "\n   Never skip a required transcript field; upload the unofficial one unless the form says official.\n"

    # Cover letter fallback text
    city = personal.get("city", "the area")
    if not cover_letter_text:
        cl_display = (
            f"None available. Skip if optional. If required, write 2 factual "
            f"sentences: (1) relevant experience from the resume that matches "
            f"this role, (2) available immediately and based in {city}."
        )
    else:
        cl_display = cover_letter_text

    # Phone digits only (for fields with country prefix)
    phone_digits = "".join(c for c in personal.get("phone", "") if c.isdigit())

    # SSO domains the agent cannot sign into (loaded from config/sites.yaml)
    from applypilot.config import load_blocked_sso
    blocked_sso = load_blocked_sso()

    # Preferred display name
    preferred_name = personal.get("preferred_name", full_name.split()[0])
    last_name = full_name.split()[-1] if " " in full_name else ""
    display_name = f"{preferred_name} {last_name}".strip()

    # Company display: prefer the real employer over the aggregator name
    company_display = (job.get("company") or "").strip()
    if not company_display:
        site = job.get("site", "Unknown")
        if (site or "").lower() in ("linkedin", "indeed", "google"):
            company_display = (f"{site} (aggregator source — the real employer "
                               f"is named on the job page itself)")
        else:
            company_display = site

    # Dry-run: override submit instruction
    if dry_run:
        submit_instruction = "IMPORTANT: Do NOT click the final Submit/Apply button. Review the form, verify all fields, then output RESULT:APPLIED CONFIRMATION=\"DRY RUN - not submitted\"."
    else:
        submit_instruction = "BEFORE clicking Submit/Apply, take a snapshot and review EVERY field on the page. Verify all data matches the APPLICANT PROFILE and TAILORED RESUME -- name, email, phone, location, work auth, resume uploaded, cover letter if applicable. If anything is wrong or missing, fix it FIRST. Only click Submit after confirming everything is correct."

    prompt = f"""You are an autonomous job application agent. Your ONE mission: get this candidate an interview. You have all the information and tools. Think strategically. Act decisively. Submit the application.

== JOB ==
URL: {job.get('application_url') or job['url']}
Title: {job['title']}
Company: {company_display}
Fit Score: {job.get('fit_score', 'N/A')}/10

== FILES ==
Resume PDF (upload this): {pdf_path}
Cover Letter PDF (upload if asked): {cl_upload_path or "N/A"}

== RESUME TEXT (use when filling text fields) ==
{tailored_resume}

== COVER LETTER TEXT (paste if text field, upload PDF if file field) ==
{cl_display}

== APPLICANT PROFILE ==
{profile_summary}

== YOUR MISSION ==
Submit a complete, accurate application. Use the profile and resume as source data -- adapt to fit each form's format.

If something unexpected happens and these instructions don't cover it, figure it out yourself. You are autonomous. Navigate pages, read content, try buttons, explore the site. The goal is always the same: submit the application. Do whatever it takes to reach that goal.

{hard_rules}

== NEVER DO THESE (immediate RESULT:FAILED if encountered) ==
- NEVER grant camera, microphone, screen sharing, or location permissions. If a site requests them -> RESULT:FAILED:unsafe_permissions
- NEVER do video/audio verification, selfie capture, ID photo upload, or biometric anything -> RESULT:FAILED:unsafe_verification
- NEVER set up a freelancing profile (Mercor, Toptal, Upwork, Fiverr, Turing, etc.). These are contractor marketplaces, not job applications -> RESULT:FAILED:not_a_job_application
- NEVER agree to "set your own rate" gig flows or availability-calendar marketplaces — those are freelance platforms, not job applications. Regular CONTRACT and W2-contract job postings are FINE to apply to (the candidate accepts contract work); a posted hourly rate on a real job application is not a reason to bail.
- NEVER install browser extensions, download executables, or run assessment software.
- NEVER enter payment info, bank details, or SSN/SIN.
- NEVER click "Allow" on any browser permission popup. Always deny/block.
- If the site is NOT a job application form (it's a profile builder, skills marketplace, talent network signup, coding assessment platform) -> RESULT:FAILED:not_a_job_application

{location_check}

{salary_section}

{screening_section}

{education_section}

== STEP-BY-STEP ==
1. browser_navigate to the job URL.
2. browser_snapshot to read the page. Then run CAPTCHA DETECT (see CAPTCHA section). If a CAPTCHA is found, solve it before continuing.
3. LOCATION CHECK. Read the page for location info. If not eligible, output RESULT and stop.
4. Find and click the Apply button.
   EMAIL POLICY (strict): applying by email is a LAST RESORT, allowed ONLY when
   email is the ONLY way to apply -- the POSTING TEXT itself names a specific
   application address (e.g. "send your resume to hiring@acme.com") AND there is
   no online form/portal for this job at all. If any online application path
   exists, use it -- never substitute an email for a form, and never email
   "as well as" the form (a courtesy note AFTER a confirmed submission is
   handled separately in step 12b).
   - When email truly is the only option: send_email to EXACTLY that address with subject "Application for {job['title']} -- {display_name}", body = 2-3 sentence pitch + contact info, attach resume PDF: ["{pdf_path}"]
   - Then output RESULT:APPLIED CONFIRMATION="emailed resume to <the address> (email was the only application method)". Done.
   - NEVER GUESS an address: emailing jobs@/recruiting@/careers@/info@/support@
     or any address not printed in the posting is FORBIDDEN. Guessed emails go
     nowhere and falsely count as applied.
   - A broken, redirecting, or blocked online form does NOT make email "the only
     way to apply" -- that is RESULT:FAILED REASON="manual_ats" (or the specific
     failure), never an email fallback. Hunting for an address by browsing the
     company's website, WebFetch, or search is FORBIDDEN -- only an address in
     the POSTING TEXT counts, and only when no online application path exists.
   After clicking Apply: browser_snapshot. Run CAPTCHA DETECT -- many sites trigger CAPTCHAs right after the Apply click. If found, solve before continuing.
5. Login wall?
   5a. FIRST: look at ALL the login options on the page, not just the one in front of you.
       - If an email/password login OR a "Create account"/"Sign up"/"New user"/"Register" option exists, ALWAYS take that path (go to 5c) -- EVEN IF a "Continue with Google/Microsoft/LinkedIn" button is also shown. Never click an SSO/OAuth button when an email path exists; the email path (create account -> email verification in 5e/5f) is expected and you can complete it.
       - GOOGLE-ONLY RIDE-THROUGH: if OAuth is the SOLE way in (no email/password field and no create-account link anywhere on the page) AND the only provider offered is GOOGLE (a "Continue with Google" / "Sign in with Google" button, or one that navigates to accounts.google.com), you MAY click it and ride an ALREADY-SIGNED-IN Google session instead of bailing. Steps:
         * Click "Continue with Google". If an account chooser appears and it lists the Jane Google account (applications@example.com), click THAT account. If clicking through goes straight to a consent / "Continue" / "Allow" / "continue to <site>" screen for that account, approve it (click Continue / Allow / Confirm).
         * After approving consent, return to the ATS/application tab: browser_tabs action "list", select the application tab (re-navigate to the job URL if you were bounced), and RESUME the application from where it left off (earlier form data may be gone -- refill).
         * ABSOLUTE GUARDRAILS (these override the mission -- obey them without exception): NEVER type a Google password. NEVER approve, solve, or complete a 2FA / one-time code / "verify it's you" / "enter the code we sent" / device-confirmation / phone / authenticator challenge. If Google shows a password field, ANY 2FA or identity-verification prompt, asks you to choose an account and applications@example.com is NOT already listed and signed in, or is otherwise not already authenticated -> STOP IMMEDIATELY and output RESULT:FAILED:sso_required. Do not try to sign in, do not enter any credential, do not request a new code.
       - For any NON-Google sole provider ({', '.join(blocked_sso)}, LinkedIn, or any other) -> do NOT attempt it. Output RESULT:FAILED:sso_required.
       - Output RESULT:FAILED:sso_required whenever OAuth is the SOLE way in and you cannot complete it under the rules above -- i.e. there is no email/password field and no create-account link anywhere, and either the provider is not Google or (for Google) the Jane account is not already signed in. Never type Google/Microsoft/SSO credentials yourself.
   5b. Check for popups. Run browser_tabs action "list". If a new tab/window appeared (login popup), switch to it with browser_tabs action "select". If it's an SSO/OAuth popup BUT the underlying page still offers an email or create-account path, close the popup and use that email path (5c). If OAuth is the sole option AND it is a GOOGLE popup (accounts.google.com), apply the GOOGLE-ONLY RIDE-THROUGH from 5a: ride an already-signed-in Jane session (applications@example.com) by selecting that account and approving consent ONLY -- NEVER a password, 2FA, or "verify it's you" challenge; if any of those appear or the Jane account is not already signed in -> RESULT:FAILED:sso_required. For a non-Google sole-option popup -> RESULT:FAILED:sso_required.
   5c. Regular login form (employer's own site)? Try sign in: {personal['email']} / {personal.get('password', '')}. If it says the password is wrong/incorrect on an EXISTING account, try this alternate password before concluding sign-in failed: {personal.get('password_alt', '')} (the account may have been created earlier with a strengthened password).
   5d. After clicking Login/Sign-in: run CAPTCHA DETECT. Login pages frequently have invisible CAPTCHAs that silently block form submissions. If found, solve it then retry login.
   5e. Sign in failed (wrong password / "no account found")? CREATE AN ACCOUNT: find "Create account" / "Sign up" / "New user" / "Register", use {personal['email']} and password {personal.get('password', '')}. If the site REJECTS that password as too weak/insecure or not meeting complexity rules, use this stronger alternate instead: {personal.get('password_alt', '')}. Many ATS (Workday, iCIMS) REQUIRE an account before you can apply -- creating one is expected, not a failure. Fill any name fields with {display_name}.
   5e2. EXISTING account but BOTH passwords rejected (or "account already exists" blocks sign-up)? RESET THE PASSWORD -- do not give up. The account belongs to the candidate; resetting it is expected and allowed:
       - Click "Forgot password" / "Reset password" / "Trouble signing in", submit {personal['email']}.
       - Wait ~10s, then search_emails (from this employer/ATS, subject like "reset", "password", "recover") and read_email the newest one. Follow the reset LINK (browser_navigate; tracking-wrapped URLs are fine) or use the reset CODE if one is given.
       - Set the new password to {personal.get('password_alt', '')} (the strong one), then sign in with it and RESUME the application.
       - No reset email after 4 waits over ~1 minute? Only then continue to 5h.
   5f. Email verification (very common right after sign-up -- do this, don't give up). You MUST get past every verification method; treat this as a core part of applying, not an obstacle:
       - Wait ~10s for the email, then search_emails (from the employer/ATS, subject like "verify", "confirm", "activate", "welcome", "security code") and read_email the newest one.
       - CODE/OTP in the email -> type it into the verification field.
       - VERIFICATION LINK and no code -> extract the URL and browser_navigate to it. Finding the URL:
         * The email body may be HTML. The link is the href behind text like "Verify email"/"Confirm"/"Activate" -- look for http(s) URLs in the body, including long tracking-wrapped ones (click.*/ls/click?upn=..., *.sendgrid.net, mandrillapp, awstrack). Tracking redirects are FINE to navigate to; they forward to the real verification page.
         * If several URLs appear, prefer ones containing verify/confirm/activate/token/email. Ignore unsubscribe/privacy/social links.
         * If read_email shows no URL at all, re-read the email and scan the raw HTML for href=.
       - After navigating the link: the page usually says "verified/confirmed" and may open a NEW logged-in session. Do NOT stop there -- browser_tabs "list", close extras, navigate back to the job's application URL, log in if asked, and RESUME the application from where it left off (your earlier form data may be gone; refill).
       - No email yet? browser_wait_for time: 10 and search again (try up to 4 times over ~1 minute) before giving up. Verification emails sometimes take 30-60s.
   5g. After login/verification, run browser_tabs action "list" again. Switch back to the application tab if needed. If you got bounced away from the form, re-navigate to the job URL and click Apply again.
   5h. Only after genuinely exhausting 5c–5g INCLUDING the password reset in 5e2 -> RESULT:FAILED:login_issue. Do not loop the same failed step more than twice.
6. Upload resume. ALWAYS upload fresh -- delete any existing resume first, then browser_file_upload with the PDF path above. This is the tailored resume for THIS job. Non-negotiable.
7. Upload cover letter if there's a field for it. Text field -> paste the cover letter text. File upload -> use the cover letter PDF path.
{documents_section}
8. Check ALL pre-filled fields. ATS systems parse your resume and auto-fill -- it's often WRONG.
   - "Current Job Title" or "Most Recent Title" -> use the title line at the top of the TAILORED RESUME (second header line), NOT whatever the parser guessed.
   - Work experience "Description" / "Responsibilities" / "What did you do in this role?" fields -> DO NOT leave blank and DO NOT invent duties. Fill them with the actual bullet points for that role from the TAILORED RESUME TEXT above (copy the bullets; condense to fit if there's a character limit). One field per role, matched to the right company.
   - Preferred name: if there's an "I have a preferred name" checkbox/toggle, enable it and enter the preferred name. If there's a preferred-name text field, fill it. (See HARD RULES for the name.)
   - Education fields -> fill using the EDUCATION section exactly (school, campus, degree, field of study). Do not let the resume parser's guess stand if it differs.
   - Compare every other field to the APPLICANT PROFILE. Fix mismatches. Fill empty fields.
9. Answer screening questions using the rules above.
10. {submit_instruction}
11. After submit: browser_snapshot. Run CAPTCHA DETECT -- submit buttons often trigger invisible CAPTCHAs. If found, solve it (the form will auto-submit once the token clears, or you may need to click Submit again). Then check for new tabs (browser_tabs action: "list"). Switch to newest, close old. Take a snapshot AND a screenshot to confirm.
11b. SECURITY CODE GATE (Greenhouse and others): if the page now asks for a "security code" that was emailed to you, the application is NOT submitted yet. Use search_emails (subject contains "security code" or "code", newest first), read_email to get the code, type it into the field, and RESUBMIT. Repeat once if a fresh code is sent. Never claim APPLIED while a code prompt is on screen.
12. CONFIRM THE SUBMISSION BEFORE CLAIMING SUCCESS. Only output RESULT:APPLIED if you can SEE explicit proof on the page: a confirmation/thank-you message ("Thank you for applying", "Application submitted/received", "We received your application"), a confirmation/reference number, OR a redirect to a "application complete" page.
   - MANDATORY FORMAT: RESULT:APPLIED CONFIRMATION="<the exact confirmation text copied from the page>" -- on one line. The system REJECTS any APPLIED claim without this quote and counts the job as FAILED. You cannot pass this check by describing your intentions; only text you actually see on the final page counts.
   - If you clicked Submit but see NO such confirmation (still on the form, blank page, or unsure) -> the application probably did NOT go through. Output RESULT:FAILED:unconfirmed. Do NOT claim APPLIED on hope.
   - If validation errors appeared, fix them and resubmit; only then re-check for confirmation.
12b. BONUS OUTREACH (optional, only AFTER a confirmed successful submission, BEFORE you output the RESULT line): if the POSTING TEXT itself prints the email address of a specific named person (a hiring manager or recruiter, e.g. "questions? contact jane.doe@acme.com"), send ONE short courtesy note to that exact address: subject "Applied: {job['title']} -- {display_name}", body = 2-3 sentences saying you just submitted your application through the official process, one line on why you fit, and your contact info. Attach the resume PDF: ["{pdf_path}"].
   - The address must be PRINTED in the posting and belong to a person. NEVER guess or construct one, and NEVER use generic addresses (jobs@/recruiting@/careers@/info@/support@) -- those get no outreach.
   - This is a bonus, not part of the application: skip it freely when no personal address is printed, and if the email fails, ignore the failure. It never changes the RESULT line.
RESULT:APPLIED CONFIRMATION="<exact text from confirmation page>" -- submitted AND proof visible (claims without the CONFIRMATION quote are auto-rejected)
RESULT:FAILED:unconfirmed -- clicked submit but no confirmation seen (do not count as applied)
RESULT:EXPIRED -- job closed or no longer accepting applications
RESULT:CAPTCHA -- blocked by unsolvable captcha
RESULT:LOGIN_ISSUE -- could not sign in or create account
RESULT:FAILED:not_eligible_location -- onsite outside acceptable area, no remote option
RESULT:FAILED:not_eligible_work_auth -- requires unauthorized work location
RESULT:FAILED:reason -- any other failure (brief reason)

== BROWSER EFFICIENCY ==
- browser_snapshot ONCE per page to understand it. Then use browser_take_screenshot to check results (10x less memory).
- Only snapshot again when you need element refs to click/fill.
- Multi-page forms (Workday, Taleo, iCIMS): snapshot each new page, fill all fields, click Next/Continue. Repeat until final review page.
- Fill ALL fields in ONE browser_fill_form call. Not one at a time.
- Keep your thinking SHORT. Don't repeat page structure back.
- CAPTCHA AWARENESS: After any navigation, Apply/Submit/Login click, or when a page feels stuck -- run CAPTCHA DETECT (see CAPTCHA section). Invisible CAPTCHAs (Turnstile, reCAPTCHA v3) show NO visual widget but block form submissions silently. The detect script finds them even when invisible.

== FORM TRICKS ==
- Popup/new window opened? browser_tabs action "list" to see all tabs. browser_tabs action "select" with the tab index to switch. ALWAYS check for new tabs after clicking login/apply/sign-in buttons.
- "Upload your resume" pre-fill page (Workday, Lever, etc.): This is NOT the application form yet. Click "Select file" or the upload area, then browser_file_upload with the resume PDF path. Wait for parsing to finish. Then click Next/Continue to reach the actual form.
- File upload not working? Try: (1) browser_click the upload button/area, (2) browser_file_upload with the path. If still failing, look for a hidden file input or a "Select file" link and click that first.
- Dropdown won't fill? browser_click to open it, then browser_click the option.
- Checkbox won't check via fill_form? Use browser_click on it instead. Snapshot to verify.
- Phone field with country prefix: just type digits {phone_digits}
- Date fields: {datetime.now().strftime('%m/%d/%Y')}
- Validation errors after submit? Take BOTH snapshot AND screenshot. Snapshot shows text errors, screenshot shows red-highlighted fields. Fix all, retry.
- Honeypot fields (hidden, "leave blank"): skip them.
- Format-sensitive fields: read the placeholder text, match it exactly.

{captcha_section}

== WHEN TO GIVE UP ==
- Same page after 3 attempts with no progress -> RESULT:FAILED:stuck
- RESULT:EXPIRED ONLY if the page EXPLICITLY says the job is closed/filled/"no longer accepting applications"/"this position has been filled". A blank page, slow load, redirect, cookie/consent wall, or a tracking-parameter landing page is NOT expired -> browser_wait_for time: 5, reload the URL once, and if there's an Apply button, proceed. Do not call EXPIRED just because the page looks empty at first.
- Page is genuinely broken/500 error/blank after a reload -> RESULT:FAILED:page_error
Stop immediately. Output your RESULT code. Do not loop."""

    return prompt
