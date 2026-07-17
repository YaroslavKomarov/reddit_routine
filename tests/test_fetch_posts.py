"""Unit-тесты src/fetch_posts.py (без реальной сети, requests.get мокается)."""
import json
import logging
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
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


_FEED_HEADER = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:media="http://search.yahoo.com/mrss/">\n'
    '  <link rel="self" href="https://www.reddit.com/r/SEO/new/.rss"/>\n'
    '  <link rel="alternate" href="https://www.reddit.com/r/SEO/new/"/>\n'
)
_FEED_FOOTER = "</feed>\n"


def _entry_xml(
    post_id="1uqy3k2",
    title="What would an SEO/GEO tool need to have",
    content=(
        '&lt;!-- SC_OFF --&gt;&lt;div class="md"&gt;&lt;p&gt;текст поста с &amp;amp; сущностью&lt;/p&gt;'
        '&lt;!-- SC_ON --&gt; submitted by &lt;a href="…"&gt;/u/Ideasaas&lt;/a&gt; '
        '&lt;a href="…"&gt;[link]&lt;/a&gt; &lt;a href="…"&gt;[comments]&lt;/a&gt;'
    ),
    published="2026-07-08T16:41:53+00:00",
    updated="2026-07-08T16:41:53+00:00",
    permalink="https://www.reddit.com/r/SEO/comments/1uqy3k2/some_title/",
    extra_links="",
):
    published_tag = f"<published>{published}</published>" if published else ""
    updated_tag = f"<updated>{updated}</updated>" if updated else ""
    id_tag = f"<id>t3_{post_id}</id>" if post_id else ""
    return f"""  <entry>
    <author><name>/u/Ideasaas</name></author>
    <category term="SEO" label="r/SEO"/>
    <content type="html">{content}</content>
    {id_tag}
    <link href="{permalink}" />
    {extra_links}
    {updated_tag}
    {published_tag}
    <title>{title}</title>
  </entry>
"""


def _feed_xml(*entries):
    return _FEED_HEADER + "".join(entries) + _FEED_FOOTER


