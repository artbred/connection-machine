"""No credentials, database or browser needed to exercise preview refusal paths."""

import io
import os
import sys
import tempfile
import unittest
from contextlib import chdir, redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')
import main
from exceptions import SessionExpiredException


class DryRunTests(unittest.TestCase):
    def test_dry_run_rejects_send_options_before_startup(self):
        for flags in (['--submit-comment'], ['--send-notification-invite'],
                      ['--debug-invite', 'https://www.linkedin.com/in/fixture/']):
            with self.subTest(flags=flags), patch.object(sys, 'argv', ['cm', '--dry-run', 'comment', *flags]), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main.parse_args()
                self.assertEqual(error.exception.code, 2)

    def test_logged_out_preview_neither_submits_credentials_nor_initializes_queue(self):
        playwright = MagicMock()
        with (
            patch.object(sys, 'argv', ['cm', '--dry-run', 'notifications']),
            tempfile.TemporaryDirectory() as work,
            chdir(work),
            patch.object(main.signal, 'signal'),
            patch.object(main, 'sync_playwright', return_value=playwright),
            patch.object(main, 'check_linkedin_auth', return_value=False),
            patch.object(main, 'login', side_effect=AssertionError('Preview submitted credentials')),
            patch.object(main, 'init_db', side_effect=AssertionError('Preview opened queue')),
        ):
            profile = Path("data/connection-machine-webkit")
            with self.assertRaisesRegex(SessionExpiredException, "existing authenticated"):
                main.main()
            self.assertFalse(profile.exists())
            profile.mkdir(parents=True)
            with self.assertRaisesRegex(SessionExpiredException, 'authenticated WebKit profile'):
                main.main()

    def test_login_wall_and_unverified_feed_do_not_pass_preview(self):
        page = Mock()
        for url in ('https://www.linkedin.com/checkpoint/', 'https://www.linkedin.com.evil.test/feed/', 'https://www.linkedin.com/feed/'):
            with self.subTest(url=url):
                page.url = url
                page.locator.return_value.count.return_value = 0
                with self.assertRaisesRegex(RuntimeError, 'Dry run'):
                    main.run_dry_run(page, 'post')
