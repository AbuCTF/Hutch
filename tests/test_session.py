import pytest

from hutch.session import Fingerprint, Session


class _FakeContext:
    pages = []

    async def add_init_script(self, script):
        pass

    def on(self, event, callback):
        pass


class _FakeChromium:
    def __init__(self, context):
        self.context = context

    async def launch_persistent_context(self, **kwargs):
        return self.context


class _FakePlaywright:
    def __init__(self, context):
        self.chromium = _FakeChromium(context)


@pytest.mark.asyncio
async def test_launch_passes_public_fingerprint_to_stealth(tmp_path, monkeypatch):
    fingerprint = Fingerprint(platform="Linux x86_64")
    context = _FakeContext()
    captured = {}

    async def fake_apply_stealth(actual_context, *, fingerprint=None):
        captured["context"] = actual_context
        captured["fingerprint"] = fingerprint

    monkeypatch.setattr("hutch.stealth.apply_stealth", fake_apply_stealth)

    session = Session(
        "fingerprint-regression",
        str(tmp_path / "profile"),
        fingerprint=fingerprint,
    )
    session._start_watchdog = lambda: None

    await session.launch(_FakePlaywright(context))

    assert captured == {"context": context, "fingerprint": fingerprint}


def test_headed_launch_uses_dedicated_window_class(tmp_path):
    session = Session(
        "headed-window",
        str(tmp_path / "profile"),
        headless=False,
        stealth=False,
    )

    args = session._launch_args()["args"]

    assert "--class=hutch-browser" in args
    assert "--start-maximized" in args
