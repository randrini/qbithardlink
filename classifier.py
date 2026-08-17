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
import logging
import logging.handlers
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Configuration (config.yaml + env overrides)
import config as cfg

log = logging.getLogger("classifier")

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

# ── Video/TV/movie skip detection ──────────────────────────────────────────
# The daemon only processes "books" category torrents, but selftest.py may
# analyze all. Skip obvious video releases instead of routing them to ebooks.
_VIDEO_RE = re.compile(
    r"(?i)\b(S\d{1,3}E\d{1,4}|S\d{1,3}\s+E\d{1,4}|\d{1,2}x\d{1,2}|\b\d{3,4}p\b|\b(?:720|1080|2160)p\b|"
    r"x264|x265|HEVC|WEB[- ]?DL|WEB[- ]?Rip|BluRay|HDTV|HDRip|DVDRip|CAM|TS\b|"
    r"XXX\b|Nubiles|New Sensations|HOTWIFE|porn|adult)"
)


def is_video(name):
    """Return True if the release name is clearly a TV/movie/adult video."""
    return bool(_VIDEO_RE.search(name))


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
    r"|[-_ ](?:NOTAG|NoTag|notag|TRADEME|kop1|AmisMed|PiXeL|RACHE|pRO|Pro|DiVER|iDiB|CTO|21A1|aKraa|NoTag|ebdz|Team-Moi|NoFace696)\b"
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

    # 0.25 Skip obvious video/TV/movie/adult releases when run outside the
    #    "books" category (e.g. selftest.py over all torrents).
    if is_video(name):
        return "skip", 0.0, ["video release: skip"]

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
        # qBittorrent WebAPI requires Referer to match the origin for non-GET requests.
        req.add_header("Referer", self.url)
        with urllib.request.urlopen(req) as resp:
            # Capture session cookies (e.g. SID from auth/login).
            for header in resp.getheaders():
                if header[0].lower() == "set-cookie":
                    cookie = header[1].split(";")[0].strip()
                    if "=" in cookie:
                        k, v = cookie.split("=", 1)
                        self.cookies[k] = v
            return resp.read()

    def login(self):
        self._request("auth/login", {"username": self.user, "password": self.password})
        if "SID" not in self.cookies:
            raise RuntimeError("qBittorrent login did not return a session cookie")

    def get_torrents(self):
        return json.loads(self._request("torrents/info"))

    def set_category(self, hashes, category):
        # qBittorrent returns 409 if the category does not exist yet.
        try:
            self._request("torrents/setCategory", {"hashes": hashes, "category": category})
        except urllib.error.HTTPError as e:
            if e.code == 409:
                self._request("torrents/createCategory", {"category": category, "savePath": ""})
                self._request("torrents/setCategory", {"hashes": hashes, "category": category})
            else:
                raise

    def add_tags(self, hashes, tags):
        self._request("torrents/addTags", {"hashes": hashes, "tags": tags})

    def set_auto_management(self, hashes, enable=True):
        try:
            self._request(
                "torrents/setAutoManagement",
                {"hashes": hashes, "enable": "true" if enable else "false"},
            )
        except urllib.error.HTTPError as e:
            # Older/newer qBittorrent builds may use enableAutoTMM or disagree
            # on parameter names; auto-management is optional, so log and continue.
            log.warning("setAutoManagement failed (%s %s) — continuing", e.code, e.reason)


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
        log.warning("could not write state file: %s", e)


def process_once(qb, use_metadata=False, state=None):
    """Classify new `books` torrents once. Idempotent: skips torrents already
    tagged review/classified or recorded in the state file."""
    if state is None:
        state = _load_state()
    changed = False
    source_category = cfg.get("qb.source_category", "books")
    hardlink_enabled = bool(cfg.get("hardlink.enabled", True))
    hardlink_script = cfg.get("hardlink.script", "/app/hardlink.sh")
    for t in qb.get_torrents():
        if t.get("category") != source_category:
            continue
        h = t.get("hash")
        tags = {x.strip().lower() for x in t.get("tags", "").split(",") if x.strip()}

        # Idempotency: skip if already handled (tag or state file).
        if tags & DONE_TAGS or h in state:
            continue

        cat, conf, reasons = classify(t.get("name", ""), t.get("tags", ""), use_metadata=use_metadata)
        if cat == "skip":
            # Non-book torrent that somehow landed in "books"; leave it untouched.
            continue
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
        log.info("[%s] → %s (conf=%.2f) %s", t.get("name"), cat, conf, reasons)

        # Hardlink the completed content into the library (if enabled).
        if hardlink_enabled:
            content_path = t.get("content_path") or (
                os.path.join(t.get("save_path", ""), t.get("name", "")) if t.get("save_path") and t.get("name") else None
            )
            if content_path:
                torrent_name = t.get("name", "")
                try:
                    result = subprocess.run(
                        [hardlink_script, torrent_name, content_path, cat],
                        check=False,
                        timeout=300,
                        capture_output=True,
                        text=True,
                    )
                    if result.returncode == 0:
                        log.info("hardlink ok: %s → %s (rc=%d)", torrent_name, content_path, result.returncode)
                    else:
                        log.warning(
                            "hardlink failed: %s (rc=%d) stderr=%s",
                            torrent_name, result.returncode, (result.stderr or "").strip(),
                        )
                except subprocess.TimeoutExpired:
                    log.warning("hardlink timed out after 300s: %s", torrent_name)
                except Exception as e:
                    log.warning("hardlink error for %s: %s", torrent_name, e)
            else:
                log.warning("hardlink enabled but no content_path/save_path for %s", t.get("name"))

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
    # ── Logging setup: rotating file + console ────────────────────────────
    log_file = cfg.get("log.file", "/app/logs/classifier.log")
    log_level = getattr(logging, str(cfg.get("log.level", "INFO")).upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        log_dir = os.path.dirname(log_file)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        handlers.insert(
            0,
            logging.handlers.RotatingFileHandler(
                log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
            ),
        )
    except Exception as e:
        log.warning("could not set up file logging (%s) — console only", e)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )

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

    log.info("Classifier watching %s every %ds...", QB_URL, POLL_INTERVAL)
    state = _load_state()
    while True:
        try:
            state = process_once(qb, use_metadata=True, state=state)
        except Exception as e:
            log.exception("error in poll loop: %s", e)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
