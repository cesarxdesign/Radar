#!/usr/bin/env python3
"""nightcv - overnight tuned CVs for fresh JobRadar openings.

Runs on the Mac (launchd, mornings). Pulls data/jds.json from the live
radar, drives the local TCV app for each opening first seen in the last
24h, and lets TCV drop each PDF in ~/Desktop/TCV/<company-role>/.

TCV does the actual tuning via the `claude` CLI on the subscription -
this script only feeds it. Cap per run keeps a flood from burning hours.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

RADAR = "https://cesarxdesign.github.io/Radar/data/jds.json"
TCV_DIR = Path.home() / "Claude" / "CXD" / "cv" / "tcv"
TCV_PORT = int(os.environ.get("TCV_PORT", "8765"))
TCV = f"http://127.0.0.1:{TCV_PORT}"
HERE = Path(__file__).resolve().parent
DONE_FILE = Path(os.environ.get("NIGHTCV_DONE") or HERE / ".nightcv_done.json")
LOG_FILE = HERE / "nightcv.log"
MAX_PER_RUN = int(os.environ.get("NIGHTCV_MAX", "8"))
PAGES = os.environ.get("NIGHTCV_PAGES", "1")

def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M')} {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def http(url, data=None, timeout=30):
    req = urllib.request.Request(
        url, data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json", "User-Agent": "nightcv"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {body or e.reason}") from None

def tcv_alive():
    try:
        http(TCV + "/api/health", timeout=2)
        return True
    except Exception:
        return False

def main():
    try:
        feed = http(RADAR + "?t=%d" % time.time())
    except Exception as e:
        log(f"FAIL couldn't fetch radar feed: {e}")
        return 1
    jobs = feed.get("jobs", {})
    done = {}
    if DONE_FILE.exists():
        try:
            done = json.loads(DONE_FILE.read_text())
        except Exception:
            done = {}
    facts_file = HERE / "data" / "facts.json"
    facts = {}
    if facts_file.exists():
        try:
            facts = json.loads(facts_file.read_text())
        except Exception:
            facts = {}
    triage = {}
    try:
        triage = json.loads((HERE / "data" / "state.json").read_text()).get("jobs", {})
    except Exception:
        pass
    def handled(k):
        e = done.get(k)
        return bool(e) and (e.get("path") or e.get("fails", 0) >= 2)
    todo = {k: v for k, v in jobs.items()
            if not handled(k) and v.get("fresh") and not triage.get(k)
            and v.get("bucket") != "tiebreak"}  # unsure roles get facts, never CVs
    to_parse = {k: v for k, v in jobs.items() if k not in facts}
    if not todo and not to_parse:
        log(f"nothing to do ({len(jobs)} active, all handled)")
        return 0

    started = None
    if not tcv_alive():
        if not (TCV_DIR / "server.py").exists():
            log(f"FAIL TCV not found at {TCV_DIR}")
            return 1
        env = dict(os.environ)
        home = str(Path.home())
        env["PATH"] = ":".join([
            home + "/.claude/local", "/opt/homebrew/bin", "/usr/local/bin",
            home + "/.npm-global/bin", home + "/.bun/bin", home + "/.local/bin",
            env.get("PATH", "/usr/bin:/bin")])
        # CLI only - never silently fall back to the billed API key
        env.setdefault("TCV_BACKEND", "cli")
        srvlog = open(HERE / "tcv-server.log", "ab")
        started = subprocess.Popen(
            [sys.executable, "server.py"], cwd=TCV_DIR, env=env,
            stdout=srvlog, stderr=subprocess.STDOUT)
        for _ in range(40):
            if tcv_alive():
                break
            if started.poll() is not None:
                log("FAIL TCV server died on start - if this mentions the "
                    "output folder, grant python Desktop access (System "
                    "Settings > Privacy > Files and Folders) or run me once "
                    "from Terminal")
                return 1
            time.sleep(0.5)
        else:
            log("FAIL TCV server never answered health check")
            started.kill()
            return 1

    # --- facts: TCV's fast parse of each JD, pushed back to the repo ------
    parsed = 0
    for jid, j in to_parse.items():
        if parsed >= int(os.environ.get("NIGHTCV_PARSE_MAX", "40")):
            log(f"parse cap reached - {len(to_parse) - parsed} left for later")
            break
        try:
            p = http(TCV + "/api/parse", {"jd": f"{j['title']}\n{j['company']}\n{j['jd']}"}, timeout=180)
            facts[jid] = {k: p.get(k, "") for k in
                          ("salary", "remote", "country", "portugal", "europe",
                           "years_experience", "seniority", "must_haves")}
            facts[jid]["at"] = time.strftime("%Y-%m-%dT%H:%M")
            parsed += 1
            facts_file.parent.mkdir(exist_ok=True)
            facts_file.write_text(json.dumps(facts, indent=1, ensure_ascii=False))
        except Exception as e:
            log(f"parse FAIL {j['company']} - {j['title']}: {e}")
    # --- hunt: search engines block cloud IPs, so the source-link hunt
    # runs here on the home connection and pushes results back -----------
    hunted = 0
    def git(*a):
        subprocess.run(["git", "-C", str(HERE),
                        "-c", "user.email=cesarxdesign@gmail.com",
                        "-c", "user.name=Cesar Garcia"] + list(a),
                       check=True, capture_output=True, timeout=120)
    try:
        git("pull", "--rebase", "--autostash", "origin", "main")  # before touching data/
    except Exception as e:
        log(f"git pull FAIL: {e}")
    try:
        import scrape
        jobs_db = json.loads((HERE / "data" / "jobs.json").read_text())
        by_id = {j["id"]: j for j in jobs_db.get("jobs", [])}
        for jid, j in jobs.items():
            if hunted >= int(os.environ.get("NIGHTCV_HUNT_MAX", "12")):
                break
            dbj = by_id.get(jid)
            if dbj is None or dbj.get("apply_url") or not dbj.get("active"):
                continue
            try:
                link, kind, stale = scrape.hunt_web(j["company"], j["title"])
            except scrape.SearchThrottled:
                log("hunt: search engine bot-challenged this IP - stopping")
                break
            except Exception as e:
                log(f"hunt FAIL {j['company']}: {e}")
                continue
            hunted += 1
            scrape.ATS_CACHE["hunt:" + jid] = {
                "url": link, "kind": kind, "stale": stale,
                "checked": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            if link:
                dbj["apply_url"], dbj["apply_kind"] = link, kind
                dbj["direct"] = kind == "direct"
                dbj["frameable"] = scrape.frameable(link)
                log(f"hunt: {j['company']} -> [{kind}] {link}")
                if kind == "direct":
                    # the original JD replaces the aggregator's noisy copy
                    # before any CV is tuned against it
                    orig = scrape.fetch_original(link, j["company"], j["title"])
                    if orig.get("text") and len(orig["text"]) >= 200:
                        j["jd"] = orig["text"][:20000]
                        log(f"hunt: {j['company']} JD swapped for the original")
        if hunted:
            scrape.ATS_CACHE_FILE.write_text(
                json.dumps(scrape.ATS_CACHE, indent=1, sort_keys=True))
            (HERE / "data" / "jobs.json").write_text(
                json.dumps(jobs_db, indent=1, ensure_ascii=False))
    except Exception as e:
        log(f"hunt phase FAIL: {e}")

    if parsed or hunted:
        log(f"parsed facts for {parsed} roles, hunted links for {hunted}")
        try:
            git("add", "data/facts.json", "data/ats_cache.json", "data/jobs.json")
            git("commit", "-m", "morning run: %d facts, %d hunted links" % (parsed, hunted))
            git("pull", "--rebase", "--autostash", "origin", "main")
            git("push", "origin", "main")
            log("results pushed to the radar")
        except Exception as e:
            log(f"push FAIL (kept locally): {e}")

    ok = fail = 0
    try:
        for i, (jid, j) in enumerate(sorted(todo.items(),
                                            key=lambda kv: kv[1].get("first_seen", ""),
                                            reverse=True)):
            if i >= MAX_PER_RUN:
                log(f"cap {MAX_PER_RUN} reached - {len(todo) - i} left for later")
                break
            label = f"{j['company']} - {j['title']}"
            jd_text = (f"{j['title']}\n{j['company']}\n"
                       f"Location: {j.get('location') or j.get('market','')}\n"
                       + (f"Salary: {j['salary']}\n" if j.get("salary") else "")
                       + f"Apply: {j.get('url','')}\n\n{j['jd']}")
            try:
                r = http(TCV + "/api/create", {
                    "jd": jd_text, "pages": PAGES,
                    "facts": {"salary": j.get("salary") or "",
                              "years_experience": j.get("xp") or "",
                              "remote": j.get("market") or "",
                              "country": j.get("location") or ""},
                }, timeout=600)
                done[jid] = {"at": time.strftime("%Y-%m-%dT%H:%M"),
                             "path": r.get("path", ""), "label": label}
                DONE_FILE.write_text(json.dumps(done, indent=1))
                extra = " OVERFLOW" if r.get("overflow") else ""
                log(f"done: {label} -> {r.get('folder','?')}{extra}")
                ok += 1
            except Exception as e:
                log(f"FAIL {label}: {e}")
                entry = done.get(jid) or {}
                entry["fails"] = entry.get("fails", 0) + 1
                entry["label"] = label
                done[jid] = entry
                DONE_FILE.write_text(json.dumps(done, indent=1))
                fail += 1
    finally:
        if started:
            started.terminate()
    # manifest for the dashboard's CV button (id -> local pdf path)
    try:
        manifest = {k: {"path": e["path"], "label": e.get("label", ""),
                        "at": e.get("at", "")}
                    for k, e in done.items() if e.get("path")}
        (HERE / "data" / "cvs.json").write_text(json.dumps(manifest, indent=1))
        if ok:
            git("add", "data/cvs.json")
            git("commit", "-m", "cv manifest: %d ready" % len(manifest))
            git("pull", "--rebase", "--autostash", "origin", "main")
            git("push", "origin", "main")
    except Exception as e:
        log(f"cv manifest FAIL: {e}")
    log(f"run complete: {ok} CVs ready, {fail} failed, {len(jobs)} fresh openings")
    return 0 if fail == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
