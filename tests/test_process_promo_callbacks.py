"""Unit-тесты src/process_promo_callbacks.py (сеть мокается, БД — временная)."""
import json
import logging
import os
import signal
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


def _msg_update(update_id, text, chat_id=42):
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


_CFG = {
    "subreddits": [
        {"name": "SEO"},
        {"name": "TechSEO"},
        {"name": "bigseo"},
    ],
}


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

        code = ppc.main(["--once"])

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
        code = ppc.main(["--once"])
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

        code = ppc.main(["--once"])

        self.assertEqual(code, 0)
        self.assertEqual(self._promo_rows(), [])
        self.assertEqual(db.get_telegram_offset(), 3)

    @patch("process_promo_callbacks.requests.post")
    @patch("process_promo_callbacks.requests.get")
    def test_chat_guard_matches_numeric_id_against_string_env(self, mock_get, mock_post):
        updates = [_update(10, _callback_query("promo:SEO:abc1", chat_id=42))]
        mock_get.return_value = _updates_response(updates)
        mock_post.return_value = _response(200)

        code = ppc.main(["--once"])

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

        code = ppc.main(["--once"])

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

        code = ppc.main(["--once"])

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
        code = ppc.main(["--once"])
        self.assertEqual(code, 1)
        self.assertEqual(mock_get.call_count, len(ppc._RETRY_DELAYS))
        self.assertEqual(db.get_telegram_offset(), 7)

    @patch("process_promo_callbacks.requests.get")
    def test_409_conflict_exits_1_without_retry(self, mock_get):
        db.set_telegram_offset(3)
        mock_get.return_value = _response(409, {"description": "Conflict: webhook is active"})
        code = ppc.main(["--once"])
        self.assertEqual(code, 1)
        self.assertEqual(mock_get.call_count, 1)
        self.assertEqual(db.get_telegram_offset(), 3)

    @patch("process_promo_callbacks.requests.get")
    def test_allowed_updates_sent_as_json_string(self, mock_get):
        mock_get.return_value = _updates_response([])
        ppc.main(["--once"])
        params = mock_get.call_args.kwargs["params"]
        self.assertEqual(json.loads(params["allowed_updates"]), ["callback_query", "message"])


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

        code = ppc.main(["--once"])

        self.assertEqual(code, 1)
        self.assertEqual(db.get_telegram_offset(), 1)


class TestNonFatalAnswerFailure(_BaseTestCase):
    @patch("process_promo_callbacks.requests.post")
    @patch("process_promo_callbacks.requests.get")
    def test_answer_callback_query_failure_is_non_fatal(self, mock_get, mock_post):
        updates = [_update(1, _callback_query("promo:SEO:abc1"))]
        mock_get.return_value = _updates_response(updates)
        mock_post.return_value = _response(500, {"description": "boom"})

        code = ppc.main(["--once"])

        self.assertEqual(code, 0)
        self.assertEqual(db.get_telegram_offset(), 1)
        self.assertEqual(len(self._promo_rows()), 1)


class TestOnceMode(_BaseTestCase):
    @patch("process_promo_callbacks.requests.get")
    def test_once_makes_single_getupdates_call_with_zero_timeout(self, mock_get):
        mock_get.return_value = _updates_response([])
        code = ppc.main(["--once"])
        self.assertEqual(code, 0)
        self.assertEqual(mock_get.call_count, 1)
        params = mock_get.call_args.kwargs["params"]
        self.assertEqual(params["timeout"], 0)


class _DaemonTestCase(_BaseTestCase):
    """База демон-тестов: сброс флага остановки и восстановление хендлеров.

    main([]) регистрирует обработчики SIGTERM/SIGINT — возвращаем прежние,
    чтобы не влиять на остальные тесты; остановку тестируем через флаг
    _shutdown_signum, а не реальный сигнал (кроссплатформенно).
    """

    def setUp(self):
        super().setUp()
        ppc._shutdown_signum = None
        self._prev_handlers = {
            signal.SIGTERM: signal.getsignal(signal.SIGTERM),
            signal.SIGINT: signal.getsignal(signal.SIGINT),
        }

    def tearDown(self):
        for signum, handler in self._prev_handlers.items():
            signal.signal(signum, handler)
        ppc._shutdown_signum = None
        super().tearDown()


