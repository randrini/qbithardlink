#!/usr/bin/env python3
"""
qBittorrent category bootstrap.

Creates the book/comics categories and their save paths in qBittorrent via the
WebUI API, so you don't have to click through the UI. Idempotent: existing
categories are left untouched (or updated if --update is passed).

Usage:
  bootstrap_categories.py            # create missing categories
  bootstrap_categories.py --update   # also update save paths of existing ones
  bootstrap_categories.py --dry-run  # show what would be done, don't call API
"""
from __future__ import annotations

import json
import sys
from typing import Any, Dict

# Config (config.yaml + env overrides)
import config as cfg
from qblib import QBClient

QB_URL = cfg.get("qb.url", "http://192.168.1.116:8084").rstrip("/")
QB_USER = cfg.get("qb.user", "bidalos")

#: Book/comics categories → save path (relative to the /data mount qBittorrent sees)
#: These are hardlinked into the media library by hardlink.sh.
#: Layout: one shared bind mount /data/books; torrents live under
#: /data/books/torrents/<category> and the library under /data/books/library.
BOOK_CATEGORIES: Dict[str, str] = {
    "manga": "/data/books/torrents/manga",
    "manhwa": "/data/books/torrents/manhwa",
    "manhua": "/data/books/torrents/manhua",
    "webtoon": "/data/books/torrents/webtoon",
    "comics": "/data/books/torrents/comics",
    "bd": "/data/books/torrents/bd",
    "light-novel": "/data/books/torrents/light-novel",
    "ebooks": "/data/books/torrents/ebooks",
    "mags": "/data/books/torrents/mags",
    "audiobooks": "/data/books/torrents/audiobooks",
    "artbook": "/data/books/torrents/artbook",
    "doujinshi": "/data/books/torrents/doujinshi",
    "books": "/data/books/torrents",  # fallback / needs-review
}

#: Exclusion categories → save path. These are created in qBittorrent so they
#: exist, but are NEVER hardlinked or classified (video content managed by the
#: *Arr apps, or non-book media). Keep them outside the /data/books share.
EXCLUDED_CATEGORIES: Dict[str, str] = {
    # Video / media
    "movies": "/data/books/torrents/movies",
    "moviesanime": "/data/books/torrents/movies/anime",
    "tv": "/data/books/torrents/tv",
    "tvanime": "/data/books/torrents/tv/anime",
    # *Arr-managed
    "radarr": "/data/books/torrents/movies",
    "radarranime": "/data/books/torrents/movies/anime",
    "sonarr": "/data/books/torrents/tv",
    "sonarranime": "/data/books/torrents/tv/anime",
}

#: All categories the bootstrap creates.
CATEGORIES: Dict[str, str] = {**BOOK_CATEGORIES, **EXCLUDED_CATEGORIES}


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    update = "--update" in sys.argv
    only_excluded = "--excluded" in sys.argv

    qb = QBClient(QB_URL, QB_USER)
    if not dry_run:
        qb.login()
        existing: Dict[str, Dict[str, Any]] = qb.get_categories()
    else:
        existing = {}

    # Which categories to manage
    if only_excluded:
        cats = EXCLUDED_CATEGORIES
        label = "EXCLUDED (never hardlinked/classified)"
    else:
        cats = CATEGORIES
        label = "ALL (book + excluded)"

    print(f"qBittorrent: {QB_URL}")
    print(f"Managing: {label}")
    print(f"{'CATEGORY':<14} {'SAVE PATH':<40} STATUS")
    print("-" * 72)

    for cat, path in cats.items():
        kind = "book" if cat in BOOK_CATEGORIES else "excl"
        if cat in existing:
            cur = existing[cat].get("savePath", "")
            if cur == path:
                print(f"{cat:<14} {path:<40} OK (exists) [{kind}]")
            elif update:
                if dry_run:
                    print(f"{cat:<14} {path:<40} WOULD UPDATE (was {cur}) [{kind}]")
                else:
                    qb.edit_category(cat, path)
                    print(f"{cat:<14} {path:<40} UPDATED (was {cur}) [{kind}]")
            else:
                print(
                    f"{cat:<14} {path:<40} EXISTS (path differs: {cur}) — use --update [{kind}]"
                )
        else:
            if dry_run:
                print(f"{cat:<14} {path:<40} WOULD CREATE [{kind}]")
            else:
                qb.create_category(cat, path)
                print(f"{cat:<14} {path:<40} CREATED [{kind}]")

    print("-" * 72)
    if dry_run:
        print("Dry run — no changes made.")


if __name__ == "__main__":
    main()
