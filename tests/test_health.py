import pytest
from hutch.health import HealthMonitor, Alert, wire_context_to_health
from hutch.context import Context


class TestHealthMonitor:

    def test_no_alerts_initially(self):
        m = HealthMonitor("test")
        assert m.alerts == []
        assert m.summary()["total_alerts"] == 0

    def test_auth_expired_after_threshold(self):
        m = HealthMonitor("test", auth_fail_threshold=3)
        fired = []
        m.on_alert(lambda a: fired.append(a))

        m.check_response(401, "/api/me")
        m.check_response(401, "/api/me")
        assert len(fired) == 0

        m.check_response(401, "/api/me")
        assert len(fired) == 1
        assert fired[0].type == "auth_expired"
        assert "3x 401" in fired[0].detail

    def test_auth_reset_on_success(self):
        m = HealthMonitor("test", auth_fail_threshold=3)
        m.check_response(401, "/a")
        m.check_response(401, "/a")
        m.check_response(200, "/a")
        m.check_response(401, "/a")
        assert len(m.alerts) == 0

    def test_403_counts_as_auth_fail(self):
        m = HealthMonitor("test", auth_fail_threshold=2)
        m.check_response(403, "/admin")
        m.check_response(403, "/admin")
        assert len(m.alerts) == 1
        assert m.alerts[0].type == "auth_expired"

    def test_rate_limited(self):
        m = HealthMonitor("test", rate_limit_threshold=2)
        m.check_response(429, "/api")
        m.check_response(429, "/api")
        assert len(m.alerts) == 1
        assert m.alerts[0].type == "rate_limited"

    def test_rate_limit_reset_on_other(self):
        m = HealthMonitor("test", rate_limit_threshold=3)
        m.check_response(429, "/a")
        m.check_response(429, "/a")
        m.check_response(200, "/a")
        m.check_response(429, "/a")
        assert len(m.alerts) == 0

    def test_captcha_detection_html(self):
        m = HealthMonitor("test")
        html_cf = '<div id="cf-challenge-running">checking your browser</div>'
        assert m.check_page_content(html_cf) is True
        assert len(m.alerts) == 1
        assert m.alerts[0].type == "captcha"

    def test_captcha_detection_recaptcha(self):
        m = HealthMonitor("test")
        html = '<div class="g-recaptcha" data-sitekey="abc"></div>'
        assert m.check_page_content(html) is True

    def test_captcha_detection_hcaptcha(self):
        m = HealthMonitor("test")
        html = '<div class="hcaptcha-box"></div>'
        assert m.check_page_content(html) is True

    def test_captcha_detection_turnstile(self):
        m = HealthMonitor("test")
        html = '<div class="cf-turnstile"></div>'
        assert m.check_page_content(html) is True

    def test_no_captcha_on_normal_page(self):
        m = HealthMonitor("test")
        html = '<html><body><h1>Hello</h1></body></html>'
        assert m.check_page_content(html) is False
        assert len(m.alerts) == 0

    def test_captcha_url_detection(self):
        m = HealthMonitor("test")
        assert m.check_navigation("https://challenges.cloudflare.com/cdn-cgi/challenge") is True
        assert len(m.alerts) == 1

    def test_crash_alert(self):
        m = HealthMonitor("test")
        m.on_crash("page process exited")
        assert len(m.alerts) == 1
        assert m.alerts[0].type == "crashed"

    def test_disconnect_alert(self):
        m = HealthMonitor("test")
        m.on_disconnect("browser closed unexpectedly")
        assert len(m.alerts) == 1
        assert m.alerts[0].type == "disconnected"

    def test_acknowledge_single(self):
        m = HealthMonitor("test", auth_fail_threshold=1)
        m.check_response(401, "/x")
        assert len(m.unacknowledged()) == 1
        m.acknowledge(0)
        assert len(m.unacknowledged()) == 0

    def test_acknowledge_all(self):
        m = HealthMonitor("test", auth_fail_threshold=1, rate_limit_threshold=1)
        m.check_response(401, "/a")
        m.check_response(429, "/b")
        assert len(m.unacknowledged()) == 2
        m.acknowledge()
        assert len(m.unacknowledged()) == 0

    def test_clear(self):
        m = HealthMonitor("test", auth_fail_threshold=1)
        m.check_response(401, "/x")
        m.clear()
        assert len(m.alerts) == 0

    def test_summary(self):
        m = HealthMonitor("test", auth_fail_threshold=1, rate_limit_threshold=1)
        m.check_response(401, "/a")
        m.check_response(429, "/b")
        s = m.summary()
        assert s["total_alerts"] == 2
        assert s["by_type"]["auth_expired"] == 1
        assert s["by_type"]["rate_limited"] == 1

    def test_alert_cap(self):
        m = HealthMonitor("test", auth_fail_threshold=1, alert_cap=5)
        for _ in range(10):
            m.check_response(401, "/x")
        assert len(m.alerts) == 5

    def test_callback_removal(self):
        m = HealthMonitor("test", auth_fail_threshold=1)
        fired = []
        unsub = m.on_alert(lambda a: fired.append(a))
        m.check_response(401, "/x")
        assert len(fired) == 1
        unsub()
        m.check_response(401, "/x")
        assert len(fired) == 1


class TestWireContextToHealth:

    def test_network_events_feed_health(self):
        ctx = Context()
        mon = HealthMonitor("test", auth_fail_threshold=2)
        wire_context_to_health(ctx, mon)

        from hutch.context import NetworkEntry
        e1 = NetworkEntry(seq=1, method="GET", url="/api/me", status=401, timestamp=1)
        ctx.network.append(e1)
        ctx._emit("network", e1)

        e2 = NetworkEntry(seq=2, method="GET", url="/api/me", status=401, timestamp=2)
        ctx.network.append(e2)
        ctx._emit("network", e2)

        assert len(mon.alerts) == 1
        assert mon.alerts[0].type == "auth_expired"

    def test_navigation_events_feed_health(self):
        ctx = Context()
        mon = HealthMonitor("test")
        wire_context_to_health(ctx, mon)

        from hutch.context import NavigationEntry
        nav = NavigationEntry(seq=1, url="https://challenges.cloudflare.com/abc", timestamp=1)
        ctx.navigations.append(nav)
        ctx._emit("navigation", nav)

        assert len(mon.alerts) == 1
        assert mon.alerts[0].type == "captcha"
