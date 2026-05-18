#!/usr/bin/env bash
#
# Production deployment helper.
#
# Usage:
#   ./deploy.sh                          # bootstrap + start in prod mode (foreground)
#   ./deploy.sh --daemon                 # bootstrap + start in background (nohup, logs to data/server.log)
#   ./deploy.sh --docker                 # build and start with docker compose
#   ./deploy.sh --restart                # stop, reinstall deps, restart daemon
#   ./deploy.sh --stop                   # stop running daemon
#   ./deploy.sh --status                 # report daemon status
#   ./deploy.sh --healthcheck            # curl the /healthz endpoint once
#   ./deploy.sh --migrate-export FILE    # download a full-server snapshot to FILE
#   ./deploy.sh --migrate-import FILE    # apply FILE on the running server (requires admin creds via env)
#
# Migration helpers expect the server to be running and ADMIN_USERNAME +
# ADMIN_PASSWORD env vars (or .env entries) to match the live admin account.
#
# Defaults to bootstrap + foreground start. All flags are optional and composable.

set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$APP_DIR/data/server.pid"
LOG_FILE="$APP_DIR/data/server.log"

# Resolve PORT: prefer env var → .env file → default 8000
if [[ -z "${PORT:-}" ]] && [[ -f "$APP_DIR/.env" ]]; then
  _env_port="$(grep -E '^PORT=[0-9]+' "$APP_DIR/.env" | tail -1 | cut -d= -f2)"
  PORT="${_env_port:-8000}"
fi
PORT="${PORT:-8000}"
HEALTHCHECK_URL="${HEALTHCHECK_URL:-http://127.0.0.1:${PORT}/healthz}"

mode_docker=0
mode_daemon=0
mode_restart=0
mode_stop=0
mode_status=0
mode_health=0
mode_export=""
mode_import=""

while (($#)); do
  case "$1" in
    --docker) mode_docker=1 ;;
    --daemon) mode_daemon=1 ;;
    --restart) mode_restart=1 ;;
    --stop) mode_stop=1 ;;
    --status) mode_status=1 ;;
    --healthcheck) mode_health=1 ;;
    --migrate-export)
      shift
      mode_export="${1:-}"
      [[ -z "$mode_export" ]] && { echo "[ERROR] --migrate-export requires a destination file" >&2; exit 2; }
      ;;
    --migrate-import)
      shift
      mode_import="${1:-}"
      [[ -z "$mode_import" ]] && { echo "[ERROR] --migrate-import requires a source file" >&2; exit 2; }
      ;;
    --help)
      sed -n '1,28p' "$0"
      exit 0
      ;;
    *)
      echo "[WARN] Ignoring unknown flag: $1" >&2
      ;;
  esac
  shift
done

mkdir -p "$APP_DIR/data"

_stop_daemon() {
  if [[ -f "$PID_FILE" ]]; then
    pid="$(cat "$PID_FILE" || true)"
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
      echo "[INFO] Stopping daemon (pid=$pid)..."
      kill "$pid"
      for _ in 1 2 3 4 5 6 7 8 9 10; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
      done
      if kill -0 "$pid" 2>/dev/null; then
        echo "[WARN] Graceful shutdown timed out, sending SIGKILL"
        kill -9 "$pid" 2>/dev/null || true
      fi
    fi
    rm -f "$PID_FILE"
  else
    echo "[INFO] No daemon PID file found."
  fi
}

_status_daemon() {
  if [[ -f "$PID_FILE" ]]; then
    pid="$(cat "$PID_FILE" || true)"
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
      echo "[OK] Daemon running (pid=$pid)."
      return 0
    fi
    echo "[WARN] Stale PID file: $PID_FILE"
    rm -f "$PID_FILE"
  fi
  echo "[INFO] Daemon is not running."
  return 1
}

_healthcheck() {
  if ! command -v curl >/dev/null 2>&1; then
    echo "[ERROR] curl not installed"
    return 2
  fi
  if curl -fsS --max-time 5 "$HEALTHCHECK_URL" >/dev/null; then
    echo "[OK] Healthcheck passed: $HEALTHCHECK_URL"
    return 0
  fi
  echo "[FAIL] Healthcheck failed: $HEALTHCHECK_URL"
  return 1
}

