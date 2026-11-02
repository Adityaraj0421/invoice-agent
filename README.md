# Invoice Agent: Gemini Computer Use demo

An agent that reads a PDF invoice and keys it into a web accounting form one field at a time, using the
[Gemini Computer Use](https://ai.google.dev/gemini-api/docs/computer-use) tool and Playwright. Each action
comes with a one-sentence explanation of what the agent is doing and why, and **nothing is submitted until
a human approves it.**

```
invoice PDF ──► Gemini (computer_use tool) ──► click / type / scroll ──► Ledgerly "New Bill" form
                        ▲                                                       │
                        └──────── screenshot + current URL after every action ◄─┘
                                       │
                       intent stream ──► terminal + live console
                       Submit ─────────► confirmation gate ──► human: Approve / Reject
```

## What's in the repo

| Path | What it is |
|---|---|
| `app/` | **Ledgerly (mock)**: a small FastAPI accounting app. It has a New Bill form (vendor, invoice no., currency, dates, line items, tax, total), a green **Saved** state, and submissions appended to `data/submissions.json`. It also serves `/console`, a live feed of the agent's steps with Approve/Reject buttons. |
| `invoices/` | Five synthetic invoices (PDF, a PNG render, ground-truth JSON). `inv-05-pinecrest-injection` has hidden prompt-injection text in its footer. Regenerate them with `python invoices/make_invoices.py`. |
| `agent/` | The Computer Use loop (`run.py`), Playwright action executor (`browser.py`), model adapters (`models.py`: Gemini, a scripted mock and a trace replay), safety helpers (`guard.py`), pricing (`pricing.py`) and the terminal/console UI (`ui.py`). |
| `bench/` | `benchmark.py` runs N invoices and records time, tokens, cost, field accuracy, confirmations and injection blocks. `accuracy.py` scores submissions against ground truth. |
| `tests/` | Unit tests plus end-to-end tests that drive the real browser against the mock app (no API key needed). |
| `RECORDING_SCRIPT.md` | A shot list and voiceover for a 60-second demo video. |

## Setup

Requires Python 3.10+.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env        # then set GEMINI_API_KEY in your shell (the code reads the environment only)
export GEMINI_API_KEY=...   # never commit this
```

> **The key has to be on a paid tier.** According to the [pricing page](https://ai.google.dev/gemini-api/docs/pricing),
> Computer Use is *"Not available"* on the free tier. On the paid tier it is *"Charged as regular tokens per model pricing"*.

The code reads `GEMINI_API_KEY` from the environment, via `google-genai`'s `genai.Client()`. It never prints or logs the key.

## Run

**No key (dry run).** A scripted model drives the same browser, gate and logging code:

```bash
python -m agent.run --invoice inv-01-larkspur --mock --start-server --headed
```

**Live, with Gemini:**

```bash
# terminal 1: the mock app
uvicorn app.server:app --port 8765
# open http://127.0.0.1:8765/console in your browser (live steps + Approve/Reject)

# terminal 2: the agent
python -m agent.run --invoice inv-02-brightline --headed --slow-mo 150 --confirm console
```

Useful flags:

| Flag | Effect |
|---|---|
| `--model` | `gemini-3.8-flash` (default), `gemini-3.5-flash-lite`, `gemini-4-argon` or `gemini-3-flash-preview` |
| `--confirm` | Where approval happens: `terminal` (default), `console`, `auto-approve` or `auto-reject` |
| `--thinking-level` | Sets the thinking level (default `low`) |
| `--local-guard block` | Refuses to start when the local scan finds injection-like text in the PDF (default `warn`) |
| `--no-injection-detection` | Turns off Gemini's prompt-injection detection |
| `--save-screenshots` | Saves each step's screenshot |
| `--replay runs/<id>/trace.jsonl` | Re-executes a recorded run without calling the API |

Every run writes `runs/<id>/trace.jsonl` (every request and response step, with screenshots elided) and `runs/<id>/result.json`.

**Manual baseline.** Open `http://127.0.0.1:8765/app?invoice=inv-03-saffron&mode=manual` and type the invoice yourself.
The app records `entry_seconds` from your first keystroke to Submit, and the benchmark picks this up as the human baseline.

**Benchmark:**

```bash
python -m bench.benchmark --n 10                     # live; writes results/bench-<timestamp>.{md,csv,json}
python -m bench.benchmark --mock --n 5               # pipeline check only; output is stamped MOCK and is not a result
```

**Tests:** `python -m pytest -q`. The tests need no key and never call the API.

## How it works

1. **Loop.** `agent/run.py` opens Ledgerly in Chromium at 1440×900 and sends the task, a screenshot and the
   `computer_use` tool through the **Interactions API**
   (`client.interactions.create(..., tools=[{"type": "computer_use", "environment": "browser", "enable_prompt_injection_detection": True}])`).
   Each `function_call` step is executed with Playwright. The loop then sends back a `function_result` containing the current
   URL and a fresh screenshot, chained with `previous_interaction_id`. It stops when the model returns no more function calls.
2. **Coordinates.** The model returns 0–999 normalized coordinates, which are converted to pixels with `int(v / 1000 * size)`, as in the docs.
3. **Intent stream.** Every function call carries an `intent` argument. The loop prints it in the terminal and pushes it, with token and cost
   counters, to `/console` over Server-Sent Events.
4. **Confirm before Submit.** There are two independent gates, and either one triggers a human prompt:
   - **Model gate.** If a function call includes `safety_decision: {"decision": "require_confirmation"}`, the action waits for a human.
     On approval, the result is sent back with `safety_acknowledgement: true`, as the docs require. On rejection, the run ends with nothing submitted.
   - **Client gate.** Before executing any click or keypress, the executor checks whether the target element sits inside
     `[data-agent-gate]` (the Submit button), using `elementFromPoint` or the focused element. If it does, the action is held for approval
     even when the model didn't ask for confirmation.
5. **Accuracy.** `bench/accuracy.py` compares the saved submission with the ground truth field by field (vendor, number, currency, dates,
   subtotal, tax, total) and line by line (description, quantity, unit price, amount). Amounts are compared as decimals.

## Safety notes

- **Invoices are untrusted input.** The system instruction tells the model to treat document text as data and never as instructions.
  Gemini's prompt-injection detection is turned on (`enable_prompt_injection_detection: True`; the docs say it is off by default and
  "blocks execution when detected" on Gemini 3.5 Flash-Lite or later).
