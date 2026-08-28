"""Text-to-PDF conversion for tailored resumes and cover letters.

Parses the structured text resume format, renders via an HTML/CSS template,
and exports to PDF using headless Chromium via Playwright.
"""

import logging
from pathlib import Path

from applypilot.config import TAILORED_DIR

log = logging.getLogger(__name__)


# ── Resume Parser ────────────────────────────────────────────────────────

def parse_resume(text: str) -> dict:
    """Parse a structured text resume into sections.

    Expects a format with header lines (name, title, location, contact)
    followed by ALL-CAPS section headers (EDUCATION, TECHNICAL SKILLS, etc.).

    Args:
        text: Full resume text.

    Returns:
        {"name": str, "title": str, "location": str, "contact": str, "sections": dict}
    """
    lines = [line.rstrip() for line in text.strip().split("\n")]

    # Header: first few lines before the first section header
    known_headers = {"SUMMARY", "EDUCATION", "TECHNICAL SKILLS", "SKILLS",
                     "EXPERIENCE", "PROJECTS"}
    header_lines: list[str] = []
    body_start = 0
    for i, line in enumerate(lines):
        if line.strip().upper() in known_headers:
            body_start = i
            break
        if line.strip():
            header_lines.append(line.strip())

    name = header_lines[0] if len(header_lines) > 0 else ""
    title = header_lines[1] if len(header_lines) > 1 else ""
    # The header may have 3 or 4 lines depending on whether location is included
    location = ""
    contact = ""
    if len(header_lines) > 3:
        location = header_lines[2]
        contact = header_lines[3]
    elif len(header_lines) > 2:
        # Could be location or contact -- check for email/phone indicators
        if "@" in header_lines[2] or "|" in header_lines[2]:
            contact = header_lines[2]
        else:
            location = header_lines[2]

    # Split body into sections by ALL-CAPS headers
    sections: dict[str, str] = {}
    current_section: str | None = None
    current_lines: list[str] = []

    for line in lines[body_start:]:
        stripped = line.strip()
        # Section headers are the known ALL-CAPS titles only \u2014 a generic
        # all-caps test misreads subtitle lines like "SQL | 2024" as headers.
        if stripped.upper() in known_headers:
            if current_section:
                sections[current_section] = "\n".join(current_lines).strip()
            current_section = stripped
            current_lines = []
        else:
            current_lines.append(line)

    if current_section:
        sections[current_section] = "\n".join(current_lines).strip()

    return {
        "name": name,
        "title": title,
        "location": location,
        "contact": contact,
        "sections": sections,
    }


def parse_skills(text: str) -> list[tuple[str, str]]:
    """Parse skills section into (category, value) pairs.

    Args:
        text: The TECHNICAL SKILLS section text.

    Returns:
        List of (category_name, skills_string) tuples.
    """
    skills: list[tuple[str, str]] = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if ":" in line:
            cat, val = line.split(":", 1)
            skills.append((cat.strip(), val.strip()))
    return skills


def parse_entries(text: str) -> list[dict]:
    """Parse experience/project entries from section text.

    Args:
        text: The EXPERIENCE or PROJECTS section text.

    Returns:
        List of {"title": str, "subtitle": str, "bullets": list[str]} dicts.
    """
    entries: list[dict] = []
    lines = text.strip().split("\n")
    current: dict | None = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- ") or stripped.startswith("\u2022 "):
            if current:
                current["bullets"].append(stripped[2:].strip())
        elif current is None or (
            not stripped.startswith("-")
            and not stripped.startswith("\u2022")
            and len(current.get("bullets", [])) > 0
        ):
            # New entry
            if current:
                entries.append(current)
            current = {"title": stripped, "subtitle": "", "bullets": []}
        elif current and not current["subtitle"]:
            current["subtitle"] = stripped
        else:
            if current:
                current["bullets"].append(stripped)

    if current:
        entries.append(current)

    return entries


# ── HTML Template (LaTeX-style: serif, small-caps sections, titlerule) ───

import re as _re

_DATE_RE = _re.compile(
    r"((19|20)\d{2}|present|current|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)",
    _re.IGNORECASE,
)


def _md_bold(text: str) -> str:
    """Convert **bold** markdown spans to <b> tags."""
    return _re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)


def _split_dates(line: str) -> tuple[str, str]:
    """Split 'Title | Company | Jun 2024 - Apr 2025' into (left, right-dates).

    The last '|'-separated segment is treated as the date column when it
    looks date-like; otherwise everything stays on the left.
    """
    parts = [p.strip() for p in line.split("|")]
    if len(parts) > 1 and _DATE_RE.search(parts[-1]) and len(parts[-1]) < 40:
        return " | ".join(parts[:-1]), parts[-1]
    return line, ""


