# Grace — AI Movie Assistant

Grace is a personal AI assistant whose speciality is film. You talk to it like a person. It answers general questions, recommends and compares films from verified movie data, researches a film (awards, filming locations, soundtrack, visual style, reviews), checks the web when it needs current facts, remembers your taste, and keeps your watchlist, watched list, ratings and reminders.

The design rule behind everything: **the model understands the user; code only enforces.** Code never guesses what a message means. It owns security, validation, database writes, execution and read-back.

## Contents

- [What Grace can do](#what-grace-can-do)
- [How it works](#how-it-works)
- [Retrieval, embeddings and memory](#retrieval-embeddings-and-memory)
- [Architecture](#architecture)
- [Repository layout](#repository-layout)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Tests and evaluation](#tests-and-evaluation)
- [CI and operations](#ci-and-operations)
- [How the main behaviours work](#how-the-main-behaviours-work)
- [Security model](#security-model)
- [Frontend state ownership contract](#frontend-state-ownership-contract)

## What Grace can do

| Area | What you get |
|---|---|
| Conversation | One chat that follows context ("the 1984 one", "add it", "yes"). Answers are prose shaped by the question; lists for picks, tables only for real grids. Spoilers only when asked. |
| Movie data | Details, cast and crew, where to watch in your region (TMDB), people pages, comparisons. |
| Recommendations | Every constraint you state (runtime, year, genre, language, mood, "like X", exclusions, already watched) is checked against real film details before a film is offered. |
| Research | Awards, filming locations, soundtrack, visual style and reviews, each with sources and a coverage note. Findings are stored and retrieved with hybrid search (see below). |
| Web search | Current facts through Gemini grounded search, with Groq `browser_search` as a fallback. The model is told today's date. |
| Your lists | Watchlist, watched, ratings and reminders. Every change is verified after it is written and shown as a card with Undo. |
| Memory | Saves tastes and facts you state ("Memory updated", with Undo), learns from feedback, summarises long chats, and recalls earlier conversations with full-text and vector search. |
| Staying in touch | Notification centre, optional daily brief, optional Web Push to a device. |
| Account | Email sign-in with one-time codes, sessions list, data export, account deletion with a restore window, guest mode that is adopted when you sign up. |
| Interface | React workspace with projects, sharing links, voice dictation and read-aloud, answer-length setting, light and dark themes, installable as a PWA. |

## How it works

- **The model decides; code executes.** Every chat turn goes to one language model (Groq `openai/gpt-oss-120b`). It reads the whole conversation and either answers or calls tools. There is no intent router, no regex shortcut and no rewriting of the model's answer. `Backend/tests/test_architecture.py` enforces this.
- **Ten tools** give the model exact data and actions: `movie_details`, `person_details`, `recommend_movies`, `compare_movies`, `movie_research`, `web_search`, `my_lists`, `update_my_list`, `remember_taste`, `recall_conversation`.
- **Code owns what must be exact.** A list change is resolved, written, read back and confirmed exactly once. A change the model decides after reading outside text (film data, reviews, web pages) is only *proposed* and needs the user's yes, so injected text cannot act. Code also owns permissions, rate limits, deadlines and failover.
- **When the model is busy**, the turn waits for it (up to 45 s, with a "busy, continuing" status). No weaker model answers instead. If nothing can answer in time, the user gets a named, retryable error with the real wait time.
- **Streaming.** The browser receives server-sent events: `started`, `status`, `providers`, `sources`, `references`, `state`, `action`, `memory`, `token`, `done` and `error`. The model's text is shown exactly as written.

## Retrieval, embeddings and memory

Grace has three stores that are searched, plus the ordinary relational data. All three live in PostgreSQL with the `pgvector` extension; vectors are 384-dimensional.

| Store | What is in it | Searched by |
|---|---|---|
| `knowledge_documents` | Research snippets saved while researching a film (awards, locations, music, visual style, reviews), each with source, source type, URL and metadata | `RAGService` (`app/rag.py`) |
| `memories` | Durable facts and tastes a user stated, with importance and last-used time | `MemoryService.memories` (`app/memory.py`) |
| `messages` | Past conversation turns, branch-aware | `MemoryService.relevant_history`, full-text |

### Embeddings

- Model: `BAAI/bge-small-en-v1.5` through Sentence-Transformers, run locally, normalised, 384 dimensions (`app/embeddings.py`). Override with `EMBEDDING_MODEL`. A dimension mismatch raises an error instead of storing a bad vector.
- The model is loaded lazily. Ordinary chat retrieval never cold-starts it: a memory is given a vector only if the model is already loaded, and otherwise it is stored without one and found by full-text search.
- **Embeddings are optional and off in the default Docker build** (`INSTALL_EMBEDDINGS: "false"` in `docker-compose.yml`). Without them, retrieval is lexical only and everything still works. To enable the vector path, set `INSTALL_EMBEDDINGS: "true"` for the `backend` and `feedback-worker` builds; this installs `requirements-embeddings.lock` (`sentence-transformers`, `torch`, `transformers`).

### Hybrid retrieval over research (RAG)

`RAGService.ingest_many` and `RAGService.search` in `Backend/app/rag.py`:

1. **Ingest.** Text is split into overlapping chunks (220 words, 40 overlap). Each chunk gets a SHA-256 content hash and is skipped if the same hash and URL already exist. A vector is stored when the embedding model is available.
2. **Lexical ranking.** BM25 over stemmed words with stopwords removed. The film's title appears in every snippet about it, so title words score low and the words that say what is asked decide the rank. A question word from an aspect group (music, filming, cinematography, critics, budget, awards) also pulls in that group's other words at a lower weight, so "where was it filmed?" finds "principal photography took place in Budapest". An exact phrase match gets a boost.
3. **Vector ranking.** Cosine distance in pgvector, when embeddings exist.
4. **Fusion.** Reciprocal Rank Fusion of the lexical and vector lists.
5. **Rerank.** Fused score plus lexical score plus query-term overlap, with a bonus for primary, official, review and interview sources and a small freshness bonus.
6. **Diversity.** An MMR-style pass prefers a source host not yet chosen, so ten snippets from one site do not crowd out everything else.

`movie_research` runs web searches per research domain, saves the results with `_persist_research` (typed as primary, review or web, with the source tier and evidence types in metadata), then calls `RAGService.search` for the film and focus. Visual-style research uses the same search. The results go to the model as sourced evidence with a coverage note.

### Memory

- **Search** (`MemoryService.memories`): on PostgreSQL, full-text (`to_tsquery`, `ts_rank_cd`) and pgvector cosine search are combined as `0.55 × semantic + 0.35 × lexical + 0.10 × importance`. If the query needs a cold embedding, the weights move to `0.70 / 0.20 / 0.10`. On SQLite (tests) a lexical and cosine scan over the latest 500 memories is used. Migration 0020 adds GIN full-text indexes on `messages` and `memories` and an HNSW cosine index on `memories.embedding`.
- **What the model is given** (`prompt_memories`): the most relevant memories first, then the user's standing memories by importance, up to 16 and 1,800 characters. The model decides which apply, so a paraphrase like "my gran" still matches "watches with their grandmother". When some are left out, the model is told how many and can search them with `recall_conversation`, which searches both past messages and memories.
- **Saving**: `remember_taste` stores what the user states, deduplicated per user by content hash. After each turn, and from written feedback, a small model extracts up to four durable facts the user stated (`app/services/memory_extraction.py`). Anything temporary, any question, and sensitive categories are dropped. A privacy filter checks the output as well. Every memory is visible and editable under My taste, can be undone, and nothing is stored when memory is turned off.
- **Long chats**: after eight messages, and every fourth message after that, a rolling summary of at most 180 words is refreshed (`app/services/memory.py`) and the latest twelve messages stay verbatim.

### Evidence and knowledge layer

`Backend/app/knowledge` checks whether what is already known covers what a question needs, without any model or network call (`KnowledgeResolver`, `EvidenceResolver`). Anything missing is routed by the acquisition router and orchestrator to a TMDB (structured), web, retrieval or vision acquirer, and comes back as normalised evidence with a source tier.

## Architecture

```
Browser (React PWA)
   |  REST + SSE  (cookies + CSRF)
   v
FastAPI app -- rate limits, auth, CSRF -- Redis (limits, guests, working state)
   |
   |- MovieAgent - dispatcher - model turn --> ModelChain --> Groq (chat, background, vision)
   |                               |
   |                               '- TurnTools --> TMDB, Wikidata, MusicBrainz, YouTube
   |                                               Gemini / Groq web search
   |                                               Retrieval: RAGService + MemoryService
   |                                               verified list actions --> PostgreSQL
   v
PostgreSQL + pgvector (27 tables, 30 migrations)
   knowledge_documents / memories (vector 384, HNSW) / messages (full-text)
   ^
Background worker: OTP mail, exports, feedback jobs, purges, alerts, daily brief, Web Push
```

## Repository layout

| Path | Contents |
|---|---|
| `Backend/app/main.py` | FastAPI app, middleware, chat streaming endpoint, rate limits |
| `Backend/app/agent` | The chat turn: dispatcher, model loop, tools, action pipeline, conversation state, context |
| `Backend/app/inference` | Model routing, the Groq provider, failover and waiting (`providers/chain.py`) |
| `Backend/app/rag.py`, `embeddings.py`, `memory.py` | Hybrid retrieval, the embedding model, memory search and profile |
| `Backend/app/knowledge` | Evidence model, sufficiency checks and the acquisition layer |
| `Backend/app/services` | TMDB, web search, recommendations, memory extraction and summaries, reminders, export, OTP mail, daily brief, Web Push |
| `Backend/app/routers` | HTTP endpoints: auth, chat, conversations, movies, people, me, push, sharing, projects, health |
| `Backend/app/jobs` | Background worker and purge jobs |
| `Backend/app/evals` | Live evaluation with a model judge, plus the retrieval and memory benchmarks |
| `Backend/alembic` | Database migrations (PostgreSQL + pgvector) |
| `Backend/tests` | Pytest suite (SQLite by default; PostgreSQL when `POSTGRES_TEST_DATABASE_URL` is set) |
| `Frontend/src` | React 19 + Vite client: `components/`, `features/`, `lib/` |
| `Frontend/e2e` | Playwright end-to-end tests |
| `ops/` | Observability stack (Prometheus, Grafana, Alertmanager), backups, load tests, staging runbook |
| `scripts/` | Release artifact builder, live-eval runner and CI helpers |
| `.github/workflows` | CI, CodeQL and the manual live-quality evaluation |

## Quick start

You need API keys for [Groq](https://console.groq.com), [TMDB](https://www.themoviedb.org/settings/api) and [Gemini](https://aistudio.google.com/apikey). OMDb is optional.

1. Copy `.env.example` to `.env` and fill in real values: `GROQ_API_KEY`, `TMDB_API_KEY`, `GEMINI_API_KEY` and every secret the file lists. `.env` is gitignored; never put real credentials in `.env.example`.
2. Start everything:

   ```bash
   docker compose up --build
   ```

   This starts PostgreSQL with pgvector, Redis, the role bootstrap and migration jobs, the API, the background worker and the frontend on <http://localhost>.

Without Docker (Python 3.12, Node 24):

```bash
cd Backend
python -m pip install --require-hashes -r requirements-dev.lock
python -m pip install --require-hashes -r requirements-embeddings.lock   # optional: enables vector search
alembic upgrade head
uvicorn app.main:app --reload

cd ../Frontend
npm ci
npm run dev
```

**Changing database role names?** The Compose stack creates the roles `grace_app` and `grace_migrator`. A database volume created earlier under different role names must be recreated (`docker compose down -v`, which erases local data) or have its roles renamed with `ALTER ROLE ... RENAME TO ...` before the migration job can run.

## Configuration

All settings come from environment variables (see `.env.example`; defaults in `Backend/app/config.py`).

| Group | Variables |
|---|---|
| Database and Redis | `POSTGRES_*`, `APP_DB_USER/PASSWORD`, `MIGRATOR_DB_USER/PASSWORD`, `DATABASE_URL`, `REDIS_PASSWORD`, `REDIS_URL`, `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` |
| Providers | `GROQ_API_KEY`, `TMDB_API_KEY`, `GEMINI_API_KEY`, `OMDB_API_KEY` |
| Models | `GROQ_DEEP_MODEL` answers every chat turn. `GROQ_MODEL` and `GROQ_SUMMARY_MODEL` run background helpers (titles, summaries, review analysis, memory extraction) with `GROQ_FALLBACK_MODELS` behind them. `GROQ_VISION_MODEL` answers about attached images. `GEMINI_MODEL` does web search first and grades the live evaluation; Groq `browser_search` is the fallback. |
| Embeddings | `EMBEDDING_MODEL` (default `BAAI/bge-small-en-v1.5`, 384 dimensions). The `INSTALL_EMBEDDINGS` build argument in `docker-compose.yml` installs the embedding dependencies into the images. |
| Security | `JWT_SECRET`, `JWT_SIGNING_KEY`, `OTP_PEPPER`, `CSRF_KEY`, `CORS_ORIGINS`, `FORWARDED_ALLOW_IPS` |
| Mail | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_EMAIL` |
| Web Push | `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_SUBJECT` (push is off without keys) |
| Rate limits | `LOGIN_RATE_LIMIT`, `REGISTER_RATE_LIMIT`, `CHAT_RATE_LIMIT`, `CHAT_IP_RATE_LIMIT` (search and browse calls have their own, higher limits) |
| Observability | `SENTRY_DSN`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `METRICS_BEARER_TOKEN`, `ALERT_WEBHOOK_URL`, `GRAFANA_ADMIN_PASSWORD` |

`ENVIRONMENT=production` turns on production-only safeguards: required secrets, CSRF enforcement, `Secure` + `__Host-` cookies, fail-closed password-breach checks and hidden API docs. Development uses plain cookie names so sign-in works over HTTP.

## Tests and evaluation

```bash
cd Backend && pytest -q                                          # backend
POSTGRES_TEST_DATABASE_URL=postgresql+psycopg://... pytest -q    # include PostgreSQL-only tests
cd Frontend && npx vitest run && npm run lint                    # frontend unit tests and lint
cd Frontend && npx playwright test                               # end to end
node scripts/verify_frontend_runtime_contracts.cjs               # client/server contract check
```

Tests use scripted models: they prove plumbing and policy, not answer quality. Retrieval and memory have their own tests (`test_rag.py`, `test_semantic_rerank.py`, `test_memory_search_contract.py`, `test_memory_dedup.py`) and two benchmarks, run from `Backend/` against a configured database:

```bash
python -m app.evals.rag_relevance    # does the right research snippet rank first? (3 films x 6 aspects, lexical path)
python -m app.evals.memory_recall    # do 10 stated facts hidden among 50 unrelated memories reach the model?
```

The memory benchmark reports `search_hit`, `in_prompt`, `recall_hit` and `reachable` over 12 questions that use shared keywords, paraphrases and another language. The suite also runs both (`test_rag_relevance_benchmark.py`, `test_memory_recall_benchmark.py`).

Answer quality is measured only by the **live evaluation**, which drives the real app with the real providers. A model judge (Gemini, never the model that answered) grades every scenario's criteria and rubric against the transcript and the app's record of verified changes.

```powershell
scripts/run-live-eval.ps1        # Windows, against the Docker Compose stack
```

or `python -m app.evals.live_agent --out <dir>` from `Backend/` with the app's environment. On GitHub, run the "Agent quality evaluation" workflow manually; it needs the `GROQ_API_KEY`, `TMDB_API_KEY` and `GEMINI_API_KEY` repository secrets.

## CI and operations

- **CI** (`.github/workflows/ci.yml`) runs on every push: backend tests and linters (black, ruff, mypy baseline), migrations up/down/up, security scans (bandit, secret scan, dependency audit, Semgrep), the release-artifact build, container scans, SBOM, a production-mode smoke test, frontend lint/tests/build/Playwright and a changed-lines coverage gate. CodeQL runs separately.
- **Observability** (`ops/docker-compose.observability.yml`): Prometheus metrics with alert rules, Grafana dashboards, Alertmanager, optional OpenTelemetry and Sentry.
- **Backups**: `ops/backup/backup-postgres.sh` and a restore drill (`ops/backup/restore-drill.sh`).
- **Staging**: `ops/STAGING_ACCEPTANCE_RUNBOOK.md`; load test in `ops/loadtest/chat.js`.

## How the main behaviours work

- **Recommendations** (`app/services/recommendation_engine.py`): the model passes every constraint the user stated. Hard constraints (runtime, year, genre, language, exclusions, already watched) are verified against TMDB details in code. A film that breaks a hard constraint is never returned as a match; a runtime overflow of up to 15 minutes is only offered as a labelled near match. Moods, "like X" and "not the obvious one" only affect ranking.
- **List changes** (`app/agent/actions.py`, `ledger.py`): resolve the film, check the idempotency ledger, write, read back, then confirm. The user sees a card with Undo. Guests can keep a watchlist; ratings and reminders need an account.
- **Personal taste**: saved genre dislikes become recommendation exclusions unless the current request asks for that genre. A newer statement replaces a contradicting one.
- **Movie Intelligence** (`GET /api/v1/movies/{id}/intelligence/{facts|awards|music|locations}`): structured records with their sources and a coverage note. Awards separate wins from nominations; locations keep story settings apart from filming locations; facts are labelled as reported claims tied to a source.
- **Visual intelligence** (`app/services/visual_intelligence.py`): claims are typed documented / observed / inferred / interpreted, and coverage states how many published stills were analysed. It never claims full-film analysis.
- **Staying in touch, on the user's terms**: reminders and alerts (a saved film is out, newly streaming in the user's region, a sequel) appear in the notification centre. An opt-in daily brief (`app/services/daily_brief.py`) sums up what is new, what is due today, what is coming out from the watchlist and one film for tonight, using only the user's own data. Notifications on a device use standard Web Push (`app/services/web_push.py`): opt-in per browser, encrypted end to end, each sent once.
- **Answer length** (Settings > Personalisation): brief, balanced or detailed by default. What a message asks for always wins.
- **Voice**: dictation in the message box and "Read aloud" on answers, using the browser's own speech features where available.
- **Honesty**: Grace says it is an AI when asked, and says plainly when it cannot do or verify something.

## Security model

- Passwords are hashed with Argon2id and checked for strength and known breaches. One-time codes are stored as peppered hashes.
- Sign-in sets HttpOnly cookies. State-changing requests need a CSRF cookie plus header. Refresh tokens rotate and reuse of an old one revokes the session.
- Per-user, per-IP and per-guest rate limits, with separate budgets for model-backed calls and for plain browsing.
- Web Push accepts only push-service endpoints, which prevents server-side request forgery.
- The write gate described above means text from the web or from movie data can never change a user's data on its own.
- Memory extraction drops sensitive categories, and every memory can be viewed, edited, undone or switched off.
- Secrets live in `.env` (gitignored); a CI secret scan and a repository hygiene check keep keys, local work files and caches out of the repository.

## Frontend state ownership contract

Every piece of client state has exactly one authority:

- **Server-authoritative state**: account, conversations, messages, watchlist, watched items, ratings, preferences, memories and settings (including the streaming region). The backend is the source of truth; the client caches it with React Query and re-fetches after mutations instead of trusting optimistic copies.
- **Session-scoped local state**: unsent drafts (`grace_draft_*` in `sessionStorage`, cleared on sign-out), the active panel and transient UI toggles. It never outlives the browser session and is never sent as truth.
- **Request-control state**: AbortControllers, request generations, in-flight flags and stream status. It exists only to cancel, de-duplicate or discard stale responses and is never persisted.
