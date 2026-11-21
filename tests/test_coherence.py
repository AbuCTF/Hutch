import pytest

from hutch import coherence, fingerprint


def _levels(fp, **kw):
    return {i.layer: i.level for i in coherence.inspect(fp, **kw)}


def test_every_preset_is_coherent():
    for p in fingerprint.list_presets():
        fp = fingerprint.generate(preset=p["name"])
        assert coherence.inspect(fp) == [], f"{p['name']} tripped coherence"
        assert coherence.enforce(fp) == []


def test_mac_platform_under_windows_ua_is_an_error():
    fp = fingerprint.generate(preset="win-desktop-1080p")
    fp.platform = "MacIntel"
    assert _levels(fp)["platform"] == "error"
    with pytest.raises(coherence.IncoherentIdentity):
        coherence.enforce(fp, name="s1")


def test_client_hint_major_lag_is_an_error():
    fp = fingerprint.generate(preset="linux-desktop")
    fp.user_agent = fp.user_agent.replace("Chrome/", "Chrome/", 1)
    import re
    fp.user_agent = re.sub(r"Chrome/\d+", "Chrome/159", fp.user_agent)
    # sec_ch_ua still carries the old major -> contradiction
    assert _levels(fp)["client-hints"] == "error"
    with pytest.raises(coherence.IncoherentIdentity):
        coherence.enforce(fp)


def test_region_bound_locale_on_foreign_clock_is_a_warning():
    fp = fingerprint.generate(preset="win-desktop-1080p", locale="de-DE")
    issues = coherence.inspect(fp)
    assert [i.level for i in issues] == ["warn"]
    assert issues[0].layer == "locale"
    # a warning never blocks a launch
    assert coherence.enforce(fp) == issues


def test_english_locale_never_contradicts_a_timezone():
    fp = fingerprint.generate(preset="win-desktop-1080p", locale="en-US", timezone="Asia/Tokyo")
    assert [i for i in coherence.inspect(fp) if i.layer == "locale"] == []


def test_proxy_region_cross_check():
    fp = fingerprint.generate(preset="win-desktop-1080p")  # America/New_York
    assert [i.layer for i in coherence.inspect(fp, proxy_region="europe")] == ["proxy"]
    assert coherence.inspect(fp, proxy_region="america") == []


def test_mode_off_and_warn_never_raise():
    fp = fingerprint.generate(preset="win-desktop-1080p")
    fp.platform = "MacIntel"
    assert coherence.enforce(fp, mode="off") == []
    assert all(i.level == "error" for i in coherence.enforce(fp, mode="warn"))


def test_env_escape_hatch(monkeypatch):
    fp = fingerprint.generate(preset="win-desktop-1080p")
    fp.platform = "Linux x86_64"
    monkeypatch.setenv("HUTCH_COHERENCE", "off")
    assert coherence.enforce(fp) == []


def test_errors_sort_before_warnings():
    fp = fingerprint.generate(preset="win-desktop-1080p", locale="de-DE")
    fp.platform = "MacIntel"
    levels = [i.level for i in coherence.inspect(fp)]
    assert levels == ["error", "warn"]
