"""Unit-тесты src/fetch_posts.py (без реальной сети, requests.get мокается)."""
import json
import logging
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "DEBUG"),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("test_fetch_posts.check")

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import fetch_posts  # noqa: E402  (path must be adjusted before import)
import db  # noqa: E402


def _child(post_id, **overrides):
    data = {
        "id": post_id,
        "title": f"title-{post_id}",
        "selftext": "",
        "url": f"http://example/{post_id}",
        "permalink": f"/r/SEO/comments/{post_id}/",
        "score": 10,
        "num_comments": 1,
        "created_utc": time.time(),
        "stickied": False,
        "removed_by_category": None,
    }
    data.update(overrides)
    return {"data": data}


def _response(status_code=200, json_body=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body or {}
    return resp


class TestFilterAndMapPosts(unittest.TestCase):
    def test_post_on_window_boundary_is_included(self):
        now = time.time()
        child = _child("boundary", created_utc=now - 12 * 3600 + 1)
        result = fetch_posts.filter_and_map_posts([child], "SEO", set(), window_hours=12, min_post_score=0)
        self.assertEqual(len(result), 1)

    def test_post_older_than_window_is_excluded(self):
        now = time.time()
        child = _child("old", created_utc=now - 13 * 3600)
        result = fetch_posts.filter_and_map_posts([child], "SEO", set(), window_hours=12, min_post_score=0)
        self.assertEqual(result, [])

    def test_seen_post_id_excluded(self):
        child = _child("seen1")
        result = fetch_posts.filter_and_map_posts([child], "SEO", {"seen1"}, window_hours=12, min_post_score=0)
        self.assertEqual(result, [])

    def test_stickied_excluded(self):
        child = _child("sticky1", stickied=True)
        result = fetch_posts.filter_and_map_posts([child], "SEO", set(), window_hours=12, min_post_score=0)
        self.assertEqual(result, [])

    def test_removed_by_category_excluded(self):
        child = _child("removed1", removed_by_category="spam")
        result = fetch_posts.filter_and_map_posts([child], "SEO", set(), window_hours=12, min_post_score=0)
        self.assertEqual(result, [])

    def test_score_below_threshold_excluded(self):
        child = _child("lowscore", score=2)
        result = fetch_posts.filter_and_map_posts([child], "SEO", set(), window_hours=12, min_post_score=5)
        self.assertEqual(result, [])

    def test_selftext_truncated_to_2000_chars(self):
        child = _child("long1", selftext="x" * 2500)
        result = fetch_posts.filter_and_map_posts([child], "SEO", set(), window_hours=12, min_post_score=0)
        self.assertEqual(len(result[0]["selftext"]), 2000)

    def test_permalink_gets_reddit_prefix(self):
        child = _child("perm1", permalink="/r/SEO/comments/perm1/")
        result = fetch_posts.filter_and_map_posts([child], "SEO", set(), window_hours=12, min_post_score=0)
        self.assertEqual(result[0]["permalink"], "https://www.reddit.com/r/SEO/comments/perm1/")


class TestFetchSubredditRaw(unittest.TestCase):
    def setUp(self):
        self.sleep_patcher = patch("fetch_posts.time.sleep")
        self.mock_sleep = self.sleep_patcher.start()

    def tearDown(self):
        self.sleep_patcher.stop()

    @patch("fetch_posts.requests.get")
    def test_success_on_first_attempt_no_retries(self, mock_get):
        mock_get.return_value = _response(200, {"ok": True})
        result = fetch_posts.fetch_subreddit_raw("SEO", 50, "ua", "test-token")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(mock_get.call_count, 1)

    @patch("fetch_posts.requests.get")
    def test_calls_oauth_endpoint_with_bearer_token(self, mock_get):
        mock_get.return_value = _response(200, {"ok": True})
        fetch_posts.fetch_subreddit_raw("SEO", 50, "ua", "test-token")
        args, kwargs = mock_get.call_args
        self.assertEqual(args[0], "https://oauth.reddit.com/r/SEO/new?limit=50")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-token")
        self.assertEqual(kwargs["headers"]["User-Agent"], "ua")

    @patch("fetch_posts.requests.get")
    def test_429_then_success_on_second_attempt(self, mock_get):
        mock_get.side_effect = [_response(429), _response(200, {"ok": True})]
        result = fetch_posts.fetch_subreddit_raw("SEO", 50, "ua", "test-token")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(mock_get.call_count, 2)

    @patch("fetch_posts.requests.get")
    def test_persistent_500_exhausts_all_attempts(self, mock_get):
        mock_get.return_value = _response(500)
        result = fetch_posts.fetch_subreddit_raw("SEO", 50, "ua", "test-token")
        self.assertIsNone(result)
        self.assertEqual(mock_get.call_count, 3)

    @patch("fetch_posts.requests.get")
    def test_timeout_exception_is_retried(self, mock_get):
        import requests as requests_module
        mock_get.side_effect = [requests_module.exceptions.Timeout("timed out"), _response(200, {"ok": True})]
        result = fetch_posts.fetch_subreddit_raw("SEO", 50, "ua", "test-token")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(mock_get.call_count, 2)

    @patch("fetch_posts.requests.get")
    def test_non_retryable_status_returns_none_without_retry(self, mock_get):
        mock_get.return_value = _response(404)
        result = fetch_posts.fetch_subreddit_raw("SEO", 50, "ua", "test-token")
        self.assertIsNone(result)
        self.assertEqual(mock_get.call_count, 1)


class TestGetAccessToken(unittest.TestCase):
    def setUp(self):
        self.sleep_patcher = patch("fetch_posts.time.sleep")
        self.mock_sleep = self.sleep_patcher.start()

    def tearDown(self):
        self.sleep_patcher.stop()

    @patch("fetch_posts.requests.post")
    def test_success_returns_token_single_call(self, mock_post):
        mock_post.return_value = _response(200, {"access_token": "tok", "token_type": "bearer"})
        token = fetch_posts.get_access_token("cid", "csec", "ua")
        self.assertEqual(token, "tok")
        self.assertEqual(mock_post.call_count, 1)

    @patch("fetch_posts.requests.post")
    def test_request_uses_basic_auth_and_client_credentials(self, mock_post):
        mock_post.return_value = _response(200, {"access_token": "tok"})
        fetch_posts.get_access_token("cid", "csec", "honest-ua")
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], "https://www.reddit.com/api/v1/access_token")
        self.assertEqual(kwargs["auth"], ("cid", "csec"))
        self.assertEqual(kwargs["data"], {"grant_type": "client_credentials"})
        self.assertEqual(kwargs["headers"]["User-Agent"], "honest-ua")

    @patch("fetch_posts.requests.post")
    def test_500_then_success_on_second_attempt(self, mock_post):
        mock_post.side_effect = [_response(500), _response(200, {"access_token": "tok"})]
        token = fetch_posts.get_access_token("cid", "csec", "ua")
        self.assertEqual(token, "tok")
        self.assertEqual(mock_post.call_count, 2)

    @patch("fetch_posts.requests.post")
    def test_persistent_500_exhausts_all_attempts(self, mock_post):
        mock_post.return_value = _response(500)
        token = fetch_posts.get_access_token("cid", "csec", "ua")
        self.assertIsNone(token)
        self.assertEqual(mock_post.call_count, 3)

    @patch("fetch_posts.requests.post")
    def test_401_returns_none_without_retry(self, mock_post):
        mock_post.return_value = _response(401)
        token = fetch_posts.get_access_token("cid", "csec", "ua")
        self.assertIsNone(token)
        self.assertEqual(mock_post.call_count, 1)

    @patch("fetch_posts.requests.post")
    def test_200_without_access_token_key_returns_none(self, mock_post):
        mock_post.return_value = _response(200, {"error": "unexpected"})
        token = fetch_posts.get_access_token("cid", "csec", "ua")
        self.assertIsNone(token)

    @patch("fetch_posts.requests.post")
    def test_timeout_exception_is_retried(self, mock_post):
        import requests as requests_module
        mock_post.side_effect = [requests_module.exceptions.Timeout("timed out"),
                                 _response(200, {"access_token": "tok"})]
        token = fetch_posts.get_access_token("cid", "csec", "ua")
        self.assertEqual(token, "tok")
        self.assertEqual(mock_post.call_count, 2)


