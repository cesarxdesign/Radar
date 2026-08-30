#!/usr/bin/env python3
"""The judge pass: read each unjudged role's JD and rule on role + location.

Runs on GitHub Actions after the nightly scrape (claude CLI on César's Max
subscription via CLAUDE_CODE_OAUTH_TOKEN). Verdicts are authoritative and
persist in data/judged.json; jobs.json is patched in place so the dashboard
updates without waiting for the next scrape.

Criteria (César, 2026-08-29):
  ROLE   product design of software/apps - UX/UI of digital products: screens,
         flows, interfaces, design systems, prototypes, usability; leading
         product design teams counts; design-code hybrids (Design Engineer,
         UX Engineer) count. NOT: marketing/brand/graphic/motion/print,
         physical/industrial/architectural/engineering design, game content,
         PM/ops. Research-only and content/UX-writing roles -> unsure (his
         call, never auto-cut).
  PLACE  onsite/hybrid: Lisbon only. Remote: anything that allows Portugal -
         Europe ok, worldwide ok, an EU company saying "remote" counts for
         all Europe UNLESS the JD is specific (country-based, citizenship,
         payroll restrictions). Specific-elsewhere (US-only etc.) = cut.
         Timezones are NOT a criterion.
  LANG   JD in English or Portuguese ok; any other language = cut.
  GATE   sure on both -> pt/eu/ww bucket. Clear fail on either -> cut.
         Anything in between -> unsure, with the failing criterion as why.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
JOBS_FILE = ROOT / "data" / "jobs.json"
JDS_FILE = ROOT / "data" / "jds.json"
JUDGED_FILE = ROOT / "data" / "judged.json"
# No count cap. The real limit is wall-clock: judge until the time budget is
# spent, saving progress as we go so a job timeout never loses work. The next
# run continues where this one stopped. Budget leaves headroom under the
# workflow's job timeout for the npm install and the final push.
TIME_BUDGET_S = int(os.environ.get("JUDGE_BUDGET_S", "6600"))  # 110 min; loop ends when pending is drained
SAVE_EVERY = 20  # flush judged.json to disk this often, so a crash keeps work

PROMPT = """You judge job postings for César, a senior product designer based in Lisbon, Portugal.
Answer with ONE JSON object, nothing else: {{"role": "pd|not_pd|unsure", "place": "pt|eu|ww|cut|unsure", "why": "<short reason, only when not clearly ok>"}}

ROLE
"pd"  = product design of software / digital apps: UX/UI, user flows, design systems, prototypes, usability; leading product-design teams; design-code hybrids (Design Engineer, UX Engineer, UI Engineer).
"not_pd" = marketing / brand / graphic / motion / print design; physical / industrial / architectural / hardware / mechanical / electrical design; game design or game content; software/backend/frontend/data engineering; product management, product ops, program/project management, sales, recruiting, legal.
"unsure" = UX/design research only, or content design / UX writing (a different discipline - César decides case by case).

PLACE (read the JD body, not just the location field)
- Onsite or hybrid: only Lisbon (or greater-Lisbon Portugal) = "pt". Onsite/hybrid ANYWHERE ELSE = "cut".
- Remote must allow someone living in Portugal:
  - Portugal / Lisbon named = "pt".
  - Europe / EMEA / EEA / "EU" / a EUROPEAN COUNTRY (Germany, France, Poland, UK, Spain, Netherlands, Ireland, etc.) = "eu". A European country in the location is FINE - César can work remotely from Portugal for a European company. Do NOT cut a Germany-remote or Poland-remote role just because it names that country.
  - Worldwide / anywhere / global / fully remote with no region limit = "ww".
- CUT the place only on explicit exclusion of Portugal:
  - A NON-European scope: US-only, "Americas", North America, LATAM, APAC, Asia, Canada, Australia, India, etc. ("Remote - US", "Americas Remote") = "cut".
  - The JD demands residence, citizenship, work authorization, or payroll IN a specific non-Portugal country ("must be based in Germany", "US citizen", "right to work in the UK required") = "cut". A European country merely LISTED as a hiring location is not this - only an explicit lock is.
- Genuinely can't tell = "unsure".
- IGNORE timezone / working-hours requirements entirely - never cut on those.
- JD written in a language other than English or Portuguese: place = "cut", why = "JD in <language>".

