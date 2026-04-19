#!/bin/bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$APP_DIR/.venv"
BOOTSTRAP=0
INSTALL_ONLY=0

while (($#)); do
  case "$1" in
    --bootstrap)
      BOOTSTRAP=1
      ;;
    --install-only)
      BOOTSTRAP=1
      INSTALL_ONLY=1
      ;;
    --help)
      echo "Usage: ./start.sh [--bootstrap] [--install-only]"
      echo
      echo "  --bootstrap    Create venv, install requirements, prepare resources."
      echo "  --install-only Run bootstrap steps and exit."
      exit 0
      ;;
  esac
  shift
done

echo "[INFO] App dir: $APP_DIR"
cd "$APP_DIR"

if [[ -x "$VENV_DIR/bin/python" ]]; then
  if "$VENV_DIR/bin/python" -m pip --version >/dev/null 2>&1; then
    PYTHON="$VENV_DIR/bin/python"
  fi
fi

if [[ -z "${PYTHON:-}" ]]; then
  SYSTEM_PYTHON=""
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >/dev/null 2>&1; then
        SYSTEM_PYTHON="$candidate"
        break
      fi
    fi
  done

  if [[ -z "$SYSTEM_PYTHON" ]]; then
    echo "[ERROR] Python 3.10+ was not found."
    exit 1
  fi

  if [[ -d "$VENV_DIR" ]]; then
    echo "[WARN] Found incomplete virtual environment. Removing it..."
    rm -rf "$VENV_DIR"
    if [[ -d "$VENV_DIR" ]]; then
      echo "[ERROR] Could not remove broken virtual environment."
      exit 1
    fi
  fi

  echo "[INFO] Creating virtual environment..."
  "$SYSTEM_PYTHON" -m venv "$VENV_DIR"
  PYTHON="$VENV_DIR/bin/python"
fi

WRITE_ENV=0
if [[ ! -f "$APP_DIR/.env" ]]; then
  WRITE_ENV=1
elif ! grep -Eq '^ADMIN_PASSWORD=.' "$APP_DIR/.env"; then
  WRITE_ENV=1
fi

if ((WRITE_ENV)); then
  echo "[INFO] Writing default .env file..."
  cat > "$APP_DIR/.env" <<EOF
HOST=0.0.0.0
PORT=8000
ADMIN_USERNAME=admin
ADMIN_PASSWORD=admin123
AI_MODEL=claude-opus-4-6
AI_BATCH_SIZE=20
VOCAB_PROVIDER=free_dict
WEIGHT_BODY=1.0
WEIGHT_STEM=1.5
WEIGHT_OPTION=3.0
OCR_LANGUAGE=eng
UPLOAD_DIR=data/uploads
RESULTS_DIR=data/exports
OCR_CACHE_DIR=data/ocr_cache
FILE_STORE_DIR=data/files
DB_PATH=app.db
EOF
  echo "[INFO] Default admin user: admin"
  echo "[INFO] Default admin password: admin123"
fi

mkdir -p "$APP_DIR/data/uploads" "$APP_DIR/data/exports" "$APP_DIR/data/ocr_cache" "$APP_DIR/data/files"

if ! "$PYTHON" -c "import fastapi, uvicorn, sqlalchemy" >/dev/null 2>&1; then
  BOOTSTRAP=1
fi

if ((BOOTSTRAP)); then
  echo "[INFO] Installing Python packages..."
  "$PYTHON" -m pip install --upgrade pip
  "$PYTHON" -m pip install -r "$APP_DIR/requirements.txt"

  echo "[INFO] Preparing language resources..."
  if ! "$PYTHON" -c "import spacy; spacy.load('en_core_web_sm')" >/dev/null 2>&1; then
    "$PYTHON" -m spacy download en_core_web_sm
  fi
  "$PYTHON" -c "import nltk; [nltk.download(pkg, quiet=True) for pkg in ('wordnet', 'averaged_perceptron_tagger', 'punkt', 'stopwords')]" >/dev/null 2>&1 || true
fi

if ((INSTALL_ONLY)); then
  echo "[OK] Bootstrap complete."
  exit 0
fi

echo "[INFO] Starting server on http://127.0.0.1:8000"
exec "$PYTHON" run.py --prod
