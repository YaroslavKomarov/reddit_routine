"""Шаг вне ежедневного конвейера: опрос нажатий кнопки «✅ Запостил».

Long-polling демон под systemd (без аргументов) либо один проход для отладки
(`--once`). Читает getUpdates Telegram Bot API начиная с сохранённого в
telegram_state offset, логирует подтверждённые публикации в promo_history
через db.log_promo, отвечает на callback и убирает нажатую кнопку из
клавиатуры сообщения. Ходит ТОЛЬКО в Telegram Bot API — ни одного запроса к
Reddit; это не автопубликация, а локальный учёт факта «владелец сам
запостил». Не импортирует другие шаги конвейера.
"""
import argparse
import json
import logging
import os
import signal
import sys
import time
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
# urllib3 на DEBUG печатает полный URL запроса вида /bot<token>/... — токен
# утёк бы в логи; глушим только сторонний логгер, свои DEBUG-строки остаются
logging.getLogger("urllib3").setLevel(logging.INFO)
logger = logging.getLogger("process_promo_callbacks")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RETRY_DELAYS = (2, 8, 30)
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_GETUPDATES_TIMEOUT = 30.0  # минимальный HTTP-таймаут (--once, poll_timeout=0)
_GETUPDATES_HTTP_MARGIN = 10.0  # запас HTTP-таймаута сверх long-poll таймаута
_SHORT_TIMEOUT = 10.0
_POLL_TIMEOUT_DAEMON = 50  # long-poll таймаут getUpdates в демон-режиме, сек
_BACKOFF_INITIAL = 10  # пауза после сетевой ошибки в демон-режиме, сек
_BACKOFF_MAX = 60

# Выставляется сигнальным хендлером; страховка на случай прихода сигнала
# вне блокирующего вызова — цикл демона проверяет перед каждой итерацией
_shutdown_signum = None


class WebhookConflictError(Exception):
    """getUpdates вернул 409 — у бота установлен webhook."""


class TelegramError(Exception):
    """getUpdates не удался после исчерпания ретраев."""


class _Shutdown(BaseException):
    """Поднимается сигнальным хендлером для graceful shutdown.

    Наследник BaseException, а не Exception: PEP 475 перезапускает блокирующий
    requests.get после возврата из хендлера, поэтому «тихий флаг» не прервал бы
    висящий long poll до ~60с; исключение прерывает его сразу, и requests его
    не перехватывает.
    """

    def __init__(self, signum):
        super().__init__(signum)
        self.signum = signum


def _signal_handler(signum, frame):
    global _shutdown_signum
    _shutdown_signum = signum
    raise _Shutdown(signum)


def _get_updates(token: str, offset: int, poll_timeout: int = 0) -> list:
    """GET getUpdates с ретраями (стиль send_telegram.send_message).

    poll_timeout — long-poll таймаут Telegram (0 = мгновенный ответ);
    HTTP-таймаут requests всегда берётся с запасом сверх него.
    409 Conflict (webhook установлен) НЕ ретраится — фатальная ошибка сразу.
    """
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    params = {
        "offset": offset,
        "timeout": poll_timeout,
        "allowed_updates": json.dumps(["callback_query", "message"]),
    }
    http_timeout = max(_GETUPDATES_TIMEOUT, poll_timeout + _GETUPDATES_HTTP_MARGIN)
    attempts = len(_RETRY_DELAYS)
    for attempt in range(1, attempts + 1):
        logger.debug("[process_promo_callbacks._get_updates] attempt=%d offset=%d", attempt, offset)
        try:
            response = requests.get(url, params=params, timeout=http_timeout)
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


