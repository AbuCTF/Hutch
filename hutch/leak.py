"""Does a driver switch on DevTools' Runtime domain, where a page can tell?

Automation libraries send ``Runtime.enable`` so they can follow script contexts. With it on, the
browser serialises every ``console`` argument for the client, and in doing so reads properties of an
``Error`` that page code can make into getters: a script logs a trap error and sees whether its
getter ran. Detectors use exactly this. ``PROBE`` is that script. It sets two traps, because Chrome
moved which property it reads: older builds read ``stack``, current ones read ``name`` (once on their
own, twice with ``Runtime.enable`` on). ``leaks`` is true when either gives it away.

Plain Playwright leaks; patchright and the cdp driver do not. ``hutch leakcheck`` shows it for a
driver, and the test suite keeps the probe honest by turning ``Runtime.enable`` on by hand.
"""

PROBE = """(async () => {
  const hits = {};
  for (const key of ['stack', 'name']) {
    let n = 0;
    const trap = new Error('probe');
    Object.defineProperty(trap, key, {get() { n++; return 'probe'; }});
    console.debug(trap);
    await new Promise(done => setTimeout(done, 50));
    hits[key] = n;
  }
  return hits;
})()"""


def verdict(hits):
    """{"stack": n, "name": n, "leaks": bool} from the probe's counts."""
    return {**hits, "leaks": hits.get("stack", 0) > 0 or hits.get("name", 0) > 1}


async def check(session):
    """Run the probe in the session's active tab (main world) and say whether the driver leaks."""
    return {"driver": session.driver, **verdict(await session.evaluate(PROBE))}
