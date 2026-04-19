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
- `AI_MODEL`: Model to use (default: claude-opus-4-6)
- `VOCAB_PROVIDER`: Which provider to use by default (claude/free_dict/merriam_webster)
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
FastAPI app with three endpoint categories:
1. **Auth** — `/auth/register`, `/auth/login`, `/auth/me`
2. **Analysis** — `/api/analyze` (upload + config), `/api/tasks/{id}` (poll/export), `/api/tasks/{id}/vocab` (generate vocab)
3. **Admin** — `/admin/users`, `/admin/codes` (requires admin JWT)

Background task system queues analysis jobs asynchronously.

### Services Layer
- **[file_handler.py](backend/services/file_handler.py)** — Extract text from PDF, DOCX, images (using pdfplumber, python-docx, tesseract)
- **[structure_recognizer.py](backend/services/structure_recognizer.py)** — Parse exam structure (identify question body, stems, options)
- **[frequency_analyzer.py](backend/services/frequency_analyzer.py)** — Extract words, lemmatize (spaCy), weight by location, build frequency tables
- **[vocabulary_generator.py](backend/services/vocabulary_generator.py)** — Select provider, enrich lemmas with definitions/examples, prioritize by rarity
- **[export_service.py](backend/services/export_service.py)** — Convert vocabulary to CSV/XLSX
- **[word_family.py](backend/services/word_family.py)** — Map lemmas to derivational families (e.g., happy → happiness, happily)

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
