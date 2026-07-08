"""Unit-тесты слоя данных src/db.py (временная БД, без сети)."""
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "DEBUG"),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("test_db.check")

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import db  # noqa: E402  (path must be adjusted before import)


class DbTestCase(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(path)
        self.tmp_db_path = path
        self._prev_env = os.environ.get("ROUTINE_DB_PATH")
        os.environ["ROUTINE_DB_PATH"] = path
        logger.debug("[test_db.check] using temp db at %s", path)

    def tearDown(self):
        if self._prev_env is None:
            os.environ.pop("ROUTINE_DB_PATH", None)
        else:
            os.environ["ROUTINE_DB_PATH"] = self._prev_env
        if os.path.exists(self.tmp_db_path):
            os.remove(self.tmp_db_path)

    @staticmethod
    def _insert_promo_at(subreddit, logged_at, promo_type="comment_promo"):
        conn = db.connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO promo_history (subreddit, type, post_url, logged_at) VALUES (?, ?, ?, ?)",
                    (subreddit, promo_type, None, logged_at),
                )
        finally:
            conn.close()


class TestMigrations(DbTestCase):
    def test_fresh_db_has_all_tables(self):
        logger.debug("[test_db.check] verifying fresh db contains all tables")
        conn = db.connect()
        try:
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            names = {row["name"] for row in rows}
        finally:
            conn.close()
        for table in ("seen_posts", "promo_history", "question_queue", "run_log"):
            self.assertIn(table, names)

    def test_ensure_schema_is_idempotent(self):
        logger.debug("[test_db.check] calling ensure_schema twice")
        conn = db.connect()
        try:
            db.ensure_schema(conn)
        finally:
            conn.close()


class TestSeenPosts(DbTestCase):
    def test_mark_and_get_round_trip(self):
        posts = [
            {"post_id": "abc1", "subreddit": "SEO", "title": "t1", "url": "http://x/1"},
            {"post_id": "abc2", "subreddit": "SEO", "title": "t2", "url": "http://x/2", "was_promo": True},
        ]
        inserted = db.mark_posts_seen(posts)
        self.assertEqual(inserted, 2)
        self.assertEqual(db.get_seen_post_ids(), {"abc1", "abc2"})

    def test_duplicate_insert_not_duplicated_or_raised(self):
        post = {"post_id": "dup1", "subreddit": "SEO", "title": "t", "url": "http://x"}
        first = db.mark_posts_seen([post])
        second = db.mark_posts_seen([post])
        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual(db.get_seen_post_ids(), {"dup1"})


class TestPromoCooldowns(DbTestCase):
    def test_empty_history_allows_promo(self):
        self.assertIsNone(db.last_promo_days_ago("SEO"))
        self.assertTrue(db.promo_allowed_today("SEO", 7))

    def test_exactly_cooldown_days_ago_allows(self):
        logged_at = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        self._insert_promo_at("SEO", logged_at)
        self.assertTrue(db.promo_allowed_today("SEO", 7))

    def test_one_day_short_of_cooldown_denies(self):
        logged_at = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat()
        self._insert_promo_at("SEO", logged_at)
        self.assertFalse(db.promo_allowed_today("SEO", 7))

    def test_log_promo_invalid_type_raises(self):
        with self.assertRaises(ValueError):
            db.log_promo("SEO", "not_a_valid_type")

    def test_get_promo_state_multiple_subs(self):
        logged_at = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        self._insert_promo_at("SEO", logged_at)
        state = db.get_promo_state([("SEO", 7), ("bigseo", 14)])
        by_sub = {s["subreddit"]: s for s in state}
        self.assertEqual(by_sub["SEO"]["last_promo_days_ago"], 3)
        self.assertFalse(by_sub["SEO"]["promo_allowed_today"])
        self.assertIsNone(by_sub["bigseo"]["last_promo_days_ago"])
        self.assertTrue(by_sub["bigseo"]["promo_allowed_today"])


