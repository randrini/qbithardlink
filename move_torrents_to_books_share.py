#!/usr/bin/env python3
"""Bulk-update qBittorrent torrent save paths to the new /data/books layout.

Usage:
    docker exec -it qbit-classifier python /app/move_torrents_to_books_share.py --dry-run
    docker exec -it qbit-classifier python /app/move_torrents_to_books_share.py

The script reads every torrent from qBittorrent, computes its new save path
under /data/books/torrents/<category>/, and calls setLocation for each.
Torrents in excluded video categories (movies, tv, *arr) are ignored.
"""
import json
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, "/app")
import config as cfg

QB_URL = cfg.get("qb.url", "http://192.168.1.116:8084").rstrip("/")
QB_USER = cfg.get("qb.user", "bidalos")
QB_PASS = cfg.get("qb.password", "")

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


class QBClient:
    def __init__(self, url, user, password):
        self.url = url
        self.user = user
        self.password = password
        self.session = None
        try:
            import requests
            self.session = requests.Session()
        except Exception:
            self.session = None

    def login(self):
        if self.session:
            r = self.session.post(f"{self.url}/api/v2/auth/login", data={"username": self.user, "password": self.password})
            if not r.ok:
                raise RuntimeError(f"login failed: HTTP {r.status_code}")
        else:
            data = urllib.parse.urlencode({"username": self.user, "password": self.password}).encode()
            req = urllib.request.Request(f"{self.url}/api/v2/auth/login", data=data)
            with urllib.request.urlopen(req) as resp:
                self.cookie = resp.headers.get("Set-Cookie")

    def get_torrents(self):
        if self.session:
            r = self.session.get(f"{self.url}/api/v2/torrents/info")
            r.raise_for_status()
            return r.json()
        req = urllib.request.Request(f"{self.url}/api/v2/torrents/info")
        if getattr(self, "cookie", None):
            req.add_header("Cookie", self.cookie)
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())

    def set_location(self, hashes, location):
        if isinstance(hashes, list):
            hashes = "|".join(hashes)
        data = {"hashes": hashes, "location": location}
        if self.session:
            r = self.session.post(f"{self.url}/api/v2/torrents/setLocation", data=data)
            r.raise_for_status()
        else:
            body = urllib.parse.urlencode(data).encode()
            req = urllib.request.Request(f"{self.url}/api/v2/torrents/setLocation", data=body)
            if getattr(self, "cookie", None):
                req.add_header("Cookie", self.cookie)
            with urllib.request.urlopen(req) as resp:
                resp.read()


def new_path_for(category):
    cat = (category or "").strip().lower()
    if cat in EXCLUDED:
        return None
    if cat in BOOK_CATS:
        return f"{NEW_BASE}/{cat}"
    return f"{NEW_BASE}/_unknown"


def main():
    dry_run = "--dry-run" in sys.argv

    qb = QBClient(QB_URL, QB_USER, QB_PASS)
    qb.login()
    torrents = qb.get_torrents()

    print(f"qBittorrent: {QB_URL}")
    print(f"Torrents:   {len(torrents)}")
    print(f"Mode:       {'dry-run' if dry_run else 'LIVE'}")
    print("-" * 70)

    planned = []
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

    # Move in batches of 10 hashes to avoid overly long API calls.
    batch = []
    current_loc = None
    for h, name, old, new, cat in planned:
        if current_loc and current_loc != new:
            qb.set_location([x[0] for x in batch], current_loc)
            print(f"Moved {len(batch)} torrents → {current_loc}")
            batch = []
        current_loc = new
        batch.append((h, name, old, new, cat))
    if batch and current_loc:
        qb.set_location([x[0] for x in batch], current_loc)
        print(f"Moved {len(batch)} torrents → {current_loc}")

    print("Done. qBittorrent will move the files on disk; if the old and new")
    print("paths are on the same filesystem this will be near-instant.")


if __name__ == "__main__":
    main()
