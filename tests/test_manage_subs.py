"""Unit-тесты src/manage_subs.py (временная БД, config.load_config подменяется)."""
import io
import logging
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from pathlib import Path

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "DEBUG"),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("test_manage_subs.check")

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import manage_subs  # noqa: E402  (path must be adjusted before import)
import db  # noqa: E402

_CFG = {
    "subreddits": [
        {"name": "SEO"},
        {"name": "TechSEO"},
        {"name": "bigseo"},
    ],
}


class ManageSubsTestCase(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(path)
        self.tmp_db_path = path
        self._prev_env = os.environ.get("ROUTINE_DB_PATH")
        os.environ["ROUTINE_DB_PATH"] = path
        logger.debug("[test_manage_subs.check] using temp db at %s", path)

        self.load_config_patcher = patch("manage_subs.config.load_config", return_value=_CFG)
        self.load_config_patcher.start()

    def tearDown(self):
        self.load_config_patcher.stop()
        if self._prev_env is None:
            os.environ.pop("ROUTINE_DB_PATH", None)
        else:
            os.environ["ROUTINE_DB_PATH"] = self._prev_env
        if os.path.exists(self.tmp_db_path):
            os.remove(self.tmp_db_path)


class TestPauseResume(ManageSubsTestCase):
    def test_pause_happy_path(self):
        code = manage_subs.main(["pause", "SEO"])
        self.assertEqual(code, 0)
        self.assertEqual(db.get_paused_subs(), {"SEO"})

    def test_repeated_pause_reports_already_paused_exit_0(self):
        manage_subs.main(["pause", "SEO"])
        code = manage_subs.main(["pause", "SEO"])
        self.assertEqual(code, 0)
        self.assertEqual(db.get_paused_subs(), {"SEO"})

    def test_resume_happy_path(self):
        db.pause_sub("SEO")
        code = manage_subs.main(["resume", "SEO"])
        self.assertEqual(code, 0)
        self.assertEqual(db.get_paused_subs(), set())

    def test_resume_not_paused_exit_0(self):
        code = manage_subs.main(["resume", "SEO"])
        self.assertEqual(code, 0)
        self.assertEqual(db.get_paused_subs(), set())

    def test_pause_unknown_subreddit_exit_1_and_lists_known(self):
        stderr = io.StringIO()
        with redirect_stdout(io.StringIO()), patch("sys.stderr", stderr):
            code = manage_subs.main(["pause", "typo_sub"])
        self.assertEqual(code, 1)
        self.assertEqual(db.get_paused_subs(), set())
        self.assertIn("SEO", stderr.getvalue())


class TestList(ManageSubsTestCase):
    def test_list_shows_active_and_paused_statuses(self):
        db.pause_sub("SEO")
        out = io.StringIO()
        with redirect_stdout(out):
            code = manage_subs.main(["list"])
        self.assertEqual(code, 0)
        output = out.getvalue()
        self.assertIn("SEO", output)
        self.assertIn("TechSEO", output)
        self.assertIn("на паузе", output)
        self.assertIn("активен", output)

    def test_list_includes_orphaned_paused_sub(self):
        db.pause_sub("removed_from_config")
        out = io.StringIO()
        with redirect_stdout(out):
            code = manage_subs.main(["list"])
        self.assertEqual(code, 0)
        self.assertIn("removed_from_config", out.getvalue())


if __name__ == "__main__":
    unittest.main()
