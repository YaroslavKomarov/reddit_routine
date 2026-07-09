"""Unit-тесты src/send_telegram.py (сеть мокается, БД — временная).

Ручной smoke после этих тестов: `bash run_daily.sh --dry-run` на машине
с установленным `claude` — полный конвейер, дайджест печатается в stdout;
затем боевой прогон `bash run_daily.sh` (дайджест приходит в Telegram;
при временно сломанном TELEGRAM_BOT_TOKEN в run_log появляется tg_failed).
"""
import io
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch, MagicMock
from pathlib import Path

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "DEBUG"),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("test_send_telegram.check")

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import send_telegram  # noqa: E402  (path must be adjusted before import)
import db  # noqa: E402

_ENV_KEYS = ("ROUTINE_DB_PATH", "DIGEST_PATH", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


def _response(status=200, body=None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body if body is not None else {}
    resp.text = json.dumps(body if body is not None else {})
    return resp


def _valid_digest():
    return {
        "question_post": {"subreddit": "SEO", "title": "Вопрос дня", "body": "Текст вопроса"},
        "suggestions": [
            {"subreddit": "SEO", "posts": [
                {"post_id": "p1", "post_title": "T1", "post_url": "https://reddit.com/p1",
                 "comment_draft": "черновик", "why": "релевантно", "is_promo": True},
            ]},
        ],
        "skipped_subs": [{"subreddit": "bigseo", "reason": "нет релевантных постов"}],
    }


class TestSplitMessage(unittest.TestCase):
    def test_short_message_not_split(self):
        self.assertEqual(send_telegram.split_message("короткое сообщение"), ["короткое сообщение"])

    def test_exactly_4096_not_split(self):
        text = "x" * 4096
        self.assertEqual(send_telegram.split_message(text), [text])

    def test_long_message_split_at_post_boundaries(self):
        blocks = ["💬 r/SEO"] + [
            f'<a href="u{i}">t{i}</a>\n<blockquote>{"q" * 500}</blockquote>\n<i>w</i>'
            for i in range(12)
        ]
        parts = send_telegram.split_message("\n\n".join(blocks))
        self.assertGreater(len(parts), 1)
        for part in parts:
            self.assertLessEqual(len(part), 4096)
            # блок поста — неделимая единица: blockquote не порван между кусками
            self.assertEqual(part.count("<blockquote>"), part.count("</blockquote>"))

    def test_continuation_has_header(self):
        blocks = ["💬 r/SEO"] + [
            f'<a href="u{i}">t{i}</a>\n<blockquote>{"q" * 500}</blockquote>\n<i>w</i>'
            for i in range(12)
        ]
        parts = send_telegram.split_message("\n\n".join(blocks))
        for part in parts[1:]:
            self.assertTrue(part.startswith("💬 r/SEO (продолжение)"), part[:60])

    def test_single_oversized_block_truncated_with_valid_html(self):
        block = f'<a href="u">t</a>\n<blockquote>{"z" * 6000}</blockquote>\n<i>w</i>'
        text = "💬 r/SEO\n\n" + block
        parts = send_telegram.split_message(text)
        joined = "\n\n".join(parts)
        self.assertIn("…", joined)
        for part in parts:
            self.assertLessEqual(len(part), 4096)
            self.assertEqual(part.count("<blockquote>"), part.count("</blockquote>"))


class TestFormatting(unittest.TestCase):
    def test_draft_with_html_specials_escaped(self):
        group = {"subreddit": "SEO", "posts": [
            {"post_title": "t", "post_url": "u", "comment_draft": "<b>жирный</b> & x > y",
             "why": "w", "is_promo": False},
        ]}
        message = send_telegram.format_subreddit_message(group)
        self.assertIn("<blockquote>&lt;b&gt;жирный&lt;/b&gt; &amp; x &gt; y</blockquote>", message)

    def test_fire_marker_only_for_promo(self):
        posts = [
            {"post_title": "t1", "post_url": "u1", "comment_draft": "d", "why": "w", "is_promo": True},
            {"post_title": "t2", "post_url": "u2", "comment_draft": "d", "why": "w", "is_promo": False},
        ]
        message = send_telegram.format_subreddit_message({"subreddit": "SEO", "posts": posts})
        self.assertEqual(message.count("🔥"), 1)
        promo_line = next(line for line in message.split("\n") if "t1" in line)
        self.assertIn("🔥", promo_line)

    def test_skipped_subs_in_stats(self):
        message = send_telegram.format_stats(
            {"posts_fetched": 1, "posts_suggested": 1, "cost_usd": 0.1},
            {"unused": 2},
            [{"subreddit": "bigseo", "reason": "нет постов"}],
            has_promo=False,
        )
        self.assertIn("r/bigseo: нет постов", message)

    def test_log_promo_reminder_only_with_promo(self):
        args = ({"posts_fetched": 1, "posts_suggested": 1, "cost_usd": None}, {"unused": 2}, [])
        with_promo = send_telegram.format_stats(*args, has_promo=True)
        without_promo = send_telegram.format_stats(*args, has_promo=False)
        self.assertIn("log-promo", with_promo)
        self.assertNotIn("log-promo", without_promo)

    def test_none_cost_renders_as_placeholder(self):
        message = send_telegram.format_stats(
            {"posts_fetched": 1, "posts_suggested": 1, "cost_usd": None},
            {"unused": 0}, [], has_promo=False,
        )
        self.assertIn("н/д", message)

    def test_question_post_notes_optional(self):
        qp = {"subreddit": "SEO", "title": "t", "body": "b"}
        self.assertNotIn("<i>", send_telegram.format_question_post(qp, 1))
        qp["notes"] = "заметка"
        self.assertIn("<i>заметка</i>", send_telegram.format_question_post(qp, 1))

    def test_null_question_post_reports_queue(self):
        message = send_telegram.format_question_post(None, 5)
        self.assertIn("без поста дня", message)
        self.assertIn("5", message)


class TestPromoKeyboard(unittest.TestCase):
    def test_keyboard_none_when_no_promo_posts(self):
        group = {"subreddit": "SEO", "posts": [
            {"post_id": "a1", "post_title": "t", "is_promo": False},
        ]}
        self.assertIsNone(send_telegram.build_promo_keyboard(group))

    def test_callback_data_format_and_within_byte_limit(self):
        sub = "a" * 21
        group = {"subreddit": sub, "posts": [
            {"post_id": "abc1234567", "post_title": "T", "is_promo": True},
        ]}
        keyboard = send_telegram.build_promo_keyboard(group)
        callback_data = keyboard["inline_keyboard"][0][0]["callback_data"]
        self.assertEqual(callback_data, f"promo:{sub}:abc1234567")
        self.assertLessEqual(len(callback_data.encode("utf-8")), 64)

    def test_multiple_promo_posts_produce_multiple_rows(self):
        group = {"subreddit": "SEO", "posts": [
            {"post_id": "p1", "post_title": "T1", "is_promo": True},
            {"post_id": "p2", "post_title": "T2", "is_promo": True},
            {"post_id": "p3", "post_title": "T3", "is_promo": False},
        ]}
        keyboard = send_telegram.build_promo_keyboard(group)
        self.assertEqual(len(keyboard["inline_keyboard"]), 2)

    def test_oversized_callback_data_skips_button(self):
        group = {"subreddit": "SEO", "posts": [
            {"post_id": "p" * 60, "post_title": "T", "is_promo": True},
        ]}
        self.assertIsNone(send_telegram.build_promo_keyboard(group))

    def test_keyboard_attached_only_to_last_chunk_when_split(self):
        posts = [
            {"post_id": "p1", "post_title": "T1", "post_url": "u1",
             "comment_draft": "d", "why": "w", "is_promo": True},
        ] + [
            {"post_id": f"p{i}", "post_title": f"t{i}", "post_url": f"u{i}",
             "comment_draft": "q" * 500, "why": "w", "is_promo": False}
            for i in range(12)
        ]
        group = {"subreddit": "SEO", "posts": posts}
        text = send_telegram.format_subreddit_message(group)
        keyboard = send_telegram.build_promo_keyboard(group)
        pairs = send_telegram._chunks_with_keyboard(text, keyboard)
        self.assertGreater(len(pairs), 1)
        for _, markup in pairs[:-1]:
            self.assertIsNone(markup)
        self.assertIsNotNone(pairs[-1][1])

    @patch("send_telegram.requests.post")
    def test_send_message_includes_reply_markup_when_given(self, mock_post):
        mock_post.return_value = _response(200)
        keyboard = {"inline_keyboard": [[{"text": "✅ Запостил: T", "callback_data": "promo:SEO:p1"}]]}
        send_telegram.send_message("tok", "42", "hi", reply_markup=keyboard)
        payload = mock_post.call_args.kwargs["data"]
        self.assertEqual(json.loads(payload["reply_markup"]), keyboard)

    @patch("send_telegram.requests.post")
    def test_send_message_without_reply_markup_omits_key(self, mock_post):
        mock_post.return_value = _response(200)
        send_telegram.send_message("tok", "42", "hi")
        payload = mock_post.call_args.kwargs["data"]
        self.assertNotIn("reply_markup", payload)


class _CliTestCase(unittest.TestCase):
    """Общий setUp CLI-тестов: временная БД, DIGEST_PATH, токены в env."""

    def setUp(self):
        self._prev_env = {key: os.environ.get(key) for key in _ENV_KEYS}

        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(db_path)
        self.tmp_db_path = db_path
        os.environ["ROUTINE_DB_PATH"] = db_path

        self.tmp_dir = Path(tempfile.mkdtemp())
        self.digest_path = self.tmp_dir / "digest.json"
        os.environ["DIGEST_PATH"] = str(self.digest_path)
        # Пустые значения не дают load_dotenv() подтянуть реальный .env владельца
        os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
        os.environ["TELEGRAM_CHAT_ID"] = "42"
        logger.debug("[test_send_telegram.check] temp dir=%s db=%s", self.tmp_dir, db_path)

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

    def _write_digest(self, digest=None, stats=None):
        payload = {
            "digest": digest if digest is not None else _valid_digest(),
            "stats": stats if stats is not None else
                {"cost_usd": 0.33, "posts_fetched": 7, "posts_suggested": 2},
        }
        self.digest_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class TestErrorMode(_CliTestCase):
    @patch("send_telegram.requests.post")
    def test_error_sends_single_message_without_digest(self, mock_post):
        mock_post.return_value = _response(200)
        # DIGEST_PATH указывает на несуществующий файл — --error не должен его читать
        os.environ["DIGEST_PATH"] = str(self.tmp_dir / "no-such-digest.json")
        code = send_telegram.main(["--error", "fetch failed"])
        self.assertEqual(code, 0)
        self.assertEqual(mock_post.call_count, 1)
        sent_text = mock_post.call_args.kwargs["data"]["text"]
        self.assertIn("⚠️ Reddit Routine: fetch failed", sent_text)

    @patch("send_telegram.requests.post")
    def test_error_without_token_exits_1(self, mock_post):
        os.environ["TELEGRAM_BOT_TOKEN"] = ""
        code = send_telegram.main(["--error", "boom"])
        self.assertEqual(code, 1)
        mock_post.assert_not_called()


class TestDryRunMode(_CliTestCase):
    @patch("send_telegram.db.log_run")
    @patch("send_telegram.requests.post")
    def test_dry_run_prints_and_touches_nothing(self, mock_post, mock_log_run):
        self._write_digest()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = send_telegram.main(["--dry-run"])
        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn("--- message 1/", output)
        self.assertIn("Пост дня", output)
        self.assertIn("💬 r/SEO", output)
        mock_post.assert_not_called()
        mock_log_run.assert_not_called()

    def test_dry_run_with_missing_digest_exits_1(self):
        code = send_telegram.main(["--dry-run"])
        self.assertEqual(code, 1)

    @patch("send_telegram.db.log_run")
    @patch("send_telegram.requests.post")
    def test_dry_run_prints_button_marker_without_network(self, mock_post, mock_log_run):
        self._write_digest()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = send_telegram.main(["--dry-run"])
        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn("[кнопки: ✅ Запостил: T1]", output)
        mock_post.assert_not_called()


class TestNormalRun(_CliTestCase):
    @patch("send_telegram.time.sleep")
    @patch("send_telegram.requests.post")
    def test_successful_run_logs_ok_with_digest_stats(self, mock_post, mock_sleep):
        mock_post.return_value = _response(200)
        self._write_digest(stats={"cost_usd": 0.33, "posts_fetched": 7, "posts_suggested": 2})
        code = send_telegram.main([])
        self.assertEqual(code, 0)
        self.assertGreaterEqual(mock_post.call_count, 3)  # пост дня + сабреддит(ы) + итоги

        conn = db.connect()
        try:
            rows = conn.execute(
                "SELECT status, posts_fetched, posts_suggested, cost_usd FROM run_log"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            (rows[0]["status"], rows[0]["posts_fetched"], rows[0]["posts_suggested"], rows[0]["cost_usd"]),
            ("ok", 7, 2, 0.33),
        )

    @patch("send_telegram.db.log_run")
    @patch("send_telegram.time.sleep")
    @patch("send_telegram.requests.post")
    def test_failed_send_exits_1_without_log_run(self, mock_post, mock_sleep, mock_log_run):
        mock_post.return_value = _response(400, {"description": "Bad Request"})
        self._write_digest()
        code = send_telegram.main([])
        self.assertEqual(code, 1)
        mock_log_run.assert_not_called()


class TestRetries(_CliTestCase):
    @patch("send_telegram.time.sleep")
    @patch("send_telegram.requests.post")
    def test_429_with_retry_after_then_success(self, mock_post, mock_sleep):
        mock_post.side_effect = [
            _response(429, {"parameters": {"retry_after": 7}}),
            _response(200),
        ]
        ok = send_telegram.send_message("tok", "42", "привет")
        self.assertTrue(ok)
        self.assertEqual(mock_post.call_count, 2)
        mock_sleep.assert_called_once_with(7)

    @patch("send_telegram.time.sleep")
    @patch("send_telegram.requests.post")
    def test_exhausted_retries_return_false(self, mock_post, mock_sleep):
        mock_post.return_value = _response(500, {"description": "boom"})
        ok = send_telegram.send_message("tok", "42", "привет")
        self.assertFalse(ok)
        self.assertEqual(mock_post.call_count, len(send_telegram._RETRY_DELAYS))


class TestDryRunSmokeSubprocess(unittest.TestCase):
    """Интеграционный smoke: send_telegram.py --dry-run как отдельный процесс.

    Ручной smoke поверх этого автотеста (на машине с установленным `claude`):
    1. `bash run_daily.sh --dry-run` — полный конвейер, дайджест в stdout.
       ВНИМАНИЕ: smoke имеет реальные побочные эффекты — тратится бюджет
       агента, предложенные посты пишутся в seen_posts, а вопрос дня
       помечается использованным (очередь уменьшается на один).
    2. Боевой прогон `bash run_daily.sh` — дайджест приходит в Telegram.
    3. Временно сломать TELEGRAM_BOT_TOKEN и повторить — в run_log должна
       появиться запись tg_failed, уведомление не отправляется.
    """

    def test_dry_run_subprocess_prints_digest_within_limits(self):
        fixture = Path(__file__).resolve().parent / "fixtures" / "digest_sample.json"
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(db_path)
        self.addCleanup(lambda: os.path.exists(db_path) and os.remove(db_path))

        env = {
            **os.environ,
            "DIGEST_PATH": str(fixture),
            "ROUTINE_DB_PATH": db_path,
            "LOG_LEVEL": "WARNING",
            "TELEGRAM_BOT_TOKEN": "",
            "TELEGRAM_CHAT_ID": "",
        }
        proc = subprocess.run(
            [sys.executable, str(SRC_DIR / "send_telegram.py"), "--dry-run"],
            cwd=REPO_ROOT, env=env, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

        output = proc.stdout
        self.assertIn("Пост дня", output)
        self.assertIn("💬 r/", output)
        self.assertIn("🔥", output)
        self.assertIn("(продолжение)", output)  # длинный черновик вызвал разбиение
        self.assertIn("bigseo", output)

        messages = re.split(r"--- message \d+/\d+ ---\n", output)[1:]
        self.assertGreaterEqual(len(messages), 4)  # пост дня + сабреддиты (с разбиением) + итоги
        self.assertIn("[кнопки: ", output)  # промо-пост из фикстуры даёт кнопку в dry-run выводе
        for message in messages:
            # маркер [кнопки: ...] — chrome только dry-run вывода, в реальном
            # sendMessage его нет; исключить перед проверкой лимита Telegram
            text_only = re.sub(r"\n\[кнопки: .*\]$", "", message.rstrip("\n"))
            self.assertLessEqual(len(text_only), 4096)


if __name__ == "__main__":
    unittest.main()