def _response(status_code=200, text="", headers=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.headers = headers or {}
    return resp


class TestParseFeedEntries(unittest.TestCase):
    def test_id_prefix_t3_is_stripped(self):
        xml_text = _feed_xml(_entry_xml(post_id="abc123"))
        posts = fetch_posts.parse_feed_entries(xml_text, "SEO")
        self.assertEqual(posts[0]["id"], "abc123")

    def test_created_utc_parsed_from_published(self):
        xml_text = _feed_xml(_entry_xml(published="2026-07-08T16:41:53+00:00"))
        posts = fetch_posts.parse_feed_entries(xml_text, "SEO")
        expected = 1783528913.0  # 2026-07-08T16:41:53+00:00 as epoch
        self.assertAlmostEqual(posts[0]["created_utc"], expected, delta=1)

    def test_created_utc_falls_back_to_updated_when_published_missing(self):
        xml_text = _feed_xml(_entry_xml(published="", updated="2026-07-08T10:00:00+00:00"))
        posts = fetch_posts.parse_feed_entries(xml_text, "SEO")
        self.assertEqual(len(posts), 1)
        self.assertGreater(posts[0]["created_utc"], 0)

    def test_permalink_and_url_use_href_without_prefix(self):
        href = "https://www.reddit.com/r/SEO/comments/perm1/some_title/"
        xml_text = _feed_xml(_entry_xml(permalink=href))
        posts = fetch_posts.parse_feed_entries(xml_text, "SEO")
        self.assertEqual(posts[0]["permalink"], href)
        self.assertEqual(posts[0]["url"], href)

    def test_selftext_tags_stripped_entities_decoded_and_tail_removed(self):
        xml_text = _feed_xml(_entry_xml())
        posts = fetch_posts.parse_feed_entries(xml_text, "SEO")
        selftext = posts[0]["selftext"]
        self.assertIn("текст поста с & сущностью", selftext)
        self.assertNotIn("submitted by", selftext)
        self.assertNotIn("[link]", selftext)
        self.assertNotIn("[comments]", selftext)

    def test_selftext_truncated_to_2000_chars(self):
        long_body = "x" * 2500
        content = f'&lt;div class="md"&gt;&lt;p&gt;{long_body}&lt;/p&gt;&lt;/div&gt; submitted by /u/x [link] [comments]'
        xml_text = _feed_xml(_entry_xml(content=content))
        posts = fetch_posts.parse_feed_entries(xml_text, "SEO")
        self.assertEqual(len(posts[0]["selftext"]), 2000)

    def test_link_post_without_md_div_yields_empty_selftext(self):
        content = 'submitted by &lt;a href="…"&gt;/u/x&lt;/a&gt; &lt;a href="…"&gt;[link]&lt;/a&gt; &lt;a href="…"&gt;[comments]&lt;/a&gt;'
        xml_text = _feed_xml(_entry_xml(content=content))
        posts = fetch_posts.parse_feed_entries(xml_text, "SEO")
        self.assertEqual(posts[0]["selftext"], "")

    def test_score_and_num_comments_are_always_zero(self):
        xml_text = _feed_xml(_entry_xml())
        posts = fetch_posts.parse_feed_entries(xml_text, "SEO")
        self.assertEqual(posts[0]["score"], 0)
        self.assertEqual(posts[0]["num_comments"], 0)

    def test_entry_without_id_is_skipped(self):
        xml_text = _feed_xml(_entry_xml(post_id=""), _entry_xml(post_id="ok1"))
        posts = fetch_posts.parse_feed_entries(xml_text, "SEO")
        self.assertEqual([p["id"] for p in posts], ["ok1"])

    def test_multiple_entries_all_parsed(self):
        xml_text = _feed_xml(_entry_xml(post_id="p1"), _entry_xml(post_id="p2"), _entry_xml(post_id="p3"))
        posts = fetch_posts.parse_feed_entries(xml_text, "SEO")
        self.assertEqual([p["id"] for p in posts], ["p1", "p2", "p3"])

    def test_invalid_xml_raises_parse_error(self):
        with self.assertRaises(fetch_posts.ET.ParseError):
            fetch_posts.parse_feed_entries("<feed><entry><unclosed></feed>", "SEO")


class TestFilterAndMapPosts(unittest.TestCase):
    def _post(self, post_id, **overrides):
        data = {
            "id": post_id,
            "title": f"title-{post_id}",
            "selftext": "",
            "url": f"https://www.reddit.com/r/SEO/comments/{post_id}/",
            "permalink": f"https://www.reddit.com/r/SEO/comments/{post_id}/",
            "score": 0,
            "num_comments": 0,
            "created_utc": time.time(),
        }
        data.update(overrides)
        return data

    def test_post_on_window_boundary_is_included(self):
        now = time.time()
        post = self._post("boundary", created_utc=now - 12 * 3600 + 1)
        result = fetch_posts.filter_and_map_posts([post], "SEO", set(), window_hours=12)
        self.assertEqual(len(result), 1)

    def test_post_older_than_window_is_excluded(self):
        now = time.time()
        post = self._post("old", created_utc=now - 13 * 3600)
        result = fetch_posts.filter_and_map_posts([post], "SEO", set(), window_hours=12)
        self.assertEqual(result, [])

    def test_seen_post_id_excluded(self):
        post = self._post("seen1")
        result = fetch_posts.filter_and_map_posts([post], "SEO", {"seen1"}, window_hours=12)
        self.assertEqual(result, [])

    def test_kept_post_carries_subreddit_and_zero_score_fields(self):
        post = self._post("keep1")
        result = fetch_posts.filter_and_map_posts([post], "SEO", set(), window_hours=12)
        self.assertEqual(result[0]["subreddit"], "SEO")
        self.assertEqual(result[0]["score"], 0)
        self.assertEqual(result[0]["num_comments"], 0)


class TestFetchSubredditFeed(unittest.TestCase):
    def setUp(self):
        self.sleep_patcher = patch("fetch_posts.time.sleep")
        self.mock_sleep = self.sleep_patcher.start()

    def tearDown(self):
        self.sleep_patcher.stop()

    @patch("fetch_posts.requests.get")
    def test_success_returns_text_and_rate_info(self, mock_get):
        mock_get.return_value = _response(
            200, "<feed></feed>", headers={"x-ratelimit-remaining": "0.0", "x-ratelimit-reset": "31"}
        )
        result = fetch_posts.fetch_subreddit_feed("SEO", 50, "ua")
        self.assertIsNotNone(result)
        text, rate_info = result
        self.assertEqual(text, "<feed></feed>")
        self.assertEqual(rate_info, {"remaining": "0.0", "reset": "31"})

    @patch("fetch_posts.requests.get")
    def test_request_uses_correct_url_and_no_authorization_header(self, mock_get):
        mock_get.return_value = _response(200, "<feed></feed>")
        fetch_posts.fetch_subreddit_feed("SEO", 50, "ua")
        args, kwargs = mock_get.call_args
        self.assertEqual(args[0], "https://www.reddit.com/r/SEO/new/.rss?limit=50")
        self.assertNotIn("Authorization", kwargs["headers"])
        self.assertEqual(kwargs["headers"]["User-Agent"], "ua")

    @patch("fetch_posts.requests.get")
    def test_429_then_success_on_second_attempt(self, mock_get):
        mock_get.side_effect = [_response(429), _response(200, "<feed></feed>")]
        result = fetch_posts.fetch_subreddit_feed("SEO", 50, "ua")
        self.assertIsNotNone(result)
        self.assertEqual(mock_get.call_count, 2)

    @patch("fetch_posts.requests.get")
    def test_persistent_500_exhausts_all_attempts(self, mock_get):
        mock_get.return_value = _response(500)
        result = fetch_posts.fetch_subreddit_feed("SEO", 50, "ua")
        self.assertIsNone(result)
        self.assertEqual(mock_get.call_count, 3)

    @patch("fetch_posts.requests.get")
    def test_timeout_exception_is_retried(self, mock_get):
        import requests as requests_module
        mock_get.side_effect = [requests_module.exceptions.Timeout("timed out"), _response(200, "<feed></feed>")]
        result = fetch_posts.fetch_subreddit_feed("SEO", 50, "ua")
        self.assertIsNotNone(result)
        self.assertEqual(mock_get.call_count, 2)

    @patch("fetch_posts.requests.get")
    def test_non_retryable_status_returns_none_without_retry(self, mock_get):
        mock_get.return_value = _response(404)
        result = fetch_posts.fetch_subreddit_feed("SEO", 50, "ua")
        self.assertIsNone(result)
        self.assertEqual(mock_get.call_count, 1)


class TestSleepForRateLimit(unittest.TestCase):
    @patch("fetch_posts.time.sleep")
    def test_remaining_zero_sleeps_reset_plus_one(self, mock_sleep):
        fetch_posts._sleep_for_rate_limit({"remaining": "0.0", "reset": "31"})
        mock_sleep.assert_called_once_with(32)

    @patch("fetch_posts.time.sleep")
    def test_missing_headers_uses_conservative_sleep(self, mock_sleep):
        fetch_posts._sleep_for_rate_limit(None)
        mock_sleep.assert_called_once_with(30)

    @patch("fetch_posts.time.sleep")
    def test_remaining_available_uses_base_pause(self, mock_sleep):
        fetch_posts._sleep_for_rate_limit({"remaining": "5.0", "reset": "31"})
        mock_sleep.assert_called_once_with(fetch_posts._SUB_PAUSE_SECONDS)

    @patch("fetch_posts.time.sleep")
    def test_delay_capped_at_sixty(self, mock_sleep):
        fetch_posts._sleep_for_rate_limit({"remaining": "0.0", "reset": "600"})
        mock_sleep.assert_called_once_with(60)


class TestMain(unittest.TestCase):
    def setUp(self):
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
                "user_agent": "test-agent",
            },
        }

    def tearDown(self):
        self.sleep_patcher.stop()
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

    @patch("fetch_posts.config.load_config")
    @patch("fetch_posts.fetch_subreddit_feed")
    def test_all_subs_succeed_writes_batch_and_exit_0(self, mock_fetch, mock_load_config):
        mock_load_config.return_value = self.cfg
        rate_info = {"remaining": "5.0", "reset": "31"}
        mock_fetch.side_effect = [
            (_feed_xml(_entry_xml(post_id="p1")), rate_info),
            (_feed_xml(_entry_xml(post_id="p2")), rate_info),
            (_feed_xml(_entry_xml(post_id="p3")), rate_info),
        ]
        code = fetch_posts.main()
        self.assertEqual(code, 0)
        with open(self.tmp_batch_path, encoding="utf-8") as f:
            batch = json.load(f)
        ids = {p["id"] for p in batch}
        self.assertEqual(ids, {"p1", "p2", "p3"})

    @patch("fetch_posts.config.load_config")
    @patch("fetch_posts.fetch_subreddit_feed")
    def test_partial_failure_still_exit_0_and_excludes_failed_sub(self, mock_fetch, mock_load_config):
        mock_load_config.return_value = self.cfg
        rate_info = {"remaining": "5.0", "reset": "31"}
        mock_fetch.side_effect = [
            (_feed_xml(_entry_xml(post_id="p1")), rate_info),
            None,
            (_feed_xml(_entry_xml(post_id="p3")), rate_info),
        ]
        code = fetch_posts.main()
        self.assertEqual(code, 0)
        with open(self.tmp_batch_path, encoding="utf-8") as f:
            batch = json.load(f)
        ids = {p["id"] for p in batch}
        self.assertEqual(ids, {"p1", "p3"})

    @patch("fetch_posts.config.load_config")
    @patch("fetch_posts.fetch_subreddit_feed")
    def test_all_subs_fail_exit_1_and_no_file_written(self, mock_fetch, mock_load_config):
        mock_load_config.return_value = self.cfg
        mock_fetch.return_value = None
        code = fetch_posts.main()
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self.tmp_batch_path))

    @patch("fetch_posts.config.load_config")
    @patch("fetch_posts.fetch_subreddit_feed")
    def test_invalid_xml_marks_sub_failed_but_others_succeed(self, mock_fetch, mock_load_config):
        mock_load_config.return_value = self.cfg
        rate_info = {"remaining": "5.0", "reset": "31"}
        mock_fetch.side_effect = [
            ("<feed><entry><unclosed></feed>", rate_info),
            (_feed_xml(_entry_xml(post_id="p2")), rate_info),
            (_feed_xml(_entry_xml(post_id="p3")), rate_info),
        ]
        code = fetch_posts.main()
        self.assertEqual(code, 0)
        with open(self.tmp_batch_path, encoding="utf-8") as f:
            batch = json.load(f)
        ids = {p["id"] for p in batch}
        self.assertEqual(ids, {"p2", "p3"})

    @patch("fetch_posts.config.load_config")
    @patch("fetch_posts.fetch_subreddit_feed")
    def test_all_invalid_xml_exit_1_and_no_file_written(self, mock_fetch, mock_load_config):
        mock_load_config.return_value = self.cfg
        mock_fetch.return_value = ("<feed><entry><unclosed></feed>", {"remaining": "5.0", "reset": "31"})
        code = fetch_posts.main()
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self.tmp_batch_path))

    @patch("fetch_posts.config.load_config")
    @patch("fetch_posts.fetch_subreddit_feed")
    def test_rate_limit_headers_trigger_extended_sleep_between_subs(self, mock_fetch, mock_load_config):
        mock_load_config.return_value = self.cfg
        mock_fetch.side_effect = [
            (_feed_xml(_entry_xml(post_id="p1")), {"remaining": "0.0", "reset": "31"}),
            (_feed_xml(_entry_xml(post_id="p2")), {"remaining": "5.0", "reset": "31"}),
            (_feed_xml(_entry_xml(post_id="p3")), {"remaining": "5.0", "reset": "31"}),
        ]
        fetch_posts.main()
        sleep_calls = [call.args[0] for call in self.mock_sleep.call_args_list]
        self.assertIn(32, sleep_calls)

    @patch("fetch_posts.config.load_config")
    @patch("fetch_posts.fetch_subreddit_feed")
    def test_paused_sub_feed_not_requested_and_excluded_from_batch(self, mock_fetch, mock_load_config):
        mock_load_config.return_value = self.cfg
        db.pause_sub("TechSEO")
        rate_info = {"remaining": "5.0", "reset": "31"}
        fresh = datetime.now(timezone.utc).isoformat()
        mock_fetch.side_effect = [
            (_feed_xml(_entry_xml(post_id="p1", published=fresh, updated=fresh)), rate_info),
            (_feed_xml(_entry_xml(post_id="p3", published=fresh, updated=fresh)), rate_info),
        ]
        code = fetch_posts.main()
        self.assertEqual(code, 0)
        self.assertEqual(mock_fetch.call_count, 2)
        requested_subs = {call.args[0] for call in mock_fetch.call_args_list}
        self.assertNotIn("TechSEO", requested_subs)
        with open(self.tmp_batch_path, encoding="utf-8") as f:
            batch = json.load(f)
        ids = {p["id"] for p in batch}
        self.assertEqual(ids, {"p1", "p3"})

    @patch("fetch_posts.config.load_config")
    @patch("fetch_posts.fetch_subreddit_feed")
    def test_all_subs_paused_exit_1_and_no_fetch_calls(self, mock_fetch, mock_load_config):
        mock_load_config.return_value = self.cfg
        for sub in self.cfg["subreddits"]:
            db.pause_sub(sub["name"])
        code = fetch_posts.main()
        self.assertEqual(code, 1)
        mock_fetch.assert_not_called()
        self.assertFalse(os.path.exists(self.tmp_batch_path))


if __name__ == "__main__":
    unittest.main()
