import asyncio

from hutch.session import Session


class _FakePage:
    def __init__(self):
        self.listeners = {}

    def on(self, event, callback):
        self.listeners.setdefault(event, []).append(callback)


def test_page_load_resyncs_hyprland_viewport_once():
    async def scenario():
        session = object.__new__(Session)
        page = _FakePage()
        synced = asyncio.Event()
        calls = []

        async def sync(candidate):
            calls.append(candidate)
            synced.set()

        session._sync_hyprland_viewport = sync
        session._watch_hyprland_viewport(page)
        session._watch_hyprland_viewport(page)

        assert len(page.listeners["load"]) == 1
        page.listeners["load"][0]()
        await asyncio.wait_for(synced.wait(), timeout=1)
        assert calls == [page]

    asyncio.run(scenario())
