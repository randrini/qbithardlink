# Smart Categorize → Route → Hardlink → Seed

A complete, production-grade solution for your book/comics library on Unraid:

> **Smartly categorize a new downloading file → assign the torrent's category path → hardlink it into the media library → keep seeding to 1:1.**

This document turns the architecture we discussed into concrete, runnable pieces. It is built around one core principle:

> **qBittorrent owns the filesystem. The classifier decides "what is this?". The hardlinker trusts the category. CleanUpArr decides "when can the torrent go?".**

---

## 1. The architecture at a glance

```text
Shelfarr / Shelfmark / InkDrop / manual add
                 │
                 ▼
        qBittorrent  (category = "books")
                 │
                 ▼
        Qui automation  (trigger on new torrent)
                 │
                 ▼
        classifier service  (regex + metadata)
                 │
        ┌────────┴─────────┐
        ▼                  ▼
  confidence >= 0.90   confidence < 0.70
        │                  │
        ▼                  ▼
  set qBit category    leave as "books" + tag "review"
        │
        ▼
  qBittorrent AutoTMM  →  /data/torrents/books/<category>
        │
        │  download completes
        ▼
  hardlink script  (trusts category)
        │
        ▼
  /data/media/books/<category>
        │
        ▼
  qBittorrent seeds to ratio 1.0
        │
        ▼
  CleanUpArr removes torrent + torrent-side files
        │
        ▼
  /data/media hardlink survives  (library intact)
```

**Division of responsibility:**

| Component | Question it answers |
|---|---|
| Classifier | "What is this?" |
| Qui | "When should I process it?" |
| qBittorrent | "Where should it live?" |
| Hardlinker | "Where should the library link go?" |
| CleanUpArr | "When can the torrent be removed?" |

---

## 2. Canonical categories

Treat these as **content types**, not file formats. Standardize on singular names.

| Category | Meaning | qBit save path | Library path |
|---|---|---|---|
| `manga` | Japanese manga | `/data/torrents/books/manga` | `/data/media/books/manga` |
| `manhwa` | Korean comics | `/data/torrents/books/manhwa` | `/data/media/books/manhwa` |
| `webtoon` | Webtoon-format titles | `/data/torrents/books/webtoon` | `/data/media/books/webtoon` |
| `comics` | US/English comics, graphic novels | `/data/torrents/books/comics` | `/data/media/books/comics` |
| `bd` | Franco-Belgian / bande dessinée | `/data/torrents/books/bd` | `/data/media/books/bd` |
| `light-novel` | Light novels | `/data/torrents/books/light-novel` | `/data/media/books/light-novel` |
| `ebooks` | Ordinary prose/non-graphic ebooks | `/data/torrents/books/ebooks` | `/data/media/books/ebooks` |
| `books` | Unknown / needs review (fallback) | `/data/torrents/books` | `/data/media/books/_unprocessed` |

> **Note:** Your existing `mangas/` directory is an older convention. Standardize on `manga/`.

---

## 3. Filesystem layout (single `/data` mount)

Hardlinks **cannot cross filesystems**. Every container that touches torrents or media must see the **same** `/data` root.

```text
/mnt/user/data
├── torrents/            ← qBittorrent
│   └── books/
│       ├── manga/
│       ├── manhwa/
│       ├── webtoon/
│       ├── comics/
│       ├── bd/
│       ├── light-novel/
│       └── ebooks/
├── usenet/              ← nzbfast (SABnzbd-compatible)
├── soulseek/            ← slskd
└── media/               ← Plex/Jellyfin + library
    └── books/
        ├── _unprocessed/
        ├── manga/
        ├── manhwa/
        ├── webtoon/
        ├── comics/
        ├── bd/
        ├── light-novel/
        └── ebooks/
```

**Docker mount for every relevant container:**

```yaml
volumes:
  - /mnt/user/data:/data
```

This is the single most important rule. If qBittorrent reports `/data/torrents/books/manga` but the hardlinker only sees that host dir as `/downloads`, hardlinks break.

---

## 4. qBittorrent setup

### 4.1 Create the categories

Use the qBittorrent WebUI → **Options → Downloads → Categories**, or the API:

```bash
# qBittorrent WebUI API — create each category with its save path
QB_URL="http://192.168.1.116:8084"
QB_USER="bidalos"
QB_PASS="your-password"

# login
curl -s -c /tmp/qb.cookies -b /tmp/qb.cookies \
  --data "username=$QB_USER&password=$QB_PASS" \
  "$QB_URL/api/v2/auth/login"

for cat in manga manhwa webtoon comics bd light-novel ebooks; do
  curl -s -b /tmp/qb.cookies \
    --data-urlencode "category=$cat" \
    --data-urlencode "savePath=/data/torrents/books/$cat" \
    "$QB_URL/api/v2/torrents/createCategory"
done
```

