import asyncio
import random

import pytest

from hutch import challenge


class Script:
    """Stands in for a browser: widgets(), tokens() and grid_open() answer from a script."""

    def __init__(self, monkeypatch, *, shown=None, tokens=None, grid=False):
        self.shown, self.tok, self.grid, self.clicks = shown or [], tokens or {}, grid, []
        monkeypatch.setattr(challenge, "widgets", self._widgets)
        monkeypatch.setattr(challenge, "tokens", self._tokens)
        monkeypatch.setattr(challenge, "grid_open", self._grid)
        monkeypatch.setattr(challenge, "click_checkbox", self._click)

    async def _widgets(self, surf):
        return self.shown() if callable(self.shown) else self.shown

    async def _tokens(self, surf):
        return self.tok() if callable(self.tok) else self.tok

    async def _grid(self, surf):
        return self.grid

    async def _click(self, surf, widget, pointer):
        self.clicks.append(widget["kind"])
        self.tok = {"turnstile": 700}
        return (1, 1)


def box(x=10, y=10, w=24, h=24):
    return {"x": x, "y": y, "width": w, "height": h}


def widget(kind="turnstile", checkbox=True):
    return {"kind": kind, "frame": {}, "box": box(0, 0, 300, 65), "checkbox": box() if checkbox else None}


@pytest.mark.asyncio
async def test_no_challenge_is_clear(monkeypatch):
    Script(monkeypatch)
    out = await challenge.clear(challenge.Surface(), timeout=5, settle=0.1)
    assert out["status"] == "clear" and out["clicks"] == 0


@pytest.mark.asyncio
async def test_checkbox_is_clicked_once_and_passes(monkeypatch):
    s = Script(monkeypatch, shown=[widget()])
    out = await challenge.clear(challenge.Surface(), timeout=10, settle=0.1)
    assert out["status"] == "passed" and out["clicked"] is True and s.clicks == ["turnstile"]


@pytest.mark.asyncio
async def test_forbidden_program_is_never_clicked(monkeypatch):
    s = Script(monkeypatch, shown=[widget()])
    out = await challenge.clear(challenge.Surface(), timeout=5, forbids_bypass=True)
    assert out["status"] == "hitl" and out["reason"] == "forbids_bypass" and s.clicks == []


@pytest.mark.asyncio
async def test_grid_stops_without_guessing(monkeypatch):
    s = Script(monkeypatch, shown=[widget("recaptcha", checkbox=False)], grid=True)
    out = await challenge.clear(challenge.Surface(), timeout=5, settle=0.1)
    assert out["status"] == "grid-required" and s.clicks == []


@pytest.mark.asyncio
async def test_waits_while_the_widget_is_still_verifying(monkeypatch):
    seen = {"n": 0}

    def shown():
        seen["n"] += 1
        return [widget(checkbox=seen["n"] > 3)]

    s = Script(monkeypatch, shown=shown)
    out = await challenge.clear(challenge.Surface(), timeout=10, settle=0.1)
    assert out["status"] == "passed" and s.clicks == ["turnstile"] and seen["n"] > 3


@pytest.mark.asyncio
async def test_widget_that_never_shows_a_box_times_out(monkeypatch):
    s = Script(monkeypatch, shown=[widget(checkbox=False)])
    out = await challenge.clear(challenge.Surface(), timeout=2, settle=0.1)
    assert out["status"] == "timeout" and s.clicks == []


@pytest.mark.asyncio
async def test_token_already_present_needs_no_click(monkeypatch):
    s = Script(monkeypatch, shown=[widget(checkbox=False)], tokens={"turnstile": 709})
    out = await challenge.clear(challenge.Surface(), timeout=5)
    assert out["status"] == "passed" and out["clicked"] is False and s.clicks == []


def test_checkbox_finder_pierces_shadow_roots_and_reads_attributes():
    tree = {"nodeName": "#document", "children": [{"nodeName": "HTML", "children": [
        {"nodeName": "BODY", "children": [], "shadowRoots": [
            {"nodeName": "#document-fragment", "children": [
                {"nodeName": "LABEL", "children": [
                    {"nodeName": "INPUT", "nodeId": 9, "attributes": ["type", "checkbox"]}]}]}]}]}]}
    assert challenge._find(tree, challenge._is_checkbox)["nodeId"] == 9
    assert challenge._find({"nodeName": "DIV"}, challenge._is_checkbox) is None


def test_widget_urls():
    assert challenge.TURNSTILE.search("https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/f/av0")
    assert challenge.RECAPTCHA.search("https://www.google.com/recaptcha/api2/anchor?ar=1&k=abc")
    assert challenge.RECAPTCHA_GRID.search("https://www.recaptcha.net/recaptcha/enterprise/bframe?hl=en")
    assert not challenge.TURNSTILE.search("https://example.com/turnstile/")


@pytest.mark.asyncio
async def test_pointer_arrives_over_a_path_then_presses(monkeypatch):
    events = []

    class Surf(challenge.Surface):
        async def send(self, method, params=None):
            events.append((method, (params or {}).get("type")))
            return {"result": {"value": [1280, 720]}}

    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda *_: real_sleep(0))
    pointer = challenge.Pointer(Surf(), random.Random(1))
    await pointer.click(400, 300)
    kinds = [t for m, t in events if m == "Input.dispatchMouseEvent"]
    assert kinds.count("mouseMoved") > 5 and kinds[-2:] == ["mousePressed", "mouseReleased"]


@pytest.mark.asyncio
async def test_session_routes_events_and_honours_the_forbids_bypass_tag(tmp_path, monkeypatch):
    from hutch.session import Session

    s = Session("c", str(tmp_path / "c"), tags={"forbids_bypass": "true"})
    monkeypatch.setattr(s, "_require_active", lambda: None)
    monkeypatch.setattr(s, "_active_page", lambda: object())
    seen = {}

    async def fake_clear(surf, *, timeout, forbids_bypass, on_event):
        seen["forbids"] = forbids_bypass
        on_event("challenge", status="clicked", kind="turnstile")
        return {"status": "hitl" if forbids_bypass else "passed", "kind": "turnstile", "seconds": 1.0, "clicks": 0}

    monkeypatch.setattr(challenge, "clear", fake_clear)
    monkeypatch.setattr(challenge, "surface", lambda page, x=None: challenge.Surface())
    out = await s.clear_challenge(timeout=5)
    assert out["status"] == "hitl" and seen["forbids"] is True
    assert s.status()["challenge"] == "hitl"
    assert [e["status"] for e in s.events.since(0, {"challenge"})] == ["clicked"]
    assert (await s.clear_challenge(timeout=5, forbids_bypass=False))["status"] == "passed"


@pytest.mark.asyncio
async def test_an_interstitial_is_never_scripted_while_it_is_up(monkeypatch):
    s = Script(monkeypatch, shown=[widget()])
    polled = []

    async def tokens(surf):
        polled.append(1)
        return {}

    monkeypatch.setattr(challenge, "tokens", tokens)

    class Surf(challenge.Surface):
        async def info(self):
            return {"title": "Just a moment...", "url": "https://example.com/"}

    await challenge.clear(Surf(), timeout=3, settle=0.1)
    assert polled == [] and s.clicks == ["turnstile"]
