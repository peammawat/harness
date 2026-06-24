# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
pip install -e ".[dev]"          # install app + test deps into a venv
uvicorn app.main:app --reload    # run the API + chat UI at http://localhost:8000
pytest                           # run the full test suite
pytest tests/test_agent_loop.py::test_name -q   # run a single test
docker compose -f deploy/docker-compose.yml up --build   # app + Searxng + Redis
```

Tests use fakes and `respx`-mocked HTTP, so no network access or API keys are needed.
There is no linter configured; match the existing style (`from __future__ import annotations`,
dataclasses, type hints, module docstrings).

## Configuration

All config is `app/config.py:Settings` (pydantic-settings), loaded from env / `.env`
(see `.env.example`). `get_settings()` is `lru_cache`d. A provider/backend whose key is
missing is silently skipped via `is_configured()` — the rest of the system keeps working.

## Auth

Two credential types, both checked by the `require_auth` dependency
(`app/api/deps.py`) which guards every `/v1/*` route:
- **Web login** — username/password accounts from `AUTH_USERS` (`user:pass,...`).
  `POST /auth/login` (`app/api/routes/auth.py`) verifies them and returns a **stateless
  HMAC session token** (`app/api/auth.py`: `create_token`/`verify_token`, signed with
  `token_secret` = `AUTH_SECRET` or fallback `API_KEYS`). The browser stores it and sends
  `Authorization: Bearer <token>`; `GET /auth/me` validates it on page load.
- **Programmatic** — `X-API-Key` checked against `API_KEYS` (comma-separated).

`require_auth` accepts **either**; it returns 401 *before* the handler runs, so a bad
credential never reaches the LLM/search. `/auth/login`, `/v1/capabilities`, `/healthz`,
and the static UI are public.

## Architecture

Two abstractions are the heart of the system. The agent loop is written against them, so
**adding a provider is one new file plus one registry line — no other code changes.**

### LLM providers (`app/llm/`)
- `base.py` defines `LLMProvider` (ABC) and the **normalized types**: `Message`, `ToolCall`,
  `ToolDef`, and streaming events `TextDelta | ToolUseRequest | TurnEnd`. Each provider's
  `stream_turn()` translates its SDK into these events so the agent loop is SDK-agnostic.
- `Message.raw` carries the provider's **native assistant content blocks verbatim** (e.g.
  Anthropic thinking blocks). The agent loop stores it and passes it back unchanged on the
  next turn so multi-step tool use round-trips correctly. It's only ever consumed by the
  provider that produced it (provider is fixed for the duration of a chat).
- `anthropic_provider.py` uses the official SDK with adaptive thinking + `effort: high`.
  `openai_provider.py` backs **two** registry entries: `openai` and `local` (the latter
  points the OpenAI client at `LOCAL_OPENAI_BASE_URL` for Ollama/vLLM/LM Studio).
- `registry.py:LLMRegistry` maps names → instances; `get()` raises if a provider is unknown
  or unconfigured.

### Search backends (`app/search/`)
- `base.py` defines `SearchProvider` (ABC) returning normalized `SearchResult`
  (`app/schemas.py`). Each backend has a class-level `name` and is constructed as
  `cls(client, settings)` (shared `httpx.AsyncClient` + settings), even for keyless ones
  like `duckduckgo`.
- `registry.py:SearchRegistry` lists backends in `_PROVIDER_CLASSES`, keyed by `cls.name`.
  Beyond `search()` it offers `aggregate()`, which fans out across configured backends
  concurrently and merges/dedupes by URL (a backend that errors is skipped, not fatal).

### Agent loop (`app/agent/loop.py`)
`run_agent()` is an async generator driving a provider through a tool-use loop and yielding
SSE-friendly dicts: `token`, `tool_call`, `tool_result`, `done`, `error`. Two tools live in
`tools.py`; `research_tools(search=, fetch=)` assembles the active set from request flags:
- **`web_search`** — runs against the `SearchRegistry` (`enable_search`).
- **`fetch_url`** — fetches a page and returns readable text; available whenever search is on
  (so a pasted link is always readable) or in deep-research mode. Implemented in `fetch.py`,
  which is **SSRF-guarded** (`validate_url`: http(s) only, rejects private/loopback/link-local
  IPs, re-validated after redirects) and caps both downloaded bytes and returned characters.
  HTML → text uses a stdlib `html.parser` (`html_to_text`), no extra deps.

**Two modes**, both gated on the relevant markdown **skill** prepended as a system message
(`skills.py` loads from `agent/skills/`, stripping YAML frontmatter):
- Normal search: `web-search-researcher.md`, budget `MAX_ITERATIONS` (6).
- Deep research (`deep_research` flag): `deep-research.md`, budget `DEEP_RESEARCH_ITERATIONS`
  (16).
On the **final permitted turn** the loop withholds tools and injects a nudge so the model
answers from what it gathered instead of dead-ending on the iteration cap.

### Attachments (`app/agent/extract.py`, `routes/chat.py`)
Chat messages can carry inline base64 attachments (size-capped by `MAX_REQUEST_BYTES`):
- **Images** become `ImagePart`s passed to vision-capable providers (Anthropic always; the
  `local` provider only when `LOCAL_SUPPORTS_VISION`).
- **Documents** (PDF / docx / txt / md) are extracted to text in `_to_messages` and inlined
  into the message as a labelled fenced block. `extract_document_text` dispatches on media
  type then extension (`pypdf` / `python-docx` / UTF-8), truncates to `MAX_DOCUMENT_CHARS`,
  and **never raises** — a bad file yields a short Thai placeholder so it can't 500 the chat.

### Chat history (`app/storage/`)
- `base.py` defines `ConversationStore` (ABC). Every method takes `user` (the identity
  `require_auth` returns), so **isolation is enforced at the storage layer** — a query can
  only touch rows owned by that user. `sqlite_store.py:SqliteConversationStore` is the
  default (one shared `aiosqlite` connection + an `asyncio.Lock`; single-node). Only message
  text (`role` + `content`) is persisted — inline attachments are not.
- Built in `main.py` `lifespan` when `CHAT_HISTORY_ENABLED` (default on) and stored on
  `app.state.conversation_store` (`None` when disabled); exposed via `get_conversation_store`.
  `routes/chat.py` resolves/creates a conversation per request (continuing `conversation_id`
  only if owned by the caller) and persists the latest user message + the assistant answer;
  streaming emits a leading `conversation` SSE event with the id. `routes/conversations.py`
  serves `GET /v1/conversations`, `GET /v1/conversations/{id}`, `DELETE /v1/conversations/{id}`.
  Swapping SQLite for Redis/Postgres = one new `ConversationStore` impl + the `lifespan` line.

### API + app wiring (`app/api/`, `app/main.py`)
- `main.py` `lifespan` builds the shared `httpx.AsyncClient`, both registries, and the
  conversation store once and stores them on `app.state`; `api/deps.py` exposes them as
  FastAPI dependencies along with the `require_auth` auth dependency.
- Routes: `routes/auth.py` (`POST /auth/login`, `GET /auth/me`), `routes/chat.py`
  (`POST /v1/chat`, SSE via `sse-starlette`, or single JSON when `stream=false`),
  `routes/search.py` (`POST /v1/search`, single or `aggregate`),
  `routes/conversations.py` (per-user chat history), `routes/health.py`
  (`GET /v1/capabilities` reports configured providers/backends, no auth).
  All `/v1/*` routes require `require_auth` (Bearer token or `X-API-Key`) except capabilities.
- The static chat UI in `web/` is mounted at `/` **last**, so `/v1/*` and `/docs` take
  precedence.

## Adding a provider/backend (the common change)

1. New file implementing the ABC (`stream_turn` + `is_configured`, or `search` + `is_configured`),
   with a unique `name`.
2. Register it: add to `LLMRegistry._providers` or `SearchRegistry._PROVIDER_CLASSES`.
3. Add any needed key to `Settings` and `.env.example`.
The agent loop, API, and schemas do not change.
