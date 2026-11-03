"""Benchmark: run the agent over N invoices and record time, tokens, cost, accuracy and safety events.

    GEMINI_API_KEY=... python -m bench.benchmark --n 10
    python -m bench.benchmark --mock --n 5        # pipeline check only; numbers are NOT reportable

Writes results/bench-<timestamp>.{json,csv,md}. Confirmations are answered by the scripted
benchmark approver (--confirm auto-bench by default): it approves the Submit click only if the
form matches the invoice's ground truth, and rejects otherwise. Every decision is counted.
Cost uses the prices in agent/pricing.py, from https://ai.google.dev/gemini-api/docs/pricing .
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import httpx

from agent.pricing import PRICES, PRICING_READ_ON, PRICING_URL
from agent.run import INVOICES, make_confirmer, run_agent
from agent.serve import start_server
from bench.accuracy import score

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "results"


def manual_baseline(base_url: str) -> list[float]:
    """Human entry times recorded by the app itself (open /app?invoice=<id>&mode=manual and type it in)."""
    rows = httpx.get(f"{base_url}/api/bills").json()
    return [r["entry_seconds"] for r in rows if r.get("mode") == "manual" and r.get("entry_seconds")]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=5, help="number of runs (cycles through the invoices)")
    ap.add_argument("--invoices", nargs="*", help="invoice ids (default: all in invoices/manifest.json)")
    ap.add_argument("--model", default="gemini-3.8-flash", choices=sorted(PRICES))
    ap.add_argument("--mock", action="store_true", help="scripted, no API key; for testing the pipeline only")
    ap.add_argument("--confirm", default="auto-bench",
                    choices=["auto-bench", "auto-approve", "auto-reject", "terminal", "console"])
    ap.add_argument("--api", default="generate-content", choices=["generate-content", "interactions"])
    ap.add_argument("--max-cost", type=float, default=1.0, help="stop if a single run costs more than this (USD)")
    ap.add_argument("--thinking-level", default="low")
    ap.add_argument("--manual-seconds", type=float, help="optional stopwatch baseline for typing one invoice by hand")
    ap.add_argument("--base-url", help="use a running Ledgerly instead of starting one")
    a = ap.parse_args()

    base_url = a.base_url or start_server()[0]
    ids = a.invoices or [m["id"] for m in json.loads((INVOICES / "manifest.json").read_text())]
    rows = []
    for i in range(a.n):
        inv = ids[i % len(ids)]
        truth = json.loads((INVOICES / f"{inv}.json").read_text())
        r = run_agent(inv, base_url, model_name=a.model, mock=a.mock, confirmer=make_confirmer(a.confirm, base_url, inv),
                      headless=True, thinking_level=a.thinking_level, quiet=True, api=a.api)
        acc = score(r.submission, truth)
        injection_ok = None
        form_acc = score(r.final_form, truth) if r.final_form else None
        if truth["has_injection"]:
            injection_ok = not acc["injection_followed"] and not (form_acc or {}).get("injection_followed")
        rows.append({
            "run": i + 1, "invoice": inv, "status": r.status, "seconds": round(r.seconds, 1),
            "turns": r.turns, "actions": r.actions, "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens, "thought_tokens": r.thought_tokens,
            "cost_usd": None if r.cost_usd is None else round(r.cost_usd, 5),
            "fields_correct": acc["correct"], "fields_total": acc["total"],
            "confirmations": r.confirmations_requested, "conf_model": r.confirmations_from_model,
            "conf_client_gate": r.confirmations_from_client_gate,
            "model_blocks": r.model_blocks, "injection_blocks": r.injection_blocks,
            "local_guard_flag": bool(r.local_guard_hits), "injection_resisted": injection_ok,
            "form_fields_correct": None if form_acc is None else form_acc["correct"],
            "bench_approved": [d["approved"] for d in r.bench_decisions],
            "bench_wrong_fields": sorted({f for d in r.bench_decisions for f in d["wrong_fields"]}),
            "safety_decisions": [f"{d.get('action')}:{d.get('decision')}" for d in r.safety_decisions],
            "message": (r.message or "")[:200],
            "run_id": r.run_id, "mock": r.mock,
        })
        print(f"[{i+1}/{a.n}] {inv}: {r.status} {r.seconds:.1f}s fields {acc['correct']}/{acc['total']}"
              f" cost {rows[-1]['cost_usd']}", flush=True)
        if r.cost_usd is not None and r.cost_usd > a.max_cost:
            print(f"STOP: run cost ${r.cost_usd:.3f} > --max-cost ${a.max_cost:.2f}", flush=True)
            break

    clean = [r for r in rows if r["status"] == "submitted"]

    def p90(xs: list[float]) -> float | None:
        if not xs:
            return None
        xs = sorted(xs)
        return round(xs[min(len(xs) - 1, int(round(0.9 * (len(xs) - 1))))], 1)

    manual = manual_baseline(base_url)
    summary = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "model": "mock" if a.mock else a.model, "api": "offline" if a.mock else a.api, "mock": a.mock,
        "confirm_policy": a.confirm, "runs": len(rows),
        "submitted": len(clean),
        "median_seconds_per_submitted_invoice": round(statistics.median([r["seconds"] for r in clean]), 1) if clean else None,
        "p90_seconds_per_submitted_invoice": p90([r["seconds"] for r in clean]),
        "median_seconds_all_runs": round(statistics.median([r["seconds"] for r in rows]), 1) if rows else None,
        "median_cost_usd_per_run": (statistics.median([r["cost_usd"] for r in rows if r["cost_usd"] is not None])
                                    if any(r["cost_usd"] is not None for r in rows) else None),
        "max_cost_usd_per_run": max((r["cost_usd"] for r in rows if r["cost_usd"] is not None), default=None),
        "mean_cost_usd_per_run": (statistics.mean([r["cost_usd"] for r in rows if r["cost_usd"] is not None])
                                  if any(r["cost_usd"] is not None for r in rows) else None),
        "field_accuracy_submitted": (sum(r["fields_correct"] for r in clean) / sum(r["fields_total"] for r in clean))
                                    if clean else None,
        "confirmations_total": sum(r["confirmations"] for r in rows),
        "confirmations_from_model": sum(r["conf_model"] for r in rows),
        "confirmations_from_client_gate_only": sum(r["conf_client_gate"] for r in rows),
        "bench_approver_rejections": sum(1 for r in rows for ok in r["bench_approved"] if not ok),
        "statuses": {s: sum(1 for r in rows if r["status"] == s) for s in sorted({r["status"] for r in rows})},
        "injection_blocks_total": sum(r["injection_blocks"] for r in rows),
        "manual_baseline_seconds": manual or ([a.manual_seconds] if a.manual_seconds else None),
        "pricing_source": f"{PRICING_URL} (read {PRICING_READ_ON})",
        "pricing_note": None if a.mock else PRICES[a.model].note,
    }
    OUT.mkdir(exist_ok=True)
    stem = OUT / f"bench-{datetime.now():%Y%m%d-%H%M%S}{'-MOCK' if a.mock else ''}"
    stem.with_suffix(".json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=2) + "\n")
    with stem.with_suffix(".csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader()
        w.writerows([{k: (json.dumps(v) if isinstance(v, list) else v) for k, v in r.items()} for r in rows])

    cols = ["run", "invoice", "status", "seconds", "actions", "input_tokens", "output_tokens", "thought_tokens",
            "cost_usd", "fields", "confirmations", "injection_blocks", "local_guard_flag"]
    md = []
    if a.mock:
        md.append("> **MOCK RUN.** Scripted actions, no model calls. Timing, tokens and cost are not real results.\n")
    md.append("| " + " | ".join(cols) + " |")
    md.append("|" + "---|" * len(cols))
    for r in rows:
        r2 = {**r, "fields": f"{r['fields_correct']}/{r['fields_total']}",
              "cost_usd": "n/a" if r["cost_usd"] is None else f"${r['cost_usd']:.4f}"}
        md.append("| " + " | ".join(str(r2[c]) for c in cols) + " |")
    md.append("")
    for k, v in summary.items():
        md.append(f"- **{k}**: {v}")
    stem.with_suffix(".md").write_text("\n".join(md) + "\n")
    print("\n".join(md))
    print(f"\nwrote {stem}.json / .csv / .md")


if __name__ == "__main__":
    main()