_REDDIT_ENV_KEYS = ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET")


class TestMain(unittest.TestCase):
    def setUp(self):
        # Непустые тестовые значения заодно не дают load_dotenv() в main()
        # подтянуть реальный .env владельца на dev-машине.
        self._prev_reddit_env = {key: os.environ.get(key) for key in _REDDIT_ENV_KEYS}
        os.environ["REDDIT_CLIENT_ID"] = "test-id"
        os.environ["REDDIT_CLIENT_SECRET"] = "test-secret"

        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(db_path)
        self.tmp_db_path = db_path
        self._prev_db_env = os.environ.get("ROUTINE_DB_PATH")
        os.environ["ROUTINE_DB_PATH"] = db_path

        fd, batch_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(batch_path)
        self.tmp_batch_path = batch_path
        self._prev_batch_env = os.environ.get("POSTS_BATCH_PATH")
        os.environ["POSTS_BATCH_PATH"] = batch_path

        self.sleep_patcher = patch("fetch_posts.time.sleep")
        self.mock_sleep = self.sleep_patcher.start()

        self.cfg = {
            "subreddits": [
                {"name": "SEO"},
                {"name": "TechSEO"},
                {"name": "bigseo"},
            ],
            "fetch": {
                "window_hours": 12,
                "posts_per_sub_limit": 50,
                "min_post_score": 0,
                "user_agent": "test-agent",
            },
        }

    def tearDown(self):
        self.sleep_patcher.stop()
        for key, value in self._prev_reddit_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if self._prev_db_env is None:
            os.environ.pop("ROUTINE_DB_PATH", None)
        else:
            os.environ["ROUTINE_DB_PATH"] = self._prev_db_env
        if os.path.exists(self.tmp_db_path):
            os.remove(self.tmp_db_path)

        if self._prev_batch_env is None:
            os.environ.pop("POSTS_BATCH_PATH", None)
        else:
            os.environ["POSTS_BATCH_PATH"] = self._prev_batch_env
        if os.path.exists(self.tmp_batch_path):
            os.remove(self.tmp_batch_path)

    @patch("fetch_posts.get_access_token", return_value="test-token")
    @patch("fetch_posts.config.load_config")
    @patch("fetch_posts.fetch_subreddit_raw")
    def test_all_subs_succeed_writes_batch_and_exit_0(self, mock_fetch, mock_load_config, mock_token):
        mock_load_config.return_value = self.cfg
        mock_fetch.side_effect = [
            {"data": {"children": [_child("p1")]}},
            {"data": {"children": [_child("p2")]}},
            {"data": {"children": [_child("p3")]}},
        ]
        code = fetch_posts.main()
        self.assertEqual(code, 0)
        with open(self.tmp_batch_path, encoding="utf-8") as f:
            batch = json.load(f)
        ids = {p["id"] for p in batch}
        self.assertEqual(ids, {"p1", "p2", "p3"})

    @patch("fetch_posts.get_access_token", return_value="test-token")
    @patch("fetch_posts.config.load_config")
    @patch("fetch_posts.fetch_subreddit_raw")
    def test_partial_failure_still_exit_0_and_excludes_failed_sub(self, mock_fetch, mock_load_config, mock_token):
        mock_load_config.return_value = self.cfg
        mock_fetch.side_effect = [
            {"data": {"children": [_child("p1")]}},
            None,
            {"data": {"children": [_child("p3")]}},
        ]
        code = fetch_posts.main()
        self.assertEqual(code, 0)
        with open(self.tmp_batch_path, encoding="utf-8") as f:
            batch = json.load(f)
        ids = {p["id"] for p in batch}
        self.assertEqual(ids, {"p1", "p3"})

    @patch("fetch_posts.get_access_token", return_value="test-token")
    @patch("fetch_posts.config.load_config")
    @patch("fetch_posts.fetch_subreddit_raw")
    def test_all_subs_fail_exit_1_and_no_file_written(self, mock_fetch, mock_load_config, mock_token):
        mock_load_config.return_value = self.cfg
        mock_fetch.return_value = None
        code = fetch_posts.main()
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self.tmp_batch_path))

    @patch("fetch_posts.get_access_token", return_value=None)
    @patch("fetch_posts.config.load_config")
    @patch("fetch_posts.fetch_subreddit_raw")
    def test_token_failure_exit_1_no_batch_no_fetch(self, mock_fetch, mock_load_config, mock_token):
        mock_load_config.return_value = self.cfg
        code = fetch_posts.main()
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self.tmp_batch_path))
        mock_fetch.assert_not_called()

    @patch("fetch_posts.requests.post")
    def test_missing_client_id_exit_1_without_token_request(self, mock_post):
        # Пустая строка = отсутствует: при удалении переменной load_dotenv()
        # подтянул бы реальный .env владельца на dev-машине.
        os.environ["REDDIT_CLIENT_ID"] = ""
        code = fetch_posts.main()
        self.assertEqual(code, 1)
        mock_post.assert_not_called()

    @patch("fetch_posts.requests.post")
    def test_missing_client_secret_exit_1_without_token_request(self, mock_post):
        os.environ["REDDIT_CLIENT_SECRET"] = ""
        code = fetch_posts.main()
        self.assertEqual(code, 1)
        mock_post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
