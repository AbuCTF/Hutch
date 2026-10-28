import json

from hutch.differ import diff_responses
from hutch.noise import DEFAULT, Noise


def test_json_drops_noisy_keys_anywhere_and_blanks_tokens():
    a = DEFAULT.body(json.dumps({"id": 1, "csrf_token": "aZ9x", "items": [{"n": "a", "next_cursor": "q1"}],
                                 "at": "2026-10-28T10:00:00Z", "file": "1f0e3d6c-1b2a-4c3d-8e9f-0a1b2c3d4e5f"}))
    b = DEFAULT.body(json.dumps({"file": "9a8b7c6d-1b2a-4c3d-8e9f-0a1b2c3d4e5f", "id": 1, "csrf_token": "q",
                                 "at": "2026-10-28T11:22:33Z", "items": [{"next_cursor": "q2", "n": "a"}]}))
    assert a == b == '{"at":"~","file":"~","id":1,"items":[{"n":"a"}]}'


def test_html_query_headers_and_cookies():
    html = '<input type=hidden name="__RequestVerificationToken" value="x1"><meta name=csrf-token content="m">' \
           '<input name=q value="keep"><a href="/p?_WD_ACTION_=AJAX&id=3">'
    out = DEFAULT.body(html)
    assert 'value=""' in out and 'value="keep"' in out and "AJAX" not in out and "id=3" in out
    assert DEFAULT.url("https://t.test/a?_=1793190000&id=5&page_token=zz") == "https://t.test/a?id=5"
    assert DEFAULT.headers({"CF-Ray": "1", "NSC_JOxyz": "a", "Content-Type": "x",
                            "X-Accellion-Transaction-Id": "9", "traceparent": "00-ab"}) == {"Content-Type": "x"}
    assert DEFAULT.body("__cf_bm=abc.def; NSC_JOab=0123ff;") == "~; ~;"


def test_rules_add_to_defaults_and_can_stand_alone():
    rid = DEFAULT.merged({"keys": ["rid"], "headers": ["x-edge-*"]})
    assert rid.body('{"rid": "7f3a", "ok": 1}') == '{"ok":1}' and rid.headers({"X-Edge-Node": "a"}) == {}
    assert DEFAULT.body('{"rid": "7f3a"}') == '{"rid":"7f3a"}'
    bare = Noise(defaults=False)
    assert bare.body('{"csrf": "x"}') == '{"csrf":"x"}'


def test_differ_ignores_noise_but_not_meaning():
    a = [{"method": "GET", "url": "https://t.test/api/me?_=1793190000", "status": 200,
          "response_headers": {"cf-ray": "a1", "x-request-id": "r1"},
          "response_body": '{"user": "alice", "csrf": "t1", "ts": 1793190000}'}]
    b = [{"method": "GET", "url": "https://t.test/api/me?_=1793190999", "status": 200,
          "response_headers": {"cf-ray": "b2", "x-request-id": "r2"},
          "response_body": '{"user": "alice", "csrf": "t2", "ts": 1793190999}'}]
    same = diff_responses(a, b, "a", "b")
    assert len(same) == 1 and same[0].body_match and same[0].header_diffs == []
    b[0]["response_body"] = '{"user": "bob", "csrf": "t2", "ts": 1793190999}'
    assert not diff_responses(a, b, "a", "b")[0].body_match
