import json
import os
import shutil
import pytest
from hutch.artifacts import ArtifactStore


_TEST_DIR = os.path.expanduser("~/.hutch/test-artifacts")


@pytest.fixture
def store():
    os.makedirs(_TEST_DIR, exist_ok=True)
    s = ArtifactStore(base_dir=_TEST_DIR)
    yield s
    if os.path.isdir(_TEST_DIR):
        shutil.rmtree(_TEST_DIR)


class TestArtifactStore:

    def test_save_har(self, store):
        har = {"log": {"entries": [{"url": "https://example.com"}]}}
        meta = store.save_har("test-session", har)
        assert meta.type == "har"
        assert os.path.exists(meta.path)
        with open(meta.path) as f:
            loaded = json.load(f)
        assert loaded["log"]["entries"][0]["url"] == "https://example.com"

    def test_save_har_with_label(self, store):
        meta = store.save_har("test-session", {"log": {}}, label="login-flow")
        assert "login-flow" in meta.path

    def test_save_screenshot(self, store):
        png = b"\x89PNG\r\n\x1a\nfake"
        meta = store.save_screenshot("test-session", png)
        assert meta.type == "screenshot"
        with open(meta.path, "rb") as f:
            assert f.read() == png

    def test_save_snapshot(self, store):
        data = {"url": "https://example.com", "title": "Test", "network": []}
        meta = store.save_snapshot("test-session", data)
        assert meta.type == "snapshot"
        with open(meta.path) as f:
            loaded = json.load(f)
        assert loaded["url"] == "https://example.com"

    def test_notes_crud(self, store):
        store.note("test-session", "auth", {"type": "jwt", "header": "Authorization"})
        notes = store.notes("test-session")
        assert notes["auth"]["type"] == "jwt"

        store.note("test-session", "endpoints", ["/api/v1/users", "/api/v1/admin"])
        notes = store.notes("test-session")
        assert len(notes) == 2

        store.delete_note("test-session", "auth")
        notes = store.notes("test-session")
        assert "auth" not in notes
        assert "endpoints" in notes

    def test_notes_empty(self, store):
        notes = store.notes("nonexistent")
        assert notes == {}

    def test_note_overwrite(self, store):
        store.note("test-session", "key", "v1")
        store.note("test-session", "key", "v2")
        assert store.notes("test-session")["key"] == "v2"

    def test_list_artifacts(self, store):
        store.save_har("test-session", {"log": {}})
        store.save_screenshot("test-session", b"png")
        store.save_snapshot("test-session", {"url": "x"})

        all_arts = store.list_artifacts("test-session")
        assert len(all_arts) == 3

        hars = store.list_artifacts("test-session", type_filter="har")
        assert len(hars) == 1

        screenshots = store.list_artifacts("test-session", type_filter="screenshot")
        assert len(screenshots) == 1

    def test_list_empty_session(self, store):
        arts = store.list_artifacts("nonexistent")
        assert arts == []

    def test_purge(self, store):
        store.save_har("test-session", {"log": {}})
        store.note("test-session", "key", "val")
        store.purge("test-session")
        assert store.notes("test-session") == {}
        assert store.list_artifacts("test-session") == []

    def test_sessions_with_artifacts(self, store):
        store.note("session-a", "k", "v")
        store.note("session-b", "k", "v")
        sessions = store.sessions_with_artifacts()
        assert "session-a" in sessions
        assert "session-b" in sessions

    def test_snapshot_with_bytes(self, store):
        data = {"screenshot": b"\x89PNG", "url": "https://x.com"}
        meta = store.save_snapshot("test-session", data)
        with open(meta.path) as f:
            loaded = json.load(f)
        assert "<4 bytes>" in loaded["screenshot"]
