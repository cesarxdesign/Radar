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
from datetime import datetime, timezone, timedelta
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text())
DATA_FILE = ROOT / "data" / "jobs.json"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) JobRadar/1.0 (personal job tracker)"
NOW = datetime.now(timezone.utc)
RUN_ID = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")

INC = re.compile(CONFIG["title_include"], re.I)
EXC_JUNIOR = re.compile(CONFIG["title_exclude_junior"], re.I)
EXC_FIELD = re.compile(CONFIG["title_exclude_field"], re.I)

# ---------------------------------------------------------------- fetch

def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")

def fetch_json(url, timeout=30):
    return json.loads(fetch(url, timeout))

# ---------------------------------------------------------------- filters

def title_ok(title):
    t = title or ""
    return bool(INC.search(t)) and not EXC_JUNIOR.search(t) and not EXC_FIELD.search(t)

PT_RE = re.compile(r"\b(portugal|lisbon|lisboa|porto)\b", re.I)
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

def classify(location_text, remote=None, restrictions=None, timezones=None):
    """Bucket a job by Portugal hireability.

    Returns one of: 'pt', 'eu', 'ww', 'tiebreak', or None (drop).
    Negative evidence only ever comes from location/restriction fields,
    never from job descriptions.
    """
    loc = (location_text or "").strip()

    # Structured country restrictions (Himalayas) are authoritative.
    if restrictions:
        joined = ", ".join(restrictions)
        if PT_RE.search(joined):
            return "pt"
        if EU_RE.search(joined):
            return "eu"
        return None  # explicit country list without Portugal
    if PT_RE.search(loc):
        return "pt"
    if EU_RE.search(loc):
        return "eu"
    if WW_RE.search(loc):
        return "ww"
    # Timezone restrictions: Portugal is UTC+0 (+1 in summer).
    if timezones:
        return "ww" if (0 in timezones or 1 in timezones) else None
    if loc:
        if ELSEWHERE_RE.search(loc):
            return None  # somewhere specific, not Portugal
        if REMOTE_RE.search(loc) or re.search(r"\bhybrid\b", loc, re.I):
            return "tiebreak"  # remote/hybrid with no scope stated
        if remote:
            return "tiebreak"  # remote but location unreadable -> surface it
        return None  # an on-site office we can't recognize is not Portugal
    # No location info at all.
    if remote is False:
        return None
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
        if not (INC.search(j["position"]) or re.search(r"\b(design|ux|ui)\b", tags, re.I)):
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
            "desc": strip_html(j.get("role_description")),
        })
    return out

# --- ATS watchlist -------------------------------------------------

def src_greenhouse(slug):
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
            "desc": strip_html(raw), "raw": raw,
        })
    return out

def src_lever(slug):
    d = fetch_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
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
    out = {"loc": None, "salary": None, "xp": None, "text": None, "dead": False}
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
        return out
    m = re.search(r'"location":\s*\{\s*"name":\s*"([^"]+)"', html)
    if m:
        out["loc"] = m.group(1)
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

SOURCE_PRIORITY = ["greenhouse", "lever", "ashby", "recruitee", "landingjobs", "remotive",
                   "himalayas", "jobicy", "weworkremotely", "workingnomads",
                   "arbeitnow", "remoteok"]

def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

def job_key(company, title):
    return hashlib.sha1(f"{norm(company)}|{norm(title)}".encode()).hexdigest()[:16]

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
    ]
    for name, fn in aggregators:
        try:
            jobs = fn()
            raw.extend(jobs)
            report[name] = {"ok": True, "fetched": len(jobs)}
        except Exception as e:
            report[name] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    wl = CONFIG.get("watchlist", {})
    ats = ([("greenhouse", s, src_greenhouse) for s in wl.get("greenhouse", [])] +
           [("lever", s, src_lever) for s in wl.get("lever", [])] +
           [("ashby", s, src_ashby) for s in wl.get("ashby", [])])
    for kind, slug, fn in ats:
        name = f"{kind}/{slug}"
        try:
            jobs = fn(slug)
            raw.extend(jobs)
            report[name] = {"ok": True, "fetched": len(jobs)}
        except urllib.error.HTTPError as e:
            report[name] = {"ok": False, "error": f"HTTP {e.code}"}
        except Exception as e:
            report[name] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # Boards discovered while hunting originals join the pool permanently -
    # where there's one design job, there are often more. Agency boards that
    # post other companies' roles under their own name stay out.
    AGENCY_BOARDS = {"jobgether", "lemonio", "jobgether-1"}
    configured = {(k, s) for k, slugs in wl.items() for s in slugs}
    for comp, ent in list(ATS_CACHE.items()):
        if comp.startswith("hunt:") or not isinstance(ent, dict) or not ent.get("kind"):
            continue
        kind, slug = ent["kind"], ent["slug"]
        name = f"{kind}/{slug}"
        if (kind, slug) in configured or name in report or slug in AGENCY_BOARDS:
            continue
        try:
            jobs = board_jobs(kind, slug)
            raw.extend(jobs)
            report[name] = {"ok": True, "fetched": len(jobs)}
        except Exception as e:
            report[name] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # Filter + classify + dedupe.
    kept = {}
    for j in raw:
        if not title_ok(j.get("title")):
            continue
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
        key = job_key(j.get("company"), j.get("title"))
        entry = {
            "id": key, "title": (j.get("title") or "").strip(),
            "company": (j.get("company") or "").strip() or "?",
            "url": j.get("url"), "source": j["source"],
            "location": (j.get("location") or "").strip(),
            "bucket": bucket, "salary": salary,
            "posted": j.get("posted"),
            "xp": find_xp(j.get("desc")),
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
        if kind == "careers":
            # bring the board's live design roles into the brief pane -
            # the honest answer when the exact posting is gone or hidden
            ent2 = ATS_CACHE.get(norm(j["company"])) or {}
            if ent2.get("kind"):
                roles = [{"title": bj["title"], "url": bj.get("url"),
                          "location": bj.get("location") or ""}
                         for bj in board_jobs(ent2["kind"], ent2["slug"])
                         if INC.search(bj.get("title") or "")][:8]
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
        target = j.get("apply_url") or j["url"]
        pu, pf = prev_frame.get(j["id"], (None, None))
        j["frameable"] = pf if (pu == target and pf is not None) else frameable(target)
    for sid in stale_ids:
        kept.pop(sid, None)
    if stale_ids:
        print(f"dropped {len(stale_ids)} listing(s) closed at the source")
    ATS_CACHE_FILE.write_text(json.dumps(ATS_CACHE, indent=1, sort_keys=True))
    report["websearch"] = ({"ok": False, "error": "bot challenge - hunts paused"}
                           if throttled else {"ok": True, "fetched": hunted})

    # Merge with existing DB.
    old = {}
    if DATA_FILE.exists():
        for j in json.loads(DATA_FILE.read_text()).get("jobs", []):
            old[j["id"]] = j
    failed_prefixes = tuple(n for n, r in report.items() if not r["ok"])
    jobs_out = []
    for key, j in kept.items():
        prev = old.pop(key, None)
        if prev and not j.get("apply_url") and prev.get("apply_url"):
            j["apply_url"] = prev["apply_url"]
            j["apply_kind"] = prev.get("apply_kind", "direct")
        if prev and not j.get("xp") and prev.get("xp"):
            j["xp"] = prev["xp"]
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

    # JDs for the overnight runner: every active role (facts parsing), the
    # fresh non-tiebreak ones flagged for CV generation.
    day_ago = (NOW - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    jds = {}
    for j in jobs_out:
        jd = j.pop("_jd", "")
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
