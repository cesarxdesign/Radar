#!/usr/bin/env python3
"""JobRadar - nightly scrape of design roles hireable from Portugal.

Free sources only, stdlib only. Writes data/jobs.json consumed by index.html.
"""
import json
import re
import hashlib
import time
import urllib.request
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text())
DATA_FILE = ROOT / "data" / "jobs.json"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) JobRadar/1.0 (personal job tracker)"
NOW = datetime.now(timezone.utc)
RUN_ID = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")

NET = re.compile(r"design|product|\bux\b|\bui\b", re.I)
DESIGN_SIGNAL = re.compile(r"design|\bux\b|\bui\b", re.I)
EXC_JUNIOR = re.compile(CONFIG["title_exclude_junior"], re.I)
EXC_SURE = re.compile(CONFIG["title_exclude_sure"], re.I)

# ---------------------------------------------------------------- fetch

def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")

def fetch_json(url, timeout=30):
    return json.loads(fetch(url, timeout))

# ---------------------------------------------------------------- filters

def title_ok(title):
    """Pass 1: wide net (design/product/ux/ui in the title) - recall is sacred.
    Only for-sure non-design phrases are cut here, and never when the title
    carries a design signal. The judge pass reads the JD and does precision."""
    t = title or ""
    if not NET.search(t):
        return False
    return not (EXC_SURE.search(t) and not DESIGN_SIGNAL.search(t))

def salary_top(s):
    """Highest figure in a salary string, normalized to plain units."""
    best = 0
    for m in re.finditer(r"(\d[\d.,]*)\s?([kKmM]?)", s or ""):
        try:
            n = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        n *= {"k": 1e3, "m": 1e6}.get(m.group(2).lower(), 1)
        best = max(best, n)
    return best

# A JD written in another language means they want that language - cut it.
# English and Portuguese are César's languages; everything else is a red flag.
# The JD text decides, never the company's nationality.
EN_STOP = re.compile(
    r"\b(the|and|you|we|with|for|our|will|team|are|is|to|of|in|be|your)\b", re.I)
PT_STOP = re.compile(
    r"\b(vaga|você|voce|nós|não|nao|uma|dos|das|será|sera|para|equipa|"
    r"candidatura|função|experiência|conhecimentos|procuramos)\b", re.I)
OTHER_STOP = re.compile(
    r"\b(und|oder|für|mit|wir|der|die|das|nicht|bei|sind|werden|eine[nr]?|"
    r"avec|pour|dans|les|des|une|vous|nous|sont|être|"
    r"con|una|los|las|nuestro|equipo|buscamos|"
    r"wij|het|een|niet|voor|met|onze)\b", re.I)

def jd_language_ok(text):
    """True for English or Portuguese JDs; False when clearly another language."""
    t = (text or "")[:4000]
    if len(t) < 120:
        return True  # too little text to judge
    return len(EN_STOP.findall(t)) + len(PT_STOP.findall(t)) >= len(OTHER_STOP.findall(t))

PT_RE = re.compile(
    r"\b(portugal|lisbon|lisboa|porto(?!\s*alegre)|braga|coimbra|aveiro|faro|"
    r"set[uú]bal|oeiras|cascais|sintra|guimar[ãa]es|matosinhos|funchal|"
    r"[ée]vora|leiria|almada|amadora|pt)\b", re.I)
EU_RE = re.compile(r"\b(europe|european|emea|eu|eea|cet|wet|gmt|utc)\b", re.I)
WW_RE = re.compile(r"\b(worldwide|world wide|anywhere|globally|global|international)\b", re.I)
REMOTE_RE = re.compile(r"\bremote\b", re.I)
# Signals that a location string is somewhere specific that is NOT Portugal.
ELSEWHERE_RE = re.compile(
    r"\b(usa?|u\.s\.a?\.?|united states|america|americas|north america|canada|latam|"
    r"latin america|south america|apac|asia|australia|new zealand|india|africa|"
    r"middle east|uk|united kingdom|england|london|germany|berlin|munich|france|paris|"
    r"spain|madrid|barcelona|italy|milan|netherlands|amsterdam|poland|warsaw|ireland|dublin|"
    r"austria|vienna|switzerland|zurich|sweden|stockholm|denmark|copenhagen|norway|oslo|"
    r"finland|helsinki|estonia|tallinn|latvia|lithuania|czech|prague|slovakia|hungary|budapest|"
    r"romania|bulgaria|greece|croatia|serbia|ukraine|turkey|israel|uae|dubai|brazil|mexico|"
    r"argentina|colombia|philippines|japan|korea|china|singapore|vietnam|indonesia|nigeria|"
    r"kenya|south africa|egypt|pakistan|bangladesh|belgium|brussels|luxembourg|malta|cyprus|"
    r"iceland|scotland|wales|manchester|hamburg|cologne|frankfurt|lyon|rotterdam|"
    r"new york|san francisco|los angeles|austin|seattle|boston|chicago|denver|miami|toronto|"
    r"vancouver|montreal|sydney|melbourne|bangalore|bengaluru|mumbai|delhi|tokyo|seoul|"
    r"california|texas|florida|colorado|pennsylvania|pittsburgh|philadelphia|phoenix|atlanta|"
    r"dallas|houston|nashville|minneapolis|detroit|san diego|san jose|salt lake|utah|arizona|"
    r"oregon|portland|nevada|illinois|michigan|ohio|georgia|virginia|maryland|massachusetts|"
    r"new jersey|north carolina|south carolina|tennessee|missouri|indiana|wisconsin|minnesota|"
    r"alabama|louisiana|kentucky|oklahoma|arkansas|kansas|iowa|nebraska|idaho|montana|wyoming|"
    r"alaska|hawaii|maine|vermont|new hampshire|connecticut|rhode island|delaware|washington|"
    r"nyc|belarus|minsk|russia|moscow|ecuador|quito|peru|chile|santiago|bolivia|venezuela|"
    r"uruguay|paraguay|costa rica|panama|guatemala|honduras|morocco|tunisia|ghana|uganda|"
    r"tanzania|ethiopia|rwanda|thailand|malaysia|taiwan|hong ?kong|nepal|sri lanka|kazakhstan|"
    r"armenia|azerbaijan|moldova|macedonia|albania|bosnia|montenegro|kosovo|slovenia)\b",
    re.I,
)

# European places named in ELSEWHERE_RE - a remote role scoped to any of
# these is hireable from Portugal (César: "european countries ok").
EU_COUNTRY_RE = re.compile(
    r"\b(uk|united kingdom|england|scotland|wales|london|manchester|"
    r"germany|berlin|munich|hamburg|cologne|frankfurt|france|paris|lyon|"
    r"spain|madrid|barcelona|italy|milan|netherlands|amsterdam|rotterdam|"
    r"poland|warsaw|ireland|dublin|austria|vienna|switzerland|zurich|"
    r"sweden|stockholm|denmark|copenhagen|norway|oslo|finland|helsinki|"
    r"estonia|tallinn|latvia|lithuania|czech|prague|slovakia|hungary|budapest|"
    r"romania|bulgaria|greece|croatia|serbia|ukraine|belgium|brussels|"
    r"luxembourg|malta|cyprus|iceland|moldova|macedonia|albania|bosnia|"
    r"montenegro|kosovo|slovenia)\b", re.I)

def classify(location_text, remote=None, restrictions=None, timezones=None):
    """Bucket a job by Portugal hireability.

    Returns one of: 'pt', 'eu', 'ww', 'tiebreak', or None (drop).
    Filter-out policy: drop ONLY on explicit negative evidence - a stated
    scope that excludes Portugal. Anything ambiguous survives as 'tiebreak'
    and is surfaced in the Unsure tier. Negative evidence only ever comes
    from location/restriction fields, never from job descriptions.
    """
    loc = (location_text or "").strip()

    # Structured country restrictions (Himalayas) are authoritative.
    if restrictions:
        joined = ", ".join(restrictions)
        if PT_RE.search(joined):
            return "pt"
        if EU_RE.search(joined):
            return "eu"
        if WW_RE.search(joined):
            return "ww"
        return None  # explicit country list without Portugal
    if PT_RE.search(loc):
        return "pt"
    if EU_RE.search(loc):
        return "eu"
    if WW_RE.search(loc):
        return "ww"
    # Timezones are NOT a criterion (César, 2026-08-29): if they accept
    # Portugal but want 10pm shifts, that's his call to make, not the radar's.
    if loc:
        if ELSEWHERE_RE.search(loc):
            # César's rule (2026-08-30): a named European place is PT-hireable
            # (remote from anywhere in Europe); a named NON-European scope
            # ("Americas Remote", "Remote - US", "APAC") is explicit negative
            # evidence and cuts. Citizenship/onsite nuance is the judge's job.
            eu_place = EU_COUNTRY_RE.search(loc)
            remote_ok = REMOTE_RE.search(loc) or remote is True
            if eu_place:
                return "eu" if remote_ok else "tiebreak"
            return None  # non-European scope, remote or not
        return "tiebreak"  # a place or scope we can't read - surface it
    # No location info at all.
    if remote is False:
        return None  # explicitly on-site, somewhere unstated
    return "tiebreak"

TAG_RE = re.compile(r"<[^>]+>")

def strip_html(s, limit=12000):
    return unescape(TAG_RE.sub(" ", s or ""))[:limit]

SAL_RE = re.compile(
    r"(?:[€$£]\s?\d{1,3}(?:[,.]\d{3})+\s?(?:-|–|—|to|a)\s?[€$£]?\s?\d{1,3}(?:[,.]\d{3})+"
    r"|[€$£]\s?\d{2,3}[,.]\d{3}\b"
    r"|\d{2,3}\s?[kK]\s?(?:-|–|—|to)\s?[€$£]?\s?\d{2,3}\s?[kK])"
)

def find_salary(*texts):
    for t in texts:
        if not t:
            continue
        m = SAL_RE.search(t)
        if m:
            return m.group(0).strip()
    return None

XP_RE = re.compile(
    r"(\d{1,2})\s*(?:\+|\s*(?:-|–|—|to)\s*(\d{1,2}))?\s*\+?\s*"
    r"(?:years?|yrs?)(?:['’]| of| in| relevant| professional| hands.on|\b)", re.I)
XP_CTX = re.compile(r"experience|track record|working (?:in|as|with)|in (?:product|ux|design)", re.I)

def find_xp(text):
    """'5+ years of experience' -> '5+y'. Context-checked, capped at 20."""
    best = None
    for m in XP_RE.finditer(text or ""):
        lo = int(m.group(1))
        if not 1 <= lo <= 20:
            continue
        window = (text[max(0, m.start() - 60):m.end() + 60])
        if not XP_CTX.search(window):
            continue
        hi = m.group(2)
        val = (lo, int(hi) if hi else None)
        if best is None or val[0] > best[0]:
            best = val
    if best is None:
        return None
    lo, hi = best
    return f"{lo}–{hi}y" if hi else f"{lo}+y"

def market_label(bucket, loc):
    l = loc or ""
    if bucket == "pt":
        return "Lisbon" if re.search(r"lisbon|lisboa", l, re.I) and not re.search(r",", l) else "Portugal"
    if bucket == "eu":
        return "EMEA" if re.search(r"emea", l, re.I) else "Europe"
    if bucket == "ww":
        return "Worldwide"
    return (l[:26] + "…" if len(l) > 27 else l) or "Unclear"

def fmt_range(lo, hi, cur=""):
    def fmt(n):
        try:
            n = float(n)
        except (TypeError, ValueError):
            return None
        return f"{int(n/1000)}k" if n >= 1000 else str(int(n))
    lo, hi = fmt(lo), fmt(hi)
    sym = {"USD": "$", "EUR": "€", "GBP": "£"}.get(cur, cur or "")
    if lo and hi and lo != hi:
        return f"{sym}{lo}–{hi}"
    if lo or hi:
        return f"{sym}{lo or hi}"
    return None

MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}

def parse_posted(s):
    """Posted date in any of the sources' formats -> aware datetime or None."""
    if not s:
        return None
    s = str(s).strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                            tzinfo=timezone.utc)
        except ValueError:
            return None
    m = re.search(r"(\d{1,2}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* (\d{4})", s)
    if m:
        try:
            return datetime(int(m.group(3)), MONTHS[m.group(2)], int(m.group(1)),
                            tzinfo=timezone.utc)
        except ValueError:
            return None
    return None

# ---------------------------------------------------------------- sources
# Each returns a list of dicts:
# {source, company, title, url, location, remote, salary, posted,
#  restrictions, timezones, desc}

def src_remotive():
    out = []
    d = fetch_json("https://remotive.com/api/remote-jobs?category=design")
    for j in d.get("jobs", []):
        out.append({
            "source": "remotive", "company": j.get("company_name"),
            "title": j.get("title"), "url": j.get("url"),
            "location": j.get("candidate_required_location"),
            "remote": True, "salary": (j.get("salary") or "").strip() or None,
            "posted": j.get("publication_date"),
            "desc": strip_html(j.get("description")), "raw": j.get("description") or "",
        })
    return out

def src_himalayas():
    out = []
    url = "https://himalayas.app/jobs/api?limit=100"
    for _ in range(5):  # up to 500 newest jobs
        d = fetch_json(url)
        for j in d.get("jobs", []):
            sal = fmt_range(j.get("minSalary"), j.get("maxSalary"), j.get("currency"))
            if sal and j.get("salaryPeriod") == "hourly":
                sal += "/h"
            out.append({
                "source": "himalayas", "company": j.get("companyName"),
                "title": j.get("title"),
                "url": j.get("applicationLink") or j.get("guid"),
                "location": ", ".join(j.get("locationRestrictions") or []),
                "remote": True, "salary": sal,
                "posted": datetime.fromtimestamp(j["pubDate"], timezone.utc).isoformat() if j.get("pubDate") else None,
                "restrictions": j.get("locationRestrictions") or None,
                "timezones": j.get("timezoneRestrictions") or None,
                "desc": strip_html(j.get("description")), "raw": j.get("description") or "",
            })
        cur = d.get("nextCursor")
        if not cur:
            break
        url = f"https://himalayas.app/jobs/api?limit=100&cursor={cur}"
    return out

def src_jobicy():
    out = []
    d = fetch_json("https://jobicy.com/api/v2/remote-jobs?count=100&tag=design")
    for j in d.get("jobs", []):
        sal = fmt_range(j.get("annualSalaryMin"), j.get("annualSalaryMax"), j.get("salaryCurrency"))
        out.append({
            "source": "jobicy", "company": j.get("companyName"),
            "title": j.get("jobTitle"), "url": j.get("url"),
            "location": j.get("jobGeo"), "remote": True, "salary": sal,
            "posted": j.get("pubDate"),
            "desc": strip_html(j.get("jobDescription")), "raw": j.get("jobDescription") or "",
        })
    return out

def src_workingnomads():
    out = []
    d = fetch_json("https://www.workingnomads.com/api/exposed_jobs/")
    for j in d:
        if (j.get("category_name") or "").lower() != "design":
            continue
        out.append({
            "source": "workingnomads", "company": j.get("company_name"),
            "title": j.get("title"), "url": j.get("url"),
            "location": j.get("location"), "remote": True, "salary": None,
            "posted": j.get("pub_date"),
            "desc": strip_html(j.get("description")), "raw": j.get("description") or "",
        })
    return out

def src_remoteok():
    out = []
    d = fetch_json("https://remoteok.com/api")
    for j in d:
        if not isinstance(j, dict) or not j.get("position"):
            continue
        tags = " ".join(j.get("tags") or [])
        if not (title_ok(j["position"]) or re.search(r"\b(design|ux|ui)\b", tags, re.I)):
            continue
        lo, hi = j.get("salary_min") or 0, j.get("salary_max") or 0
        sal = fmt_range(lo, hi, "USD") if (lo or hi) else None
        out.append({
            "source": "remoteok", "company": j.get("company"),
            "title": j.get("position"), "url": j.get("url"),
            "location": j.get("location"), "remote": True, "salary": sal,
            "posted": j.get("date"),
            "desc": strip_html(j.get("description")), "raw": j.get("description") or "",
        })
    return out

def src_arbeitnow():
    out = []
    d = fetch_json("https://www.arbeitnow.com/api/job-board-api")
    for j in d.get("data", []):
        out.append({
            "source": "arbeitnow", "company": j.get("company_name"),
            "title": j.get("title"), "url": j.get("url"),
            "location": j.get("location"), "remote": bool(j.get("remote")),
            "salary": None,
            "posted": datetime.fromtimestamp(j["created_at"], timezone.utc).isoformat() if j.get("created_at") else None,
            "desc": strip_html(j.get("description")), "raw": j.get("description") or "",
        })
    return out

def src_wwr():
    out = []
    xml = fetch("https://weworkremotely.com/categories/remote-design-jobs.rss")
    root = ET.fromstring(xml)
    for item in root.iter("item"):
        get = lambda tag: (item.findtext(tag) or "").strip()
        raw_title = get("title")
        company, _, title = raw_title.partition(":")
        if not title:
            company, title = "", raw_title
        out.append({
            "source": "weworkremotely", "company": company.strip(),
            "title": title.strip(), "url": get("link"),
            "location": get("region"), "remote": True, "salary": None,
            "posted": get("pubDate") or None,
            "desc": strip_html(get("description")), "raw": get("description"),
        })
    return out

def src_landingjobs():
    out = []
    d = fetch_json("https://landing.jobs/api/v1/jobs?limit=200")
    today = NOW.strftime("%Y-%m-%d")
    for j in d:
        if (j.get("expires_at") or "9999") < today:
            continue
        m = re.search(r"landing\.jobs/at/([^/]+)/", j.get("url") or "")
        company = (m.group(1).replace("-", " ").title() if m else "")
        locs = j.get("locations") or []
        if isinstance(locs, list):
            loc = ", ".join(
                l.get("city") or l.get("name") or l.get("country") or ""
                if isinstance(l, dict) else str(l) for l in locs)
        else:
            loc = str(locs)
        sal = fmt_range(j.get("gross_salary_low"), j.get("gross_salary_high"),
                        j.get("currency_code"))
        out.append({
            "source": "landingjobs", "company": company,
            "title": j.get("title"), "url": j.get("url"),
            "location": loc or "Portugal", "remote": bool(j.get("remote")),
            "salary": sal, "posted": j.get("published_at"),
            "updated": j.get("updated_at"),
            "desc": strip_html(j.get("role_description")),
        })
    return out

# --- ATS watchlist -------------------------------------------------

DJW_ATS_RE = re.compile(
    r'https?://[^"\']*(?:greenhouse\.io|lever\.co|ashbyhq\.com|workable\.com|'
    r'recruitee\.com|teamtailor\.com|bamboohr\.com|breezy\.hr|myworkdayjobs\.com|'
    r'smartrecruiters\.com|applytojob\.com|ats\.rippling\.com|jobs\.personio\.|'
    r'pinpointhq\.com|careers-page\.com|freshteam\.com|zohorecruit\.)[^"\']*')

def src_designjobsworld():
    """designjobs.world republishes ATS design roles with JSON-LD JobPosting
    markup and a direct apply link - their recent-jobs sitemap is the delta.
    Using the ATS link as our url makes the resolver learn each board."""
    xml = fetch("https://designjobs.world/recent_jobs.xml")
    urls = re.findall(r"<loc>(https://designjobs\.world/jobs/[^<]+)</loc>", xml)[:250]
    out = []
    for u in urls:
        try:
            h = fetch(u, timeout=15)
        except Exception:
            continue
        ld = None
        for x in re.findall(r'<script type="application/ld\+json">(.*?)</script>', h, re.S):
            try:
                d = json.loads(x)
                if d.get("@type") == "JobPosting":
                    ld = d
                    break
            except Exception:
                pass
        if not ld or not ld.get("title"):
            continue
        m = DJW_ATS_RE.search(h)
        locs = ld.get("jobLocation") or []
        if isinstance(locs, dict):
            locs = [locs]
        parts = []
        for l in locs:
            a = (l.get("address") or {}) if isinstance(l, dict) else {}
            if isinstance(a, dict):
                parts += [a.get("addressLocality"), a.get("addressCountry")]
        alr = ld.get("applicantLocationRequirements") or []
        if isinstance(alr, dict):
            alr = [alr]
        parts += [x.get("name") for x in alr if isinstance(x, dict)]
        loc = ", ".join(dict.fromkeys(str(p) for p in parts if p))
        raw = ld.get("description") or ""
        out.append({
            "source": "designjobsworld",
            "company": (ld.get("hiringOrganization") or {}).get("name") or "?",
            "title": ld.get("title"), "url": (m.group(0) if m else u),
            "location": loc,
            "remote": bool(re.search(r"remote|anywhere|telecommute",
                                     f"{loc} {ld.get('jobLocationType') or ''}", re.I)) or None,
            "salary": None, "posted": ld.get("datePosted"),
            "desc": strip_html(raw), "raw": raw,
        })
    return out

def src_greenhouse(slug, eu=False):
    # the standard boards-api serves EU-hosted orgs too (verified: the .eu API
    # host doesn't even resolve) - eu is accepted only for registry compat
    d = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    out = []
    for j in d.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        raw = unescape(j.get("content") or "")
        out.append({
            "source": f"greenhouse/{slug}",
            "company": j.get("company_name") or slug.title(),
            "title": j.get("title"), "url": j.get("absolute_url"),
            "location": loc, "remote": bool(REMOTE_RE.search(loc)),
            "salary": None, "posted": j.get("first_published"),
            "updated": j.get("updated_at"),
            "desc": strip_html(raw), "raw": raw,
        })
    return out

def src_lever(slug, eu=False):
    api = "api.eu.lever.co" if eu else "api.lever.co"
    d = fetch_json(f"https://{api}/v0/postings/{slug}?mode=json")
    out = []
    for j in d:
        cats = j.get("categories") or {}
        loc = ", ".join(filter(None, [cats.get("location")] + (j.get("additionalLocations") or [])))
        remote = (j.get("workplaceType") == "remote") or bool(REMOTE_RE.search(loc))
        out.append({
            "source": f"lever/{slug}", "company": slug.title(),
            "title": j.get("text"), "url": j.get("hostedUrl"),
            "location": loc, "remote": remote, "salary": None,
            "posted": datetime.fromtimestamp(j["createdAt"] / 1000, timezone.utc).isoformat() if j.get("createdAt") else None,
            "desc": strip_html(j.get("description")), "raw": j.get("description") or "",
        })
    return out

def src_ashby(slug):
    d = fetch_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
    out = []
    for j in d.get("jobs", []):
        if not j.get("isListed", True):
            continue
        locs = [j.get("location") or ""] + [s.get("location", "") for s in (j.get("secondaryLocations") or [])]
        loc = ", ".join(filter(None, locs))
        comp = (j.get("compensation") or {}).get("compensationTierSummary")
        out.append({
            "source": f"ashby/{slug}", "company": slug.title(),
            "title": j.get("title"), "url": j.get("jobUrl"),
            "location": loc, "remote": bool(j.get("isRemote")),
            "salary": comp, "posted": j.get("publishedAt"),
            "desc": strip_html(j.get("descriptionPlain")),
            "raw": j.get("descriptionPlain") or "",
        })
    return out

# --- ATS platform fetchers (RemoteRocketship-class coverage) -------------
# Every platform below serves a public, keyless feed per company. The
# companies come from data/companies.json (the registry), which discovery
# and the census seed keep growing.