class TestQuestionQueue(DbTestCase):
    def test_pop_oldest_is_fifo(self):
        first_id = db.add_question("first?")
        second_id = db.add_question("second?", target_sub="TechSEO")
        popped_first = db.pop_oldest_question()
        popped_second = db.pop_oldest_question()
        self.assertEqual(popped_first["id"], first_id)
        self.assertEqual(popped_second["id"], second_id)
        self.assertEqual(popped_second["target_sub"], "TechSEO")

    def test_pop_marks_used_and_is_not_returned_again(self):
        db.add_question("only one")
        first = db.pop_oldest_question()
        self.assertIsNotNone(first)
        self.assertIsNone(db.pop_oldest_question())
        self.assertEqual(db.list_unused_questions(), [])

    def test_pop_empty_queue_returns_none(self):
        self.assertIsNone(db.pop_oldest_question())

    def test_queue_stats_counts_unused_and_used(self):
        db.add_question("q1")
        db.add_question("q2")
        db.pop_oldest_question()
        self.assertEqual(db.queue_stats(), {"unused": 1, "used": 1})

    def test_add_question_empty_text_raises(self):
        with self.assertRaises(ValueError):
            db.add_question("   ")


class TestRunLog(DbTestCase):
    def test_log_run_writes_row(self):
        db.log_run("ok", posts_fetched=10, posts_suggested=4, cost_usd=0.12)
        conn = db.connect()
        try:
            row = conn.execute("SELECT * FROM run_log ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["posts_fetched"], 10)
        self.assertEqual(row["posts_suggested"], 4)
        self.assertAlmostEqual(row["cost_usd"], 0.12)

    def test_log_run_invalid_status_raises(self):
        with self.assertRaises(ValueError):
            db.log_run("not_a_status")

    def test_cli_log_run_ok_exit_code_and_row_written(self):
        env = {**os.environ, "ROUTINE_DB_PATH": self.tmp_db_path}
        result = subprocess.run(
            [sys.executable, str(SRC_DIR / "db.py"), "--log-run", "ok"],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        conn = db.connect()
        try:
            count = conn.execute("SELECT COUNT(*) AS n FROM run_log WHERE status = 'ok'").fetchone()["n"]
        finally:
            conn.close()
        self.assertGreaterEqual(count, 1)

    def test_recent_runs_empty_table_returns_empty_list(self):
        self.assertEqual(db.recent_runs(), [])

    def test_recent_runs_order_and_limit(self):
        db.log_run("ok", posts_fetched=1, posts_suggested=1, cost_usd=0.01)
        db.log_run("fetch_failed", error="boom")
        db.log_run("ok", posts_fetched=2, posts_suggested=2, cost_usd=0.02)
        runs = db.recent_runs(limit=2)
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0]["status"], "ok")
        self.assertEqual(runs[0]["posts_fetched"], 2)
        self.assertEqual(runs[1]["status"], "fetch_failed")
        self.assertEqual(runs[1]["error"], "boom")

    def test_recent_runs_fields_match_what_was_written(self):
        db.log_run("ok", posts_fetched=7, posts_suggested=3, cost_usd=0.15)
        run = db.recent_runs(limit=1)[0]
        self.assertEqual(run["status"], "ok")
        self.assertEqual(run["posts_fetched"], 7)
        self.assertEqual(run["posts_suggested"], 3)
        self.assertAlmostEqual(run["cost_usd"], 0.15)

    def test_cli_show_runs_exit_code(self):
        env = {**os.environ, "ROUTINE_DB_PATH": self.tmp_db_path}
        result = subprocess.run(
            [sys.executable, str(SRC_DIR / "db.py"), "--show-runs", "5"],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cli_log_run_still_works_alongside_show_runs(self):
        env = {**os.environ, "ROUTINE_DB_PATH": self.tmp_db_path}
        result = subprocess.run(
            [sys.executable, str(SRC_DIR / "db.py"), "--log-run", "ok", "--cost-usd", "0.08"],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cli_both_flags_together_is_parser_error(self):
        env = {**os.environ, "ROUTINE_DB_PATH": self.tmp_db_path}
        result = subprocess.run(
            [sys.executable, str(SRC_DIR / "db.py"), "--log-run", "ok", "--show-runs", "5"],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)

    def test_cli_neither_flag_is_parser_error(self):
        env = {**os.environ, "ROUTINE_DB_PATH": self.tmp_db_path}
        result = subprocess.run(
            [sys.executable, str(SRC_DIR / "db.py")],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
