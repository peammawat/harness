# Onebix Harness

A multi-provider AI agent harness with pluggable web search, exposed as a
REST API (with API-key auth) plus a built-in chat web UI (username/password login).

- **Multi-provider LLM** — Claude (default `claude-opus-4-8`), OpenAI, or any
  OpenAI-compatible local server (Ollama / vLLM), switchable per request.
- **7 search backends** behind one interface — Searxng, Brave, Tavily, Serper,
  Perplexity, Google CSE, DuckDuckGo. The agent calls them via a `web_search`
  tool; you can also hit them directly via `/v1/search` (single or aggregated).
- **REST API + chat UI** — SSE-streamed `/v1/chat`, OpenAPI docs at `/docs`,
  and a zero-build static chat UI at `/`.

## Architecture

```
app/
  config.py            Settings (env / .env)
  schemas.py           API request/response models
  search/              SearchProvider ABC + 7 backends + registry (aggregate)
  llm/                 LLMProvider ABC (normalized events) + anthropic/openai + registry
  agent/               web_search tool + provider-agnostic agent loop
  api/                 auth dependency + routes (chat, search, health)
  main.py              FastAPI app, mounts the static UI
web/                   static chat UI (index.html / app.js / style.css)
deploy/                Dockerfile + docker-compose (app + searxng + redis)
tests/                 pytest (fakes + mocked HTTP)
```

The two abstractions (`LLMProvider`, `SearchProvider`) mean adding a provider is
one new file + one registry line — the agent loop never changes.

## Quick start (local)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
# Edit .env: set API_KEYS and at least one provider key (e.g. ANTHROPIC_API_KEY).
# DuckDuckGo works with no key, so you can try search immediately.

uvicorn app.main:app --reload
```

Open <http://localhost:8000/> for the chat UI, or <http://localhost:8000/docs>
for the API explorer.

### Web login

The chat UI is gated by a username/password login. Configure accounts in
`AUTH_USERS` as comma-separated `user:password` pairs, and set `AUTH_SECRET` to a
long random value (used to sign session tokens):

```bash
AUTH_USERS=alice:secret,bob:hunter2
AUTH_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
```

`POST /auth/login` returns a signed token the browser stores and sends as
`Authorization: Bearer <token>`; the `/v1/*` endpoints accept **either** that
token (browser) **or** an `X-API-Key` (programmatic clients).

### Generate an API key

```bash
python -c "import secrets; print('sk-harness-'+secrets.token_urlsafe(32))"
```

Put it in `API_KEYS` (comma-separated for multiple) and send it as the
`X-API-Key` header.

## API

All `/v1/*` endpoints require auth: either an `Authorization: Bearer <token>`
(from `/auth/login`) or an `X-API-Key` header.

### `POST /v1/search`

```bash
curl -s localhost:8000/v1/search -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"query":"claude opus 4.8","backend":"duckduckgo","num_results":5}'
```

Set `"aggregate": true` (optionally `"backends": ["brave","tavily"]`) to fan out
across multiple configured backends and merge/dedupe by URL.

### `POST /v1/chat` (SSE)

```bash
curl -N localhost:8000/v1/chat -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"What did Anthropic release this week?"}],
       "provider":"anthropic","search_backend":"duckduckgo"}'
```

Streams `token`, `tool_call`, `tool_result`, and `done` SSE events. Pass
`"stream": false` for a single JSON response. Pick the model with `"model"`,
the LLM with `"provider"` (`anthropic` | `openai` | `local`), and disable
tools with `"enable_search": false`.

### `GET /v1/capabilities`

Lists which providers/backends are actually configured (no auth).

## Run with Docker (includes Searxng)

```bash
cp .env.example .env   # fill in API_KEYS + provider keys
docker compose -f deploy/docker-compose.yml up --build
```

This starts the app (`:8000`), a Searxng instance (`:8080`), and Redis. The app
is pre-wired to `SEARXNG_URL=http://searxng:8080`, so `backend=searxng` works
out of the box. Change `deploy/searxng/settings.yml` `secret_key` before any
non-local deployment.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

Covers the agent tool-use loop, aggregate dedupe, and API auth — all with fakes
/ mocked HTTP, so no network or API keys are needed.

## Notes

- **Auth** lives in `app/api/auth.py` (login + stateless HMAC session tokens)
  and the `require_auth` dependency (`app/api/deps.py`), which accepts a Bearer
  token or an `X-API-Key`. Web accounts come from `AUTH_USERS`; programmatic keys
  from `API_KEYS`. For per-user tracking / a management dashboard, swap these env
  vars for a small SQLite table behind the same `require_auth` dependency.
- **The "local" provider** reuses the OpenAI client against
  `LOCAL_OPENAI_BASE_URL` — point it at Ollama/vLLM/LM Studio.
- The Claude provider uses adaptive thinking + `effort: high` and preserves
  native content blocks across tool turns, so multi-step tool use round-trips
  correctly.