class TestDaemonMode(_DaemonTestCase):
    @patch("process_promo_callbacks.time.sleep")
    @patch("process_promo_callbacks.run_iteration")
    def test_network_error_does_not_kill_daemon(self, mock_iter, mock_sleep):
        logger.debug("[test_process_promo_callbacks.check] daemon survives TelegramError, stops on _Shutdown")
        mock_iter.side_effect = [
            ppc.TelegramError("network down"),
            0,
            ppc._Shutdown(signal.SIGTERM),
        ]
        code = ppc.main([])
        self.assertEqual(code, 0)
        self.assertEqual(mock_iter.call_count, 3)
        mock_sleep.assert_called_once_with(ppc._BACKOFF_INITIAL)

    @patch("process_promo_callbacks.requests.post")
    @patch("process_promo_callbacks.requests.get")
    def test_shutdown_flag_finishes_batch_saves_offset_returns_0(self, mock_get, mock_post):
        logger.debug("[test_process_promo_callbacks.check] shutdown flag set mid-iteration: batch completes")
        updates = [_update(11, _callback_query("promo:SEO:abc1"))]

        def _get_and_request_shutdown(*args, **kwargs):
            ppc._shutdown_signum = signal.SIGTERM
            return _updates_response(updates)

        mock_get.side_effect = _get_and_request_shutdown
        mock_post.return_value = _response(200)

        code = ppc.main([])

        self.assertEqual(code, 0)
        self.assertEqual(mock_get.call_count, 1)
        self.assertEqual(db.get_telegram_offset(), 11)
        self.assertEqual(len(self._promo_rows()), 1)

    @patch("process_promo_callbacks.requests.get")
    def test_409_in_daemon_mode_exits_1(self, mock_get):
        logger.debug("[test_process_promo_callbacks.check] 409 is fatal in daemon mode")
        mock_get.return_value = _response(409, {"description": "Conflict: webhook is active"})
        code = ppc.main([])
        self.assertEqual(code, 1)
        self.assertEqual(mock_get.call_count, 1)

    @patch("process_promo_callbacks.requests.get")
    def test_daemon_getupdates_uses_long_poll_timeout(self, mock_get):
        logger.debug("[test_process_promo_callbacks.check] daemon passes poll_timeout=50 to getUpdates")

        def _get_and_request_shutdown(*args, **kwargs):
            ppc._shutdown_signum = signal.SIGTERM
            return _updates_response([])

        mock_get.side_effect = _get_and_request_shutdown

        code = ppc.main([])

        self.assertEqual(code, 0)
        params = mock_get.call_args.kwargs["params"]
        self.assertEqual(params["timeout"], ppc._POLL_TIMEOUT_DAEMON)
        self.assertEqual(
            mock_get.call_args.kwargs["timeout"],
            ppc._POLL_TIMEOUT_DAEMON + ppc._GETUPDATES_HTTP_MARGIN,
        )


class TestTokenNotLogged(_BaseTestCase):
    @patch("process_promo_callbacks.requests.post")
    @patch("process_promo_callbacks.requests.get")
    def test_no_log_line_contains_token_at_debug_level(self, mock_get, mock_post):
        logger.debug("[test_process_promo_callbacks.check] token must not leak into any DEBUG log line")
        updates = [_update(1, _callback_query("promo:SEO:abc1"))]
        mock_get.return_value = _updates_response(updates)
        mock_post.return_value = _response(200)

        with self.assertLogs(level="DEBUG") as cm:
            code = ppc.main(["--once"])

        self.assertEqual(code, 0)
        for line in cm.output:
            self.assertNotIn("test-token", line)
        # прямой критерий задачи 1: сторонний логгер urllib3 приглушён до INFO
        self.assertEqual(logging.getLogger("urllib3").getEffectiveLevel(), logging.INFO)


