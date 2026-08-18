#!/bin/bash
# qBittorrent classifier — Docker entrypoint
# Runs as root briefly to fix bind-mount permissions, then drops to appuser.
set -euo pipefail

PUID="${PUID:-99}"
PGID="${PGID:-100}"

# Ensure the appuser/group exists (created at image build time, but re-check
# in case the host UID/GID differ from the image defaults).
if ! getent group "$PGID" >/dev/null 2>&1; then
    groupadd -g "$PGID" appgroup
fi
if ! getent passwd "$PUID" >/dev/null 2>&1; then
    useradd -u "$PUID" -g "$PGID" -d /app -s /bin/bash appuser
fi

# Single shared bind mount: /data/books contains both qBittorrent downloads
# and the media library. Hardlinks only work when source and destination are
# inside the same filesystem as seen by the container.
BOOKS_MOUNT="/data/books"
DOWNLOAD_PATH="${DOWNLOAD_PATH:-/data/books/torrents}"
LIBRARY_ROOT="${LIBRARY_ROOT:-/data/books/library}"

# Auto-create the folder structure expected by qBittorrent and hardlink.sh.
mkdir -p /app/logs /app/state "${BOOKS_MOUNT}" "${DOWNLOAD_PATH}" "${LIBRARY_ROOT}"

# Fix ownership of writable paths.
if ! chown -R "${PUID}:${PGID}" /app/logs /app/state "${BOOKS_MOUNT}"; then
    # On shares with root_squash (NFS / Unraid user-shares), root can't chown.
    # Fall back to making directories group-writable.
    echo "[entrypoint] chown failed (likely root_squash); falling back to chmod 2775"
    chmod -R 2775 /app/logs /app/state "${BOOKS_MOUNT}" || true
fi

# Export paths for hardlink.sh.
export LIBRARY_ROOT MEDIA_ROOT="${LIBRARY_ROOT}"
export HARDLINK_LOG="/app/logs/hardlink.log"

echo "[entrypoint] Running as ${PUID}:${PGID}; books mount: ${BOOKS_MOUNT} ($(stat -c '%A %U:%G' "${BOOKS_MOUNT}"))"
echo "[entrypoint] Downloads: ${DOWNLOAD_PATH} | Library: ${LIBRARY_ROOT}"

# Run the classifier as the configured unprivileged user.
exec gosu "${PUID}:${PGID}" "$@"
