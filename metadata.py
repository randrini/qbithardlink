#!/usr/bin/env python3
"""
Metadata lookup layer for the qBittorrent classifier.

Queries 2-3 free/no-key providers per content type (manga, ebooks, bd,
comics, light-novel, webtoon) to resolve ambiguous release names and
derive the category from provider metadata.

Provider selection is based on the MetaKavita community-scraper catalog
(https://github.com/raukorim-bot/community-scraper-metakavita), which grades
each provider's payload quality. We use the free/no-key, API-based providers
first (most reliable), then HTML providers where they are the best fit.

        Each provider returns a normalized dict:
    {
        "title": str,
        "format": "manga"|"webtoon"|"comic"|"book"|"light_novel",
        "publisher": str|None,
        "authors": [str]|None, # writers/creators where available
        "artist": str|None,    # illustrator/cover artist where available
        "language": str|None,   # ISO-639-1
        "country": str|None,   # ISO-3166-1
        "year": str|int|None,
        "genres": [str],
        "isbn": str|None,
        "confidence": float,   # 0..1 provider match confidence
    }
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("metadata")

# ── Optional deps (BeautifulSoup for HTML providers, requests for FlareSolverr) ──
try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except Exception:
    HAS_BS4 = False

try:
    import requests as _requests
    HAS_REQUESTS = True
except Exception:
    HAS_REQUESTS = False

# FlareSolverr endpoint (for Cloudflare/anti-bot protected sites). Must be an
# RFC1918/private address to avoid accidental SSRF to public hosts.
FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "")

# Google Books API key (optional; improves ebook/comic/manga resolution)
GOOGLE_BOOKS_API_KEY = os.environ.get("GOOGLE_BOOKS_API_KEY", "").strip()

# ComicVine API key (required for ComicVineProvider)
COMICVINE_API_KEY = os.environ.get("COMICVINE_API_KEY", "").strip()


def _is_private_url(url: str) -> bool:
    """Return True if the URL host is an RFC1918/private or loopback address."""
    try:
        host = urllib.parse.urlparse(url).hostname or ""
        if not host:
            return False
        # Allow localhost names commonly used in container networks.
        if host.lower() in {"localhost", "127.0.0.1", "::1"}:
            return True
        addr = ipaddress.ip_address(host)
        return addr.is_private or addr.is_loopback
    except ValueError:
        # Hostname couldn't be parsed as IP; conservatively reject.
        return False


# Provider enable/disable + rate limits from config.yaml
try:
    import config as _cfg
    _PROVIDER_SETTINGS = _cfg.get_provider_settings()
    _META_ENABLED = bool(_cfg.get("metadata.enabled", True))
    _FLARESOLVERR_RETRIES = int(_cfg.get("metadata.flaresolverr_retries", 3))
    _FLARESOLVERR_BACKOFF = float(_cfg.get("metadata.flaresolverr_backoff_seconds", 2.0))
    if _cfg.get("metadata.flaresolverr_url"):
        FLARESOLVERR_URL = _cfg.get("metadata.flaresolverr_url")
    if _cfg.get("metadata.google_books_api_key"):
        GOOGLE_BOOKS_API_KEY = _cfg.get("metadata.google_books_api_key")
    if _cfg.get("metadata.comicvine_api_key"):
        COMICVINE_API_KEY = _cfg.get("metadata.comicvine_api_key")
except Exception as _e:
    log.warning("metadata: config import failed: %s; using defaults", _e)
    _PROVIDER_SETTINGS = {}
    _META_ENABLED = True
    _FLARESOLVERR_RETRIES = 3
    _FLARESOLVERR_BACKOFF = 2.0

# Validate FlareSolverr URL points to a private host; disable if not.
if FLARESOLVERR_URL and not _is_private_url(FLARESOLVERR_URL):
    log.warning(
        "metadata: FLARESOLVERR_URL %s does not resolve to a private host; disabling JS bypass",
        FLARESOLVERR_URL,
    )
    FLARESOLVERR_URL = ""


# Cached FlareSolverr availability flag.
_FLARESOLVERR_OK = None
_FLARESOLVERR_WARNED = False


def _flaresolverr_available():
    """Check (and cache) whether the solver is reachable.

    Tries Trawl's /health first, then FlareSolverr/Byparr GET /v1.
    """
    global _FLARESOLVERR_OK, _FLARESOLVERR_WARNED
    if not HAS_REQUESTS or not FLARESOLVERR_URL:
        _FLARESOLVERR_OK = False
        return False
    if _FLARESOLVERR_OK is not None:
        return _FLARESOLVERR_OK

    _FLARESOLVERR_OK = False
    for path in ("/health", "/v1"):
        try:
            r = _requests.get(f"{FLARESOLVERR_URL}{path}", timeout=3)
            if r.status_code == 200:
                _FLARESOLVERR_OK = True
                break
        except Exception:
            continue

    if not _FLARESOLVERR_OK and not _FLARESOLVERR_WARNED:
        _FLARESOLVERR_WARNED = True
        log.warning("FlareSolverr/Trawl API unavailable at %s; disabling JS-dependent providers", FLARESOLVERR_URL)
    return _FLARESOLVERR_OK


def _flaresolverr_get(url, max_timeout=15000):
    """Fetch a URL through FlareSolverr/Trawl (bypasses Cloudflare/anti-bot).

    Retries with exponential backoff on timeout/connection errors.
    """
    if not HAS_REQUESTS:
        return None
    if not _flaresolverr_available():
        return None

    base = FLARESOLVERR_URL.rstrip("/")
    endpoint = f"{base}/v1" if not base.endswith("/v1") else base
    payload = {"cmd": "request.get", "url": url, "maxTimeout": max_timeout}
    # Network timeout slightly longer than the browser maxTimeout so the solver
    # has a chance to return a proper error response before we give up.
    net_timeout = max_timeout / 1000 + 5

    last_error = None
    for attempt in range(max(1, _FLARESOLVERR_RETRIES)):
        try:
            resp = _requests.post(endpoint, json=payload, timeout=net_timeout)
            data = resp.json()
            if data.get("status") != "ok":
                log.warning("FlareSolverr error: %s", data.get("message"))
                return None
            return data.get("solution", {}).get("response")
        except Exception as e:
            last_error = e
            if attempt < _FLARESOLVERR_RETRIES - 1:
                wait = _FLARESOLVERR_BACKOFF * (2 ** attempt)
                log.debug("FlareSolverr attempt %d failed (%s); retrying in %.1fs", attempt + 1, e, wait)
                time.sleep(wait)
            else:
                log.warning("FlareSolverr request failed after %d attempts: %s", _FLARESOLVERR_RETRIES, e)
    return None

# ── HTTP helper (stdlib only, no external deps) ──────────────────────────
_UA = "qbit-classifier/1.0 (metadata lookup)"


def _http_get_json(url, params=None, headers=None, timeout=12):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": _UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _http_post_json(url, payload, headers=None, timeout=12):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"User-Agent": _UA, "Content-Type": "application/json", **(headers or {})},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


# ── Normalization ────────────────────────────────────────────────────────
_ACCENTS = str.maketrans("àâäáãçéèêëíìîïñóòôöõúùûüýÿ", "aaaaaceeeeiiiinooooouuuuyy")


#: Words too weak to anchor a title match (avoid "Le"/"The" matching anything).
_STOPWORDS = {
    "le", "la", "les", "l", "un", "une", "des", "de", "du", "au", "aux",
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
}


def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower().translate(_ACCENTS)).strip()


def _tokens(text):
    """Non-stopword tokens of a normalized title."""
    return {t for t in _norm(text).split() if len(t) > 2 and t not in _STOPWORDS}


def _title_match_score(a, b, min_overlap=0.6):
    """Return a 0..1 score for how strongly two titles match.

    Safe strategies:
      1. Exact equality -> 1.0.
      2. Short query (<=2 non-stop tokens): 1.0 if every query token appears
         as a whole word in the provider title, else 0.0.
      3. Longer query: score = shared tokens / query tokens, but at least 2
         shared non-stop tokens are required. Return 0.0 if below min_overlap.
    """
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0

    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0

    # Strategy 1: exact equality after normalization.
    if na == nb:
        return 1.0

    # Strategy 2: short query -> require whole-word presence of every token.
    if len(ta) <= 2:
        nb_words = set(nb.split())
        return 1.0 if all(t in nb_words for t in ta) else 0.0

    # Strategy 3: token overlap relative to the query token set.
    shared = ta & tb
    if len(shared) < 2:
        return 0.0
    score = len(shared) / len(ta)
    return score if score >= min_overlap else 0.0


def _title_similar(a, b, min_overlap=0.6):
    """Boolean wrapper for _title_match_score."""
    return _title_match_score(a, b, min_overlap) > 0.0


# ── Provider base ─────────────────────────────────────────────────────────
class Provider:
    id = ""
    display_name = ""
    #: content types this provider can resolve
    types = set()
    rate_limit = 1.0  # seconds between requests
    #: If True, a single-vote result from this provider is not enough;
    #: another independent provider must agree before we route to its category.
    requires_corroboration = False

    def __init__(self):
        self._last = 0.0

    def _throttle(self):
        wait = self.rate_limit - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()

    def lookup(self, title):
        """Return normalized dict or None. Subclasses implement."""
        raise NotImplementedError

    def _candidate(self, **kw):
        base = {
            "title": None, "format": None, "publisher": None,
            "authors": None, "artist": None,
            "language": None, "country": None, "year": None, "genres": [],
            "isbn": None, "confidence": 0.0, "provider": self.id,
        }
        base.update(kw)
        return base


# ════════════════════════════════════════════════════════════════════════
# MANGA providers
# ════════════════════════════════════════════════════════════════════════
class MangaDexProvider(Provider):
    """MangaDex public API — free, no key. format from original language."""
    id = "MANGADEX"
    display_name = "MangaDex"
    types = {"manga", "webtoon"}
    rate_limit = 0.25

    def lookup(self, title):
        self._throttle()
        try:
            params = [
                ("title", title), ("limit", "5"), ("order[relevance]", "desc"),
                ("contentRating[]", "safe"), ("contentRating[]", "suggestive"),
                ("contentRating[]", "erotica"), ("contentRating[]", "pornographic"),
                ("includes[]", "author"), ("includes[]", "artist"), ("includes[]", "cover_art"),
            ]
            url = "https://api.mangadex.org/manga?" + urllib.parse.urlencode(params)
            data = _http_get_json(url, headers={"User-Agent": "qbit-classifier/1.0"})
            items = data.get("data", [])
            if not items:
                return None
            best = items[0]
            attrs = best.get("attributes", {})
            orig_lang = (attrs.get("originalLanguage") or "").lower()
            fmt = "webtoon" if orig_lang in ("ko", "zh") else "manga"
            # title: prefer en, then ja-ro, then any
            title_map = attrs.get("title", {}) or {}
            t = title_map.get("en") or title_map.get("ja-ro") or next(iter(title_map.values()), None)
            if not t or not _title_similar(t, title):
                return None
            authors = []
            artists = []
            for rel in best.get("relationships", []):
                if not isinstance(rel, dict):
                    continue
                rid = rel.get("id")
                rtype = (rel.get("type") or "").lower()
                # resolve from included list
                included = {item.get("id"): item for item in data.get("included", []) if isinstance(item, dict)}
                info = included.get(rid, {})
                name = None
                if isinstance(info, dict):
                    attr = info.get("attributes", {})
                    name = attr.get("name") if isinstance(attr, dict) else None
                if rtype == "author":
                    if name:
                        authors.append(name)
                elif rtype == "artist":
                    if name:
                        artists.append(name)
            return self._candidate(
                title=t, format=fmt, language=orig_lang,
                authors=authors or None,
                artist=artists[0] if artists else None,
                year=str(attrs.get("year")) if attrs.get("year") else None,
                genres=[g.get("attributes", {}).get("name", {}).get("en", "")
                        for g in attrs.get("tags", []) if g.get("attributes", {}).get("name", {}).get("en")],
                confidence=0.9,
            )
        except Exception as e:
            log.debug("MangaDex lookup failed: %s", e)
            return None


class KitsuProvider(Provider):
    """Kitsu JSON:API — free, no key. format from mangaType."""
    id = "KITSU"
    display_name = "Kitsu"
    types = {"manga", "webtoon"}
    rate_limit = 1.5

    def lookup(self, title):
        self._throttle()
        try:
            url = "https://kitsu.io/api/edge/manga"
            params = {"filter[text]": title, "page[limit]": 5, "include": "categories"}
            data = _http_get_json(url, params=params, headers={"Accept": "application/vnd.api+json"})
            items = data.get("data", [])
            if not items:
                return None
            best = items[0]
            attrs = best.get("attributes", {})
            manga_type = (attrs.get("mangaType") or "").lower()
            fmt = "webtoon" if manga_type in ("manhwa", "manhua", "webtoon") else "manga"
            t = attrs.get("canonicalTitle") or attrs.get("titles", {}).get("en")
            if not t or not _title_similar(t, title):
                return None
            return self._candidate(
                title=t, format=fmt,
                authors=[a.get("attributes", {}).get("name") for a in data.get("included", [])
                         if isinstance(a, dict) and a.get("type") == "authors" and a.get("attributes", {}).get("name")],
                genres=[c.get("attributes", {}).get("title") for c in data.get("included", []) if c.get("type") == "categories"],
                confidence=0.9,
            )
        except Exception as e:
            log.debug("Kitsu lookup failed: %s", e)
            return None


class ShikimoriProvider(Provider):
    """Shikimori JSON API — free, no key. format from kind."""
    id = "SHIKIMORI"
    display_name = "Shikimori"
    types = {"manga", "webtoon", "light_novel"}
    rate_limit = 0.75

    def lookup(self, title):
        self._throttle()
        try:
            url = "https://shikimori.one/api/mangas"
            params = {"search": title, "limit": 5}
            items = _http_get_json(url, params=params)
            if not isinstance(items, list) or not items:
                return None
            best = items[0]
            kind = str(best.get("kind", "")).lower()
            if kind in ("manhwa", "manhua"):
                fmt = "webtoon"
            elif kind in ("light_novel", "novel"):
                fmt = "light_novel"
            else:
                fmt = "manga"
            t = best.get("name") or best.get("russian")
            if not t or not _title_similar(t, title):
                return None
            return self._candidate(
                title=t, format=fmt,
                authors=[str(a) for a in best.get("authors") or [] if a] or None,
                genres=[g.get("name") for g in best.get("genres", []) if isinstance(g, dict)],
                confidence=0.9,
            )
        except Exception as e:
            log.debug("Shikimori lookup failed: %s", e)
            return None


class MangaBakaProvider(Provider):
    """MangaBaka V2 API — free, no key. Fast manga/manhwa/webtoon/novel search.

    Endpoint: https://api.mangabaka.org/v2/series/search?q=...&schema=full
    Format comes from the `type` field (MANGA/MANHWA/WEBTOON/NOVEL), with a
    tag/genre fallback for manhwa/webtoon.
    """
    id = "MANGABAKA"
    display_name = "MangaBaka"
    types = {"manga", "webtoon", "light_novel"}
    rate_limit = 2.25  # MangaBaka search quota is 30/min

    def lookup(self, title):
        self._throttle()
        try:
            url = "https://api.mangabaka.org/v2/series/search"
            params = {"q": title, "schema": "full"}
            data = _http_get_json(url, params=params)
            items = data.get("data") if isinstance(data, dict) else data
            if not isinstance(items, list) or not items:
                return None

            best = None
            best_score = 0.0
            best_t = ""
            for item in items:
                if not isinstance(item, dict):
                    continue
                candidates = [item.get("name") or item.get("title")]
                for alt in item.get("titles") or []:
                    if isinstance(alt, dict) and alt.get("title"):
                        candidates.append(alt["title"])
                    elif isinstance(alt, str) and alt.strip():
                        candidates.append(alt)
                for t in candidates:
                    if not t:
                        continue
                    score = _title_match_score(t, title)
                    if score > best_score:
                        best_score = score
                        best = item
                        best_t = t

            if best is None or best_score < 0.5:
                return None

            # Format from `type` field, with tag/genre fallback.
            mb_type = str(best.get("type", "")).upper()
            if "MANHWA" in mb_type or "WEBTOON" in mb_type:
                fmt = "webtoon"
            elif "NOVEL" in mb_type:
                fmt = "light_novel"
            elif "MANGA" in mb_type:
                fmt = "manga"
            else:
                tags = " ".join(str(x) for x in (best.get("tags") or [])).upper()
                genres = " ".join(str(x) for x in (best.get("genres") or [])).upper()
                if "MANHWA" in tags or "WEBTOON" in tags or "MANHWA" in genres or "WEBTOON" in genres:
                    fmt = "webtoon"
                else:
                    fmt = "manga"

            # Publisher: prefer localized edition, fall back to original.
            publisher = None
            for pub in best.get("publishers") or []:
                if isinstance(pub, dict) and pub.get("name"):
                    p_type = str(pub.get("type", "")).lower()
                    if "original" not in p_type and "ja" not in p_type:
                        publisher = pub["name"].strip()
                        break
            if not publisher:
                for pub in best.get("publishers") or []:
                    if isinstance(pub, dict) and pub.get("name"):
                        publisher = pub["name"].strip()
                        break

            genres_list = []
            raw_tags = best.get("tags") or []
            if raw_tags and isinstance(raw_tags[0], dict) and "is_genre" in raw_tags[0]:
                for tag in raw_tags:
                    if isinstance(tag, dict) and tag.get("name") and tag.get("is_genre"):
                        genres_list.append(tag["name"])
            for g in best.get("genres") or []:
                if isinstance(g, dict) and g.get("name"):
                    genres_list.append(g["name"])
                elif isinstance(g, str) and g.strip():
                    genres_list.append(g.strip())

            # Authors/artists from staff list if available.
            authors = []
            artists = []
            for person in best.get("staff") or []:
                if not isinstance(person, dict):
                    continue
                p_name = person.get("name") if isinstance(person.get("name"), str) else None
                roles = " ".join(str(r) for r in (person.get("roles") or [])).lower()
                if not p_name:
                    continue
                if "art" in roles or "illustration" in roles or "artist" in roles:
                    artists.append(p_name)
                elif "story" in roles or "author" in roles:
                    authors.append(p_name)

            return self._candidate(
                title=best_t, format=fmt, publisher=publisher,
                authors=authors or None,
                artist=artists[0] if artists else None,
                year=str(best.get("year")) if best.get("year") else None,
                genres=genres_list, confidence=best_score,
            )
        except Exception as e:
            log.debug("MangaBaka lookup failed: %s", e)
            return None


class JikanProvider(Provider):
    """Jikan (unofficial MyAnimeList API) — free, no key.

    Endpoint: https://api.jikan.moe/v4/manga?q=...&limit=10
    Public API with a ~3 req/sec rate limit. Format comes from the `type`
    field (Manga/Manhwa/Manhua/Light Novel/Novel), with a webtoon override
    for manhwa whose genres include webtoon.
    """
    id = "jikan"
    display_name = "Jikan (MyAnimeList)"
    types = {"manga", "webtoon", "light_novel", "book"}
    rate_limit = 0.35  # ~3 req/sec
    requires_corroboration = False

    _BASE = "https://api.jikan.moe/v4/manga"

    @staticmethod
    def _format_from_type(mtype, genres):
        """Map Jikan `type` to our format string; None for unknown types."""
        t = str(mtype or "").strip().lower()
        genres_joined = " ".join(genres or []).lower()
        if t == "manga":
            return "manga"
        if t == "manhwa":
            return "webtoon" if "webtoon" in genres_joined else "manga"
        if t == "manhua":
            return "manga"
        if t == "light novel":
            return "light_novel"
        if t == "novel":
            return "book"
        return None

    def lookup(self, title):
        if not HAS_REQUESTS:
            return None
        self._throttle()
        try:
            resp = _requests.get(self._BASE, params={"q": title, "limit": 10}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict) or data.get("error") or data.get("status") not in (None, 200):
                log.debug("Jikan API error response: %s", str(data)[:200])
                return None
            items = data.get("data") or []
            if not items:
                return None

            best = None
            best_score = 0.0
            best_t = ""
            for item in items:
                if not isinstance(item, dict):
                    continue
                candidates = [item.get("title"), item.get("title_english"),
                              item.get("title_japanese")]
                for syn in item.get("synonyms") or []:
                    if isinstance(syn, str) and syn.strip():
                        candidates.append(syn)
                for t in candidates:
                    if not t:
                        continue
                    score = _title_match_score(str(t), title)
                    if score > best_score:
                        best_score = score
                        best = item
                        best_t = str(t)

            if best is None or best_score < 0.6:
                return None

            genres = [g.get("name") for g in best.get("genres") or []
                      if isinstance(g, dict) and g.get("name")]
            fmt = self._format_from_type(best.get("type"), genres)
            if not fmt:
                log.debug("Jikan: unknown type %r for %r", best.get("type"), best_t)
                return None

            # Confidence from title match + MAL popularity.
            scored_by = best.get("scored_by") or 0
            confidence = 0.7 + 0.1 * best_score
            if scored_by > 1000:
                confidence += 0.1
            confidence = min(confidence, 0.95)

            return self._candidate(
                title=best_t, format=fmt, genres=genres,
                confidence=confidence,
            )
        except Exception as e:
            log.debug("Jikan lookup failed: %s", e)
            return None


# ════════════════════════════════════════════════════════════════════════
# EBOOK providers
# ════════════════════════════════════════════════════════════════════════
class OpenLibraryProvider(Provider):
    """Open Library search.json — free, no key. format=book."""
    id = "OPENLIBRARY"
    display_name = "Open Library"
    types = {"book"}
    rate_limit = 1.1

    def lookup(self, title):
        self._throttle()
        try:
            url = "https://openlibrary.org/search.json"
            params = {"q": title, "limit": 5}
            data = _http_get_json(url, params=params)
            docs = data.get("docs", [])
            if not docs:
                return None
            best = docs[0]
            t = best.get("title")
            if not t or not _title_similar(t, title):
                return None
            isbn = None
            if best.get("isbn"):
                isbn = str(best["isbn"][0]).replace("-", "").replace(" ", "")
            authors = None
            if best.get("author_name"):
                authors = [str(a) for a in best["author_name"][:3] if a]
            year = None
            if best.get("first_publish_year"):
                year = int(best["first_publish_year"])
            return self._candidate(
                title=t, format="book",
                publisher=best.get("publisher", [None])[0] if best.get("publisher") else None,
                authors=authors,
                language=best.get("language", [None])[0] if best.get("language") else None,
                year=year,
                genres=best.get("subject", [])[:5],
                isbn=isbn, confidence=0.9,
            )
        except Exception as e:
            log.debug("OpenLibrary lookup failed: %s", e)
            return None


class GoogleBooksProvider(Provider):
    """Google Books API — needs a key (optional; works without for low volume)."""
    id = "GOOGLEBOOKS"
    display_name = "Google Books"
    types = {"book", "comic", "manga"}
    rate_limit = 1.0

    def __init__(self, api_key=None):
        super().__init__()
        self.api_key = api_key

    @staticmethod
    def _format_from_categories(categories):
        """Derive format from Google Books `categories` (comics/manga detection).

        Google Books categories are free-text; look for strong markers.
        Manga is checked first because "Comics & Graphic Novels / Manga" also
        contains "comic".
        """
        joined = " ".join(categories or []).lower()
        if "manga" in joined:
            return "manga"
        if "comics & graphic novels" in joined or "comic" in joined:
            return "comic"
        return "book"

    def lookup(self, title):
        self._throttle()
        try:
            url = "https://www.googleapis.com/books/v1/volumes"
            params = {"q": title, "maxResults": 5, "country": "US", "printType": "books"}
            if self.api_key:
                params["key"] = self.api_key
            data = _http_get_json(url, params=params)
            items = data.get("items", [])
            if not items:
                return None
            best = items[0].get("volumeInfo", {})
            t = best.get("title")
            if not t or not _title_similar(t, title):
                return None
            isbn = None
            for ident in best.get("industryIdentifiers", []):
                if ident.get("type") in ("ISBN_13", "ISBN_10"):
                    isbn = str(ident.get("identifier")).replace("-", "").replace(" ", "")
                    break
            categories = best.get("categories", [])
            fmt = self._format_from_categories(categories)
            authors = [str(a) for a in best.get("authors") or [] if a] or None
            year = None
            pub_date = best.get("publishedDate") or ""
            if isinstance(pub_date, str):
                m = re.search(r"\b(19|20)\d{2}\b", pub_date)
                if m:
                    year = int(m.group(0))
            return self._candidate(
                title=t, format=fmt,
                publisher=best.get("publisher"),
                authors=authors,
                language=best.get("language"),
                year=year,
                genres=categories,
                isbn=isbn, confidence=0.9,
            )
        except Exception as e:
            log.debug("GoogleBooks lookup failed: %s", e)
            return None


# ════════════════════════════════════════════════════════════════════════
# BD / COMIC providers (HTML via FlareSolverr + BeautifulSoup)
# ════════════════════════════════════════════════════════════════════════
class PlaneteBDProvider(Provider):
    """Planète BD — French BD + US comics (HTML via FlareSolverr). format=comic."""
    id = "PLANETEBD"
    display_name = "Planète BD"
    types = {"comic", "bd"}
    rate_limit = 2.5
    requires_corroboration = True

    _BASE = "https://www.planetebd.com"
    _ALBUM_RE = re.compile(
        r"^/(?P<kind>bd|comics|mangas)/(?P<publisher>[^/]+)/(?P<series>[^/]+)/(?P<album>[^/]+)/(?P<id>\d+)\.html",
        re.I,
    )

    def lookup(self, title):
        if not HAS_BS4:
            return None
        self._throttle()
        try:
            # 1. Search
            search_url = f"{self._BASE}/recherche/?mot-clef={urllib.parse.quote(title)}"
            html = _flaresolverr_get(search_url)
            if not html:
                return None
            soup = BeautifulSoup(html, "html.parser")
            hits = []
            for art in soup.select("article.featured"):
                a = art.select_one(".image a[href], a[href*='/bd/'], a[href*='/comics/']")
                if not a:
                    continue
                href = a.get("href")
                if not href:
                    continue
                path = urllib.parse.urlparse(href).path
                m = self._ALBUM_RE.match(path)
                if not m:
                    continue
                label = (a.get("title") or a.get_text(" ", strip=True) or "").strip()
                label = re.split(r",\s*(?:bd|comics)\s+chez\s+", label, maxsplit=1, flags=re.I)[0].strip()
                hits.append({"url": href, "label": label, "kind": m.group("kind")})
            if not hits:
                return None

            # 2. Pick best hit by title similarity score. Require a real match —
            #    never accept the first hit blindly (false positives).
            best = None
            best_score = 0.0
            for h in hits:
                score = _title_match_score(h["label"], title)
                if score > best_score:
                    best_score = score
                    best = h
            if not best or best_score < 0.7:
                return None

            # 3. Fetch detail page
            detail_url = best["url"] if best["url"].startswith("http") else f"{self._BASE}{best['url']}"
            detail_html = _flaresolverr_get(detail_url)
            if not detail_html:
                return None
            dsoup = BeautifulSoup(detail_html, "html.parser")

            # title
            h1 = dsoup.find("h1")
            fetched_title = h1.get_text(" ", strip=True) if h1 else best["label"]

            # format from path: /bd/ → bd, /comics/ → comic, /mangas/ → manga
            if best["kind"] == "bd":
                fmt = "bd"
            elif best["kind"] == "mangas":
                fmt = "manga"
            else:
                fmt = "comic"

            # artist/author from typical Planète BD labels (strip "chez...")
            def _extract_persons(text, label_re):
                out = []
                for m in re.finditer(label_re, text):
                    chunk = m.group(1)
                    # Split on " chez " / " de " / commas and take first clean token group
                    for sep in (" chez ", " chez", " de ", " par ", ","):
                        if sep in chunk:
                            chunk = chunk.split(sep)[0]
                            break
                    chunk = chunk.strip(" :")
                    for person in re.split(r"[,;/&]|\bet\b|\bavec\b", chunk):
                        person = person.strip()
                        if person and len(person) > 2:
                            out.append(person)
                return out

            authors = _extract_persons(detail_html, r"(?:scénario|scenario)\s*:\s*([^<\n]+)")
            artists = _extract_persons(detail_html, r"(?:dessin|dessins)\s*:\s*([^<\n]+)")
            if not artists:
                artists = _extract_persons(detail_html, r"(?:illustration|illustrateur)\s*:\s*([^<\n]+)")

            # publisher from "bd chez <publisher> de ..."
            publisher = None
            m = re.search(r"bd chez\s+([^<]+)", detail_html)
            if m:
                publisher = re.split(r"\s+de\s+", m.group(1).strip(), maxsplit=1)[0].strip()

            # year
            year = None
            m = re.search(r"\b(19|20)\d{2}\b", detail_html)
            if m:
                year = int(m.group(0))

            # isbn
            isbn = None
            m = re.search(r"\b(?:978|979)\d{10}\b", detail_html)
            if m:
                isbn = m.group(0)

            return self._candidate(
                title=fetched_title, format=fmt, publisher=publisher,
                authors=authors or None,
                artist=artists[0] if artists else None,
                country="FR", year=year, isbn=isbn, confidence=0.9,
            )
        except Exception as e:
            log.debug("PlaneteBD lookup failed: %s", e)
            return None


class BedethequeProvider(Provider):
    """Bédéthèque — Franco-Belgian comics (HTML via FlareSolverr). format=comic."""
    id = "BEDETHEQUE"
    display_name = "Bédéthèque"
    types = {"comic", "bd"}
    rate_limit = 2.0
    requires_corroboration = True

    _BASE = "https://www.bedetheque.com"

    def lookup(self, title):
        if not HAS_BS4:
            return None
        self._throttle()
        try:
            # 1. Get CSRF token
            token_html = _flaresolverr_get(f"{self._BASE}/search/albums")
            if not token_html:
                return None
            tsoup = BeautifulSoup(token_html, "html.parser")
            token = None
            tag = tsoup.find("input", {"name": "csrf_token_bel"})
            if tag and tag.get("value"):
                token = tag["value"]
            if not token:
                return None

            # 2. Search with token
            search_url = f"{self._BASE}/search/albums?RechSerie={urllib.parse.quote(title)}&csrf_token_bel={token}"
            search_html = _flaresolverr_get(search_url)
            if not search_html:
                return None
            ssoup = BeautifulSoup(search_html, "html.parser")

            # 3. Find album link
            album_url = None
            results_ul = ssoup.find("ul", class_="search-list")
            if results_ul:
                for li in results_ul.find_all("li"):
                    a = li.find("a", class_="image-tooltip") or li.find("a")
                    if a and a.get("href"):
                        album_url = a["href"]
                        break
            if not album_url:
                return None
            if not album_url.startswith("http"):
                album_url = f"{self._BASE}{album_url}"

            # 4. Fetch detail
            detail_html = _flaresolverr_get(album_url)
            if not detail_html:
                return None
            dsoup = BeautifulSoup(detail_html, "html.parser")

            h1 = dsoup.find("h1")
            fetched_title = h1.get_text(" ", strip=True) if h1 else title

            # author/artist from common Bédéthèque detail labels.
            authors = []
            artists = []
            for m in re.finditer(r"scénario\s*:\s*([^<\n]+)", detail_html):
                for person in re.split(r"[,;/&]|\bet\b|\bavec\b", m.group(1)):
                    person = person.strip(" :")
                    if person and len(person) > 2:
                        authors.append(person)
            for m in re.finditer(r"dessin\s*:\s*([^<\n]+)", detail_html):
                for person in re.split(r"[,;/&]|\bet\b|\bavec\b", m.group(1)):
                    person = person.strip(" :")
                    if person and len(person) > 2:
                        artists.append(person)

            publisher = None
            m = re.search(r"éditeur\s*[:\s]+([^<]+)", detail_html)
            if m:
                publisher = m.group(1).strip()

            year = None
            m = re.search(r"\b(19|20)\d{2}\b", detail_html)
            if m:
                year = int(m.group(0))

            isbn = None
            m = re.search(r"\b(?:978|979)\d{10}\b", detail_html)
            if m:
                isbn = m.group(0)

            return self._candidate(
                title=fetched_title, format="comic", publisher=publisher,
                authors=authors or None,
                artist=artists[0] if artists else None,
                country="FR", year=year, isbn=isbn, confidence=0.9,
            )
        except Exception as e:
            log.debug("Bedetheque lookup failed: %s", e)
            return None


class ComicVineProvider(Provider):
    """ComicVine API — US comics (requires COMICVINE_API_KEY). format=comic.

    Endpoint: https://comicvine.gamespot.com/api/volumes/?filter=name:...
    ComicVine's catalog is US-centric; a single-vote result is not trusted
    (requires_corroboration) to avoid false positives on homonyms.
    """
    id = "COMICVINE"
    display_name = "ComicVine"
    types = {"comic"}
    rate_limit = 1.2
    requires_corroboration = True

    def __init__(self, api_key=None):
        super().__init__()
        self.api_key = api_key

    def lookup(self, title):
        api_key = str(self.api_key or COMICVINE_API_KEY or "").strip()
        if not api_key:
            return None
        self._throttle()
        try:
            url = "https://comicvine.gamespot.com/api/volumes/"
            params = {
                "api_key": api_key,
                "format": "json",
                "filter": f"name:{title}",
                "limit": 20,
                "field_list": "id,name,start_year,count_of_issues,publisher",
            }
            data = _http_get_json(url, params=params)
            if data.get("status_code") != 1:
                log.debug("ComicVine API error: %s", data.get("error"))
                return None
            results = data.get("results") or []
            if not results:
                return None
            best = results[0]
            t = best.get("name")
            if not t or not _title_similar(t, title):
                return None
            pub = best.get("publisher") or {}
            publisher = pub.get("name") if isinstance(pub, dict) else None
            year = best.get("start_year")
            return self._candidate(
                title=t, format="comic", publisher=publisher,
                country="US", year=int(year) if str(year).isdigit() else None,
                confidence=0.9,
            )
        except Exception as e:
            log.debug("ComicVine lookup failed: %s", e)
            return None


class BDthequeProvider(Provider):
    """BDTheque.com — Franco-Belgian comics (HTML via FlareSolverr). format=bd.

    Search via AJAX typeahead: GET /ajax/search/series/{query} (JSON list),
    then fetch the series page /series/{id}/{slug} for the title/publisher.
    """
    id = "BDTHEQUE"
    display_name = "BDTheque"
    types = {"comic", "bd"}
    rate_limit = 2.2
    requires_corroboration = True

    _BASE = "https://www.bdtheque.com"

    def lookup(self, title):
        if not HAS_BS4:
            return None
        self._throttle()
        try:
            # 1. AJAX typeahead search (returns a JSON list of series)
            search_url = f"{self._BASE}/ajax/search/series/{urllib.parse.quote(title.strip(), safe='')}"
            html = _flaresolverr_get(search_url)
            if not html:
                return None
            try:
                hits = json.loads(html)
            except Exception:
                return None
            if not isinstance(hits, list):
                return None
            hits = [h for h in hits if isinstance(h, dict) and h.get("id")]
            if not hits:
                return None

            # 2. Pick best hit by title similarity (nom / nomvo).
            best = None
            best_score = 0.0
            for h in hits:
                for key in ("nom", "nomvo"):
                    n = h.get(key)
                    if not n:
                        continue
                    score = _title_match_score(n, title)
                    if score > best_score:
                        best_score = score
                        best = h
            if not best or best_score < 0.7:
                return None

            # 3. Fetch series detail page
            series_url = f"{self._BASE}/series/{best['id']}"
            detail_html = _flaresolverr_get(series_url)
            if not detail_html:
                return None
            dsoup = BeautifulSoup(detail_html, "html.parser")

            h1 = dsoup.find("h1")
            fetched_title = h1.get_text(" ", strip=True) if h1 else (best.get("nom") or title)

            # BDTheque has "Auteur(s)" / "Dessinateur(s)" rows in the same info table.
            authors = []
            artists = []
            for tr in dsoup.select("table.table-sm tr"):
                cells = tr.find_all("td")
                if len(cells) < 2:
                    continue
                label = cells[0].get_text(" ", strip=True).lower()
                text = cells[1].get_text(" ", strip=True)
                if "auteur" in label or "scénario" in label or "scenario" in label:
                    for person in re.split(r"[,;/&]|\bet\b|\bavec\b", text):
                        person = person.strip(" :")
                        if person and len(person) > 2:
                            authors.append(person)
                elif "dessin" in label or "illustration" in label:
                    for person in re.split(r"[,;/&]|\bet\b|\bavec\b", text):
                        person = person.strip(" :")
                        if person and len(person) > 2:
                            artists.append(person)

            publisher = None
            for tr in dsoup.select("table.table-sm tr"):
                cells = tr.find_all("td")
                if len(cells) < 2:
                    continue
                label = cells[0].get_text(" ", strip=True).lower()
                if "editeur" in label or "éditeur" in label:
                    links = [a.get_text(strip=True) for a in cells[1].find_all("a")]
                    publisher = (links[0] if links else cells[1].get_text(" ", strip=True).split("/")[0].strip())
                    break

            year = None
            m = re.search(r"\b(19|20)\d{2}\b", detail_html)
            if m:
                year = int(m.group(0))

            return self._candidate(
                title=fetched_title, format="bd", publisher=publisher,
                authors=authors or None,
                artist=artists[0] if artists else None,
                country="FR", year=year, confidence=0.9,
            )
        except Exception as e:
            log.debug("BDtheque lookup failed: %s", e)
            return None


# ════════════════════════════════════════════════════════════════════════
# LIGHT NOVEL providers
# ════════════════════════════════════════════════════════════════════════
class RanobeDBProvider(Provider):
    """RanobeDB — light novel database (official REST API, no key).

    Endpoint: https://ranobedb.org/api/v0/series?q=...
    Since the whole catalog is light novels, format is hardcoded to
    light_novel. Use _title_match_score on title + alternative titles to
    avoid false positives from the API's token-based relevance search.
    """
    id = "RANOBEDB"
    display_name = "RanobeDB"
    types = {"light_novel"}
    rate_limit = 0.6  # comfortably under documented 60/min ceiling

    _BASE = "https://ranobedb.org/api/v0"

    def lookup(self, title):
        self._throttle()
        try:
            data = _http_get_json(f"{self._BASE}/series", params={"q": title, "limit": 10})
            items = data.get("series") if isinstance(data, dict) else data
            if not isinstance(items, list) or not items:
                return None

            best = None
            best_score = 0.0
            best_t = ""
            for item in items:
                if not isinstance(item, dict):
                    continue
                candidates = []
                for key in ("title", "romaji"):
                    v = item.get(key)
                    if v:
                        candidates.append(str(v))
                for alt in item.get("titles") or []:
                    if isinstance(alt, dict) and alt.get("title"):
                        candidates.append(str(alt["title"]))

                for t in candidates:
                    if not t:
                        continue
                    score = _title_match_score(t, title)
                    # RanobeDB's native sim_score (0..1) is available on search results.
                    sim = float(item.get("sim_score") or 0)
                    if sim:
                        score = max(score, sim)
                    if score > best_score:
                        best_score = score
                        best = item
                        best_t = t

            if best is None or best_score < 0.6:
                return None

            # Prefer localized title; fall back to original/romaji.
            fetched_title = best_t or best.get("title") or ""

            # Extract year from YYYYMMDD int start_date.
            year = None
            for key in ("c_start_date", "start_date"):
                d = best.get(key)
                if isinstance(d, int) and d > 10000000:
                    year = d // 10000
                    break

            # Genres from tags.
            genres = []
            for tag in best.get("tags") or []:
                if isinstance(tag, dict) and tag.get("ttype") == "genre" and tag.get("name"):
                    genres.append(tag["name"])

            # Publisher: prefer publisher_type=="publisher" in English, else first.
            publisher = None
            for pub in best.get("publishers") or []:
                if isinstance(pub, dict) and pub.get("name"):
                    p_type = str(pub.get("publisher_type", "")).lower()
                    p_lang = str(pub.get("lang", "")).lower()
                    if p_type == "publisher" and p_lang in ("en", title[:2].lower()):
                        publisher = pub["name"].strip()
                        break
            if not publisher:
                for pub in best.get("publishers") or []:
                    if isinstance(pub, dict) and pub.get("name"):
                        publisher = pub["name"].strip()
                        break

            # Authors/artists from people list.
            authors = []
            artists = []
            for person in best.get("people") or []:
                if not isinstance(person, dict):
                    continue
                p_name = person.get("name")
                if not isinstance(p_name, str):
                    continue
                roles = " ".join(str(r) for r in (person.get("roles") or [])).lower()
                if "art" in roles or "illustration" in roles or "illustrator" in roles:
                    artists.append(p_name)
                else:
                    authors.append(p_name)

            return self._candidate(
                title=fetched_title, format="light_novel", publisher=publisher,
                authors=authors or None,
                artist=artists[0] if artists else None,
                language=best.get("lang"), year=year, genres=genres,
                confidence=best_score,
            )
        except Exception as e:
            log.debug("RanobeDB lookup failed: %s", e)
            return None


class NovelUpdatesProvider(Provider):
    """Novel Updates — EN light novels (HTML/CF). format=book."""
    id = "NOVELUPDATES"
    display_name = "Novel Updates"
    types = {"light_novel", "book"}
    rate_limit = 3.0

    def lookup(self, title):
        return None  # HTML/Cloudflare placeholder


# ════════════════════════════════════════════════════════════════════════
# Registry
# ════════════════════════════════════════════════════════════════════════
def _provider_enabled(pid):
    """Whether a provider is enabled in config.yaml (default: enabled)."""
    s = _PROVIDER_SETTINGS.get(pid)
    if s is None:
        return True
    return bool(s.get("enabled", True))


def _provider_rate_limit(pid, default):
    """Per-provider rate limit from config.yaml (default: class default)."""
    s = _PROVIDER_SETTINGS.get(pid)
    if s is None:
        return default
    return float(s.get("rate_limit", default))


#: Provider id → signal flags it serves (used for targeted provider selection).
_SIGNAL_PROVIDERS = {
    "MANGADEX": {"manga"},
    "MANGABAKA": {"manga", "light_novel"},
    "jikan": {"manga", "webtoon", "light_novel"},
    "SHIKIMORI": {"manga", "light_novel"},
    "KITSU": {"manga"},
    "RANOBEDB": {"manga", "light_novel"},
    "GOOGLEBOOKS": {"light_novel", "audiobook"},
    "OPENLIBRARY": {"light_novel", "audiobook"},
    "COMICVINE": {"comics", "bd"},
    "PLANETEBD": {"comics", "bd"},
    "BEDETHEQUE": {"comics", "bd"},
    "BDTHEQUE": {"comics", "bd"},
}


def _provider_matches_signals(p, signals):
    """True if provider `p` is in the targeted subset for any active signal."""
    if not signals:
        return False
    sigs = _SIGNAL_PROVIDERS.get(p.id, set())
    return any(signals.get(s) for s in sigs)


def _build_providers(google_books_key=None, comicvine_key=None, signals=None):
    """Build provider list ordered by speed / reliability.

    Fast no-key API providers go first so that lookup_category can reach a 2-provider
    consensus quickly. JS/FlareSolverr-dependent providers are queried last.

    When `signals` are present, only the providers relevant to those signals
    are returned (targeted pass). With no signals, ALL providers are returned
    in the default order.
    """
    providers = []

    # 1. Manga / webtoon — fast, public API
    if _provider_enabled("mangadex"):
        p = MangaDexProvider()
        p.rate_limit = _provider_rate_limit("mangadex", p.rate_limit)
        providers.append(p)
    if _provider_enabled("mangabaka"):
        p = MangaBakaProvider()
        p.rate_limit = _provider_rate_limit("mangabaka", p.rate_limit)
        providers.append(p)
    if _provider_enabled("jikan"):
        p = JikanProvider()
        p.rate_limit = _provider_rate_limit("jikan", p.rate_limit)
        providers.append(p)

    # 2. Ebooks — fast, public APIs
    if _provider_enabled("openlibrary"):
        p = OpenLibraryProvider()
        p.rate_limit = _provider_rate_limit("openlibrary", p.rate_limit)
        providers.append(p)
    if _provider_enabled("googlebooks"):
        p = GoogleBooksProvider(google_books_key)
        p.rate_limit = _provider_rate_limit("googlebooks", p.rate_limit)
        providers.append(p)

    # 3. Anime/manga/light-novel metadata — public APIs, sometimes slow
    if _provider_enabled("shikimori"):
        p = ShikimoriProvider()
        p.rate_limit = _provider_rate_limit("shikimori", p.rate_limit)
        providers.append(p)
    if _provider_enabled("kitsu"):
        p = KitsuProvider()
        p.rate_limit = _provider_rate_limit("kitsu", p.rate_limit)
        providers.append(p)
    if _provider_enabled("ranobedb"):
        p = RanobeDBProvider()
        p.rate_limit = _provider_rate_limit("ranobedb", p.rate_limit)
        providers.append(p)

    # 4. Comics — public API, needs key
    if _provider_enabled("comicvine"):
        p = ComicVineProvider(comicvine_key)
        p.rate_limit = _provider_rate_limit("comicvine", p.rate_limit)
        providers.append(p)

    # 5. BD / comics — require FlareSolverr (JS rendering)
    if _provider_enabled("planetebd"):
        p = PlaneteBDProvider()
        p.rate_limit = _provider_rate_limit("planetebd", p.rate_limit)
        providers.append(p)
    if _provider_enabled("bedetheque"):
        p = BedethequeProvider()
        p.rate_limit = _provider_rate_limit("bedetheque", p.rate_limit)
        providers.append(p)
    if _provider_enabled("bdtheque"):
        p = BDthequeProvider()
        p.rate_limit = _provider_rate_limit("bdtheque", p.rate_limit)
        providers.append(p)

    if not signals:
        return providers

    # Targeted pass: keep only providers relevant to the active signals.
    # Order within the targeted list follows the default order above.
    targeted = [p for p in providers if _provider_matches_signals(p, signals)]
    return targeted


#: Known BD/comics/manga publishers/imprints used to disambiguate generic "book".
_FRENCH_BD_PUBLISHERS = {
    "glénat", "dupuis", "casterman", "le lombard", "dargaud", "delcourt",
    "bamboo", "albin michel", "clair de lune", "soleil", "tonkam", "ki-oon",
    "jungle", "vex", "paquet", "le triangle", "flblb", "bd kids",
}
_US_COMIC_PUBLISHERS = {
    "marvel", "dc comics", "dc", "image comics", "dark horse comics",
    "dark horse", "idw publishing", "idw", "boom! studios", "boom studios",
    "vertigo", "wildstorm", "max", "icon", "valiant", "dynamite entertainment",
}
_MANGA_PUBLISHERS = {
    "viz media", "kodansha comics", "kodansha", "yen press", "seven seas",
    "tokyopop", "vertical", "denpa", "j-novel club", "sol press",
}

#: Strong French BD markers in title/genre text.
_BD_MARKERS = {"tome", "bande dessinee", "bd", "franco belge", "integrale"}
#: Strong manga/manhwa/webtoon markers.
_MANGA_MARKERS = {"manga", "manhwa", "webtoon", "shonen", "shojo", "seinen", "josei"}
#: Strong US comic markers.
_COMIC_MARKERS = {"comic", "graphic novel", "superhero", "dc comics", "marvel comics"}


def _norm_token(text):
    return _norm(text).lower()


def _publisher_tokens(publisher):
    """Yield normalized publisher tokens from a string."""
    if not publisher:
        return
    for part in re.split(r"[,;/&]|\band\b", str(publisher), flags=re.I):
        yield _norm_token(part)


def _publisher_match(publisher, known_set):
    for tok in _publisher_tokens(publisher):
        if tok in known_set:
            return True
    return False


def _refine_format(cand, query=""):
    """Refine a candidate format using publisher/language/genres/title context.

    Google Books/OpenLibrary often return generic categories; this uses
    publisher, language, subject markers, and the original release query to
    correct them without hardcoding series names.
    """
    fmt = cand.get("format")
    title = str(cand.get("title") or "")
    publisher = str(cand.get("publisher") or "")
    language = str(cand.get("language") or "").lower()
    genres = " ".join(g.lower() for g in (cand.get("genres") or []))
    combined = f"{title} {publisher} {genres}"
    combined_norm = _norm_token(combined)
    tokens = set(combined_norm.split())
    query_norm = _norm(query)
    query_tokens = set(query_norm.split())

    has_manga_marker = bool(
        tokens & _MANGA_MARKERS or query_tokens & _MANGA_MARKERS
        or _publisher_match(publisher, _MANGA_PUBLISHERS)
    )
    has_french = (
        "fr" in language or "fre" in language or "french" in combined_norm
        or "french" in query_norm
    )
    has_bd_marker = bool(
        tokens & _BD_MARKERS or query_tokens & _BD_MARKERS
        or _publisher_match(publisher, _FRENCH_BD_PUBLISHERS)
    )
    has_comic_marker = bool(
        tokens & _COMIC_MARKERS or query_tokens & _COMIC_MARKERS
        or _publisher_match(publisher, _US_COMIC_PUBLISHERS)
    )
    # CBZ/CBR in the original release name is a strong visual-comics signal.
    has_cbz_cbr = bool(re.search(r"\[?cb[rz]\]?", query, re.I))

    # Manga/manhwa/webtoon take precedence (strong genre/subject signals)
    if has_manga_marker:
        if "manhwa" in tokens or "webtoon" in tokens or "manhwa" in query_tokens or "webtoon" in query_tokens:
            return "webtoon"
        return "manga"

    # If provider says "comic" but it's French + BD markers, it's likely a BD.
    if fmt == "comic" and has_french and has_bd_marker and not has_comic_marker:
        return "bd"

    # Generic "book" disambiguation
    if fmt == "book":
        if has_french and has_bd_marker:
            return "bd"
        if has_comic_marker or (has_cbz_cbr and not has_bd_marker):
            return "comic"
        if has_cbz_cbr and has_bd_marker:
            return "bd"

    return fmt


#: format → qBittorrent category
FORMAT_TO_CATEGORY = {
    "manga": "manga",
    "webtoon": "webtoon",
    "comic": "comics",
    "bd": "bd",
    "book": "ebooks",
    "light_novel": "light-novel",
}


def _lookup_with_timeout(provider, title, timeout):
    """Run a single provider.lookup() in a thread; return candidate or None."""
    result = [None]
    def _run():
        try:
            result[0] = provider.lookup(title)
        except Exception as e:
            log.debug("%s lookup error: %s", provider.id, e)
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    return result[0]


def _query_providers(providers, title, per_call_timeout, deadline):
    """Query a provider list, tally votes, return (votes, best_by_cat).

    Shared by the targeted and fallback phases of lookup_category.
    """
    votes = {}   # category -> [provider_ids]
    best_by_cat = {}
    for p in providers:
        if time.time() > deadline:
            log.debug("Metadata lookup deadline reached for %r", title)
            break
        cand = _lookup_with_timeout(p, title, per_call_timeout)
        if not cand or not cand.get("format"):
            continue
        # Disambiguate generic "book" using publisher/language/genres + original query.
        cand["format"] = _refine_format(cand, query=title)
        cat = FORMAT_TO_CATEGORY.get(cand["format"])
        if not cat:
            continue
        cand["provider"] = p.id
        votes.setdefault(cat, []).append(p.id)
        cur = best_by_cat.get(cat)
        if cur is None or cand.get("confidence", 0) > cur.get("confidence", 0):
            best_by_cat[cat] = cand
        # Early-exit: 2 independent providers agree -> consensus reached.
        if len(votes[cat]) >= 2:
            break
    return votes, best_by_cat


def _resolve_votes(votes, best_by_cat, providers):
    """Apply the voting/consensus logic to a set of provider votes.

    Returns (category, confidence, provider_ids, candidate_dict) where
    candidate_dict contains the winning candidate metadata (title, publisher,
    authors, artist, year, country, etc.). On unresolved returns
    (None, 0.0, None, reason) where reason is a string.
    """
    if not votes:
        return None, 0.0, None, "no provider match"

    # A category wins if >=2 providers agree on it. If only one provider
    # returned a category, accept it ONLY from fast public APIs that have a
    # single-vote trust level. FlareSolverr-dependent providers (PlaneteBD,
    # Bedetheque) can false-positive; they require a second provider to agree.
    best_cat = None
    best_count = 0
    for cat, ids in votes.items():
        if len(ids) >= 2:
            if len(ids) > best_count:
                best_cat, best_count = cat, len(ids)

    # ── Strong single-vote override ─────────────────────────────────────────
    # When no category reaches a 2-provider consensus, accept the single
    # strongest candidate if it is a high-confidence match (>=0.9) from a
    # provider that does not require corroboration. This prevents weak,
    # conflicting generic/FlareSolverr results from overriding an exact match.
    if best_cat is None:
        strong = None
        strong_score = 0.0
        for cat, cand in best_by_cat.items():
            provider_id = cand.get("provider")
            conf = cand.get("confidence", 0.0)
            provider_requires_corroboration = any(
                pp.id == provider_id and pp.requires_corroboration for pp in providers
            )
            if conf >= 0.9 and not provider_requires_corroboration and conf > strong_score:
                strong = cat
                strong_score = conf
        if strong:
            best_cat = strong

    if best_cat is None and len(votes) == 1:
        cat = next(iter(votes))
        cand = best_by_cat[cat]
        provider_id = cand.get("provider")
        provider_requires_corroboration = any(
            pp.id == provider_id and pp.requires_corroboration for pp in providers
        )
        if not provider_requires_corroboration:
            best_cat = cat
        else:
            return None, 0.0, None, f"{provider_id} single-vote; needs corroboration"

    if best_cat:
        cand = best_by_cat[best_cat]
        voters = votes[best_cat]
        conf = min(cand.get("confidence", 0.7) + 0.05 * (len(voters) - 1), 1.0)
        return best_cat, conf, "+".join(voters), cand
    # Disagreement with no 2-provider agreement -> unresolved, tag for review
    return None, 0.0, None, f"providers disagree: {dict(votes)}"


def lookup_category(title, google_books_key=None, comicvine_key=None, signals=None):
    """Query providers for a release title, return (category, confidence, provider, candidate_or_reason).

    Two-phase, signal-driven routing:

    Phase 1 (targeted): when `signals` are present, query only the providers
    relevant to those signals. A single high-confidence (>=0.9) match from a
    targeted provider is accepted immediately — no 2-provider consensus needed
    for a strong targeted hit. Otherwise the normal consensus logic applies.

    Phase 2 (fallback): if Phase 1 produced no result, query ALL providers with
    the existing voting logic. This handles wrong or missing signals.

    Each provider call is wrapped with a thread-level timeout so a single slow
    provider cannot block the whole cascade.

    Returns (None, 0.0, None, reason) if no provider resolves it, otherwise
    returns (category, confidence, provider_string, candidate_dict).
    """
    if not _META_ENABLED:
        return None, 0.0, None, "metadata disabled in config"
    if google_books_key is None:
        google_books_key = GOOGLE_BOOKS_API_KEY or None
    if comicvine_key is None:
        comicvine_key = COMICVINE_API_KEY or None

    deadline = time.time() + float(_cfg.get("metadata.timeout_seconds", 25))

    # ── Phase 1: targeted providers (signal-driven) ─────────────────────────
    if signals:
        targeted = _build_providers(google_books_key, comicvine_key, signals=signals)
        if targeted:
            per_call_timeout = float(_cfg.get("metadata.timeout_seconds", 25)) / max(len(targeted), 1)
            per_call_timeout = max(per_call_timeout, 20.0)
            votes, best_by_cat = _query_providers(targeted, title, per_call_timeout, deadline)

            # A single high-confidence match from a targeted provider is
            # accepted immediately (signals already narrowed the domain).
            if best_by_cat:
                strong_cat, strong_cand = max(
                    best_by_cat.items(), key=lambda kv: kv[1].get("confidence", 0.0)
                )
                if strong_cand.get("confidence", 0.0) >= 0.9:
                    conf = min(strong_cand.get("confidence", 0.9), 1.0)
                    return strong_cat, conf, strong_cand.get("provider"), strong_cand

            # Otherwise use the normal consensus logic on the targeted votes.
            cat, conf, prov, cand = _resolve_votes(votes, best_by_cat, targeted)
            if cat:
                return cat, conf, prov, cand

    # ── Phase 2: fallback to ALL providers (existing voting logic) ─────────
    providers = _build_providers(google_books_key, comicvine_key)
    per_call_timeout = float(_cfg.get("metadata.timeout_seconds", 25)) / max(len(providers), 1)
    per_call_timeout = max(per_call_timeout, 20.0)  # slow providers throttle 2-3s before the HTTP call

    votes, best_by_cat = _query_providers(providers, title, per_call_timeout, deadline)
    return _resolve_votes(votes, best_by_cat, providers)


# ════════════════════════════════════════════════════════════════════════
# LLM final-arbiter classification
# ════════════════════════════════════════════════════════════════════════
#: Allowed LLM output formats → internal category strings.
_LLM_FORMAT_TO_CATEGORY = {
    "manga": "manga",
    "manhwa": "manhwa",
    "webtoon": "webtoon",
    "manhua": "manhua",
    "comics": "comics",
    "comic": "comics",
    "bd": "bd",
    "light-novel": "light-novel",
    "light_novel": "light-novel",
    "ebooks": "ebooks",
    "ebook": "ebooks",
    "book": "ebooks",
    "audiobook": "audiobooks",
    "audiobooks": "audiobooks",
    "artbook": "artbook",
    "doujinshi": "doujinshi",
}

#: LLM settings (config.yaml `llm:` section, env-overridable).
try:
    _LLM_ENABLED = bool(_cfg.get("llm.enabled", False))
    _LLM_COOLDOWN_MINUTES = float(_cfg.get("llm.cooldown_minutes", 60) or 60)
except Exception:
    _LLM_ENABLED = False
    _LLM_COOLDOWN_MINUTES = 60.0

#: Legacy single-provider config (kept for backward compatibility).
try:
    _LEGACY_LLM_ENDPOINT = str(_cfg.get("llm.endpoint", "") or "").strip()
    _LEGACY_LLM_MODEL = str(_cfg.get("llm.model", "") or "").strip()
    _LEGACY_LLM_API_KEY = str(_cfg.get("llm.api_key", "") or "").strip()
    _LEGACY_LLM_TIMEOUT = float(_cfg.get("llm.timeout", 30) or 30)
except Exception:
    _LEGACY_LLM_ENDPOINT = ""
    _LEGACY_LLM_MODEL = ""
    _LEGACY_LLM_API_KEY = ""
    _LEGACY_LLM_TIMEOUT = 30.0

#: Unix timestamp until which the LLM is temporarily disabled after a quota/rate-limit error.
_llm_cooldown_until = 0.0


def _load_llm_providers():
    """Return a list of provider dicts from config/env.

    Preferred order:
      1. LLM_PROVIDERS env var (JSON list)
      2. llm.providers list from config
      3. Legacy llm.endpoint/model/api_key/timeout from config

    Each provider dict has: endpoint, model, api_key, timeout, id.
    Provider IDs are generated as "provider-host-N" (e.g. "gemini-1", "openrouter-2").
    """
    import json

    env_providers = os.environ.get("LLM_PROVIDERS", "").strip()
    if env_providers:
        try:
            parsed = json.loads(env_providers)
            if isinstance(parsed, list) and parsed:
                providers = []
                for p in parsed:
                    if not isinstance(p, dict):
                        continue
                    providers.append({
                        "endpoint": str(p.get("endpoint") or "").strip(),
                        "model": str(p.get("model") or "").strip(),
                        "api_key": str(p.get("api_key") or p.get("apiKey") or "").strip(),
                        "timeout": float(p.get("timeout", 30) or 30),
                    })
                if providers:
                    return _normalize_provider_ids(providers)
        except Exception:
            log.warning("Could not parse LLM_PROVIDERS env var as JSON list")

    cfg_providers = _cfg.get("llm.providers", []) or []
    if isinstance(cfg_providers, list) and cfg_providers:
        providers = []
        for p in cfg_providers:
            if not isinstance(p, dict):
                continue
            providers.append({
                "endpoint": str(p.get("endpoint") or "").strip(),
                "model": str(p.get("model") or "").strip(),
                "api_key": str(p.get("api_key") or p.get("apiKey") or "").strip(),
                "timeout": float(p.get("timeout", 30) or 30),
            })
        if providers:
            # Env overrides for the first provider only.
            if os.environ.get("LLM_ENDPOINT"):
                providers[0]["endpoint"] = os.environ["LLM_ENDPOINT"].strip()
            if os.environ.get("LLM_MODEL"):
                providers[0]["model"] = os.environ["LLM_MODEL"].strip()
            if os.environ.get("LLM_API_KEY"):
                providers[0]["api_key"] = os.environ["LLM_API_KEY"].strip()
            if os.environ.get("LLM_TIMEOUT"):
                try:
                    providers[0]["timeout"] = float(os.environ["LLM_TIMEOUT"])
                except Exception:
                    pass
            return _normalize_provider_ids(providers)

    # Legacy single-provider fallback.
    endpoint = _LEGACY_LLM_ENDPOINT
    model = _LEGACY_LLM_MODEL
    api_key = _LEGACY_LLM_API_KEY
    timeout = _LEGACY_LLM_TIMEOUT
    if os.environ.get("LLM_ENDPOINT"):
        endpoint = os.environ["LLM_ENDPOINT"].strip()
    if os.environ.get("LLM_MODEL"):
        model = os.environ["LLM_MODEL"].strip()
    if os.environ.get("LLM_API_KEY"):
        api_key = os.environ["LLM_API_KEY"].strip()
    if os.environ.get("LLM_TIMEOUT"):
        try:
            timeout = float(os.environ["LLM_TIMEOUT"])
        except Exception:
            pass
    if endpoint and model:
        return _normalize_provider_ids([{
            "endpoint": endpoint,
            "model": model,
            "api_key": api_key,
            "timeout": timeout,
        }])
    return []


def _normalize_provider_ids(providers):
    """Assign short generated IDs to a list of provider dicts."""
    out = []
    for i, p in enumerate(providers, start=1):
        endpoint = p.get("endpoint", "")
        host = "provider"
        if endpoint:
            host = (urllib.parse.urlparse(endpoint).hostname or "provider").lower()
            # Strip common API subdomains for readability.
            host = host.replace("generativelanguage.googleapis.com", "gemini")
            host = host.replace("openrouter.ai", "openrouter")
            host = re.sub(r"[^a-z0-9]+", "-", host).strip("-")
            host = host[:20]
        p = dict(p)
        p["id"] = f"{host}-{i}"
        out.append(p)
    return out


def _sanitize_for_prompt(text: str, max_len: int = 300) -> str:
    """Escape JSON/control characters and normalize a release name for LLM prompts.

    Prevents a malicious or malformed release name from injecting JSON structure
    into the prompt and normalizes dots/underscores/hyphens between words to
    spaces, similar to how video release parsers clean titles.
    """
    text = str(text or "")[:max_len]
    # Neutralize curly braces and backticks so the model can't be tricked into
    # emitting JSON by the raw input alone.
    text = text.replace("{", "[").replace("}", "]")
    text = text.replace("`", "'")
    # Normalize separators between words: dots/underscores/hyphens become spaces,
    # but keep hyphens attached to words where they likely belong (e.g. "re-release").
    text = re.sub(r"(?<=\w)[._](?=\w)", " ", text)
    text = re.sub(r"(?<=\w)-(?=\d{4}\b)", " ", text)  # "Title-2025" → "Title 2025"
    text = re.sub(r"(?<=\d{4})-(?=\w)", " ", text)   # "2025-Title" → "2025 Title"
    # Collapse multiple whitespace.
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _llm_enabled():
    """Whether the LLM final-arbiter is enabled (env/config) and not in cooldown."""
    if not _LLM_ENABLED:
        return False
    providers = _load_llm_providers()
    if not providers:
        log.debug("LLM enabled but no providers configured; skipping")
        return False
    if time.time() < _llm_cooldown_until:
        remaining = int(_llm_cooldown_until - time.time())
        log.debug("LLM in cooldown for %ss; skipping", remaining)
        return False
    return True


def _strip_markdown_fences(text):
    """Remove ```json ... ``` fences (and stray backticks) from an LLM reply."""
    t = str(text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _parse_llm_json(text):
    """Robustly parse a JSON object out of an LLM reply.

    Tries json.loads directly, then strips markdown fences, then falls back
    to extracting the first balanced {...} block.
    """
    t = _strip_markdown_fences(text)
    if not t:
        return None
    try:
        data = json.loads(t)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    # Fallback: find the first balanced {...} block.
    start = t.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(t)):
        if t[i] == "{":
            depth += 1
        elif t[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(t[start:i + 1])
                    if isinstance(data, dict):
                        return data
                except Exception:
                    pass
                break
    return None


def _redact_url_secret(url_or_msg: str, key: str) -> str:
    """Return a copy with the API key stripped for safe logging."""
    if not key:
        return url_or_msg
    text = str(url_or_msg)
    # Strip the key whether it appears raw or URL-encoded.
    for variant in (key, urllib.parse.quote(key, safe="")):
        text = text.replace(variant, "***")
    return text


def _llm_request_for_provider(payload, provider, retries=2):
    """POST a chat payload to a single provider and return the result.

    Handles backend selection (Gemini, OpenAI-compatible, Ollama) and per-provider
    transient retries. Returns a dict:
        {
            "ok": bool,
            "text": raw response text (only if ok),
            "status": HTTP status code or 0,
            "error": exception / message string or None,
            "continue": bool,  # True if caller should try the next provider
        }
    """
    endpoint = provider.get("endpoint", "")
    model = provider.get("model", "")
    api_key = provider.get("api_key", "")
    timeout = float(provider.get("timeout", 30) or 30)
    provider_id = provider.get("id", "provider")
    headers = {"Content-Type": "application/json"}

    def _result(ok, text=None, status=0, error=None, continue_=True):
        return {"ok": ok, "text": text, "status": status, "error": error, "continue": continue_}

    def _is_ratelimit(status, err_text):
        return status in (429, 403) or any(k in err_text for k in ("quota", "rate limit", "billing", "exhausted", "insufficient_quota"))

    # ── Gemini v1beta native API ─────────────────────────────────────────
    if "generativelanguage.googleapis.com" in endpoint:
        prompt_text = ""
        for msg in payload.get("messages", []):
            if msg.get("role") == "user":
                prompt_text = msg.get("content", "")
                break
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt_text}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.0,
                "maxOutputTokens": 300,
            },
        }
        sep = "&" if "?" in endpoint else "?"
        url = f"{endpoint}{sep}key={api_key}" if api_key else endpoint
        last_err = None
        last_status = 0
        for attempt in range(retries + 1):
            try:
                resp = _requests.post(url, json=body, headers=headers, timeout=timeout)
                resp.raise_for_status()
                data = resp.json()
                candidates = data.get("candidates") or []
                if candidates:
                    parts = (candidates[0].get("content") or {}).get("parts") or []
                    if parts and parts[0].get("text"):
                        return _result(True, text=parts[0]["text"])
                log.warning("%s response had no candidates/content for prompt", provider_id)
                return _result(False, status=0, error="no candidates/content", continue_=True)
            except Exception as e:
                last_err = e
                last_status = getattr(getattr(e, "response", None), "status_code", 0)
                err_text = str(getattr(getattr(e, "response", None), "text", "") or "").lower()
                if _is_ratelimit(last_status, err_text):
                    log.debug("%s rate-limited/quota (HTTP %s); trying next provider", provider_id, last_status or "?")
                    return _result(False, status=last_status, error=str(e), continue_=True)
                if last_status in (502, 503, 504) and attempt < retries:
                    sleep_s = 1.5 * (2 ** attempt)
                    log.debug("%s transient error HTTP %s; retrying in %.1fs", provider_id, last_status, sleep_s)
                    time.sleep(sleep_s)
                    continue
                break
        safe_endpoint = _redact_url_secret(endpoint, api_key)
        log.debug("%s request to %s failed: %s", provider_id, safe_endpoint, last_err)
        return _result(False, status=last_status, error=str(last_err), continue_=True)

    # ── OpenAI-compatible / Ollama ─────────────────────────────────────────
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    # OpenRouter requires referer/app headers; add them when talking to openrouter.ai.
    if "openrouter.ai" in endpoint:
        headers.setdefault("HTTP-Referer", "https://github.com/randrini/qbithardlink")
        headers.setdefault("X-Title", "qbithardlink")

    if endpoint.rstrip("/").endswith("/v1/chat/completions"):
        body = {
            "model": model,
            "messages": payload.get("messages"),
            "temperature": 0.0,
            "max_tokens": 300,
        }
    else:
        # Ollama native /api/chat
        body = {
            "model": model,
            "messages": payload.get("messages"),
            "stream": False,
            "options": {"temperature": 0.0, "num_predict": 300},
        }
    last_err = None
    last_status = 0
    for attempt in range(retries + 1):
        try:
            resp = _requests.post(endpoint, json=body, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return _result(True, text=resp.text)
        except Exception as e:
            last_err = e
            last_status = getattr(getattr(e, "response", None), "status_code", 0)
            err_text = str(getattr(getattr(e, "response", None), "text", "") or "").lower()
            if _is_ratelimit(last_status, err_text):
                log.debug("%s rate-limited/quota (HTTP %s); trying next provider", provider_id, last_status or "?")
                return _result(False, status=last_status, error=str(e), continue_=True)
            if last_status in (502, 503, 504, 429) and attempt < retries:
                sleep_s = 1.5 * (2 ** attempt)
                log.debug("%s transient error HTTP %s; retrying in %.1fs", provider_id, last_status, sleep_s)
                time.sleep(sleep_s)
                continue
            # Log 4xx response body to make provider-specific errors debuggable.
            if last_status in (400, 401, 404, 422) and getattr(getattr(e, "response", None), "text", None):
                err_text_short = e.response.text[:400]
                log.warning("%s HTTP %s response: %s", provider_id, last_status, err_text_short)
            break
    safe_endpoint = _redact_url_secret(endpoint, api_key)
    log.debug("%s request to %s failed: %s", provider_id, safe_endpoint, last_err)
    return _result(False, status=last_status, error=str(last_err), continue_=True)


def _is_llm_rate_limit_error(status, error):
    """Return True if a final failure looks like a rate-limit / quota error."""
    err_text = str(error or "").lower()
    return (
        status in (429, 403)
        or any(k in err_text for k in ("quota", "rate limit", "billing", "exhausted", "insufficient_quota"))
    )


def _llm_request(payload, retries=2):
    """POST a chat payload to each configured provider until one succeeds.

    Iterates over providers from `_load_llm_providers()`. On rate-limit / quota
    for a provider, logs at debug and tries the next. On transient failures, retries
    within the provider up to `retries` times, then moves to the next provider.
    If all providers fail due to rate-limit, sets a global cooldown. Otherwise
    returns None and logs the last provider error.
    """
    global _llm_cooldown_until
    if not HAS_REQUESTS:
        log.warning("LLM classification requested but `requests` is unavailable")
        return None

    providers = _load_llm_providers()
    if not providers:
        log.debug("No LLM providers configured; skipping")
        return None

    all_rate_limited = True
    last_error = None
    last_status = 0
    last_provider_id = None

    for provider in providers:
        result = _llm_request_for_provider(payload, provider, retries=retries)
        if result["ok"]:
            return result["text"]
        last_status = result.get("status") or 0
        last_error = result.get("error")
        last_provider_id = provider.get("id", "provider")
        if not _is_llm_rate_limit_error(last_status, last_error):
            all_rate_limited = False
        if result.get("continue"):
            continue
        # Provider signalled a non-continue failure (should not happen in current impl).
        break

    if all_rate_limited:
        _llm_cooldown_until = time.time() + (_LLM_COOLDOWN_MINUTES * 60)
        log.warning(
            "All LLM providers rate-limited/quota (last HTTP %s). Cooling down for %.0f minutes.",
            last_status or "?", _LLM_COOLDOWN_MINUTES,
        )
    else:
        safe_msg = _redact_url_secret(str(last_error or "unknown error"), "")
        log.warning("LLM request failed for all providers (last %s): %s", last_provider_id or "provider", safe_msg)
    return None


def _llm_extract_content(raw_text):
    """Extract the assistant message text or direct JSON object from an LLM response.

    Handles:
    - OpenAI-compatible: choices[0].message.content
    - Ollama /api/chat: message.content
    - Ollama /api/generate: response
    - Gemini v1beta with responseMimeType=application/json: the response body
      IS the JSON object directly, not wrapped in candidates[].content.parts[].
    """
    if not raw_text:
        return None
    try:
        data = json.loads(raw_text)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    # LLM returned the JSON object we asked for directly (any backend).
    # We require at least "format"; "sources" is optional and will default to [].
    if "format" in data:
        return raw_text
    # OpenAI-compatible: choices[0].message.content
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        if content:
            return content
    # Gemini v1beta native when responseMimeType is NOT application/json or when
    # the model still wraps JSON text inside candidates[].content.parts[].text.
    candidates = data.get("candidates")
    if isinstance(candidates, list) and candidates:
        parts = (candidates[0].get("content") or {}).get("parts") or []
        if parts and parts[0].get("text"):
            return parts[0]["text"]
    # Ollama /api/chat: message.content
    msg = data.get("message") or {}
    if msg.get("content"):
        return msg["content"]
    # Ollama /api/generate: response
    if data.get("response"):
        return data["response"]
    return None


def llm_classify(title, files=None, signals=None, preliminary=None):
    """LLM classification / verification.

    When `preliminary` is None, acts as a final-arbiter: builds a short prompt
    from the cleaned title, file list, and detected signals, then asks the
    configured LLM for a JSON verdict: {"format": "...", "sources": ["..."]}.

    When `preliminary` is provided (dict with category/reasons/confidence), the
    LLM is asked to verify that verdict and return the same JSON. If it
    disagrees, the LLM's format is returned; if it agrees, the LLM returns the
    same format plus confirmation sources.

    Returns (category, confidence, reasons) or (None, 0.0, []) when disabled,
    unreachable, or the response is invalid. Confidence is fixed at 0.85 for
    an LLM override/confirmation — strong but not authoritative.
    """
    if not _llm_enabled():
        return None, 0.0, []

    # ── Build the prompt ────────────────────────────────────────────────────
    file_exts = []
    for f in files or []:
        path = str(f.get("name") or "") if isinstance(f, dict) else str(f or "")
        ext = os.path.splitext(path)[1].lower()
        if ext:
            file_exts.append(ext)
    # De-duplicate while preserving order, cap the list.
    seen = set()
    exts = []
    for e in file_exts:
        if e not in seen:
            seen.add(e)
            exts.append(e)
    exts_str = ", ".join(exts[:15]) if exts else "unknown"

    signals = signals or {}
    sig_parts = []
    for key in ("manga", "comics", "bd", "light_novel", "audiobook", "french"):
        if signals.get(key):
            sig_parts.append(key)
    matched = signals.get("matched") or []
    if matched:
        sig_parts.append("matched:" + ",".join(str(m) for m in matched[:10]))
    signals_str = ", ".join(sig_parts) if sig_parts else "none"
    safe_title = _sanitize_for_prompt(title)

    if preliminary:
        safe_prelim_cat = _sanitize_for_prompt(preliminary["category"])
        safe_reasons = _sanitize_for_prompt(str(preliminary["reasons"]), max_len=500)
        prompt = (
            "You verify book/comics torrent classifications. The classifier "
            f"proposed '{safe_prelim_cat}' (confidence {preliminary['confidence']:.2f}) "
            f"for reasons: {safe_reasons}.\n"
            f"Raw release name: {safe_title}\n"
            f"Files: {exts_str}\n"
            f"Signals: {signals_str}\n"
            "Return JSON {\"format\":\"...\",\"sources\":[\"...\"]}. "
            "Allowed formats: manga, manhwa, webtoon, manhua, comics, bd, light-novel, ebooks, audiobooks, artbook, doujinshi.\n"
            "Definitions:\n"
            "  'comics' = US/UK comic books (Marvel, DC, Image, Dark Horse, IDW, Boom, Valiant, Titan, Dynamite, Avatar, Oni, Vertigo, etc.) and game/novel adaptations in US comic format, regardless of language or publisher.\n"
            "  'bd' = ORIGINAL Franco-Belgian bande dessinée only (Tintin, Astérix, Lucky Luke, Blacksad, etc.). NOT a French edition of a foreign work.\n"
            "  'manga' = Japanese manga, even in French translation.\n"
            "CLASSIFICATION RULE — apply in this exact order:\n"
            "1. If the series/character, creator, or original publisher is from a US/UK publisher (Marvel, DC, Image, Dark Horse, IDW, Boom, Valiant, Titan, Dynamite, Avatar, Oni, Vertigo) OR is a well-known US superhero/horror/action property (Superman, Batman, Spider-Man, X-Men, Avengers, Bloodborne, The Witcher, etc.), return 'comics' even if the release is in French and published by Panini, Urban Comics, Delcourt, or Glénat.\n"
            "2. If the work is originally Japanese manga/manhwa/webtoon, return 'manga'/'manhwa'/'webtoon' even if translated to French.\n"
            "3. Return 'bd' ONLY if the work is an original Franco-Belgian creation that originated in France/Belgium as bande dessinée.\n"
            "4. Otherwise return the format that matches the original work's country and medium.\n"
            "CRITICAL: French language, 'Tome', French publisher names, and French titles are NEVER enough on their own for 'bd'. Many US comics and manga are translated to French and published by French imprints. Always determine the original work's origin, not the edition language.\n"
            "If the proposed category matches the real format, return that format with sources confirming why. "
            "If it is wrong, return the correct format with sources. "
            "Be concise. A source can be 'title contains ...', 'publisher ...', 'series is known as ...', etc."
        )
    else:
        prompt = (
            "You classify book/comics torrents. Identify the actual book/comic from the release name and "
            "return JSON {\"format\":\"...\",\"sources\":[\"...\"]}.\n"
            "Allowed formats: manga, manhwa, webtoon, manhua, comics, bd, light-novel, ebooks, audiobooks, artbook, doujinshi.\n"
            "Definitions:\n"
            "  'comics' = US/UK comic books (Marvel, DC, Image, Dark Horse, IDW, Boom, Valiant, Titan, Dynamite, Avatar, Oni, Vertigo, etc.) and game/novel adaptations in US comic format, regardless of language or publisher.\n"
            "  'bd' = ORIGINAL Franco-Belgian bande dessinée only (Tintin, Astérix, Lucky Luke, Blacksad, etc.). NOT a French edition of a foreign work.\n"
            "  'manga' = Japanese manga, even in French translation.\n"
            "CLASSIFICATION RULE — apply in this exact order:\n"
            "1. If the series/character, creator, or original publisher is from a US/UK publisher (Marvel, DC, Image, Dark Horse, IDW, Boom, Valiant, Titan, Dynamite, Avatar, Oni, Vertigo) OR is a well-known US superhero/horror/action property (Superman, Batman, Spider-Man, X-Men, Avengers, Bloodborne, The Witcher, etc.), return 'comics' even if the release is in French and published by Panini, Urban Comics, Delcourt, or Glénat.\n"
            "2. If the work is originally Japanese manga/manhwa/webtoon, return 'manga'/'manhwa'/'webtoon' even if translated to French.\n"
            "3. Return 'bd' ONLY if the work is an original Franco-Belgian creation that originated in France/Belgium as bande dessinée.\n"
            "4. Otherwise return the format that matches the original work's country and medium.\n"
            "CRITICAL: French language, 'Tome', French publisher names, and French titles are NEVER enough on their own for 'bd'. Many US comics and manga are translated to French and published by French imprints. Always determine the original work's origin, not the edition language.\n"
            f"Raw release name: {safe_title}\n"
            f"Files: {exts_str}\n"
            f"Signals: {signals_str}\n"
            "Use your knowledge of published works. Be concise."
        )

    raw = _llm_request({"messages": [{"role": "user", "content": prompt}]})
    content = _llm_extract_content(raw)
    if not content:
        log.warning("LLM returned no usable content for %r", title)
        return None, 0.0, []

    data = _parse_llm_json(content)
    if not data:
        log.warning("LLM returned unparseable JSON for %r: %r", title, content[:200])
        return None, 0.0, []

    fmt = str(data.get("format") or "").strip().lower()
    cat = _LLM_FORMAT_TO_CATEGORY.get(fmt)
    if not cat:
        log.warning("LLM returned unknown format %r for %r", fmt, title)
        return None, 0.0, []

    sources = data.get("sources") or []
    if isinstance(sources, str):
        sources = [sources]
    sources = [str(s).strip() for s in sources if str(s).strip()]
    reasons = [f"llm:{cat} → " + ", ".join(sources) if sources else f"llm:{cat}"]
    return cat, 0.85, reasons


if __name__ == "__main__":
    import sys
    for t in sys.argv[1:]:
        cat, conf, prov, cand = lookup_category(t)
        title = cand.get("title") if isinstance(cand, dict) else cand
        print(f"{t!r} → {cat} (conf={conf:.2f}, provider={prov}, title={title!r})")
