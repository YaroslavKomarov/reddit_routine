"""CLI управления паузой сабреддитов.

Тонкий argparse-шаблон над db.py (как question_queue.py). Импортирует
только db.py и config.py — валидация имён сабов живёт здесь, а не в db.py.
"""
import argparse
import logging
import os
import sys

import config
import db

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "DEBUG"),
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("manage_subs")


def _known_sub_names() -> set:
    return {sub["name"] for sub in config.load_config()["subreddits"]}


def _reject_unknown_sub(subreddit: str, known: set) -> int:
    logger.error("[manage_subs._reject_unknown_sub] unknown subreddit '%s'", subreddit)
    print(f"Error: unknown subreddit '{subreddit}'", file=sys.stderr)
    print(f"known subreddits: {', '.join(sorted(known))}", file=sys.stderr)
    return 1


def _cmd_pause(args) -> int:
    known = _known_sub_names()
    if args.subreddit not in known:
        return _reject_unknown_sub(args.subreddit, known)
    paused_now = db.pause_sub(args.subreddit)
    logger.info("[manage_subs._cmd_pause] paused '%s'", args.subreddit)
    if paused_now:
        print(f"r/{args.subreddit} поставлен на паузу")
    else:
        print(f"r/{args.subreddit} уже на паузе")
    return 0


def _cmd_resume(args) -> int:
    known = _known_sub_names()
    if args.subreddit not in known:
        return _reject_unknown_sub(args.subreddit, known)
    was_paused = db.resume_sub(args.subreddit)
    logger.info("[manage_subs._cmd_resume] resumed '%s'", args.subreddit)
    if was_paused:
        print(f"r/{args.subreddit} снова активен")
    else:
        print(f"r/{args.subreddit} и не был на паузе")
    return 0


def _cmd_list(_args) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # emoji при Windows-консоли в cp1251
    cfg_subs = [sub["name"] for sub in config.load_config()["subreddits"]]
    paused = db.get_paused_subs()
    logger.debug("[manage_subs._cmd_list] cfg_subs=%s paused=%s", cfg_subs, sorted(paused))
    for name in cfg_subs:
        status = "⏸ на паузе" if name in paused else "✅ активен"
        print(f"{name}\t{status}")
    orphaned = sorted(paused - set(cfg_subs))
    for name in orphaned:
        print(f"{name}\t⏸ на паузе (нет в config.yaml)")
    return 0


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reddit Routine subreddit pause CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    pause_parser = subparsers.add_parser("pause")
    pause_parser.add_argument("subreddit")
    pause_parser.set_defaults(func=_cmd_pause)

    resume_parser = subparsers.add_parser("resume")
    resume_parser.add_argument("subreddit")
    resume_parser.set_defaults(func=_cmd_resume)

    subparsers.add_parser("list").set_defaults(func=_cmd_list)

    return parser


def main(argv=None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    logger.debug("[manage_subs.main] command='%s' args=%s", args.command, vars(args))
    try:
        return args.func(args)
    except Exception as exc:
        logger.error("[manage_subs.main] unhandled error in command '%s': %s", args.command, exc)
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