class TestSubsCommands(_BaseTestCase):
    def setUp(self):
        super().setUp()
        self.load_config_patcher = patch("process_promo_callbacks.config.load_config", return_value=_CFG)
        self.load_config_patcher.start()

    def tearDown(self):
        self.load_config_patcher.stop()
        super().tearDown()

    @patch("process_promo_callbacks.requests.post")
    @patch("process_promo_callbacks.requests.get")
    def test_pause_from_own_chat_pauses_and_confirms(self, mock_get, mock_post):
        mock_get.return_value = _updates_response([_msg_update(1, "/pause SEO")])
        mock_post.return_value = _response(200)

        code = ppc.main(["--once"])

        self.assertEqual(code, 0)
        self.assertEqual(db.get_paused_subs(), {"SEO"})
        send_calls = [c for c in mock_post.call_args_list if "sendMessage" in c.args[0]]
        self.assertEqual(len(send_calls), 1)
        self.assertIn("SEO", send_calls[0].kwargs["data"]["text"])

    @patch("process_promo_callbacks.requests.post")
    @patch("process_promo_callbacks.requests.get")
    def test_pause_unknown_subreddit_not_paused_lists_known(self, mock_get, mock_post):
        mock_get.return_value = _updates_response([_msg_update(1, "/pause typo_sub")])
        mock_post.return_value = _response(200)

        code = ppc.main(["--once"])

        self.assertEqual(code, 0)
        self.assertEqual(db.get_paused_subs(), set())
        send_calls = [c for c in mock_post.call_args_list if "sendMessage" in c.args[0]]
        self.assertEqual(len(send_calls), 1)
        reply_text = send_calls[0].kwargs["data"]["text"]
        self.assertIn("SEO", reply_text)
        self.assertIn("TechSEO", reply_text)
        self.assertIn("bigseo", reply_text)

    @patch("process_promo_callbacks.requests.post")
    @patch("process_promo_callbacks.requests.get")
    def test_subs_command_reports_statuses(self, mock_get, mock_post):
        db.pause_sub("SEO")
        mock_get.return_value = _updates_response([_msg_update(1, "/subs")])
        mock_post.return_value = _response(200)

        code = ppc.main(["--once"])

        self.assertEqual(code, 0)
        send_calls = [c for c in mock_post.call_args_list if "sendMessage" in c.args[0]]
        self.assertEqual(len(send_calls), 1)
        reply_text = send_calls[0].kwargs["data"]["text"]
        self.assertIn("SEO: ⏸ на паузе", reply_text)
        self.assertIn("TechSEO: ✅ активен", reply_text)

    @patch("process_promo_callbacks.requests.post")
    @patch("process_promo_callbacks.requests.get")
    def test_foreign_chat_message_not_executed(self, mock_get, mock_post):
        mock_get.return_value = _updates_response([_msg_update(1, "/pause SEO", chat_id=999)])
        mock_post.return_value = _response(200)

        code = ppc.main(["--once"])

        self.assertEqual(code, 0)
        self.assertEqual(db.get_paused_subs(), set())
        send_calls = [c for c in mock_post.call_args_list if "sendMessage" in c.args[0]]
        self.assertEqual(len(send_calls), 0)

    @patch("process_promo_callbacks.requests.post")
    @patch("process_promo_callbacks.requests.get")
    def test_non_command_text_silently_consumed_offset_advances(self, mock_get, mock_post):
        mock_get.return_value = _updates_response([_msg_update(1, "привет")])
        mock_post.return_value = _response(200)

        code = ppc.main(["--once"])

        self.assertEqual(code, 0)
        self.assertEqual(db.get_telegram_offset(), 1)
        send_calls = [c for c in mock_post.call_args_list if "sendMessage" in c.args[0]]
        self.assertEqual(len(send_calls), 0)

    @patch("process_promo_callbacks.requests.post")
    @patch("process_promo_callbacks.requests.get")
    def test_mixed_batch_callback_and_message_processed_offset_is_last(self, mock_get, mock_post):
        updates = [
            _update(1, _callback_query("promo:SEO:abc1")),
            _msg_update(2, "/subs"),
        ]
        mock_get.return_value = _updates_response(updates)
        mock_post.return_value = _response(200)

        code = ppc.main(["--once"])

        self.assertEqual(code, 0)
        self.assertEqual(db.get_telegram_offset(), 2)
        self.assertEqual(len(self._promo_rows()), 1)
        send_calls = [c for c in mock_post.call_args_list if "sendMessage" in c.args[0]]
        self.assertEqual(len(send_calls), 1)

    @patch("process_promo_callbacks.requests.post")
    @patch("process_promo_callbacks.requests.get")
    def test_pause_with_botname_suffix_works_like_plain_command(self, mock_get, mock_post):
        mock_get.return_value = _updates_response([_msg_update(1, "/pause@my_bot SEO")])
        mock_post.return_value = _response(200)

        code = ppc.main(["--once"])

        self.assertEqual(code, 0)
        self.assertEqual(db.get_paused_subs(), {"SEO"})


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
