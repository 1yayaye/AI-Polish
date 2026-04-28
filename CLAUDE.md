# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AI 学术写作助手 (AI Academic Writing Assistant) — a full-stack application for academic paper polishing and originality enhancement using OpenAI-compatible LLM APIs. Two-stage pipeline: polish (润色) → enhance (增强). Also includes a Word Formatter module for AI-assisted docx layout formatting.

## Commands

### Backend (Python 3.9+, FastAPI)

```bash
cd backend
pip install -r requirements.txt
cp .env.example .env   # edit with API keys
python -m app.main                    # API only on :9800
python -m app.main --serve-static     # serve frontend dist on :9800
```

### Frontend (Node 18+, React + Vite + TailwindCSS)

```bash
cd frontend
npm install
npm run dev       # dev server on :5174, proxies /api → :9800
npm run build     # production build → dist/
```

### Testing

```bash
cd backend
python -m pytest test/ -v                          # all tests
python -m pytest test/test_auth.py -v              # single file
python -m pytest test/ -k "test_name" -v           # by pattern
```

### One-click dev (PowerShell)

```powershell
./start-dev.ps1              # validates env, builds frontend, starts both
./start-dev.ps1 -CheckOnly   # validate without starting
```

## Architecture

### Backend (`backend/`)

- **`app/main.py`** — FastAPI app entry point. Registers routes under `/api` prefix, mounts static files for SPA serving, auto-creates system default prompts on startup.
- **`app/config.py`** — Pydantic `Settings` loaded from `backend/.env`. Config keys include per-stage model configs (polish/enhance/emotion), Redis URL, JWT secrets, concurrency limits. Supports hot-reload via `reload_settings()`.
- **`app/database.py`** — SQLAlchemy engine + `SessionLocal`. `init_db()` creates tables and migrates schema (adds missing columns safely). Auto-creates performance indexes.
- **`app/models/models.py`** — All ORM models: `User` (card-key auth), `OptimizationSession` (processing state machine), `OptimizationSegment`, `SessionHistory`, `ChangeLog`, `BillingTransaction`, `ModelProfile` (admin-configured AI model presets), `SavedSpec`, `CustomPrompt`, `SystemSetting`.
- **`app/routes/`** — API routers:
  - `admin.py` → re-exports from `admin_routes/` (auth, users, sessions, model profiles, database viewer, config)
  - `optimization.py` → `/api/optimization/*` — session CRUD, SSE progress streaming, export, retry/stop
  - `prompts.py` → `/api/prompts/*` — custom prompt CRUD per user
- **`app/services/`** — Business logic:
  - `ai_service.py` — `AIService` wraps OpenAI SDK AsyncOpenAI. Handles retry logic (non-retryable vs retryable errors), thinking mode, non-streaming by default to avoid Gemini blocks.
  - `optimization_service.py` — `OptimizationService` orchestrates the full polish/enhance/emotion pipeline per session.
  - `concurrency.py` — `ConcurrencyManager` in-memory async semaphore + queue (no Redis dependency at runtime despite the config key existing).
  - `stream_manager.py` — `StreamManager` for SSE event fan-out to connected clients.
  - `billing_service.py` — Workspace balance ledger (precharge/capture/refund pattern).
- **`app/word_formatter/`** — AI Word Formatter module. AI generates a `StyleSpec` for paragraph types → deterministic `python-docx` OOXML rendering. Organized as: `models/` (AST, StyleSpec, Validation, Patch), `services/` (spec_generator, compiler, renderer, fixer, validator, format_checker, job_manager, preprocessor, template_generator, ast_generator), `api/` (format, format_check, jobs, preprocess, specs, usage), `utils/` (chinese, docx_text, doc_convert, ooxml).

### Frontend (`frontend/`)

- **React 18 + React Router 6 + TailwindCSS 3 + Vite 5**
- `src/App.jsx` — Router with routes: `/` (WelcomePage), `/workspace`, `/session/:sessionId`, `/admin`, `/word-formatter`, `/spec-generator`, `/article-preprocessor`, `/format-checker`
- `src/api/index.js` — Axios client with `X-Card-Key` auth interceptor. API grouped by domain: `adminAPI`, `promptsAPI`, `optimizationAPI`, `wordFormatterAPI`, `adminModelProfilesAPI`
- Protected routes check `localStorage.cardKey`; admin requires non-5174 port (runs on :9800)
- Vite dev server proxies `/api` to backend on :9800

### Auth

- **User auth**: card-key passed via `X-Card-Key` header or `card_key` query param (for SSE). Stored in localStorage on frontend.
- **Admin auth**: JWT token via `/api/admin/login`. Admin credentials in `.env` (auto-rotated from defaults on first start).

### Data Flow (Optimization Pipeline)

1. User submits text → `POST /api/optimization/start`
2. Backend creates `OptimizationSession` (status=queued), precharges billing, enqueues background task
3. `OptimizationService` splits text into segments, processes stage-by-stage via `AIService`, streams progress via SSE
4. Each segment's before/after stored in `ChangeLog` for audit trail
5. Final result assembled from segments (enhanced > polished > original), exportable as TXT

### Key Configuration (.env)

| Key | Purpose |
|-----|---------|
| `POLISH_MODEL/API_KEY/BASE_URL` | Stage 1: academic polish |
| `ENHANCE_MODEL/API_KEY/BASE_URL` | Stage 2: originality enhancement |
| `EMOTION_MODEL/API_KEY/BASE_URL` | Emotion article polish mode |
| `COMPRESSION_MODEL/API_KEY/BASE_URL` | Session history compression |
| `USE_STREAMING` | Default false — avoids Gemini block errors |
| `MAX_CONCURRENT_USERS` | In-memory concurrency limit |
| `WORKSPACE_PRICE_PER_10K_CENTS` | Workspace billing rate (0 = disabled) |
| `THINKING_MODE_ENABLED/EFFORT` | AI reasoning/thinking toggle |
| `HISTORY_COMPRESSION_THRESHOLD` | Chinese char count threshold for context compression |
| `API_REQUEST_INTERVAL` | Seconds between AI calls to avoid rate limits |
| `MAX_UPLOAD_FILE_SIZE_MB` | Word Formatter upload limit (0 = unlimited) |
