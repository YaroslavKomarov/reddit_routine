"""Шаг конвейера: валидация JSON-ответа агента.

Читает data/tmp/agent_raw.json (конверт claude --output-format json),
извлекает и валидирует ответ агента; при невалидном JSON — одна повторная
попытка вызова run_agent.sh; при успехе пишет seen_posts через db.py и
data/tmp/digest.json для send_telegram.py. Не импортирует другие шаги —
retry идёт через subprocess, как это делает оркестратор.
"""
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import db

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "DEBUG"),
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("parse_agent_output")

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _raw_path() -> Path:
    override = os.environ.get("AGENT_RAW_PATH")
    return Path(override) if override else _REPO_ROOT / "data" / "tmp" / "agent_raw.json"


def _digest_path() -> Path:
    override = os.environ.get("DIGEST_PATH")
    path = Path(override) if override else _REPO_ROOT / "data" / "tmp" / "digest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _batch_path() -> Path:
    override = os.environ.get("POSTS_BATCH_PATH")
    return Path(override) if override else _REPO_ROOT / "data" / "tmp" / "posts_batch.json"


def _input_path() -> Path:
    override = os.environ.get("AGENT_INPUT_PATH")
    return Path(override) if override else _REPO_ROOT / "data" / "tmp" / "agent_input.json"


def extract_result(raw_envelope: dict) -> tuple:
    """(result, total_cost_usd) из конверта claude --output-format json."""
    result = raw_envelope.get("result")
    if not isinstance(result, str):
        logger.error("[parse_agent_output.extract_result] envelope has no string 'result' field")
        raise ValueError("agent envelope has no string 'result' field")
    cost = raw_envelope.get("total_cost_usd")
    logger.debug("[parse_agent_output.extract_result] result length=%d chars", len(result))
    logger.info("[parse_agent_output.extract_result] total_cost_usd=%s", cost)
    return result, cost


def strip_json_fences(text: str) -> str:
    """Снять ```json-обёртки и текст-болтовню вокруг JSON-объекта.

    Устойчиво к пояснениям до/после: берётся подстрока от первой '{' до
    последней '}'. Если фигурных скобок нет — вернуть как есть, пусть
    json.loads даст осмысленную ошибку.
    """
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        logger.debug("[parse_agent_output.strip_json_fences] no JSON object braces found")
        return text.strip()
    stripped = text[start:end + 1]
    if stripped != text.strip():
        logger.debug(
            "[parse_agent_output.strip_json_fences] stripped %d chars of wrapping around JSON",
            len(text.strip()) - len(stripped),
        )
    return stripped


def _require_str(obj: dict, field: str, path: str, errors: list) -> None:
    value = obj.get(field)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{path}.{field}: обязательная непустая строка")


def validate_digest(parsed: dict, batch_ids: set, promo_state: list) -> list:
    """Схемная валидация ответа агента; вернуть список ошибок (пустой = валидно).

    Промо-нарушения — не ошибки, а WARN: код детерминированно проверяет флаги,
    владелец видит предупреждение и решает сам (система ничего не публикует).
    """
    errors = []
    if not isinstance(parsed, dict):
        errors.append("root: ожидается JSON-объект")
        logger.error("[parse_agent_output.validate_digest] root is not an object")
        return errors

    promo_allowed = {s["subreddit"]: s.get("promo_allowed_today", False) for s in promo_state}
    question_allowed = {s["subreddit"]: s.get("question_posts_allowed", False) for s in promo_state}

    question_post = parsed.get("question_post")
    if question_post is not None:
        if not isinstance(question_post, dict):
            errors.append("question_post: объект или null")
        else:
            for field in ("subreddit", "title", "body"):
                _require_str(question_post, field, "question_post", errors)
            sub = question_post.get("subreddit")
            if isinstance(sub, str) and sub in question_allowed and not question_allowed[sub]:
                logger.warning(
                    "[parse_agent_output.validate_digest] question_post назначен в '%s', "
                    "где question_posts_allowed=false — проверь перед публикацией",
                    sub,
                )

    suggestions = parsed.get("suggestions")
    if not isinstance(suggestions, list):
        errors.append("suggestions: обязательный список")
    else:
        for i, group in enumerate(suggestions):
            gpath = f"suggestions[{i}]"
            if not isinstance(group, dict):
                errors.append(f"{gpath}: ожидается объект")
                continue
            _require_str(group, "subreddit", gpath, errors)
            posts = group.get("posts")
            if not isinstance(posts, list):
                errors.append(f"{gpath}.posts: обязательный список")
                continue
            for j, post in enumerate(posts):
                ppath = f"{gpath}.posts[{j}]"
                if not isinstance(post, dict):
                    errors.append(f"{ppath}: ожидается объект")
                    continue
                for field in ("post_id", "post_title", "post_url", "comment_draft", "why"):
                    _require_str(post, field, ppath, errors)
                if not isinstance(post.get("is_promo"), bool):
                    errors.append(f"{ppath}.is_promo: строго bool (true/false)")
                post_id = post.get("post_id")
                if isinstance(post_id, str) and post_id not in batch_ids:
                    errors.append(f"{ppath}.post_id: '{post_id}' отсутствует во входном батче")
                sub = group.get("subreddit")
                if post.get("is_promo") is True and isinstance(sub, str) and not promo_allowed.get(sub, False):
                    logger.warning(
                        "[parse_agent_output.validate_digest] is_promo=true в '%s' при "
                        "promo_allowed_today=false — пост оставлен, реши сам",
                        sub,
                    )

    skipped = parsed.get("skipped_subs")
    if not isinstance(skipped, list):
        errors.append("skipped_subs: обязательный список")
    else:
        for i, entry in enumerate(skipped):
            spath = f"skipped_subs[{i}]"
            if not isinstance(entry, dict):
                errors.append(f"{spath}: ожидается объект")
                continue
            _require_str(entry, "subreddit", spath, errors)
            _require_str(entry, "reason", spath, errors)

    for error in errors:
        logger.error("[parse_agent_output.validate_digest] schema error: %s", error)
    logger.debug("[parse_agent_output.validate_digest] validation finished, %d error(s): %s", len(errors), errors)
    return errors


