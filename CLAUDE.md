# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**English Exam Word Analyzer** — A full-stack application that analyzes English exam PDFs to extract, rank, and enrich vocabulary words. Users upload exams, the system parses them, extracts word frequencies, and generates vocabulary lists enriched with definitions and examples from configurable providers (Claude AI, Free Dictionary, Merriam-Webster).

**Key features:**
- User authentication with JWT tokens
- PDF/image OCR and text extraction
- NLP-based word extraction and lemmatization (spaCy)
- Frequency analysis with weighted scoring (body/stem/option text)
- Pluggable vocabulary enrichment providers
- CSV/XLSX export
- Public share codes for results
- Admin user management

## Getting Started

### Setup

```bash
# On Windows
start.bat --bootstrap      # Install dependencies, setup venv
start.bat                  # Start dev server (hot-reload, debug mode)

# On Linux/macOS
./start.sh --bootstrap     # Install dependencies, setup venv
./start.sh                 # Start dev server

# Direct Python (if venv already active)
python run.py              # Dev mode (auto-reload, 1 worker)
python run.py --prod       # Production mode (no reload, 4 workers)
```

### Configuration

Settings are driven by environment variables (`.env` file, or environment):
- `HOST` / `PORT`: Server binding (default: 0.0.0.0:8000)
- `ADMIN_USERNAME` / `ADMIN_PASSWORD`: Initial admin credentials (default: admin/admin123)
- `DB_PATH`: SQLite database location (default: app.db)
- `ANTHROPIC_API_KEY`: Claude API key for vocabulary enrichment
- `AI_MODEL`: Model to use (default: claude-opus-4-7)
- `VOCAB_PROVIDER`: Which provider to use by default (claude/deepseek/openai/free_dict/iciba/merriam_webster/youdao/ecdict)
- `WEIGHT_BODY` / `WEIGHT_STEM` / `WEIGHT_OPTION`: Scoring weights for word occurrence locations
- `TESSERACT_CMD`: Path to tesseract executable (for OCR on Windows)
- See [backend/config.py](backend/config.py) for full list.

### Database

SQLite database initialized automatically on first run. Schema:
- `users` — User accounts, admin flag
- `exams` — Uploaded exam files + analysis results
- `dicts` — Generated vocabulary lists
- `app_settings` — Runtime configuration (filter defaults, weights)

## Architecture

### API Layer ([backend/main.py](backend/main.py))
FastAPI app with these endpoint categories:
1. **Auth** — `/auth/register`, `/auth/login`, `/auth/me`
2. **Analysis** — `/api/analyze` (upload + config), `/api/tasks/{id}` (poll/export), `/api/tasks/{id}/vocab` (generate vocab)
3. **Library + review** — `/api/library/*`, `/api/review/*` (per-user vocab notebook + spaced repetition)
4. **Word relations** — `/api/words/related`, `/api/library/{id}/related`, `/api/library/suggestions/gaps` (offline related-word lookup + gap recommendations)
5. **Practice quiz** — `/api/quiz/generate`, `/api/quiz/submit`, `/api/quiz/stats` (self-test with auto-grading; results feed into the review heatmap)
6. **Admin** — `/admin/users`, `/admin/codes`, `/admin/migration/*` (requires admin JWT)

Background task system queues analysis jobs asynchronously.

