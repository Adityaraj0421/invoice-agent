"""Run the invoice agent on one invoice.

    python -m agent.run --invoice inv-01-larkspur --mock --start-server
    GEMINI_API_KEY=... python -m agent.run --invoice inv-01-larkspur --headed --confirm console
"""
from __future__ import annotations

import argparse
import base64
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
from playwright.sync_api import sync_playwright

from . import guard
from .browser import BrowserEnv
from .models import TASK_PROMPT, GeminiGenerateContentModel, GeminiModel, ReplayModel, ScriptedMock, Turn
from .pricing import estimate_cost
from .ui import AutoBenchConfirmer, AutoConfirmer, ConsoleConfirmer, Confirmer, Reporter, TerminalConfirmer

ROOT = Path(__file__).resolve().parent.parent
INVOICES = ROOT / "invoices"
RUNS = ROOT / "runs"
ALLOWED_DECISIONS = {None, "", "allowed", "allow", "regular", "require_confirmation"}

# Outer window in device-independent pixels. --force-device-scale-factor multiplies
# this into screen pixels, so a 1440x900 page at 0.67 is about 965x603 plus the frame.
# The extra DIPs are the tab strip and window frame; without them the viewport is clipped.
_HEADED_FRAME_W = 64
_HEADED_FRAME_H = 160


def chromium_launch_args(headless: bool, window_scale: float, width: int, height: int) -> list[str]:
    """Headed-only flags. Below 1, shrink the window; the CSS viewport stays width x height."""
    if headless or window_scale >= 1:
        return []
    if window_scale <= 0:
        raise ValueError("window_scale must be positive")
    win_w, win_h = width + _HEADED_FRAME_W, height + _HEADED_FRAME_H
    return [
        f"--force-device-scale-factor={window_scale:g}",
        f"--window-size={win_w},{win_h}",
    ]


def _apply_headed_window(page, headless: bool, window_scale: float, width: int, height: int) -> None:
    """Playwright resizes the window to the viewport after launch. Put the scaled size back."""
    if headless or window_scale >= 1:
        return
    win_w, win_h = width + _HEADED_FRAME_W, height + _HEADED_FRAME_H
    session = page.context.new_cdp_session(page)
    window = session.send("Browser.getWindowForTarget")
    session.send("Browser.setWindowBounds", {
        "windowId": window["windowId"],
        "bounds": {"width": win_w, "height": win_h, "windowState": "normal"},
    })


@dataclass
class RunResult:
    run_id: str
    invoice: str
    model: str
    mock: bool
    status: str = "running"  # submitted | blocked_by_model | rejected_by_human | blocked_by_local_guard | no_submit | max_turns | api_error
    message: str = ""
    seconds: float = 0.0
    turns: int = 0
    actions: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    thought_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = 0.0
    confirmations_requested: int = 0
    confirmations_from_model: int = 0      # safety_decision == require_confirmation
    confirmations_from_client_gate: int = 0  # our submit gate fired without a model safety_decision
    confirmations_approved: int = 0
    model_blocks: int = 0
    injection_blocks: int = 0
    local_guard_hits: list[str] = field(default_factory=list)
    safety_decisions: list[dict[str, Any]] = field(default_factory=list)
    confirmer: str = ""
    api: str = ""
    final_form: dict[str, Any] | None = None  # form on screen when the run ended (submitted or not)
    bench_decisions: list[dict[str, Any]] = field(default_factory=list)  # auto-bench approver only
    submission: dict[str, Any] | None = None


def _args_summary(name: str, args: dict[str, Any]) -> str:
    a = {k: v for k, v in args.items() if k not in ("intent", "safety_decision")}
    return ", ".join(f"{k}={json.dumps(v)}" for k, v in a.items())


def _looks_like_injection(*texts: Any) -> bool:
    t = " ".join(json.dumps(x, default=str) if not isinstance(x, str) else x for x in texts).lower()
    return "injection" in t or "adversarial" in t


