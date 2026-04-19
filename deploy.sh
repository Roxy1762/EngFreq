#!/bin/bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$APP_DIR/start.sh" --bootstrap "$@"
