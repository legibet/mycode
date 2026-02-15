# mycode

A personal minimal coding agent (Python) with a web UI.

Design principles (inspired by pi):

- **Minimal core**: only 4 tools: `read`, `write`, `edit`, `bash`
- **Clean agent loop**: streaming + tool loop, no extra framework features
- **Low token overhead**: truncation everywhere, explicit "use rg" guidance
- **Sessions are append-only**: JSONL message log per session

## Run (backend)

```bash
cd mycode
uv run uvicorn app.main:app --reload --port 8000
```

Then open the frontend dev server or build the static frontend.

## Run (frontend)

```bash
cd mycode/frontend
npm install
npm run dev
```

Configure the backend URL via Vite proxy if needed.

## CLI

```bash
cd mycode
uv run python cli.py
```

## Environment

- `MODEL` (e.g. `anthropic:claude-sonnet-4-5`)
- `BASE_URL` (optional)

API keys:
- `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY`
- Or pass `api_key` from the web UI (per request)
