"""Unit-тесты src/process_promo_callbacks.py (сеть мокается, БД — временная)."""
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
logger = logging.getLogger("test_process_promo_callbacks.check")

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import process_promo_callbacks as ppc  # noqa: E402  (path must be adjusted before import)
import db  # noqa: E402

_ENV_KEYS = ("ROUTINE_DB_PATH", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
_CHAT_ID = "42"


def _response(status=200, body=None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body if body is not None else {}
    resp.text = json.dumps(body if body is not None else {})
    return resp


def _updates_response(updates):
    return _response(200, {"ok": True, "result": updates})


def _callback_query(data, callback_id="cb1", chat_id=42, message_id=500, reply_markup=None, with_message=True):
    cq = {"id": callback_id, "data": data}
    if with_message:
        cq["message"] = {
            "message_id": message_id,
            "chat": {"id": chat_id},
            "reply_markup": reply_markup if reply_markup is not None else {
                "inline_keyboard": [[{"text": "✅ Запостил: T", "callback_data": data}]]
            },
        }
    return cq


def _update(update_id, callback_query=None):
    payload = {"update_id": update_id}
    if callback_query is not None:
        payload["callback_query"] = callback_query
    return payload


class _BaseTestCase(unittest.TestCase):
    def setUp(self):
        self._prev_env = {key: os.environ.get(key) for key in _ENV_KEYS}
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(db_path)
        self.tmp_db_path = db_path
        os.environ["ROUTINE_DB_PATH"] = db_path
        os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
        os.environ["TELEGRAM_CHAT_ID"] = _CHAT_ID
        logger.debug("[test_process_promo_callbacks.check] temp db=%s", db_path)

    def tearDown(self):
        for key, value in self._prev_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if os.path.exists(self.tmp_db_path):
            os.remove(self.tmp_db_path)

    def _promo_rows(self):
        conn = db.connect()
        try:
            rows = conn.execute(
                "SELECT subreddit, type, post_url FROM promo_history ORDER BY id ASC"
            ).fetchall()
        finally:
            conn.close()
        return [dict(row) for row in rows]


class TestNormalFlow(_BaseTestCase):
    @patch("process_promo_callbacks.requests.post")
    @patch("process_promo_callbacks.requests.get")
    def test_two_valid_updates_logged_offset_advances(self, mock_get, mock_post):
        updates = [
            _update(101, _callback_query("promo:SEO:abc1")),
            _update(102, _callback_query("promo:TechSEO:def2")),
        ]
        mock_get.return_value = _updates_response(updates)
        mock_post.return_value = _response(200)

        code = ppc.main()

        self.assertEqual(code, 0)
        rows = self._promo_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], {"subreddit": "SEO", "type": "comment_promo",
                                    "post_url": "https://www.reddit.com/comments/abc1"})
        self.assertEqual(rows[1], {"subreddit": "TechSEO", "type": "comment_promo",
                                    "post_url": "https://www.reddit.com/comments/def2"})
        self.assertEqual(db.get_telegram_offset(), 102)

        post_urls_called = [c.args[0] for c in mock_post.call_args_list]
        self.assertTrue(any("answerCallbackQuery" in url for url in post_urls_called))
        self.assertTrue(any("editMessageReplyMarkup" in url for url in post_urls_called))

    @patch("process_promo_callbacks.requests.get")
    def test_empty_updates_exit_0_offset_untouched(self, mock_get):
        db.set_telegram_offset(5)
        mock_get.return_value = _updates_response([])
        code = ppc.main()
        self.assertEqual(code, 0)
        self.assertEqual(db.get_telegram_offset(), 5)


class TestFiltering(_BaseTestCase):
    @patch("process_promo_callbacks.requests.post")
    @patch("process_promo_callbacks.requests.get")
    def test_foreign_updates_filtered_offset_consumed(self, mock_get, mock_post):
        updates = [
            _update(1),  # no callback_query at all
            _update(2, {"id": "cb", "data": "not-promo", "message": {
                "message_id": 1, "chat": {"id": 42}, "reply_markup": {}}}),
            _update(3, _callback_query("promo:SEO:abc1", chat_id=999)),  # foreign chat
        ]
        mock_get.return_value = _updates_response(updates)
        mock_post.return_value = _response(200)

        code = ppc.main()

        self.assertEqual(code, 0)
        self.assertEqual(self._promo_rows(), [])
        self.assertEqual(db.get_telegram_offset(), 3)

    @patch("process_promo_callbacks.requests.post")
    @patch("process_promo_callbacks.requests.get")
    def test_chat_guard_matches_numeric_id_against_string_env(self, mock_get, mock_post):
        updates = [_update(10, _callback_query("promo:SEO:abc1", chat_id=42))]
        mock_get.return_value = _updates_response(updates)
        mock_post.return_value = _response(200)

        code = ppc.main()

        self.assertEqual(code, 0)
        self.assertEqual(len(self._promo_rows()), 1)


