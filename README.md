# qbithardlink — qBittorrent Book/Comic Classifier

Automatically classify qBittorrent torrents as **manga / webtoon / comics / bd / light-novel / ebooks / mags / audiobooks** and hardlink them into a library folder.

## Quick Start

```bash
# 1. Clone
git clone https://github.com/randrini/qbithardlink.git
cd qbithardlink

# 2. Configure secrets
cp .env.example .env
nano .env          # set QB_PASS, API keys, paths
cp config.local.yaml.example config.local.yaml
nano config.local.yaml  # optional: multi-provider LLM cascade

# 3. Build & run
docker compose up -d --build

# 4. Reprocess existing torrents
docker exec qbit-classifier python /app/reprocess_all_books.py
```

## What it does

- Watches a qBittorrent category (default: `books`).
- Classifies each torrent by metadata + regex signals + optional LLM verification.
- Hardlinks completed downloads into `/data/books/library/<category>/`.
- Supports Unraid, single-share layout, and qBittorrent bypass-auth.

## Configuration

### Required `.env` variables

| Variable | Purpose |
|----------|---------|
| `QB_PASS` | qBittorrent WebUI password |
| `BOOKS_SHARE_PATH` | Host path mounted at `/data/books` |
| `BOOKS_SHARE_PATH` | Host path mounted at `/data/books` (must contain both downloads and library) |

### Optional API keys

| Variable | Used for |
|----------|----------|
| `GOOGLE_BOOKS_API_KEY` | Ebook/comic metadata lookups |
| `COMICVINE_API_KEY` | US comic metadata |
| `LANGSEARCH_API_KEY` | Web-search context for LLM |
| `OLLAMA_API_KEY` | Ollama Cloud LLM + web search |

### Multi-provider LLM cascade (`config.local.yaml`)

```yaml
llm:
  enabled: true
  mode: "verify"          # "verify" = always confirm; "fallback" = only when uncertain
  providers:
    - endpoint: "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent"
      model: "gemini-flash-latest"
      api_key: "YOUR_GEMINI_KEY"
      timeout: 30
    - endpoint: "https://openrouter.ai/api/v1/chat/completions"
      model: "google/gemma-4-26b-a4b-it:free"
      api_key: "YOUR_OPENROUTER_KEY"
      timeout: 30
    - endpoint: "https://ollama.com/api/chat"
      model: "gpt-oss:120b"
      api_key: "YOUR_OLLAMA_API_KEY"
      timeout: 60
  delay_seconds: 5.0
  cooldown_minutes: 1
```

Providers are tried in order. If one is rate-limited or fails, the next is used.

## Usage

### Reprocess all existing book/comic torrents

```bash
docker exec qbit-classifier python /app/reprocess_all_books.py
```

### Disable LLM for one run

```bash
docker exec qbit-classifier python /app/reprocess_all_books.py --no-llm
```

### Reprocess only one category

```bash
docker exec qbit-classifier python /app/reprocess_all_books.py --category=ebooks
```

## Layout

```
/mnt/user/data/books/
├── torrents/          # qBittorrent download location
└── library/
    ├── manga/
    ├── comics/
    ├── bd/
    ├── light-novel/
    ├── ebooks/
    ├── mags/
    └── audiobooks/
```

Both directories must live on the same filesystem (same Unraid share) for hardlinks to work.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `LLM returned no usable content` | All providers failed; check quotas or switch to Ollama Cloud |
| `429` rate limits | Add more providers or increase `delay_seconds` |
| `400 Bad Request` from Gemini | Make sure no legacy `LLM_API_KEY` is overriding the provider list |
| Misclassified title | Add tokens to `config.yaml` rules or US-comic-origin list |

## Files

- `classifier.py` — main classification engine
- `metadata.py` — metadata providers + LLM integration
- `config.yaml` — default config and regex rules
- `config.local.yaml` — local overrides (git-ignored)
- `reprocess_all_books.py` — one-shot reclassification script
