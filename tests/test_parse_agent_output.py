"""Unit-тесты src/parse_agent_output.py (временные файлы, retry-сабпроцесс мокается)."""
import json
import logging
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "DEBUG"),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("test_parse_agent_output.check")

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import parse_agent_output  # noqa: E402  (path must be adjusted before import)
import db  # noqa: E402

_ENV_KEYS = ("ROUTINE_DB_PATH", "AGENT_RAW_PATH", "DIGEST_PATH", "POSTS_BATCH_PATH", "AGENT_INPUT_PATH")

_PROMO_STATE = [
    {"subreddit": "SEO", "last_promo_days_ago": None,
     "promo_allowed_today": True, "question_posts_allowed": True},
    {"subreddit": "bigseo", "last_promo_days_ago": 3,
     "promo_allowed_today": False, "question_posts_allowed": False},
]


def _valid_post(post_id="p1", **overrides):
    post = {
        "post_id": post_id,
        "post_title": f"title-{post_id}",
        "post_url": f"https://reddit.com/{post_id}",
        "comment_draft": "черновик",
        "is_promo": False,
        "why": "релевантно",
    }
    post.update(overrides)
    return post


def _valid_digest(**overrides):
    digest = {
        "question_post": None,
        "suggestions": [{"subreddit": "SEO", "posts": [_valid_post("p1"), _valid_post("p2", is_promo=True)]}],
        "skipped_subs": [{"subreddit": "bigseo", "reason": "нет релевантных постов"}],
    }
    digest.update(overrides)
    return digest


class TestStripJsonFences(unittest.TestCase):
    def test_clean_json_unchanged(self):
        text = '{"a": 1}'
        self.assertEqual(parse_agent_output.strip_json_fences(text), text)

    def test_json_fence_stripped(self):
        text = '```json\n{"a": 1}\n```'
        self.assertEqual(parse_agent_output.strip_json_fences(text), '{"a": 1}')

    def test_bare_fence_stripped(self):
        text = '```\n{"a": 1}\n```'
        self.assertEqual(parse_agent_output.strip_json_fences(text), '{"a": 1}')

    def test_chatter_around_json_stripped(self):
        text = 'Вот ответ:\n{"a": 1}\nНадеюсь, помог!'
        self.assertEqual(parse_agent_output.strip_json_fences(text), '{"a": 1}')


class TestExtractResult(unittest.TestCase):
    def test_valid_envelope_returns_result_and_cost(self):
        result, cost = parse_agent_output.extract_result({"result": "{}", "total_cost_usd": 0.5})
        self.assertEqual(result, "{}")
        self.assertEqual(cost, 0.5)

    def test_envelope_without_result_raises(self):
        with self.assertRaises(ValueError):
            parse_agent_output.extract_result({"total_cost_usd": 0.5})


class TestValidateDigest(unittest.TestCase):
    BATCH_IDS = {"p1", "p2"}

    def test_valid_digest_has_no_errors(self):
        errors = parse_agent_output.validate_digest(_valid_digest(), self.BATCH_IDS, _PROMO_STATE)
        self.assertEqual(errors, [])

    def test_is_promo_as_int_is_error(self):
        digest = _valid_digest(
            suggestions=[{"subreddit": "SEO", "posts": [_valid_post("p1", is_promo=1)]}]
        )
        errors = parse_agent_output.validate_digest(digest, self.BATCH_IDS, _PROMO_STATE)
        self.assertTrue(any("is_promo" in e for e in errors))

    def test_unknown_post_id_is_error(self):
        digest = _valid_digest(
            suggestions=[{"subreddit": "SEO", "posts": [_valid_post("ghost")]}]
        )
        errors = parse_agent_output.validate_digest(digest, self.BATCH_IDS, _PROMO_STATE)
        self.assertTrue(any("ghost" in e for e in errors))

    def test_null_question_post_is_valid(self):
        digest = _valid_digest(question_post=None)
        errors = parse_agent_output.validate_digest(digest, self.BATCH_IDS, _PROMO_STATE)
        self.assertEqual(errors, [])

    def test_missing_comment_draft_is_error(self):
        post = _valid_post("p1")
        del post["comment_draft"]
        digest = _valid_digest(suggestions=[{"subreddit": "SEO", "posts": [post]}])
        errors = parse_agent_output.validate_digest(digest, self.BATCH_IDS, _PROMO_STATE)
        self.assertTrue(any("comment_draft" in e for e in errors))

    def test_promo_in_disallowed_sub_warns_but_no_error(self):
        digest = _valid_digest(
            suggestions=[{"subreddit": "bigseo", "posts": [_valid_post("p1", is_promo=True)]}]
        )
        with self.assertLogs("parse_agent_output", level="WARNING") as captured:
            errors = parse_agent_output.validate_digest(digest, self.BATCH_IDS, _PROMO_STATE)
        self.assertEqual(errors, [])
        self.assertTrue(any("is_promo=true" in line for line in captured.output))

    def test_question_post_in_disallowed_sub_warns_but_no_error(self):
        digest = _valid_digest(
            question_post={"subreddit": "bigseo", "title": "t", "body": "b", "notes": "n"}
        )
        with self.assertLogs("parse_agent_output", level="WARNING") as captured:
            errors = parse_agent_output.validate_digest(digest, self.BATCH_IDS, _PROMO_STATE)
        self.assertEqual(errors, [])
        self.assertTrue(any("question_posts_allowed=false" in line for line in captured.output))

    def test_question_post_in_sub_outside_promo_state_warns_but_no_error(self):
        digest = _valid_digest(
            question_post={"subreddit": "SEOTools", "title": "t", "body": "b", "notes": "n"}
        )
        with self.assertLogs("parse_agent_output", level="WARNING") as captured:
            errors = parse_agent_output.validate_digest(digest, self.BATCH_IDS, _PROMO_STATE)
        self.assertEqual(errors, [])
        self.assertTrue(any("вне promo_state" in line for line in captured.output))


