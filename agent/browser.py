"""Playwright execution environment for Computer Use browser actions.

Action names and arguments follow the browser action table at
https://ai.google.dev/gemini-api/docs/computer-use (coordinates are normalized 0-999).
"""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import Page, Route

from . import guard

CLICKS = {"click", "double_click", "triple_click", "middle_click", "right_click", "move",
          "mouse_down", "mouse_up"}

KEY_ALIASES = {
    "enter": "Enter", "return": "Enter", "tab": "Tab", "esc": "Escape", "escape": "Escape",
    "backspace": "Backspace", "delete": "Delete", "space": "Space", "up": "ArrowUp", "down": "ArrowDown",
    "left": "ArrowLeft", "right": "ArrowRight", "ctrl": "Control", "control": "Control", "cmd": "Meta",
    "command": "Meta", "meta": "Meta", "alt": "Alt", "option": "Alt", "shift": "Shift", "home": "Home",
    "end": "End", "pageup": "PageUp", "pagedown": "PageDown",
}


def denormalize(v: float, size: int) -> int:
    """Normalized (0-999/1000) model coordinate -> pixel, as in the docs' denormalize_x/y."""
    return int(v / 1000 * size)


def norm_key(k: str) -> str:
    k = str(k).strip()
    # X11 keysym spellings seen from the real model, e.g. "Control_L" (2026-11-03 smoke test).
    if len(k) > 2 and k[-2:] in ("_L", "_R"):
        k = k[:-2]
    if "+" in k and len(k) > 1:
        return "+".join(norm_key(p) for p in k.split("+"))
    return KEY_ALIASES.get(k.lower(), k if len(k) > 1 else k)


@dataclass
class BrowserEnv:
    page: Page
    width: int
    height: int
    base_url: str
    allowed_prefixes: tuple[str, ...] = ("/app", "/static/", "/invoices/", "/api/bills")
    blocked_requests: list[str] = field(default_factory=list)

    def install_allowlist(self) -> None:
        """Agent browser may only reach the mock app pages. /console and the confirm API are off limits,
        so the model can never see or press the human approval button."""
        base = urlparse(self.base_url)

        def handler(route: Route) -> None:
            u = urlparse(route.request.url)
            same = (u.scheme, u.netloc) == (base.scheme, base.netloc)
            if same and any(u.path.startswith(p) for p in self.allowed_prefixes):
                route.continue_()
            else:
                self.blocked_requests.append(route.request.url)
                route.abort()

        self.page.context.route("**/*", handler)

    # ---- gate checks -------------------------------------------------
    def gate_at(self, args: dict[str, Any]) -> str | None:
        if "x" not in args or "y" not in args:
            return None
        px, py = denormalize(args["x"], self.width), denormalize(args["y"], self.height)
        return self.page.evaluate(guard.GATE_JS, [px, py])

    def gate_focused(self) -> str | None:
        return self.page.evaluate(guard.FOCUSED_GATE_JS)

    def would_hit_gate(self, name: str, args: dict[str, Any]) -> bool:
        if name in {"click", "double_click", "triple_click", "mouse_up"}:
            return self.gate_at(args) == "submit"
        if name in {"press_key", "hotkey", "type"}:
            keys = args.get("keys") or [args.get("key", "")]
            enterish = any(norm_key(k) in ("Enter", "Space") for k in keys if k) or args.get("press_enter")
            return bool(enterish) and self.gate_focused() == "submit"
        return False

    def form_snapshot(self) -> dict[str, Any]:
        return self.page.evaluate(guard.FORM_SNAPSHOT_JS)

    # ---- execution ---------------------------------------------------
    def execute(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        p, W, H = self.page, self.width, self.height
        x = denormalize(args["x"], W) if "x" in args else None
        y = denormalize(args["y"], H) if "y" in args else None
        if name == "click":
            p.mouse.click(x, y)
        elif name == "double_click":
            p.mouse.dblclick(x, y)
        elif name == "triple_click":
            p.mouse.click(x, y, click_count=3)
        elif name == "middle_click":
            p.mouse.click(x, y, button="middle")
        elif name == "right_click":
            p.mouse.click(x, y, button="right")
        elif name == "move":
            p.mouse.move(x, y)
        elif name == "mouse_down":
            p.mouse.move(x, y); p.mouse.down()
        elif name == "mouse_up":
            p.mouse.move(x, y); p.mouse.up()
        elif name == "type":
            if x is not None and y is not None:
                p.mouse.click(x, y)
            # The docs sample clears with Meta+A (macOS). ControlOrMeta works on Linux/Windows too.
            p.keyboard.press("ControlOrMeta+A")
            p.keyboard.press("Backspace")
            p.keyboard.type(str(args.get("text", "")), delay=12)
            if args.get("press_enter"):
                p.keyboard.press("Enter")
        elif name == "press_key":
            p.keyboard.press(norm_key(args["key"]))
        elif name == "key_down":
            p.keyboard.down(norm_key(args["key"]))
        elif name == "key_up":
            p.keyboard.up(norm_key(args["key"]))
        elif name == "hotkey":
            p.keyboard.press("+".join(norm_key(k) for k in args["keys"]))
        elif name == "drag_and_drop":
            p.mouse.move(denormalize(args["start_x"], W), denormalize(args["start_y"], H))
            p.mouse.down()
            p.mouse.move(denormalize(args["end_x"], W), denormalize(args["end_y"], H), steps=8)
            p.mouse.up()
        elif name == "scroll":
            mag = int(args.get("magnitude_in_pixels", 300))
            dx, dy = {"up": (0, -mag), "down": (0, mag), "left": (-mag, 0), "right": (mag, 0)}[args["direction"]]
            p.mouse.move(x, y)
            p.mouse.wheel(dx, dy)
        elif name == "wait":
            time.sleep(min(float(args.get("seconds", 1)), 10))
        elif name == "take_screenshot":
            pass  # a screenshot is always returned
        elif name == "navigate":
            u, base = urlparse(args["url"]), urlparse(self.base_url)
            if (u.scheme, u.netloc) != (base.scheme, base.netloc):
                return {"error": "Navigation blocked by client allowlist: only the Ledgerly app is reachable."}
            p.goto(args["url"])
        elif name == "go_back":
            p.go_back()
        elif name == "go_forward":
            p.go_forward()
        else:
            return {"error": f"Unsupported action: {name}"}
        try:
            p.wait_for_load_state(timeout=5000)
        except Exception:
            pass
        return {}

    def screenshot(self) -> bytes:
        return self.page.screenshot(type="png")

    def function_result(self, name: str, call_id: str, result: dict[str, Any], png: bytes) -> dict[str, Any]:
        """Shape from the docs' get_function_responses()."""
        return {
            "type": "function_result",
            "name": name,
            "call_id": call_id,
            "result": [
                {"type": "text", "text": json.dumps({"url": self.page.url, **result})},
                {"type": "image", "data": base64.b64encode(png).decode("utf-8"), "mime_type": "image/png"},
            ],
        }

    def locate(self, selector: str) -> tuple[int, int]:
        """Normalized centre of an element. Used only by the scripted mock model."""
        box = self.page.locator(selector).first.bounding_box()
        if box is None:
            raise RuntimeError(f"element not visible: {selector}")
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        return round(cx / self.width * 1000), round(cy / self.height * 1000)
