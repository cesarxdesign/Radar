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
  PLACE  read the WORK MODEL out of the JD body first, then apply it to the
         location (César, 2026-08-31 - the radar was putting plain onsite
         Madrid/Amsterdam/Berlin roles in Open because the location named a
         European country). A remote-work PERK ("flexible hybrid model",
         "#LI-Hybrid", a WFH stipend) is not a remote HIRING SCOPE; a JD that
         names a city and never offers to hire elsewhere is onsite there.
         onsite/hybrid: Lisbon only, everywhere else cut. Remote: Europe /
         EMEA / worldwide ok; an explicit country list only ok when Portugal
         is in it; remote scoped to one non-PT country -> unsure, his call.
         A posting that CONTRADICTS itself (listed remote, body describes
         office days) is unsure, never cut - either half can be the stale
         one. Only a residence lock in the body settles it. Timezones are
         NOT a criterion.
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

# One source of truth for the geography. scrape.py's module level is a config
# read and a pile of re.compile - no network, no writes - so this is cheap.
from scrape import ELSEWHERE_RE, EU_COUNTRY_RE, EU_RE, PT_RE

ROOT = Path(__file__).resolve().parent
JOBS_FILE = ROOT / "data" / "jobs.json"
STATE_FILE = ROOT / "data" / "state.json"
JDS_FILE = ROOT / "data" / "jds.json"
JUDGED_FILE = ROOT / "data" / "judged.json"
# No count cap. The real limit is wall-clock: judge until the time budget is
# spent, saving progress as we go so a job timeout never loses work. The next
# run continues where this one stopped. Budget leaves headroom under the
# workflow's job timeout for the npm install and the final push.
TIME_BUDGET_S = int(os.environ.get("JUDGE_BUDGET_S", "6600"))  # 110 min; loop ends when pending is drained
SAVE_EVERY = 20  # flush judged.json to disk this often, so a crash keeps work
# Bumped whenever the criteria in PROMPT change, so rejudge.py can find every
# verdict that was reached under an older reading and re-run just those.
#   1 -> original (2026-08-29)
#   2 -> work model read from the JD body before place (2026-08-31)
PROMPT_V = 2

REMOTE_FIELD_RE = re.compile(r"\bremote\b|\banywhere\b|\bwork from home\b", re.I)


def contradicts(location):
    """True when the location field claims a remote scope the body could be
    overruling - i.e. the two halves of the posting genuinely conflict.

    "Remote" or "Remote - Europe" against a body describing Berlin office days
    is a conflict: either half may be the stale one. "Remote - US" against the
    same body is NOT - the field already scoped Portugal out, so the body is
    not contradicting anything and the role cuts on its own scope.
    """
    loc = location or ""
    if not REMOTE_FIELD_RE.search(loc):
        return False
    if ELSEWHERE_RE.search(loc) and not (EU_COUNTRY_RE.search(loc)
                                         or EU_RE.search(loc)
                                         or PT_RE.search(loc)):
        return False
    return True


