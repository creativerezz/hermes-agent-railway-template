#!/bin/bash
set -e

# Refuse links before first-boot cp/touch: both commands can follow a planted
# config.yaml or .env link outside the mounted Hermes volume. The profile scan
# below also rejects links, but it runs after these seed operations.
for path in /data/.hermes /data/.hermes/config.yaml /data/.hermes/.env /data/.hermes/profiles; do
  if [ -L "$path" ]; then
    echo "[template-config-migrate] ERROR: refusing symlinked Hermes path: $path" >&2
    exit 1
  fi
done

# Mirror dashboard-ref-only's startup: create every directory hermes expects
# and seed a default config.yaml if the volume is empty. Without these,
# `hermes dashboard` endpoints that hit logs/, sessions/, cron/, etc. can fail
# with opaque errors even though no auth is actually involved.
# NOTE (hermes >= v2026.7.1): several dirs were consolidated and are now
# resolved via get_hermes_dir("<new>", "<old>"), which returns the NEW path
# unless the OLD one already has *content*. Seeding an empty legacy stub no
# longer "claims" it — hermes ignores empty stubs and writes to the new path
# (upstream #27602). So we seed the NEW paths: pairing -> platforms/pairing,
# image_cache -> cache/images, audio_cache -> cache/audio. A populated legacy
# dir from a pre-v2026.7.1 deploy still wins on both sides, so no migration is
# needed. server.py:_resolve_pairing_dir() mirrors this same rule for the
# admin panel's Users tab — keep the two in sync on future bumps.
mkdir -p /data/.hermes/cron /data/.hermes/sessions /data/.hermes/logs \
         /data/.hermes/memories /data/.hermes/skills /data/.hermes/platforms/pairing \
         /data/.hermes/hooks /data/.hermes/cache/images /data/.hermes/cache/audio \
         /data/.hermes/workspace /data/.hermes/skins /data/.hermes/plans \
         /data/.hermes/home

# Stamp the install method as "docker" so hermes treats this as an immutable
# container image, not a pip checkout. detect_install_method() reads the
# code-scoped /opt/hermes-agent/.install_method baked by Dockerfile first, then
# this home-scoped stamp when running in a container, before .git / pip fallback.
# Those stamps make the dashboard's "Update Hermes" button refuse an ephemeral
# in-container pip upgrade, which could desync the Python package from this
# image's pre-built web_dist/ui-tui bundles. The real upgrade path is to bump
# HERMES_REF and redeploy. Rewrite this home stamp each boot to self-heal it.
printf 'docker\n' > /data/.hermes/.install_method

if [ ! -f /data/.hermes/config.yaml ] && [ -f /opt/hermes-agent/cli-config.yaml.example ]; then
  cp /opt/hermes-agent/cli-config.yaml.example /data/.hermes/config.yaml
fi

[ ! -f /data/.hermes/.env ] && touch /data/.hermes/.env

# The template installs Hermes into system Python with no venv, so optional
# runtime packages need a writable target instead of a bare `uv pip install`.
# Keep that target on the volume and export it BEFORE migration: older schema
# steps may import optional providers already installed there. The gateway and
# dashboard inherit the same value; hermes_bootstrap.py activates it on start.
mkdir -p /data/.hermes/lazy-packages
export HERMES_LAZY_INSTALL_TARGET=/data/.hermes/lazy-packages

# v2026.9.21 could leave a named profile's outgoing final reply in that
# profile's state.db while the shared gateway's boot recovery checked the root
# ledger. v2026.9.24 fixes new writes but does not replay those old rows. They
# are an advisory: an operator may accept that a sender must make a new request
# (which can rerun tools). An unreadable ledger is a different, unknown state;
# stop rather than claiming the upgrade has checked it.
delivery_check_rc=0
python /app/check_pending_deliveries.py --hermes-home /data/.hermes || delivery_check_rc=$?
if [ "$delivery_check_rc" -eq 2 ]; then
  echo "[delivery-preflight] WARNING: old named-profile replies may not be replayed; continuing the upgrade as configured" >&2
elif [ "$delivery_check_rc" -ne 0 ]; then
  echo "[delivery-preflight] ERROR: could not verify named-profile delivery ledgers; stopping before config migration" >&2
  exit "$delivery_check_rc"
fi

# The official Docker hook migrates only its current HERMES_HOME. This image
# also serves named profiles under the same persistent volume, so migrate the
# root and every live profile before the dashboard or gateway can read them.
# The helper rejects symlinked state paths, preserves each profile's own
# HERMES_HOME, and stops startup if a migration fails after upstream rollback.
python /app/migrate_hermes_configs.py

# Bootstrap OAuth tokens from env var (e.g. xAI Grok SuperGrok).
# Set HERMES_AUTH_JSON_BOOTSTRAP to the contents of a locally-generated
# ~/.hermes/auth.json. Written only once — subsequent token refreshes update
# the file in place on the persistent volume.
if [ ! -f /data/.hermes/auth.json ] && [ -n "${HERMES_AUTH_JSON_BOOTSTRAP}" ]; then
  printf '%s' "${HERMES_AUTH_JSON_BOOTSTRAP}" > /data/.hermes/auth.json
  chmod 600 /data/.hermes/auth.json
fi

# Clear stale gateway runtime files left over from the previous container.
# hermes writes these on start but does not remove them on SIGTERM, and /data
# is a persistent volume, so they survive into the next boot:
#   gateway.pid   -> "PID file race lost to another gateway instance"
#   gateway.lock  -> since v2026.8.27 get_running_pid() also consults the lock,
#                    and the new cross-profile gate makes `--replace` REFUSE a
#                    pid it cannot prove owns this HERMES_HOME (gateway/run.py
#                    "Refusing --replace"), which no retry can clear
#   gateway.sock  -> a stale control socket blocks the fresh bind
# No hermes process can be running here (we are pre-exec in a fresh
# container), so removing all three unconditionally is safe.
rm -f /data/.hermes/gateway.pid /data/.hermes/gateway.lock /data/.hermes/gateway.sock


# HERMES_DASHBOARD_PUBLIC_URL is deliberately NOT exported here. server.py owns
# it: build_hermes_env() sets it only alongside the basic-auth credentials that
# satisfy hermes' auth gate. Declaring the URL without them makes the dashboard
# SystemExit at startup (v2026.8.27's should_require_dashboard_auth), and since
# Dashboard has no respawn supervisor every proxied page 503s until redeploy
# while /setup and /health stay green. Setting it here would skip that pairing.

exec python /app/server.py