def run_agent(invoice: str, base_url: str, model_name: str = "gemini-3.8-flash", mock: bool = False,
              replay: Path | None = None, confirmer: Confirmer | None = None, headless: bool = True,
              width: int = 1440, height: int = 900, max_turns: int = 80, local_guard: str = "warn",
              thinking_level: str | None = "low", injection_detection: bool = True, slow_mo: int = 0,
              window_scale: float = 1.0, quiet: bool = False, save_screenshots: bool = False,
              api: str = "generate-content") -> RunResult:
    truth = json.loads((INVOICES / f"{invoice}.json").read_text())
    run_id = uuid.uuid4().hex[:8]
    run_dir = RUNS / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    trace = (run_dir / "trace.jsonl").open("w")
    rep = Reporter(base_url, quiet=quiet)
    confirmer = confirmer or TerminalConfirmer()
    offline = mock or replay is not None
    res = RunResult(run_id=run_id, invoice=invoice, model="mock" if mock else ("replay" if replay else model_name),
                    mock=offline, confirmer=confirmer.label, api="offline" if offline else api)

    def log(rec: dict[str, Any]) -> None:
        trace.write(json.dumps(rec) + "\n"); trace.flush()

    def add_usage(turn: Turn) -> None:
        u = turn.usage or {}
        res.input_tokens += int(u.get("total_input_tokens") or 0)
        res.output_tokens += int(u.get("total_output_tokens") or 0)
        res.thought_tokens += int(u.get("total_thought_tokens") or 0)
        res.cached_tokens += int(u.get("total_cached_tokens") or 0)
        res.total_tokens += int(u.get("total_tokens") or 0)
        res.cost_usd = None if offline else estimate_cost(model_name, res.input_tokens, res.output_tokens,
                                                          res.thought_tokens, res.cached_tokens)
        if not offline:
            rep.emit({"kind": "usage", "total_tokens": res.total_tokens, "cost_usd": res.cost_usd})

    # Local, model-independent injection scan of the PDF text layer.
    hits = guard.scan_for_injection(guard.pdf_text(INVOICES / f"{invoice}.pdf"))
    res.local_guard_hits = hits

    rep.emit({"kind": "run_start", "run_id": run_id, "invoice": f"{invoice}.pdf", "model": res.model, "mock": offline})
    if hits:
        rep.emit({"kind": "guard", "message": "Invoice text contains instruction-like phrases (possible prompt injection).",
                  "detail": f"{len(hits)} pattern(s) matched; policy={local_guard}"})
    t0 = time.perf_counter()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless, slow_mo=slow_mo,
                                     args=chromium_launch_args(headless, window_scale, width, height))
        ctx = browser.new_context(viewport={"width": width, "height": height})
        page = ctx.new_page()
        _apply_headed_window(page, headless, window_scale, width, height)
        env = BrowserEnv(page, width, height, base_url)
        env.install_allowlist()
        page.goto(f"{base_url}/app?invoice={invoice}&run={run_id}")
        page.wait_for_selector("#doc-img")
        page.wait_for_function("document.getElementById('doc-img').complete")

        def finish(status: str, message: str = "") -> None:
            res.status, res.message = status, message

        try:
            if hits and local_guard == "block":
                rep.emit({"kind": "block", "message": "Stopped by the local guard before any model call.",
                          "detail": "Run with --local-guard warn to let the Gemini detector see it."})
                finish("blocked_by_local_guard")
                raise StopIteration

            if mock:
                model: Any = ScriptedMock(env, truth)
            elif replay:
                model = ReplayModel(replay)
            else:
                backend = GeminiGenerateContentModel if api == "generate-content" else GeminiModel
                model = backend(model_name, thinking_level=thinking_level, injection_detection=injection_detection)

            shot = env.screenshot()
            turn = model.start(TASK_PROMPT, base64.b64encode(shot).decode())
            asked_in_text = False
            empty_nudges = 0
            step_n = 0
            for _ in range(max_turns):
                res.turns += 1
                add_usage(turn)
                log({"kind": "turn", "id": turn.id, "status": turn.status, "steps": turn.steps,
                     "usage": turn.usage, "errors": turn.errors, "raw": turn.raw})
                if turn.errors or (turn.status or "").endswith("failed"):
                    inj = _looks_like_injection(turn.errors)
                    if inj:
                        res.injection_blocks += 1
                    # GenerateContent signals a blocked input with promptFeedback.blockReason (seen live on
                    # inv-05 on 2026-11-03: blockReason SAFETY, no further detail). It doesn't name the cause.
                    input_blocked = any((e or {}).get("prompt_feedback", {}).get("blockReason")
                                        for e in turn.errors if isinstance(e, dict) and e.get("prompt_feedback"))
                    res.model_blocks += 1
                    msg = ("Gemini refused this screen (input blocked by Google's safety filter). Nothing was entered."
                           if input_blocked else "The API returned an error / failed status.")
                    rep.emit({"kind": "block", "message": msg, "detail": json.dumps(turn.errors, default=str)[:400]})
                    finish("blocked_by_model" if (inj or input_blocked) else "api_error",
                           json.dumps(turn.errors, default=str)[:400])
                    break

                calls = turn.function_calls
                if not calls:
                    submitted = env.page.locator("#status.saved").count() > 0
                    if submitted:
                        finish("submitted", turn.text)
                        break
                    # An empty turn (no actions, no text) mid-task was seen live on 2026-11-03 (inv-03,
                    # finishReason STOP, text ""). Nudge it to carry on, at most twice.
                    if not turn.text and empty_nudges < 2:
                        empty_nudges += 1
                        rep.emit({"kind": "guard", "message": "The model returned an empty turn; asking it to continue.",
                                  "detail": f"nudge {empty_nudges}/2"})
                        turn = model.send_text("Continue the task from the current screen.",
                                               base64.b64encode(env.screenshot()).decode())
                        continue
                    # The model stopped and (probably) asked in text. Show the human the form and relay.
                    if not asked_in_text and env.form_snapshot().get("total"):
                        asked_in_text = True
                        res.confirmations_requested += 1
                        res.confirmations_from_model += 1
                        if isinstance(confirmer, AutoBenchConfirmer):
                            confirmer.snapshot = env.form_snapshot()
                        ok = confirmer.ask(turn.text or "The agent is asking to submit.",
                                           guard.summarize_form(env.form_snapshot()), run_id)
                        if not isinstance(confirmer, ConsoleConfirmer):  # the server already publishes console decisions
                            rep.emit({"kind": "confirm_decided", "confirm_id": "-", "decision": "approved" if ok else "rejected"})
                        if not ok:
                            finish("rejected_by_human", "Human rejected the submission.")
                            break
                        res.confirmations_approved += 1
                        turn = model.send_text("The user approved. Click Submit bill now.",
                                               base64.b64encode(env.screenshot()).decode())
                        continue
                    finish("no_submit", turn.text)
                    break

                results: list[dict[str, Any]] = []
                stop = False
                for fc in calls:
                    name, args, call_id = fc["name"], dict(fc.get("arguments") or {}), fc.get("id", "")
                    sd = args.get("safety_decision") or None
                    decision = (sd or {}).get("decision")
                    if sd:
                        res.safety_decisions.append({"action": name, **sd})
                    gated = decision == "require_confirmation" or env.would_hit_gate(name, args)
                    step_n += 1
                    rep.emit({"kind": "step", "n": step_n, "action": name, "intent": args.get("intent"),
                              "args": _args_summary(name, args), "safety": decision, "gated": gated})

                    if decision not in ALLOWED_DECISIONS:
                        res.model_blocks += 1
                        if _looks_like_injection(sd):
                            res.injection_blocks += 1
                        rep.emit({"kind": "block", "message": f"Model safety decision: {decision}. Action not executed.",
                                  "detail": (sd or {}).get("explanation", "")})
                        finish("blocked_by_model", (sd or {}).get("explanation", ""))
                        stop = True
                        break

                    action_result: dict[str, Any] = {}
                    if gated:
                        res.confirmations_requested += 1
                        if decision == "require_confirmation":
                            res.confirmations_from_model += 1
                        else:
                            res.confirmations_from_client_gate += 1
                        explanation = (sd or {}).get("explanation") or "The agent wants to press Submit bill."
                        if isinstance(confirmer, AutoBenchConfirmer):
                            confirmer.snapshot = env.form_snapshot()
                        ok = confirmer.ask(explanation, guard.summarize_form(env.form_snapshot()), run_id)
                        if not isinstance(confirmer, ConsoleConfirmer):  # the server already publishes console decisions
                            rep.emit({"kind": "confirm_decided", "confirm_id": "-",
                                      "decision": "approved" if ok else "rejected"})
                        if not ok:
                            finish("rejected_by_human", "Human rejected the submission. Nothing was submitted.")
                            stop = True
                            break
                        res.confirmations_approved += 1
                        if sd:
                            action_result["safety_acknowledgement"] = True  # per docs

                    try:
                        action_result.update(env.execute(name, args))
                    except Exception as e:  # report execution errors back to the model
                        action_result["error"] = str(e)
                    res.actions += 1
                    time.sleep(0.15)
                    results.append((name, call_id, action_result))
                if stop:
                    break

                # Once the bill shows Saved, the job is done: stop here rather than asking the model
                # for another turn. Saves a round trip, and in a live run on 2026-11-03 the model
                # otherwise clicked Submit a second time (a second approval prompt for a saved bill).
                if any(n == "click" and r.get("safety_acknowledgement") for n, _, r in results) or \
                        env.page.locator("#status.saved").count() > 0:
                    try:
                        env.page.wait_for_selector("#status.saved", timeout=3000)
                    except Exception:
                        pass
                if env.page.locator("#status.saved").count() > 0:
                    log({"kind": "results", "results": [{"name": n, "call_id": c, "result": r} for n, c, r in results]})
                    finish("submitted", "Bill saved. Run stopped at the Saved state.")
                    break

                png = env.screenshot()
                if save_screenshots:
                    (run_dir / f"step-{step_n:03d}.png").write_bytes(png)
                payload = [env.function_result(n, c, r, png) for n, c, r in results]
                log({"kind": "results", "results": [{"name": n, "call_id": c, "result": r} for n, c, r in results]})
                turn = model.send_results(payload)
            else:
                finish("max_turns", f"Stopped after {max_turns} turns.")
        except StopIteration:
            pass
        except Exception as e:
            # Response shape for a detected injection is undocumented; an API error that mentions it counts as a block.
            if _looks_like_injection(str(e)):
                res.injection_blocks += 1
                res.model_blocks += 1
                finish("blocked_by_model", f"{type(e).__name__}: {e}")
            else:
                finish("api_error", f"{type(e).__name__}: {e}")
        finally:
            res.seconds = time.perf_counter() - t0
            try:
                res.final_form = env.form_snapshot()
            except Exception:
                pass
            if isinstance(confirmer, AutoBenchConfirmer):
                res.bench_decisions = confirmer.decisions
            try:
                page.wait_for_timeout(600 if headless else 4000)  # headed: keep the Saved banner on screen for the recording
            except Exception:
                pass
            browser.close()

    subs = httpx.get(f"{base_url}/api/bills", params={"run_id": run_id}).json()
    res.submission = subs[-1] if subs else None
    if res.submission and res.status not in ("submitted",):
        res.status = "submitted"
    rep.emit({"kind": "run_end", "status": res.status, "seconds": res.seconds, "message": res.message})
    (run_dir / "result.json").write_text(json.dumps(asdict(res), indent=2) + "\n")
    trace.close()
    return res


