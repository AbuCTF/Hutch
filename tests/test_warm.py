from hutch import warm

NOW = 1_700_000_000
H = warm.HOUR


def row(state="fresh", age_h=0, sessions=0, challenges=0):
    return {"state": state, "created_at": NOW - int(age_h * H),
            "sessions_count": sessions, "challenge_count": challenges}


def test_fresh_advances_on_first_session():
    assert warm.next_state(row("fresh", sessions=0), NOW) == "fresh"
    assert warm.next_state(row("fresh", sessions=1), NOW) == "warming"


def test_warming_advances_to_aging_after_enough_sessions():
    assert warm.next_state(row("warming", sessions=2), NOW) == "warming"
    assert warm.next_state(row("warming", sessions=3), NOW) == "aging"


def test_aging_needs_both_age_and_sessions_for_ready():
    assert warm.next_state(row("aging", age_h=10, sessions=9), NOW) == "aging"   # too young
    assert warm.next_state(row("aging", age_h=72, sessions=3), NOW) == "aging"   # too few sessions
    assert warm.next_state(row("aging", age_h=72, sessions=6), NOW) == "ready"


def test_high_challenge_rate_cools_then_retires():
    assert warm.next_state(row("ready", age_h=100, sessions=8, challenges=2), NOW) == "cooling"
    assert warm.next_state(row("ready", age_h=100, sessions=8, challenges=5), NOW) == "retired"


def test_one_early_challenge_does_not_burn_a_new_profile():
    # 1 challenge in 2 sessions is a 0.5 rate but below the minimum sample, so it keeps warming
    assert warm.next_state(row("warming", sessions=2, challenges=1), NOW) == "warming"


def test_assigned_and_retired_are_left_alone():
    assert warm.next_state(row("assigned", age_h=100, sessions=9), NOW) == "assigned"
    assert warm.next_state(row("retired", age_h=100, sessions=1, challenges=1), NOW) == "retired"


def test_is_ready_gate():
    assert warm.is_ready(row("ready", age_h=72, sessions=6), NOW)
    assert not warm.is_ready(row("aging", age_h=72, sessions=6), NOW)          # wrong state
    assert not warm.is_ready(row("ready", age_h=10, sessions=6), NOW)          # too young
    assert not warm.is_ready(row("ready", age_h=72, sessions=8, challenges=3), NOW)  # challenging


def test_warmth_score_orders_profiles():
    clean = row("ready", age_h=200, sessions=20, challenges=0)
    challenged = row("ready", age_h=200, sessions=20, challenges=8)
    young = row("ready", age_h=48, sessions=5, challenges=0)
    assert warm.warmth_score(clean, NOW) > warm.warmth_score(challenged, NOW)
    assert warm.warmth_score(clean, NOW) > warm.warmth_score(young, NOW)
    assert 0.0 <= warm.warmth_score(challenged, NOW) <= 1.0


def test_challenge_rate_zero_when_never_run():
    assert warm.challenge_rate(row("fresh", sessions=0, challenges=0)) == 0.0
    assert warm.challenge_rate(row("ready", sessions=4, challenges=1)) == 0.25


def test_plan_is_deterministic_per_seed():
    assert warm.plan("acme-a1") == warm.plan("acme-a1")
    assert warm.plan("acme-a1") != warm.plan("acme-b2")


def test_plan_tiers_differ_in_pace_and_depth():
    organic = warm.plan("s", tier="organic")
    snapshot = warm.plan("s", tier="snapshot")
    assert len(organic) == 8 and len(snapshot) == 4
    assert all(step["scroll"] for step in organic)
    assert all(not step["scroll"] for step in snapshot)
    assert max(s["dwell"] for s in snapshot) < min(s["dwell"] for s in organic)


def test_plan_never_visits_an_excluded_target():
    itinerary = warm.plan("s", steps=12, exclude_hosts=["reddit.com", "youtube.com"])
    hosts = {__import__("urllib.parse", fromlist=["urlsplit"]).urlsplit(s["url"]).hostname
             for s in itinerary}
    assert not any(h.endswith("reddit.com") or h.endswith("youtube.com") for h in hosts)


def test_plan_rejects_unknown_tier():
    import pytest
    with pytest.raises(ValueError):
        warm.plan("s", tier="turbo")
