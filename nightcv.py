#!/usr/bin/env python3
"""nightcv - the morning Mac leg of the radar: the source-link hunt.

CV tuning and JD fact-parsing moved to the cloud (nightcv_cloud.py, run
by GitHub Actions right after the nightly scrape), so the Mac can stay
closed overnight. What remains here is the one job that cannot leave the
house: hunting each opening's original posting link. Search engines block
cloud IPs, so this runs on the home connection (launchd, mornings) and
pushes what it finds back to the radar.
"""
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

RADAR = "https://cesarxdesign.github.io/Radar/data/jds.json"
HERE = Path(__file__).resolve().parent
LOG_FILE = HERE / "nightcv.log"


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M')} {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def http(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "nightcv"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.reason}") from None


def main():
    try:
        feed = http(RADAR + "?t=%d" % time.time())
    except Exception as e:
        log(f"FAIL couldn't fetch radar feed: {e}")
        return 1
    jobs = feed.get("jobs", {})

    def git(*a):
        subprocess.run(["git", "-C", str(HERE),
                        "-c", "user.email=cesarxdesign@gmail.com",
                        "-c", "user.name=Cesar Garcia"] + list(a),
                       check=True, capture_output=True, timeout=120)
    try:
        git("pull", "--rebase", "--autostash", "origin", "main")  # before touching data/
    except Exception as e:
        log(f"git pull FAIL: {e}")

    hunted = 0
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
        if hunted:
            scrape.ATS_CACHE_FILE.write_text(
                json.dumps(scrape.ATS_CACHE, indent=1, sort_keys=True))
            (HERE / "data" / "jobs.json").write_text(
                json.dumps(jobs_db, indent=1, ensure_ascii=False))
    except Exception as e:
        log(f"hunt phase FAIL: {e}")

    if hunted:
        log(f"hunted links for {hunted} roles")
        try:
            git("add", "data/ats_cache.json", "data/jobs.json")
            git("commit", "-m", "morning run: %d hunted links" % hunted)
            git("pull", "--rebase", "--autostash", "origin", "main")
            git("push", "origin", "main")
            log("results pushed to the radar")
        except Exception as e:
            log(f"push FAIL (kept locally): {e}")
    else:
        log(f"nothing to hunt ({len(jobs)} active)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
