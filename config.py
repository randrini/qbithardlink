#!/usr/bin/env python3
"""Configuration loader for the qBittorrent classifier.

Loads config.yaml (or CONFIG_PATH env override) and exposes typed accessors.
Secrets can be overridden via env vars (QB_PASS, GOOGLE_BOOKS_API_KEY, ...).
"""
import json
import logging
import os
import re

try:
    import yaml
    HAS_YAML = True
except Exception:
    HAS_YAML = False

log = logging.getLogger("config")

_CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("CONFIG_PATH", os.path.join(_CONFIG_DIR, "config.yaml"))
LOCAL_CONFIG_PATH = os.environ.get("LOCAL_CONFIG_PATH", os.path.join(_CONFIG_DIR, "config.local.yaml"))

#: Defaults used when config.yaml is missing or a key is absent.
DEFAULTS = {
    "qb": {
        "url": "http://192.168.1.116:8084",
        "user": "bidalos",
        "password": "",  # must be set via QB_PASS env or config.local.yaml
        "source_category": "books",
    },
    "library": {"root": "/data/books/library"},
    "hardlink": {"script": "/app/hardlink.sh", "enabled": True},
    "log": {"file": "/app/logs/classifier.log", "level": "INFO"},
    "poll_interval": 10,
    "thresholds": {"auto": 0.90, "review": 0.70},
    "default_category": "ebooks",
    "metadata": {
        "enabled": True,
        "timeout_seconds": 45,
        "flaresolverr_url": "",
        "flaresolverr_retries": 3,
        "flaresolverr_backoff_seconds": 2.0,
        "google_books_api_key": "",
        "comicvine_api_key": "",
        "langsearch_api_key": "",
        "ollama_api_key": "",
        "providers": {},
    },
    "llm": {
        "enabled": False,
        "mode": "fallback",  # "fallback" = only when cascade is uncertain; "verify" = always check
        # Legacy single-provider keys (still supported)
        "endpoint": "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent",
        "model": "gemini-flash-latest",
        "api_key": "",
        "timeout": 30,
        # New multi-provider list. If non-empty it overrides the legacy keys above.
        "providers": [],
        "delay_seconds": 10.0,
        "cooldown_minutes": 5,
    },
    "tag_overrides": {
        "manga": "manga", "manhwa": "manhwa", "webtoon": "webtoon",
        "comics": "comics", "bd": "bd", "light-novel": "light-novel",
        "ebooks": "ebooks", "mags": "mags", "audiobooks": "audiobooks",
    },
    "rules": {},
}


def _deep_merge(base, override):
    """Recursively merge override into base (override wins)."""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_yaml(path):
    """Load one yaml file if it exists, return {} otherwise."""
    if not HAS_YAML:
        return {}
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning("could not load %s: %s", path, e)
        return {}


def load_config():
    """Load config.yaml merged with config.local.yaml over DEFAULTS."""
    cfg = _deep_merge(DEFAULTS, {})
    cfg = _deep_merge(cfg, _load_yaml(CONFIG_PATH))
    cfg = _deep_merge(cfg, _load_yaml(LOCAL_CONFIG_PATH))
    return cfg


