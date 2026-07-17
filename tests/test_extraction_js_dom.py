"""Executes PROFILE_CONTENT_EXTRACTION_JS in a real headless browser.

Covers the contamination scenarios observed live on LinkedIn profile pages:
foreign recommendation modules, footer/aside noise, and Activity items that
are verb-banner cards ("NAME commented on this" / "likes this" / "reposted
this") whose body is another person's post.
"""

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from tasks.invite import (  # noqa: E402
    PROFILE_CONTENT_EXTRACTION_JS,
    PROFILE_FOREIGN_MODULE_HEADINGS,
)

FIXTURE_HTML = """
<main>
  <section>
    <h2>Jane Prospect</h2>
    <div>Building data platforms | Ex-Acme</div>
    <div>Berlin, Germany</div>
    <button>Connect</button>
  </section>
  <section>
    <h2>About</h2>
    <div>I design streaming pipelines and write about event-driven systems.</div>
  </section>
  <section>
    <h2>Activity</h2>
    <ul>
      <li><div>Jane Prospect</div>
        <div>Own post about exactly-once semantics in Kafka pipelines.</div></li>
      <li><div>Jane Prospect · 2nd</div>
        <div>Second own post about schema registries.</div></li>
      <li><div>Jane Prospect commented on this</div>
        <div>Stranger One</div>
        <div>Growth hacking guru | 10x your funnel</div>
        <div>Stranger post body about growth hacking funnels.</div></li>
      <li><div>Jane Prospect likes this</div>
        <div>Stranger Two</div>
        <div>Crypto influencer</div>
        <div>Stranger post body about crypto trading signals.</div></li>
      <li><div>Jane Prospect reposted this</div>
        <div>Stranger Three</div>
        <div>Sales coach</div>
        <div>Stranger post body about cold outreach scripts.</div></li>
    </ul>
  </section>
  <section>
    <h2>Explore Premium profiles</h2>
    <div>Foreign Person One — Aerospace Engineering Graduate</div>
  </section>
  <section>
    <h2>People you may know</h2>
    <div>Foreign Person Two — Quality Engineer</div>
  </section>
  <aside>Ad: Your Company Page is waiting</aside>
  <footer>About Accessibility LinkedIn Corporation</footer>
  <div role="dialog">Add a note to your invitation</div>
</main>
"""


class ExtractionJsDomTest(unittest.TestCase):
    result = None

    @classmethod
    def setUpClass(cls):
        try:
            from patchright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest("patchright is not installed")

        cls._pw = sync_playwright().start()
        try:
            try:
                cls._browser = cls._pw.chromium.launch(headless=True)
            except Exception:
                cls._browser = cls._pw.chromium.launch(headless=True, channel="chrome")
        except Exception as exc:
            cls._pw.stop()
            raise unittest.SkipTest(f"No Chromium available: {exc}")

        page = cls._browser.new_page()
        page.set_content(FIXTURE_HTML)
        cls.result = page.evaluate(
            PROFILE_CONTENT_EXTRACTION_JS, PROFILE_FOREIGN_MODULE_HEADINGS
        )
        cls.fixture_after = page.evaluate("() => document.querySelector('main').innerText")
        cls._browser.close()
        cls._pw.stop()

    def test_extracts_profile_name(self):
        self.assertEqual(self.result["name"], "Jane Prospect")

    def test_keeps_own_content(self):
        content = self.result["content"]
        self.assertIn("Building data platforms", content)
        self.assertIn("streaming pipelines", content)
        self.assertIn("exactly-once semantics", content)
        self.assertIn("schema registries", content)

    def test_drops_foreign_modules_and_chrome(self):
        content = self.result["content"]
        self.assertNotIn("Foreign Person One", content)
        self.assertNotIn("Foreign Person Two", content)
        self.assertNotIn("LinkedIn Corporation", content)
        self.assertNotIn("Your Company Page", content)
        self.assertNotIn("Add a note", content)
        self.assertNotIn("Connect", content)

    def test_drops_verb_banner_activity_items(self):
        content = self.result["content"]
        self.assertNotIn("growth hacking", content)
        self.assertNotIn("crypto trading", content)
        self.assertNotIn("cold outreach", content)
        self.assertNotIn("Stranger One", content)
        self.assertNotIn("Stranger Two", content)
        self.assertNotIn("Stranger Three", content)

    def test_page_state_is_restored_after_extraction(self):
        self.assertIn("Foreign Person One", self.fixture_after)
        self.assertIn("growth hacking", self.fixture_after)


if __name__ == "__main__":
    unittest.main()
