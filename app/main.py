from __future__ import annotations
import asyncio
import secrets
from datetime import datetime, timezone
from typing import Any
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from supabase import create_client, Client
from app.config import settings
from app.scraper import scrape_page

app = FastAPI(title="e-Tender Analyzer Worker", version="0.1.1")
_lock = asyncio.Lock()
_job: dict[str, Any] = {
    "status": "idle", "start_id": None, "end_id": None, "current_id": None,
    "pages_checked": 0, "products_saved": 0, "errors": 0,
    "started_at": None, "finished_at": None, "message": "No scan has been started"
}

def require_key(x_api_key: str | None = Header(default=None)) -> None:
    if not settings.worker_api_key:
        raise HTTPException(status_code=503, detail="WORKER_API_KEY is not configured")
    if not x_api_key or not secrets.compare_digest(x_api_key, settings.worker_api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")

def get_supabase() -> Client:
    if not settings.supabase_url or not settings.supabase_service_role_key:
        raise HTTPException(status_code=503, detail="Supabase server credentials are not configured")
    # Accept either the project URL or the Data API URL pasted from Supabase.
    url = settings.supabase_url.strip().rstrip("/")
    if url.endswith("/rest/v1"):
        url = url[:-len("/rest/v1")]
    return create_client(url, settings.supabase_service_role_key)

class ScanRequest(BaseModel):
    start_id: int = Field(ge=1)
    end_id: int = Field(ge=1)
    delay_seconds: float = Field(default=1.2, ge=0.5, le=30)

@app.get("/health")
async def health():
    return {"ok": True, "service": "e-tender-analyzer-worker", "timestamp": datetime.now(timezone.utc).isoformat()}

@app.get("/")
async def root():
    return {"service": "e-Tender Analyzer Worker", "docs": "/docs", "health": "/health"}

@app.get("/scan/status", dependencies=[Depends(require_key)])
async def scan_status():
    return dict(_job)

async def persist_page(data: dict[str, Any]) -> int:
    client = get_supabase()
    page_id = data["page_id"]
    products = data.get("products", [])
    if products:
        payload = []
        for product in products:
            row = dict(product)
            row["row_key"] = f'{page_id}:{row.get("row_no", 0)}'
            payload.append(row)
        await asyncio.to_thread(
            lambda: client.table("etender_products").upsert(payload, on_conflict="row_key").execute()
        )
    check = {
        "page_id": page_id, "checked_at": data["collected_at"],
        "status": data["page_status"], "details": data.get("details"),
    }
    await asyncio.to_thread(
        lambda: client.table("etender_page_checks").upsert(check, on_conflict="page_id").execute()
    )
    return len(products)

async def run_scan(start_id: int, end_id: int, delay: float):
    _job.update(status="running", start_id=start_id, end_id=end_id, current_id=None,
                pages_checked=0, products_saved=0, errors=0,
                started_at=datetime.now(timezone.utc).isoformat(), finished_at=None,
                message="Scan started")
    try:
        for page_id in range(start_id, end_id + 1):
            _job["current_id"] = page_id
            try:
                data = await scrape_page(page_id)
                saved = await persist_page(data)
                _job["products_saved"] += saved
                if data["page_status"] not in ("ok", "empty_or_unrecognized"):
                    _job["errors"] += 1
                    _job["message"] = f"Page {page_id}: {data.get('details') or data['page_status']}"
            except Exception as exc:
                _job["errors"] += 1
                _job["message"] = f"Page {page_id}: {str(exc)[:250]}"
            _job["pages_checked"] += 1
            await asyncio.sleep(delay)
    except asyncio.CancelledError:
        _job["status"] = "stopped"
        raise
    finally:
        if _job["status"] == "running":
            _job["status"] = "completed"
        _job["finished_at"] = datetime.now(timezone.utc).isoformat()
        _job["current_id"] = None
        if _job["status"] == "completed" and _job["errors"] == 0:
            _job["message"] = "Scan finished"

@app.post("/scan/start", dependencies=[Depends(require_key)])
async def start_scan(request: ScanRequest):
    if request.end_id < request.start_id:
        raise HTTPException(status_code=400, detail="end_id must be >= start_id")
    if request.end_id - request.start_id + 1 > settings.max_scan_pages:
        raise HTTPException(status_code=400, detail=f"Range exceeds MAX_SCAN_PAGES ({settings.max_scan_pages}).")
    if _lock.locked():
        raise HTTPException(status_code=409, detail="A scan is already running.")
    await _lock.acquire()
    async def managed():
        try:
            await run_scan(request.start_id, request.end_id, request.delay_seconds)
        finally:
            _lock.release()
    asyncio.create_task(managed())
    return {"accepted": True, "status": "starting", "start_id": request.start_id, "end_id": request.end_id}

@app.post("/scan/stop", dependencies=[Depends(require_key)])
async def stop_scan():
    return {"accepted": False, "message": "Immediate stop is not enabled in this initial version; use small bounded ranges."}
