"""CLI управления очередью вопросов-постов.

Тонкий argparse-шаблон над db.py. Ничего не знает о Reddit/агенте/
Telegram/config.yaml — вызывает только функции db.py.
"""
import argparse
import logging
import os
import sys
from pathlib import Path

import db

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "DEBUG"),
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("question_queue")

_PREVIEW_LEN = 80


def _split_blocks(text: str) -> list:
    blocks = []
    current = []
    for line in text.splitlines():
        if line.strip() == "---":
            blocks.append("\n".join(current))
            current = []
        else:
            current.append(line)
    blocks.append("\n".join(current))
    return blocks


def parse_questions_file(text: str) -> list:
    questions = []
    for block in _split_blocks(text):
        stripped_lines = [l for l in block.splitlines() if l.strip()]
        if not stripped_lines:
            logger.warning("[question_queue.parse_questions_file] skipping empty block")
            continue

        first_line = stripped_lines[0].strip()
        target_sub = None
        body_lines = stripped_lines
        if first_line.lower().startswith("target_sub:"):
            target_sub = first_line.split(":", 1)[1].strip() or None
            body_lines = stripped_lines[1:]

        question_text = "\n".join(body_lines).strip()
        if not question_text:
            logger.warning("[question_queue.parse_questions_file] skipping block with no question text")
            continue

        logger.debug(
            "[question_queue.parse_questions_file] parsed question target_sub=%s text=%.50r",
            target_sub, question_text,
        )
        questions.append({"text": question_text, "target_sub": target_sub})

    return questions


def _cmd_list(_args) -> int:
    questions = db.list_unused_questions()
    if not questions:
        print("очередь пуста")
        return 0
    for q in questions:
        preview = q["text"][:_PREVIEW_LEN]
        print(f"{q['id']}\t{q.get('target_sub') or '—'}\t{preview}")
    return 0


def _cmd_stats(_args) -> int:
    stats = db.queue_stats()
    print(f"unused={stats['unused']} used={stats['used']}")
    return 0


def _cmd_pop(_args) -> int:
    question = db.pop_oldest_question()
    if question is None:
        print("⚠️ очередь вопросов пуста")
        return 0
    print(f"{question['id']}\t{question.get('target_sub') or '—'}\t{question['text']}")
    return 0


def _cmd_log_promo(args) -> int:
    try:
        db.log_promo(args.subreddit, args.type, post_url=args.url)
    except ValueError as exc:
        logger.error("[question_queue._cmd_log_promo] %s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"logged {args.type} for {args.subreddit}")
    return 0


def _cmd_add(args) -> int:
    if args.file:
        return _add_from_file(args.file)
    return _add_interactive()


def _add_interactive() -> int:
    text = ""
    while not text.strip():
        text = input("Текст вопроса: ")
        if not text.strip():
            print("Текст не может быть пустым, попробуйте ещё раз.")
    target_sub = input("target_sub (Enter — без привязки): ").strip() or None
    question_id = db.add_question(text, target_sub)
    logger.info("[question_queue._add_interactive] added question id=%d", question_id)
    print(f"добавлен вопрос id={question_id}")
    return 0


def _add_from_file(file_path: str) -> int:
    path = Path(file_path)
    if not path.exists():
        print(f"Error: file not found: {file_path}", file=sys.stderr)
        return 1

    text = path.read_text(encoding="utf-8")
    logger.debug("[question_queue._add_from_file] reading %s", path)
    parsed = parse_questions_file(text)
    logger.debug("[question_queue._add_from_file] found %d block(s) with question text", len(parsed))

    added = 0
    for item in parsed:
        db.add_question(item["text"], item["target_sub"])
        added += 1

    total_blocks = len(_split_blocks(text))
    logger.info("[question_queue._add_from_file] added %d of %d block(s)", added, total_blocks)
    print(f"добавлено {added} из {total_blocks}")
    return 0


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reddit Routine question queue CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list").set_defaults(func=_cmd_list)
    subparsers.add_parser("stats").set_defaults(func=_cmd_stats)
    subparsers.add_parser("pop").set_defaults(func=_cmd_pop)

    log_promo_parser = subparsers.add_parser("log-promo")
    log_promo_parser.add_argument("subreddit")
    log_promo_parser.add_argument("type")
    log_promo_parser.add_argument("--url", default=None)
    log_promo_parser.set_defaults(func=_cmd_log_promo)

    add_parser = subparsers.add_parser("add")
    add_parser.add_argument("--file", default=None)
    add_parser.set_defaults(func=_cmd_add)

    return parser


def main(argv=None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    logger.debug("[question_queue.main] command='%s' args=%s", args.command, vars(args))
    try:
        return args.func(args)
    except Exception as exc:
        logger.error("[question_queue.main] unhandled error in command '%s': %s", args.command, exc)
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
