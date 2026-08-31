#!/usr/bin/env python3
"""Batch-tune the missing CVs locally, straight to ~/Desktop/TCV/<slug>/.
Run OUTSIDE any Claude Code session (e.g. from cron) so the tuner's `claude`
call uses the normal login and isn't blocked as a nested session.
Self-contained: computes the missing set itself, one saved the instant it's built."""
import json, os, shutil, subprocess, sys, time, urllib.request
from pathlib import Path

HERE = Path("/Users/cgair/Claude/jobradar")
TCV_DIR = HERE / "tcv"
PORT = os.environ.get("TCV_PORT", "8790")
TCV = f"http://127.0.0.1:{PORT}"
DEST = Path.home() / "Desktop" / "TCV"
PDF = "Cesar Garcia CV.pdf"


def log(m): print(f"{time.strftime('%H:%M:%S')} {m}", flush=True)


def http(url, data=None, timeout=3900):
    req = urllib.request.Request(
        url, data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def alive():
    try:
        http(TCV + "/api/health", timeout=2); return True
    except Exception:
        return False


batch = json.load(open(HERE / "data" / "cvbatch.json"))
jds = json.load(open(HERE / "data" / "jds.json"))["jobs"]
slugs, labels = batch["slugs"], batch["labels"]

# MISSING = a role with a JD but no folder on the Desktop (top-level or pre-Radar)
missing = [i for i in slugs
           if i in jds and len(jds[i].get("jd", "")) >= 200
           and not (DEST / (slugs[i]) / PDF).exists()
           and not (DEST / "pre-Radar" / (slugs[i]) / PDF).exists()]
log(f"missing to tune: {len(missing)}")
if not missing:
    log("nothing to do"); sys.exit(0)

env = dict(os.environ)
env["TCV_BACKEND"] = "cli"
env["TCV_PORT"] = PORT
env.pop("CLAUDECODE", None)           # belt and braces (cron is already clean)
env.pop("CLAUDE_CODE_ENTRYPOINT", None)
srv = subprocess.Popen([sys.executable, "server.py"], cwd=str(TCV_DIR), env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
for _ in range(80):
    if alive(): break
    if srv.poll() is not None: log("FATAL server died on start"); sys.exit(1)
    time.sleep(0.5)
else:
    log("FATAL server never answered"); srv.kill(); sys.exit(1)
log("tuner up")

ok = fail = 0
try:
    for n, jid in enumerate(missing, 1):
        j = jds[jid]; label = labels.get(jid, jid); slug = slugs[jid]
        jd = (f"{j['title']}\n{j['company']}\n"
              f"Location: {j.get('location') or j.get('market','')}\n"
              + (f"Salary: {j['salary']}\n" if j.get('salary') else "")
              + f"\n{j['jd']}")
        try:
            r = http(TCV + "/api/create", {
                "jd": jd, "pages": "1",
                "facts": {"salary": j.get('salary') or "",
                          "years_experience": j.get('xp') or "",
                          "remote": j.get('market') or "",
                          "country": j.get('location') or ""}}, timeout=3900)
            src = r.get("path", "")
            if src and os.path.exists(src):
                d = DEST / slug; d.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, d / PDF)
                ok += 1; log(f"[{n}/{len(missing)}] OK  {slug}")
            else:
                fail += 1; log(f"[{n}/{len(missing)}] NO-PDF {label}: {str(r)[:180]}")
        except Exception as e:
            fail += 1; log(f"[{n}/{len(missing)}] FAIL {label}: {str(e)[:180]}")
finally:
    srv.terminate()
log(f"DONE: {ok} made, {fail} failed, of {len(missing)}")