PROMPT = """You judge job postings for César, a senior product designer based in Lisbon, Portugal.
Read the WHOLE description as carefully as a person would, then answer with ONE JSON object, nothing else:
{{"role": "pd|not_pd|unsure", "work": "remote|hybrid|onsite|unclear", "scope": "<the hiring scope in your own words, max 8 words>", "place": "pt|eu|ww|cut|unsure", "why": "<short reason, only when not clearly ok>"}}

ROLE
"pd"  = product design of software / digital apps: UX/UI, user flows, design systems, prototypes, usability; leading product-design teams; design-code hybrids (Design Engineer, UX Engineer, UI Engineer).
"not_pd" = marketing / brand / graphic / motion / print design; physical / industrial / architectural / hardware / mechanical / electrical design; game design or game content; software/backend/frontend/data engineering; product management, product ops, program/project management, sales, recruiting, legal.
"unsure" = UX/design research only, or content design / UX writing (a different discipline - César decides case by case).

WORK MODEL - decide this FIRST, from the body of the JD, before you touch "place".
César lives in Lisbon and is not relocating. A job in another country only works if the company hires people who live somewhere else. So the question is never "which city is on the ad" - it is "does this posting offer to hire someone who does not live there".
- "remote" = the posting states a REMOTE HIRING SCOPE: it says who it will hire and where they may live. "Remote - Europe", "fully remote across the EU", "you can be based anywhere in Europe", "remote (Portugal, Spain, Germany)", "distributed team, hire anywhere".
- "hybrid" = expected in an office some days, or the ad pairs a city with hybrid/flexible working.
- "onsite" = expected in an office.
- "unclear" = the JD genuinely never says, in any form.

A REMOTE HIRING SCOPE IS NOT THE SAME AS A REMOTE-WORK PERK. This is the single most common mistake - do not make it.
The word "remote" inside a benefits or culture blurb says nothing about where you may live. All of these are "hybrid", NOT "remote":
- "a flexible hybrid work model that balances remote focus with vibrant office collaboration"
- "hybrid work model balancing office and remote work"
- "2-3 days a week in the office, remote the rest"
- "#LI-Hybrid", "work from home stipend", "flexible working", "remote-friendly culture"
- "work from anywhere for up to N weeks a year" (a holiday policy, not a hiring scope)
Other tells that a posting is really office-bound: it names a specific office or neighbourhood, offers a relocation package, lists a commuter benefit, or lists local perks (meal vouchers, a gym at HQ, a national health plan).

IF THE JD NAMES A PLACE AND NEVER STATES A REMOTE HIRING SCOPE, THE ANSWER IS "onsite" - NOT "unclear", NOT "remote".
A posting headed "Madrid, Spain" that says nothing about working remotely is a job in Madrid. Ads do not mention the office because being there is assumed. Never grant a remote scope the posting did not claim.
Use "unclear" only when there is no usable place either - e.g. the location field is empty and the body never says where.

ONE EXCEPTION, and it matters. If the text below is obviously NOT a real job posting - a single short paragraph, a summary written about the role rather than by the employer, anything that has no requirements list, no benefits, no "about us" - then we never captured the real description. A summary cannot mention a work model, so its silence tells you nothing. Answer work="unclear", place="unsure", why="no full JD captured". Do NOT read a stub as onsite.

PLACE - now apply the work model to the location.
- work=onsite or hybrid, in Lisbon / greater Lisbon / Portugal -> "pt".
- work=onsite or hybrid, ANYWHERE ELSE -> "cut", why = "onsite <city>" or "hybrid <city>". This is true no matter how European the city is. Madrid, Berlin, Amsterdam, Paris, London, Warsaw, Dublin, Copenhagen: all "cut" when the job is office-bound. César cannot commute to them.
- work=remote, scoped to Europe / EMEA / EEA / "the EU" / "anywhere in Europe" -> "eu".
- work=remote, scoped worldwide / anywhere / globally / no region limit at all -> "ww".
- work=remote, with an explicit list of countries or a named set of locations:
    - Portugal is in the list -> "pt".
    - Portugal is NOT in the list -> "cut", why = "remote scope excludes Portugal (<list>)".
- work=remote, scoped to ONE non-Portugal country and nothing wider ("Remote - Germany", "All France (remote)", "remote within Poland") -> "unsure", why = "remote but scoped to <country> only". These often mean local payroll, but not always - César decides.
- The JD demands residence, citizenship, work authorization, or payroll in a specific non-Portugal country ("must be based in Germany", "US citizen", "right to work in the UK required") -> "cut".
- A NON-European remote scope: US-only, "Americas", North America, LATAM, APAC, Asia, Canada, Australia, India -> "cut".
- work=unclear -> "unsure", why = "work model not stated".
- IGNORE timezone / working-hours requirements entirely - never cut on those.
- JD written in a language other than English or Portuguese: place = "cut", why = "JD in <language>".

WHEN THE LOCATION FIELD AND THE BODY DISAGREE
- The body ADDS a scope the field never had - field "Amsterdam", body "this role is fully remote across Europe" -> the body wins, remote/eu. A location field naming an office is perfectly compatible with hiring remotely; it is just naming the anchor office.
- The body TAKES AWAY a scope the field claimed - field "Remote", body "you will join us in our Berlin office 3 days a week" -> this is a genuine CONTRADICTION, not a cut. One of the two is wrong and you cannot tell which: the field may be a stale default, or the office line may be a boilerplate "how we work" block pasted into every ad including the remote ones. Answer work="hybrid", place="unsure", why="listed remote, body says <city> hybrid". Do NOT cut it.
- The one exception: if the body does not merely describe office days but LOCKS residence - "you must be based in Berlin", "this role requires relocation to Munich", "candidates must hold a German work permit" - there is no contradiction left. The body has settled it: "cut".

EXAMPLES (title | location | what the body says | -> verdict)
- Senior Design Engineer | Madrid, Spain | only "flexible hybrid work model that balances remote focus with office collaboration" -> {{"role":"pd","work":"hybrid","scope":"Madrid office","place":"cut","why":"hybrid Madrid, remote is a perk not a hiring scope"}}
- Senior Product Designer | Madrid, MD, Spain | "#LI-Hybrid", offices near the Bernabeu, relocation package -> {{"role":"pd","work":"hybrid","scope":"Madrid office","place":"cut","why":"hybrid Madrid"}}
- Senior Product Designer | Amsterdam, Netherlands | body never mentions working remotely -> {{"role":"pd","work":"onsite","scope":"Amsterdam office","place":"cut","why":"onsite Amsterdam, no remote scope stated"}}
- Product Designer, Risk | Ireland / United Kingdom | body never mentions working remotely -> {{"role":"pd","work":"onsite","scope":"Ireland or UK office","place":"cut","why":"onsite, no remote scope stated"}}
- Product Designer | Europe, London | "Location: Europe remote or London hybrid" -> {{"role":"pd","work":"remote","scope":"Europe remote or London hybrid","place":"eu"}}
- Senior Product Designer | Remote - Europe | "hire anywhere in Europe" -> {{"role":"pd","work":"remote","scope":"Europe","place":"eu"}}
- Staff Product Designer | Germany, ... Portugal, France, Poland, Spain | remote, that country list -> {{"role":"pd","work":"remote","scope":"EU country list incl. Portugal","place":"pt"}}
- Senior Product Designer | Remote - Germany, Austria, Switzerland | that list only -> {{"role":"pd","work":"remote","scope":"DACH only","place":"cut","why":"remote scope excludes Portugal (DE/AT/CH)"}}
- Design system designer | All France (remote) | remote but French contract implied -> {{"role":"pd","work":"remote","scope":"France only","place":"unsure","why":"remote but scoped to France only"}}
- Senior Product Designer | Remote | body: "we work from our Berlin office 3 days a week" -> {{"role":"pd","work":"hybrid","scope":"listed remote, Berlin office 3 days","place":"unsure","why":"listed remote, body says Berlin hybrid"}}
- Senior Product Designer | Remote | body: "you must be based in Berlin" -> {{"role":"pd","work":"hybrid","scope":"Berlin residence required","place":"cut","why":"Berlin residence required"}}
- Lead Product Designer | Portugal - Remote, UK - Remote | -> {{"role":"pd","work":"remote","scope":"Portugal or UK","place":"pt"}}
- Product Designer | Lisbon | in-office -> {{"role":"pd","work":"onsite","scope":"Lisbon office","place":"pt"}}
- Staff Product Designer | Americas Remote | -> {{"role":"pd","work":"remote","scope":"Americas","place":"cut","why":"Americas-only scope"}}
- Design Engineer | Remote - EMEA | -> {{"role":"pd","work":"remote","scope":"EMEA","place":"eu"}}
- Staff Backend Engineer | Europe | -> {{"role":"not_pd","work":"remote","scope":"Europe","place":"eu","why":"engineering, not design"}}
- Brand Designer | Anywhere | -> {{"role":"not_pd","work":"remote","scope":"worldwide","place":"ww","why":"brand, not product design"}}
- UX Researcher | Remote - Europe | -> {{"role":"unsure","work":"remote","scope":"Europe","place":"eu","why":"research discipline"}}

TITLE: {title}
COMPANY: {company}
LOCATION FIELD: {location}
JD:
{jd}"""


