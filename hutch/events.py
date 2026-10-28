"""Per-session event log: dialogs, downloads, file choosers, popups and crashes, in order."""

import asyncio
import time
from collections import deque


class EventLog:
    def __init__(self, maxlen=500):
        self._items = deque(maxlen=maxlen)
        self._seq = 0
        self._waiters = []

    @property
    def last(self):
        return self._seq

    def emit(self, type, **data):
        self._seq += 1
        event = {"seq": self._seq, "time": round(time.time(), 3), "type": type, **data}
        self._items.append(event)
        for fut in self._waiters:
            if not fut.done():
                fut.set_result(None)
        self._waiters.clear()
        return event

    def since(self, seq=0, types=None):
        return [e for e in self._items if e["seq"] > seq and (not types or e["type"] in types)]

    async def wait(self, seq=0, types=None, timeout=30.0):
        """Events after seq, waiting up to timeout for the first one to arrive."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            found = self.since(seq, types)
            remaining = deadline - loop.time()
            if found or remaining <= 0:
                return found
            fut = loop.create_future()
            self._waiters.append(fut)
            try:
                await asyncio.wait_for(fut, remaining)
            except asyncio.TimeoutError:
                pass

    def clear(self):
        self._items.clear()
