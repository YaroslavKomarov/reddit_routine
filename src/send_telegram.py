"""Шаг конвейера: форматирование и отправка дайджеста в Telegram.

Читает data/tmp/digest.json (выход parse_agent_output.py) и queue_stats()
из db.py, строит HTML-сообщения (parse_mode=HTML, лимит 4096 символов),
отправляет их через Telegram Bot API. Режимы: --dry-run (печать в stdout,
без сети и без run_log) и --error MSG (короткое уведомление об ошибке,
digest.json не читается). Не импортирует другие шаги конвейера.
"""
import argparse
import html
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

import config
import db

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "DEBUG"),
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("send_telegram")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TG_LIMIT = 4096
_RETRY_DELAYS = (2, 8, 30)
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_MESSAGE_PAUSE_SECONDS = 1  # лимит Telegram ~1 msg/sec

_TAG_RE = re.compile(r"<(/?)([a-z]+)(?:\s[^>]*)?>")


def _digest_path() -> Path:
    override = os.environ.get("DIGEST_PATH")
    return Path(override) if override else _REPO_ROOT / "data" / "tmp" / "digest.json"


# --- чистые функции форматирования (тестируются без сети) ---------------------

def escape(text) -> str:
    """Экранировать пользовательский текст для parse_mode=HTML (&, <, >)."""
    return html.escape(str(text), quote=False)


def format_question_post(qp, queue_unused: int) -> str:
    """Сообщение 1: пост дня из очереди вопросов (или его отсутствие)."""
    logger.debug("[send_telegram.format_question_post] qp=%s queue_unused=%s",
                 "present" if qp else None, queue_unused)
    if qp is None:
        return (
            "📝 Сегодня без поста дня — очередь вопросов пуста или пост не выбран.\n\n"
            f"Вопросов в очереди: {queue_unused}"
        )
    lines = [
        f"📝 Пост дня → r/{escape(qp.get('subreddit', ''))}",
        "",
        f"<b>{escape(qp.get('title', ''))}</b>",
        "",
        escape(qp.get("body", "")),
    ]
    notes = qp.get("notes")
    if isinstance(notes, str) and notes.strip():
        lines += ["", f"<i>{escape(notes)}</i>"]
    return "\n".join(lines)


def format_subreddit_message(group: dict) -> str:
    """Сообщение по сабреддиту: заголовок + блок на каждый предложенный пост."""
    sub = group.get("subreddit", "")
    posts = group.get("posts") or []
    logger.debug("[send_telegram.format_subreddit_message] sub='%s' posts=%d", sub, len(posts))
    blocks = [f"💬 r/{escape(sub)}"]
    for post in posts:
        url = html.escape(str(post.get("post_url", "")), quote=True)
        title_line = f'<a href="{url}">{escape(post.get("post_title", ""))}</a>'
        if post.get("is_promo") is True:
            title_line += " 🔥"
        blocks.append(
            title_line
            + f"\n<blockquote>{escape(post.get('comment_draft', ''))}</blockquote>"
            + f"\n<i>{escape(post.get('why', ''))}</i>"
        )
    return "\n\n".join(blocks)


def format_stats(stats: dict, queue_stats: dict, skipped_subs: list, has_promo: bool) -> str:
    """Финальное сообщение: итоги прогона, пропуски, напоминание про промо."""
    logger.debug("[send_telegram.format_stats] stats=%s queue_stats=%s skipped=%d has_promo=%s",
                 stats, queue_stats, len(skipped_subs), has_promo)
    cost = stats.get("cost_usd")
    cost_str = f"${cost:.4f}" if isinstance(cost, (int, float)) else "н/д"
    lines = [
        "📊 Итоги прогона",
        "",
        f"Постов собрано: {stats.get('posts_fetched', 'н/д')}",
        f"Постов предложено: {stats.get('posts_suggested', 'н/д')}",
        f"Стоимость: {cost_str}",
        f"Вопросов в очереди: {queue_stats.get('unused', 'н/д')}",
    ]
    if skipped_subs:
        lines += ["", "⏭ Пропущенные сабреддиты:"]
        for entry in skipped_subs:
            lines.append(f"— r/{escape(entry.get('subreddit', '?'))}: {escape(entry.get('reason', ''))}")
    if has_promo:
        lines += ["", "🔥 " + escape(
            "Запостил промо — жми кнопку «✅ Запостил» под постом; "
            "fallback: python src/question_queue.py log-promo <sub> comment_promo"
        )]
    return "\n".join(lines)


