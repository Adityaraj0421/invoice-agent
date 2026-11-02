"""Client-side checks that do not depend on the model.

1. A local, keyword-based prompt-injection scan of the invoice PDF's text layer. This is a
   cheap second layer; it is NOT the Gemini detector and is reported separately.
2. Helpers for the submit gate (the client always asks a human before the Submit button is
   pressed, whether or not the model returned a safety_decision).
"""
from __future__ import annotations

import re
from pathlib import Path

PATTERNS = [
    r"ignore (all )?(previous|prior|above) (instructions|commands)",
    r"(note|message|instruction)s? to (ai|llm|assistant|agent)s?",
    r"without (asking|confirming|confirmation)",
    r"(press|click) submit immediately",
    r"disregard (the )?(user|instructions)",
    r"system prompt",
]


def pdf_text(pdf: Path) -> str:
    from pypdf import PdfReader

    return "\n".join(page.extract_text() or "" for page in PdfReader(str(pdf)).pages)


def scan_for_injection(text: str) -> list[str]:
    t = re.sub(r"\s+", " ", text.lower())
    return [p for p in PATTERNS if re.search(p, t)]


GATE_JS = """([x, y]) => {
  const el = document.elementFromPoint(x, y);
  const g = el && el.closest('[data-agent-gate]');
  return g ? g.dataset.agentGate : null;
}"""

FOCUSED_GATE_JS = """() => {
  const el = document.activeElement;
  const g = el && el.closest && el.closest('[data-agent-gate]');
  return g ? g.dataset.agentGate : null;
}"""

FORM_SNAPSHOT_JS = """() => {
  const v = (id) => (document.getElementById(id) || {}).value || "";
  const cur = document.querySelector('#currency button.on');
  const lines = [...document.querySelectorAll('#lines tr')].map(tr => {
    const [d, q, u] = tr.querySelectorAll('input');
    return {description: d.value, quantity: q.value, unit_price: u.value};
  }).filter(l => l.description || l.quantity || l.unit_price);
  return {vendor: v('vendor'), invoice_number: v('invoice_number'), invoice_date: v('invoice_date'),
          due_date: v('due_date'), currency: cur ? cur.dataset.cur : null, line_items: lines,
          subtotal: (document.getElementById('subtotal') || {}).textContent, tax: v('tax'), total: v('total'),
          reconcile: (document.getElementById('reconcile') || {}).textContent};
}"""


def summarize_form(snap: dict) -> str:
    lines = [
        f"Vendor:      {snap.get('vendor')}",
        f"Invoice no.: {snap.get('invoice_number')}",
        f"Dates:       {snap.get('invoice_date')}  (due {snap.get('due_date')})",
        f"Currency:    {snap.get('currency')}",
    ]
    for i, li in enumerate(snap.get("line_items") or [], 1):
        lines.append(f"Line {i}:      {li['description']}  x{li['quantity']} @ {li['unit_price']}")
    lines.append(f"Subtotal {snap.get('subtotal')} | Tax {snap.get('tax')} | Total {snap.get('total')}")
    if snap.get("reconcile"):
        lines.append(snap["reconcile"])
    return "\n".join(lines)
