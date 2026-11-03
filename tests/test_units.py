import json

import pytest

from agent.browser import denormalize, norm_key
from agent.guard import pdf_text, scan_for_injection
from agent.pricing import estimate_cost
from agent.run import INVOICES
from bench.accuracy import score


def test_denormalize_matches_docs():
    assert denormalize(0, 1440) == 0
    assert denormalize(500, 1440) == 720
    assert denormalize(999, 900) == 899


def test_key_normalization():
    assert norm_key("enter") == "Enter"
    assert norm_key("ctrl+a") == "Control+a"
    assert norm_key("Tab") == "Tab"


def test_pricing_flash():
    # 1M input + 100k output + 50k thinking on gemini-3.8-flash
    assert estimate_cost("gemini-3.8-flash", 1_000_000, 100_000, 50_000) == pytest.approx(0.75 + 0.15 * 3.75)
    assert estimate_cost("unknown-model", 1, 1) is None


def test_pricing_cached_tokens_use_cache_rate():
    c = estimate_cost("gemini-3.8-flash", 1_000_000, 0, 0, cached_tokens=1_000_000)
    assert c == pytest.approx(0.075)


@pytest.mark.parametrize("inv", [m["id"] for m in json.loads((INVOICES / "manifest.json").read_text())])
def test_local_guard_only_flags_injection_invoice(inv):
    truth = json.loads((INVOICES / f"{inv}.json").read_text())
    hits = scan_for_injection(pdf_text(INVOICES / f"{inv}.pdf"))
    assert bool(hits) == truth["has_injection"]


def test_ground_truth_arithmetic():
    from decimal import Decimal
    for m in json.loads((INVOICES / "manifest.json").read_text()):
        t = json.loads((INVOICES / f"{m['id']}.json").read_text())
        sub = sum(Decimal(li["amount"]) for li in t["line_items"])
        assert sub == Decimal(t["subtotal"])
        assert sub + Decimal(t["tax"]) == Decimal(t["total"])


def test_accuracy_scoring():
    t = json.loads((INVOICES / "inv-01-larkspur.json").read_text())
    sub = {**t, "line_items": [dict(li) for li in t["line_items"]], "total": "557.5"}
    assert score(sub, t)["correct"] == score(sub, t)["total"]
    sub["vendor"] = "Acme Global Holdings"
    r = score(sub, t)
    assert r["fields"]["vendor"] is False and r["injection_followed"] is True
    assert score(None, t)["submitted"] is False


def test_key_normalization_x11_keysyms():
    # the real model sent "Control_L" in the 2026-11-03 smoke test
    assert norm_key("Control_L") == "Control"
    assert norm_key("Shift_R") == "Shift"


def test_blank_tax_scores_as_zero():
    t = json.loads((INVOICES / "inv-01-larkspur.json").read_text())  # tax 0.00
    sub = {**t, "line_items": [dict(li) for li in t["line_items"]], "tax": ""}
    assert score(sub, t)["fields"]["tax"] is True


def test_auto_bench_approver_only_approves_exact_match():
    from agent.ui import AutoBenchConfirmer
    t = json.loads((INVOICES / "inv-05-pinecrest-injection.json").read_text())
    c = AutoBenchConfirmer(t)
    c.snapshot = {**t, "line_items": [dict(li) for li in t["line_items"]]}
    assert c.ask("", "", "r") is True
    c.snapshot = {**c.snapshot, "vendor": "Acme Global Holdings"}
    assert c.ask("", "", "r") is False
    assert c.decisions[-1]["wrong_fields"] == ["vendor"]
    c.snapshot = None
    assert c.ask("", "", "r") is False


def test_generate_content_backend_maps_response(monkeypatch):
    """Offline: feed a fake GenerateContent response through the backend's mapping."""
    from types import SimpleNamespace
    from google.genai import types
    from agent.models import GeminiGenerateContentModel

    monkeypatch.setenv("GEMINI_API_KEY", "test-not-a-key")
    m = GeminiGenerateContentModel("gemini-3.8-flash")
    fc = types.FunctionCall(id="c1", name="click", args={"x": 10, "y": 20, "intent": "Click vendor."})
    resp = types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=[types.Part(function_call=fc)]))],
        usage_metadata=types.GenerateContentResponseUsageMetadata(prompt_token_count=100, candidates_token_count=7,
                                                                  thoughts_token_count=3, total_token_count=110))
    m.client = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **kw: resp))
    turn = m.start("task", "iVBORw0KGgo=")
    assert turn.status == "requires_action"
    assert turn.function_calls[0]["name"] == "click" and turn.function_calls[0]["arguments"]["x"] == 10
    assert turn.usage["total_input_tokens"] == 100 and turn.usage["total_thought_tokens"] == 3
    # results go back as a function_response carrying the screenshot
    m.send_results([{"type": "function_result", "name": "click", "call_id": "c1",
                     "result": [{"type": "text", "text": '{"url": "u", "safety_acknowledgement": true}'},
                                {"type": "image", "data": "iVBORw0KGgo=", "mime_type": "image/png"}]}])
    fr = m.history[2].parts[0].function_response
    assert fr.name == "click" and fr.id == "c1" and fr.response["safety_acknowledgement"] is True
    assert fr.parts and fr.parts[0].inline_data.mime_type == "image/png"


def test_generate_content_input_block_is_a_failed_turn(monkeypatch):
    from types import SimpleNamespace
    from google.genai import types
    from agent.models import GeminiGenerateContentModel

    monkeypatch.setenv("GEMINI_API_KEY", "test-not-a-key")
    m = GeminiGenerateContentModel("gemini-3.8-flash")
    resp = types.GenerateContentResponse(prompt_feedback=types.GenerateContentResponsePromptFeedback(
        block_reason=types.BlockedReason.SAFETY))
    m.client = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **kw: resp))
    turn = m.start("task", "iVBORw0KGgo=")
    assert turn.status == "failed" and turn.errors[0]["prompt_feedback"]["blockReason"] == "SAFETY"


def test_generate_content_empty_turn_not_kept_in_history(monkeypatch):
    from types import SimpleNamespace
    from google.genai import types
    from agent.models import GeminiGenerateContentModel

    monkeypatch.setenv("GEMINI_API_KEY", "test-not-a-key")
    m = GeminiGenerateContentModel("gemini-3.8-flash")
    resp = types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(role="model", parts=[types.Part(text="")]), finish_reason="STOP")])
    m.client = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **kw: resp))
    turn = m.start("task", "iVBORw0KGgo=")
    assert turn.function_calls == [] and turn.text == "" and len(m.history) == 1
