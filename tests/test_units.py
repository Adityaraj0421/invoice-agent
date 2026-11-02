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