JD_CHARS = 14000  # a full JD; the remote scope is often the last line of one
# Under this, what we hold is a blurb, not a posting. Some aggregators serve
# their own one-paragraph summary as the description and we never reached the
# original - a summary cannot mention a work model, so its silence proves
# nothing. Roles like that go to Unsure, never cut. (designjobs.world, 2026-08-31)
JD_STUB_CHARS = 600

def jd_window(jd):
    """Trim a JD to the model's window keeping BOTH ends.

    Where a posting states its work model is bimodal: a "Location:" line near
    the top, or the benefits block at the very bottom. A head-only cut threw
    away the bottom half of every long JD - exactly the half that says
    "hybrid, 3 days in the Madrid office".
    """
    if len(jd) <= JD_CHARS:
        return jd
    half = JD_CHARS // 2
    return jd[:half] + "\n\n[…middle of the description omitted…]\n\n" + jd[-half:]



def _extract(text):
    """Pull the verdict object out of a model reply, tolerating markdown
    fences, prose, or a wrapping envelope. Returns dict or None."""
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    for m in re.finditer(r"\{[^{}]*\}", text, re.S):  # smallest balanced objects first
        try:
            v = json.loads(m.group(0))
            if "role" in v or "place" in v:
                return v
        except Exception:
            continue
    m = re.search(r"\{.*\}", text, re.S)  # last resort: greedy
    try:
        return json.loads(m.group(0)) if m else None
    except Exception:
        return None