_MAX_CALLBACK_DATA_BYTES = 64
_BUTTON_TITLE_MAX_CHARS = 30


def build_promo_keyboard(group: dict):
    """Инлайн-клавиатура «✅ Запостил» для промо-постов сабреддита, или None."""
    sub = group.get("subreddit", "")
    posts = group.get("posts") or []
    rows = []
    for post in posts:
        if post.get("is_promo") is not True:
            continue
        post_id = post.get("post_id", "")
        callback_data = f"promo:{sub}:{post_id}"
        callback_bytes = len(callback_data.encode("utf-8"))
        if callback_bytes > _MAX_CALLBACK_DATA_BYTES:
            logger.warning(
                "[send_telegram.build_promo_keyboard] callback_data %d bytes exceeds %d, "
                "skipping button (sub='%s' post_id='%s')",
                callback_bytes, _MAX_CALLBACK_DATA_BYTES, sub, post_id,
            )
            continue
        title = str(post.get("post_title", ""))[:_BUTTON_TITLE_MAX_CHARS]
        rows.append([{"text": f"✅ Запостил: {title}", "callback_data": callback_data}])
        logger.debug(
            "[send_telegram.build_promo_keyboard] sub='%s' post_id='%s' callback_bytes=%d",
            sub, post_id, callback_bytes,
        )
    if not rows:
        return None
    return {"inline_keyboard": rows}


def _chunks_with_keyboard(text: str, keyboard) -> list:
    """split_message(text) как пары (chunk, reply_markup); клавиатура — на последнем chunk."""
    chunks = [(chunk, None) for chunk in split_message(text)]
    if keyboard and chunks:
        last_text, _ = chunks[-1]
        chunks[-1] = (last_text, keyboard)
    return chunks


def _unclosed_tags(text: str) -> list:
    """Стек открытых, но не закрытых HTML-тегов в тексте."""
    stack = []
    for match in _TAG_RE.finditer(text):
        closing, name = match.group(1), match.group(2)
        if closing:
            if stack and stack[-1] == name:
                stack.pop()
        else:
            stack.append(name)
    return stack


def _truncate_block(block: str, limit: int) -> str:
    """Жёстко обрезать неделимый блок до limit, не порвав HTML-теги и entity."""
    cut = limit - 1  # минимум один символ уходит на маркер …
    while cut > 0:
        truncated = block[:cut]
        last_open = truncated.rfind("<")
        if last_open > truncated.rfind(">"):
            truncated = truncated[:last_open]  # не резать внутри тега
        last_amp = truncated.rfind("&")
        if last_amp != -1 and len(truncated) - last_amp < 8 and ";" not in truncated[last_amp:]:
            truncated = truncated[:last_amp]  # не резать внутри &amp;-entity
        closers = "".join(f"</{name}>" for name in reversed(_unclosed_tags(truncated)))
        candidate = f"{truncated}…{closers}"
        if len(candidate) <= limit:
            logger.debug("[send_telegram._truncate_block] %d -> %d chars (closers=%r)",
                         len(block), len(candidate), closers)
            return candidate
        cut -= len(candidate) - limit
    return "…"


