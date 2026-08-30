#!/usr/bin/env python3
"""Deliver the open-tab CVs to ~/Desktop/TCV/<slug>/Cesar Garcia CV.pdf.

Reads data/cvbatch.json (the frozen open-tab allowlist, with per-role slugs and
labels) and files each role's tuned PDF into the Desktop TCV folder, matching
the pre-Radar naming. Idempotent - skips what's already delivered - so it can be
re-run after each batch dispatch until all 53 have landed.

Run after the cloud batch: `git pull` first so cvs/<id>/ is present locally.
"""
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEST = Path.home() / "Desktop" / "TCV"
PDF = "Cesar Garcia CV.pdf"


def find_pdf(jid, cvdone):
    # 1. cloud-generated PDF sitting in the repo checkout
    p = HERE / "cvs" / jid / PDF
    if p.exists():
        return p
    # 2. whatever cvdone recorded - a repo-relative web path or, for the
    #    pre-Radar batch, an absolute Desktop path
    rec = (cvdone.get(jid) or {}).get("path") or ""
    if rec:
        cand = Path(rec)
        if not cand.is_absolute():
            cand = HERE / rec
        if cand.exists():
            return cand
    return None


def main():
    batch = json.loads((HERE / "data" / "cvbatch.json").read_text())
    cvdone = json.loads((HERE / "data" / "cvdone.json").read_text())
    ids = batch["ids"]
    slugs = batch.get("slugs", {})
    labels = batch.get("labels", {})

    delivered = skipped = 0
    waiting = []
    for jid in ids:
        dest = DEST / (slugs.get(jid) or jid) / PDF
        if dest.exists():
            skipped += 1
            continue
        src = find_pdf(jid, cvdone)
        if not src:
            waiting.append(labels.get(jid, jid))
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        delivered += 1
        print(f"  + {dest.parent.name}")

    print(f"\ndelivered {delivered}, already present {skipped}, "
          f"not yet tuned {len(waiting)} / {len(ids)}  ->  {DEST}")
    for w in waiting:
        print("  waiting:", w)
    return 0 if not waiting else 2


if __name__ == "__main__":
    sys.exit(main())