REGISTRY_FILE = ROOT / "data" / "companies.json"
try:
    REGISTRY = json.loads(REGISTRY_FILE.read_text())
except Exception:
    REGISTRY = {}

def src_workday(tenant, host, site, company):
    """Workday CXS API. Detail-fetches design roles only - boards are huge."""
    out, seen = [], set()
    for q in ("design", "ux"):
        offset = 0
        for _ in range(5):
            body = json.dumps({"appliedFacets": {}, "limit": 20,
                               "offset": offset, "searchText": q}).encode()
            req = urllib.request.Request(
                f"https://{host}/wday/cxs/{tenant}/{site}/jobs", data=body,
                headers={"User-Agent": UA, "Content-Type": "application/json",
                         "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            for p in d.get("jobPostings") or []:
                ep = p.get("externalPath")
                if not ep or ep in seen or not title_ok(p.get("title")):
                    continue
                seen.add(ep)
                try:
                    det = fetch_json(f"https://{host}/wday/cxs/{tenant}/{site}{ep}").get("jobPostingInfo") or {}
                except Exception:
                    det = {}
                loc = ", ".join(filter(None, [det.get("location")] +
                                       (det.get("additionalLocations") or [])))
                raw = det.get("jobDescription") or ""
                out.append({
                    "source": f"workday/{tenant}", "company": company,
                    "title": p.get("title"),
                    "url": det.get("externalUrl") or f"https://{host}/{site}{ep}",
                    "location": loc,
                    "remote": bool(re.search(r"remote", loc, re.I)) or None,
                    "salary": None, "posted": det.get("startDate"),
                    "desc": strip_html(raw), "raw": raw,
                })
            offset += 20
            if offset >= (d.get("total") or 0):
                break
    return out

def src_bamboohr(slug, company):
    d = fetch_json(f"https://{slug}.bamboohr.com/careers/list")
    out = []
    for j in d.get("result") or []:
        title = j.get("jobOpeningName")
        loc = j.get("atsLocation") or j.get("location") or {}
        loc_s = ", ".join(filter(None, [loc.get("city"), loc.get("state"),
                                        loc.get("country")]))
        raw = ""
        posted = None
        if title_ok(title):
            try:
                det = fetch_json(f"https://{slug}.bamboohr.com/careers/{j['id']}/detail")
                jo = (det.get("result") or {}).get("jobOpening") or {}
                raw = unescape(jo.get("description") or "")
                posted = jo.get("datePosted")
            except Exception:
                pass
        remote = j.get("isRemote")
        if remote is None:
            remote = bool(re.search(r"remote", f"{title} {loc_s}", re.I)) or None
        out.append({
            "source": f"bamboohr/{slug}", "company": company,
            "title": title,
            "url": f"https://{slug}.bamboohr.com/careers/{j['id']}",
            "location": loc_s, "remote": remote, "salary": None,
            "posted": posted, "desc": strip_html(raw), "raw": raw,
        })
    return out

def src_breezy(slug, company):
    d = fetch_json(f"https://{slug}.breezy.hr/json")
    out = []
    for j in d:
        loc = j.get("location") or {}
        raw = j.get("description") or ""
        job = {
            "source": f"breezy/{slug}", "company": company,
            "title": j.get("name"), "url": j.get("url"),
            "location": loc.get("name") or "",
            "remote": bool(loc.get("is_remote")) or None,
            "salary": (j.get("salary") or "").strip() or None,
            "posted": j.get("published_date"),
            "desc": strip_html(raw), "raw": raw,
        }
        # the list feed carries no JD - read the posting for design roles
        if title_ok(job["title"]) and not raw and job["url"]:
            orig = fetch_original(job["url"], company, job["title"])
            if orig.get("text"):
                job["desc"] = job["raw"] = orig["text"]
        out.append(job)
    return out

def src_workable(slug, company):
    d = fetch_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true")
    out = []
    for j in d.get("jobs", []):
        locs = j.get("locations") or []
        loc = ", ".join(dict.fromkeys(
            ", ".join(filter(None, [l.get("city"), l.get("country")]))
            for l in locs if isinstance(l, dict))) or \
            ", ".join(filter(None, [j.get("city"), j.get("country")]))
        raw = j.get("description") or ""
        out.append({
            "source": f"workable/{slug}", "company": d.get("name") or company,
            "title": j.get("title"), "url": j.get("url"),
            "location": loc, "remote": bool(j.get("telecommuting")) or None,
            "salary": None, "posted": j.get("published_on") or j.get("created_at"),
            "desc": strip_html(raw), "raw": raw,
        })
    return out

def src_smartrecruiters(slug, company):
    """Design roles only - big consultancies list 1000+ postings."""
    out, offset = [], 0
    for _ in range(5):  # 500 newest postings
        d = fetch_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&offset={offset}")
        items = d.get("content") or []
        for j in items:
            title = j.get("name")
            if not title_ok(title):
                continue
            loc = j.get("location") or {}
            loc_s = loc.get("fullLocation") or ", ".join(
                filter(None, [loc.get("city"), (loc.get("country") or "").upper()]))
            raw = ""
            try:
                det = fetch_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{j['id']}")
                secs = (det.get("jobAd") or {}).get("sections") or {}
                raw = " ".join((secs.get(k) or {}).get("text") or "" for k in
                               ("companyDescription", "jobDescription",
                                "qualifications", "additionalInformation"))
            except Exception:
                pass
            out.append({
                "source": f"smartrecruiters/{slug}",
                "company": (j.get("company") or {}).get("name") or company,
                "title": title,
                "url": f"https://jobs.smartrecruiters.com/{slug}/{j['id']}",
                "location": loc_s, "remote": bool(loc.get("remote")) or None,
                "salary": None, "posted": j.get("releasedDate"),
                "desc": strip_html(raw), "raw": raw,
            })
        offset += 100
        if not items or offset >= (d.get("totalFound") or 0):
            break
    return out

def src_teamtailor(host, company):
    xml = fetch(f"https://{host}/jobs.rss")
    root = ET.fromstring(xml)
    out = []
    for item in root.iter("item"):
        get = lambda tag: (item.findtext(tag) or "").strip()
        title, link = get("title"), get("link")
        raw = get("description")
        loc = ", ".join(filter(None, [get("location"), get("region"), get("country")]))
        job = {
            "source": f"teamtailor/{host}", "company": company,
            "title": title, "url": link, "location": loc,
            "remote": bool(re.search(r"remote", f"{title} {loc}", re.I)) or None,
            "salary": None, "posted": get("pubDate") or None,
            "desc": strip_html(raw), "raw": raw,
        }
        if title_ok(title) and link and not loc:
            orig = fetch_original(link, company, title)
            if orig.get("loc"):
                job["location"] = orig["loc"]
            if orig.get("salary"):
                job["salary"] = orig["salary"]
            if orig.get("posted"):
                job["posted"] = orig["posted"]
        out.append(job)
    return out

_CHROME_RE = re.compile(r"(?is)<(script|style|head|nav|footer)\b[^>]*>.*?</\1>")

def personio_page_jd(url):
    """Personio's feed sometimes serves an empty <description> (Peratera: the
    XML endpoint 404s and search.json omits the body). The JD is still on the
    job page - fetch it, drop the chrome, keep the visible text. strip_html
    alone leaves <style>/<script> inner text behind, so cut those blocks first."""
    try:
        html = fetch(url)
    except Exception:
        return ""
    txt = re.sub(r"\s+", " ", strip_html(_CHROME_RE.sub(" ", html))).strip()
    return txt if len(txt) >= 200 else ""

def src_personio(host, company):
    try:
        xml = fetch(f"https://{host}/xml")
    except Exception:
        # some personio sites disable the XML feed - search.json still serves
        out = []
        for j in fetch_json(f"https://{host}/search.json"):
            loc = ", ".join(dict.fromkeys(j.get("offices") or []))
            raw = j.get("description") or ""
            url = f"https://{host}/job/{j.get('id')}"
            desc = strip_html(raw)
            if len(desc) < 200:  # feed gave no body - read the page itself
                page = personio_page_jd(url)
                if page:
                    desc = raw = page
            out.append({
                "source": f"personio/{host.split('.')[0]}",
                "company": (j.get("subcompany") or "").strip() or company,
                "title": (j.get("name") or "").strip(),
                "url": url,
                "location": loc,
                "remote": bool(re.search(r"remote", loc, re.I)) or None,
                "salary": None, "posted": None,
                "desc": desc, "raw": raw,
            })
        return out
    root = ET.fromstring(xml)
    out = []
    for pos in root.iter("position"):
        offices = [o.text.strip() for o in pos.iter("office") if o.text]
        loc = ", ".join(dict.fromkeys(offices))
        raw = " ".join(jd.findtext("value") or "" for jd in pos.iter("jobDescription"))
        pid = (pos.findtext("id") or "").strip()
        url = f"https://{host}/job/{pid}"
        desc = strip_html(raw)
        if len(desc) < 200:
            page = personio_page_jd(url)
            if page:
                desc = raw = page
        out.append({
            "source": f"personio/{host.split('.')[0]}",
            "company": (pos.findtext("subcompany") or "").strip() or company,
            "title": (pos.findtext("name") or "").strip(),
            "url": url,
            "location": loc,
            "remote": bool(re.search(r"remote", loc, re.I)) or None,
            "salary": None, "posted": (pos.findtext("createdAt") or "").strip() or None,
            "desc": desc, "raw": raw,
        })
    return out

def src_pinpoint(slug, company):
    d = fetch_json(f"https://{slug}.pinpointhq.com/postings.json")
    out = []
    for j in (d.get("data") if isinstance(d, dict) else d) or []:
        loc = j.get("location") or {}
        loc_s = loc.get("name") if isinstance(loc, dict) else str(loc or "")
        raw = j.get("description") or ""
        wt = (j.get("workplace_type") or "").lower()
        out.append({
            "source": f"pinpoint/{slug}", "company": company,
            "title": j.get("title"),
            "url": j.get("url") or f"https://{slug}.pinpointhq.com/en/postings/{j.get('id')}",
            "location": loc_s or "",
            "remote": ("remote" in wt) or bool(re.search(r"remote", loc_s or "", re.I)) or None,
            "salary": (j.get("compensation") or "").strip() or None,
            "posted": j.get("published_at") or j.get("created_at"),
            "desc": strip_html(raw), "raw": raw,
        })
    return out

def src_join(slug, company):
    html = fetch(f"https://join.com/companies/{slug}")
    m = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return []
    try:
        items = (json.loads(m.group(1))["props"]["pageProps"]
                 ["initialState"]["jobs"]["items"])
    except (KeyError, TypeError, ValueError):
        return []
    out = []
    for j in items:
        city = j.get("city") or {}
        loc = ", ".join(filter(None, [city.get("cityName"), city.get("countryName")]))
        if (j.get("remoteType") or "").upper() == "ANYWHERE":
            loc = ", ".join(filter(None, [loc, "Worldwide"]))
        url = (f"https://join.com/companies/{slug}/{j['idParam']}"
               if j.get("idParam") else None)
        job = {
            "source": f"join/{slug}", "company": company,
            "title": j.get("title"), "url": url,
            "location": loc,
            "remote": ((j.get("workplaceType") or "").upper() == "REMOTE") or None,
            "salary": None, "posted": j.get("createdAt"),
            "desc": "", "raw": "",
        }
        if title_ok(job["title"]) and url:
            orig = fetch_original(url, company, job["title"])
            if orig.get("text"):
                job["desc"] = job["raw"] = orig["text"]
            if orig.get("posted"):
                job["posted"] = job.get("posted") or orig["posted"]
        out.append(job)
    return out

def src_manatal(slug, company):
    base = f"https://www.careers-page.com/{slug}"
    html = fetch(base)
    out, seen = [], set()
    for m in re.finditer(r'<a\b[^>]*href="([^"]*/job/[A-Za-z0-9]+)"[^>]*>(.*?)</a>', html, re.S | re.I):
        u = urllib.parse.urljoin(base, m.group(1))
        if u in seen:
            continue
        seen.add(u)
        title = re.sub(r"\s+", " ", strip_html(m.group(2), 200)).strip()
        job = {"source": f"manatal/{slug}", "company": company, "title": title,
               "url": u, "location": "", "remote": None, "salary": None,
               "posted": None, "desc": "", "raw": ""}
        if title_ok(title):
            orig = fetch_original(u, company, title)
            if orig.get("text"):
                job["desc"] = job["raw"] = orig["text"]
            if orig.get("loc"):
                job["location"] = orig["loc"]
            if orig.get("salary"):
                job["salary"] = orig["salary"]
        out.append(job)
    return out

def src_zoho(sub, tld, company):
    """Zoho Recruit careers page: the full job list rides in a hidden
    input id="jobs", entity-encoded, near the end of a ~1.8MB page."""
    html = fetch(f"https://{sub}.zohorecruit.{tld}/jobs/Careers", timeout=30)
    m = re.search(r'value="([^"]*)"\s*id="jobs"', html)
    if not m:
        m = re.search(r'id="jobs"[^>]*value="([^"]*)"', html)
    if not m:
        return []
    data = json.loads(unescape(m.group(1)))
    out = []
    for j in (data if isinstance(data, list) else data.get("jobs", [])):
        if str(j.get("Publish", "true")).lower() == "false":
            continue
        title = j.get("Job_Opening_Name") or ""
        jid = j.get("id") or j.get("Id")
        slug_name = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-")
        url = f"https://{sub}.zohorecruit.{tld}/jobs/Careers/{jid}/{slug_name}"
        loc = ", ".join(filter(None, [j.get("City"), j.get("State"), j.get("Country")]))
        remote = bool(re.search(r"remote", f"{title} {j.get('Job_Type') or ''} {loc}", re.I)) or None
        desc = f"Experience asked: {j.get('Work_Experience')}" if j.get("Work_Experience") else ""
        job = {"source": f"zoho/{sub}", "company": company, "title": title,
               "url": url, "location": loc, "remote": remote, "salary": None,
               "posted": j.get("Date_Opened"), "desc": desc, "raw": ""}
        if title_ok(title):
            orig = fetch_original(url, company, title)
            if orig.get("text"):
                job["desc"] = job["raw"] = orig["text"]
            if orig.get("loc"):
                job["location"] = orig["loc"]
        out.append(job)
    return out

def src_successfactors(host, company, loc=""):
    """SAP SuccessFactors RMK careers sites (custom domains, server-rendered
    search). One query per design keyword, dedupe by numeric job id."""
    seen, out = set(), []
    # RMK search drops short tokens - "ux" alone misses what "UX Analyst"
    # finds. Query phrases, dedupe by id.
    for kw in ("designer", "design", "UX designer", "UX analyst", "user experience"):
        try:
            html = fetch(f"https://{host}/search/?q={urllib.parse.quote(kw)}"
                         f"&locationsearch={urllib.parse.quote(loc)}", timeout=30)
        except Exception:
            continue
        for m in re.finditer(r'href="(/job/[^"]+/(\d+)/?)"[^>]*class="jobTitle-link"[^>]*>(.*?)</a>',
                             html, re.S):
            path, jid, title = m.group(1), m.group(2), strip_html(m.group(3), 200).strip()
            if jid in seen or not title:
                continue
            seen.add(jid)
            url = f"https://{host}{path}"
            job = {"source": f"successfactors/{host.split('.')[1] if host.count('.') > 1 else host}",
                   "company": company, "title": title, "url": url,
                   "location": loc.title() if loc else "", "remote": None,
                   "salary": None, "posted": None, "desc": "", "raw": ""}
            if title_ok(title):
                orig = fetch_original(url, company, title)
                if orig.get("text"):
                    job["desc"] = job["raw"] = orig["text"]
                if orig.get("loc"):
                    job["location"] = orig["loc"]
                if orig.get("posted"):
                    job["posted"] = orig["posted"]
            out.append(job)
    return out

def src_freshteam(slug, company):
    d = fetch_json(f"https://{slug}.freshteam.com/hire/widgets/jobs.json")
    out = []
    for j in d.get("jobs", []):
        if j.get("deleted") or str(j.get("status", "")).lower() in ("closed", "on_hold"):
            continue
        raw = unescape(j.get("description") or "")
        remote = str(j.get("remote", "")).lower() == "true"
        locs = j.get("preferred_remote_job_locations") or ""
        out.append({
            "source": f"freshteam/{slug}", "company": company,
            "title": j.get("title"), "url": j.get("url"),
            "location": (locs if isinstance(locs, str)
                         else ", ".join(map(str, locs))) or ("Remote" if remote else ""),
            "remote": remote or None, "salary": None,
            "posted": j.get("created_at"),
            "desc": strip_html(raw), "raw": raw,
        })
    return out

def src_rippling(slug, company):
    """Rippling ATS. The official board API works even where the HTML page
    bot-walls (Q4); NEXT_DATA scrape stays as the fallback."""
    try:
        d = fetch_json(f"https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs")
        out = []
        for j in d if isinstance(d, list) else []:
            loc = (j.get("workLocation") or {}).get("label") or ""
            job = {
                "source": f"rippling/{slug}", "company": company,
                "title": j.get("name"), "url": j.get("url"),
                "location": loc,
                "remote": bool(re.search(r"remote", loc, re.I)) or None,
                "salary": None, "posted": None, "desc": "", "raw": "",
            }
            if title_ok(job["title"]) and j.get("uuid"):
                try:
                    det = fetch_json(
                        f"https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs/{j['uuid']}")
                    raw = " ".join(str(v) for v in (det.get("description") or {}).values())
                    job["desc"] = job["raw"] = strip_html(raw)
                    job["salary"] = find_salary(job["desc"])
                except Exception:
                    pass
            out.append(job)
        return out
    except Exception:
        pass
    html = fetch(f"https://ats.rippling.com/{slug}/jobs")
    m = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return []
    def find_items(o):
        if isinstance(o, dict):
            v = o.get("items")
            if (isinstance(v, list) and v and isinstance(v[0], dict)
                    and "name" in v[0] and "url" in v[0]):
                return v
            for x in o.values():
                r = find_items(x)
                if r:
                    return r
        elif isinstance(o, list):
            for x in o:
                r = find_items(x)
                if r:
                    return r
        return None
    out = []
    for j in find_items(json.loads(m.group(1))) or []:
        locs = [l for l in (j.get("locations") or []) if isinstance(l, dict)]
        loc = ", ".join(dict.fromkeys(filter(None, [
            ", ".join(filter(None, [l.get("city"), l.get("country")])) or l.get("name")
            for l in locs])))
        remote = any((l.get("workplaceType") or "").upper() == "REMOTE" for l in locs)
        job = {
            "source": f"rippling/{slug}", "company": company,
            "title": j.get("name"), "url": j.get("url"),
            "location": loc, "remote": remote or None, "salary": None,
            "posted": None, "desc": "", "raw": "",
        }
        if title_ok(job["title"]) and job["url"]:
            orig = fetch_original(job["url"], company, job["title"])
            if orig.get("text"):
                job["desc"] = job["raw"] = orig["text"]
            if orig.get("salary"):
                job["salary"] = orig["salary"]
        out.append(job)
    return out

def src_jazzhr(slug, company):
    """JazzHR - server-rendered list at {slug}.applytojob.com/apply."""
    html = fetch(f"https://{slug}.applytojob.com/apply")
    out = []
    for block in re.findall(r'class="list-group-item"[\s\S]*?</ul>', html):
        am = re.search(r'<a[^>]*href="([^"]*/apply/[A-Za-z0-9]+/[^"]*)"[^>]*>([\s\S]*?)</a>', block)
        if not am:
            continue
        url = am.group(1)
        title = re.sub(r"\s+", " ", strip_html(am.group(2), 150)).strip()
        lm = re.search(r"fa-map-marker[^>]*></i>\s*([^<]{0,80})", block)
        loc = lm.group(1).strip() if lm else ""
        job = {"source": f"jazzhr/{slug}", "company": company, "title": title,
               "url": url, "location": loc,
               "remote": bool(re.search(r"remote", loc, re.I)) or None,
               "salary": None, "posted": None, "desc": "", "raw": ""}
        if title_ok(title):
            orig = fetch_original(url, company, title)
            if orig.get("text"):
                job["desc"] = job["raw"] = orig["text"]
            if orig.get("loc") and not loc:
                job["location"] = orig["loc"]
        out.append(job)
    return out

def src_comeet(slug, uid, token, company):
    """Comeet - positions API; the page-embedded token is re-read if absent."""
    if not token:
        page = fetch(f"https://www.comeet.com/jobs/{slug}/{uid}")
        tm = re.search(r'"token"\s*:\s*"([A-F0-9]{20,40})"', page)
        if not tm:
            return []
        token = tm.group(1)
    d = fetch_json(f"https://www.comeet.co/careers-api/2.0/company/{uid}/positions?token={token}")
    out = []
    for p in d:
        loc = p.get("location") or {}
        loc_s = ", ".join(filter(None, [loc.get("city"), loc.get("country")])) \
            or (loc.get("name") or "")
        job = {
            "source": f"comeet/{slug}", "company": company,
            "title": p.get("name"),
            "url": p.get("url_active_page") or p.get("url_comeet_hosted_page"),
            "location": loc_s, "remote": bool(loc.get("is_remote")) or None,
            "salary": None, "posted": None, "desc": "", "raw": "",
        }
        if title_ok(job["title"]) and job["url"]:
            orig = fetch_original(job["url"], company, job["title"])
            if orig.get("text"):
                job["desc"] = job["raw"] = orig["text"]
        out.append(job)
    return out

def registry_tasks():
    """(name, fetch-callable) per registry company. Names match job sources."""
    tasks = []
    def label(e, default):
        return e.get("company") or default
    def brand(jobs, e):
        c = e.get("company")
        return [dict(j, company=c or j.get("company")) for j in jobs] if c else jobs
    for e in REGISTRY.get("workday", []):
        tasks.append((f"workday/{e['tenant']}", lambda e=e: src_workday(
            e["tenant"], e["host"], e["site"], label(e, e["tenant"].title()))))
    for e in REGISTRY.get("bamboohr", []):
        tasks.append((f"bamboohr/{e['slug']}", lambda e=e: src_bamboohr(
            e["slug"], label(e, e["slug"].title()))))
    for e in REGISTRY.get("breezy", []):
        tasks.append((f"breezy/{e['slug']}", lambda e=e: src_breezy(
            e["slug"], label(e, e["slug"].title()))))
    for e in REGISTRY.get("workable", []):
        tasks.append((f"workable/{e['slug']}", lambda e=e: src_workable(
            e["slug"], label(e, e["slug"].title()))))
    for e in REGISTRY.get("smartrecruiters", []):
        tasks.append((f"smartrecruiters/{e['slug']}", lambda e=e: src_smartrecruiters(
            e["slug"], label(e, e["slug"].title()))))
    for e in REGISTRY.get("teamtailor", []):
        tasks.append((f"teamtailor/{e['host']}", lambda e=e: src_teamtailor(
            e["host"], label(e, e["host"].split(".")[0].title()))))
    for e in REGISTRY.get("personio", []):
        tasks.append((f"personio/{e['host'].split('.')[0]}", lambda e=e: src_personio(
            e["host"], label(e, e["host"].split(".")[0].title()))))
    for e in REGISTRY.get("pinpoint", []):
        tasks.append((f"pinpoint/{e['slug']}", lambda e=e: src_pinpoint(
            e["slug"], label(e, e["slug"].title()))))
    for e in REGISTRY.get("join", []):
        tasks.append((f"join/{e['slug']}", lambda e=e: src_join(
            e["slug"], label(e, e["slug"].title()))))
    for e in REGISTRY.get("manatal", []):
        tasks.append((f"manatal/{e['slug']}", lambda e=e: src_manatal(
            e["slug"], label(e, e["slug"].title()))))
    for e in REGISTRY.get("recruitee", []):
        tasks.append((f"recruitee/{e['slug']}", lambda e=e: brand(src_recruitee(e["slug"]), e)))
    for e in REGISTRY.get("greenhouse", []):
        tasks.append((f"greenhouse/{e['slug']}", lambda e=e: brand(
            src_greenhouse(e["slug"], e.get("eu", False)), e)))
    for e in REGISTRY.get("lever", []):
        tasks.append((f"lever/{e['slug']}", lambda e=e: brand(
            src_lever(e["slug"], e.get("eu", False)), e)))
    for e in REGISTRY.get("ashby", []):
        tasks.append((f"ashby/{e['slug']}", lambda e=e: brand(src_ashby(e["slug"]), e)))
    for e in REGISTRY.get("rippling", []):
        tasks.append((f"rippling/{e['slug']}", lambda e=e: src_rippling(
            e["slug"], label(e, e["slug"].title()))))
    for e in REGISTRY.get("jazzhr", []):
        tasks.append((f"jazzhr/{e['slug']}", lambda e=e: src_jazzhr(
            e["slug"], label(e, e["slug"].title()))))
    for e in REGISTRY.get("zoho", []):
        tasks.append((f"zoho/{e['sub']}", lambda e=e: src_zoho(
            e["sub"], e.get("tld", "com"), label(e, e["sub"].title()))))
    for e in REGISTRY.get("successfactors", []):
        tasks.append((f"successfactors/{e['host']}", lambda e=e: src_successfactors(
            e["host"], label(e, e["host"]), e.get("loc", ""))))
    for e in REGISTRY.get("freshteam", []):
        tasks.append((f"freshteam/{e['slug']}", lambda e=e: src_freshteam(
            e["slug"], label(e, e["slug"].title()))))
    for e in REGISTRY.get("comeet", []):
        tasks.append((f"comeet/{e['slug']}", lambda e=e: src_comeet(
            e["slug"], e["uid"], e.get("token"), label(e, e["slug"].title()))))
    return tasks

# --- registry discovery: search engines teach us new company boards ------

DISCOVERY_SITES = [
    ("workable", "apply.workable.com"), ("bamboohr", "bamboohr.com"),
    ("breezy", "breezy.hr"), ("teamtailor", "teamtailor.com"),
    ("personio", "jobs.personio.com"), ("pinpoint", "pinpointhq.com"),
    ("smartrecruiters", "jobs.smartrecruiters.com"), ("recruitee", "recruitee.com"),
    ("manatal", "careers-page.com"), ("join", "join.com"),
    ("ashby", "jobs.ashbyhq.com"), ("greenhouse", "boards.greenhouse.io"),
    ("lever", "jobs.lever.co"), ("workday", "myworkdayjobs.com"),
    ("rippling", "ats.rippling.com"), ("jazzhr", "applytojob.com"),
    ("comeet", "comeet.com"), ("freshteam", "freshteam.com"),
    ("zoho", "zohorecruit.com"),
]

def parse_board_url(u):
    """URL on any known ATS platform -> (platform, registry entry) or None."""
    m = re.search(r"https?://([a-z0-9-]+)\.freshteam\.com", u, re.I)
    if m and m.group(1) not in ("www", "support"):
        return "freshteam", {"slug": m.group(1).lower()}
    m = re.search(r"https?://([a-z0-9-]+)\.zohorecruit\.(com|eu)", u, re.I)
    if m and m.group(1) not in ("www", "support"):
        return "zoho", {"sub": m.group(1).lower(), "tld": m.group(2).lower()}
    m = re.search(r"https?://([a-z0-9-]+)(\.wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)", u, re.I)
    if m:
        return "workday", {"tenant": m.group(1).lower(),
                           "host": (m.group(1) + m.group(2)).lower() + ".myworkdayjobs.com",
                           "site": m.group(3)}
    for plat, pat in [
            ("bamboohr", r"https?://([a-z0-9-]+)\.bamboohr\.com"),
            ("breezy", r"https?://([a-z0-9-]+)\.breezy\.hr"),
            ("pinpoint", r"https?://([a-z0-9-]+)\.pinpointhq\.com"),
            ("recruitee", r"https?://([a-z0-9-]+)\.recruitee\.com")]:
        m = re.search(pat, u, re.I)
        if m and m.group(1).lower() != "www":
            return plat, {"slug": m.group(1).lower()}
    m = re.search(r"https?://apply\.workable\.com/([a-z0-9-]+)/", u, re.I)
    if m and m.group(1).lower() not in ("j", "api"):
        return "workable", {"slug": m.group(1).lower()}
    m = re.search(r"https?://jobs\.smartrecruiters\.com/([A-Za-z0-9-]+)/", u)
    if m:
        return "smartrecruiters", {"slug": m.group(1)}
    m = re.search(r"https?://([a-z0-9-]+)\.teamtailor\.com", u, re.I)
    if m and m.group(1).lower() != "www":
        return "teamtailor", {"host": m.group(1).lower() + ".teamtailor.com"}
    m = re.search(r"https?://([a-z0-9-]+\.jobs\.personio\.(?:com|de))/", u, re.I)
    if m:
        return "personio", {"host": m.group(1).lower()}
    m = re.search(r"https?://(?:www\.)?join\.com/companies/([a-z0-9-]+)", u, re.I)
    if m:
        return "join", {"slug": m.group(1).lower()}
    m = re.search(r"https?://(?:www\.)?careers-page\.com/([a-z0-9-]+)", u, re.I)
    if m:
        return "manatal", {"slug": m.group(1).lower()}
    m = re.search(r"https?://(?:job-boards|boards)\.(eu\.)?greenhouse\.io/([A-Za-z0-9_-]+)", u)
    if m and m.group(2) != "embed":
        ent = {"slug": m.group(2).lower()}
        if m.group(1):  # EU-hosted greenhouse boards live on a separate API host
            ent["eu"] = True
        return "greenhouse", ent
    m = re.search(r"https?://jobs(\.eu)?\.lever\.co/([A-Za-z0-9_-]+)", u)
    if m:
        ent = {"slug": m.group(2).lower()}
        if m.group(1):
            ent["eu"] = True
        return "lever", ent
    m = re.search(r"https?://jobs\.ashbyhq\.com/([A-Za-z0-9_-]+)", u)
    if m:
        return "ashby", {"slug": m.group(1)}
    m = re.search(r"https?://ats\.rippling\.com/([A-Za-z0-9_-]+)/jobs", u, re.I)
    if m:
        return "rippling", {"slug": m.group(1).lower()}
    m = re.search(r"https?://([a-z0-9-]+)\.applytojob\.com", u, re.I)
    if m and m.group(1).lower() != "www":
        return "jazzhr", {"slug": m.group(1).lower()}
    m = re.search(r"https?://(?:www\.)?comeet\.com/jobs/([a-z0-9-]+)/(\d{2}\.[0-9A-F]{3})", u, re.I)
    if m:
        return "comeet", {"slug": m.group(1).lower(), "uid": m.group(2)}
    return None

def registry_add(reg, platform, ent):
    def key(e):
        return tuple(sorted((k, v) for k, v in e.items()
                            if k in ("slug", "host", "tenant", "site")))
    lst = reg.setdefault(platform, [])
    if key(ent) in {key(e) for e in lst}:
        return False
    lst.append(ent)
    return True

def discover(reg):
    """Two site: searches a night, rotating platforms. New boards join the
    registry and get polled from the next run on."""
    meta = reg.setdefault("_discovery", {"next": 0})
    i = meta.get("next", 0)
    added = 0
    for step in range(2):
        plat, dom = DISCOVERY_SITES[(i + step) % len(DISCOVERY_SITES)]
        try:
            links = ddg_links(f'site:{dom} "product designer" OR "ux designer" remote')
        except SearchThrottled:
            raise  # IP is bot-walled - do not advance, retry this platform next run
        except Exception:
            meta["next"] = (i + step + 1) % len(DISCOVERY_SITES)
            continue
        meta["next"] = (i + step + 1) % len(DISCOVERY_SITES)
        time.sleep(10)
        for u in links[:20]:
            pe = parse_board_url(u)
            if pe and pe[0] == plat and registry_add(
                    reg, plat, dict(pe[1], via="ddg", added=RUN_ID)):
                added += 1
    return added

# ------------------------------------------------- direct apply-link hunting

AGGREGATORS = {"remotive", "himalayas", "jobicy", "weworkremotely",
               "workingnomads", "arbeitnow", "remoteok"}
ATS_LINK_RE = re.compile(
    r'https?://[^\s"\'<>]*(?:greenhouse\.io|lever\.co|ashbyhq\.com|workable\.com|'
    r'recruitee\.com|teamtailor\.com|bamboohr\.com|smartrecruiters\.com|breezy\.hr|'
    r'personio\.(?:de|com)|jobvite\.com|join\.com|homerun\.co)[^\s"\'<>]*', re.I)
CAREERS_LINK_RE = re.compile(r'href="(https?://[^"]*(?:careers|jobs|apply|join)[^"]*)"', re.I)
SOCIAL_RE = re.compile(r'weworkremotely|remotive|jobicy|himalayas|remoteok|workingnomads|'
                       r'twitter|x\.com|facebook|linkedin|instagram|youtube|tiktok|t\.me', re.I)

ATS_CACHE_FILE = ROOT / "data" / "ats_cache.json"
try:
    ATS_CACHE = json.loads(ATS_CACHE_FILE.read_text())
except Exception:
    ATS_CACHE = {}
_board_jobs_memo = {}

def desc_ats_link(raw):
    m = ATS_LINK_RE.search(unescape(raw or ""))
    return m.group(0).rstrip(').,;\'"') if m else None

def desc_careers_link(raw):
    for m in CAREERS_LINK_RE.finditer(unescape(raw or "")):
        if not SOCIAL_RE.search(m.group(1)):
            return m.group(1)
    return None

def desc_company_url(raw):
    m = re.search(r'URL:\s*(?:<[^>]+>\s*)*(https?://[^\s<>"\']+)', unescape(raw or ""))
    if m and not SOCIAL_RE.search(m.group(1)):
        return m.group(1).rstrip("/.,")
    return None

def board_url(kind, slug):
    return {"ashby": f"https://jobs.ashbyhq.com/{slug}",
            "greenhouse": f"https://job-boards.greenhouse.io/{slug}",
            "lever": f"https://jobs.lever.co/{slug}",
            "recruitee": f"https://{slug}.recruitee.com"}[kind]

def slug_candidates(company):
    low = (company or "").lower()
    stripped = re.sub(r"\b(inc|ltd|gmbh|bv|llc|co|corp|labs|hq|io|app)\b\.?", "", low)
    cands = [re.sub(r"[^a-z0-9]+", "", low),
             re.sub(r"[^a-z0-9]+", "-", low).strip("-"),
             re.sub(r"[^a-z0-9]+", "", stripped)]
    return [c for c in dict.fromkeys(cands) if len(c) >= 3]

def src_recruitee(slug):
    d = fetch_json(f"https://{slug}.recruitee.com/api/offers/")
    out = []
    for o in d.get("offers", []):
        out.append({
            "source": f"recruitee/{slug}", "company": slug.title(),
            "title": o.get("title"), "url": o.get("careers_url") or o.get("url"),
            "location": o.get("location") or "", "remote": bool(o.get("remote")),
            "salary": None, "posted": o.get("published_at"),
            "updated": o.get("updated_at"),
            "desc": strip_html(o.get("description")), "raw": o.get("description") or "",
        })
    return out

def board_jobs(kind, slug):
    memo = _board_jobs_memo.get((kind, slug))
    if memo is not None:
        return memo
    fn = {"greenhouse": src_greenhouse, "lever": src_lever, "ashby": src_ashby,
          "recruitee": src_recruitee}[kind]
    try:
        jobs = fn(slug)
    except Exception:
        jobs = []
    _board_jobs_memo[(kind, slug)] = jobs
    return jobs

def probe_company(company):
    comp = norm(company)
    for kind in ("greenhouse", "ashby", "lever", "recruitee"):
        for slug in slug_candidates(company):
            try:
                jobs = board_jobs(kind, slug)
            except Exception:
                continue
            if not jobs:
                continue
            if kind == "greenhouse":
                # greenhouse tells us whose board this is - verify it
                bc = norm(jobs[0].get("company") or "")
                if bc and comp not in bc and bc not in comp:
                    continue
            return {"kind": kind, "slug": slug, "checked": RUN_ID}
    return {"kind": None, "checked": RUN_ID}

def find_direct(company, title):
    """Hunt the company's own ATS board for this job. Cached in data/ats_cache.json."""
    comp = norm(company)
    if not comp:
        return None
    ent = ATS_CACHE.get(comp)
    stale = (NOW - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if ent is None or (not ent.get("kind") and ent.get("checked", "") < stale):
        ent = probe_company(company)
        ATS_CACHE[comp] = ent
    if not ent.get("kind"):
        return None
    t = norm(title)
    for bj in board_jobs(ent["kind"], ent["slug"]):
        bt = norm(bj.get("title"))
        if bt and (bt == t or t in bt or bt in t):
            return bj.get("url")
    return None

# --- web hunt: find the posting at its source via DuckDuckGo ------------

AGG_HOST_RE = re.compile(
    r"weworkremotely|remotive|jobicy|himalayas|remoteok|workingnomads|arbeitnow|"
    r"builtin\.com|wellfound|glassdoor|indeed|linkedin|ziprecruiter|simplyhired|"
    r"jobera|jobshives|startup\.jobs|jogglejobs|workbenchdata|ixdf\.org|jobs-radar|"
    r"adzuna|talent\.com|jooble|whatjobs|remote\.co|dailyremote|nodesk|remotees|"
    r"jobgether|landing\.jobs|google\.com|bing\.com|reddit\.com|youtube", re.I)

class SearchThrottled(Exception):
    pass

BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"

def ddg_links(q):
    url = "https://lite.duckduckgo.com/lite/?q=" + urllib.parse.quote(q)
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA,
                                               "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=20) as r:
        html = r.read().decode("utf-8", "replace")
    if re.search(r"made by a human|challenge|anomaly", html, re.I):
        raise SearchThrottled("duckduckgo served a bot challenge")
    out = []
    for m in re.finditer(r'href="([^"]+)"', html):
        u = m.group(1)
        if "uddg=" in u:
            mm = re.search(r"uddg=([^&]+)", u)
            u = urllib.parse.unquote(mm.group(1)) if mm else u
        if u.startswith("http") and "duckduckgo" not in u:
            out.append(u)
    return out

def posting_alive(url, title):
    """True if the posting still exists and carries this title."""
    t = norm(title)
    # greenhouse job pages are client-rendered - ask the API instead
    m = re.search(r"greenhouse\.io/([^/?#]+)/jobs/(\d+)", url)
    if m:
        try:
            j = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{m.group(1)}/jobs/{m.group(2)}")
            bt = norm(j.get("title"))
            return bool(bt) and (bt == t or t in bt or bt in t)
        except urllib.error.HTTPError:
            return False
        except Exception:
            pass  # fall through to the page check
    try:
        html = fetch(url, timeout=20)
    except Exception:
        return False
    body = norm(strip_html(unescape(html), 40000))
    return t in body

def greenhouse_stale(url, title):
    """A dead greenhouse posting whose board is alive without the title = closed."""
    m = re.search(r"greenhouse\.io/([^/?#]+)", url)
    if not m:
        return False
    try:
        jobs = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{m.group(1)}/jobs")
    except Exception:
        return False
    t = norm(title)
    return not any(t == norm(j.get("title")) or t in norm(j.get("title"))
                   for j in jobs.get("jobs", []))

def crawl_for_posting(url, title, depth=1, budget=None, company_key=None):
    """Do what a human does on a company site: find careers, find the role.
    Returns the specific posting URL or None. At most one hop deep."""
    if budget is None:
        budget = [4]  # posting_alive checks allowed
    try:
        html = fetch(url, timeout=20)
    except Exception:
        return None
    t = norm(title)
    # greenhouse embed on the careers page -> board api -> frameable job url
    m = re.search(r"greenhouse\.io/embed/job_board(?:/js)?\?for=([a-z0-9_-]+)", html, re.I)
    if m:
        for bj in board_jobs("greenhouse", m.group(1)):
            bt = norm(bj.get("title"))
            if bt and (bt == t or t in bt or bt in t):
                return bj.get("url")
    anchors = [(urllib.parse.urljoin(url, unescape(am.group(1))),
                norm(strip_html(am.group(2), 200)))
               for am in re.finditer(r'<a\b[^>]*href="([^"#]+)"[^>]*>(.*?)</a>', html, re.S | re.I)]
    # a link to the company's ATS *board* resolves through the board API -
    # and teaches the slug cache for every future role at this company
    BOARD_RES = [(r"jobs\.ashbyhq\.com/([A-Za-z0-9_-]+)/?$", "ashby"),
                 (r"(?:job-boards|boards)\.greenhouse\.io/([A-Za-z0-9_-]+)/?$", "greenhouse"),
                 (r"jobs(?:\.eu)?\.lever\.co/([A-Za-z0-9_-]+)/?$", "lever"),
                 (r"https?://([A-Za-z0-9_-]+)\.recruitee\.com", "recruitee")]
    for u, _ in anchors:
        for pat, kind in BOARD_RES:
            bm = re.search(pat, u)
            if not bm:
                continue
            slug = bm.group(1)
            jobs = board_jobs(kind, slug)
            if jobs and company_key and not (ATS_CACHE.get(company_key) or {}).get("kind"):
                ATS_CACHE[company_key] = {"kind": kind, "slug": slug,
                                          "checked": RUN_ID}
            for bj in jobs:
                bt = norm(bj.get("title"))
                if bt and (bt == t or t in bt or bt in t):
                    return bj.get("url")
    def try_url(u):
        if budget[0] <= 0:
            return False
        budget[0] -= 1
        return posting_alive(u, title)
    usable = [(u, text) for u, text in anchors
              if u.startswith("http") and not AGG_HOST_RE.search(u)]
    # an anchor on the company's own page naming the exact role is the
    # company vouching for the link - take it as is
    for u, text in usable:
        if t and t in text:
            return u
    # anonymous ATS links still need proof; never spend budget on links
    # labelled with a different role
    for u, text in usable:
        if ATS_LINK_RE.search(u) and not text and try_url(u):
            return u
    if depth > 0:
        for u, text in anchors:
            if not u.startswith("http") or AGG_HOST_RE.search(u):
                continue
            if (re.search(r"careers|jobs|join us|open roles|positions|vacancies|hiring", text)
                    or re.search(r"/(careers|jobs|join)(/|$)", u, re.I)):
                if u.rstrip("/") != url.rstrip("/"):
                    return crawl_for_posting(u, title, depth - 1, budget, company_key)
    return None

def hunt_web(company, title):
    """Search for the posting at its source. Returns (url, kind, stale).

    Raises SearchThrottled when the engine bot-walls us - the caller must
    NOT cache that as a real no-result.
    """
    stale_hit = False
    results = ddg_links(f'"{company}" "{title}"')
    time.sleep(10)
    for u in results[:8]:
        if AGG_HOST_RE.search(u):
            continue
        if ATS_LINK_RE.search(u):
            if posting_alive(u, title):
                return u.split("?")[0], "direct", False
            if greenhouse_stale(u, title):
                stale_hit = True
            continue
    comp_token = re.sub(r"[^a-z0-9]", "", (company or "").lower())
    results = ddg_links(f"{company} careers")
    time.sleep(10)
    for u in results[:5]:
        host = re.sub(r"[^a-z0-9]", "", (urllib.parse.urlsplit(u).netloc or "").lower())
        if AGG_HOST_RE.search(u) or comp_token not in host:
            continue
        if re.search(r"careers|jobs|join", u, re.I) or ATS_LINK_RE.search(u):
            hit = crawl_for_posting(u, title, company_key=norm(company))
            if hit:
                return hit, "direct", stale_hit
            return u, "careers", stale_hit
    return None, None, stale_hit

# --- original-posting reader: the source page is the authority ----------

def _ldjson_jobposting(html):
    for m in re.finditer(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            d = json.loads(m.group(1).strip())
        except Exception:
            continue
        for it in (d if isinstance(d, list) else [d]):
            if isinstance(it, dict) and it.get("@type") == "JobPosting":
                return it
    return None

def fetch_original(url, company, title):
    """Read the original posting. Structured ATS APIs first, page scrape last.
    Returns {"loc","salary","xp","text","dead"}; any field may be None."""
    out = {"loc": None, "salary": None, "xp": None, "text": None, "dead": False,
           "posted": None}
    t = norm(title)
    ok_title = lambda bt: not bt or not t or t in bt or bt in t

    def learn_board(kind, slug):
        ck = norm(company)
        if ck and not (ATS_CACHE.get(ck) or {}).get("kind"):
            ATS_CACHE[ck] = {"kind": kind, "slug": slug, "checked": RUN_ID}

    m = re.search(r"jobs\.ashbyhq\.com/([^/]+)/([0-9a-f-]{36})", url)
    if m:
        jobs = board_jobs("ashby", m.group(1))
        for bj in jobs:
            if m.group(2) in (bj.get("url") or ""):
                if not ok_title(norm(bj.get("title"))):
                    return out
                learn_board("ashby", m.group(1))
                out.update(loc=bj.get("location"), salary=bj.get("salary"),
                           text=bj.get("desc") or None)
                return out
        out["dead"] = bool(jobs)  # board alive, posting gone
        return out

    m = re.search(r"jobs(?:\.eu)?\.lever\.co/([^/]+)/([0-9a-f-]{36})", url)
    if m:
        try:
            j = fetch_json(f"https://api.lever.co/v0/postings/{m.group(1)}/{m.group(2)}")
            if not ok_title(norm(j.get("text"))):
                return out
            cats = j.get("categories") or {}
            learn_board("lever", m.group(1))
            out.update(loc=cats.get("location"),
                       text=strip_html(j.get("description"), 20000) or None)
            return out
        except urllib.error.HTTPError as e:
            out["dead"] = e.code in (404, 410)
            return out
        except Exception:
            return out

    m = re.search(r"https?://([A-Za-z0-9_-]+)\.recruitee\.com/o/([^/?#]+)", url)
    if m:
        jobs = board_jobs("recruitee", m.group(1))
        for bj in jobs:
            if m.group(2) in (bj.get("url") or ""):
                if not ok_title(norm(bj.get("title"))):
                    return out
                learn_board("recruitee", m.group(1))
                out.update(loc=bj.get("location"), text=bj.get("desc") or None)
                return out
        out["dead"] = bool(jobs)
        return out

    gid = re.search(r"greenhouse\.io/[^/]+/jobs/(\d+)", url) or re.search(r"gh_jid=(\d+)", url)
    if gid:
        slugs = []
        m = re.search(r"(?:job-boards|boards)\.greenhouse\.io/([^/?#]+)", url)
        if m:
            slugs.append(m.group(1))
        ent = ATS_CACHE.get(norm(company)) or {}
        if ent.get("kind") == "greenhouse":
            slugs.append(ent["slug"])
        slugs += slug_candidates(company)
        for slug in dict.fromkeys(slugs):
            try:
                j = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{gid.group(1)}")
            except Exception:
                continue
            if not ok_title(norm(j.get("title"))):
                return out
            learn_board("greenhouse", slug)
            out.update(loc=(j.get("location") or {}).get("name"),
                       text=strip_html(unescape(j.get("content") or ""), 20000) or None)
            return out

    # Generic page read.
    try:
        html = fetch(url, timeout=25)
    except urllib.error.HTTPError as e:
        out["dead"] = e.code in (404, 410)
        return out
    except Exception:
        return out
    ld = _ldjson_jobposting(html)
    if ld:
        if not ok_title(norm(ld.get("title"))):
            return out
        locs = ld.get("applicantLocationRequirements") or []
        locs = locs if isinstance(locs, list) else [locs]
        names = [l.get("name", "") for l in locs if isinstance(l, dict)]
        jl = ld.get("jobLocation") or {}
        jl = jl[0] if isinstance(jl, list) and jl else jl
        if isinstance(jl, dict):
            addr = jl.get("address") or {}
            names += [addr.get("addressCountry", ""), addr.get("addressLocality", "")]
        if ld.get("jobLocationType") == "TELECOMMUTE":
            names.append("Remote")
        out["loc"] = ", ".join(filter(None, names)) or None
        sal = (ld.get("baseSalary") or {}).get("value") or {}
        if isinstance(sal, dict) and (sal.get("minValue") or sal.get("maxValue")):
            out["salary"] = fmt_range(sal.get("minValue"), sal.get("maxValue"),
                                      (ld.get("baseSalary") or {}).get("currency", ""))
        out["text"] = strip_html(unescape(ld.get("description") or ""), 20000) or None
        out["posted"] = ld.get("datePosted") or None
        return out
    m = re.search(r'"location":\s*\{\s*"name":\s*"([^"]+)"', html)
    if m:
        out["loc"] = m.group(1)
    pm = re.search(r'"datePosted"\s*:\s*"([^"]+)"', html)
    if pm:
        out["posted"] = pm.group(1)
    text = strip_html(unescape(html), 30000)
    out["text"] = text if norm(title) in norm(text) else None
    return out

def frameable(url):
    """Can the browser embed this page in our iframe? Header check."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
        r = urllib.request.urlopen(req, timeout=12)
        heads = {k.lower(): v for k, v in r.headers.items()}
        r.close()
    except Exception:
        return False
    if re.search(r"deny|sameorigin", heads.get("x-frame-options", ""), re.I):
        return False
    m = re.search(r"frame-ancestors([^;]*)", heads.get("content-security-policy", ""))
    if m and "*" not in m.group(1):
        return False
    return True

# ---------------------------------------------------------------- pipeline

SOURCE_PRIORITY = ["greenhouse", "lever", "ashby", "recruitee", "workday",
                   "smartrecruiters", "workable", "teamtailor", "personio",
                   "bamboohr", "breezy", "pinpoint", "join", "manatal",
                   "rippling", "jazzhr", "comeet",
                   "landingjobs", "remotive", "himalayas", "jobicy",
                   "weworkremotely", "workingnomads", "arbeitnow", "remoteok"]

def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

def job_key(company, title):
    return hashlib.sha1(f"{norm(company)}|{norm(title)}".encode()).hexdigest()[:16]

# A posting's stable identity is the id its ATS mints, not its company name or
# title - both of which the employer edits freely (a rename or a title tweak
# must never orphan triage). We read that id out of the apply URL. Platform-
# tagged so ids never collide across boards; the same gh_jid appearing on a
# direct board and inside an aggregator link resolves to one fingerprint.
_FP_PATTERNS = [
    ("gh",    r"[?&]gh_jid=(\d+)"),
    ("gh",    r"greenhouse\.io/[^/]+/jobs/(\d+)"),
    ("ashby", r"ashbyhq\.com/[^/]+/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"),
    ("lever", r"lever\.co/[^/]+/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"),
    ("sr",    r"smartrecruiters\.com/[^/]+/(\d{8,})"),
    ("wk",    r"workable\.com/j/([A-Za-z0-9]+)"),
    ("ph",    r"personio\.(?:com|de)/job/(\d+)"),
    ("zoho",  r"zohorecruit\.[^/]+/jobs/[^/]+/(\d{10,})"),
    ("rip",   r"rippling\.com/[^/]+/jobs/([0-9a-f-]{20,})"),
    ("fresh", r"freshteam\.com/jobs/([A-Za-z0-9]+)"),
    ("jazz",  r"applytojob\.com/apply/([A-Za-z0-9]+)"),
    ("wd",    r"myworkdayjobs\.com/.*?_([A-Za-z]{0,3}\d{3,})"),
    ("tt",    r"teamtailor\.com/jobs/(\d+)"),
    ("cp",    r"careers-page\.com/[^/]+/job/([A-Za-z0-9]+)"),
    ("pin",   r"pinpointhq\.com/(?:en/)?postings/([0-9a-f-]{36})"),
    ("join",  r"join\.com/companies/[^/]+/(\d{5,})"),
]
_FP_PATTERNS = [(tag, re.compile(pat, re.I)) for tag, pat in _FP_PATTERNS]
# generic hosted board (custom domain fronting greenhouse/recruitee etc.):
# /jobs/<digits>. Tagged by host root so two boards' "8285191" stay distinct.
_FP_GENERIC = re.compile(r"/jobs/(\d{5,})\b", re.I)

def posting_fp(url):
    """Stable, rename-proof fingerprint for a posting, or None when the URL
    carries no ATS id (a bare careers page, an unresolved aggregator link)."""
    u = url or ""
    for tag, rx in _FP_PATTERNS:
        m = rx.search(u)
        if m:
            return f"{tag}:{m.group(1).lower()}"
    m = _FP_GENERIC.search(u)
    if m:
        host = re.match(r"https?://([^/]+)", u)
        root = ""
        if host:
            parts = host.group(1).lower().replace("www.", "").split(".")
            root = parts[-2] if len(parts) >= 2 else parts[0]
        return f"{root}:{m.group(1)}"
    return None

def priority(source):
    base = source.split("/")[0]
    return SOURCE_PRIORITY.index(base) if base in SOURCE_PRIORITY else 99

VERDICTS_FILE = ROOT / "data" / "verdicts.json"
try:
    VERDICTS = json.loads(VERDICTS_FILE.read_text()).get("jobs", {})
except Exception:
    VERDICTS = {}

def learned(job_id, company, location):
    """Y/N verdicts from the dashboard. Exact job first, then company+location
    consensus, then company-wide consensus. Returns 'y', 'n', or None."""
    if job_id in VERDICTS:
        return VERDICTS[job_id].get("v")
    comp, loc = norm(company), norm(location)
    for scope in ("both", "company"):
        vs = {e.get("v") for e in VERDICTS.values()
              if e.get("company") == comp
              and (scope == "company" or e.get("location") == loc)}
        if len(vs) == 1:
            return vs.pop()
    return None

def run():
    report = {}
    raw = []
    aggregators = [
        ("remotive", src_remotive), ("himalayas", src_himalayas),
        ("jobicy", src_jobicy), ("workingnomads", src_workingnomads),
        ("remoteok", src_remoteok), ("arbeitnow", src_arbeitnow),
        ("weworkremotely", src_wwr), ("landingjobs", src_landingjobs),
        ("designjobsworld", src_designjobsworld),
    ]
    wl = CONFIG.get("watchlist", {})
    tasks = list(aggregators)
    tasks += [(f"greenhouse/{s}", lambda s=s: src_greenhouse(s)) for s in wl.get("greenhouse", [])]
    tasks += [(f"lever/{s}", lambda s=s: src_lever(s)) for s in wl.get("lever", [])]
    tasks += [(f"ashby/{s}", lambda s=s: src_ashby(s)) for s in wl.get("ashby", [])]
    names = {n for n, _ in tasks}
    for n, fn in registry_tasks():
        if n not in names:
            tasks.append((n, fn))
            names.add(n)

    def run_tasks(task_list):
        with ThreadPoolExecutor(max_workers=10) as ex:
            futs = {ex.submit(fn): n for n, fn in task_list}
            for fut in as_completed(futs):
                n = futs[fut]
                try:
                    jobs = fut.result()
                    raw.extend(jobs)
                    report[n] = {"ok": True, "fetched": len(jobs)}
                except urllib.error.HTTPError as e:
                    report[n] = {"ok": False, "error": f"HTTP {e.code}"}
                except Exception as e:
                    report[n] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    run_tasks(tasks)

    # Boards discovered while hunting originals join the pool permanently -
    # where there's one design job, there are often more. Agency boards that
    # post other companies' roles under their own name stay out.
    AGENCY_BOARDS = {"jobgether", "lemonio", "jobgether-1"}
    configured = {(k, s) for k, slugs in wl.items() for s in slugs}
    learned_tasks = []
    for comp, ent in list(ATS_CACHE.items()):
        if comp.startswith("hunt:") or not isinstance(ent, dict) or not ent.get("kind"):
            continue
        kind, slug = ent["kind"], ent["slug"]
        name = f"{kind}/{slug}"
        if ((kind, slug) in configured or name in report or name in names
                or slug in AGENCY_BOARDS):
            continue
        names.add(name)
        def fetch_learned(kind=kind, slug=slug, via=ent.get("via")):
            jobs = board_jobs(kind, slug)
            for bj in jobs:
                bj["derived_from"] = via
            return jobs
        learned_tasks.append((name, fetch_learned))
    run_tasks(learned_tasks)

    # Filter + classify + dedupe.
    max_age = CONFIG.get("max_age_days")
    posted_cutoff = NOW - timedelta(days=max_age) if max_age else None
    kept = {}
    for j in raw:
        if not title_ok(j.get("title")):
            continue
        if not jd_language_ok(j.get("desc")):
            continue  # JD in another language - they want that language
        if posted_cutoff:
            pdate = parse_posted(j.get("posted"))
            if pdate and pdate < posted_cutoff:
                continue  # stale listing an aggregator never cleaned up
        bucket = classify(j.get("location"), j.get("remote"),
                          j.get("restrictions"), j.get("timezones"))
        if bucket is None:
            continue
        if bucket == "tiebreak":
            v = learned(job_key(j.get("company"), j.get("title")),
                        j.get("company"), j.get("location"))
            if v == "n":
                continue
            if v == "y":
                bucket = "ok"
        salary = j.get("salary") or find_salary(j.get("desc"))
        if EXC_JUNIOR.search(j.get("title") or ""):
            # junior/mid titles only survive when the money says otherwise
            if salary_top(salary) < 100_000:
                continue
        key = job_key(j.get("company"), j.get("title"))
        entry = {
            "id": key, "title": (j.get("title") or "").strip(),
            "company": (j.get("company") or "").strip() or "?",
            "url": j.get("url"), "source": j["source"],
            "location": (j.get("location") or "").strip(),
            "bucket": bucket, "salary": salary,
            "posted": j.get("posted"),
            "updated": j.get("updated"),
            "xp": find_xp(j.get("desc")),
            "derived_from": j.get("derived_from"),
            "market": market_label(bucket, j.get("location")),
            "_raw": j.get("raw") or "",
            "_jd": j.get("desc") or "",
        }
        if key not in kept or priority(j["source"]) < priority(kept[key]["source"]):
            prev = kept.get(key)
            if prev:
                entry["salary"] = entry["salary"] or prev["salary"]
                if prev["bucket"] == "pt" or entry["bucket"] == "tiebreak" and prev["bucket"] != "tiebreak":
                    entry["bucket"] = prev["bucket"]
            kept[key] = entry
        else:
            cur = kept[key]
            cur["salary"] = cur["salary"] or salary
            if cur["bucket"] == "tiebreak" and bucket != "tiebreak":
                cur["bucket"] = bucket

    # Resolve direct apply links for aggregator finds.
    prev_seen = {}
    prev_frame = {}
    if DATA_FILE.exists():
        try:
            for pj in json.loads(DATA_FILE.read_text()).get("jobs", []):
                prev_seen[pj["id"]] = pj.get("first_seen", "")
                prev_frame[pj["id"]] = (pj.get("apply_url") or pj.get("url"),
                                        pj.get("frameable"))
        except Exception:
            pass
    resolved = hunted = 0
    upgrades = 0
    stale_ids = []
    throttled = False
    week_ago = (NOW - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for j in list(kept.values()):
        rawdesc = j.pop("_raw", "")
        j["market"] = j.get("market") or market_label(j["bucket"], j.get("location"))
        if j["source"].split("/")[0] not in AGGREGATORS:
            j["direct"] = True   # straight off the company's own board
            if not j.get("posted") and j.get("url"):
                # parse-time date fetch can fail transiently - one retry
                orig = fetch_original(j["url"], j["company"], j["title"])
                if orig.get("posted"):
                    j["posted"] = orig["posted"]
            continue
        link = desc_ats_link(rawdesc) or find_direct(j["company"], j["title"])
        company_url = desc_company_url(rawdesc)
        if not link and company_url:
            link = crawl_for_posting(company_url, j["title"],
                                     company_key=norm(j["company"]))
        kind = "direct" if link else None
        if not link:
            hk = "hunt:" + j["id"]
            ent = ATS_CACHE.get(hk)
            if ent and ent.get("kind") == "careers" and upgrades < 8:
                upgrades += 1
                hit = crawl_for_posting(ent["url"], j["title"],
                                        company_key=norm(j["company"]))
                if hit:
                    ent = {"url": hit, "kind": "direct", "stale": False,
                           "checked": RUN_ID}
                    ATS_CACHE[hk] = ent
                link, kind = ent.get("url"), ent.get("kind")
            elif ent and (ent.get("url") or ent.get("checked", "") >= week_ago):
                link, kind = ent.get("url"), ent.get("kind")
                if ent.get("stale"):
                    stale_ids.append(j["id"])
            elif (hunted < 15 and not throttled
                  and prev_seen.get(j["id"], RUN_ID) >= week_ago):
                hunted += 1
                try:
                    link, kind, stale = hunt_web(j["company"], j["title"])
                    ATS_CACHE[hk] = {"url": link, "kind": kind, "stale": stale,
                                     "checked": RUN_ID}
                    if stale and not link:
                        stale_ids.append(j["id"])
                except SearchThrottled:
                    throttled = True
                except Exception:
                    pass
        if not link:
            ent2 = ATS_CACHE.get(norm(j["company"])) or {}
            link = (desc_careers_link(rawdesc)
                    or (board_url(ent2["kind"], ent2["slug"]) if ent2.get("kind") else None)
                    or company_url)
            kind = "careers"
        ent_v = ATS_CACHE.get(norm(j["company"]))
        if isinstance(ent_v, dict) and ent_v.get("kind") and not ent_v.get("via"):
            ent_v["via"] = j["id"]
        if kind == "careers":
            # bring the board's live design roles into the brief pane -
            # the honest answer when the exact posting is gone or hidden
            ent2 = ATS_CACHE.get(norm(j["company"])) or {}
            if ent2.get("kind"):
                roles = [{"title": bj["title"], "url": bj.get("url"),
                          "location": bj.get("location") or ""}
                         for bj in board_jobs(ent2["kind"], ent2["slug"])
                         if title_ok(bj.get("title"))][:8]
                if roles:
                    j["board_roles"] = roles
        if link:
            j["apply_url"] = link
            j["apply_kind"] = kind or "direct"
            resolved += 1
        if j.get("apply_kind") == "direct":
            orig = fetch_original(j["apply_url"], j["company"], j["title"])
            if orig["dead"]:
                stale_ids.append(j["id"])
                continue
            if orig.get("posted") and not j.get("posted"):
                j["posted"] = orig["posted"]
            if orig["text"] and len(orig["text"]) >= 200:
                j["_jd"] = orig["text"]
                j["xp"] = find_xp(orig["text"]) or j.get("xp")
                new_sal = orig["salary"] or find_salary(orig["text"])
                ranged = lambda s: bool(s and re.search(r"[–—-]|\bto\b", s))
                if new_sal and (not j["salary"] or ranged(new_sal) or not ranged(j["salary"])):
                    j["salary"] = new_sal
            if orig["loc"] and j["bucket"] != "ok":
                cls = classify(orig["loc"])
                if cls is None:
                    print(f"dropped (original says '{orig['loc']}'): "
                          f"{j['company']} - {j['title']}")
                    stale_ids.append(j["id"])
                    continue
                # a bare "Remote" upstream is weaker, not contradicting - keep
                # the aggregator's signal in that case
                if cls != "tiebreak" or j["bucket"] == "tiebreak":
                    j["bucket"] = cls
                    j["location"] = orig["loc"]
                    j["market"] = market_label(cls, orig["loc"])
    for j in kept.values():
        if "direct" not in j:
            j["direct"] = j.get("apply_kind") == "direct"
    def _frame(j):
        target = j.get("apply_url") or j["url"]
        pu, pf = prev_frame.get(j["id"], (None, None))
        j["frameable"] = pf if (pu == target and pf is not None) else frameable(target)
    with ThreadPoolExecutor(max_workers=10) as ex:
        list(ex.map(_frame, kept.values()))
    for sid in stale_ids:
        kept.pop(sid, None)
    if stale_ids:
        print(f"dropped {len(stale_ids)} listing(s) closed at the source")
    ATS_CACHE_FILE.write_text(json.dumps(ATS_CACHE, indent=1, sort_keys=True))
    report["websearch"] = ({"ok": False, "error": "bot challenge - hunts paused"}
                           if throttled else {"ok": True, "fetched": hunted})

    # Grow the registry: two site: searches a night, rotating platforms.
    # Self-gates like the hunts - datacenter IPs get bot-walled and skip.
    discovered = 0
    if not throttled:
        try:
            discovered = discover(REGISTRY)
        except SearchThrottled:
            throttled = True
        except Exception:
            pass
    REGISTRY_FILE.write_text(json.dumps(REGISTRY, indent=1, sort_keys=True,
                                        ensure_ascii=False))
    report["discovery"] = ({"ok": False, "error": "search throttled"} if throttled
                           else {"ok": True, "fetched": discovered})

    # Merge with existing DB.
    old = {}
    old_by_fp = {}
    if DATA_FILE.exists():
        for j in json.loads(DATA_FILE.read_text()).get("jobs", []):
            old[j["id"]] = j
            fp = posting_fp(j.get("apply_url") or j.get("url"))
            if fp:
                old_by_fp.setdefault(fp, j["id"])
    failed_prefixes = tuple(n for n, r in report.items() if not r["ok"])
    jobs_out = []
    adopted = set()
    for key, j in kept.items():
        prev = old.pop(key, None)
        if prev is None:
            # Company or title changed since last run: the company+title id is
            # new, but the ATS fingerprint pins it to the same posting. Adopt
            # the old id so triage, judge verdicts and first_seen stay bound.
            fp = posting_fp(j.get("apply_url") or j.get("url"))
            pid = old_by_fp.get(fp) if fp else None
            if pid and pid not in adopted and pid in old:
                prev = old.pop(pid)
                if prev.get("source", "").split("/")[0] == j["source"].split("/")[0]:
                    adopted.add(pid)
                    j["id"] = pid
                else:
                    old[pid] = prev  # different board, not the same posting
                    prev = None
        if prev and not j.get("apply_url") and prev.get("apply_url"):
            j["apply_url"] = prev["apply_url"]
            j["apply_kind"] = prev.get("apply_kind", "direct")
        if prev and not j.get("xp") and prev.get("xp"):
            j["xp"] = prev["xp"]
        if prev and not j.get("posted") and prev.get("posted"):
            j["posted"] = prev["posted"]
        if prev and not j.get("updated") and prev.get("updated"):
            j["updated"] = prev["updated"]
        j["first_seen"] = prev["first_seen"] if prev else RUN_ID
        j["first_run"] = prev.get("first_run", prev["first_seen"]) if prev else RUN_ID
        j["last_seen"] = RUN_ID
        j["active"] = True
        jobs_out.append(j)
    cutoff = (NOW - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for key, j in old.items():
        src = j.get("source", "")
        if any(src.startswith(p) for p in failed_prefixes):
            j["active"] = True  # source down, don't declare its jobs dead
        else:
            j["active"] = False
        if j["last_seen"] >= cutoff:
            jobs_out.append(j)

    # Judge verdicts (the JD-reading pass) are authoritative once given:
    # cut roles go dark for good, judged buckets override classify()'s guess.
    # Unjudged actives carry judged:false until the judge pass reads them.
    judged = {}
    jfile = ROOT / "data" / "judged.json"
    if jfile.exists():
        judged = json.loads(jfile.read_text()).get("jobs", {})
    for j in jobs_out:
        v = judged.get(j["id"])
        if not v:
            j["judged"] = False
            continue
        j["judged"] = True
        if v["bucket"] == "cut":
            j["active"] = False
        else:
            j["bucket"] = v["bucket"]
            j["market"] = market_label(v["bucket"], j.get("location"))
            if v.get("why"):
                j["why"] = v["why"]

    # JDs for the overnight runner: every active role (facts parsing), the
    # fresh non-tiebreak ones flagged for CV generation.
    day_ago = (NOW - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    jds = {}
    for j in jobs_out:
        jd = j.pop("_jd", "")
        if j["active"] and jd:
            # content signature the dashboard watches: a real JD edit flips it,
            # so a role discarded on an old description can resurface for a
            # second look. Normalized so page volatility alone never trips it.
            sig = re.sub(r"[^a-z0-9]+", " ", jd.lower()).strip()
            j["jdhash"] = hashlib.sha1(sig.encode()).hexdigest()[:12]
        if j["active"] and len(jd) >= 200:
            jds[j["id"]] = {
                "title": j["title"], "company": j["company"],
                "url": j.get("apply_url") or j["url"],
                "location": j["location"], "salary": j["salary"],
                "market": j["market"], "xp": j.get("xp"),
                "bucket": j["bucket"], "first_seen": j["first_seen"],
                "fresh": j.get("first_seen", "") >= day_ago,
                "jd": jd[:20000],
            }
    (ROOT / "data" / "jds.json").write_text(
        json.dumps({"generated_at": RUN_ID, "jobs": jds}, indent=1, ensure_ascii=False))

    jobs_out.sort(key=lambda j: (j.get("first_seen") or ""), reverse=True)
    out = {
        "generated_at": RUN_ID,
        "run_id": RUN_ID,
        "counts": {
            "active": sum(1 for j in jobs_out if j["active"]),
            "new": sum(1 for j in jobs_out if j.get("first_run") == RUN_ID),
        },
        "sources": report,
        "jobs": jobs_out,
    }
    DATA_FILE.parent.mkdir(exist_ok=True)
    DATA_FILE.write_text(json.dumps(out, indent=1, ensure_ascii=False))
    # Console summary.
    ok = sum(1 for r in report.values() if r["ok"])
    print(f"sources: {ok}/{len(report)} ok | raw {len(raw)} | kept {len(kept)} | "
          f"new {out['counts']['new']} | active {out['counts']['active']} | "
          f"direct links {resolved} | jds {len(jds)}")
    for n, r in sorted(report.items()):
        if not r["ok"]:
            print(f"  FAIL {n}: {r['error']}")
    buckets = {}
    for j in jobs_out:
        if j["active"]:
            buckets[j["bucket"]] = buckets.get(j["bucket"], 0) + 1
    print("buckets:", buckets)

if __name__ == "__main__":
    run()