### Services Layer
- **[file_handler.py](backend/services/file_handler.py)** — Extract text from PDF, DOCX, images (using pdfplumber, python-docx, tesseract)
- **[structure_recognizer.py](backend/services/structure_recognizer.py)** — Parse exam structure (identify question body, stems, options)
- **[frequency_analyzer.py](backend/services/frequency_analyzer.py)** — Extract words, lemmatize (spaCy), weight by location, build frequency tables
- **[vocabulary_generator.py](backend/services/vocabulary_generator.py)** — Select provider, enrich lemmas with definitions/examples, prioritize by rarity
- **[export_service.py](backend/services/export_service.py)** — Convert vocabulary to CSV/XLSX
- **[word_family.py](backend/services/word_family.py)** — Map lemmas to derivational families (e.g., happy → happiness, happily)
- **[word_relations.py](backend/services/word_relations.py)** — Related-word suggestions: same family, similar difficulty (CEFR + Zipf), library siblings, plus "gap" recommendations from the gaokao 3500 list that the user hasn't saved yet (offline, no LLM calls)
- **[quiz_service.py](backend/services/quiz_service.py)** — Practice quiz generator + grader. Modes: `definition_to_word`, `word_to_definition`, `fill_in_blank`, `mixed`. Distractors come from peer library entries; scoring feeds into the existing `ReviewEvent`/heatmap pipeline.
- **[migration_service.py](backend/services/migration_service.py)** — Full-server export/import (DB via SQLite Backup API + user files + wordlists into a single zip, with manifest, sha256 checksum, path-traversal-safe extraction, automatic rollback snapshots)

### Vocabulary Provider System ([backend/providers/](backend/providers/))
Pluggable architecture for enriching words with definitions. Base class: [BaseVocabProvider](backend/providers/base_provider.py).

**Implementations:**
- **ClaudeProvider** — Uses Claude API to generate context-aware definitions with examples
- **FreeDictProvider** — Queries freedictionary.dev API
- **MerriamWebsterProvider** — Queries Merriam-Webster API (requires API key)

Adding a new provider:
1. Create class extending `BaseVocabProvider`, implement `name` and `enrich()` async method
2. Register in [vocabulary_generator.py:_build_registry()](backend/services/vocabulary_generator.py)
3. Set `VOCAB_PROVIDER` env var or select via admin UI

### Database Models ([backend/database.py](backend/database.py))
SQLAlchemy ORM:
- `User` — Accounts; cascades delete exams/dicts
- `Exam` — Uploaded file, raw parse result, analysis result (JSON)
- `Dict` — Vocabulary list (vocab_json), linked to Exam or standalone
- `AppSetting` — Key-value store for runtime config

### Data Models ([backend/models/schemas.py](backend/models/schemas.py))
Pydantic models for request/response validation:
- `FilterConfig` — min_word_length, stopword/number/proper-noun filters, basic-word threshold
- `WeightConfig` — Scoring weights for body/stem/option occurrences
- `WordEntry` / `LemmaEntry` / `FamilyEntry` — Frequency analysis results (surface forms, lemmas, word families)
- `VocabEntry` — Enriched vocabulary entry (headword, definition, examples, POS, source)
- `AnalysisResult` — Complete exam analysis (words/lemmas/families ranked by score)

### Authentication ([backend/auth.py](backend/auth.py))
- JWT tokens (HS256, 7-day expiry)
- Password hashing with bcrypt
- Share code generation (8-char alphanumeric) for public exam/vocab sharing

## Key Workflows

### 1. Upload & Analyze
1. POST `/api/analyze` with file + config (FilterConfig, WeightConfig, top_n)
2. File extracted → text parsed by structure_recognizer → word frequencies calculated
3. Result stored as `Exam` with `exam_code`, returns `task_id` for polling
4. GET `/api/tasks/{id}` polls until complete
5. Optionally trigger vocab generation in same call with `generate_vocab=true`

### 2. Generate Vocabulary
1. POST `/api/tasks/{id}/vocab` with provider selection
2. Service calls `vocabulary_generator.generate_vocabulary()` → selects provider → enriches lemmas
3. Result stored as `Dict` with `dict_code`, linked to exam
4. Returns list of VocabEntry with definitions, examples, etc.

### 3. Export
GET `/api/tasks/{id}/export/{csv|xlsx}` → returns binary file (to_csv / to_xlsx)

### 4. Public Sharing
GET `/api/share/exam/{exam_code}` / `/api/share/dict/{dict_code}` → returns JSON (no auth required)

## Development Notes

