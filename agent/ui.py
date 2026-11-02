"""Terminal output, the browser console event feed, and human confirmation."""
from __future__ import annotations

import time
from typing import Any

import httpx
from rich.console import Console
from rich.panel import Panel

console = Console(highlight=False)


class Reporter:
    """Prints each step to the terminal and mirrors it to the Ledgerly /console page (best effort)."""

    def __init__(self, base_url: str, quiet: bool = False):
        self.base_url = base_url.rstrip("/")
        self.quiet = quiet
        self.http = httpx.Client(timeout=2)

    def emit(self, evt: dict[str, Any]) -> None:
        try:
            self.http.post(f"{self.base_url}/api/events", json=evt)
        except httpx.HTTPError:
            pass
        if self.quiet:
            return
        k = evt.get("kind")
        if k == "run_start":
            tag = "[magenta]MOCK, no API calls[/]" if evt.get("mock") else "[green]LIVE[/]"
            console.rule(f"[bold]Invoice agent[/]  {evt['invoice']}  ·  {evt['model']}  ·  {tag}")
        elif k == "step":
            gate = "  [yellow]⏸ submit gate[/]" if evt.get("gated") else ""
            safety = f"  [yellow]safety: {evt['safety']}[/]" if evt.get("safety") else ""
            console.print(f"[dim]#{evt['n']:>2}[/] [cyan]{evt['action']:<12}[/] {evt.get('intent') or ''}{safety}{gate}")
            if evt.get("args"):
                console.print(f"     [dim]{evt['args']}[/]")
        elif k == "guard":
            console.print(f"[yellow]⚠ local guard:[/] {evt['message']}  [dim]{evt.get('detail', '')}[/]")
        elif k == "block":
            console.print(Panel(f"{evt['message']}\n[dim]{evt.get('detail', '')}[/]", title="Blocked", border_style="red"))
        elif k == "usage":
            cost = "n/a" if evt.get("cost_usd") is None else f"${evt['cost_usd']:.4f}"
            console.print(f"     [dim]tokens so far {evt['total_tokens']:,} · est. cost {cost}[/]")
        elif k == "run_end":
            style = "green" if evt["status"] == "submitted" else "yellow"
            console.print(Panel(f"{evt['status']} in {evt['seconds']:.1f}s\n{evt.get('message', '')}",
                                title="Run finished", border_style=style))


class Confirmer:
    label = "base"

    def ask(self, explanation: str, summary: str, run_id: str) -> bool:  # pragma: no cover
        raise NotImplementedError


class TerminalConfirmer(Confirmer):
    label = "terminal"

    def ask(self, explanation: str, summary: str, run_id: str) -> bool:
        console.print(Panel(f"[bold]{explanation}[/]\n\n{summary}", title="Approval needed before submitting",
                            border_style="yellow"))
        ans = console.input("[bold yellow]Submit this bill? [y/N] [/]").strip().lower()
        return ans in ("y", "yes")


class ConsoleConfirmer(Confirmer):
    """Asks on the /console page (Approve / Reject buttons). The agent's own browser cannot load that page."""
    label = "console"

    def __init__(self, base_url: str, timeout_s: float = 600):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def ask(self, explanation: str, summary: str, run_id: str) -> bool:
        with httpx.Client(timeout=5) as h:
            cid = h.post(f"{self.base_url}/api/confirm",
                         json={"explanation": explanation, "summary": summary, "run_id": run_id}).json()["id"]
            console.print("[yellow]⏸ Waiting for approval in the agent console…[/]")
            deadline = time.time() + self.timeout_s
            while time.time() < deadline:
                st = h.get(f"{self.base_url}/api/confirm/{cid}").json()["status"]
                if st != "pending":
                    return st == "approved"
                time.sleep(0.3)
        return False


class AutoConfirmer(Confirmer):
    """For unattended benchmark runs only. Every auto decision is counted and reported."""

    def __init__(self, approve: bool):
        self.approve = approve
        self.label = "auto-approve" if approve else "auto-reject"

    def ask(self, explanation: str, summary: str, run_id: str) -> bool:
        return self.approve
