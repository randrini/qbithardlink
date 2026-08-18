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

# Create / fix ownership of paths the app needs to write to (bind mounts may
# arrive owned by root or a different host user).
LIBRARY_ROOT="${LIBRARY_ROOT:-/data/media/books}"
mkdir -p /app/logs /app/state "${LIBRARY_ROOT}"

if ! chown -R "${PUID}:${PGID}" /app/logs /app/state "${LIBRARY_ROOT}"; then
    # On shares with root_squash (NFS / Unraid user-shares), root can't chown.
    # Fall back to making the directories group-writable so the unprivileged
    # user can still create category subdirectories.
    echo "[entrypoint] chown failed (likely root_squash); falling back to chmod 2775"
    chmod -R 2775 /app/logs /app/state "${LIBRARY_ROOT}" || true
fi

# Export library root under the name hardlink.sh expects.
export LIBRARY_ROOT MEDIA_ROOT="${LIBRARY_ROOT}"
export HARDLINK_LOG="/app/logs/hardlink.log"

echo "[entrypoint] Running as ${PUID}:${PGID}; library root: ${LIBRARY_ROOT} ($(stat -c '%A %U:%G' "${LIBRARY_ROOT}"))"

# Run the classifier as the configured unprivileged user.
exec gosu "${PUID}:${PGID}" "$@"
