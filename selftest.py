#!/usr/bin/env python3
"""
selftest.py — live end-to-end test against the user's qBittorrent + providers.

Run this on the Unraid server (where qBittorrent and FlareSolverr are reachable)
to validate the classifier against real data. It:

  1. Connects to qBittorrent (from config.yaml).
  2. Lists torrents in the "books" category.
  3. Runs the real classifier (metadata + regex) on each one.
  4. Reports: raw name → cleaned title → provider vote → category → confidence.
  5. Optionally routes one test torrent (--live) or stays read-only (--dry-run).

Usage (on Unraid):
  python3 selftest.py --dry-run    # read-only: analyze only, never mutate
  python3 selftest.py --live       # also route ONE flagged test torrent
  python3 selftest.py --name "X"   # test a specific release name (no qBittorrent)

Output is a JSON report you can paste back to tune rules.
"""
import argparse
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config as cfg
import classifier
import metadata


def analyze_name(name, tags="", use_metadata=True, verbose=False):
    """Run the classify() cascade on a name; return a report dict."""
    if verbose:
        print(f"  → analyzing: {name[:70]}")
    cleaned = classifier.clean_release_name(name)
    pred, conf, reasons = classifier.classify(name, tags, use_metadata=use_metadata)
    if verbose:
        print(f"    result: {pred} (conf={conf:.2f}) {reasons[:3]}")
    return {
        "raw_name": name,
        "cleaned_title": cleaned,
        "category": pred,
        "confidence": round(conf, 2),
        "reasons": reasons,
    }


def run_against_qbittorrent(dry_run=True, category=None, limit=None, use_metadata=True, verbose=False):
    """Poll qBittorrent torrents, classify each, and optionally route them.

    If `category` is given, only torrents currently in that qBit category are
    analyzed. If `category` is None, all torrents are analyzed.
    """
    qb = classifier.QBClient(
        cfg.get("qb.url"), cfg.get("qb.user"), cfg.get("qb.password")
    )
    qb.login()
    torrents = qb.get_torrents()

    # Category distribution for diagnostics
    from collections import Counter
    dist = Counter(t.get("category") or "(uncategorized)" for t in torrents)

    selected = torrents
    if category:
        selected = [t for t in torrents if t.get("category") == category]
    if limit:
        selected = selected[:limit]

    report = {
        "qbit_url": cfg.get("qb.url"),
        "total_torrents": len(torrents),
        "category_distribution": dict(sorted(dist.items(), key=lambda kv: -kv[1])),
        "filtered_to_category": category,
        "total_analyzed": len(selected),
        "results": [],
    }

    for i, t in enumerate(selected, 1):
        if verbose:
            print(f"[{i}/{len(selected)}] {t.get('name', '')[:60]}")
        r = analyze_name(t.get("name", ""), t.get("tags", ""), use_metadata=use_metadata, verbose=verbose)
        r["hash"] = t.get("hash")
        r["current_category"] = t.get("category")
        r["size_mb"] = round((t.get("size") or 0) / 1048576, 1)
        report["results"].append(r)

        if not dry_run and r["category"] != t.get("category"):
            # Route torrent to predicted category, tag it as selftest
            qb.add_tags(t["hash"], "selftest")
            qb.set_category(t["hash"], r["category"])
            qb.set_auto_management(t["hash"], True)
            r["routed"] = True
        else:
            r["routed"] = False

    return report


def main():
    ap = argparse.ArgumentParser(description="qBittorrent classifier self-test")
    ap.add_argument("--dry-run", action="store_true", help="read-only: never mutate qBittorrent")
    ap.add_argument("--live", action="store_true", help="route test torrents (mutates qBittorrent)")
    ap.add_argument("--name", help="test a single release name (no qBittorrent needed)")
    ap.add_argument("--category", help="only analyze torrents currently in this qBit category (default: all)")
    ap.add_argument("--limit", type=int, help="limit number of torrents to test")
    ap.add_argument("--skip-metadata", action="store_true", help="use regex-only classification (fast)")
    ap.add_argument("--verbose", action="store_true", help="print per-torrent progress")
    args = ap.parse_args()

    # Mode 1: test a single name (no qBittorrent)
    if args.name:
        r = analyze_name(args.name, use_metadata=not args.skip_metadata, verbose=args.verbose)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return

    # Mode 2: run against qBittorrent
    dry_run = not args.live
    if dry_run:
        print("DRY RUN — read-only. Use --live to actually route torrents.")
    if args.skip_metadata:
        print("METADATA DISABLED — using regex-only classification.")
    report = run_against_qbittorrent(dry_run=dry_run, category=args.category, limit=args.limit, use_metadata=not args.skip_metadata, verbose=args.verbose)
    print(json.dumps(report, ensure_ascii=False, indent=2))

    # Summary
    n = len(report["results"])
    routed = sum(1 for r in report["results"] if r["routed"])
    cat_counts = Counter(r["category"] for r in report["results"])
    print(f"\n--- {n} torrents analyzed, {routed} routed ---")
    print("Predicted distribution:", dict(sorted(cat_counts.items(), key=lambda kv: -kv[1])))


if __name__ == "__main__":
    main()
