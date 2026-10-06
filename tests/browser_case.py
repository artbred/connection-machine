"""Real Chromium DOM tests with isolated contexts and fixture-only networking."""

import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class OfflineBrowserTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            from patchright.sync_api import sync_playwright
        except ImportError as exc:
            raise unittest.SkipTest("patchright is not installed") from exc
        cls._pw = sync_playwright().start()
        try:
            try:
                cls._browser = cls._pw.chromium.launch(headless=True)
            except Exception:
                cls._browser = cls._pw.chromium.launch(headless=True, channel="chrome")
        except Exception as exc:
            cls._pw.stop()
            raise unittest.SkipTest(
                f"No local Chromium or Chrome available: {exc}"
            ) from exc

    @classmethod
    def tearDownClass(cls):
        cls._browser.close()
        cls._pw.stop()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.context = self._browser.new_context(service_workers="block")
        self.addCleanup(self.context.close)
        self._fixtures = {}
        self.blocked_requests = []

        def route_request(route):
            request = route.request
            html = self._fixtures.get(request.url)
            if html is not None and request.is_navigation_request():
                route.fulfill(status=200, content_type="text/html", body=html)
            else:
                self.blocked_requests.append(request.url)
                route.abort()

        self.context.route("**/*", route_request)
        self.page = self.context.new_page()
        self.page.set_default_timeout(1500)

    def load_html(
        self, html: str, url: str = "https://www.linkedin.com/in/jane-prospect/"
    ):
        self._fixtures[url] = html
        self.page.goto(url, wait_until="domcontentloaded")
