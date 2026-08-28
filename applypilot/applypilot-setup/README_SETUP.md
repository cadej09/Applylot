# ApplyPilot — Your Setup

Everything here is pre-filled from your resume and the preferences you gave me
(OpenAI as the AI provider, dry-run first, Greater Seattle Area + remote-US).

## What's in this folder

| File | What it is |
|------|-----------|
| `profile.json` | Your contact info, work authorization (authorized, no sponsorship), salary ($80k–$120k, expecting ~$95k), skills, and experience facts. |
| `searches.yaml` | Job titles (data analyst / BI / analytics / data scientist / data engineer), Seattle-area + remote-US locations, and excluded senior/clearance roles. |
| `.env` | Your OpenAI key goes here. Model set to `gpt-4o-mini`. |
| `resume.txt` | Plain-text resume used for scoring + tailoring. |
| `resume.pdf` | A clean PDF fallback for uploads (the tool also generates a tailored PDF per job). |
| `setup.sh` | Copies all of the above into `~/.applypilot/` where ApplyPilot looks for them. |

## Steps to run (on your Mac)

**1. Add your OpenAI API key.** Open `.env` in this folder and replace
`PASTE_YOUR_OPENAI_API_KEY_HERE` with your real key (get one at
https://platform.openai.com/api-keys). No quotes, no spaces.

**2. Install the config:**
```bash
bash ~/ApplyPilot/applypilot-setup/setup.sh
```

**3. Install ApplyPilot itself (if you haven't):**
```bash
pip install applypilot
pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex
```

**4. Verify everything:**
```bash
applypilot doctor
```
This tells you what's installed and what's missing (Node.js, Chrome, Claude Code CLI
are only needed for the auto-apply stage).

**5. Run the discovery → scoring → tailoring pipeline:**
```bash
applypilot run
```
This finds jobs, scores them 1–10 against your profile, and writes a tailored
resume + cover letter for the good ones. Nothing is submitted in this step.

**6. Review, then auto-apply in DRY-RUN first (recommended):**
```bash
applypilot apply --dry-run
```
This opens Chrome and fills out the forms **without submitting** so you can watch
exactly what it does. When you're comfortable:
```bash
applypilot apply
```

## Notes & things to double-check
- **Review the scored jobs before applying.** `applypilot status` and
  `applypilot dashboard` show what it found and plans to apply to.
- `profile.json` leaves your **street address** and **postal code** blank (not on
  your resume). Some forms require them — fill those two fields in if you want
  fewer manual interventions.
- `password` in `profile.json` is blank. If you want it to log into job sites that
  require an account, add the password you use there.
- The title filter currently **excludes "senior/lead/staff/manager"** etc. so you
  get entry/mid roles. Loosen `exclude_titles` in `searches.yaml` if you want more.
- Auto-apply submits real applications under your name. Keep it on `--dry-run`
  until you've seen a few go through correctly.
