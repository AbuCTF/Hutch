import pytest
from hutch.differ import diff_responses, ResponseDiff


def _entry(url, status=200, method="GET", body="", headers=None):
    return {
        "url": url,
        "status": status,
        "method": method,
        "response_body": body,
        "response_headers": headers or {},
    }


class TestDiffResponses:

    def test_identical_responses(self):
        a = [_entry("/api/me", 200, body='{"id":1}')]
        b = [_entry("/api/me", 200, body='{"id":1}')]
        diffs = diff_responses(a, b, "user-a", "user-b")
        assert len(diffs) == 1
        assert diffs[0].status_match is True
        assert diffs[0].body_match is True
        assert diffs[0].interesting is False

    def test_status_diff_flagged(self):
        a = [_entry("/api/admin", 200, body="admin panel")]
        b = [_entry("/api/admin", 403, body="forbidden")]
        diffs = diff_responses(a, b, "user-a", "user-b")
        assert len(diffs) == 1
        assert diffs[0].status_match is False
        assert diffs[0].interesting is True
        assert any("authz_bypass" in n for n in diffs[0].notes)

    def test_reverse_authz_bypass(self):
        a = [_entry("/api/admin", 401, body="unauthorized")]
        b = [_entry("/api/admin", 200, body="admin data")]
        diffs = diff_responses(a, b, "low-priv", "admin")
        assert diffs[0].interesting is True
        assert any("session_b gets 200" in n for n in diffs[0].notes)

    def test_body_diff_generated(self):
        a = [_entry("/api/profile", 200, body='{"name":"Alice","role":"admin"}')]
        b = [_entry("/api/profile", 200, body='{"name":"Bob","role":"user"}')]
        diffs = diff_responses(a, b, "alice", "bob")
        assert diffs[0].body_match is False
        assert diffs[0].body_diff is not None
        assert "Alice" in diffs[0].body_diff
        assert "Bob" in diffs[0].body_diff

    def test_size_disparity_flagged(self):
        a = [_entry("/api/data", 200, body="x" * 1000)]
        b = [_entry("/api/data", 200, body="x" * 100)]
        diffs = diff_responses(a, b, "a", "b")
        assert diffs[0].interesting is True
        assert any("size_disparity" in n for n in diffs[0].notes)

    def test_authz_language_asymmetry(self):
        a = [_entry("/api/secrets", 200, body="access denied, forbidden")]
        b = [_entry("/api/secrets", 200, body='{"data": "sensitive"}')]
        diffs = diff_responses(a, b, "a", "b")
        assert diffs[0].interesting is True
        assert any("authz_asymmetry" in n for n in diffs[0].notes)

    def test_noise_headers_ignored(self):
        a = [_entry("/api/x", 200, headers={"date": "Mon", "content-type": "text/html"})]
        b = [_entry("/api/x", 200, headers={"date": "Tue", "content-type": "text/html"})]
        diffs = diff_responses(a, b, "a", "b", ignore_noise=True)
        assert len(diffs[0].header_diffs) == 0

    def test_noise_headers_shown_when_not_ignored(self):
        a = [_entry("/api/x", 200, headers={"date": "Mon"})]
        b = [_entry("/api/x", 200, headers={"date": "Tue"})]
        diffs = diff_responses(a, b, "a", "b", ignore_noise=False)
        assert len(diffs[0].header_diffs) > 0

    def test_url_pattern_filter(self):
        a = [_entry("/api/users", 200), _entry("/static/logo.png", 200)]
        b = [_entry("/api/users", 200), _entry("/static/logo.png", 200)]
        diffs = diff_responses(a, b, "a", "b", url_pattern="/api/")
        assert len(diffs) == 1
        assert "/api/users" in diffs[0].url

    def test_no_match_skipped(self):
        a = [_entry("/api/a", 200)]
        b = [_entry("/api/b", 200)]
        diffs = diff_responses(a, b, "a", "b")
        assert len(diffs) == 0

    def test_interesting_sorted_first(self):
        a = [
            _entry("/api/normal", 200, body="ok"),
            _entry("/api/admin", 200, body="admin data"),
        ]
        b = [
            _entry("/api/normal", 200, body="ok"),
            _entry("/api/admin", 403, body="forbidden"),
        ]
        diffs = diff_responses(a, b, "a", "b")
        assert diffs[0].interesting is True
        assert "/api/admin" in diffs[0].url

    def test_method_matching(self):
        a = [_entry("/api/x", 200, method="POST")]
        b = [_entry("/api/x", 200, method="GET")]
        diffs = diff_responses(a, b, "a", "b")
        assert len(diffs) == 0
