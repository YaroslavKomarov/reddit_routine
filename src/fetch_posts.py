"""Шаг конвейера: сбор свежих постов из отслеживаемых сабреддитов.

Читает config.yaml (через config.py) и seen_posts (через db.py), анонимно
тянет публичный Atom-фид `https://www.reddit.com/r/{sub}/new/.rss` по каждому
сабреддиту (без OAuth, без credentials), фильтрует и пишет батч в
data/tmp/posts_batch.json. Не импортирует другие шаги конвейера.

Публичный анонимный бюджет rate limit жёсткий (~1 запрос на ~30-секундное
окно), поэтому полный прогон по 8 сабреддитам занимает ориентировочно ~4
минуты — между запросами выдерживается пауза по заголовкам `x-ratelimit-*`.
"""
import json
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

import requests

import config
import db

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "DEBUG"),
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
# urllib3 на DEBUG печатает полный URL каждого запроса; для единообразия с
# Telegram-шагами (там URL содержит токен бота) глушим сторонний логгер
logging.getLogger("urllib3").setLevel(logging.INFO)
logger = logging.getLogger("fetch_posts")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RETRY_DELAYS = (2, 8, 30)
_SUB_PAUSE_SECONDS = 2
_SELFTEXT_MAX_LEN = 2000
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
_ID_PREFIX = "t3_"
_SUBMITTED_BY_RE = re.compile(r"submitted by", re.IGNORECASE)


def _batch_path() -> Path:
    override = os.environ.get("POSTS_BATCH_PATH")
    path = Path(override) if override else _REPO_ROOT / "data" / "tmp" / "posts_batch.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class _TextExtractor(HTMLParser):
    """Собирает текстовые узлы HTML, разэкранируя сущности силами stdlib."""

    def __init__(self):
        super().__init__()
        self.chunks = []

    def handle_data(self, data):
        self.chunks.append(data)

    def text(self) -> str:
        return "".join(self.chunks)


def _extract_selftext(content_html: str) -> str:
    parser = _TextExtractor()
    parser.feed(content_html)
    text = parser.text()
    match = _SUBMITTED_BY_RE.search(text)
    if match:
        text = text[:match.start()]
    return text.strip()[:_SELFTEXT_MAX_LEN]


def fetch_subreddit_feed(sub: str, limit: int, user_agent: str, timeout: float = 10.0):
    """Анонимный запрос публичного Atom-фида. (text, rate_info) при успехе, иначе None.

    rate_info — словарь с ключами remaining/reset из заголовков
    x-ratelimit-remaining/x-ratelimit-reset ответа (могут отсутствовать).
    """
    url = f"https://www.reddit.com/r/{sub}/new/.rss?limit={limit}"
    headers = {"User-Agent": user_agent}
    attempts = len(_RETRY_DELAYS)
    last_status = None
    for attempt in range(1, attempts + 1):
        logger.debug("[fetch_posts.fetch_subreddit_feed] sub='%s' attempt=%d url=%s", sub, attempt, url)
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
        except requests.exceptions.RequestException as exc:
            logger.debug("[fetch_posts.fetch_subreddit_feed] sub='%s' attempt=%d network error: %s", sub, attempt, exc)
            last_status = str(exc)
            if attempt < attempts:
                time.sleep(_RETRY_DELAYS[attempt - 1])
                continue
            logger.error("[fetch_posts.fetch_subreddit_feed] sub='%s' exhausted %d attempts, last error: %s",
                         sub, attempts, last_status)
            return None

        if response.status_code == 200:
            rate_info = {
                "remaining": response.headers.get("x-ratelimit-remaining"),
                "reset": response.headers.get("x-ratelimit-reset"),
            }
            logger.debug("[fetch_posts.fetch_subreddit_feed] sub='%s' rate remaining=%s reset=%s",
                         sub, rate_info["remaining"], rate_info["reset"])
            return response.text, rate_info

        last_status = response.status_code
        if response.status_code in _RETRYABLE_STATUSES:
            if attempt < attempts:
                logger.debug("[fetch_posts.fetch_subreddit_feed] sub='%s' attempt=%d status=%d, retrying",
                             sub, attempt, response.status_code)
                time.sleep(_RETRY_DELAYS[attempt - 1])
                continue
            logger.error("[fetch_posts.fetch_subreddit_feed] sub='%s' exhausted %d attempts, last status=%s",
                         sub, attempts, last_status)
            return None

        logger.warning("[fetch_posts.fetch_subreddit_feed] sub='%s' non-2xx status=%d, not retrying",
                        sub, response.status_code)
        return None

    return None  # unreachable: every branch above returns by the final attempt


def _entry_permalink(entry) -> str:
    """У entry обычно единственный <link> без rel — это permalink на пост.

    Если ссылок несколько, берём без rel-атрибута, иначе rel="alternate".
    """
    links = entry.findall("atom:link", _ATOM_NS)
    if not links:
        return ""
    for link_el in links:
        if link_el.get("rel") is None:
            return link_el.get("href", "")
    for link_el in links:
        if link_el.get("rel") == "alternate":
            return link_el.get("href", "")
    return links[0].get("href", "")


