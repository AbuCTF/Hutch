import json
import os

import pytest

from hutch import registry


@pytest.fixture
def reg(tmp_path):
    r = registry.Registry(path=str(tmp_path / "registry.sqlite"))
    yield r
    r.close()


def test_register_and_get(reg):
    reg.register("p1", program_id="acme", role="a", proxy_id="http://res:1", state="ready")
    row = reg.get("p1")
    assert row["profile_name"] == "p1"
    assert row["program_id"] == "acme"
    assert row["role"] == "a"
    assert row["state"] == "ready"
    assert row["sessions_count"] == 0


def test_register_upsert_preserves_counters(reg):
    reg.register("p1", program_id="acme", role="a", state="ready")
    reg.record_use("p1", challenged=True)
    reg.register("p1", program_id="acme", role="a", state="assigned")
    row = reg.get("p1")
    assert row["state"] == "assigned"
    assert row["sessions_count"] == 1
    assert row["challenge_count"] == 1


def test_claim_returns_the_same_profile(reg):
    reg.register("p1", program_id="acme", role="a", state="ready")
    first = reg.claim("acme", "a")
    second = reg.claim("acme", "a")
    assert first["profile_name"] == "p1"
    assert second["profile_name"] == "p1"
    assert second["state"] == "assigned"


def test_claim_none_when_no_ready_profile(reg):
    assert reg.claim("acme", "a") is None
    reg.register("p1", program_id="acme", role="a", state="warming")
    assert reg.claim("acme", "a") is None


def test_assign_conflict_on_second_profile_same_role(reg):
    reg.register("p1")
    reg.register("p2")
    reg.assign("p1", "acme", "a")
    with pytest.raises(ValueError):
        reg.assign("p2", "acme", "a")
    reg.assign("p2", "acme", "b")
    assert reg.get("p2")["role"] == "b"


def test_assign_same_profile_is_idempotent(reg):
    reg.register("p1")
    reg.assign("p1", "acme", "a")
    reg.assign("p1", "acme", "a", target_host="acme.test")
    row = reg.get("p1")
    assert row["role"] == "a"
    assert row["target_host"] == "acme.test"


def test_retired_role_frees_the_slot(reg):
    reg.register("p1")
    reg.assign("p1", "acme", "a")
    reg.retire("p1")
    reg.register("p2")
    reg.assign("p2", "acme", "a")
    assert reg.get("p2")["role"] == "a"


def test_record_use_and_states(reg):
    reg.register("p1", program_id="acme", role="a", state="ready")
    reg.record_use("p1")
    reg.record_use("p1", challenged=True)
    row = reg.get("p1")
    assert row["sessions_count"] == 2
    assert row["challenge_count"] == 1
    assert reg.release("p1")["state"] == "cooling"
    assert reg.retire("p1", note="burned")["state"] == "retired"
    assert reg.get("p1")["note"] == "burned"


def test_record_use_auto_advances_the_pipeline(reg):
    reg.register("p1", program_id="acme", role="a", state="fresh")
    assert reg.record_use("p1")["state"] == "warming"       # fresh -> warming on first session
    reg.record_use("p1")
    assert reg.record_use("p1")["state"] == "aging"         # 3 sessions -> aging
    assert reg.record_use("p1", advance=False)["state"] == "aging"  # opting out leaves state alone


def test_advance_promotes_to_ready_past_the_age_gate(reg):
    reg.register("p1", program_id="acme", role="a", state="aging")
    created = reg.get("p1")["created_at"]
    for _ in range(5):
        reg.record_use("p1", advance=False)
    assert reg.advance("p1", now=created + 72 * 3600)["state"] == "ready"


def test_record_use_retires_a_burned_profile(reg):
    reg.register("p1", program_id="acme", role="a", state="ready")
    for _ in range(4):
        reg.record_use("p1", challenged=True)
    assert reg.get("p1")["state"] == "retired"


def test_advance_all_sweeps_and_leaves_assigned_alone(reg):
    reg.register("p1", program_id="acme", role="a", state="fresh")
    reg.register("p2", program_id="acme", role="b", state="assigned")
    reg.record_use("p1", advance=False)
    moved = {m["profile_name"]: (m["from"], m["to"]) for m in reg.advance_all()}
    assert moved["p1"] == ("fresh", "warming")
    assert "p2" not in moved


def test_ls_filters(reg):
    reg.register("p1", program_id="acme", role="a", state="ready")
    reg.register("p2", program_id="acme", role="b", state="warming")
    reg.register("p3", program_id="other", role="a", state="ready")
    assert len(reg.ls()) == 3
    assert {r["profile_name"] for r in reg.ls(program_id="acme")} == {"p1", "p2"}
    assert {r["profile_name"] for r in reg.ls(state="ready")} == {"p1", "p3"}


def test_bad_state_and_role_raise(reg):
    with pytest.raises(ValueError):
        reg.register("p1", state="toasty")
    with pytest.raises(ValueError):
        reg.register("p1", role="z")


def test_fingerprint_hash_stable_and_distinguishes(reg):
    a = {"platform": "MacIntel", "user_agent": "x", "locale": "en-US", "timezone": "UTC",
         "screen_width": 1440, "screen_height": 900}
    b = dict(a, platform="Win32")
    assert registry.fingerprint_hash(a) == registry.fingerprint_hash(dict(a))
    assert registry.fingerprint_hash(a) != registry.fingerprint_hash(b)
    assert registry.fingerprint_hash(None) is None


def _write_profile(base, name, proxy):
    d = os.path.join(base, name)
    os.makedirs(d)
    meta = {"name": name, "created_at": 1700000000, "proxy": proxy,
            "fingerprint": {"platform": "Linux x86_64", "user_agent": "ua", "locale": "en-US",
                            "timezone": "America/New_York", "screen_width": 1920, "screen_height": 1080},
            "tags": {"program": "acme"}}
    with open(os.path.join(d, "hutch_meta.json"), "w") as fh:
        json.dump(meta, fh)


def test_import_tolerates_odd_meta_shapes(tmp_path):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    _write_profile(str(profiles), "ok", {"server": "http://res:1"})
    os.makedirs(profiles / "listtags")
    (profiles / "listtags" / "hutch_meta.json").write_text(
        json.dumps({"name": "listtags", "proxy": {"server": "http://res:2"}, "tags": ["a", "b"]}))
    os.makedirs(profiles / "toplist")
    (profiles / "toplist" / "hutch_meta.json").write_text("[1, 2, 3]")
    reg = registry.Registry(path=str(tmp_path / "r.sqlite"))
    summary = reg.import_profiles(base_dir=str(profiles))
    assert summary["skipped"] == 1
    assert reg.get("listtags")["program_id"] is None
    assert reg.get("listtags")["state"] == "aging"
    reg.close()


def test_import_profiles_quarantines_proxy_null(tmp_path):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    _write_profile(str(profiles), "with-proxy", {"server": "http://res:1"})
    _write_profile(str(profiles), "no-proxy", None)
    os.makedirs(profiles / "corrupt")
    (profiles / "corrupt" / "hutch_meta.json").write_text("{not json")
    reg = registry.Registry(path=str(tmp_path / "r.sqlite"))
    summary = reg.import_profiles(base_dir=str(profiles))
    assert summary == {"imported": 1, "quarantined": 1, "skipped": 1}
    assert reg.get("with-proxy")["state"] == "aging"
    assert reg.get("with-proxy")["program_id"] == "acme"
    assert reg.get("no-proxy")["state"] == "retired"
    assert "quarantined" in reg.get("no-proxy")["note"]
    reg.close()
