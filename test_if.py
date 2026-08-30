#!/usr/bin/env python3
"""IngestionFilter regression gate. César's rule (2026-08-30): every role he
has actually applied to - from the Gmail dump AND from radar's applied set -
MUST survive the IngestionFilter (all the passes that decide what reaches the
Radar). This must always be 100%. Run before shipping any filter change.

The IF = pass-1 title net (title_ok) + location classify() must not drop +
JD-language must be English/Portuguese. The LLM judge is downstream and not
tested here (it re-reads and can be re-run); this gate protects the cheap,
sticky-by-omission passes that would silently lose a real role.
"""
import json
import sys
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("scrape", ROOT / "scrape.py")
s = importlib.util.module_from_spec(spec)
sys.modules["scrape"] = s
spec.loader.exec_module(s)

VOLUNTARY = {"open to opportunities"}  # hirehire talent-pool, not a real role


def load_ground_truth():
    ae = json.loads((ROOT / "data" / "applied_email.json").read_text())
    jobs = {j["id"]: j for j in json.loads((ROOT / "data" / "jobs.json").read_text())["jobs"]}
    state = json.loads((ROOT / "data" / "state.json").read_text())["jobs"]

    titles = {}  # title -> location/remote/jd context (None where unknown)
    for a in ae.get("applications", []):
        t = (a.get("title") or "").strip()
        if t and t.lower() not in VOLUNTARY:
            titles.setdefault(t, {"loc": None, "remote": None, "jd": None, "src": "email"})
    jds = json.loads((ROOT / "data" / "jds.json").read_text()).get("jobs", {})
    for i, v in state.items():
        if v == "applied" and i in jobs:
            j = jobs[i]
            t = (j["title"] or "").strip()
            if t.lower() in VOLUNTARY:
                continue
            titles[t] = {"loc": j.get("location"), "remote": j.get("remote"),
                         "jd": jds.get(i, {}).get("jd"), "src": "radar"}
    return titles


def check(title, ctx):
    fails = []
    if not s.title_ok(title):
        fails.append("pass-1 title net")
    if ctx["loc"] is not None:  # only radar rows carry a location
        if s.classify(ctx["loc"], ctx["remote"]) is None:
            fails.append(f"location classify dropped ({ctx['loc']!r})")
    if ctx["jd"] and not s.jd_language_ok(ctx["jd"]):
        fails.append("JD language")
    return fails


def main():
    gt = load_ground_truth()
    failures = [(t, check(t, c)) for t, c in gt.items()]
    failures = [(t, f) for t, f in failures if f]
    n = len(gt)
    print(f"IngestionFilter gate: {n - len(failures)}/{n} applied roles survive")
    for t, f in failures:
        print(f"  ✗ {t}  ->  {', '.join(f)}")
    if failures:
        print("FAIL: an applied role would be dropped by the IngestionFilter.")
        return 1
    print("PASS: 100% - every applied role reaches the Radar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