def split_message(text: str, limit: int = _TG_LIMIT) -> list:
    """Разбить сообщение по границам блоков (\\n\\n); блок — неделимая единица.

    Продолжение сообщения сабреддита получает заголовок «💬 r/{sub} (продолжение)».
    Блок длиннее limit жёстко обрезается с маркером … и валидным HTML.
    """
    logger.debug("[send_telegram.split_message] len=%d limit=%d", len(text), limit)
    if len(text) <= limit:
        return [text]

    first_line = text.split("\n", 1)[0]
    cont_header = f"{first_line} (продолжение)" if first_line.startswith("💬 r/") else None
    blocks = text.split("\n\n")
    logger.debug("[send_telegram.split_message] %d block(s)", len(blocks))

    chunks = []
    current = ""
    for block in blocks:
        if len(block) > limit:
            logger.warning("[send_telegram.split_message] block of %d chars exceeds limit %d, truncating",
                           len(block), limit)
            block = _truncate_block(block, limit)
        candidate = block if not current else f"{current}\n\n{block}"
        if len(candidate) <= limit:
            current = candidate
            continue
        chunks.append(current)
        if cont_header:
            current = f"{cont_header}\n\n{block}"
            if len(current) > limit:
                current = f"{cont_header}\n\n{_truncate_block(block, limit - len(cont_header) - 2)}"
        else:
            current = block
    if current:
        chunks.append(current)

    logger.debug("[send_telegram.split_message] produced %d chunk(s), lengths=%s",
                 len(chunks), [len(c) for c in chunks])
    return chunks


def build_messages(digest: dict, stats: dict, queue_stats: dict, split_by_subreddit: bool) -> list:
    """Полный список пар (text, reply_markup | None) дайджеста в порядке отправки."""
    logger.debug("[send_telegram.build_messages] split_by_subreddit=%s", split_by_subreddit)
    messages = [(format_question_post(digest.get("question_post"), queue_stats.get("unused", 0)), None)]

    groups = digest.get("suggestions") or []
    has_promo = any(
        post.get("is_promo") is True
        for group in groups
        for post in (group.get("posts") or [])
    )
    if split_by_subreddit:
        for group in groups:
            text = format_subreddit_message(group)
            keyboard = build_promo_keyboard(group)
            messages.extend(_chunks_with_keyboard(text, keyboard))
    elif groups:
        combined_text = "\n\n".join(format_subreddit_message(group) for group in groups)
        combined_rows = []
        for group in groups:
            keyboard = build_promo_keyboard(group)
            if keyboard:
                combined_rows.extend(keyboard["inline_keyboard"])
        combined_keyboard = {"inline_keyboard": combined_rows} if combined_rows else None
        messages.extend(_chunks_with_keyboard(combined_text, combined_keyboard))

    messages.append((format_stats(stats, queue_stats, digest.get("skipped_subs") or [], has_promo), None))
    logger.info("[send_telegram.build_messages] built %d message(s)", len(messages))
    return messages


# --- отправка ------------------------------------------------------------------

_MASKED_URL = "https://api.telegram.org/bot***/sendMessage"


def send_message(token: str, chat_id: str, text: str, timeout: float = 30.0, reply_markup: dict = None) -> bool:
    """Отправить одно сообщение через Bot API; ретраи как в fetch_posts.

    Отличие от fetch: при 429 пауза берётся из parameters.retry_after
    JSON-тела ответа Telegram (fallback — задержка из _RETRY_DELAYS).
    Токен в логи не попадает — URL всегда маскируется.
    """
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup)
    attempts = len(_RETRY_DELAYS)
    for attempt in range(1, attempts + 1):
        logger.debug("[send_telegram.send_message] attempt=%d url=%s text_len=%d",
                     attempt, _MASKED_URL, len(text))
        try:
            response = requests.post(url, data=payload, timeout=timeout)
        except requests.exceptions.RequestException as exc:
            logger.warning("[send_telegram.send_message] attempt=%d network error: %s", attempt, exc)
            if attempt < attempts:
                time.sleep(_RETRY_DELAYS[attempt - 1])
                continue
            logger.error("[send_telegram.send_message] exhausted %d attempts, last error: %s",
                         attempts, exc)
            return False

        logger.debug("[send_telegram.send_message] attempt=%d status=%d", attempt, response.status_code)
        if response.status_code == 200:
            return True

        if response.status_code in _RETRYABLE_STATUSES and attempt < attempts:
            delay = _RETRY_DELAYS[attempt - 1]
            if response.status_code == 429:
                try:
                    retry_after = response.json().get("parameters", {}).get("retry_after")
                except ValueError:
                    retry_after = None
                if isinstance(retry_after, (int, float)) and retry_after > 0:
                    delay = retry_after
            logger.warning("[send_telegram.send_message] attempt=%d status=%d, retrying in %ss",
                           attempt, response.status_code, delay)
            time.sleep(delay)
            continue

        logger.error("[send_telegram.send_message] send failed, status=%d body=%s",
                     response.status_code, response.text)
        return False

    return False  # unreachable: каждый исход выше возвращает результат на последней попытке


