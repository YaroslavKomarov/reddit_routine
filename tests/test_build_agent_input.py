"""Unit-тесты src/build_agent_input.py (временная БД и context/, без сети)."""
import json
import logging
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch
from pathlib import Path

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "DEBUG"),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("test_build_agent_input.check")

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import build_agent_input  # noqa: E402  (path must be adjusted before import)
import db  # noqa: E402

_ENV_KEYS = ("ROUTINE_DB_PATH", "POSTS_BATCH_PATH", "AGENT_INPUT_PATH", "CONTEXT_DIR")


class BuildAgentInputTestCase(unittest.TestCase):
    def setUp(self):
        self._prev_env = {key: os.environ.get(key) for key in _ENV_KEYS}

        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(db_path)
        self.tmp_db_path = db_path
        os.environ["ROUTINE_DB_PATH"] = db_path

        self.tmp_dir = Path(tempfile.mkdtemp())
        self.context_dir = self.tmp_dir / "context"
        (self.context_dir / "rules").mkdir(parents=True)
        (self.context_dir / "product.md").write_text("# Продукт\nреальное описание", encoding="utf-8")
        (self.context_dir / "tone.md").write_text("# Тон\nкоротко и по делу", encoding="utf-8")
        (self.context_dir / "rules" / "SEO.md").write_text("правила SEO", encoding="utf-8")
        os.environ["CONTEXT_DIR"] = str(self.context_dir)

        self.batch_path = self.tmp_dir / "posts_batch.json"
        os.environ["POSTS_BATCH_PATH"] = str(self.batch_path)
        self.input_path = self.tmp_dir / "agent_input.json"
        os.environ["AGENT_INPUT_PATH"] = str(self.input_path)

        logger.debug("[test_build_agent_input.check] temp db=%s context=%s", db_path, self.context_dir)

        self.cfg = {
            "subreddits": [
                {"name": "SEO", "promo_allowed": True, "promo_cooldown_days": 7,
                 "question_posts_allowed": True, "review_post_allowed": False},
                {"name": "bigseo", "promo_allowed": False, "promo_cooldown_days": 14,
                 "question_posts_allowed": False, "review_post_allowed": False},
            ],
            "selection": {"posts_per_sub": [3, 5], "promo_ratio_target": "1 из 6"},
        }

    def tearDown(self):
        for key, value in self._prev_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if os.path.exists(self.tmp_db_path):
            os.remove(self.tmp_db_path)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

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


class TestBuildPromoState(BuildAgentInputTestCase):
    def test_config_promo_disallowed_overrides_empty_history(self):
        logger.debug("[test_build_agent_input.check] promo_allowed=false must win over clean cooldown")
        state = build_agent_input.build_promo_state(self.cfg["subreddits"])
        by_sub = {s["subreddit"]: s for s in state}
        self.assertFalse(by_sub["bigseo"]["promo_allowed_today"])

    def test_fresh_promo_within_cooldown_denies(self):
        logged_at = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        self._insert_promo_at("SEO", logged_at)
        state = build_agent_input.build_promo_state(self.cfg["subreddits"])
        by_sub = {s["subreddit"]: s for s in state}
        self.assertFalse(by_sub["SEO"]["promo_allowed_today"])

    def test_no_history_and_config_allowed_allows(self):
        state = build_agent_input.build_promo_state(self.cfg["subreddits"])
        by_sub = {s["subreddit"]: s for s in state}
        self.assertTrue(by_sub["SEO"]["promo_allowed_today"])
        self.assertIsNone(by_sub["SEO"]["last_promo_days_ago"])

    def test_question_posts_allowed_forwarded_from_config(self):
        state = build_agent_input.build_promo_state(self.cfg["subreddits"])
        by_sub = {s["subreddit"]: s for s in state}
        self.assertTrue(by_sub["SEO"]["question_posts_allowed"])
        self.assertFalse(by_sub["bigseo"]["question_posts_allowed"])