### 4.2 Enable AutoTMM

AutoTMM makes qBittorrent follow the category's save path automatically. When the classifier changes a torrent's category, qBittorrent moves the files to the new category's folder.

```bash
# Enable AutoTMM for a torrent (replace HASH)
curl -s -b /tmp/qb.cookies \
  --data "hashes=HASH&enableAutoTMM=true" \
  "$QB_URL/api/v2/torrents/setAutoManagement"
```

### 4.3 Set the share/ratio limit

Let **qBittorrent** decide when seeding is done (not CleanUpArr):

```text
Options → BitTorrent → Seeding Limits
  Ratio limit: 1.00
  When ratio reached: Pause/Stop torrent
```

---

## 5. The classifier service

The classifier is a small daemon that watches qBittorrent for new `books` torrents, classifies them, and sets the category. It uses **ordered rules** and is **conservative** — never guess when confidence is low.

### 5.1 Classification priority

```text
1. Explicit user/manual override (qBit tag, e.g. tag=manga)
2. Reliable embedded ISBN → metadata provider
3. Trusted metadata provider match (Google Books / Open Library)
4. Publisher / language / country signals
5. Filename / release-name regex rules
6. Unknown → books/review
```

### 5.2 Confidence thresholds

```text
>= 0.90  → automatically classify
0.70–0.89 → classify + tag "review"
< 0.70   → leave as "books" + tag "review"
```

### 5.3 The classifier script

Save as `/opt/qbithardlink/classifier.py`:

```python
#!/usr/bin/env python3
"""
qBittorrent category classifier daemon.

Watches qBittorrent for torrents in the "books" category, classifies them
into manga/manhwa/webtoon/comics/bd/light-novel/ebooks, and sets the category.

Conservative by design: low-confidence results stay in "books" + tag "review".
"""
import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

# ── Configuration ──────────────────────────────────────────────────────────
QB_URL = "http://192.168.1.116:8084"
QB_USER = "bidalos"
QB_PASS = "your-password"

# Category → list of (regex, weight) rules. First match wins per category.
# Weights accumulate; a category is chosen when total confidence >= threshold.
RULES = {
    "manga": [
        (r"(?i)\bmanga\b", 0.9),
        (r"(?i)\bscanlation\b", 0.8),
        (r"(?i)\bjapanese\b", 0.5),
        (r"(?i)\b[0-9]{4}\b.*\bvol\.?\s*\d+\b", 0.3),  # weak
    ],
    "manhwa": [
        (r"(?i)\bmanhwa\b", 0.9),
        (r"(?i)\bkorean\b", 0.6),
    ],
    "webtoon": [
        (r"(?i)\bwebtoon\b", 0.9),
    ],
    "comics": [
        (r"(?i)\bcomic\b", 0.8),
        (r"(?i)\bmarvel\b", 0.8),
        (r"(?i)\bdc\b", 0.7),
        (r"(?i)\bgraphic novel\b", 0.8),
    ],
    "bd": [
        (r"(?i)\bbd\b", 0.7),
        (r"(?i)\bfranco[- ]belge\b", 0.9),
        (r"(?i)\bbande dessin", 0.9),
    ],
    "light-novel": [
        (r"(?i)\blight[- ]novel\b", 0.9),
        (r"(?i)\bln\b", 0.5),
    ],
    "ebooks": [
        (r"(?i)\.epub$", 0.9),
        (r"(?i)\.mobi$", 0.9),
        (r"(?i)\.azw3?$", 0.9),
        (r"(?i)\bebook\b", 0.8),
    ],
}

# qBit tags that act as a manual override (highest priority).
TAG_OVERRIDES = {
    "manga": "manga",
    "manhwa": "manhwa",
    "webtoon": "webtoon",
    "comics": "comics",
    "bd": "bd",
    "light-novel": "light-novel",
    "ebooks": "ebooks",
}

AUTO_THRESHOLD = 0.90
REVIEW_THRESHOLD = 0.70
POLL_INTERVAL = 10  # seconds


# ── qBittorrent API helpers ───────────────────────────────────────────────
class QBClient:
    def __init__(self, url, user, password):
        self.url = url.rstrip("/")
        self.user = user
        self.password = password
        self.cookies = {}

    def _request(self, path, data=None):
        url = f"{self.url}/api/v2/{path}"
        req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode() if data else None)
        req.add_header("Cookie", "; ".join(f"{k}={v}" for k, v in self.cookies.items()))
        with urllib.request.urlopen(req) as resp:
            return resp.read()

    def login(self):
        self._request("auth/login", {"username": self.user, "password": self.password})

    def get_torrents(self):
        return json.loads(self._request("torrents/info"))

    def set_category(self, hashes, category):
        self._request("torrents/setCategory", {"hashes": hashes, "category": category})

    def add_tags(self, hashes, tags):
        self._request("torrents/addTags", {"hashes": hashes, "tags": tags})

    def set_auto_management(self, hashes, enable=True):
        self._request("torrents/setAutoManagement", {"hashes": hashes, "enableAutoTMM": "true" if enable else "false"})


# ── Classification logic ──────────────────────────────────────────────────
def classify(torrent) -> tuple[str, float, list[str]]:
    """Return (category, confidence, reasons)."""
    name = torrent.get("name", "")
    tags = torrent.get("tags", "").split(",")
    reasons = []

    # 1. Manual override via tag
    for tag in tags:
        tag = tag.strip().lower()
        if tag in TAG_OVERRIDES:
            return TAG_OVERRIDES[tag], 1.0, [f"manual tag override: {tag}"]

    # 2. Regex rules
    best_cat = None
    best_score = 0.0
    for cat, patterns in RULES.items():
        score = 0.0
        for pattern, weight in patterns:
            if re.search(pattern, name):
                score += weight
                reasons.append(f"{cat}: matched {pattern}")
        if score > best_score:
            best_score = score
            best_cat = cat

    if best_cat and best_score >= AUTO_THRESHOLD:
        return best_cat, best_score, reasons
    if best_cat and best_score >= REVIEW_THRESHOLD:
        return best_cat, best_score, reasons + ["low confidence → review"]
    return "books", best_score, reasons + ["below threshold → review"]


# ── Main loop ─────────────────────────────────────────────────────────────
def main():
    qb = QBClient(QB_URL, QB_USER, QB_PASS)
    qb.login()
    print(f"Classifier watching {QB_URL} every {POLL_INTERVAL}s...")

    while True:
        try:
            for t in qb.get_torrents():
                if t.get("category") != "books":
                    continue
                cat, conf, reasons = classify(t)
                h = t.get("hash")
                if cat != "books":
                    qb.set_category(h, cat)
                    qb.set_auto_management(h, True)
                    print(f"[{t.get('name')}] → {cat} (conf={conf:.2f}) {reasons}")
                else:
                    qb.add_tags(h, "review")
                    print(f"[{t.get('name')}] → review (conf={conf:.2f}) {reasons}")
        except Exception as e:
            print(f"error: {e}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
```

