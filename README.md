# JobRadar

Personal nightly scraper for design roles hireable from Portugal.
Free sources only, Python stdlib only, no dependencies.

- `scrape.py` — pulls 8 aggregator APIs + ~50 company ATS boards
  (Greenhouse / Lever / Ashby), filters design titles (no junior/intern),
  classifies Portugal-hireability, dedupes, tracks first/last seen.
- `config.json` — title filters + company watchlist. Add a company by
  adding its ATS slug (dead slugs fail soft and show in the dashboard footer).
- `data/jobs.json` — the accumulated database, committed nightly.
- `index.html` — dashboard: New overnight / Check eligibility / Active.
  Applied + Hide live in localStorage.
- `.github/workflows/scrape.yml` — runs at 05:30 Lisbon, commits, Pages serves it.

Local run:

    python3 scrape.py && python3 -m http.server 8123

then open http://localhost:8123
