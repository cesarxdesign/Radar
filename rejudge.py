#!/usr/bin/env python3
"""Re-run the judge over verdicts reached under an older PROMPT_V.

The judge's criteria change; its verdicts persist. Without this, a prompt fix
only ever reaches roles scraped after it, and everything already in Open keeps
the old reading forever.

Resumable by construction: every verdict is written to judged.json the moment
it lands and stamped with the prompt version that produced it, so a kill, a
crash, or a Ctrl-C costs the one role in flight and nothing else. Run it again
and it picks up exactly where it stopped.

  python3 rejudge.py --dry-run          what would be re-judged, and why
  python3 rejudge.py --open             every active role currently in Open
  python3 rejudge.py --at-risk          only the ones the old rule likely got wrong
  python3 rejudge.py --open --limit 10  a taste first
"""
import argparse
import json
import re
import sys
import time

from nightjudge import (JD_STUB_CHARS, JOBS_FILE, JDS_FILE, JUDGED_FILE, PROMPT,
                        PROMPT_V, ROOT, ask, decided, jd_window, preflight,
                        verdict_to_bucket)

STATE_FILE = ROOT / "data" / "state.json"

OPEN_BUCKETS = ("pt", "eu", "ww")
PT_RE = re.compile(r"portugal|lisbon|lisboa|porto\b", re.I)
# A location that already carries its own remote/Europe scope was never the
# failure mode - "Madrid, Spain" with nothing else was.
SCOPED_RE = re.compile(
    r"\b(remote|europe|european|emea|eea|worldwide|anywhere|global)\b", re.I)


def cohort(jobs, judged, mode, off_limits=frozenset()):
    out = []
    for j in jobs:
        if not j.get("active"):
            continue
        if j["id"] in off_limits:
            continue  # his call, not ours to revisit
        v = judged.get(j["id"])
        if not v or v.get("pv") == PROMPT_V:
            continue  # unjudged (the nightly picks it up) or already current
        if v.get("bucket") not in OPEN_BUCKETS:
            continue  # cut/unsure roles were not the complaint
        if mode == "at-risk":
            loc = (j.get("location") or "").strip()
            if not loc or PT_RE.search(loc) or SCOPED_RE.search(loc):
                continue
        out.append(j)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--open", action="store_true", help="every active Open role")
    ap.add_argument("--at-risk", action="store_true",
                    help="only Open roles whose location names a bare place")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    mode = "at-risk" if a.at_risk else "open"

    data = json.loads(JOBS_FILE.read_text())
    jds = json.loads(JDS_FILE.read_text()).get("jobs", {})
    judged = json.loads(JUDGED_FILE.read_text())

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    off = decided(state)
    todo = cohort(data["jobs"], judged["jobs"], mode, off)
    skipped = len(cohort(data["jobs"], judged["jobs"], mode)) - len(todo)
    if a.limit:
        todo = todo[:a.limit]
    print(f"re-judging {len(todo)} role(s) [{mode}] against prompt v{PROMPT_V}")
    print(f"leaving {skipped} alone - already applied / discarded / OK'd by you\n")
    if a.dry_run:
        for j in todo:
            v = judged["jobs"][j["id"]]
            print(f"  [{v['bucket']}] {j['company'][:24]:24s} | "
                  f"{j['title'][:44]:44s} | {(j.get('location') or '')[:40]}")
        return 0

    if not todo:
        print("nothing to do.")
        return 0
    if not preflight():
        return 1

    moved, same, failed = [], 0, 0
    t0 = time.monotonic()
    for n, j in enumerate(todo, 1):
        was = judged["jobs"][j["id"]]["bucket"]
        jd = jd_window(jds.get(j["id"], {}).get("jd") or "")
        try:
            v = ask(PROMPT.format(
                title=j["title"], company=j["company"],
                location=j.get("location") or "(none)",
                jd=jd or "(no description available - judge on title and location only)"))
        except Exception as e:
            print(f"  {n}/{len(todo)} FAILED {j['company']} - {j['title']}: {e}")
            failed += 1
            continue
        out = verdict_to_bucket(v, j.get("location") or "",
                                 stub=len(jd) < JD_STUB_CHARS)
        if not out:
            print(f"  {n}/{len(todo)} FAILED {j['company']} - {j['title']}")
            failed += 1
            if failed >= 3 and not moved and same == 0:
                print("\nfirst 3 roles all failed - stopping rather than "
                      "grinding through the rest. Nothing was written.")
                return 1
            continue
        rec = {"bucket": out["bucket"], "why": out["why"],
               "at": judged["jobs"][j["id"]].get("at", ""), "pv": PROMPT_V}
        for k in ("work", "scope"):
            if out.get(k):
                rec[k] = out[k]
        judged["jobs"][j["id"]] = rec
        # persist per item: a kill here costs this role, never the batch
        JUDGED_FILE.write_text(json.dumps(judged, indent=1, ensure_ascii=False))

        now = rec["bucket"]
        if now == was:
            same += 1
            flag = "   ="
        else:
            moved.append((j, was, now, rec))
            flag = f"{was:>4s}→{now}"
        rate = (time.monotonic() - t0) / n
        print(f"  {n}/{len(todo)} {flag}  {j['company'][:22]:22s} | "
              f"{j['title'][:38]:38s} | {rec.get('work','?'):7s} | "
              f"{rec.get('scope') or rec['why'] or ''}"[:150]
              + f"   [kept {same}, moved {len(moved)}, failed {failed}, ~{rate:.0f}s/role]")

    # patch jobs.json exactly the way nightjudge does, so the dashboard is live
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

    print(f"\nkept {same} | moved {len(moved)} | failed {failed} | "
          f"active now {data['counts']['active']}")
    for j, was, now, rec in moved:
        print(f"  {was}→{now:8s} {j['company'][:22]:22s} | {j['title'][:40]:40s} | "
              f"{rec.get('work','?')} | {rec['why'] or rec.get('scope','')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