def _send_message(token: str, chat_id, text: str) -> None:
    """Не-фатально: сбой отправки ответа не должен ронять прогон (стиль _answer_callback_query)."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        response = requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=_SHORT_TIMEOUT)
        if response.status_code != 200:
            logger.warning("[process_promo_callbacks._send_message] status=%d body=%s",
                           response.status_code, response.text)
    except requests.exceptions.RequestException as exc:
        logger.warning("[process_promo_callbacks._send_message] network error: %s", exc)


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


_SUBS_COMMANDS = ("/subs", "/pause", "/resume", "/help", "/start")

_HELP_TEXT = (
    "Команды:\n"
    "/subs — статус всех сабреддитов (⏸ на паузе / ✅ активен)\n"
    "/pause <sub> — поставить сабреддит на паузу (действует со следующего прогона)\n"
    "/resume <sub> — снять сабреддит с паузы\n"
    "/help — эта подсказка"
)


def _format_subs_status() -> str:
    """Список сабов из config.yaml со статусом ⏸/✅; паузнутые вне config.yaml — отдельной строкой."""
    cfg_subs = [sub["name"] for sub in config.load_config()["subreddits"]]
    paused = db.get_paused_subs()
    lines = [f"{name}: {'⏸ на паузе' if name in paused else '✅ активен'}" for name in cfg_subs]
    for name in sorted(paused - set(cfg_subs)):
        lines.append(f"{name}: ⏸ на паузе (нет в config.yaml)")
    return "\n".join(lines) if lines else "сабреддиты не настроены"


def _handle_message(token: str, chat_id_env: str, update: dict) -> bool:
    """Обработать текстовое сообщение с командой /subs, /pause, /resume, /help.

    Возвращает True, если команда исполнена. Невалидные/чужие/нерелевантные
    сообщения — молча потребляются (False), не поднимают исключение. Сбой
    db.pause_sub/resume_sub распространяется наверх — как у log_promo, апдейт
    не потребляется и повторится.
    """
    update_id = update.get("update_id")
    message = update.get("message")
    text = message.get("text") if message else None
    if not message or not text:
        logger.debug("[process_promo_callbacks._handle_message] update %s has no message/text, skipping", update_id)
        return False

    chat_id = message.get("chat", {}).get("id")
    if chat_id is None or str(chat_id) != chat_id_env.strip():
        logger.warning("[process_promo_callbacks._handle_message] update %s from foreign/unknown chat %s, ignoring",
                       update_id, chat_id)
        return False

    parts = text.strip().split(maxsplit=1)
    command = parts[0].split("@", 1)[0]
    arg = parts[1].strip() if len(parts) > 1 else None

    if command not in _SUBS_COMMANDS:
        logger.debug("[process_promo_callbacks._handle_message] update %s text %r not a subs command, skipping",
                     update_id, text)
        return False

    if command in ("/help", "/start"):
        logger.info("[process_promo_callbacks._handle_message] command=%s", command)
        _send_message(token, chat_id, _HELP_TEXT)
        return True

    if command == "/subs":
        logger.info("[process_promo_callbacks._handle_message] command=/subs")
        _send_message(token, chat_id, _format_subs_status())
        return True

    if not arg:
        _send_message(token, chat_id, f"использование: {command} <subreddit>")
        return True

    known = {sub["name"] for sub in config.load_config()["subreddits"]}
    if arg not in known:
        logger.warning("[process_promo_callbacks._handle_message] command=%s unknown subreddit '%s'", command, arg)
        _send_message(token, chat_id, f"неизвестный сабреддит '{arg}'\nизвестные: {', '.join(sorted(known))}")
        return True

    if command == "/pause":
        paused_now = db.pause_sub(arg)
        result = "paused" if paused_now else "already_paused"
        reply = f"⏸ r/{arg} на паузе — действует со следующего прогона" if paused_now else f"r/{arg} уже на паузе"
    else:
        was_paused = db.resume_sub(arg)
        result = "resumed" if was_paused else "was_not_paused"
        reply = f"✅ r/{arg} снова активен" if was_paused else f"r/{arg} и не был на паузе"
    logger.info("[process_promo_callbacks._handle_message] command=%s sub='%s' result=%s", command, arg, result)
    _send_message(token, chat_id, reply)
    return True


def _handle_update(token: str, chat_id_env: str, update: dict, seen_callback_data: set) -> bool:
    """Диспетчер: callback_query -> _handle_callback, message -> _handle_message.

    Возвращает True, если промо залогирован или TG-команда исполнена.
    """
    update_id = update.get("update_id")
    if update.get("callback_query"):
        return _handle_callback(token, chat_id_env, update, seen_callback_data)
    if update.get("message"):
        return _handle_message(token, chat_id_env, update)
    logger.debug("[process_promo_callbacks._handle_update] update %s has no callback_query/message, skipping",
                 update_id)
    return False


def _handle_callback(token: str, chat_id_env: str, update: dict, seen_callback_data: set) -> bool:
    """Обработать один callback_query-апдейт. Возвращает True, если промо залогирован.

    Ожидаемые/невалидные случаи (чужой чат, не-promo callback, дубль) —
    считаются потреблёнными и не поднимают исключение. Непредвиденные
    ошибки (например, сбой db.log_promo) распространяются наверх — вызывающий
    код НЕ продвигает offset за этот апдейт, апдейт повторится в следующем прогоне.
    """
    update_id = update.get("update_id")
    callback_query = update.get("callback_query")

    message = callback_query.get("message")
    chat_id = None
    if message is not None:
        chat_id = message.get("chat", {}).get("id")
        if chat_id is None or str(chat_id) != chat_id_env.strip():
            logger.warning("[process_promo_callbacks._handle_callback] update %s from foreign/unknown chat %s, ignoring",
                           update_id, chat_id)
            return False

    data = callback_query.get("data", "") or ""
    parsed = _parse_callback_data(data)
    if parsed is None:
        logger.debug("[process_promo_callbacks._handle_callback] update %s data %r not a valid promo callback, skipping",
                     update_id, data)
        return False
    sub, post_id = parsed

    callback_id = callback_query.get("id")
    if data in seen_callback_data:
        logger.info("[process_promo_callbacks._handle_callback] duplicate callback_data %r in batch, skipping log_promo",
                    data)
        _answer_callback_query(token, callback_id, "Уже залогировано")
        return False
    seen_callback_data.add(data)

    post_url = f"https://www.reddit.com/comments/{post_id}"
    db.log_promo(sub, "comment_promo", post_url=post_url)
    logger.info("[process_promo_callbacks._handle_callback] logged promo sub='%s' post_id='%s'", sub, post_id)
    _answer_callback_query(token, callback_id, "Залогировано ✅")

    if message is None:
        logger.warning("[process_promo_callbacks._handle_callback] update %s has no message, skipping edit", update_id)
        return True

    reply_markup = message.get("reply_markup") or {}
    rows = reply_markup.get("inline_keyboard") or []
    new_rows = [[btn for btn in row if btn.get("callback_data") != data] for row in rows]
    new_rows = [row for row in new_rows if row]
    _edit_message_reply_markup(token, chat_id, message.get("message_id"), {"inline_keyboard": new_rows})
    return True


def run_iteration(token: str, chat_id_env: str, poll_timeout: int) -> int:
    """Одна итерация: getUpdates → обработка батча → продвижение offset.

    WebhookConflictError/TelegramError из getUpdates распространяются наверх —
    режимы (--once/демон) решают, фатальны ли они. Ошибка обработки апдейта
    НЕ потребляет его: offset-watermark продвигается только за успешно
    обработанные апдейты, упавший повторится в следующей итерации.
    """
    offset = db.get_telegram_offset()
    updates = _get_updates(token, offset + 1, poll_timeout)

    if not updates:
        logger.debug("[process_promo_callbacks.run_iteration] no updates")
        return 0

    logger.debug("[process_promo_callbacks.run_iteration] offset=%d received=%d updates", offset, len(updates))

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
        logger.error("[process_promo_callbacks.run_iteration] unexpected error while processing updates: %s", exc)
        exit_code = 1
    finally:
        db.set_telegram_offset(watermark)
        logger.info("[process_promo_callbacks.run_iteration] processed=%d logged=%d new_offset=%d",
                    processed, logged, watermark)

    return exit_code


def _run_daemon(token: str, chat_id_env: str) -> int:
    """Бесконечный long-polling цикл. Выход: 0 по сигналу, 1 по 409.

    Сетевые ошибки (TelegramError) не фатальны — WARNING + нарастающий
    backoff; 409 фатален: рестарт демона его не лечит, это сигнал владельцу
    в journald снять webhook.
    """
    logger.info("[process_promo_callbacks._run_daemon] daemon started: poll_timeout=%d start_offset=%d",
                _POLL_TIMEOUT_DAEMON, db.get_telegram_offset())
    backoff = _BACKOFF_INITIAL
    try:
        while _shutdown_signum is None:
            try:
                run_iteration(token, chat_id_env, _POLL_TIMEOUT_DAEMON)
                backoff = _BACKOFF_INITIAL
            except WebhookConflictError as exc:
                logger.error("[process_promo_callbacks._run_daemon] %s — рестарт не поможет, снять webhook", exc)
                return 1
            except TelegramError as exc:
                logger.warning("[process_promo_callbacks._run_daemon] getUpdates failed: %s — retry in %ds",
                               exc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX)
    except _Shutdown as exc:
        # offset уже сохранён finally-блоком итерации — просто выходим чисто
        logger.info("[process_promo_callbacks._run_daemon] received signal %s, final_offset=%d, shutting down",
                    exc.signum, db.get_telegram_offset())
        return 0
    logger.info("[process_promo_callbacks._run_daemon] shutdown flag set (signal %s), final_offset=%d",
                _shutdown_signum, db.get_telegram_offset())
    return 0


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        # без эмодзи: argparse печатает help в консоль, на Windows (cp1251)
        # символ вне кодировки роняет --help с UnicodeEncodeError
        description="Опрос нажатий кнопки «Запостил» (Telegram Bot API getUpdates)."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="один проход getUpdates (timeout=0) и выход — для отладки и тестов",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    load_dotenv(_REPO_ROOT / ".env")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id_env = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id_env:
        logger.error("[process_promo_callbacks.main] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID обязательны")
        return 1

    if args.once:
        logger.info("[process_promo_callbacks.main] once mode: start_offset=%d", db.get_telegram_offset())
        try:
            return run_iteration(token, chat_id_env, poll_timeout=0)
        except WebhookConflictError as exc:
            logger.error("[process_promo_callbacks.main] %s", exc)
            return 1
        except TelegramError as exc:
            logger.error("[process_promo_callbacks.main] getUpdates failed: %s", exc)
            return 1

    # Демон-режим: хендлеры только здесь, не на уровне модуля — импорт в тестах
    # остаётся чистым (signal.signal работает и на Windows для SIGTERM/SIGINT)
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    return _run_daemon(token, chat_id_env)


if __name__ == "__main__":
    sys.exit(main())
