"""Шаг вне ежедневного конвейера: опрос нажатий кнопки «✅ Запостил».

Периодический (cron ~5 минут) самостоятельный скрипт. Читает getUpdates
Telegram Bot API начиная с сохранённого в telegram_state offset, логирует
подтверждённые публикации в promo_history через db.log_promo, отвечает на
callback и убирает нажатую кнопку из клавиатуры сообщения. Ходит ТОЛЬКО в
Telegram Bot API — ни одного запроса к Reddit; это не автопубликация, а
локальный учёт факта «владелец сам запостил». Не импортирует другие шаги
конвейера.
"""
import json
import logging
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

import db

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "DEBUG"),
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("process_promo_callbacks")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RETRY_DELAYS = (2, 8, 30)
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_GETUPDATES_TIMEOUT = 30.0
_SHORT_TIMEOUT = 10.0


class WebhookConflictError(Exception):
    """getUpdates вернул 409 — у бота установлен webhook."""


class TelegramError(Exception):
    """getUpdates не удался после исчерпания ретраев."""


def _get_updates(token: str, offset: int) -> list:
    """GET getUpdates с ретраями (стиль send_telegram.send_message).

    409 Conflict (webhook установлен) НЕ ретраится — фатальная ошибка сразу.
    """
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    params = {
        "offset": offset,
        "timeout": 0,
        "allowed_updates": json.dumps(["callback_query"]),
    }
    attempts = len(_RETRY_DELAYS)
    for attempt in range(1, attempts + 1):
        logger.debug("[process_promo_callbacks._get_updates] attempt=%d offset=%d", attempt, offset)
        try:
            response = requests.get(url, params=params, timeout=_GETUPDATES_TIMEOUT)
        except requests.exceptions.RequestException as exc:
            logger.warning("[process_promo_callbacks._get_updates] attempt=%d network error: %s", attempt, exc)
            if attempt < attempts:
                time.sleep(_RETRY_DELAYS[attempt - 1])
                continue
            raise TelegramError(f"network error after {attempts} attempts: {exc}") from exc

        logger.debug("[process_promo_callbacks._get_updates] attempt=%d status=%d", attempt, response.status_code)
        if response.status_code == 200:
            return response.json().get("result", [])

        if response.status_code == 409:
            logger.error(
                "[process_promo_callbacks._get_updates] 409 Conflict — у бота установлен webhook; "
                "снять webhook: curl https://api.telegram.org/bot<token>/deleteWebhook"
            )
            raise WebhookConflictError("bot has a webhook set, getUpdates cannot be used")

        if response.status_code in _RETRYABLE_STATUSES and attempt < attempts:
            delay = _RETRY_DELAYS[attempt - 1]
            if response.status_code == 429:
                try:
                    retry_after = response.json().get("parameters", {}).get("retry_after")
                except ValueError:
                    retry_after = None
                if isinstance(retry_after, (int, float)) and retry_after > 0:
                    delay = retry_after
            logger.warning("[process_promo_callbacks._get_updates] attempt=%d status=%d, retrying in %ss",
                           attempt, response.status_code, delay)
            time.sleep(delay)
            continue

        raise TelegramError(f"getUpdates failed, status={response.status_code} body={response.text}")

    raise TelegramError("getUpdates exhausted retries")  # unreachable: каждая ветка выше возвращает/райзит


def _answer_callback_query(token: str, callback_query_id: str, text: str) -> None:
    """Не-фатально: сбой ответа на callback не должен ронять прогон (промо уже залогировано)."""
    url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
    try:
        response = requests.post(
            url, data={"callback_query_id": callback_query_id, "text": text}, timeout=_SHORT_TIMEOUT
        )
        if response.status_code != 200:
            logger.warning("[process_promo_callbacks._answer_callback_query] status=%d body=%s",
                           response.status_code, response.text)
    except requests.exceptions.RequestException as exc:
        logger.warning("[process_promo_callbacks._answer_callback_query] network error: %s", exc)


def _edit_message_reply_markup(token: str, chat_id, message_id, reply_markup: dict) -> None:
    """Не-фатально: сбой снятия кнопки не должен ронять прогон. 400 «not modified» — успех."""
    url = f"https://api.telegram.org/bot{token}/editMessageReplyMarkup"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "reply_markup": json.dumps(reply_markup),
    }
    try:
        response = requests.post(url, data=payload, timeout=_SHORT_TIMEOUT)
    except requests.exceptions.RequestException as exc:
        logger.warning("[process_promo_callbacks._edit_message_reply_markup] network error: %s", exc)
        return
    if response.status_code == 200:
        return
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code == 400 and "not modified" in str(body.get("description", "")):
        logger.debug("[process_promo_callbacks._edit_message_reply_markup] message not modified, treating as success")
        return
    logger.warning("[process_promo_callbacks._edit_message_reply_markup] status=%d body=%s",
                   response.status_code, response.text)