def _entry_header(title: str, subtitle: str) -> str:
    """Two-column entry header: bold title left / dates right, italic subtitle left / location right."""
    t_left, t_right = _split_dates(title)
    s_left, s_right = _split_dates(subtitle) if subtitle else ("", "")
    html = f'<div class="row"><span class="etitle">{_md_bold(t_left)}</span><span class="edate">{t_right}</span></div>'
    if subtitle:
        html += f'<div class="row"><span class="esub">{s_left}</span><span class="esub">{s_right}</span></div>'
    return html


def build_html(resume: dict) -> str:
    """Build a LaTeX-resume-style HTML page from parsed data.

    Design matches the user's preferred LaTeX resume: Computer-Modern-like
    serif, huge centered name, pipe-separated contact line, small-caps
    section titles over a hairline rule, two-column entry headers with
    right-aligned dates, tight one-page spacing.
    """
    sections = resume["sections"]

    def entries_html(section_name: str, css_class: str) -> str:
        if section_name not in sections:
            return ""
        items = ""
        for e in parse_entries(sections[section_name]):
            bullets = "".join(f"<li>{_md_bold(b)}</li>" for b in e["bullets"])
            items += f'<div class="entry">{_entry_header(e["title"], e["subtitle"])}<ul>{bullets}</ul></div>'
        pretty = section_name.title().replace("Technical Skills", "Skills")
        return f'<div class="section"><div class="stitle">{pretty}</div>{items}</div>'

    # Skills: "Category: a, b, c" rows, no bullets
    skills_html = ""
    for key in ("TECHNICAL SKILLS", "SKILLS"):
        if key in sections:
            rows = "".join(
                f'<div class="skill-row"><b>{cat}:</b> {val}</div>'
                for cat, val in parse_skills(sections[key])
            )
            skills_html = f'<div class="section"><div class="stitle">Skills</div>{rows}</div>'
            break

    # Education: entry-style if parseable, plain text otherwise
    edu_html = ""
    if "EDUCATION" in sections:
        edu_entries = parse_entries(sections["EDUCATION"])
        if edu_entries and edu_entries[0]["title"]:
            items = ""
            for e in edu_entries:
                bullets = "".join(f"<li>{_md_bold(b)}</li>" for b in e["bullets"])
                blist = f"<ul>{bullets}</ul>" if bullets else ""
                items += f'<div class="entry">{_entry_header(e["title"], e["subtitle"])}{blist}</div>'
            edu_html = f'<div class="section"><div class="stitle">Education</div>{items}</div>'
        else:
            edu_html = (
                f'<div class="section"><div class="stitle">Education</div>'
                f'<div class="plain">{sections["EDUCATION"].strip()}</div></div>'
            )

    summary_html = ""
    if "SUMMARY" in sections and sections["SUMMARY"].strip():
        summary_html = (
            f'<div class="section"><div class="stitle">Summary</div>'
            f'<div class="plain">{_md_bold(sections["SUMMARY"].strip())}</div></div>'
        )

    contact = resume["contact"]
    contact_parts = [p.strip() for p in contact.split("|")] if contact else []
    if resume["location"]:
        contact_parts.insert(0, resume["location"])
    contact_html = " &nbsp;|&nbsp; ".join(contact_parts)

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
@page {{
    size: letter;
    margin: 0.4in 0.5in;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    font-family: 'Times New Roman', 'Nimbus Roman', Georgia, serif;
    font-size: 10pt;
    line-height: 1.25;
    color: #000;
}}
.header {{ text-align: center; margin-bottom: 6px; }}
.name {{ font-size: 22pt; font-weight: 700; letter-spacing: 0.3px; }}
.contact {{ font-size: 9.5pt; margin-top: 2px; }}
.contact a {{ color: #000; text-decoration: none; }}
.section {{ margin-top: 6px; }}
.stitle {{
    font-size: 11.5pt;
    font-variant: small-caps;
    letter-spacing: 0.4px;
    border-bottom: 0.75pt solid #000;
    padding-bottom: 1px;
    margin-bottom: 3px;
}}
.row {{ display: flex; justify-content: space-between; align-items: baseline; }}
.etitle {{ font-weight: 700; font-size: 10pt; }}
.edate {{ font-size: 10pt; white-space: nowrap; padding-left: 8px; }}
.esub {{ font-size: 9.5pt; font-style: italic; }}
.entry {{ margin-bottom: 4px; break-inside: avoid; }}
ul {{ margin-left: 16px; padding: 0; margin-top: 1px; }}
li {{ font-size: 9.7pt; margin-bottom: 1px; line-height: 1.28; }}
li::marker {{ font-size: 7pt; }}
.skill-row {{ font-size: 9.7pt; line-height: 1.35; }}
.plain {{ font-size: 9.7pt; }}
</style>
</head>
<body>
<div class="header">
    <div class="name">{resume['name']}</div>
    <div class="contact">{contact_html}</div>
</div>
{summary_html}
{edu_html}
{skills_html}
{entries_html("EXPERIENCE", "exp")}
{entries_html("PROJECTS", "proj")}
</body>
</html>"""


def build_letter_html(text: str) -> str:
    """Render a plain letter (cover letter — no section headers) as paragraphs.

    Cover letters used to go through build_html, whose resume header logic
    swallowed the first/last paragraphs (every pre-2026-07-20 _CL.pdf shipped
    without its opening hook or sign-off).
    """
    import html as _htmlmod
    paras = [p.strip() for p in _re.split(r"\n\s*\n", text.strip()) if p.strip()]
    body = "".join(f"<p>{_md_bold(_htmlmod.escape(p))}</p>" for p in paras)
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
@page {{
    size: letter;
    margin: 0.75in 0.85in;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    font-family: 'Times New Roman', 'Nimbus Roman', Georgia, serif;
    font-size: 11pt;
    line-height: 1.45;
    color: #000;
}}
p {{ margin-bottom: 11px; }}
</style>
</head>
<body>
{body}
</body>
</html>"""


# ── PDF Renderer ─────────────────────────────────────────────────────────

def render_pdf(html: str, output_path: str, one_page: bool = True) -> None:
    """Render HTML to PDF using Playwright's headless Chromium.

    Args:
        html: Complete HTML string.
        output_path: Path to write the PDF file.
        one_page: Shrink content uniformly (down to 72%) so it always fits
            a single letter page — the user's hard rule for resumes.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="networkidle")
        if one_page:
            # HARD one-page rule: shrink exactly as much as needed, no floor.
            # (The old 0.72 floor let >39%-oversized resumes spill to page 2.)
            # Usable height = 11in minus 0.8in vertical margins, at 96 CSS px/in.
            zoom = page.evaluate(
                """() => {
                    const usable = (11 - 0.8) * 96;
                    const h = document.body.scrollHeight;
                    if (h > usable) {
                        const z = usable / h;
                        document.body.style.zoom = z;
                        return z;
                    }
                    return 1;
                }"""
            )
            if zoom < 0.8:
                log.warning(
                    "One-page shrink at %.0f%% — content is oversized; "
                    "tailor prompt length budget may need tightening (%s)",
                    zoom * 100, output_path,
                )
        page.pdf(
            path=output_path,
            format="Letter",
            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
            print_background=True,
        )
        browser.close()


# ── Public API ───────────────────────────────────────────────────────────

def convert_to_pdf(
    text_path: Path, output_path: Path | None = None, html_only: bool = False
) -> Path:
    """Convert a text resume/cover letter to PDF.

    Args:
        text_path: Path to the .txt file to convert.
        output_path: Optional override for the output path. Defaults to same
            name with .pdf extension.
        html_only: If True, output HTML instead of PDF.

    Returns:
        Path to the generated PDF (or HTML) file.
    """
    text_path = Path(text_path)
    text = text_path.read_text(encoding="utf-8")
    resume = parse_resume(text)
    # No recognizable resume sections = it's a letter, not a resume.
    if resume["sections"]:
        html = build_html(resume)
    else:
        html = build_letter_html(text)

    if html_only:
        out = output_path or text_path.with_suffix(".html")
        out = Path(out)
        out.write_text(html, encoding="utf-8")
        log.info("HTML generated: %s", out)
        return out

    out = output_path or text_path.with_suffix(".pdf")
    out = Path(out)
    render_pdf(html, str(out))
    log.info("PDF generated: %s", out)
    return out


def batch_convert(limit: int = 50) -> int:
    """Convert .txt files in TAILORED_DIR that don't have corresponding PDFs.

    Scans for .txt files (excluding _JOB.txt and _REPORT.json), checks if a
    .pdf with the same stem already exists, and converts any that are missing.

    Args:
        limit: Maximum number of files to convert.

    Returns:
        Number of PDFs generated.
    """
    if not TAILORED_DIR.exists():
        log.warning("Tailored directory does not exist: %s", TAILORED_DIR)
        return 0

    txt_files = sorted(TAILORED_DIR.glob("*.txt"))
    # Exclude _JOB.txt and _CL.txt files from resume conversion
    # (they get their own conversion calls)
    candidates = [
        f for f in txt_files
        if not f.name.endswith("_JOB.txt")
    ]

    # Filter to those without a corresponding PDF
    to_convert: list[Path] = []
    for f in candidates:
        pdf_path = f.with_suffix(".pdf")
        if not pdf_path.exists():
            to_convert.append(f)
        if len(to_convert) >= limit:
            break

    if not to_convert:
        log.info("All text files already have PDFs.")
        return 0

    log.info("Converting %d files to PDF...", len(to_convert))
    converted = 0
    for f in to_convert:
        try:
            convert_to_pdf(f)
            converted += 1
        except Exception as e:
            log.error("Failed to convert %s: %s", f.name, e)

    log.info("Done: %d/%d PDFs generated in %s", converted, len(to_convert), TAILORED_DIR)
    return converted
