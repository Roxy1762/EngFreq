#!/usr/bin/env bash
#
# Production deployment helper.
#
# Usage:
#   ./deploy.sh                 # bootstrap + start in prod mode (foreground)
#   ./deploy.sh --daemon        # bootstrap + start in background (nohup, logs to data/server.log)
#   ./deploy.sh --docker        # build and start with docker compose
#   ./deploy.sh --restart       # stop, reinstall deps, restart daemon
#   ./deploy.sh --stop          # stop running daemon
#   ./deploy.sh --status        # report daemon status
#   ./deploy.sh --healthcheck   # curl the /healthz endpoint once
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

while (($#)); do
  case "$1" in
    --docker) mode_docker=1 ;;
    --daemon) mode_daemon=1 ;;
    --restart) mode_restart=1 ;;
    --stop) mode_stop=1 ;;
    --status) mode_status=1 ;;
    --healthcheck) mode_health=1 ;;
    --help)
      sed -n '1,20p' "$0"
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
