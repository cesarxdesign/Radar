#!/usr/bin/env python3
"""One-time Gmail sweep ingest.

Reads data/email_raw.json (gitignored - email content never leaves this
machine) and produces:
  - data/applied_email.json (gitignored) for the dashboard's appliedEMAIL tab
  - new proven boards appended to data/companies.json (via="email")

email_raw.json: [{date, from, subject, body, links[], company?, title?, platform?}]
Company/title are extracted at capture time where readable; light heuristics
here only fill gaps.
"""
import hashlib
import json
import re
import sys

import scrape
from scrape import ROOT, norm, job_key, parse_board_url, registry_add

RAW_FILE = ROOT / "data" / "email_raw.json"
OUT_FILE = ROOT / "data" / "applied_email.json"
REG_FILE = ROOT / "data" / "companies.json"

PLATFORM_BY_SENDER = {
    "greenhouse": "greenhouse", "lever.co": "lever", "ashbyhq": "ashby",
    "workable": "workable", "teamtailor": "teamtailor",
    "smartrecruiters": "smartrecruiters", "bamboohr": "bamboohr",
    "breezy": "breezy", "personio": "personio", "recruitee": "recruitee",
    "pinpointhq": "pinpoint", "myworkdayjobs": "workday", "workday": "workday",
    "icims": "icims", "linkedin": "linkedin", "indeed": "indeed",
    "join.com": "join", "manatal": "manatal", "careers-page": "manatal",
    "otta": "otta", "welcometothejungle": "welcometothejungle",
    "jobgether": "jobgether", "landing.jobs": "landingjobs",
    "wellfound": "wellfound", "himalayas": "himalayas", "remotive": "remotive",
    "jobicy": "jobicy", "weworkremotely": "weworkremotely",
}

SUBJECT_RES = [
    re.compile(r"(?:thank(?:s| you) for applying (?:to|at|for)|your application (?:to|at|for)|"
               r"application (?:received|for|to)|applying (?:to|at))[:\s]+(?P<a>.{2,80})", re.I),
    re.compile(r"(?P<a>.{2,60}?)\s+(?:application|candidatura)", re.I),
]

def sender_platform(from_addr):
    f = (from_addr or "").lower()
    for token, plat in PLATFORM_BY_SENDER.items():
        if token in f:
            return plat
    return None

def guess_company_title(subject, body):
    """Fallback when capture didn't label the email."""
    company = title = None
    for rx in SUBJECT_RES:
        m = rx.search(subject or "")
        if m:
            frag = m.group("a").strip(" .!-—")
            # "Product Designer at Acme" / "Acme - Product Designer"
            m2 = re.search(r"(.+?)\s+(?:at|@)\s+(.+)", frag)
            if m2 and scrape.INC.search(m2.group(1)):
                title, company = m2.group(1).strip(), m2.group(2).strip()
            elif scrape.INC.search(frag):
                title = frag
            else:
                company = frag
            break
    if not title:
        m = re.search(r"(?:position|role|vacancy|opening)[:\s]+([^\n.]{4,70})", body or "", re.I)
        if m and scrape.INC.search(m.group(1)):
            title = m.group(1).strip()
    return company, title

def match_radar(jobs, company, title):
    """Radar job id + confidence for an applied role, or (None, None)."""
    if not company and not title:
        return None, None
    exact = job_key(company, title)
    for j in jobs:
        if j["id"] == exact:
            return j["id"], "exact"
    nc, nt = norm(company), norm(title)
    best = (None, None)
    for j in jobs:
        jc, jt = norm(j.get("company")), norm(j.get("title"))
        comp_hit = nc and jc and (nc == jc or nc in jc or jc in nc)
        if not comp_hit:
            continue
        if nt and jt and (nt in jt or jt in nt):
            return j["id"], "company+title"
        if nt and jt:
            a, b = set(nt.split()), set(jt.split())
            if a and b and len(a & b) / len(a | b) >= 0.5:
                best = (j["id"], "fuzzy")
    return best

def main():
    raw = json.loads(RAW_FILE.read_text())
    jobs = [j for j in json.loads((ROOT / "data" / "jobs.json").read_text())["jobs"]
            if j.get("active")]
    try:
        registry = json.loads(REG_FILE.read_text())
    except Exception:
        registry = {}

    matches, new_boards, aggregators = [], [], {}
    for e in raw:
        subject, body = e.get("subject") or "", e.get("body") or ""
        company, title = e.get("company"), e.get("title")
        if not company or not title:
            gc, gt = guess_company_title(subject, body)
            company, title = company or gc, title or gt
        platform = e.get("platform") or sender_platform(e.get("from"))
        radar_id, conf = match_radar(jobs, company, title)
        key = hashlib.sha1(f"{e.get('from')}|{e.get('date')}|{subject}"
                           .encode()).hexdigest()[:12]
        matches.append({
            "key": key, "date": e.get("date"), "from": e.get("from"),
            "subject": subject, "company": company, "title": title,
            "platform": platform, "urls": e.get("links") or [],
            "radar_id": radar_id, "confidence": conf,
            "body": body[:6000],
        })
        for u in e.get("links") or []:
            pe = parse_board_url(u)
            if pe and pe[0] in registry_platforms():
                ent = dict(pe[1], company=company or "", via="email")
                if registry_add(registry, pe[0], ent):
                    new_boards.append((pe[0], ent))
            else:
                host = re.sub(r"^www\.", "", re.sub(r"^https?://", "", u).split("/")[0])
                if platform and platform not in scrape.SOURCE_PRIORITY:
                    aggregators.setdefault(platform, host)

    REG_FILE.write_text(json.dumps(registry, indent=1, sort_keys=True,
                                   ensure_ascii=False))
    matches.sort(key=lambda m: m.get("date") or "", reverse=True)
    OUT_FILE.write_text(json.dumps(
        {"generated_at": scrape.RUN_ID, "matches": matches},
        indent=1, ensure_ascii=False))

    paired = [m for m in matches if m["radar_id"]]
    print(f"emails: {len(matches)} | paired with radar: {len(paired)} | "
          f"new boards for the pool: {len(new_boards)}")
    for p, ent in new_boards:
        print(f"  +{p}: {ent.get('slug') or ent.get('host') or ent.get('tenant')}"
              f" ({ent.get('company', '?')})")
    if aggregators:
        print("aggregator platforms seen in emails, not in pool (need fetchers):")
        for plat, host in sorted(aggregators.items()):
            print(f"  - {plat} ({host})")
    for m in paired:
        print(f"  paired[{m['confidence']}]: {m['company']} - {m['title']}")

def registry_platforms():
    return {"workday", "bamboohr", "breezy", "workable", "smartrecruiters",
            "teamtailor", "personio", "pinpoint", "join", "manatal",
            "recruitee", "greenhouse", "lever", "ashby"}

if __name__ == "__main__":
    sys.exit(main())
