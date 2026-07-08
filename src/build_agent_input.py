"""Шаг конвейера: сборка входного JSON для агента.

Читает батч постов (data/tmp/posts_batch.json), стейт из db.py
(promo_state, вопрос дня) и файлы context/, собирает agent_input.json
по разделу 5.2 спеки. Не импортирует другие шаги конвейера.
"""
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

import config
import db

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "DEBUG"),
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("build_agent_input")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RULES_PLACEHOLDER = (
    "правила сабреддита не заполнены — предлагать только безопасные немаркетинговые комментарии"
)


def _input_path() -> Path:
    override = os.environ.get("AGENT_INPUT_PATH")
    path = Path(override) if override else _REPO_ROOT / "data" / "tmp" / "agent_input.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _batch_path() -> Path:
    override = os.environ.get("POSTS_BATCH_PATH")
    return Path(override) if override else _REPO_ROOT / "data" / "tmp" / "posts_batch.json"


def _context_dir() -> Path:
    override = os.environ.get("CONTEXT_DIR")
    return Path(override) if override else _REPO_ROOT / "context"


def build_promo_state(subreddits_cfg: list) -> list:
    """promo_state для agent_input: кулдаун из db.py AND promo_allowed из config.

    Саб с promo_allowed: false получает promo_allowed_today: false всегда —
    детерминированное решение кода, агенту уходит готовый флаг.
    Дополнительно прокидывает question_posts_allowed из config, чтобы агент
    знал, куда можно назначить вопрос дня.
    """
    logger.debug("[build_agent_input.build_promo_state] input: %d subreddit(s)", len(subreddits_cfg))
    cooldown_state = db.get_promo_state(
        [(sub["name"], sub["promo_cooldown_days"]) for sub in subreddits_cfg]
    )
    state = []
    for sub_cfg, db_entry in zip(subreddits_cfg, cooldown_state):
        promo_allowed_cfg = bool(sub_cfg.get("promo_allowed", False))
        allowed_today = promo_allowed_cfg and db_entry["promo_allowed_today"]
        logger.debug(
            "[build_agent_input.build_promo_state] subreddit='%s' cooldown_ok=%s config_promo_allowed=%s "
            "promo_allowed_today=%s question_posts_allowed=%s",
            db_entry["subreddit"], db_entry["promo_allowed_today"], promo_allowed_cfg,
            allowed_today, sub_cfg.get("question_posts_allowed", False),
        )
        state.append({
            "subreddit": db_entry["subreddit"],
            "last_promo_days_ago": db_entry["last_promo_days_ago"],
            "promo_allowed_today": allowed_today,
            "question_posts_allowed": bool(sub_cfg.get("question_posts_allowed", False)),
        })
    logger.debug("[build_agent_input.build_promo_state] built promo_state for %d subreddit(s)", len(state))
    return state


def pop_question():
    """Вопрос дня из очереди: {"text", "target_sub"} или None (пустая очередь — штатно)."""
    logger.debug("[build_agent_input.pop_question] popping oldest unused question")
    question = db.pop_oldest_question()
    if question is None:
        logger.info("[build_agent_input.pop_question] question queue is empty, question_of_the_day=null")
        return None
    logger.info("[build_agent_input.pop_question] popped question id=%d", question["id"])
    return {"text": question["text"], "target_sub": question["target_sub"]}


def read_context_files(subreddits_cfg: list) -> tuple:
    """(product, tone, subreddit_rules) из context/.

    product.md и tone.md обязательны: нет или пустые — исключение (шаг падает
    с ненулевым кодом). rules/<sub>.md заполняет владелец, отсутствие — не
    ошибка: подставляется строка-заглушка с WARN.
    """
    context_dir = _context_dir()
    logger.debug("[build_agent_input.read_context_files] context dir: %s", context_dir)

    required = {}
    for name in ("product", "tone"):
        path = context_dir / f"{name}.md"
        if not path.is_file():
            logger.error("[build_agent_input.read_context_files] required file missing: %s", path)
            raise FileNotFoundError(f"required context file missing: {path}")
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            logger.error("[build_agent_input.read_context_files] required file is empty: %s", path)
            raise ValueError(f"required context file is empty: {path}")
        required[name] = text

    if "<!-- TODO" in required["product"]:
        logger.warning(
            "[build_agent_input.read_context_files] product.md выглядит незаполненным шаблоном "
            "(содержит маркеры <!-- TODO) — дайджест будет бессодержательным, заполни context/product.md"
        )

    subreddit_rules = {}
    stubbed = 0
    for sub_cfg in subreddits_cfg:
        sub = sub_cfg["name"]
        rules_path = context_dir / "rules" / f"{sub}.md"
        if rules_path.is_file():
            subreddit_rules[sub] = rules_path.read_text(encoding="utf-8")
        else:
            logger.warning(
                "[build_agent_input.read_context_files] rules file missing for '%s' (%s), using placeholder",
                sub, rules_path,
            )
            subreddit_rules[sub] = _RULES_PLACEHOLDER
            stubbed += 1

    logger.debug(
        "[build_agent_input.read_context_files] loaded rules for %d subreddit(s), %d placeholder(s)",
        len(subreddit_rules), stubbed,
    )
    return required["product"], required["tone"], subreddit_rules


def main() -> int:
    cfg = config.load_config()
    subreddits_cfg = cfg["subreddits"]

    batch_path = _batch_path()
    logger.debug("[build_agent_input.main] batch path: %s", batch_path)
    if not batch_path.is_file():
        logger.error(
            "[build_agent_input.main] batch file missing: %s — fetch_posts.py должен был отработать раньше",
            batch_path,
        )
        return 1
    with open(batch_path, "r", encoding="utf-8") as f:
        posts = json.load(f)

    product, tone, subreddit_rules = read_context_files(subreddits_cfg)

    agent_input = {
        "date": date.today().isoformat(),
        "product": product,
        "tone": tone,
        "subreddit_rules": subreddit_rules,
        "promo_state": build_promo_state(subreddits_cfg),
        "question_of_the_day": pop_question(),
        "posts": posts,
        "selection_config": {
            "posts_per_sub": cfg["selection"]["posts_per_sub"],
            "promo_ratio_target": cfg["selection"]["promo_ratio_target"],
        },
    }

    output_path = _input_path()
    payload = json.dumps(agent_input, ensure_ascii=False)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(payload)
    logger.debug(
        "[build_agent_input.main] wrote %s: %d post(s), %d subreddit rule(s), %d bytes",
        output_path, len(posts), len(subreddit_rules), len(payload.encode("utf-8")),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
