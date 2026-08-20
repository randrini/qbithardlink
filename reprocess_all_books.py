#!/usr/bin/env python3
"""One-shot reclassify of all existing book/comic torrents.

Usage:
    docker exec qbit-classifier python /app/reprocess_all_books.py
    docker exec qbit-classifier python /app/reprocess_all_books.py --no-llm
    docker exec qbit-classifier python /app/reprocess_all_books.py --category=bd

The script scans every torrent currently in a book/comic category,
re-runs classify() on it, and updates the qBittorrent category if the
verdict changed. It also re-hardlinks into the new library location.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Dict, Set

import config as cfg
from classifier import QBClient, classify_with_metadata, _best_torrent_name

BOOK_CATEGORIES: Set[str] = {
    "books", "manga", "manhwa", "manhua", "webtoon", "comics", "bd",
    "light-novel", "ebooks", "mags", "audiobooks", "artbook", "doujinshi",
}


def _content_path(t: Dict, dest_cat: str) -> str | None:
    """Return the filesystem path to the torrent content inside /data/books."""
    save = (t.get("save_path") or "").rstrip("/")
    name = t.get("name", "")
    candidates = [
        t.get("content_path"),
        os.path.join(save, name) if save and name else None,
        save if save and os.path.basename(save) == name else None,
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def main() -> None:
    no_llm = "--no-llm" in sys.argv
    dry_run = "--dry-run" in sys.argv
    only_category = None
    llm_delay = float(cfg.get("llm.delay_seconds", 5.0))  # default from config
    for arg in sys.argv[1:]:
        if arg.startswith("--category="):
            only_category = arg.split("=", 1)[1]
        if arg.startswith("--delay="):
            try:
                llm_delay = float(arg.split("=", 1)[1])
            except ValueError:
                pass

    if no_llm:
        # Disable LLM for this run by overriding config in memory.
        cfg.CONFIG.setdefault("llm", {})["enabled"] = False
        print("LLM disabled for this run (using metadata + regex only).")
    else:
        # Check if LLM is actually enabled in config
        llm_enabled = bool(cfg.get("llm.enabled", False))
        if llm_enabled:
            print(f"LLM enabled (mode={cfg.get('llm.mode', 'fallback')}, delay={llm_delay}s between calls).")
    if dry_run:
        print("DRY RUN: no categories or hardlinks will be changed.")

    use_metadata = bool(cfg.get("metadata.enabled", True))
    hardlink_enabled = bool(cfg.get("hardlink.enabled", True))
    hardlink_script = cfg.get("hardlink.script", "/app/hardlink.sh")
    source_category = cfg.get("qb.source_category", "books")

    qb = QBClient()
    qb.login()

    torrents = qb.get_torrents()
    to_reprocess = [t for t in torrents if (t.get("category") or "") in BOOK_CATEGORIES]
    if only_category:
        to_reprocess = [t for t in to_reprocess if t.get("category") == only_category]

    print(f"Reprocessing {len(to_reprocess)} book/comic torrents...", flush=True)
    changed = 0
    for idx, t in enumerate(to_reprocess, 1):
        h = t["hash"]
        name = t.get("name", "")
        files = qb.get_torrent_files(h)
        classify_name = _best_torrent_name(name, files)
        print(f"[{idx}/{len(to_reprocess)}] {name[:70]:70s} ...", flush=True)
        cat, conf, reasons, metadata = classify_with_metadata(classify_name, t.get("tags", ""), files=files, use_metadata=use_metadata)
        if cat == "skip":
            print("    → skip (video/non-book)", flush=True)
            continue

        old_cat = t.get("category", "")
        if cat == old_cat:
            print(f"    → {cat} (unchanged)", flush=True)
        else:
            print(f"    → {cat} (was {old_cat}, conf={conf:.2f})", flush=True)

        # Always show why the category was chosen.
        why = "; ".join(str(r) for r in reasons)
        if why:
            print(f"      why: {why}", flush=True)

        # Show who/where metadata is available.
        if metadata:
            parts = []
            if metadata.get("authors"):
                authors = ", ".join(str(a) for a in metadata["authors"][:3])
                parts.append(f"{authors} (author)")
            if metadata.get("artist"):
                parts.append(f"{metadata['artist']} (artist)")
            if metadata.get("publisher"):
                parts.append(f"{metadata['publisher']} (publisher)")
            if metadata.get("year"):
                parts.append(f"{metadata['year']} (year)")
            if metadata.get("country"):
                parts.append(f"{metadata['country']} (country)")
            if parts:
                print(f"      by: {', '.join(parts)}", flush=True)

        if cat == old_cat:
            continue

        if dry_run:
            changed += 1
            print("    [dry-run] would re-hardlink and change category", flush=True)
            continue

        # Re-hardlink BEFORE changing category, same as daemon.
        content_path = _content_path(t, cat)
        if hardlink_enabled and content_path:
            try:
                result = subprocess.run(
                    [hardlink_script, name, content_path, cat],
                    check=False,
                    timeout=300,
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    print(f"    hardlink warn: rc={result.returncode} {result.stderr.strip()[:200]}")
            except Exception as e:
                print(f"    hardlink error: {e}")

        # Move back to source category then to new category so qBittorrent
        # relocates the files to the new save path.
        if old_cat != source_category:
            qb.set_category(h, source_category)
        qb.set_category(h, cat)
        changed += 1
        # Throttle: sleep between LLM calls to avoid 429 rate limits,
        # plus a small delay for qBittorrent API.
        if not no_llm:
            time.sleep(llm_delay)
        else:
            time.sleep(0.5)

    print(f"Done. {'Would have re-categorized' if dry_run else 'Re-categorized'} {changed} torrents.", flush=True)


if __name__ == "__main__":
    main()
