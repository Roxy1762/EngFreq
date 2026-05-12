# ─── Build stage ──────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# System deps for pdfplumber, PyMuPDF, tesseract, and NumPy wheel
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps into a dedicated prefix that will be copied into runtime
COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt

# Prebake spaCy model into /install so it gets copied to the runtime stage.
# python -m spacy download installs to the *system* Python's site-packages via
# pip — not to our /install prefix — so we derive the correct wheel URL from
# the installed spaCy version and use pip install --prefix=/install directly.
RUN SPACY_VER=$(PYTHONPATH=/install/lib/python3.12/site-packages \
        python -c "import spacy; print(spacy.__version__)") \
 && pip install --prefix=/install --no-deps \
    "https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-${SPACY_VER}/en_core_web_sm-${SPACY_VER}-py3-none-any.whl"

# Prebake NLTK corpora into /install/nltk_data (copied to runtime via COPY below)
RUN PYTHONPATH=/install/lib/python3.12/site-packages \
    python -c "import nltk; [nltk.download(p, quiet=True, download_dir='/install/nltk_data') for p in ('wordnet','averaged_perceptron_tagger','punkt','punkt_tab','stopwords')]"


# ─── Runtime stage ────────────────────────────────────────────────────────────
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NLTK_DATA=/usr/local/nltk_data \
    HOST=0.0.0.0 \
    PORT=8000 \
    DB_PATH=/app/data/app.db \
    UPLOAD_DIR=/app/data/uploads \
    RESULTS_DIR=/app/data/exports \
    OCR_CACHE_DIR=/app/data/ocr_cache \
    FILE_STORE_DIR=/app/data/files

# Runtime system libs:
#   - tesseract + eng pack for OCR fallback
#   - poppler-utils for pdf2image rasterisation (used when PDFs are scanned)
#   - libgl1 / libglib2.0-0 for opencv / PyMuPDF / rapidocr ONNX
#   - curl for HEALTHCHECK
#   - tini for proper PID-1 signal handling
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        poppler-utils \
        libgl1 \
        libglib2.0-0 \
        curl \
        tini \
    && rm -rf /var/lib/apt/lists/*

# Non-root runtime user for the app
RUN useradd --create-home --uid 1000 app
WORKDIR /app

COPY --from=builder /install /usr/local
COPY --from=builder /install/nltk_data /usr/local/nltk_data

# Application code
COPY --chown=app:app . .

# Single /app/data tree holds every piece of mutable state — uploads, OCR
# cache, persisted file copies, the SQLite DB, AND migration backups — so a
# single volume mount preserves the entire server.
RUN mkdir -p data/uploads data/exports data/ocr_cache data/files data/migration_backups \
 && chown -R app:app /app

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS http://127.0.0.1:${PORT}/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "run.py", "--prod"]
