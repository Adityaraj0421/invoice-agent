"""End-to-end through the real browser + mock app, with the scripted (no-API) model."""
import json

import httpx
import pytest

from agent.run import INVOICES, run_agent
from agent.ui import AutoConfirmer
from bench.accuracy import score


@pytest.mark.parametrize("inv", ["inv-01-larkspur", "inv-03-saffron"])
def test_mock_run_fills_form_exactly(base_url, inv):
    r = run_agent(inv, base_url, mock=True, confirmer=AutoConfirmer(True), quiet=True)
    assert r.status == "submitted"
    assert r.confirmations_requested == 1 and r.confirmations_from_model == 1
    truth = json.loads((INVOICES / f"{inv}.json").read_text())
    s = score(r.submission, truth)
    assert s["correct"] == s["total"], s["fields"]


def test_rejecting_confirmation_submits_nothing(base_url):
    r = run_agent("inv-02-brightline", base_url, mock=True, confirmer=AutoConfirmer(False), quiet=True)
    assert r.status == "rejected_by_human"
    assert r.submission is None
    assert httpx.get(f"{base_url}/api/bills", params={"run_id": r.run_id}).json() == []


def test_injection_invoice_is_blocked(base_url):
    r = run_agent("inv-05-pinecrest-injection", base_url, mock=True, confirmer=AutoConfirmer(True), quiet=True)
    assert r.status == "blocked_by_model"
    assert r.injection_blocks == 1 and r.local_guard_hits
    assert r.submission is None


def test_local_guard_block_policy_stops_before_model(base_url):
    r = run_agent("inv-05-pinecrest-injection", base_url, mock=True, confirmer=AutoConfirmer(True),
                  quiet=True, local_guard="block")
    assert r.status == "blocked_by_local_guard" and r.turns == 0


def test_client_gate_fires_without_model_safety_decision(base_url, tmp_path):
    """Replay a trace with the safety_decision stripped: the client gate must still ask before Submit."""
    r1 = run_agent("inv-04-kestrel", base_url, mock=True, confirmer=AutoConfirmer(True), quiet=True)
    from agent.run import RUNS
    lines = []
    for line in (RUNS / r1.run_id / "trace.jsonl").read_text().splitlines():
        rec = json.loads(line)
        for s in rec.get("steps", []):
            (s.get("arguments") or {}).pop("safety_decision", None)
        lines.append(json.dumps(rec))
    p = tmp_path / "trace.jsonl"; p.write_text("\n".join(lines))
    r2 = run_agent("inv-04-kestrel", base_url, replay=p, confirmer=AutoConfirmer(False), quiet=True)
    assert r2.confirmations_from_client_gate == 1 and r2.confirmations_from_model == 0
    assert r2.status == "rejected_by_human" and r2.submission is None


def test_agent_browser_cannot_reach_console(base_url):
    from playwright.sync_api import sync_playwright
    from agent.browser import BrowserEnv

    with sync_playwright() as pw:
        b = pw.chromium.launch()
        page = b.new_page()
        env = BrowserEnv(page, 1440, 900, base_url)
        env.install_allowlist()
        with pytest.raises(Exception):
            page.goto(base_url + "/console")
        assert any("/console" in u for u in env.blocked_requests)
        out = env.execute("navigate", {"url": "https://example.com"})
        assert "blocked" in out["error"]
        b.close()


def test_confirm_endpoint_requires_console_token(base_url):
    cid = httpx.post(f"{base_url}/api/confirm", json={"explanation": "x"}).json()["id"]
    assert httpx.post(f"{base_url}/api/confirm/{cid}/decide", json={"decision": "approve", "token": "nope"}).status_code == 403
    from app.server import CONSOLE_TOKEN
    assert httpx.post(f"{base_url}/api/confirm/{cid}/decide",
                      json={"decision": "approve", "token": CONSOLE_TOKEN}).json()["status"] == "approved"


def test_gemini_model_refuses_without_key():
    from agent.models import GeminiModel
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        GeminiModel("gemini-3.8-flash")
