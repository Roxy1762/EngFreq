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
    PORT=8000

# Runtime system libs (OCR + image codecs + PyMuPDF runtime deps)
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        libgl1 \
        libglib2.0-0 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root runtime user for the app
RUN useradd --create-home --uid 1000 app
WORKDIR /app

COPY --from=builder /install /usr/local
COPY --from=builder /install/nltk_data /usr/local/nltk_data

# Application code
COPY --chown=app:app . .

RUN mkdir -p data/uploads data/exports data/ocr_cache data/files \
 && chown -R app:app /app

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS http://127.0.0.1:${PORT}/healthz || exit 1

CMD ["python", "run.py", "--prod"]
