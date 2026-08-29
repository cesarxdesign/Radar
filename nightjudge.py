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
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
JOBS_FILE = ROOT / "data" / "jobs.json"
JDS_FILE = ROOT / "data" / "jds.json"
JUDGED_FILE = ROOT / "data" / "judged.json"
CAP = 120  # runaway guard; nightly delta is normally 10-40

PROMPT = """You judge job postings for César, a senior product designer in Lisbon, Portugal.
Answer with ONE JSON object, nothing else: {{"role": "pd|not_pd|unsure", "place": "pt|eu|ww|cut|unsure", "why": "<short reason, only when not clearly ok>"}}

ROLE = "pd" only if the job is product design of software/apps (UX/UI, flows, design systems, prototypes, usability; product-design leadership; design-code hybrids like Design Engineer). Marketing/brand/graphic/motion/print, physical/industrial/architecture/engineering, game content, PM or ops = "not_pd". Research-only or content/UX-writing = "unsure".
PLACE: onsite/hybrid roles must be in Lisbon ("pt"); remote roles must allow Portugal - Europe/EMEA = "eu", worldwide/anywhere = "ww", Lisbon/Portugal = "pt". A European company saying remote counts for all Europe unless the JD demands a specific country, citizenship, or payroll location - then "cut". Specific non-Portugal scope (US-only, Germany-based, etc.) = "cut". Onsite/hybrid outside Lisbon = "cut". Can't tell = "unsure". Ignore timezone requirements completely.
If the JD is written in a language other than English or Portuguese, place = "cut", why = "JD in <language>".

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
    print(f"pending: {len(pending)} (cap {CAP})")
    done = 0
    for j in pending[:CAP]:
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
