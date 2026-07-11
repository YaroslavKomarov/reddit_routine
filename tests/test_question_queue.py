"""Unit- и CLI-тесты src/question_queue.py (временная БД, без сети)."""
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "DEBUG"),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("test_question_queue.check")

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import question_queue  # noqa: E402  (path must be adjusted before import)
import db  # noqa: E402


class TestParseQuestionsFile(unittest.TestCase):
    def test_multiple_blocks_with_and_without_target_sub(self):
        text = "target_sub: SEO\nquestion one\n---\njust a question, no sub\n"
        result = question_queue.parse_questions_file(text)
        self.assertEqual(result, [
            {"text": "question one", "target_sub": "SEO"},
            {"text": "just a question, no sub", "target_sub": None},
        ])

    def test_empty_block_between_two_blocks_is_skipped(self):
        text = "first block\n---\n\n---\nsecond block\n"
        result = question_queue.parse_questions_file(text)
        self.assertEqual(result, [
            {"text": "first block", "target_sub": None},
            {"text": "second block", "target_sub": None},
        ])

    def test_file_without_separator_is_single_question(self):
        text = "the whole file is one question\nwith two lines"
        result = question_queue.parse_questions_file(text)
        self.assertEqual(result, [
            {"text": "the whole file is one question\nwith two lines", "target_sub": None},
        ])


class QuestionQueueCliTestCase(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(path)
        self.tmp_db_path = path
        self._prev_env = os.environ.get("ROUTINE_DB_PATH")
        os.environ["ROUTINE_DB_PATH"] = path
        self.run_env = {**os.environ, "ROUTINE_DB_PATH": path, "PYTHONIOENCODING": "utf-8"}

    def tearDown(self):
        if self._prev_env is None:
            os.environ.pop("ROUTINE_DB_PATH", None)
        else:
            os.environ["ROUTINE_DB_PATH"] = self._prev_env
        if os.path.exists(self.tmp_db_path):
            os.remove(self.tmp_db_path)

    def _run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(SRC_DIR / "question_queue.py"), *args],
            env=self.run_env,
            capture_output=True,
            encoding="utf-8",
        )


class TestAddFile(QuestionQueueCliTestCase):
    def test_add_file_with_one_empty_block_reports_discrepancy(self):
        fd, questions_path = tempfile.mkstemp(suffix=".md")
        os.close(fd)
        try:
            Path(questions_path).write_text(
                "target_sub: SEO\nfirst question\n---\n\n---\nsecond question\n",
                encoding="utf-8",
            )
            result = self._run_cli("add", "--file", questions_path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("2 из 3", result.stdout)
            questions = db.list_unused_questions()
            self.assertEqual(len(questions), 2)
        finally:
            os.remove(questions_path)


class TestAddFileDedup(QuestionQueueCliTestCase):
    def _write_questions_file(self, content):
        fd, questions_path = tempfile.mkstemp(suffix=".md")
        os.close(fd)
        self.addCleanup(os.remove, questions_path)
        Path(questions_path).write_text(content, encoding="utf-8")
        return questions_path

    def test_repeated_add_file_skips_all_as_duplicates(self):
        questions_path = self._write_questions_file("first question\n---\nsecond question\n")
        first = self._run_cli("add", "--file", questions_path)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("добавлено 2 из 2", first.stdout)

        second = self._run_cli("add", "--file", questions_path)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("добавлено 0 из 2", second.stdout)
        self.assertIn("пропущено дублей 2", second.stdout)
        self.assertEqual(len(db.list_unused_questions()), 2)

    def test_mixed_file_adds_only_new_questions(self):
        db.add_question("old question")
        questions_path = self._write_questions_file("old question\n---\nnew question\n")
        result = self._run_cli("add", "--file", questions_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("добавлено 1 из 2", result.stdout)
        self.assertIn("пропущено дублей 1", result.stdout)
        texts = [q["text"] for q in db.list_unused_questions()]
        self.assertEqual(sorted(texts), ["new question", "old question"])

    def test_duplicate_inside_one_file_added_once(self):
        questions_path = self._write_questions_file("same question\n---\nsame question\n")
        result = self._run_cli("add", "--file", questions_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("добавлено 1 из 2", result.stdout)
        self.assertIn("пропущено дублей 1", result.stdout)
        self.assertEqual(len(db.list_unused_questions()), 1)

    def test_used_question_is_not_a_duplicate(self):
        db.add_question("burned question")
        popped = db.pop_oldest_question()
        self.assertEqual(popped["text"], "burned question")

        questions_path = self._write_questions_file("burned question\n")
        result = self._run_cli("add", "--file", questions_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("добавлено 1 из 1", result.stdout)
        self.assertNotIn("пропущено дублей", result.stdout)
        self.assertEqual(len(db.list_unused_questions()), 1)

    def test_file_without_duplicates_has_no_skip_suffix(self):
        questions_path = self._write_questions_file("unique one\n---\nunique two\n")
        result = self._run_cli("add", "--file", questions_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("добавлено 2 из 2", result.stdout)
        self.assertNotIn("пропущено дублей", result.stdout)


class TestListStatsPop(QuestionQueueCliTestCase):
    def test_stats_and_list_after_add_and_pop(self):
        db.add_question("q1")
        db.add_question("q2", target_sub="TechSEO")
        popped = db.pop_oldest_question()

        stats_result = self._run_cli("stats")
        self.assertEqual(stats_result.returncode, 0, stats_result.stderr)
        self.assertIn("unused=1", stats_result.stdout)
        self.assertIn("used=1", stats_result.stdout)

        list_result = self._run_cli("list")
        self.assertEqual(list_result.returncode, 0, list_result.stderr)
        self.assertNotIn(popped["text"], list_result.stdout)
        self.assertIn("q2", list_result.stdout)

    def test_pop_on_empty_queue_exit_0_with_warning(self):
        result = self._run_cli("pop")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("очередь вопросов пуста", result.stdout)
        self.assertEqual(db.list_unused_questions(), [])


class TestLogPromo(QuestionQueueCliTestCase):
    def test_valid_type_logs_to_db(self):
        result = self._run_cli("log-promo", "SEO", "comment_promo")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(db.last_promo_days_ago("SEO"), 0)

    def test_invalid_type_exit_1_with_clean_message(self):
        result = self._run_cli("log-promo", "SEO", "not_a_type")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Error", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
