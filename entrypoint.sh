#!/usr/bin/env bash
# Entrypoint for the DMS container.
#
# Honors PUID/PGID (defaulting to 99/100 for Unraid) by mutating the bundled
# `dms` user/group at runtime, chowns /config to that user, then drops
# privileges via gosu before exec'ing the CMD.
#
# Designed to be safe to re-run after a docker restart: id changes are
# idempotent (groupmod / usermod no-op when ids already match).

set -euo pipefail

PUID="${PUID:-99}"
PGID="${PGID:-100}"

# Reconcile the dms group/user with the requested ids. Errors here are
# normally "id already in use" which we tolerate when an unrelated user
# happens to share the id.
if [ "$(id -g dms)" != "$PGID" ]; then
  groupmod -o -g "$PGID" dms || true
fi
if [ "$(id -u dms)" != "$PUID" ]; then
  usermod -o -u "$PUID" -g "$PGID" dms || true
fi

# /config is the only writable mount. Lock it down + own it.
mkdir -p /config
chown -R "$PUID:$PGID" /config 2>/dev/null || true
chmod 700 /config 2>/dev/null || true

# /app is read-only at runtime — make sure the runtime user can read it.
chown -R "$PUID:$PGID" /app 2>/dev/null || true

# Hand off to the CMD as the unprivileged user.
exec gosu "$PUID:$PGID" "$@"
