#!/usr/bin/env python3
"""One-shot reclassify of all existing book/comic torrents.

Usage:
    docker exec qbit-classifier python /app/reprocess_all_books.py

The script scans every torrent currently in a book/comic category,
re-runs classify() on it, and updates the qBittorrent category if the
verdict changed. It also re-hardlinks into the new library location.
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Dict, List

import config as cfg
from classifier import QBClient, classify, _best_torrent_name

BOOK_CATEGORIES = {
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
    use_metadata = bool(cfg.get("metadata.enabled", True))
    hardlink_enabled = bool(cfg.get("hardlink.enabled", True))
    hardlink_script = cfg.get("hardlink.script", "/app/hardlink.sh")
    source_category = cfg.get("qb.source_category", "books")

    qb = QBClient()
    qb.login()

    torrents = qb.get_torrents()
    to_reprocess = [t for t in torrents if (t.get("category") or "") in BOOK_CATEGORIES]

    print(f"Reprocessing {len(to_reprocess)} book/comic torrents...")
    changed = 0
    for t in to_reprocess:
        h = t["hash"]
        name = t.get("name", "")
        files = qb.get_torrent_files(h)
        classify_name = _best_torrent_name(name, files)
        cat, conf, reasons = classify(classify_name, t.get("tags", ""), files=files, use_metadata=use_metadata)
        if cat == "skip":
            continue

        old_cat = t.get("category", "")
        if cat == old_cat:
            print(f"  {name[:70]:70s} → {cat} (unchanged)")
            continue

        print(f"  {name[:70]:70s} → {cat} (was {old_cat}, conf={conf:.2f}) {reasons}")

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

    print(f"Done. Re-categorized {changed} torrents.")


if __name__ == "__main__":
    main()
