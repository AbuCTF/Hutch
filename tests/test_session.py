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


def test_headed_launch_uses_dedicated_window_class(tmp_path, monkeypatch):
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    session = Session(
        "headed-window",
        str(tmp_path / "profile"),
        headless=False,
        stealth=False,
    )

    args = session._launch_args()["args"]

    assert "--class=hutch-browser-headed-window" in args
    assert "--start-maximized" in args


def test_hyprland_headed_launch_defers_geometry_to_compositor(
        tmp_path, monkeypatch):
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "test-instance")
    session = Session(
        "hyprland-window",
        str(tmp_path / "profile"),
        headless=False,
        stealth=False,
    )

    args = session._launch_args()["args"]

    assert "--class=hutch-browser-hyprland-window" in args
    assert "--start-maximized" not in args


@pytest.mark.asyncio
async def test_hyprland_viewport_tracks_scaled_tile(tmp_path, monkeypatch):
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "test-instance")
    session = Session(
        "scaled-tile",
        str(tmp_path / "profile"),
        headless=False,
        stealth=False,
    )

    client_samples = iter([
        {
            "class": "hutch-browser-scaled-tile",
            "at": [1536, 34],
            "size": [2024, 662],
            "monitor": 1,
            "mapped": True,
        },
        {
            "class": "hutch-browser-scaled-tile",
            "at": [1548, 34],
            "size": [1005, 1106],
            "monitor": 1,
            "mapped": True,
        },
    ])
    stable_client = {
        "class": "hutch-browser-scaled-tile",
        "at": [1548, 34],
        "size": [1005, 1106],
        "monitor": 1,
        "mapped": True,
    }

    async def fake_hyprctl(resource):
        if resource == "clients":
            return [next(client_samples, stable_client)]
        return [{
            "name": "HDMI-A-2",
            "x": 1536,
            "y": 0,
            "width": 2560,
            "height": 1440,
            "scale": 1.25,
            "transform": 0,
        }]

    class Page:
        viewport = None

        async def evaluate(self, expression):
            return {"dpr": 1, "chromeHeight": 85}

        async def set_viewport_size(self, viewport):
            self.viewport = viewport

    monkeypatch.setattr(session, "_hyprctl_json", fake_hyprctl)
    page = Page()

    await session._sync_hyprland_viewport(page)

    assert page.viewport == {"width": 1232, "height": 1273}
