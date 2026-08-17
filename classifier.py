#!/usr/bin/env python3
"""
qBittorrent category classifier daemon.

Watches qBittorrent for torrents in the "books" category, classifies them
into manga/manhwa/webtoon/comics/bd/light-novel/ebooks/mags/audiobooks,
and sets the category.

Design: PRIORITY-ORDERED rules, not weight-summing. The first category that
hits a strong signal wins. Format tags ([EPUB]/[PDF]/[CBZ]/[CBR]/[M4B]) are
ambiguous, so content-type signals (publisher, language, characters, source
app) disambiguate.

Conservative by design: low-confidence results stay in "books" + tag "review".

Usage:
  classifier.py            # run as a daemon (poll loop)
  classifier.py --once     # run a single pass (for Qui automation)
  classifier.py --test     # run against corpus.txt and report accuracy
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

# Configuration (config.yaml + env overrides)
import config as cfg

# Optional metadata lookup (free/no-key providers). Imported lazily so the
# classifier still works if metadata.py is missing or its deps are absent.
try:
    from metadata import lookup_category
    HAS_METADATA = True
except Exception:
    lookup_category = None
    HAS_METADATA = False

# ── Configuration (from config.yaml, env-overridable) ────────────────────
QB_URL = cfg.get("qb.url", "http://192.168.1.116:8084")
QB_USER = cfg.get("qb.user", "bidalos")
QB_PASS = cfg.get("qb.password", "your-password")

AUTO_THRESHOLD = float(cfg.get("thresholds.auto", 0.90))
REVIEW_THRESHOLD = float(cfg.get("thresholds.review", 0.70))
POLL_INTERVAL = int(cfg.get("poll_interval", 10))
DEFAULT_CATEGORY = cfg.get("default_category", "ebooks")

# qBit tags that act as a manual override (highest priority).
TAG_OVERRIDES = set(cfg.get_tag_overrides().keys())

# ── CJK detection ────────────────────────────────────────────────────────
# Japanese/Chinese/Korean characters are a strong manga signal.
_CJK_RE = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af]")
# Japanese volume markers: 第X巻, 第X号, 第X部
_JP_VOL_RE = re.compile(r"第\s*[0-9０-９]+\s*[巻号部]")
# Japanese magazine names (weekly/monthly)
_JP_MAG_RE = re.compile(r"(?:週刊|月刊|ビッグコミック|ヤングマガジン|少年|少女|ジャンプ|マガジン|サンデー|チャンピオン)")


def normalize(name):
    """Release names use dots/underscores instead of spaces (Harley.Quinn).
    Normalize to spaces so character/publisher regexes match. Keep the
    original for format-tag detection."""
    return re.sub(r"[._]", " ", name)

# ── Priority-ordered rules (from config.yaml) ─────────────────────────────
# Each entry: (category, [(regex, weight), ...], min_score)
# The FIRST category whose total score >= min_score wins.
# Order matters: more specific categories come first.
RULES = cfg.get_rules()


def has_cjk(name):
    return bool(_CJK_RE.search(name))


def has_jp_volume(name):
    return bool(_JP_VOL_RE.search(name))


def has_jp_magazine(name):
    return bool(_JP_MAG_RE.search(name))


# ── Release-name cleaning for metadata lookup ────────────────────────────
# Strip format tags, release groups, years, and volume markers so the
# remaining title matches a metadata provider.
_META_NOISE = re.compile(
    r"(?i)"
    r"\[(?:epub|pdf|cbz|cbr|mobi|azw3?|m4b|aac|mp3|int?egrale|collection|bonus|scan|retail|web|hybrid|raw)\]"
    r"|\.(?:epub|pdf|cbz|cbr|mobi|azw3?|m4b|aac|mp3)\b"
    r"|[-_ ](?:NOTAG|NoTag|notag|TRADEME|kop1|AmisMed|PiXeL|RACHE|pRO|Pro|DiVER|iDiB|CTO|21A1|aKraa|NoTag)\b"
    r"|\b(?:19|20)\d{2}\b"
    r"|\bT\d{1,4}\b"
    r"|\b(?:FR|FRENCH|ENGLISH|iTALiAN|JP|JPN|KR|KOR|CN|CHN)\b"
    r"|\b(?:RETAiL|SCAN|eBOOK|eBook|ebook|AUDIOBOOK|HYBRiD|HYBRID|MANGA|COMICS|BD)\b"
    r"|\b(?:vol\.?\s*\d+|tome\s*\d+|part\s*\d+)\b"
)


def clean_release_name(name):
    """Reduce a release name to a searchable title for metadata providers."""
    cleaned = _META_NOISE.sub(" ", name)
    cleaned = re.sub(r"[._]+", " ", cleaned)  # dots/underscores → spaces
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def classify(name, tags=None, use_metadata=False):
    """Return (category, confidence, reasons).

    Priority:
      0. Manual tag override (highest)
      0.5 CJK characters → manga (very strong signal)
      1. Metadata lookup (authoritative): strip noise from the release name,
         query providers, use the provider's `format` as the category.
         Formats/extensions are NOT deterministic (a PDF can be manga, ebook,
         comic, or BD) — only provider metadata decides.
      2. Regex rules (fast-path fallback when metadata is off/unavailable)
      3. books/review
    """
    tags = [t.strip().lower() for t in (tags or "").split(",") if t.strip()]
    reasons = []
    norm = normalize(name)  # dots/underscores → spaces for content matching

    # 0. Manual override via tag (highest priority)
    for tag in tags:
        if tag in TAG_OVERRIDES:
            return tag, 1.0, [f"manual tag override: {tag}"]

    # 0.5 CJK characters → manga (very strong signal)
    if has_cjk(name):
        if has_jp_volume(name) or has_jp_magazine(name):
            return "manga", 1.0, ["CJK + Japanese volume/magazine marker"]
        return "manga", 0.95, ["CJK characters"]

    # 1. Metadata lookup (authoritative). Strip noise → query → category.
    if use_metadata and HAS_METADATA and lookup_category is not None:
        try:
            cat, conf, prov, title = lookup_category(clean_release_name(name))
            if cat:
                reasons.append(f"metadata:{prov} → {title!r}")
                return cat, conf, reasons
        except Exception as e:
            reasons.append(f"metadata error: {e}")

    # 2. Regex rules (fast-path fallback when metadata is off/unavailable)
    for cat, patterns, min_score in RULES:
        score = 0.0
        for pattern, weight in patterns:
            if re.search(pattern, norm):
                score += weight
                reasons.append(f"{cat}:{pattern}")
        if score >= min_score:
            return cat, min(score, 1.0), reasons

    # 3. Default: ebooks. If the whole cascade (metadata + regex) can't
    #    determine the type, the safest default for a book/comics library is
    #    ebooks — most undetermined releases are prose ebooks. Tag "review"
    #    so the user can correct it.
    return DEFAULT_CATEGORY, 0.5, reasons + [f"cascade undetermined → default {DEFAULT_CATEGORY} (review)"]


# ── qBittorrent API helpers ───────────────────────────────────────────────
class QBClient:
    def __init__(self, url, user, password):
        self.url = url.rstrip("/")
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

    def get_torrents(self):
        return json.loads(self._request("torrents/info"))

    def set_category(self, hashes, category):
        self._request("torrents/setCategory", {"hashes": hashes, "category": category})

    def add_tags(self, hashes, tags):
        self._request("torrents/addTags", {"hashes": hashes, "tags": tags})

    def set_auto_management(self, hashes, enable=True):
        self._request(
            "torrents/setAutoManagement",
            {"hashes": hashes, "enableAutoTMM": "true" if enable else "false"},
        )


# ── Idempotency state ─────────────────────────────────────────────────────
# Tags are the visible, user-controllable signal:
#   "review"     → cascade couldn't determine type; user should correct it.
#   "classified" → already routed to a category; skip on future polls.
# A state file is the invisible guard: even if tags are cleared, we never
# re-query metadata for a hash we've already processed.
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".classifier_state.json")

#: Tags that mark a torrent as already handled → skip on future polls.
DONE_TAGS = frozenset({"review", "classified"})


def _load_state():
    try:
        with open(STATE_FILE) as f:
            return set(json.load(f).get("processed", []))
    except Exception:
        return set()


def _save_state(processed):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"processed": sorted(processed)}, f)
    except Exception as e:
        print(f"warning: could not write state file: {e}")


def process_once(qb, use_metadata=False, state=None):
    """Classify new `books` torrents once. Idempotent: skips torrents already
    tagged review/classified or recorded in the state file."""
    if state is None:
        state = _load_state()
    changed = False
    for t in qb.get_torrents():
        if t.get("category") != "books":
            continue
        h = t.get("hash")
        tags = {x.strip().lower() for x in t.get("tags", "").split(",") if x.strip()}

        # Idempotency: skip if already handled (tag or state file).
        if tags & DONE_TAGS or h in state:
            continue

        cat, conf, reasons = classify(t.get("name", ""), t.get("tags", ""), use_metadata=use_metadata)
        # Tag "review" when the cascade fell back to the default (low conf),
        # so the user can correct it. High-confidence results are auto-routed.
        if conf < 0.7:
            qb.add_tags(h, "review")
        else:
            qb.add_tags(h, "classified")
        qb.set_category(h, cat)
        qb.set_auto_management(h, True)
        state.add(h)
        changed = True
        print(f"[{t.get('name')}] → {cat} (conf={conf:.2f}) {reasons}")

    if changed:
        _save_state(state)
    return state


def run_test(use_metadata=False):
    """Run against corpus.txt and report per-category accuracy."""
    from collections import defaultdict
    corpus_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "corpus.txt")
    by_cat = defaultdict(list)
    for line in open(corpus_path):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cat, _, name = line.partition("|")
        by_cat[cat.strip()].append(name.strip())

    total = correct = 0
    print(f"{'TRUE':<12} {'PRED':<12} {'CONF':<6} NAME")
    print("-" * 100)
    for cat in sorted(by_cat):
        for name in by_cat[cat]:
            pred, conf, _ = classify(name, use_metadata=use_metadata)
            total += 1
            ok = pred == cat
            correct += ok
            mark = "OK " if ok else "XX "
            print(f"{mark}{cat:<10} {pred:<12} {conf:<6.2f} {name[:55]}")
    print("-" * 100)
    print(f"\nAccuracy: {correct}/{total} = {correct/total*100:.1f}%")


def main():
    if "--test-meta" in sys.argv:
        run_test(use_metadata=True)
        return
    if "--test" in sys.argv:
        run_test()
        return

    qb = QBClient(QB_URL, QB_USER, QB_PASS)
    qb.login()

    if "--once" in sys.argv:
        process_once(qb, use_metadata=True)
        return

    print(f"Classifier watching {QB_URL} every {POLL_INTERVAL}s...")
    state = _load_state()
    while True:
        try:
            state = process_once(qb, use_metadata=True, state=state)
        except Exception as e:
            print(f"error: {e}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