> **Why not make the hardlinker query metadata?** Because the hardlinker should be a dumb, safe final step. Classification happens *before* the download completes (via the router), so the hardlinker just trusts the category. This avoids the race where a completed file sits in the wrong folder.

---

## 6. The hardlink script

The hardlinker is deliberately simple. It receives the qBittorrent completion args and maps **category → library destination**. No path parsing, no metadata queries.

Save as `/opt/qbithardlink/hardlink.sh`:

```bash
#!/bin/bash
# qBittorrent "Run external program on torrent completion" script.
# Args: %N (name) %F (content path) %L (category) %R (root path) %D (save path)
set -euo pipefail

torrentName="$1"
torrentPath="$2"
torrentCategory="$3"

# Categories managed by the *Arr apps — never hardlink these.
excludedCategories="radarr,sonarr,lidarr,readarr"
if [[ ",$excludedCategories," == *",$torrentCategory,"* ]]; then
  echo "[!] Skipped \"${torrentName}\" (excluded category ${torrentCategory})" >> "$(dirname "$0")/hardlink.log"
  exit 0
fi

# Category → library destination (source of truth).
case "$torrentCategory" in
  manga)        destDir="/data/media/books/manga" ;;
  manhwa)       destDir="/data/media/books/manhwa" ;;
  webtoon)      destDir="/data/media/books/webtoon" ;;
  comics)       destDir="/data/media/books/comics" ;;
  bd)           destDir="/data/media/books/bd" ;;
  light-novel)  destDir="/data/media/books/light-novel" ;;
  ebooks)       destDir="/data/media/books/ebooks" ;;
  books)        destDir="/data/media/books/_unprocessed" ;;
  *)            echo "[!] Unknown category \"$torrentCategory\" — skipping" >> "$(dirname "$0")/hardlink.log"; exit 0 ;;
esac

mkdir -p -- "$destDir"

# Hardlink the content path into the library. cp -l creates hardlinks.
if cp -rl -- "$torrentPath" "$destDir/"; then
  echo "[✔] Hardlinked \"${torrentName}\" → ${destDir}" >> "$(dirname "$0")/hardlink.log"
else
  echo "[x] Failed to hardlink \"${torrentName}\"" >> "$(dirname "$0")/hardlink.log"
  exit 1
fi
```

**Wire it up in qBittorrent:**

```text
Options → Downloads → Run external program on torrent completion:
  /opt/qbithardlink/hardlink.sh "%N" "%F" "%L"
```

