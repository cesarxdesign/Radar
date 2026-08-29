#!/usr/bin/env python3
"""One-off cloud tune - a single pasted JD, outside the radar queue.

Driven by .github/workflows/tune-one.yml: César opens an issue titled
"tune: Company — Role" with the posting pasted in the body, and this
turns it into cvs/manual/<slug>/Cesar Garcia CV.pdf on the Claude
subscription, same tuner and prompts as the night run.

Usage: tune_one.py <jd-file> <slug>
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
PDF_NAME = "Cesar Garcia CV.pdf"


def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M')} {msg}", flush=True)


def http(url, data=None, timeout=30):
    req = urllib.request.Request(
        url, data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json", "User-Agent": "tune-one"})
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
    jd_file, slug = sys.argv[1], sys.argv[2]
    jd = Path(jd_file).read_text().strip()
    if len(jd) < 80:
        log("FAIL the issue body is too short to be a JD - paste the posting text")
        return 1
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        log("FAIL CLAUDE_CODE_OAUTH_TOKEN is not set")
        return 1

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

    try:
        facts = {}
        try:
            p = http(TCV + "/api/parse", {"jd": jd}, timeout=180)
            facts = {"salary": p.get("salary", ""),
                     "years_experience": p.get("years_experience", ""),
                     "remote": p.get("remote", ""),
                     "country": p.get("country", "")}
        except Exception as e:
            log(f"parse skipped: {e}")
        r = http(TCV + "/api/create", {
            "jd": jd, "pages": os.environ.get("NIGHTCV_PAGES", "1"),
            "facts": facts}, timeout=600)
        src = r.get("path", "")
        if not (src and os.path.exists(src)):
            log("FAIL tuner returned no PDF")
            return 1
        dest = HERE / "cvs" / "manual" / slug
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest / PDF_NAME)
        web = f"cvs/manual/{slug}/{PDF_NAME}"
        extra = " OVERFLOW" if r.get("overflow") else ""
        log(f"done: {web}{extra}")
        out = os.environ.get("GITHUB_OUTPUT")
        if out:
            with open(out, "a") as f:
                f.write(f"cvpath={web}\n")
        return 0
    finally:
        started.terminate()


if __name__ == "__main__":
    sys.exit(main())