_RETRY_NOTE = (
    "Предыдущий ответ не удалось распарсить как JSON. Верни строго один валидный JSON "
    "по схеме, без markdown-обёрток и пояснений."
)


def _read_and_validate(batch_ids: set, promo_state: list) -> tuple:
    """Один проход: raw → extract → strip → parse → validate.

    Возвращает (parsed, cost, errors); parsed/cost — None при фатальной ошибке чтения.
    """
    raw_path = _raw_path()
    try:
        with open(raw_path, "r", encoding="utf-8") as f:
            envelope = json.load(f)
        result, cost = extract_result(envelope)
        parsed = json.loads(strip_json_fences(result))
    except (OSError, ValueError) as exc:  # json.JSONDecodeError — подкласс ValueError
        logger.error("[parse_agent_output._read_and_validate] cannot read agent response from %s: %s", raw_path, exc)
        return None, None, [f"agent response unreadable: {exc}"]
    return parsed, cost, validate_digest(parsed, batch_ids, promo_state)


def main() -> int:
    batch_path = _batch_path()
    try:
        with open(batch_path, "r", encoding="utf-8") as f:
            batch = json.load(f)
    except (OSError, ValueError) as exc:
        logger.error("[parse_agent_output.main] cannot read posts batch from %s: %s", batch_path, exc)
        return 1
    batch_ids = {post["id"] for post in batch}

    input_path = _input_path()
    try:
        with open(input_path, "r", encoding="utf-8") as f:
            promo_state = json.load(f).get("promo_state", [])
    except (OSError, ValueError) as exc:
        logger.error("[parse_agent_output.main] cannot read agent input from %s: %s", input_path, exc)
        return 1

    parsed, cost, errors = _read_and_validate(batch_ids, promo_state)
    if errors:
        logger.info(
            "[parse_agent_output.main] invalid agent response (%d error(s)), retrying once: %s",
            len(errors), errors,
        )
        command = ["bash", str(_REPO_ROOT / "src" / "run_agent.sh")]
        logger.debug("[parse_agent_output.main] retry command: %s", command)
        proc = subprocess.run(command, env={**os.environ, "AGENT_RETRY_NOTE": _RETRY_NOTE})
        logger.debug("[parse_agent_output.main] retry subprocess exit code=%s", proc.returncode)
        if proc.returncode != 0:
            logger.error("[parse_agent_output.main] retry run_agent.sh failed with code %s", proc.returncode)
            return 1
        parsed, cost, errors = _read_and_validate(batch_ids, promo_state)
        if errors:
            logger.error(
                "[parse_agent_output.main] agent response still invalid after retry, giving up: %s",
                errors,
            )
            return 1

    suggested = [
        {
            "post_id": post["post_id"],
            "subreddit": group["subreddit"],
            "title": post["post_title"],
            "url": post["post_url"],
            "was_promo": post["is_promo"],
        }
        for group in parsed["suggestions"]
        for post in group["posts"]
    ]
    inserted = db.mark_posts_seen(suggested)
    logger.info("[parse_agent_output.main] marked %d post(s) as seen (%d suggested)", inserted, len(suggested))

    stats = {
        "cost_usd": cost,
        "posts_fetched": len(batch),
        "posts_suggested": len(suggested),
    }
    digest_path = _digest_path()
    with open(digest_path, "w", encoding="utf-8") as f:
        json.dump({"digest": parsed, "stats": stats}, f, ensure_ascii=False)
    logger.info("[parse_agent_output.main] wrote digest to %s, stats=%s", digest_path, stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