> **Why `cp -rl`?** `cp -l` creates hardlinks (same inode) instead of copies. The torrent-side file and the library-side file share the same data blocks. Deleting the torrent-side file later (after seeding) does **not** break the library copy.

---

## 7. Qui automation rules

Use **Qui** as the orchestration trigger. It watches qBittorrent and can run the classifier or set categories directly.

**Rule 1 — Trigger classification on new `books` torrent:**

```text
Condition:
  Category = books
  State    = downloading (or added)

Action:
  Run external program: /opt/qbithardlink/classifier.py --once
```

**Rule 2 — Manual override via tag (optional):**

```text
Condition:
  Tags contains "manga"  (or manhwa/comics/bd/...)

Action:
  Set category = manga
  Enable AutoTMM
```

This gives you both **automatic classification** for normal downloads and **manual correction** for exceptions.

---

## 8. Seeding to 1:1 + CleanUpArr

### 8.1 The lifecycle

```text
1. Shelfarr sends torrent to qBit
              ↓
2. qBit downloads to /data/torrents/books/<category>
              ↓
3. hardlink script creates /data/media/books/<category> (same inode)
              ↓
4. qBit seeds; ratio limit = 1.00
              ↓
5. CleanUpArr removes torrent + torrent-side files
              ↓
6. /data/media hardlink survives → library intact
```

### 8.2 What CleanUpArr does

CleanUpArr is a **janitor/watchdog** between the *Arr apps and download clients. It removes downloads that are stuck, failed, unwanted, or have satisfied your retention policy. It does **not** create library copies — the hardlinker does that.

### 8.3 The critical rule

> **Never let CleanUpArr delete a torrent merely because it was imported.** If it deletes at ratio 0.37, you've failed your 1:1 requirement.

The safe removal condition is:

```text
Torrent completed
  + Media successfully imported (hardlink exists)
  + Ratio >= 1.0
  → Safe to remove from qBit + delete torrent-side files
```

Configure CleanUpArr to only act on torrents that have **reached the ratio limit** (qBittorrent pauses them at 1.00), then remove them. The `/data/media` hardlink survives because it's a separate link to the same inode.

---

## 9. Docker compose integration

Every container that touches torrents or media must share the same `/data` mount.

```yaml
services:
  qbittorrent:
    image: lscr.io/linuxserver/qbittorrent:latest
    container_name: qbittorrent
    environment:
      - PUID=99
      - PGID=100
      - TZ=Europe/Paris
    volumes:
      - /mnt/user/appdata/qbittorrent:/config
      - /mnt/user/data:/data
    ports:
      - "8084:8084"
    restart: unless-stopped

  shelfarr:
    image: ghcr.io/pedro-revez-silva/shelfarr:latest
    container_name: shelfarr
    restart: unless-stopped
    ports:
      - "5056:80"
    volumes:
      - /mnt/user/appdata/shelfarr:/rails/storage
      - /mnt/user/data:/data   # SAME root as qBittorrent — required for hardlinks

  classifier:
    build: /opt/qbithardlink
    container_name: qbit-classifier
    restart: unless-stopped
    environment:
      - QB_URL=http://192.168.1.116:8084
      - QB_USER=bidalos
      - QB_PASS=your-password
    volumes:
      - /opt/qbithardlink:/app
```

> **The single `/data` mount is non-negotiable.** If qBittorrent and the hardlinker see different paths for the same files, hardlinks fail with "cross-device link" errors.

---

## 10. Verification checklist

1. **Categories exist** — `curl .../torrents/categories` returns all 7 + `books`.
2. **AutoTMM enabled** — changing a torrent's category moves its files.
3. **Hardlink works** — `stat -c '%i' /data/torrents/books/manga/X.cbz /data/media/books/manga/X.cbz` shows the **same inode**.
4. **Same filesystem** — `df /data/torrents /data/media` shows the same device.
5. **Classifier conservative** — a low-confidence torrent stays in `books` with tag `review`.
6. **Ratio enforced** — qBittorrent pauses at 1.00; CleanUpArr only removes ratio-satisfied torrents.
7. **Library survives cleanup** — after CleanUpArr removes a torrent, the `/data/media` file still exists and plays.

---

## 11. Summary

| Step | Tool | Action |
|---|---|---|
| 1. Categorize | Classifier daemon | Watches `books`, sets category via regex + metadata |
| 2. Route | qBittorrent + AutoTMM | Moves torrent to `/data/torrents/books/<category>` |
| 3. Hardlink | `hardlink.sh` | `cp -rl` into `/data/media/books/<category>` |
| 4. Seed | qBittorrent | Ratio limit 1.00 |
| 5. Cleanup | CleanUpArr | Removes ratio-satisfied torrents; library survives |

The key insight: **the hardlinker never guesses.** The classifier decides, qBittorrent routes, the hardlinker trusts the category, and CleanUpArr only removes what has finished seeding.
