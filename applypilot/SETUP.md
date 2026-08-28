# ApplyPilot — Setting up on a new machine

Two halves make a working install:

1. **Code** — this repo. No secrets, no personal data.
2. **Personal data** — `~/.applypilot/` (profile.json, resume.txt, searches.yaml,
   .env with API keys, the SQLite DB, generated resumes/covers). Cade's copy
   syncs to the private `applypilot-data` repo; a new user creates their own.

---

## Prerequisites (any OS)

- Python 3.11+ and git
- Google Chrome (the apply agent drives a real Chrome via CDP)
- Node.js 18+ (`npx` is used to launch the Playwright + Gmail MCP servers)
- **Claude Code CLI**, logged in to a Claude subscription (Pro or Max) —
  this powers the apply agent: `npm install -g @anthropic-ai/claude-code`, then `claude` once to log in
- *(Optional)* Ollama + `llama3.1:8b` — last-resort local LLM fallback.
  Scoring runs on free cloud tiers (NVIDIA NIM / Groq) so this is not required.

## Install

```bash
git clone https://github.com/your-github-user/ApplyPilot.git
cd ApplyPilot
python -m venv .venv
source .venv/bin/activate        # Windows: .\.venv\Scripts\Activate.ps1
pip install -e .
```

## Personal data directory

**Existing user, new machine (e.g. Cade's MacBook):**

```bash
git clone https://github.com/your-github-user/applypilot-data.git ~/.applypilot
```

The `.env` (keys) is gitignored — copy it over manually (USB/airdrop, never
commit it). Same for `~/.gmail-mcp/` (Gmail OAuth for the apply agent) —
copy both files or re-run `npx @gongrzhe/server-gmail-autoauth-mcp auth`.

> **Two-machine rule:** the SQLite DB does not merge. Only run the cycle on
> one machine per day, and `git pull` in `~/.applypilot` before starting.

**New user (friend):**

```bash
mkdir ~/.applypilot
applypilot init          # onboarding wizard: profile, resume, searches
```

Then fill in `~/.applypilot/.env` (template below) and set up Gmail auth:
create a Google Cloud project → enable Gmail API → OAuth consent (External,
add yourself as test user) → OAuth client (Desktop app) → save the JSON as
`~/.gmail-mcp/gcp-oauth.keys.json` → `npx @gongrzhe/server-gmail-autoauth-mcp auth`.

### .env template

```ini
# Writing quality (tailor/cover). Anthropic recommended; ~$0.05/job.
LLM_URL=https://api.anthropic.com/v1
LLM_API_KEY=sk-ant-...
LLM_MODEL=claude-haiku-4-5-20251001

# Free fallback chain (pipeline keeps running when the primary dies)
NVIDIA_API_KEY=nvapi-...        # free at build.nvidia.com
GROQ_API_KEY=gsk_...            # free at console.groq.com
# GEMINI_API_KEY=...            # optional, aistudio.google.com/apikey

# CAPTCHA solving during auto-apply (optional, a few $ of credit)
CAPSOLVER_API_KEY=CAP-...

# Extra job boards (optional free keys)
# ADZUNA_APP_ID= / ADZUNA_APP_KEY=   developer.adzuna.com
# USAJOBS_API_KEY= / USAJOBS_EMAIL=  developer.usajobs.gov
```

## Running

```bash
applypilot run discover enrich    # find jobs (24h window)
applypilot run score              # fit-score 1-10
applypilot run tailor cover pdf   # resumes + cover letters for 7+
applypilot apply --limit 999 --workers 2
applypilot status
```

Or the full nightly cycle: `scripts/run-cycle.ps1` (Windows) /
`scripts/run-cycle.sh` (macOS). Schedule with Task Scheduler / launchd.

## Known gaps on macOS (first-run checklist)

- Verify the apply launcher finds Chrome (`/Applications/Google Chrome.app/...`);
  if not, it's the Chrome-launch block in `apply/launcher.py`.
- `run-cycle.sh` uses `caffeinate` instead of `powercfg` to keep the machine awake.
- Ollama for mac from ollama.com if you want the local fallback.

## Multi-user readiness

The scoring gates (location, years of experience, education level), the
tailor/cover-letter truth rules, and the validator's overclaim check are all
generated at runtime from `profile.json` + `searches.yaml` — a new user's
constraints apply automatically once their profile is filled in.

Remaining per-user setup: write your own `searches.yaml` (queries, locations,
accept/reject patterns — the init wizard helps), and note that
`apply/prompt.py`'s education section falls back to placeholder defaults if
`profile.json` has no `education` entry, so fill that in.