EXAMPLES (title | location | -> verdict)
- Senior Product Designer | Remote - Europe            -> {{"role":"pd","place":"eu"}}
- Product Designer | Germany (Remote)                  -> {{"role":"pd","place":"eu"}}   (European country remote = fine)
- Staff Product Designer | Americas Remote             -> {{"role":"pd","place":"cut","why":"Americas-only scope"}}
- Senior Product Designer | US Remote                  -> {{"role":"pd","place":"cut","why":"US-only"}}
- Product Designer | Lisbon                            -> {{"role":"pd","place":"pt"}}
- Product Designer | Berlin (onsite 3 days/week)       -> {{"role":"pd","place":"cut","why":"hybrid outside Lisbon"}}
- Design Engineer | Remote - EMEA                      -> {{"role":"pd","place":"eu"}}   (design-code hybrid counts)
- Staff Backend Engineer - Core Product | Europe       -> {{"role":"not_pd","place":"eu","why":"engineering, not design"}}
- Brand Designer | Anywhere                            -> {{"role":"not_pd","place":"ww","why":"brand, not product design"}}
- UX Researcher | Remote - Europe                      -> {{"role":"unsure","place":"eu","why":"research discipline"}}
- Product Designer (German-speaking) | must reside in Germany -> {{"role":"pd","place":"cut","why":"Germany residence required"}}

TITLE: {title}
COMPANY: {company}
LOCATION FIELD: {location}
JD:
{jd}"""


def ask(prompt):
    r = subprocess.run(["claude", "-p", prompt], capture_output=True, text=True, timeout=300)
    m = re.search(r"\{.*\}", r.stdout, re.S)
    return json.loads(m.group(0)) if m else None


def verdict_to_bucket(v):
    if not v:
        return None
    role, place = v.get("role"), v.get("place")
    why = (v.get("why") or "").strip()
    if role == "not_pd" or place == "cut":
        return {"bucket": "cut", "why": why or "judge: not product design / place excluded"}
    if role == "unsure" or place == "unsure":
        return {"bucket": "tiebreak", "why": why or "judge: not clear-cut on role or place"}
    if role == "pd" and place in ("pt", "eu", "ww"):
        return {"bucket": place, "why": ""}
    return None


def main():
    data = json.loads(JOBS_FILE.read_text())
    jds = json.loads(JDS_FILE.read_text()).get("jobs", {})
    judged = json.loads(JUDGED_FILE.read_text()) if JUDGED_FILE.exists() else {"jobs": {}}
    pending = [j for j in data["jobs"]
               if j.get("active") and j["id"] not in judged["jobs"]]
    print(f"pending: {len(pending)} | budget {TIME_BUDGET_S}s")
    start = time.monotonic()
    done = 0
    for j in pending:
        if time.monotonic() - start > TIME_BUDGET_S:
            print(f"  time budget spent after {done}; {len(pending)-done} left for next run")
            break
        jd = (jds.get(j["id"], {}).get("jd") or "")[:8000]
        try:
            v = ask(PROMPT.format(title=j["title"], company=j["company"],
                                  location=j.get("location") or "(none)",
                                  jd=jd or "(no description available - judge on title and location only)"))
        except Exception as e:
            print(f"  skip {j['company']} - {j['title']}: {e}")
            continue
        out = verdict_to_bucket(v)
        if not out:
            print(f"  unparseable verdict for {j['company']} - {j['title']}")
            continue
        judged["jobs"][j["id"]] = {"bucket": out["bucket"], "why": out["why"],
                                   "at": data.get("run_id", "")}
        done += 1
        if done % SAVE_EVERY == 0:
            JUDGED_FILE.write_text(json.dumps(judged, indent=1, ensure_ascii=False))
    JUDGED_FILE.write_text(json.dumps(judged, indent=1, ensure_ascii=False))

    # patch jobs.json in place (same application the scraper does)
    from scrape import market_label
    for j in data["jobs"]:
        v = judged["jobs"].get(j["id"])
        if not v:
            continue
        j["judged"] = True
        if v["bucket"] == "cut":
            j["active"] = False
        else:
            j["bucket"] = v["bucket"]
            j["market"] = market_label(v["bucket"], j.get("location"))
            if v.get("why"):
                j["why"] = v["why"]
    data["counts"]["active"] = sum(1 for j in data["jobs"] if j["active"])
    JOBS_FILE.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    print(f"judged {done}; active now {data['counts']['active']}")


if __name__ == "__main__":
    sys.exit(main())