class TestDuplicateInBatch(_BaseTestCase):
    @patch("process_promo_callbacks.requests.post")
    @patch("process_promo_callbacks.requests.get")
    def test_duplicate_callback_data_logs_once(self, mock_get, mock_post):
        updates = [
            _update(1, _callback_query("promo:SEO:abc1", callback_id="cb1")),
            _update(2, _callback_query("promo:SEO:abc1", callback_id="cb2")),
        ]
        mock_get.return_value = _updates_response(updates)
        mock_post.return_value = _response(200)

        code = ppc.main()

        self.assertEqual(code, 0)
        self.assertEqual(len(self._promo_rows()), 1)
        self.assertEqual(db.get_telegram_offset(), 2)


class TestNoMessage(_BaseTestCase):
    @patch("process_promo_callbacks.requests.post")
    @patch("process_promo_callbacks.requests.get")
    def test_update_without_message_logs_but_skips_edit(self, mock_get, mock_post):
        updates = [_update(1, _callback_query("promo:SEO:abc1", with_message=False))]
        mock_get.return_value = _updates_response(updates)
        mock_post.return_value = _response(200)

        code = ppc.main()

        self.assertEqual(code, 0)
        self.assertEqual(len(self._promo_rows()), 1)
        post_urls_called = [c.args[0] for c in mock_post.call_args_list]
        self.assertFalse(any("editMessageReplyMarkup" in url for url in post_urls_called))
        self.assertTrue(any("answerCallbackQuery" in url for url in post_urls_called))


class TestGetUpdatesFailures(_BaseTestCase):
    @patch("process_promo_callbacks.time.sleep")
    @patch("process_promo_callbacks.requests.get")
    def test_500_on_all_retries_exit_1_offset_untouched(self, mock_get, mock_sleep):
        db.set_telegram_offset(7)
        mock_get.return_value = _response(500, {"description": "boom"})
        code = ppc.main()
        self.assertEqual(code, 1)
        self.assertEqual(mock_get.call_count, len(ppc._RETRY_DELAYS))
        self.assertEqual(db.get_telegram_offset(), 7)

    @patch("process_promo_callbacks.requests.get")
    def test_409_conflict_exits_1_without_retry(self, mock_get):
        db.set_telegram_offset(3)
        mock_get.return_value = _response(409, {"description": "Conflict: webhook is active"})
        code = ppc.main()
        self.assertEqual(code, 1)
        self.assertEqual(mock_get.call_count, 1)
        self.assertEqual(db.get_telegram_offset(), 3)

    @patch("process_promo_callbacks.requests.get")
    def test_allowed_updates_sent_as_json_string(self, mock_get):
        mock_get.return_value = _updates_response([])
        ppc.main()
        params = mock_get.call_args.kwargs["params"]
        self.assertEqual(json.loads(params["allowed_updates"]), ["callback_query"])


class TestPartialFailureOffset(_BaseTestCase):
    @patch("process_promo_callbacks.requests.post")
    @patch("process_promo_callbacks.requests.get")
    @patch("process_promo_callbacks.db.log_promo")
    def test_failure_on_second_of_three_offset_points_to_first(self, mock_log_promo, mock_get, mock_post):
        updates = [
            _update(1, _callback_query("promo:SEO:a1")),
            _update(2, _callback_query("promo:SEO:a2")),
            _update(3, _callback_query("promo:SEO:a3")),
        ]
        mock_get.return_value = _updates_response(updates)
        mock_post.return_value = _response(200)
        mock_log_promo.side_effect = [None, RuntimeError("boom"), None]

        code = ppc.main()

        self.assertEqual(code, 1)
        self.assertEqual(db.get_telegram_offset(), 1)


class TestNonFatalAnswerFailure(_BaseTestCase):
    @patch("process_promo_callbacks.requests.post")
    @patch("process_promo_callbacks.requests.get")
    def test_answer_callback_query_failure_is_non_fatal(self, mock_get, mock_post):
        updates = [_update(1, _callback_query("promo:SEO:abc1"))]
        mock_get.return_value = _updates_response(updates)
        mock_post.return_value = _response(500, {"description": "boom"})

        code = ppc.main()

        self.assertEqual(code, 0)
        self.assertEqual(db.get_telegram_offset(), 1)
        self.assertEqual(len(self._promo_rows()), 1)


class TestParseCallbackData(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(ppc._parse_callback_data("promo:SEO:abc123"), ("SEO", "abc123"))

    def test_missing_prefix(self):
        self.assertIsNone(ppc._parse_callback_data("other:SEO:abc123"))

    def test_promo_without_parts(self):
        self.assertIsNone(ppc._parse_callback_data("promo:"))

    def test_empty_sub(self):
        self.assertIsNone(ppc._parse_callback_data("promo::abc123"))

    def test_extra_colons_go_into_post_id(self):
        self.assertEqual(ppc._parse_callback_data("promo:SEO:abc:123"), ("SEO", "abc:123"))

    def test_empty_string(self):
        self.assertIsNone(ppc._parse_callback_data(""))


if __name__ == "__main__":
    unittest.main()