### Word Scoring Logic
Score = `body_count * weight_body + stem_count * weight_stem + option_count * weight_option`

Weights (configurable):
- `weight_body`: 1.0 (default) — Full question text
- `weight_stem`: 1.5 (default) — Question stems
- `weight_option`: 3.0 (default) — Answer options (highest impact)

### Frequency Analysis Defaults
- Min word length: 2
- Filter stopwords: False
- Keep proper nouns: True
- Filter numbers: True
- Filter basic words (by Zipf score): Optional

### Environment Defaults (config.py)
These are applied if not in `.env`:
```
HOST=0.0.0.0, PORT=8000
ADMIN_USERNAME=admin, ADMIN_PASSWORD=admin123
DB_PATH=app.db
AI_MODEL=claude-opus-4-6
AI_BATCH_SIZE=20
VOCAB_PROVIDER=claude
```

### Notes on Key Dependencies
- **spaCy** — Lemmatization + POS tagging (en_core_web_sm model)
- **NLTK** — Additional NLP utilities
- **pdfplumber** — PDF text extraction (preserves layout)
- **pytesseract** — OCR for images/scanned PDFs
- **Pillow** — Image processing
- **FastAPI + Uvicorn** — Web framework + ASGI server
- **SQLAlchemy 2.0+** — ORM with modern async patterns
- **Pydantic** — Request/response validation

### Testing
No existing test suite. To add tests:
- Use pytest + pytest-asyncio for async tests
- Mock external APIs (Anthropic, dictionary endpoints)
- Test data models with Pydantic validators
- Integration tests hit SQLite (use temp db fixture)

### Common Tasks

**Add a new vocabulary provider:**
1. Create `backend/providers/my_provider.py`
2. Extend `BaseVocabProvider`, implement `name` and `enrich()` (async)
3. Register in `vocabulary_generator._build_registry()`
4. Set `VOCAB_PROVIDER=my_provider` in .env

**Modify scoring weights:**
- Update `WeightConfig` in [backend/models/schemas.py](backend/models/schemas.py)
- Or change defaults in [backend/config.py](backend/config.py)
- Users can override per-request via `AnalyzeRequest.weights`

**Change OCR language:**
- Set `OCR_LANGUAGE` env var (spacy language codes, e.g., "fra" for French)
- Ensure Tesseract language pack installed

**Reset admin password:**
- POST `/admin/users/{user_id}/reset-password` (admin only)
- Or delete `app.db` and restart (recreates with default admin/admin123)

**Inspect task results:**
- GET `/api/tasks/{id}` returns full AnalysisResult (words, lemmas, families)
- Task status: "pending", "processing", "completed", "error"
- Raw parse result stored in `Exam.raw_parse_result_json`

## File Structure

```
F:\0AIDEV2\ENGL1/
├── backend/
│   ├── main.py              # FastAPI app, endpoints
│   ├── config.py            # Settings from env
│   ├── database.py          # SQLAlchemy models, session
│   ├── auth.py              # JWT, password hashing, codes
│   ├── models/
│   │   └── schemas.py       # Pydantic request/response models
│   ├── services/            # Business logic (file, NLP, analysis, export)
│   │   ├── file_handler.py
│   │   ├── structure_recognizer.py
│   │   ├── frequency_analyzer.py
│   │   ├── vocabulary_generator.py
│   │   ├── export_service.py
│   │   ├── word_family.py
│   │   ├── migration_service.py    # Full-server export/import + per-member checksums
│   │   ├── backup_scheduler.py     # Scheduled automatic backups (async background task)
│   │   └── ...
│   └── providers/           # Vocabulary enrichment (pluggable)
│       ├── base_provider.py
│       ├── claude_provider.py
│       ├── free_dict_provider.py
│       └── merriam_webster_provider.py
├── run.py                   # Entry point (python run.py [--prod])
├── requirements.txt         # Python dependencies
├── start.bat / start.sh     # Bootstrap + dev server scripts
├── deploy.bat / deploy.sh   # Production deployment scripts
└── .env.example             # Template for environment variables
```

