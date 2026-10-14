# e-Tender Analyzer Worker

FastAPI + Playwright worker for collecting product rows from public e-tender competition pages and persisting them to Supabase.

## Important status

This repository contains an initial implementation, **not yet deployed or verified against live portal pages**. Test only a very small ID range first. Portal HTML, access requirements, and current table headers may differ from the parser assumptions.

## Requirements

- Supabase project with tables `etender_products` and `etender_page_checks` (the product table needs `row_key` as a text primary key).
- A Docker-capable worker host with Playwright Chromium support.
- Keep `SUPABASE_SERVICE_ROLE_KEY` and `WORKER_API_KEY` private in server environment variables only. Never put them in browser code or commit real values.

## Environment

Copy `.env.example` to `.env` for local testing and fill in server-side secrets. Do not commit `.env`.

## Run locally

```bash
docker build -t etender-worker .
docker run --rm -p 8000:8000 --env-file .env etender-worker
```

Then open `http://localhost:8000/docs` and `http://localhost:8000/health`.

## API

All scan endpoints require `X-API-Key: <WORKER_API_KEY>`.

- `GET /health` — liveness check
- `POST /scan/start` — JSON body: `{"start_id":346000,"end_id":346001,"delay_seconds":1.2}`
- `GET /scan/status` — in-memory current scan status

The range is bounded by `MAX_SCAN_PAGES` (default 100). Start with 1–2 IDs. This initial version does not bypass login, CAPTCHA, throttling, or other access controls.

## Deploy

`render.yaml` is a starting blueprint for a free Render web service; free-tier limits and worker availability can change. Select the repository in the hosting dashboard, configure the three secret environment variables, and deploy the Docker service. Verify the `/health` endpoint before scanning. A sleeping free service may have cold starts and is not a guarantee of continuous long-running scan availability.
