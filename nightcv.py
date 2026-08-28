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
import urllib.request
from pathlib import Path

RADAR = "https://cesarxdesign.github.io/jobradar/data/jds.json"
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
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

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
    todo = {k: v for k, v in jobs.items() if k not in done}
    if not todo:
        log(f"nothing to do ({len(jobs)} fresh, all handled)")
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
        started = subprocess.Popen(
            [sys.executable, "server.py"], cwd=TCV_DIR, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
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
                fail += 1
    finally:
        if started:
            started.terminate()
    log(f"run complete: {ok} CVs ready, {fail} failed, {len(jobs)} fresh openings")
    return 0 if fail == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
