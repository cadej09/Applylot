# Your Job Search Workflow (broad discovery + tracker + manual/auto apply)

The insight from testing: **LinkedIn/Indeed are great for *finding* jobs, bad for
*auto-applying*.** So the workflow is — cast a wide net, rank everything by fit,
auto-apply the ATS jobs, and apply by hand (with AI-tailored materials) to the rest.

## 1. Widen the net (run this to refresh your job pool)

```
cp ~/ApplyPilot/applypilot-setup/searches.yaml ~/.applypilot/searches.yaml
caffeinate -i applypilot run discover -w 4
```
This now pulls from **Indeed + LinkedIn + Google Jobs**, and the discover stage also
scrapes **48 Workday employer portals + 30 direct career sites** automatically.
Google Jobs and the Workday/direct sites are where the *auto-appliable* jobs come from.

Then score + tailor the new jobs (Haiku, ~15 min):
```
caffeinate -i applypilot run score tailor cover pdf -w 4
```
> Tailoring/cover are capped at 20 per run. Re-run `... tailor cover pdf` to prep the next batch.

## 2. Build your master tracker

```
python3 ~/ApplyPilot/applypilot-setup/applylog_export.py --tracker
```
Writes `~/ApplyPilot/job_tracker.xlsx` (+ `.csv`): every job scoring ≥7, ranked by
fit, with an **Apply Method** column:
- **Auto (ATS)** — a real application URL exists; the bot can submit it.
- **By hand** — LinkedIn/Indeed listing; you apply manually (2 min with the tailored resume + cover letter it already made).

Other columns: company, title, location, salary, whether a tailored resume/cover
letter is ready, the apply link, the job description, and the match keywords.
Re-run anytime to refresh. Options: `--min-score 6`, `--applied` (only submitted), `--all`.

## 3. Auto-apply the ATS jobs (hands-free)

```
applypilot apply --dry-run        # watch it fill a couple, no submit
applypilot apply                  # go live once you're happy
```
The bot naturally skips login-walled LinkedIn/Indeed and works the ATS jobs. Keep it
on `--dry-run` for the first batch.

## 4. Apply by hand to the "By hand" jobs — fast

For each By-hand row in the tracker: open the **Apply / Job Link**, and upload the
matching tailored resume + cover letter already sitting in:
```
~/.applypilot/tailored_resumes/
~/.applypilot/cover_letters/
```
(Filenames are `<source>_<Job_Title>.pdf`.)

## 5. Add a job you found yourself (Handshake, a referral, anywhere)

Handshake can't be auto-scraped (login-walled, no API), so add postings by hand:

```
# If you can paste the description (works even for login-walled pages):
python3 ~/ApplyPilot/applypilot-setup/add_job.py "PASTE_JOB_URL" \
    --title "Data Analyst" --company "Acme" --location "Seattle, WA" \
    --desc-file ~/Desktop/jd.txt
applypilot run score tailor cover pdf

# If it's a public page the scraper can read, just give the URL:
python3 ~/ApplyPilot/applypilot-setup/add_job.py "PASTE_JOB_URL"
applypilot run enrich score tailor cover pdf
```
It gets scored + tailored like any other job and shows up in your tracker.

---

### Cheat sheet
| Goal | Command |
|------|---------|
| Refresh job pool (wide) | `caffeinate -i applypilot run discover -w 4` |
| Score + tailor | `caffeinate -i applypilot run score tailor cover pdf -w 4` |
| Master tracker | `python3 ~/ApplyPilot/applypilot-setup/applylog_export.py --tracker` |
| Auto-apply (test) | `applypilot apply --dry-run` |
| Add one job | `python3 ~/ApplyPilot/applypilot-setup/add_job.py "URL" [--desc-file f]` |