def _parse_callback_data(data: str):
    """'promo:<sub>:<post_id>' -> (sub, post_id) | None. Лишние ':' идут в post_id."""
    if not data or not data.startswith("promo:"):
        return None
    rest = data[len("promo:"):]
    parts = rest.split(":", 1)
    if len(parts) != 2:
        return None
    sub, post_id = parts
    if not sub or not post_id:
        return None
    return sub, post_id


def _handle_update(token: str, chat_id_env: str, update: dict, seen_callback_data: set) -> bool:
    """Обработать один апдейт. Возвращает True, если промо залогирован.

    Ожидаемые/невалидные случаи (чужой чат, не-promo callback, дубль) —
    считаются потреблёнными и не поднимают исключение. Непредвиденные
    ошибки (например, сбой db.log_promo) распространяются наверх — вызывающий
    код НЕ продвигает offset за этот апдейт, апдейт повторится в следующем прогоне.
    """
    update_id = update.get("update_id")
    callback_query = update.get("callback_query")
    if not callback_query:
        logger.debug("[process_promo_callbacks._handle_update] update %s has no callback_query, skipping", update_id)
        return False

    message = callback_query.get("message")
    chat_id = None
    if message is not None:
        chat_id = message.get("chat", {}).get("id")
        if chat_id is None or str(chat_id) != chat_id_env.strip():
            logger.warning("[process_promo_callbacks._handle_update] update %s from foreign/unknown chat %s, ignoring",
                           update_id, chat_id)
            return False

    data = callback_query.get("data", "") or ""
    parsed = _parse_callback_data(data)
    if parsed is None:
        logger.debug("[process_promo_callbacks._handle_update] update %s data %r not a valid promo callback, skipping",
                     update_id, data)
        return False
    sub, post_id = parsed

    callback_id = callback_query.get("id")
    if data in seen_callback_data:
        logger.info("[process_promo_callbacks._handle_update] duplicate callback_data %r in batch, skipping log_promo",
                    data)
        _answer_callback_query(token, callback_id, "Уже залогировано")
        return False
    seen_callback_data.add(data)

    post_url = f"https://www.reddit.com/comments/{post_id}"
    db.log_promo(sub, "comment_promo", post_url=post_url)
    logger.info("[process_promo_callbacks._handle_update] logged promo sub='%s' post_id='%s'", sub, post_id)
    _answer_callback_query(token, callback_id, "Залогировано ✅")

    if message is None:
        logger.warning("[process_promo_callbacks._handle_update] update %s has no message, skipping edit", update_id)
        return True

    reply_markup = message.get("reply_markup") or {}
    rows = reply_markup.get("inline_keyboard") or []
    new_rows = [[btn for btn in row if btn.get("callback_data") != data] for row in rows]
    new_rows = [row for row in new_rows if row]
    _edit_message_reply_markup(token, chat_id, message.get("message_id"), {"inline_keyboard": new_rows})
    return True


def main(argv=None) -> int:
    load_dotenv(_REPO_ROOT / ".env")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id_env = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id_env:
        logger.error("[process_promo_callbacks.main] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID обязательны")
        return 1

    offset = db.get_telegram_offset()
    try:
        updates = _get_updates(token, offset + 1)
    except WebhookConflictError as exc:
        logger.error("[process_promo_callbacks.main] %s", exc)
        return 1
    except TelegramError as exc:
        logger.error("[process_promo_callbacks.main] getUpdates failed: %s", exc)
        return 1

    if not updates:
        logger.debug("[process_promo_callbacks.main] no updates")
        return 0

    logger.debug("[process_promo_callbacks.main] offset=%d received=%d updates", offset, len(updates))

    watermark = offset
    seen_callback_data = set()
    processed = 0
    logged = 0
    exit_code = 0
    try:
        for update in sorted(updates, key=lambda u: u["update_id"]):
            update_id = update["update_id"]
            if _handle_update(token, chat_id_env, update, seen_callback_data):
                logged += 1
            processed += 1
            watermark = update_id
    except Exception as exc:
        logger.error("[process_promo_callbacks.main] unexpected error while processing updates: %s", exc)
        exit_code = 1
    finally:
        db.set_telegram_offset(watermark)
        logger.info("[process_promo_callbacks.main] processed=%d logged=%d new_offset=%d",
                    processed, logged, watermark)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