## Deployment

See [deploy.bat](deploy.bat) / [deploy.sh](deploy.sh) for production setup scripts. Key considerations:
- Use `python run.py --prod` for multi-worker mode
- Set `ADMIN_PASSWORD` and `ANTHROPIC_API_KEY` in production
- Ensure SQLite DB path is persistent (or use external DB)
- Configure CORS appropriately for frontend domain
- Set up HTTPS reverse proxy (nginx, etc.)

### Docker deployment

`docker compose up -d --build` mounts a single `engfreq-data` volume at `/app/data` covering DB + uploads + OCR cache + file_store + migration backups. The container's `DB_PATH` is set to `/app/data/app.db` so the database is captured by the volume (and by the migration system).

### One-click server migration

Endpoints (admin-only, JWT auth required):
- `GET  /admin/migration/stats` — current counts + on-disk sizes
- `GET  /admin/migration/export?include_file_store=&include_wordlists=&include_ocr_cache=&compression=&notes=` — stream a `.zip` snapshot (`compression` ∈ `store|fast|balanced|best`)
- `POST /admin/migration/preview` — multipart upload (streamed to disk), validates manifest without applying
- `POST /admin/migration/import` — multipart upload + `dry_run`/`replace_*`/`make_safety_backup`/`abort_on_user_conflict`/`verify_all_checksums` flags
- `GET  /admin/migration/backups` — list every backup (auto/pre-import/manual) with size + category
- `GET  /admin/migration/backups/{name}` — download a backup
- `DELETE /admin/migration/backups/{name}` — delete one
- `POST /admin/migration/backups/{name}/restore` — restore directly from a local backup (no upload)
- `GET  /admin/migration/schedule` — current automatic-backup schedule + last-run status
- `PUT  /admin/migration/schedule` — update schedule (`enabled`, `interval_hours`, `retention_count`, `include_*`, `compression`)
- `POST /admin/migration/schedule/run-now` — trigger an immediate auto-backup

CLI helpers (uses `.env` admin credentials):
- `./deploy.sh --migrate-export ./snapshot.zip` (set `MIGRATE_COMPRESSION=balanced` to override)
- `./deploy.sh --migrate-import ./snapshot.zip`

Bundle layout (`engfreq-migration-<ts>.zip`, schema_version=2):
```
manifest.json         format/schema_version/app_version/exported_at/counts/includes/compression
                      checksums: {arc_name → sha256} for *every* member, not just the DB
db/app.db             hot snapshot via sqlite3.Connection.backup()
data/files/...        persistent file copies (FILE_STORE_DIR)
data/wordlists/...    optional, default included
data/ocr_cache/...    optional, default excluded (large)
```

Safety invariants in [migration_service.py](backend/services/migration_service.py):
- Zip extraction rejects absolute paths and `..` traversal
- Every member's SHA256 is verified before extraction (v2 bundles); v1 bundles fall back to DB-only verification
- A safety snapshot is written to `data/migration_backups/` before any destructive change
- A module-level asyncio.Lock serialises imports
- Engine pool is disposed before/after the file swap so subsequent ORM calls open a fresh connection against the restored DB
- Transient/hidden files (`*.partial`, `*.tmp`, `*.lock`, `.DS_Store`, …) are skipped on export
- Restore copies inside data dirs run on a thread pool for many-small-files workloads

Scheduled backups ([backup_scheduler.py](backend/services/backup_scheduler.py)):
- An asyncio task runs in the background (started by the FastAPI lifespan)
- Configuration + last-run status persist in the `app_settings` table
- Files named `auto-YYYYMMDD-HHMMSS.zip` go alongside `pre-import-*.zip` in `data/migration_backups/`
- Retention prune is per-category: auto-backups are pruned to `retention_count`; pre-import snapshots are kept indefinitely
