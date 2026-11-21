"""The warm-profile lifecycle policy — when a profile advances, and when it has burned.

A profile earns trust by behaving like a returning human before it is ever pointed at a target: it
ages on the clock, it accrues benign sessions, and it is watched for how often a challenge renders.
The registry stores those counters; this module is the rule that reads them. ``next_state`` says which
lifecycle state a profile *should* be in given its age, session count and challenge rate, so the warmer
and the registry agree on one policy instead of scattering thresholds. ``is_ready`` is the gate a
claim must pass, and ``warmth_score`` ranks the ready profiles so the most-trusted one serves first.

Grounded in the two laws that govern the first week: behaviour (not fingerprint) is what flags a young
profile, so a profile cannot skip the aging it needs; and a rising challenge rate is the one signal
that a profile is being doubted, so it cools, then retires, long before it can burn a program's slot.
Pure and clock-injected — no browser, no network, no new dependency.
"""

import random
from urllib.parse import urlsplit

HOUR = 3600

TIERS = ("organic", "snapshot")

# benign, high-traffic, non-target sites a warming profile visits to build real history and bank
# cookies / cf_clearance on its pinned proxy. Deliberately generic and never a program target: warming
# traffic must never touch an in-scope host — that is the assigned session's job once the profile is ready.
_WARM_SITES = (
    "https://en.wikipedia.org/wiki/Special:Random",
    "https://www.reddit.com/",
    "https://news.ycombinator.com/",
    "https://www.youtube.com/",
    "https://www.bing.com/",
    "https://duckduckgo.com/",
    "https://stackoverflow.com/",
    "https://www.nytimes.com/",
    "https://www.theguardian.com/international",
    "https://www.imdb.com/",
    "https://weather.com/",
    "https://www.goodreads.com/",
)

# dwell (seconds) and scroll behaviour per tier. organic reads like a person settling in to read; snapshot
# just seeds trust tokens fast. A profile stays in ONE tier for life — mixing the two is an incoherent
# history (a human who browses deep one day and bounce-loads the next looks like two different people).
_TIER = {
    "organic": {"dwell": (25, 120), "scroll": True, "steps": 8},
    "snapshot": {"dwell": (3, 9), "scroll": False, "steps": 4},
}


def plan(seed, tier="organic", steps=None, exclude_hosts=()):
    """A deterministic warming itinerary for a profile: benign sites with human dwell/scroll, seeded by the
    profile so a given profile warms the same way each run. ``exclude_hosts`` drops any site on those hosts
    so warming can never touch a program's target. Returns a list of {url, dwell, scroll, tier} steps."""
    if tier not in TIERS:
        raise ValueError(f"unknown warming tier {tier!r}, expected one of {', '.join(TIERS)}")
    cfg = _TIER[tier]
    excluded = {h.lower().lstrip(".") for h in exclude_hosts}
    pool = [s for s in _WARM_SITES
            if not any(urlsplit(s).hostname.endswith(h) for h in excluded)]
    if not pool:
        raise ValueError("no warming sites left after exclusions")
    rng = random.Random(seed)
    rng.shuffle(pool)
    lo, hi = cfg["dwell"]
    n = steps or cfg["steps"]
    return [{"url": pool[i % len(pool)], "dwell": rng.randint(lo, hi),
             "scroll": cfg["scroll"], "tier": tier} for i in range(n)]


# the warming pipeline's thresholds — benign sessions and wall-clock age a profile accrues before it is
# trusted to serve. Deliberately conservative: a profile that ages too fast reads as a script, not a human.
WARMING_SESSIONS = 3        # fresh/warming -> aging once this many benign sessions are banked
READY_AGE_HOURS = 48        # a profile must be at least this old before it is ever "ready"
READY_SESSIONS = 5          # ...and have banked at least this many sessions

# challenge rate (challenges / sessions) is the health signal. Above COOL the profile rests; above
# RETIRE it is burned. Both need a minimum sample so one early challenge does not retire a new profile.
COOL_RATE = 0.25
COOL_MIN_SESSIONS = 4
RETIRE_RATE = 0.50
RETIRE_MIN_SESSIONS = 4

# states the policy will move a profile between. "assigned" is owned by the registry's claim/release and
# "retired" is terminal, so the policy never moves a profile out of either on its own.
_PIPELINE = ("fresh", "warming", "aging", "ready", "cooling")


def _age_hours(row, now):
    created = row.get("created_at")
    return max(0.0, (now - created) / HOUR) if created else 0.0


def challenge_rate(row):
    """challenges per session, 0.0 when the profile has never run."""
    sessions = row.get("sessions_count") or 0
    return (row.get("challenge_count") or 0) / sessions if sessions else 0.0


def next_state(row, now):
    """The state ``row`` should be in per policy. Returns the current state unchanged for 'assigned' and
    'retired' (not the policy's to move) and whenever nothing has changed, so a caller can diff and skip."""
    state = row.get("state") or "fresh"
    if state in ("assigned", "retired"):
        return state

    sessions = row.get("sessions_count") or 0
    rate = challenge_rate(row)
    if sessions >= RETIRE_MIN_SESSIONS and rate >= RETIRE_RATE:
        return "retired"
    if sessions >= COOL_MIN_SESSIONS and rate >= COOL_RATE:
        return "cooling"

    if state == "fresh":
        return "warming" if sessions >= 1 else "fresh"
    if state == "warming":
        return "aging" if sessions >= WARMING_SESSIONS else "warming"
    if state == "aging":
        return "ready" if (_age_hours(row, now) >= READY_AGE_HOURS and sessions >= READY_SESSIONS) else "aging"
    # ready stays ready while healthy (the cool/retire checks above catch a decline); cooling waits for a
    # human or the warmer to re-age it — the policy does not silently promote a rested profile back.
    return state


def is_ready(row, now):
    """Whether a profile may be claimed to serve: marked ready, old enough, and not challenging too often."""
    return (row.get("state") == "ready"
            and _age_hours(row, now) >= READY_AGE_HOURS
            and challenge_rate(row) <= COOL_RATE)


def warmth_score(row, now):
    """A 0..1 trust score for ranking ready profiles — older, more-used, less-challenged scores higher."""
    age = min(1.0, _age_hours(row, now) / (READY_AGE_HOURS * 2))
    use = min(1.0, (row.get("sessions_count") or 0) / (READY_SESSIONS * 2))
    clean = max(0.0, 1.0 - challenge_rate(row) / RETIRE_RATE)
    return round(0.4 * age + 0.2 * use + 0.4 * clean, 4)
