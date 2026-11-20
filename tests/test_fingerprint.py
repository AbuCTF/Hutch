import re

from hutch import fingerprint


def test_generate_repins_to_installed_chrome(monkeypatch):
    monkeypatch.setattr(fingerprint, "_installed_chrome_version", lambda: "156.0.1.2")
    fp = fingerprint.generate(preset="win-desktop-1080p")
    assert "Chrome/156.0.0.0" in fp.user_agent
    assert "Chrome/137" not in fp.user_agent
    assert 'v="156"' in (fp.sec_ch_ua or "")
    assert 'v="137"' not in (fp.sec_ch_ua or "")


def test_sec_ch_ua_major_follows_ua(monkeypatch):
    monkeypatch.setattr(fingerprint, "_installed_chrome_version", lambda: "148.0.0.0")
    fp = fingerprint.generate(preset="linux-desktop")
    ua_major = re.search(r"Chrome/(\d+)", fp.user_agent).group(1)
    assert ua_major == "148"
    assert f'v="{ua_major}"' in fp.sec_ch_ua


def test_generate_keeps_preset_when_no_chrome(monkeypatch):
    monkeypatch.setattr(fingerprint, "_installed_chrome_version", lambda: None)
    fp = fingerprint.generate(preset="mac-desktop-retina")
    assert "Chrome/137" in fp.user_agent