def _send_all(token: str, chat_id: str, messages: list) -> bool:
    for index, (message, reply_markup) in enumerate(messages, 1):
        logger.info("[send_telegram._send_all] sending message %d/%d, len=%d, has_keyboard=%s",
                    index, len(messages), len(message), reply_markup is not None)
        if not send_message(token, chat_id, message, reply_markup=reply_markup):
            logger.error("[send_telegram._send_all] message %d/%d failed, aborting", index, len(messages))
            return False
        if index < len(messages):
            time.sleep(_MESSAGE_PAUSE_SECONDS)
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Отправка Reddit-дайджеста в Telegram")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--error", metavar="MSG", default=None,
                      help="отправить короткое уведомление об ошибке (digest.json не читается)")
    mode.add_argument("--dry-run", action="store_true",
                      help="напечатать сообщения в stdout вместо отправки, без записи в run_log")
    args = parser.parse_args(argv)

    # load_dotenv не перезаписывает уже установленные env — при запуске
    # из run_daily.sh (source .env) значения останутся прежними.
    load_dotenv(_REPO_ROOT / ".env")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    if args.error is not None:
        if not token or not chat_id:
            logger.error("[send_telegram.main] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID обязательны для --error")
            return 1
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        text = f"⚠️ Reddit Routine: {escape(args.error)}\n{date_str}"
        logger.info("[send_telegram.main] sending error notification, len=%d", len(text))
        return 0 if send_message(token, chat_id, text) else 1

    digest_path = _digest_path()
    try:
        with open(digest_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError) as exc:
        logger.error("[send_telegram.main] cannot read digest from %s: %s", digest_path, exc)
        return 1
    digest = payload.get("digest")
    stats = payload.get("stats") or {}
    if not isinstance(digest, dict):
        logger.error("[send_telegram.main] digest.json has no 'digest' object")
        return 1

    queue_stats = db.queue_stats()
    split_by_subreddit = bool(config.load_config().get("telegram", {}).get("split_by_subreddit", True))
    messages = build_messages(digest, stats, queue_stats, split_by_subreddit)

    if args.dry_run:
        logger.info("[send_telegram.main] dry-run: printing %d message(s) to stdout", len(messages))
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")  # emoji при Windows-консоли в cp1251
        for index, (message, reply_markup) in enumerate(messages, 1):
            print(f"--- message {index}/{len(messages)} ---")
            print(message)
            if reply_markup:
                button_texts = [btn["text"] for row in reply_markup["inline_keyboard"] for btn in row]
                print(f"[кнопки: {', '.join(button_texts)}]")
        return 0

    if not token or not chat_id:
        logger.error("[send_telegram.main] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID обязательны для отправки")
        return 1
    if not _send_all(token, chat_id, messages):
        return 1

    db.log_run(
        "ok",
        posts_fetched=stats.get("posts_fetched"),
        posts_suggested=stats.get("posts_suggested"),
        cost_usd=stats.get("cost_usd"),
    )
    logger.info("[send_telegram.main] digest sent, %d message(s), run logged as ok", len(messages))
    return 0


if __name__ == "__main__":
    sys.exit(main())
