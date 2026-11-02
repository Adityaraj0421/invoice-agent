"""Ledgerly: a mock accounting app for the invoice-agent demo.

Runs entirely on localhost. Nothing here talks to a real accounting system.

    uvicorn app.server:app --port 8765
"""
from __future__ import annotations

import asyncio
import json
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).parent / "static"
INVOICES = ROOT / "invoices"
DATA = Path(__import__("os").environ.get("LEDGERLY_DATA_DIR", ROOT / "data"))
SUBMISSIONS = DATA / "submissions.json"

# Token that the human-facing console must present to approve/reject a submit.
# The agent's browser is blocked from loading /console, so the model never sees it.
CONSOLE_TOKEN = secrets.token_urlsafe(16)

app = FastAPI(title="Ledgerly (mock)")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

_lock = threading.Lock()
_events: list[dict[str, Any]] = []
_confirms: dict[str, dict[str, Any]] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_submissions() -> list[dict[str, Any]]:
    if not SUBMISSIONS.exists():
        return []
    try:
        return json.loads(SUBMISSIONS.read_text() or "[]")
    except json.JSONDecodeError:
        return []


@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse("/app")


@app.get("/app")
def app_page() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/console", response_class=HTMLResponse)
def console_page() -> str:
    html = (STATIC / "console.html").read_text()
    return html.replace("__CONSOLE_TOKEN__", CONSOLE_TOKEN)


@app.get("/invoices/{name}")
def invoice_file(name: str) -> FileResponse:
    path = (INVOICES / name).resolve()
    if path.parent != INVOICES.resolve() or path.suffix not in {".png", ".pdf"} or not path.exists():
        raise HTTPException(404)
    return FileResponse(path)


@app.get("/api/invoices")
def list_invoices() -> list[dict[str, Any]]:
    return json.loads((INVOICES / "manifest.json").read_text())


@app.post("/api/bills")
async def create_bill(request: Request) -> dict[str, Any]:
    body = await request.json()
    record = {
        "bill_id": f"BILL-{uuid.uuid4().hex[:6].upper()}",
        "saved_at": _now(),
        **{k: body.get(k) for k in (
            "run_id", "mode", "invoice_ref", "vendor", "invoice_number", "invoice_date", "due_date",
            "currency", "line_items", "subtotal", "tax", "total", "entry_seconds")},
    }
    with _lock:
        DATA.mkdir(parents=True, exist_ok=True)
        rows = _read_submissions()
        rows.append(record)
        SUBMISSIONS.write_text(json.dumps(rows, indent=2) + "\n")
    _publish({"kind": "saved", "run_id": record["run_id"], "bill_id": record["bill_id"]})
    return record


@app.get("/api/bills")
def get_bills(run_id: str | None = None) -> list[dict[str, Any]]:
    rows = _read_submissions()
    return [r for r in rows if run_id is None or r.get("run_id") == run_id]


# ---------- agent event stream (agent -> server -> console) ----------

def _publish(evt: dict[str, Any]) -> None:
    evt = {"ts": time.time(), **evt}
    with _lock:
        _events.append(evt)
        del _events[:-500]


@app.post("/api/events")
async def post_event(request: Request) -> dict[str, bool]:
    _publish(await request.json())
    return {"ok": True}


@app.get("/api/events/stream")
async def stream_events(request: Request) -> StreamingResponse:
    async def gen():
        idx = len(_events)
        yield "retry: 1000\n\n"
        while not await request.is_disconnected():
            with _lock:
                new = _events[idx:]
                idx = len(_events)
            for e in new:
                yield f"data: {json.dumps(e)}\n\n"
            await asyncio.sleep(0.2)
    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------- human confirmation (agent asks, console answers) ----------

@app.post("/api/confirm")
async def create_confirm(request: Request) -> dict[str, str]:
    body = await request.json()
    cid = uuid.uuid4().hex[:10]
    with _lock:
        _confirms[cid] = {"id": cid, "status": "pending", "explanation": body.get("explanation", ""),
                          "run_id": body.get("run_id"), "summary": body.get("summary")}
    _publish({"kind": "confirm_request", "confirm_id": cid, **body})
    return {"id": cid}


@app.get("/api/confirm/{cid}")
def get_confirm(cid: str) -> dict[str, Any]:
    if cid not in _confirms:
        raise HTTPException(404)
    return _confirms[cid]


@app.post("/api/confirm/{cid}/decide")
async def decide_confirm(cid: str, request: Request) -> dict[str, Any]:
    body = await request.json()
    if body.get("token") != CONSOLE_TOKEN:
        raise HTTPException(403, "bad console token")
    if cid not in _confirms or _confirms[cid]["status"] != "pending":
        raise HTTPException(409, "not pending")
    decision = "approved" if body.get("decision") == "approve" else "rejected"
    _confirms[cid]["status"] = decision
    _publish({"kind": "confirm_decided", "confirm_id": cid, "decision": decision})
    return _confirms[cid]