def parse_feed_entries(xml_text: str, sub: str) -> list:
    """Парсит Atom-фид (API-формат Reddit, не HTML-страница) в список сырых постов."""
    root = ET.fromstring(xml_text)
    entries = root.findall("atom:entry", _ATOM_NS)

    posts = []
    skipped = 0
    for entry in entries:
        id_el = entry.find("atom:id", _ATOM_NS)
        published_el = entry.find("atom:published", _ATOM_NS)
        updated_el = entry.find("atom:updated", _ATOM_NS)

        if id_el is None or id_el.text is None or (published_el is None and updated_el is None):
            skipped += 1
            logger.warning("[fetch_posts.parse_feed_entries] sub='%s' entry missing id or dates, skipping", sub)
            continue

        raw_id = id_el.text
        post_id = raw_id[len(_ID_PREFIX):] if raw_id.startswith(_ID_PREFIX) else raw_id

        date_text = published_el.text if published_el is not None else updated_el.text
        try:
            created_utc = datetime.fromisoformat(date_text).timestamp()
        except (ValueError, TypeError):
            skipped += 1
            logger.warning("[fetch_posts.parse_feed_entries] sub='%s' id='%s' unparseable date, skipping",
                           sub, post_id)
            continue

        permalink = _entry_permalink(entry)

        title_el = entry.find("atom:title", _ATOM_NS)
        title = title_el.text if title_el is not None and title_el.text else ""

        content_el = entry.find("atom:content", _ATOM_NS)
        selftext = _extract_selftext(content_el.text) if content_el is not None and content_el.text else ""

        posts.append({
            "id": post_id,
            "title": title,
            "selftext": selftext,
            "url": permalink,
            "permalink": permalink,
            "score": 0,
            "num_comments": 0,
            "created_utc": created_utc,
        })

    logger.debug("[fetch_posts.parse_feed_entries] sub='%s' parsed=%d skipped=%d", sub, len(posts), skipped)
    return posts


def filter_and_map_posts(raw_posts: list, sub: str, seen_ids: set, window_hours: int) -> list:
    cutoff = time.time() - window_hours * 3600
    kept = []
    dropped_seen = dropped_window = 0
    for post in raw_posts:
        post_id = post["id"]
        if post_id in seen_ids:
            dropped_seen += 1
            continue
        if post.get("created_utc", 0) < cutoff:
            dropped_window += 1
            continue

        kept.append({
            "id": post_id,
            "subreddit": sub,
            "title": post.get("title", ""),
            "selftext": post.get("selftext", ""),
            "url": post.get("url", ""),
            "permalink": post.get("permalink", ""),
            "score": post.get("score", 0),
            "num_comments": post.get("num_comments", 0),
            "created_utc": post.get("created_utc"),
        })

    logger.debug(
        "[fetch_posts.filter_and_map_posts] sub='%s' before=%d after=%d dropped_seen=%d dropped_window=%d",
        sub, len(raw_posts), len(kept), dropped_seen, dropped_window,
    )
    return kept


def _sleep_for_rate_limit(rate_info) -> None:
    remaining = rate_info.get("remaining") if rate_info else None
    reset = rate_info.get("reset") if rate_info else None
    if remaining is None or reset is None:
        logger.debug("[fetch_posts._sleep_for_rate_limit] no rate headers, conservative sleep=30")
        time.sleep(30)
        return
    try:
        remaining_f = float(remaining)
        reset_f = float(reset)
    except ValueError:
        logger.debug("[fetch_posts._sleep_for_rate_limit] unparseable rate headers remaining=%s reset=%s, "
                     "conservative sleep=30", remaining, reset)
        time.sleep(30)
        return

    if remaining_f < 1:
        delay = min(reset_f + 1, 60)
        logger.debug("[fetch_posts._sleep_for_rate_limit] remaining=%.1f reset=%.1f, sleeping %.1f",
                     remaining_f, reset_f, delay)
        time.sleep(delay)
    else:
        logger.debug("[fetch_posts._sleep_for_rate_limit] remaining=%.1f, using base pause=%d",
                     remaining_f, _SUB_PAUSE_SECONDS)
        time.sleep(_SUB_PAUSE_SECONDS)


def main() -> int:
    cfg = config.load_config()
    seen_ids = db.get_seen_post_ids()
    fetch_cfg = cfg["fetch"]
    paused = db.get_paused_subs()
    subreddits = [s for s in cfg["subreddits"] if s["name"] not in paused]

    if paused:
        skipped = [s["name"] for s in cfg["subreddits"] if s["name"] in paused]
        logger.info("[fetch_posts.main] paused subreddits skipped: %s", skipped)

    if not subreddits:
        logger.error("[fetch_posts.main] all subreddits are paused, nothing to fetch")
        return 1

    batch = []
    succeeded = []
    failed = []

    for index, sub_cfg in enumerate(subreddits):
        sub = sub_cfg["name"]
        result = fetch_subreddit_feed(sub, fetch_cfg["posts_per_sub_limit"], fetch_cfg["user_agent"])
        rate_info = None
        if result is None:
            failed.append(sub)
        else:
            xml_text, rate_info = result
            try:
                raw_posts = parse_feed_entries(xml_text, sub)
            except ET.ParseError as exc:
                logger.error("[fetch_posts.main] sub='%s' invalid XML: %s", sub, exc)
                failed.append(sub)
            else:
                posts = filter_and_map_posts(raw_posts, sub, seen_ids, fetch_cfg["window_hours"])
                batch.extend(posts)
                succeeded.append(sub)

        if index < len(subreddits) - 1:
            _sleep_for_rate_limit(rate_info)

    if not succeeded:
        logger.error("[fetch_posts.main] all %d subreddit(s) failed: %s", len(subreddits), failed)
        return 1

    logger.info(
        "[fetch_posts.main] batch size=%d succeeded=%s failed=%s",
        len(batch), succeeded, failed,
    )

    path = _batch_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(batch, f, ensure_ascii=False)
    logger.debug("[fetch_posts.main] wrote batch to %s", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
