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
import json
import os
import sys
import urllib.parse
import urllib.request

# Config (config.yaml + env overrides)
import config as cfg

QB_URL = cfg.get("qb.url", "http://192.168.1.116:8084").rstrip("/")
QB_USER = cfg.get("qb.user", "bidalos")
QB_PASS = cfg.get("qb.password", "your-password")

#: Book/comics categories → save path (relative to the /data mount qBittorrent sees)
#: These are hardlinked into the media library by hardlink.sh.
#: Layout: one shared bind mount /data/books; torrents live under
#: /data/books/torrents/<category> and the library under /data/books/library.
BOOK_CATEGORIES = {
    "manga": "/data/books/torrents/manga",
    "manhwa": "/data/books/torrents/manhwa",
    "webtoon": "/data/books/torrents/webtoon",
    "comics": "/data/books/torrents/comics",
    "bd": "/data/books/torrents/bd",
    "light-novel": "/data/books/torrents/light-novel",
    "ebooks": "/data/books/torrents/ebooks",
    "books": "/data/books/torrents",  # fallback / needs-review
}

#: Exclusion categories → save path. These are created in qBittorrent so they
#: exist, but are NEVER hardlinked or classified (video content managed by the
#: *Arr apps, or non-book media). Keep them outside the /data/books share.
EXCLUDED_CATEGORIES = {
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
CATEGORIES = {**BOOK_CATEGORIES, **EXCLUDED_CATEGORIES}


class QBClient:
    def __init__(self, url, user, password):
        self.url = url
        self.user = user
        self.password = password
        self.cookies = {}

    def _request(self, path, data=None):
        url = f"{self.url}/api/v2/{path}"
        body = urllib.parse.urlencode(data).encode() if data else None
        req = urllib.request.Request(url, data=body)
        if self.cookies:
            req.add_header("Cookie", "; ".join(f"{k}={v}" for k, v in self.cookies.items()))
        with urllib.request.urlopen(req) as resp:
            return resp.read()

    def login(self):
        self._request("auth/login", {"username": self.user, "password": self.password})

    def get_categories(self):
        return json.loads(self._request("torrents/categories"))

    def create_category(self, category, save_path):
        self._request("torrents/createCategory", {"category": category, "savePath": save_path})

    def edit_category(self, category, save_path):
        self._request("torrents/editCategory", {"category": category, "savePath": save_path})


def main():
    dry_run = "--dry-run" in sys.argv
    update = "--update" in sys.argv
    only_excluded = "--excluded" in sys.argv

    qb = QBClient(QB_URL, QB_USER, QB_PASS)
    if not dry_run:
        qb.login()
        existing = qb.get_categories()
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
                print(f"{cat:<14} {path:<40} EXISTS (path differs: {cur}) — use --update [{kind}]")
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
