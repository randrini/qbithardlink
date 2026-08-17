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

# Fix ownership of paths the app needs to write to (bind mounts may arrive
# owned by root or a different host user).
chown -R "${PUID}:${PGID}" /app/logs /app/.classifier_state.json 2>/dev/null || true

# Run the classifier as the configured unprivileged user.
exec gosu "${PUID}:${PGID}" "$@"