def ask(prompt, tries=3):
    """Judge one posting. Retries on a flaky/unparseable response so a single
    bad reply never strands a good role as permanently pending.

    Every failure path reports WHY. The api_error branch used to `continue`
    silently, so a run where the CLI refused every call printed 28 identical
    "UNPARSEABLE" lines and not one word of cause.
    """
    last = None
    for attempt in range(tries):
        try:
            r = subprocess.run(["claude", "-p", prompt, "--output-format", "json"],
                               capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            last = "timed out after 300s"
            continue
        text = r.stdout
        try:
            env = json.loads(r.stdout)
            if env.get("is_error"):
                last = f"api error: {str(env.get('result') or env)[:300]}"
                continue
            text = env.get("result") or env.get("text") or r.stdout
        except Exception:
            pass
        v = _extract(text)
        if v and (v.get("role") or v.get("place")):
            return v
        last = (f"rc={r.returncode} unparseable reply: "
                f"{(text or r.stderr or '')[:300]!r}")
    ask.last_error = last
    print(f"    ! judge call failed after {tries} tries: {last}")
    return None


ask.last_error = None


NEST_HINT = ("the claude CLI refuses to run inside another Claude Code session. "
             "Run this from a plain terminal outside the Claude Code app, or "
             "let the nightly GitHub Actions run do it.")


def preflight():
    """One cheap call to prove the CLI answers, before spending a whole batch.

    A batch that cannot make its first call should say so in two seconds, not
    grind through every role failing identically.
    """
    v = ask('Reply with exactly this JSON and nothing else: '
            '{"role":"pd","work":"remote","scope":"test","place":"eu"}', tries=1)
    if v:
        return True
    err = ask.last_error or ""
    print("\npreflight failed - not starting the batch.")
    if "another Claude Code session" in err or "CLAUDECODE" in err:
        print(f"  {NEST_HINT}")
    return False


def verdict_to_bucket(v, location="", stub=False):
    if not v:
        return None
    role, place = v.get("role"), v.get("place")
    work = (v.get("work") or "").strip().lower()
    scope = (v.get("scope") or "").strip()
    why = (v.get("why") or "").strip()
    # Belt and braces on the failure this prompt exists to stop: an office-bound
    # job outside Portugal can never come back pt/eu/ww, whatever the model said.
    if work in ("onsite", "hybrid") and place in ("eu", "ww"):
        # ...unless the posting contradicts itself. A listing that says "Remote"
        # and then describes office days is not evidence of an office job - one
        # of the two halves is stale and there is no telling which (César,
        # 2026-08-31). Contradiction is the definition of Unsure, so never cut
        # on it; a residence lock is the only thing that settles it, and the
        # model returns place="cut" for that on its own.
        if contradicts(location):
            return {"bucket": "tiebreak",
                    "why": why or f"listed remote, body says {scope or work}"}
        if stub:
            return {"bucket": "tiebreak",
                    "why": "no full JD captured - work model unverified"}
        return {"bucket": "cut",
                "why": why or f"{work} {scope or 'outside Portugal'}"}
    # A work model the model never committed to is not evidence of remote.
    if work == "unclear" and place in ("eu", "ww"):
        return {"bucket": "tiebreak", "why": why or "work model not stated"}
    if role == "not_pd":
        return {"bucket": "cut", "why": why or "judge: not product design"}
    if place == "cut":
        # A place cut needs a posting to have said something. With only a stub
        # we never saw one, so the cut would be an artefact of our scrape.
        if stub:
            return {"bucket": "tiebreak",
                    "why": "no full JD captured - work model unverified"}
        return {"bucket": "cut", "why": why or "judge: place excluded"}
    if role == "unsure" or place == "unsure":
        return {"bucket": "tiebreak", "why": why or "judge: not clear-cut on role or place"}
    if role == "pd" and place in ("pt", "eu", "ww"):
        return {"bucket": place, "why": "", "work": work, "scope": scope}
    return None


def decided(state):
    """Every role César has already ruled on himself - applied, discarded, or
    manually moved to Open.

    Off limits to the judge. A verdict of "cut" sets active=false, which would
    delete a role he applied to out of his own Applied tab and silently
    overrule a move-to-open he made by hand. His decisions win.
    """
    return set(state.get("jobs") or {}) | set(state.get("verdicts") or {})


def main():
    data = json.loads(JOBS_FILE.read_text())
    jds = json.loads(JDS_FILE.read_text()).get("jobs", {})
    judged = json.loads(JUDGED_FILE.read_text()) if JUDGED_FILE.exists() else {"jobs": {}}
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    off = decided(state)

    # Criteria change; verdicts persist. Without this a prompt fix only ever
    # reached roles scraped after it, and everything already on the board kept
    # the old reading forever - which needed a human at a terminal to undo.
    # New roles go first so a big backlog can never starve tonight's intake.
    fresh, stale = [], []
    for j in data["jobs"]:
        if not j.get("active") or j["id"] in off:
            continue
        v = judged["jobs"].get(j["id"])
        if not v:
            fresh.append(j)
        elif v.get("pv") != PROMPT_V:
            stale.append(j)
    pending = fresh + stale
    print(f"pending: {len(pending)} ({len(fresh)} new, {len(stale)} on an "
          f"older prompt) | budget {TIME_BUDGET_S}s")
    start = time.monotonic()
    done = 0
    for j in pending:
        if time.monotonic() - start > TIME_BUDGET_S:
            print(f"  time budget spent after {done}; {len(pending)-done} left for next run")
            break
        jd = jd_window(jds.get(j["id"], {}).get("jd") or "")
        try:
            v = ask(PROMPT.format(title=j["title"], company=j["company"],
                                  location=j.get("location") or "(none)",
                                  jd=jd or "(no description available - judge on title and location only)"))
        except Exception as e:
            print(f"  skip {j['company']} - {j['title']}: {e}")
            continue
        out = verdict_to_bucket(v, j.get("location") or "",
                                 stub=len(jd) < JD_STUB_CHARS)
        if not out:
            print(f"  unparseable verdict for {j['company']} - {j['title']}")
            continue
        rec = {"bucket": out["bucket"], "why": out["why"],
               "at": data.get("run_id", ""), "pv": PROMPT_V}
        # keep the model's own reading of the work model - it is what the
        # place verdict hangs on, so it has to be auditable after the fact
        for k in ("work", "scope"):
            if out.get(k):
                rec[k] = out[k]
        judged["jobs"][j["id"]] = rec
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
        if v["bucket"] == "cut" and j["id"] not in off:
            j["active"] = False
        elif v["bucket"] == "cut":
            continue  # his verdict stands; leave the role exactly as it is
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
