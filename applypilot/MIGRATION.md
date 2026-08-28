# Moving ApplyPilot from Mac → Windows

You need to move **two folders** and rebuild the Python environment (the env itself
can't be copied across OSes). Plan for ~20–30 min.

---

## 1. What to copy

| Folder (Mac) | Windows destination | Contains |
|---|---|---|
| `~/ApplyPilot` | `C:\Users\<you>\ApplyPilot` | the code + your `my_jobs*.txt`, setup scripts |
| `~/.applypilot` | `C:\Users\<you>\.applypilot` | profile, résumés, cover letters, **the database (all your job/apply state)**, `.env` (keys) |
| `~/.gmail-mcp` (optional) | `C:\Users\<you>\.gmail-mcp` | Gmail auth for reading verification codes during auto-apply |

**Do NOT copy** `~/ApplyPilot/.venv` — it's Mac binaries and won't run on Windows. Delete it after copying (you'll rebuild it in step 3).

> `~/.applypilot` holds your API keys (`.env`) and personal data (the DB). Keep it
> private — use a USB drive or a private cloud folder, not a public GitHub repo.

**Transfer options:** a USB drive, or upload both folders to Google Drive/OneDrive/Dropbox and download on the PC. (On Mac, `~/.applypilot` is hidden — in Finder press `Cmd+Shift+.` to see dotfolders, or `Cmd+Shift+G` and type `~/.applypilot`.)

---

## 2. Install prerequisites on Windows

- **Python 3.11+** — python.org installer, and CHECK "Add Python to PATH".
- **Node.js** (LTS) — nodejs.org.
- **Google Chrome**.
- **Git** (optional) — git-scm.com.
- **Claude Code CLI**: `npm install -g @anthropic-ai/claude-code`, then run `claude` once and log in (`/login`). This is what the apply step uses.

---

## 3. Rebuild the Python environment

Open **PowerShell**, then:

```
cd C:\Users\<you>\ApplyPilot
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
pip install --no-deps python-jobspy
pip install pydantic tls-client requests markdownify regex pyyaml python-dotenv rich typer beautifulsoup4 lxml playwright openpyxl
python -m playwright install chromium
```

(If `pip install -e .` pulls the deps itself, the extra `pip install` line is just a safety net.)

---

## 4. Fix the database file paths

The DB stores Mac paths like `/Users/cadejeong/.applypilot/...`. Rewrite them to the
Windows folder (run from the activated venv):

```
python applypilot-setup\migrate_paths.py
```

It should say "All referenced résumé PDFs found." If it reports missing PDFs, either
you didn't copy the `tailored_resumes` / `cover_letters` subfolders, or just regenerate:
`applypilot run tailor cover pdf`.

---

## 5. Verify

```
applypilot doctor
```

Fix anything it flags (usually a missing dependency or Claude Code not logged in).
Then check your state carried over:

```
python applypilot-setup\apply_queue.py
```

You should see the same ready jobs as on the Mac.

---

## Windows differences to remember

- **No `caffeinate`** — just drop it. Instead of `caffeinate -i applypilot run ...`,
  run `applypilot run ...`. (To stop sleep during long runs: Settings → System →
  Power → Screen/Sleep → Never, or run `powercfg /change standby-timeout-ac 0`.)
- **Use `python`, not `python3`.**
- **Paths use `\`** and scripts live at `applypilot-setup\add_jobs.py` etc.
- **The `python - <<'PY' ... PY` heredoc trick doesn't work in PowerShell/cmd.**
  Put such snippets in a `.py` file and run `python thatfile.py` instead.
- Your `.env` keys (Claude/LLM, CapSolver) transfer as-is — no change needed.

---

## Quick checklist

- [ ] Copy `~/ApplyPilot` (minus `.venv`) and `~/.applypilot` to the PC
- [ ] Install Python, Node, Chrome, Claude Code (`claude` login)
- [ ] `python -m venv .venv` → activate → `pip install -e .` → `playwright install chromium`
- [ ] `python applypilot-setup\migrate_paths.py`
- [ ] `applypilot doctor`
- [ ] `python applypilot-setup\apply_queue.py` to confirm your jobs are there
