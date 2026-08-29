#!/usr/bin/env python3
"""nightcv, cloud edition - tuned CVs in GitHub Actions, Mac closed.

Runs right after the nightly scrape (see .github/workflows/nightcv.yml).
Reads data/jds.json straight from the checkout, starts the CV-Tuner server
(checked out at ./tcv) with TCV_BACKEND=cli so every tune runs on the
Claude subscription via the CLAUDE_CODE_OAUTH_TOKEN secret, then copies
each finished PDF into cvs/<job id>/ where GitHub Pages serves it to the
dashboard's CV button.

State lives in the repo so runs are stateless between runners:
data/cvdone.json (what's been tuned), data/cvs.json (the dashboard
manifest), data/facts.json (parsed JD facts).

The source-link hunt stays in nightcv.py on the Mac: search engines block
cloud IPs.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
TCV_DIR = HERE / "tcv"
TCV_PORT = int(os.environ.get("TCV_PORT", "8765"))
TCV = f"http://127.0.0.1:{TCV_PORT}"
OUT_DIR = HERE / "cv_out"
DONE_FILE = HERE / "data" / "cvdone.json"
CVS_DIR = HERE / "cvs"
MAX_PER_RUN = int(os.environ.get("NIGHTCV_MAX", "8"))
PARSE_MAX = int(os.environ.get("NIGHTCV_PARSE_MAX", "40"))
PAGES = os.environ.get("NIGHTCV_PAGES", "1")
PDF_NAME = "Cesar Garcia CV.pdf"


def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M')} {msg}", flush=True)


def http(url, data=None, timeout=30):
    req = urllib.request.Request(
        url, data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json", "User-Agent": "nightcv-cloud"})
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


def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def main():
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        log("FAIL CLAUDE_CODE_OAUTH_TOKEN is not set - create one with "
            "`claude setup-token` and add it as a repo secret")
        return 1
    jobs = read_json(HERE / "data" / "jds.json", {}).get("jobs", {})
    done = read_json(DONE_FILE, {})
    facts = read_json(HERE / "data" / "facts.json", {})
    triage = read_json(HERE / "data" / "state.json", {}).get("jobs", {})

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

    env = dict(os.environ)
    env["TCV_BACKEND"] = "cli"  # subscription only, never a billed API key
    env["TCV_OUT"] = str(OUT_DIR)
    started = subprocess.Popen([sys.executable, "server.py"], cwd=TCV_DIR, env=env)
    for _ in range(40):
        if tcv_alive():
            break
        if started.poll() is not None:
            log("FAIL TCV server died on start")
            return 1
        time.sleep(0.5)
    else:
        log("FAIL TCV server never answered health check")
        started.kill()
        return 1

    # --- facts: TCV's fast parse of each JD --------------------------------
    parsed = 0
    facts_file = HERE / "data" / "facts.json"
    for jid, j in to_parse.items():
        if parsed >= PARSE_MAX:
            log(f"parse cap reached - {len(to_parse) - parsed} left for later")
            break
        try:
            p = http(TCV + "/api/parse",
                     {"jd": f"{j['title']}\n{j['company']}\n{j['jd']}"}, timeout=180)
            facts[jid] = {k: p.get(k, "") for k in
                          ("salary", "remote", "country", "portugal", "europe",
                           "years_experience", "seniority", "must_haves")}
            facts[jid]["at"] = time.strftime("%Y-%m-%dT%H:%M")
            parsed += 1
            facts_file.write_text(json.dumps(facts, indent=1, ensure_ascii=False))
        except Exception as e:
            log(f"parse FAIL {j['company']} - {j['title']}: {e}")
    if parsed:
        log(f"parsed facts for {parsed} roles")

    # --- tunes: one CV per fresh opening, newest first ---------------------
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
                src = r.get("path", "")
                web = ""
                if src and os.path.exists(src):
                    dest = CVS_DIR / jid
                    dest.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest / PDF_NAME)
                    web = f"cvs/{jid}/{PDF_NAME}"
                done[jid] = {"at": time.strftime("%Y-%m-%dT%H:%M"),
                             "path": web, "label": label}
                DONE_FILE.write_text(json.dumps(done, indent=1))
                extra = " OVERFLOW" if r.get("overflow") else ""
                log(f"done: {label} -> {web or '?'}{extra}")
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
        started.terminate()

    # manifest for the dashboard's CV button (id -> Pages path, or the old
    # Mac path for CVs tuned before the move - the button handles both)
    manifest = {k: {"path": e["path"], "label": e.get("label", ""),
                    "at": e.get("at", "")}
                for k, e in done.items() if e.get("path")}
    (HERE / "data" / "cvs.json").write_text(json.dumps(manifest, indent=1))
    log(f"run complete: {ok} CVs ready, {fail} failed, {len(jobs)} fresh openings")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