_load_dotenv_admin() {
  # Pull ADMIN_USERNAME / ADMIN_PASSWORD from .env if not already set, so
  # users don't have to re-export them when running migration helpers.
  if [[ -f "$APP_DIR/.env" ]]; then
    if [[ -z "${ADMIN_USERNAME:-}" ]]; then
      ADMIN_USERNAME="$(grep -E '^ADMIN_USERNAME=' "$APP_DIR/.env" | tail -1 | cut -d= -f2- | tr -d '\r\n')"
    fi
    if [[ -z "${ADMIN_PASSWORD:-}" ]]; then
      ADMIN_PASSWORD="$(grep -E '^ADMIN_PASSWORD=' "$APP_DIR/.env" | tail -1 | cut -d= -f2- | tr -d '\r\n')"
    fi
  fi
  ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
  ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"
  if [[ -z "$ADMIN_PASSWORD" ]]; then
    echo "[ERROR] ADMIN_PASSWORD is required for migration helpers (set env or .env)" >&2
    return 2
  fi
}

_admin_token() {
  # echo a fresh JWT for the admin user; relies on /auth/login
  local resp
  resp="$(curl -fsS --max-time 10 -H 'Content-Type: application/json' \
    -d "$(printf '{"username":"%s","password":"%s"}' "$ADMIN_USERNAME" "$ADMIN_PASSWORD")" \
    "http://127.0.0.1:${PORT}/auth/login" || true)"
  # Pull out "token" field without depending on jq
  python3 - <<EOF
import json, sys
try:
    d = json.loads('''$resp''')
    print(d.get("token") or d.get("access_token") or "", end="")
except Exception:
    pass
EOF
}

_migrate_export() {
  local dest="$1"
  _load_dotenv_admin || return $?
  local token; token="$(_admin_token)"
  if [[ -z "$token" ]]; then
    echo "[ERROR] Could not authenticate as admin — is the server running?" >&2
    return 2
  fi
  local compression="${MIGRATE_COMPRESSION:-fast}"
  echo "[INFO] Exporting full-server snapshot → $dest (compression=$compression)"
  curl -fsSL --max-time 600 \
    -H "Authorization: Bearer $token" \
    "http://127.0.0.1:${PORT}/admin/migration/export?include_file_store=true&include_wordlists=true&compression=${compression}" \
    -o "$dest"
  echo "[OK] Snapshot written: $dest ($(stat -c%s "$dest" 2>/dev/null || stat -f%z "$dest") bytes)"
}

_migrate_import() {
  local src="$1"
  if [[ ! -f "$src" ]]; then
    echo "[ERROR] No such file: $src" >&2
    return 2
  fi
  _load_dotenv_admin || return $?
  local token; token="$(_admin_token)"
  if [[ -z "$token" ]]; then
    echo "[ERROR] Could not authenticate as admin — is the server running?" >&2
    return 2
  fi
  echo "[INFO] Importing snapshot from $src"
  curl -fsS --max-time 1800 \
    -H "Authorization: Bearer $token" \
    -F "file=@$src" \
    -F "dry_run=false" \
    -F "make_safety_backup=true" \
    "http://127.0.0.1:${PORT}/admin/migration/import"
  echo
  echo "[OK] Import request completed."
}

# ── Dispatch single-action flags ──────────────────────────────────────────────

if ((mode_stop)); then
  _stop_daemon
  exit 0
fi

if ((mode_status)); then
  _status_daemon
  exit $?
fi

if ((mode_health)); then
  _healthcheck
  exit $?
fi

if [[ -n "$mode_export" ]]; then
  _migrate_export "$mode_export"
  exit $?
fi

if [[ -n "$mode_import" ]]; then
  _migrate_import "$mode_import"
  exit $?
fi

if ((mode_docker)); then
  cd "$APP_DIR"
  echo "[INFO] Building & starting via docker compose..."
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    docker compose up -d --build
    echo "[OK] Container started. Run 'docker compose logs -f' to tail logs."
  elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose up -d --build
  else
    echo "[ERROR] Docker / docker compose not installed"
    exit 1
  fi
  exit 0
fi

if ((mode_restart)); then
  _stop_daemon
  mode_daemon=1
fi

# ── Default path: bootstrap + start ───────────────────────────────────────────

"$APP_DIR/start.sh" --install-only
echo "[INFO] Bootstrap complete."

if ((mode_daemon)); then
  echo "[INFO] Starting daemon. Logs → $LOG_FILE"
  # disown after nohup so the server survives ssh disconnect
  nohup "$APP_DIR/start.sh" > "$LOG_FILE" 2>&1 &
  pid=$!
  echo "$pid" > "$PID_FILE"
  echo "[OK] Daemon started (pid=$pid)"
  # wait briefly for port to open, then healthcheck
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
    sleep 2
    if _healthcheck >/dev/null 2>&1; then
      echo "[OK] Healthcheck passed."
      exit 0
    fi
  done
  echo "[WARN] Healthcheck did not pass within 24s. Tail $LOG_FILE for details."
  exit 0
fi

# foreground
exec "$APP_DIR/start.sh"
