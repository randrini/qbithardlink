#!/usr/bin/env python3
"""Bulk-update qBittorrent torrent save paths to the new /data/books layout.

Usage:
    docker exec -it qbit-classifier python /app/move_torrents_to_books_share.py --dry-run
    docker exec -it qbit-classifier python /app/move_torrents_to_books_share.py

The script reads every torrent from qBittorrent, computes its new save path
under /data/books/torrents/<category>/, and calls setLocation for each.
Torrents in excluded video categories (movies, tv, *arr) are ignored.
"""
from __future__ import annotations

import sys
from typing import Dict, List, Tuple

import config as cfg
from qblib import QBClient

NEW_BASE = "/data/books/torrents"

# Categories that are part of the books share layout.
BOOK_CATS = {
    "books", "manga", "manhwa", "webtoon", "comics", "bd", "light-novel", "ebooks"
}

# Categories that should stay outside the books share (video / *Arr).
EXCLUDED = {
    "movies", "moviesanime", "tv", "tvanime",
    "radarr", "radarranime", "sonarr", "sonarranime", "lidarr", "readarr",
}


def new_path_for(category: str) -> str | None:
    cat = (category or "").strip().lower()
    if cat in EXCLUDED:
        return None
    if cat in BOOK_CATS:
        return f"{NEW_BASE}/{cat}"
    return f"{NEW_BASE}/_unknown"


def main() -> None:
    dry_run = "--dry-run" in sys.argv

    qb = QBClient()
    qb.login()
    torrents = qb.get_torrents()

    print(f"qBittorrent: {qb.url}")
    print(f"Torrents:   {len(torrents)}")
    print(f"Mode:       {'dry-run' if dry_run else 'LIVE'}")
    print("-" * 70)

    planned: List[Tuple[str, str, str, str, str]] = []
    for t in torrents:
        cat = t.get("category", "") or ""
        new_loc = new_path_for(cat)
        if new_loc is None:
            continue
        old_loc = (t.get("save_path") or "").rstrip("/")
        if old_loc == new_loc:
            continue
        planned.append((t["hash"], t.get("name", "")[:60], old_loc, new_loc, cat))

    if not planned:
        print("No torrents need moving.")
        return

    print(f"Torrents to move: {len(planned)}")
    print(f"{'HASH':<45} {'CATEGORY':<12} {'OLD PATH':<35} {'NEW PATH'}")
    for h, name, old, new, cat in planned:
        print(f"{h:<45} {cat:<12} {old:<35} {new}")
    print("-" * 70)

    if dry_run:
        print("Dry run — no changes made. Pass without --dry-run to execute.")
        return

    # Move in batches grouped by destination location.
    batch_by_loc: Dict[str, List[str]] = {}
    for h, name, old, new, cat in planned:
        batch_by_loc.setdefault(new, []).append(h)

    for location, hashes in batch_by_loc.items():
        qb.set_location(hashes, location)
        print(f"Moved {len(hashes)} torrents → {location}")

    print("Done. qBittorrent will move the files on disk; if the old and new")
    print("paths are on the same filesystem this will be near-instant.")


if __name__ == "__main__":
    main()