class TestPopQuestion(BuildAgentInputTestCase):
    def test_non_empty_queue_returns_dict_and_marks_used(self):
        db.add_question("вопрос?", target_sub="TechSEO")
        question = build_agent_input.pop_question()
        self.assertEqual(question, {"text": "вопрос?", "target_sub": "TechSEO"})
        self.assertEqual(db.list_unused_questions(), [])

    def test_empty_queue_returns_none(self):
        self.assertIsNone(build_agent_input.pop_question())


class TestReadContextFiles(BuildAgentInputTestCase):
    def test_missing_rules_file_uses_placeholder(self):
        logger.debug("[test_build_agent_input.check] bigseo has no rules file, expecting placeholder")
        _, _, rules = build_agent_input.read_context_files(self.cfg["subreddits"])
        self.assertEqual(rules["SEO"], "правила SEO")
        self.assertEqual(rules["bigseo"], build_agent_input._RULES_PLACEHOLDER)

    def test_missing_product_raises(self):
        (self.context_dir / "product.md").unlink()
        with self.assertRaises(FileNotFoundError):
            build_agent_input.read_context_files(self.cfg["subreddits"])

    def test_empty_product_raises(self):
        (self.context_dir / "product.md").write_text("   \n", encoding="utf-8")
        with self.assertRaises(ValueError):
            build_agent_input.read_context_files(self.cfg["subreddits"])

    def test_product_todo_template_warns_but_does_not_raise(self):
        (self.context_dir / "product.md").write_text(
            "<!-- TODO: заполнить -->\n# Продукт", encoding="utf-8"
        )
        with self.assertLogs("build_agent_input", level="WARNING") as captured:
            product, _, _ = build_agent_input.read_context_files(self.cfg["subreddits"])
        self.assertIn("<!-- TODO", product)
        self.assertTrue(any("незаполненным шаблоном" in line for line in captured.output))


class TestMain(BuildAgentInputTestCase):
    _EXPECTED_KEYS = {
        "date", "product", "tone", "subreddit_rules", "promo_state",
        "question_of_the_day", "posts", "selection_config",
    }

    def _write_batch(self, posts):
        with open(self.batch_path, "w", encoding="utf-8") as f:
            json.dump(posts, f, ensure_ascii=False)

    @patch("build_agent_input.config.load_config")
    def test_main_writes_agent_input_with_all_keys(self, mock_load_config):
        mock_load_config.return_value = self.cfg
        posts = [
            {"id": "p1", "subreddit": "SEO", "title": "t1", "selftext": "", "url": "u1",
             "permalink": "pl1", "score": 1, "num_comments": 0, "created_utc": 1.0},
            {"id": "p2", "subreddit": "bigseo", "title": "t2", "selftext": "s", "url": "u2",
             "permalink": "pl2", "score": 2, "num_comments": 3, "created_utc": 2.0},
        ]
        self._write_batch(posts)
        db.add_question("вопрос дня", target_sub=None)

        code = build_agent_input.main()
        self.assertEqual(code, 0)
        with open(self.input_path, encoding="utf-8") as f:
            agent_input = json.load(f)
        self.assertEqual(set(agent_input.keys()), self._EXPECTED_KEYS)
        self.assertEqual(agent_input["posts"], posts)
        self.assertEqual(agent_input["date"], date.today().isoformat())
        self.assertEqual(agent_input["question_of_the_day"], {"text": "вопрос дня", "target_sub": None})
        self.assertEqual(agent_input["selection_config"],
                         {"posts_per_sub": [3, 5], "promo_ratio_target": "1 из 6"})

    @patch("build_agent_input.config.load_config")
    def test_main_without_batch_file_exit_1(self, mock_load_config):
        mock_load_config.return_value = self.cfg
        code = build_agent_input.main()
        self.assertEqual(code, 1)
        self.assertFalse(self.input_path.exists())


if __name__ == "__main__":
    unittest.main()