def make_confirmer(kind: str, base_url: str, invoice: str | None = None) -> Confirmer:
    if kind == "auto-bench":  # benchmark only: approves iff the form matches ground truth
        return AutoBenchConfirmer(json.loads((INVOICES / f"{invoice}.json").read_text()))
    return {"terminal": lambda: TerminalConfirmer(), "console": lambda: ConsoleConfirmer(base_url),
            "auto-approve": lambda: AutoConfirmer(True), "auto-reject": lambda: AutoConfirmer(False)}[kind]()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--invoice", default="inv-01-larkspur", help="invoice id in invoices/ (without .pdf)")
    ap.add_argument("--model", default="gemini-3.8-flash")
    ap.add_argument("--api", default="generate-content", choices=["generate-content", "interactions"],
                    help="Gemini API surface. Default generate-content: on 2026-11-03 every Interactions "
                         "function_result continuation returned HTTP 400 (see README). "
                         "--api interactions switches back once Google fixes it")
    ap.add_argument("--mock", "--dry-run", dest="mock", action="store_true", help="scripted actions, no API key")
    ap.add_argument("--replay", type=Path, help="replay function calls from a runs/<id>/trace.jsonl")
    ap.add_argument("--base-url", default="http://127.0.0.1:8765")
    ap.add_argument("--start-server", action="store_true", help="start Ledgerly in-process on a free port")
    ap.add_argument("--confirm", default="terminal", choices=["terminal", "console", "auto-approve", "auto-reject", "auto-bench"],
                    help="who approves the Submit click. terminal/console = a human (demo default). "
                         "auto-bench = BENCHMARK ONLY scripted approver (approves iff the form matches ground truth)")
    ap.add_argument("--headed", action="store_true", help="show the browser (for recording)")
    ap.add_argument("--slow-mo", type=int, default=0, help="ms delay per browser op, for a watchable video")
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--window-scale", type=float, default=1.0,
                    help="headed only. Shrink the Chromium window (0.67 fits a 1440x900 page in about "
                         "two-thirds of a laptop screen). Screenshots stay 1440x900 CSS pixels")
    ap.add_argument("--max-turns", type=int, default=80)
    ap.add_argument("--thinking-level", default="low", choices=["minimal", "low", "medium", "high", "none"])
    ap.add_argument("--no-injection-detection", action="store_true")
    ap.add_argument("--local-guard", default="warn", choices=["warn", "block"])
    ap.add_argument("--save-screenshots", action="store_true")
    a = ap.parse_args()

    if a.window_scale <= 0:
        ap.error("--window-scale must be positive")
    base_url = a.base_url
    if a.start_server:
        from .serve import start_server
        base_url, _ = start_server()
        print(f"Ledgerly running at {base_url}/app  (console: {base_url}/console)")
    res = run_agent(a.invoice, base_url, model_name=a.model, mock=a.mock, replay=a.replay,
                    confirmer=make_confirmer(a.confirm, base_url, a.invoice), headless=not a.headed,
                    width=a.width, height=a.height, max_turns=a.max_turns, local_guard=a.local_guard,
                    thinking_level=None if a.thinking_level == "none" else a.thinking_level,
                    injection_detection=not a.no_injection_detection, slow_mo=a.slow_mo,
                    window_scale=a.window_scale, save_screenshots=a.save_screenshots, api=a.api)
    print(json.dumps({k: v for k, v in asdict(res).items() if k not in ("submission", "safety_decisions")}, indent=2))


if __name__ == "__main__":
    main()