- **Local injection scan.** `agent/guard.py` scans the PDF's text layer for injection phrases ("ignore all previous instructions"
  and similar) and warns before the run, or blocks it with `--local-guard block`. It is a keyword heuristic, not a defence.
- **Scoped browser.** The agent's browser context can only load `/app`, `/static/`, `/invoices/` and `/api/bills` on the
  local app. It cannot open `/console` or the confirmation API, so it cannot approve its own submit. `navigate` is limited to the same origin.
- **Approval needs a token.** Approve/Reject requests must carry a random token that is generated when the server starts and only embedded in the
  `/console` page.
- **No silent submits.** Pressing Enter in the form never submits. Only the Submit button does, and that button is gated.

## Limitations

- This is a demo against a mock app on localhost. It is not production accounting software, and it has not been tested against real accounting systems.
- **Live numbers have not been measured yet.** No API key was available while this was built, so the agent has only run with the
  scripted mock. Treat any figure in `results/*-MOCK.*` as a pipeline check, not a performance claim.
- **Injection detection response shape.** The docs don't show what a detected-injection response looks like. The loop treats a
  `safety_decision` of `blocked`, a `failed` interaction status or an error that mentions injection as a block. It also logs the raw response so the
  handling can be tightened once a real detection has been observed.
- **Cost is an estimate** from the token usage the API reports, using the prices in `agent/pricing.py` (from the
  [pricing page](https://ai.google.dev/gemini-api/docs/pricing), read 2026-11-02). Cached input is billed at the cache rate and
  thinking tokens at the output rate. Several prices are promotional and will rise: `gemini-3.8-flash` after 2026-12-31 and
  `gemini-4-argon` after 2026-11-30. Check the pricing page before publishing any cost figure.
- The agent works one action per turn and sends a screenshot each time, so it is slower than a person who knows the form.
- Synthetic invoices are clean, text-based PDFs. Scans, handwriting and unusual layouts are untested.

## License

MIT. See [LICENSE](LICENSE). The copyright holder in LICENSE is still a placeholder.