# ── Env-var secret overrides ──────────────────────────────────────────────
def _env_override(cfg):
    """Apply env-var overrides for secrets/connection."""
    if os.environ.get("QB_URL"):
        cfg["qb"]["url"] = os.environ["QB_URL"]
    if os.environ.get("QB_USER"):
        cfg["qb"]["user"] = os.environ["QB_USER"]
    if os.environ.get("QB_PASS"):
        cfg["qb"]["password"] = os.environ["QB_PASS"]
    if os.environ.get("QB_SOURCE_CATEGORY"):
        cfg["qb"]["source_category"] = os.environ["QB_SOURCE_CATEGORY"]
    if os.environ.get("LIBRARY_ROOT"):
        cfg["library"]["root"] = os.environ["LIBRARY_ROOT"]
    if os.environ.get("HARDLINK_SCRIPT"):
        cfg["hardlink"]["script"] = os.environ["HARDLINK_SCRIPT"]
    if os.environ.get("HARDLINK_ENABLED"):
        cfg["hardlink"]["enabled"] = os.environ["HARDLINK_ENABLED"].strip().lower() in ("1", "true", "yes", "on")
    if os.environ.get("LOG_FILE"):
        cfg["log"]["file"] = os.environ["LOG_FILE"]
    if os.environ.get("LOG_LEVEL"):
        cfg["log"]["level"] = os.environ["LOG_LEVEL"].strip().upper()
    if os.environ.get("POLL_INTERVAL"):
        cfg["poll_interval"] = int(os.environ["POLL_INTERVAL"])
    if os.environ.get("FLARESOLVERR_URL"):
        cfg["metadata"]["flaresolverr_url"] = os.environ["FLARESOLVERR_URL"]
    if os.environ.get("GOOGLE_BOOKS_API_KEY"):
        cfg["metadata"]["google_books_api_key"] = os.environ["GOOGLE_BOOKS_API_KEY"]
    if os.environ.get("COMICVINE_API_KEY"):
        cfg["metadata"]["comicvine_api_key"] = os.environ["COMICVINE_API_KEY"]
    if os.environ.get("LANGSEARCH_API_KEY"):
        cfg["metadata"]["langsearch_api_key"] = os.environ["LANGSEARCH_API_KEY"]
    if os.environ.get("OLLAMA_API_KEY"):
        cfg["metadata"]["ollama_api_key"] = os.environ["OLLAMA_API_KEY"]
    if os.environ.get("LLM_ENABLED"):
        cfg["llm"]["enabled"] = os.environ["LLM_ENABLED"].strip().lower() in ("1", "true", "yes", "on")
    if os.environ.get("LLM_MODE"):
        cfg["llm"]["mode"] = os.environ["LLM_MODE"].strip().lower()
    if os.environ.get("LLM_ENDPOINT"):
        cfg["llm"]["endpoint"] = os.environ["LLM_ENDPOINT"]
    if os.environ.get("LLM_MODEL"):
        cfg["llm"]["model"] = os.environ["LLM_MODEL"]
    if os.environ.get("LLM_API_KEY"):
        cfg["llm"]["api_key"] = os.environ["LLM_API_KEY"]
    if os.environ.get("LLM_TIMEOUT"):
        cfg["llm"]["timeout"] = int(os.environ["LLM_TIMEOUT"])
    if os.environ.get("LLM_COOLDOWN_MINUTES"):
        cfg["llm"]["cooldown_minutes"] = float(os.environ["LLM_COOLDOWN_MINUTES"])
    if os.environ.get("LLM_DELAY_SECONDS"):
        cfg["llm"]["delay_seconds"] = float(os.environ["LLM_DELAY_SECONDS"])
    if os.environ.get("LLM_PROVIDERS"):
        try:
            parsed = json.loads(os.environ["LLM_PROVIDERS"])
            if isinstance(parsed, list):
                cfg["llm"]["providers"] = parsed
        except Exception:
            log.warning("could not parse LLM_PROVIDERS as JSON list; ignoring env override")
    return cfg


#: Module-level singleton config (loaded once).
CONFIG = _env_override(load_config())


def get(key, default=None):
    """Dot-path accessor: get('qb.url') → value."""
    node = CONFIG
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def get_rules():
    """Return rules as [(category, [(regex, weight), ...], min_score), ...]."""
    rules = get("rules", {}) or {}
    out = []
    for cat, spec in rules.items():
        min_score = float(spec.get("min_score", 0.9))
        patterns = []
        for pat, weight in spec.get("patterns", []):
            patterns.append((pat, float(weight)))
        out.append((cat, patterns, min_score))
    return out


def get_tag_overrides():
    """Return {tag: category} for manual overrides."""
    return get("tag_overrides", {}) or {}


def get_provider_settings():
    """Return {provider_id: {enabled, rate_limit}}."""
    return get("metadata.providers", {}) or {}
