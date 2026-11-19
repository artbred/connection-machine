"""Exercise real WebKit login and profile reuse without a network or account."""

import os
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
import main


class BrowserSessionTests(unittest.TestCase):
    def test_idle_close_and_restart_reuse_persistent_login(self):
        feed = "https://www.linkedin.com/feed/"
        login = "https://www.linkedin.com/login"
        login_visits = []

        def serve(route):
            if route.request.url == login:
                login_visits.append(login)
                html = """<form onsubmit="event.preventDefault();
                    document.cookie='fixture_login=yes; Path=/; Max-Age=3600; Secure';
                    localStorage.setItem('owner','fixture'); location.href='/feed/'">
                    <input id=username><input id=password type=password>
                    <button type=submit>Sign in</button></form>"""
            elif route.request.url == feed:
                html = """<body><script>if (document.cookie.includes('fixture_login=yes')
                    && localStorage.owner === 'fixture') document.body.innerHTML = '<nav>Feed</nav>';
                    </script></body>"""
            else:
                route.abort()
                return
            route.fulfill(content_type="text/html", body=html)

        authenticate = main.authenticate

        def fixture_auth(page):
            page.context.route("**/*", serve)
            authenticate(page)

        with tempfile.TemporaryDirectory() as profile, patch.object(
            main, "authenticate", fixture_auth
        ), patch.dict(os.environ, LINKEDIN_USERNAME="fixture", LINKEDIN_PASSWORD="fixture"):
            with closing(main.BrowserSession(profile)) as session:
                first = session.acquire_page()
                self.assertIs(session.acquire_page(), first)
                session.close()
                self.assertTrue(first.is_closed())
                reopened = session.acquire_page()
                self.assertTrue(reopened.locator("nav").is_visible())
            with closing(main.BrowserSession(profile)) as restarted:
                self.assertTrue(restarted.acquire_page().locator("nav").is_visible())
        self.assertEqual(login_visits, [login])
