# ApplyPilot

An autonomous job-application pipeline. It discovers postings across job boards
and company ATSs, scores them against a candidate profile, tailors a résumé and
cover letter per role, renders PDFs, and submits the application through a
browser agent — then tracks the outcome in the inbox.

It has submitted **300+ real applications**. Most of what's interesting here is
not the happy path; it's the machinery that keeps an autonomous system honest
and cheap when it runs unattended every night.

```
discover → enrich → prefilter → score → gates → tailor → cover → pdf → liveness → apply
```

---

## Design decisions worth reading

### 1. It refuses to lie on your behalf

An LLM writing a résumé will embellish unless something stops it. Every tailored
document passes a validation layer before it can ship:

- **Years-overclaim check** — the résumé can never assert more experience than
  the profile declares.
- **Invented-attribute check** — regex + LLM judge for fabricated language
  fluency, citizenship, clearances, or leadership claims. This exists because an
  early lenient mode shipped a résumé asserting "Native Spanish speaker."
- **LLM judge** — an independent pass that reads the generated document against
  the source facts. A `FAIL` sets status `failed_judge` and the document is
  never sent. There is no "approve with warnings" escape hatch.

The lesson encoded here: the validator runs in *every* mode. The one time it was
skippable, it shipped a fabrication.

### 2. Scoring failures must never look like scoring results

The scorer assigns a 1–10 fit score. The subtle bug is that a provider outage
returns *something*, and if you coerce that to `0` you have silently and
permanently marked thousands of good jobs as bad.

So: a provider error or an unparseable response leaves the job **unscored**
(`NULL`) to be retried, never `0`. Ten consecutive failures abort the run. This
rule was written after an outage buried ~1,000 jobs at score 0.

### 3. Gates cap, they don't delete

Business rules (seniority too high, wrong domain, spam listers, experience
requirements, location tiers) *cap* a score rather than dropping the row. Every
gate caps to the same value, one below the apply threshold — which means a
gate-rejected job is visible and auditable rather than gone.

That invariant has a sharp edge, documented in the code: an employer-reputation
bonus that adds +1 must never be able to lift a gate-capped job back over the
bar. When the threshold moved, that bonus had to move with it.

### 4. Free-first LLM routing

Scoring and tailoring run on a chain of ~9 free-tier providers before any paid
one is touched. Adding a key to `.env` adds a provider; commenting it out
removes it — no code change. Most nightly runs cost **$0** in LLM spend.

Provider quirks are handled explicitly rather than by retry-and-pray — reasoning
models that bill hidden tokens against `max_tokens` and return empty strings,
endpoints that reject an explicit `temperature`, models that return HTTP 200
with garbage and must be demoted mid-run.

### 5. The expensive step is the browser agent, so don't waste it

Applying is the only genuinely costly operation: one browser-automation agent
session per job. Measurement over a two-week window found **34% of those
sessions were spent discovering a posting had already expired** — launching a
browser and an agent just to read "no longer accepting applications."

Two fixes, both free:

- **Liveness pre-check** (`check-expired.py`) — one HTTP GET retires dead
  postings before the agent starts. Deliberately conservative: it retires only
  on a `404/410` from a real ATS or an explicit expiry phrase. Timeouts,
  `403/429` bot-blocks, and aggregator URLs are left alone, because a false
  positive silently deletes a real opportunity and that is much worse than
  wasting one session.
- **Freshest-first ordering** — expired postings had a median age of 13 days;
  successful applications, 2.3 days. Score still dominates the ordering;
  freshness only breaks ties.

Result: expired-posting waste fell from 34% of attempts to 7%.

### 6. Duplicate applications are worse than missed ones

The same job appears on LinkedIn, Indeed, and the employer's own ATS as three
rows with three URLs, so URL-keyed dedup never catches it. Two guards run at
claim time: a title + employer match (with umbrella-brand normalization, so
`Amazon.com` and `Amazon Web Services` collapse to one employer), and a
description-hash signature, since the employer writes the description once and
boards only reformat it.

A subtle failure this codebase hit: the signature was computed only in the
enrichment stage, but boards that supply a description inline skip that stage —
so the guard silently had nothing to compare and let a duplicate through. The
signature is now derived at every insert site.

---

## Layout

```
applypilot/src/applypilot/
  discovery/     board + ATS crawlers, US-eligibility gate
  enrichment/    full-description fetch, apply-URL resolution
  scoring/       fit scoring, résumé tailoring, cover letters, validation
  apply/         browser agent orchestration, per-worker Chrome, dedup guards
  pdf.py         résumé + letter rendering
*.py, *.ps1      operational scripts (healing, gates, QA, queue maintenance)
```

## Running it

```bash
cp applypilot/.env.example ~/.applypilot/.env      # add provider API keys
cp applypilot/profile.example.json ~/.applypilot/profile.json
applypilot run discover enrich score
applypilot run tailor cover pdf --min-score 6
applypilot apply --limit 10 --workers 1
```

`run-cycle.ps1` chains the full nightly pipeline with healing, gate sweeps, the
liveness pre-check, pruning, and a backup push.

## Notes

- Personal configuration (`profile.json`, `.env`, credentials) lives outside the
  repo in `~/.applypilot/` and is gitignored. Only `.example` templates ship.
- The browser agent requires a Claude subscription or API key; scoring and
  tailoring run on free-tier providers.
- Published as a portfolio piece. It is tuned to one candidate's profile and job
  market, so treat it as a reference implementation rather than a turnkey tool.

## License

MIT — see [LICENSE](LICENSE).
