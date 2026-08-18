#!/bin/bash
# qBittorrent "Run external program on torrent completion" script.
# Args: %N (name) %F (content path / root path) %L (category)
#
# Maps category → library destination and hardlinks the completed content
# into the media library. The torrent-side file keeps seeding; the library
# hardlink shares the same inode, so deleting the torrent later does not
# break the library copy.
#
# Handles:
#   - single-file torrents (path to file)
#   - folder torrents (path to folder)
#   - already-imported detection (skip if the library copy already exists)
#   - cross-device fallback (hardlink fails → copy, with a warning)
set -uo pipefail

torrentName="$1"
torrentPath="$2"
torrentCategory="$3"

# Persist hardlink.log alongside classifier.log (both under /app/logs in Docker).
logFile="${HARDLINK_LOG:-$(dirname "$0")/logs/hardlink.log}"
mkdir -p "$(dirname "$logFile")"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$logFile"; }

# Categories managed by the *Arr apps or video content — never hardlink these.
excludedCategories="radarr,radarranime,sonarr,sonarranime,lidarr,readarr,movies,moviesanime,tv,tvanime"
if [[ ",$excludedCategories," == *",$torrentCategory,"* ]]; then
  log "[!] Skipped \"${torrentName}\" (excluded category ${torrentCategory})"
  exit 0
fi

# Category → library destination (source of truth).
# LIBRARY_ROOT is the canonical env var; MEDIA_ROOT is accepted as legacy fallback.
MEDIA_ROOT="${LIBRARY_ROOT:-${MEDIA_ROOT:-/data/books/library}}"
case "$torrentCategory" in
  manga)        destDir="$MEDIA_ROOT/manga" ;;
  manhwa)       destDir="$MEDIA_ROOT/manhwa" ;;
  webtoon)      destDir="$MEDIA_ROOT/webtoon" ;;
  comics)       destDir="$MEDIA_ROOT/comics" ;;
  bd)           destDir="$MEDIA_ROOT/bd" ;;
  light-novel)  destDir="$MEDIA_ROOT/light-novel" ;;
  ebooks)       destDir="$MEDIA_ROOT/ebooks" ;;
  books)        destDir="$MEDIA_ROOT/_unprocessed" ;;
  *)            log "[!] Unknown category \"$torrentCategory\" — skipping"; exit 0 ;;
esac

# Validate source exists.
if [[ ! -e "$torrentPath" ]]; then
  log "[x] Source does not exist: ${torrentPath}"
  exit 1
fi

mkdir -p -- "$destDir"

# Determine the destination name: for a folder, use the folder name; for a
# single file, use the filename.
srcBase="$(basename "$torrentPath")"

# Kavita/Komga expect library root folders to contain only subdirectories, not
# loose files. If the source is a single file, wrap it in a folder named after
# the file (without extension) so the library stays series-folder clean.
if [[ -f "$torrentPath" ]]; then
  wrapDir="${srcBase%.*}"
  mkdir -p -- "$destDir/$wrapDir"
  destPath="$destDir/$wrapDir/$srcBase"
else
  destPath="$destDir/$srcBase"
fi

# Already-imported detection: if the destination already exists, skip.
if [[ -e "$destPath" ]]; then
  log "[✔] Already imported (exists): ${destPath}"
  exit 0
fi

# Hardlink. cp -l creates hardlinks (same inode) instead of copies.
# Keep stderr visible to the caller (daemon captures it) while also logging summary.
if [[ -f "$torrentPath" ]]; then
  if cp -l -- "$torrentPath" "$destPath"; then
    log "[✔] Hardlinked \"${torrentName}\" → ${destPath}"
    exit 0
  fi
else
  if cp -rl -- "$torrentPath" "$destDir/"; then
    log "[✔] Hardlinked \"${torrentName}\" → ${destPath}"
    exit 0
  fi
fi

# Cross-device fallback: hardlink failed (likely different filesystems).
# Fall back to a copy so the library still gets the file, but warn loudly.
log "[!] Hardlink failed for \"${torrentName}\"; falling back to copy"
if [[ -f "$torrentPath" ]]; then
  mkdir -p -- "$(dirname "$destPath")"
  if cp -- "$torrentPath" "$destPath"; then
    log "[!] Hardlink failed (cross-device?) — COPIED instead: ${destPath}"
    log "    WARNING: this duplicates disk space. Ensure torrents and library are on the SAME filesystem."
    exit 0
  fi
else
  if cp -r -- "$torrentPath" "$destDir/"; then
    log "[!] Hardlink failed (cross-device?) — COPIED instead: ${destPath}"
    log "    WARNING: this duplicates disk space. Ensure torrents and library are on the SAME filesystem."
    exit 0
  fi
fi

log "[x] Failed to import \"${torrentName}\" (hardlink and copy both failed)"
exit 1