class TestMain(unittest.TestCase):
    def setUp(self):
        self._prev_env = {key: os.environ.get(key) for key in _ENV_KEYS}

        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(db_path)
        self.tmp_db_path = db_path
        os.environ["ROUTINE_DB_PATH"] = db_path

        self.tmp_dir = Path(tempfile.mkdtemp())
        self.raw_path = self.tmp_dir / "agent_raw.json"
        self.digest_path = self.tmp_dir / "digest.json"
        self.batch_path = self.tmp_dir / "posts_batch.json"
        self.input_path = self.tmp_dir / "agent_input.json"
        os.environ["AGENT_RAW_PATH"] = str(self.raw_path)
        os.environ["DIGEST_PATH"] = str(self.digest_path)
        os.environ["POSTS_BATCH_PATH"] = str(self.batch_path)
        os.environ["AGENT_INPUT_PATH"] = str(self.input_path)
        logger.debug("[test_parse_agent_output.check] temp dir=%s db=%s", self.tmp_dir, db_path)

        batch = [
            {"id": "p1", "subreddit": "SEO", "title": "t1", "selftext": "", "url": "u1",
             "permalink": "pl1", "score": 1, "num_comments": 0, "created_utc": 1.0},
            {"id": "p2", "subreddit": "SEO", "title": "t2", "selftext": "", "url": "u2",
             "permalink": "pl2", "score": 2, "num_comments": 0, "created_utc": 2.0},
            {"id": "p3", "subreddit": "bigseo", "title": "t3", "selftext": "", "url": "u3",
             "permalink": "pl3", "score": 3, "num_comments": 0, "created_utc": 3.0},
        ]
        self.batch_path.write_text(json.dumps(batch), encoding="utf-8")
        self.input_path.write_text(json.dumps({"promo_state": _PROMO_STATE}), encoding="utf-8")

    def tearDown(self):
        for key, value in self._prev_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if os.path.exists(self.tmp_db_path):
            os.remove(self.tmp_db_path)
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _write_raw(self, result_text, cost=0.33):
        self.raw_path.write_text(
            json.dumps({"result": result_text, "total_cost_usd": cost}), encoding="utf-8"
        )

    def test_valid_response_writes_digest_and_seen_posts(self):
        self._write_raw("```json\n" + json.dumps(_valid_digest()) + "\n```")
        code = parse_agent_output.main()
        self.assertEqual(code, 0)

        with open(self.digest_path, encoding="utf-8") as f:
            out = json.load(f)
        self.assertEqual(out["stats"], {"cost_usd": 0.33, "posts_fetched": 3, "posts_suggested": 2})
        self.assertEqual(out["digest"]["skipped_subs"][0]["subreddit"], "bigseo")

        self.assertEqual(db.get_seen_post_ids(), {"p1", "p2"})
        conn = db.connect()
        try:
            rows = conn.execute("SELECT post_id, was_promo FROM seen_posts").fetchall()
        finally:
            conn.close()
        promo_by_id = {row["post_id"]: row["was_promo"] for row in rows}
        self.assertEqual(promo_by_id, {"p1": 0, "p2": 1})

    @patch("parse_agent_output.subprocess.run")
    def test_broken_json_with_successful_retry_exit_0(self, mock_run):
        self._write_raw("это вообще не JSON")

        def fake_rerun(command, env):
            logger.debug("[test_parse_agent_output.check] fake retry rewrites %s", self.raw_path)
            self._write_raw(json.dumps(_valid_digest()), cost=0.44)
            return MagicMock(returncode=0)

        mock_run.side_effect = fake_rerun
        code = parse_agent_output.main()
        self.assertEqual(code, 0)
        self.assertEqual(mock_run.call_count, 1)
        env = mock_run.call_args.kwargs["env"]
        self.assertIn("AGENT_RETRY_NOTE", env)
        with open(self.digest_path, encoding="utf-8") as f:
            out = json.load(f)
        self.assertEqual(out["stats"]["cost_usd"], 0.44)

    @patch("parse_agent_output.subprocess.run")
    def test_broken_json_with_broken_retry_exit_1(self, mock_run):
        self._write_raw("это вообще не JSON")
        mock_run.return_value = MagicMock(returncode=0)  # retry «отработал», но файл остался битым
        code = parse_agent_output.main()
        self.assertEqual(code, 1)
        self.assertEqual(mock_run.call_count, 1)
        self.assertFalse(self.digest_path.exists())
        self.assertEqual(db.get_seen_post_ids(), set())


if __name__ == "__main__":
    unittest.main()
